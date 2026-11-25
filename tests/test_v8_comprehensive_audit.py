"""v8 全项目深度审计回归测试套件。

覆盖：
1. v2 HKDF 高速加密派生（防事件循环阻塞）、旧版 v1 PBKDF2 密文兼容与 BRIDGE_SECRET_KEY_PREVIOUS 密钥轮转
2. 本机回环防 DNS Rebinding、浏览器写接口 CSRF 拦截、登出服务端 epoch 吊销、改密失效与登录防爆破限流
3. 跨租户 Webhook SSRF 隔离（全局白名单不可越权跳过项目同源校验）、内网放行模式下阻断 169.254/0.0.0.0 与签名密钥解密失败闭合阻断
4. 日志脱敏完整遮蔽验证码/口令/Basic/Cookie/内嵌引号 JSON 与公开 /health 插件信息脱敏
5. 0 小时留存脱敏后重发短信（resend_base_digest HMAC 防旧码 + 无 WAIT 快速新码立即放行 + 终态擦除摘要）
6. 验证码提取抗误判加固、HeroSMS/5sim 边界修复与编排器 registered=True & login_active=False 账号保护
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import httpx
import pytest

from app.config import env
from app.core.url_guard import normalize_url_hostname, validate_webhook_url_async
from app.errors import AppError
from app.log_redaction import redact_text
from app.main import create_app
from app.orchestrator import RegisterOrchestrator
from app.phone_normalizer import build_phone_input
from app.providers.base import (
    Activation,
    ProviderCapabilities,
    SmsProviderError,
    extract_otp_code,
    parse_cancel_outcome,
    parse_complete_outcome,
    parse_resend_outcome,
)
from app.providers.five_sim import FiveSimProvider
from app.providers.hero_sms import HeroSmsProvider
from app.providers.registry import ProviderRegistry
from tests.conftest import make_activation, make_phone_input, make_probe_result, make_register_result


def test_v2_hkdf_encryption_speed_legacy_v1_compat_and_key_rotation(monkeypatch):
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "legacy-secret-2025")
    monkeypatch.delenv("BRIDGE_SECRET_KEY_PREVIOUS", raising=False)
    env.reload()

    # 构造旧版 v1 格式密文（16 字节盐 + Fernet token，无 v2 前缀）
    salt = os.urandom(16)
    legacy_key = env._derive_fernet_key(salt)
    v1_cipher = base64.urlsafe_b64encode(salt + Fernet(legacy_key).encrypt(b"legacy-api-token-001")).decode("ascii")
    assert env.decrypt_value(v1_cipher) == "legacy-api-token-001"

    # 旧主密钥生成的 v2 密文
    old_v2_cipher = env.encrypt_value("v2-secret-before-rotation")
    assert env.decrypt_value(old_v2_cipher) == "v2-secret-before-rotation"

    # 轮转主密钥并通过 BRIDGE_SECRET_KEY_PREVIOUS 平滑兼容旧 v1 / v2 密文
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "rotated-primary-secret-2026")
    monkeypatch.setenv("BRIDGE_SECRET_KEY_PREVIOUS", "legacy-secret-2025")
    env.reload()

    assert env.decrypt_value(v1_cipher) == "legacy-api-token-001"
    assert env.decrypt_value(old_v2_cipher) == "v2-secret-before-rotation"

    # 验证 200 次随机盐 v2 加解密不阻塞事件循环（远低于旧版 13.2s）
    t0 = time.perf_counter()
    for idx in range(200):
        ct = env.encrypt_value(f"fast-hkdf-secret-{idx}")
        assert env.decrypt_value(ct) == f"fast-hkdf-secret-{idx}"
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.5, f"v2 HKDF 加解密过慢: {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_loopback_dns_rebinding_csrf_session_revocation_and_login_rate_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path / "data_auth"))
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v8-auth-secret-key-strong")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("CONSOLE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    env.reload()

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)

        # 1) DNS Rebinding 防护：即使来源 IP 是 127.0.0.1，伪造/重绑定非本机 Host 必须拒绝免密会话与管理员接口
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9090") as rebind_client:
            rebind_status = await rebind_client.get(
                "/api/v1/auth/status",
                headers={"Host": "evil-rebind.attacker.com:9090"},
            )
            assert rebind_status.status_code == 200
            assert rebind_status.json()["direct_loopback"] is False
            assert rebind_status.json()["can_bootstrap_local_session"] is False

            rebind_sess = await rebind_client.post(
                "/api/v1/auth/session",
                headers={"Host": "evil-rebind.attacker.com:9090"},
            )
            assert rebind_sess.status_code == 403
            assert "sms_hub_session" not in rebind_sess.cookies

            rebind_settings = await rebind_client.get(
                "/api/v1/settings",
                headers={"Host": "evil-rebind.attacker.com:9090"},
            )
            assert rebind_settings.status_code in {401, 403}

        # 2) 正常本机回环获取会话 Cookie 后，跨站写请求（CSRF）必须返回 403 CSRF_REJECTED
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9090") as client:
            sess = await client.post("/api/v1/auth/session")
            assert sess.status_code == 200
            assert sess.json()["is_admin"] is True
            captured_cookie = client.cookies.get("sms_hub_session")
            assert captured_cookie

            # 跨站 Origin 拦截
            csrf_origin = await client.put(
                "/api/v1/settings",
                headers={"Origin": "https://evil.example"},
                json={"api_auth_required": True},
            )
            assert csrf_origin.status_code == 403
            assert csrf_origin.json()["error"]["code"] == "CSRF_REJECTED"

            # Sec-Fetch-Site: cross-site 拦截
            csrf_fetch_site = await client.put(
                "/api/v1/settings",
                headers={"Sec-Fetch-Site": "cross-site"},
                json={"api_auth_required": True},
            )
            assert csrf_fetch_site.status_code == 403
            assert csrf_fetch_site.json()["error"]["code"] == "CSRF_REJECTED"

            # HTML 表单 Simple Request (text/plain 无自定义 CSRF 头) 拦截
            csrf_form = await client.post(
                "/api/v1/orders",
                headers={"Content-Type": "text/plain"},
                content='{"country":"52","service":"whatsapp"}',
            )
            assert csrf_form.status_code == 403
            assert csrf_form.json()["error"]["code"] == "CSRF_REJECTED"

            # 同源合法开启严格鉴权，再执行登出：旧复制出来的 Cookie 必须因服务端 epoch 递增而立即失效
            enable_strict = await client.put(
                "/api/v1/settings",
                headers={"Origin": "http://127.0.0.1:9090"},
                json={"api_auth_required": True},
            )
            assert enable_strict.status_code == 200

            logout_res = await client.post("/api/v1/auth/logout", headers={"Origin": "http://127.0.0.1:9090"})
            assert logout_res.status_code == 200

        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9090") as replay_client:
            replay_client.cookies.set("sms_hub_session", captured_cookie)
            replayed = await replay_client.get("/api/v1/orders")
            assert replayed.status_code == 401

        # 3) 配置管理员密码后：改密使既有会话立即失效，且连续错误口令触发 429 AUTH_RATE_LIMITED
        monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-pass-v1")
        env.reload()

        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9090") as pw_client:
            login_ok = await pw_client.post("/api/v1/auth/login", json={"password": "admin-pass-v1"})
            assert login_ok.status_code == 200
            assert (await pw_client.get("/api/v1/settings")).status_code == 200

            # 修改管理员密码后，原 Cookie 签名自动失效
            monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-pass-v2")
            env.reload()
            after_pw_change = await pw_client.get("/api/v1/settings")
            assert after_pw_change.status_code == 401

        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9090") as brute_client:
            statuses = []
            last_body = {}
            for _ in range(11):
                r = await brute_client.post("/api/v1/auth/login", json={"password": "wrong-password"})
                statuses.append(r.status_code)
                last_body = r.json()
            assert 429 in statuses
            assert last_body["error"]["code"] == "AUTH_RATE_LIMITED"


@pytest.mark.asyncio
async def test_ssrf_cross_tenant_origin_isolation_link_local_block_and_secret_decrypt_fail_closed(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path / "data_ssrf"))
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v8-ssrf-secret-key-strong")
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "internal-receiver.corp.local")
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_NETS", "true")
    env.reload()

    # 规范化域名尾点与大小写
    assert normalize_url_hostname("Hooks.Example.COM.") == "hooks.example.com"

    # 即使 WEBHOOK_ALLOW_PRIVATE_NETS=true，也严禁访问云元数据 169.254.169.254、0.0.0.0 及未白名单本机回环
    for forbidden_url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0:8080/hook",
        "http://127.0.0.1:9090/api/v1/health",
    ):
        with pytest.raises(AppError):
            await validate_webhook_url_async(forbidden_url, resolve_dns=False)

    # 跨租户越权防护：全局 WEBHOOK_ALLOWED_HOSTS 中的主机绝不允许非管理员项目 Key 绕过自身 default_webhook_url 限制
    with pytest.raises(AppError) as exc_unconfigured:
        await validate_webhook_url_async(
            "http://internal-receiver.corp.local/steal",
            allowed_project_url="",
            enforce_project_origin=True,
            resolve_dns=False,
        )
    assert exc_unconfigured.value.code == "WEBHOOK_TARGET_NOT_AUTHORIZED"

    with pytest.raises(AppError) as exc_mismatch:
        await validate_webhook_url_async(
            "http://internal-receiver.corp.local/steal",
            allowed_project_url="https://tenant-a.example.com/webhook",
            enforce_project_origin=True,
            resolve_dns=False,
        )
    assert exc_mismatch.value.code == "WEBHOOK_TARGET_NOT_AUTHORIZED"

    # 项目签名密钥密文损坏（解密失败）时，WebhookDispatcher 必须闭合阻断投递，绝不降级发送无签名请求
    app = create_app()
    async with app.router.lifespan_context(app):
        container = app.state.container
        db = container.db
        rec, _ = await container.auth_service.create_project_key(
            name="SignedHookBot",
            default_webhook_url="http://internal-receiver.corp.local/hook",
            webhook_secret="top-secret-hmac",
        )
        pid = rec["project_id"]
        await db.conn.write(
            "UPDATE api_keys SET webhook_secret_enc='e_corrupted_unreadable_cipher' WHERE project_id=?",
            (pid,),
        )

        sent_requests: list[str] = []
        container.webhook_dispatcher._client.post = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda url, **kw: sent_requests.append(url)
        )
        await db.webhooks.enqueue(
            delivery_id="dlv_corrupt_sec_1",
            event_id="evt_corrupt_sec_1",
            event_type="order.code_received",
            order_id="ord_sec_1",
            project_id=pid,
            target_url="http://internal-receiver.corp.local/hook",
            payload={"code": "654321"},
        )
        await container.webhook_dispatcher.process_due_once()
        await container.webhook_dispatcher.drain_inflight()

        assert sent_requests == []
        dlv = (await db.webhooks.list(project_id=pid))[0]
        assert "密钥解密失败" in dlv["last_error"]


@pytest.mark.asyncio
async def test_log_redaction_full_otp_mask_paired_quotes_numeric_code_and_public_health_sanitization(
    tmp_path: Path, monkeypatch
):
    sample = (
        '{"code": "482910", "numeric": {"code": 918273}, '
        '"raw_text": "Don\'t share code 736451", "password": "my_admin_password"} '
        "Authorization: Basic dXNlcjpwYXNz Cookie: sms_hub_session=admin:999:1:abc.sig "
        "proxy=https://proxyuser:proxypass@10.0.0.2:8443"
    )
    redacted = redact_text(sample)
    assert "482910" not in redacted
    # 确保 6 位验证码首尾两位也未泄露（旧 mask_identifier 会泄露 48***10）
    assert "48***10" not in redacted
    assert "918273" not in redacted
    assert "736451" not in redacted
    assert "my_admin_password" not in redacted
    assert "dXNlcjpwYXNz" not in redacted
    assert "admin:999:1:abc.sig" not in redacted
    assert "proxypass" not in redacted

    # 公开 /api/v1/health 在开启 WA 插件时不得泄露内部 WA_APP_URL 或上游异常栈文本
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path / "data_health"))
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v8-health-secret-key")
    monkeypatch.setenv("ENABLE_WA_PLUGIN", "true")
    monkeypatch.setenv("WA_APP_URL", "http://internal-secret-wa-node.local:18080")
    env.reload()

    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            resp = await client.get("/api/v1/health")
            assert resp.status_code == 200
            wa_info = resp.json()["plugins"]["whatsapp_wa"]
            assert wa_info["enabled"] is True
            assert "ok" in wa_info
            assert "internal-secret-wa-node.local" not in str(wa_info)
            assert "url" not in wa_info
            assert "detail" not in wa_info


@pytest.mark.asyncio
async def test_post_scrub_resend_accepts_fast_new_code_via_hmac_digest_and_wipes_on_terminal(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path / "data_resend"))
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "v8-resend-hmac-secret-key")
    monkeypatch.setenv("SMS_CODE_RETENTION_HOURS", "0")
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    env.reload()

    app = create_app()
    async with app.router.lifespan_context(app):
        container = app.state.container
        db = container.db
        order_service = container.order_service
        poll_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        class FastResendProvider:
            provider_id = "smsbower"
            capabilities = ProviderCapabilities(
                supports_resend=True, supports_price_lock=True, supports_custom_service=True
            )

            async def resolve_country(self, country: str):
                return 52, "Thailand"

            async def get_prices(self, cid, svc):
                return [{"cost": 0.10, "count": 5}]

            async def request_number(self, cid, svc, max_price=None):
                return Activation(
                    "aid_fast_resend", "66811112222", 52, "Thailand", provider="smsbower", price=0.10, price_is_estimated=False
                )

            async def normalize_phone(self, raw_phone, cid):
                return build_phone_input(raw_phone, country_calling_code="66", country_iso2="th")

            async def get_sms_code(self, aid):
                return await poll_queue.get()

            def get_last_raw_sms(self, aid):
                return ""

            async def request_additional_sms_structured(self, aid):
                return parse_resend_outcome("ACCESS_RETRY_GET")

            async def complete_activation_structured(self, aid):
                return parse_complete_outcome("ACCESS_ACTIVATION")

            async def cancel_activation_structured(self, aid):
                return parse_cancel_outcome("ACCESS_CANCEL")

            async def close(self):
                return None

        container.registry.init_provider("smsbower", FastResendProvider())  # type: ignore[arg-type]
        order_service._poll_intervals = (0.02, 0.02, 0.02)

        order = await order_service.create_order(country="52", service="whatsapp", provider_name="smsbower")
        oid = order["order_id"]

        # 首码到达
        await poll_queue.put(("OK", "111222"))
        first_res = await order_service.wait_for_code(oid, timeout=3, after_seq=0)
        assert first_res["latest_code"] == "111222"

        # 触发留存脱敏：明文 latest_code 被清空，但不可逆 resend_base_digest 保留用于防旧码
        await order_service.scrub_order_code(oid)
        raw_row = await db.conn.fetchone("SELECT latest_code, code_scrubbed, resend_base_digest FROM sms_orders WHERE order_id=?", (oid,))
        assert raw_row is not None
        assert raw_row["latest_code"] == ""
        assert int(raw_row["code_scrubbed"]) == 1

        # 发起重发：先返回已脱敏的旧码 111222（必须通过 HMAC 摘要识别并跳过），随后无 WAIT 直接返回新码 333444（必须立即放行！）
        await order_service.resend_sms(oid)
        post_resend_row = await db.conn.fetchone("SELECT resend_base_digest FROM sms_orders WHERE order_id=?", (oid,))
        assert post_resend_row is not None
        assert post_resend_row["resend_base_digest"]
        assert "111222" not in post_resend_row["resend_base_digest"]

        await poll_queue.put(("OK", "111222"))
        await poll_queue.put(("OK", "333444"))
        second_res = await order_service.wait_for_code(oid, timeout=3, after_seq=1)
        assert second_res["status"] == "code_received"
        assert second_res["latest_code"] == "333444"

        # 订单完成终态：resend_base_digest 必须一并擦除
        await order_service.complete_order(oid)
        done_row = await db.conn.fetchone("SELECT resend_base_digest FROM sms_orders WHERE order_id=?", (oid,))
        assert done_row is not None
        assert done_row["resend_base_digest"] == ""


@pytest.mark.asyncio
async def test_otp_parser_hardening_provider_fixes_and_orchestrator_registered_preservation(
    mock_db, mock_smsbower, mock_waapi, monkeypatch
):
    # 1) 验证码提取：裸“码”（号码）、换行分隔多段数字、年份/金额促销不应误拼或误提
    assert extract_otp_code("您的手机号码尾号已更新，请知悉") == ""
    assert extract_otp_code("订单批次 1234\n流水序号 5678") != "12345678"
    assert extract_otp_code("2026 年度账单已生成，消费 500 元") == ""
    assert extract_otp_code("Your verification code is 748291. Valid for 5 minutes.") == "748291"
    assert extract_otp_code("<#> Verification code: 412-908\n@login.example.com #412908") == "412908"

    # 2) HeroSMS: 纯数字优先命中上游国家 ID（"7" -> 马来西亚 ID=7），带 "+" 优先命中国际区号（"+7" -> 俄罗斯 ID=0）
    hero = HeroSmsProvider(AsyncMock(return_value="hero_key"))
    monkeypatch.setattr(
        hero,
        "get_countries",
        AsyncMock(
            return_value=[
                {"id": 0, "name": "Russia", "cc": "7", "iso": "ru", "aliases": ["0", "7"]},
                {"id": 7, "name": "Malaysia", "cc": "60", "iso": "my", "aliases": ["7", "60"]},
                {"id": 52, "name": "Thailand", "cc": "66", "iso": "th", "aliases": ["52", "66"]},
                {"id": 66, "name": "Pakistan", "cc": "92", "iso": "pk", "aliases": ["66", "92"]},
            ]
        ),
    )
    try:
        assert (await hero.resolve_country("7"))[0] == 7
        assert (await hero.resolve_country("+7"))[0] == 0
        assert (await hero.resolve_country("66"))[0] == 66
        assert (await hero.resolve_country("+66"))[0] == 52
    finally:
        await hero.close()

    # 3) 5sim: 非法非 ASCII 服务码直接抛 SmsProviderError；cancel 返回 400 时回查 check_order 确认 CANCELED
    def fs_handler(req: httpx.Request) -> httpx.Response:
        if "/user/cancel/" in str(req.url):
            return httpx.Response(400, text="order already canceled")
        if "/user/check/" in str(req.url):
            return httpx.Response(200, json={"id": 888, "status": "CANCELED"})
        return httpx.Response(200, json={})

    fs = FiveSimProvider(AsyncMock(return_value="jwt_token"))
    await fs._client.aclose()
    fs._client = httpx.AsyncClient(transport=httpx.MockTransport(fs_handler))
    try:
        with pytest.raises(ValueError):
            await fs.get_prices("thailand", product="非法中文服务")
        cancel_out = await fs.cancel_activation_structured("888")
        assert cancel_out.settled is True
        assert cancel_out.refund_confirmed is True
    finally:
        await fs.close()

    # 4) RegisterOrchestrator: submit_otp 返回 registered=True 但 login_active=False 时必须进入 check_login_state 保号，绝不误删已注册账号！
    reg = ProviderRegistry()
    reg.init_provider("smsbower", mock_smsbower)
    orch = RegisterOrchestrator(reg, mock_waapi, mock_db)

    mock_smsbower.request_number = AsyncMock(return_value=make_activation("act_reg_keep", "79001234567", 0, "Russia"))
    mock_smsbower.normalize_phone = AsyncMock(return_value=make_phone_input())
    mock_smsbower.poll_sms_code = AsyncMock(return_value="654321")
    mock_waapi.probe_phone = AsyncMock(return_value=make_probe_result(passed=True))
    mock_waapi.register = AsyncMock(return_value=make_register_result(success=True, wa_account_id="wa_keep_1"))
    mock_waapi.submit_otp = AsyncMock(
        return_value=make_register_result(
            success=True,
            wa_account_id="wa_keep_1",
            registered=True,
            login_active=False,
        )
    )
    mock_waapi.check_login_state = AsyncMock(return_value=True)

    started = await orch.start_registration(
        task_id="task_keep_registered",
        country="0",
        country_fallback=[],
        max_attempts=1,
        provider_name="smsbower",
    )
    assert started["status"] == "running"
    bg_task = orch._tasks.get("task_keep_registered")
    if bg_task:
        await bg_task

    assert mock_waapi.check_login_state.await_count == 1
    assert mock_waapi.check_login_state.await_args.args[0] == "wa_keep_1"
    mock_waapi.cleanup_failed_account.assert_not_awaited()
    mock_smsbower.complete_activation.assert_awaited_once_with("act_reg_keep")
    mock_db.record_registration_success.assert_awaited_once()
