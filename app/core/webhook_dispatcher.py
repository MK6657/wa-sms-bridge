"""持久化 Webhook Outbox 分发器（修复 P1-01 / P1-09 / 复核 P0-02 / P1-02 / v3 复核 P0-01）。

设计特性：
- 订单状态与待投递记录在 SQLite 同事务落盘，验证码事件加密落盘并在投递/失败/脱敏后立即清除副本
- 发包顺序安全（修复 v3 复核 P0-01）：先完成异步 DNS 与策略校验，发包前最后时刻在订单锁内复核
  投递租约与订单脱敏状态再解密发送；状态写回严格校验 status='delivering' + lease_expires_at，
  绝不把并发脱敏的 scrubbed 覆盖回 delivered
- 严禁将接收方 HTTP 响应正文写入 last_error 或日志，杜绝接收端回显验证码导致二次明文泄露
- 连接级固定到已批准 IP（PinnedIPAsyncTransport），关闭 trust_env 与 3xx 重定向
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import httpx

from app.config import env
from app.core.url_guard import (
    PinnedIPAsyncTransport,
    _host_matches_allowlist,
    normalize_url_hostname,
    pin_webhook_host,
    validate_webhook_url_async,
)
from app.db import Database
from app.db.webhooks_repo import decode_outbox_payload
from app.errors import AppError

logger = logging.getLogger(__name__)


def compute_webhook_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _format_http_error(status_code: int) -> str:
    """只记录结构化 HTTP 状态码与标准短语，绝不保存或记录接收端原始正文（防止回显验证码泄露）。"""
    if 300 <= status_code < 400:
        return f"禁止 Webhook 重定向 (HTTP {status_code})"
    phrase = httpx.codes.get_reason_phrase(status_code)
    return f"接收端返回 HTTP {status_code}" + (f" ({phrase})" if phrase else "")


class WebhookDispatcher:
    """基于 SQLite Outbox 的持久可靠 Webhook 投递器。"""

    def __init__(
        self,
        db: Database,
        *,
        timeout_seconds: float = 8.0,
        retry_delays: tuple[float, ...] = (2.0, 8.0, 24.0, 60.0),
        max_concurrency: int = 4,
        poll_interval: float = 2.0,
        dns_timeout: float = 3.0,
    ):
        self._db = db
        self._timeout = max(1.0, float(timeout_seconds))
        self._dns_timeout = max(0.2, float(dns_timeout))
        self._retry_delays = retry_delays
        self._max_concurrency = max(1, int(max_concurrency))
        self._poll_interval = max(0.1, float(poll_interval))
        # P1-01 / 复核 P1-02: 禁用环境代理(trust_env=False)、禁止自动重定向、绑定已批准解析 IP
        self._transport = PinnedIPAsyncTransport()
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._wakeup = asyncio.Event()
        self._worker_task: Optional[asyncio.Task] = None
        self._inflight: set[asyncio.Task] = set()
        self._inflight_code_by_order: dict[str, set[asyncio.Task]] = {}

    async def _send_http_post(self, clean_url: str, *, body: bytes, headers: dict[str, str]) -> int:
        """发送 Webhook POST 并仅读取状态头，不缓冲任意大小响应体（兼容测试对 _client.post 的桩替换）。"""
        overall_timeout = max(3.0, self._timeout * 2.0 + 2.0)
        if not isinstance(self._client, httpx.AsyncClient) or "post" in getattr(self._client, "__dict__", {}):
            resp = await asyncio.wait_for(
                self._client.post(clean_url, content=body, headers=headers),
                timeout=overall_timeout,
            )
            return int(resp.status_code)

        async def _stream_status_only() -> int:
            async with self._client.stream("POST", clean_url, content=body, headers=headers) as resp:
                return int(resp.status_code)

        return await asyncio.wait_for(_stream_status_only(), timeout=overall_timeout)

    def notify(self) -> None:
        self._wakeup.set()

    def start(self) -> None:
        if self._worker_task and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(self._run_worker_loop())

    async def recover_and_start(self) -> int:
        recovered = await self._db.webhooks.recover_inflight()
        self.start()
        if recovered:
            self.notify()
        return recovered

    def cancel_code_deliveries_for_orders(self, order_ids: Iterable[str]) -> int:
        """订单脱敏清理时立即取消所有仍在准备发送的该订单验证码投递任务。"""
        cancelled = 0
        for oid in order_ids:
            tasks = list(self._inflight_code_by_order.get(str(oid), ()))
            for task in tasks:
                if not task.done():
                    task.cancel()
                    cancelled += 1
        return cancelled

    async def shutdown(self) -> None:
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
        self._worker_task = None
        for task in list(self._inflight):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        self._inflight.clear()
        self._inflight_code_by_order.clear()
        await self._client.aclose()

    async def _run_worker_loop(self) -> None:
        try:
            while True:
                self._wakeup.clear()
                try:
                    processed = await self.process_due_once()
                    if processed > 0:
                        continue
                except Exception:
                    logger.warning("Webhook outbox poll loop error", exc_info=True)

                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    async def process_due_once(self) -> int:
        """认领并派发一批到期 Outbox 记录（也可在测试中直接显式调用）。"""
        available_slots = max(0, self._max_concurrency - len(self._inflight))
        if available_slots <= 0:
            return 0
        lease_seconds = max(30, int(self._dns_timeout + self._timeout * 2.0 + 15))
        claimed = await self._db.webhooks.claim_due(limit=available_slots, lease_seconds=lease_seconds)
        for item in claimed:
            oid = str(item.get("order_id") or "")
            is_code = bool(item.get("contains_code")) or str(item.get("event_type")) == "order.code_received"
            task = asyncio.create_task(self._deliver_outbox_item(item))
            self._inflight.add(task)
            if is_code and oid:
                self._inflight_code_by_order.setdefault(oid, set()).add(task)

            def _cleanup(done_task: asyncio.Task, _oid: str = oid, _is_code: bool = is_code) -> None:
                self._inflight.discard(done_task)
                if _is_code and _oid:
                    bucket = self._inflight_code_by_order.get(_oid)
                    if bucket:
                        bucket.discard(done_task)
                        if not bucket:
                            self._inflight_code_by_order.pop(_oid, None)
                self._wakeup.set()

            task.add_done_callback(_cleanup)
        return len(claimed)

    async def drain_inflight(self) -> None:
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    async def _lookup_project_delivery_auth(self, project_id: str) -> tuple[bool, str, str]:
        """校验项目仍处于启用状态并返回其签名密钥；若密钥已停用或签名密钥解密失败则拒绝无签名外发。"""
        if not project_id or project_id == "web-console":
            return True, "", ""
        row = await self._db.api_keys.get(project_id, include_secret=True)
        if row is None or not row.get("enabled"):
            return False, "", "项目密钥已停用或删除，停止投递"
        if row.get("webhook_secret_decrypt_error"):
            return False, "", "项目 Webhook 签名密钥解密失败，停止无签名投递"
        return True, str(row.get("webhook_secret") or ""), ""

    async def _deliver_outbox_item(self, item: dict[str, Any]) -> None:
        delivery_id = str(item["delivery_id"])
        target_url = str(item["target_url"])
        event_type = str(item["event_type"])
        order_id = str(item.get("order_id") or "")
        project_id = str(item.get("project_id") or "")
        lease_token = str(item.get("lease_expires_at") or "")
        contains_code = bool(item.get("contains_code")) or event_type == "order.code_received"
        attempts = int(item.get("attempts") or 0) + 1
        max_attempts = int(item.get("max_attempts") or 5)

        # 快速预检：已脱敏则直接收尾，无需做 DNS 解析
        if bool(item.get("payload_scrubbed")):
            await self._db.webhooks.mark_scrubbed(delivery_id)
            return
        if contains_code and order_id:
            order = await self._db.orders.get(order_id, include_messages=False)
            if order is not None and bool(order.get("code_scrubbed")):
                await self._db.webhooks.mark_scrubbed(delivery_id)
                return

        allowed_proj, secret, auth_err = await self._lookup_project_delivery_auth(project_id)
        if not allowed_proj:
            await self._db.webhooks.mark_retry_or_failed(
                delivery_id,
                attempts=max_attempts,
                max_attempts=max_attempts,
                status_code=0,
                error=auth_err or "项目密钥已停用或删除，停止投递",
                retry_delay_seconds=0,
                scrub_on_terminal=contains_code,
                expected_lease=lease_token,
            )
            return

        # 阶段 1: 先完成异步 DNS 校验（此时尚未解密验证码！）
        try:
            clean_url, approved_ips = await validate_webhook_url_async(
                target_url,
                resolve_dns=True,
                dns_timeout=self._dns_timeout,
            )
        except AppError as exc:
            is_retryable_dns = bool(exc.retryable)
            await self._db.webhooks.mark_retry_or_failed(
                delivery_id,
                attempts=attempts if is_retryable_dns else max_attempts,
                max_attempts=max_attempts,
                status_code=0,
                error=f"出站策略拦截: {exc.code}",
                retry_delay_seconds=self._retry_delay_for(attempts) if is_retryable_dns else 0,
                scrub_on_terminal=contains_code,
                expected_lease=lease_token,
            )
            return

        parsed_host = normalize_url_hostname(urlparse(clean_url).hostname or "")
        explicit_allow = _host_matches_allowlist(parsed_host, env.webhook_allowed_hosts)
        allow_private = explicit_allow or env.webhook_allow_private_nets

        # 阶段 2: 真正发送 HTTP 前最后时刻复核租约、项目启用态与订单脱敏标记，再解密并立即发送（修复 v3 复核 P0-01）
        async with self._semaphore:
            fresh_row = await self._db.webhooks.get_if_active_lease(delivery_id, lease_token)
            if fresh_row is None:
                # 在 DNS 期间已被清理(scrubbed)或租约已失效，立即中止发送！
                return
            still_allowed, latest_secret, latest_auth_err = await self._lookup_project_delivery_auth(project_id)
            if not still_allowed:
                await self._db.webhooks.mark_retry_or_failed(
                    delivery_id,
                    attempts=max_attempts,
                    max_attempts=max_attempts,
                    status_code=0,
                    error=latest_auth_err or "项目密钥已停用或删除，停止投递",
                    retry_delay_seconds=0,
                    scrub_on_terminal=contains_code,
                    expected_lease=lease_token,
                )
                return
            secret = latest_secret
            if contains_code and order_id:
                fresh_order = await self._db.orders.get(order_id, include_messages=False)
                if fresh_order is not None and bool(fresh_order.get("code_scrubbed")):
                    await self._db.webhooks.mark_scrubbed(delivery_id)
                    return

            try:
                decoded_json = decode_outbox_payload(str(fresh_row["payload_json"]))
            except Exception as exc:
                await self._db.webhooks.mark_retry_or_failed(
                    delivery_id,
                    attempts=max_attempts,
                    max_attempts=max_attempts,
                    status_code=0,
                    error=f"Webhook 负载解密失败 ({type(exc).__name__})",
                    retry_delay_seconds=0,
                    scrub_on_terminal=contains_code,
                    expected_lease=lease_token,
                )
                return

            body = decoded_json.encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "sms-hub-webhook/1.0",
                "X-SMS-Event": event_type,
                "X-SMS-Delivery": delivery_id,
            }
            if secret:
                headers["X-SMS-Signature"] = compute_webhook_signature(secret, body)

            status_code = 0
            error_text = ""
            ok = False
            with pin_webhook_host(
                parsed_host,
                approved_ips,
                allow_private=allow_private,
                allow_loopback=explicit_allow,
            ):
                try:
                    status_code = await self._send_http_post(clean_url, body=body, headers=headers)
                    if 200 <= status_code < 300:
                        ok = True
                    else:
                        # 绝不将接收端响应正文存入 last_error 或日志，防止回显验证码二次泄露
                        error_text = _format_http_error(status_code)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_text = f"投递网络异常 ({type(exc).__name__})"

        if ok:
            # 严格校验 expected_lease，绝不将并发脱敏后的 scrubbed 改回 delivered
            await self._db.webhooks.mark_delivered(
                delivery_id,
                status_code=status_code,
                attempts=attempts,
                scrub_payload=contains_code,
                expected_lease=lease_token,
            )
            return

        delay = self._retry_delay_for(attempts)
        updated = await self._db.webhooks.mark_retry_or_failed(
            delivery_id,
            attempts=attempts,
            max_attempts=max_attempts,
            status_code=status_code,
            error=error_text,
            retry_delay_seconds=delay,
            scrub_on_terminal=contains_code,
            expected_lease=lease_token,
        )
        if updated:
            logger.warning(
                "Webhook delivery retry/fail id=%s event=%s attempt=%s/%s status=%s error=%s",
                delivery_id,
                event_type,
                attempts,
                max_attempts,
                status_code,
                error_text,
            )

    def _retry_delay_for(self, attempts: int) -> float:
        if not self._retry_delays:
            return 5.0
        delay_idx = min(max(0, attempts - 1), len(self._retry_delays) - 1)
        return float(self._retry_delays[delay_idx])

    async def send_test(self, url: str, *, secret: str = "", project_id: str = "") -> dict[str, Any]:
        """测试回调同样执行异步出站安全策略、IP 绑定与禁止重定向。"""
        clean_url, approved_ips = await validate_webhook_url_async(
            url,
            resolve_dns=True,
            dns_timeout=self._dns_timeout,
        )
        parsed_host = normalize_url_hostname(urlparse(clean_url).hostname or "")
        explicit_allow = _host_matches_allowlist(parsed_host, env.webhook_allowed_hosts)
        allow_private = explicit_allow or env.webhook_allow_private_nets

        payload = {
            "id": "evt_test",
            "event": "webhook.test",
            "project_id": project_id or "web-console",
            "data": {
                "message": "SMS Hub Webhook 连通性测试成功",
                "sample_code": "123456",
            },
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "sms-hub-webhook/1.0",
            "X-SMS-Event": "webhook.test",
            "X-SMS-Delivery": "dlv_test",
        }
        if secret:
            headers["X-SMS-Signature"] = compute_webhook_signature(secret, body)

        async with self._semaphore:
            with pin_webhook_host(
                parsed_host,
                approved_ips,
                allow_private=allow_private,
                allow_loopback=explicit_allow,
            ):
                try:
                    status_code = await self._send_http_post(clean_url, body=body, headers=headers)
                    if 200 <= status_code < 300:
                        return {"ok": True, "status_code": status_code, "attempts": 1}
                    return {
                        "ok": False,
                        "status_code": status_code,
                        "error": _format_http_error(status_code),
                        "attempts": 1,
                    }
                except Exception as exc:
                    return {
                        "ok": False,
                        "status_code": 0,
                        "error": f"投递网络异常 ({type(exc).__name__})",
                        "attempts": 1,
                    }
