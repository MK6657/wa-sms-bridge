"""编排器核心逻辑测试 — 验证 Saga 补偿的正确性。"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.providers.base import (
    SmsCancelledError, SmsExpiredError, SmsTimeoutError,
    NoNumbersError, BalanceError, Activation, SmsProviderError,
)
from app.models import PhoneInput
from app.orchestrator import RegisterOrchestrator
from tests.conftest import make_activation, make_probe_result, make_register_result


async def run_and_await(orch, task_id, country="0", country_fallback=None, **kw):
    result = await orch.start_registration(
        task_id=task_id, country=country,
        country_fallback=country_fallback or [], **kw)
    assert result["status"] == "running"
    task = orch._tasks.get(task_id)
    if task:
        await task
    return await orch.get_task(task_id)


def make_stateful_task_db(mock_db, task_id: str):
    state = {
        "task_id": task_id,
        "status": "running",
        "provider": "smsbower",
        "country_id": "0",
        "country_label": "Russia",
        "activation_id": "",
        "phone_number": "",
        "wa_account_id": "",
        "verification_request_id": "",
        "account_commit_state": "none",
        "successes_done": 0,
        "target_success_count": 1,
        "step": "init",
        "error": "",
    }

    async def create_task(*args, **kwargs):
        state["target_success_count"] = kwargs.get("target_success_count", 1)
        return dict(state)

    async def update_task(_task_id, **kwargs):
        assert _task_id == task_id
        state.update(kwargs)

    async def get_task(_task_id):
        return dict(state) if _task_id == task_id else None

    async def record_success(**kwargs):
        state.update({
            "successes_done": kwargs["success_count"],
            "target_success_count": kwargs["target_success_count"],
            "account_commit_state": "committed",
            "wa_account_id": kwargs["wa_account_id"],
            "verification_request_id": kwargs["verification_request_id"],
        })

    mock_db.create_task.side_effect = create_task
    mock_db.update_task.side_effect = update_task
    mock_db.get_task.side_effect = get_task
    mock_db.record_registration_success.side_effect = record_success
    return state


@pytest.fixture
async def orch(mock_smsbower, mock_waapi, mock_db):
    from app.providers.registry import ProviderRegistry
    reg = ProviderRegistry()
    reg.init_provider("smsbower", mock_smsbower)
    o = RegisterOrchestrator(reg, mock_waapi, mock_db)
    yield o
    for t in list(o._tasks.values()):
        t.cancel()
    o._tasks.clear()


class TestActivationCompensation:
    """确保所有出口 activation 都被 cancel 或 complete。"""

    @pytest.mark.asyncio
    async def test_cleanup_refusal_for_active_account_is_not_marked_cleaned(
        self, orch, mock_waapi
    ):
        mock_waapi.cleanup_failed_account.return_value = {
            "success": True,
            "deleted": False,
            "reason": "not_pending_registration",
            "status": "WA_ACCOUNT_STATUS_ACTIVE",
        }

        cleaned, reason = await orch._safe_cleanup_account(
            "waacc_active",
            verification_request_id="vreq_active",
            task_id="t_active",
            reason="test",
        )

        assert cleaned is False
        assert reason == "not_pending_registration"

    async def _assert_cancel(self, orch, ms, **overrides):
        ms.request_number.return_value = make_activation(**overrides)
        for k, v in overrides.items():
            setattr(ms, k, v)
        await run_and_await(orch, "t1")
        ms._safe_cancel.assert_awaited()
        ms.complete_activation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blacklist_cancels(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.return_value = make_activation()
        mock_db.is_blacklisted.return_value = True
        await run_and_await(orch, "t1")
        mock_smsbower._safe_cancel.assert_awaited()

    @pytest.mark.asyncio
    async def test_probe_fail_cancels(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.return_value = make_activation()
        mock_waapi.probe_phone.return_value = make_probe_result(success=False, passed=False)
        await run_and_await(orch, "t2")
        mock_smsbower._safe_cancel.assert_awaited()

    @pytest.mark.asyncio
    async def test_bad_phone_payload_cancels_without_calling_wa(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.return_value = make_activation(
            activation_id="act_bad",
            phone_number="276523227",
            country_id=31,
            country_label="South Africa",
        )
        mock_smsbower.normalize_phone.return_value = PhoneInput(
            phone="6523227",
            e164_number="276523227",
            country_calling_code="",
            country_iso2="",
        )

        await run_and_await(orch, "t_bad_phone", country="31", max_attempts=1)

        mock_smsbower._safe_cancel.assert_awaited_with("act_bad")
        mock_waapi.probe_phone.assert_not_awaited()
        mock_waapi.register.assert_not_awaited()
        assert any(
            call.kwargs.get("step") == "号码归一化失败"
            for call in mock_db.update_task.await_args_list
        )

    @pytest.mark.asyncio
    async def test_register_fail_cancels(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.return_value = make_activation()
        mock_waapi.register.return_value = make_register_result(success=False, wa_account_id="")
        await run_and_await(orch, "t3")
        mock_smsbower._safe_cancel.assert_awaited()

    @pytest.mark.asyncio
    async def test_sms_timeout_cancels(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.return_value = make_activation()
        mock_smsbower.poll_sms_code.side_effect = SmsTimeoutError("timeout")
        await run_and_await(orch, "t4")
        mock_smsbower._safe_cancel.assert_awaited()
        mock_waapi.cleanup_failed_account.assert_awaited()

    @pytest.mark.asyncio
    async def test_sms_cancelled_cancels(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.return_value = make_activation()
        mock_smsbower.poll_sms_code.side_effect = SmsCancelledError("cancelled")
        await run_and_await(orch, "t5")
        mock_smsbower._safe_cancel.assert_awaited()

    @pytest.mark.asyncio
    async def test_sms_expired_cancels(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.return_value = make_activation()
        mock_smsbower.poll_sms_code.side_effect = SmsExpiredError("expired")
        await run_and_await(orch, "t6")
        mock_smsbower._safe_cancel.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_numbers_skips(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.side_effect = NoNumbersError("none")
        await run_and_await(orch, "t7")
        mock_smsbower._safe_cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_balance_error_skips(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.side_effect = BalanceError("no balance")
        await run_and_await(orch, "t8")
        mock_smsbower._safe_cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_full_success_completes(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.return_value = make_activation()
        mock_waapi.register.return_value = make_register_result()
        mock_waapi.submit_otp.return_value = make_register_result()
        mock_waapi.check_login_state.return_value = True
        mock_waapi.list_accounts.return_value = [{"wa_account_id": "wa_acc_111"}]
        mock_db.get_task.return_value = {"task_id": "t9", "status": "completed"}
        result = await run_and_await(orch, "t9")
        assert result["status"] == "completed"
        mock_smsbower.complete_activation.assert_awaited()
        mock_smsbower.cancel_activation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_security_code_is_reported_as_paid_security_boundary(
        self, orch, mock_smsbower, mock_waapi, mock_db
    ):
        state = make_stateful_task_db(mock_db, "t_security_code")
        mock_smsbower.request_number.return_value = make_activation()
        mock_waapi.register.return_value = make_register_result()
        mock_waapi.submit_otp.return_value = make_register_result(
            success=False,
            registered=False,
            login_active=False,
            status="REGISTRATION_STATUS_REJECTED",
            error_code="WA_ERROR_CODE_REJECTED",
            failure_reason="security_code",
        )

        await run_and_await(orch, "t_security_code", max_attempts=1)

        assert state["status"] == "failed"
        assert state["step"] == "耗尽"
        assert state["failure_category"] == "security_verification_required"
        assert state["failure_code"] == "security_code"
        assert "二步验证" in state["error"]

    @pytest.mark.asyncio
    async def test_reconciliation_does_not_flag_cleaned_pending_account_as_attention(
        self, orch, mock_db
    ):
        state = make_stateful_task_db(mock_db, "t_reconciled_security")
        state.update({
            "status": "compensation_pending",
            "account_commit_state": "pending",
            "account_compensated": 1,
            "activation_compensated": 0,
            "error": "二步验证/安全确认拦截：补偿尚未完成",
            "failure_category": "security_verification_required",
            "failure_code": "security_code",
        })
        outcome = MagicMock(
            activation_settled=True,
            account_cleaned=True,
            complete=True,
            activation_result="ACCESS_ACTIVATION",
            account_result="already_cleaned",
        )

        await orch._finish_reconciled_task(state, outcome, source="后台补偿")

        assert state["status"] == "failed"
        assert state["reconciliation_required"] == 0
        assert "二步验证" in state["error"]

    @pytest.mark.asyncio
    async def test_proxy_is_passed_only_to_wa_adapter(self, orch, mock_smsbower, mock_waapi):
        mock_smsbower.request_number.return_value = make_activation()
        await run_and_await(orch, "t10", proxy="socks5://127.0.0.1:1080")

        assert mock_waapi.probe_phone.await_args.kwargs["proxy_url"] == "socks5://127.0.0.1:1080"
        assert mock_waapi.register.await_args.kwargs["proxy_url"] == "socks5://127.0.0.1:1080"
        assert mock_waapi.submit_otp.await_args.kwargs["verification_request_id"] == "vreq_999"

    @pytest.mark.asyncio
    async def test_non_whatsapp_service_is_rejected_before_buying(self, orch, mock_smsbower):
        result = await orch.start_registration(
            task_id="t11",
            country="0",
            country_fallback=[],
            service="telegram",
        )

        assert result["status"] == "failed"
        assert "仅支持 WhatsApp" in result["error"]
        mock_smsbower.request_number.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_target_success_count_runs_until_goal(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_smsbower.request_number.side_effect = [
            make_activation(activation_id="act_1", phone_number="79001230001"),
            make_activation(activation_id="act_2", phone_number="79001230002"),
        ]
        mock_waapi.register.return_value = make_register_result()
        mock_waapi.submit_otp.return_value = make_register_result()
        mock_waapi.check_login_state.return_value = True
        mock_waapi.list_accounts.return_value = [{"wa_account_id": "wa_acc_111"}]
        mock_db.get_task.return_value = {"task_id": "t12", "status": "completed"}

        await run_and_await(orch, "t12", target_success_count=2, max_attempts=5)

        assert mock_smsbower.request_number.await_count == 2
        assert mock_smsbower.complete_activation.await_count == 2
        assert mock_db.record_registration_success.await_count == 2

    @pytest.mark.asyncio
    async def test_locked_price_checks_prices_and_uses_target_max(self, orch, mock_smsbower):
        mock_smsbower.get_prices.return_value = [{"cost": 0.1, "count": 2}]
        mock_smsbower.request_number.return_value = make_activation()

        await run_and_await(orch, "t13", target_price=0.1, price_lock_enabled=True)

        mock_smsbower.get_prices.assert_awaited()
        assert mock_smsbower.request_number.await_args.args[1] == "wa"
        assert mock_smsbower.request_number.await_args.args[2] == 0.1

    @pytest.mark.asyncio
    async def test_recover_stale_tasks_compensates_external_handles(self, orch, mock_smsbower, mock_waapi, mock_db):
        mock_db.get_pending_tasks.return_value = [{
            "task_id": "stale_1",
            "status": "running",
            "provider": "smsbower",
            "activation_id": "act_stale",
            "wa_account_id": "wa_stale",
            "verification_request_id": "vreq_stale",
        }]

        count = await orch.recover_stale_tasks()

        assert count == 1
        mock_smsbower._safe_cancel.assert_awaited_with("act_stale")
        mock_waapi.cleanup_failed_account.assert_awaited_with(
            "wa_stale",
            verification_request_id="vreq_stale",
        )
        assert any(call.kwargs.get("status") == "cancelled" for call in mock_db.update_task.await_args_list)

    @pytest.mark.asyncio
    async def test_compensation_retries_then_registration_can_finish(
        self, orch, mock_smsbower, mock_waapi, mock_db
    ):
        state = make_stateful_task_db(mock_db, "t_comp_retry")
        orch._compensation_retry_delays = (0,)
        mock_waapi.register.return_value = make_register_result(
            success=False,
            wa_account_id="",
            registered=False,
        )
        mock_smsbower._safe_cancel.side_effect = ["", "ACCESS_CANCEL"]

        result = await run_and_await(orch, "t_comp_retry", max_attempts=1)

        assert mock_smsbower._safe_cancel.await_count == 2
        assert result["status"] == "failed"
        assert state["activation_id"] == ""
        assert state.get("reconciliation_required", 0) == 0

    @pytest.mark.asyncio
    async def test_background_reconciliation_settles_orphaned_activation(
        self, orch, mock_smsbower, mock_db
    ):
        state = make_stateful_task_db(mock_db, "t_orphan")
        state.update({
            "status": "compensation_pending",
            "activation_id": "act_orphan",
            "phone_number": "79001234567",
            "error": "WA 注册请求失败：补偿尚未完成",
            "reconciliation_required": 1,
            "activation_compensated": 0,
            "account_compensated": 1,
        })
        mock_db.get_pending_tasks.return_value = [dict(state)]

        reconciled = await orch.reconcile_pending_tasks()

        assert reconciled == 1
        mock_smsbower._safe_cancel.assert_awaited_once_with("act_orphan")
        assert state["status"] == "failed"
        assert state["step"] == "补偿完成，任务结束"
        assert state["error"] == "WA 注册请求失败"
        assert state["activation_id"] == ""
        assert state["reconciliation_required"] == 0

    @pytest.mark.asyncio
    async def test_provider_error_before_purchase_does_not_consume_attempt(
        self, orch, mock_smsbower, mock_db
    ):
        state = make_stateful_task_db(mock_db, "t_provider_retry")
        orch._provider_error_retry_delay = 0
        mock_smsbower.get_prices.side_effect = [
            SmsProviderError("temporary network failure"),
            [{"cost": 0.1, "count": 1}],
        ]

        result = await run_and_await(
            orch,
            "t_provider_retry",
            max_attempts=1,
            target_price=0.1,
            price_lock_enabled=True,
        )

        assert mock_smsbower.get_prices.await_count == 2
        mock_smsbower.request_number.assert_awaited_once()
        assert result["status"] == "completed"
        assert state["attempts_used"] == 1

    @pytest.mark.asyncio
    async def test_cancel_between_successes_does_not_cleanup_committed_account(
        self, orch, mock_smsbower, mock_waapi, mock_db
    ):
        state = make_stateful_task_db(mock_db, "t_multi_cancel")
        mock_smsbower.request_number.side_effect = [
            make_activation(activation_id="act_1", phone_number="79001230001"),
            make_activation(activation_id="act_2", phone_number="79001230002"),
        ]

        result = await orch.start_registration(
            task_id="t_multi_cancel",
            country="0",
            country_fallback=[],
            target_success_count=2,
            max_attempts=5,
            request_interval_enabled=True,
            request_interval=3600,
        )
        assert result["status"] == "running"

        for _ in range(100):
            if state["step"] == "完成 1/2，继续":
                break
            await asyncio.sleep(0.01)
        assert state["step"] == "完成 1/2，继续"
        assert state["activation_id"] == ""
        assert state["wa_account_id"] == ""

        stopped = await orch.stop_task("t_multi_cancel")

        assert stopped["status"] == "cancelled"
        mock_waapi.cleanup_failed_account.assert_not_awaited()
        mock_smsbower._safe_cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_ledger_failure_never_cleans_registered_account(
        self, orch, mock_smsbower, mock_waapi, mock_db
    ):
        state = make_stateful_task_db(mock_db, "t_ledger_fail")
        mock_db.record_registration_success.side_effect = RuntimeError("disk unavailable")

        await run_and_await(orch, "t_ledger_fail", max_attempts=1)

        assert state["status"] == "attention"
        assert state["account_commit_state"] == "committed"
        mock_waapi.cleanup_failed_account.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_login_state_protects_registered_account(
        self, orch, mock_smsbower, mock_waapi, mock_db
    ):
        state = make_stateful_task_db(mock_db, "t_login_unknown")
        mock_waapi.submit_otp.return_value = make_register_result(login_active=False)
        mock_waapi.check_login_state.side_effect = RuntimeError("upstream timeout")
        mock_waapi.list_accounts.side_effect = RuntimeError("list unavailable")

        await run_and_await(orch, "t_login_unknown", max_attempts=1)

        assert state["status"] == "attention"
        assert state["account_commit_state"] == "committed"
        mock_waapi.cleanup_failed_account.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancellation_compensates_current_attempt_only_once(
        self, orch, mock_smsbower, mock_waapi, mock_db
    ):
        state = make_stateful_task_db(mock_db, "t_cancel_once")
        waiting = asyncio.Event()

        async def wait_for_sms(*args, **kwargs):
            waiting.set()
            await asyncio.Future()

        mock_smsbower.poll_sms_code.side_effect = wait_for_sms
        await orch.start_registration(
            task_id="t_cancel_once",
            country="0",
            country_fallback=[],
            max_attempts=1,
        )
        await asyncio.wait_for(waiting.wait(), timeout=1)

        stopped = await orch.stop_task("t_cancel_once")

        assert stopped["status"] == "cancelled"
        assert state["activation_id"] == ""
        mock_smsbower._safe_cancel.assert_awaited_once()
        mock_waapi.cleanup_failed_account.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fallback_countries_are_deduplicated_after_resolution(
        self, orch, mock_smsbower
    ):
        original_run = orch._run_registration
        orch._run_registration = AsyncMock()
        mock_smsbower.resolve_country.side_effect = [
            (31, "South Africa"),
            (31, "South Africa"),
            (52, "Thailand"),
        ]
        try:
            result = await orch.start_registration(
                task_id="t_fallback_dedupe",
                country="South Africa",
                country_fallback=["31", "Thailand"],
            )
            assert result["status"] == "running"
            await asyncio.sleep(0)
            candidates = orch._run_registration.await_args.args[1]
            assert candidates == [(31, "South Africa"), (52, "Thailand")]
        finally:
            orch._run_registration = original_run
