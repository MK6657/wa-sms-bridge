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


_QUERY_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key=)[^&\s]+"),
    re.compile(r"(?i)(token=)[^&\s]+"),
    re.compile(r"(?i)(password=)[^&\s]+"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s,;]+"),
)

_SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?i)\b(activation|activation_id|wa_account_id|verification_request_id|login_state_id|"
    r"challenge_data|api_key|token|password)=([^\s,;)\]}]+)"
)

_JSON_SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?i)([\"']?(activation|activation_id|wa_account_id|verification_request_id|login_state_id|"
    r"challenge_data|api_key|token|password)[\"']?\s*:\s*[\"'])([^\"']+)([\"'])"
)
_JSON_PHONE_FIELD_PATTERN = re.compile(
    r"(?i)([\"']?(phone|phone_number|e164_number)[\"']?\s*:\s*[\"'])([+]?\d[\d\s().-]{6,}\d)([\"'])"
)
_ACCESS_NUMBER_PATTERN = re.compile(r"(?i)(ACCESS_NUMBER:)([^:\s]+):([+]?\d[\d\s().-]{6,}\d)")
_ACTIVATION_WORD_PATTERN = re.compile(r"(?i)\b(activation)\s+([A-Za-z0-9_.:-]{6,})")
_PHONE_FIELD_PATTERN = re.compile(r"(?i)\b(phone|phone_number|e164_number)=([+]?\d[\d\s().-]{6,}\d)")
_LONG_PHONE_PATTERN = re.compile(r"(?<![\w.])\+?\d{9,15}(?![\w.])")


def redact_text(value: Any) -> str:
    text = str(value or "")
    for pattern in _QUERY_SECRET_PATTERNS:
        text = pattern.sub(r"\1***", text)

    text = _ACCESS_NUMBER_PATTERN.sub(lambda m: f"{m.group(1)}{mask_identifier(m.group(2))}:{mask_phone(m.group(3))}", text)
    text = _JSON_PHONE_FIELD_PATTERN.sub(lambda m: f"{m.group(1)}{mask_phone(m.group(3))}{m.group(4)}", text)
    text = _JSON_SENSITIVE_FIELD_PATTERN.sub(lambda m: f"{m.group(1)}{mask_identifier(m.group(3))}{m.group(4)}", text)
    text = _PHONE_FIELD_PATTERN.sub(lambda m: f"{m.group(1)}={mask_phone(m.group(2))}", text)
    text = _SENSITIVE_FIELD_PATTERN.sub(lambda m: f"{m.group(1)}={mask_identifier(m.group(2))}", text)
    text = _ACTIVATION_WORD_PATTERN.sub(lambda m: f"{m.group(1)} {mask_identifier(m.group(2))}", text)
    text = _LONG_PHONE_PATTERN.sub(lambda m: mask_phone(m.group(0)), text)
    return text
