"""项目 API Key 鉴权、管理员会话、多项目隔离与持久化 Webhook Outbox 测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.webhook_dispatcher import WebhookDispatcher, compute_webhook_signature
from app.db import Database
from app.main import create_app
from app.models import PhoneInput
from app.providers.base import Activation, CancelOutcome, ProviderCapabilities
from app.providers.registry import ProviderRegistry


def _make_prov():
    prov = MagicMock()
    prov.provider_id = "smsbower"
    prov.capabilities = ProviderCapabilities(supports_resend=True)
    prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
    prov.request_number = AsyncMock(
        return_value=Activation("act_auth_1", "66800001111", 52, "Thailand", provider="smsbower", price=0.11)
    )
    prov.normalize_phone = AsyncMock(
        return_value=PhoneInput(region="TH", phone="800001111", e164_number="+66800001111", country_calling_code="66", country_iso2="th")
    )
    prov.get_sms_code = AsyncMock(return_value=("WAIT", ""))
    prov.cancel_activation_structured = AsyncMock(
        return_value=CancelOutcome(settled=True, status="refund_confirmed", raw="ACCESS_CANCEL", refund_confirmed=True)
    )
    prov.close = AsyncMock()
    return prov


@pytest.mark.asyncio
async def test_project_api_key_auth_and_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "test-auth-secret")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    from app.config import env
    env.reload()

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_prov())
    application = create_app(
        db_path=str(tmp_path / "auth.db"),
        enable_wa_plugin=False,
        registry_instance=reg,
        skip_startup_probe=True,
    )

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as ext_client:
            # 管理员通过本机回环签发 HttpOnly Cookie 会话
            boot = await admin_client.post("/api/v1/auth/session")
            assert boot.status_code == 200

            # 1. 创建两个项目密钥
            k1 = (await admin_client.post("/api/v1/projects/keys", json={"name": "TG Bot", "project_id": "tg_bot"})).json()
            k2 = (await admin_client.post("/api/v1/projects/keys", json={"name": "Google Tool", "project_id": "google_tool"})).json()
            assert k1["api_key"].startswith("sms_k_")

            # 2. 开启严格鉴权模式
            res = await admin_client.put("/api/v1/settings", json={"api_auth_required": True})
            assert res.status_code == 200

            # 即使伪造旧标头 X-SMS-Hub-Client + Origin，外部客户端在严格模式下依然 401（P0-01）
            spoofed = await ext_client.get(
                "/api/v1/orders",
                headers={"X-SMS-Hub-Client": "web-console", "Origin": "http://127.0.0.1:9099"},
            )
            assert spoofed.status_code == 401
            assert spoofed.json()["error"]["code"] == "AUTH_REQUIRED"

            # 无效 Key 被 401 拒绝
            bad = await ext_client.get("/api/v1/orders", headers={"X-API-Key": "sms_k_invalid"})
            assert bad.status_code == 401

            # 3. 项目 1 创建订单，项目 2 不可见（数据隔离）
            created = await ext_client.post(
                "/api/v1/orders",
                headers={"X-API-Key": k1["api_key"]},
                json={"service": "telegram", "country": "Thailand"},
            )
            assert created.status_code == 201
            ord_id = created.json()["order_id"]
            assert created.json()["project_id"] == "tg_bot"

            list_p2 = (await ext_client.get("/api/v1/orders", headers={"X-API-Key": k2["api_key"]})).json()
            assert list_p2["total"] == 0
            get_p2 = await ext_client.get(f"/api/v1/orders/{ord_id}", headers={"X-API-Key": k2["api_key"]})
            assert get_p2.status_code == 404

            # 4. 管理员使用合法签名 Cookie 停用项目 1 密钥，立即生效
            patch_resp = await admin_client.patch("/api/v1/projects/keys/tg_bot", json={"enabled": False})
            assert patch_resp.status_code == 200
            disabled_resp = await ext_client.get("/api/v1/orders", headers={"X-API-Key": k1["api_key"]})
            assert disabled_resp.status_code == 401
            assert disabled_resp.json()["error"]["code"] == "AUTH_KEY_DISABLED"


@pytest.mark.asyncio
async def test_webhook_outbox_hmac_signature_and_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "test-wh-secret")
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "hooks.partner.internal")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "wh.db"))
    await db.open()
    dispatcher = WebhookDispatcher(db, retry_delays=(0.01, 0.01), poll_interval=0.05)
    try:
        # 注册项目与加密的 Webhook 签名密钥
        await db.api_keys.create(
            project_id="proj_wh",
            name="WH Project",
            key_prefix="sms_k_test",
            key_hash="hash_wh_test",
            scopes=["orders:rw"],
            default_webhook_url="https://hooks.partner.internal/cb",
            webhook_secret="my-hmac-secret",
        )
        await db.webhooks.enqueue(
            delivery_id="dlv_stable_001",
            event_id="evt_999",
            event_type="order.code_received",
            order_id="ord_1",
            project_id="proj_wh",
            target_url="https://hooks.partner.internal/cb",
            payload={"event": "order.code_received", "code": "654321"},
            max_attempts=3,
        )

        captured = []

        async def fake_post(url, *, content, headers):
            captured.append((url, content, dict(headers)))
            resp = MagicMock()
            if len(captured) == 1:
                resp.status_code = 503
                resp.text = "busy"
            else:
                resp.status_code = 200
                resp.text = "ok"
            return resp

        dispatcher._client.post = AsyncMock(side_effect=fake_post)

        # 第 1 次失败进入 pending + 退避
        assert await dispatcher.process_due_once() == 1
        await dispatcher.drain_inflight()
        rows_after_1 = await db.webhooks.list(project_id="proj_wh")
        assert rows_after_1[0]["status"] == "pending"
        assert rows_after_1[0]["attempts"] == 1

        # 等待极短退避后第 2 次重试成功，且保持同一个稳定 X-SMS-Delivery
        import asyncio
        await asyncio.sleep(0.03)
        assert await dispatcher.process_due_once() == 1
        await dispatcher.drain_inflight()

        rows_after_2 = await db.webhooks.list(project_id="proj_wh")
        assert rows_after_2[0]["status"] == "delivered"
        assert rows_after_2[0]["attempts"] == 2

        _url, body, headers = captured[1]
        assert headers["X-SMS-Event"] == "order.code_received"
        assert headers["X-SMS-Delivery"] == "dlv_stable_001"
        assert headers["X-SMS-Signature"] == compute_webhook_signature("my-hmac-secret", body)
    finally:
        await dispatcher.shutdown()
        await db.close()
