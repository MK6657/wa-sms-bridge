"""独立 SMS 接码订单引擎（OrderService）核心状态机测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.event_bus import EventBus
from app.core.order_service import AcquireOptions, OrderService
from app.db import Database
from app.errors import AppError
from app.models import PhoneInput
from app.providers.base import (
    Activation,
    CancelOutcome,
    NoNumbersError,
    ProviderCapabilities,
)
from app.providers.registry import ProviderRegistry


def _make_mock_provider():
    prov = MagicMock()
    prov.provider_id = "smsbower"
    prov.capabilities = ProviderCapabilities(supports_resend=True, supports_price_lock=True, supports_custom_service=True)
    prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
    prov.get_prices = AsyncMock(return_value=[{"cost": 0.12, "count": 10}])
    prov.request_number = AsyncMock(
        return_value=Activation("act_001", "66812345678", 52, "Thailand", provider="smsbower", price=0.12)
    )
    prov.normalize_phone = AsyncMock(
        return_value=PhoneInput(
            region="TH",
            phone="812345678",
            e164_number="+66812345678",
            country_calling_code="66",
            country_iso2="th",
        )
    )
    prov.get_sms_code = AsyncMock(return_value=("WAIT", ""))
    prov.request_additional_sms = AsyncMock(return_value="ACCESS_RETRY_GET")
    prov.complete_activation = AsyncMock(return_value="ACCESS_ACTIVATION")
    prov.cancel_activation_structured = AsyncMock(
        return_value=CancelOutcome(
            settled=True,
            status="refund_confirmed",
            retryable=False,
            raw="ACCESS_CANCEL",
            refund_confirmed=True,
        )
    )
    prov.close = AsyncMock()
    return prov


@pytest.fixture
async def order_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "order-service-test-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "orders.db"))
    await db.open()
    prov = _make_mock_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    bus = EventBus()
    svc = OrderService(db, reg, bus, poll_intervals=(0.02, 0.02, 0.02))
    yield db, prov, bus, svc
    await svc.shutdown()
    await db.close()


@pytest.mark.asyncio
async def test_acquire_receive_code_resend_and_complete(order_env):
    db, prov, _bus, svc = order_env
    poll_responses = iter([
        ("WAIT", ""),
        ("OK", "482910"),
    ])
    prov.get_sms_code = AsyncMock(side_effect=lambda _aid: next(poll_responses, ("WAIT", "")))

    order = await svc.create_order(country="Thailand", service="telegram", wait_timeout=60)
    assert order["status"] == "waiting_code"
    assert order["phone_e164"] == "+66812345678"
    assert order["service"] == "telegram"
    assert order["service_code"] == "tg"

    # 通过 wait_for_code 挂接 EventBus 实时拿到验证码
    received = await svc.wait_for_code(order["order_id"], timeout=5)
    assert received["status"] == "code_received"
    assert received["latest_code"] == "482910"
    assert len(received["messages"]) == 1

    # 请求重发短信（status=3），等待第二条验证码
    resend_responses = iter([
        ("WAIT", ""),
        ("OK", "991024"),
    ])
    prov.get_sms_code = AsyncMock(side_effect=lambda _aid: next(resend_responses, ("WAIT", "")))
    resent = await svc.resend_sms(order["order_id"])
    assert resent["status"] == "waiting_next_code"

    second = await svc.wait_for_code(order["order_id"], timeout=5, after_seq=1)
    assert second["status"] == "code_received"
    assert second["latest_code"] == "991024"
    assert [m["code"] for m in second["messages"]] == ["482910", "991024"]

    # 确认完成（status=6）
    completed = await svc.complete_order(order["order_id"])
    assert completed["status"] == "completed"
    prov.complete_activation.assert_awaited_once_with("act_001")


@pytest.mark.asyncio
async def test_idempotency_key_prevents_double_purchase(order_env):
    _db, prov, _bus, svc = order_env
    first = await svc.create_order(country="52", service="whatsapp", idempotency_key="idem-123")
    second = await svc.create_order(country="52", service="whatsapp", idempotency_key="idem-123")
    assert first["order_id"] == second["order_id"]
    assert prov.request_number.await_count == 1


@pytest.mark.asyncio
async def test_blacklisted_number_is_refunded_and_skipped(order_env):
    db, prov, _bus, svc = order_env
    await db.blacklist.add("+66811111111", reason="bad", provider="smsbower")

    activations = iter([
        Activation("act_bad", "66811111111", 52, "Thailand", provider="smsbower"),
        Activation("act_good", "66822222222", 52, "Thailand", provider="smsbower"),
    ])
    prov.request_number = AsyncMock(side_effect=lambda *a, **kw: next(activations))
    prov.normalize_phone = AsyncMock(
        side_effect=[
            PhoneInput(region="TH", phone="811111111", e164_number="+66811111111", country_calling_code="66", country_iso2="th"),
            PhoneInput(region="TH", phone="822222222", e164_number="+66822222222", country_calling_code="66", country_iso2="th"),
        ]
    )

    order = await svc.create_order(
        country="Thailand",
        service="whatsapp",
        strategy=AcquireOptions(max_attempts=3),
    )
    assert order["activation_id"] == "act_good"
    assert order["phone_e164"] == "+66822222222"
    prov.cancel_activation_structured.assert_awaited_once_with("act_bad")


@pytest.mark.asyncio
async def test_cancel_with_blacklist_adds_phone_and_refunds(order_env):
    db, prov, _bus, svc = order_env
    order = await svc.create_order(country="52", service="google")
    cancelled = await svc.cancel_order(order["order_id"], blacklist=True, reason="收不到码")
    assert cancelled["status"] == "cancelled"
    assert await db.blacklist.is_blacklisted("+66812345678", provider="smsbower") is True
    prov.cancel_activation_structured.assert_awaited_once_with("act_001")


@pytest.mark.asyncio
async def test_timeout_triggers_auto_refund_and_compensation_reconcile(order_env):
    db, prov, _bus, svc = order_env
    # 首次取消返回未结清（模拟临时网络故障），应进入 compensation_pending
    prov.cancel_activation_structured = AsyncMock(
        return_value=CancelOutcome(settled=False, status="early_cancel_denied", retryable=True, raw="EARLY_CANCEL_DENIED")
    )
    order = await svc.create_order(country="52", service="openai")
    # 将 expires_at 调为过去时间触发立即超时
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    await db.orders.update(order["order_id"], expires_at=past)

    await asyncio.sleep(0.1)
    after_timeout = await svc.get_order(order["order_id"])
    assert after_timeout["status"] == "compensation_pending"

    # 后台补偿重试成功
    prov.cancel_activation_structured = AsyncMock(
        return_value=CancelOutcome(
            settled=True,
            status="refund_confirmed",
            retryable=False,
            raw="ACCESS_CANCEL",
            refund_confirmed=True,
        )
    )
    reconciled = await svc.reconcile_pending()
    assert reconciled == 1
    settled = await svc.get_order(order["order_id"])
    assert settled["status"] == "cancelled"


@pytest.mark.asyncio
async def test_no_numbers_raises_structured_app_error(order_env):
    _db, prov, _bus, svc = order_env
    prov.request_number = AsyncMock(side_effect=NoNumbersError("NO_NUMBERS"))
    with pytest.raises(AppError) as exc:
        await svc.create_order(country="52", service="whatsapp")
    assert exc.value.code == "NO_NUMBERS"
    assert exc.value.status_code == 409
