"""针对 REVIEW_2026-11-25.md 全部 17 项问题的专项回归测试套件。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.event_bus import EventBus
from app.core.order_service import AcquireOptions, OrderService
from app.core.url_guard import validate_upstream_base_url, validate_webhook_url
from app.db import Database
from app.errors import AppError, ConflictError, UpstreamError
from app.main import create_app
from app.models import PhoneInput
from app.price_policy import effective_price_max
from app.providers.base import (
    ActionOutcome,
    Activation,
    CancelOutcome,
    ProviderCapabilities,
    parse_cancel_outcome,
    parse_complete_outcome,
)
from app.providers.registry import ProviderRegistry


def _make_provider(phone: str = "66812345678", activation_id: str = "act_reg_1"):
    prov = MagicMock()
    prov.provider_id = "smsbower"
    prov.capabilities = ProviderCapabilities(supports_resend=True, supports_price_lock=True)
    prov.get_balance = AsyncMock(return_value=10.0)
    prov.get_countries = AsyncMock(return_value=[
        {"id": 0, "name": "Russian Federation", "name_zh": "俄罗斯", "cc": "7", "iso": "ru"},
        {"id": 52, "name": "Thailand", "name_zh": "泰国", "cc": "66", "iso": "th"},
    ])
    prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
    prov.get_prices = AsyncMock(return_value=[{"cost": 0.10, "count": 5}])
    prov.request_number = AsyncMock(
        return_value=Activation(activation_id, phone, 52, "Thailand", provider="smsbower", price=0.10, price_is_estimated=True)
    )
    prov.normalize_phone = AsyncMock(
        return_value=PhoneInput(region="TH", phone=phone[2:], e164_number=f"+{phone}", country_calling_code="66", country_iso2="th")
    )
    prov.get_sms_code = AsyncMock(return_value=("WAIT", ""))
    prov.complete_activation = AsyncMock(return_value="ACCESS_ACTIVATION")
    prov.request_additional_sms = AsyncMock(return_value="ACCESS_RETRY_GET")
    prov.cancel_activation = AsyncMock(return_value="ACCESS_CANCEL")
    prov.close = AsyncMock()
    return prov


# ── P0-01: 管理员身份不可通过客户端标头伪造，反向代理不放行免密 ────────


@pytest.mark.asyncio
async def test_p0_01_admin_auth_cannot_be_spoofed_via_headers_or_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p0-01-secret")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "super-admin-pass")
    from app.config import env
    env.reload()

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    app = create_app(db_path=str(tmp_path / "p001.db"), enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. 伪造 X-SMS-Hub-Client + Origin 在口令模式下被拒
            r = await client.get("/api/v1/settings", headers={"X-SMS-Hub-Client": "web-console", "Origin": "http://127.0.0.1:9099"})
            assert r.status_code == 401

            # 2. 错误口令被拒
            bad_login = await client.post("/api/v1/auth/login", json={"password": "wrong"})
            assert bad_login.status_code == 401

            # 3. 正确口令登录签发 HttpOnly Cookie 后可访问管理员接口
            ok_login = await client.post("/api/v1/auth/login", json={"password": "super-admin-pass"})
            assert ok_login.status_code == 200
            assert "sms_hub_session" in client.cookies
            assert (await client.get("/api/v1/settings")).status_code == 200

            # 4. 项目密钥无法访问管理接口或修改上游地址
            key_data = (await client.post("/api/v1/projects/keys", json={"name": "P1", "project_id": "p1"})).json()
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as p1_client:
                assert (await p1_client.get("/api/v1/settings", headers={"X-API-Key": key_data["api_key"]})).status_code == 403
                assert (await p1_client.post("/api/v1/projects/keys", headers={"X-API-Key": key_data["api_key"]}, json={"name": "Evil"})).status_code == 403

            # 5. 修改上游 Base URL 拒绝非授权域名
            bad_upstream = await client.put("/api/v1/settings", json={"smsbower_base_url": "https://evil.attacker.example/steal"})
            assert bad_upstream.status_code == 422

    # 6. 未设口令但带反向代理头 X-Forwarded-For，同样拒绝匿名/免密管理访问
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    env.reload()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as proxied:
            r = await proxied.get("/api/v1/settings", headers={"X-Forwarded-For": "203.0.113.5"})
            assert r.status_code == 401
            boot = await proxied.post("/api/v1/auth/session", headers={"X-Forwarded-For": "203.0.113.5"})
            assert boot.status_code == 403


# ── P0-02: 幂等键按项目隔离 + 同项目并发仅购买一次 + 参数冲突返回 409 ──


@pytest.mark.asyncio
async def test_p0_02_idempotency_scoped_per_project_and_concurrent_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p0-02-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "p002.db"))
    await db.open()
    prov = _make_provider()
    buy_counter = 0

    async def slow_buy(cid, svc, max_p):
        nonlocal buy_counter
        buy_counter += 1
        idx = buy_counter
        await asyncio.sleep(0.05)
        return Activation(f"act_{idx}", f"6681000000{idx}", cid, "Thailand", provider="smsbower", price=0.10)

    prov.request_number = AsyncMock(side_effect=slow_buy)
    prov.normalize_phone = AsyncMock(
        side_effect=lambda raw, cid: PhoneInput(region="TH", phone=raw[2:], e164_number=f"+{raw}", country_calling_code="66", country_iso2="th")
    )

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc = OrderService(db, reg, EventBus(), poll_intervals=(0.05, 0.05, 0.05))
    try:
        # 1. 同项目两个并发请求携带相同 Idempotency-Key -> 只购买 1 次，两者拿到同一个订单，无 IntegrityError
        o1, o2 = await asyncio.gather(
            svc.create_order(country="Thailand", service="whatsapp", project_id="proj_a", idempotency_key="same-key"),
            svc.create_order(country="Thailand", service="whatsapp", project_id="proj_a", idempotency_key="same-key"),
        )
        assert o1["order_id"] == o2["order_id"]
        assert buy_counter == 1

        # 2. 同项目复用同一 Idempotency-Key 但修改业务参数 -> 返回 409 IDEMPOTENCY_CONFLICT
        with pytest.raises(ConflictError) as exc:
            await svc.create_order(country="Thailand", service="telegram", project_id="proj_a", idempotency_key="same-key")
        assert exc.value.code == "IDEMPOTENCY_CONFLICT"

        # 3. 另一项目使用完全相同的 Idempotency-Key -> 独立下单，绝不返回 proj_a 的订单或验证码
        o_b = await svc.create_order(country="Thailand", service="whatsapp", project_id="proj_b", idempotency_key="same-key")
        assert o_b["order_id"] != o1["order_id"]
        assert o_b["project_id"] == "proj_b"
        assert buy_counter == 2
    finally:
        await svc.shutdown()
        await db.close()


# ── P1-01: Webhook SSRF 防护与项目回调同源授权 ────────────────────────


def test_p1_01_webhook_ssrf_and_project_origin_guard(monkeypatch):
    monkeypatch.delenv("WEBHOOK_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_NETS", "false")
    from app.config import env
    env.reload()

    # 阻断回环、云元数据、私有网段与非 HTTPS
    for bad_url in (
        "http://127.0.0.1:8080/hook",
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/internal",
        "http://example.com/hook",
        "https://user:pass@example.com/hook",
    ):
        with pytest.raises(AppError):
            validate_webhook_url(bad_url, resolve_dns=False)

    # 非管理员项目不可越权指定与管理员登记不同源的回调地址
    with pytest.raises(AppError) as exc:
        validate_webhook_url(
            "https://evil.example/hook",
            allowed_project_url="https://good.example/default-hook",
            enforce_project_origin=True,
            resolve_dns=False,
        )
    assert exc.value.status_code == 403

    # 同源回调放行
    clean, _ = validate_webhook_url(
        "https://good.example/order-123",
        allowed_project_url="https://good.example/default-hook",
        enforce_project_origin=True,
        resolve_dns=False,
    )
    assert clean == "https://good.example/order-123"

    # 上游域名校验阻断未知域名
    with pytest.raises(AppError):
        validate_upstream_base_url("smsbower", "https://attacker.tld/stubs/handler_api.php")


# ── P1-02: WA 插件接入主鉴权 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_02_wa_plugin_requires_auth_and_admin_for_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p1-02-secret")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "wa-admin-pass")
    from app.config import env
    env.reload()

    fake_wa = MagicMock()
    fake_wa._base_url = "http://wa-app:8080"
    fake_wa.configure_auth = MagicMock()
    fake_wa.health = AsyncMock(return_value={"ok": True})
    fake_wa.list_accounts = AsyncMock(return_value=[{"wa_account_id": "acc_1"}])
    fake_wa.set_base_url = MagicMock()
    fake_wa.close = AsyncMock()
    monkeypatch.setattr("app.plugins.whatsapp_wa.plugin.create_wa_adapter", lambda _n, _u: fake_wa)

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    app = create_app(db_path=str(tmp_path / "p102.db"), enable_wa_plugin=True, registry_instance=reg, skip_startup_probe=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anon, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin:
            # 匿名请求 WA 账号与配置均返回 401
            assert (await anon.get("/api/v1/plugins/whatsapp_wa/accounts")).status_code == 401
            assert (await anon.put("/api/v1/plugins/whatsapp_wa/config", json={"wa_app_url": "http://evil:80"})).status_code == 401

            # 管理员登录并签发无 wa 权限的普通项目 Key
            await admin.post("/api/v1/auth/login", json={"password": "wa-admin-pass"})
            proj = (await admin.post("/api/v1/projects/keys", json={"name": "NormalProj", "project_id": "norm"})).json()

            # 普通项目密钥无 wa:r / wa:rw 权限 -> 403
            assert (await anon.get("/api/v1/plugins/whatsapp_wa/accounts", headers={"X-API-Key": proj["api_key"]})).status_code == 403
            assert (await anon.put("/api/v1/plugins/whatsapp_wa/config", headers={"X-API-Key": proj["api_key"]}, json={"wa_app_url": "http://wa2:8080"})).status_code == 403

            # 管理员本人允许读取和配置
            assert (await admin.get("/api/v1/plugins/whatsapp_wa/accounts")).status_code == 200
            assert (await admin.put("/api/v1/plugins/whatsapp_wa/config", json={"wa_app_url": "http://wa2:8080"})).status_code == 200


# ── P1-03: 黑名单退款未结清停止继续买号并追踪激活补偿 ──────────────────


@pytest.mark.asyncio
async def test_p1_03_blacklist_unconfirmed_refund_stops_buying_and_tracks_activation(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p1-03-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "p103.db"))
    await db.open()
    await db.blacklist.add("+66812345678", reason="bad", provider="smsbower")

    prov = _make_provider()
    # 上游拒绝早期取消（退款未确认）
    prov.cancel_activation = AsyncMock(return_value="EARLY_CANCEL_DENIED")

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc = OrderService(db, reg, EventBus(), poll_intervals=(0.05, 0.05, 0.05))
    try:
        with pytest.raises(AppError) as exc:
            await svc.create_order(country="Thailand", service="whatsapp", strategy=AcquireOptions(max_attempts=3))
        assert exc.value.code == "BLACKLIST_REFUND_UNCONFIRMED"
        # 绝不继续购买第二个号！
        assert prov.request_number.await_count == 1

        # order_activations 中保留了该 activation 的 refund_pending 记录
        pending_acts = await db.activations.list_pending_refunds()
        assert len(pending_acts) == 1
        assert pending_acts[0]["activation_id"] == "act_reg_1"

        # 冷却结束后后台对账确认退款
        prov.cancel_activation = AsyncMock(return_value="ACCESS_CANCEL")
        reconciled = await svc.reconcile_pending()
        assert reconciled >= 1
        assert await db.activations.list_pending_refunds() == []
    finally:
        await svc.shutdown()
        await db.close()


# ── P1-04: 完成失败不伪报 completed；并发完成与取消严格互斥 ──────────────


@pytest.mark.asyncio
async def test_p1_04_complete_failure_rollback_and_mutex_with_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p1-04-secret")
    from app.config import env
    env.reload()

    # 先验证严格语义解析
    assert parse_cancel_outcome("ACCESS_CANCEL").refund_confirmed is True
    assert parse_cancel_outcome("ACCESS_READY").refund_confirmed is False
    assert parse_cancel_outcome("NO_ACTIVATION").refund_confirmed is False
    assert parse_complete_outcome("ACCESS_ACTIVATION").ok is True
    assert parse_complete_outcome("BAD_STATUS").ok is False

    db = Database(str(tmp_path / "p104.db"))
    await db.open()
    prov = _make_provider()
    prov.get_sms_code = AsyncMock(return_value=("OK", "123456"))

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc = OrderService(db, reg, EventBus(), poll_intervals=(0.01, 0.01, 0.01))
    try:
        order = await svc.create_order(country="Thailand", service="whatsapp")
        oid = order["order_id"]
        received = await svc.wait_for_code(oid, timeout=3)
        assert received["status"] == "code_received"

        # 1. 上游完成返回错误时必须报错且回滚状态，不可伪报 completed
        prov.complete_activation = AsyncMock(return_value="BAD_STATUS")
        with pytest.raises(UpstreamError) as exc:
            await svc.complete_order(oid)
        assert exc.value.code == "COMPLETE_FAILED"
        after_fail = await svc.get_order(oid)
        assert after_fail["status"] == "code_received"
        assert after_fail["settlement_state"] == "complete_failed"

        # 2. 并发调用 complete 与 cancel：只允许一方获准执行上游动作，另一方 409 冲突
        async def slow_complete(_aid):
            await asyncio.sleep(0.05)
            return "ACCESS_ACTIVATION"

        async def slow_cancel(_aid):
            await asyncio.sleep(0.05)
            return "ACCESS_CANCEL"

        prov.complete_activation = AsyncMock(side_effect=slow_complete)
        prov.cancel_activation = AsyncMock(side_effect=slow_cancel)

        results = await asyncio.gather(
            svc.complete_order(oid),
            svc.cancel_order(oid),
            return_exceptions=True,
        )
        successes = [r for r in results if isinstance(r, dict)]
        conflicts = [r for r in results if isinstance(r, ConflictError)]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert prov.complete_activation.await_count + prov.cancel_activation.await_count == 1
    finally:
        await svc.shutdown()
        await db.close()


# ── P1-05: 绝对价格上限不可被 target_price/浮动覆盖 + 成本分账准确 ───────


def test_p1_05_hard_price_max_cannot_be_exceeded():
    # target_price > price_max 直接校验报错
    with pytest.raises(ValueError, match="price_max"):
        effective_price_max(target_price=0.15, price_max=0.10, price_float_enabled=True, price_float_percent=20)

    # 浮动计算结果 0.10 * 1.5 = 0.15 受 price_max=0.12 硬性截断
    assert effective_price_max(target_price=0.10, price_max=0.12, price_float_enabled=True, price_float_percent=50) == 0.12


# ── P1-06: 缺密钥拒绝明文降级 + 过期未关单 code_received 也能自动脱敏 ──


@pytest.mark.asyncio
async def test_p1_06_no_plaintext_fallback_and_open_order_code_scrub(tmp_path, monkeypatch):
    from app.config import env

    # 1. 缺失 BRIDGE_SECRET_KEY 时写验证码或 Webhook 密钥必须显式抛错，不落明文
    monkeypatch.delenv("BRIDGE_SECRET_KEY", raising=False)
    env.reload()
    db_no_key = Database(str(tmp_path / "nokey.db"))
    await db_no_key.open()
    try:
        await db_no_key.orders.create(
            order_id="ord_nokey", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="waiting_code",
        )
        with pytest.raises(RuntimeError, match="BRIDGE_SECRET_KEY"):
            await db_no_key.orders.append_code("ord_nokey", "999888")
        with pytest.raises(RuntimeError, match="BRIDGE_SECRET_KEY"):
            await db_no_key.api_keys.create(
                project_id="p_nokey", name="NoKey", key_prefix="sms_k_", key_hash="h_nokey",
                scopes=["orders:rw"], webhook_secret="my-secret",
            )
    finally:
        await db_no_key.close()

    # 2. 过期 2 天但未手动关单的 code_received 订单，在 retention_hours=0 时同样被清理！
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p1-06-secret")
    env.reload()
    db = Database(str(tmp_path / "scrub_open.db"))
    await db.open()
    try:
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        await db.orders.create(
            order_id="ord_open_old", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="waiting_code",
        )
        await db.orders.append_code("ord_open_old", "771122")
        await db.orders.update("ord_open_old", expires_at=two_days_ago, code_received_at=two_days_ago)

        scrubbed = await db.orders.scrub_expired_codes(retention_hours=0)
        assert scrubbed == 1
        after = await db.orders.get("ord_open_old")
        assert after is not None
        assert after["status"] == "code_received"  # 不破坏结算状态
        assert after["code_scrubbed"] is True
        assert after["latest_code"] == ""
    finally:
        await db.close()


# ── P1-07: 重发后重启 & 查码网络异常绝不把旧验证码记为新短信 ────────────


@pytest.mark.asyncio
async def test_p1_07_resend_restart_and_network_error_do_not_duplicate_old_code(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p1-07-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "p107.db"))
    await db.open()
    prov = _make_provider()
    prov.get_sms_code = AsyncMock(return_value=("OK", "111222"))

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc1 = OrderService(db, reg, EventBus(), poll_intervals=(0.01, 0.01, 0.01))
    order = await svc1.create_order(country="Thailand", service="whatsapp")
    oid = order["order_id"]
    await svc1.wait_for_code(oid, timeout=3)

    # 触发重发（上游短暂先抛网络异常，随后仍返回旧码 111222）
    call_seq = iter([
        RuntimeError("temp network blip"),
        ("OK", "111222"),  # 旧码不应被误认为新码！
    ])

    async def flaky_poll(_aid):
        nxt = next(call_seq, ("OK", "111222"))
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    prov.get_sms_code = AsyncMock(side_effect=flaky_poll)
    await svc1.resend_sms(oid)
    await asyncio.sleep(0.06)
    mid = await svc1.get_order(oid)
    assert mid["status"] == "waiting_next_code"
    assert mid["code_count"] == 1  # 旧码没有被重复记录！

    # 模拟进程重启：新建 OrderService 执行 recover_on_startup()
    await svc1.shutdown()
    svc2 = OrderService(db, reg, EventBus(), poll_intervals=(0.01, 0.01, 0.01))
    try:
        # 重启后上游先继续返回旧码，之后进入 WAIT 再来新码 333444
        restart_seq = iter([
            ("OK", "111222"),
            ("WAIT", ""),
            ("OK", "333444"),
        ])
        prov.get_sms_code = AsyncMock(side_effect=lambda _aid: next(restart_seq, ("WAIT", "")))
        await svc2.recover_on_startup()
        final = await svc2.wait_for_code(oid, timeout=3, after_seq=1)
        assert final["status"] == "code_received"
        assert [m["code"] for m in final["messages"]] == ["111222", "333444"]
    finally:
        await svc2.shutdown()
        await db.close()


# ── P1-08: 严格模式下工作台 SSE 共用签名 Cookie 认证 & Last-Event-ID 回放 ─


@pytest.mark.asyncio
async def test_p1_08_strict_mode_sse_with_cookie_and_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p1-08-secret")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    from app.config import env
    env.reload()

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    app = create_app(db_path=str(tmp_path / "p108.db"), enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as anon:
            await admin.post("/api/v1/auth/session")
            await admin.put("/api/v1/settings", json={"api_auth_required": True})

            # 无 Cookie 无 Key 订阅 SSE -> 401
            assert (await anon.get("/api/v1/events/stream")).status_code == 401

            # EventBus 历史环支持 Last-Event-ID 补发
            bus = app.state.container.event_bus
            e1 = bus.publish("order.created", {"order_id": "ord_x", "project_id": "web-console"})
            e2 = bus.publish("order.code_received", {"order_id": "ord_x", "project_id": "web-console", "latest_code": "888999"})
            replayed = bus.replay_since(e1.id, project_id="web-console")
            assert [e.id for e in replayed] == [e2.id]

            # 管理员带签名 Cookie 打开 SSE（通过原生 ASGI 校验 200 与 Last-Event-ID 断线回放后主动断开）
            cookie_val = admin.cookies.get("sms_hub_session")
            assert cookie_val
            received_chunks: list[bytes] = []
            status_holder: list[int] = []
            disconnected = asyncio.Event()

            async def asgi_receive():
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def asgi_send(message):
                if message["type"] == "http.response.start":
                    status_holder.append(message["status"])
                elif message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    if body:
                        received_chunks.append(body)
                        if b"888999" in body:
                            disconnected.set()

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/v1/events/stream",
                "raw_path": b"/api/v1/events/stream",
                "query_string": b"",
                "headers": [
                    (b"cookie", f"sms_hub_session={cookie_val}".encode("utf-8")),
                    (b"last-event-id", e1.id.encode("utf-8")),
                ],
                "client": ("127.0.0.1", 50000),
                "server": ("127.0.0.1", 9090),
            }
            sse_task = asyncio.create_task(app(scope, asgi_receive, asgi_send))
            await asyncio.wait_for(disconnected.wait(), timeout=3.0)
            sse_task.cancel()
            await asyncio.gather(sse_task, return_exceptions=True)

            assert status_holder == [200]
            assert any(b"888999" in chunk for chunk in received_chunks)


# ── P1-10: 统计与黑名单租户隔离 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_10_stats_and_blacklist_isolated_per_project(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p1-10-secret")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    from app.config import env
    env.reload()

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    app = create_app(db_path=str(tmp_path / "p110.db"), enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as ext:
            await admin.post("/api/v1/auth/session")
            k1 = (await admin.post("/api/v1/projects/keys", json={"name": "P1", "project_id": "p1"})).json()["api_key"]
            k2 = (await admin.post("/api/v1/projects/keys", json={"name": "P2", "project_id": "p2"})).json()["api_key"]

            # P1 下单并取消拉黑
            o1 = (await ext.post("/api/v1/orders", headers={"X-API-Key": k1}, json={"service": "whatsapp", "country": "Thailand"})).json()
            await ext.post(f"/api/v1/orders/{o1['order_id']}/cancel", headers={"X-API-Key": k1}, json={"blacklist": True})

            # P2 看不到 P1 的统计和黑名单
            s2 = (await ext.get("/api/v1/stats", headers={"X-API-Key": k2})).json()
            assert s2["scope"] == "project"
            assert s2["today"]["total"] == 0
            assert s2["blacklist_count"] == 0
            bl2 = (await ext.get("/api/v1/blacklist", headers={"X-API-Key": k2})).json()
            assert bl2["total"] == 0

            # P1 能看到自己的统计和私有黑名单，且不能越权写全局黑名单
            s1 = (await ext.get("/api/v1/stats", headers={"X-API-Key": k1})).json()
            assert s1["today"]["total"] == 1
            assert s1["blacklist_count"] == 1
            forbidden_global = await ext.post(
                "/api/v1/blacklist",
                headers={"X-API-Key": k1},
                json={"phone": "+66888888888", "scope": "global"},
            )
            assert forbidden_global.status_code == 403


# ── P2-01, P2-03, P2-04, P2-05: 默认平台、健康 503、旧路由兼容与 OpenAPI ─


@pytest.mark.asyncio
async def test_p2_default_provider_health_503_legacy_compat_and_openapi(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p2-secret")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    from app.config import env
    env.reload()

    # P2-01: ACTIVE_PROVIDERS 顺序与 DEFAULT_PROVIDER 不同时，遵循 DEFAULT_PROVIDER
    app = create_app(
        db_path=str(tmp_path / "p2.db"),
        active_providers=["5sim", "smsbower"],
        default_provider="smsbower",
        enable_wa_plugin=False,
        skip_startup_probe=True,
    )
    async with app.router.lifespan_context(app):
        assert app.state.container.registry.default_provider == "smsbower"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # P2-03: DB 正常返回 200，DB 故障返回 503
            ok_h = await client.get("/api/v1/health")
            assert ok_h.status_code == 200
            assert ok_h.json()["default_provider"] == "smsbower"

            app.state.container.db.conn.health = AsyncMock(return_value=False)
            bad_h = await client.get("/api/v1/health")
            assert bad_h.status_code == 503
            assert bad_h.json()["ok"] is False
            assert (await client.get("/api/v1/health/live")).status_code == 200

            # P2-05: OpenAPI 含安全方案、201/202 与结构化错误模型
            spec = (await client.get("/openapi.json")).json()
            schemes = spec.get("components", {}).get("securitySchemes", {})
            assert "APIKeyHeader" in schemes
            assert "HTTPBearer" in schemes
            order_post_resps = spec["paths"]["/api/v1/orders"]["post"]["responses"]
            assert "201" in order_post_resps
            assert "202" in order_post_resps
            assert "401" in order_post_resps
            assert "409" in order_post_resps


@pytest.mark.asyncio
async def test_p1_05_p1_09_p2_04_cost_ledger_webhook_recovery_and_legacy_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "p-extra-secret")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "extra-admin-pass")
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "hooks.partner.internal")
    from app.config import env
    env.reload()

    fake_wa = MagicMock()
    fake_wa._base_url = "http://wa-app:8080"
    fake_wa.configure_auth = MagicMock()
    fake_wa.health = AsyncMock(return_value={"ok": True})
    fake_wa.list_accounts = AsyncMock(return_value=[])
    fake_wa.close = AsyncMock()
    monkeypatch.setattr("app.plugins.whatsapp_wa.plugin.create_wa_adapter", lambda _n, _u: fake_wa)

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    app = create_app(
        db_path=str(tmp_path / "extra.db"),
        enable_wa_plugin=True,
        enable_legacy_api=True,
        registry_instance=reg,
        skip_startup_probe=True,
    )

    async with app.router.lifespan_context(app):
        db: Database = app.state.container.db
        # P1-05: 成本分账（区分 actual vs estimated vs unknown + refund_confirmed / pending / unconfirmed）
        await db.orders.create(
            order_id="ord_cost_est", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="completed", project_id="web-console",
        )
        await db.orders.update("ord_cost_est", cost=0.12, quoted_max_price=0.15, cost_status="estimated")
        await db.orders.create(
            order_id="ord_cost_act", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="completed", project_id="web-console",
        )
        await db.orders.update("ord_cost_act", cost=0.25, actual_cost=0.25, cost_status="actual")
        await db.orders.create(
            order_id="ord_cost_unk", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="completed", project_id="web-console",
        )
        await db.orders.update("ord_cost_unk", cost_status="unknown")
        await db.orders.create(
            order_id="ord_ref_ok", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="cancelled", project_id="web-console",
        )
        await db.orders.update("ord_ref_ok", refund_state="confirmed")
        await db.orders.create(
            order_id="ord_ref_pend", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="cancelled", project_id="web-console",
        )
        await db.orders.update("ord_ref_pend", refund_state="pending")

        stats = await db.orders.get_stats()
        assert stats["today"]["actual_cost_total"] == pytest.approx(0.25)
        assert stats["today"]["estimated_cost_total"] == pytest.approx(0.12)
        assert stats["today"]["unknown_cost_count"] == 1
        assert stats["today"]["refund_confirmed_count"] == 1
        assert stats["today"]["refund_pending_count"] == 1
        assert stats["today"]["unconfirmed_refund_count"] == 1

        # P2-04: ENABLE_LEGACY_API=true 挂载 /api/tasks 且强制要求管理员权限
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anon, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin:
            assert (await anon.get("/api/tasks")).status_code == 401
            await admin.post("/api/v1/auth/login", json={"password": "extra-admin-pass"})
            proj = (await admin.post("/api/v1/projects/keys", json={"name": "WH1", "project_id": "p_wh1"})).json()
            assert (await anon.get("/api/tasks", headers={"X-API-Key": proj["api_key"]})).status_code == 403
            assert (await admin.get("/api/tasks")).status_code == 200

            # P1-09: 重启恢复 delivering 状态的 Outbox 投递 + 按项目隔离查询投递日志
            await db.webhooks.enqueue(
                delivery_id="dlv_rec_1",
                event_id="evt_rec_1",
                event_type="order.code_received",
                order_id="ord_cost_act",
                project_id="p_wh1",
                target_url="https://hooks.partner.internal/hook",
                payload={"event": "order.code_received"},
            )
            claimed = await db.webhooks.claim_due(limit=5)
            assert len(claimed) == 1
            assert claimed[0]["status"] == "delivering"
            recovered = await db.webhooks.recover_inflight()
            assert recovered == 1
            after_rec = await db.webhooks.list(project_id="p_wh1")
            assert after_rec[0]["status"] == "pending"

            # 项目密钥只看得到自己的 Webhook 投递记录
            wh_list = await anon.get("/api/v1/projects/webhooks/deliveries", headers={"X-API-Key": proj["api_key"]})
            assert wh_list.status_code == 200
            assert wh_list.json()["total"] == 1
            assert wh_list.json()["items"][0]["delivery_id"] == "dlv_rec_1"

            # P1-01: Webhook 分发器禁止自动重定向（防止 302 跳内网）
            wh_disp = app.state.container.webhook_dispatcher
            assert wh_disp._client.follow_redirects is False
            redirect_resp = MagicMock(status_code=302, text="")
            wh_disp._client.post = AsyncMock(return_value=redirect_resp)
            redir_res = await wh_disp.send_test("https://hooks.partner.internal/redirect")
            assert redir_res["ok"] is False
            assert "重定向" in redir_res["error"]
