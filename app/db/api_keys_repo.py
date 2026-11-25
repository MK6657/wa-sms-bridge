"""项目级 API Key 与永久项目身份仓储（修复 P1-06 严格加密、补充 P1-03 永久身份/轮转 & P1-05 统一统计）。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import env
from app.db.connection import DatabaseConnection

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encrypt_secret_strict(value: str) -> str:
    """严格加密敏感密钥：未配置 BRIDGE_SECRET_KEY 或加密失败时显式抛错，绝不降级明文落盘。"""
    if not value:
        return ""
    return f"e_{env.encrypt_value(value)}"


def _decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if value.startswith("e_"):
        try:
            return env.decrypt_value(value[2:])
        except Exception:
            logger.warning("decrypt webhook_secret failed")
            return ""
    return value


class ApiKeysRepo:
    def __init__(self, conn: DatabaseConnection):
        self._conn = conn

    def _hydrate(
        self,
        row: Optional[dict[str, Any]],
        *,
        include_secret: bool = False,
        include_hash: bool = False,
    ) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        item: dict[str, Any] = {
            "project_id": row["project_id"],
            "name": row["name"],
            "key_prefix": row["key_prefix"],
            "key_version": int(row.get("key_version") or 1),
            "scopes": [s.strip() for s in str(row.get("scopes") or "").split(",") if s.strip()],
            "default_webhook_url": row.get("default_webhook_url") or "",
            "has_webhook_secret": bool(row.get("webhook_secret_enc")),
            "enabled": bool(row.get("enabled")),
            "last_used_at": row.get("last_used_at") or "",
            "created_at": row.get("created_at") or "",
        }
        if include_secret:
            raw_enc = str(row.get("webhook_secret_enc") or "")
            if raw_enc.startswith("e_"):
                try:
                    item["webhook_secret"] = env.decrypt_value(raw_enc[2:])
                    item["webhook_secret_decrypt_error"] = False
                except Exception:
                    logger.warning("decrypt webhook_secret failed for project=%s", row.get("project_id"))
                    item["webhook_secret"] = ""
                    item["webhook_secret_decrypt_error"] = True
            else:
                item["webhook_secret"] = raw_enc
                item["webhook_secret_decrypt_error"] = False
        if include_hash:
            item["key_hash"] = str(row.get("key_hash") or "")
        return item

    async def project_exists(self, project_id: str) -> bool:
        """检查项目 ID 是否已被现存项目或任何历史已删除项目占用（修复补充 P1-03 永久身份防复用）。"""
        pid = (project_id or "").strip()
        if not pid:
            return False
        async with self._conn.read_transaction() as raw:
            for query in (
                "SELECT 1 FROM projects WHERE project_id=? LIMIT 1",
                "SELECT 1 FROM api_keys WHERE project_id=? LIMIT 1",
                "SELECT 1 FROM sms_orders WHERE project_id=? LIMIT 1",
                "SELECT 1 FROM blacklist WHERE project_id=? LIMIT 1",
                "SELECT 1 FROM webhook_deliveries WHERE project_id=? LIMIT 1",
            ):
                cur = await raw.execute(query, (pid,))
                if (await cur.fetchone()) is not None:
                    return True
        return False

    async def create(
        self,
        *,
        project_id: str,
        name: str,
        key_prefix: str,
        key_hash: str,
        scopes: list[str],
        default_webhook_url: str = "",
        webhook_secret: str = "",
    ) -> dict[str, Any]:
        now = _utc_now_iso()
        enc_secret = _encrypt_secret_strict(webhook_secret.strip())
        async with self._conn.transaction() as raw:
            await raw.execute(
                "INSERT INTO projects (project_id, name, status, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, ?)",
                (project_id, name, now, now),
            )
            await raw.execute(
                "INSERT INTO api_keys ("
                "project_id, name, key_prefix, key_hash, key_version, scopes, "
                "default_webhook_url, webhook_secret_enc, enabled, created_at"
                ") VALUES (?, ?, ?, ?, 1, ?, ?, ?, 1, ?)",
                (
                    project_id,
                    name,
                    key_prefix,
                    key_hash,
                    ",".join(scopes),
                    default_webhook_url.strip(),
                    enc_secret,
                    now,
                ),
            )
        created = await self.get(project_id)
        assert created is not None
        return created

    async def rotate_key(
        self,
        project_id: str,
        *,
        key_prefix: str,
        key_hash: str,
    ) -> Optional[dict[str, Any]]:
        """在原项目身份内原子轮转 API 密钥并递增 key_version（旧密钥与已连 SSE 立即失效）。"""
        now = _utc_now_iso()
        async with self._conn.transaction() as raw:
            cur = await raw.execute(
                "UPDATE api_keys SET key_prefix=?, key_hash=?, key_version=key_version+1 WHERE project_id=?",
                (key_prefix, key_hash, project_id),
            )
            if (cur.rowcount or 0) != 1:
                return None
            await raw.execute(
                "UPDATE projects SET updated_at=? WHERE project_id=?",
                (now, project_id),
            )
        return await self.get(project_id)

    async def get(
        self,
        project_id: str,
        *,
        include_secret: bool = False,
        include_hash: bool = False,
    ) -> Optional[dict[str, Any]]:
        row = await self._conn.fetchone("SELECT * FROM api_keys WHERE project_id=?", (project_id,))
        return self._hydrate(row, include_secret=include_secret, include_hash=include_hash)

    async def get_by_hash(self, key_hash: str) -> Optional[dict[str, Any]]:
        row = await self._conn.fetchone("SELECT * FROM api_keys WHERE key_hash=?", (key_hash,))
        return self._hydrate(row, include_secret=True, include_hash=True)

    async def list(self, *, project_summaries: Optional[dict[str, dict[str, Any]]] = None) -> list[dict[str, Any]]:
        """列出所有活跃项目密钥，并与统计接口共用同一财务聚合逻辑（修复补充 P1-05）。"""
        rows = await self._conn.fetchall("SELECT * FROM api_keys ORDER BY created_at DESC")
        if project_summaries is None:
            from app.db.orders_repo import OrdersRepo
            project_summaries = await OrdersRepo(self._conn).get_project_summaries()

        def _opt_round(val: Any) -> Optional[float]:
            if val is None:
                return None
            return round(float(val), 4)

        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._hydrate(row)
            if item is None:
                continue
            stat = project_summaries.get(item["project_id"], {})
            is_multi = bool(stat.get("is_multi_currency"))
            item["order_count"] = int(stat.get("order_count") or 0)
            item["received_count"] = int(stat.get("received_count") or 0)
            item["currency"] = stat.get("currency")
            item["is_multi_currency"] = is_multi
            item["actual_cost_total"] = None if is_multi else _opt_round(stat.get("actual_cost_total", 0.0))
            item["refunded_amount_total"] = None if is_multi else _opt_round(stat.get("refunded_amount_total", 0.0))
            item["refund_amount_unknown_count"] = int(stat.get("refund_amount_unknown_count") or 0)
            item["net_actual_cost_total"] = None if is_multi else _opt_round(stat.get("net_actual_cost_total", 0.0))
            item["estimated_cost_total"] = None if is_multi else _opt_round(stat.get("estimated_cost_total", 0.0))
            item["unknown_cost_count"] = int(stat.get("unknown_cost_count") or 0)
            item["cost_total"] = None if is_multi else _opt_round(stat.get("cost_total", 0.0))
            item["by_currency"] = list(stat.get("by_currency") or [])
            item["currency_review_count"] = int(stat.get("currency_review_count") or 0)
            item["currency_review_by_currency"] = list(stat.get("currency_review_by_currency") or [])
            item["fx_conversion"] = stat.get("fx_conversion")
            result.append(item)
        return result

    async def update(
        self,
        project_id: str,
        *,
        name: Optional[str] = None,
        enabled: Optional[bool] = None,
        scopes: Optional[list[str]] = None,
        default_webhook_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        sets: list[str] = []
        params: list[Any] = []
        bump_version = False
        if name is not None:
            sets.append("name=?")
            params.append(name.strip())
        if enabled is not None:
            sets.append("enabled=?")
            params.append(int(bool(enabled)))
            bump_version = True
        if scopes is not None:
            sets.append("scopes=?")
            params.append(",".join(scopes))
            bump_version = True
        if default_webhook_url is not None:
            sets.append("default_webhook_url=?")
            params.append(default_webhook_url.strip())
        if webhook_secret is not None:
            sets.append("webhook_secret_enc=?")
            params.append(_encrypt_secret_strict(webhook_secret.strip()))
        if not sets:
            return await self.get(project_id)
        if bump_version:
            sets.append("key_version=key_version+1")
        params.append(project_id)
        now = _utc_now_iso()
        async with self._conn.transaction() as raw:
            await raw.execute(f"UPDATE api_keys SET {', '.join(sets)} WHERE project_id=?", tuple(params))
            if name is not None:
                await raw.execute(
                    "UPDATE projects SET name=?, updated_at=? WHERE project_id=?",
                    (name.strip(), now, project_id),
                )
        return await self.get(project_id)

    async def touch_last_used(self, project_id: str) -> None:
        await self._conn.write(
            "UPDATE api_keys SET last_used_at=? WHERE project_id=?",
            (_utc_now_iso(), project_id),
        )

    async def delete(self, project_id: str) -> bool:
        """删除密钥并在 projects 表永久保留该 project_id（status='deleted'），防止新建同名项目继承历史数据。"""
        now = _utc_now_iso()
        async with self._conn.transaction() as raw:
            cur = await raw.execute("DELETE FROM api_keys WHERE project_id=?", (project_id,))
            deleted = (cur.rowcount or 0) > 0
            if deleted:
                await raw.execute(
                    "INSERT INTO projects (project_id, name, status, created_at, updated_at) "
                    "VALUES (?, ?, 'deleted', ?, ?) "
                    "ON CONFLICT(project_id) DO UPDATE SET status='deleted', updated_at=excluded.updated_at",
                    (project_id, project_id, now, now),
                )
            return deleted
