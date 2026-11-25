"""Schema v7 修复复核（REVIEW_V7_2026-11-25.md）全部 8 项问题 + 双路深度审计发现的专项回归测试。

覆盖：
- V7 P0-01 + 审查 Finding 8：脱敏后重发绝不把上游残留旧码当新码（无需存 HMAC 哈希，强依赖 WAIT 跳变），且查码途中脱敏不提前杀死轮询
- V7 P1-01 + 审查 Finding 2：5sim FINISH / 上游 CANCEL / EXPIRED / 写码失败均单事务原子落盘订单+激活+Outbox，中途失败整体回滚不分裂
- V7 P1-02：人工核销为 completed 原子推进父订单终态、与自动补偿互斥，下轮维护绝不发起取消
- V7 P1-03 + 审计 Finding 1：待核对币种与已核实统计分离；普通不带币种核销不清除待核对标记；5sim 显式识别响应币种并区分默认值；估算成本单核实退款绝不产生负净支出
- V7 P1-04：超时处理锁内复检最新 expires_at 与重发批次；平台停用走独立原因分支不误报 SMS_TIMEOUT
- V7 P1-05：whatsapp 与 custom:whatsapp 幂等摘要隔离；v4 自定义服务与改项目默认回调后原样重试均正确回放
- V7 P2-01：引用计数同键锁在 3 路重叠请求、第 1 路准备失败时仍完全串行
- 深度审计综合回归：5sim code 优先于 text / 5xx&空响应不确定购买；HeroSMS 取消结构化与 5xx 不确定购买；SSRF 拦截 100.100.100.200 与非法端口 422；
  管理员按 project_id 删除项目黑名单与 scope=project；/api/v1/health 多平台配置透出；retention_hours=0 取消即刻脱敏 + compensation_pending 脱敏 + TOCTOU 保护；
  acquiring 取消等待在途绑定并保留 purchasing 不确定证据；重启恢复不漏 uncertain 且同步单写 >=400 幂等快照；停用平台 complete/cancel 不卡中间态；
  v5->v6 迁移不覆写 idempotency_http_status=201 的 CODE_PERSIST_FAILED 订单。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.event_bus import EventBus
from app.core.order_service import AcquireOptions, OrderService
from app.core.url_guard import validate_webhook_url
from app.db import Database
from app.db.migrations import (
    API_KEYS_DDL,
    BASE_SCHEMA_SQL,
    ORDER_ACTIVATIONS_DDL,
    PROJECTS_DDL,
    SMS_MESSAGES_DDL,
    SMS_ORDERS_DDL,
    WEBHOOK_DELIVERIES_DDL,
)
from app.errors import AppError
from app.main import create_app
from app.models import PhoneInput
from app.providers.base import (
    Activation,
    BaseSmsProvider,
    ProviderCapabilities,
    PurchaseUncertainError,
)
from app.providers.five_sim import FiveSimProvider
from app.providers.hero_sms import HeroSmsProvider
from app.providers.registry import ProviderRegistry


def _make_provider(
    *,
    provider_id: str = "smsbower",
    supports_resend: bool = True,
    supports_exact_price_lock: bool = False,
) -> AsyncMock:
    prov = AsyncMock(spec=BaseSmsProvider)
    prov.provider_id = provider_id
    prov.capabilities = ProviderCapabilities(
        supports_resend=supports_resend,
        supports_price_lock=True,
        supports_custom_service=True,
        supports_exact_price_lock=supports_exact_price_lock,
    )
    prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
    prov.get_countries = AsyncMock(return_value=[{"id": 52, "name": "Thailand", "cc": "66", "iso": "th"}])
    prov.get_prices = AsyncMock(return_value=[{"cost": 0.15, "count": 10}])
    prov.get_balance = AsyncMock(return_value=20.0)
    prov.normalize_phone = AsyncMock(
        side_effect=lambda raw, cid: PhoneInput(
            raw_input=raw,
            e164_number=raw if raw.startswith("+") else f"+{raw}",
            country_iso2="TH",
            country_calling_code="66",
            phone=raw.lstrip("+"),
        )
    )
    prov.request_number = AsyncMock(
        return_value=Activation("act_v7_1", "66812345678", 52, "Thailand", provider=provider_id, price=0.20)
    )
    prov.get_sms_code = AsyncMock(return_value=("WAIT", ""))
    prov.cancel_activation = AsyncMock(return_value="ACCESS_CANCEL")
    prov.complete_activation = AsyncMock(return_value="ACCESS_ACTIVATION")
    prov.request_additional_sms = AsyncMock(return_value="ACCESS_RETRY_GET")
    prov.close = AsyncMock()
    return prov


# ── V7 P0-01 + 审查 Finding 8: 脱敏后重发绝不复活旧码，且查码途中脱敏不终止轮询 ──


@pytest.mark.asyncio
async def test_v7_p0_01_scrub_then_resend_blocks_stale_code_until_wait_and_mid_poll_scrub_keeps_polling(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v7-p0-01-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v7_p001.db"))
    await db.open()
    prov = _make_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    bus = EventBus()
    svc = OrderService(db, reg, bus, poll_intervals=(0.015, 0.015, 0.015))

    try:
        # 场景 A: 首条短信写入 -> 脱敏清理 -> 重发短信 -> 上游连续 4 次仍返回旧码 "111222" -> 再返回 WAIT -> 再返回新码 "999888"
        await db.orders.create(
            order_id="ord_resend_scrub",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            webhook_url="https://hooks.example.test/v7",
            status="waiting_code",
        )
        await db.orders.update(
            "ord_resend_scrub",
            activation_id="act_rs_1",
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        )
        await db.orders.append_code("ord_resend_scrub", "111222")
        assert await svc.scrub_order_code("ord_resend_scrub") is True

        stage = {"phase": "stale"}
        polls = {"n": 0}

        async def mock_get_sms(_aid):
            polls["n"] += 1
            if stage["phase"] == "stale":
                return ("OK", "111222")
            if stage["phase"] == "wait":
                stage["phase"] = "new"
                return ("WAIT", "")
            return ("OK", "999888")

        prov.get_sms_code = AsyncMock(side_effect=mock_get_sms)
        await svc.resend_sms("ord_resend_scrub")

        # 等多次轮询：上游一直返回旧码 "111222"，但未见 WAIT 绝不能写入或生成新 Webhook！
        for _ in range(30):
            if polls["n"] >= 4:
                break
            await asyncio.sleep(0.015)

        mid = await svc.get_order("ord_resend_scrub")
        assert mid["status"] == "waiting_next_code"
        assert mid["code_count"] == 1
        assert mid["code_scrubbed"] is True
        assert mid["latest_code"] == ""
        dlvs_mid = await db.webhooks.list(order_id="ord_resend_scrub")
        assert all(d["status"] == "scrubbed" for d in dlvs_mid)

        # 上游切到 WAIT 后真正送达新短信 "999888" -> 必须正常写入 seq=2 并生成待发 Webhook！
        stage["phase"] = "wait"
        for _ in range(50):
            done = await svc.get_order("ord_resend_scrub")
            if done["status"] == "code_received":
                break
            await asyncio.sleep(0.02)

        final_ord = await svc.get_order("ord_resend_scrub")
        assert final_ord["status"] == "code_received"
        assert final_ord["code_count"] == 2
        assert final_ord["latest_code"] == "999888"
        assert final_ord["code_scrubbed"] is False

        # 场景 B (审查 Finding 8): waiting_code 查码在途触发 scrub -> 丢弃过期旧码且轮询保持存活继续接新码！
        await db.orders.create(
            order_id="ord_mid_scrub",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_code",
        )
        await db.orders.update(
            "ord_mid_scrub",
            activation_id="act_ms_1",
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        )
        in_poll = asyncio.Event()
        release_poll = asyncio.Event()
        call_idx = {"n": 0}

        async def mid_scrub_sms(_aid):
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                in_poll.set()
                await release_poll.wait()
                return ("OK", "333444")
            if call_idx["n"] == 2:
                return ("WAIT", "")
            return ("OK", "555666")

        prov.get_sms_code = AsyncMock(side_effect=mid_scrub_sms)
        svc._start_code_poller("ord_mid_scrub")
        await asyncio.wait_for(in_poll.wait(), timeout=2.0)
        assert await svc.scrub_order_code("ord_mid_scrub") is True
        release_poll.set()

        for _ in range(60):
            cur_ms = await svc.get_order("ord_mid_scrub")
            if cur_ms["status"] == "code_received":
                break
            await asyncio.sleep(0.02)

        final_ms = await svc.get_order("ord_mid_scrub")
        assert final_ms["status"] == "code_received"
        assert final_ms["latest_code"] == "555666"
    finally:
        await svc.shutdown()
        await db.close()


# ── V7 P1-01 + 审查 Finding 2: 单事务原子提交父订单+激活台账+Outbox，第二步失败整体回滚 ──


@pytest.mark.asyncio
async def test_v7_p1_01_atomic_order_and_activation_commit_prevents_torn_reads_and_rollback_on_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v7-p1-01-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v7_p101.db"))
    await db.open()
    five = FiveSimProvider(api_key_getter=AsyncMock(return_value="k_5sim"))
    five._get = AsyncMock(return_value={"status": "FINISH", "sms": []})
    reg = ProviderRegistry(default="5sim")
    reg.init_provider("5sim", five)
    bus = EventBus()
    svc = OrderService(db, reg, bus, poll_intervals=(0.015, 0.015, 0.015))

    try:
        # 1. 注入激活写入暂停点：写事务提交前并发读取绝不能看到 status='expired' 但 activations=[]
        await db.orders.create(
            order_id="ord_atomic_fin",
            provider="5sim",
            service="whatsapp",
            service_code="whatsapp",
            country_id="russia",
            status="waiting_code",
        )
        await db.orders.update(
            "ord_atomic_fin",
            activation_id="fs_at_1",
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        )

        orig_ensure = db.activations.ensure_activation_state_in_tx
        in_tx_entered = asyncio.Event()
        release_tx = asyncio.Event()

        async def paused_ensure(raw, *args, **kwargs):
            in_tx_entered.set()
            await release_tx.wait()
            return await orig_ensure(raw, *args, **kwargs)

        monkeypatch.setattr(db.activations, "ensure_activation_state_in_tx", paused_ensure)

        order_snap = await svc.get_order("ord_atomic_fin")
        task = asyncio.create_task(svc._handle_provider_finished_in_poll(order_snap, "fs_at_1"))
        await asyncio.wait_for(in_tx_entered.wait(), timeout=2.0)

        # 事务尚未提交，并发读取必须仍看到提交前完整状态 (waiting_code, 0 activations)，绝无 torn read！
        mid_read = await svc.get_order("ord_atomic_fin")
        assert mid_read["status"] == "waiting_code"
        assert mid_read["activations"] == []

        release_tx.set()
        await task

        # 提交后立即同时可见 expired + 1 条 unrefundable 激活！
        after_read = await svc.get_order("ord_atomic_fin")
        assert after_read["status"] == "expired"
        assert len(after_read["activations"]) == 1
        assert after_read["activations"][0]["state"] == "unrefundable"

        # 2. 注入第二步（激活台账写入）抛异常 -> 整笔事务必须回滚，父订单保持 waiting_code 且不发 SSE！
        await db.orders.create(
            order_id="ord_atomic_rb",
            provider="5sim",
            service="whatsapp",
            service_code="whatsapp",
            country_id="russia",
            status="waiting_code",
        )
        await db.orders.update(
            "ord_atomic_rb",
            activation_id="fs_rb_1",
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        )
        monkeypatch.setattr(
            db.activations,
            "ensure_activation_state_in_tx",
            AsyncMock(side_effect=RuntimeError("injected activation write failure")),
        )
        pre_events = len(bus.replay_since(0))
        with pytest.raises(RuntimeError, match="injected activation write failure"):
            await svc._handle_provider_finished_in_poll(await svc.get_order("ord_atomic_rb"), "fs_rb_1")
        rb_ord = await svc.get_order("ord_atomic_rb")
        assert rb_ord["status"] == "waiting_code"
        assert rb_ord["activations"] == []
        assert len(bus.replay_since(0)) == pre_events
    finally:
        await svc.shutdown()
        await five.close()
        await db.close()


# ── V7 P1-02: 人工核销为 completed 原子推进父订单终态，下轮维护绝不发起上游取消 ──


@pytest.mark.asyncio
async def test_v7_p1_02_manual_resolve_completed_prevents_background_cancel_and_serializes_race(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v7-p1-02-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v7_p102.db"))
    await db.open()
    prov = _make_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc = OrderService(db, reg, EventBus())

    try:
        # 1. 待补偿订单人工结清为 completed -> 父订单同事务变为 completed，下轮 reconcile_pending 绝不调用 cancel_activation！
        await db.orders.create(
            order_id="ord_man_done",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="compensation_pending",
        )
        await db.orders.update("ord_man_done", activation_id="act_md_1", refund_state="pending")
        act_id = await db.activations.record_intent(
            order_id="ord_man_done", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )
        await db.activations.bind_acquired(
            act_id, activation_id="act_md_1", phone_raw="6681", actual_cost=0.25, currency="USD", state="refund_pending"
        )

        resolved = await svc.resolve_activation_manually(act_id, state="completed", note="人工核对已使用完毕")
        assert resolved["state"] == "completed"

        ord_after = await svc.get_order("ord_man_done")
        assert ord_after["status"] == "completed"
        assert ord_after["settlement_state"] == "manual_resolved"
        assert ord_after["refund_state"] == "none"

        prov.cancel_activation.reset_mock()
        assert await svc.reconcile_pending() == 0
        prov.cancel_activation.assert_not_awaited()

        ord_final = await svc.get_order("ord_man_done")
        assert ord_final["status"] == "completed"
        assert ord_final["activations"][0]["state"] == "completed"

        # 2. 并发竞争：人工核销与已开始的自动补偿通过同一订单锁+激活锁互斥，不存在父子状态分裂
        await db.orders.create(
            order_id="ord_race_man",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="compensation_pending",
        )
        await db.orders.update("ord_race_man", activation_id="act_rm_1", refund_state="pending")
        act_r = await db.activations.record_intent(
            order_id="ord_race_man", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )
        await db.activations.bind_acquired(
            act_r, activation_id="act_rm_1", phone_raw="6682", actual_cost=0.25, currency="USD", state="refund_pending"
        )

        in_cancel = asyncio.Event()
        release_cancel = asyncio.Event()

        async def slow_cancel(_aid):
            in_cancel.set()
            await release_cancel.wait()
            return "ACCESS_CANCEL"

        prov.cancel_activation = AsyncMock(side_effect=slow_cancel)
        rec_task = asyncio.create_task(svc.reconcile_pending())
        await asyncio.wait_for(in_cancel.wait(), timeout=2.0)

        man_task = asyncio.create_task(
            svc.resolve_activation_manually(act_r, state="completed", note="并发人工结清")
        )
        await asyncio.sleep(0.03)
        release_cancel.set()
        await rec_task

        # 补偿先确认退款后，等待锁的人工核销必须被 409 ACTIVATION_REFUND_IMMUTABLE 拒绝且父订单与激活保持一致！
        with pytest.raises(AppError) as ei:
            await man_task
        assert ei.value.status_code == 409
        assert ei.value.code == "ACTIVATION_REFUND_IMMUTABLE"
        race_ord = await svc.get_order("ord_race_man")
        assert race_ord["status"] == "cancelled"
        assert race_ord["refund_state"] == "confirmed"
        assert race_ord["activations"][0]["state"] == "refund_confirmed"
    finally:
        await svc.shutdown()
        await db.close()


# ── V7 P1-03 + 审计 Finding 1: 待核对币种统计隔离、普通核销保留标记、5sim 显式币种、估算退款绝不负净支出 ──


@pytest.mark.asyncio
async def test_v7_p1_03_and_estimated_refund_financial_accuracy(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v7-p1-03-secret")
    from app.config import env
    env.reload()

    # 1. 5sim 响应携带 RUB 时显式记录 RUB + currency_source='provider_response'；缺省 currency 时标 provider_default
    five = FiveSimProvider(api_key_getter=AsyncMock(return_value="k_5sim"))
    try:
        five._client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"id": 9001, "phone": "+79991112233", "price": 15.5, "currency": "RUB"},
                headers={"content-type": "application/json"},
            )
        )
        act_rub = await five.request_number("russia", "whatsapp")
        assert act_rub.currency == "RUB"
        assert act_rub.currency_source == "provider_response"

        five._client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"id": 9002, "phone": "+79991112234", "price": 0.45},
                headers={"content-type": "application/json"},
            )
        )
        act_def = await five.request_number("russia", "whatsapp")
        assert act_def.currency == "USD"
        assert act_def.currency_source == "provider_default"
    finally:
        await five.close()

    # 2. 待核对币种不计入已核实 by_currency；不提供币种的人工核销保留 currency_review_required=1；提供有效币种才清除
    db = Database(str(tmp_path / "v7_p103.db"))
    await db.open()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    svc = OrderService(db, reg, EventBus())
    try:
        await db.orders.create(
            order_id="ord_ccy_rev",
            project_id="p_ccy",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="completed",
        )
        await db.orders.update("ord_ccy_rev", currency="EUR")
        aid_row = await db.activations.record_intent(
            order_id="ord_ccy_rev", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )
        await db.activations.bind_acquired(
            aid_row, activation_id="act_cr_1", phone_raw="6681", actual_cost=1.0, currency="USD", state="completed"
        )
        await db.conn.write(
            "UPDATE order_activations SET currency_source='v5_unverified', currency_review_required=1 WHERE id=?",
            (aid_row,),
        )

        st1 = await db.orders.get_stats(project_id="p_ccy")
        assert st1["today"]["by_currency"] == []
        assert st1["today"]["currency_review_count"] == 1
        assert st1["today"]["actual_cost_total"] is None
        assert st1["today"]["currency_review_by_currency"][0]["currency"] == "USD"
        assert st1["today"]["currency_review_by_currency"][0]["actual_cost_total"] == 1.0

        # 不传币种的人工核销绝不能清除 currency_review_required！
        r_no_ccy = await svc.resolve_activation_manually(aid_row, state="completed", note="仅更新结算")
        assert bool(r_no_ccy["currency_review_required"]) is True
        assert r_no_ccy["currency_source"] == "v5_unverified"

        # 显式传入 UNKNOWN 也绝不能清除！
        r_unk = await svc.resolve_activation_manually(aid_row, state="completed", currency="UNKNOWN")
        assert bool(r_unk["currency_review_required"]) is True

        # 显式传入核实币种 EUR 才清除标记并进入已核实统计！
        r_eur = await svc.resolve_activation_manually(aid_row, state="completed", currency="EUR")
        assert bool(r_eur["currency_review_required"]) is False
        assert r_eur["currency"] == "EUR"
        assert r_eur["currency_source"] == "manual_resolve"

        st2 = await db.orders.get_stats(project_id="p_ccy")
        assert st2["today"]["currency_review_count"] == 0
        assert st2["today"]["actual_cost_total"] == 1.0
        assert st2["today"]["by_currency"][0]["currency"] == "EUR"

        # 3. 审计 Finding 1: SMSBower 估算成本单核实退款绝不产生负净支出或冲抵其他已完成订单！
        await db.orders.create(
            order_id="ord_sb_ok",
            project_id="p_sb",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="completed",
        )
        await db.orders.update("ord_sb_ok", currency="USD")
        a_ok = await db.activations.record_intent(
            order_id="ord_sb_ok", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )
        await db.activations.bind_acquired(
            a_ok,
            activation_id="act_sb_1",
            phone_raw="6681",
            estimated_cost=0.50,
            cost_status="estimated",
            currency="USD",
            state="completed",
        )
        await db.orders.create(
            order_id="ord_sb_ref",
            project_id="p_sb",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="compensation_pending",
        )
        await db.orders.update("ord_sb_ref", currency="USD")
        a_ref = await db.activations.record_intent(
            order_id="ord_sb_ref", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )
        await db.activations.bind_acquired(
            a_ref,
            activation_id="act_sb_2",
            phone_raw="6682",
            estimated_cost=0.50,
            cost_status="estimated",
            currency="USD",
            state="refund_pending",
        )
        await svc.resolve_activation_manually(a_ref, state="refund_confirmed", refund_amount=0.50)

        st_sb = await db.orders.get_stats(project_id="p_sb")
        assert st_sb["today"]["actual_cost_total"] == 0.0
        assert st_sb["today"]["refunded_amount_total"] == 0.50
        assert st_sb["today"]["net_actual_cost_total"] == 0.0
        assert st_sb["today"]["estimated_cost_total"] == 0.50
        assert st_sb["today"]["cost_total"] == 0.50
    finally:
        await svc.shutdown()
        await db.close()


# ── V7 P1-04: 超时处理锁内复核最新 expires_at，平台停用走独立分支 ──


@pytest.mark.asyncio
async def test_v7_p1_04_timeout_ignores_stale_snapshot_after_resend_and_separates_inactive_provider(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v7-p1-04-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v7_p104.db"))
    await db.open()
    prov = _make_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc = OrderService(db, reg, EventBus(), poll_intervals=(5.0, 5.0, 5.0))

    try:
        # 1. 旧快照已过期 -> 等锁期间并发重发把 expires_at 延长至未来 -> 释放锁后 _handle_poll_timeout 绝不能误判 RESEND_TIMEOUT！
        past = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
        await db.orders.create(
            order_id="ord_stale_to",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_next_code",
        )
        await db.orders.update("ord_stale_to", activation_id="act_st_1", code_count=1, expires_at=past)
        stale_snap = await svc.get_order("ord_stale_to")

        async with svc._lock_for("ord_stale_to"):
            # 在持有订单锁期间启动超时处理（挂起在锁上），同时把数据库截止时间延长到未来 3 分钟
            timeout_task = asyncio.create_task(
                svc._handle_poll_timeout(stale_snap, expected_expires_at=past)
            )
            await asyncio.sleep(0.02)
            future_exp = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
            await db.orders.update("ord_stale_to", expires_at=future_exp)

        await timeout_task
        cur = await svc.get_order("ord_stale_to")
        assert cur["status"] == "waiting_next_code"
        assert cur["error_code"] == ""

        # 2. 停用平台但尚未过期的 waiting_code 订单经 reconcile_pending 走 PROVIDER_NOT_ACTIVE，而非误报 SMS_TIMEOUT！
        await db.orders.create(
            order_id="ord_inact_prov",
            provider="5sim",
            service="whatsapp",
            service_code="whatsapp",
            country_id="russia",
            status="waiting_code",
        )
        await db.orders.update(
            "ord_inact_prov",
            activation_id="fs_inact_1",
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        await svc.reconcile_pending()
        inact = await svc.get_order("ord_inact_prov")
        assert inact["status"] == "compensation_pending"
        assert inact["error_code"] == "PROVIDER_NOT_ACTIVE"
    finally:
        await svc.shutdown()
        await db.close()


# ── V7 P1-05 & P2-01: custom 服务码摘要隔离、v4 历史自定义/默认回调变更重放、3 路并发同键准备锁 ──


@pytest.mark.asyncio
async def test_v7_p1_05_and_p2_01_custom_service_v4_replay_and_refcounted_idem_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v7-p1-05-secret")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "v7-admin-pass")
    monkeypatch.delenv("WEBHOOK_ALLOWED_HOSTS", raising=False)
    from app.config import env
    env.reload()

    db_path = str(tmp_path / "bridge.db")
    # 预先构造两笔 v4 历史订单：1) custom:whatsapp 异步单；2) 未显式指定 webhook_url（继承了旧默认回调）的异步单
    h_custom = OrderService._build_legacy_hash_v1(
        existing={"provider": "smsbower", "webhook_url": ""},
        canonical_svc="whatsapp",
        raw_service="custom:whatsapp",
        country_input="Thailand",
        country_fallback=[],
        max_price=None,
        opts=AcquireOptions(),
        wait_timeout=180,
        skip_blacklisted=True,
        async_acquire=True,
        webhook_url="",
        explicit_webhook_url="",
    )
    h_default_wh = OrderService._build_legacy_hash_v1(
        existing={"provider": "smsbower", "webhook_url": "https://old.hooks.test/base"},
        canonical_svc="whatsapp",
        raw_service="whatsapp",
        country_input="Thailand",
        country_fallback=[],
        max_price=None,
        opts=AcquireOptions(),
        wait_timeout=180,
        skip_blacklisted=True,
        async_acquire=True,
        webhook_url="https://old.hooks.test/base",
        explicit_webhook_url="",
    )
    async with aiosqlite.connect(db_path) as raw:
        await raw.executescript(BASE_SCHEMA_SQL)
        await raw.execute(SMS_ORDERS_DDL)
        await raw.execute(SMS_MESSAGES_DDL)
        await raw.execute(ORDER_ACTIVATIONS_DDL)
        await raw.execute(WEBHOOK_DELIVERIES_DDL)
        await raw.execute(API_KEYS_DDL)
        for v in (1, 2, 3, 4):
            await raw.execute("INSERT INTO schema_version (version, description) VALUES (?, 'v4')", (v,))
        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, idempotency_key, request_hash, provider, service, "
            "service_code, country_id, status) VALUES ('ord_v4_custom', 'web-console', 'k-v4-custom', ?, "
            "'smsbower', 'whatsapp', 'whatsapp', '52', 'failed')",
            (h_custom,),
        )
        from app.core.auth_service import hash_api_key

        await raw.execute(
            "INSERT INTO api_keys (project_id, name, key_hash, key_prefix, scopes, default_webhook_url, enabled) "
            "VALUES ('p_v4wh', 'V4WH', ?, 'sk_v4wh', 'orders:rw,orders:r', 'https://old.hooks.test/base', 1)",
            (hash_api_key("sk_v4wh_test_key"),),
        )
        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, idempotency_key, request_hash, provider, service, "
            "service_code, country_id, webhook_url, status) VALUES ('ord_v4_defwh', 'p_v4wh', 'k-v4-defwh', ?, "
            "'smsbower', 'whatsapp', 'wa', '52', 'https://old.hooks.test/base', 'completed')",
            (h_default_wh,),
        )
        await raw.commit()

    monkeypatch.setenv("BRIDGE_DB_PATH", db_path)
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    env.reload()

    app = create_app()
    async with app.router.lifespan_context(app):
        prov = _make_provider()
        app.state.container.registry.init_provider("smsbower", prov)

        import socket as _socket

        loop = asyncio.get_running_loop()
        orig_gai = loop.getaddrinfo

        async def fake_gai(host, port, *args, **kwargs):
            if host in ("old.hooks.test", "new.hooks.test"):
                return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", int(port or 443)))]
            return await orig_gai(host, port, *args, **kwargs)

        monkeypatch.setattr(loop, "getaddrinfo", fake_gai)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as admin, AsyncClient(
            transport=transport, base_url="http://test"
        ) as ext:
            assert (await admin.post("/api/v1/auth/login", json={"password": "v7-admin-pass"})).status_code == 200

            # 1. 新单 whatsapp 与 custom:whatsapp 用同一 Idempotency-Key 必须 409 冲突（绝不误返第一单）！
            r1 = await admin.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "k-custom-diff"},
                json={"service": "whatsapp", "country": "Thailand"},
            )
            assert r1.status_code == 201 and r1.json()["service_code"] == "wa"
            r2 = await admin.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "k-custom-diff"},
                json={"service": "custom:whatsapp", "country": "Thailand"},
            )
            assert r2.status_code == 409 and r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            # 2. v4 历史 custom:whatsapp 原样重试 -> 精确回放 202 + ord_v4_custom！
            rv4_c = await admin.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "k-v4-custom"},
                json={"service": "custom:whatsapp", "country": "Thailand", "async_acquire": True},
            )
            assert rv4_c.status_code == 202 and rv4_c.json()["order_id"] == "ord_v4_custom"

            # 3. v4 历史默认回调订单：管理员将项目默认回调改为 new.hooks.test 后，
            #    不传 webhook_url 原样重放仍返 202（绝不因默认回调变化报 409）！
            upd_v4 = await admin.patch(
                "/api/v1/projects/keys/p_v4wh",
                json={"default_webhook_url": "https://new.hooks.test/base"},
            )
            assert upd_v4.status_code == 200
            rv4_w = await ext.post(
                "/api/v1/orders",
                headers={"X-API-Key": "sk_v4wh_test_key", "Idempotency-Key": "k-v4-defwh"},
                json={"service": "whatsapp", "country": "Thailand", "async_acquire": True},
            )
            assert rv4_w.status_code == 202 and rv4_w.json()["order_id"] == "ord_v4_defwh"

            # 4. V7 P2-01: 3 路同键重叠请求，第 1 路国家解析失败，第 2/3 路已排队 -> 第 2 路成功落盘，第 3 路命中回放，
            #    总共仅调用 2 次国家解析（绝不出现第 3 路绕过锁并发执行导致 3 次国家解析）！
            resolve_hits = {"n": 0}
            first_started = asyncio.Event()
            release_first = asyncio.Event()

            async def flaky_first_resolve(_c):
                resolve_hits["n"] += 1
                if resolve_hits["n"] == 1:
                    first_started.set()
                    await release_first.wait()
                    raise RuntimeError("transient country API failure #1")
                await asyncio.sleep(0.04)
                return (52, "Thailand")

            prov.resolve_country = AsyncMock(side_effect=flaky_first_resolve)
            prov.request_number = AsyncMock(
                return_value=Activation("act_3way", "66877772222", 52, "Thailand", provider="smsbower", price=0.15)
            )
            t1 = asyncio.create_task(
                admin.post(
                    "/api/v1/orders",
                    headers={"Idempotency-Key": "k-3way-lock"},
                    json={"service": "whatsapp", "country": "Thailand"},
                )
            )
            await asyncio.wait_for(first_started.wait(), timeout=2.0)
            t2 = asyncio.create_task(
                admin.post(
                    "/api/v1/orders",
                    headers={"Idempotency-Key": "k-3way-lock"},
                    json={"service": "whatsapp", "country": "Thailand"},
                )
            )
            t3 = asyncio.create_task(
                admin.post(
                    "/api/v1/orders",
                    headers={"Idempotency-Key": "k-3way-lock"},
                    json={"service": "whatsapp", "country": "Thailand"},
                )
            )
            await asyncio.sleep(0.03)
            release_first.set()
            res1, res2, res3 = await asyncio.gather(t1, t2, t3)
            assert res1.status_code == 502
            assert res2.status_code == 201 and res3.status_code == 201
            assert res2.json()["order_id"] == res3.json()["order_id"]
            assert resolve_hits["n"] == 2
            assert prov.request_number.await_count == 1
            assert ("web-console", "k-3way-lock") not in app.state.container.order_service._idem_locks


# ── 双路深度审计综合回归：Provider/SSRF/Blacklist/Health/Retention/Cancel/Startup/Migration ──


@pytest.mark.asyncio
async def test_v7_audit_providers_ssrf_blacklist_health_retention_cancel_startup_and_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v7-audit-secret")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "v7-audit-admin")
    monkeypatch.delenv("WEBHOOK_ALLOWED_HOSTS", raising=False)
    from app.config import env
    env.reload()

    # 1. 5sim: sms["code"] 优先于 sms["text"]；HTTP 502 / 空 200 均抛 PurchaseUncertainError
    five = FiveSimProvider(api_key_getter=AsyncMock(return_value="k_5sim"))
    try:
        five._get = AsyncMock(
            return_value={
                "status": "RECEIVED",
                "sms": [{"text": "Your code is 482-910. Valid for 10 minutes.", "code": "482910"}],
            }
        )
        assert await five.get_sms_code("fs_code") == ("OK", "482910")

        five._client.get = AsyncMock(return_value=httpx.Response(502, text="Bad Gateway"))
        with pytest.raises(PurchaseUncertainError):
            await five.request_number("russia", "whatsapp")

        five._client.get = AsyncMock(return_value=httpx.Response(200, text="", headers={"content-type": "text/plain"}))
        with pytest.raises(PurchaseUncertainError):
            await five.request_number("russia", "whatsapp")
    finally:
        await five.close()

    # 2. HeroSMS: ACCESS_READY / NO_ACTIVATION 结构化取消立即结清为 unrefundable；HTTP 502 抛 PurchaseUncertainError
    hero = HeroSmsProvider(api_key_getter=AsyncMock(return_value="k_hero"))
    try:
        hero._get = AsyncMock(return_value="ACCESS_READY")
        o_ready = await hero.cancel_activation_structured("h1")
        assert o_ready.settled is True and o_ready.refund_confirmed is False

        hero._get = AsyncMock(return_value="NO_ACTIVATION")
        o_no = await hero.cancel_activation_structured("h2")
        assert o_no.settled is True and o_no.refund_confirmed is False

        hero._client.get = AsyncMock(return_value=httpx.Response(502, text="Bad Gateway"))
        hero._get = HeroSmsProvider._get.__get__(hero, HeroSmsProvider)
        with pytest.raises(PurchaseUncertainError):
            await hero.request_number(0, "wa")
    finally:
        await hero.close()

    # 3. SSRF 拦截 RFC 6598 (100.100.100.200 & ::ffff:100.100.100.200)，非法端口返回 422 而非 500
    for bad_url in (
        "https://100.100.100.200/hook",
        "https://[::ffff:100.100.100.200]/hook",
        "https://example.com:99999/hook",
    ):
        with pytest.raises(AppError) as ei:
            validate_webhook_url(bad_url, resolve_dns=False)
        assert ei.value.status_code == 422

    # 4. v5 -> v6 迁移不覆写 idempotency_http_status=201 的 CODE_PERSIST_FAILED 订单
    mig_path = str(tmp_path / "v5_persist_fail.db")
    async with aiosqlite.connect(mig_path) as raw:
        await raw.executescript(BASE_SCHEMA_SQL)
        await raw.execute(SMS_ORDERS_DDL)
        await raw.execute(SMS_MESSAGES_DDL)
        await raw.execute(ORDER_ACTIVATIONS_DDL)
        await raw.execute(WEBHOOK_DELIVERIES_DDL)
        await raw.execute(API_KEYS_DDL)
        await raw.execute(PROJECTS_DDL)
        for sql in (
            "ALTER TABLE sms_orders ADD COLUMN request_hash_version INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE sms_orders ADD COLUMN country_input TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sms_orders ADD COLUMN idempotency_http_status INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sms_orders ADD COLUMN idempotency_retryable INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sms_orders ADD COLUMN async_acquire INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE order_activations ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'",
            "ALTER TABLE order_activations ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE order_activations ADD COLUMN estimated_cost REAL",
            "ALTER TABLE order_activations ADD COLUMN refund_amount REAL",
        ):
            await raw.execute(sql)
        for v in (1, 2, 3, 4, 5):
            await raw.execute("INSERT INTO schema_version (version, description) VALUES (?, 'v5')", (v,))
        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, idempotency_key, status, error_code, "
            "idempotency_http_status, async_acquire) VALUES ('ord_201_cpf', 'web-console', 'k201', "
            "'failed', 'CODE_PERSIST_FAILED', 201, 0)"
        )
        await raw.commit()

    db_m = Database(mig_path)
    await db_m.open()
    try:
        migrated_ord = await db_m.orders.get("ord_201_cpf")
        assert migrated_ord is not None
        assert migrated_ord["idempotency_http_status"] == 201
        assert migrated_ord["idempotency_error_code"] == ""
    finally:
        await db_m.close()

    # 5. HTTP 集成：管理员按 project_id 删除项目黑名单、scope=project 正确过滤、/health 报告多平台配置
    monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "v7_api.db"))
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ACTIVE_PROVIDERS", "smsbower,5sim")
    monkeypatch.setenv("DEFAULT_PROVIDER", "5sim")
    env.reload()

    app = create_app()
    async with app.router.lifespan_context(app):
        db = app.state.container.db
        svc = app.state.container.order_service
        await db.config.set("five_sim_api_key", "test-5sim-key", encrypt=True)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            assert (await admin.post("/api/v1/auth/login", json={"password": "v7-audit-admin"})).status_code == 200

            h = (await admin.get("/api/v1/health")).json()
            assert h["default_provider"] == "5sim"
            assert h["provider_configured"]["5sim"] is True
            assert h["provider_configured"]["smsbower"] is False

            # 项目黑名单查询与管理员精准删除
            await db.blacklist.add("+66810001111", reason="proj", provider="smsbower", project_id="proj_a")
            await db.blacklist.add("+66810002222", reason="glob", provider="smsbower", project_id="")
            proj_bl = (await admin.get("/api/v1/blacklist?provider=smsbower&scope=project")).json()
            assert [e["phone"] for e in proj_bl["entries"]] == ["+66810001111"]

            del_res = (
                await admin.delete("/api/v1/blacklist?phone=%2B66810001111&provider=smsbower&project_id=proj_a")
            ).json()
            assert del_res["deleted"] == 1
            after_bl = (await admin.get("/api/v1/blacklist?provider=smsbower")).json()
            assert [e["phone"] for e in after_bl["entries"]] == ["+66810002222"]

            # retention_hours=0 取消不可退订单立即脱敏且 SSE 不泄明文；compensation_pending 也纳入留存清理
            await db.config.set("sms_code_retention_hours", "0")
            prov_sb = _make_provider(provider_id="smsbower")
            prov_sb.cancel_activation = AsyncMock(return_value="ACCESS_ACTIVATION")
            app.state.container.registry.init_provider("smsbower", prov_sb)

            await db.orders.create(
                order_id="ord_ret0_cancel",
                provider="smsbower",
                service="whatsapp",
                service_code="wa",
                country_id="52",
                status="waiting_code",
            )
            await db.orders.update("ord_ret0_cancel", activation_id="act_r0_1")
            await db.orders.append_code("ord_ret0_cancel", "778899")
            cancelled = await svc.cancel_order("ord_ret0_cancel")
            assert cancelled["code_scrubbed"] is True
            assert cancelled["latest_code"] == ""
            assert all("778899" not in str(ev.to_payload()) for ev in app.state.container.event_bus.replay_since(0))

            # compensation_pending 持码滞留订单也会被 scrub_expired_codes(0) 清理
            await db.orders.create(
                order_id="ord_comp_scrub",
                provider="smsbower",
                service="whatsapp",
                service_code="wa",
                country_id="52",
                status="waiting_code",
            )
            await db.orders.append_code("ord_comp_scrub", "445566")
            await db.orders.update("ord_comp_scrub", status="compensation_pending")
            scrubbed_cnt = await db.orders.scrub_expired_codes(0)
            assert scrubbed_cnt >= 1
            assert (await svc.get_order("ord_comp_scrub"))["code_scrubbed"] is True

            # 停用平台 complete_order / cancel_order 不应卡在 completing/cancelling，应直接返回 400 PROVIDER_NOT_ACTIVE
            await db.orders.create(
                order_id="ord_dis_hero",
                provider="herosms",
                service="whatsapp",
                service_code="wa",
                country_id="0",
                status="code_received",
            )
            await db.orders.update("ord_dis_hero", activation_id="h_dis_1")
            with pytest.raises(AppError) as ei_c:
                await svc.complete_order("ord_dis_hero")
            assert ei_c.value.status_code == 400 and ei_c.value.code == "PROVIDER_NOT_ACTIVE"
            assert (await svc.get_order("ord_dis_hero"))["status"] == "code_received"

            with pytest.raises(AppError) as ei_x:
                await svc.cancel_order("ord_dis_hero")
            assert ei_x.value.status_code == 400 and ei_x.value.code == "PROVIDER_NOT_ACTIVE"
            assert (await svc.get_order("ord_dis_hero"))["status"] == "code_received"

            # 重启恢复：Attempt 1 黑名单已退款 + Attempt 2 处于 purchasing -> 绝不能忽略 uncertain_purchase，且同步单写入 >=400 幂等快照！
            await db.orders.create(
                order_id="ord_rec_mix",
                provider="smsbower",
                service="whatsapp",
                service_code="wa",
                country_id="52",
                idempotency_key="k-rec-mix",
                status="acquiring",
            )
            a1 = await db.activations.record_intent(
                order_id="ord_rec_mix", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
            )
            await db.activations.bind_acquired(
                a1, activation_id="act_bl_prev", phone_raw="6681", state="refund_confirmed"
            )
            a2 = await db.activations.record_intent(
                order_id="ord_rec_mix", attempt_no=2, provider="smsbower", country_id="52", service_code="wa"
            )
            await svc.recover_on_startup()
            rec_mix = await svc.get_order("ord_rec_mix")
            assert rec_mix["status"] == "failed"
            assert rec_mix["settlement_state"] == "uncertain"
            assert rec_mix["idempotency_http_status"] == 502
            a2_row = await db.activations.get(a2)
            assert a2_row is not None and a2_row["state"] == "uncertain_purchase"
