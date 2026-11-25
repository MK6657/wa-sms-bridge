"""针对 REVIEW_V3_2026-11-25.md 全部 4 项复核问题的专项回归测试套件。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

import app.core.webhook_dispatcher as wh_mod
from app.core.event_bus import EventBus
from app.core.order_service import OrderService
from app.core.webhook_dispatcher import WebhookDispatcher
from app.db import Database
from app.models import PhoneInput
from app.providers.base import Activation, ProviderCapabilities
from app.providers.registry import ProviderRegistry


def _make_provider(phone: str = "66812345678", activation_id: str = "act_v3_1"):
    prov = MagicMock()
    prov.provider_id = "smsbower"
    prov.capabilities = ProviderCapabilities(
        supports_resend=True,
        supports_price_lock=True,
        supports_exact_price_lock=False,
    )
    prov.get_balance = AsyncMock(return_value=10.0)
    prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
    prov.request_number = AsyncMock(
        return_value=Activation(activation_id, phone, 52, "Thailand", provider="smsbower", price=0.10, price_is_estimated=True)
    )
    prov.normalize_phone = AsyncMock(
        return_value=PhoneInput(region="TH", phone=phone[2:], e164_number=f"+{phone}", country_calling_code="66", country_iso2="th")
    )
    prov.get_sms_code = AsyncMock(return_value=("OK", "543210"))
    prov.complete_activation = AsyncMock(return_value="ACCESS_ACTIVATION")
    prov.request_additional_sms = AsyncMock(return_value="ACCESS_RETRY_GET")
    prov.cancel_activation = AsyncMock(return_value="ACCESS_CANCEL")
    prov.close = AsyncMock()
    return prov


# ── P0-01: Webhook 暂停在 DNS 时清理绝不发出旧码；并发清理不被覆写；接收方回显正文不落库/不进日志 ──


@pytest.mark.asyncio
async def test_v3_p0_01_webhook_dns_race_lease_cas_and_error_body_redaction(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v3-p0-01-secret")
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "hooks.partner.internal")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v3_p001.db"))
    await db.open()
    bus = EventBus()
    prov = _make_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc = OrderService(db, reg, bus, poll_intervals=(0.01, 0.01, 0.01))
    dispatcher = WebhookDispatcher(db, retry_delays=(0.01,), poll_interval=0.05)

    try:
        # 场景 A: 投递任务已认领并卡在 DNS 校验，期间执行订单脱敏清理（不依赖取消任务，靠发前复核与租约 CAS 兜底）
        order_a = await svc.create_order(
            country="Thailand",
            service="whatsapp",
            webhook_url="https://hooks.partner.internal/cb",
        )
        oid_a = order_a["order_id"]
        await svc.wait_for_code(oid_a, timeout=3)

        dns_entered = asyncio.Event()
        dns_release = asyncio.Event()
        real_validate = wh_mod.validate_webhook_url_async

        async def paused_dns(url, **kwargs):
            dns_entered.set()
            await dns_release.wait()
            return await real_validate(url, **kwargs)

        monkeypatch.setattr(wh_mod, "validate_webhook_url_async", paused_dns)
        post_mock = AsyncMock(return_value=MagicMock(status_code=200, text="ok"))
        dispatcher._client.post = post_mock

        assert await dispatcher.process_due_once() >= 1
        await asyncio.wait_for(dns_entered.wait(), timeout=2.0)

        # 在 DNS 阻塞期间完成订单验证码清理
        await svc.scrub_order_code(oid_a)

        # 放行 DNS，验证不会发出旧验证码且状态保持 scrubbed（不会被改回 delivered）
        dns_release.set()
        await dispatcher.drain_inflight()

        for call in post_mock.await_args_list:
            assert b"543210" not in call.kwargs.get("content", b"")
        wh_a = await db.conn.fetchone(
            "SELECT status, payload_json, payload_scrubbed FROM webhook_deliveries "
            "WHERE order_id=? AND event_type='order.code_received'",
            (oid_a,),
        )
        assert wh_a["status"] == "scrubbed"
        assert wh_a["payload_scrubbed"] == 1
        assert wh_a["payload_json"] == "{}"

        # 场景 B: 即使并发脱敏发生在 HTTP POST 飞行途中，mark_delivered 租约 CAS 也绝不把 scrubbed 覆写回 delivered
        monkeypatch.setattr(wh_mod, "validate_webhook_url_async", real_validate)
        prov.get_sms_code = AsyncMock(return_value=("OK", "654987"))
        order_b = await svc.create_order(
            country="Thailand",
            service="whatsapp",
            webhook_url="https://hooks.partner.internal/cb",
        )
        oid_b = order_b["order_id"]
        await svc.wait_for_code(oid_b, timeout=3)

        post_entered = asyncio.Event()
        post_release = asyncio.Event()

        async def inflight_post(url, *, content, headers):
            if b"654987" in content:
                post_entered.set()
                await post_release.wait()
            return MagicMock(status_code=200, text="ok")

        dispatcher._client.post = AsyncMock(side_effect=inflight_post)
        await dispatcher.process_due_once()
        await asyncio.wait_for(post_entered.wait(), timeout=2.0)
        await svc.scrub_order_code(oid_b)
        post_release.set()
        await dispatcher.drain_inflight()

        wh_b = await db.conn.fetchone(
            "SELECT status, payload_scrubbed FROM webhook_deliveries "
            "WHERE order_id=? AND event_type='order.code_received'",
            (oid_b,),
        )
        assert wh_b["status"] == "scrubbed"
        assert wh_b["payload_scrubbed"] == 1

        # 场景 C: 接收端 500 错误正文回显验证码，绝不写入 last_error 或日志
        prov.get_sms_code = AsyncMock(return_value=("OK", "192837"))
        order_c = await svc.create_order(
            country="Thailand",
            service="whatsapp",
            webhook_url="https://hooks.partner.internal/cb",
        )
        oid_c = order_c["order_id"]
        await svc.wait_for_code(oid_c, timeout=3)

        dispatcher._client.post = AsyncMock(
            return_value=MagicMock(status_code=500, text="partner rejected code=192837 invalid")
        )
        with caplog.at_level(logging.WARNING, logger="app.core.webhook_dispatcher"):
            await dispatcher.process_due_once()
            await dispatcher.drain_inflight()

        wh_c = await db.conn.fetchone(
            "SELECT last_error, last_status_code FROM webhook_deliveries "
            "WHERE order_id=? AND event_type='order.code_received'",
            (oid_c,),
        )
        assert wh_c["last_status_code"] == 500
        assert "192837" not in wh_c["last_error"]
        assert "HTTP 500" in wh_c["last_error"]
        assert "192837" not in caplog.text
    finally:
        await dispatcher.shutdown()
        await svc.shutdown()
        await db.close()


# ── P0-02: 慢订阅者积压队列、多订阅者并发与已开始的回放列表清理后均不再输出旧码 ──


@pytest.mark.asyncio
async def test_v3_p0_02_sse_slow_queues_and_extracted_replay_scrubbed_immediately():
    bus = EventBus()
    anchor = bus.publish("order.created", {"order_id": "ord_sse_1", "project_id": "p1"})

    async with bus.subscribe(project_id="p1") as slow_q1, bus.subscribe(project_id="p1") as slow_q2:
        # 1. 发布含验证码的事件到两个未消费的慢订阅者队列
        bus.publish(
            "order.code_received",
            {
                "order_id": "ord_sse_1",
                "project_id": "p1",
                "latest_code": "889900",
                "code_scrubbed": False,
                "messages": [{"id": 1, "seq": 1, "code": "889900", "raw_text": "OTP 889900"}],
                "message": {"id": 1, "seq": 1, "code": "889900", "raw_text": "OTP 889900"},
            },
        )
        # 在清理前就已经提取出的回放列表（模拟 SSE 回放中途停顿后触发清理）
        extracted_replay, found = bus.replay_since_with_status(anchor.id, project_id="p1")
        assert found is True and len(extracted_replay) == 1

        # 2. 执行验证码清理
        bus.scrub_order_codes(["ord_sse_1"])

        # 3. 两个慢订阅者现在才从队列消费 -> 两者拿到的事件均已无旧码！
        for q in (slow_q1, slow_q2):
            evt = q.get_nowait()
            payload = evt.to_payload()
            assert "889900" not in str(payload)
            assert evt.data["latest_code"] == ""
            assert evt.data["code_scrubbed"] is True
            assert evt.data["messages"][0]["code"] == ""
            assert evt.data["message"]["code"] == ""

        # 4. 清理前已取出的回放列表对象也即时失效 -> 不含旧码！
        for replayed_evt in extracted_replay:
            assert "889900" not in str(replayed_evt.to_payload())
            assert replayed_evt.data["latest_code"] == ""

        # 5. 同一订单后续重发收到的全新验证码（更高 seq）不受旧清理影响
        bus.publish(
            "order.code_received",
            {
                "order_id": "ord_sse_1",
                "project_id": "p1",
                "latest_code": "112299",
                "code_scrubbed": False,
            },
        )
        fresh_evt = slow_q1.get_nowait()
        assert fresh_evt.to_payload()["data"]["latest_code"] == "112299"


# ── P1-01: 补偿单轮仅调一次上游、同步结清订单与台账、已确认退款永不被覆盖 ──


@pytest.mark.asyncio
async def test_v3_p1_01_reconcile_single_pass_and_immutable_refund_confirmed(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v3-p1-01-secret")
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "hooks.partner.internal")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v3_p101.db"))
    await db.open()
    prov = _make_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc = OrderService(db, reg, EventBus(), poll_intervals=(0.01, 0.01, 0.01))
    try:
        # 场景 A: 精确复现上游依次返回 [EARLY_CANCEL_DENIED, ACCESS_CANCEL, NO_ACTIVATION]
        await db.orders.create(
            order_id="ord_rc_1",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="compensation_pending",
            webhook_url="https://hooks.partner.internal/cb",
        )
        await db.orders.update("ord_rc_1", activation_id="act_rc_1", refund_state="pending")
        act_row = await db.activations.record_intent(
            order_id="ord_rc_1", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )
        await db.activations.bind_acquired(act_row, activation_id="act_rc_1", phone_raw="66812345678", state="refund_pending")

        responses = iter(["EARLY_CANCEL_DENIED", "ACCESS_CANCEL", "NO_ACTIVATION"])
        prov.cancel_activation = AsyncMock(side_effect=lambda _aid: next(responses))

        # 第 1 轮：仅调上游 1 次（绝不在同轮第二遍扫激活表再调一次），保持待重试
        assert await svc.reconcile_pending() == 0
        assert prov.cancel_activation.await_count == 1
        ord_r1 = await svc.get_order("ord_rc_1")
        assert ord_r1["status"] == "compensation_pending"
        assert ord_r1["activations"][0]["state"] == "refund_pending"

        # 第 2 轮：调上游 1 次得到 ACCESS_CANCEL -> 同步结清激活台账、订单与 Outbox
        assert await svc.reconcile_pending() == 1
        assert prov.cancel_activation.await_count == 2
        ord_r2 = await svc.get_order("ord_rc_1")
        assert ord_r2["status"] == "cancelled"
        assert ord_r2["refund_state"] == "confirmed"
        assert ord_r2["activations"][0]["state"] == "refund_confirmed"
        wh_rows = await db.webhooks.list(order_id="ord_rc_1")
        assert any(w["event_type"] == "order.cancelled" for w in wh_rows)

        # 第 3 轮：不再调用上游（绝不触发 NO_ACTIVATION 覆盖成 unrefundable）
        assert await svc.reconcile_pending() == 0
        assert prov.cancel_activation.await_count == 2
        ord_r3 = await svc.get_order("ord_rc_1")
        assert ord_r3["refund_state"] == "confirmed"
        assert ord_r3["activations"][0]["state"] == "refund_confirmed"

        # 场景 B: 模拟崩溃后激活台账已 refund_confirmed 但订单仍停在 compensation_pending
        await db.orders.create(
            order_id="ord_rc_cached",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="compensation_pending",
        )
        await db.orders.update("ord_rc_cached", activation_id="act_rc_cached", refund_state="pending")
        act_c = await db.activations.record_intent(
            order_id="ord_rc_cached", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )
        await db.activations.bind_acquired(act_c, activation_id="act_rc_cached", phone_raw="66811112222", state="refund_confirmed")

        prov.cancel_activation = AsyncMock(return_value="NO_ACTIVATION")
        assert await svc.reconcile_pending() == 1
        assert prov.cancel_activation.await_count == 0  # 直接复用持久化结算证据，零上游调用
        ord_cached = await svc.get_order("ord_rc_cached")
        assert ord_cached["status"] == "cancelled"
        assert ord_cached["refund_state"] == "confirmed"

        # 场景 C: 手动 cancel_order 与后台 reconcile_pending 并发竞争，上游仅被调用 1 次
        await db.orders.create(
            order_id="ord_rc_race",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="compensation_pending",
        )
        await db.orders.update("ord_rc_race", activation_id="act_rc_race", refund_state="pending")
        act_r = await db.activations.record_intent(
            order_id="ord_rc_race", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )
        await db.activations.bind_acquired(act_r, activation_id="act_rc_race", phone_raw="66833334444", state="refund_pending")

        async def slow_cancel(_aid):
            await asyncio.sleep(0.05)
            return "ACCESS_CANCEL"

        prov.cancel_activation = AsyncMock(side_effect=slow_cancel)
        await asyncio.gather(svc.cancel_order("ord_rc_race"), svc.reconcile_pending())
        assert prov.cancel_activation.await_count == 1
        ord_race = await svc.get_order("ord_rc_race")
        assert ord_race["status"] == "cancelled"
        assert ord_race["refund_state"] == "confirmed"
    finally:
        await svc.shutdown()
        await db.close()


# ── P1-02: v4 严格依据 order_activations 退款凭据校正历史误标，同时保留真实已确认退款 ──


@pytest.mark.asyncio
async def test_v3_p1_02_historical_unverified_refunds_corrected_and_real_confirmed_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v3-p1-02-secret")
    from app.config import env
    env.reload()

    # 1. 直接从旧 v1 升级到 v4（含 USER_CANCELLED 无激活与有激活两种历史取消）
    v1_file = str(tmp_path / "direct_v1.db")
    async with aiosqlite.connect(v1_file) as conn:
        await conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')), description TEXT NOT NULL DEFAULT '');
            CREATE TABLE sms_orders (
                order_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, project_id TEXT NOT NULL DEFAULT 'web-console',
                external_ref TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT 'smsbower',
                service TEXT NOT NULL DEFAULT 'whatsapp', service_code TEXT NOT NULL DEFAULT 'wa',
                country_id TEXT NOT NULL DEFAULT '52', country_label TEXT NOT NULL DEFAULT '',
                country_iso2 TEXT NOT NULL DEFAULT '', country_calling_code TEXT NOT NULL DEFAULT '',
                activation_id TEXT NOT NULL DEFAULT '', phone_raw TEXT NOT NULL DEFAULT '',
                phone_e164 TEXT NOT NULL DEFAULT '', phone_national TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'cancelled', latest_code TEXT NOT NULL DEFAULT '',
                code_scrubbed INTEGER NOT NULL DEFAULT 0, code_count INTEGER NOT NULL DEFAULT 0,
                max_price REAL, cost REAL, cost_is_estimated INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'USD', wait_timeout INTEGER NOT NULL DEFAULT 180,
                expires_at TEXT NOT NULL DEFAULT '', attempts_used INTEGER NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '',
                webhook_url TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO schema_version (version, description) VALUES (1, 'v1');
            INSERT INTO sms_orders (order_id, activation_id, status, error_code) VALUES ('ord_v1_no_act', '', 'cancelled', 'USER_CANCELLED');
            INSERT INTO sms_orders (order_id, activation_id, status, error_code) VALUES ('ord_v1_with_act', 'act_old_1', 'cancelled', 'USER_CANCELLED');
            """
        )
        await conn.commit()

    db_v1 = Database(v1_file)
    await db_v1.open()
    try:
        assert (await db_v1.orders.get("ord_v1_no_act"))["refund_state"] == "none"
        assert (await db_v1.orders.get("ord_v1_with_act"))["refund_state"] == "unknown"
        assert (await db_v1.orders.get_stats())["today"]["refund_confirmed_count"] == 0
    finally:
        await db_v1.close()

    # 2. 经旧 v2/v3 升级后残留的误标（USER_CANCELLED 却标为 confirmed 且无 order_activations 凭据）vs 真实已确认退款
    v3_file = str(tmp_path / "legacy_v3.db")
    db_seed = Database(v3_file)
    await db_seed.open()
    await db_seed.close()

    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(v3_file) as conn:
        # 回退版本号到 3，模拟刚走完旧 v2 -> 旧 v3 迁移的状态
        await conn.execute("DELETE FROM schema_version WHERE version >= 4")
        # 场景 2a: 旧 v2 误标的无激活 USER_CANCELLED
        await conn.execute(
            "INSERT INTO sms_orders (order_id, provider, service, service_code, country_id, activation_id, status, refund_state, error_code, created_at) "
            "VALUES ('ord_oldv2_no_act', 'smsbower', 'whatsapp', 'wa', '52', '', 'cancelled', 'confirmed', 'USER_CANCELLED', ?)",
            (now_iso,),
        )
        # 场景 2b: 旧 v2 误标的有激活但无 order_activations 退款凭据的 USER_CANCELLED
        await conn.execute(
            "INSERT INTO sms_orders (order_id, provider, service, service_code, country_id, activation_id, status, refund_state, error_code, created_at) "
            "VALUES ('ord_oldv2_no_proof', 'smsbower', 'whatsapp', 'wa', '52', 'act_no_proof', 'cancelled', 'confirmed', 'USER_CANCELLED', ?)",
            (now_iso,),
        )
        # 场景 2c: 真实 v2+ 已确认退款（存在匹配 activation_id 且 state='refund_confirmed' 的台账凭据）
        await conn.execute(
            "INSERT INTO sms_orders (order_id, provider, service, service_code, country_id, activation_id, status, refund_state, error_code, created_at) "
            "VALUES ('ord_real_confirmed', 'smsbower', 'whatsapp', 'wa', '52', 'act_real_ok', 'cancelled', 'confirmed', 'USER_CANCELLED', ?)",
            (now_iso,),
        )
        await conn.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, country_id, service_code, activation_id, state) "
            "VALUES ('ord_real_confirmed', 1, 'smsbower', '52', 'wa', 'act_real_ok', 'refund_confirmed')"
        )
        await conn.commit()

    db_v4 = Database(v3_file)
    await db_v4.open()
    try:
        assert (await db_v4.orders.get("ord_oldv2_no_act"))["refund_state"] == "none"
        assert (await db_v4.orders.get("ord_oldv2_no_proof"))["refund_state"] == "unknown"
        assert (await db_v4.orders.get("ord_real_confirmed"))["refund_state"] == "confirmed"
        stats = await db_v4.orders.get_stats()
        assert stats["today"]["refund_confirmed_count"] == 1
    finally:
        await db_v4.close()
