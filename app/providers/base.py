"""SMS Provider 抽象基类与标准领域对象。

所有 SMS 接码平台必须实现 BaseSmsProvider。
基类不反向依赖任何具体平台实现，并提供结构化的取消/完成/重发结果解析（修复 P1-04）。
"""

from __future__ import annotations

import html
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from app.log_redaction import mask_identifier, mask_phone
from app.models import PhoneInput


# ── 公共异常体系 ────────────────────────────────────────


class SmsProviderError(Exception):
    """SMS provider 基类异常。"""


class NoNumbersError(SmsProviderError):
    """无可用号码。"""


class BalanceError(SmsProviderError):
    """余额不足。"""


class SmsCancelledError(SmsProviderError):
    """激活被取消。"""


class SmsExpiredError(SmsProviderError):
    """激活已过期/不存在。"""


class SmsProtocolError(SmsProviderError):
    """协议异常（未知状态码等）。"""


class SmsTimeoutError(SmsProviderError):
    """等码超时。"""


class PurchaseUncertainError(SmsProviderError):
    """取号请求可能已被上游处理，但服务未拿到订单句柄。"""


# ── 公共领域对象 ────────────────────────────────────────


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_resend: bool = False
    supports_price_lock: bool = True
    supports_custom_service: bool = True
    supports_exact_price_lock: bool = False
    confirms_full_refund_on_cancel: bool = False


@dataclass(frozen=True)
class CancelOutcome:
    """规范化的取消激活结果，明确区分已确认退款、已完结不可退与待重试。"""

    settled: bool
    status: str
    retryable: bool = False
    raw: str = ""
    refund_confirmed: bool = False
    refund_amount: Optional[float] = None
    refund_amount_source: str = ""


@dataclass(frozen=True)
class ActionOutcome:
    """规范化的完成/重发等上游动作结果。"""

    ok: bool
    status: str
    retryable: bool = False
    raw: str = ""


class Activation:
    """一次取号激活的封装。"""

    def __init__(
        self,
        activation_id: str,
        phone_number: str,
        country_id: Any,
        country_label: str,
        provider: str = "",
        price: Optional[float] = None,
        price_is_estimated: bool = False,
        currency: Optional[str] = "USD",
        currency_source: str = "",
    ):
        self.activation_id = activation_id
        self.phone_number = phone_number
        self.country_id = country_id
        self.country_label = country_label
        self.provider = provider
        self.price: Optional[float] = price
        self.price_is_estimated = bool(price_is_estimated)
        norm_ccy = (currency or "").strip().upper() if currency is not None else ""
        self.currency = norm_ccy or "UNKNOWN"
        self.currency_source = (
            currency_source.strip()
            if currency_source
            else ("provider_default" if self.currency != "UNKNOWN" else "unknown")
        )

    def __repr__(self) -> str:
        return f"<Activation {mask_identifier(self.activation_id)} phone={mask_phone(self.phone_number)}>"


def parse_cancel_outcome(raw_result: Any) -> CancelOutcome:
    """严格区分：明确退款确认 vs 已完成/不存在(不可确认退款) vs 冷却拒绝/异常(可重试)。"""
    text = str(raw_result or "").strip()
    normalized = text.upper()
    if not normalized:
        return CancelOutcome(settled=False, status="empty_response", retryable=True, raw=text, refund_confirmed=False)

    # 明确由上游确认取消/退款
    if any(token in normalized for token in ("ACCESS_CANCEL", "STATUS_CANCEL")) or normalized in {
        "CANCELLED",
        "CANCELED",
    }:
        return CancelOutcome(settled=True, status="refund_confirmed", retryable=False, raw=text, refund_confirmed=True)

    # 上游告知已完成（不可再视作已退款）
    if any(token in normalized for token in ("ACCESS_ACTIVATION", "ACCESS_READY", "COMPLETED", "FINISH")) or normalized == "READY":
        return CancelOutcome(settled=True, status="already_completed", retryable=False, raw=text, refund_confirmed=False)

    # 上游告知激活不存在、已过期或不可重试的致命参数/凭据错误（无法确认退款，也不再死循环重试）
    if any(
        token in normalized
        for token in ("NO_ACTIVATION", "EXPIRED", "NOT_FOUND", "WRONG_ACTIVATION_ID", "BAD_KEY", "BAD_ACTION", "BANNED")
    ):
        return CancelOutcome(settled=True, status="not_found_or_expired", retryable=False, raw=text, refund_confirmed=False)

    # 冷却期拒绝或暂态冲突，可后续重试
    if any(token in normalized for token in ("EARLY_CANCEL", "WAIT", "COOLDOWN", "BAD_STATUS")):
        return CancelOutcome(settled=False, status="early_cancel_denied", retryable=True, raw=text, refund_confirmed=False)

    return CancelOutcome(settled=False, status="unknown_response", retryable=True, raw=text, refund_confirmed=False)


_GOOGLE_OTP_RE = re.compile(r"(?i)(?<![A-Za-z0-9/_-])g-\s*(\d{4,8})\b")
_WEBOTP_RE = re.compile(r"(?m)@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\s+#(\d{2,4}-\d{2,4}|\d{4,8})(?![A-Za-z0-9-])")
_URL_RE = re.compile(
    r"(?:https?://\S+|(?<!@)\b[A-Za-z0-9-]+(?:\.[A-Za-z]{2,})+/[^\s<>\"')]+)",
    re.IGNORECASE,
)
_E164_PHONE_RE = re.compile(r"(?<!\d)\+\d{8,15}\b")
_FORMATTED_PHONE_RE = re.compile(r"(?<!\d)\+?\d{1,3}(?:[\t .-]+\d{2,4}|\(\d{2,4}\)){2,4}(?!\d)")
_ISO_DATE_RE = re.compile(r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b")
_CLOCK_TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\b")
_APP_HASH_TAIL_RE = re.compile(r"(?m)(?:^|\s)(?=[A-Za-z0-9+/]{11}\s*$)(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9+/]{11}\s*$")
_OTP_KEYWORDS = (
    r"(?:(?<![A-Za-z])(?:code|otp|pin|passcode|verification|código|codigo)(?![A-Za-z])"
    r"|验证码|驗證碼|校验码|校驗碼|动态码|動態碼|确认码|確認碼|安全码|安全碼|认证码|認證碼|登录码|登錄碼|提取码|代码|代碼"
    r"|コード|코드|код|รหัส)"
)
_OTP_TOKEN_PATTERN = (
    r"(?:\d{3}(?:[-\t ]+|\s+and\s+)\d{3}|\d{2}[-\t ]\d{2}[-\t ]\d{2}|\d{3,4}[-\t ]\d{3,4}|\d{4,8})"
)
_KEYWORD_FWD_OTP_RE = re.compile(
    rf"(?i){_OTP_KEYWORDS}[^\d\n]{{0,24}}?({_OTP_TOKEN_PATTERN})(?!\d)"
)
_KEYWORD_REV_OTP_RE = re.compile(
    rf"(?i)(?<!\d)({_OTP_TOKEN_PATTERN})(?!\d)[^\d\n]{{0,24}}?(?:(?<![A-Za-z])(?:is\s+your|for\s+your)(?![A-Za-z])|{_OTP_KEYWORDS})"
)
_HYPHEN_OTP_RE = re.compile(r"(?<![A-Za-z0-9-])(\d{3,4}-\d{3,4})(?![A-Za-z0-9-])")
_STANDALONE_OTP_RE = re.compile(
    r"(?<![A-Za-z0-9._/-])(\d{4,8})(?![A-Za-z0-9._/-])"
    r"(?!\s*(?:(?:min|mins|minute|minutes|sec|secs|second|seconds|hr|hrs|hour|hours|day|days|usd|rub|eur)\b|分钟|分|秒钟|秒|小时|天|年|元))",
    re.IGNORECASE,
)


def _strip_long_formatted_phones(text: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return " " if len(digits) >= 9 else m.group(0)

    return _FORMATTED_PHONE_RE.sub(_repl, text)


def extract_otp_code(raw: Any) -> str:
    """从上游短信文本或预解析码串中精准提取单个验证码，避免把有效分钟数、URL 路径、发送方号码或应用签名哈希拼入验证码。"""
    text = html.unescape(str(raw or "")).strip()[:4096]
    if not text:
        return ""
    if re.fullmatch(r"\d{3,8}|\d{3,4}[-\t ]\d{3,4}|\d{2}[-\t ]\d{2}[-\t ]\d{2}", text):
        return re.sub(r"\D", "", text)

    m_webotp = _WEBOTP_RE.search(text)
    if m_webotp:
        return re.sub(r"\D", "", m_webotp.group(1))

    # 先剔除 URL、国际长号码、日期时间与尾部 11 位签名哈希，防止 URL 路径如 /g-999999 抢占验证码
    cleaned = _URL_RE.sub(" ", text)
    cleaned = _E164_PHONE_RE.sub(" ", cleaned)
    cleaned = _ISO_DATE_RE.sub(" ", cleaned)
    cleaned = _CLOCK_TIME_RE.sub(" ", cleaned)
    cleaned = _APP_HASH_TAIL_RE.sub(" ", cleaned)

    m_google = _GOOGLE_OTP_RE.search(cleaned)
    if m_google:
        return m_google.group(1)

    # 先执行紧邻验证码关键词的正/反向提取，避免 "Code: 123-456 (valid 10 min)" 被格式化电话正则误吞
    m_fwd = _KEYWORD_FWD_OTP_RE.search(cleaned)
    if m_fwd:
        return re.sub(r"\D", "", m_fwd.group(1))

    m_rev = _KEYWORD_REV_OTP_RE.search(cleaned)
    if m_rev:
        return re.sub(r"\D", "", m_rev.group(1))

    cleaned = _strip_long_formatted_phones(cleaned)

    m_hyph = _HYPHEN_OTP_RE.search(cleaned)
    if m_hyph:
        return re.sub(r"\D", "", m_hyph.group(1))

    candidates = [m.group(1) for m in _STANDALONE_OTP_RE.finditer(cleaned)]
    if candidates:
        for cand in candidates:
            if len(cand) == 6:
                return cand
        for cand in candidates:
            if len(cand) in {5, 7, 8}:
                return cand
        for cand in candidates:
            if not (len(cand) == 4 and 1990 <= int(cand) <= 2039):
                return cand
        return candidates[0]

    return ""


def parse_complete_outcome(raw_result: Any) -> ActionOutcome:
    """严格校验完成激活（status=6）上游应答。"""
    text = str(raw_result or "").strip()
    normalized = text.upper()
    if any(neg in normalized for neg in ("NOT_FINISH", "UNFINISH", "NOT_COMPLETE", "INCOMPLETE")):
        return ActionOutcome(ok=False, status="rejected", retryable=False, raw=text)
    if any(token in normalized for token in ("ACCESS_ACTIVATION", "ACCESS_READY", "COMPLETED", "FINISHED", "FINISH")) or normalized == "OK":
        return ActionOutcome(ok=True, status="completed", retryable=False, raw=text)
    if any(token in normalized for token in ("ERROR_SQL", "SQL_ERROR", "INTERNAL_ERROR")):
        return ActionOutcome(ok=False, status="transient_error", retryable=True, raw=text)
    if any(
        token in normalized
        for token in (
            "NO_ACTIVATION",
            "BAD_STATUS",
            "BAD_KEY",
            "BAD_ACTION",
            "WRONG_ACTIVATION_ID",
            "ACCESS_CANCEL",
            "STATUS_CANCEL",
            "CANCELED",
            "CANCELLED",
            "EXPIRED",
            "ERROR",
        )
    ):
        return ActionOutcome(ok=False, status="rejected", retryable=False, raw=text)
    return ActionOutcome(ok=False, status="unknown_response", retryable=True, raw=text)


def parse_resend_outcome(raw_result: Any) -> ActionOutcome:
    """严格校验请求重发短信（status=3）上游应答。"""
    text = str(raw_result or "").strip()
    normalized = text.upper()
    if any(token in normalized for token in ("ACCESS_RETRY_GET", "ACCESS_READY", "STATUS_WAIT_RETRY")) or normalized == "OK":
        return ActionOutcome(ok=True, status="retry_requested", retryable=False, raw=text)
    if any(token in normalized for token in ("ERROR_SQL", "SQL_ERROR", "INTERNAL_ERROR")):
        return ActionOutcome(ok=False, status="transient_error", retryable=True, raw=text)
    if any(
        token in normalized
        for token in (
            "NO_ACTIVATION",
            "BAD_STATUS",
            "BAD_KEY",
            "BAD_ACTION",
            "WRONG_ACTIVATION_ID",
            "ACCESS_CANCEL",
            "STATUS_CANCEL",
            "CANCELED",
            "CANCELLED",
            "EXPIRED",
            "ERROR",
        )
    ):
        return ActionOutcome(ok=False, status="rejected", retryable=False, raw=text)
    return ActionOutcome(ok=False, status="unknown_response", retryable=True, raw=text)


class BaseSmsProvider(ABC):
    """所有 SMS 接码平台必须实现的抽象基类。"""

    provider_id: str = ""
    capabilities: ProviderCapabilities = ProviderCapabilities()
    _last_raw_sms: dict[str, str]

    def record_raw_sms(self, activation_id: str, raw_text: str) -> None:
        store = getattr(self, "_last_raw_sms", None)
        if store is None:
            store = {}
            self._last_raw_sms = store
        if len(store) >= 512 and activation_id not in store:
            store.pop(next(iter(store)), None)
        if activation_id:
            store[str(activation_id)] = str(raw_text or "")

    def get_last_raw_sms(self, activation_id: str) -> str:
        store = getattr(self, "_last_raw_sms", None)
        if not store or not activation_id:
            return ""
        return str(store.get(str(activation_id), "") or "")

    async def resolve_country(self, country: str) -> tuple[Any, str]:
        raw = str(country or "").strip()
        if not raw:
            raise ValueError("未知国家: empty")
        if raw.isdigit():
            cid = int(raw)
            return cid, f"Country #{cid}"
        return raw.lower(), raw

    @abstractmethod
    async def get_balance(self) -> float:
        ...

    @abstractmethod
    async def get_countries(self) -> list[dict]:
        ...

    @abstractmethod
    async def get_prices(self, country_id: Any, product: str = "whatsapp") -> list[dict]:
        ...

    @abstractmethod
    async def request_number(
        self,
        country_id: Any,
        service: str = "whatsapp",
        price_max: Optional[float] = None,
    ) -> Activation:
        ...

    @abstractmethod
    async def get_sms_code(self, activation_id: str) -> tuple[str, str]:
        """返回 (status, code_or_empty)。status: OK / WAIT / CANCEL / EXPIRED / UNKNOWN"""
        ...

    @abstractmethod
    async def cancel_activation(self, activation_id: str) -> str:
        ...

    @abstractmethod
    async def complete_activation(self, activation_id: str) -> str:
        ...

    @abstractmethod
    async def poll_sms_code(self, activation_id: str, timeout: int = 180) -> str:
        ...

    async def request_additional_sms(self, activation_id: str) -> str:
        raise NotImplementedError(f"Provider '{self.provider_id}' 不支持请求重发短信")

    async def cancel_activation_structured(self, activation_id: str) -> CancelOutcome:
        try:
            raw = await self.cancel_activation(activation_id)
            return parse_cancel_outcome(raw)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "cancel_activation_structured %s failed: %s",
                mask_identifier(activation_id),
                exc,
            )
            return CancelOutcome(settled=False, status="error", retryable=True, raw=str(exc), refund_confirmed=False)

    async def complete_activation_structured(self, activation_id: str) -> ActionOutcome:
        try:
            raw = await self.complete_activation(activation_id)
            return parse_complete_outcome(raw)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "complete_activation_structured %s failed: %s",
                mask_identifier(activation_id),
                exc,
            )
            return ActionOutcome(ok=False, status="error", retryable=True, raw=str(exc))

    async def request_additional_sms_structured(self, activation_id: str) -> ActionOutcome:
        try:
            raw = await self.request_additional_sms(activation_id)
            return parse_resend_outcome(raw)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "request_additional_sms_structured %s failed: %s",
                mask_identifier(activation_id),
                exc,
            )
            return ActionOutcome(ok=False, status="error", retryable=True, raw=str(exc))

    async def normalize_phone(self, raw_phone: str, country_id: Any) -> PhoneInput:
        from app.phone_normalizer import build_phone_input, resolve_country_metadata

        rows: list[dict] = []
        try:
            rows = await self.get_countries()
        except Exception:
            rows = []
        meta = resolve_country_metadata(country_id, country_label=str(country_id or ""), countries=rows)
        return build_phone_input(
            raw_phone,
            country_calling_code=meta.get("calling_code", ""),
            country_iso2=meta.get("iso2", ""),
            country_label=meta.get("name", ""),
        )

    async def validate_registration_country(self, country_id: Any):
        return None

    async def _safe_cancel(self, activation_id: str):
        try:
            return await self.cancel_activation(activation_id)
        except Exception as e:
            logging.getLogger(__name__).warning(
                "safe cancel activation %s failed: %s",
                mask_identifier(activation_id),
                e,
            )
            return ""
