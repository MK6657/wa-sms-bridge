"""注册编排器 — 自动 WhatsApp 注册的核心 Saga 流程。

流程：
  取号 → 查黑名单 → 探测 → 注册 → 等码 → 提交OTP → 验证 → 确认

补偿原则：
  每轮国家循环必须保证：要么 complete（销毁 activation），要么 cancel（退款）。
  不允许中间态泄漏。
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Any

import phonenumbers

from app.config import env
from app.db import Database
from app.models import PhoneInput
from app.providers.registry import ProviderRegistry
from app.providers.base import (
    Activation,
    BalanceError,
    NoNumbersError,
    PurchaseUncertainError,
    SmsCancelledError,
    SmsExpiredError,
    SmsProviderError,
    SmsTimeoutError,
)
from app.log_redaction import mask_identifier, mask_phone
from app.phone_normalizer import PhoneNormalizationError, validate_phone_input
from app.price_policy import effective_price_max, locked_price_available, polling_interval_seconds
from app.service_codes import canonical_service, provider_service_code

logger = logging.getLogger(__name__)

DEFAULT_COMPENSATION_RETRY_DELAYS = (1.0, 2.0, 5.0)
DEFAULT_RECONCILIATION_INTERVAL = 30.0

OTP_FAILURE_MESSAGES = {
    "security_verification_required": (
        "二步验证/安全确认拦截：短信验证码已收到，但号码需要 PIN 或额外安全确认；接码平台可能已扣费"
    ),
    "otp_invalid": "短信验证码被 WhatsApp 判定为无效或不匹配",
    "otp_rate_limited": "验证码提交过于频繁，WhatsApp 已限制继续尝试",
    "wa_blocked": "WhatsApp 已阻止该号码继续注册",
}


@dataclass
class CompensationOutcome:
    activation_settled: bool = True
    account_cleaned: bool = True
    activation_result: str = ""
    account_result: str = ""

    @property
    def complete(self) -> bool:
        return self.activation_settled and self.account_cleaned


def _normalize_smsbower_phone(raw_phone: str, country_id: int) -> PhoneInput:
    """将 SMSBower 原始号码转为 wa-app PhoneInput。

    SMSBower 返回含国家码前缀的号码字符串。
    用 phonenumbers 库归一化到 E.164。
    """
    # 从 COUNTRIES 取国家区号
    from app.providers.smsbower import COUNTRIES, COUNTRY_ISO2
    info = COUNTRIES.get(country_id, {})
    cc = info.get("cc", "")
    e164_str = f"+{raw_phone}" if not raw_phone.startswith("+") else raw_phone

    try:
        parsed = phonenumbers.parse(e164_str, None)
        if not phonenumbers.is_possible_number(parsed):
            if cc and not raw_phone.startswith(cc):
                e164_str = f"+{cc}{raw_phone}"
                parsed = phonenumbers.parse(e164_str, None)
    except phonenumbers.NumberParseException:
        if cc:
            e164_str = f"+{cc}{raw_phone.lstrip('+')}"
            try:
                parsed = phonenumbers.parse(e164_str, None)
            except phonenumbers.NumberParseException:
                parsed = None
        else:
            parsed = None

    if parsed and phonenumbers.is_valid_number(parsed):
        return PhoneInput(
            region=phonenumbers.region_code_for_number(parsed) or "",
            phone=str(parsed.national_number),
            e164_number=phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
            country_calling_code=str(parsed.country_code),
            country_iso2=(phonenumbers.region_code_for_number(parsed) or "").lower(),
        )

    # 兜底：手动构造（phonenumbers 解析失败时的最后手段）
    national = raw_phone
    if cc and raw_phone.startswith(cc):
        national = raw_phone[len(cc):]
    iso2 = COUNTRY_ISO2.get(country_id, "")
    region = iso2.upper() if iso2 else info.get("eng", "")[:3].upper()
    return PhoneInput(
        region=region,
        phone=national,
        e164_number=f"+{cc}{national}" if cc else national,
        country_calling_code=cc,
        country_iso2=iso2,
    )


class RegisterOrchestrator:
    """注册编排器。"""

    def __init__(self, registry: ProviderRegistry, waapi: "WaApiClient", db: Database):
        self._registry = registry
        self._waapi = waapi
        self._db = db
        self._provider_name: str = "smsbower"
        self._tasks: dict[str, asyncio.Task] = {}  # task_id → asyncio.Task
        self._compensation_retry_delays = DEFAULT_COMPENSATION_RETRY_DELAYS
        self._reconciliation_interval = DEFAULT_RECONCILIATION_INTERVAL
        self._provider_error_retry_delay = 1.0
        self._reconciliation_task: Optional[asyncio.Task] = None

    @property
    def _provider(self):
        """获取当前 provider 实例。"""
        return self._registry.get(self._provider_name)

    async def _safe_cancel_activation(
        self,
        provider_name: str,
        activation_id: str,
        task_id: str = "",
        reason: str = "",
    ) -> tuple[bool, str]:
        if not activation_id:
            return True, "not_required"
        try:
            from app.providers.base import parse_cancel_outcome

            prov = self._registry.get(provider_name)
            result = await prov._safe_cancel(activation_id)
            outcome = parse_cancel_outcome(result)
            settled = outcome.settled
            text = str(result or "").strip() or outcome.status
            log_method = logger.info if settled else logger.warning
            log_method(
                "[%s] 补偿取消激活: provider=%s activation=%s reason=%s result=%s settled=%s",
                task_id or "-",
                provider_name,
                mask_identifier(activation_id),
                reason or "-",
                text or "-",
                settled,
            )
            return settled, text
        except Exception as e:
            logger.warning(
                "[%s] 补偿取消激活失败: provider=%s activation=%s reason=%s error=%s",
                task_id or "-",
                provider_name,
                mask_identifier(activation_id),
                reason or "-",
                e,
            )
            return False, str(e)

    async def _safe_cleanup_account(
        self,
        wa_account_id: str,
        verification_request_id: str = "",
        task_id: str = "",
        reason: str = "",
    ) -> tuple[bool, str]:
        if not wa_account_id:
            return True, "not_required"
        try:
            result = await self._waapi.cleanup_failed_account(
                wa_account_id,
                verification_request_id=verification_request_id or "",
            )
            if isinstance(result, dict):
                deleted = bool(result.get("deleted"))
                cleanup_reason = str(result.get("reason") or "").strip().lower()
                cleaned = deleted or cleanup_reason in {"already_deleted", "missing_wa_account_id"}
                if not cleaned:
                    logger.warning(
                        "[%s] WA 账号未删除: account=%s reason=%s status=%s",
                        task_id or "-",
                        mask_identifier(wa_account_id),
                        cleanup_reason or "unknown",
                        result.get("status") or "-",
                    )
                    return False, cleanup_reason or "not_deleted"
            logger.info(
                "[%s] 已补偿清理 WA 账号: account=%s reason=%s",
                task_id or "-",
                mask_identifier(wa_account_id),
                reason or "-",
            )
            return True, "cleaned"
        except Exception as e:
            logger.warning(
                "[%s] 补偿清理 WA 账号失败: account=%s reason=%s error=%s",
                task_id or "-",
                mask_identifier(wa_account_id),
                reason or "-",
                e,
            )
            return False, str(e)

    async def _safe_complete_activation(
        self,
        provider_name: str,
        activation_id: str,
        task_id: str = "",
        reason: str = "",
    ) -> tuple[bool, str]:
        if not activation_id:
            return True, "not_required"
        try:
            result = await self._registry.get(provider_name).complete_activation(activation_id)
            logger.info(
                "[%s] 已完成 SMS 激活: provider=%s activation=%s reason=%s result=%s",
                task_id or "-",
                provider_name,
                mask_identifier(activation_id),
                reason or "-",
                result or "-",
            )
            return True, str(result or "completed")
        except Exception as exc:
            logger.warning(
                "[%s] 完成 SMS 激活失败: provider=%s activation=%s reason=%s error=%s",
                task_id or "-",
                provider_name,
                mask_identifier(activation_id),
                reason or "-",
                exc,
            )
            return False, str(exc)

    async def _compensate_handles(
        self,
        task_id: str,
        provider_name: str,
        *,
        activation: Optional[Activation] = None,
        reg: Any = None,
        task_snapshot: Optional[dict] = None,
        reason: str = "",
    ) -> CompensationOutcome:
        activation_id = (
            getattr(activation, "activation_id", "") or
            (task_snapshot or {}).get("activation_id", "")
        )
        wa_account_id = (
            getattr(reg, "wa_account_id", "") or
            (task_snapshot or {}).get("wa_account_id", "")
        )
        verification_request_id = (
            getattr(reg, "verification_request_id", "") or
            (task_snapshot or {}).get("verification_request_id", "")
        )
        activation_already_settled = bool((task_snapshot or {}).get("activation_compensated"))
        account_already_cleaned = bool((task_snapshot or {}).get("account_compensated"))
        async def with_timeout(coro, label: str):
            try:
                return await asyncio.wait_for(coro, timeout=12)
            except asyncio.TimeoutError:
                logger.warning("[%s] %s 超时，保留为待补偿", task_id or "-", label)
                return False, "timeout"

        activation_result, account_result = await asyncio.gather(
            asyncio.sleep(
                0,
                result=(True, "already_settled"),
            ) if activation_already_settled else with_timeout(
                self._safe_cancel_activation(provider_name, activation_id, task_id, reason),
                "取消 SMS 激活",
            ),
            asyncio.sleep(
                0,
                result=(True, "already_cleaned"),
            ) if account_already_cleaned else with_timeout(
                self._safe_cleanup_account(
                    wa_account_id,
                    verification_request_id,
                    task_id,
                    reason,
                ),
                "清理 WA 账号",
            ),
        )
        return CompensationOutcome(
            activation_settled=activation_result[0],
            account_cleaned=account_result[0],
            activation_result=activation_result[1],
            account_result=account_result[1],
        )

    async def _compensate_task_snapshot(self, task: Optional[dict], reason: str = ""):
        if not task:
            return CompensationOutcome()
        commit_state = str(task.get("account_commit_state") or "none")
        account_already_cleaned = bool(task.get("account_compensated"))
        if commit_state in {"pending", "committed"} and not account_already_cleaned:
            settled, result = await self._safe_complete_activation(
                task.get("provider", "smsbower") or "smsbower",
                task.get("activation_id", ""),
                task.get("task_id", ""),
                reason or f"保护 {commit_state} 账号",
            )
            return CompensationOutcome(
                activation_settled=settled,
                account_cleaned=True,
                activation_result=result,
                account_result=f"skipped_{commit_state}",
            )
        return await self._compensate_handles(
            task.get("task_id", ""),
            task.get("provider", "smsbower") or "smsbower",
            task_snapshot=task,
            reason=reason,
        )

    async def _clear_attempt_handles(self, task_id: str, **extra):
        await self._db.update_task(
            task_id,
            activation_id="",
            phone_number="",
            wa_account_id="",
            verification_request_id="",
            wa_status="",
            registration_phase="",
            account_commit_state="none",
            attempt_request_id="",
            sms_finalize_state="",
            activation_compensated=0,
            account_compensated=0,
            **extra,
        )

    async def _checkpoint_task(self, task_id: str, **kwargs):
        """Persist an external handle before allowing cancellation to continue."""
        checkpoint = asyncio.create_task(self._db.update_task(task_id, **kwargs))
        try:
            await asyncio.shield(checkpoint)
        except asyncio.CancelledError:
            await checkpoint
            raise

    async def _mark_attention(self, task_id: str, message: str, **kwargs):
        fields = {
            "status": "attention",
            "error": message,
            "reconciliation_required": 1,
            "reconciliation_error": message,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        fields.update(kwargs)
        await self._db.update_task(task_id, **fields)

    async def _mark_compensation_pending(
        self,
        task_id: str,
        outcome: CompensationOutcome,
        message: str,
        step: str = "补偿待重试",
    ):
        await self._db.update_task(
            task_id,
            status="compensation_pending",
            step=step,
            error=message,
            reconciliation_required=1,
            reconciliation_error=(
                f"sms={outcome.activation_result or '-'}; "
                f"wa={outcome.account_result or '-'}"
            ),
            activation_compensated=int(outcome.activation_settled),
            account_compensated=int(outcome.account_cleaned),
        )

    async def _compensate_attempt_with_retry(
        self,
        task_id: str,
        provider_name: str,
        *,
        activation: Optional[Activation] = None,
        reg: Any = None,
        reason: str,
    ) -> bool:
        """Retry transient cleanup failures before handing the task to reconciliation."""
        outcome = await self._compensate_handles(
            task_id,
            provider_name,
            activation=activation,
            reg=reg,
            reason=reason,
        )
        if outcome.complete:
            await self._clear_attempt_handles(task_id)
            return True

        delays = tuple(self._compensation_retry_delays)
        for retry_index, delay in enumerate(delays, start=1):
            await self._mark_compensation_pending(
                task_id,
                outcome,
                f"{reason}：补偿重试中",
                step=f"补偿重试 {retry_index}/{len(delays)}",
            )
            logger.warning(
                "[%s] 补偿未完成，%.1f 秒后重试 (%s/%s): sms=%s wa=%s",
                task_id,
                delay,
                retry_index,
                len(delays),
                outcome.activation_result or "-",
                outcome.account_result or "-",
            )
            await asyncio.sleep(max(0.0, float(delay)))
            snapshot = await self._db.get_task(task_id)
            outcome = await self._compensate_handles(
                task_id,
                provider_name,
                activation=activation,
                reg=reg,
                task_snapshot=snapshot,
                reason=f"{reason} retry {retry_index}",
            )
            if outcome.complete:
                await self._clear_attempt_handles(
                    task_id,
                    status="running",
                    step="补偿完成，继续取号",
                    error="",
                    reconciliation_required=0,
                    reconciliation_error="",
                )
                logger.info("[%s] 补偿重试成功，恢复注册轮询", task_id)
                return True

        await self._mark_compensation_pending(
            task_id,
            outcome,
            f"{reason}：补偿尚未完成",
            step="补偿待后台重试",
        )
        return False

    @staticmethod
    def _reconciled_error(task: dict) -> str:
        message = str(task.get("error") or "").strip()
        for suffix in ("：补偿尚未完成", "：补偿重试中", "补偿尚未完成"):
            message = message.replace(suffix, "")
        return message.strip(" ：") or "上次尝试失败，补偿已完成"

    async def _finish_reconciled_task(
        self,
        task: dict,
        outcome: CompensationOutcome,
        *,
        source: str,
    ) -> bool:
        task_id = str(task.get("task_id") or "")
        commit_state = str(task.get("account_commit_state") or "none")
        account_already_cleaned = bool(task.get("account_compensated"))
        if commit_state in {"pending", "committed"} and not account_already_cleaned:
            await self._mark_attention(
                task_id,
                "账号可能已经提交，已保护账号且未执行删除，请核对登录态和 SMS 订单",
                step=f"{source}后需核对",
                sms_finalize_state="completed" if outcome.activation_settled else "pending",
            )
            return True
        if not outcome.complete:
            await self._mark_compensation_pending(
                task_id,
                outcome,
                f"{source}：补偿尚未完成",
                step="补偿待后台重试",
            )
            return False

        original_status = str(task.get("status") or "")
        original_error = str(task.get("error") or "")
        cancelled = original_status == "running" or any(
            token in original_error for token in ("手动停止", "任务取消", "任务被取消")
        )
        await self._clear_attempt_handles(
            task_id,
            status="cancelled" if cancelled else "failed",
            step="已恢复取消" if cancelled else "补偿完成，任务结束",
            error=(
                "container restart recovery"
                if cancelled and source == "启动恢复"
                else self._reconciled_error(task)
            ),
            reconciliation_required=0,
            reconciliation_error="",
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info("[%s] %s完成，遗留资源已结清", task_id, source)
        return True

    async def reconcile_pending_tasks(self) -> int:
        """Retry orphaned compensation tasks without racing active registrations."""
        reconciled = 0
        for task in await self._db.get_pending_tasks():
            if task.get("status") != "compensation_pending":
                continue
            task_id = str(task.get("task_id") or "")
            active = self._tasks.get(task_id)
            if active and not active.done():
                continue
            outcome = await self._compensate_task_snapshot(task, reason="后台补偿重试")
            if await self._finish_reconciled_task(task, outcome, source="后台补偿"):
                reconciled += 1
        return reconciled

    async def _reconciliation_loop(self):
        try:
            while True:
                try:
                    reconciled = await self.reconcile_pending_tasks()
                    if reconciled:
                        logger.info("后台补偿扫描已处理 %s 个遗留任务", reconciled)
                except Exception:
                    logger.warning("后台补偿扫描异常", exc_info=True)
                await asyncio.sleep(max(1.0, float(self._reconciliation_interval)))
        except asyncio.CancelledError:
            return

    def start_background_tasks(self):
        if self._reconciliation_task and not self._reconciliation_task.done():
            return
        self._reconciliation_task = asyncio.create_task(self._reconciliation_loop())
        logger.info("后台补偿器已启动: interval=%.1fs", self._reconciliation_interval)

    async def recover_stale_tasks(self) -> int:
        """启动恢复：补偿 running 任务遗留的上游资源，然后标记取消。"""
        tasks = await self._db.get_pending_tasks()
        for task in tasks:
            logger.warning(
                "recovery: stale task %s activation=%s wa_account=%s",
                task.get("task_id"),
                mask_identifier(task.get("activation_id", "")),
                mask_identifier(task.get("wa_account_id", "")),
            )
            outcome = await self._compensate_task_snapshot(task, reason="container restart recovery")
            await self._finish_reconciled_task(task, outcome, source="启动恢复")
        return len(tasks)

    async def shutdown(self):
        """关闭时取消所有运行中的任务。"""
        if self._reconciliation_task and not self._reconciliation_task.done():
            self._reconciliation_task.cancel()
            await asyncio.gather(self._reconciliation_task, return_exceptions=True)
        self._reconciliation_task = None
        tasks = list(self._tasks.items())
        for tid, task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        self._tasks.clear()

    # ── 公开接口 ────────────────────────────────────────

    async def start_registration(self, task_id: str, country: str,
                                  country_fallback: list[Any],
                                  max_attempts: int = 3,
                                  price_max: Optional[float] = None,
                                  wait_timeout: int = 180,
                                  provider_name: str = "smsbower",
                                  service: str = "whatsapp",
                                  target_price: Optional[float] = None,
                                  price_lock_enabled: bool = False,
                                  price_float_enabled: bool = False,
                                  price_float_percent: float = 0,
                                  continuous_poll: bool = False,
                                  target_success_count: int = 1,
                                  request_interval_enabled: bool = False,
                                  request_interval: float = 1.0,
                                  proxy: str = "") -> dict:
        """异步启动注册任务。"""
        provider_name = (provider_name or "smsbower").strip().lower()
        raw_svc = str(service or "whatsapp").strip().lower()
        if raw_svc.startswith("custom:") or canonical_service(service) != "whatsapp":
            return {
                "task_id": task_id,
                "status": "failed",
                "error": "自动注册链路仅支持 WhatsApp；Google/Telegram 仅可用于价格查询",
            }
        try:
            provider = self._registry.get(provider_name)
        except ValueError:
            return {"task_id": task_id, "status": "failed",
                    "error": f"未知 SMS 平台: {provider_name}"}
        try:
            cid, clabel = await provider.resolve_country(country)
            await provider.validate_registration_country(cid)
        except ValueError as exc:
            return {
                "task_id": task_id,
                "status": "failed",
                "error": str(exc) or f"未知国家: {country}",
            }
        except Exception as e:
            logger.warning("[%s] 国家解析失败: provider=%s country=%s error=%s", task_id, provider_name, country, e)
            return {"task_id": task_id, "status": "failed", "error": f"国家解析失败: {e}"}

        # 构建候选国家列表
        candidates = [(cid, clabel)]
        seen = {cid}
        for fb in country_fallback:
            try:
                info = await provider.resolve_country(str(fb))
                if not info or info[0] in seen:
                    continue
                await provider.validate_registration_country(info[0])
                candidates.append(info)
                seen.add(info[0])
            except ValueError:
                continue
            except Exception as e:
                logger.warning("[%s] 备用国家解析失败: provider=%s country=%s error=%s", task_id, provider_name, fb, e)
                continue

        await self._db.create_task(
            task_id,
            cid,
            clabel,
            provider=provider_name,
            target_success_count=target_success_count,
        )

        # 异步执行 — 传 provider_name，不依赖 self._provider_name（避免竞态）
        coro = self._run_registration(
            task_id, candidates, max_attempts, price_max, wait_timeout,
            provider_name, service, target_price, price_lock_enabled,
            price_float_enabled, price_float_percent, continuous_poll,
            target_success_count, request_interval_enabled, request_interval, proxy,
        )
        task = asyncio.create_task(coro)
        self._tasks[task_id] = task
        return {"task_id": task_id, "status": "running"}

    async def get_task(self, task_id: str):
        return await self._db.get_task(task_id)

    async def list_tasks(self) -> list[dict]:
        return await self._db.list_tasks()

    async def stop_task(self, task_id: str) -> Optional[dict]:
        """手动停止任务。"""
        existing = await self._db.get_task(task_id)
        if not existing:
            return None
        if existing.get("status") not in {"running", "compensation_pending"}:
            return existing

        t = self._tasks.get(task_id)
        if t and not t.done():
            t.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=15)
            except asyncio.TimeoutError:
                logger.warning("[%s] stop_task: 等待任务补偿超时，执行 DB 快照兜底补偿", task_id)
            except asyncio.CancelledError:
                logger.info("[%s] stop_task: 任务已取消", task_id)
            except Exception:
                logger.warning("[%s] stop_task: 任务取消期间异常", task_id, exc_info=True)

        task = await self._db.get_task(task_id)
        if task and task.get("status") in {"running", "compensation_pending"}:
            commit_state = str(task.get("account_commit_state") or "none")
            account_already_cleaned = bool(task.get("account_compensated"))
            outcome = await self._compensate_task_snapshot(task, reason="手动停止")
            if commit_state in {"pending", "committed"} and not account_already_cleaned:
                await self._mark_attention(
                    task_id,
                    "停止发生在账号提交阶段，已保护账号且未执行删除，请核对登录态",
                    step="停止后需核对",
                    sms_finalize_state="completed" if outcome.activation_settled else "pending",
                )
            elif outcome.complete:
                await self._clear_attempt_handles(
                    task_id,
                    status="cancelled",
                    error="手动停止",
                    step="已停止",
                )
            else:
                await self._mark_compensation_pending(
                    task_id,
                    outcome,
                    "手动停止：补偿尚未完成",
                )
        self._tasks.pop(task_id, None)
        return await self._db.get_task(task_id)

    # ── 核心编排 ────────────────────────────────────────

    async def _run_registration(self, task_id: str, candidates: list[tuple[int, str]],
                                 max_attempts: int,
                                 price_max: Optional[float],
                                 wait_timeout: int,
                                 provider_name: str = "smsbower",
                                 service: str = "whatsapp",
                                 target_price: Optional[float] = None,
                                 price_lock_enabled: bool = False,
                                 price_float_enabled: bool = False,
                                 price_float_percent: float = 0,
                                 continuous_poll: bool = False,
                                 target_success_count: int = 1,
                                 request_interval_enabled: bool = False,
                                 request_interval: float = 1.0,
                                 proxy: str = ""):
        """Saga 编排主循环。task_state 只保存当前尝试句柄，成功结果进入历史账本。"""
        attempts_used = 0
        final_error = ""
        proxy_url = (proxy or "").strip()

        def _p():
            return self._registry.get(provider_name)

        success_target = max(1, int(target_success_count or 1))
        successes_done = 0
        candidate_index = 0
        service_code = provider_service_code(provider_name, service)

        try:
            max_price = effective_price_max(
                target_price=target_price,
                price_max=price_max,
                price_lock_enabled=price_lock_enabled,
                price_float_enabled=price_float_enabled,
                price_float_percent=price_float_percent,
            )
            sleep_seconds = polling_interval_seconds(
                request_interval_enabled=request_interval_enabled,
                request_interval=request_interval,
                continuous_poll=continuous_poll,
            )

            async def _sleep_between_attempts():
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)

            while successes_done < success_target:
                if not continuous_poll and attempts_used >= max_attempts:
                    final_error = final_error or f"已达最大尝试次数 ({max_attempts})"
                    break

                cid, clabel = candidates[candidate_index % len(candidates)]
                candidate_index += 1
                activation: Optional[Activation] = None
                reg: Any = None
                attempt_committed = False
                wait_before_next = False

                async def compensate_attempt(reason: str) -> bool:
                    return await self._compensate_attempt_with_retry(
                        task_id,
                        provider_name,
                        activation=activation,
                        reg=reg,
                        reason=reason,
                    )

                try:
                    attempts_used += 1
                    attempt_request_id = f"{task_id}:{attempts_used}"
                    await self._db.update_task(
                        task_id,
                        step="锁价检查" if price_lock_enabled else "取号",
                        country_id=cid,
                        country_label=clabel,
                        attempts_used=attempts_used,
                        activation_id="",
                        phone_number="",
                        wa_account_id="",
                        verification_request_id="",
                        wa_status="",
                        registration_phase="",
                        failure_category="",
                        failure_code="",
                        account_commit_state="none",
                        attempt_request_id=attempt_request_id,
                        sms_finalize_state="",
                        reconciliation_required=0,
                        reconciliation_error="",
                    )
                    if price_lock_enabled and target_price is not None:
                        entries = await _p().get_prices(cid, service_code)
                        available, stock = locked_price_available(entries, target_price)
                        if not available:
                            raise NoNumbersError(f"锁定价格 {target_price} 暂无库存 (stock={stock})")

                    await self._db.update_task(task_id, step="取号")
                    activation = await _p().request_number(cid, service_code, max_price)
                    await self._checkpoint_task(
                        task_id,
                        step="已取号",
                        activation_id=activation.activation_id,
                        phone_number=activation.phone_number,
                    )
                    logger.info(
                        "[%s] 取号成功: provider=%s service=%s activation=%s phone=%s",
                        task_id,
                        provider_name,
                        service_code,
                        mask_identifier(activation.activation_id),
                        mask_phone(activation.phone_number),
                    )

                    if await self._db.is_blacklisted(activation.phone_number, provider=provider_name):
                        final_error = "号码在黑名单中"
                        if not await compensate_attempt("号码在黑名单中"):
                            return
                        wait_before_next = True
                        continue

                    try:
                        phone_input = validate_phone_input(
                            await _p().normalize_phone(activation.phone_number, cid)
                        )
                    except PhoneNormalizationError as exc:
                        logger.warning(
                            "[%s] 号码归一化失败: provider=%s country=%s activation=%s phone=%s error=%s",
                            task_id,
                            provider_name,
                            clabel,
                            mask_identifier(activation.activation_id),
                            mask_phone(activation.phone_number),
                            exc,
                        )
                        final_error = str(exc)
                        if not await compensate_attempt("号码归一化失败"):
                            return
                        await self._db.update_task(
                            task_id,
                            step="号码归一化失败",
                            error=str(exc),
                            attempts_used=attempts_used,
                        )
                        wait_before_next = True
                        continue

                    if phone_input.e164_number != activation.phone_number and await self._db.is_blacklisted(
                        phone_input.e164_number, provider=provider_name
                    ):
                        final_error = "号码在黑名单中"
                        if not await compensate_attempt("号码在黑名单中"):
                            return
                        wait_before_next = True
                        continue

                    await self._db.update_task(task_id, step="探测中")
                    probe = await self._waapi.probe_phone(
                        phone_input,
                        proxy_url=proxy_url,
                        correlation_id=task_id,
                        request_id=f"{attempt_request_id}:probe",
                    )
                    if not probe.ok:
                        final_error = probe.status or probe.error_message or "WA 探测不通过"
                        if not await compensate_attempt("WA 探测不通过"):
                            return
                        wait_before_next = True
                        continue

                    await self._db.update_task(task_id, step="注册中")
                    reg = await self._waapi.register(
                        phone_input,
                        proxy_url=proxy_url,
                        correlation_id=task_id,
                        request_id=f"{attempt_request_id}:register",
                    )
                    if reg.request_failed or not reg.success or not reg.wa_account_id:
                        final_error = reg.error_message or reg.status or "WA 注册未返回账号"
                        if not await compensate_attempt("WA 注册请求失败"):
                            return
                        wait_before_next = True
                        continue

                    if reg.is_transfer_challenge:
                        logger.info(
                            "[%s] 需要账号转移验证, challenge_keys=%s",
                            task_id,
                            sorted(reg.challenge_data.keys()),
                        )
                    await self._checkpoint_task(
                        task_id,
                        step="已注册",
                        wa_account_id=reg.wa_account_id,
                        verification_request_id=reg.verification_request_id,
                        wa_status=reg.status,
                        registration_phase=reg.registration_phase,
                    )

                    await self._db.update_task(task_id, step="等码中")
                    try:
                        sms_code = await _p().poll_sms_code(
                            activation.activation_id,
                            timeout=wait_timeout,
                        )
                    except SmsTimeoutError:
                        final_error = f"等码超时 ({wait_timeout}s)"
                        if not await compensate_attempt("等码超时"):
                            return
                        await self._db.update_task(
                            task_id,
                            step="等码超时",
                            attempts_used=attempts_used,
                        )
                        wait_before_next = True
                        continue

                    logger.info("[%s] 收到验证码: digits=%s", task_id, len(sms_code))
                    await self._db.update_task(
                        task_id,
                        step="提交 OTP",
                        account_commit_state="pending",
                    )
                    try:
                        otp_result = await self._waapi.submit_otp(
                            reg.wa_account_id,
                            sms_code,
                            verification_request_id=reg.verification_request_id,
                            proxy_url=proxy_url,
                            correlation_id=task_id,
                            request_id=f"{attempt_request_id}:otp",
                        )
                    except Exception as otp_exc:
                        attempt_committed = True
                        raise otp_exc
                    if otp_result.request_failed or not otp_result.success or not (
                        otp_result.login_active or otp_result.registered
                    ):
                        failure_category = otp_result.failure_category
                        failure_code = otp_result.failure_reason or otp_result.error_code or otp_result.status
                        final_error = OTP_FAILURE_MESSAGES.get(
                            failure_category,
                            otp_result.error_message or otp_result.status or "OTP 提交后注册未完成",
                        )
                        failure_step = (
                            "二步验证/安全确认拦截"
                            if failure_category == "security_verification_required"
                            else "OTP 提交失败"
                        )
                        await self._db.update_task(
                            task_id,
                            step=failure_step,
                            error=final_error,
                            failure_category=failure_category,
                            failure_code=failure_code,
                            account_commit_state="none",
                            attempts_used=attempts_used,
                            wa_status=otp_result.status,
                            registration_phase=otp_result.registration_phase,
                        )
                        logger.warning(
                            "[%s] OTP 注册失败: category=%s code=%s status=%s error=%s",
                            task_id,
                            failure_category,
                            failure_code or "-",
                            otp_result.status or "-",
                            otp_result.error_message or "-",
                        )
                        if not await compensate_attempt(failure_step):
                            return
                        wait_before_next = True
                        continue

                    attempt_committed = True
                    await self._checkpoint_task(
                        task_id,
                        step="注册已提交",
                        account_commit_state="committed",
                        wa_status=otp_result.status,
                        registration_phase=otp_result.registration_phase,
                    )

                    await self._db.update_task(task_id, step="验证登录态")
                    login_ok = otp_result.login_active
                    login_check_error = ""
                    if not login_ok:
                        try:
                            login_ok = await self._waapi.check_login_state(
                                reg.wa_account_id,
                                login_state_id=otp_result.login_state_id,
                                proxy_url=proxy_url,
                                correlation_id=task_id,
                                request_id=f"{attempt_request_id}:login",
                            )
                        except Exception as exc:
                            login_check_error = str(exc)
                    if not login_ok:
                        try:
                            accounts = await self._waapi.list_accounts(limit=1000)
                        except Exception as exc:
                            await self._mark_attention(
                                task_id,
                                "OTP 已返回 REGISTERED，但登录态无法确认；"
                                f"已保护账号不做删除: {login_check_error or exc}",
                                step="登录态待核对",
                                sms_finalize_state="pending",
                            )
                            return
                        if any(a.get("wa_account_id") == reg.wa_account_id for a in accounts):
                            login_ok = True
                        else:
                            attempt_committed = False
                            await self._db.update_task(task_id, account_commit_state="none")
                            final_error = "登录态验证失败且账号不存在"
                            if not await compensate_attempt("登录态验证失败"):
                                return
                            wait_before_next = True
                            continue

                    next_success_count = successes_done + 1
                    try:
                        await self._db.record_registration_success(
                            task_id=task_id,
                            phone=phone_input.e164_number,
                            country_id=cid,
                            country_label=clabel,
                            activation_id=activation.activation_id,
                            wa_account_id=reg.wa_account_id,
                            sms_code=sms_code,
                            cost=activation.price,
                            provider=provider_name,
                            verification_request_id=reg.verification_request_id,
                            success_count=next_success_count,
                            target_success_count=success_target,
                            cost_is_estimated=activation.price_is_estimated,
                            currency=activation.currency,
                        )
                    except Exception as exc:
                        await self._mark_attention(
                            task_id,
                            f"账号已注册，但成功账本写入失败，已停止后续取号: {exc}",
                            step="成功记录待核对",
                            sms_finalize_state="pending",
                        )
                        return
                    successes_done = next_success_count

                    sms_finalized, finalize_result = await self._safe_complete_activation(
                        provider_name,
                        activation.activation_id,
                        task_id,
                        "注册成功",
                    )
                    if not sms_finalized:
                        await self._mark_attention(
                            task_id,
                            f"账号已注册，但 SMS 订单完成失败: {finalize_result}",
                            step=f"完成 {successes_done}/{success_target}，订单待收尾",
                            sms_finalize_state="pending",
                        )
                        return

                    if successes_done >= success_target:
                        await self._db.update_task(
                            task_id,
                            status="completed",
                            step=f"完成 {successes_done}/{success_target}",
                            wa_account_id=reg.wa_account_id,
                            verification_request_id=reg.verification_request_id,
                            wa_status=otp_result.status,
                            registration_phase=otp_result.registration_phase,
                            failure_category="",
                            failure_code="",
                            error="",
                            successes_done=successes_done,
                            target_success_count=success_target,
                            sms_finalize_state="completed",
                            reconciliation_required=0,
                            reconciliation_error="",
                            completed_at=datetime.now(timezone.utc).isoformat(),
                        )
                        logger.info(
                            "[%s] 注册完成: wa_account_id=%s login_ok=%s success=%s/%s",
                            task_id,
                            mask_identifier(reg.wa_account_id),
                            login_ok,
                            successes_done,
                            success_target,
                        )
                        return

                    await self._clear_attempt_handles(
                        task_id,
                        status="running",
                        step=f"完成 {successes_done}/{success_target}，继续",
                        successes_done=successes_done,
                        target_success_count=success_target,
                        error="",
                    )
                    attempt_committed = False
                    wait_before_next = True
                    continue

                except PurchaseUncertainError as exc:
                    logger.error("[%s] 取号结果未知，停止自动购买: %s", task_id, exc)
                    await self._mark_attention(
                        task_id,
                        str(exc),
                        step="取号结果未知",
                    )
                    return
                except NoNumbersError as exc:
                    logger.debug("[%s] 跳过国家 %s: %s", task_id, clabel, exc)
                    final_error = str(exc)
                    if activation and not await compensate_attempt("无号"):
                        return
                    wait_before_next = True
                    continue
                except BalanceError as exc:
                    logger.warning(
                        "[%s] 平台余额不足或余额检查失败，停止任务 (country=%s): %s",
                        task_id,
                        clabel,
                        exc,
                    )
                    final_error = str(exc)
                    if activation and not await compensate_attempt("余额异常"):
                        return
                    break
                except SmsCancelledError:
                    final_error = "SMS 激活被平台取消"
                    if not await compensate_attempt("SMS 激活取消"):
                        return
                    wait_before_next = True
                    continue
                except SmsExpiredError:
                    final_error = "SMS 激活过期"
                    if not await compensate_attempt("SMS 激活过期"):
                        return
                    wait_before_next = True
                    continue
                except SmsProviderError as exc:
                    final_error = str(exc)
                    upper_err = final_error.upper()
                    is_fatal_provider_err = any(
                        tok in upper_err
                        for tok in ("BAD_KEY", "BAD_SERVICE", "BAD_ACTION", "BANNED", "API KEY 未配置", "鉴权失败")
                    )
                    if is_fatal_provider_err:
                        logger.error("[%s] 短信平台配置或鉴权错误，停止重试 (country=%s): %s", task_id, clabel, exc)
                        if activation:
                            await compensate_attempt("短信平台异常")
                        break
                    logger.warning(
                        "[%s] 短信平台暂时异常，稍后重试 (country=%s): %s",
                        task_id,
                        clabel,
                        exc,
                    )
                    if activation:
                        if not await compensate_attempt("短信平台异常"):
                            return
                    else:
                        attempts_used = max(0, attempts_used - 1)
                        await self._db.update_task(
                            task_id,
                            status="running",
                            step="平台连接重试",
                            error=str(exc),
                            attempts_used=attempts_used,
                        )
                    await asyncio.sleep(max(self._provider_error_retry_delay, sleep_seconds))
                    continue
                except asyncio.CancelledError:
                    logger.info("[%s] 当前尝试被取消 (country=%s)", task_id, clabel)
                    raise
                except Exception as exc:
                    logger.error(
                        "[%s] 编排异常 (country=%s): %s",
                        task_id,
                        clabel,
                        exc,
                        exc_info=True,
                    )
                    final_error = str(exc)
                    if attempt_committed:
                        settled, finalize_result = await self._safe_complete_activation(
                            provider_name,
                            getattr(activation, "activation_id", ""),
                            task_id,
                            "提交后异常保护",
                        )
                        await self._mark_attention(
                            task_id,
                            f"账号已注册，后续收尾异常，未执行账号删除: {exc}",
                            step="注册后需核对",
                            sms_finalize_state="completed" if settled else "pending",
                            reconciliation_error=(
                                str(exc) if settled else f"{exc}; sms={finalize_result}"
                            ),
                        )
                        return
                    if not await compensate_attempt("编排异常"):
                        return
                    wait_before_next = True
                    continue
                finally:
                    if wait_before_next:
                        await _sleep_between_attempts()

            if successes_done:
                await self._mark_attention(
                    task_id,
                    final_error or "未达到目标成功数量",
                    step=f"部分完成 {successes_done}/{success_target}",
                    successes_done=successes_done,
                    target_success_count=success_target,
                )
            else:
                await self._db.update_task(
                    task_id,
                    status="failed",
                    step="耗尽",
                    error=final_error or "未达到目标成功数量，均无可用号码或注册失败",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

        except asyncio.CancelledError:
            logger.info("[%s] 任务被取消", task_id)
            task = await self._db.get_task(task_id)
            commit_state = str((task or {}).get("account_commit_state") or "none")
            account_already_cleaned = bool((task or {}).get("account_compensated"))
            outcome = await self._compensate_task_snapshot(task, reason="任务取消")
            if commit_state in {"pending", "committed"} and not account_already_cleaned:
                await self._mark_attention(
                    task_id,
                    "任务在账号提交阶段被停止，已保护账号且未执行删除，请核对登录态",
                    step="停止后需核对",
                    sms_finalize_state="completed" if outcome.activation_settled else "pending",
                )
            elif outcome.complete:
                await self._clear_attempt_handles(
                    task_id,
                    status="cancelled",
                    error="任务被取消",
                    step="已停止",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                await self._mark_compensation_pending(
                    task_id,
                    outcome,
                    "任务取消：补偿尚未完成",
                )
        except Exception as exc:
            logger.error("[%s] 编排器顶层异常: %s", task_id, exc, exc_info=True)
            task = await self._db.get_task(task_id)
            if str((task or {}).get("account_commit_state") or "none") in {"pending", "committed"}:
                await self._mark_attention(
                    task_id,
                    f"账号提交后发生顶层异常，未执行账号删除: {exc}",
                    step="注册后需核对",
                )
            else:
                await self._db.update_task(
                    task_id,
                    status="failed",
                    error=str(exc),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
        finally:
            self._tasks.pop(task_id, None)
