"""全项目深度审查回归测试套件：覆盖上游三平台协议边界、智能验证码提取、国家/号码碰撞、状态机回滚、鉴权与日志脱敏。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import env
from app.country_index import GlobalCountryIndex
from app.core.pricing_service import PricingService
from app.log_redaction import redact_text
from app.main import RedactingFormatter, create_app
from app.phone_normalizer import build_phone_input, resolve_country_metadata, validate_phone_input
from app.providers.base import (
    Activation,
    NoNumbersError,
    ProviderCapabilities,
    SmsExpiredError,
    extract_otp_code,
    parse_complete_outcome,
    parse_resend_outcome,
)
from app.providers.five_sim import FiveSimProvider
from app.providers.hero_sms import HeroSmsProvider
from app.providers.registry import ProviderRegistry, registry as global_registry
from app.providers.smsbower import SmsBowerProvider, resolve_country_id
from app.service_codes import provider_service_code


# ── 1. 智能验证码提取与全文本 SMS 抗污染 ──────────────────────


def test_extract_otp_code_ignores_minutes_sender_phone_and_app_hash():
    # Google 英文全文本（含 10 minutes）
    assert extract_otp_code("G-482910 is your Google verification code. Valid for 10 minutes.") == "482910"
    # 中文短信（含 10分钟）
    assert extract_otp_code("【Google】您的验证码是 739104，请在10分钟内填写。") == "739104"
    # WhatsApp 含连字符与 11 位 Android SMS Retriever 签名哈希
    wa_text = "<#> Your WhatsApp code: 123-456\nYou can also tap on this link to verify: https://v.whatsapp.com/999888\n4sgLq1p5sV6"
    assert extract_otp_code(wa_text) == "123456"
    # 含发送方 E.164 号码前缀
    assert extract_otp_code("+12025550199: code 839201") == "839201"
    # 纯数字与连字符纯码
    assert extract_otp_code("908172") == "908172"
    assert extract_otp_code("451-892") == "451892"
    # Apple 多语言与 WebOTP 短信模板精准提取
    assert extract_otp_code("Your Apple Account code is: 482910. Do not share it with anyone.") == "482910"
    assert extract_otp_code("<#> Your Apple ID Code is: 839201. Don't share it.\n@apple.com #839201 %apple.com") == "839201"
    assert extract_otp_code("您的 Apple 账户代码为：739104。请勿与他人共享。") == "739104"
    assert extract_otp_code("รหัสบัญชี Apple ของคุณคือ: 651902 อย่าแชร์กับใคร") == "651902"
    # Apple 服务映射与常用置顶校验
    from app.service_codes import canonical_service, list_services
    assert canonical_service("apple") == "apple"
    assert canonical_service("苹果") == "apple"
    assert canonical_service("icloud") == "apple"
    assert provider_service_code("smsbower", "apple") == "wx"
    assert provider_service_code("herosms", "苹果") == "wx"
    assert provider_service_code("5sim", "appleid") == "apple"
    apple_entry = next(s for s in list_services("smsbower") if s["id"] == "apple")
    assert apple_entry["popular"] is True and apple_entry["code"] == "wx"
    # 无数字正文不误提取
    assert extract_otp_code("Your verification code is pending") == ""


@pytest.mark.asyncio
async def test_smsbower_wait_retry_with_last_code_and_bad_status_cancel_probe(monkeypatch):
    provider = SmsBowerProvider(AsyncMock(return_value="sb_test_key"))
    responses = iter([
        "STATUS_WAIT_RETRY:111222",
        "STATUS_OK:G-654321 is your Google code (valid 15 minutes)",
        "BAD_STATUS",
        "STATUS_OK:654321",
        "BAD_STATUS",
        "STATUS_CANCEL",
        "WRONG_MAX_PRICE:0.15",
    ])

    async def fake_get(**params):
        return next(responses)

    monkeypatch.setattr(provider, "_get", fake_get)
    try:
        st1, code1 = await provider.get_sms_code("act_1001")
        assert (st1, code1) == ("WAIT", "")

        st2, code2 = await provider.get_sms_code("act_1001")
        assert (st2, code2) == ("OK", "654321")
        assert "G-654321" in provider.get_last_raw_sms("act_1001")

        # setStatus=8 返回 BAD_STATUS 时二次探测 getStatus 区分已完成 vs 已取消
        out_completed = await provider.cancel_activation_structured("act_1001")
        assert out_completed.settled is True
        assert out_completed.refund_confirmed is False
        assert out_completed.status == "already_completed"

        out_cancelled = await provider.cancel_activation_structured("act_1001")
        assert out_cancelled.settled is True
        assert out_cancelled.refund_confirmed is True

        with pytest.raises(NoNumbersError):
            await provider.request_number(52, "go", price_max=0.01)
    finally:
        await provider.close()


def test_parse_complete_and_resend_outcomes_distinguish_sql_error_vs_cancelled():
    assert parse_complete_outcome("ERROR_SQL").retryable is True
    assert parse_resend_outcome("ERROR_SQL").retryable is True
    assert parse_complete_outcome("STATUS_CANCEL").retryable is False
    assert parse_resend_outcome("ACCESS_CANCEL").retryable is False
    assert provider_service_code("smsbower", "custom:") == "wa"


# ── 2. 国家解析与号码归一化碰撞修复 ──────────────────────────


@pytest.mark.asyncio
async def test_country_resolution_collisions_and_five_sim_plain_text_errors(monkeypatch):
    # "US" 绝不能因 "russian federation" 含子串 "us" 误命中俄罗斯 0！
    assert resolve_country_id("US")[0] == 187
    assert resolve_country_id("+1")[0] == 187

    sb = SmsBowerProvider(AsyncMock(return_value="k"))
    monkeypatch.setattr(
        sb,
        "get_countries",
        AsyncMock(return_value=[
            {"id": 55, "name": "Taiwan", "cc": "886", "iso": "tw", "aliases": ["55", "886"]},
            {"id": 73, "name": "Brazil", "cc": "55", "iso": "br", "aliases": ["73", "55"]},
        ]),
    )
    try:
        # "+55" 必须按国际区号命中巴西 73，而非台湾 ID=55
        cid, _ = await sb.resolve_country("+55")
        assert cid == 73
    finally:
        await sb.close()

    hero = HeroSmsProvider(AsyncMock(return_value="k"))
    try:
        # 俄罗斯 ID=0 不能因 Python truthiness 丢失；"31" 应优先命中南非 ID=31 而非荷兰 cc=31；"日本" 不可因 compact="" 命中阿富汗
        assert (await hero.resolve_country("ru"))[0] == 0
        assert (await hero.resolve_country("31"))[0] == 31
        with pytest.raises(ValueError):
            await hero.resolve_country("日本")
    finally:
        await hero.close()

    # 5sim: 服务码 "in"/"mm" 不可被国家别名替换成 india/myanmar；纯文本 404 "order not found" 抛出 SmsExpiredError
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "/guest/prices" in str(request.url):
            return httpx.Response(200, json={"india": {"in": {"cost": 4.5, "count": "few"}}})
        return httpx.Response(404, text="order not found")

    fs = FiveSimProvider(AsyncMock(return_value="jwt_token"))
    await fs._client.aclose()
    fs._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        prices = await fs.get_prices("in", product="in")
        assert any("country=india&product=in" in u for u in seen_urls)
        assert prices == [{"cost": 4.5, "count": 1, "in_stock": True}]
        with pytest.raises(SmsExpiredError):
            await fs.get_sms_code("999111")
    finally:
        await fs.close()


def test_phone_normalizer_id_priority_leading_zeros_and_same_prefix_nationals():
    # 土耳其 ID=62 不能被排在前面的印尼 cc=62 抢占
    rows = [
        {"id": 6, "name": "Indonesia", "iso": "id", "cc": "62"},
        {"id": 62, "name": "Turkey", "iso": "tr", "cc": "90"},
    ]
    meta = resolve_country_metadata(62, countries=rows)
    assert meta["iso2"] == "tr"
    assert meta["calling_code"] == "90"

    # 科特迪瓦 +225 07... 保留有效首位 0 并通过严格校验
    ci = build_phone_input("+2250701234567", country_calling_code="225", country_iso2="ci")
    assert ci.e164_number == "+2250701234567"
    assert ci.phone == "0701234567"
    validate_phone_input(ci)

    # 巴西 DDD=55 的 11 位本地手机号（与国家码 55 同前缀）必须补齐 +55
    br = build_phone_input("55991234567", country_calling_code="55", country_iso2="br")
    assert br.e164_number == "+5555991234567"

    # 泰国带本地干线 0 的号码自动去 0 归一化为合法 E.164
    th = build_phone_input("0812345678", country_calling_code="66", country_iso2="th")
    assert th.e164_number == "+66812345678"

    # 越南以 84 开头的 9 位本地号不被误剥国家码
    vn = build_phone_input("843456789", country_calling_code="84", country_iso2="vn")
    assert vn.e164_number == "+84843456789"


@pytest.mark.asyncio
async def test_pricing_invalidate_generation_and_multi_id_country_index(monkeypatch):
    reg = ProviderRegistry()
    gate = asyncio.Event()
    calls = 0

    class StubProv:
        provider_id = "smsbower"

        async def resolve_country(self, c):
            return 52, "Thailand"

        async def get_prices(self, cid, svc):
            nonlocal calls
            calls += 1
            if calls == 1:
                await gate.wait()
                return [{"cost": 0.11, "count": 5}]
            return [{"cost": 0.22, "count": 8}]

    reg.init_provider("smsbower", StubProv())  # type: ignore[arg-type]
    svc = PricingService(reg, cache_ttl=30.0)

    t1 = asyncio.create_task(svc.get_price_snapshot("smsbower", "52", "go"))
    await asyncio.sleep(0.02)
    # 在途期间失效缓存并发起 force=True 刷新：旧任务结果不可污染缓存，force=True 必须立即发起新请求
    svc.invalidate()
    snap_forced = await svc.get_price_snapshot("smsbower", "52", "go", force=True)
    assert snap_forced["low"] == 0.22
    gate.set()
    await t1
    snap_cached = await svc.get_price_snapshot("smsbower", "52", "go", force=False)
    assert snap_cached["low"] == 0.22
    assert snap_cached["cached"] is True

    # GlobalCountryIndex 同一 ISO 下多 ID（如美国 187 与美国虚拟 12）全部保留
    idx = GlobalCountryIndex()
    mock_prov = MagicMock()
    mock_prov.get_countries = AsyncMock(return_value=[
        {"id": 187, "name": "United States", "iso": "us", "cc": "1"},
        {"id": 12, "name": "United States (Virtual)", "iso": "us", "cc": "1"},
    ])
    monkeypatch.setattr(global_registry, "list_providers", lambda: ["smsbower"])
    monkeypatch.setattr(global_registry, "get", lambda name: mock_prov)
    await idx.refresh(force=True)
    us_rows = idx.get_countries("smsbower")
    assert {r["id"] for r in us_rows} == {12, 187}


# ── 3. 日志脱敏、先行刷盘、密文闭合、鉴权吊销与对账隔离端到端验证 ──


def test_log_redaction_covers_wait_retry_json_code_and_exception_tracebacks():
    redacted = redact_text('resp=STATUS_WAIT_RETRY:889900 payload={"code":"445566","raw_text":"G-445566"}')
    assert "889900" not in redacted
    assert "445566" not in redacted

    fmt = RedactingFormatter("%(levelname)s %(message)s")
    try:
        raise RuntimeError("upstream leaked STATUS_OK:776655 and api_key=super_secret_token")
    except RuntimeError:
        import sys
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    rendered = fmt.format(record)
    assert "776655" not in rendered
    assert "super_secret_token" not in rendered


@pytest.mark.asyncio
async def test_end_to_end_audit_fixes_in_app_container(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "audit-secret-key-2026-very-strong")
    monkeypatch.setenv("SMS_CODE_RETENTION_HOURS", "0")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    env.reload()

    app = create_app()
    async with app.router.lifespan_context(app):
        container = app.state.container
        db = container.db
        order_service = container.order_service
        auth_service = container.auth_service

        # 1) 诊断日志在不足 100 条 INFO 缓冲且文件尚未落盘时，GET /api/v1/logs/recent 先刷盘再返回条目
        logging.getLogger("audit.probe").info("audit_log_probe_marker_123")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            sess = await client.post("/api/v1/auth/session")
            assert sess.status_code == 200
            logs_resp = await client.get("/api/v1/logs/recent?lines=50")
            assert logs_resp.status_code == 200
            assert any("audit_log_probe_marker_123" in line for line in logs_resp.json()["entries"])

            # 空白项目名必须返回 422 而非 500
            bad_proj = await client.post("/api/v1/projects/keys", json={"name": "   "})
            assert bad_proj.status_code == 422

        # 2) ConfigRepo 解密失败必须闭合返回空串，绝不把 e_ 密文当作 API Key 外发
        await db.conn.write(
            "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES ('smsbower_api_key', 'e_corrupted_cipher', datetime('now'))"
        )
        assert await db.config.get("smsbower_api_key") == ""

        # 3) 注册模拟平台验证：complete_order 失败回滚到 waiting_code 后仍能立即接收首码 + 完整保留脱敏前 raw_text
        poll_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        class AuditMockProvider:
            provider_id = "smsbower"
            capabilities = ProviderCapabilities(supports_resend=True, supports_price_lock=True, supports_custom_service=True)
            _raw_map: dict[str, str] = {}

            async def resolve_country(self, country: str):
                return 52, "Thailand"

            async def get_prices(self, cid, svc):
                return [{"cost": 0.12, "count": 10}]

            async def request_number(self, cid, svc, max_price=None):
                return Activation("aid_audit_1", "66812345678", 52, "Thailand", provider="smsbower", price=0.12, price_is_estimated=False)

            async def normalize_phone(self, raw_phone, cid):
                return build_phone_input(raw_phone, country_calling_code="66", country_iso2="th")

            async def get_sms_code(self, aid):
                st, code = await poll_queue.get()
                if st == "OK":
                    self._raw_map[aid] = f"G-{code} is your Google verification code. Valid for 10 minutes."
                return st, code

            def get_last_raw_sms(self, aid):
                return self._raw_map.get(aid, "")

            async def complete_activation_structured(self, aid):
                return parse_complete_outcome("BAD_STATUS")

            async def cancel_activation_structured(self, aid):
                from app.providers.base import parse_cancel_outcome
                return parse_cancel_outcome("ACCESS_CANCEL")

            async def close(self):
                return None

        mock_prov = AuditMockProvider()
        container.registry.init_provider("smsbower", mock_prov)  # type: ignore[arg-type]
        order_service._poll_intervals = (0.03, 0.03, 0.03)

        order = await order_service.create_order(country="52", service="google", provider_name="smsbower")
        oid = order["order_id"]
        assert order["status"] == "waiting_code"

        # 在 waiting_code 尝试 complete 失败回滚后，无需先见 WAIT 即可接收首条验证码！
        with pytest.raises(Exception):
            await order_service.complete_order(oid)
        rolled = await order_service.get_order(oid)
        assert rolled["status"] == "waiting_code"

        await poll_queue.put(("OK", "482910"))
        waited = await order_service.wait_for_code(oid, timeout=3, after_seq=0)
        assert waited["status"] == "code_received"
        assert waited["latest_code"] == "482910"
        assert "G-482910" in waited["messages"][0]["raw_text"]

        # 4) 细粒度 orders:w 权限、/wait 期间轮转密钥立即 401 中断、以及停用项目阻断待发 Webhook
        rec, raw_key = await auth_service.create_project_key(
            name="Audit Bot",
            scopes=["orders:r", "orders:w"],
        )
        proj_id = rec["project_id"]

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as pclient:
            create_res = await pclient.post(
                "/api/v1/orders",
                headers={"X-API-Key": raw_key},
                json={"country": "52", "service": "google"},
            )
            assert create_res.status_code == 201
            p_oid = create_res.json()["order_id"]

            # 启动阻塞 /wait，同时管理员轮转该项目密钥 -> /wait 必须立即返回 401 而非悬挂到超时！
            wait_req = asyncio.create_task(
                pclient.get(f"/api/v1/orders/{p_oid}/wait?timeout=20", headers={"X-API-Key": raw_key})
            )
            await asyncio.sleep(0.08)
            await auth_service.rotate_project_key(proj_id)
            container.event_bus.revoke_project_subscribers(proj_id)
            wait_resp = await asyncio.wait_for(wait_req, timeout=2.0)
            assert wait_resp.status_code == 401

        # 停用项目后，已入队的验证码 Outbox 投递必须立即中止并脱敏，绝不外发 HTTP
        await db.api_keys.update(proj_id, enabled=False)
        await db.webhooks.enqueue(
            delivery_id="dlv_disabled_proj_1",
            event_id="evt_disabled_proj_1",
            event_type="order.code_received",
            order_id=p_oid,
            project_id=proj_id,
            target_url="https://example.com/hook",
            payload={"code": "999111"},
        )
        await container.webhook_dispatcher.process_due_once()
        await container.webhook_dispatcher.drain_inflight()
        dlv_items = await db.webhooks.list(project_id=proj_id)
        assert dlv_items[0]["status"] == "failed"
        assert "停用" in dlv_items[0]["last_error"]
        assert bool(dlv_items[0]["payload_scrubbed"]) is True

        # 5) 人工核销隔离：核销同订单下早期无 activation_id 的 uncertain_purchase 绝不能终结已绑定真实激活的父订单；
        #    核销真实主激活为 completed 时，在 retention_hours=0 下必须同步脱敏并取消后台轮询！
        uncertain_row_id = await db.activations.record_intent(
            order_id=oid,
            attempt_no=0,
            provider="smsbower",
            country_id="52",
            service_code="go",
            quoted_max_price=0.12,
        )
        await db.activations.update_state(uncertain_row_id, state="uncertain_purchase", last_error="timeout")

        await order_service.resolve_activation_manually(uncertain_row_id, state="unrefundable", note="早期超时未扣款")
        still_active_parent = await order_service.get_order(oid)
        assert still_active_parent["status"] == "code_received"
        assert still_active_parent["latest_code"] == "482910"

        primary_act = await db.activations.get_by_activation_id(oid, "aid_audit_1")
        assert primary_act is not None
        await order_service.resolve_activation_manually(int(primary_act["id"]), state="completed", note="人工确认已完成")
        resolved_parent = await order_service.get_order(oid)
        assert resolved_parent["status"] == "completed"
        assert resolved_parent["settlement_state"] == "manual_resolved"
        assert resolved_parent["code_scrubbed"] is True
        assert resolved_parent["latest_code"] == ""


# ── 4. v1.1.0 深度审查回归验证 ──────────────────────────────────


@pytest.mark.asyncio
async def test_v1_1_0_audit_regressions(tmp_path: Path, monkeypatch):
    # 1) 上游永久拒绝 complete_activation_structured 必须返回 retryable=False（避免补偿死循环）
    sb = SmsBowerProvider(AsyncMock(return_value="k"))
    monkeypatch.setattr(sb, "_get", AsyncMock(side_effect=["BAD_STATUS", "NO_ACTIVATION"]))
    try:
        r1 = await sb.complete_activation_structured("act_dead_1")
        r2 = await sb.complete_activation_structured("act_dead_2")
        assert r1.ok is False and r1.retryable is False and r1.status == "rejected"
        assert r2.ok is False and r2.retryable is False and r2.status == "rejected"
    finally:
        await sb.close()

    hero = HeroSmsProvider(AsyncMock(return_value="k"))
    monkeypatch.setattr(hero, "_get", AsyncMock(return_value="BAD_STATUS"))
    try:
        rh = await hero.complete_activation_structured("act_hero_dead")
        assert rh.ok is False and rh.retryable is False and rh.status == "rejected"
    finally:
        await hero.close()

    fs = FiveSimProvider(AsyncMock(return_value="jwt"))
    await fs._client.aclose()
    fs._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(404, text="order not found")))
    try:
        rf = await fs.complete_activation_structured("999000")
        assert rf.ok is False and rf.retryable is False and rf.status == "rejected"
    finally:
        await fs.close()

    # 2) Virtual US (#12) 与实体 US (#187) 区分，且 "Papua New Guinea" 不可反向子串误命中 "Guinea"
    sb2 = SmsBowerProvider(AsyncMock(return_value="k"))
    monkeypatch.setattr(
        sb2,
        "get_countries",
        AsyncMock(return_value=[
            {"id": 68, "name": "Guinea", "iso": "gn", "cc": "224", "aliases": ["guinea"]},
            {"id": 187, "name": "United States", "iso": "us", "cc": "1", "aliases": ["usa"]},
            {"id": 12, "name": "United States (Virtual)", "iso": "us", "cc": "1", "aliases": ["usa virtual"]},
        ]),
    )
    try:
        assert (await sb2.resolve_country("US"))[0] == 187
        assert (await sb2.resolve_country("United States (Virtual)"))[0] == 12
        with pytest.raises(ValueError):
            await sb2.resolve_country("Papua New Guinea")
    finally:
        await sb2.close()

    # GlobalCountryIndex.search 可检索 provider_variants（如 virtual）
    idx = GlobalCountryIndex()
    mock_prov = MagicMock()
    mock_prov.get_countries = AsyncMock(return_value=[
        {"id": 187, "name": "United States", "iso": "us", "cc": "1"},
        {"id": 12, "name": "United States (Virtual)", "iso": "us", "cc": "1"},
    ])
    monkeypatch.setattr(global_registry, "list_providers", lambda: ["smsbower"])
    monkeypatch.setattr(global_registry, "get", lambda name: mock_prov)
    await idx.refresh(force=True)
    virt_hits = idx.search("virtual")
    assert any(h["iso"] == "us" for h in virt_hits)

    # 3) PricingService 仅调用 get_price_snapshot(..., force=True)（不调 invalidate）亦递增代数防旧包回写
    reg = ProviderRegistry()
    slow_gate = asyncio.Event()
    p_calls = 0

    class SlowThenFastProv:
        provider_id = "smsbower"

        async def resolve_country(self, c):
            return 52, "Thailand"

        async def get_prices(self, cid, svc):
            nonlocal p_calls
            p_calls += 1
            if p_calls == 1:
                await slow_gate.wait()
                return [{"cost": 0.09, "count": 2}]
            return [{"cost": 0.35, "count": 9}]

    reg.init_provider("smsbower", SlowThenFastProv())  # type: ignore[arg-type]
    psvc = PricingService(reg, cache_ttl=30.0)
    t_slow = asyncio.create_task(psvc.get_price_snapshot("smsbower", "52", "go", force=False))
    await asyncio.sleep(0.02)
    fresh = await psvc.get_price_snapshot("smsbower", "52", "go", force=True)
    assert fresh["low"] == 0.35
    slow_gate.set()
    await t_slow
    after = await psvc.get_price_snapshot("smsbower", "52", "go", force=False)
    assert after["low"] == 0.35

    # 4) 应用级：版本号 1.1.0、API Key scopes 校验、黑名单跨格式命中与去重计数、delivering webhook 人工核销脱敏
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path / "data_v110"))
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "audit-v110-secret-key-very-strong")
    monkeypatch.setenv("SMS_CODE_RETENTION_HOURS", "0")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    env.reload()

    app = create_app()
    async with app.router.lifespan_context(app):
        container = app.state.container
        db = container.db

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            health = await client.get("/api/v1/health")
            assert health.status_code == 200
            assert health.json()["version"] == "1.2.0"
            assert app.version == "1.2.0"

            await client.post("/api/v1/auth/session")

            # 非法 scope 拼写必须 422 拦截
            bad_scope = await client.post(
                "/api/v1/projects/keys",
                json={"name": "BadScopeBot", "scopes": ["order:rw"]},
            )
            assert bad_scope.status_code == 422

            # 黑名单批量添加：过滤过短无效串 + 重复项不虚增 added 计数 + 跨 +E.164/纯数字命中
            b1 = await client.post(
                "/api/v1/blacklist/batch",
                json={"phones": ["+66899998888", "+66899998888", "12"], "provider": "smsbower"},
            )
            assert b1.status_code == 201
            assert b1.json()["added"] == 1
            b2 = await client.post(
                "/api/v1/blacklist/batch",
                json={"phones": ["+66899998888"], "provider": "smsbower"},
            )
            assert b2.json()["added"] == 0
            assert await db.blacklist.is_blacklisted("66899998888", "smsbower") is True
            assert await db.blacklist.is_blacklisted("+66899998888", "smsbower") is True

        # 人工结清时若有 status='delivering' 的验证码投递，必须同步标记为 scrubbed
        await db.orders.reserve(
            order_id="ord_v110_dlv",
            provider="smsbower",
            service="google",
            service_code="go",
            country_id="52",
            country_input="52",
            country_label="Thailand",
            status="code_received",
        )
        act_row_id = await db.activations.record_intent(
            order_id="ord_v110_dlv",
            attempt_no=1,
            provider="smsbower",
            country_id="52",
            service_code="go",
            quoted_max_price=0.12,
        )
        await db.activations.bind_acquired_atomic(
            act_row_id,
            order_id="ord_v110_dlv",
            activation_id="aid_v110_dlv",
            phone_raw="66812345678",
            phone_e164="+66812345678",
            actual_cost=0.12,
        )
        await db.webhooks.enqueue(
            delivery_id="dlv_inflight_v110",
            event_id="evt_inflight_v110",
            event_type="order.code_received",
            order_id="ord_v110_dlv",
            project_id="proj_test",
            target_url="https://example.com/hook",
            payload={"code": "135790"},
        )
        await db.conn.write("UPDATE webhook_deliveries SET status='delivering' WHERE delivery_id='dlv_inflight_v110'")
        await container.order_service.resolve_activation_manually(act_row_id, state="completed", note="人工结清验证脱敏")
        dlv_row = await db.conn.fetchone("SELECT status, payload_scrubbed, payload_json FROM webhook_deliveries WHERE delivery_id='dlv_inflight_v110'")
        assert dlv_row is not None
        assert dlv_row["status"] == "scrubbed"
        assert int(dlv_row["payload_scrubbed"]) == 1
        assert "135790" not in dlv_row["payload_json"]

