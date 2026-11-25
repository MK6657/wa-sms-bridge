"""验证码加密落盘、自动脱敏留存、日志脱敏以及缓存/Singleflight 测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.pricing_service import PricingService
from app.db import Database
from app.log_redaction import redact_text
from app.providers.registry import ProviderRegistry
from app.providers.smsbower import SmsBowerProvider


@pytest.mark.asyncio
async def test_sms_code_encrypted_at_rest_and_scrubbed_by_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "retention-test-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "retention.db"))
    await db.open()
    try:
        await db.orders.create(
            order_id="ord_old",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            country_label="Thailand",
            status="waiting_code",
        )
        await db.orders.append_code("ord_old", "519283", "Your code is 519283")

        # 验证数据库原始列为 Fernet 密文（以 e_ 开头，不含明文）
        raw_row = await db.conn.fetchone("SELECT latest_code FROM sms_orders WHERE order_id='ord_old'")
        assert raw_row is not None
        assert raw_row["latest_code"].startswith("e_")
        assert "519283" not in raw_row["latest_code"]

        # 业务读取自动透明解密
        order = await db.orders.get("ord_old")
        assert order is not None
        assert order["latest_code"] == "519283"
        assert order["messages"][0]["code"] == "519283"

        # 标记为 48 小时前已完成，执行 24 小时留存清理
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        await db.orders.update("ord_old", status="completed", completed_at=old_time)

        scrubbed_count = await db.orders.scrub_expired_codes(retention_hours=24)
        assert scrubbed_count == 1

        after = await db.orders.get("ord_old")
        assert after is not None
        assert after["code_scrubbed"] is True
        assert after["latest_code"] == ""
        assert after["messages"][0]["code"] == ""
    finally:
        await db.close()


def test_log_redaction_masks_sms_codes_and_project_keys():
    raw = (
        "X-API-Key: sms_k_AbCdEfGhIjKlMnOpQrStUv "
        "STATUS_OK:482910 sms_code=482910 "
        '{"latest_code":"991122","webhook_secret":"topsecret"}'
    )
    redacted = redact_text(raw)
    assert "AbCdEfGhIjKlMnOpQrStUv" not in redacted
    assert "482910" not in redacted
    assert "991122" not in redacted
    assert "topsecret" not in redacted


@pytest.mark.asyncio
async def test_smsbower_countries_cache_and_invalidation():
    async def get_key():
        return "fake_key"

    prov = SmsBowerProvider(get_key, country_cache_ttl=600.0)
    payload = json.dumps({"52": {"eng": "Thailand", "chn": "泰国"}})
    prov._get = AsyncMock(return_value=payload)

    first = await prov.get_countries()
    second = await prov.get_countries()
    assert len(first) == 1
    assert len(second) == 1
    assert prov._get.await_count == 1  # 命中缓存

    prov.invalidate_countries_cache()
    await prov.get_countries()
    assert prov._get.await_count == 2  # 清除缓存后重新拉取
    await prov.close()


@pytest.mark.asyncio
async def test_pricing_service_singleflight_coalesces_concurrent_requests():
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_get_prices(_cid, _svc):
        started.set()
        await release.wait()
        return [{"cost": 0.1, "count": 8}]

    prov = MagicMock()
    prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
    prov.get_prices = AsyncMock(side_effect=slow_get_prices)

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    pricing = PricingService(reg, cache_ttl=10.0)

    t1 = asyncio.create_task(pricing.get_price_snapshot("smsbower", "Thailand", "whatsapp"))
    await asyncio.wait_for(started.wait(), timeout=1)
    t2 = asyncio.create_task(pricing.get_price_snapshot("smsbower", "Thailand", "whatsapp"))
    await asyncio.sleep(0)
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)

    assert prov.get_prices.await_count == 1  # 并发合单为 1 次上游调用
    assert r1["low"] == 0.1 and r2["low"] == 0.1

    # 10 秒内再次查询命中缓存
    r3 = await pricing.get_price_snapshot("smsbower", "Thailand", "whatsapp")
    assert r3["cached"] is True
    assert prov.get_prices.await_count == 1
