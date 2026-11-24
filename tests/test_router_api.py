from unittest.mock import AsyncMock, MagicMock

import pytest

from app.providers.base import Activation
from app.models import RegisterRequest
from fastapi import HTTPException

from app.router_api import _summarize_prices, api_health, api_register, api_test_number, router


@pytest.mark.asyncio
async def test_test_number_resolves_country_and_service(monkeypatch):
    provider = MagicMock()
    provider.provider_id = "smsbower"
    provider.resolve_country = AsyncMock(return_value=(31, "South Africa"))
    provider.request_number = AsyncMock(return_value=Activation("act_123456", "27630445764", 31, "South Africa"))
    provider._safe_cancel = AsyncMock()

    registry = MagicMock()
    registry.get.return_value = provider
    monkeypatch.setattr(router, "_registry", registry, raising=False)

    result = await api_test_number({
        "provider": "smsbower",
        "country": "South Africa",
        "service": "telegram",
        "price_max": 0.3,
    })

    assert result["success"] is True
    provider.resolve_country.assert_awaited_once_with("South Africa")
    provider.request_number.assert_awaited_once_with(31, "tg", price_max=0.3)
    provider._safe_cancel.assert_awaited_once_with("act_123456")


@pytest.mark.asyncio
async def test_test_number_checks_locked_price_before_buy(monkeypatch):
    provider = MagicMock()
    provider.provider_id = "smsbower"
    provider.resolve_country = AsyncMock(return_value=(31, "South Africa"))
    provider.get_prices = AsyncMock(return_value=[{"cost": 0.11, "count": 0}])
    provider.request_number = AsyncMock()
    provider._safe_cancel = AsyncMock()

    registry = MagicMock()
    registry.get.return_value = provider
    monkeypatch.setattr(router, "_registry", registry, raising=False)

    result = await api_test_number({
        "provider": "smsbower",
        "country": "South Africa",
        "service": "whatsapp",
        "target_price": 0.11,
        "price_lock_enabled": True,
    })

    assert result["success"] is False
    provider.request_number.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_register_passes_strategy_fields(monkeypatch):
    orch = MagicMock()
    orch.start_registration = AsyncMock(return_value={"task_id": "task_x", "status": "running"})
    monkeypatch.setattr(router, "_orch", orch, raising=False)

    await api_register(RegisterRequest(
        provider="smsbower",
        service="whatsapp",
        country="南非",
        country_fallback=["泰国", "za"],
        target_price=0.1,
        price_lock_enabled=True,
        price_float_enabled=True,
        price_float_percent=10,
        continuous_poll=True,
        target_success_count=3,
        request_interval_enabled=True,
        request_interval=0.5,
    ))

    kwargs = orch.start_registration.await_args.kwargs
    assert kwargs["service"] == "whatsapp"
    assert kwargs["country"] == "南非"
    assert kwargs["country_fallback"] == ["泰国", "za"]
    assert kwargs["target_price"] == 0.1
    assert kwargs["price_lock_enabled"] is True
    assert kwargs["price_float_enabled"] is True
    assert kwargs["continuous_poll"] is True
    assert kwargs["target_success_count"] == 3
    assert kwargs["request_interval_enabled"] is True
    assert kwargs["request_interval"] == 0.5


@pytest.mark.asyncio
async def test_api_register_rejects_non_whatsapp_before_orchestrator(monkeypatch):
    orch = MagicMock()
    orch.start_registration = AsyncMock()
    monkeypatch.setattr(router, "_orch", orch, raising=False)

    with pytest.raises(HTTPException) as exc:
        await api_register(RegisterRequest(service="telegram"))

    assert exc.value.status_code == 400
    orch.start_registration.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_respects_upstream_ok_flag(monkeypatch):
    db = MagicMock()
    db.health = AsyncMock(return_value=True)
    registry = MagicMock()
    registry.list_providers.return_value = ["smsbower"]
    waapi = MagicMock()
    waapi.health = AsyncMock(return_value={"ok": False, "reason": "degraded"})
    monkeypatch.setattr(router, "_db", db, raising=False)
    monkeypatch.setattr(router, "_registry", registry, raising=False)
    monkeypatch.setattr(router, "_waapi", waapi, raising=False)

    result = await api_health()

    assert result["ok"] is False
    assert result["wa_app"]["ok"] is False


def test_price_controls_require_positive_target():
    with pytest.raises(ValueError):
        RegisterRequest(price_lock_enabled=True, target_price=0)
    with pytest.raises(ValueError):
        RegisterRequest(price_float_enabled=True, target_price=None)


def test_price_summary_ignores_zero_stock_for_recommendation_bands():
    summary = _summarize_prices([
        {"cost": 0.08, "count": 0},
        {"cost": 0.10, "count": 3},
        {"cost": 0.20, "count": 0},
        {"cost": 0.30, "count": 5},
    ])

    assert summary["low"] == 0.10
    assert summary["middle"] == 0.30
    assert summary["high"] == 0.30
    assert summary["stock"] == 8
