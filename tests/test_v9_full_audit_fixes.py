"""v9 全盘深度审计回归测试套件。

覆盖：
1. SQLite BEGIN IMMEDIATE 取消自愈与迁移后无悬挂隐式事务
2. continuous_poll 复用未购号 no_numbers 意图行 + 致命上游鉴权错误立即熔断
3. 批量过期脱敏保留非终态订单 resend_base_digest + 轮转密钥 HMAC 校验兼容
4. 同步下单并发取消绝不伪造 201 成功 + 买号后绑定异常持久化 activation_id 供后台追回退款
5. uncertain_purchase 人工核销实扣/实退金额准确计入 /stats 净消耗 + 拒绝 NaN/Infinity 输入
6. PinnedIPNetworkBackend 阻断云元数据/NAT64/IPv6 内嵌地址重绑定 + Webhook 测试密钥同源隔离
7. v2 篡改密文快速失败跳过 100k PBKDF2 + 未授信代理忽略 XFF + CORS 与后台 CSRF 隔离 + 多词 STATUS_OK/JWT 日志脱敏
8. 连字符/空格分节验证码提取、跨国区号防误拼接、SmsBower/5sim 国家解析抗误判与编排器 BAD_STATUS 核实结算
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
import socket
from unittest.mock import AsyncMock, MagicMock

from cryptography.fernet import InvalidToken
import httpcore
import httpx
import pytest

from app.config import env
from app.core.url_guard import PinnedIPNetworkBackend, pin_webhook_host, validate_webhook_url_async
from app.db.connection import DatabaseConnection
from app.db.migrations import run_migrations
from app.db.orders_repo import OrdersRepo
from app.errors import AppError
from app.log_redaction import redact_text
from app.main import create_app
from app.orchestrator import RegisterOrchestrator
from app.phone_normalizer import build_phone_input
from app.providers.base import (
    Activation,
    NoNumbersError,
    ProviderCapabilities,
    SmsProviderError,
    extract_otp_code,
)
from app.providers.five_sim import FiveSimProvider
from app.providers.registry import ProviderRegistry
from app.providers.smsbower import SmsBowerProvider


@pytest.mark.asyncio
async def test_db_transaction_begin_cancel_recovery_and_migrations_clean_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v9-db-tx-secret-key")
    env.reload()

    db = DatabaseConnection(str(tmp_path / "v9_tx.db"))
    await db.open()
    try:
        await run_migrations(db.raw)
        assert db.raw.in_transaction is False

        orig_execute = db.raw.execute
        triggered = False

        async def _cancel_after_begin(sql: str, parameters=()):
            nonlocal triggered
            cur = await orig_execute(sql, parameters)
            if not triggered and sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                triggered = True
                raise asyncio.CancelledError()
            return cur

        monkeypatch.setattr(db.raw, "execute", _cancel_after_begin)

        with pytest.raises(asyncio.CancelledError):
            async with db.transaction():
                pass

        # 恢复正常 execute 后，下一笔事务必须自动清理悬挂状态并正常提交
        monkeypatch.setattr(db.raw, "execute", orig_execute)
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO config(key, value, updated_at) VALUES (?, ?, ?)",
                ("v9_tx_probe", "ok", "2026-11-27T00:00:00Z"),
            )

        row = await db.fetchone("SELECT value FROM config WHERE key = ?", ("v9_tx_probe",))
        assert row is not None and row["value"] == "ok"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_continuous_poll_reuses_no_numbers_intent_and_stops_on_fatal_provider_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v9-poll-secret-key")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    env.reload()

    fatal_prov = MagicMock()
    fatal_prov.capabilities = ProviderCapabilities(
        supports_resend=True, supports_price_lock=True, supports_custom_service=True
    )
    fatal_prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
    fatal_prov.request_number = AsyncMock(
        side_effect=SmsProviderError("API Key 无效: BAD_KEY")
    )
    fatal_prov.close = AsyncMock()

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", fatal_prov)
    app = create_app(
        db_path=str(tmp_path / "poll.db"),
        enable_wa_plugin=False,
        registry_instance=reg,
        skip_startup_probe=True,
    )
    async with app.router.lifespan_context(app):
        db = app.state.container.db

        await db.orders.create(
            order_id="ord_reuse_intent",
            project_id="web-console",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            country_label="Thailand",
            status="acquiring",
        )
        row_id_1 = await db.activations.record_intent(
            order_id="ord_reuse_intent",
            attempt_no=1,
            provider="smsbower",
            country_id="52",
            service_code="wa",
            quoted_max_price=0.2,
        )
        await db.activations.update_state(row_id_1, state="no_numbers", last_error="NO_NUMBERS")

        row_id_2 = await db.activations.record_intent(
            order_id="ord_reuse_intent",
            attempt_no=2,
            provider="smsbower",
            country_id="52",
            service_code="wa",
            quoted_max_price=0.2,
            reuse_unpurchased_row_id=row_id_1,
        )
        assert row_id_2 == row_id_1
        rows = await db.activations.list_for_order("ord_reuse_intent")
        assert len(rows) == 1
        assert rows[0]["attempt_no"] == 2

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9099") as client:
            resp = await client.post(
                "/api/v1/orders",
                json={
                    "provider": "smsbower",
                    "service": "whatsapp",
                    "country": "52",
                    "async_acquire": True,
                    "strategy": {"continuous_poll": True, "request_interval": 0.1},
                },
            )
            assert resp.status_code in (201, 202)
            oid = resp.json()["order_id"]

            cur = {}
            for _ in range(30):
                cur = (await client.get(f"/api/v1/orders/{oid}")).json()
                if cur["status"] == "failed":
                    break
                await asyncio.sleep(0.05)
            assert cur["status"] == "failed"
            assert fatal_prov.request_number.await_count == 1


@pytest.mark.asyncio
async def test_batch_scrub_preserves_resend_digest_and_previous_secret_hmac_rotation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v9-hmac-old-secret")
    monkeypatch.delenv("BRIDGE_SECRET_KEY_PREVIOUS", raising=False)
    env.reload()

    db = DatabaseConnection(str(tmp_path / "v9_scrub.db"))
    await db.open()
    try:
        await run_migrations(db.raw)
        await db.open_read_connection()
        orders = OrdersRepo(db)

        await orders.create(
            order_id="ord_open_code",
            project_id="web-console",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_code",
        )
        await orders.append_code("ord_open_code", "617283", "Your code is 617283")
        # 模拟旧库无摘要历史行，验证批量脱敏在擦除明文前自动补算并保留非终态订单摘要
        await db.write(
            "UPDATE sms_orders SET resend_base_digest = '', code_received_at = '2020-01-01T00:00:00+00:00', "
            "updated_at = '2020-01-01T00:00:00+00:00' WHERE order_id = ?",
            ("ord_open_code",),
        )

        await orders.create(
            order_id="ord_closed_code",
            project_id="web-console",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="waiting_code",
        )
        await orders.append_code("ord_closed_code", "998877", "Your code is 998877")
        await orders.transition_status(
            "ord_closed_code",
            expected_statuses=("code_received",),
            new_status="completed",
            completed_at="2020-01-01T00:02:00+00:00",
        )

        versions, non_terminal_ids = await orders.scrub_expired_codes_detailed(retention_hours=1)
        assert "ord_open_code" in versions
        assert "ord_open_code" in non_terminal_ids
        assert "ord_closed_code" in versions
        assert "ord_closed_code" not in non_terminal_ids

        open_raw = await db.fetchone(
            "SELECT latest_code, code_scrubbed, resend_base_digest FROM sms_orders WHERE order_id = ?",
            ("ord_open_code",),
        )
        closed_raw = await db.fetchone(
            "SELECT latest_code, code_scrubbed, resend_base_digest FROM sms_orders WHERE order_id = ?",
            ("ord_closed_code",),
        )
        assert open_raw is not None and open_raw["latest_code"] == ""
        assert open_raw["code_scrubbed"] == 1
        assert open_raw["resend_base_digest"] != ""
        assert closed_raw is not None and closed_raw["resend_base_digest"] == ""

        # 主密钥轮转后，通过 BRIDGE_SECRET_KEY_PREVIOUS 仍能准确去重旧验证码
        monkeypatch.setenv("BRIDGE_SECRET_KEY", "v9-hmac-new-secret")
        monkeypatch.setenv("BRIDGE_SECRET_KEY_PREVIOUS", "v9-hmac-old-secret")
        env.reload()

        assert env.verify_hmac_digest(
            "resend:ord_open_code",
            "617283",
            open_raw["resend_base_digest"],
        )
        assert not env.verify_hmac_digest(
            "resend:ord_open_code",
            "111222",
            open_raw["resend_base_digest"],
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sync_create_order_cancel_race_never_returns_201_and_bind_failure_persists_activation_id(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v9-race-secret-key")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    env.reload()

    race_prov = MagicMock()
    race_prov.capabilities = ProviderCapabilities(
        supports_resend=True, supports_price_lock=True, supports_custom_service=True
    )
    race_prov.resolve_country = AsyncMock(return_value=(52, "Thailand"))
    race_prov.cancel_activation = AsyncMock(return_value="EARLY_CANCEL_DENIED")
    race_prov.close = AsyncMock()

    reg = ProviderRegistry(default="smsbower")
    reg.init_provider("smsbower", race_prov)
    app = create_app(
        db_path=str(tmp_path / "race.db"),
        enable_wa_plugin=False,
        registry_instance=reg,
        skip_startup_probe=True,
    )
    async with app.router.lifespan_context(app):
        svc = app.state.container.order_service
        db = app.state.container.db

        buy_started = asyncio.Event()
        buy_gate = asyncio.Event()

        async def _slow_request_number(*_args, **_kwargs):
            buy_started.set()
            await buy_gate.wait()
            raise NoNumbersError("NO_NUMBERS")

        race_prov.request_number = AsyncMock(side_effect=_slow_request_number)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9099") as client:
            post_task = asyncio.create_task(
                client.post(
                    "/api/v1/orders",
                    headers={"Idempotency-Key": "idem-sync-cancel-race"},
                    json={"provider": "smsbower", "service": "whatsapp", "country": "52"},
                )
            )
            await asyncio.wait_for(buy_started.wait(), timeout=2.0)
            listing = await db.orders.list(active_only=True, page=1, page_size=10)
            for item in listing["items"]:
                if item["status"] == "acquiring":
                    await svc.cancel_order(item["order_id"], reason="race_cancel")
            buy_gate.set()
            r1 = await asyncio.wait_for(post_task, timeout=2.0)
            assert r1.status_code == 409
            assert r1.json()["error"]["code"] == "USER_CANCELLED"

            # 同幂等键重放也必须返回 409 USER_CANCELLED，绝不能把无号取消单当作 201 成功返回
            r2 = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "idem-sync-cancel-race"},
                json={"provider": "smsbower", "service": "whatsapp", "country": "52"},
            )
            assert r2.status_code == 409
            assert r2.json()["error"]["code"] == "USER_CANCELLED"

            # 场景 B: 上游成功出号但本地 bind_acquired_atomic 异常且立即取消处于冷却期 -> 必须持久化 activation_id
            race_prov.request_number = AsyncMock(
                return_value=Activation(
                    "act_orphan_777",
                    "66899998888",
                    52,
                    "Thailand",
                    provider="smsbower",
                    price=0.18,
                    price_is_estimated=False,
                    currency="USD",
                )
            )
            orig_bind = db.activations.bind_acquired_atomic

            async def _boom_bind(*_a, **_kw):
                raise RuntimeError("simulated sqlite failure on bind")

            monkeypatch.setattr(db.activations, "bind_acquired_atomic", _boom_bind)
            r_err = await client.post(
                "/api/v1/orders",
                headers={"Idempotency-Key": "idem-bind-boom"},
                json={"provider": "smsbower", "service": "whatsapp", "country": "52"},
            )
            monkeypatch.setattr(db.activations, "bind_acquired_atomic", orig_bind)

            assert r_err.status_code in (500, 502)
            err_oid = r_err.json()["error"]["details"]["order_id"]
            act_rows = await db.activations.list_for_order(err_oid)
            assert any(r["activation_id"] == "act_orphan_777" for r in act_rows)


@pytest.mark.asyncio
async def test_manual_reconciliation_uncertain_purchase_stats_and_nan_inf_rejection(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v9-stats-secret-key")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    env.reload()

    app = create_app(
        db_path=str(tmp_path / "stats.db"),
        enable_wa_plugin=False,
        skip_startup_probe=True,
    )
    async with app.router.lifespan_context(app):
        db = app.state.container.db
        await db.orders.create(
            order_id="ord_uncertain_1",
            project_id="web-console",
            provider="smsbower",
            service="whatsapp",
            service_code="wa",
            country_id="52",
            status="failed",
        )
        await db.orders.update(
            "ord_uncertain_1",
            settlement_state="uncertain",
            refund_state="unknown",
            currency="USD",
        )
        act_row_id = await db.activations.record_intent(
            order_id="ord_uncertain_1",
            attempt_no=1,
            provider="smsbower",
            country_id="52",
            service_code="wa",
            quoted_max_price=0.35,
        )
        await db.activations.update_state(act_row_id, state="uncertain_purchase", last_error="read timeout")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9099") as client:
            res = await client.post(
                f"/api/v1/orders/reconciliation/{act_row_id}/resolve",
                json={
                    "state": "refund_confirmed",
                    "actual_cost": 0.35,
                    "refund_amount": 0.35,
                    "currency": "USD",
                    "note": "verified refunded on upstream bill",
                },
            )
            assert res.status_code == 200

            stats = (await client.get("/api/v1/stats?days=7")).json()
            assert stats["today"]["actual_cost_total"] == pytest.approx(0.35)
            assert stats["today"]["refunded_amount_total"] == pytest.approx(0.35)
            assert stats["today"]["net_actual_cost_total"] == pytest.approx(0.0)

            nan_resp = await client.post(
                "/api/v1/orders",
                content=b'{"service":"whatsapp","country":"52","strategy":{"target_price":NaN}}',
                headers={"Content-Type": "application/json"},
            )
            assert nan_resp.status_code == 422


@pytest.mark.asyncio
async def test_ssrf_pinned_backend_cloud_imds_ipv6_unwrapping_and_webhook_secret_origin_isolation(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v9-ssrf-secret-key")
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_NETS", "true")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    env.reload()

    # 即使开启 WEBHOOK_ALLOW_PRIVATE_NETS=true，云元数据地址与 NAT64 映射链路本地地址也必须严格阻断
    for blocked_url in (
        "http://100.100.100.200/latest/meta-data/",
        "https://[fd00:ec2::254]/latest/meta-data/",
        "https://[64:ff9b::a9fe:a9fe]/hook",
    ):
        with pytest.raises(AppError) as exc_info:
            await validate_webhook_url_async(blocked_url)
        assert exc_info.value.code == "WEBHOOK_HOST_BLOCKED"

    # PinnedIPNetworkBackend 在 approved_ips 为空时也必须现场解析并校验 IP，阻断 DNS Rebinding
    inner_backend = AsyncMock()
    pinned = PinnedIPNetworkBackend(inner=inner_backend)
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]),
    )
    with pin_webhook_host("rebind.internal.example", (), allow_private=True, allow_loopback=False):
        with pytest.raises(httpcore.ConnectError):
            await pinned.connect_tcp("rebind.internal.example", 443)
    inner_backend.connect_tcp.assert_not_awaited()

    # Webhook 测试接口：项目已存签名密钥仅对同源 URL 生效，防止向异源探测端泄露签名摘要
    app = create_app(
        db_path=str(tmp_path / "ssrf.db"),
        enable_wa_plugin=False,
        skip_startup_probe=True,
    )
    async with app.router.lifespan_context(app):
        send_test_mock = AsyncMock(return_value={"ok": True, "status_code": 200, "attempts": 1})
        monkeypatch.setattr(app.state.container.webhook_dispatcher, "send_test", send_test_mock)
        monkeypatch.setattr(
            "app.api.v1.projects.validate_webhook_url_async",
            AsyncMock(side_effect=lambda url, **_kw: (url, ["93.184.216.34"])),
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9099") as client:
            created = await client.post(
                "/api/v1/projects/keys",
                json={
                    "name": "Tenant A",
                    "project_id": "tenant_a",
                    "default_webhook_url": "https://hooks.tenant-a.example.com/sms",
                    "webhook_secret": "tenant-a-top-secret",
                },
            )
            assert created.status_code == 201

            same_origin = await client.post(
                "/api/v1/projects/webhook-test",
                json={"project_id": "tenant_a", "url": "https://hooks.tenant-a.example.com/sms-v2"},
            )
            assert same_origin.status_code == 200
            passed_same = (
                send_test_mock.await_args.kwargs.get("secret")
                if "secret" in send_test_mock.await_args.kwargs
                else send_test_mock.await_args.args[1]
            )
            assert passed_same == "tenant-a-top-secret"

            foreign_origin = await client.post(
                "/api/v1/projects/webhook-test",
                json={"project_id": "tenant_a", "url": "https://collector.evil.example.org/inspect"},
            )
            assert foreign_origin.status_code == 200
            passed_foreign = (
                send_test_mock.await_args.kwargs.get("secret")
                if "secret" in send_test_mock.await_args.kwargs
                else send_test_mock.await_args.args[1]
            )
            assert passed_foreign == ""


@pytest.mark.asyncio
async def test_v2_tampered_ciphertext_skips_pbkdf2_trusted_proxy_xff_cors_csrf_and_log_redaction(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v9-security-master-key")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "AdminStrong#2026")
    monkeypatch.setenv("CORS_ORIGINS", "https://external-partner.example.com")
    monkeypatch.delenv("ADMIN_TRUSTED_ORIGINS", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    env.reload()

    # 1) 损坏的 v2 密文必须直接报错，绝不回退执行 100k 次 PBKDF2
    valid_v2 = env.encrypt_value("sensitive-api-key")
    raw_bytes = bytearray(base64.urlsafe_b64decode(valid_v2.encode("ascii")))
    raw_bytes[-1] ^= 0xFF
    tampered_v2 = base64.urlsafe_b64encode(bytes(raw_bytes)).decode("ascii")

    def _forbid_pbkdf2(*_args, **_kwargs):
        raise AssertionError("Tampered v2 ciphertext must not fall back to legacy PBKDF2")

    monkeypatch.setattr("app.config._derive_legacy_pbkdf2_fernet_key", _forbid_pbkdf2)
    with pytest.raises((ValueError, InvalidToken)):
        env.decrypt_value(tampered_v2)

    # 2) 日志脱敏覆盖多词 STATUS_OK 短信全文与 JWT 令牌
    scrubbed_sms = redact_text("resp=STATUS_OK:Your WhatsApp code is 849-201 do not share with anyone")
    assert "849-201" not in scrubbed_sms
    assert "WhatsApp code" not in scrubbed_sms
    jwt_sample = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbiJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    assert "eyJzdWIi" not in redact_text(f"token={jwt_sample}")

    # 3) 未配置 TRUSTED_PROXY_IPS 时伪造 X-Forwarded-For 不可绕过登录限流，且 CORS_ORIGINS 不可越权通过后台写接口 CSRF
    app = create_app(
        db_path=str(tmp_path / "sec.db"),
        enable_wa_plugin=False,
        skip_startup_probe=True,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9099") as client:
            ok_login = await client.post(
                "/api/v1/auth/login",
                json={"password": "AdminStrong#2026"},
                headers={"Origin": "http://127.0.0.1:9099"},
            )
            assert ok_login.status_code == 200

            # 跨源 CORS 白名单域名携带管理员 Cookie 调用后台写接口 -> 必须被 CSRF 拦截
            csrf_blocked = await client.put(
                "/api/v1/settings",
                json={"sms_code_mask_in_history": True},
                headers={"Origin": "https://external-partner.example.com"},
            )
            assert csrf_blocked.status_code == 403
            assert csrf_blocked.json()["error"]["code"] == "CSRF_REJECTED"

        # 伪造随机 X-Forwarded-For 连续输错口令仍按真实对端 IP 触发 429 限流
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9099") as attacker:
            statuses = []
            for idx in range(12):
                r = await attacker.post(
                    "/api/v1/auth/login",
                    json={"password": "wrong-password"},
                    headers={
                        "Origin": "http://127.0.0.1:9099",
                        "X-Forwarded-For": f"203.0.113.{idx + 1}",
                    },
                )
                statuses.append(r.status_code)
            assert 429 in statuses


@pytest.mark.asyncio
async def test_otp_hyphen_space_extraction_phone_normalizer_country_resolvers_and_orchestrator_bad_status():
    # 1) 连字符、空格分节、WebOTP 连字符与 URL 路径抗干扰
    assert extract_otp_code("WhatsApp code: 123-456 (valid for 10 minutes)") == "123456"
    assert extract_otp_code("Your verification PIN is 12 34 56") == "123456"
    assert extract_otp_code("Manage settings at https://example.com/g-999999 ; OTP: 741852") == "741852"
    assert extract_otp_code("<#> Portal login\n@portal.example.com #4829-10") == "482910"

    # 2) 未指定目标区号时自动识别显式 '+' 国际号；已指定目标区号时严格保持目标国家优先并剥离本地前导 0
    uk_auto = build_phone_input("+447400123456")
    assert uk_auto.e164_number == "+447400123456"
    assert uk_auto.country_calling_code == "44"
    assert uk_auto.region.upper() == "GB"
    th_leading_zero = build_phone_input("0812345678", country_calling_code="66", country_iso2="th")
    assert th_leading_zero.e164_number == "+66812345678"

    # 3) SmsBower 国家解析：输入 "Oman" 在仅有 Romania 时绝不能子串误匹配为 Romania
    sb = SmsBowerProvider(AsyncMock(return_value="test-key"))
    try:
        sb._countries_cache = [{"id": 32, "name": "Romania", "name_zh": "罗马尼亚", "iso": "ro", "cc": "40"}]
        sb._countries_by_id = {32: sb._countries_cache[0]}
        sb._countries_fetched_at = 1e12
        with pytest.raises((SmsProviderError, ValueError)):
            await sb.resolve_country("Oman")
    finally:
        await sb.close()

    # 4) 5sim 原生国家别名与未知国家明确报错
    fs = FiveSimProvider(AsyncMock(return_value="test-5sim-key"))
    try:
        cid_ci, _ = await fs.resolve_country("CI")
        assert cid_ci == "ivorycoast"
        cid_sv, _ = await fs.resolve_country("El Salvador")
        assert cid_sv == "salvador"
        with pytest.raises((SmsProviderError, ValueError)):
            await fs.resolve_country("AtlantisNotRealCountry")
    finally:
        await fs.close()

    # 5) 编排器补偿取消遇到 BAD_STATUS 时通过 get_sms_code 核实上游终态
    reg = ProviderRegistry(default="smsbower")
    mock_sb = MagicMock()
    mock_sb._safe_cancel = AsyncMock(return_value="BAD_STATUS")
    mock_sb.get_sms_code = AsyncMock(return_value=("CANCEL", None))
    reg.init_provider("smsbower", mock_sb)
    orch = RegisterOrchestrator(reg, AsyncMock(), AsyncMock())
    settled, detail = await orch._safe_cancel_activation("smsbower", "act_bad_status_1", "task_1", "probe_fail")
    assert settled is True
    assert "getStatus=CANCEL" in detail
