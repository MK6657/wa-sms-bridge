"""WA 上游适配层 contract-style 测试。"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.models import PhoneInput
from app.adapters import ProbeResult, RegisterResult, WaApiError, WhatsappWaAdapter, create_wa_adapter


def test_register_result_extracts_nested_fields():
    raw = {
        "success": True,
        "status": "VERIFICATION_REQUEST_STATUS_SENT",
        "registration_phase": "OTP_WAITING",
        "registration": {"wa_account_id": "waacc_1"},
        "verification_request": {"id": "vreq_1"},
    }

    result = RegisterResult(raw)

    assert result.success is True
    assert result.wa_account_id == "waacc_1"
    assert result.verification_request_id == "vreq_1"
    assert result.registration_phase == "OTP_WAITING"


def test_register_result_understands_resume_otp_shape():
    raw = {
        "success": True,
        "registration": {
            "wa_account_id": "waacc_2",
            "verification_request_id": "vreq_2",
            "status": "REGISTRATION_STATUS_REGISTERED",
        },
        "login_state": {
            "login_state_id": "ls_1",
            "status": "LOGIN_STATE_STATUS_ACTIVE",
        },
    }

    result = RegisterResult(raw)

    assert result.registered is True
    assert result.login_active is True
    assert result.login_state_id == "ls_1"


def test_register_result_classifies_security_code_rejection():
    result = RegisterResult({
        "success": False,
        "registration": {
            "status": "REGISTRATION_STATUS_REJECTED",
            "last_error": {
                "code": "WA_ERROR_CODE_REJECTED",
                "message": "registration was rejected: reason=security_code",
            },
        },
    })

    assert result.error_code == "WA_ERROR_CODE_REJECTED"
    assert result.failure_reason == "security_code"
    assert result.failure_category == "security_verification_required"


def test_probe_result_prefers_phone_status_can_register():
    result = ProbeResult({
        "success": False,
        "phone_status": {
            "can_register": True,
            "blocked": False,
            "request_failed": False,
        },
    })

    assert result.ok is True


@pytest.mark.asyncio
async def test_factory_creates_default_whatsapp_wa_adapter():
    client = create_wa_adapter("whatsapp_wa", "http://wa.example")
    try:
        assert isinstance(client, WhatsappWaAdapter)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_register_payload_is_bridge_owned_and_proxy_aware():
    client = WhatsappWaAdapter("http://wa.example")
    client._post_json = AsyncMock(return_value={
        "success": True,
        "wa_account_id": "waacc_3",
        "verification_request_id": "vreq_3",
    })

    result = await client.register(
        PhoneInput(
            phone="9001234567",
            e164_number="+79001234567",
            country_calling_code="7",
            country_iso2="ru",
        ),
        proxy_url="socks5://127.0.0.1:1080",
        correlation_id="task_1",
        request_id="task_1:1:register",
    )

    path, payload = client._post_json.await_args.args
    await client.close()

    assert path == "/api/wa/register"
    assert payload["phone"]["e164_number"] == "+79001234567"
    assert payload["phone"]["national_number"] == "9001234567"
    assert payload["proxy_url"] == "socks5://127.0.0.1:1080"
    assert payload["correlation_id"] == "task_1"
    assert payload["request_id"] == "task_1:1:register"
    assert result.wa_account_id == "waacc_3"


@pytest.mark.asyncio
async def test_register_rejects_incomplete_phone_payload_before_http():
    client = WhatsappWaAdapter("http://wa.example")
    client._post_json = AsyncMock(return_value={})

    with pytest.raises(WaApiError, match="country_calling_code is required"):
        await client.register(
            PhoneInput(
                phone="6523227",
                e164_number="276523227",
                country_calling_code="",
                country_iso2="",
            )
        )

    client._post_json.assert_not_called()
    await client.close()


@pytest.mark.asyncio
async def test_submit_otp_payload_keeps_verification_request_id():
    client = WhatsappWaAdapter("http://wa.example")
    client._post_json = AsyncMock(return_value={
        "success": True,
        "registration": {"status": "REGISTRATION_STATUS_REGISTERED"},
        "login_state": {"status": "LOGIN_STATE_STATUS_ACTIVE"},
    })

    await client.submit_otp(
        "waacc_4",
        "123456",
        verification_request_id="vreq_4",
        correlation_id="task_2",
    )

    path, payload = client._post_json.await_args.args
    await client.close()

    assert path == "/api/wa/actions/registration/resume-otp"
    assert payload["wa_account_id"] == "waacc_4"
    assert payload["verification_request_id"] == "vreq_4"
    assert payload["otp"] == "123456"
    assert payload["code"] == "123456"


@pytest.mark.asyncio
async def test_non_idempotent_post_is_not_retried_after_connection_loss():
    client = WhatsappWaAdapter("http://wa.example")
    transport = MagicMock()
    transport.request = AsyncMock(side_effect=httpx.RemoteProtocolError("response lost"))
    transport.aclose = AsyncMock()
    await client._client.aclose()
    client._client = transport

    with pytest.raises(WaApiError, match="POST 连接失败"):
        await client._request("POST", "/api/wa/register", json={"request_id": "req_1"})

    transport.request.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
async def test_get_retries_without_replacing_shared_client(monkeypatch):
    client = WhatsappWaAdapter("http://wa.example")
    response = MagicMock(status_code=200)
    transport = MagicMock()
    transport.request = AsyncMock(side_effect=[
        httpx.ConnectError("down"),
        response,
    ])
    transport.aclose = AsyncMock()
    await client._client.aclose()
    client._client = transport
    sleep = AsyncMock()
    monkeypatch.setattr("app.adapters.whatsapp_wa.asyncio.sleep", sleep)

    result = await client._request("GET", "/api/wa/health")

    assert result is response
    assert transport.request.await_count == 2
    assert client._client is transport
    sleep.assert_awaited_once()
    await client.close()
