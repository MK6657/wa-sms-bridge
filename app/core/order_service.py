"""独立接码订单引擎（OrderService）。

已全面加固以解决评审中的所有资金安全与并发状态问题：
- P0-02: 分项目原子幂等占位 + request_hash 冲突校验 + 同项目并发请求复用结果不重复买号
- P1-03: 购买前落意图 (order_activations) + 取号后立刻持久化 activation_id + 黑名单退款未结清立即停止重试并追踪补偿
- P1-04: 每单锁 + DB 条件 CAS 状态转换（completing / cancelling）杜绝完成与取消互相覆盖；严格校验上游业务语义
- P1-05: 区分 quoted_max_price / actual_cost / cost_status 与 refund_state
- P1-06: 严格加密（缺失 BRIDGE_SECRET_KEY 不降级明文）+ 独立留存清理时钟
- P1-07: 持久化重发基准码 resend_base_code 与 resend_seen_wait，网络抖动不误触 WAIT，重启不重复记旧码
- P1-09: 状态转换同事务写入 webhook_deliveries Outbox 持久化队列，保证事件不丢
- P1-10: 取消并拉黑按调用方身份区分全局黑名单（管理员）与项目私有黑名单
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from uuid import uuid4

from app.config import env
from app.core.event_bus import EventBus
from app.core.url_guard import validate_webhook_url_async
from app.db import Database
from app.errors import AppError, ConflictError, NotFoundError, UpstreamError
from app.log_redaction import mask_identifier, mask_phone
from app.price_policy import effective_price_max, locked_price_available, polling_interval_seconds
from app.providers.base import (
    ActionOutcome,
    Activation,
    BalanceError,
    CancelOutcome,
    NoNumbersError,
    PurchaseUncertainError,
    SmsProviderError,
    parse_cancel_outcome,
    parse_complete_outcome,
    parse_resend_outcome,
)
from app.providers.registry import ProviderRegistry
from app.service_codes import canonical_service, provider_service_code

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"completed", "cancelled", "expired", "failed"})
WEBHOOK_OUTBOX_EVENTS = frozenset({
    "order.acquired",
    "order.code_received",
    "order.completed",
    "order.cancelled",
    "order.expired",
    "order.failed",
})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def compute_request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_minimal_webhook_payload(event_type: str, event_id: str, order: dict[str, Any]) -> dict[str, Any]:
    """只暴露文档契约所需的最小字段集（修复 P1-01 载荷最小化）。"""
    return {
        "id": event_id,
        "event": event_type,
        "timestamp": _utc_now().isoformat(),
        "order_id": order.get("order_id", ""),
        "project_id": order.get("project_id", ""),
        "data": {
            "order_id": order.get("order_id", ""),
            "project_id": order.get("project_id", ""),
            "external_ref": order.get("external_ref", ""),
            "provider": order.get("provider", ""),
            "service": order.get("service", ""),
            "country_id": order.get("country_id", ""),
            "phone_e164": order.get("phone_e164", ""),
            "status": order.get("status", ""),
            "refund_state": order.get("refund_state", "none"),
            "latest_code": order.get("latest_code", "") if event_type == "order.code_received" else "",
            "code_count": int(order.get("code_count") or 0),
            "error_code": order.get("error_code", ""),
            "error_message": order.get("error_message", ""),
        },
    }


@dataclass
class AcquireOptions:
    target_price: Optional[float] = None
    price_max: Optional[float] = None
    price_lock: bool = False
    strict_exact_price: bool = False
    price_float_enabled: bool = False
    price_float_percent: float = 0.0
    max_attempts: int = 1
    continuous_poll: bool = False
    request_interval_enabled: bool = False
    request_interval: float = 1.0


@dataclass
class _RefCountedLock:
    """带引用计数的异步锁条目，确保持有者与等待者全部退出前绝不从锁表移除（修复 v7 复核 P2-01）。"""

    lock: asyncio.Lock
    refs: int = 0


class _ManagedAsyncLock:
    """自动引用计数回收的异步互斥锁（无持有者与等待者时自动从字典剔除，杜绝长稳运行内存增长）。"""

    def __init__(self, table: dict[Any, "_ManagedAsyncLock"], key: Any):
        self._table = table
        self._key = key
        self._lock = asyncio.Lock()
        self.refs = 0

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> "_ManagedAsyncLock":
        self.refs += 1
        try:
            await self._lock.acquire()
        except BaseException:
            self.refs -= 1
            if self.refs <= 0 and self._table.get(self._key) is self and not self._lock.locked():
                self._table.pop(self._key, None)
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self._lock.release()
        finally:
            self.refs -= 1
            if self.refs <= 0 and self._table.get(self._key) is self and not self._lock.locked():
                self._table.pop(self._key, None)


class OrderService:
    """通用 SMS 接码订单状态机与按需轮询管理器。"""

    def __init__(
        self,
        db: Database,
        registry: ProviderRegistry,
        event_bus: EventBus,
        *,
        poll_intervals: tuple[float, float, float] = (3.0, 5.0, 8.0),
        max_code_write_retries: int = 3,
        max_poll_loop_errors: int = 5,
        on_outbox_enqueued: Optional[Callable[[], None]] = None,
        on_orders_scrubbed: Optional[Callable[[list[str]], Any]] = None,
    ):
        self._db = db
        self._registry = registry
        self._event_bus = event_bus
        self._poll_intervals = poll_intervals
        self._max_code_write_retries = max(1, int(max_code_write_retries))
        self._max_poll_loop_errors = max(1, int(max_poll_loop_errors))
        self._on_outbox_enqueued = on_outbox_enqueued
        self._on_orders_scrubbed_cb = on_orders_scrubbed
        self._tasks: dict[str, asyncio.Task] = {}
        self._inflight_binds: dict[str, asyncio.Task] = {}
        self._order_locks: dict[str, _ManagedAsyncLock] = {}
        self._activation_locks: dict[tuple[str, str], _ManagedAsyncLock] = {}
        self._idem_locks: dict[tuple[str, str], _RefCountedLock] = {}
        self._acquire_waiters: dict[str, asyncio.Event] = {}
        self._acquire_errors: dict[str, AppError] = {}
        self._active_acquiring_orders: set[str] = set()
        self._last_code_persist_errors: dict[str, str] = {}

    def _has_live_task(self, order_id: str) -> bool:
        task = self._tasks.get(order_id)
        return task is not None and not task.done()

    @staticmethod
    def _resolve_refund_evidence(
        provider: Any,
        outcome: CancelOutcome,
        act_row: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[float], str]:
        """仅在上游结果返回金额或平台明确声明全额退款契约且购入成本已知时返回核实退款额，否则保持未知 (None)。"""
        if outcome.refund_amount is not None:
            return float(outcome.refund_amount), (outcome.refund_amount_source or "provider_response")
        caps = getattr(provider, "capabilities", None)
        if (
            caps is not None
            and getattr(caps, "confirms_full_refund_on_cancel", False)
            and act_row is not None
            and act_row.get("actual_cost") is not None
        ):
            return float(act_row["actual_cost"]), "full_refund_contract"
        return None, ""

    def set_outbox_notifier(self, callback: Callable[[], None]) -> None:
        self._on_outbox_enqueued = callback

    def set_scrub_notifier(self, callback: Callable[[list[str]], Any]) -> None:
        self._on_orders_scrubbed_cb = callback

    def _is_order_locked(self, order_id: str) -> bool:
        entry = self._order_locks.get(order_id)
        return bool(entry is not None and entry.locked())

    def _lock_for(self, order_id: str) -> _ManagedAsyncLock:
        lock = self._order_locks.get(order_id)
        if lock is None:
            lock = _ManagedAsyncLock(self._order_locks, order_id)
            self._order_locks[order_id] = lock
        return lock

    def _activation_lock_for(self, provider: str, activation_id: str) -> _ManagedAsyncLock:
        key = ((provider or "").strip().lower(), (activation_id or "").strip())
        lock = self._activation_locks.get(key)
        if lock is None:
            lock = _ManagedAsyncLock(self._activation_locks, key)
            self._activation_locks[key] = lock
        return lock

    def _idem_lock_entry_for(self, project_id: str, idempotency_key: str) -> _RefCountedLock:
        key = (project_id or "web-console", idempotency_key)
        entry = self._idem_locks.get(key)
        if entry is None:
            entry = _RefCountedLock(lock=asyncio.Lock(), refs=0)
            self._idem_locks[key] = entry
        return entry

    def _emit(
        self,
        event: str,
        order: dict[str, Any],
        extra: Optional[dict[str, Any]] = None,
        *,
        code_scrub_version: Optional[int] = None,
    ) -> None:
        payload = dict(order)
        if extra:
            payload.update(extra)
        eff_ver = code_scrub_version
        if eff_ver is None and order.get("code_scrub_version") is not None:
            try:
                eff_ver = int(order["code_scrub_version"])
            except (TypeError, ValueError):
                eff_ver = None
        self._event_bus.publish(
            event,
            payload,
            order_id=str(order.get("order_id") or ""),
            project_id=str(order.get("project_id") or ""),
            code_scrub_version=eff_ver,
        )

    def _notify_outbox(self) -> None:
        if self._on_outbox_enqueued:
            try:
                self._on_outbox_enqueued()
            except Exception:
                pass

    async def _resolve_webhook_target(self, order: dict[str, Any]) -> str:
        url = str(order.get("webhook_url") or "").strip()
        if url:
            return url
        pid = str(order.get("project_id") or "")
        if pid and pid != "web-console":
            row = await self._db.api_keys.get(pid)
            if row:
                return str(row.get("default_webhook_url") or "").strip()
        return ""

    async def _make_outbox_entry(self, event_type: str, order_snapshot: dict[str, Any]) -> Optional[dict[str, Any]]:
        if event_type not in WEBHOOK_OUTBOX_EVENTS:
            return None
        target_url = await self._resolve_webhook_target(order_snapshot)
        if not target_url:
            return None
        event_id = f"evt_{uuid4().hex[:12]}"
        delivery_id = f"dlv_{uuid4().hex[:12]}"
        return {
            "delivery_id": delivery_id,
            "event_id": event_id,
            "event_type": event_type,
            "order_id": str(order_snapshot.get("order_id") or ""),
            "project_id": str(order_snapshot.get("project_id") or ""),
            "target_url": target_url,
            "payload": build_minimal_webhook_payload(event_type, event_id, order_snapshot),
        }

    async def _finalize_order_status(
        self,
        order_id: str,
        *,
        expected_statuses: tuple[str, ...],
        new_status: str,
        base_order: Optional[dict[str, Any]] = None,
        fallback_project_id: str = "",
        activation_tx_hook=None,
        scrub_if_zero_retention: bool = False,
        **fields: Any,
    ) -> Optional[dict[str, Any]]:
        """统一终态提交入口：单事务原子完成订单终态、激活台账同步、可选即刻脱敏与 Outbox 入队，提交后再发布 SSE（修复复核 P1-04 & v7 P1-01）。"""
        event_map = {
            "completed": "order.completed",
            "cancelled": "order.cancelled",
            "expired": "order.expired",
            "failed": "order.failed",
        }
        event_type = event_map.get(new_status, "order.status_changed")
        snap_base = dict(base_order or (await self._db.orders.get(order_id, include_messages=False)) or {})
        if not snap_base.get("order_id"):
            snap_base["order_id"] = order_id
        if not snap_base.get("project_id") and fallback_project_id:
            snap_base["project_id"] = fallback_project_id
        snap_base["status"] = new_status
        for k in ("refund_state", "settlement_state", "error_code", "error_message", "latest_code", "code_count"):
            if k in fields:
                snap_base[k] = fields[k]

        do_scrub = False
        if scrub_if_zero_retention and (await self._retention_hours()) == 0:
            do_scrub = True
            snap_base["latest_code"] = ""

        outbox = await self._make_outbox_entry(event_type, snap_base)
        if "completed_at" not in fields and new_status in TERMINAL_STATUSES:
            fields["completed_at"] = _utc_now().isoformat()

        scrub_versions: dict[str, int] = {}
        updated = await self._db.orders.transition_status(
            order_id,
            expected_statuses=expected_statuses,
            new_status=new_status,
            outbox_entry=outbox,
            tx_hook=activation_tx_hook,
            scrub_in_tx=do_scrub,
            scrub_result=scrub_versions,
            **fields,
        )
        if updated is not None:
            if scrub_versions:
                self.on_orders_scrubbed(list(scrub_versions.keys()), scrub_versions=scrub_versions)
            if outbox is not None:
                self._notify_outbox()
            self._emit(event_type, updated)
        return updated

    async def scrub_order_code(self, order_id: str) -> bool:
        """脱敏清理单笔订单的验证码，原子递增脱敏版本并同步清空数据库、Webhook Outbox、在途投递与 SSE 全视图（修复 v6 复核 P0-01）。"""
        new_version = await self._db.orders.scrub_order_code_with_version(order_id)
        if new_version is not None:
            self.on_orders_scrubbed([order_id], scrub_versions={order_id: new_version})
            refreshed = await self._db.orders.get(order_id, include_messages=True, include_activations=True)
            if refreshed is not None:
                self._emit("order.status_changed", refreshed, code_scrub_version=new_version)
            return True
        return False

    def on_orders_scrubbed(
        self,
        order_ids: list[str],
        *,
        scrub_versions: Optional[dict[str, int]] = None,
    ) -> None:
        """清理后同步抬高脱敏版本门槛、擦除 SSE 历史/慢订阅队列并取消仍在途的验证码 Webhook 投递任务。"""
        if not order_ids and not scrub_versions:
            return
        all_ids = list(dict.fromkeys([*(order_ids or []), *(scrub_versions.keys() if scrub_versions else [])]))
        self._event_bus.scrub_order_codes(all_ids, scrub_versions=scrub_versions)
        if self._on_orders_scrubbed_cb:
            try:
                self._on_orders_scrubbed_cb(all_ids)
            except Exception:
                pass

    async def _retention_hours(self) -> int:
        raw = await self._db.config.get("sms_code_retention_hours", "")
        if raw != "":
            try:
                return int(raw)
            except ValueError:
                pass
        return int(env.sms_code_retention_hours)

    def _cancel_bg_task(self, order_id: str) -> Optional[asyncio.Task]:
        task = self._tasks.pop(order_id, None)
        if task and not task.done():
            task.cancel()
        return task

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        bind_tasks = list(self._inflight_binds.values())
        self._inflight_binds.clear()
        if bind_tasks:
            await asyncio.gather(*bind_tasks, return_exceptions=True)
        self._acquire_errors.clear()

    # ── Provider 结构化动作统一适配（兼容旧 mock）───────────────

    @staticmethod
    async def _provider_cancel(provider: Any, activation_id: str) -> CancelOutcome:
        fn = getattr(provider, "cancel_activation_structured", None)
        if fn is not None and inspect.iscoroutinefunction(fn):
            outcome = await fn(activation_id)
            if isinstance(outcome, CancelOutcome):
                return outcome
        try:
            raw = await provider.cancel_activation(activation_id)
            return parse_cancel_outcome(raw)
        except Exception as exc:
            return CancelOutcome(settled=False, status="error", retryable=True, raw=str(exc), refund_confirmed=False)

    @staticmethod
    async def _provider_complete(provider: Any, activation_id: str) -> ActionOutcome:
        fn = getattr(provider, "complete_activation_structured", None)
        if fn is not None and inspect.iscoroutinefunction(fn):
            outcome = await fn(activation_id)
            if isinstance(outcome, ActionOutcome):
                return outcome
        try:
            raw = await provider.complete_activation(activation_id)
            return parse_complete_outcome(raw)
        except Exception as exc:
            return ActionOutcome(ok=False, status="error", retryable=True, raw=str(exc))

    @staticmethod
    async def _provider_resend(provider: Any, activation_id: str) -> ActionOutcome:
        fn = getattr(provider, "request_additional_sms_structured", None)
        if fn is not None and inspect.iscoroutinefunction(fn):
            outcome = await fn(activation_id)
            if isinstance(outcome, ActionOutcome):
                return outcome
        try:
            raw = await provider.request_additional_sms(activation_id)
            return parse_resend_outcome(raw)
        except Exception as exc:
            return ActionOutcome(ok=False, status="error", retryable=True, raw=str(exc))

    # ── 创建订单 / 幂等前置校验与统一受管取号任务（P0-02 / 补充 P1-01 / P1-02）──

    @staticmethod
    def _error_status_for_code(code: str) -> int:
        return {
            "INSUFFICIENT_BALANCE": 402,
            "NO_NUMBERS": 409,
            "BLACKLISTED": 409,
            "BLACKLIST_REFUND_UNCONFIRMED": 409,
            "PRICE_EXCEEDED": 409,
            "PURCHASE_UNCERTAIN": 502,
            "PROVIDER_ERROR": 502,
            "ACQUIRE_POST_ERROR": 500,
            "INTERNAL_ERROR": 500,
        }.get(code, 502)

    @staticmethod
    def _hash_service_token(service: str) -> str:
        """保留 custom: 自定义直通语义，防止 whatsapp 与 custom:whatsapp 在幂等摘要中碰撞（修复 v7 复核 P1-05）。"""
        raw = (service or "whatsapp").strip().lower()
        if raw.startswith("custom:"):
            custom_val = raw.split(":", 1)[1].strip()
            return f"custom:{custom_val}" if custom_val else "whatsapp"
        return canonical_service(raw)

    @staticmethod
    def _build_request_hash_v2(
        *,
        provider_input: str,
        canonical_svc: str,
        country_input: str,
        country_fallback: Optional[list[str]],
        opts: AcquireOptions,
        wait_timeout: int,
        skip_blacklisted: bool,
        async_acquire: bool,
        external_ref: str,
        explicit_webhook_url: str,
        metadata: Optional[dict[str, Any]],
        raw_service: Optional[str] = None,
    ) -> str:
        svc_token = OrderService._hash_service_token(raw_service) if raw_service is not None else canonical_svc
        return compute_request_hash({
            "v": 2,
            "provider_input": (provider_input or "").strip().lower(),
            "service": svc_token,
            "country": str(country_input).strip().lower(),
            "country_fallback": [str(c).strip().lower() for c in (country_fallback or []) if str(c).strip()],
            "strategy": {
                "target_price": opts.target_price,
                "price_max": opts.price_max,
                "price_lock": bool(opts.price_lock),
                "strict_exact_price": bool(opts.strict_exact_price),
                "price_float_enabled": bool(opts.price_float_enabled),
                "price_float_percent": float(opts.price_float_percent or 0.0),
                "max_attempts": int(opts.max_attempts or 1),
                "continuous_poll": bool(opts.continuous_poll),
                "request_interval_enabled": bool(opts.request_interval_enabled),
                "request_interval": float(opts.request_interval or 1.0),
            },
            "wait_timeout": int(wait_timeout),
            "skip_blacklisted": bool(skip_blacklisted),
            "async_acquire": bool(async_acquire or opts.continuous_poll),
            "external_ref": (external_ref or "").strip(),
            "webhook_url": (explicit_webhook_url or "").strip(),
            "metadata": metadata or {},
        })

    @staticmethod
    def _build_legacy_hash_v1(
        *,
        existing: dict[str, Any],
        canonical_svc: str,
        country_input: str,
        country_fallback: Optional[list[str]],
        max_price: Optional[float],
        opts: AcquireOptions,
        wait_timeout: int,
        skip_blacklisted: bool,
        async_acquire: bool,
        webhook_url: str,
        raw_service: Optional[str] = None,
        explicit_webhook_url: Optional[str] = None,
    ) -> str:
        provider_key = str(existing.get("provider") or "smsbower")
        # v7 复核 P1-05: 历史 v1 摘要必须用原始 service 计算 service_code（保留 custom: 语义），
        # 且当本次请求未显式指定 webhook_url 时沿用订单落盘的既有 webhook_url，绝不受后续项目默认回调变更影响！
        service_input = raw_service if raw_service is not None else canonical_svc
        service_code = provider_service_code(provider_key, service_input)
        effective_wh = (
            (explicit_webhook_url if explicit_webhook_url is not None else webhook_url)
            or existing.get("webhook_url")
            or ""
        ).strip()
        return compute_request_hash({
            "provider": provider_key,
            "service": canonical_svc,
            "service_code": service_code,
            "country": str(country_input).strip().lower(),
            "country_fallback": [str(c).strip().lower() for c in (country_fallback or []) if str(c).strip()],
            "max_price": max_price,
            "price_lock": bool(opts.price_lock),
            "strict_exact_price": bool(opts.strict_exact_price),
            "target_price": opts.target_price,
            "wait_timeout": int(wait_timeout),
            "skip_blacklisted": bool(skip_blacklisted),
            "async_acquire": bool(async_acquire or opts.continuous_poll),
            "webhook_url": effective_wh,
        })

    async def _replay_idempotent_order(
        self,
        existing: dict[str, Any],
        *,
        req_hash_v2: str,
        canonical_svc: str,
        raw_service: str = "",
        country_input: str,
        country_fallback: Optional[list[str]],
        max_price: Optional[float],
        opts: AcquireOptions,
        wait_timeout: int,
        skip_blacklisted: bool,
        is_async: bool,
        webhook_url: str,
        explicit_webhook_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """校验幂等摘要并按原始请求语义回放成功结果、进行中状态或失败错误（修复补充 P1-02 & v7 P1-05）。"""
        existing_hash = str(existing.get("request_hash") or "")
        hash_version = int(existing.get("request_hash_version") or 1)
        proven_legacy_async = False
        if existing_hash:
            if hash_version >= 2:
                if existing_hash != req_hash_v2:
                    raise ConflictError(
                        "相同的 Idempotency-Key 已被用于不同参数的请求，请更换幂等键",
                        code="IDEMPOTENCY_CONFLICT",
                        details={"order_id": existing["order_id"]},
                    )
            else:
                legacy_h = self._build_legacy_hash_v1(
                    existing=existing,
                    canonical_svc=canonical_svc,
                    raw_service=raw_service or canonical_svc,
                    country_input=country_input,
                    country_fallback=country_fallback,
                    max_price=max_price,
                    opts=opts,
                    wait_timeout=wait_timeout,
                    skip_blacklisted=skip_blacklisted,
                    async_acquire=is_async,
                    webhook_url=webhook_url,
                    explicit_webhook_url=explicit_webhook_url,
                )
                if existing_hash != legacy_h and existing_hash != req_hash_v2:
                    raise ConflictError(
                        "相同的 Idempotency-Key 已被用于不同参数的请求，请更换幂等键",
                        code="IDEMPOTENCY_CONFLICT",
                        details={"order_id": existing["order_id"]},
                    )
                # v6 复核 P1-06: v4 旧单无 async_acquire 列，但其 v1 SHA-256 哈希包含了 async_acquire 布尔值；
                # 若当前请求以 is_async=True 精确命中 v1 哈希，即可密码学证实该历史订单原为异步受理！
                if is_async and existing_hash == legacy_h:
                    proven_legacy_async = True

        oid = str(existing["order_id"])
        waiter = self._acquire_waiters.get(oid)
        if waiter is not None and not is_async:
            try:
                await asyncio.wait_for(waiter.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
            refreshed = await self._db.orders.get(oid, include_messages=True, include_activations=True)
            if refreshed is not None:
                existing = refreshed

        # v5 复核 P1-01 & v6 复核 P1-06: 幂等回放严格依据初次受理/初次错误快照，不随后续退款或后台终态漂移！
        was_async = bool(existing.get("async_acquire")) or proven_legacy_async
        saved_http = int(existing.get("idempotency_http_status") or 0)
        if was_async:
            # 异步受理重放固定保持 202 + 原订单 ID，不把后台失败改写为 201 或报错，也不恢复已脱敏验证码
            replay_order = dict(existing)
            replay_order["async_acquire"] = True
            replay_order["idempotency_http_status"] = 202
            return replay_order

        status = str(existing.get("status") or "")
        if saved_http >= 400 or (saved_http == 0 and status == "failed"):
            raw_idem_code = str(existing.get("idempotency_error_code") or "")
            raw_cur_code = str(existing.get("error_code") or "")
            non_acquire_codes = {
                "COMPENSATED_CANCEL",
                "CANCEL_UNREFUNDABLE",
                "USER_CANCELLED",
                "SMS_TIMEOUT",
                "PROVIDER_CANCELLED",
                "PROVIDER_EXPIRED",
                "RESTART_RECOVERY",
                "MAINTENANCE_RECOVERY",
                "COMPLETE_RECOVERY",
                "ACQUIRE_COMPENSATION_PENDING",
                "PROVIDER_FINISHED",
            }
            if raw_idem_code:
                err_code = raw_idem_code
                err_msg = str(existing.get("idempotency_error_message") or existing.get("error_message") or "取号失败")
            elif raw_cur_code and raw_cur_code not in non_acquire_codes:
                err_code = raw_cur_code
                err_msg = str(existing.get("error_message") or "取号失败")
            else:
                err_code = "HISTORICAL_ACQUIRE_ERROR"
                err_msg = "历史订单初次取号失败（后续已变更状态，请查询订单获取最新结果）"
            http_status = saved_http if saved_http >= 400 else self._error_status_for_code(err_code)
            raise AppError(
                err_msg,
                status_code=http_status,
                code=err_code,
                retryable=bool(existing.get("idempotency_retryable")),
                details={
                    "order_id": oid,
                    "replayed": True,
                    "current_status": status,
                    "order_query_path": f"/api/v1/orders/{oid}",
                },
            )
        return existing

    async def create_order(
        self,
        *,
        country: str,
        service: str = "whatsapp",
        provider_name: str = "",
        country_fallback: Optional[list[str]] = None,
        strategy: Optional[AcquireOptions] = None,
        wait_timeout: int = 180,
        skip_blacklisted: bool = True,
        async_acquire: bool = False,
        project_id: str = "web-console",
        external_ref: str = "",
        idempotency_key: Optional[str] = None,
        webhook_url: str = "",
        explicit_webhook_url: Optional[str] = None,
        validate_webhook_dns: bool = False,
        allowed_project_webhook_url: str = "",
        enforce_project_webhook_origin: bool = False,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        opts = strategy or AcquireOptions()
        pid = project_id or "web-console"
        clean_idem = (idempotency_key or "").strip() or None
        canonical = canonical_service(service)
        is_async = bool(async_acquire or opts.continuous_poll)
        raw_explicit_wh = (webhook_url if explicit_webhook_url is None else explicit_webhook_url) or ""

        # 纯本地价格策略校验（不触达任何上游网络、DNS 或会动态变更的项目回调策略）
        try:
            max_price = effective_price_max(
                target_price=opts.target_price,
                price_max=opts.price_max,
                price_lock_enabled=opts.price_lock or opts.strict_exact_price,
                price_float_enabled=opts.price_float_enabled or (opts.price_float_percent > 0 and not opts.price_lock and not opts.strict_exact_price),
                price_float_percent=opts.price_float_percent,
            )
        except ValueError as exc:
            raise AppError(str(exc), status_code=422, code="INVALID_PRICE_POLICY") from exc

        req_hash_v2 = self._build_request_hash_v2(
            provider_input=provider_name,
            canonical_svc=canonical,
            raw_service=service,
            country_input=country,
            country_fallback=country_fallback,
            opts=opts,
            wait_timeout=wait_timeout,
            skip_blacklisted=skip_blacklisted,
            async_acquire=is_async,
            external_ref=external_ref,
            explicit_webhook_url=raw_explicit_wh,
            metadata=metadata,
        )

        # 补充 P1-02 & v6 复核 P1-06: 在任何新订单同源策略、DNS、平台检查或国家查询之前，
        # 优先按 (project_id, Idempotency-Key) 快速命中已有订单！
        if clean_idem:
            existing = await self._db.orders.get_by_idempotency_key(pid, clean_idem)
            if existing is not None:
                return await self._replay_idempotent_order(
                    existing,
                    req_hash_v2=req_hash_v2,
                    canonical_svc=canonical,
                    raw_service=service,
                    country_input=country,
                    country_fallback=country_fallback,
                    max_price=max_price,
                    opts=opts,
                    wait_timeout=wait_timeout,
                    skip_blacklisted=skip_blacklisted,
                    is_async=is_async,
                    webhook_url=webhook_url,
                    explicit_webhook_url=raw_explicit_wh,
                )

        async def _prepare_and_reserve() -> tuple[dict[str, Any], Optional[asyncio.Event], bool]:
            # v6 复核 P1-06: 持有同幂等键锁后再次查表，保证同键并发请求绝不重复调用国家解析或 DNS！
            if clean_idem:
                existing_in_lock = await self._db.orders.get_by_idempotency_key(pid, clean_idem)
                if existing_in_lock is not None:
                    return existing_in_lock, None, False

            # 仅在首次创建新订单时执行项目同源政策、外部 DNS 校验、平台可用性检查与国家解析
            final_webhook = (webhook_url or "").strip()
            if raw_explicit_wh.strip() and (validate_webhook_dns or enforce_project_webhook_origin):
                final_webhook, _ = await validate_webhook_url_async(
                    raw_explicit_wh.strip(),
                    allowed_project_url=allowed_project_webhook_url,
                    enforce_project_origin=enforce_project_webhook_origin,
                    resolve_dns=validate_webhook_dns,
                )

            provider_key = (provider_name or self._registry.default_provider).strip().lower()
            try:
                provider = self._registry.get(provider_key)
            except ValueError as exc:
                raise AppError(str(exc), status_code=400, code="PROVIDER_NOT_ACTIVE") from exc

            service_code = provider_service_code(provider_key, service)

            if opts.strict_exact_price and not getattr(provider.capabilities, "supports_exact_price_lock", False):
                raise AppError(
                    f"平台 {provider_key} 仅支持最高价上限 (maxPrice) 语义，不支持精确固定成交价档校验",
                    status_code=422,
                    code="EXACT_PRICE_LOCK_UNSUPPORTED",
                )

            try:
                cid, clabel = await provider.resolve_country(country)
            except ValueError as exc:
                raise AppError(str(exc), status_code=400, code="INVALID_COUNTRY") from exc
            except Exception as exc:
                raise UpstreamError(f"国家解析失败: {exc}", code="COUNTRY_RESOLVE_FAILED") from exc

            candidates: list[tuple[Any, str]] = [(cid, clabel)]
            seen = {str(cid)}
            for fb in country_fallback or []:
                if not str(fb).strip():
                    continue
                try:
                    fb_cid, fb_label = await provider.resolve_country(str(fb))
                    if str(fb_cid) not in seen:
                        candidates.append((fb_cid, fb_label))
                        seen.add(str(fb_cid))
                except Exception:
                    continue

            new_order_id = f"ord_{_utc_now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:6]}"

            # 原子占位：同项目同幂等键只会由一个协程成功占位并执行购买，其他并发请求安全复用结果！
            reserved_order, created_new = await self._db.orders.reserve(
                order_id=new_order_id,
                provider=provider_key,
                service=canonical,
                service_code=service_code,
                country_id=str(cid),
                country_input=str(country).strip(),
                country_label=clabel,
                project_id=pid,
                external_ref=external_ref,
                idempotency_key=clean_idem,
                request_hash=req_hash_v2,
                request_hash_version=2,
                idempotency_http_status=202 if is_async else 0,
                async_acquire=is_async,
                max_price=max_price,
                wait_timeout=max(30, min(int(wait_timeout), 1200)),
                webhook_url=final_webhook,
                metadata=metadata,
                status="acquiring",
            )
            if not created_new:
                return reserved_order, None, False

            oid = str(reserved_order["order_id"])
            self._emit("order.created", reserved_order)

            # 补充 P1-01: 同步与异步下单统一交由受管任务执行；客户端断开仅结束等待，任务仍完成落库或补偿交接！
            w = asyncio.Event()
            self._acquire_waiters[oid] = w
            self._active_acquiring_orders.add(oid)
            task = asyncio.create_task(
                self._managed_acquire_and_poll(
                    order_id=oid,
                    provider_key=provider_key,
                    candidates=candidates,
                    service_code=service_code,
                    max_price=max_price,
                    opts=opts,
                    wait_timeout=reserved_order["wait_timeout"],
                    skip_blacklisted=skip_blacklisted,
                    project_id=pid,
                    waiter=w,
                    is_async=is_async,
                )
            )
            self._tasks[oid] = task
            return reserved_order, w, True

        if clean_idem:
            lock_key = (pid, clean_idem)
            entry = self._idem_lock_entry_for(pid, clean_idem)
            entry.refs += 1
            try:
                async with entry.lock:
                    order, waiter, created_new = await _prepare_and_reserve()
            finally:
                entry.refs -= 1
                if entry.refs <= 0 and self._idem_locks.get(lock_key) is entry:
                    self._idem_locks.pop(lock_key, None)
        else:
            order, waiter, created_new = await _prepare_and_reserve()

        if not created_new:
            return await self._replay_idempotent_order(
                order,
                req_hash_v2=req_hash_v2,
                canonical_svc=canonical,
                raw_service=service,
                country_input=country,
                country_fallback=country_fallback,
                max_price=max_price,
                opts=opts,
                wait_timeout=wait_timeout,
                skip_blacklisted=skip_blacklisted,
                is_async=is_async,
                webhook_url=webhook_url,
                explicit_webhook_url=raw_explicit_wh,
            )

        order_id = str(order["order_id"])
        if is_async:
            return order

        assert waiter is not None
        # 同步请求等待受管任务的取号阶段完成；如果客户端断开，仅取消本 await，不取消后台受管任务
        await waiter.wait()
        recorded_err = self._acquire_errors.pop(order_id, None)
        if recorded_err is not None:
            raise recorded_err
        refreshed = await self._db.orders.get(order_id, include_messages=True, include_activations=True)
        if refreshed is not None:
            saved_http = int(refreshed.get("idempotency_http_status") or 0)
            if saved_http >= 400:
                err_code = str(refreshed.get("idempotency_error_code") or refreshed.get("error_code") or "ORDER_FAILED")
                err_msg = str(refreshed.get("idempotency_error_message") or refreshed.get("error_message") or "取号失败")
                raise AppError(
                    err_msg,
                    status_code=saved_http,
                    code=err_code,
                    retryable=bool(refreshed.get("idempotency_retryable")),
                    details={"order_id": order_id},
                )
            return refreshed
        return order

    async def _managed_acquire_and_poll(
        self,
        *,
        order_id: str,
        provider_key: str,
        candidates: list[tuple[Any, str]],
        service_code: str,
        max_price: Optional[float],
        opts: AcquireOptions,
        wait_timeout: int,
        skip_blacklisted: bool,
        project_id: str,
        waiter: asyncio.Event,
        is_async: bool = False,
    ) -> None:
        """同步与异步共用的受管取号+轮询生命周期任务（修复补充 P1-01 同步异常漏补偿 & v5 复核 P1-01/P1-02）。"""
        acquired: Optional[dict[str, Any]] = None
        try:
            acquired = await self._acquire_number(
                order_id=order_id,
                provider_key=provider_key,
                candidates=candidates,
                service_code=service_code,
                max_price=max_price,
                opts=opts,
                wait_timeout=wait_timeout,
                skip_blacklisted=skip_blacklisted,
                project_id=project_id,
                is_async=is_async,
            )
        except asyncio.CancelledError:
            return
        except AppError as app_exc:
            if not is_async:
                if len(self._acquire_errors) >= 512:
                    self._acquire_errors.pop(next(iter(self._acquire_errors)), None)
                self._acquire_errors[order_id] = app_exc
        except Exception as exc:
            logger.error("[%s] 取号过程发生未预期异常，触发统一补偿交接: %s", order_id, exc, exc_info=True)
            err_msg = f"取号后处理异常，已转入退款补偿: {exc}"
            if not is_async:
                if len(self._acquire_errors) >= 512:
                    self._acquire_errors.pop(next(iter(self._acquire_errors)), None)
                self._acquire_errors[order_id] = AppError(
                    err_msg,
                    status_code=500,
                    code="ACQUIRE_POST_ERROR",
                    retryable=True,
                    details={"order_id": order_id},
                )
            try:
                cur = await self._db.orders.get(order_id, include_messages=False)
                acts = await self._db.activations.list_for_order(order_id)
                active_acts = [
                    a for a in acts
                    if str(a.get("activation_id") or "").strip() and a.get("state") in {"active", "refund_pending"}
                ]
                uncertain_acts = [
                    a for a in acts
                    if str(a.get("state") or "") in {"purchasing", "uncertain_purchase"}
                    and not str(a.get("activation_id") or "").strip()
                ]
                known_aid = str((cur or {}).get("activation_id") or "").strip() or (
                    str(active_acts[-1]["activation_id"]) if active_acts else ""
                )
                if known_aid:
                    for act in active_acts:
                        await self._db.activations.update_state(
                            int(act["id"]), state="refund_pending", last_error=str(exc)
                        )
                    idem_kw: dict[str, Any] = {} if is_async else {
                        "idempotency_http_status": 500,
                        "idempotency_retryable": True,
                        "idempotency_error_code": "ACQUIRE_POST_ERROR",
                        "idempotency_error_message": err_msg,
                    }
                    pending = await self._db.orders.transition_status(
                        order_id,
                        expected_statuses=("acquiring", "waiting_code", "waiting_next_code"),
                        new_status="compensation_pending",
                        activation_id=known_aid,
                        refund_state="pending",
                        error_code="ACQUIRE_POST_ERROR",
                        error_message=err_msg,
                        **idem_kw,
                    )
                    if pending:
                        self._emit("order.status_changed", pending)
                elif uncertain_acts:
                    async def _mark_uncertain_tx(raw, now):
                        for act in uncertain_acts:
                            await self._db.activations.update_state_in_tx(
                                raw,
                                int(act["id"]),
                                state="uncertain_purchase",
                                now=now,
                                last_error=f"取号落库或后处理异常，购买结果未知: {exc}",
                            )

                    idem_kw = {} if is_async else {
                        "idempotency_http_status": 502,
                        "idempotency_retryable": False,
                        "idempotency_error_code": "PURCHASE_UNCERTAIN",
                        "idempotency_error_message": f"取号后处理异常且存在未知购买结果: {exc}",
                    }
                    if not is_async:
                        self._acquire_errors[order_id] = AppError(
                            f"取号后处理异常且存在未知购买结果，请核对上游账单: {exc}",
                            status_code=502,
                            code="PURCHASE_UNCERTAIN",
                            retryable=False,
                            details={"order_id": order_id},
                        )
                    await self._finalize_order_status(
                        order_id,
                        expected_statuses=("acquiring", "waiting_code", "waiting_next_code"),
                        new_status="failed",
                        base_order=cur,
                        fallback_project_id=project_id,
                        activation_tx_hook=_mark_uncertain_tx,
                        refund_state="unknown",
                        settlement_state="uncertain",
                        error_code="PURCHASE_UNCERTAIN",
                        error_message=f"取号后处理异常，存在未确认购买结果: {exc}",
                        **idem_kw,
                    )
                else:
                    idem_kw = {} if is_async else {
                        "idempotency_http_status": 500,
                        "idempotency_retryable": True,
                        "idempotency_error_code": "INTERNAL_ERROR",
                        "idempotency_error_message": str(exc),
                    }
                    await self._finalize_order_status(
                        order_id,
                        expected_statuses=("acquiring", "waiting_code", "waiting_next_code"),
                        new_status="failed",
                        base_order=cur,
                        fallback_project_id=project_id,
                        error_code="INTERNAL_ERROR",
                        error_message=str(exc),
                        **idem_kw,
                    )
            except Exception:
                # 即使补偿落库也因数据库瞬时错误失败，finally 中移出 _active_acquiring_orders 后
                # 下次 reconcile_pending() 仍可通过 order_activations(state='active') 发现并自动退款
                logger.warning("[%s] 写入补偿交接状态失败，留待后台巡检通过激活台账接管", order_id, exc_info=True)
        finally:
            self._active_acquiring_orders.discard(order_id)
            waiter.set()
            self._acquire_waiters.pop(order_id, None)

        if acquired is None:
            if self._tasks.get(order_id) is asyncio.current_task():
                self._tasks.pop(order_id, None)
            return

        try:
            await self._poll_code_loop(order_id)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("[%s] 后台等码轮询异常退出（待周期维护自动接管）: %s", order_id, exc, exc_info=True)
        finally:
            if self._tasks.get(order_id) is asyncio.current_task():
                self._tasks.pop(order_id, None)

    async def _acquire_number(
        self,
        *,
        order_id: str,
        provider_key: str,
        candidates: list[tuple[Any, str]],
        service_code: str,
        max_price: Optional[float],
        opts: AcquireOptions,
        wait_timeout: int,
        skip_blacklisted: bool,
        project_id: str,
        is_async: bool = False,
    ) -> Optional[dict[str, Any]]:
        provider = self._registry.get(provider_key)
        sleep_seconds = polling_interval_seconds(
            request_interval_enabled=opts.request_interval_enabled,
            request_interval=opts.request_interval,
            continuous_poll=opts.continuous_poll,
        )
        max_attempts = max(1, int(opts.max_attempts or 1))
        attempts_used = 0
        candidate_idx = 0
        last_error_code = "NO_NUMBERS"
        last_error_msg = "暂无可用号码"
        terminal_refund_state = "none"
        terminal_settlement_state = "none"
        price_exceeded_pending = False

        while opts.continuous_poll or attempts_used < max_attempts:
            # 确认订单尚未被并发取消
            cur_state = await self._db.orders.get(order_id, include_messages=False)
            if not cur_state or cur_state["status"] != "acquiring":
                return None

            cid, clabel = candidates[candidate_idx % len(candidates)]
            candidate_idx += 1
            attempts_used += 1
            activation: Optional[Activation] = None
            act_row_id: Optional[int] = None

            upd_attempt = await self._db.orders.update(
                order_id,
                country_id=str(cid),
                country_label=clabel,
                attempts_used=attempts_used,
            )
            if upd_attempt is not None and attempts_used > 1:
                self._emit("order.status_changed", upd_attempt)

            try:
                if opts.price_lock and opts.target_price is not None:
                    entries = await provider.get_prices(cid, service_code)
                    available, stock = locked_price_available(entries, opts.target_price)
                    if not available:
                        raise NoNumbersError(f"锁定价格档位 ${opts.target_price} 暂无库存 (stock={stock})")

                # P1-03: 购买前先持久化购买意图
                act_row_id = await self._db.activations.record_intent(
                    order_id=order_id,
                    attempt_no=attempts_used,
                    provider=provider_key,
                    country_id=str(cid),
                    service_code=service_code,
                    quoted_max_price=max_price,
                )

                activation = await provider.request_number(cid, service_code, max_price)

                # P1-03 & 复核 P1-01 & 补充 P1-05: 取号返回后立刻在单事务内用可追踪任务原子绑定激活台账、订单与成本口径；
                # 若并发 cancel_order 取消取号任务，cancel_order 会等待 _inflight_binds 完成，彻底杜绝 detached shield 竞态！
                actual_cost = None if activation.price_is_estimated else activation.price
                estimated_cost = activation.price if activation.price_is_estimated else None
                cost_status = (
                    "unknown"
                    if activation.price is None
                    else ("estimated" if activation.price_is_estimated else "actual")
                )
                currency = (getattr(activation, "currency", None) or "").strip().upper() or "UNKNOWN"
                currency_source = str(getattr(activation, "currency_source", "") or "").strip()
                bind_task = asyncio.create_task(
                    self._db.activations.bind_acquired_atomic(
                        act_row_id,
                        order_id=order_id,
                        activation_id=activation.activation_id,
                        phone_raw=activation.phone_number,
                        actual_cost=actual_cost,
                        estimated_cost=estimated_cost,
                        cost_status=cost_status,
                        currency=currency,
                        currency_source=currency_source,
                        state="active",
                    )
                )
                self._inflight_binds[order_id] = bind_task
                bound_ok = False
                try:
                    bound_ok = bool(await asyncio.shield(bind_task))
                except asyncio.CancelledError:
                    raise
                except Exception as bind_exc:
                    # 买号已成功但本地写库失败：立刻尝试上游止损取消并尽力记录对账证据，绝不静默丢弃 activation_id！
                    logger.error(
                        "[%s] 买号成功 (aid=%s) 但绑定落库失败，尝试立即上游止损取消: %s",
                        order_id,
                        mask_identifier(activation.activation_id),
                        bind_exc,
                    )
                    try:
                        cancel_out = await self._provider_cancel(provider, activation.activation_id)
                        best_state = (
                            "refund_confirmed"
                            if cancel_out.refund_confirmed
                            else ("unrefundable" if cancel_out.settled else "uncertain_purchase")
                        )
                        try:
                            await self._db.activations.update_state(
                                act_row_id,
                                state=best_state,
                                last_error=f"绑定落库失败 ({bind_exc})，止损取消结果={cancel_out.status}",
                            )
                        except Exception:
                            pass
                    except Exception:
                        try:
                            await self._db.activations.update_state(
                                act_row_id,
                                state="uncertain_purchase",
                                last_error=f"绑定落库失败且止损取消异常: {bind_exc}",
                            )
                        except Exception:
                            pass
                    raise
                finally:
                    if self._inflight_binds.get(order_id) is bind_task and bind_task.done():
                        self._inflight_binds.pop(order_id, None)

                if not bound_ok:
                    # 购买意图行在取号期间已被并发取消或人工核销：立即补偿取消上游刚分配的激活
                    await self._provider_cancel(provider, activation.activation_id)
                    return None

                # 复核 P1-03: 对支持精确成交价回报的平台，校验成交价未越过锁定目标价
                if (
                    (opts.price_lock or opts.strict_exact_price)
                    and getattr(provider.capabilities, "supports_exact_price_lock", False)
                    and opts.target_price is not None
                    and actual_cost is not None
                    and actual_cost > opts.target_price
                ):
                    cancel_outcome = await self._provider_cancel(provider, activation.activation_id)
                    ref_amt, ref_src = (
                        self._resolve_refund_evidence(provider, cancel_outcome, {"actual_cost": actual_cost})
                        if cancel_outcome.refund_confirmed
                        else (None, "")
                    )
                    act_pe_state = (
                        "refund_confirmed"
                        if cancel_outcome.refund_confirmed
                        else ("refund_pending" if cancel_outcome.retryable else "unrefundable")
                    )
                    await self._db.activations.update_state(
                        act_row_id,
                        state=act_pe_state,
                        last_error=f"成交价 {actual_cost} 高于目标价 {opts.target_price}",
                        refund_amount=ref_amt,
                        refund_amount_source=ref_src,
                    )
                    last_error_code = "PRICE_EXCEEDED"
                    last_error_msg = f"上游实际成交价 {currency} {actual_cost} 高于目标价 {opts.target_price}"
                    if cancel_outcome.refund_confirmed:
                        terminal_refund_state = "confirmed"
                        terminal_settlement_state = "cancelled"
                    elif cancel_outcome.retryable:
                        terminal_refund_state = "pending"
                        terminal_settlement_state = "cancelling"
                        price_exceeded_pending = True
                    else:
                        terminal_refund_state = "unrefundable"
                        terminal_settlement_state = "cancelled"
                    break

                phone_input = await provider.normalize_phone(activation.phone_number, cid)
                phone_e164 = phone_input.e164_number or (
                    activation.phone_number
                    if activation.phone_number.startswith("+")
                    else f"+{activation.phone_number}"
                )
                await self._db.activations.update_state(act_row_id, state="active", phone_e164=phone_e164)

                # P1-10: 检查全局黑名单与本项目私有黑名单
                bl_project = "" if project_id == "web-console" else project_id
                if skip_blacklisted and (
                    await self._db.blacklist.is_blacklisted(activation.phone_number, provider=provider_key, project_id=bl_project)
                    or await self._db.blacklist.is_blacklisted(phone_e164, provider=provider_key, project_id=bl_project)
                ):
                    logger.info("[%s] 号码命中黑名单，尝试安全退款跳过: phone=%s", order_id, mask_phone(phone_e164))
                    cancel_outcome = await self._provider_cancel(provider, activation.activation_id)

                    # P1-03: 仅当上游明确确认退款后才继续购买下一个号；否则停止自动购买并进入补偿跟踪！
                    if cancel_outcome.refund_confirmed:
                        ref_amt, ref_src = self._resolve_refund_evidence(
                            provider, cancel_outcome, {"actual_cost": actual_cost}
                        )
                        await self._db.activations.update_state(
                            act_row_id,
                            state="refund_confirmed",
                            last_error="命中黑名单已确认退款",
                            refund_amount=ref_amt,
                            refund_amount_source=ref_src,
                        )
                        await self._db.orders.update(
                            order_id,
                            activation_id="",
                            phone_raw="",
                            phone_e164="",
                            phone_national="",
                            cost=None,
                            actual_cost=None,
                            cost_status="unknown",
                            cost_is_estimated=False,
                        )
                        last_error_code = "BLACKLISTED"
                        last_error_msg = "取到黑名单号码，已确认退款并跳过"
                        if sleep_seconds > 0:
                            await asyncio.sleep(sleep_seconds)
                        continue

                    # 退款未确认：立即中止后续购买防止重复扣费，保留补偿台账；不可退终态同事务写 Outbox
                    act_state = "refund_pending" if cancel_outcome.retryable else "unrefundable"
                    await self._db.activations.update_state(
                        act_row_id,
                        state=act_state,
                        last_error=f"黑名单取消未确认退款: {cancel_outcome.raw or cancel_outcome.status}",
                    )
                    err_msg = (
                        f"黑名单号码取消退款未确认 ({cancel_outcome.status})，"
                        "已停止继续取号并转入后台补偿对账"
                    )
                    idem_bl_kw: dict[str, Any] = {} if is_async else {
                        "idempotency_http_status": 409,
                        "idempotency_retryable": bool(cancel_outcome.retryable),
                        "idempotency_error_code": "BLACKLIST_REFUND_UNCONFIRMED",
                        "idempotency_error_message": err_msg,
                    }
                    if cancel_outcome.retryable:
                        pending_order = await self._db.orders.transition_status(
                            order_id,
                            expected_statuses=("acquiring",),
                            new_status="compensation_pending",
                            refund_state="pending",
                            error_code="BLACKLIST_REFUND_UNCONFIRMED",
                            error_message=err_msg,
                            **idem_bl_kw,
                        )
                        if pending_order:
                            self._emit("order.status_changed", pending_order)
                    else:
                        await self._finalize_order_status(
                            order_id,
                            expected_statuses=("acquiring",),
                            new_status="failed",
                            fallback_project_id=project_id,
                            refund_state="unrefundable",
                            settlement_state="cancelled",
                            error_code="BLACKLIST_REFUND_UNCONFIRMED",
                            error_message=err_msg,
                            **idem_bl_kw,
                        )
                    raise AppError(
                        f"命中黑名单号码且退款暂未确认 ({cancel_outcome.status})，已停止重复取号并记录待补偿",
                        status_code=409,
                        code="BLACKLIST_REFUND_UNCONFIRMED",
                        retryable=cancel_outcome.retryable,
                        details={"order_id": order_id, "activation_id": activation.activation_id},
                    )

                expires_at = (_utc_now() + timedelta(seconds=wait_timeout)).isoformat()
                order_preview = {
                    **(cur_state or {}),
                    "order_id": order_id,
                    "status": "waiting_code",
                    "country_id": str(cid),
                    "phone_e164": phone_e164,
                    "refund_state": "none",
                }
                outbox = await self._make_outbox_entry("order.acquired", order_preview)
                idem_ok_kw: dict[str, Any] = {} if is_async else {
                    "idempotency_http_status": 201,
                    "idempotency_retryable": False,
                    "idempotency_error_code": "",
                    "idempotency_error_message": "",
                }
                updated = await self._db.orders.transition_status(
                    order_id,
                    expected_statuses=("acquiring",),
                    new_status="waiting_code",
                    country_id=str(cid),
                    country_label=activation.country_label or clabel,
                    country_iso2=phone_input.country_iso2 or "",
                    country_calling_code=phone_input.country_calling_code or "",
                    activation_id=activation.activation_id,
                    phone_raw=activation.phone_number,
                    phone_e164=phone_e164,
                    phone_national=phone_input.phone or activation.phone_number,
                    cost=activation.price,
                    actual_cost=actual_cost,
                    cost_status=cost_status,
                    cost_is_estimated=activation.price_is_estimated,
                    currency=currency,
                    expires_at=expires_at,
                    error_code="",
                    error_message="",
                    outbox_entry=outbox,
                    **idem_ok_kw,
                )
                if updated is None:
                    # 订单在取号期间已被并发取消：立即补偿取消刚刚买到的 activation
                    cancel_outcome = await self._provider_cancel(provider, activation.activation_id)
                    ref_amt, ref_src = (
                        self._resolve_refund_evidence(provider, cancel_outcome, {"actual_cost": actual_cost})
                        if cancel_outcome.refund_confirmed
                        else (None, "")
                    )
                    await self._db.activations.update_state(
                        act_row_id,
                        state="refund_confirmed" if cancel_outcome.refund_confirmed else "refund_pending",
                        last_error="订单取号期间已被取消",
                        refund_amount=ref_amt,
                        refund_amount_source=ref_src,
                    )
                    return None

                self._notify_outbox()
                logger.info(
                    "[%s] 取号成功: provider=%s service=%s activation=%s phone=%s",
                    order_id,
                    provider_key,
                    service_code,
                    mask_identifier(activation.activation_id),
                    mask_phone(phone_e164),
                )
                self._emit("order.acquired", updated)
                return updated

            except BalanceError as exc:
                if act_row_id is not None:
                    await self._db.activations.update_state(act_row_id, state="failed", last_error=str(exc))
                last_error_code = "INSUFFICIENT_BALANCE"
                last_error_msg = str(exc)
                break
            except PurchaseUncertainError as exc:
                # P1-03: 上游购买结果未知，标记 uncertain_purchase 保留对账线索，停止自动重试
                if act_row_id is not None:
                    await self._db.activations.update_state(act_row_id, state="uncertain_purchase", last_error=str(exc))
                last_error_code = "PURCHASE_UNCERTAIN"
                last_error_msg = str(exc)
                break
            except NoNumbersError as exc:
                if act_row_id is not None:
                    await self._db.activations.update_state(act_row_id, state="no_numbers", last_error=str(exc))
                last_error_code = "NO_NUMBERS"
                last_error_msg = str(exc)
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)
                continue
            except SmsProviderError as exc:
                last_error_code = "PROVIDER_ERROR"
                last_error_msg = str(exc)
                if activation is not None and act_row_id is not None:
                    cancel_outcome = await self._provider_cancel(provider, activation.activation_id)
                    act_cost_tmp = None if activation.price_is_estimated else activation.price
                    ref_amt, ref_src = (
                        self._resolve_refund_evidence(provider, cancel_outcome, {"actual_cost": act_cost_tmp})
                        if cancel_outcome.refund_confirmed
                        else (None, "")
                    )
                    if cancel_outcome.refund_confirmed:
                        await self._db.activations.update_state(
                            act_row_id,
                            state="refund_confirmed",
                            last_error=str(exc),
                            refund_amount=ref_amt,
                            refund_amount_source=ref_src,
                        )
                        await self._db.orders.update(
                            order_id,
                            activation_id="",
                            phone_raw="",
                            phone_e164="",
                            phone_national="",
                            cost=None,
                            actual_cost=None,
                            cost_status="unknown",
                            cost_is_estimated=False,
                        )
                    elif cancel_outcome.retryable:
                        await self._db.activations.update_state(
                            act_row_id,
                            state="refund_pending",
                            last_error=str(exc),
                        )
                        price_exceeded_pending = True
                        break
                    else:
                        await self._db.activations.update_state(
                            act_row_id,
                            state="unrefundable",
                            last_error=str(exc),
                        )
                        terminal_refund_state = "unrefundable"
                        terminal_settlement_state = "cancelled"
                        break
                elif act_row_id is not None:
                    await self._db.activations.update_state(act_row_id, state="failed", last_error=str(exc))
                if not opts.continuous_poll and attempts_used >= max_attempts:
                    break
                await asyncio.sleep(max(1.0, sleep_seconds))
                continue

        fail_http_status = self._error_status_for_code(last_error_code)
        fail_retryable = last_error_code in {"NO_NUMBERS", "BLACKLISTED", "PROVIDER_ERROR"}
        cur_state = await self._db.orders.get(order_id, include_messages=False)
        idem_fail_kw: dict[str, Any] = {} if is_async else {
            "idempotency_http_status": fail_http_status,
            "idempotency_retryable": fail_retryable,
            "idempotency_error_code": last_error_code,
            "idempotency_error_message": last_error_msg,
        }
        if price_exceeded_pending:
            pending_pe = await self._db.orders.transition_status(
                order_id,
                expected_statuses=("acquiring",),
                new_status="compensation_pending",
                refund_state="pending",
                settlement_state="cancelling",
                error_code=last_error_code,
                error_message=last_error_msg,
                **idem_fail_kw,
            )
            if pending_pe:
                self._emit("order.status_changed", pending_pe)
        else:
            eff_refund = "unknown" if last_error_code == "PURCHASE_UNCERTAIN" else terminal_refund_state
            eff_settle = "uncertain" if last_error_code == "PURCHASE_UNCERTAIN" else terminal_settlement_state
            await self._finalize_order_status(
                order_id,
                expected_statuses=("acquiring",),
                new_status="failed",
                base_order=cur_state,
                fallback_project_id=project_id,
                refund_state=eff_refund,
                settlement_state=eff_settle,
                error_code=last_error_code,
                error_message=last_error_msg,
                **idem_fail_kw,
            )

        raise AppError(
            last_error_msg,
            status_code=fail_http_status,
            code=last_error_code,
            retryable=fail_retryable,
            details={"order_id": order_id},
        )

    # ── 按需等码轮询器（修复 P1-04 / P1-07）──────────────────────

    def _start_code_poller(
        self,
        order_id: str,
        *,
        previous_code: str = "",
        initial_seen_wait: Optional[bool] = None,
    ) -> None:
        self._cancel_bg_task(order_id)
        task = asyncio.create_task(
            self._poll_code_loop(
                order_id,
                previous_code=previous_code,
                initial_seen_wait=initial_seen_wait,
            )
        )
        self._tasks[order_id] = task

    async def _try_persist_received_code(
        self,
        order: dict[str, Any],
        code: str,
        *,
        raw_text: str = "",
        observed_scrub_version: int,
    ) -> str:
        """在同激活上按脱敏版本号幂等写入验证码与 Outbox（修复 v5 P1-02、v6 P0-01 与审查 Finding 8）。

        返回值：
        - "persisted"：写入成功（或写入后并发脱敏但已完成记录），结束轮询；
        - "inactive"：订单已离开等码态，结束轮询；
        - "stale_scrub"：查码期间订单触发了脱敏版本变更但仍处于等码态，丢弃本次旧码并继续轮询！
        - "retry"：发生可重试写库异常，保留在同激活上重试。
        """
        order_id = str(order["order_id"])
        try:
            target_url = await self._resolve_webhook_target(order)

            def _build_outbox(next_seq: int, _now: str) -> Optional[dict[str, Any]]:
                if not target_url:
                    return None
                ev_id = f"evt_{order_id}_code_{next_seq}"
                dlv_id = f"dlv_{order_id}_code_{next_seq}"
                snap = {
                    **order,
                    "status": "code_received",
                    "latest_code": code,
                    "code_count": next_seq,
                }
                return {
                    "delivery_id": dlv_id,
                    "event_id": ev_id,
                    "event_type": "order.code_received",
                    "order_id": order_id,
                    "project_id": str(order.get("project_id") or ""),
                    "target_url": target_url,
                    "payload": build_minimal_webhook_payload("order.code_received", ev_id, snap),
                }

            result = await self._db.orders.append_code(
                order_id,
                code,
                raw_text or code,
                expected_scrub_version=observed_scrub_version,
                outbox_entry_builder=_build_outbox,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_code_persist_errors[order_id] = str(exc)
            logger.warning("[%s] 验证码落库异常，将在同激活上继续重试: %s", order_id, exc)
            return "retry"

        self._last_code_persist_errors.pop(order_id, None)
        if result is None:
            latest = await self._db.orders.get(order_id, include_messages=False)
            if latest and latest.get("status") in {"waiting_code", "waiting_next_code"} and (
                int(latest.get("code_scrub_version") or 0) != observed_scrub_version
            ):
                logger.info(
                    "[%s] 查码在途遭遇脱敏版本变更 (v%s -> v%s)，丢弃该次旧码并保持轮询继续等待新短信",
                    order_id,
                    observed_scrub_version,
                    latest.get("code_scrub_version"),
                )
                return "stale_scrub"
            return "inactive"
        updated, message = result
        # v6 复核 P0-01: 提交与回读窗口之间若已被脱敏，绝不推送旧版本明文 SSE 或触发旧 Outbox！
        if bool(updated.get("code_scrubbed")) or int(updated.get("code_scrub_version") or 0) != observed_scrub_version:
            logger.info("[%s] 收码提交后已被并发脱敏 (v%s -> v%s)，跳过旧码事件发布", order_id, observed_scrub_version, updated.get("code_scrub_version"))
            return "persisted"
        self._notify_outbox()
        logger.info("[%s] 收到验证码: seq=%s digits=%s", order_id, message["seq"], len(code))
        self._emit(
            "order.code_received",
            updated,
            extra={"message": message},
            code_scrub_version=observed_scrub_version,
        )
        return "persisted"

    async def _handle_provider_finished_in_poll(self, order: dict[str, Any], activation_id: str) -> None:
        """上游查询回报 FINISH（如 5sim 已完成）时单事务原子落盘订单与激活台账，绝不误记为已确认退款（修复 v6 P1-04 & v7 P1-01）。"""
        order_id = str(order["order_id"])
        async with self._lock_for(order_id):
            latest = await self._db.orders.get(order_id, include_messages=False)
            if not latest or latest["status"] not in {"waiting_code", "waiting_next_code"}:
                return

            if int(latest.get("code_count") or 0) > 0:
                async def _act_completed_tx(raw, now):
                    await self._db.activations.ensure_activation_state_in_tx(
                        raw,
                        order_id,
                        activation_id,
                        provider=str(latest["provider"]),
                        state="completed",
                        now=now,
                        last_error="上游激活已完成",
                        country_id=str(latest.get("country_id") or ""),
                        service_code=str(latest.get("service_code") or ""),
                        currency=str(latest.get("currency") or ""),
                    )

                await self._finalize_order_status(
                    order_id,
                    expected_statuses=("waiting_code", "waiting_next_code"),
                    new_status="completed",
                    base_order=latest,
                    activation_tx_hook=_act_completed_tx,
                    scrub_if_zero_retention=True,
                    refund_state="none",
                    settlement_state="completed",
                    error_code="",
                    error_message="",
                )
                return

            async def _act_unrefundable_tx(raw, now):
                await self._db.activations.ensure_activation_state_in_tx(
                    raw,
                    order_id,
                    activation_id,
                    provider=str(latest["provider"]),
                    state="unrefundable",
                    now=now,
                    last_error="上游激活返回 FINISH（无退款确认）",
                    country_id=str(latest.get("country_id") or ""),
                    service_code=str(latest.get("service_code") or ""),
                    currency=str(latest.get("currency") or ""),
                )

            await self._finalize_order_status(
                order_id,
                expected_statuses=("waiting_code", "waiting_next_code"),
                new_status="expired",
                base_order=latest,
                activation_tx_hook=_act_unrefundable_tx,
                scrub_if_zero_retention=True,
                refund_state="unrefundable",
                settlement_state="provider_finished",
                error_code="PROVIDER_FINISHED",
                error_message="激活已在上游结束且未收到验证码（无退款确认）",
            )

    async def _handle_provider_cancelled_in_poll_locked(self, order: dict[str, Any], activation_id: str) -> None:
        order_id = str(order["order_id"])
        latest = await self._db.orders.get(order_id, include_messages=False)
        if not latest or latest["status"] not in {"waiting_code", "waiting_next_code"}:
            return
        provider = self._registry.get(str(latest["provider"])) if self._registry.is_active(str(latest["provider"])) else None
        act_row = await self._db.activations.get_by_activation_id(order_id, activation_id)
        synthetic = CancelOutcome(settled=True, status="refund_confirmed", retryable=False, raw="STATUS_CANCEL", refund_confirmed=True)
        ref_amt, ref_src = self._resolve_refund_evidence(provider, synthetic, act_row)

        async def _act_cancel_tx(raw, now):
            await self._db.activations.ensure_activation_state_in_tx(
                raw,
                order_id,
                activation_id,
                provider=str(latest["provider"]),
                state="refund_confirmed",
                now=now,
                last_error="上游平台取消",
                country_id=str(latest.get("country_id") or ""),
                service_code=str(latest.get("service_code") or ""),
                currency=str(latest.get("currency") or ""),
                refund_amount=ref_amt,
                refund_amount_source=ref_src,
            )

        await self._finalize_order_status(
            order_id,
            expected_statuses=("waiting_code", "waiting_next_code"),
            new_status="cancelled",
            base_order=latest,
            activation_tx_hook=_act_cancel_tx,
            scrub_if_zero_retention=True,
            refund_state="confirmed",
            settlement_state="cancelled",
            error_code="PROVIDER_CANCELLED",
            error_message="激活已被上游平台取消",
        )

    async def _handle_provider_cancelled_in_poll(self, order: dict[str, Any], activation_id: str) -> None:
        async with self._lock_for(str(order["order_id"])):
            await self._handle_provider_cancelled_in_poll_locked(order, activation_id)

    async def _handle_provider_expired_in_poll(self, order: dict[str, Any], activation_id: str) -> None:
        order_id = str(order["order_id"])
        async with self._lock_for(order_id):
            latest = await self._db.orders.get(order_id, include_messages=False)
            if not latest or latest["status"] not in {"waiting_code", "waiting_next_code"}:
                return

            async def _act_exp_tx(raw, now):
                await self._db.activations.ensure_activation_state_in_tx(
                    raw,
                    order_id,
                    activation_id,
                    provider=str(latest["provider"]),
                    state="unrefundable",
                    now=now,
                    last_error="上游激活已过期或不存在",
                    country_id=str(latest.get("country_id") or ""),
                    service_code=str(latest.get("service_code") or ""),
                    currency=str(latest.get("currency") or ""),
                )

            await self._finalize_order_status(
                order_id,
                expected_statuses=("waiting_code", "waiting_next_code"),
                new_status="expired",
                base_order=latest,
                activation_tx_hook=_act_exp_tx,
                scrub_if_zero_retention=True,
                refund_state="unrefundable",
                settlement_state="cancelled",
                error_code="PROVIDER_EXPIRED",
                error_message="激活已过期或不存在",
            )

    async def _handle_code_persist_failure(
        self,
        order: dict[str, Any],
        *,
        activation_id: str,
        code_len: int,
        last_error: str,
    ) -> None:
        """上游已收短信但本地写码持续失败：单事务原子保存订单与激活台账对账证据，绝不引发孤儿激活误取消（修复 v5 P1-02 & 并发审查 Finding 2）。"""
        order_id = str(order["order_id"])
        async with self._lock_for(order_id):
            latest = await self._db.orders.get(order_id, include_messages=False)
            if not latest or latest["status"] not in {"waiting_code", "waiting_next_code"}:
                return

            upstream_status = "UNKNOWN"
            if activation_id and self._registry.is_active(str(latest["provider"])):
                try:
                    provider = self._registry.get(str(latest["provider"]))
                    upstream_status, _ = await provider.get_sms_code(activation_id)
                except Exception as exc:
                    logger.warning("[%s] 写码持续失败后复核上游状态异常: %s", order_id, exc)
                    upstream_status = "ERROR"

            if upstream_status == "CANCEL":
                await self._handle_provider_cancelled_in_poll_locked(latest, activation_id)
                return

            evidence_msg = (
                f"上游已返回验证码(digits={code_len}, upstream={upstream_status})但本地落库持续失败: {last_error}"
            )

            async def _act_persist_fail_tx(raw, now):
                if activation_id:
                    await self._db.activations.ensure_activation_state_in_tx(
                        raw,
                        order_id,
                        activation_id,
                        provider=str(latest["provider"]),
                        state="code_persist_failed",
                        now=now,
                        last_error=evidence_msg,
                        country_id=str(latest.get("country_id") or ""),
                        service_code=str(latest.get("service_code") or ""),
                        currency=str(latest.get("currency") or ""),
                    )

            # 重发短信场景保留已收到的首条验证码，同事务同步激活对账证据
            if int(latest.get("code_count") or 0) > 0:
                updated = await self._db.orders.transition_status(
                    order_id,
                    expected_statuses=("waiting_next_code",),
                    new_status="code_received",
                    settlement_state="code_persist_failed",
                    resend_base_code="",
                    resend_seen_wait=False,
                    error_code="CODE_PERSIST_FAILED",
                    error_message=f"重发短信已到达但本地落库持续失败 (upstream={upstream_status})，已保留首码与对账证据",
                    tx_hook=_act_persist_fail_tx,
                )
                if updated:
                    self._emit("order.status_changed", updated)
                return

            # 首条短信写入持续失败：上游已交付短信，单事务原子写入 failed + code_persist_failed 激活证据，绝不被补偿误取消！
            refund_state = "unrefundable" if upstream_status in {"OK", "EXPIRED"} else "unknown"
            await self._finalize_order_status(
                order_id,
                expected_statuses=("waiting_code", "waiting_next_code"),
                new_status="failed",
                base_order=latest,
                activation_tx_hook=_act_persist_fail_tx,
                refund_state=refund_state,
                settlement_state="code_persist_failed",
                error_code="CODE_PERSIST_FAILED",
                error_message=f"上游已收到验证码但本地写入持续失败 (upstream={upstream_status})，已保留对账证据等待核对",
            )

    async def _poll_code_loop(
        self,
        order_id: str,
        *,
        previous_code: str = "",
        initial_seen_wait: Optional[bool] = None,
    ) -> None:
        started = asyncio.get_running_loop().time()
        base_code = previous_code
        seen_wait_since_resend = (not bool(base_code)) if initial_seen_wait is None else bool(initial_seen_wait)
        last_seen_scrub_ver: Optional[int] = None
        pending_code = ""
        pending_raw_text = ""
        pending_code_scrub_version: Optional[int] = None
        ignored_scrubbed_codes: set[str] = set()
        code_write_failures = 0
        consecutive_loop_errors = 0

        try:
            while True:
                try:
                    order = await self._db.orders.get(order_id, include_messages=False)
                    if not order or order["status"] not in {"waiting_code", "waiting_next_code"}:
                        return

                    current_scrub_ver = int(order.get("code_scrub_version") or 0)
                    if last_seen_scrub_ver is not None and current_scrub_ver != last_seen_scrub_ver:
                        # 脱敏版本提升时同步清空内存中的旧基准码，避免旧码泄露或绕过 WAIT 检查
                        base_code = ""
                    last_seen_scrub_ver = current_scrub_ver

                    # v6 复核 P0-01: 若在内存等待重试写码期间触发了脱敏清理，立即丢弃旧版本待写码，绝不回写库或入队 Webhook！
                    if pending_code and pending_code_scrub_version is not None and current_scrub_ver != pending_code_scrub_version:
                        logger.info(
                            "[%s] 脱敏版本已变更 (v%s -> v%s)，丢弃内存中过期待写验证码",
                            order_id,
                            pending_code_scrub_version,
                            current_scrub_ver,
                        )
                        ignored_scrubbed_codes.add(pending_code)
                        pending_code = ""
                        pending_raw_text = ""
                        pending_code_scrub_version = None
                        code_write_failures = 0

                    # 兜底同步 DB 中持久化的重发基准（P1-07 & v7 P0-01）
                    if order["status"] == "waiting_next_code":
                        if order.get("code_scrubbed"):
                            base_code = ""
                        elif not base_code and order.get("resend_base_code"):
                            base_code = str(order["resend_base_code"])
                        if order.get("resend_seen_wait"):
                            seen_wait_since_resend = True

                    expires_dt = _parse_iso(str(order.get("expires_at") or ""))
                    is_expired = bool(expires_dt and _utc_now() >= expires_dt)
                    provider_key = str(order["provider"])
                    activation_id = str(order["activation_id"])

                    # 若上游已返回新码但本地首次落库失败：优先在同一激活上按原脱敏版本重试写入
                    if pending_code:
                        assert pending_code_scrub_version is not None
                        persist_res = await self._try_persist_received_code(
                            order,
                            pending_code,
                            raw_text=pending_raw_text or pending_code,
                            observed_scrub_version=pending_code_scrub_version,
                        )
                        if persist_res in {"persisted", "inactive"}:
                            return
                        if persist_res == "stale_scrub":
                            ignored_scrubbed_codes.add(pending_code)
                            pending_code = ""
                            pending_raw_text = ""
                            pending_code_scrub_version = None
                            code_write_failures = 0
                            continue
                        code_write_failures += 1
                        if code_write_failures >= self._max_code_write_retries or is_expired:
                            await self._handle_code_persist_failure(
                                order,
                                activation_id=activation_id,
                                code_len=len(pending_code),
                                last_error=self._last_code_persist_errors.pop(order_id, "本地验证码写入持续失败"),
                            )
                            return
                    else:
                        if is_expired:
                            await self._handle_poll_timeout(
                                order,
                                expected_expires_at=str(order.get("expires_at") or ""),
                            )
                            return

                        try:
                            provider = self._registry.get(provider_key)
                            status, code = await provider.get_sms_code(activation_id)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning("[%s] 查码网络异常（保持原重发去重进度）: %s", order_id, exc)
                            # P1-07: 网络错误绝不伪装成 WAIT，防止提前放开重复码判定！
                            status, code = "ERROR", ""

                        if status == "OK" and code:
                            if order["status"] == "waiting_next_code":
                                if base_code:
                                    is_new_code = (
                                        (seen_wait_since_resend or (code != base_code))
                                        and code not in ignored_scrubbed_codes
                                    )
                                else:
                                    base_digest = str(order.get("resend_base_digest") or "")
                                    if base_digest:
                                        cand_digest = env.hmac_digest(f"resend:{order_id}", code)
                                        matches_old = hmac.compare_digest(cand_digest, base_digest)
                                        is_new_code = (
                                            (seen_wait_since_resend or not matches_old)
                                            and code not in ignored_scrubbed_codes
                                        )
                                    else:
                                        is_new_code = seen_wait_since_resend and code not in ignored_scrubbed_codes
                            else:
                                is_new_code = (
                                    (seen_wait_since_resend or (bool(base_code) and code != base_code))
                                    and code not in ignored_scrubbed_codes
                                )
                            if is_new_code:
                                raw_sms_fn = getattr(provider, "get_last_raw_sms", None)
                                cur_raw_text = str(raw_sms_fn(activation_id) if callable(raw_sms_fn) else "") or code
                                persist_res = await self._try_persist_received_code(
                                    order,
                                    code,
                                    raw_text=cur_raw_text,
                                    observed_scrub_version=current_scrub_ver,
                                )
                                if persist_res in {"persisted", "inactive"}:
                                    return
                                if persist_res == "stale_scrub":
                                    ignored_scrubbed_codes.add(code)
                                    base_code = ""
                                    continue
                                pending_code = code
                                pending_raw_text = cur_raw_text
                                pending_code_scrub_version = current_scrub_ver
                                code_write_failures += 1
                                if code_write_failures >= self._max_code_write_retries:
                                    await self._handle_code_persist_failure(
                                        order,
                                        activation_id=activation_id,
                                        code_len=len(code),
                                        last_error=self._last_code_persist_errors.pop(order_id, "本地验证码写入持续失败"),
                                    )
                                    return
                        elif status == "WAIT":
                            ignored_scrubbed_codes.clear()
                            if not seen_wait_since_resend:
                                seen_wait_since_resend = True
                                if order["status"] == "waiting_next_code":
                                    await self._db.orders.update(order_id, resend_seen_wait=True)
                        elif status == "CANCEL":
                            await self._handle_provider_cancelled_in_poll(order, activation_id)
                            return
                        elif status == "FINISHED":
                            await self._handle_provider_finished_in_poll(order, activation_id)
                            return
                        elif status == "EXPIRED":
                            await self._handle_provider_expired_in_poll(order, activation_id)
                            return

                    consecutive_loop_errors = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_loop_errors += 1
                    logger.warning(
                        "[%s] 等码轮询迭代异常 (attempt=%d/%d): %s",
                        order_id,
                        consecutive_loop_errors,
                        self._max_poll_loop_errors,
                        exc,
                        exc_info=True,
                    )
                    if consecutive_loop_errors >= self._max_poll_loop_errors:
                        if pending_code:
                            try:
                                cur = await self._db.orders.get(order_id, include_messages=False)
                                if cur and cur["status"] in {"waiting_code", "waiting_next_code"}:
                                    await self._handle_code_persist_failure(
                                        cur,
                                        activation_id=str(cur.get("activation_id") or ""),
                                        code_len=len(pending_code),
                                        last_error=str(exc),
                                    )
                            except Exception:
                                logger.error("[%s] 写码持续失败后记录对账证据亦失败", order_id, exc_info=True)
                        return

                elapsed = asyncio.get_running_loop().time() - started
                d1, d2, d3 = self._poll_intervals
                delay = d1 if elapsed < 60 else (d2 if elapsed < 180 else d3)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        finally:
            self._last_code_persist_errors.pop(order_id, None)
            if self._tasks.get(order_id) is asyncio.current_task():
                self._tasks.pop(order_id, None)

    async def _handle_poll_timeout(
        self,
        order: dict[str, Any],
        *,
        expected_expires_at: Optional[str] = None,
    ) -> None:
        """锁内复核最新状态、重发批次与最新截止时间后再执行超时结算，单事务原子同步激活台账（修复 v7 复核 P1-04 & 并发审查 Finding 2）。"""
        order_id = str(order["order_id"])
        restart_poller: Optional[tuple[str, bool]] = None
        async with self._lock_for(order_id):
            latest = await self._db.orders.get(order_id, include_messages=False)
            if not latest or latest["status"] not in {"waiting_code", "waiting_next_code"}:
                return

            latest_exp_raw = str(latest.get("expires_at") or "")
            if expected_expires_at is not None and latest_exp_raw != expected_expires_at:
                # 截止时间已被并发重发更新，丢弃旧快照超时请求
                return
            latest_exp_dt = _parse_iso(latest_exp_raw)
            if latest_exp_dt is not None and _utc_now() < latest_exp_dt:
                # 最新截止时间仍在未来，绝不可误判超时；若当前无活跃轮询且平台启用则顺手补启轮询
                if not self._has_live_task(order_id) and self._registry.is_active(str(latest.get("provider") or "")):
                    prev_code = (
                        str(latest.get("resend_base_code") or latest.get("latest_code") or "")
                        if latest["status"] == "waiting_next_code"
                        else ""
                    )
                    seen_wait = bool(latest.get("resend_seen_wait")) if latest["status"] == "waiting_next_code" else True
                    restart_poller = (prev_code, seen_wait)
                else:
                    return
            else:
                if int(latest.get("code_count") or 0) > 0:
                    updated = await self._db.orders.transition_status(
                        order_id,
                        expected_statuses=("waiting_next_code",),
                        new_status="code_received",
                        resend_base_code="",
                        resend_seen_wait=False,
                        error_code="RESEND_TIMEOUT",
                        error_message="重发短信等待超时，已保留先前收到的验证码",
                    )
                    if updated:
                        self._emit("order.status_changed", updated)
                    return

                # 未收到过短信：先 CAS 认领 cancelling，再向上游申请退款
                claimed = await self._db.orders.transition_status(
                    order_id,
                    expected_statuses=("waiting_code",),
                    new_status="cancelling",
                    settlement_state="cancelling",
                )
                if claimed is None:
                    return

                activation_id = str(latest.get("activation_id") or "")
                if not activation_id:
                    await self._finalize_order_status(
                        order_id,
                        expected_statuses=("cancelling",),
                        new_status="expired",
                        base_order=claimed,
                        refund_state="none",
                        settlement_state="cancelled",
                        error_code="SMS_TIMEOUT",
                        error_message=f"等码超时 ({latest.get('wait_timeout', 180)}s)",
                    )
                    return

                provider_key = str(latest["provider"]).strip().lower()
                async with self._activation_lock_for(provider_key, activation_id):
                    existing_act = await self._db.activations.get_by_activation_id(order_id, activation_id)
                    provider = self._registry.get(provider_key) if self._registry.is_active(provider_key) else None
                    if existing_act and existing_act.get("state") == "refund_confirmed":
                        outcome = CancelOutcome(
                            settled=True,
                            status="refund_confirmed",
                            retryable=False,
                            raw="CACHED_REFUND_CONFIRMED",
                            refund_confirmed=True,
                        )
                    elif existing_act and existing_act.get("state") in {"unrefundable", "completed", "manual_resolved"}:
                        outcome = CancelOutcome(
                            settled=True,
                            status=str(existing_act.get("state")),
                            retryable=False,
                            raw=f"CACHED_{str(existing_act.get('state')).upper()}",
                            refund_confirmed=False,
                        )
                    elif provider is None:
                        outcome = CancelOutcome(
                            settled=False,
                            status="provider_inactive",
                            retryable=True,
                            raw=f"provider '{provider_key}' not active",
                            refund_confirmed=False,
                        )
                    else:
                        try:
                            outcome = await self._provider_cancel(provider, activation_id)
                        except Exception as exc:
                            outcome = CancelOutcome(settled=False, status="error", retryable=True, raw=str(exc), refund_confirmed=False)

                    if outcome.refund_confirmed:
                        ref_amt, ref_src = self._resolve_refund_evidence(provider, outcome, existing_act)

                        async def _act_to_ref_tx(raw, now):
                            await self._db.activations.ensure_activation_state_in_tx(
                                raw,
                                order_id,
                                activation_id,
                                provider=str(latest["provider"]),
                                state="refund_confirmed",
                                now=now,
                                last_error="等码超时已确认退款",
                                country_id=str(latest.get("country_id") or ""),
                                service_code=str(latest.get("service_code") or ""),
                                currency=str(latest.get("currency") or ""),
                                refund_amount=ref_amt,
                                refund_amount_source=ref_src,
                                protect_settled=True,
                            )

                        await self._finalize_order_status(
                            order_id,
                            expected_statuses=("cancelling",),
                            new_status="expired",
                            base_order=claimed,
                            activation_tx_hook=_act_to_ref_tx,
                            refund_state="confirmed",
                            settlement_state="cancelled",
                            error_code="SMS_TIMEOUT",
                            error_message=f"等码超时 ({latest.get('wait_timeout', 180)}s)，已确认取消退款",
                        )
                        return
                    elif outcome.settled:
                        async def _act_to_unref_tx(raw, now):
                            await self._db.activations.ensure_activation_state_in_tx(
                                raw,
                                order_id,
                                activation_id,
                                provider=str(latest["provider"]),
                                state="unrefundable",
                                now=now,
                                last_error=outcome.raw,
                                country_id=str(latest.get("country_id") or ""),
                                service_code=str(latest.get("service_code") or ""),
                                currency=str(latest.get("currency") or ""),
                                protect_settled=True,
                            )

                        await self._finalize_order_status(
                            order_id,
                            expected_statuses=("cancelling",),
                            new_status="expired",
                            base_order=claimed,
                            activation_tx_hook=_act_to_unref_tx,
                            refund_state="unrefundable",
                            settlement_state="cancelled",
                            error_code="SMS_TIMEOUT",
                            error_message=f"等码超时，上游返回 {outcome.status}（未确认退款）",
                        )
                    else:
                        async def _act_to_pend_tx(raw, now):
                            await self._db.activations.ensure_activation_state_in_tx(
                                raw,
                                order_id,
                                activation_id,
                                provider=str(latest["provider"]),
                                state="refund_pending",
                                now=now,
                                last_error=outcome.raw,
                                country_id=str(latest.get("country_id") or ""),
                                service_code=str(latest.get("service_code") or ""),
                                currency=str(latest.get("currency") or ""),
                                protect_settled=True,
                            )

                        updated = await self._db.orders.transition_status(
                            order_id,
                            expected_statuses=("cancelling",),
                            new_status="compensation_pending",
                            refund_state="pending",
                            error_code="CANCEL_RETRY_PENDING",
                            error_message=f"等码超时，自动退款待后台重试: {outcome.raw or outcome.status}",
                            tx_hook=_act_to_pend_tx,
                        )
                        if updated:
                            self._emit("order.status_changed", updated)
                    return

        if restart_poller is not None:
            self._start_code_poller(order_id, previous_code=restart_poller[0], initial_seen_wait=restart_poller[1])

    async def _handle_provider_inactive_in_recovery(self, order: dict[str, Any]) -> None:
        """平台停用但订单尚未过期时，走独立原因分支而非误判为 SMS_TIMEOUT（修复 v7 复核 P1-04）。"""
        order_id = str(order["order_id"])
        async with self._lock_for(order_id):
            latest = await self._db.orders.get(order_id, include_messages=False)
            if not latest or latest["status"] not in {"waiting_code", "waiting_next_code"}:
                return
            if self._registry.is_active(str(latest.get("provider") or "")):
                return
            if int(latest.get("code_count") or 0) > 0:
                updated = await self._db.orders.transition_status(
                    order_id,
                    expected_statuses=("waiting_next_code",),
                    new_status="code_received",
                    resend_base_code="",
                    resend_seen_wait=False,
                    error_code="PROVIDER_NOT_ACTIVE",
                    error_message=f"平台 {latest.get('provider')} 已停用，暂停重发等待并保留已有验证码",
                )
                if updated:
                    self._emit("order.status_changed", updated)
                return
            updated = await self._db.orders.transition_status(
                order_id,
                expected_statuses=("waiting_code",),
                new_status="compensation_pending",
                refund_state="pending",
                error_code="PROVIDER_NOT_ACTIVE",
                error_message=f"平台 {latest.get('provider')} 当前未启用，待恢复启用后继续对账或人工核销",
            )
            if updated:
                self._emit("order.status_changed", updated)

    # ── 订单查询与 CAS 互斥生命周期操作（P1-04 / P1-07）──────────

    async def get_order(self, order_id: str) -> dict[str, Any]:
        order = await self._db.orders.get(order_id, include_messages=True, include_activations=True)
        if order is None:
            raise NotFoundError(f"订单 {order_id} 不存在", code="ORDER_NOT_FOUND")
        return order

    async def wait_for_code(
        self,
        order_id: str,
        *,
        timeout: int = 60,
        after_seq: int = 0,
        caller: Any = None,
        auth_service: Any = None,
    ) -> dict[str, Any]:
        from app.errors import AuthError

        POLLING_STATUSES = {"acquiring", "waiting_code", "waiting_next_code"}
        order = await self.get_order(order_id)
        if int(order.get("code_count") or 0) > after_seq or order["status"] not in POLLING_STATUSES:
            return order

        cred_project = "" if (caller is None or getattr(caller, "is_admin", False)) else str(getattr(caller, "project_id", "") or "")
        deadline = asyncio.get_running_loop().time() + max(1, min(int(timeout), 300))
        async with self._event_bus.subscribe_with_control(
            order_id=order_id,
            credential_project_id=cred_project,
        ) as (queue, revoke_event):
            order = await self.get_order(order_id)
            if int(order.get("code_count") or 0) > after_seq or order["status"] not in POLLING_STATUSES:
                return order

            while True:
                if caller is not None and auth_service is not None and not getattr(caller, "is_admin", False):
                    if not await auth_service.verify_active_credential(caller, required_scope="orders:r"):
                        raise AuthError("该 API Key 已被停用或轮转", code="AUTH_KEY_REVOKED")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return await self.get_order(order_id)

                get_task = asyncio.create_task(queue.get())
                rev_task = asyncio.create_task(revoke_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {get_task, rev_task},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for p in pending:
                        p.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)

                    if rev_task in done:
                        revoke_event.clear()
                        if caller is not None and auth_service is not None and not getattr(caller, "is_admin", False):
                            if not await auth_service.verify_active_credential(caller, required_scope="orders:r"):
                                raise AuthError("该 API Key 已被停用或轮转", code="AUTH_KEY_REVOKED")
                        if get_task not in done:
                            continue

                    if get_task not in done:
                        return await self.get_order(order_id)
                    event = get_task.result()
                except asyncio.CancelledError:
                    for t in (get_task, rev_task):
                        if not t.done():
                            t.cancel()
                    raise

                if caller is not None and auth_service is not None and not getattr(caller, "is_admin", False):
                    if not await auth_service.verify_active_credential(caller, required_scope="orders:r"):
                        raise AuthError("该 API Key 已被停用或轮转", code="AUTH_KEY_REVOKED")

                if event.event in {
                    "order.code_received",
                    "order.status_changed",
                    "order.completed",
                    "order.cancelled",
                    "order.expired",
                    "order.failed",
                }:
                    cur = await self.get_order(order_id)
                    if int(cur.get("code_count") or 0) > after_seq or cur["status"] not in POLLING_STATUSES:
                        return cur

    async def resend_sms(self, order_id: str) -> dict[str, Any]:
        async with self._lock_for(order_id):
            order = await self.get_order(order_id)
            if order["status"] != "code_received":
                raise ConflictError(
                    f"当前状态 '{order['status']}' 不支持请求重发，仅在收到验证码（code_received）后可用",
                    code="ORDER_INVALID_STATE",
                )
            try:
                provider = self._registry.get(order["provider"])
            except ValueError as exc:
                raise AppError(str(exc), status_code=400, code="PROVIDER_NOT_ACTIVE") from exc
            if not getattr(provider.capabilities, "supports_resend", False):
                raise AppError(f"平台 {order['provider']} 不支持请求重发短信", status_code=400, code="RESEND_UNSUPPORTED")

            outcome = await self._provider_resend(provider, order["activation_id"])
            if not outcome.ok:
                raise UpstreamError(
                    f"上游拒绝或重发失败 ({outcome.status}): {outcome.raw}",
                    code="RESEND_FAILED",
                    retryable=outcome.retryable,
                )

            previous_code = str(order.get("latest_code") or "")
            base_digest = (
                env.hmac_digest(f"resend:{order_id}", previous_code)
                if previous_code
                else str(order.get("resend_base_digest") or "")
            )
            expires_at = (_utc_now() + timedelta(seconds=int(order.get("wait_timeout") or 180))).isoformat()
            updated = await self._db.orders.transition_status(
                order_id,
                expected_statuses=("code_received",),
                new_status="waiting_next_code",
                expires_at=expires_at,
                resend_base_code=previous_code,
                resend_base_digest=base_digest,
                resend_seen_wait=False,
                error_code="",
                error_message="",
            )
            if updated is None:
                raise ConflictError("订单状态已变化，无法重发短信", code="ORDER_INVALID_STATE")
            self._emit("order.status_changed", updated)
            self._start_code_poller(order_id, previous_code=previous_code, initial_seen_wait=False)
            return updated

    async def complete_order(self, order_id: str) -> dict[str, Any]:
        """CAS 互斥完成订单：先校验平台启用再认领 completing，单事务原子提交终态/激活/即刻脱敏与 Outbox（修复 P1-04 & 审查 Finding 2/6）。"""
        async with self._lock_for(order_id):
            order = await self.get_order(order_id)
            if order["status"] == "completed":
                return order

            if str(order.get("activation_id") or "").strip() and not self._registry.is_active(str(order["provider"])):
                raise AppError(
                    f"平台 '{order['provider']}' 当前未启用，无法向上游确认完成订单",
                    status_code=400,
                    code="PROVIDER_NOT_ACTIVE",
                )

            prev_status = str(order["status"])
            claimed = await self._db.orders.transition_status(
                order_id,
                expected_statuses=("code_received", "waiting_code", "waiting_next_code"),
                new_status="completing",
                settlement_state="completing",
            )
            if claimed is None:
                latest = await self.get_order(order_id)
                if latest["status"] == "completed":
                    return latest
                raise ConflictError(
                    f"订单当前状态为 '{latest['status']}'，无法标记完成（可能正在取消或已结束）",
                    code="ORDER_INVALID_STATE",
                )

            bg = self._cancel_bg_task(order_id)
            if bg is not None:
                await asyncio.gather(bg, return_exceptions=True)

            activation_id = str(claimed.get("activation_id") or "")
            if activation_id:
                try:
                    provider = self._registry.get(claimed["provider"])
                except ValueError as exc:
                    rolled_back = await self._db.orders.transition_status(
                        order_id,
                        expected_statuses=("completing",),
                        new_status=prev_status,
                        settlement_state="none",
                    )
                    if rolled_back:
                        self._emit("order.status_changed", rolled_back)
                    raise AppError(str(exc), status_code=400, code="PROVIDER_NOT_ACTIVE") from exc
                outcome = await self._provider_complete(provider, activation_id)
                if not outcome.ok:
                    # 上游报错或拒绝：回滚至先前可恢复状态，记录 settlement_state='complete_failed' 并显式抛错！
                    rolled_back = await self._db.orders.transition_status(
                        order_id,
                        expected_statuses=("completing",),
                        new_status=prev_status,
                        settlement_state="complete_failed",
                        error_code="COMPLETE_FAILED",
                        error_message=f"上游完成确认失败 ({outcome.status}): {outcome.raw}",
                    )
                    if rolled_back:
                        self._emit("order.status_changed", rolled_back)
                    if prev_status in {"waiting_code", "waiting_next_code"}:
                        prev_code = (
                            str(claimed.get("resend_base_code") or "")
                            if (prev_status == "waiting_next_code" and not claimed.get("code_scrubbed"))
                            else ""
                        )
                        seen_wait = bool(claimed.get("resend_seen_wait")) if prev_status == "waiting_next_code" else True
                        self._start_code_poller(
                            order_id,
                            previous_code=prev_code,
                            initial_seen_wait=seen_wait,
                        )
                    raise UpstreamError(
                        f"确认完成失败，上游返回异常 ({outcome.status}): {outcome.raw}",
                        code="COMPLETE_FAILED",
                        retryable=outcome.retryable,
                    )

            async def _act_complete_tx(raw, now):
                if activation_id:
                    await self._db.activations.ensure_activation_state_in_tx(
                        raw,
                        order_id,
                        activation_id,
                        provider=str(claimed["provider"]),
                        state="completed",
                        now=now,
                        last_error="订单已完成",
                        country_id=str(claimed.get("country_id") or ""),
                        service_code=str(claimed.get("service_code") or ""),
                        currency=str(claimed.get("currency") or ""),
                    )

            updated = await self._finalize_order_status(
                order_id,
                expected_statuses=("completing",),
                new_status="completed",
                base_order=claimed,
                activation_tx_hook=_act_complete_tx,
                scrub_if_zero_retention=True,
                settlement_state="completed",
                error_code="",
                error_message="",
            )
            assert updated is not None
            return updated

    async def cancel_order(
        self,
        order_id: str,
        *,
        blacklist: bool = False,
        reason: str = "",
        blacklist_project_id: str = "",
    ) -> dict[str, Any]:
        """CAS 互斥取消订单：校验平台与本地缓存证据、等待在途绑定、单事务提交终态/激活/即刻脱敏与 Outbox。"""
        async with self._lock_for(order_id):
            order = await self.get_order(order_id)
            if order["status"] == "cancelled":
                return order

            prev_status = str(order["status"])
            pre_aid = str(order.get("activation_id") or "").strip()
            if pre_aid and not self._registry.is_active(str(order["provider"])):
                pre_act = await self._db.activations.get_by_activation_id(order_id, pre_aid)
                if not (pre_act and pre_act.get("state") in {"refund_confirmed", "unrefundable", "completed", "manual_resolved"}):
                    raise AppError(
                        f"平台 '{order['provider']}' 当前未启用，无法向上游发起取消退款",
                        status_code=400,
                        code="PROVIDER_NOT_ACTIVE",
                    )

            claimed = await self._db.orders.transition_status(
                order_id,
                expected_statuses=("acquiring", "waiting_code", "code_received", "waiting_next_code", "compensation_pending"),
                new_status="cancelling",
                settlement_state="cancelling",
            )
            if claimed is None:
                latest = await self.get_order(order_id)
                if latest["status"] == "cancelled":
                    return latest
                raise ConflictError(
                    f"订单当前状态为 '{latest['status']}'，无法取消（可能正在完成或已终结）",
                    code="ORDER_INVALID_STATE",
                )

            bg = self._cancel_bg_task(order_id)
            if bg is not None:
                await asyncio.gather(bg, return_exceptions=True)

            # 等待正在进行的 bind_acquired_atomic 提交完成，彻底消除 detached shield 竞态（并发审查 Finding 4.2）
            inflight_bind = self._inflight_binds.pop(order_id, None)
            if inflight_bind is not None and not inflight_bind.done():
                await asyncio.gather(inflight_bind, return_exceptions=True)

            # 重新读取确保拿到取号阶段刚刚落库的 activation_id
            latest = await self.get_order(order_id)
            if blacklist:
                phone_to_block = latest.get("phone_e164") or latest.get("phone_raw") or ""
                if phone_to_block:
                    await self._db.blacklist.add(
                        phone_to_block,
                        reason=reason or "接码订单取消并拉黑",
                        provider=latest["provider"],
                        project_id=blacklist_project_id,
                    )

            sync_cancel_idem_kw: dict[str, Any] = {}
            if (
                prev_status == "acquiring"
                and not latest.get("async_acquire")
                and int(latest.get("idempotency_http_status") or 0) == 0
            ):
                sync_cancel_idem_kw = {
                    "idempotency_http_status": 409,
                    "idempotency_retryable": False,
                    "idempotency_error_code": "USER_CANCELLED",
                    "idempotency_error_message": reason or "取号过程中订单已被取消",
                }

            activation_id = str(latest.get("activation_id") or "").strip()
            if not activation_id:
                acts = await self._db.activations.list_for_order(order_id)
                uncertain_acts = [
                    a for a in acts
                    if str(a.get("state") or "") in {"purchasing", "uncertain_purchase"}
                    and not str(a.get("activation_id") or "").strip()
                ]
                if uncertain_acts:
                    async def _mark_cancel_uncertain_tx(raw, now):
                        for act in uncertain_acts:
                            await self._db.activations.update_state_in_tx(
                                raw,
                                int(act["id"]),
                                state="uncertain_purchase",
                                now=now,
                                last_error="取号途中被取消，上游购买结果未知，待人工核对",
                            )

                    updated = await self._finalize_order_status(
                        order_id,
                        expected_statuses=("cancelling",),
                        new_status="cancelled",
                        base_order=latest,
                        activation_tx_hook=_mark_cancel_uncertain_tx,
                        refund_state="unknown",
                        settlement_state="uncertain",
                        error_code="PURCHASE_UNCERTAIN",
                        error_message=reason or "取号途中取消，上游购买结果未知，请核对上游账单",
                        **sync_cancel_idem_kw,
                    )
                    assert updated is not None
                    return updated

                updated = await self._finalize_order_status(
                    order_id,
                    expected_statuses=("cancelling",),
                    new_status="cancelled",
                    base_order=latest,
                    refund_state="none",
                    settlement_state="cancelled",
                    error_code="USER_CANCELLED",
                    error_message=reason or "已取消取号",
                    **sync_cancel_idem_kw,
                )
                assert updated is not None
                return updated

            provider_key = str(latest["provider"]).strip().lower()
            async with self._activation_lock_for(provider_key, activation_id):
                # v3 复核 P1-01 & v7 复核 P1-02: 若激活台账已留存终结证据，绝不重复向平台发起取消！
                existing_act = await self._db.activations.get_by_activation_id(order_id, activation_id)
                provider = self._registry.get(provider_key) if self._registry.is_active(provider_key) else None
                if existing_act and existing_act.get("state") == "refund_confirmed":
                    outcome = CancelOutcome(
                        settled=True,
                        status="refund_confirmed",
                        retryable=False,
                        raw="CACHED_REFUND_CONFIRMED",
                        refund_confirmed=True,
                    )
                elif existing_act and existing_act.get("state") in {"unrefundable", "completed", "manual_resolved"}:
                    outcome = CancelOutcome(
                        settled=True,
                        status=str(existing_act.get("state")),
                        retryable=False,
                        raw=f"CACHED_{str(existing_act.get('state')).upper()}",
                        refund_confirmed=False,
                    )
                elif provider is None:
                    rolled_back = await self._db.orders.transition_status(
                        order_id,
                        expected_statuses=("cancelling",),
                        new_status=prev_status,
                        settlement_state=str(order.get("settlement_state") or "none"),
                    )
                    if rolled_back:
                        self._emit("order.status_changed", rolled_back)
                    raise AppError(
                        f"平台 '{latest['provider']}' 当前未启用，无法向上游发起取消退款",
                        status_code=400,
                        code="PROVIDER_NOT_ACTIVE",
                    )
                else:
                    outcome = await self._provider_cancel(provider, activation_id)

                if outcome.refund_confirmed:
                    ref_amt, ref_src = self._resolve_refund_evidence(provider, outcome, existing_act)

                    async def _act_cancel_ref_tx(raw, now):
                        await self._db.activations.ensure_activation_state_in_tx(
                            raw,
                            order_id,
                            activation_id,
                            provider=str(latest["provider"]),
                            state="refund_confirmed",
                            now=now,
                            last_error=reason or "已取消并确认退款",
                            country_id=str(latest.get("country_id") or ""),
                            service_code=str(latest.get("service_code") or ""),
                            currency=str(latest.get("currency") or ""),
                            refund_amount=ref_amt,
                            refund_amount_source=ref_src,
                        )

                    updated = await self._finalize_order_status(
                        order_id,
                        expected_statuses=("cancelling",),
                        new_status="cancelled",
                        base_order=latest,
                        activation_tx_hook=_act_cancel_ref_tx,
                        scrub_if_zero_retention=True,
                        refund_state="confirmed",
                        settlement_state="cancelled",
                        error_code="USER_CANCELLED",
                        error_message=reason or "已取消并确认退款",
                        **sync_cancel_idem_kw,
                    )
                    assert updated is not None
                    return updated

                if outcome.settled:
                    # 上游告知已完成或已过期（不可退），绝不计作已确认退款，且遵守 retention_hours==0 即刻脱敏
                    async def _act_cancel_unref_tx(raw, now):
                        await self._db.activations.ensure_activation_state_in_tx(
                            raw,
                            order_id,
                            activation_id,
                            provider=str(latest["provider"]),
                            state="unrefundable",
                            now=now,
                            last_error=outcome.raw,
                            country_id=str(latest.get("country_id") or ""),
                            service_code=str(latest.get("service_code") or ""),
                            currency=str(latest.get("currency") or ""),
                            protect_settled=True,
                        )

                    updated = await self._finalize_order_status(
                        order_id,
                        expected_statuses=("cancelling",),
                        new_status="cancelled",
                        base_order=latest,
                        activation_tx_hook=_act_cancel_unref_tx,
                        scrub_if_zero_retention=True,
                        refund_state="unrefundable",
                        settlement_state="cancelled",
                        error_code="CANCEL_UNREFUNDABLE",
                        error_message=f"上游返回 {outcome.status}，无法确认退款 ({outcome.raw})",
                        **sync_cancel_idem_kw,
                    )
                    assert updated is not None
                    return updated

                async def _act_cancel_pend_tx(raw, now):
                    await self._db.activations.ensure_activation_state_in_tx(
                        raw,
                        order_id,
                        activation_id,
                        provider=str(latest["provider"]),
                        state="refund_pending",
                        now=now,
                        last_error=outcome.raw,
                        country_id=str(latest.get("country_id") or ""),
                        service_code=str(latest.get("service_code") or ""),
                        currency=str(latest.get("currency") or ""),
                        protect_settled=True,
                    )

                updated = await self._db.orders.transition_status(
                    order_id,
                    expected_statuses=("cancelling",),
                    new_status="compensation_pending",
                    refund_state="pending",
                    error_code="CANCEL_RETRY_PENDING",
                    error_message=f"取消退款暂未确认，已转入后台重试: {outcome.raw or outcome.status}",
                    tx_hook=_act_cancel_pend_tx,
                    **sync_cancel_idem_kw,
                )
                assert updated is not None
                self._emit("order.status_changed", updated)
                return updated

    # ── 重启恢复与后台激活对账补偿（修复 P1-03 / P1-07 / 复核 P1-01 / P1-04）──

    async def recover_on_startup(self) -> int:
        recoverable = await self._db.orders.list_recoverable()
        attempted_activations: set[tuple[str, str]] = set()
        count = 0
        for order in recoverable:
            order_id = str(order["order_id"])
            status = str(order["status"])

            if status == "acquiring":
                # 复核 P1-01 & 审查 Finding 5.1/5.2: 以 order_activations 台账为首要事实源；
                # 绝不让先前黑名单已退款的尝试掩盖后续未知购买尝试，同步恢复订单也固定写入 >=400 幂等快照！
                acts = await self._db.activations.list_for_order(order_id)
                live_active = [
                    a for a in acts
                    if str(a.get("activation_id") or "").strip()
                    and str(a.get("state") or "") in {"active", "refund_pending"}
                ]
                confirmed_acts = [
                    a for a in acts
                    if str(a.get("activation_id") or "").strip()
                    and str(a.get("state") or "") == "refund_confirmed"
                ]
                uncertain_acts = [
                    a for a in acts
                    if str(a.get("state") or "") in {"purchasing", "uncertain_purchase"}
                    and not str(a.get("activation_id") or "").strip()
                ]
                sync_rec_idem_kw: dict[str, Any] = {}
                if not order.get("async_acquire") and int(order.get("idempotency_http_status") or 0) == 0:
                    sync_rec_idem_kw = {
                        "idempotency_http_status": 502 if uncertain_acts else 500,
                        "idempotency_retryable": not bool(uncertain_acts),
                        "idempotency_error_code": "PURCHASE_UNCERTAIN" if uncertain_acts else "RESTART_RECOVERY",
                        "idempotency_error_message": (
                            "上游取号途中服务重启，购买结果未知，请核对上游账单"
                            if uncertain_acts
                            else "取号途中服务重启，请查询订单获取最新状态"
                        ),
                    }

                known_aid = str(order.get("activation_id") or "").strip()
                if not known_aid and live_active:
                    known_aid = str(live_active[-1]["activation_id"])

                if known_aid:
                    if not order.get("activation_id"):
                        await self._db.orders.update(
                            order_id,
                            activation_id=known_aid,
                            phone_raw=str(live_active[-1].get("phone_raw") or "") if live_active else "",
                        )
                    for act in live_active:
                        await self._db.activations.update_state(
                            int(act["id"]),
                            state="refund_pending",
                            last_error="取号后服务重启，已转入退款补偿",
                        )
                    for act in uncertain_acts:
                        await self._db.activations.update_state(
                            int(act["id"]),
                            state="uncertain_purchase",
                            last_error="上游取号途中服务重启，购买结果未知，待人工核对",
                        )
                    await self._db.orders.transition_status(
                        order_id,
                        expected_statuses=("acquiring",),
                        new_status="compensation_pending",
                        refund_state="pending",
                        settlement_state="uncertain" if uncertain_acts else "cancelling",
                        error_code="RESTART_COMPENSATION",
                        error_message="取号落库后服务重启，已转入补偿对账",
                        **sync_rec_idem_kw,
                    )
                    await self._reconcile_one_order(
                        await self.get_order(order_id),
                        attempted_activations=attempted_activations,
                    )
                    count += 1
                    continue

                if uncertain_acts:
                    async def _rec_unc_tx(raw, now, _uacts=uncertain_acts):
                        for act in _uacts:
                            await self._db.activations.update_state_in_tx(
                                raw,
                                int(act["id"]),
                                state="uncertain_purchase",
                                now=now,
                                last_error="上游取号途中服务重启，购买结果未知，待人工核对",
                            )

                    await self._finalize_order_status(
                        order_id,
                        expected_statuses=("acquiring",),
                        new_status="failed",
                        base_order=order,
                        activation_tx_hook=_rec_unc_tx,
                        refund_state="unknown",
                        settlement_state="uncertain",
                        error_code="PURCHASE_UNCERTAIN",
                        error_message="上游取号途中服务重启，购买结果未知，请核对上游账单",
                        **sync_rec_idem_kw,
                    )
                    count += 1
                    continue

                if confirmed_acts:
                    await self._finalize_order_status(
                        order_id,
                        expected_statuses=("acquiring",),
                        new_status="cancelled",
                        base_order=order,
                        refund_state="confirmed",
                        settlement_state="cancelled",
                        error_code="RESTART_RECOVERY",
                        error_message="先前尝试已确认退款，重启中断未完成取号",
                        **sync_rec_idem_kw,
                    )
                    count += 1
                    continue

                await self._finalize_order_status(
                    order_id,
                    expected_statuses=("acquiring",),
                    new_status="cancelled",
                    base_order=order,
                    refund_state="none",
                    settlement_state="cancelled",
                    error_code="RESTART_RECOVERY",
                    error_message="服务重启，中断未完成取号",
                    **sync_rec_idem_kw,
                )
                count += 1
                continue

            if status == "cancelling":
                acts = await self._db.activations.list_for_order(order_id)
                known_aid = str(order.get("activation_id") or "").strip()
                if not known_aid:
                    for act in reversed(acts):
                        if str(act.get("activation_id") or "").strip() and act.get("state") in {"active", "refund_pending", "refund_confirmed"}:
                            known_aid = str(act["activation_id"])
                            await self._db.orders.update(order_id, activation_id=known_aid)
                            break
                uncertain_acts = [
                    a for a in acts
                    if str(a.get("state") or "") in {"purchasing", "uncertain_purchase"}
                    and not str(a.get("activation_id") or "").strip()
                ]
                if not known_aid:
                    if uncertain_acts:
                        async def _rec_can_unc_tx(raw, now, _uacts=uncertain_acts):
                            for act in _uacts:
                                await self._db.activations.update_state_in_tx(
                                    raw,
                                    int(act["id"]),
                                    state="uncertain_purchase",
                                    now=now,
                                    last_error="取消途中服务重启，存在未知购买结果，待人工核对",
                                )

                        await self._finalize_order_status(
                            order_id,
                            expected_statuses=("cancelling",),
                            new_status="cancelled",
                            base_order=order,
                            activation_tx_hook=_rec_can_unc_tx,
                            refund_state="unknown",
                            settlement_state="uncertain",
                            error_code="PURCHASE_UNCERTAIN",
                            error_message="取消途中服务重启，存在未知购买结果，请核对上游账单",
                        )
                    else:
                        await self._finalize_order_status(
                            order_id,
                            expected_statuses=("cancelling",),
                            new_status="cancelled",
                            base_order=order,
                            refund_state="none",
                            settlement_state="cancelled",
                            error_code="RESTART_RECOVERY",
                            error_message="取消途中服务重启（无激活 ID）",
                        )
                    count += 1
                    continue
                await self._db.orders.transition_status(
                    order_id,
                    expected_statuses=("cancelling",),
                    new_status="compensation_pending",
                    refund_state="pending",
                )
                await self._reconcile_one_order(
                    await self.get_order(order_id),
                    attempted_activations=attempted_activations,
                )
                count += 1
                continue

            if status == "completing":
                fallback_status = "code_received" if int(order.get("code_count") or 0) > 0 else "waiting_code"
                recovered = await self._db.orders.transition_status(
                    order_id,
                    expected_statuses=("completing",),
                    new_status=fallback_status,
                    settlement_state="complete_uncertain",
                    error_code="COMPLETE_RECOVERY",
                    error_message="完成确认途中服务重启，请核对上游完成状态或重试完成",
                )
                if recovered and fallback_status == "waiting_code":
                    expires_dt = _parse_iso(str(recovered.get("expires_at") or ""))
                    if expires_dt and _utc_now() >= expires_dt:
                        await self._handle_poll_timeout(
                            recovered,
                            expected_expires_at=str(recovered.get("expires_at") or ""),
                        )
                    elif self._registry.is_active(recovered["provider"]):
                        logger.info("[%s] completing 恢复为 waiting_code 后重启等码轮询", order_id)
                        self._start_code_poller(order_id)
                    else:
                        await self._handle_provider_inactive_in_recovery(recovered)
                count += 1
                continue

            if status in {"waiting_code", "waiting_next_code"}:
                expires_dt = _parse_iso(str(order.get("expires_at") or ""))
                if expires_dt and _utc_now() >= expires_dt:
                    await self._handle_poll_timeout(
                        order,
                        expected_expires_at=str(order.get("expires_at") or ""),
                    )
                elif self._registry.is_active(order["provider"]):
                    logger.info("[%s] 重启恢复等码轮询 (status=%s)", order_id, status)
                    prev_code = (
                        str(order.get("resend_base_code") or order.get("latest_code") or "")
                        if (status == "waiting_next_code" and not order.get("code_scrubbed"))
                        else ""
                    )
                    seen_wait = bool(order.get("resend_seen_wait")) if status == "waiting_next_code" else True
                    self._start_code_poller(order_id, previous_code=prev_code, initial_seen_wait=seen_wait)
                else:
                    await self._handle_provider_inactive_in_recovery(order)
                count += 1
            elif status == "compensation_pending":
                await self._reconcile_one_order(order, attempted_activations=attempted_activations)
                count += 1

        # 共享同一 attempted_activations 集合，确保本轮未结清的激活不会被第二遍重复取消
        count += await self.reconcile_pending_activations(attempted_activations=attempted_activations)
        return count

    async def _reconcile_one_order(
        self,
        order: dict[str, Any],
        *,
        attempted_activations: Optional[set[tuple[str, str]]] = None,
    ) -> bool:
        """按 (provider, activation_id) 单次认领并同步结清激活台账与订单终态（修复 v3 P1-01 & v6 P1-02）。"""
        order_id = str(order["order_id"])
        async with self._lock_for(order_id):
            fresh = await self._db.orders.get(order_id, include_messages=False)
            if not fresh or fresh.get("status") != "compensation_pending":
                return False

            activation_id = str(fresh.get("activation_id") or "").strip()
            if not activation_id:
                acts = await self._db.activations.list_for_order(order_id)
                for act in reversed(acts):
                    if str(act.get("activation_id") or "").strip() and act.get("state") in {
                        "refund_confirmed", "active", "refund_pending", "unrefundable"
                    }:
                        activation_id = str(act["activation_id"])
                        await self._db.orders.update(order_id, activation_id=activation_id)
                        break

            if not activation_id:
                return False

            provider_key = str(fresh["provider"]).strip().lower()
            act_key = (provider_key, activation_id)
            async with self._activation_lock_for(provider_key, activation_id):
                # 1. 先检查持久化的激活台账结算证据：已确认退款/不可退/已完成/已人工结清时直接对齐订单，绝不重复调上游！
                act_row = await self._db.activations.get_by_activation_id(order_id, activation_id)
                if act_row and act_row.get("state") in {"completed", "manual_resolved"}:
                    if attempted_activations is not None:
                        attempted_activations.add(act_key)
                    target_status = (
                        "completed"
                        if act_row.get("state") == "completed" or int(fresh.get("code_count") or 0) > 0
                        else "cancelled"
                    )
                    updated = await self._finalize_order_status(
                        order_id,
                        expected_statuses=("compensation_pending",),
                        new_status=target_status,
                        base_order=fresh,
                        scrub_if_zero_retention=True,
                        refund_state="none" if act_row.get("state") == "completed" else str(fresh.get("refund_state") or "none"),
                        settlement_state="manual_resolved",
                        error_code="",
                        error_message=str(act_row.get("last_error") or "激活已人工结清"),
                    )
                    return updated is not None

                if act_row and act_row.get("state") == "refund_confirmed":
                    if attempted_activations is not None:
                        attempted_activations.add(act_key)
                    updated = await self._finalize_order_status(
                        order_id,
                        expected_statuses=("compensation_pending",),
                        new_status="cancelled",
                        base_order=fresh,
                        scrub_if_zero_retention=True,
                        refund_state="confirmed",
                        settlement_state="cancelled",
                        error_code="COMPENSATED_CANCEL",
                        error_message="按已确认退款台账结清订单",
                    )
                    return updated is not None

                if act_row and act_row.get("state") == "unrefundable":
                    if attempted_activations is not None:
                        attempted_activations.add(act_key)
                    updated = await self._finalize_order_status(
                        order_id,
                        expected_statuses=("compensation_pending",),
                        new_status="cancelled",
                        base_order=fresh,
                        scrub_if_zero_retention=True,
                        refund_state="unrefundable",
                        settlement_state="cancelled",
                        error_code="CANCEL_UNREFUNDABLE",
                        error_message=str(act_row.get("last_error") or "按已结算激活台账确认不可退款"),
                    )
                    return updated is not None

                # v6 复核 P1-02: 先确认平台已启用，再按 (provider, activation_id) 登记已尝试集合，绝不让停用平台挡住其他平台！
                if not self._registry.is_active(provider_key):
                    return False

                if attempted_activations is not None:
                    if act_key in attempted_activations:
                        return False
                    attempted_activations.add(act_key)

                provider = self._registry.get(provider_key)
                outcome = await self._provider_cancel(provider, activation_id)
                if outcome.refund_confirmed:
                    ref_amt, ref_src = self._resolve_refund_evidence(provider, outcome, act_row)

                    async def _act_rec_ref_tx(raw, now):
                        await self._db.activations.ensure_activation_state_in_tx(
                            raw,
                            order_id,
                            activation_id,
                            provider=str(fresh["provider"]),
                            state="refund_confirmed",
                            now=now,
                            last_error="后台补偿退款已确认",
                            country_id=str(fresh.get("country_id") or ""),
                            service_code=str(fresh.get("service_code") or ""),
                            currency=str(fresh.get("currency") or ""),
                            refund_amount=ref_amt,
                            refund_amount_source=ref_src,
                            protect_settled=True,
                        )

                    updated = await self._finalize_order_status(
                        order_id,
                        expected_statuses=("compensation_pending",),
                        new_status="cancelled",
                        base_order=fresh,
                        activation_tx_hook=_act_rec_ref_tx,
                        scrub_if_zero_retention=True,
                        refund_state="confirmed",
                        settlement_state="cancelled",
                        error_code="COMPENSATED_CANCEL",
                        error_message="后台补偿退款已确认",
                    )
                    return updated is not None
                if outcome.settled:
                    async def _act_rec_unref_tx(raw, now):
                        await self._db.activations.ensure_activation_state_in_tx(
                            raw,
                            order_id,
                            activation_id,
                            provider=str(fresh["provider"]),
                            state="unrefundable",
                            now=now,
                            last_error=outcome.raw,
                            country_id=str(fresh.get("country_id") or ""),
                            service_code=str(fresh.get("service_code") or ""),
                            currency=str(fresh.get("currency") or ""),
                            protect_settled=True,
                        )

                    updated = await self._finalize_order_status(
                        order_id,
                        expected_statuses=("compensation_pending",),
                        new_status="cancelled",
                        base_order=fresh,
                        activation_tx_hook=_act_rec_unref_tx,
                        scrub_if_zero_retention=True,
                        refund_state="unrefundable",
                        settlement_state="cancelled",
                        error_code="CANCEL_UNREFUNDABLE",
                        error_message=f"后台补偿确认不可退款 ({outcome.status}): {outcome.raw}",
                    )
                    return updated is not None
                # 暂未结清（可重试）：仅标记 refund_pending，留待下一轮补偿，绝不在本轮二次扫描重调上游
                await self._db.activations.ensure_activation_state(
                    order_id,
                    activation_id,
                    provider=str(fresh["provider"]),
                    state="refund_pending",
                    last_error=outcome.raw or outcome.status,
                    country_id=str(fresh.get("country_id") or ""),
                    service_code=str(fresh.get("service_code") or ""),
                    currency=str(fresh.get("currency") or ""),
                    protect_settled=True,
                )
                return False

    async def reconcile_pending_activations(
        self,
        *,
        attempted_activations: Optional[set[tuple[str, str]]] = None,
    ) -> int:
        """补偿独立激活表中待退款及孤儿 active 激活；按 (provider, activation_id) 去重与加锁，严格跳过已人工结清激活（修复 v6 P1-02 & v7 P1-02）。"""
        rows = await self._db.activations.list_pending_refunds(
            active_acquiring_order_ids=self._active_acquiring_orders,
        )
        settled = 0
        for row in rows:
            activation_id = str(row.get("activation_id") or "").strip()
            order_id = str(row.get("order_id") or "")
            provider_key = str(row["provider"]).strip().lower()
            if not activation_id or not self._registry.is_active(provider_key):
                continue
            act_key = (provider_key, activation_id)
            if attempted_activations is not None:
                if act_key in attempted_activations:
                    continue
                attempted_activations.add(act_key)

            try:
                async with self._lock_for(order_id):
                    async with self._activation_lock_for(provider_key, activation_id):
                        # 再次确认期间没有新启动的取号任务占有该订单
                        if order_id in self._active_acquiring_orders:
                            continue
                        fresh_act = await self._db.activations.get(int(row["id"]))
                        if not fresh_act or fresh_act.get("state") in {
                            "refund_confirmed",
                            "unrefundable",
                            "completed",
                            "manual_resolved",
                            "code_persist_failed",
                        }:
                            continue
                        parent = await self._db.orders.get(order_id, include_messages=False)
                        if (
                            fresh_act.get("state") == "active"
                            and parent is not None
                            and str(parent.get("settlement_state") or "") in {"code_persist_failed", "manual_resolved"}
                        ):
                            continue

                        provider = self._registry.get(provider_key)
                        outcome = await self._provider_cancel(provider, activation_id)
                        if outcome.refund_confirmed:
                            ref_amt, ref_src = self._resolve_refund_evidence(provider, outcome, fresh_act)

                            async def _act_upd_ref_tx(raw, now, _rid=int(row["id"]), _ra=ref_amt, _rs=ref_src):
                                await self._db.activations.update_state_in_tx(
                                    raw,
                                    _rid,
                                    state="refund_confirmed",
                                    now=now,
                                    last_error="后台激活补偿退款已确认",
                                    refund_amount=_ra,
                                    refund_amount_source=_rs,
                                    protect_settled=True,
                                )

                            if parent and parent.get("status") in {"compensation_pending", "acquiring"} and (
                                not parent.get("activation_id") or parent.get("activation_id") == activation_id
                            ):
                                idem_extra: dict[str, Any] = {}
                                if (
                                    parent.get("status") == "acquiring"
                                    and not parent.get("async_acquire")
                                    and int(parent.get("idempotency_http_status") or 0) == 0
                                ):
                                    idem_extra = {
                                        "idempotency_http_status": 500,
                                        "idempotency_retryable": True,
                                        "idempotency_error_code": "ACQUIRE_POST_ERROR",
                                        "idempotency_error_message": "取号异常，已由后台补偿退款",
                                    }
                                await self._finalize_order_status(
                                    order_id,
                                    expected_statuses=("compensation_pending", "acquiring"),
                                    new_status="cancelled",
                                    base_order=parent,
                                    activation_tx_hook=_act_upd_ref_tx,
                                    refund_state="confirmed",
                                    settlement_state="cancelled",
                                    error_code="COMPENSATED_CANCEL",
                                    error_message="后台激活补偿退款已确认",
                                    **idem_extra,
                                )
                            elif (
                                parent
                                and parent.get("status") in {"cancelled", "failed", "expired"}
                                and parent.get("activation_id") == activation_id
                                and str(parent.get("refund_state") or "none") in {"none", "pending", "unknown"}
                            ):
                                async with self._db.conn.transaction() as raw:
                                    now = _utc_now().isoformat()
                                    await _act_upd_ref_tx(raw, now)
                                    await raw.execute(
                                        "UPDATE sms_orders SET refund_state='confirmed', version=version+1, updated_at=? WHERE order_id=?",
                                        (now, order_id),
                                    )
                                refreshed_parent = await self._db.orders.get(order_id, include_messages=True, include_activations=True)
                                if refreshed_parent:
                                    self._emit("order.status_changed", refreshed_parent)
                            else:
                                await self._db.activations.update_state(
                                    int(row["id"]),
                                    state="refund_confirmed",
                                    last_error="后台激活补偿退款已确认",
                                    refund_amount=ref_amt,
                                    refund_amount_source=ref_src,
                                )
                            settled += 1
                        elif outcome.settled:
                            async def _act_upd_unref_tx(raw, now, _rid=int(row["id"]), _raw=outcome.raw):
                                await self._db.activations.update_state_in_tx(
                                    raw,
                                    _rid,
                                    state="unrefundable",
                                    now=now,
                                    last_error=_raw,
                                    protect_settled=True,
                                )

                            if parent and parent.get("status") in {"compensation_pending", "acquiring"} and (
                                not parent.get("activation_id") or parent.get("activation_id") == activation_id
                            ):
                                idem_extra = {}
                                if (
                                    parent.get("status") == "acquiring"
                                    and not parent.get("async_acquire")
                                    and int(parent.get("idempotency_http_status") or 0) == 0
                                ):
                                    idem_extra = {
                                        "idempotency_http_status": 500,
                                        "idempotency_retryable": True,
                                        "idempotency_error_code": "ACQUIRE_POST_ERROR",
                                        "idempotency_error_message": "取号异常，后台补偿确认不可退款",
                                    }
                                await self._finalize_order_status(
                                    order_id,
                                    expected_statuses=("compensation_pending", "acquiring"),
                                    new_status="cancelled",
                                    base_order=parent,
                                    activation_tx_hook=_act_upd_unref_tx,
                                    refund_state="unrefundable",
                                    settlement_state="cancelled",
                                    error_code="CANCEL_UNREFUNDABLE",
                                    error_message=f"后台补偿确认不可退款 ({outcome.status}): {outcome.raw}",
                                    **idem_extra,
                                )
                            elif (
                                parent
                                and parent.get("status") in {"cancelled", "failed", "expired"}
                                and parent.get("activation_id") == activation_id
                                and str(parent.get("refund_state") or "none") in {"none", "pending", "unknown"}
                            ):
                                async with self._db.conn.transaction() as raw:
                                    now = _utc_now().isoformat()
                                    await _act_upd_unref_tx(raw, now)
                                    await raw.execute(
                                        "UPDATE sms_orders SET refund_state='unrefundable', version=version+1, updated_at=? WHERE order_id=?",
                                        (now, order_id),
                                    )
                                refreshed_parent = await self._db.orders.get(order_id, include_messages=True, include_activations=True)
                                if refreshed_parent:
                                    self._emit("order.status_changed", refreshed_parent)
                            else:
                                await self._db.activations.update_state(
                                    int(row["id"]), state="unrefundable", last_error=outcome.raw
                                )
                            settled += 1
                        elif fresh_act.get("state") == "active":
                            async def _act_upd_pend_tx(raw, now, _rid=int(row["id"]), _msg=f"孤儿激活待重试退款: {outcome.raw or outcome.status}"):
                                await self._db.activations.update_state_in_tx(
                                    raw,
                                    _rid,
                                    state="refund_pending",
                                    now=now,
                                    last_error=_msg,
                                    protect_settled=True,
                                )

                            if parent and parent.get("status") == "acquiring":
                                idem_extra = {}
                                if not parent.get("async_acquire") and int(parent.get("idempotency_http_status") or 0) == 0:
                                    idem_extra = {
                                        "idempotency_http_status": 500,
                                        "idempotency_retryable": True,
                                        "idempotency_error_code": "ACQUIRE_POST_ERROR",
                                        "idempotency_error_message": "滞留激活已转入后台退款重试",
                                    }
                                comp_ord = await self._db.orders.transition_status(
                                    order_id,
                                    expected_statuses=("acquiring",),
                                    new_status="compensation_pending",
                                    activation_id=activation_id,
                                    refund_state="pending",
                                    error_code="ACQUIRE_COMPENSATION_PENDING",
                                    error_message="滞留激活已转入后台退款重试",
                                    tx_hook=_act_upd_pend_tx,
                                    **idem_extra,
                                )
                                if comp_ord:
                                    self._emit("order.status_changed", comp_ord)
                            else:
                                await self._db.activations.update_state(
                                    int(row["id"]),
                                    state="refund_pending",
                                    last_error=f"孤儿激活待重试退款: {outcome.raw or outcome.status}",
                                )
            except Exception:
                logger.warning("[%s] 激活补偿处理异常（不影响后续激活继续补偿）", order_id, exc_info=True)
        return settled

    async def _recover_stranded_cancelling(
        self,
        order: dict[str, Any],
        *,
        attempted_activations: Optional[set[tuple[str, str]]] = None,
    ) -> bool:
        order_id = str(order["order_id"])
        async with self._lock_for(order_id):
            fresh = await self._db.orders.get(order_id, include_messages=False)
            if not fresh or fresh.get("status") != "cancelling":
                return False
            acts = await self._db.activations.list_for_order(order_id)
            known_aid = str(fresh.get("activation_id") or "").strip()
            if not known_aid:
                for act in reversed(acts):
                    if str(act.get("activation_id") or "").strip() and act.get("state") in {
                        "active", "refund_pending", "refund_confirmed"
                    }:
                        known_aid = str(act["activation_id"])
                        await self._db.orders.update(order_id, activation_id=known_aid)
                        break
            uncertain_acts = [
                a for a in acts
                if str(a.get("state") or "") in {"purchasing", "uncertain_purchase"}
                and not str(a.get("activation_id") or "").strip()
            ]
            if not known_aid:
                if uncertain_acts:
                    async def _m_can_unc_tx(raw, now, _uacts=uncertain_acts):
                        for act in _uacts:
                            await self._db.activations.update_state_in_tx(
                                raw,
                                int(act["id"]),
                                state="uncertain_purchase",
                                now=now,
                                last_error="滞留取消中订单存在未知购买结果，待人工核对",
                            )

                    updated = await self._finalize_order_status(
                        order_id,
                        expected_statuses=("cancelling",),
                        new_status="cancelled",
                        base_order=fresh,
                        activation_tx_hook=_m_can_unc_tx,
                        refund_state="unknown",
                        settlement_state="uncertain",
                        error_code="PURCHASE_UNCERTAIN",
                        error_message="滞留取消中订单存在未知购买结果，请核对上游账单",
                    )
                    return updated is not None
                updated = await self._finalize_order_status(
                    order_id,
                    expected_statuses=("cancelling",),
                    new_status="cancelled",
                    base_order=fresh,
                    refund_state="none",
                    settlement_state="cancelled",
                    error_code="MAINTENANCE_RECOVERY",
                    error_message="滞留取消中订单已由周期维护收尾（无激活 ID）",
                )
                return updated is not None
            await self._db.orders.transition_status(
                order_id,
                expected_statuses=("cancelling",),
                new_status="compensation_pending",
                refund_state="pending",
            )
        return await self._reconcile_one_order(
            await self.get_order(order_id),
            attempted_activations=attempted_activations,
        )

    async def _recover_stranded_completing(self, order: dict[str, Any]) -> bool:
        """恢复滞留完成中订单：持锁区仅执行状态回退，释放锁后再按真正过期/平台停用分流处理（修复 v6 P1-01 & v7 P1-04）。"""
        order_id = str(order["order_id"])
        need_start_poller = False
        need_timeout_order: Optional[dict[str, Any]] = None
        need_inactive_order: Optional[dict[str, Any]] = None

        async with self._lock_for(order_id):
            fresh = await self._db.orders.get(order_id, include_messages=False)
            if not fresh or fresh.get("status") != "completing":
                return False
            fallback_status = "code_received" if int(fresh.get("code_count") or 0) > 0 else "waiting_code"
            recovered = await self._db.orders.transition_status(
                order_id,
                expected_statuses=("completing",),
                new_status=fallback_status,
                settlement_state="complete_uncertain",
                error_code="COMPLETE_RECOVERY",
                error_message="滞留完成中订单已由周期维护恢复，请核对上游完成状态或重试完成",
            )
            if recovered is None:
                return False
            self._emit("order.status_changed", recovered)
            if fallback_status == "waiting_code":
                expires_dt = _parse_iso(str(recovered.get("expires_at") or ""))
                if expires_dt and _utc_now() >= expires_dt:
                    need_timeout_order = recovered
                elif self._registry.is_active(recovered["provider"]):
                    need_start_poller = True
                else:
                    need_inactive_order = recovered

        # 释放单订单锁后再重启轮询、触发超时退款或处理平台停用
        if need_start_poller:
            self._start_code_poller(order_id)
        elif need_timeout_order is not None:
            await self._handle_poll_timeout(
                need_timeout_order,
                expected_expires_at=str(need_timeout_order.get("expires_at") or ""),
            )
        elif need_inactive_order is not None:
            await self._handle_provider_inactive_in_recovery(need_inactive_order)
        return True

    async def reconcile_pending(self) -> int:
        """周期维护入口：结清待补偿订单/激活，并接管无活跃任务的在途等码/取消中/完成中订单；单条异常不阻塞其余订单与脱敏。"""
        pending = await self._db.orders.list_recoverable()
        attempted_activations: set[tuple[str, str]] = set()
        reconciled = 0
        for order in pending:
            order_id = str(order["order_id"])
            status = str(order.get("status") or "")
            try:
                if status == "compensation_pending":
                    if await self._reconcile_one_order(order, attempted_activations=attempted_activations):
                        reconciled += 1
                elif status in {"waiting_code", "waiting_next_code"}:
                    if self._has_live_task(order_id):
                        continue
                    expires_dt = _parse_iso(str(order.get("expires_at") or ""))
                    if expires_dt and _utc_now() >= expires_dt:
                        await self._handle_poll_timeout(
                            order,
                            expected_expires_at=str(order.get("expires_at") or ""),
                        )
                        reconciled += 1
                    elif self._registry.is_active(str(order.get("provider") or "")):
                        logger.info("[%s] 周期维护接管无活跃轮询任务的在途订单 (status=%s)", order_id, status)
                        prev_code = (
                            str(order.get("resend_base_code") or order.get("latest_code") or "")
                            if (status == "waiting_next_code" and not order.get("code_scrubbed"))
                            else ""
                        )
                        seen_wait = bool(order.get("resend_seen_wait")) if status == "waiting_next_code" else True
                        self._start_code_poller(order_id, previous_code=prev_code, initial_seen_wait=seen_wait)
                        reconciled += 1
                    else:
                        await self._handle_provider_inactive_in_recovery(order)
                        reconciled += 1
                elif status == "cancelling":
                    if self._has_live_task(order_id) or self._is_order_locked(order_id):
                        continue
                    if await self._recover_stranded_cancelling(order, attempted_activations=attempted_activations):
                        reconciled += 1
                elif status == "completing":
                    if self._has_live_task(order_id) or self._is_order_locked(order_id):
                        continue
                    if await self._recover_stranded_completing(order):
                        reconciled += 1
            except Exception:
                logger.warning("[%s] 周期维护处理订单异常 (status=%s)，继续处理其余订单", order_id, status, exc_info=True)
        reconciled += await self.reconcile_pending_activations(attempted_activations=attempted_activations)
        return reconciled

    async def list_reconciliation_items(self) -> dict[str, Any]:
        """汇总所有需要人工关注的对账项：不确定购买、收码写库持续失败、待退款激活、待核对历史币种及待核对订单。"""
        uncertain_activations = await self._db.activations.list_uncertain(
            active_acquiring_order_ids=self._active_acquiring_orders,
        )
        pending_refund_activations = await self._db.activations.list_pending_refunds(
            active_acquiring_order_ids=self._active_acquiring_orders,
        )
        currency_review_activations = await self._db.activations.list_currency_review()
        uncertain_orders = await self._db.conn.fetchall(
            "SELECT order_id, project_id, provider, service, activation_id, status, "
            "settlement_state, refund_state, error_code, error_message, updated_at "
            "FROM sms_orders WHERE settlement_state IN ('uncertain', 'complete_uncertain', 'complete_failed', 'code_persist_failed') "
            "   OR status = 'compensation_pending' "
            "ORDER BY updated_at DESC LIMIT 100"
        )
        return {
            "uncertain_activations": uncertain_activations,
            "pending_refund_activations": pending_refund_activations,
            "currency_review_activations": currency_review_activations,
            "orders_requiring_review": uncertain_orders,
        }

    async def resolve_activation_manually(
        self,
        activation_row_id: int,
        *,
        state: str,
        note: str = "",
        refund_amount: Optional[float] = None,
        currency: Optional[str] = None,
    ) -> dict[str, Any]:
        """持单订单锁+单激活锁，在同一事务内受条件保护地人工核销激活与父订单终态，与自动补偿完全互斥（修复 v6 P1-03 & v7 P1-02）。"""
        existing = await self._db.activations.get(activation_row_id)
        if existing is None:
            raise NotFoundError(f"激活记录 #{activation_row_id} 不存在", code="ACTIVATION_NOT_FOUND")

        order_id = str(existing["order_id"])
        provider_key = str(existing.get("provider") or "").strip().lower()
        activation_id = str(existing.get("activation_id") or "").strip()

        async def _do_resolve() -> tuple[Optional[dict[str, Any]], bool, dict[str, Any], dict[str, int]]:
            parent_snapshot = await self._db.orders.get(order_id, include_messages=False)
            target_url = await self._resolve_webhook_target(parent_snapshot) if parent_snapshot else ""
            zero_retention = (await self._retention_hours()) == 0
            meta: dict[str, Any] = {}
            scrub_vers: dict[str, int] = {}

            def _build_parent_outbox(snap: dict[str, Any], new_status: str) -> Optional[dict[str, Any]]:
                if not target_url:
                    return None
                ev_map = {
                    "completed": "order.completed",
                    "cancelled": "order.cancelled",
                    "expired": "order.expired",
                    "failed": "order.failed",
                }
                ev_type = ev_map.get(new_status)
                if not ev_type:
                    return None
                ev_id = f"evt_{uuid4().hex[:12]}"
                dlv_id = f"dlv_{uuid4().hex[:12]}"
                return {
                    "delivery_id": dlv_id,
                    "event_id": ev_id,
                    "event_type": ev_type,
                    "order_id": order_id,
                    "project_id": str(snap.get("project_id") or ""),
                    "target_url": target_url,
                    "payload": build_minimal_webhook_payload(ev_type, ev_id, snap),
                }

            upd, ok = await self._db.activations.resolve_manually_atomic(
                activation_row_id,
                state=state,
                note=note,
                refund_amount=refund_amount,
                currency=currency,
                outbox_entry_builder=_build_parent_outbox,
                result_meta=meta,
                scrub_if_terminal=zero_retention,
                scrub_result=scrub_vers,
            )
            return upd, ok, meta, scrub_vers

        async with self._lock_for(order_id):
            if activation_id:
                async with self._activation_lock_for(provider_key, activation_id):
                    updated, applied, meta, scrub_vers = await _do_resolve()
            else:
                updated, applied, meta, scrub_vers = await _do_resolve()
            if applied and meta.get("parent_status_changed") and str(meta.get("new_status") or "") in TERMINAL_STATUSES:
                bg = self._cancel_bg_task(order_id)
                if bg is not None:
                    await asyncio.gather(bg, return_exceptions=True)

        if updated is None:
            raise NotFoundError(f"激活记录 #{activation_row_id} 不存在", code="ACTIVATION_NOT_FOUND")
        if not applied:
            raise ConflictError(
                "已确认退款的激活记录不可覆盖为其他结算状态",
                code="ACTIVATION_REFUND_IMMUTABLE",
                details={"activation_row_id": activation_row_id, "current_state": updated.get("state")},
            )
        if scrub_vers:
            self.on_orders_scrubbed(list(scrub_vers.keys()), scrub_versions=scrub_vers)
        if meta.get("outbox_enqueued"):
            self._notify_outbox()
        if meta.get("parent_status_changed"):
            fresh_parent = await self._db.orders.get(order_id, include_messages=True, include_activations=True)
            if fresh_parent is not None:
                ev_map = {
                    "completed": "order.completed",
                    "cancelled": "order.cancelled",
                    "expired": "order.expired",
                    "failed": "order.failed",
                }
                self._emit(ev_map.get(str(meta.get("new_status") or ""), "order.status_changed"), fresh_parent)
        return updated
