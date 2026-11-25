"""whatsapp_WA HTTP adapter.

此模块是 wa-sms-bridge 与外部 whatsapp_WA 服务的防腐层。
编排器只依赖这里归一化后的结果对象，避免上游 HTTP 字段变化扩散到业务流程。
"""

import asyncio
import logging
import re
from typing import Any

import httpx
from app.models import PhoneInput
from app.log_redaction import mask_identifier
from app.phone_normalizer import PhoneNormalizationError, validate_phone_input

logger = logging.getLogger(__name__)


class WaApiError(Exception):
    """wa-app API 调用异常。"""


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _maybe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _bool(value: Any, default: bool = False) -> bool:
    parsed = _maybe_bool(value)
    return default if parsed is None else parsed


def _active_login_status(status: str) -> bool:
    normalized = status.strip().upper()
    if "NOT_ACTIVE" in normalized or "INACTIVE" in normalized:
        return False
    return normalized in {"ACTIVE", "LOGIN_STATE_STATUS_ACTIVE"} or normalized.endswith("_ACTIVE")


def _registered_status(status: str) -> bool:
    normalized = status.strip().upper()
    if "NOT_REGISTERED" in normalized or "UNREGISTERED" in normalized:
        return False
    return normalized in {"REGISTERED", "REGISTRATION_STATUS_REGISTERED"} or normalized.endswith("_REGISTERED")


class RegisterResult:
    """注册/OTP 类响应的稳定视图。

    whatsapp_WA 可能把字段放在顶层、registration、verification_request 或
    login_state 中。这里统一提取，编排器无需关心上游响应形状。
    """

    def __init__(self, raw: dict | None):
        self.raw = raw or {}
        self.registration: dict = _object(self.raw.get("registration"))
        self.login_state: dict = _object(self.raw.get("login_state"))
        self.verification_request: dict = _object(self.raw.get("verification_request"))
        self.phone_status: dict = _object(self.raw.get("phone_status"))
        self.error: dict = _object(self.raw.get("error"))
        self.registration_error: dict = _object(self.registration.get("last_error"))

        self.success: bool = _bool(self.raw.get("success"))
        self.status: str = _text(
            self.raw.get("status"),
            self.registration.get("status"),
            self.verification_request.get("status"),
        )
        self.error_message: str = _text(
            self.raw.get("error_message"),
            self.error.get("message"),
            self.registration_error.get("message"),
            self.phone_status.get("reject_reason"),
        )
        self.error_code: str = _text(
            self.raw.get("error_code"),
            self.error.get("code"),
            self.registration_error.get("code"),
        )
        self.failure_reason: str = _text(
            self.raw.get("reason"),
            self.error.get("reason"),
            self.registration_error.get("reason"),
            self.phone_status.get("reject_reason"),
        ).strip().lower()
        if not self.failure_reason:
            match = re.search(r"(?:^|[;\s])reason=([a-z0-9_\-]+)", self.error_message, re.IGNORECASE)
            if match:
                self.failure_reason = match.group(1).lower()
        self.registration_phase: str = _text(
            self.raw.get("registration_phase"),
            self.phone_status.get("registration_phase"),
        )
        self.request_failed: bool = _bool(
            self.raw.get("request_failed"),
            default=_bool(self.phone_status.get("request_failed")),
        )
        self.wa_account_id: str = _text(
            self.raw.get("wa_account_id"),
            self.registration.get("wa_account_id"),
            _object(self.raw.get("account")).get("wa_account_id"),
            self.login_state.get("wa_account_id"),
        )
        self.verification_request_id: str = _text(
            self.raw.get("verification_request_id"),
            self.registration.get("verification_request_id"),
            self.verification_request.get("verification_request_id"),
            self.verification_request.get("id"),
        )
        self.challenge_data: dict = _object(self.raw.get("account_transfer_challenge"))
        self.is_transfer_challenge: bool = (
            self.registration_phase == "ACCOUNT_TRANSFER_WAITING" or bool(self.challenge_data)
        )

    @property
    def login_state_id(self) -> str:
        """从 resume-otp 返回中提取 login_state_id。"""
        return _text(
            self.raw.get("login_state_id"),
            self.login_state.get("login_state_id"),
            self.login_state.get("id"),
        )

    @property
    def login_active(self) -> bool:
        """登录态是否 ACTIVE（用于 resume-otp 返回判断）。"""
        return _active_login_status(_text(self.login_state.get("status"), self.raw.get("login_state_status")))

    @property
    def registered(self) -> bool:
        """注册状态是否 REGISTERED。"""
        return _registered_status(_text(self.registration.get("status"), self.raw.get("registration_status"), self.status))

    @property
    def failure_category(self) -> str:
        """将上游拒绝原因映射为 bridge 自有的稳定分类。"""
        reason = self.failure_reason
        if reason in {"security_code", "second_code", "device_confirm_or_second_code"}:
            return "security_verification_required"
        if reason in {"mismatch", "bad_code", "fail_mismatch", "incorrect"}:
            return "otp_invalid"
        if reason in {"guessed_too_fast", "fail_guessed_too_fast", "too_many"}:
            return "otp_rate_limited"
        if reason in {"blocked", "fail_blocked"}:
            return "wa_blocked"
        if self.error_code:
            return self.error_code.lower()
        return "otp_registration_failed"


class ProbeResult:
    """号码探测响应的稳定视图。"""

    def __init__(self, raw: dict | None):
        self.raw = raw or {}
        self.phone_status: dict = _object(self.raw.get("phone_status"))
        self.error: dict = _object(self.raw.get("error"))
        self.success: bool = _bool(self.raw.get("success"))
        self.passed: bool = _bool(self.raw.get("passed"))
        self.request_failed: bool = _bool(
            self.raw.get("request_failed"),
            default=_bool(self.phone_status.get("request_failed")),
        )
        self.blocked: bool = _bool(self.raw.get("blocked"), default=_bool(self.phone_status.get("blocked")))
        self.status: str = _text(self.raw.get("status"), self.phone_status.get("account_status"))
        self.error_message: str = _text(
            self.raw.get("error_message"),
            self.error.get("message"),
            self.phone_status.get("reject_reason"),
        )
        self.can_register: bool | None = _maybe_bool(self.phone_status.get("can_register"))

    @property
    def ok(self) -> bool:
        """探测通过（可注册）。"""
        if self.can_register is not None:
            return self.can_register and not self.blocked and not self.request_failed
        return (self.success or self.passed) and not self.blocked and not self.request_failed


class WhatsappWaAdapter:
    """whatsapp_WA BFF HTTP adapter."""

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=30.0)
        self._auth_token: str = ""
        self._auth_type: str = "none"
        self._auth_username: str = ""
        self._auth_password: str = ""

    def configure_auth(self, auth_type: str = "none", token: str = "",
                       username: str = "", password: str = ""):
        self._auth_type = auth_type
        self._auth_token = token
        self._auth_username = username
        self._auth_password = password

    def set_base_url(self, url: str):
        """运行时更新 wa-app 地址（热更新）。"""
        self._base_url = url.rstrip("/")
        logger.info("wa-app 地址已更新: %s", url)

    async def close(self):
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """统一请求封装；只自动重试无副作用的读取请求。"""
        headers = kwargs.pop("headers", {})
        headers.setdefault("Content-Type", "application/json")

        # 鉴权头
        if self._auth_type == "bearer" and self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        elif self._auth_type == "basic" and self._auth_username:
            import base64
            raw = f"{self._auth_username}:{self._auth_password}"
            token = base64.b64encode(raw.encode()).decode()
            headers["Authorization"] = f"Basic {token}"

        method_name = method.upper()
        max_attempts = 3 if method_name in {"GET", "HEAD", "OPTIONS"} else 1
        base_url = self._base_url
        for attempt in range(max_attempts):
            url = f"{base_url}{path}"
            try:
                resp = await self._client.request(method, url, headers=headers, **kwargs)
                if resp.status_code == 401:
                    logger.warning("wa-app API 返回 401: auth_type=%s", self._auth_type)
                return resp
            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.WriteError,
            ) as e:
                if attempt == max_attempts - 1:
                    retry_note = f"，已重试 {max_attempts} 次" if max_attempts > 1 else ""
                    raise WaApiError(f"wa-app {method_name} 连接失败{retry_note}: {e}") from e
                logger.warning(
                    "wa-app 读取请求连接异常，保留共享连接池并重试 (attempt %d/%d)",
                    attempt + 1,
                    max_attempts,
                )
                await asyncio.sleep(0.25 * (2 ** attempt))

        raise WaApiError("wa-app 请求失败（无法到达）")

    async def _post_json(self, path: str, data: dict) -> Any:
        resp = await self._request("POST", path, json=data)
        if not resp.is_success:
            raise WaApiError(f"wa-app HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except Exception:
            raise WaApiError(f"wa-app 返回非 JSON: {resp.text[:200]}")

    async def _get_json(self, path: str) -> Any:
        resp = await self._request("GET", path)
        if not resp.is_success:
            raise WaApiError(f"wa-app HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except Exception:
            raise WaApiError(f"wa-app 返回非 JSON: {resp.text[:200]}")

    def _ensure_phone(self, phone: PhoneInput | dict) -> dict:
        """统一 phone 参数为 dict。"""
        if isinstance(phone, PhoneInput):
            return phone.model_dump()
        return dict(phone)

    def _build_phone_payload(
        self,
        phone: PhoneInput | dict,
        *,
        proxy_url: str = "",
        correlation_id: str = "",
        request_id: str = "",
    ) -> dict:
        """构建兼容 whatsapp_WA BFF 的号码 payload。"""
        try:
            data = validate_phone_input(self._ensure_phone(phone)).model_dump()
        except PhoneNormalizationError as exc:
            raise WaApiError(f"bridge phone payload invalid: {exc}") from exc
        national = _text(data.get("national_number"), data.get("phone"), data.get("number"))
        iso2 = _text(data.get("country_iso2"), data.get("region")).lower()
        calling_code = _text(data.get("country_calling_code"), data.get("country_code"), data.get("cc"))
        e164 = _text(data.get("e164_number"), data.get("e164"))

        data["phone"] = {
            "e164_number": e164,
            "country_calling_code": calling_code,
            "national_number": national,
            "country_iso2": iso2,
        }
        data["e164_number"] = e164
        data["country_calling_code"] = calling_code
        data["national_number"] = national
        data["country_iso2"] = iso2
        data["region"] = iso2
        if proxy_url:
            data["proxy_url"] = proxy_url
            data["proxy"] = {"proxy_url": proxy_url}
        if correlation_id:
            data["correlation_id"] = correlation_id
            data["job_id"] = correlation_id
        if request_id:
            data["request_id"] = request_id
        return data

    # ── API 方法 ────────────────────────────────────────

    async def health(self) -> dict:
        """探测上游健康与能力。"""
        raw = await self._get_json("/api/wa/health")
        return raw if isinstance(raw, dict) else {}

    async def probe_phone(
        self,
        phone: PhoneInput | dict,
        *,
        proxy_url: str = "",
        correlation_id: str = "",
        request_id: str = "",
    ) -> ProbeResult:
        """POST /api/wa/phone/sms-probe 号码探测。"""
        data = self._build_phone_payload(
            phone,
            proxy_url=proxy_url,
            correlation_id=correlation_id,
            request_id=request_id,
        )
        raw = await self._post_json("/api/wa/phone/sms-probe", data)
        return ProbeResult(raw)

    async def register(
        self,
        phone: PhoneInput | dict,
        delivery_method: str = "VERIFICATION_DELIVERY_METHOD_SMS",
        *,
        proxy_url: str = "",
        correlation_id: str = "",
        request_id: str = "",
    ) -> RegisterResult:
        """POST /api/wa/register 发起注册（探测+请求 OTP 一步）。"""
        data = self._build_phone_payload(
            phone,
            proxy_url=proxy_url,
            correlation_id=correlation_id,
            request_id=request_id,
        )
        data["delivery_method"] = delivery_method
        raw = await self._post_json("/api/wa/register", data)
        return RegisterResult(raw)

    async def submit_otp(
        self,
        wa_account_id: str,
        otp: str,
        *,
        verification_request_id: str = "",
        proxy_url: str = "",
        correlation_id: str = "",
        request_id: str = "",
    ) -> RegisterResult:
        """POST /api/wa/actions/registration/resume-otp 提交验证码。

        注意：返回的 success 是注册状态+登录态双重条件。
        """
        payload = {
            "wa_account_id": wa_account_id,
            "otp": otp,
            "code": otp,
            "verification_code": otp,
        }
        if verification_request_id:
            payload["verification_request_id"] = verification_request_id
        if proxy_url:
            payload["proxy_url"] = proxy_url
            payload["proxy"] = {"proxy_url": proxy_url}
        if correlation_id:
            payload["correlation_id"] = correlation_id
            payload["job_id"] = correlation_id
        if request_id:
            payload["request_id"] = request_id
        raw = await self._post_json("/api/wa/actions/registration/resume-otp", payload)
        return RegisterResult(raw)

    async def check_login_state(
        self,
        wa_account_id: str,
        login_state_id: str = "",
        *,
        proxy_url: str = "",
        correlation_id: str = "",
        request_id: str = "",
    ) -> bool:
        """POST /api/wa/login-state-check 检查登录态是否 ACTIVE。

        优先使用 login_state_id（从 resume-otp 返回中提取），
        fallback 到 wa_account_id。
        """
        try:
            body = {"wa_account_id": wa_account_id}
            if login_state_id:
                body["login_state_id"] = login_state_id
            if proxy_url:
                body["proxy_url"] = proxy_url
                body["proxy"] = {"proxy_url": proxy_url}
            if correlation_id:
                body["correlation_id"] = correlation_id
                body["job_id"] = correlation_id
            if request_id:
                body["request_id"] = request_id
            raw = await self._post_json("/api/wa/login-state-check", body)
            raw_obj = _object(raw)
            ls = _object(raw_obj.get("login_state"))
            return _active_login_status(_text(ls.get("status"), raw_obj.get("status")))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "wa-app 登录态检查失败: account=%s correlation=%s error=%s",
                mask_identifier(wa_account_id),
                correlation_id or "-",
                exc,
            )
            raise

    async def cleanup_failed_account(self, wa_account_id: str, *, verification_request_id: str = "") -> dict:
        """POST /api/wa/actions/registration/cleanup-failed-account

        注意：只接受 wa_account_id，不接受 phone 格式。
        """
        payload = {"wa_account_id": wa_account_id}
        if verification_request_id:
            payload["verification_request_id"] = verification_request_id
        raw = await self._post_json("/api/wa/actions/registration/cleanup-failed-account", payload)
        return raw if isinstance(raw, dict) else {"success": True, "deleted": True}

    async def list_accounts(self, limit: int = 100) -> list[dict]:
        """GET /api/wa/accounts 列出账号。"""
        raw = await self._get_json(f"/api/wa/accounts?limit={limit}")
        if isinstance(raw, list):
            return [a for a in raw if isinstance(a, dict)]
        raw_obj = _object(raw)
        accounts = raw_obj.get("accounts")
        if isinstance(accounts, list):
            return [a for a in accounts if isinstance(a, dict)]
        return []


WaApiClient = WhatsappWaAdapter
