"""针对 REVIEW_ADDITIONAL_2026-11-25.md 与 REPAIR_PLAN_2026-11-25.md 全部 7 项问题的专项回归测试套件。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import app.db as db_pkg
from app.core.event_bus import EventBus
from app.core.order_service import AcquireOptions, OrderService
from app.db import Database
from app.errors import AppError, ConflictError
from app.main import create_app
from app.models import PhoneInput
from app.providers.base import Activation, NoNumbersError, ProviderCapabilities
from app.providers.registry import ProviderRegistry


def _make_provider(phone: str = "66812345678", activation_id: str = "act_add_1"):
    prov = MagicMock()
    prov.provider_id = "smsbower"
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
    prov.get_prices = AsyncMock(return_value=[{"cost": 0.10, "count": 5}])
    prov.request_number = AsyncMock(
        return_value=Activation(activation_id, phone, 52, "Thailand", provider="smsbower", price=0.10, price_is_estimated=False)
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


# ── P1-01: 同步取号异常、客户端取消与写库失败全部可补偿，无需重启即可退款 ──


@pytest.mark.asyncio
async def test_add_p1_01_sync_acquire_post_error_client_cancel_and_stranded_reconcile(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "add-p1-01-secret")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    from app.config import env
    env.reload()

    prov = _make_provider(activation_id="act_sync_boom")
    prov.normalize_phone = AsyncMock(side_effect=RuntimeError("normalize crash"))
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    app = create_app(db_path=str(tmp_path / "add_p101.db"), enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)

    async with app.router.lifespan_context(app):
        svc: OrderService = app.state.container.order_service
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. 购买成功后归一化抛异常 -> 返回 500 ACQUIRE_POST_ERROR，订单转 compensation_pending、激活转 refund_pending
            r1 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "sync-boom-1"},
                json={"service": "whatsapp", "country": "Thailand"},
            )
            assert r1.status_code == 500
            assert r1.json()["error"]["code"] == "ACQUIRE_POST_ERROR"
            oid = r1.json()["error"]["details"]["order_id"]

            # 2. 同幂等键重试 -> 回放 500 ACQUIRE_POST_ERROR（绝不返回 202 或再次购买）
            r2 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "sync-boom-1"},
                json={"service": "whatsapp", "country": "Thailand"},
            )
            assert r2.status_code == 500
            assert r2.json()["error"]["code"] == "ACQUIRE_POST_ERROR"
            assert prov.request_number.await_count == 1

            # 3. 无需重启，直接执行一轮后台补偿即可发现并退款该激活！
            reconciled = await svc.reconcile_pending()
            assert reconciled >= 1
            prov.cancel_activation.assert_any_await("act_sync_boom")
            after_rec = await svc.get_order(oid)
            assert after_rec["status"] == "cancelled"
            assert after_rec["refund_state"] == "confirmed"

            # 4. 模拟补偿状态写入也失败（订单滞留 acquiring、激活滞留 active）-> 后台补偿依然无需重启自动回收退款！
            db: Database = app.state.container.db
            await db.orders.create(
                order_id="ord_stranded_acq",
                provider="smsbower",
                service="whatsapp",
                service_code="wa",
                country_id="52",
                status="acquiring",
            )
            act_row = await db.activations.record_intent(
                order_id="ord_stranded_acq", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
            )
            await db.activations.bind_acquired_atomic(
                act_row,
                order_id="ord_stranded_acq",
                activation_id="act_stranded_9",
                phone_raw="66877776666",
                actual_cost=0.10,
                state="active",
            )
            assert await svc.reconcile_pending() >= 1
            prov.cancel_activation.assert_any_await("act_stranded_9")
            stranded_after = await svc.get_order("ord_stranded_acq")
            assert stranded_after["status"] == "cancelled"
            assert stranded_after["refund_state"] == "confirmed"

            # 5. 客户端在同步取号等待期间断开（取消调用协程）-> 后台受管任务仍完整落库，不遗失激活
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_buy(cid, sc, mp):
                started.set()
                await release.wait()
                return Activation("act_client_disc", "66822223333", cid, "Thailand", provider="smsbower", price=0.10)

            prov.request_number = AsyncMock(side_effect=slow_buy)
            prov.normalize_phone = AsyncMock(
                return_value=PhoneInput(region="TH", phone="822223333", e164_number="+66822223333", country_calling_code="66", country_iso2="th")
            )
            caller_task = asyncio.create_task(
                svc.create_order(country="Thailand", service="whatsapp", idempotency_key="client-disc-1")
            )
            await asyncio.wait_for(started.wait(), timeout=2.0)
            caller_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller_task

            release.set()
            await asyncio.sleep(0.08)
            saved = await db.orders.get_by_idempotency_key("web-console", "client-disc-1")
            assert saved is not None
            assert saved["status"] == "waiting_code"
            assert saved["activation_id"] == "act_client_disc"


# ── P1-02: 幂等优先查询、失败状态回放、完整摘要校验与脱敏后回放 ──────────────


@pytest.mark.asyncio
async def test_add_p1_02_idempotency_first_replay_failure_v2_hash_and_scrubbed_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "add-p1-02-secret")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    from app.config import env
    env.reload()

    prov = _make_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    app = create_app(db_path=str(tmp_path / "add_p102.db"), enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. 库存不足首次返回 409 -> 同键重试必须依然返回 409（绝不返回 201 + failed！）
            prov.request_number = AsyncMock(side_effect=NoNumbersError("无号"))
            f1 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "idem-no-num"},
                json={"service": "whatsapp", "country": "Thailand", "external_ref": "ref-1"},
            )
            assert f1.status_code == 409
            assert f1.json()["error"]["code"] == "NO_NUMBERS"

            f2 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "idem-no-num"},
                json={"service": "whatsapp", "country": "Thailand", "external_ref": "ref-1"},
            )
            assert f2.status_code == 409
            assert f2.json()["error"]["code"] == "NO_NUMBERS"
            assert prov.request_number.await_count == 1

            # 2. 完整摘要覆盖 external_ref / metadata / strategy.max_attempts -> 改动任一字段返回 409 IDEMPOTENCY_CONFLICT
            for mutated in (
                {"service": "whatsapp", "country": "Thailand", "external_ref": "ref-CHANGED"},
                {"service": "whatsapp", "country": "Thailand", "external_ref": "ref-1", "metadata": {"tag": "x"}},
                {"service": "whatsapp", "country": "Thailand", "external_ref": "ref-1", "strategy": {"max_attempts": 5}},
            ):
                conflict = await client.post(
                    "/api/v1/orders",
                    headers={"Idempotency-Key": "idem-no-num"},
                    json=mutated,
                )
                assert conflict.status_code == 409
                assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            # 3. 已成功订单收码后脱敏，随后国家解析服务故障 -> 重试仍优先命中幂等记录返回 201 且不恢复旧验证码！
            prov.request_number = AsyncMock(
                return_value=Activation("act_ok_idem", "66812345678", 52, "Thailand", provider="smsbower", price=0.10)
            )
            prov.get_sms_code = AsyncMock(return_value=("OK", "889911"))
            ok1 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "idem-ok-1"},
                json={"service": "whatsapp", "country": "Thailand", "external_ref": "ok-ref", "metadata": {"k": 1}},
            )
            assert ok1.status_code == 201
            oid_ok = ok1.json()["order_id"]

            # 等码到达后脱敏
            svc: OrderService = app.state.container.order_service
            await svc.wait_for_code(oid_ok, timeout=3)
            assert (await client.delete(f"/api/v1/orders/{oid_ok}/code")).status_code == 200

            # 注入国家解析故障（模拟上游不可用），同键重试绝不报 502 且返回已脱敏订单
            prov.resolve_country = AsyncMock(side_effect=RuntimeError("upstream country API down"))
            ok2 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "idem-ok-1"},
                json={"service": "whatsapp", "country": "Thailand", "external_ref": "ok-ref", "metadata": {"k": 1}},
            )
            assert ok2.status_code == 201
            assert ok2.json()["order_id"] == oid_ok
            assert ok2.json()["code_scrubbed"] is True
            assert ok2.json()["latest_code"] == ""


# ── P1-03: 永久项目身份防复用、显式密钥轮转与已连 SSE 实时撤销 ────────────────


@pytest.mark.asyncio
async def test_add_p1_03_permanent_project_id_rotation_and_live_sse_revocation(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "add-p1-03-secret")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "p103-admin")
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "hooks.partner.internal")
    from app.config import env
    env.reload()

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    app = create_app(db_path=str(tmp_path / "add_p103.db"), enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as ext:
            await admin.post("/api/v1/auth/login", json={"password": "p103-admin"})

            # 1. 创建项目 Partner A，产生订单、私有黑名单与 Webhook 投递记录
            p_old = (
                await admin.post(
                    "/api/v1/projects/keys",
                    json={"name": "Partner A", "default_webhook_url": "https://hooks.partner.internal/a"},
                )
            ).json()
            old_pid = p_old["project_id"]
            old_key = p_old["api_key"]
            assert old_pid == "partner_a"

            ord_old = (
                await ext.post("/api/v1/orders", headers={"X-API-Key": old_key}, json={"service": "whatsapp", "country": "Thailand"})
            ).json()
            await ext.post("/api/v1/blacklist", headers={"X-API-Key": old_key}, json={"phone": "+66899998888"})

            # 2. 删除旧密钥后同名新建 -> 分配全新 project_id，绝不继承旧订单、旧黑名单或旧投递记录！
            assert (await admin.delete(f"/api/v1/projects/keys/{old_pid}")).status_code == 200
            dup_explicit = await admin.post(
                "/api/v1/projects/keys",
                json={"name": "Partner A", "project_id": old_pid},
            )
            assert dup_explicit.status_code == 409

            p_new = (await admin.post("/api/v1/projects/keys", json={"name": "Partner A"})).json()
            assert p_new["project_id"] != old_pid
            new_key = p_new["api_key"]
            assert (await ext.get("/api/v1/orders", headers={"X-API-Key": new_key})).json()["total"] == 0
            assert (await ext.get(f"/api/v1/orders/{ord_old['order_id']}", headers={"X-API-Key": new_key})).status_code == 404
            assert (await ext.get("/api/v1/blacklist", headers={"X-API-Key": new_key})).json()["total"] == 0
            assert (await ext.get("/api/v1/projects/webhooks/deliveries", headers={"X-API-Key": new_key})).json()["total"] == 0

            # 3. 明确密钥轮转：建立 SSE 长连接 -> 轮转密钥 -> 旧 SSE 自动断开、旧密钥 401、新密钥继承历史订单！
            rot_proj = (await admin.post("/api/v1/projects/keys", json={"name": "RotateMe", "project_id": "rot_p"})).json()
            rot_old_key = rot_proj["api_key"]
            rot_ord = (
                await ext.post("/api/v1/orders", headers={"X-API-Key": rot_old_key}, json={"service": "whatsapp", "country": "Thailand"})
            ).json()

            # 打开一条使用 rot_old_key 的原生 ASGI SSE 连接
            sse_connected = asyncio.Event()
            sse_chunks: list[bytes] = []
            sse_finished = asyncio.Event()

            async def asgi_receive():
                await asyncio.sleep(10)
                return {"type": "http.disconnect"}

            async def asgi_send(message):
                if message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    if body:
                        sse_chunks.append(body)
                        if b"event: connected" in body:
                            sse_connected.set()
                    if not message.get("more_body", False):
                        sse_finished.set()

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/v1/events/stream",
                "raw_path": b"/api/v1/events/stream",
                "query_string": b"",
                "headers": [(b"x-api-key", rot_old_key.encode("utf-8"))],
                "client": ("127.0.0.1", 50001),
                "server": ("127.0.0.1", 9090),
            }
            sse_task = asyncio.create_task(app(scope, asgi_receive, asgi_send))
            await asyncio.wait_for(sse_connected.wait(), timeout=2.0)

            # 执行轮转：应立刻唤醒并关闭已建立的旧 SSE 连接，后续事件绝不推送给旧连接
            rot_resp = await admin.post("/api/v1/projects/keys/rot_p/rotate")
            assert rot_resp.status_code == 200
            rot_new_key = rot_resp.json()["api_key"]

            await asyncio.wait_for(sse_finished.wait(), timeout=2.0)
            await asyncio.gather(sse_task, return_exceptions=True)

            # 轮转后再发布事件，旧 SSE 绝不会收到
            app.state.container.event_bus.publish(
                "order.code_received",
                {"order_id": rot_ord["order_id"], "project_id": "rot_p", "latest_code": "999111"},
            )
            assert b"999111" not in b"".join(sse_chunks)

            # 旧密钥 401，新密钥保留历史订单访问权
            assert (await ext.get("/api/v1/orders", headers={"X-API-Key": rot_old_key})).status_code == 401
            assert (await ext.get(f"/api/v1/orders/{rot_ord['order_id']}", headers={"X-API-Key": rot_new_key})).status_code == 200


# ── P1-04: SQLite WAL 独立读连接隔离未提交写入与回滚，复合读取一致快照 ────────


@pytest.mark.asyncio
async def test_add_p1_04_wal_read_isolation_blocks_dirty_reads_and_keeps_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "add-p1-04-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "wal_iso.db"))
    await db.open()
    try:
        assert db.conn._read_conn is not None
        await db.orders.create(
            order_id="ord_iso",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_code",
        )

        # 1. 启动写事务修改为 code_received 并插入短信，但暂不提交（准备回滚）
        entered_tx = asyncio.Event()
        release_tx = asyncio.Event()

        async def rolling_back_writer():
            with pytest.raises(RuntimeError, match="force rollback"):
                async with db.conn.transaction() as raw:
                    await raw.execute(
                        "UPDATE sms_orders SET status='code_received', code_count=1 WHERE order_id='ord_iso'"
                    )
                    await raw.execute(
                        "INSERT INTO sms_messages (order_id, seq, code, raw_text) VALUES ('ord_iso', 1, 'enc', 'enc')"
                    )
                    entered_tx.set()
                    await release_tx.wait()
                    raise RuntimeError("force rollback")

        writer_task = asyncio.create_task(rolling_back_writer())
        await asyncio.wait_for(entered_tx.wait(), timeout=2.0)

        # 在写事务未提交前并发读取：独立 WAL 只读连接绝不可见 code_received 或未提交短信！
        mid_read = await db.orders.get("ord_iso", include_messages=True)
        assert mid_read is not None
        assert mid_read["status"] == "waiting_code"
        assert mid_read["messages"] == []

        # 放行写事务触发回滚后，再次读取依然是干净的 waiting_code
        release_tx.set()
        await writer_task
        after_rb = await db.orders.get("ord_iso", include_messages=True)
        assert after_rb is not None
        assert after_rb["status"] == "waiting_code"
        assert after_rb["messages"] == []

        # 2. 真实提交写入后，单快照同时可见 code_received 与短信明细
        await db.orders.append_code("ord_iso", "246810")
        committed = await db.orders.get("ord_iso", include_messages=True)
        assert committed is not None
        assert committed["status"] == "code_received"
        assert len(committed["messages"]) == 1
        assert committed["messages"][0]["code"] == "246810"
    finally:
        await db.close()


# ── P1-05: 收码率仅认实际短信；实付/退款/净支出基于激活台账且项目页口径一致 ──


@pytest.mark.asyncio
async def test_add_p1_05_accurate_code_rate_real_cost_partial_refund_and_projects_parity(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "add-p1-05-secret")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "add_p105.db"))
    await db.open()
    try:
        await db.api_keys.create(
            project_id="proj_fin",
            name="Finance Proj",
            key_prefix="sms_k_fin",
            key_hash="hash_fin_1",
            scopes=["orders:rw"],
        )

        # 1. 零短信完成单（actual_cost=0.10）-> 不应计入 received_count，计入 completed_without_code_count 与实付
        await db.orders.create(
            order_id="o_zero_comp", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", project_id="proj_fin", status="completed",
        )
        a1 = await db.activations.record_intent(order_id="o_zero_comp", attempt_no=1, provider="smsbower", country_id="52", service_code="wa")
        await db.activations.bind_acquired_atomic(a1, order_id="o_zero_comp", activation_id="act_f1", phone_raw="6681", actual_cost=0.10, cost_status="actual", currency="USD", state="completed")

        # 2. 未收码且不可退款的过期单（actual_cost=0.25）-> 必须计入实付与净支出（此前被漏记为 0！）
        await db.orders.create(
            order_id="o_exp_unref", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", project_id="proj_fin", status="expired",
        )
        await db.orders.update("o_exp_unref", refund_state="unrefundable")
        a2 = await db.activations.record_intent(order_id="o_exp_unref", attempt_no=1, provider="smsbower", country_id="52", service_code="wa")
        await db.activations.bind_acquired_atomic(a2, order_id="o_exp_unref", activation_id="act_f2", phone_raw="6682", actual_cost=0.25, cost_status="actual", currency="USD", state="unrefundable")

        # 3. 多次购买订单：第 1 次拉黑经契约确认全退 0.15，第 2 次实付 0.20 并收到短信（之后脱敏仍应计入收码）
        await db.orders.create(
            order_id="o_multi", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", project_id="proj_fin", status="waiting_code",
        )
        a3_1 = await db.activations.record_intent(order_id="o_multi", attempt_no=1, provider="smsbower", country_id="52", service_code="wa")
        await db.activations.bind_acquired(a3_1, activation_id="act_f3_1", phone_raw="6683", actual_cost=0.15, cost_status="actual", currency="USD", state="active")
        await db.activations.update_state(a3_1, state="refund_confirmed", refund_amount=0.15, refund_amount_source="full_refund_contract")
        a3_2 = await db.activations.record_intent(order_id="o_multi", attempt_no=2, provider="smsbower", country_id="52", service_code="wa")
        await db.activations.bind_acquired_atomic(a3_2, order_id="o_multi", activation_id="act_f3_2", phone_raw="6684", actual_cost=0.20, cost_status="actual", currency="USD", state="completed")
        await db.orders.append_code("o_multi", "556677")
        await db.orders.scrub_order_code("o_multi")  # 脱敏后仍保留短信记录/计数

        # 4. 部分退款单（实付 0.30，仅退回 0.10，净支出 0.20）
        await db.orders.create(
            order_id="o_partial", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", project_id="proj_fin", status="cancelled",
        )
        await db.orders.update("o_partial", refund_state="confirmed")
        a4 = await db.activations.record_intent(order_id="o_partial", attempt_no=1, provider="smsbower", country_id="52", service_code="wa")
        await db.activations.bind_acquired_atomic(a4, order_id="o_partial", activation_id="act_f4", phone_raw="6685", actual_cost=0.30, cost_status="actual", currency="USD", state="active")
        await db.activations.update_state(a4, state="refund_confirmed", refund_amount=0.10, refund_amount_source="provider_response")

        # 5. 第二币种 RUB 估算成本订单
        await db.orders.create(
            order_id="o_rub", provider="smsbower", service="telegram", service_code="tg",
            country_id="0", project_id="proj_fin", status="completed",
        )
        a5 = await db.activations.record_intent(order_id="o_rub", attempt_no=1, provider="smsbower", country_id="0", service_code="tg", currency="RUB")
        await db.activations.bind_acquired_atomic(a5, order_id="o_rub", activation_id="act_f5", phone_raw="7900", estimated_cost=12.5, cost_status="estimated", currency="RUB", state="completed")

        stats = await db.orders.get_stats(project_id="proj_fin")
        today = stats["today"]
        assert today["total"] == 5
        assert today["received_count"] == 1  # 仅 o_multi 收到真实短信，零短信完成单不计入！
        assert today["completed_count"] == 2
        assert today["completed_without_code_count"] == 2
        # 多币种（USD + RUB）未配置汇率时，顶层不跨币种相加，明细由 by_currency 分别精准汇总
        assert today["is_multi_currency"] is True
        assert today["currency"] is None
        assert today["actual_cost_total"] is None
        assert today["cost_total"] is None
        assert today["unreceived_paid_count"] >= 3  # o_zero_comp, o_exp_unref, o_partial, o_rub

        by_ccy = {c["currency"]: c for c in today["by_currency"]}
        # USD 毛实付 = 0.10 + 0.25 + 0.15 + 0.20 + 0.30 = 1.00，已核实退款 = 0.15 + 0.10 = 0.25，净实付 = 0.75
        assert by_ccy["USD"]["actual_cost_total"] == pytest.approx(1.00)
        assert by_ccy["USD"]["refunded_amount_total"] == pytest.approx(0.25)
        assert by_ccy["USD"]["net_actual_cost_total"] == pytest.approx(0.75)
        assert by_ccy["RUB"]["estimated_cost_total"] == pytest.approx(12.5)

        # 项目接入页与统计 API 口径完全一致（含多币种明细）
        keys_list = await db.api_keys.list()
        fin_proj = next(k for k in keys_list if k["project_id"] == "proj_fin")
        assert fin_proj["received_count"] == today["received_count"]
        assert fin_proj["is_multi_currency"] is True
        assert fin_proj["cost_total"] is None
        assert fin_proj["by_currency"] == today["by_currency"]
    finally:
        await db.close()


# ── P2-01: 中文及任意 Unicode 管理员口令正常登录与 401 校验 ──────────────────


@pytest.mark.asyncio
async def test_add_p2_01_chinese_unicode_admin_password_login(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "add-p2-01-secret")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "中文管理员口令#2026!")
    from app.config import env
    env.reload()

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    app = create_app(db_path=str(tmp_path / "add_p201.db"), enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            bad_cn = await client.post("/api/v1/auth/login", json={"password": "错误的中文密码"})
            assert bad_cn.status_code == 401
            assert bad_cn.json()["error"]["code"] == "AUTH_INVALID_PASSWORD"

            bad_ascii = await client.post("/api/v1/auth/login", json={"password": "wrong-ascii"})
            assert bad_ascii.status_code == 401

            ok_cn = await client.post("/api/v1/auth/login", json={"password": "中文管理员口令#2026!"})
            assert ok_cn.status_code == 200
            assert "sms_hub_session" in client.cookies
            assert (await client.get("/api/v1/settings")).status_code == 200


# ── P2-02: 启动阶段任意位置失败均按逆序释放已打开的数据库连接、客户端与后台任务 ─


@pytest.mark.asyncio
async def test_add_p2_02_startup_failure_releases_db_clients_and_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "add-p2-02-secret")
    from app.config import env
    env.reload()

    # 场景 A: 迁移阶段抛错 -> Database.open() 与 lifespan 必须立即关闭已打开的 SQLite 连接
    async def boom_migrate(_conn):
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(db_pkg, "run_migrations", boom_migrate)
    reg_a = ProviderRegistry(default="smsbower")
    reg_a.init_provider("smsbower", _make_provider())
    app_a = create_app(db_path=str(tmp_path / "fail_mig.db"), enable_wa_plugin=False, registry_instance=reg_a, skip_startup_probe=True)
    with pytest.raises(RuntimeError, match="migration exploded"):
        async with app_a.router.lifespan_context(app_a):
            pass
    # 确认没有残留已打开的 DB 连接
    assert getattr(app_a.state, "db", None) is None or not app_a.state.db.conn.is_open

    # 恢复 run_migrations
    from app.db.migrations import run_migrations as real_run_migrations
    monkeypatch.setattr(db_pkg, "run_migrations", real_run_migrations)

    # 场景 B: recover_on_startup 阶段抛错 -> 数据库连接与 provider 客户端全部释放
    prov_b = _make_provider()
    reg_b = ProviderRegistry(default="smsbower")
    reg_b.init_provider("smsbower", prov_b)
    monkeypatch.setattr(OrderService, "recover_on_startup", AsyncMock(side_effect=RuntimeError("recover exploded")))
    app_b = create_app(db_path=str(tmp_path / "fail_rec.db"), enable_wa_plugin=False, registry_instance=reg_b, skip_startup_probe=True)
    with pytest.raises(RuntimeError, match="recover exploded"):
        async with app_b.router.lifespan_context(app_b):
            pass
    assert not app_b.state.db.conn.is_open
    prov_b.close.assert_awaited()

    # 场景 C: 插件启动阶段抛错（此时 webhook_dispatcher 和 retention_service 均已启动）-> 全部后台任务与 DB 释放
    from app.plugins import PluginManager
    monkeypatch.setattr(OrderService, "recover_on_startup", AsyncMock(return_value=0))
    monkeypatch.setattr(PluginManager, "startup_all", AsyncMock(side_effect=RuntimeError("plugin exploded")))
    prov_c = _make_provider()
    reg_c = ProviderRegistry(default="smsbower")
    reg_c.init_provider("smsbower", prov_c)
    app_c = create_app(db_path=str(tmp_path / "fail_plug.db"), enable_wa_plugin=False, registry_instance=reg_c, skip_startup_probe=True)
    with pytest.raises(RuntimeError, match="plugin exploded"):
        async with app_c.router.lifespan_context(app_c):
            pass
    assert not app_c.state.db.conn.is_open
    prov_c.close.assert_awaited()
