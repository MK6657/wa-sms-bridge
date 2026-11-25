"""针对 REVIEW_V6_2026-11-25.md 全部 11 项问题（1 P0 / 6 P1 / 4 P2，Schema v7）的专项回归测试套件。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import httpx
import pytest

import app.db as db_pkg
from app.core.event_bus import EventBus
from app.core.order_service import AcquireOptions, OrderService
from app.core.retention_service import RetentionService
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
from app.errors import ConflictError
from app.main import create_app
from app.models import PhoneInput
from app.providers.base import Activation, CancelOutcome, ProviderCapabilities, SmsExpiredError, SmsProviderError
from app.providers.five_sim import FiveSimProvider
from app.providers.registry import ProviderRegistry


def _make_provider(
    *,
    provider_id: str = "smsbower",
    phone: str = "66812345678",
    activation_id: str = "act_v6_1",
    price: float = 0.25,
    currency: str = "USD",
):
    prov = MagicMock()
    prov.provider_id = provider_id
    prov.capabilities = ProviderCapabilities(
        supports_resend=True,
        supports_price_lock=True,
        supports_exact_price_lock=False,
    )
    prov.get_balance = AsyncMock(return_value=10.0)
    prov.get_countries = AsyncMock(return_value=[
        {"id": 52, "name": "Thailand", "name_zh": "泰国", "cc": "66", "iso": "th"},
    ])
    prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
    prov.get_prices = AsyncMock(return_value=[{"cost": price, "count": 5}])
    prov.request_number = AsyncMock(
        return_value=Activation(
            activation_id,
            phone,
            52,
            "Thailand",
            provider=provider_id,
            price=price,
            price_is_estimated=False,
            currency=currency,
        )
    )
    prov.normalize_phone = AsyncMock(
        return_value=PhoneInput(
            region="TH",
            phone=phone[2:],
            e164_number=f"+{phone}",
            country_calling_code="66",
            country_iso2="th",
        )
    )
    prov.get_sms_code = AsyncMock(return_value=("WAIT", ""))
    prov.complete_activation = AsyncMock(return_value="ACCESS_ACTIVATION")
    prov.request_additional_sms = AsyncMock(return_value="ACCESS_RETRY_GET")
    prov.cancel_activation = AsyncMock(return_value="ACCESS_CANCEL")
    prov.close = AsyncMock()
    return prov


# ── P0-01: 脱敏版本号门控封堵提交后延迟 SSE 与内存待重试回写，同时允许清理后新短信正常送达 ──


@pytest.mark.asyncio
async def test_v6_p0_01_scrub_version_blocks_delayed_sse_memory_retry_and_allows_new_sms(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v6-p0-01-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v6_p001.db"))
    await db.open()
    prov = _make_provider(activation_id="act_scrub_race_1")
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    bus = EventBus()
    svc = OrderService(db, reg, bus, poll_intervals=(0.02, 0.02, 0.02), max_code_write_retries=3)

    try:
        # 场景 A: 短信写入已提交但在回读结果时暂停 -> 此时脱敏 -> 恢复后绝不能推送含旧码的 SSE (含 extra.message.code)
        real_get = db.orders.get
        paused_after_commit = asyncio.Event()
        resume_after_scrub = asyncio.Event()
        pause_armed = {"armed": False}

        async def pausing_get(order_id, *, include_messages=True, include_activations=False):
            if pause_armed["armed"] and include_messages and include_activations:
                pause_armed["armed"] = False
                paused_after_commit.set()
                await resume_after_scrub.wait()
            return await real_get(order_id, include_messages=include_messages, include_activations=include_activations)

        monkeypatch.setattr(db.orders, "get", pausing_get)

        prov.get_sms_code = AsyncMock(return_value=("WAIT", ""))
        ord_a = await svc.create_order(country="Thailand", service="whatsapp")
        oid_a = ord_a["order_id"]

        async with bus.subscribe(order_id=oid_a) as sse_q:
            pause_armed["armed"] = True
            prov.get_sms_code = AsyncMock(return_value=("OK", "112233"))
            await asyncio.wait_for(paused_after_commit.wait(), timeout=2.0)

            # 在提交后、回读/SSE 发布前执行脱敏清理
            assert await svc.scrub_order_code(oid_a) is True
            resume_after_scrub.set()
            await asyncio.sleep(0.08)

            # 检查所有队列中与历史环中的事件：绝不可出现旧码 112233（无论在 latest_code、messages 还是 message 中）
            seen_payloads = []
            while not sse_q.empty():
                seen_payloads.append(sse_q.get_nowait().to_payload())
            for item in seen_payloads:
                assert "112233" not in str(item)

            # 即使外部拿着旧版本号直接调用 bus.publish，EventBus 也会立即擦除旧码
            stale_evt = bus.publish(
                "order.code_received",
                {"order_id": oid_a, "latest_code": "112233", "message": {"code": "112233", "raw_text": "112233"}},
                order_id=oid_a,
                code_scrub_version=0,
            )
            assert "112233" not in str(stale_evt.to_payload())

        monkeypatch.setattr(db.orders, "get", real_get)

        # 场景 B: 首次写码失败，旧码留在内存等待重试 -> 脱敏清理 -> 下次重试绝不可把旧码写回库或入队 Webhook
        prov.request_number = AsyncMock(
            return_value=Activation("act_scrub_retry", "66899991111", 52, "Thailand", provider="smsbower", price=0.20)
        )
        poll_calls = {"count": 0}

        async def poll_once_then_wait(_aid):
            poll_calls["count"] += 1
            if poll_calls["count"] == 1:
                return ("OK", "445566")
            return ("WAIT", "")

        prov.get_sms_code = AsyncMock(side_effect=poll_once_then_wait)

        real_append = db.orders.append_code
        first_write_failed = asyncio.Event()
        allow_next_iteration = asyncio.Event()
        fail_once = {"flag": True}

        async def fail_first_append(*args, **kwargs):
            if fail_once["flag"]:
                fail_once["flag"] = False
                first_write_failed.set()
                await allow_next_iteration.wait()
                raise RuntimeError("transient sqlite busy")
            return await real_append(*args, **kwargs)

        monkeypatch.setattr(db.orders, "append_code", fail_first_append)

        ord_b = await svc.create_order(
            country="Thailand",
            service="whatsapp",
            webhook_url="https://hooks.partner.internal/cb",
        )
        oid_b = ord_b["order_id"]

        await asyncio.wait_for(first_write_failed.wait(), timeout=2.0)
        # 在首次写码失败进入内存待重试前/后完成脱敏
        assert await svc.scrub_order_code(oid_b) is True
        allow_next_iteration.set()
        await asyncio.sleep(0.12)

        after_b = await svc.get_order(oid_b)
        assert after_b["code_scrubbed"] is True
        assert after_b["latest_code"] == ""
        assert after_b["code_count"] == 0
        wh_b = await db.webhooks.list(order_id=oid_b)
        assert not any(w["event_type"] == "order.code_received" for w in wh_b)

        # 场景 C: 清理后确实收到一条新短信（版本号为清理后的最新版本）-> 仍可正常写入并送达 SSE 与 Webhook！
        monkeypatch.setattr(db.orders, "append_code", real_append)
        async with bus.subscribe(order_id=oid_b) as sse_new:
            prov.get_sms_code = AsyncMock(return_value=("OK", "778899"))
            delivered = await svc.wait_for_code(oid_b, timeout=3)
            assert delivered["status"] == "code_received"
            assert delivered["code_scrubbed"] is False
            assert delivered["latest_code"] == "778899"
            assert delivered["code_count"] == 1

            ev_new = await asyncio.wait_for(sse_new.get(), timeout=2.0)
            payload_new = ev_new.to_payload()
            assert payload_new["data"]["latest_code"] == "778899"
            assert payload_new["data"]["message"]["code"] == "778899"
            wh_new = await db.webhooks.list(order_id=oid_b)
            assert any(w["event_type"] == "order.code_received" and w["status"] == "pending" for w in wh_new)
    finally:
        await svc.shutdown()
        await db.close()


# ── P1-01: 恢复已过期的 completing 订单绝不死锁，维护循环继续推进其他补偿与自动脱敏 ──


@pytest.mark.asyncio
async def test_v6_p1_01_expired_completing_recovery_does_not_deadlock_maintenance_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v6-p1-01-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v6_p101.db"))
    await db.open()
    prov = _make_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc = OrderService(db, reg, EventBus(), poll_intervals=(5.0, 5.0, 5.0))
    ret = RetentionService(db, svc, reconcile_interval=0.05, scrub_every_n_ticks=1)

    try:
        past_exp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        old_done = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()

        # 1. 已过期且无短信的 completing 订单（此前会在 _recover_stranded_completing 内重入 _lock_for 死锁！）
        await db.orders.create(
            order_id="ord_deadlock_comp",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="completing",
        )
        await db.orders.update("ord_deadlock_comp", activation_id="act_dl_comp", expires_at=past_exp)
        a1 = await db.activations.record_intent(order_id="ord_deadlock_comp", attempt_no=1, provider="smsbower", country_id="52", service_code="wa")
        await db.activations.bind_acquired(a1, activation_id="act_dl_comp", phone_raw="6681", actual_cost=0.10, currency="USD", state="active")

        # 2. 另一笔待补偿订单
        await db.orders.create(
            order_id="ord_other_comp",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="compensation_pending",
        )
        await db.orders.update("ord_other_comp", activation_id="act_other_comp", refund_state="pending")
        a2 = await db.activations.record_intent(order_id="ord_other_comp", attempt_no=1, provider="smsbower", country_id="52", service_code="wa")
        await db.activations.bind_acquired(a2, activation_id="act_other_comp", phone_raw="6682", actual_cost=0.15, currency="USD", state="refund_pending")

        # 3. 一笔到期需要自动脱敏的历史订单
        await db.orders.create(
            order_id="ord_need_scrub",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_code",
        )
        await db.orders.append_code("ord_need_scrub", "555666")
        await db.orders.update("ord_need_scrub", status="completed", completed_at=old_done)

        # 启动真实维护循环，必须在短时间内完成全部三项而不卡死！
        ret.start()
        for _ in range(60):
            o1 = await svc.get_order("ord_deadlock_comp")
            o2 = await svc.get_order("ord_other_comp")
            o3 = await svc.get_order("ord_need_scrub")
            if o1["status"] == "expired" and o2["status"] == "cancelled" and o3["code_scrubbed"]:
                break
            await asyncio.sleep(0.05)

        o1 = await svc.get_order("ord_deadlock_comp")
        o2 = await svc.get_order("ord_other_comp")
        o3 = await svc.get_order("ord_need_scrub")
        assert o1["status"] == "expired" and o1["refund_state"] == "confirmed"
        assert o2["status"] == "cancelled" and o2["refund_state"] == "confirmed"
        assert o3["code_scrubbed"] is True and o3["latest_code"] == ""
        prov.cancel_activation.assert_any_await("act_dl_comp")
        prov.cancel_activation.assert_any_await("act_other_comp")
    finally:
        await ret.shutdown()
        await svc.shutdown()
        await db.close()


# ── P1-02: 不同平台的同名 activation_id 互不阻塞，同平台同激活单轮仅尝试一次 ──


@pytest.mark.asyncio
async def test_v6_p1_02_same_activation_id_across_providers_does_not_block_refund(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v6-p1-02-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v6_p102.db"))
    await db.open()
    prov_bower = _make_provider(provider_id="smsbower")
    prov_5sim = _make_provider(provider_id="5sim")
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov_bower)
    svc = OrderService(db, reg, EventBus(), poll_intervals=(5.0, 5.0, 5.0))

    try:
        # 场景 A: 停用的 5sim 旧单（更早创建）与当前启用的 smsbower 新单使用相同 activation_id="dup_100"
        await db.orders.create(
            order_id="ord_5sim_old",
            provider="5sim",
            service="whatsapp",
            service_code="whatsapp",
            country_id="russia",
            status="compensation_pending",
        )
        await db.orders.update("ord_5sim_old", activation_id="dup_100", refund_state="pending")
        await db.orders.create(
            order_id="ord_bower_new",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="compensation_pending",
        )
        await db.orders.update("ord_bower_new", activation_id="dup_100", refund_state="pending")

        assert await svc.reconcile_pending() == 1
        prov_bower.cancel_activation.assert_awaited_once_with("dup_100")
        assert (await svc.get_order("ord_bower_new"))["status"] == "cancelled"
        assert (await svc.get_order("ord_5sim_old"))["status"] == "compensation_pending"

        # 场景 B: 两个平台同时启用，拥有相同 activation_id="dup_200" -> 双方各自成功退款，互不阻挡
        reg.init_provider("5sim", prov_5sim)
        prov_bower.cancel_activation = AsyncMock(return_value="ACCESS_CANCEL")
        prov_5sim.cancel_activation = AsyncMock(return_value="CANCELED")

        await db.orders.create(
            order_id="ord_5sim_b", provider="5sim", service="whatsapp", service_code="whatsapp", country_id="russia", status="compensation_pending"
        )
        await db.orders.update("ord_5sim_b", activation_id="dup_200", refund_state="pending")
        await db.orders.create(
            order_id="ord_bower_b", provider="smsbower", service="whatsapp", service_code="wa", country_id="52", status="compensation_pending"
        )
        await db.orders.update("ord_bower_b", activation_id="dup_200", refund_state="pending")

        assert await svc.reconcile_pending() >= 2
        prov_bower.cancel_activation.assert_awaited_once_with("dup_200")
        prov_5sim.cancel_activation.assert_any_await("dup_200")
        assert (await svc.get_order("ord_5sim_b"))["status"] == "cancelled"
        assert (await svc.get_order("ord_bower_b"))["status"] == "cancelled"
    finally:
        await svc.shutdown()
        await db.close()


# ── P1-03: 旧订单补建台账同步保留原实付（净支出 0.20 非 -0.10），人工核销原子一致拒绝覆盖已确认退款 ──


@pytest.mark.asyncio
async def test_v6_p1_03_legacy_order_cost_preserved_on_ledger_backfill_and_atomic_manual_resolve(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v6-p1-03-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v6_p103.db"))
    await db.open()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    svc = OrderService(db, reg, EventBus(), poll_intervals=(5.0, 5.0, 5.0))

    try:
        # 1. 旧订单表记有实付 0.30 USD（无激活行），补建激活台账并记入可信部分退款 0.10 USD -> 净支出 0.20 USD
        await db.orders.create(
            order_id="ord_leg_cost",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            project_id="p_leg",
            status="cancelled",
        )
        await db.orders.update(
            "ord_leg_cost",
            activation_id="act_leg_1",
            actual_cost=0.30,
            cost=0.30,
            cost_status="actual",
            currency="USD",
            refund_state="confirmed",
        )
        await db.activations.ensure_activation_state(
            "ord_leg_cost",
            "act_leg_1",
            provider="smsbower",
            state="refund_confirmed",
            refund_amount=0.10,
            refund_amount_source="provider_response",
        )
        act_row = await db.activations.get_by_activation_id("ord_leg_cost", "act_leg_1")
        assert act_row is not None
        assert act_row["actual_cost"] == pytest.approx(0.30)
        assert act_row["refund_amount"] == pytest.approx(0.10)

        st1 = await db.orders.get_stats(project_id="p_leg")
        assert st1["today"]["actual_cost_total"] == pytest.approx(0.30)
        assert st1["today"]["refunded_amount_total"] == pytest.approx(0.10)
        assert st1["today"]["net_actual_cost_total"] == pytest.approx(0.20)

        # 2. 兜底场景：若已有旧补建行缺少 actual_cost (NULL, cost_status='unknown') 但匹配父订单 activation_id -> 聚合层同样合并为净支出 0.20
        await db.orders.create(
            order_id="ord_leg_existing_null",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            project_id="p_leg_null",
            status="cancelled",
        )
        await db.orders.update(
            "ord_leg_existing_null",
            activation_id="act_leg_null",
            actual_cost=0.30,
            cost=0.30,
            cost_status="actual",
            currency="USD",
            refund_state="confirmed",
        )
        await db.conn.write(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, cost_status, currency, refund_amount, refund_amount_status) "
            "VALUES ('ord_leg_existing_null', 1, 'smsbower', 'act_leg_null', 'refund_confirmed', 'unknown', 'USD', 0.10, 'verified')"
        )
        st2 = await db.orders.get_stats(project_id="p_leg_null")
        assert st2["today"]["actual_cost_total"] == pytest.approx(0.30)
        assert st2["today"]["refunded_amount_total"] == pytest.approx(0.10)
        assert st2["today"]["net_actual_cost_total"] == pytest.approx(0.20)

        # 3. 人工核销：已确认退款的激活绝不可被改成 completed，且父订单不得被改写成 manual_resolved / refund_state='none'
        await db.orders.update("ord_leg_cost", settlement_state="uncertain", refund_state="confirmed")
        with pytest.raises(ConflictError) as exc_info:
            await svc.resolve_activation_manually(int(act_row["id"]), state="completed", note="尝试误覆盖")
        assert exc_info.value.code == "ACTIVATION_REFUND_IMMUTABLE"

        after_act = await db.activations.get(int(act_row["id"]))
        after_ord = await svc.get_order("ord_leg_cost")
        assert after_act is not None and after_act["state"] == "refund_confirmed"
        assert after_ord["settlement_state"] == "uncertain"
        assert after_ord["refund_state"] == "confirmed"
    finally:
        await svc.shutdown()
        await db.close()


# ── P1-04: 5sim 严格区分 FINISH / EXPIRED / NOT_FOUND 与真实 CANCELED 退款确认 ──


@pytest.mark.asyncio
async def test_v6_p1_04_five_sim_finish_and_expired_never_marked_refund_confirmed(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v6-p1-04-secret")
    from app.config import env
    env.reload()

    five = FiveSimProvider(api_key_getter=AsyncMock(return_value="k_5sim"))
    try:
        # 1. /user/check 返回 FINISH 且无短信 -> get_sms_code 返回 ("FINISHED", "")，绝不返回 CANCEL
        five._get = AsyncMock(return_value={"status": "FINISH", "sms": []})
        assert await five.get_sms_code("fs_1") == ("FINISHED", "")

        # 2. 轮询结算收到 FINISHED（且无短信）-> 订单记 expired + unrefundable，激活记 unrefundable，绝不记 refund_confirmed，也不调 cancel
        db = Database(str(tmp_path / "v6_p104.db"))
        await db.open()
        reg = ProviderRegistry(default="5sim")
        reg.init_provider("5sim", five)
        svc = OrderService(db, reg, EventBus(), poll_intervals=(0.02, 0.02, 0.02))
        try:
            five.cancel_activation = AsyncMock(return_value="CANCELED")
            await db.orders.create(
                order_id="ord_5sim_fin",
                provider="5sim",
                service="whatsapp",
                service_code="whatsapp",
                country_id="russia",
                status="waiting_code",
            )
            await db.orders.update(
                "ord_5sim_fin",
                activation_id="fs_fin_1",
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
            )
            svc._start_code_poller("ord_5sim_fin")
            for _ in range(50):
                cur = await svc.get_order("ord_5sim_fin")
                if cur["status"] != "waiting_code":
                    break
                await asyncio.sleep(0.02)
            final_ord = await svc.get_order("ord_5sim_fin")
            assert final_ord["status"] == "expired"
            assert final_ord["refund_state"] == "unrefundable"
            assert final_ord["error_code"] == "PROVIDER_FINISHED"
            assert final_ord["activations"][0]["state"] == "unrefundable"
            five.cancel_activation.assert_not_awaited()
        finally:
            await svc.shutdown()
            await db.close()

        # 3. 显式取消遇到过期 / not found / FINISH -> refund_confirmed 必须为 False；仅真实 CANCELED 为 True
        five._get = AsyncMock(side_effect=SmsExpiredError("order expired"))
        out_exp = await five.cancel_activation_structured("fs_exp")
        assert out_exp.settled is True and out_exp.refund_confirmed is False

        five._get = AsyncMock(side_effect=SmsProviderError("order not found"))
        out_nf = await five.cancel_activation_structured("fs_nf")
        assert out_nf.settled is True and out_nf.refund_confirmed is False

        five._get = AsyncMock(return_value={"status": "FINISH"})
        out_fin = await five.cancel_activation_structured("fs_fin")
        assert out_fin.settled is True and out_fin.refund_confirmed is False

        five._get = AsyncMock(return_value={"status": "CANCELED"})
        out_ok = await five.cancel_activation_structured("fs_ok")
        assert out_ok.settled is True and out_ok.refund_confirmed is True and out_ok.refund_amount is None
    finally:
        await five.close()


# ── P1-05: 保留同单多币种记录绝不盲改、v4 缺字段正常补齐、已运行旧 v6 库受影响行入核对队列 ──


@pytest.mark.asyncio
async def test_v6_p1_05_non_destructive_currency_migration_and_v6_review_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v6-p1-05-secret")
    from app.config import env
    env.reload()

    # 场景 A: v5 数据库同一订单两笔明确记账激活 (1 USD + 2 EUR，父订单币种 EUR) -> 升级 v7 绝不把 1 USD 改成 EUR！
    v5_multi = str(tmp_path / "v5_same_order_multi_ccy.db")
    async with aiosqlite.connect(v5_multi) as raw:
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
            "INSERT INTO sms_orders (order_id, project_id, status, actual_cost, cost_status, currency) "
            "VALUES ('ord_eur_mix', 'p_eur', 'completed', 2.0, 'actual', 'EUR')"
        )
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, actual_cost, cost_status, currency) "
            "VALUES ('ord_eur_mix', 1, 'smsbower', 'act_u1', 'completed', 1.0, 'actual', 'USD')"
        )
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, actual_cost, cost_status, currency) "
            "VALUES ('ord_eur_mix', 2, '5sim', 'act_e2', 'completed', 2.0, 'actual', 'EUR')"
        )
        await raw.commit()

    db_a = Database(v5_multi)
    await db_a.open()
    try:
        a_u1 = await db_a.activations.get_by_activation_id("ord_eur_mix", "act_u1")
        a_e2 = await db_a.activations.get_by_activation_id("ord_eur_mix", "act_e2")
        assert a_u1 is not None and a_u1["currency"] == "USD" and bool(a_u1["currency_review_required"]) is True
        assert a_e2 is not None and a_e2["currency"] == "EUR" and bool(a_e2["currency_review_required"]) is False

        st = await db_a.orders.get_stats(project_id="p_eur")
        assert st["today"]["is_multi_currency"] is True
        assert st["today"]["actual_cost_total"] is None
        assert st["today"]["currency_review_count"] == 1
        verified_ccy = {b["currency"]: b["actual_cost_total"] for b in st["today"]["by_currency"]}
        review_ccy = {b["currency"]: b["actual_cost_total"] for b in st["today"]["currency_review_by_currency"]}
        assert verified_ccy == {"EUR": 2.0}
        assert review_ccy == {"USD": 1.0}
    finally:
        await db_a.close()

    # 场景 B: 已经过旧 v6 迁移（曾把多激活中的 USD 改成了 EUR）的库升级 v7 -> 标入币种核对队列且允许人工修正
    v6_old = str(tmp_path / "already_v6_review.db")
    async with aiosqlite.connect(v6_old) as raw:
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
            "ALTER TABLE sms_orders ADD COLUMN idempotency_error_code TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sms_orders ADD COLUMN idempotency_error_message TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE order_activations ADD COLUMN currency TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE order_activations ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE order_activations ADD COLUMN estimated_cost REAL",
            "ALTER TABLE order_activations ADD COLUMN refund_amount REAL",
            "ALTER TABLE order_activations ADD COLUMN refund_amount_status TEXT NOT NULL DEFAULT 'none'",
            "ALTER TABLE order_activations ADD COLUMN refund_amount_source TEXT NOT NULL DEFAULT ''",
        ):
            await raw.execute(sql)
        for v in (1, 2, 3, 4, 5, 6):
            await raw.execute("INSERT INTO schema_version (version, description) VALUES (?, 'old v6')", (v,))

        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, status, actual_cost, cost_status, currency) "
            "VALUES ('ord_v6_suspect', 'p_v6s', 'completed', 2.0, 'actual', 'EUR')"
        )
        # 模拟旧 v6 把 smsbower 的 USD 激活覆写成了 EUR
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, actual_cost, cost_status, currency) "
            "VALUES ('ord_v6_suspect', 1, 'smsbower', 'act_s1', 'completed', 1.0, 'actual', 'EUR')"
        )
        await raw.commit()

    db_b = Database(v6_old)
    await db_b.open()
    reg_b = ProviderRegistry(default="smsbower")
    reg_b.init_provider("smsbower", _make_provider())
    svc_b = OrderService(db_b, reg_b, EventBus())
    try:
        recon = await svc_b.list_reconciliation_items()
        assert any(a["activation_id"] == "act_s1" for a in recon["currency_review_activations"])
        row = recon["currency_review_activations"][0]
        resolved = await svc_b.resolve_activation_manually(
            int(row["id"]), state="completed", note="核对账单确认 USD", currency="USD"
        )
        assert resolved["currency"] == "USD"
        assert bool(resolved["currency_review_required"]) is False
    finally:
        await svc_b.shutdown()
        await db_b.close()


# ── P1-06: 历史 v4 异步单、v5 补偿失败单、并发首次国家查询及回调域名变更后的幂等重放 ──


@pytest.mark.asyncio
async def test_v6_p1_06_historical_v4_async_v5_compensated_concurrent_country_and_webhook_origin_change(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v6-p1-06-secret")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "v6-p106-admin")
    monkeypatch.delenv("WEBHOOK_ALLOWED_HOSTS", raising=False)
    from app.config import env
    env.reload()

    # 1. 构造 v4 异步失败单 + v5 已补偿 500 单（均在旧 Schema 落盘）-> 升级 v7 后验证精确幂等重放
    hist_path = str(tmp_path / "hist_idem_v4v5.db")
    v1_async_hash = OrderService._build_legacy_hash_v1(
        existing={"provider": "smsbower", "webhook_url": ""},
        canonical_svc="whatsapp",
        country_input="Thailand",
        country_fallback=[],
        max_price=None,
        opts=AcquireOptions(),
        wait_timeout=180,
        skip_blacklisted=True,
        async_acquire=True,
        webhook_url="",
    )
    async with aiosqlite.connect(hist_path) as raw:
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

        # 模拟 v4 异步失败单（v4 无 async_acquire 列故为 0，request_hash_version=1 且含 async_acquire=True）
        await raw.execute(
            "INSERT INTO sms_orders (order_id, idempotency_key, request_hash, request_hash_version, project_id, "
            "provider, service, service_code, country_id, status, error_code, error_message, async_acquire) "
            "VALUES ('ord_v4_async_fail', 'v4-async-k1', ?, 1, 'web-console', 'smsbower', 'whatsapp', 'wa', '52', "
            "'failed', 'NO_NUMBERS', '无可用号码', 0)",
            (v1_async_hash,),
        )
        # 模拟 v5 同步 500 订单补偿退款后：idempotency_http_status=500, status='cancelled', error_code='COMPENSATED_CANCEL'
        v2_sync_hash = OrderService._build_request_hash_v2(
            provider_input="",
            canonical_svc="whatsapp",
            country_input="Thailand",
            country_fallback=[],
            opts=AcquireOptions(),
            wait_timeout=180,
            skip_blacklisted=True,
            async_acquire=False,
            external_ref="",
            explicit_webhook_url="",
            metadata={},
        )
        await raw.execute(
            "INSERT INTO sms_orders (order_id, idempotency_key, request_hash, request_hash_version, project_id, "
            "provider, service, service_code, country_id, status, refund_state, error_code, error_message, "
            "idempotency_http_status, idempotency_retryable, async_acquire) "
            "VALUES ('ord_v5_comp_500', 'v5-comp-k1', ?, 2, 'web-console', 'smsbower', 'whatsapp', 'wa', '52', "
            "'cancelled', 'confirmed', 'COMPENSATED_CANCEL', '后台补偿退款已确认', 500, 1, 0)",
            (v2_sync_hash,),
        )
        await raw.commit()

    prov = _make_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    app = create_app(db_path=hist_path, enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as ext:
            await admin.post("/api/v1/auth/login", json={"password": "v6-p106-admin"})

            # 校验 1: v4 异步失败单以 async_acquire=True 重试 -> 精确返回 202 + 原订单 ID（绝不返 409 NO_NUMBERS！）
            r_v4 = await admin.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "v4-async-k1"},
                json={"service": "whatsapp", "country": "Thailand", "async_acquire": True},
            )
            assert r_v4.status_code == 202
            assert r_v4.json()["order_id"] == "ord_v4_async_fail"

            # 校验 2: v5 已补偿 500 单重试 -> 返回 500 HISTORICAL_ACQUIRE_ERROR 并带订单查询路径（绝不伪报 COMPENSATED_CANCEL！）
            r_v5 = await admin.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "v5-comp-k1"},
                json={"service": "whatsapp", "country": "Thailand"},
            )
            assert r_v5.status_code == 500
            err_v5 = r_v5.json()["error"]
            assert err_v5["code"] == "HISTORICAL_ACQUIRE_ERROR"
            assert err_v5["details"]["order_id"] == "ord_v5_comp_500"
            assert err_v5["details"]["order_query_path"] == "/api/v1/orders/ord_v5_comp_500"

            # 校验 3: 同键并发首次请求 -> 第 1 次国家查询耗时，第 2 次若再次调用国家查询会抛 502；
            # 修复后应按幂等键互斥，仅调用 1 次 resolve_country 和 1 次 request_number，两个请求都拿到 201！
            resolve_calls = {"n": 0}

            async def flaky_once_resolve(_c):
                resolve_calls["n"] += 1
                if resolve_calls["n"] == 1:
                    await asyncio.sleep(0.05)
                    return (52, "Thailand")
                raise RuntimeError("upstream country API down on duplicate call")

            prov.resolve_country = AsyncMock(side_effect=flaky_once_resolve)
            prov.request_number = AsyncMock(
                return_value=Activation("act_conc_1", "66877771111", 52, "Thailand", provider="smsbower", price=0.15)
            )
            c1, c2 = await asyncio.gather(
                admin.post(
                    "/api/v1/orders",
                    headers={"Idempotency-Key": "conc-first-k1"},
                    json={"service": "whatsapp", "country": "Thailand"},
                ),
                admin.post(
                    "/api/v1/orders",
                    headers={"Idempotency-Key": "conc-first-k1"},
                    json={"service": "whatsapp", "country": "Thailand"},
                ),
            )
            assert c1.status_code == 201 and c2.status_code == 201
            assert c1.json()["order_id"] == c2.json()["order_id"]
            assert resolve_calls["n"] == 1
            assert prov.request_number.await_count == 1

            # 校验 4: 项目成功下单后管理员修改了项目默认回调域名（且旧域名 DNS 已不可达）->
            # 原样同键重试仍返 201（不被 403/422 拒绝），但新单用旧域名仍被 403 拦截！
            import socket as _socket

            loop = asyncio.get_running_loop()
            orig_getaddrinfo = loop.getaddrinfo
            dns_allow = {"old.hooks.test", "new.hooks.test"}

            async def fake_getaddrinfo(host, port, *args, **kwargs):
                if host in ("old.hooks.test", "new.hooks.test"):
                    if host not in dns_allow:
                        raise _socket.gaierror("NXDOMAIN for retired host")
                    return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", int(port or 443)))]
                return await orig_getaddrinfo(host, port, *args, **kwargs)

            monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
            prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
            proj_resp = await admin.post(
                "/api/v1/projects/keys",
                json={"name": "HookShift", "project_id": "p_hookshift", "default_webhook_url": "https://old.hooks.test/base"},
            )
            assert proj_resp.status_code == 201, proj_resp.text
            pkey = proj_resp.json()["api_key"]

            ok_first = await ext.post(
                "/api/v1/orders",
                headers={"X-API-Key": pkey, "Idempotency-Key": "wh-shift-k1"},
                json={"service": "whatsapp", "country": "Thailand", "webhook_url": "https://old.hooks.test/order1"},
            )
            assert ok_first.status_code == 201

            # 管理员修改默认回调域名为 new.hooks.test，且下线旧域名 DNS
            upd_p = await admin.patch(
                "/api/v1/projects/keys/p_hookshift",
                json={"default_webhook_url": "https://new.hooks.test/base"},
            )
            assert upd_p.status_code == 200
            dns_allow.discard("old.hooks.test")

            # 原样同键重试必须命中已存在订单返回 201（不受新同源策略和 DNS 下线影响）
            ok_replay = await ext.post(
                "/api/v1/orders",
                headers={"X-API-Key": pkey, "Idempotency-Key": "wh-shift-k1"},
                json={"service": "whatsapp", "country": "Thailand", "webhook_url": "https://old.hooks.test/order1"},
            )
            assert ok_replay.status_code == 201
            assert ok_replay.json()["order_id"] == ok_first.json()["order_id"]

            # 换新键仍传旧域名 -> 必须被新同源策略拒绝为 403！
            rejected_new = await ext.post(
                "/api/v1/orders",
                headers={"X-API-Key": pkey, "Idempotency-Key": "wh-shift-k2"},
                json={"service": "whatsapp", "country": "Thailand", "webhook_url": "https://old.hooks.test/order2"},
            )
            assert rejected_new.status_code == 403


# ── P2-03: 生命周期退出（正常/异常/同进程换目录重启）释放所有日志文件句柄（Windows 可立即删除）──


@pytest.mark.asyncio
async def test_v6_p2_03_lifespan_closes_log_file_handles_and_supports_same_process_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v6-p2-03-secret")
    monkeypatch.delenv("BRIDGE_LOG_FILE", raising=False)
    from app.config import env
    env.reload()

    # 1. 正常退出后可立即删除日志文件（Windows 无 WinError 32）
    import logging as py_logging
    dir1 = tmp_path / "run_dir_1"
    reg1 = ProviderRegistry(default="smsbower")
    reg1.init_provider("smsbower", _make_provider())
    app1 = create_app(db_path=str(dir1 / "app1.db"), enable_wa_plugin=False, registry_instance=reg1, skip_startup_probe=True)
    async with app1.router.lifespan_context(app1):
        py_logging.getLogger("app.main").warning("open handle in dir1")
    log1 = dir1 / "bridge.log"
    assert log1.exists() and log1.stat().st_size > 0
    log1.unlink()  # 必须不抛 PermissionError / WinError 32
    assert not log1.exists()

    # 2. 启动异常（迁移抛错）后同样立即释放日志句柄
    dir_err = tmp_path / "run_dir_err"
    from app.db.migrations import run_migrations as real_run_migrations

    async def boom(_conn):
        py_logging.getLogger("app.main").warning("open handle before migration boom")
        raise RuntimeError("boom migrate")

    monkeypatch.setattr(db_pkg, "run_migrations", boom)
    reg_e = ProviderRegistry(default="smsbower")
    reg_e.init_provider("smsbower", _make_provider())
    app_e = create_app(db_path=str(dir_err / "err.db"), enable_wa_plugin=False, registry_instance=reg_e, skip_startup_probe=True)
    with pytest.raises(RuntimeError, match="boom migrate"):
        async with app_e.router.lifespan_context(app_e):
            pass
    monkeypatch.setattr(db_pkg, "run_migrations", real_run_migrations)
    log_err = dir_err / "bridge.log"
    assert log_err.exists()
    log_err.unlink()
    assert not log_err.exists()

    # 3. 同进程换新目录重启 -> 切换到 dir2/bridge.log 写入并在退出后可删除
    dir2 = tmp_path / "run_dir_2"
    reg2 = ProviderRegistry(default="smsbower")
    reg2.init_provider("smsbower", _make_provider())
    app2 = create_app(db_path=str(dir2 / "app2.db"), enable_wa_plugin=False, registry_instance=reg2, skip_startup_probe=True)
    async with app2.router.lifespan_context(app2):
        py_logging.getLogger("app.main").warning("open handle in dir2")
    log2 = dir2 / "bridge.log"
    assert log2.exists() and log2.stat().st_size > 0
    log2.unlink()
    assert not log2.exists()


# ── P2-04: 统计时间下界精确涵盖零点、零点前后 1 秒、SQLite 空格格式与带时区格式 ──


@pytest.mark.asyncio
async def test_v6_p2_04_utc_midnight_boundary_space_and_offset_timestamps(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v6-p2-04-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v6_p204.db"))
    await db.open()
    try:
        # 计算 UTC 当日零点（timezone_offset_minutes = 0）
        today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        before_1s = today_utc - timedelta(seconds=1)
        after_1s = today_utc + timedelta(seconds=1)
        # 同一时刻用 +08:00 表示（等于 UTC 00:00:00）
        exact_plus8 = (today_utc + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        before_plus8 = (before_1s + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S+08:00")

        samples = [
            ("o_space_before", before_1s.strftime("%Y-%m-%d %H:%M:%S"), False),
            ("o_space_exact", today_utc.strftime("%Y-%m-%d %H:%M:%S"), True),
            ("o_space_after", after_1s.strftime("%Y-%m-%d %H:%M:%S"), True),
            ("o_iso_exact", today_utc.isoformat(), True),
            ("o_z_exact", today_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), True),
            ("o_plus8_exact", exact_plus8, True),
            ("o_plus8_before", before_plus8, False),
        ]
        for oid, ts, _included in samples:
            await db.conn.write(
                "INSERT INTO sms_orders (order_id, project_id, status, actual_cost, cost_status, currency, created_at, updated_at) "
                "VALUES (?, 'p_tz', 'completed', 1.0, 'actual', 'USD', ?, ?)",
                (oid, ts, ts),
            )

        stats = await db.orders.get_stats(timezone_offset_minutes=0, days=1, project_id="p_tz")
        # 5 笔应在当日内（含精确零点空格时间、+1s、ISO、Z、+08:00 等价零点），2 笔零点前 1 秒排除
        assert stats["today"]["total"] == 5
        assert stats["today"]["actual_cost_total"] == pytest.approx(5.0)
    finally:
        await db.close()
