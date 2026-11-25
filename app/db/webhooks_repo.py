"""持久化 Webhook Outbox 队列仓储（修复 P1-09、复核 P0-02 与 v3 复核 P0-01 租约 CAS）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app.config import env
from app.db.connection import DatabaseConnection

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def payload_has_code(event_type: str, payload: dict[str, Any]) -> bool:
    if event_type == "order.code_received":
        return True
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and str(data.get("latest_code") or "").strip():
        return True
    return False


def encode_outbox_payload(event_type: str, payload: dict[str, Any]) -> tuple[str, bool]:
    """序列化 Outbox 负载；含验证码的事件严格加密落盘，绝不降级明文。"""
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    has_code = payload_has_code(event_type, payload)
    if has_code:
        return f"e_{env.encrypt_value(raw_json)}", True
    return raw_json, False


def decode_outbox_payload(stored: str) -> str:
    """解码 Outbox 负载（若为加密密文则解密还原 JSON 文本）。"""
    if not stored:
        return ""
    if stored.startswith("e_"):
        return env.decrypt_value(stored[2:])
    return stored


class WebhooksRepo:
    def __init__(self, conn: DatabaseConnection):
        self._conn = conn

    async def enqueue(
        self,
        *,
        delivery_id: str,
        event_id: str,
        event_type: str,
        order_id: str,
        project_id: str,
        target_url: str,
        payload: dict[str, Any],
        max_attempts: int = 5,
    ) -> None:
        now = _utc_now().isoformat()
        stored_payload, contains_code = encode_outbox_payload(event_type, payload)
        await self._conn.write(
            "INSERT OR IGNORE INTO webhook_deliveries ("
            "delivery_id, event_id, event_type, order_id, project_id, target_url, payload_json, "
            "contains_code, payload_scrubbed, status, attempts, max_attempts, next_retry_at, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', 0, ?, ?, ?, ?)",
            (
                delivery_id,
                event_id,
                event_type,
                order_id,
                project_id,
                target_url,
                stored_payload,
                1 if contains_code else 0,
                max_attempts,
                now,
                now,
                now,
            ),
        )

    async def get(self, delivery_id: str) -> Optional[dict[str, Any]]:
        return await self._conn.fetchone(
            "SELECT * FROM webhook_deliveries WHERE delivery_id=?",
            (delivery_id,),
        )

    async def get_if_active_lease(self, delivery_id: str, expected_lease: str) -> Optional[dict[str, Any]]:
        """发送前复核投递行是否仍持有原租约且未被脱敏清理（修复 v3 复核 P0-01）。"""
        return await self._conn.fetchone(
            "SELECT * FROM webhook_deliveries "
            "WHERE delivery_id=? AND status='delivering' AND lease_expires_at=? AND payload_scrubbed=0",
            (delivery_id, expected_lease),
        )

    async def claim_due(self, *, limit: int = 8, lease_seconds: int = 30) -> list[dict[str, Any]]:
        """原子认领到期或租约过期的待投递 Webhook 记录。"""
        now = _utc_now()
        now_iso = now.isoformat()
        lease_iso = (now + timedelta(seconds=lease_seconds)).isoformat()
        claimed: list[dict[str, Any]] = []
        async with self._conn.transaction() as raw:
            cur = await raw.execute(
                "SELECT * FROM webhook_deliveries "
                "WHERE (status='pending' AND next_retry_at <= ?) "
                "   OR (status='delivering' AND lease_expires_at != '' AND lease_expires_at <= ?) "
                "ORDER BY created_at ASC LIMIT ?",
                (now_iso, now_iso, limit),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            for row in rows:
                upd = await raw.execute(
                    "UPDATE webhook_deliveries SET status='delivering', lease_expires_at=?, updated_at=? "
                    "WHERE delivery_id=? AND status=?",
                    (lease_iso, now_iso, row["delivery_id"], row["status"]),
                )
                if (upd.rowcount or 0) == 1:
                    row["status"] = "delivering"
                    row["lease_expires_at"] = lease_iso
                    claimed.append(row)
        return claimed

    async def mark_delivered(
        self,
        delivery_id: str,
        *,
        status_code: int,
        attempts: int,
        scrub_payload: bool = False,
        expected_lease: Optional[str] = None,
    ) -> bool:
        """按租约与状态条件写回 delivered，绝不将已被并发清理的 scrubbed 改回 delivered。"""
        now = _utc_now().isoformat()
        lease_clause = " AND status='delivering' AND lease_expires_at=?" if expected_lease is not None else " AND status != 'scrubbed'"
        lease_params: tuple[Any, ...] = (expected_lease,) if expected_lease is not None else ()
        if scrub_payload:
            cur = await self._conn.write(
                f"UPDATE webhook_deliveries SET status='delivered', attempts=?, last_status_code=?, "
                f"last_error='', lease_expires_at='', payload_json='{{}}', payload_scrubbed=1, "
                f"delivered_at=?, updated_at=? WHERE delivery_id=?{lease_clause}",
                (attempts, status_code, now, now, delivery_id, *lease_params),
            )
        else:
            cur = await self._conn.write(
                f"UPDATE webhook_deliveries SET status='delivered', attempts=?, last_status_code=?, "
                f"last_error='', lease_expires_at='', delivered_at=?, updated_at=? WHERE delivery_id=?{lease_clause}",
                (attempts, status_code, now, now, delivery_id, *lease_params),
            )
        return (cur.rowcount or 0) == 1

    async def mark_scrubbed(
        self,
        delivery_id: str,
        *,
        reason: str = "验证码已脱敏清理，取消投递",
    ) -> None:
        now = _utc_now().isoformat()
        await self._conn.write(
            "UPDATE webhook_deliveries SET status='scrubbed', payload_json='{}', payload_scrubbed=1, "
            "lease_expires_at='', last_error=?, updated_at=? WHERE delivery_id=?",
            (reason[:300], now, delivery_id),
        )

    async def mark_retry_or_failed(
        self,
        delivery_id: str,
        *,
        attempts: int,
        max_attempts: int,
        status_code: int,
        error: str,
        retry_delay_seconds: float,
        scrub_on_terminal: bool = False,
        expected_lease: Optional[str] = None,
    ) -> bool:
        """按租约与状态条件写回 pending/failed，防止覆盖并发脱敏的 scrubbed 状态。"""
        now = _utc_now()
        now_iso = now.isoformat()
        lease_clause = " AND status='delivering' AND lease_expires_at=?" if expected_lease is not None else " AND status != 'scrubbed'"
        lease_params: tuple[Any, ...] = (expected_lease,) if expected_lease is not None else ()
        if attempts >= max_attempts:
            if scrub_on_terminal:
                cur = await self._conn.write(
                    f"UPDATE webhook_deliveries SET status='failed', attempts=?, last_status_code=?, "
                    f"last_error=?, lease_expires_at='', payload_json='{{}}', payload_scrubbed=1, updated_at=? "
                    f"WHERE delivery_id=?{lease_clause}",
                    (attempts, status_code, error[:300], now_iso, delivery_id, *lease_params),
                )
            else:
                cur = await self._conn.write(
                    f"UPDATE webhook_deliveries SET status='failed', attempts=?, last_status_code=?, "
                    f"last_error=?, lease_expires_at='', updated_at=? WHERE delivery_id=?{lease_clause}",
                    (attempts, status_code, error[:300], now_iso, delivery_id, *lease_params),
                )
        else:
            next_iso = (now + timedelta(seconds=max(0.0, retry_delay_seconds))).isoformat()
            cur = await self._conn.write(
                f"UPDATE webhook_deliveries SET status='pending', attempts=?, last_status_code=?, "
                f"last_error=?, next_retry_at=?, lease_expires_at='', updated_at=? WHERE delivery_id=?{lease_clause}",
                (attempts, status_code, error[:300], next_iso, now_iso, delivery_id, *lease_params),
            )
        return (cur.rowcount or 0) == 1

    @staticmethod
    async def scrub_for_orders_in_tx(raw, order_ids: Iterable[str], now_iso: str) -> int:
        """在同一数据库事务中清空指定订单的 Outbox 验证码副本并使未发出的验证码事件失效。"""
        ids = [str(oid) for oid in order_ids if oid]
        if not ids:
            return 0
        ph = ",".join("?" for _ in ids)
        cur1 = await raw.execute(
            f"UPDATE webhook_deliveries "
            f"SET status='scrubbed', payload_json='{{}}', payload_scrubbed=1, lease_expires_at='', "
            f"    last_error='验证码已脱敏清理', updated_at=? "
            f"WHERE order_id IN ({ph}) AND contains_code=1 AND status IN ('pending', 'delivering')",
            (now_iso, *ids),
        )
        cur2 = await raw.execute(
            f"UPDATE webhook_deliveries "
            f"SET payload_json='{{}}', payload_scrubbed=1, updated_at=? "
            f"WHERE order_id IN ({ph}) AND (contains_code=1 OR event_type='order.code_received') "
            f"  AND payload_scrubbed=0",
            (now_iso, *ids),
        )
        return int(cur1.rowcount or 0) + int(cur2.rowcount or 0)

    async def recover_inflight(self) -> int:
        now = _utc_now().isoformat()
        cur = await self._conn.write(
            "UPDATE webhook_deliveries SET status='pending', lease_expires_at='', next_retry_at=?, updated_at=? "
            "WHERE status='delivering' AND payload_scrubbed=0",
            (now, now),
        )
        return int(cur.rowcount or 0)

    async def list(
        self,
        *,
        project_id: str = "",
        order_id: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if order_id:
            clauses.append("order_id=?")
            params.append(order_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._conn.fetchall(
            f"SELECT delivery_id, event_id, event_type, order_id, project_id, target_url, "
            f"contains_code, payload_scrubbed, status, attempts, max_attempts, next_retry_at, "
            f"last_status_code, last_error, created_at, updated_at, delivered_at "
            f"FROM webhook_deliveries{where_sql} ORDER BY created_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        )
        return rows
