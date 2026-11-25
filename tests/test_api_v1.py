"""API v1 端到端集成测试（独立模式，零 wa-app 依赖）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.main import create_app
from app.models import PhoneInput
from app.providers.base import Activation, CancelOutcome, ProviderCapabilities
from app.providers.registry import ProviderRegistry


def _mock_smsbower():
    prov = MagicMock()
    prov.provider_id = "smsbower"
    prov.capabilities = ProviderCapabilities(supports_resend=True, supports_price_lock=True, supports_custom_service=True)
    prov.get_balance = AsyncMock(return_value=12.5)
    prov.get_countries = AsyncMock(return_value=[
        {"id": 52, "name": "Thailand", "name_zh": "泰国", "cc": "66", "iso": "th", "aliases": ["th", "52"]},
        {"id": 6, "name": "Indonesia", "name_zh": "印度尼西亚", "cc": "62", "iso": "id", "aliases": ["id", "6"]},
    ])
    prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
    prov.get_prices = AsyncMock(return_value=[{"cost": 0.1, "count": 20}, {"cost": 0.15, "count": 5}])
    prov.request_number = AsyncMock(
        return_value=Activation("act_api_1", "66899990000", 52, "Thailand", provider="smsbower", price=0.1)
    )
    prov.normalize_phone = AsyncMock(
        return_value=PhoneInput(region="TH", phone="899990000", e164_number="+66899990000", country_calling_code="66", country_iso2="th")
    )
    prov.get_sms_code = AsyncMock(return_value=("OK", "735201"))
    prov.complete_activation = AsyncMock(return_value="ACCESS_ACTIVATION")
    prov.cancel_activation_structured = AsyncMock(
        return_value=CancelOutcome(settled=True, status="refund_confirmed", retryable=False, raw="ACCESS_CANCEL", refund_confirmed=True)
    )
    prov.close = AsyncMock()
    return prov


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "test-secret-key-for-v1")
    from app.config import env
    env.reload()

    prov = _mock_smsbower()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)

    application = create_app(
        db_path=str(tmp_path / "api_v1.db"),
        enable_wa_plugin=False,
        registry_instance=reg,
        skip_startup_probe=True,
    )
    async with application.router.lifespan_context(application):
        # 加快测试中轮询间隔
        application.state.container.order_service._poll_intervals = (0.02, 0.02, 0.02)
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, prov, application


@pytest.mark.asyncio
async def test_health_is_ok_without_wa_app(api_client):
    client, _prov, _app = api_client
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["active_providers"] == ["smsbower"]
    assert data["plugins"]["whatsapp_wa"]["enabled"] is False

    # 旧路径 /api/health 仍可用
    legacy = await client.get("/api/health")
    assert legacy.status_code == 200
    assert legacy.json()["ok"] is True


@pytest.mark.asyncio
async def test_providers_catalog_and_disabled_legacy_rejection(api_client):
    client, _prov, _app = api_client
    resp = await client.get("/api/v1/providers")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] == ["smsbower"]
    catalog_by_id = {item["id"]: item for item in data["catalog"]}
    assert catalog_by_id["smsbower"]["active"] is True
    assert catalog_by_id["5sim"]["active"] is False
    assert catalog_by_id["herosms"]["active"] is False

    # 未启用的 5sim 返回规范结构化错误
    rejected = await client.get("/api/v1/providers/5sim/balance")
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "PROVIDER_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_full_order_lifecycle_over_http(api_client):
    client, _prov, _app = api_client
    create_resp = await client.post(
        "/api/v1/orders",
        headers={"Idempotency-Key": "http-idem-1"},
        json={"service": "whatsapp", "country": "Thailand", "wait_timeout": 120},
    )
    assert create_resp.status_code == 201
    order = create_resp.json()
    order_id = order["order_id"]
    assert order["phone_e164"] == "+66899990000"

    # 通过 /wait 拿到验证码
    wait_resp = await client.get(f"/api/v1/orders/{order_id}/wait?timeout=5")
    assert wait_resp.status_code == 200
    waited = wait_resp.json()
    assert waited["status"] == "code_received"
    assert waited["latest_code"] == "735201"

    # 完成订单
    comp_resp = await client.post(f"/api/v1/orders/{order_id}/complete")
    assert comp_resp.status_code == 200
    assert comp_resp.json()["status"] == "completed"

    # 统计接口与列表接口反映该笔订单
    stats = (await client.get("/api/v1/stats")).json()
    assert stats["today"]["received_count"] == 1
    assert stats["today"]["completed_count"] == 1

    # 单条脱敏清理
    scrub = await client.delete(f"/api/v1/orders/{order_id}/code")
    assert scrub.status_code == 200
    detail = (await client.get(f"/api/v1/orders/{order_id}")).json()
    assert detail["code_scrubbed"] is True
    assert detail["latest_code"] == ""


@pytest.mark.asyncio
async def test_spa_frontend_static_served(api_client):
    client, _prov, _app = api_client
    root = await client.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers.get("content-type", "")
    assert "SMS Hub" in root.text
