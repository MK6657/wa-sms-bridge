"""针对 REVIEW_V5_2026-11-25.md 全部 5 项高优先级问题（Schema v6）的专项回归测试套件。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import httpx
import pytest

from app.core.event_bus import EventBus
from app.core.order_service import OrderService
from app.db import Database
from app.db.migrations import (
    API_KEYS_DDL,
    BASE_SCHEMA_SQL,
    ORDER_ACTIVATIONS_DDL,
    PROJECTS_DDL,
    SMS_MESSAGES_DDL,
    SMS_ORDERS_DDL,
    WEBHOOK_DELIVERIES_DDL,
    run_migrations,
)
from app.main import create_app
from app.models import PhoneInput
from app.providers.base import Activation, CancelOutcome, NoNumbersError, ProviderCapabilities
from app.providers.registry import ProviderRegistry


def _make_provider(
    *,
    phone: str = "66812345678",
    activation_id: str = "act_v5_1",
    price: float = 0.25,
    currency: str = "USD",
    confirms_full_refund_on_cancel: bool = False,
):
    prov = MagicMock()
    prov.provider_id = "smsbower"
    prov.capabilities = ProviderCapabilities(
        supports_resend=True,
        supports_price_lock=True,
        supports_exact_price_lock=False,
        confirms_full_refund_on_cancel=confirms_full_refund_on_cancel,
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
            provider="smsbower",
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


# ── P1-01: 幂等快照与后续订单状态解耦（同步 500 退款后仍回 500；异步 202 失败/脱敏后仍回 202）──


@pytest.mark.asyncio
async def test_v5_p1_01_idempotency_immutable_sync_error_and_async_202_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v5-p1-01-secret")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    from app.config import env
    env.reload()

    prov = _make_provider(activation_id="act_v5_sync_boom")
    prov.normalize_phone = AsyncMock(side_effect=RuntimeError("normalize crash"))
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    app = create_app(
        db_path=str(tmp_path / "v5_p101.db"),
        enable_wa_plugin=False,
        registry_instance=reg,
        skip_startup_probe=True,
    )

    async with app.router.lifespan_context(app):
        svc: OrderService = app.state.container.order_service
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. 同步取号购买后处理失败 -> 首次 500 ACQUIRE_POST_ERROR
            r1 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "sync-post-err-1"},
                json={"service": "whatsapp", "country": "Thailand"},
            )
            assert r1.status_code == 500
            assert r1.json()["error"]["code"] == "ACQUIRE_POST_ERROR"
            sync_oid = r1.json()["error"]["details"]["order_id"]

            # 2. 后台补偿完成退款，订单变为 cancelled + error_code='COMPENSATED_CANCEL'
            assert await svc.reconcile_pending() >= 1
            after_comp = await svc.get_order(sync_oid)
            assert after_comp["status"] == "cancelled"
            assert after_comp["refund_state"] == "confirmed"
            assert after_comp["error_code"] == "COMPENSATED_CANCEL"

            # 3. 退款完成后用相同 Idempotency-Key 重试 -> 仍必须返回 500 ACQUIRE_POST_ERROR（绝不变成 201 cancelled！）
            r2 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "sync-post-err-1"},
                json={"service": "whatsapp", "country": "Thailand"},
            )
            assert r2.status_code == 500
            assert r2.json()["error"]["code"] == "ACQUIRE_POST_ERROR"
            assert r2.json()["error"]["details"]["order_id"] == sync_oid
            assert prov.request_number.await_count == 1

            # 4. 异步下单首次返回 202，后台无库存失败转为 failed -> 同键重试固定返回 202 + 原订单 ID（绝不变成 201！）
            prov.request_number = AsyncMock(side_effect=NoNumbersError("无可用号码"))
            a1 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "async-fail-1"},
                json={"service": "whatsapp", "country": "Thailand", "async_acquire": True},
            )
            assert a1.status_code == 202
            async_fail_oid = a1.json()["order_id"]

            # 等待后台取号任务落盘 failed
            for _ in range(50):
                cur = await svc.get_order(async_fail_oid)
                if cur["status"] == "failed":
                    break
                await asyncio.sleep(0.02)
            assert (await svc.get_order(async_fail_oid))["status"] == "failed"

            a2 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "async-fail-1"},
                json={"service": "whatsapp", "country": "Thailand", "async_acquire": True},
            )
            assert a2.status_code == 202
            assert a2.json()["order_id"] == async_fail_oid
            assert prov.request_number.await_count == 1

            # 5. 异步下单成功 -> 收码 -> 脱敏 -> 同键重试仍返回 202，仅购买一次且绝不恢复已脱敏验证码
            prov.request_number = AsyncMock(
                return_value=Activation("act_v5_async_ok", "66899990000", 52, "Thailand", provider="smsbower", price=0.12)
            )
            prov.normalize_phone = AsyncMock(
                return_value=PhoneInput(
                    region="TH", phone="899990000", e164_number="+66899990000", country_calling_code="66", country_iso2="th"
                )
            )
            prov.get_sms_code = AsyncMock(return_value=("OK", "739104"))

            s1 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "async-ok-scrub-1"},
                json={"service": "whatsapp", "country": "Thailand", "async_acquire": True},
            )
            assert s1.status_code == 202
            async_ok_oid = s1.json()["order_id"]

            # 等待收码并脱敏
            res = await svc.wait_for_code(async_ok_oid, timeout=3)
            assert res["status"] == "code_received"
            assert res["latest_code"] == "739104"
            assert (await client.delete(f"/api/v1/orders/{async_ok_oid}/code")).status_code == 200

            s2 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "async-ok-scrub-1"},
                json={"service": "whatsapp", "country": "Thailand", "async_acquire": True},
            )
            assert s2.status_code == 202
            assert s2.json()["order_id"] == async_ok_oid
            assert s2.json()["code_scrubbed"] is True
            assert s2.json()["latest_code"] == ""
            assert prov.request_number.await_count == 1


# ── P1-02: 收码写入暂态失败同激活恢复、持续失败留对账证据、任务异常退出由周期维护接管 ──


@pytest.mark.asyncio
async def test_v5_p1_02_code_write_retry_persistent_failure_and_maintenance_takeover(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v5-p1-02-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v5_p102.db"))
    await db.open()
    prov = _make_provider(activation_id="act_poll_1")
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    bus = EventBus()
    svc = OrderService(
        db,
        reg,
        bus,
        poll_intervals=(0.02, 0.02, 0.02),
        max_code_write_retries=3,
    )

    try:
        # 场景 A: 首次 append_code 写库抛异常 -> 轮询不退出，在同一激活上重试成功，消息与 Outbox/SSE 仅 1 条（幂等）
        prov.get_sms_code = AsyncMock(return_value=("OK", "654321"))
        real_append_code = db.orders.append_code
        fail_once = {"remaining": 1}

        async def flaky_append(*args, **kwargs):
            if fail_once["remaining"] > 0:
                fail_once["remaining"] -= 1
                raise RuntimeError("sqlite busy transient failure")
            return await real_append_code(*args, **kwargs)

        monkeypatch.setattr(db.orders, "append_code", flaky_append)

        async with bus.subscribe() as sse_q:
            ord_a = await svc.create_order(
                country="Thailand",
                service="whatsapp",
                webhook_url="https://hooks.partner.internal/cb",
            )
            oid_a = ord_a["order_id"]
            got_a = await svc.wait_for_code(oid_a, timeout=3)
            assert got_a["status"] == "code_received"
            assert got_a["code_count"] == 1
            assert got_a["latest_code"] == "654321"
            assert len(got_a["messages"]) == 1

            # 校验 Outbox 与 SSE 仅记录/发送一次 order.code_received
            deliveries = await db.webhooks.list(order_id=oid_a)
            code_dlvs = [d for d in deliveries if d["event_type"] == "order.code_received"]
            assert len(code_dlvs) == 1

            code_events = []
            while not sse_q.empty():
                ev = sse_q.get_nowait()
                if ev.order_id == oid_a and ev.event == "order.code_received":
                    code_events.append(ev)
            assert len(code_events) == 1
            assert prov.request_number.await_count == 1

        # 场景 B: 持续写入失败 -> 不卡在 waiting_code、不误当无码超时退款，保留对账证据并按上游状态结算
        prov.request_number = AsyncMock(
            return_value=Activation("act_poll_persist_fail", "66811112222", 52, "Thailand", provider="smsbower", price=0.25)
        )
        prov.get_sms_code = AsyncMock(return_value=("OK", "998877"))
        monkeypatch.setattr(db.orders, "append_code", AsyncMock(side_effect=RuntimeError("disk readonly")))

        ord_b = await svc.create_order(country="Thailand", service="whatsapp")
        oid_b = ord_b["order_id"]

        for _ in range(80):
            cur_b = await svc.get_order(oid_b)
            if cur_b["status"] != "waiting_code":
                break
            await asyncio.sleep(0.02)

        final_b = await svc.get_order(oid_b)
        assert final_b["status"] == "failed"
        assert final_b["settlement_state"] == "code_persist_failed"
        assert final_b["error_code"] == "CODE_PERSIST_FAILED"
        assert final_b["refund_state"] == "unrefundable"
        # 未把已收码付费激活误当超时取消
        prov.cancel_activation.assert_not_awaited()
        # 对账队列可见该激活与订单，且错误日志不泄露验证码明文
        recon = await svc.list_reconciliation_items()
        assert any(a["activation_id"] == "act_poll_persist_fail" and a["state"] == "code_persist_failed" for a in recon["uncertain_activations"])
        assert all("998877" not in str(a.get("last_error") or "") for a in recon["uncertain_activations"])
        assert any(o["order_id"] == oid_b for o in recon["orders_requiring_review"])

        # 恢复真实 append_code
        monkeypatch.setattr(db.orders, "append_code", real_append_code)

        # 场景 C1: 轮询任务异常退出/缺失（未超时）-> 周期维护 reconcile_pending() 自动接管并收码
        future_exp = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        await db.orders.create(
            order_id="ord_orphan_wait",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_code",
        )
        await db.orders.update("ord_orphan_wait", activation_id="act_orphan_wait", expires_at=future_exp)
        prov.get_sms_code = AsyncMock(return_value=("OK", "135790"))

        assert await svc.reconcile_pending() >= 1
        got_c1 = await svc.wait_for_code("ord_orphan_wait", timeout=3)
        assert got_c1["status"] == "code_received"
        assert got_c1["latest_code"] == "135790"

        # 场景 C2: 轮询任务缺失且已超时 -> 周期维护 reconcile_pending() 直接执行超时取消退款
        past_exp = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        await db.orders.create(
            order_id="ord_orphan_expired",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_code",
        )
        await db.orders.update("ord_orphan_expired", activation_id="act_orphan_exp", expires_at=past_exp)
        a_exp = await db.activations.record_intent(
            order_id="ord_orphan_expired", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )
        await db.activations.bind_acquired(a_exp, activation_id="act_orphan_exp", phone_raw="66833334444", actual_cost=0.20, currency="USD", state="active")

        assert await svc.reconcile_pending() >= 1
        prov.cancel_activation.assert_any_await("act_orphan_exp")
        got_c2 = await svc.get_order("ord_orphan_expired")
        assert got_c2["status"] == "expired"
        assert got_c2["refund_state"] == "confirmed"
    finally:
        await svc.shutdown()
        await db.close()


# ── P1-03: read_transaction 在 BEGIN 期间取消、查询途中取消与连续两次取消后仍正常读写 ──


@pytest.mark.asyncio
async def test_v5_p1_03_read_transaction_cancel_during_begin_mid_query_and_double_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v5-p1-03-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "v5_p103.db"))
    await db.open()
    try:
        assert db.conn._read_conn is not None
        await db.orders.create(
            order_id="ord_rtx_1",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_code",
        )

        # 1. BEGIN DEFERRED 已在 SQLite 执行但 await 尚未返回时取消任务
        rconn = db.conn._read_conn
        real_execute = rconn.execute
        begin_executed = asyncio.Event()
        release_begin = asyncio.Event()

        async def slow_begin(sql, *args, **kwargs):
            res = await real_execute(sql, *args, **kwargs)
            if str(sql).strip().upper().startswith("BEGIN"):
                begin_executed.set()
                await release_begin.wait()
            return res

        monkeypatch.setattr(rconn, "execute", slow_begin)

        async def do_snapshot_read():
            return await db.orders.get("ord_rtx_1", include_messages=True)

        t1 = asyncio.create_task(do_snapshot_read())
        await asyncio.wait_for(begin_executed.wait(), timeout=2.0)
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1

        # 恢复正常 execute 后，紧接着普通读取与复合快照查询必须全部成功（绝不报 cannot start a transaction within a transaction！）
        monkeypatch.setattr(rconn, "execute", real_execute)
        assert not getattr(db.conn._read_conn, "in_transaction", False)
        plain = await db.conn.fetchone("SELECT order_id FROM sms_orders WHERE order_id=?", ("ord_rtx_1",))
        assert plain is not None and plain["order_id"] == "ord_rtx_1"
        snap = await db.orders.get("ord_rtx_1", include_messages=True)
        assert snap is not None and snap["order_id"] == "ord_rtx_1"
        stats = await db.orders.get_stats()
        assert stats["today"]["total"] == 1

        # 2. 查询途中取消
        mid_entered = asyncio.Event()

        async def cancel_mid_query():
            async with db.conn.read_transaction() as raw:
                await raw.execute("SELECT 1")
                mid_entered.set()
                await asyncio.sleep(10)

        t2 = asyncio.create_task(cancel_mid_query())
        await asyncio.wait_for(mid_entered.wait(), timeout=2.0)
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2
        assert (await db.orders.get("ord_rtx_1")) is not None

        # 3. 连续两次取消（含一次打断 rollback 迫使连接摘除重建，再连续取消一次）
        rconn2 = db.conn._read_conn
        assert rconn2 is not None
        rb_entered = asyncio.Event()
        real_rollback = rconn2.rollback

        async def slow_rollback():
            rb_entered.set()
            await asyncio.sleep(5)
            return await real_rollback()

        monkeypatch.setattr(rconn2, "rollback", slow_rollback)
        entered_3 = asyncio.Event()

        async def double_cancel_reader():
            async with db.conn.read_transaction() as raw:
                await raw.execute("SELECT 1")
                entered_3.set()
                await asyncio.sleep(10)

        t3 = asyncio.create_task(double_cancel_reader())
        await asyncio.wait_for(entered_3.wait(), timeout=2.0)
        t3.cancel()  # 第一次取消进入 finally -> slow_rollback
        await asyncio.wait_for(rb_entered.wait(), timeout=2.0)
        t3.cancel()  # 第二次取消打断 rollback -> 必须摘除旧连接并在下次读取自动重建
        with pytest.raises(asyncio.CancelledError):
            await t3

        # 再立刻连续取消一次新的 read_transaction
        entered_4 = asyncio.Event()

        async def quick_cancel_reader():
            async with db.conn.read_transaction() as raw:
                await raw.execute("SELECT 1")
                entered_4.set()
                await asyncio.sleep(10)

        t4 = asyncio.create_task(quick_cancel_reader())
        await asyncio.wait_for(entered_4.wait(), timeout=2.0)
        t4.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t4

        # 普通读取 + 订单详情 + 统计快照全部正常
        assert await db.conn.fetchone("SELECT COUNT(*) AS c FROM sms_orders") == {"c": 1}
        assert (await db.orders.get("ord_rtx_1", include_messages=True)) is not None
        assert (await db.orders.get_stats())["today"]["total"] == 1
    finally:
        await db.close()


# ── P1-04: 退款确认与金额证据分离（4 种样本 + v5 -> v6 推定金额前向校正）──


@pytest.mark.asyncio
async def test_v5_p1_04_refund_amount_evidence_four_samples_and_v5_to_v6_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v5-p1-04-secret")
    from app.config import env
    env.reload()

    # Part 1: 运行时四种样本（金额未给 / 部分退款 / 契约全额退款 / 历史估算退款）
    db = Database(str(tmp_path / "v5_p104_rt.db"))
    await db.open()
    prov_no_amt = _make_provider(activation_id="act_no_amt", price=0.25, confirms_full_refund_on_cancel=False)
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov_no_amt)
    svc = OrderService(db, reg, EventBus(), poll_intervals=(5.0, 5.0, 5.0))

    try:
        # 样本 1: 金额未给（上游仅返 ACCESS_CANCEL，平台未声明全额退款契约）-> refund_amount 必须保持 None（unknown）
        o1 = await svc.create_order(country="Thailand", service="whatsapp", project_id="p_ref")
        await svc.cancel_order(o1["order_id"])
        o1_after = await svc.get_order(o1["order_id"])
        assert o1_after["refund_state"] == "confirmed"
        assert o1_after["activations"][0]["state"] == "refund_confirmed"
        assert o1_after["activations"][0]["refund_amount"] is None
        assert o1_after["activations"][0]["refund_amount_status"] == "unknown"

        # 样本 2: 部分退款（上游 CancelOutcome 明确返回 refund_amount=0.10）
        prov_no_amt.request_number = AsyncMock(
            return_value=Activation("act_partial", "66810000002", 52, "Thailand", provider="smsbower", price=0.30, price_is_estimated=False)
        )
        prov_no_amt.cancel_activation_structured = AsyncMock(
            return_value=CancelOutcome(
                settled=True,
                status="refund_confirmed",
                retryable=False,
                raw="PARTIAL_REFUND:0.10",
                refund_confirmed=True,
                refund_amount=0.10,
                refund_amount_source="provider_response",
            )
        )
        o2 = await svc.create_order(country="Thailand", service="whatsapp", project_id="p_ref")
        await svc.cancel_order(o2["order_id"])
        o2_after = await svc.get_order(o2["order_id"])
        assert o2_after["activations"][0]["refund_amount"] == pytest.approx(0.10)
        assert o2_after["activations"][0]["refund_amount_status"] == "verified"

        # 样本 3: 全额退款（平台具备 confirms_full_refund_on_cancel=True 契约）
        prov_no_amt.capabilities = ProviderCapabilities(confirms_full_refund_on_cancel=True)
        prov_no_amt.request_number = AsyncMock(
            return_value=Activation("act_full_contract", "66810000003", 52, "Thailand", provider="smsbower", price=0.40, price_is_estimated=False)
        )
        prov_no_amt.cancel_activation_structured = AsyncMock(
            return_value=CancelOutcome(settled=True, status="refund_confirmed", retryable=False, raw="ACCESS_CANCEL", refund_confirmed=True)
        )
        o3 = await svc.create_order(country="Thailand", service="whatsapp", project_id="p_ref")
        await svc.cancel_order(o3["order_id"])
        o3_after = await svc.get_order(o3["order_id"])
        assert o3_after["activations"][0]["refund_amount"] == pytest.approx(0.40)
        assert o3_after["activations"][0]["refund_amount_status"] == "verified"
        assert o3_after["activations"][0]["refund_amount_source"] == "full_refund_contract"

        # 样本 4: 历史估算成本退款（无 actual_cost，仅 estimated_cost=0.20，上游未给退款额）
        prov_no_amt.capabilities = ProviderCapabilities(confirms_full_refund_on_cancel=False)
        prov_no_amt.request_number = AsyncMock(
            return_value=Activation("act_est_ref", "66810000004", 52, "Thailand", provider="smsbower", price=0.20, price_is_estimated=True)
        )
        o4 = await svc.create_order(country="Thailand", service="whatsapp", project_id="p_ref")
        await svc.cancel_order(o4["order_id"])
        o4_after = await svc.get_order(o4["order_id"])
        assert o4_after["activations"][0]["refund_amount"] is None
        assert o4_after["activations"][0]["refund_amount_status"] == "unknown"

        stats = await db.orders.get_stats(project_id="p_ref")
        today = stats["today"]
        # 实付总计 = 0.25 + 0.30 + 0.40 = 0.95
        assert today["actual_cost_total"] == pytest.approx(0.95)
        # 仅核实的退款额入账 = 0.10 + 0.40 = 0.50（样本 1 的 0.25 绝不被推定计入！）
        assert today["refunded_amount_total"] == pytest.approx(0.50)
        assert today["net_actual_cost_total"] == pytest.approx(0.45)
        # 金额未知的已确认退款 = 2（样本 1 + 样本 4）
        assert today["refund_amount_unknown_count"] == 2
        assert today["by_currency"][0]["refund_amount_unknown_count"] == 2
    finally:
        await svc.shutdown()
        await db.close()

    # Part 2: 已升级至 v5 的旧库前向迁移至 v6 -> 清除旧 v5 无证据推定的全额退款，保留可核实的部分退款
    v5_path = str(tmp_path / "legacy_v5_refund.db")
    async with aiosqlite.connect(v5_path) as raw:
        raw.row_factory = aiosqlite.Row
        await raw.executescript(BASE_SCHEMA_SQL)
        await raw.execute(SMS_ORDERS_DDL)
        await raw.execute(SMS_MESSAGES_DDL)
        await raw.execute(ORDER_ACTIVATIONS_DDL)
        await raw.execute(WEBHOOK_DELIVERIES_DDL)
        await raw.execute(API_KEYS_DDL)
        await raw.execute(PROJECTS_DDL)
        # 模拟旧 v5 表结构与旧 v5 推定写入的数据
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
            await raw.execute("INSERT INTO schema_version (version, description) VALUES (?, 'seed')", (v,))

        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, status, refund_state, actual_cost, cost_status, currency) "
            "VALUES ('ord_v5_imp', 'p_mig', 'cancelled', 'confirmed', 0.25, 'actual', 'USD')"
        )
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, actual_cost, cost_status, currency, refund_amount) "
            "VALUES ('ord_v5_imp', 1, 'smsbower', 'act_v5_imp', 'refund_confirmed', 0.25, 'actual', 'USD', 0.25)"
        )
        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, status, refund_state, actual_cost, cost_status, currency) "
            "VALUES ('ord_v5_part', 'p_mig', 'cancelled', 'confirmed', 0.30, 'actual', 'USD')"
        )
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, actual_cost, cost_status, currency, refund_amount) "
            "VALUES ('ord_v5_part', 1, 'smsbower', 'act_v5_part', 'refund_confirmed', 0.30, 'actual', 'USD', 0.10)"
        )
        await raw.commit()
        await run_migrations(raw)

    db_v6 = Database(v5_path)
    await db_v6.open()
    try:
        imp_act = await db_v6.activations.get_by_activation_id("ord_v5_imp", "act_v5_imp")
        part_act = await db_v6.activations.get_by_activation_id("ord_v5_part", "act_v5_part")
        assert imp_act is not None and imp_act["refund_amount"] is None and imp_act["refund_amount_status"] == "unknown"
        assert part_act is not None and part_act["refund_amount"] == pytest.approx(0.10) and part_act["refund_amount_status"] == "verified"

        mig_stats = await db_v6.orders.get_stats(project_id="p_mig")
        assert mig_stats["today"]["actual_cost_total"] == pytest.approx(0.55)
        assert mig_stats["today"]["refunded_amount_total"] == pytest.approx(0.10)
        assert mig_stats["today"]["refund_amount_unknown_count"] == 1
    finally:
        await db_v6.close()


# ── P1-05: v4->v6 / v5->v6 非美元与未知币种迁移、无库存尝试排除、多币种不混加与显式汇率折算 ──


@pytest.mark.asyncio
async def test_v5_p1_05_v4_and_v5_migration_currency_unpurchased_exclusion_and_mixed_currency(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v5-p1-05-secret")
    from app.config import env
    env.reload()

    # 1. 构造真实 v4 数据库：含 RUB 实付单、USD 实付+估算单、未知币种单、明确无库存尝试 (quoted_max_price=0.50)
    v4_path = str(tmp_path / "legacy_v4_fin.db")
    async with aiosqlite.connect(v4_path) as raw:
        raw.row_factory = aiosqlite.Row
        await raw.executescript(BASE_SCHEMA_SQL)
        await raw.execute(SMS_ORDERS_DDL)
        await raw.execute(SMS_MESSAGES_DDL)
        await raw.execute(ORDER_ACTIVATIONS_DDL)
        await raw.execute(WEBHOOK_DELIVERIES_DDL)
        await raw.execute(API_KEYS_DDL)
        for v in (1, 2, 3, 4):
            await raw.execute("INSERT INTO schema_version (version, description) VALUES (?, 'v4 seed')", (v,))

        # RUB 已购激活 (12.50 RUB)
        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, status, actual_cost, cost, cost_status, currency) "
            "VALUES ('ord_rub', 'p_mix', 'completed', 12.50, 12.50, 'actual', 'RUB')"
        )
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, actual_cost) "
            "VALUES ('ord_rub', 1, '5sim', 'act_rub_1', 'completed', 12.50)"
        )
        # USD 已购激活 (21.00 USD)
        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, status, actual_cost, cost, cost_status, currency) "
            "VALUES ('ord_usd_act', 'p_mix', 'completed', 21.00, 21.00, 'actual', 'USD')"
        )
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, actual_cost) "
            "VALUES ('ord_usd_act', 1, 'smsbower', 'act_usd_1', 'completed', 21.00)"
        )
        # USD 真实已购但估算成本 (0.50 USD)
        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, status, cost, cost_status, currency) "
            "VALUES ('ord_usd_est', 'p_mix', 'completed', 0.50, 'estimated', 'USD')"
        )
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, quoted_max_price) "
            "VALUES ('ord_usd_est', 1, 'smsbower', 'act_usd_est', 'completed', 0.50)"
        )
        # 明确无库存尝试：无 activation_id，quoted_max_price=0.50 -> 绝不可计入估算支出！
        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, status, max_price, quoted_max_price, cost_status, currency) "
            "VALUES ('ord_no_stock', 'p_mix', 'failed', 0.50, 0.50, 'unknown', 'USD')"
        )
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, quoted_max_price) "
            "VALUES ('ord_no_stock', 1, 'smsbower', '', 'no_numbers', 0.50)"
        )
        # 空币种历史单 -> 归入 UNKNOWN（绝不误标 USD）
        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, status, actual_cost, cost_status, currency) "
            "VALUES ('ord_unk_ccy', 'p_unk', 'completed', 3.00, 'actual', '')"
        )
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, actual_cost) "
            "VALUES ('ord_unk_ccy', 1, 'custom', 'act_unk_1', 'completed', 3.00)"
        )
        await raw.commit()

    db_v4 = Database(v4_path)
    await db_v4.open()
    try:
        # 检查 RUB 激活未变成 USD
        rub_act = await db_v4.activations.get_by_activation_id("ord_rub", "act_rub_1")
        assert rub_act is not None and rub_act["currency"] == "RUB"

        # 检查无库存尝试保留 quoted_max_price 供诊断，但 estimated_cost=None, cost_status='none'
        no_stock_acts = await db_v4.activations.list_for_order("ord_no_stock")
        assert len(no_stock_acts) == 1
        assert no_stock_acts[0]["quoted_max_price"] == pytest.approx(0.50)
        assert no_stock_acts[0]["estimated_cost"] is None
        assert no_stock_acts[0]["cost_status"] == "none"

        # 检查空币种订单归为 UNKNOWN
        unk_act = await db_v4.activations.get_by_activation_id("ord_unk_ccy", "act_unk_1")
        assert unk_act is not None and unk_act["currency"] == "UNKNOWN"
        unk_stats = await db_v4.orders.get_stats(project_id="p_unk")
        assert unk_stats["today"]["currency"] == "UNKNOWN"
        assert unk_stats["today"]["by_currency"][0]["currency"] == "UNKNOWN"

        # 检查混合币种：无汇率时不返回 34.00 跨币种合计，分币种精确返回 RUB 12.50 / USD 21.00 实付 + 0.50 估算
        mix_stats = await db_v4.orders.get_stats(project_id="p_mix")
        t = mix_stats["today"]
        assert t["is_multi_currency"] is True
        assert t["currency"] is None
        assert t["actual_cost_total"] is None
        assert t["cost_total"] is None
        assert t["fx_conversion"] is None

        ccy = {b["currency"]: b for b in t["by_currency"]}
        assert set(ccy.keys()) == {"USD", "RUB"}
        assert ccy["USD"]["actual_cost_total"] == pytest.approx(21.00)
        assert ccy["USD"]["estimated_cost_total"] == pytest.approx(0.50)  # 无库存 0.50 已被排除！
        assert ccy["USD"]["cost_total"] == pytest.approx(21.50)
        assert ccy["RUB"]["actual_cost_total"] == pytest.approx(12.50)
        assert ccy["RUB"]["estimated_cost_total"] == pytest.approx(0.0)
        assert ccy["RUB"]["cost_total"] == pytest.approx(12.50)

        # 显式提供汇率与折算日期时才生成跨币种折算结果
        fx_stats = await db_v4.orders.get_stats(
            project_id="p_mix",
            fx_rates={"USD": 1.0, "RUB": 0.01},
            fx_target_currency="USD",
            fx_date="2026-11-25",
        )
        fx = fx_stats["today"]["fx_conversion"]
        assert fx is not None
        assert fx["target_currency"] == "USD"
        assert fx["as_of_date"] == "2026-11-25"
        assert fx["actual_cost_total"] == pytest.approx(21.125)
        assert fx["cost_total"] == pytest.approx(21.625)
    finally:
        await db_v4.close()

    # 2. 模拟已被旧 v5 污染的数据库前向修复至 v6：RUB 被错填 USD、无库存被错填 estimated_cost=0.50
    v5_bad_path = str(tmp_path / "polluted_v5_fin.db")
    async with aiosqlite.connect(v5_bad_path) as raw:
        raw.row_factory = aiosqlite.Row
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
            await raw.execute("INSERT INTO schema_version (version, description) VALUES (?, 'v5 bad')", (v,))

        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, status, actual_cost, cost_status, currency) "
            "VALUES ('ord_bad_rub', 'p_v5fix', 'completed', 12.50, 'actual', 'RUB')"
        )
        # 旧 v5 污染：currency 被错填成 USD
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, actual_cost, cost_status, currency) "
            "VALUES ('ord_bad_rub', 1, '5sim', 'act_bad_rub', 'completed', 12.50, 'actual', 'USD')"
        )
        await raw.execute(
            "INSERT INTO sms_orders (order_id, project_id, status, max_price, quoted_max_price, cost_status, currency) "
            "VALUES ('ord_bad_nostock', 'p_v5fix', 'failed', 0.50, 0.50, 'unknown', 'USD')"
        )
        # 旧 v5 污染：无库存尝试被错填成 cost_status='estimated', estimated_cost=0.50
        await raw.execute(
            "INSERT INTO order_activations (order_id, attempt_no, provider, activation_id, state, quoted_max_price, estimated_cost, cost_status, currency) "
            "VALUES ('ord_bad_nostock', 1, 'smsbower', '', 'no_numbers', 0.50, 0.50, 'estimated', 'USD')"
        )
        await raw.commit()

    db_fixed = Database(v5_bad_path)
    await db_fixed.open()
    try:
        # v6 复核 P1-05 强化后：旧 v5 中已有非空激活币种 (USD) 绝不被盲目覆写，而是原样保留并标入币种核对队列
        fixed_rub = await db_fixed.activations.get_by_activation_id("ord_bad_rub", "act_bad_rub")
        assert fixed_rub is not None and fixed_rub["currency"] == "USD"
        assert bool(fixed_rub["currency_review_required"]) is True

        fixed_ns = await db_fixed.activations.list_for_order("ord_bad_nostock")
        assert fixed_ns[0]["quoted_max_price"] == pytest.approx(0.50)
        assert fixed_ns[0]["estimated_cost"] is None
        assert fixed_ns[0]["cost_status"] == "none"

        # v7 复核 P1-03 强化后：待核对币种记录不计入已知币种总额，单独归入 currency_review_by_currency
        st = await db_fixed.orders.get_stats(project_id="p_v5fix")
        assert st["today"]["by_currency"] == []
        assert st["today"]["currency"] is None
        assert st["today"]["actual_cost_total"] is None
        assert st["today"]["currency_review_count"] == 1
        assert st["today"]["currency_review_by_currency"][0]["currency"] == "USD"
        assert st["today"]["currency_review_by_currency"][0]["actual_cost_total"] == pytest.approx(12.50)
        assert st["today"]["currency_review_by_currency"][0]["estimated_cost_total"] == pytest.approx(0.0)
    finally:
        await db_fixed.close()
