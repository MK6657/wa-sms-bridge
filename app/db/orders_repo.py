"""独立接码订单（sms_orders）与短信消息（sms_messages）持久化仓储。

修复项：
- P0-02: 分项目原子幂等占位 reserve() + request_hash 校验
- P1-04: 条件 CAS 状态迁移 transition_status() + append_code 前置状态校验
- P1-05: 区分 actual_cost / estimated / unknown 与已确认退款统计
- P1-06: 禁止验证码静默明文降级；独立验证码留存时钟（覆盖未手动关单的过期 code_received）
- P1-07: 持久化加密 resend_base_code 与 resend_seen_wait
- P1-09: 同事务写入 webhook_deliveries Outbox 队列
- P1-10: get_stats() 支持按 project_id 租户隔离
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.config import env
from app.db.connection import DatabaseConnection
from app.db.webhooks_repo import WebhooksRepo, encode_outbox_payload

logger = logging.getLogger(__name__)

ACTIVE_ORDER_STATUSES = (
    "acquiring",
    "waiting_code",
    "code_received",
    "waiting_next_code",
    "completing",
    "cancelling",
    "compensation_pending",
)
CLOSED_ORDER_STATUSES = ("completed", "cancelled", "expired", "failed")
RECOVERABLE_ORDER_STATUSES = (
    "acquiring",
    "waiting_code",
    "waiting_next_code",
    "completing",
    "cancelling",
    "compensation_pending",
)

ALLOWED_ORDER_UPDATE_FIELDS = frozenset({
    "provider",
    "service",
    "service_code",
    "country_id",
    "country_label",
    "country_iso2",
    "country_calling_code",
    "activation_id",
    "phone_raw",
    "phone_e164",
    "phone_national",
    "status",
    "settlement_state",
    "refund_state",
    "latest_code",
    "code_received_at",
    "code_scrubbed",
    "code_count",
    "resend_base_code",
    "resend_base_digest",
    "resend_seen_wait",
    "max_price",
    "quoted_max_price",
    "cost",
    "actual_cost",
    "cost_status",
    "cost_is_estimated",
    "currency",
    "wait_timeout",
    "expires_at",
    "attempts_used",
    "error_code",
    "error_message",
    "webhook_url",
    "metadata_json",
    "completed_at",
    "request_hash",
    "request_hash_version",
    "country_input",
    "idempotency_http_status",
    "idempotency_retryable",
    "idempotency_error_code",
    "idempotency_error_message",
    "async_acquire",
})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _encrypt_code_strict(plaintext: str) -> str:
    """严格加密验证码：缺失 BRIDGE_SECRET_KEY 或加密失败时显式报错，绝不降级明文落盘。"""
    if not plaintext:
        return ""
    return f"e_{env.encrypt_value(plaintext)}"


def _decrypt_code(stored: str) -> str:
    if not stored:
        return ""
    if stored.startswith("e_"):
        try:
            return env.decrypt_value(stored[2:])
        except Exception:
            logger.warning("decrypt sms code failed")
            return ""
    return stored


def _compute_resend_digest(order_id: str, code: str) -> str:
    if not order_id or not code:
        return ""
    return env.hmac_digest(f"resend:{order_id}", code)


def _encode_field(field_name: str, val: Any) -> Any:
    if field_name in {"latest_code", "resend_base_code"}:
        return _encrypt_code_strict(str(val or ""))
    if field_name in {"code_scrubbed", "cost_is_estimated", "resend_seen_wait", "idempotency_retryable", "async_acquire"}:
        return int(bool(val))
    return val


class OrdersRepo:
    def __init__(self, conn: DatabaseConnection):
        self._conn = conn

    def _hydrate_order(
        self,
        row: Optional[dict[str, Any]],
        messages: Optional[list[dict[str, Any]]] = None,
        activations: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        order = dict(row)
        order["code_scrubbed"] = bool(order.get("code_scrubbed"))
        order["code_scrub_version"] = int(order.get("code_scrub_version") or 0)
        order["cost_is_estimated"] = bool(order.get("cost_is_estimated"))
        order["resend_seen_wait"] = bool(order.get("resend_seen_wait"))
        order["idempotency_retryable"] = bool(order.get("idempotency_retryable"))
        order["async_acquire"] = bool(order.get("async_acquire"))
        order["request_hash_version"] = int(order.get("request_hash_version") or 1)
        order["idempotency_http_status"] = int(order.get("idempotency_http_status") or 0)
        order["idempotency_error_code"] = str(order.get("idempotency_error_code") or "")
        order["idempotency_error_message"] = str(order.get("idempotency_error_message") or "")
        order["latest_code"] = "" if order["code_scrubbed"] else _decrypt_code(str(order.get("latest_code") or ""))
        order["resend_base_code"] = "" if order["code_scrubbed"] else _decrypt_code(str(order.get("resend_base_code") or ""))
        order["resend_base_digest"] = str(order.get("resend_base_digest") or "")
        try:
            order["metadata"] = json.loads(order.get("metadata_json") or "{}")
        except Exception:
            order["metadata"] = {}
        order.pop("metadata_json", None)

        if messages is not None:
            order["messages"] = [
                {
                    "id": msg["id"],
                    "seq": msg["seq"],
                    "code": "" if order["code_scrubbed"] else _decrypt_code(str(msg.get("code") or "")),
                    "raw_text": "" if order["code_scrubbed"] else _decrypt_code(str(msg.get("raw_text") or "")),
                    "received_at": msg["received_at"],
                }
                for msg in messages
            ]
        else:
            order.setdefault("messages", [])

        if activations is not None:
            order["activations"] = activations
        else:
            order.setdefault("activations", [])
        return order

    async def reserve(
        self,
        *,
        order_id: str,
        provider: str,
        service: str,
        service_code: str,
        country_id: str,
        country_input: str = "",
        country_label: str = "",
        country_iso2: str = "",
        country_calling_code: str = "",
        project_id: str = "web-console",
        external_ref: str = "",
        idempotency_key: Optional[str] = None,
        request_hash: str = "",
        request_hash_version: int = 2,
        idempotency_http_status: int = 0,
        idempotency_retryable: bool = False,
        idempotency_error_code: str = "",
        idempotency_error_message: str = "",
        async_acquire: bool = False,
        max_price: Optional[float] = None,
        wait_timeout: int = 180,
        webhook_url: str = "",
        metadata: Optional[dict[str, Any]] = None,
        status: str = "acquiring",
    ) -> tuple[dict[str, Any], bool]:
        """原子创建/占位订单，返回 (order, created_new)。

        当同项目同 idempotency_key 并发插入冲突时，安全返回已占位记录而不抛未捕获 IntegrityError。
        """
        now = _utc_now_iso()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        pid = project_id or "web-console"
        idem = (idempotency_key or "").strip() or None
        initial_http = int(idempotency_http_status) if idempotency_http_status else (202 if async_acquire else 0)

        try:
            await self._conn.write(
                "INSERT INTO sms_orders ("
                "order_id, idempotency_key, request_hash, request_hash_version, project_id, external_ref, provider, "
                "service, service_code, country_id, country_input, country_label, country_iso2, country_calling_code, "
                "status, max_price, quoted_max_price, wait_timeout, webhook_url, metadata_json, "
                "idempotency_http_status, idempotency_retryable, idempotency_error_code, idempotency_error_message, "
                "async_acquire, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id, idem, request_hash, int(request_hash_version), pid, external_ref or "", provider,
                    service, service_code, str(country_id), country_input or str(country_id), country_label,
                    country_iso2, country_calling_code, status, max_price, max_price, int(wait_timeout),
                    webhook_url or "", meta_json, initial_http, int(bool(idempotency_retryable)),
                    idempotency_error_code or "", idempotency_error_message or "",
                    int(bool(async_acquire)), now, now,
                ),
            )
            created = await self.get(order_id, include_messages=True, include_activations=True)
            assert created is not None
            return created, True
        except sqlite3.IntegrityError:
            if not idem:
                raise
            existing = await self.get_by_idempotency_key(pid, idem)
            if existing is None:
                raise
            return existing, False

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        order, _ = await self.reserve(**kwargs)
        return order

    async def get(
        self,
        order_id: str,
        *,
        include_messages: bool = True,
        include_activations: bool = False,
    ) -> Optional[dict[str, Any]]:
        """在单个只读事务快照内读取订单、短信和激活明细（修复补充 P1-04）。"""
        async with self._conn.read_transaction() as raw:
            cur = await raw.execute("SELECT * FROM sms_orders WHERE order_id=?", (order_id,))
            row_raw = await cur.fetchone()
            if row_raw is None:
                return None
            row = dict(row_raw)
            messages: Optional[list[dict[str, Any]]] = None
            activations: Optional[list[dict[str, Any]]] = None
            if include_messages:
                mcur = await raw.execute(
                    "SELECT id, order_id, seq, code, raw_text, received_at "
                    "FROM sms_messages WHERE order_id=? ORDER BY seq ASC, id ASC",
                    (order_id,),
                )
                messages = [dict(r) for r in await mcur.fetchall()]
            if include_activations:
                acur = await raw.execute(
                    "SELECT id, order_id, attempt_no, provider, country_id, service_code, activation_id, "
                    "phone_raw, phone_e164, state, quoted_max_price, actual_cost, estimated_cost, cost_status, "
                    "currency, currency_source, currency_review_required, refund_amount, refund_amount_status, "
                    "refund_amount_source, last_error, created_at, updated_at "
                    "FROM order_activations WHERE order_id=? ORDER BY attempt_no ASC, id ASC",
                    (order_id,),
                )
                activations = [dict(r) for r in await acur.fetchall()]
        return self._hydrate_order(row, messages, activations)

    async def get_by_idempotency_key(self, project_id: str, key: str) -> Optional[dict[str, Any]]:
        if not key:
            return None
        async with self._conn.read_transaction() as raw:
            cur = await raw.execute(
                "SELECT * FROM sms_orders WHERE project_id=? AND idempotency_key=?",
                (project_id or "web-console", key),
            )
            row_raw = await cur.fetchone()
            if row_raw is None:
                return None
            row = dict(row_raw)
            oid = str(row["order_id"])
            mcur = await raw.execute(
                "SELECT id, order_id, seq, code, raw_text, received_at "
                "FROM sms_messages WHERE order_id=? ORDER BY seq ASC, id ASC",
                (oid,),
            )
            messages = [dict(r) for r in await mcur.fetchall()]
            acur = await raw.execute(
                "SELECT id, order_id, attempt_no, provider, country_id, service_code, activation_id, "
                "phone_raw, phone_e164, state, quoted_max_price, actual_cost, estimated_cost, cost_status, "
                "currency, currency_source, currency_review_required, refund_amount, refund_amount_status, "
                "refund_amount_source, last_error, created_at, updated_at "
                "FROM order_activations WHERE order_id=? ORDER BY attempt_no ASC, id ASC",
                (oid,),
            )
            activations = [dict(r) for r in await acur.fetchall()]
        return self._hydrate_order(row, messages, activations)

    async def list_messages(self, order_id: str) -> list[dict[str, Any]]:
        return await self._conn.fetchall(
            "SELECT id, order_id, seq, code, raw_text, received_at "
            "FROM sms_messages WHERE order_id=? ORDER BY seq ASC, id ASC",
            (order_id,),
        )

    async def list_activations(self, order_id: str) -> list[dict[str, Any]]:
        return await self._conn.fetchall(
            "SELECT id, order_id, attempt_no, provider, country_id, service_code, activation_id, "
            "phone_raw, phone_e164, state, quoted_max_price, actual_cost, estimated_cost, cost_status, "
            "currency, currency_source, currency_review_required, refund_amount, refund_amount_status, "
            "refund_amount_source, last_error, created_at, updated_at "
            "FROM order_activations WHERE order_id=? ORDER BY attempt_no ASC, id ASC",
            (order_id,),
        )

    async def update(
        self,
        order_id: str,
        *,
        outbox_entry: Optional[dict[str, Any]] = None,
        include_messages: bool = True,
        include_activations: bool = True,
        **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        if not kwargs and not outbox_entry:
            return await self.get(order_id, include_messages=include_messages, include_activations=include_activations)
        sets: list[str] = []
        values: list[Any] = []
        for field_name, val in kwargs.items():
            if field_name not in ALLOWED_ORDER_UPDATE_FIELDS:
                raise ValueError(f"Unsupported sms_orders column: {field_name}")
            sets.append(f"{field_name}=?")
            values.append(_encode_field(field_name, val))
        now = _utc_now_iso()
        sets.append("version=version+1")
        sets.append("updated_at=?")
        values.append(now)
        values.append(order_id)

        async with self._conn.transaction() as raw:
            await raw.execute(f"UPDATE sms_orders SET {', '.join(sets)} WHERE order_id=?", tuple(values))
            if outbox_entry:
                await self._insert_outbox_in_tx(raw, outbox_entry, now)

        return await self.get(order_id, include_messages=include_messages, include_activations=include_activations)

    async def transition_status(
        self,
        order_id: str,
        *,
        expected_statuses: tuple[str, ...],
        new_status: str,
        outbox_entry: Optional[dict[str, Any]] = None,
        tx_hook=None,
        scrub_in_tx: bool = False,
        scrub_result: Optional[dict[str, int]] = None,
        on_scrub_committed: Optional[Callable[[dict[str, int]], None]] = None,
        **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        """条件 CAS 原子状态切换：同事务完成订单切换、激活台账同步（tx_hook）、可选即刻脱敏与 Outbox 入队。"""
        if not expected_statuses:
            raise ValueError("expected_statuses cannot be empty")
        sets = ["status=?", "version=version+1", "updated_at=?"]
        now = _utc_now_iso()
        values: list[Any] = [new_status, now]
        for field_name, val in kwargs.items():
            if field_name not in ALLOWED_ORDER_UPDATE_FIELDS:
                raise ValueError(f"Unsupported sms_orders column: {field_name}")
            sets.append(f"{field_name}=?")
            values.append(_encode_field(field_name, val))

        if new_status in CLOSED_ORDER_STATUSES and "resend_base_digest" not in kwargs:
            sets.append("resend_base_digest=''")

        placeholders = ",".join("?" for _ in expected_statuses)
        values.append(order_id)
        values.extend(expected_statuses)
        committed_scrub: dict[str, int] = {}

        async with self._conn.transaction() as raw:
            cur = await raw.execute(
                f"UPDATE sms_orders SET {', '.join(sets)} WHERE order_id=? AND status IN ({placeholders})",
                tuple(values),
            )
            if (cur.rowcount or 0) != 1:
                return None
            if tx_hook is not None:
                await tx_hook(raw, now)
            if scrub_in_tx:
                sc_cur = await raw.execute(
                    "SELECT code_scrubbed, latest_code, code_count, code_scrub_version "
                    "FROM sms_orders WHERE order_id=?",
                    (order_id,),
                )
                sc_row = await sc_cur.fetchone()
                if sc_row is not None and not int(sc_row["code_scrubbed"] or 0) and (
                    str(sc_row["latest_code"] or "") != "" or int(sc_row["code_count"] or 0) > 0
                ):
                    new_ver = int(sc_row["code_scrub_version"] or 0) + 1
                    await raw.execute(
                        "UPDATE sms_orders SET latest_code='', resend_base_code='', resend_base_digest='', code_scrubbed=1, "
                        "code_scrub_version=?, updated_at=? WHERE order_id=?",
                        (new_ver, now, order_id),
                    )
                    await raw.execute(
                        "UPDATE sms_messages SET code='', raw_text='' WHERE order_id=?",
                        (order_id,),
                    )
                    await WebhooksRepo.scrub_for_orders_in_tx(raw, [order_id], now)
                    committed_scrub[order_id] = new_ver
                    if scrub_result is not None:
                        scrub_result[order_id] = new_ver
            if outbox_entry:
                await self._insert_outbox_in_tx(raw, outbox_entry, now)

        if committed_scrub and on_scrub_committed is not None:
            on_scrub_committed(committed_scrub)
        return await self.get(order_id, include_messages=True, include_activations=True)

    async def append_code(
        self,
        order_id: str,
        code: str,
        raw_text: str = "",
        *,
        expected_scrub_version: Optional[int] = None,
        outbox_entry_builder=None,
    ) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
        """仅当订单处于 waiting_code / waiting_next_code 且脱敏版本匹配时原子写入新验证码（修复 P1-04 & v6 复核 P0-01）。"""
        now = _utc_now_iso()
        enc_code = _encrypt_code_strict(code)
        enc_raw = _encrypt_code_strict(raw_text or code)
        code_digest = _compute_resend_digest(order_id, code)
        commit_scrub_version = 0
        async with self._conn.transaction() as raw:
            state_cur = await raw.execute(
                "SELECT status, webhook_url, project_id, code_scrub_version FROM sms_orders WHERE order_id=?",
                (order_id,),
            )
            st_row = await state_cur.fetchone()
            if not st_row or st_row["status"] not in {"waiting_code", "waiting_next_code"}:
                return None

            current_scrub_ver = int(st_row["code_scrub_version"] or 0)
            if expected_scrub_version is not None and current_scrub_ver != int(expected_scrub_version):
                # 观察到该验证码之后订单已触发脱敏，旧版本待写码彻底失效（绝不把旧码写回库或入队 Webhook！）
                return None
            commit_scrub_version = current_scrub_ver

            cur = await raw.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM sms_messages WHERE order_id=?",
                (order_id,),
            )
            row = await cur.fetchone()
            next_seq = int((row["max_seq"] if row else 0) or 0) + 1

            upd = await raw.execute(
                "UPDATE sms_orders SET status='code_received', version=version+1, "
                "latest_code=?, code_received_at=?, code_scrubbed=0, code_count=?, "
                "resend_base_code='', resend_base_digest=?, resend_seen_wait=0, error_code='', error_message='', updated_at=? "
                "WHERE order_id=? AND status IN ('waiting_code', 'waiting_next_code') AND code_scrub_version=?",
                (enc_code, now, next_seq, code_digest, now, order_id, commit_scrub_version),
            )
            if (upd.rowcount or 0) != 1:
                return None

            msg_cur = await raw.execute(
                "INSERT INTO sms_messages (order_id, seq, code, raw_text, received_at) VALUES (?, ?, ?, ?, ?)",
                (order_id, next_seq, enc_code, enc_raw, now),
            )
            msg_id = int(msg_cur.lastrowid or 0)

            if outbox_entry_builder is not None:
                entry = outbox_entry_builder(next_seq, now)
                if entry:
                    await self._insert_outbox_in_tx(raw, entry, now)

        order = await self.get(order_id, include_messages=True, include_activations=True)
        assert order is not None
        # v6 复核 P0-01: 若提交与回读窗口之间并发执行了脱敏清理，绝不在返回的 message 中残留明文旧码！
        scrubbed_after_commit = bool(order.get("code_scrubbed")) or (
            int(order.get("code_scrub_version") or 0) != commit_scrub_version
        )
        message = {
            "id": msg_id,
            "seq": next_seq,
            "code": "" if scrubbed_after_commit else code,
            "raw_text": "" if scrubbed_after_commit else (raw_text or code),
            "received_at": now,
            "scrub_version": commit_scrub_version,
        }
        return order, message

    @staticmethod
    async def _insert_outbox_in_tx(raw, entry: dict[str, Any], now_iso: str) -> None:
        stored_payload, contains_code = encode_outbox_payload(entry["event_type"], entry["payload"])
        await raw.execute(
            "INSERT OR IGNORE INTO webhook_deliveries ("
            "delivery_id, event_id, event_type, order_id, project_id, target_url, payload_json, "
            "contains_code, payload_scrubbed, status, attempts, max_attempts, next_retry_at, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', 0, ?, ?, ?, ?)",
            (
                entry["delivery_id"],
                entry["event_id"],
                entry["event_type"],
                entry.get("order_id", ""),
                entry.get("project_id", ""),
                entry["target_url"],
                stored_payload,
                1 if contains_code else 0,
                int(entry.get("max_attempts", 5)),
                now_iso,
                now_iso,
                now_iso,
            ),
        )

    async def list(
        self,
        *,
        status: str = "",
        active_only: bool = False,
        project_id: str = "",
        service: str = "",
        provider: str = "",
        search: str = "",
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
            clauses.append(f"status IN ({placeholders})")
            params.extend(ACTIVE_ORDER_STATUSES)
        elif status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            if len(statuses) == 1:
                clauses.append("status=?")
                params.append(statuses[0])
            elif statuses:
                placeholders = ",".join("?" for _ in statuses)
                clauses.append(f"status IN ({placeholders})")
                params.extend(statuses)
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if service:
            clauses.append("(service=? OR service_code=?)")
            params.extend([service, service])
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if search.strip():
            like = f"%{search.strip()}%"
            clauses.append(
                "(order_id LIKE ? OR phone_e164 LIKE ? OR phone_raw LIKE ? OR "
                "country_label LIKE ? OR external_ref LIKE ? OR service LIKE ?)"
            )
            params.extend([like, like, like, like, like, like])

        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        safe_page = max(1, int(page))
        safe_size = max(1, min(int(page_size), 200))
        offset = (safe_page - 1) * safe_size

        # 补充 P1-04: 总数、分页列表与短信明细在单个只读快照事务内读取，确保视图一致
        async with self._conn.read_transaction() as raw:
            cnt_cur = await raw.execute(f"SELECT COUNT(*) AS total FROM sms_orders{where_sql}", tuple(params))
            count_raw = await cnt_cur.fetchone()
            total = int((dict(count_raw) if count_raw else {}).get("total") or 0)

            rows_cur = await raw.execute(
                f"SELECT * FROM sms_orders{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, safe_size, offset),
            )
            rows = [dict(r) for r in await rows_cur.fetchall()]

            order_ids = [str(r["order_id"]) for r in rows]
            messages_by_order: dict[str, list[dict[str, Any]]] = {oid: [] for oid in order_ids}
            if order_ids:
                placeholders = ",".join("?" for _ in order_ids)
                msg_cur = await raw.execute(
                    f"SELECT id, order_id, seq, code, raw_text, received_at "
                    f"FROM sms_messages WHERE order_id IN ({placeholders}) ORDER BY seq ASC, id ASC",
                    tuple(order_ids),
                )
                for msg in (dict(m) for m in await msg_cur.fetchall()):
                    messages_by_order.setdefault(str(msg["order_id"]), []).append(msg)

        items = [
            self._hydrate_order(r, messages_by_order.get(str(r["order_id"]), []))
            for r in rows
        ]
        return {
            "items": [item for item in items if item is not None],
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
        }

    async def list_recoverable(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in RECOVERABLE_ORDER_STATUSES)
        rows = await self._conn.fetchall(
            f"SELECT * FROM sms_orders WHERE status IN ({placeholders}) ORDER BY created_at ASC",
            RECOVERABLE_ORDER_STATUSES,
        )
        return [self._hydrate_order(r, []) for r in rows if r is not None]  # type: ignore[misc]

    async def scrub_order_code_with_version(
        self,
        order_id: str,
        *,
        on_scrub_committed: Optional[Callable[[dict[str, int]], None]] = None,
    ) -> Optional[int]:
        """原子脱敏单笔订单并递增 code_scrub_version，返回新的脱敏版本号（不存在则返回 None）。"""
        now = _utc_now_iso()
        closed_ph = ",".join("?" for _ in CLOSED_ORDER_STATUSES)
        new_ver: Optional[int] = None
        async with self._conn.transaction() as raw:
            pre_cur = await raw.execute(
                "SELECT status, latest_code, resend_base_digest FROM sms_orders WHERE order_id=?",
                (order_id,),
            )
            pre_row = await pre_cur.fetchone()
            if pre_row is None:
                return None
            computed_digest = str(pre_row["resend_base_digest"] or "")
            if not computed_digest and str(pre_row["status"] or "") not in CLOSED_ORDER_STATUSES:
                plain_lc = _decrypt_code(str(pre_row["latest_code"] or ""))
                if plain_lc:
                    computed_digest = _compute_resend_digest(order_id, plain_lc)

            cur = await raw.execute(
                f"UPDATE sms_orders SET latest_code='', resend_base_code='', "
                f"resend_base_digest=CASE WHEN status IN ({closed_ph}) THEN '' ELSE ? END, "
                f"code_scrubbed=1, code_scrub_version=code_scrub_version+1, updated_at=? WHERE order_id=?",
                (*CLOSED_ORDER_STATUSES, computed_digest, now, order_id),
            )
            if (cur.rowcount or 0) <= 0:
                return None
            await raw.execute(
                "UPDATE sms_messages SET code='', raw_text='' WHERE order_id=?",
                (order_id,),
            )
            await WebhooksRepo.scrub_for_orders_in_tx(raw, [order_id], now)
            v_cur = await raw.execute(
                "SELECT code_scrub_version FROM sms_orders WHERE order_id=?",
                (order_id,),
            )
            v_row = await v_cur.fetchone()
            new_ver = int((v_row["code_scrub_version"] if v_row else 1) or 1)

        if new_ver is not None and on_scrub_committed is not None:
            on_scrub_committed({order_id: new_ver})
        return new_ver

    async def scrub_order_code(self, order_id: str) -> bool:
        return (await self.scrub_order_code_with_version(order_id)) is not None

    async def scrub_expired_codes_detailed(
        self,
        retention_hours: int,
        *,
        on_scrub_committed: Optional[Callable[[dict[str, int]], None]] = None,
    ) -> tuple[dict[str, int], set[str]]:
        """分批原子清理超期验证码，保留在途订单重发防重摘要，并返回 (versions, non_terminal_ids)。"""
        if retention_hours < 0:
            return {}, set()
        now = _utc_now()
        now_iso = now.isoformat()
        cutoff_iso = (now - timedelta(hours=retention_hours)).isoformat()
        closed_ph = ",".join("?" for _ in CLOSED_ORDER_STATUSES)
        pending_statuses = ("compensation_pending", "cancelling", "completing")
        pending_ph = ",".join("?" for _ in pending_statuses)
        chunk_limit = 500

        versions: dict[str, int] = {}
        non_terminal_ids: set[str] = set()

        while True:
            chunk_versions: dict[str, int] = {}
            async with self._conn.transaction() as raw:
                closed_cur = await raw.execute(
                    f"SELECT order_id, status, latest_code, resend_base_digest FROM sms_orders "
                    f"WHERE status IN ({closed_ph}) AND code_scrubbed=0 AND (latest_code != '' OR code_count > 0) "
                    f"AND strftime('%Y-%m-%dT%H:%M:%f', COALESCE(NULLIF(completed_at, ''), updated_at)) "
                    f" <= strftime('%Y-%m-%dT%H:%M:%f', ?) LIMIT ?",
                    (*CLOSED_ORDER_STATUSES, cutoff_iso, chunk_limit),
                )
                closed_rows = await closed_cur.fetchall()

                rem_after_closed = max(0, chunk_limit - len(closed_rows))
                pending_rows = []
                if rem_after_closed > 0:
                    pending_cur = await raw.execute(
                        f"SELECT order_id, status, latest_code, resend_base_digest FROM sms_orders "
                        f"WHERE status IN ({pending_ph}) AND code_scrubbed=0 AND (latest_code != '' OR code_count > 0) "
                        f"AND strftime('%Y-%m-%dT%H:%M:%f', COALESCE(NULLIF(code_received_at, ''), updated_at)) "
                        f" <= strftime('%Y-%m-%dT%H:%M:%f', ?) LIMIT ?",
                        (*pending_statuses, cutoff_iso, rem_after_closed),
                    )
                    pending_rows = await pending_cur.fetchall()

                rem_after_pending = max(0, rem_after_closed - len(pending_rows))
                open_rows = []
                if rem_after_pending > 0:
                    if retention_hours > 0:
                        open_cur = await raw.execute(
                            "SELECT order_id, status, latest_code, resend_base_digest FROM sms_orders "
                            "WHERE status='code_received' AND code_scrubbed=0 AND (latest_code != '' OR code_count > 0) "
                            "AND strftime('%Y-%m-%dT%H:%M:%f', COALESCE(NULLIF(code_received_at, ''), updated_at)) "
                            " <= strftime('%Y-%m-%dT%H:%M:%f', ?) LIMIT ?",
                            (cutoff_iso, rem_after_pending),
                        )
                    else:
                        open_cur = await raw.execute(
                            "SELECT order_id, status, latest_code, resend_base_digest FROM sms_orders "
                            "WHERE status='code_received' AND code_scrubbed=0 AND (latest_code != '' OR code_count > 0) "
                            "AND ((expires_at != '' AND strftime('%Y-%m-%dT%H:%M:%f', expires_at) <= strftime('%Y-%m-%dT%H:%M:%f', ?)) "
                            "     OR (expires_at = '' AND strftime('%Y-%m-%dT%H:%M:%f', COALESCE(NULLIF(code_received_at, ''), updated_at)) "
                            "         <= strftime('%Y-%m-%dT%H:%M:%f', ?))) LIMIT ?",
                            (now_iso, (now - timedelta(minutes=15)).isoformat(), rem_after_pending),
                        )
                    open_rows = await open_cur.fetchall()

                candidate_rows = (*closed_rows, *pending_rows, *open_rows)
                if not candidate_rows:
                    break

                seen_batch: dict[str, dict[str, Any]] = {}
                for r in candidate_rows:
                    oid = str(r["order_id"])
                    if oid not in seen_batch:
                        seen_batch[oid] = dict(r)
                ids = list(seen_batch.keys())

                # 对非终态订单（如 code_received）若尚无 resend_base_digest，先补算 HMAC 摘要再擦除明文验证码，防止后续重发误收旧码！
                for oid, rdict in seen_batch.items():
                    st = str(rdict.get("status") or "")
                    if st not in CLOSED_ORDER_STATUSES:
                        non_terminal_ids.add(oid)
                        if not str(rdict.get("resend_base_digest") or ""):
                            plain_lc = _decrypt_code(str(rdict.get("latest_code") or ""))
                            if plain_lc:
                                await raw.execute(
                                    "UPDATE sms_orders SET resend_base_digest=? WHERE order_id=?",
                                    (_compute_resend_digest(oid, plain_lc), oid),
                                )

                id_ph = ",".join("?" for _ in ids)
                await raw.execute(
                    f"UPDATE sms_orders SET latest_code='', resend_base_code='', "
                    f"resend_base_digest=CASE WHEN status IN ({closed_ph}) THEN '' ELSE resend_base_digest END, "
                    f"code_scrubbed=1, code_scrub_version=code_scrub_version+1, updated_at=? "
                    f"WHERE order_id IN ({id_ph})",
                    (*CLOSED_ORDER_STATUSES, now_iso, *ids),
                )
                await raw.execute(
                    f"UPDATE sms_messages SET code='', raw_text='' WHERE order_id IN ({id_ph})",
                    tuple(ids),
                )
                await WebhooksRepo.scrub_for_orders_in_tx(raw, ids, now_iso)
                v_cur = await raw.execute(
                    f"SELECT order_id, code_scrub_version FROM sms_orders WHERE order_id IN ({id_ph})",
                    tuple(ids),
                )
                for row in await v_cur.fetchall():
                    chunk_versions[str(row["order_id"])] = int(row["code_scrub_version"] or 1)

            versions.update(chunk_versions)
            if chunk_versions and on_scrub_committed is not None:
                on_scrub_committed(chunk_versions)
            if len(chunk_versions) < chunk_limit:
                break

        return versions, non_terminal_ids

    async def scrub_expired_codes_with_versions(
        self,
        retention_hours: int,
        *,
        on_scrub_committed: Optional[Callable[[dict[str, int]], None]] = None,
    ) -> dict[str, int]:
        """独立验证码留存清理：返回 {order_id: new_version}。"""
        versions, _ = await self.scrub_expired_codes_detailed(
            retention_hours,
            on_scrub_committed=on_scrub_committed,
        )
        return versions

    async def scrub_expired_codes_with_ids(self, retention_hours: int) -> list[str]:
        return list((await self.scrub_expired_codes_with_versions(retention_hours)).keys())

    async def scrub_expired_codes(self, retention_hours: int) -> int:
        return len(await self.scrub_expired_codes_with_versions(retention_hours))

    @staticmethod
    async def _collect_financial_rows(
        raw,
        *,
        since_iso: Optional[str] = None,
        project_id: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """在当前只读快照事务中同时提取订单摘要与精确财务条目（激活台账优先，补建缺实付台账按同激活合并父单实付，UTC 精确边界过滤）。"""
        ord_clauses: list[str] = []
        ord_params: list[Any] = []
        if since_iso is not None:
            # v6 复核 P2-04: 统一转为 UTC 规范时间 ('YYYY-MM-DDTHH:MM:SS.SSS') 比较，
            # 完整兼容 SQLite 空格时间、零点边界、Z 后缀与带时区偏移格式！
            ord_clauses.append("strftime('%Y-%m-%dT%H:%M:%f', o.created_at) >= strftime('%Y-%m-%dT%H:%M:%f', ?)")
            ord_params.append(since_iso)
        if project_id:
            ord_clauses.append("o.project_id = ?")
            ord_params.append(project_id)
        where_ord = f" WHERE {' AND '.join(ord_clauses)}" if ord_clauses else ""

        ord_cur = await raw.execute(
            f"""
            SELECT
                o.order_id, o.project_id, o.service, o.service_code, o.status, o.refund_state,
                o.activation_id, o.code_count, o.cost, o.actual_cost, o.cost_status,
                strftime('%Y-%m-%dT%H:%M:%f', o.created_at) AS _norm_created_utc,
                COALESCE(NULLIF(UPPER(TRIM(o.currency)), ''), 'UNKNOWN') AS currency,
                CASE
                    WHEN o.code_count > 0 OR EXISTS (SELECT 1 FROM sms_messages m WHERE m.order_id = o.order_id)
                    THEN 1 ELSE 0
                END AS has_sms,
                EXISTS (
                    SELECT 1 FROM order_activations oa
                    WHERE oa.order_id = o.order_id
                      AND (
                          COALESCE(oa.activation_id, '') != ''
                          OR oa.actual_cost IS NOT NULL
                          OR oa.refund_amount IS NOT NULL
                          OR oa.state IN ('completed', 'unrefundable', 'refund_confirmed')
                      )
                ) AS has_act_ledger
            FROM sms_orders o
            {where_ord}
            """,
            tuple(ord_params),
        )
        orders = [dict(r) for r in await ord_cur.fetchall()]

        act_cur = await raw.execute(
            f"""
            SELECT
                oa.order_id, o.project_id, o.service, o.service_code,
                strftime('%Y-%m-%dT%H:%M:%f', o.created_at) AS _norm_created_utc,
                oa.activation_id, oa.state, oa.actual_cost, oa.estimated_cost, oa.quoted_max_price,
                oa.cost_status, oa.refund_amount, oa.refund_amount_status,
                oa.currency_review_required,
                o.activation_id AS order_activation_id,
                o.actual_cost AS order_actual_cost,
                o.cost AS order_cost,
                o.cost_status AS order_cost_status,
                COALESCE(
                    NULLIF(UPPER(TRIM(oa.currency)), ''),
                    NULLIF(UPPER(TRIM(o.currency)), ''),
                    'UNKNOWN'
                ) AS currency,
                CASE
                    WHEN o.code_count > 0 OR EXISTS (SELECT 1 FROM sms_messages m WHERE m.order_id = o.order_id)
                    THEN 1 ELSE 0
                END AS order_has_sms
            FROM order_activations oa
            JOIN sms_orders o ON o.order_id = oa.order_id
            {where_ord}
            {"AND" if where_ord else "WHERE"} (
                COALESCE(oa.activation_id, '') != ''
                OR oa.actual_cost IS NOT NULL
                OR oa.refund_amount IS NOT NULL
                OR oa.state IN ('completed', 'unrefundable', 'refund_confirmed')
            )
            """,
            tuple(ord_params),
        )
        activations = [dict(r) for r in await act_cur.fetchall()]
        return orders, activations

    @staticmethod
    def _aggregate_financials(
        orders: list[dict[str, Any]],
        activations: list[dict[str, Any]],
        *,
        fx_rates: Optional[dict[str, float]] = None,
        fx_target_currency: Optional[str] = None,
        fx_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """统一财务聚合口径（修复补充 P1-05、v5 P1-04/P1-05、v7 P1-03 及估算成本退款负净支出缺陷）：
        - 收码率仅认实际短信记录 (has_sms == 1)，零短信完成单不计入收码；
        - 确认未购买的尝试 (no_numbers / failed 且无 activation_id) 不计入估算支出；
        - 退款金额仅在有核实证据 (refund_amount IS NOT NULL) 时计入 refunded_amount_total，
          且仅当对应激活计入了 actual_cost 时才扣减 net_actual_cost_total（杜绝 SMSBower 等估算成本单退款产生负净支出或冲抵其他订单）；
        - 标为 currency_review_required=1 的待核对币种记录单独归入 currency_review_by_currency，绝不混入已核实 by_currency 金额；
        - 按币种分别汇总 (by_currency)，绝不将不同币种直接相加；跨币种折算仅在显式提供汇率、目标币种与折算日期且无待核对币种时生成。
        """
        total = len(orders)
        received_count = 0
        completed_count = 0
        completed_without_code_count = 0
        refund_confirmed_count = 0
        refund_pending_count = 0
        unconfirmed_refund_count = 0

        for o in orders:
            has_sms = bool(o.get("has_sms"))
            status = str(o.get("status") or "")
            ref = str(o.get("refund_state") or "none")
            if has_sms:
                received_count += 1
            if status == "completed":
                completed_count += 1
                if not has_sms:
                    completed_without_code_count += 1
            if ref == "confirmed":
                refund_confirmed_count += 1
            elif ref == "pending":
                refund_pending_count += 1
            if status in {"cancelled", "expired"} and ref not in {"confirmed", "none"}:
                unconfirmed_refund_count += 1

        refund_amount_unknown_count = 0
        unknown_cost_count = 0
        unreceived_paid_count = 0
        currency_review_count = 0
        by_currency: dict[str, dict[str, Any]] = {}
        review_by_currency: dict[str, dict[str, Any]] = {}

        def _get_bucket(store: dict[str, dict[str, Any]], ccy: str) -> dict[str, Any]:
            key = (ccy or "").strip().upper() or "UNKNOWN"
            bucket = store.get(key)
            if bucket is None:
                bucket = {
                    "currency": key,
                    "actual_cost_total": 0.0,
                    "refunded_amount_total": 0.0,
                    "_actual_refunded_total": 0.0,
                    "net_actual_cost_total": 0.0,
                    "estimated_cost_total": 0.0,
                    "cost_total": 0.0,
                    "unknown_cost_count": 0,
                    "refund_amount_unknown_count": 0,
                }
                store[key] = bucket
            return bucket

        def _finite_opt(raw_val: Any) -> Optional[float]:
            if raw_val is None:
                return None
            try:
                num = float(raw_val)
                return num if math.isfinite(num) else None
            except (TypeError, ValueError, OverflowError):
                return None

        # 1. 有激活台账的订单：按每一次真实购买的激活明细记账
        orders_with_act_cost: set[tuple[str, str]] = {
            (str(a.get("order_id") or ""), str(a.get("activation_id") or "").strip())
            for a in activations
            if _finite_opt(a.get("actual_cost")) is not None
            or (str(a.get("cost_status") or "") == "estimated" and _finite_opt(a.get("estimated_cost")) is not None)
        }
        merged_parent_cost: set[tuple[str, str]] = set()

        for act in activations:
            state = str(act.get("state") or "")
            act_id = str(act.get("activation_id") or "").strip()
            oid = str(act.get("order_id") or "")
            act_cost = _finite_opt(act.get("actual_cost"))
            ref_amt = _finite_opt(act.get("refund_amount"))
            if (
                not act_id
                and act_cost is None
                and ref_amt is None
                and state not in {"completed", "unrefundable", "refund_confirmed"}
            ):
                continue

            ccy = str(act.get("currency") or "UNKNOWN")
            needs_review = bool(act.get("currency_review_required"))
            target_store = review_by_currency if needs_review else by_currency
            if needs_review:
                currency_review_count += 1

            c_status = str(act.get("cost_status") or "unknown")
            est_cost = _finite_opt(act.get("estimated_cost"))
            if (
                act_cost is None
                and c_status == "unknown"
                and act_id
                and act_id == str(act.get("order_activation_id") or "").strip()
                and (oid, act_id) not in orders_with_act_cost
                and (oid, act_id) not in merged_parent_cost
            ):
                p_status = str(act.get("order_cost_status") or "unknown")
                p_act = _finite_opt(act.get("order_actual_cost"))
                p_est = _finite_opt(act.get("order_cost"))
                if p_status == "actual" and p_act is not None:
                    act_cost = p_act
                    c_status = "actual"
                    merged_parent_cost.add((oid, act_id))
                elif p_status == "estimated" and p_est is not None:
                    est_cost = p_est
                    c_status = "estimated"
                    merged_parent_cost.add((oid, act_id))

            if est_cost is None and c_status == "estimated" and act_id:
                est_cost = _finite_opt(act.get("quoted_max_price"))
            is_refunded = state == "refund_confirmed"
            has_sms = bool(act.get("order_has_sms"))

            if act_cost is not None:
                bucket = _get_bucket(target_store, ccy)
                val = float(act_cost)
                bucket["actual_cost_total"] += val
                if is_refunded:
                    if ref_amt is not None:
                        rval = float(ref_amt)
                        bucket["refunded_amount_total"] += rval
                        bucket["_actual_refunded_total"] += rval
                        if not has_sms and (val - rval) > 1e-9:
                            unreceived_paid_count += 1
                    else:
                        refund_amount_unknown_count += 1
                        bucket["refund_amount_unknown_count"] += 1
                elif not has_sms:
                    unreceived_paid_count += 1
            elif c_status == "estimated" and est_cost is not None:
                bucket = _get_bucket(target_store, ccy)
                if is_refunded:
                    # 估算成本未计入 actual_cost_total，仅记录核实退款额用于展示，绝不扣减 net_actual_cost_total！
                    if ref_amt is not None:
                        bucket["refunded_amount_total"] += float(ref_amt)
                    else:
                        refund_amount_unknown_count += 1
                        bucket["refund_amount_unknown_count"] += 1
                else:
                    bucket["estimated_cost_total"] += float(est_cost)
                    if not has_sms:
                        unreceived_paid_count += 1
            else:
                if is_refunded:
                    bucket = _get_bucket(target_store, ccy)
                    if ref_amt is not None:
                        bucket["refunded_amount_total"] += float(ref_amt)
                    else:
                        refund_amount_unknown_count += 1
                        bucket["refund_amount_unknown_count"] += 1
                elif state not in {"failed", "no_numbers", "purchasing", "uncertain_purchase"}:
                    bucket = _get_bucket(target_store, ccy)
                    unknown_cost_count += 1
                    bucket["unknown_cost_count"] += 1
                    if not has_sms:
                        unreceived_paid_count += 1

        # 2. 无激活台账的历史/手工构造订单：按订单级财务字段兜底（绝不与第 1 步重复，也不推定无证据退款额）
        for o in orders:
            if o.get("has_act_ledger"):
                continue
            status = str(o.get("status") or "")
            ref = str(o.get("refund_state") or "none")
            c_status = str(o.get("cost_status") or "unknown")
            act_cost = _finite_opt(o.get("actual_cost"))
            est_cost = _finite_opt(o.get("cost"))
            ccy = str(o.get("currency") or "UNKNOWN")
            has_sms = bool(o.get("has_sms"))
            is_refunded = ref == "confirmed"
            incurred_purchase = has_sms or status in {"completed", "waiting_code", "waiting_next_code", "code_received"} or (
                status in {"cancelled", "expired"} and ref != "none"
            )

            if c_status == "actual" and act_cost is not None:
                bucket = _get_bucket(by_currency, ccy)
                val = float(act_cost)
                bucket["actual_cost_total"] += val
                if is_refunded:
                    refund_amount_unknown_count += 1
                    bucket["refund_amount_unknown_count"] += 1
                elif not has_sms:
                    unreceived_paid_count += 1
            elif c_status == "estimated" and est_cost is not None:
                if is_refunded:
                    bucket = _get_bucket(by_currency, ccy)
                    refund_amount_unknown_count += 1
                    bucket["refund_amount_unknown_count"] += 1
                elif incurred_purchase:
                    bucket = _get_bucket(by_currency, ccy)
                    bucket["estimated_cost_total"] += float(est_cost)
                    if not has_sms:
                        unreceived_paid_count += 1
            elif c_status == "unknown":
                if is_refunded:
                    bucket = _get_bucket(by_currency, ccy)
                    refund_amount_unknown_count += 1
                    bucket["refund_amount_unknown_count"] += 1
                elif has_sms or status == "completed" or (status in {"cancelled", "expired"} and ref == "unrefundable"):
                    bucket = _get_bucket(by_currency, ccy)
                    unknown_cost_count += 1
                    bucket["unknown_cost_count"] += 1
                    if not has_sms:
                        unreceived_paid_count += 1

        def _ccy_sort_key(item: dict[str, Any]) -> tuple[int, str]:
            code = str(item["currency"])
            if code == "USD":
                return (0, code)
            if code == "UNKNOWN":
                return (2, code)
            return (1, code)

        def _finalize_buckets(store: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            for bucket in sorted(store.values(), key=_ccy_sort_key):
                act_r = round(bucket["actual_cost_total"], 4)
                ref_r = round(bucket["refunded_amount_total"], 4)
                act_ref_r = round(bucket.pop("_actual_refunded_total", 0.0), 4)
                net_r = round(act_r - act_ref_r, 4)
                est_r = round(bucket["estimated_cost_total"], 4)
                bucket["actual_cost_total"] = act_r
                bucket["refunded_amount_total"] = ref_r
                bucket["net_actual_cost_total"] = net_r
                bucket["estimated_cost_total"] = est_r
                bucket["cost_total"] = round(net_r + est_r, 4)
                result.append(bucket)
            return result

        by_currency_list = _finalize_buckets(by_currency)
        review_by_currency_list = _finalize_buckets(review_by_currency)

        has_unverified_currency = currency_review_count > 0
        is_multi_currency = (len(by_currency_list) > 1) or (has_unverified_currency and len(by_currency_list) >= 1)
        single_currency: Optional[str] = None
        actual_top: Optional[float]
        refunded_top: Optional[float]
        net_actual_top: Optional[float]
        estimated_top: Optional[float]
        cost_top: Optional[float]

        if has_unverified_currency:
            # 存在待核对币种记录时，顶层不输出可能误导的单一合计，明确由 by_currency 与 currency_review_by_currency 分别呈现
            actual_top = None
            refunded_top = None
            net_actual_top = None
            estimated_top = None
            cost_top = None
        elif len(by_currency_list) == 0:
            actual_top = 0.0
            refunded_top = 0.0
            net_actual_top = 0.0
            estimated_top = 0.0
            cost_top = 0.0
        elif len(by_currency_list) == 1:
            only = by_currency_list[0]
            single_currency = str(only["currency"])
            actual_top = only["actual_cost_total"]
            refunded_top = only["refunded_amount_total"]
            net_actual_top = only["net_actual_cost_total"]
            estimated_top = only["estimated_cost_total"]
            cost_top = only["cost_total"]
        else:
            # 存在多种币种（或含 UNKNOWN）时，严禁直接数值相加，顶层汇总置为 None，由 by_currency 或显式汇率折算呈现
            actual_top = None
            refunded_top = None
            net_actual_top = None
            estimated_top = None
            cost_top = None

        fx_conversion: Optional[dict[str, Any]] = None
        if fx_rates and fx_target_currency and fx_date and by_currency_list and not has_unverified_currency:
            target_ccy = fx_target_currency.strip().upper()
            norm_rates = {str(k).strip().upper(): float(v) for k, v in fx_rates.items() if float(v) > 0}
            norm_rates.setdefault(target_ccy, 1.0)
            if all(b["currency"] != "UNKNOWN" and b["currency"] in norm_rates for b in by_currency_list):
                fx_act = sum(b["actual_cost_total"] * norm_rates[b["currency"]] for b in by_currency_list)
                fx_ref = sum(b["refunded_amount_total"] * norm_rates[b["currency"]] for b in by_currency_list)
                fx_net = sum(b["net_actual_cost_total"] * norm_rates[b["currency"]] for b in by_currency_list)
                fx_est = sum(b["estimated_cost_total"] * norm_rates[b["currency"]] for b in by_currency_list)
                used_rates = {b["currency"]: norm_rates[b["currency"]] for b in by_currency_list}
                fx_conversion = {
                    "target_currency": target_ccy,
                    "as_of_date": str(fx_date).strip(),
                    "rates": used_rates,
                    "actual_cost_total": round(fx_act, 4),
                    "refunded_amount_total": round(fx_ref, 4),
                    "net_actual_cost_total": round(fx_net, 4),
                    "estimated_cost_total": round(fx_est, 4),
                    "cost_total": round(fx_net + fx_est, 4),
                }

        return {
            "total": total,
            "received_count": received_count,
            "completed_count": completed_count,
            "completed_without_code_count": completed_without_code_count,
            "unreceived_paid_count": unreceived_paid_count,
            "refunded_count": refund_confirmed_count,
            "refund_confirmed_count": refund_confirmed_count,
            "refund_pending_count": refund_pending_count,
            "unconfirmed_refund_count": unconfirmed_refund_count,
            "success_rate": round(received_count / total, 4) if total else 0.0,
            "currency": single_currency,
            "is_multi_currency": is_multi_currency,
            "actual_cost_total": actual_top,
            "refunded_amount_total": refunded_top,
            "refund_amount_unknown_count": refund_amount_unknown_count,
            "net_actual_cost_total": net_actual_top,
            "estimated_cost_total": estimated_top,
            "unknown_cost_count": unknown_cost_count,
            "cost_total": cost_top,
            "by_currency": by_currency_list,
            "currency_review_count": currency_review_count,
            "currency_review_by_currency": review_by_currency_list,
            "fx_conversion": fx_conversion,
        }

    async def get_project_summaries(self) -> dict[str, dict[str, Any]]:
        """与统计 API 共用同一聚合逻辑，供项目接入页展示一致的收码数、按币种实付/估算/退款口径。"""
        async with self._conn.read_transaction() as raw:
            orders, activations = await self._collect_financial_rows(raw, since_iso=None)
        orders_by_proj: dict[str, list[dict[str, Any]]] = {}
        acts_by_proj: dict[str, list[dict[str, Any]]] = {}
        for o in orders:
            orders_by_proj.setdefault(str(o["project_id"]), []).append(o)
        for a in activations:
            acts_by_proj.setdefault(str(a["project_id"]), []).append(a)

        result: dict[str, dict[str, Any]] = {}
        for pid in set(orders_by_proj) | set(acts_by_proj):
            agg = self._aggregate_financials(orders_by_proj.get(pid, []), acts_by_proj.get(pid, []))
            result[pid] = {
                "order_count": agg["total"],
                "received_count": agg["received_count"],
                "currency": agg["currency"],
                "is_multi_currency": agg["is_multi_currency"],
                "actual_cost_total": agg["actual_cost_total"],
                "refunded_amount_total": agg["refunded_amount_total"],
                "refund_amount_unknown_count": agg["refund_amount_unknown_count"],
                "net_actual_cost_total": agg["net_actual_cost_total"],
                "estimated_cost_total": agg["estimated_cost_total"],
                "unknown_cost_count": agg["unknown_cost_count"],
                "cost_total": agg["cost_total"],
                "by_currency": agg["by_currency"],
                "currency_review_count": agg["currency_review_count"],
                "currency_review_by_currency": agg["currency_review_by_currency"],
                "fx_conversion": agg["fx_conversion"],
            }
        return result

    async def get_stats(
        self,
        timezone_offset_minutes: int = 0,
        days: int = 7,
        *,
        project_id: str = "",
        fx_rates: Optional[dict[str, float]] = None,
        fx_target_currency: Optional[str] = None,
        fx_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """多维接码统计（修复 P1-05 / P1-10、补充 P1-04 / P1-05 与 v5 复核 P1-04 / P1-05）。"""
        offset = max(-840, min(840, int(timezone_offset_minutes or 0)))
        now_utc = _utc_now()
        local_now = now_utc - timedelta(minutes=offset)
        local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_utc = (local_day_start + timedelta(minutes=offset)).isoformat()
        window_days = max(1, min(int(days or 7), 90))
        window_start_utc = (local_day_start - timedelta(days=window_days - 1) + timedelta(minutes=offset)).isoformat()

        proj_clause = " AND project_id=?" if project_id else ""
        proj_params = (project_id,) if project_id else ()
        active_ph = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)

        async with self._conn.read_transaction() as raw:
            act_cur = await raw.execute(
                f"SELECT COUNT(*) AS cnt FROM sms_orders WHERE status IN ({active_ph}){proj_clause}",
                (*ACTIVE_ORDER_STATUSES, *proj_params),
            )
            act_row_raw = await act_cur.fetchone()
            active_orders = int((dict(act_row_raw) if act_row_raw else {}).get("cnt") or 0)

            ts_cur = await raw.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%f', ?) AS norm_today",
                (today_start_utc,),
            )
            ts_row = await ts_cur.fetchone()
            norm_today_ts = str((dict(ts_row) if ts_row else {}).get("norm_today") or today_start_utc)

            win_orders, win_acts = await self._collect_financial_rows(
                raw, since_iso=window_start_utc, project_id=project_id
            )
            today_orders = [o for o in win_orders if str(o.get("_norm_created_utc") or "") >= norm_today_ts]
            today_acts = [a for a in win_acts if str(a.get("_norm_created_utc") or "") >= norm_today_ts]

        fx_kwargs = {
            "fx_rates": fx_rates,
            "fx_target_currency": fx_target_currency,
            "fx_date": fx_date,
        }
        today_summary = self._aggregate_financials(today_orders, today_acts, **fx_kwargs)

        # 按项目分组聚合
        p_orders: dict[str, list[dict[str, Any]]] = {}
        p_acts: dict[str, list[dict[str, Any]]] = {}
        for o in win_orders:
            p_orders.setdefault(str(o["project_id"]), []).append(o)
        for a in win_acts:
            p_acts.setdefault(str(a["project_id"]), []).append(a)
        by_project = []
        for pid in sorted(set(p_orders) | set(p_acts), key=lambda k: len(p_orders.get(k, [])), reverse=True):
            agg = self._aggregate_financials(p_orders.get(pid, []), p_acts.get(pid, []), **fx_kwargs)
            by_project.append({
                "project_id": pid,
                "total": agg["total"],
                "received_count": agg["received_count"],
                "completed_count": agg["completed_count"],
                "currency": agg["currency"],
                "is_multi_currency": agg["is_multi_currency"],
                "actual_cost_total": agg["actual_cost_total"],
                "refunded_amount_total": agg["refunded_amount_total"],
                "refund_amount_unknown_count": agg["refund_amount_unknown_count"],
                "net_actual_cost_total": agg["net_actual_cost_total"],
                "estimated_cost_total": agg["estimated_cost_total"],
                "unknown_cost_count": agg["unknown_cost_count"],
                "cost_total": agg["cost_total"],
                "by_currency": agg["by_currency"],
                "currency_review_count": agg["currency_review_count"],
                "currency_review_by_currency": agg["currency_review_by_currency"],
                "fx_conversion": agg["fx_conversion"],
            })

        # 按服务分组聚合
        s_orders: dict[tuple[str, str], list[dict[str, Any]]] = {}
        s_acts: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for o in win_orders:
            s_orders.setdefault((str(o["service"]), str(o["service_code"])), []).append(o)
        for a in win_acts:
            s_acts.setdefault((str(a["service"]), str(a["service_code"])), []).append(a)
        by_service = []
        for skey in sorted(set(s_orders) | set(s_acts), key=lambda k: len(s_orders.get(k, [])), reverse=True):
            agg = self._aggregate_financials(s_orders.get(skey, []), s_acts.get(skey, []), **fx_kwargs)
            by_service.append({
                "service": skey[0],
                "service_code": skey[1],
                "total": agg["total"],
                "received_count": agg["received_count"],
                "completed_count": agg["completed_count"],
                "currency": agg["currency"],
                "is_multi_currency": agg["is_multi_currency"],
                "actual_cost_total": agg["actual_cost_total"],
                "refunded_amount_total": agg["refunded_amount_total"],
                "refund_amount_unknown_count": agg["refund_amount_unknown_count"],
                "net_actual_cost_total": agg["net_actual_cost_total"],
                "estimated_cost_total": agg["estimated_cost_total"],
                "unknown_cost_count": agg["unknown_cost_count"],
                "cost_total": agg["cost_total"],
                "by_currency": agg["by_currency"],
                "currency_review_count": agg["currency_review_count"],
                "currency_review_by_currency": agg["currency_review_by_currency"],
                "fx_conversion": agg["fx_conversion"],
            })

        return {
            "scope": "project" if project_id else "global",
            "project_id": project_id or None,
            "timezone_offset_minutes": offset,
            "window_days": window_days,
            "active_orders": active_orders,
            "today": today_summary,
            "by_project": by_project,
            "by_service": by_service,
        }
