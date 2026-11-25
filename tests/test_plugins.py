"""可选插件隔离机制测试（whatsapp_wa 默认关闭、按需开启）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.main import create_app
from app.providers.registry import ProviderRegistry


def _dummy_provider():
    prov = MagicMock()
    prov.provider_id = "smsbower"
    prov.close = AsyncMock()
    return prov


@pytest.mark.asyncio
async def test_wa_plugin_disabled_by_default(tmp_path):
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _dummy_provider())

    application = create_app(
        db_path=str(tmp_path / "plugin_off.db"),
        enable_wa_plugin=False,
        registry_instance=reg,
        skip_startup_probe=True,
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            health = (await client.get("/api/v1/health")).json()
            assert health["ok"] is True
            assert health["plugins"]["whatsapp_wa"]["enabled"] is False

            tasks = await client.get("/api/v1/plugins/whatsapp_wa/tasks")
            assert tasks.status_code == 503


@pytest.mark.asyncio
async def test_wa_plugin_can_be_enabled_explicitly(tmp_path, monkeypatch):
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _dummy_provider())

    fake_wa = MagicMock()
    fake_wa._base_url = "http://wa-app:8080"
    fake_wa.configure_auth = MagicMock()
    fake_wa.health = AsyncMock(return_value={"ok": True, "version": "test"})
    fake_wa.list_accounts = AsyncMock(return_value=[{"wa_account_id": "acc_1"}])
    fake_wa.close = AsyncMock()

    monkeypatch.setattr("app.plugins.whatsapp_wa.plugin.create_wa_adapter", lambda _n, _u: fake_wa)

    application = create_app(
        db_path=str(tmp_path / "plugin_on.db"),
        enable_wa_plugin=True,
        registry_instance=reg,
        skip_startup_probe=True,
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            health = (await client.get("/api/v1/health")).json()
            assert health["ok"] is True
            assert health["plugins"]["whatsapp_wa"]["enabled"] is True
            assert health["plugins"]["whatsapp_wa"]["ok"] is True

            accounts = (await client.get("/api/v1/plugins/whatsapp_wa/accounts")).json()
            assert accounts["accounts"] == [{"wa_account_id": "acc_1"}]
