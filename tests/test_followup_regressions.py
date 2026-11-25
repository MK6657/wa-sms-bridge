"""针对 REVIEW_FOLLOWUP_2026-11-25.md 全部 9 项复核问题的专项回归测试套件。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import httpcore
import httpx
import pytest

from app.core.event_bus import EventBus
from app.core.order_service import AcquireOptions, OrderService
from app.core.retention_service import RetentionService
from app.core.url_guard import (
    PinnedIPNetworkBackend,
    pin_webhook_host,
    validate_webhook_url_async,
)
from app.core.webhook_dispatcher import WebhookDispatcher, compute_webhook_signature
from app.db import Database
from app.db.migrations import _encrypt_legacy_plaintext_secrets, run_migrations
from app.errors import AppError
from app.main import create_app
from app.models import PhoneInput
from app.price_policy import effective_price_max
from app.providers.base import Activation, CancelOutcome, ProviderCapabilities
from app.providers.registry import ProviderRegistry


def _make_provider(phone: str = "66812345678", activation_id: str = "act_fu_1"):
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


# ── P0-01 + P1-03(历史退款): 已有 v1 数据库升级后外键完整、数据无损、可写入新短信 ──


@pytest.mark.asyncio
async def test_followup_p0_01_v1_database_upgrade_preserves_messages_fk_and_allows_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "fu-p0-01-secret")
    from app.config import env
    env.reload()

    db_file = str(tmp_path / "legacy_v1.db")
    # 构造真实的 v1 初始数据库（全局 idempotency_key UNIQUE、旧 blacklist、sms_messages 引用 sms_orders）
    async with aiosqlite.connect(db_file) as conn:
        await conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now')),
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'smsbower',
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(phone, provider)
            );
            CREATE TABLE sms_orders (
                order_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                project_id TEXT NOT NULL DEFAULT 'web-console',
                external_ref TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'smsbower',
                service TEXT NOT NULL DEFAULT 'whatsapp',
                service_code TEXT NOT NULL DEFAULT 'wa',
                country_id TEXT NOT NULL DEFAULT '',
                country_label TEXT NOT NULL DEFAULT '',
                country_iso2 TEXT NOT NULL DEFAULT '',
                country_calling_code TEXT NOT NULL DEFAULT '',
                activation_id TEXT NOT NULL DEFAULT '',
                phone_raw TEXT NOT NULL DEFAULT '',
                phone_e164 TEXT NOT NULL DEFAULT '',
                phone_national TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'acquiring',
                latest_code TEXT NOT NULL DEFAULT '',
                code_scrubbed INTEGER NOT NULL DEFAULT 0,
                code_count INTEGER NOT NULL DEFAULT 0,
                max_price REAL,
                cost REAL,
                cost_is_estimated INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'USD',
                wait_timeout INTEGER NOT NULL DEFAULT 180,
                expires_at TEXT NOT NULL DEFAULT '',
                attempts_used INTEGER NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                webhook_url TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE sms_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL REFERENCES sms_orders(order_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL DEFAULT 1,
                code TEXT NOT NULL DEFAULT '',
                raw_text TEXT NOT NULL DEFAULT '',
                received_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO schema_version (version, description) VALUES (1, 'v1 initial');
            INSERT INTO blacklist (phone, provider, reason) VALUES ('+66800009999', 'smsbower', 'v1 bad');
            INSERT INTO sms_orders (
                order_id, idempotency_key, project_id, provider, service, service_code,
                country_id, status, latest_code, code_count
            ) VALUES (
                'ord_v1_ok', 'idem_v1', 'proj_old', 'smsbower', 'whatsapp', 'wa',
                '52', 'completed', '112233', 1
            );
            INSERT INTO sms_messages (order_id, seq, code, raw_text) VALUES ('ord_v1_ok', 1, '112233', '112233');
            INSERT INTO sms_orders (
                order_id, project_id, provider, service, service_code, country_id, activation_id, status
            ) VALUES (
                'ord_v1_cancel', 'proj_old', 'smsbower', 'whatsapp', 'wa', '52', 'act_v1_cancel', 'cancelled'
            );
            """
        )
        await conn.commit()

    # 打开 Database 触发 v1 -> v2 -> v3 迁移
    db = Database(db_file)
    await db.open()
    try:
        fk_errors = await db.conn.fetchall("PRAGMA foreign_key_check")
        assert fk_errors == []

        fk_list = await db.conn.fetchall("PRAGMA foreign_key_list(sms_messages)")
        assert {r["table"] for r in fk_list} == {"sms_orders"}

        dangling = await db.conn.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%_sms_orders_v1%'"
        )
        assert dangling == []

        # 原有 v1 订单和短信数据完整保留且原地加密
        v1_order = await db.orders.get("ord_v1_ok", include_messages=True)
        assert v1_order is not None
        assert len(v1_order["messages"]) == 1
        assert v1_order["messages"][0]["code"] == "112233"

        # 复核 P1-03: v1 历史 cancelled 订单不能被无条件标记为已确认退款
        v1_cancelled = await db.orders.get("ord_v1_cancel")
        assert v1_cancelled is not None
        assert v1_cancelled["refund_state"] == "unknown"
        stats = await db.orders.get_stats()
        assert stats["today"]["refund_confirmed_count"] == 0

        # 迁移后新建订单并写入首条短信必须成功（不再抛 no such table: main._sms_orders_v1）
        await db.orders.create(
            order_id="ord_after_v1",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_code",
        )
        appended = await db.orders.append_code("ord_after_v1", "556677")
        assert appended is not None
        ord_after, msg = appended
        assert ord_after["latest_code"] == "556677"
        assert msg["seq"] == 1
    finally:
        await db.close()

    # 模拟重启再次打开数据库，确认幂等且继续可写
    db2 = Database(db_file)
    await db2.open()
    try:
        assert await db2.conn.fetchall("PRAGMA foreign_key_check") == []
        fetched = await db2.orders.get("ord_after_v1", include_messages=True)
        assert fetched is not None and fetched["latest_code"] == "556677"
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_followup_p0_01_broken_v2_repaired_by_v3_and_retryable_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "fu-p0-01-repair")
    from app.config import env
    env.reload()

    # 1. 构造已经被错误标记为 v2、且 sms_messages 外键指向不存在表 _sms_orders_v1 的受损数据库
    broken_file = str(tmp_path / "broken_v2.db")
    async with aiosqlite.connect(broken_file) as conn:
        await conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now')),
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE sms_orders (
                order_id TEXT PRIMARY KEY,
                idempotency_key TEXT,
                request_hash TEXT NOT NULL DEFAULT '',
                project_id TEXT NOT NULL DEFAULT 'web-console',
                external_ref TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'smsbower',
                service TEXT NOT NULL DEFAULT 'whatsapp',
                service_code TEXT NOT NULL DEFAULT 'wa',
                country_id TEXT NOT NULL DEFAULT '',
                country_label TEXT NOT NULL DEFAULT '',
                country_iso2 TEXT NOT NULL DEFAULT '',
                country_calling_code TEXT NOT NULL DEFAULT '',
                activation_id TEXT NOT NULL DEFAULT '',
                phone_raw TEXT NOT NULL DEFAULT '',
                phone_e164 TEXT NOT NULL DEFAULT '',
                phone_national TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'acquiring',
                version INTEGER NOT NULL DEFAULT 1,
                settlement_state TEXT NOT NULL DEFAULT 'none',
                refund_state TEXT NOT NULL DEFAULT 'none',
                latest_code TEXT NOT NULL DEFAULT '',
                code_received_at TEXT NOT NULL DEFAULT '',
                code_scrubbed INTEGER NOT NULL DEFAULT 0,
                code_count INTEGER NOT NULL DEFAULT 0,
                resend_base_code TEXT NOT NULL DEFAULT '',
                resend_seen_wait INTEGER NOT NULL DEFAULT 0,
                max_price REAL,
                quoted_max_price REAL,
                cost REAL,
                actual_cost REAL,
                cost_status TEXT NOT NULL DEFAULT 'unknown',
                cost_is_estimated INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'USD',
                wait_timeout INTEGER NOT NULL DEFAULT 180,
                expires_at TEXT NOT NULL DEFAULT '',
                attempts_used INTEGER NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                webhook_url TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT NOT NULL DEFAULT '',
                UNIQUE(project_id, idempotency_key)
            );
            CREATE TABLE sms_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL REFERENCES "_sms_orders_v1"(order_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL DEFAULT 1,
                code TEXT NOT NULL DEFAULT '',
                raw_text TEXT NOT NULL DEFAULT '',
                received_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO schema_version (version, description) VALUES (1, 'v1'), (2, 'broken v2');
            INSERT INTO sms_orders (order_id, provider, service, service_code, country_id, status, code_count)
            VALUES ('ord_broken_1', 'smsbower', 'whatsapp', 'wa', '52', 'completed', 1);
            INSERT INTO sms_messages (order_id, seq, code, raw_text) VALUES ('ord_broken_1', 1, '909090', '909090');
            """
        )
        await conn.commit()

    # 2. 模拟迁移中途失败 -> 回滚且不记录 v3，可安全重试
    import app.db.migrations as mig_mod
    real_verify = mig_mod._verify_schema_integrity
    fail_once = {"triggered": False}

    async def flaky_verify(c):
        if not fail_once["triggered"]:
            fail_once["triggered"] = True
            raise RuntimeError("injected mid-migration failure")
        await real_verify(c)

    monkeypatch.setattr(mig_mod, "_verify_schema_integrity", flaky_verify)
    async with aiosqlite.connect(broken_file) as conn:
        conn.row_factory = aiosqlite.Row
        with pytest.raises(RuntimeError, match="injected mid-migration failure"):
            await run_migrations(conn)
        cur = await conn.execute("SELECT version FROM schema_version")
        versions = {int(r["version"]) for r in await cur.fetchall()}
        assert 3 not in versions  # 回滚成功，未写版本号

    # 3. 恢复校验后重试迁移 -> v3 自动修复损坏外键且数据无损、新短信可写
    db = Database(broken_file)
    await db.open()
    try:
        assert await db.conn.fetchall("PRAGMA foreign_key_check") == []
        fk_list = await db.conn.fetchall("PRAGMA foreign_key_list(sms_messages)")
        assert {r["table"] for r in fk_list} == {"sms_orders"}

        old_order = await db.orders.get("ord_broken_1", include_messages=True)
        assert old_order is not None and old_order["messages"][0]["code"] == "909090"

        await db.orders.create(
            order_id="ord_repaired_new",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_code",
        )
        res = await db.orders.append_code("ord_repaired_new", "123123")
        assert res is not None and res[0]["latest_code"] == "123123"
    finally:
        await db.close()


# ── P0-02: Webhook Outbox 加密存储 + 投递/手工/定时脱敏全链路清零（含 SSE 历史） ──


@pytest.mark.asyncio
async def test_followup_p0_02_outbox_encrypted_and_scrub_clears_all_tables_and_sse(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "fu-p0-02-secret")
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "hooks.partner.internal")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "p002_scrub.db"))
    await db.open()
    bus = EventBus()
    prov = _make_provider()
    prov.get_sms_code = AsyncMock(return_value=("OK", "987654"))
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)

    svc = OrderService(db, reg, bus, poll_intervals=(0.01, 0.01, 0.01))
    dispatcher = WebhookDispatcher(db, retry_delays=(0.01,), poll_interval=0.05)
    retention = RetentionService(db, svc)

    try:
        # 场景 A: 收码后 Outbox 密文落盘（无明文），手工脱敏后所有表与 SSE 回放彻底清零且不再外发
        anchor_evt = bus.publish("anchor", {"order_id": "anchor"})
        order_a = await svc.create_order(
            country="Thailand",
            service="whatsapp",
            webhook_url="https://hooks.partner.internal/hook",
        )
        oid_a = order_a["order_id"]
        received_a = await svc.wait_for_code(oid_a, timeout=3)
        assert received_a["latest_code"] == "987654"

        raw_wh_a = await db.conn.fetchall("SELECT * FROM webhook_deliveries WHERE order_id=? AND event_type='order.code_received'", (oid_a,))
        assert len(raw_wh_a) == 1
        assert raw_wh_a[0]["contains_code"] == 1
        assert raw_wh_a[0]["payload_json"].startswith("e_")
        assert "987654" not in raw_wh_a[0]["payload_json"]

        # 执行脱敏
        await svc.scrub_order_code(oid_a)

        # 检查所有持久化表均不含明文或未清理负载
        raw_ord = await db.conn.fetchone("SELECT latest_code, resend_base_code, code_scrubbed FROM sms_orders WHERE order_id=?", (oid_a,))
        raw_msgs = await db.conn.fetchall("SELECT code, raw_text FROM sms_messages WHERE order_id=?", (oid_a,))
        raw_wh_after = await db.conn.fetchall("SELECT status, payload_json, payload_scrubbed FROM webhook_deliveries WHERE order_id=? AND event_type='order.code_received'", (oid_a,))
        assert raw_ord["latest_code"] == "" and raw_ord["code_scrubbed"] == 1
        assert all(m["code"] == "" and m["raw_text"] == "" for m in raw_msgs)
        assert raw_wh_after[0]["status"] == "scrubbed"
        assert raw_wh_after[0]["payload_scrubbed"] == 1
        assert raw_wh_after[0]["payload_json"] == "{}"

        # 检查 SSE 环形历史回放不再包含旧验证码
        replayed = bus.replay_since(anchor_evt.id)
        for ev in replayed:
            assert "987654" not in str(ev.to_payload())

        # Dispatcher 不会再投递已脱敏的验证码事件
        sent_payloads: list[bytes] = []

        async def capture_post(url, *, content, headers):
            sent_payloads.append(content)
            resp = MagicMock(status_code=200, text="ok")
            return resp

        dispatcher._client.post = AsyncMock(side_effect=capture_post)
        await dispatcher.process_due_once()
        await dispatcher.drain_inflight()
        assert all(b"987654" not in body for body in sent_payloads)

        # 场景 B: 投递成功后，Outbox 立即清空验证码负载副本
        prov.get_sms_code = AsyncMock(return_value=("OK", "445566"))
        order_b = await svc.create_order(
            country="Thailand",
            service="whatsapp",
            webhook_url="https://hooks.partner.internal/hook",
        )
        oid_b = order_b["order_id"]
        await svc.wait_for_code(oid_b, timeout=3)
        sent_payloads.clear()
        await dispatcher.process_due_once()
        await dispatcher.drain_inflight()
        assert any(b"445566" in body for body in sent_payloads)

        raw_wh_b = await db.conn.fetchone(
            "SELECT status, payload_json, payload_scrubbed FROM webhook_deliveries WHERE order_id=? AND event_type='order.code_received'",
            (oid_b,),
        )
        assert raw_wh_b["status"] == "delivered"
        assert raw_wh_b["payload_scrubbed"] == 1
        assert raw_wh_b["payload_json"] == "{}"

        # 场景 C: RetentionService 定时清理同样同步清理 Outbox 与 SSE 历史环
        prov.get_sms_code = AsyncMock(return_value=("OK", "334455"))
        anchor_c = bus.publish("anchor_c", {"order_id": "anchor_c"})
        order_c = await svc.create_order(
            country="Thailand",
            service="whatsapp",
            webhook_url="https://hooks.partner.internal/hook",
        )
        oid_c = order_c["order_id"]
        await svc.wait_for_code(oid_c, timeout=3)
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        await db.orders.update(oid_c, expires_at=past, code_received_at=past)
        scrubbed_count = await retention.run_scrub_once(retention_hours=0)
        assert scrubbed_count >= 1

        raw_wh_c = await db.conn.fetchone(
            "SELECT status, payload_json, payload_scrubbed FROM webhook_deliveries WHERE order_id=? AND event_type='order.code_received'",
            (oid_c,),
        )
        assert raw_wh_c["status"] == "scrubbed"
        assert raw_wh_c["payload_json"] == "{}"
        for ev in bus.replay_since(anchor_c.id):
            assert "334455" not in str(ev.to_payload())

        # 场景 D: 历史明文 Outbox 记录迁移检查（pending 自动加密，delivered 自动清空）
        await db.conn.write(
            "INSERT INTO webhook_deliveries (delivery_id, event_id, event_type, order_id, project_id, target_url, payload_json, status) "
            "VALUES ('dlv_leg_pend', 'e1', 'order.code_received', 'ord_x', 'p1', 'https://hooks.partner.internal/h', "
            "'{\"data\":{\"latest_code\":\"777888\"}}', 'pending')"
        )
        await db.conn.write(
            "INSERT INTO webhook_deliveries (delivery_id, event_id, event_type, order_id, project_id, target_url, payload_json, status) "
            "VALUES ('dlv_leg_done', 'e2', 'order.code_received', 'ord_x', 'p1', 'https://hooks.partner.internal/h', "
            "'{\"data\":{\"latest_code\":\"666777\"}}', 'delivered')"
        )
        await _encrypt_legacy_plaintext_secrets(db.conn.raw)
        leg_pend = await db.conn.fetchone("SELECT payload_json, contains_code FROM webhook_deliveries WHERE delivery_id='dlv_leg_pend'")
        leg_done = await db.conn.fetchone("SELECT payload_json, payload_scrubbed FROM webhook_deliveries WHERE delivery_id='dlv_leg_done'")
        assert leg_pend["payload_json"].startswith("e_") and "777888" not in leg_pend["payload_json"]
        assert leg_pend["contains_code"] == 1
        assert leg_done["payload_json"] == "{}" and leg_done["payload_scrubbed"] == 1
    finally:
        await dispatcher.shutdown()
        await svc.shutdown()
        await db.close()


# ── P1-01: 取号中断孤儿激活退款、不确定购买人工核对、completing 恢复后重启轮询 ──


@pytest.mark.asyncio
async def test_followup_p1_01_recovery_from_ledger_uncertain_purchase_and_completing_poller(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "fu-p1-01-secret")
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "hooks.partner.internal")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "p101.db"))
    await db.open()
    prov = _make_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc = OrderService(db, reg, EventBus(), poll_intervals=(0.01, 0.01, 0.01))
    try:
        # 1. 复现断点：激活台账已写入 active + activation_id，但 sms_orders.activation_id 仍为空
        await db.orders.create(
            order_id="ord_orphan", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="acquiring", webhook_url="https://hooks.partner.internal/h",
        )
        act_id = await db.activations.record_intent(
            order_id="ord_orphan", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )
        await db.activations.bind_acquired(act_id, activation_id="act_orphan_99", phone_raw="66899990000", state="active")

        # 2. 复现断点：取号途中中断（台账停留在 purchasing、无 activation_id）
        await db.orders.create(
            order_id="ord_uncertain", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="acquiring", webhook_url="https://hooks.partner.internal/h",
        )
        unc_row = await db.activations.record_intent(
            order_id="ord_uncertain", attempt_no=1, provider="smsbower", country_id="52", service_code="wa"
        )

        # 3. 复现断点：completing 中断且 code_count=0，恢复为 waiting_code 后必须重启查码轮询
        future_exp = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        await db.orders.create(
            order_id="ord_completing", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="completing",
        )
        await db.orders.update("ord_completing", activation_id="act_comp_1", expires_at=future_exp)
        prov.get_sms_code = AsyncMock(return_value=("OK", "778899"))

        recovered = await svc.recover_on_startup()
        assert recovered >= 3

        # 验证 1：孤儿激活已向上游取消退款并结清
        prov.cancel_activation.assert_any_await("act_orphan_99")
        ord_orphan = await svc.get_order("ord_orphan")
        assert ord_orphan["status"] == "cancelled"
        assert ord_orphan["refund_state"] == "confirmed"
        assert ord_orphan["activations"][0]["state"] == "refund_confirmed"

        # 验证 2：purchasing 中断转为 uncertain_purchase + failed/uncertain + Outbox order.failed + 可人工核销
        ord_unc = await svc.get_order("ord_uncertain")
        assert ord_unc["status"] == "failed"
        assert ord_unc["settlement_state"] == "uncertain"
        assert ord_unc["error_code"] == "PURCHASE_UNCERTAIN"
        assert ord_unc["activations"][0]["state"] == "uncertain_purchase"
        unc_wh = await db.webhooks.list(order_id="ord_uncertain")
        assert any(w["event_type"] == "order.failed" for w in unc_wh)

        recon = await svc.list_reconciliation_items()
        assert any(a["id"] == unc_row for a in recon["uncertain_activations"])
        resolved = await svc.resolve_activation_manually(unc_row, state="refund_confirmed", note="已核对上游未扣费")
        assert resolved["state"] == "refund_confirmed"

        # 验证 3：completing 恢复为 waiting_code 后自动重启轮询并成功收码
        comp_order = await svc.wait_for_code("ord_completing", timeout=3)
        assert comp_order["status"] == "code_received"
        assert comp_order["latest_code"] == "778899"
    finally:
        await svc.shutdown()
        await db.close()


# ── P1-02: Webhook 连接绑定已批准 IP、禁用环境代理、异步 DNS 超时 ─────────


@pytest.mark.asyncio
async def test_followup_p1_02_pinned_ip_connection_no_trust_env_and_async_dns_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "fu-p1-02-secret")
    monkeypatch.delenv("WEBHOOK_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_NETS", "false")
    from app.config import env
    env.reload()

    # 1. WebhookDispatcher 关闭 trust_env（禁止环境代理绕过 SSRF 策略）且关闭重定向
    db = Database(str(tmp_path / "p102.db"))
    await db.open()
    dispatcher = WebhookDispatcher(db)
    try:
        assert dispatcher._client._trust_env is False
        assert dispatcher._client.follow_redirects is False
    finally:
        await dispatcher.shutdown()
        await db.close()

    # 2. PinnedIPNetworkBackend 强制使用已批准解析 IP 建立 TCP 连接，拒绝未绑定或内网 IP
    inner = MagicMock()
    inner.connect_tcp = AsyncMock(return_value="mock-stream")
    backend = PinnedIPNetworkBackend(inner=inner)

    with pin_webhook_host("hooks.example.com", ["93.184.216.34"], allow_private=False):
        stream = await backend.connect_tcp("hooks.example.com", 443)
        assert stream == "mock-stream"
    inner.connect_tcp.assert_awaited_once_with(
        "93.184.216.34", 443, timeout=None, local_address=None, socket_options=None
    )

    # 未经过 pin_webhook_host 审批的主机直接拒连
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("unpinned.example.com", 443)

    # 即便强行绑定到回环 IP，非 allow_private 模式依然拒连
    with pin_webhook_host("rebind.example.com", ["127.0.0.1"], allow_private=False):
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("rebind.example.com", 443)

    # 3. 异步 DNS 解析超时立即报可重试错误，不卡死事件循环
    loop = asyncio.get_running_loop()

    async def slow_dns(*_args, **_kwargs):
        await asyncio.sleep(5.0)
        return []

    monkeypatch.setattr(loop, "getaddrinfo", slow_dns)
    with pytest.raises(AppError) as exc:
        await validate_webhook_url_async("https://slow.example.com/hook", resolve_dns=True, dns_timeout=0.2)
    assert exc.value.code == "WEBHOOK_DNS_TIMEOUT"
    assert exc.value.retryable is True


# ── P1-03: 拒绝 price_max=0、向下截断不越界、精确锁价能力显式校验 ─────────


@pytest.mark.asyncio
async def test_followup_p1_03_zero_cap_rejected_floor_rounding_and_exact_price_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "fu-p1-03-secret")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    from app.config import env
    env.reload()

    # 1. price_max=0 或 target_price=0 直接报错，绝不省略上限
    with pytest.raises(ValueError):
        effective_price_max(price_max=0)
    with pytest.raises(ValueError):
        effective_price_max(target_price=0)

    # 2. 向下截断到 4 位小数：0.10006 -> 0.1000（绝不向上进位变成 0.1001 > 0.10006）
    assert effective_price_max(price_max=0.10006) == 0.1
    assert effective_price_max(target_price=0.10006, price_max=0.10006) == 0.1
    assert effective_price_max(target_price=0.10005, price_max=0.10006, price_float_enabled=True, price_float_percent=20) == 0.1

    # 3. 零容差：哪怕仅高 0.00004 也报错
    with pytest.raises(ValueError):
        effective_price_max(target_price=0.10005, price_max=0.10001)

    # 4. API 层拒绝 price_max=0 以及在 SMSBower 上要求 strict_exact_price=True
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    app = create_app(db_path=str(tmp_path / "p103.db"), enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_zero = await client.post(
                "/api/v1/orders",
                json={"service": "whatsapp", "country": "Thailand", "strategy": {"price_max": 0}},
            )
            assert r_zero.status_code == 422

            r_exact = await client.post(
                "/api/v1/orders",
                json={
                    "service": "whatsapp",
                    "country": "Thailand",
                    "strategy": {"target_price": 0.10, "price_lock": True, "strict_exact_price": True},
                },
            )
            assert r_exact.status_code == 422
            assert r_exact.json()["error"]["code"] == "EXACT_PRICE_LOCK_UNSUPPORTED"


# ── P1-04: 所有终态（含补偿不可退、后台未预期异常、重启取消）统一写 Outbox ─


@pytest.mark.asyncio
async def test_followup_p1_04_all_terminal_paths_enqueue_outbox_webhooks(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "fu-p1-04-secret")
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "hooks.partner.internal")
    from app.config import env
    env.reload()

    db = Database(str(tmp_path / "p104.db"))
    await db.open()
    prov = _make_provider()
    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", prov)
    svc = OrderService(db, reg, EventBus(), poll_intervals=(0.01, 0.01, 0.01))
    try:
        # 1. 后台补偿判断不可退款 (NO_ACTIVATION) -> cancelled/unrefundable 必须入队 order.cancelled
        await db.orders.create(
            order_id="ord_unref", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="compensation_pending", webhook_url="https://hooks.partner.internal/h",
        )
        await db.orders.update("ord_unref", activation_id="act_unref_1")
        prov.cancel_activation = AsyncMock(return_value="NO_ACTIVATION")
        assert await svc.reconcile_pending() >= 1
        wh_unref = await db.webhooks.list(order_id="ord_unref")
        assert len(wh_unref) == 1 and wh_unref[0]["event_type"] == "order.cancelled"

        # 2. 后台取号遇到未预期 RuntimeError -> failed 必须入队 order.failed
        prov.request_number = AsyncMock(side_effect=RuntimeError("unexpected crash"))
        ord_bg = await svc.create_order(
            country="Thailand",
            service="whatsapp",
            async_acquire=True,
            webhook_url="https://hooks.partner.internal/h",
        )
        await asyncio.sleep(0.08)
        bg_after = await svc.get_order(ord_bg["order_id"])
        assert bg_after["status"] == "failed"
        wh_bg = await db.webhooks.list(order_id=ord_bg["order_id"])
        assert len(wh_bg) == 1 and wh_bg[0]["event_type"] == "order.failed"

        # 3. 重启恢复取消无激活 acquiring 订单 -> cancelled 必须入队 order.cancelled
        await db.orders.create(
            order_id="ord_restart_cancel", provider="smsbower", service="whatsapp", service_code="wa",
            country_id="52", status="acquiring", webhook_url="https://hooks.partner.internal/h",
        )
        await svc.recover_on_startup()
        wh_rc = await db.webhooks.list(order_id="ord_restart_cancel")
        assert len(wh_rc) == 1 and wh_rc[0]["event_type"] == "order.cancelled"
    finally:
        await svc.shutdown()
        await db.close()


# ── P1-05: 服务端游标过期返回 resync_required，有效游标补发断线期间完成事件 ─


@pytest.mark.asyncio
async def test_followup_p1_05_sse_resync_required_and_query_cursor_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "fu-p1-05-secret")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    from app.config import env
    env.reload()

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    app = create_app(db_path=str(tmp_path / "p105.db"), enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)

    async def _collect_sse_until(query_string: bytes, cookie_val: str, stop_token: bytes) -> bytes:
        chunks: list[bytes] = []
        done = asyncio.Event()

        async def asgi_receive():
            await done.wait()
            return {"type": "http.disconnect"}

        async def asgi_send(message):
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    chunks.append(body)
                    if stop_token in b"".join(chunks):
                        done.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/events/stream",
            "raw_path": b"/api/v1/events/stream",
            "query_string": query_string,
            "headers": [(b"cookie", f"sms_hub_session={cookie_val}".encode("utf-8"))],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 9090),
        }
        task = asyncio.create_task(app(scope, asgi_receive, asgi_send))
        await asyncio.wait_for(done.wait(), timeout=3.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return b"".join(chunks)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin:
            await admin.post("/api/v1/auth/session")
            cookie_val = admin.cookies.get("sms_hub_session")
            assert cookie_val

            bus = app.state.container.event_bus
            e1 = bus.publish("order.acquired", {"order_id": "ord_s1", "project_id": "web-console"})
            bus.publish("order.code_received", {"order_id": "ord_s1", "project_id": "web-console", "latest_code": "246810"})
            bus.publish("order.completed", {"order_id": "ord_s1", "project_id": "web-console", "status": "completed"})

            # 1. 工作台携带 ?last_event_id= 续传，补回断线期间的收码与完成事件
            replayed = await _collect_sse_until(
                f"last_event_id={e1.id}".encode("utf-8"),
                cookie_val,
                b"order.completed",
            )
            assert b"246810" in replayed
            assert b"event: order.completed" in replayed

            # 2. 携带过期/重启前游标，服务端显式推送 resync_required 通知工作台全量补刷
            expired_stream = await _collect_sse_until(
                b"last_event_id=evt_0_expired_cursor",
                cookie_val,
                b"event: resync_required",
            )
            assert b'"resync_required": true' in expired_stream
            assert b"event: resync_required" in expired_stream


# ── P1-06: 清空 scopes 保持零权限；管理员 Token 严禁在 SSE query 中使用 ──


@pytest.mark.asyncio
async def test_followup_p1_06_empty_scopes_no_escalation_and_admin_token_blocked_in_query(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "fu-p1-06-secret")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-p106")
    monkeypatch.setenv("CONSOLE_ADMIN_TOKEN", "adm_super_token_123")
    from app.config import env
    env.reload()

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", _make_provider())
    app = create_app(db_path=str(tmp_path / "p106.db"), enable_wa_plugin=False, registry_instance=reg, skip_startup_probe=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as ext:
            await admin.post("/api/v1/auth/login", json={"password": "admin-p106"})

            # 1. 先创建只读项目密钥，再将 scopes 清空为 [] -> 立即失去所有权限，绝不回退默认读写权限
            ro = (
                await admin.post(
                    "/api/v1/projects/keys",
                    json={"name": "ReadOnly", "project_id": "ro_proj", "scopes": ["orders:r"]},
                )
            ).json()
            ro_key = ro["api_key"]
            assert (await ext.get("/api/v1/orders", headers={"X-API-Key": ro_key})).status_code == 200
            assert (
                await ext.post("/api/v1/orders", headers={"X-API-Key": ro_key}, json={"service": "whatsapp", "country": "Thailand"})
            ).status_code == 403

            cleared = await admin.patch("/api/v1/projects/keys/ro_proj", json={"scopes": []})
            assert cleared.status_code == 200
            assert cleared.json()["scopes"] == []

            # 清空后读、写、SSE 全部 403，绝不恢复 orders:rw / events:r
            assert (await ext.get("/api/v1/orders", headers={"X-API-Key": ro_key})).status_code == 403
            assert (
                await ext.post("/api/v1/orders", headers={"X-API-Key": ro_key}, json={"service": "whatsapp", "country": "Thailand"})
            ).status_code == 403
            assert (await ext.get("/api/v1/events/stream", headers={"X-API-Key": ro_key})).status_code == 403

            # 2. 创建时显式传 scopes=[] 同样保持空权限，不回退 DEFAULT_SCOPES
            empty_created = (
                await admin.post(
                    "/api/v1/projects/keys",
                    json={"name": "EmptyProj", "project_id": "empty_proj", "scopes": []},
                )
            ).json()
            assert empty_created["scopes"] == []
            assert (await ext.get("/api/v1/orders", headers={"X-API-Key": empty_created["api_key"]})).status_code == 403

            # 3. 管理员 Token 严禁通过 SSE 查询参数传递（防止泄露到访问日志/反代日志）
            q_resp = await ext.get("/api/v1/events/stream?api_key=adm_super_token_123")
            assert q_resp.status_code == 401
            assert q_resp.json()["error"]["code"] == "AUTH_ADMIN_TOKEN_IN_QUERY_FORBIDDEN"

            # 通过请求头传递管理员 Token 依然正常放行
            hdr_resp = await ext.get("/api/v1/settings", headers={"X-API-Key": "adm_super_token_123"})
            assert hdr_resp.status_code == 200
