"""Shared log redaction helpers."""

from __future__ import annotations

import re
from typing import Any


def _mask_keep_edges(value: str, prefix: int = 4, suffix: int = 4) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= prefix + suffix:
        return text[: max(1, min(2, len(text)))] + "****"
    return text[:prefix] + "****" + text[-suffix:]


def mask_identifier(value: Any) -> str:
    return _mask_keep_edges(str(value or ""), 4, 4)


def mask_phone(value: Any) -> str:
    text = str(value or "")
    sign = "+" if text.startswith("+") else ""
    digits = re.sub(r"\D", "", text)
    if len(digits) < 7:
        return text
    if len(digits) <= 10:
        return f"{sign}{digits[:3]}****{digits[-2:]}"
    return f"{sign}{digits[:4]}****{digits[-4:]}"


def mask_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 2:
        return "****"
    return text[:2] + "*" * max(2, len(text) - 2)


_QUERY_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key=)[^&\s]+"),
    re.compile(r"(?i)(token=)[^&\s]+"),
    re.compile(r"(?i)(password=)[^&\s]+"),
    re.compile(r"(?i)((?:webhook[_-]?)?secret(?:[_-]?key)?=)[^&\s]+"),
    re.compile(r"(?i)(sms_hub_session=)[^;\s,]+"),
    re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)[^\s,;]+"),
    re.compile(r"(?i)(x-api-key:\s*)[^\s,;]+"),
    re.compile(r"(?i)(cookie:\s*)[^\r\n]+"),
)

_URL_EMBEDDED_CRED_PATTERN = re.compile(r"(?i)(://[^/:\s@]+:)[^@\s/]+(@)")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

_FULL_REDACT_FIELD_NAMES = frozenset({
    "api_key",
    "x-api-key",
    "authorization",
    "cookie",
    "sms_hub_session",
    "token",
    "password",
    "secret",
    "secret_key",
    "bridge_secret_key",
    "sms_code",
    "latest_code",
    "code",
    "otp",
    "verification_code",
    "raw_text",
    "webhook_secret",
    "challenge_data",
})

_SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?i)\b(?P<name>activation|activation_id|wa_account_id|verification_request_id|login_state_id|"
    r"challenge_data|api_key|x-api-key|authorization|cookie|sms_hub_session|token|password|secret|secret_key|bridge_secret_key|"
    r"sms_code|latest_code|code|otp|verification_code|raw_text|webhook_secret)=(?P<val>[^\s,;)\]}]+)"
)

_JSON_SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?i)(?P<prefix>[\"']?(?P<name>activation|activation_id|wa_account_id|verification_request_id|login_state_id|"
    r"challenge_data|api_key|x-api-key|authorization|cookie|sms_hub_session|token|password|secret|secret_key|bridge_secret_key|"
    r"sms_code|latest_code|code|otp|verification_code|raw_text|webhook_secret)[\"']?\s*:\s*)(?P<q>[\"'])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"
)
_JSON_NUMERIC_CODE_FIELD_PATTERN = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:sms_code|latest_code|code|otp|verification_code)[\"']?\s*:\s*)(?P<val>\d{3,10})\b"
)
_JSON_PHONE_FIELD_PATTERN = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:phone|phone_number|e164_number|phone_e164|phone_raw)[\"']?\s*:\s*)(?P<q>[\"'])(?P<val>[+]?\d[\d\s().-]{6,}\d)(?P=q)"
)
_ACCESS_NUMBER_PATTERN = re.compile(r"(?i)(ACCESS_NUMBER:)([^:\s]+):([+]?\d[\d\s().-]{6,}\d)")
_STATUS_OK_PATTERN = re.compile(r"(?i)(STATUS_OK:)([^\r\n]+)")
_STATUS_WAIT_RETRY_PATTERN = re.compile(r"(?i)(STATUS_WAIT_RETRY:)([^\r\n]+)")
_PROJECT_KEY_PATTERN = re.compile(r"\bsms_k_[A-Za-z0-9_-]{8,}\b")
_ACTIVATION_WORD_PATTERN = re.compile(r"(?i)\b(activation)\s+([A-Za-z0-9_.:-]{6,})")
_PHONE_FIELD_PATTERN = re.compile(r"(?i)\b(phone|phone_number|e164_number|phone_e164|phone_raw)=([+]?\d[\d\s().-]{6,}\d)")
_LONG_PHONE_PATTERN = re.compile(r"(?<![\w.])\+?\d{9,15}(?![\w.])")


def _redact_field_value(name: str, val: str) -> str:
    if (name or "").lower() in _FULL_REDACT_FIELD_NAMES:
        return "***"
    return mask_identifier(val)


def redact_text(value: Any) -> str:
    text = str(value or "")
    text = _URL_EMBEDDED_CRED_PATTERN.sub(r"\1***\2", text)
    for pattern in _QUERY_SECRET_PATTERNS:
        text = pattern.sub(r"\1***", text)

    text = _JWT_PATTERN.sub("***", text)
    text = _PROJECT_KEY_PATTERN.sub(lambda m: mask_identifier(m.group(0)), text)
    text = _STATUS_OK_PATTERN.sub(r"\1***", text)
    text = _STATUS_WAIT_RETRY_PATTERN.sub(r"\1***", text)
    text = _ACCESS_NUMBER_PATTERN.sub(
        lambda m: f"{m.group(1)}{mask_identifier(m.group(2))}:{mask_phone(m.group(3))}",
        text,
    )
    text = _JSON_PHONE_FIELD_PATTERN.sub(
        lambda m: f"{m.group('prefix')}{m.group('q')}{mask_phone(m.group('val'))}{m.group('q')}",
        text,
    )
    text = _JSON_SENSITIVE_FIELD_PATTERN.sub(
        lambda m: f"{m.group('prefix')}{m.group('q')}{_redact_field_value(m.group('name'), m.group('val'))}{m.group('q')}",
        text,
    )
    text = _JSON_NUMERIC_CODE_FIELD_PATTERN.sub(lambda m: f"{m.group('prefix')}\"***\"", text)
    text = _PHONE_FIELD_PATTERN.sub(lambda m: f"{m.group(1)}={mask_phone(m.group(2))}", text)
    text = _SENSITIVE_FIELD_PATTERN.sub(
        lambda m: f"{m.group('name')}={_redact_field_value(m.group('name'), m.group('val'))}",
        text,
    )
    text = _ACTIVATION_WORD_PATTERN.sub(lambda m: f"{m.group(1)} {mask_identifier(m.group(2))}", text)
    text = _LONG_PHONE_PATTERN.sub(lambda m: mask_phone(m.group(0)), text)
    return text
