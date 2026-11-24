"""Phone normalization helpers shared by SMS provider adapters.

The bridge owns the phone payload sent to whatsapp_WA. Providers may return
numbers in provider-specific shapes, but the adapter boundary must always see
an E.164 number plus stable country metadata.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

import phonenumbers

from app.countries import cc_to_iso, iso_to_cc, name_to_iso
from app.models import PhoneInput


class PhoneNormalizationError(ValueError):
    """Raised when a phone cannot be made safe for the WA adapter."""


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _text(value: Any) -> str:
    return str(value or "").strip()


def resolve_country_metadata(
    country_id: Any = "",
    *,
    country_label: str = "",
    countries: Iterable[dict] | None = None,
) -> dict[str, str]:
    """Resolve minimal country metadata from provider country rows.

    Returns keys: provider_country_id, name, iso2, calling_code.
    """

    key = _text(country_id).lower()
    label = _text(country_label)
    matched: dict[str, Any] = {}
    for item in countries or []:
        item_id = _text(item.get("id")).lower()
        item_name = _text(item.get("name"))
        item_iso = _text(item.get("iso")).lower()
        item_cc = _digits(item.get("cc"))
        fields = {item_id, item_name.lower(), item_iso, item_cc}
        if key and key in fields:
            matched = dict(item)
            break

    name = _text(matched.get("name")) or label
    iso2 = _text(matched.get("iso")).lower()
    calling_code = _digits(matched.get("cc"))

    if not iso2 and name:
        iso2 = name_to_iso(name)
    if not iso2 and label:
        iso2 = name_to_iso(label)
    if not calling_code and iso2:
        calling_code = iso_to_cc(iso2)
    if not iso2 and calling_code:
        iso2 = cc_to_iso(calling_code)

    return {
        "provider_country_id": _text(matched.get("id")) or _text(country_id),
        "name": name,
        "iso2": iso2,
        "calling_code": calling_code,
    }


def _format_parsed(
    parsed: phonenumbers.PhoneNumber,
    *,
    country_calling_code: str = "",
    country_iso2: str = "",
) -> PhoneInput:
    region = country_iso2.upper() if country_iso2 else phonenumbers.region_code_for_number(parsed) or ""
    return PhoneInput(
        region=region,
        phone=str(parsed.national_number),
        e164_number=phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
        country_calling_code=country_calling_code or str(parsed.country_code),
        country_iso2=country_iso2 or region.lower(),
    )


def _parsed_matches_metadata(parsed: phonenumbers.PhoneNumber, cc: str, iso2: str) -> bool:
    parsed_cc = str(parsed.country_code)
    if cc and parsed_cc != cc:
        return False
    if iso2:
        iso_cc = iso_to_cc(iso2)
        if iso_cc and parsed_cc != iso_cc:
            return False
        regions = phonenumbers.COUNTRY_CODE_TO_REGION_CODE.get(parsed.country_code, ())
        if regions and iso2.upper() not in regions:
            return False
    return True


def build_phone_input(
    raw_phone: Any,
    *,
    country_calling_code: str = "",
    country_iso2: str = "",
    country_label: str = "",
) -> PhoneInput:
    """Build a best-effort PhoneInput without calling external services.

    Invalid virtual numbers are still carried forward if country metadata is
    known, because some SMS platforms sell ranges libphonenumber considers
    unusual. The WA boundary performs the final required-field validation.
    """

    raw_text = _text(raw_phone)
    raw_digits = _digits(raw_text)
    cc = _digits(country_calling_code)
    iso2 = _text(country_iso2).lower()

    if not iso2 and country_label:
        iso2 = name_to_iso(country_label)
    if not cc and iso2:
        cc = iso_to_cc(iso2)
    if not iso2 and cc:
        iso2 = cc_to_iso(cc)

    if not raw_digits:
        return PhoneInput(
            region=iso2.upper(),
            phone="",
            e164_number="",
            country_calling_code=cc,
            country_iso2=iso2,
        )

    if cc:
        if raw_text.startswith("+") or raw_digits.startswith(cc):
            candidates = [f"+{raw_digits}"]
        else:
            candidates = [f"+{cc}{raw_digits}", f"+{raw_digits}"]
    else:
        candidates = [f"+{raw_digits}"]

    seen: set[str] = set()
    first_possible: phonenumbers.PhoneNumber | None = None
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = phonenumbers.parse(candidate, None)
        except phonenumbers.NumberParseException:
            continue
        if not _parsed_matches_metadata(parsed, cc, iso2):
            continue
        if phonenumbers.is_valid_number(parsed):
            return _format_parsed(parsed, country_calling_code=cc, country_iso2=iso2)
        if first_possible is None and phonenumbers.is_possible_number(parsed):
            first_possible = parsed

    if first_possible and not cc:
        return _format_parsed(first_possible)

    national = raw_digits
    if cc and raw_digits.startswith(cc):
        national = raw_digits[len(cc):]
    e164 = f"+{cc}{national}" if cc else f"+{raw_digits}"
    return PhoneInput(
        region=iso2.upper(),
        phone=national,
        e164_number=e164,
        country_calling_code=cc,
        country_iso2=iso2,
    )


def validate_phone_input(phone: PhoneInput | dict[str, Any]) -> PhoneInput:
    """Validate the minimum WA adapter phone contract."""

    data = phone.model_dump() if isinstance(phone, PhoneInput) else dict(phone)
    result = PhoneInput(**data)
    errors: list[str] = []
    e164 = _text(result.e164_number)
    cc = _digits(result.country_calling_code)
    iso2 = _text(result.country_iso2).lower()
    national = _digits(result.phone)

    if not e164.startswith("+"):
        errors.append("e164_number must start with +")
    if not cc:
        errors.append("country_calling_code is required")
    elif not cc.isdigit():
        errors.append("country_calling_code must be digits")
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        errors.append("country_iso2 is required")
    if not national:
        errors.append("national_number is required")

    if e164.startswith("+") and cc:
        try:
            parsed = phonenumbers.parse(e164, None)
            if str(parsed.country_code) != cc:
                errors.append("country_calling_code does not match e164_number")
            if not phonenumbers.is_possible_number(parsed):
                errors.append("e164_number is not a possible phone number")
            parsed_national = _digits(parsed.national_number)
            if parsed_national and national and parsed_national != national:
                errors.append("national_number does not match e164_number")
        except phonenumbers.NumberParseException:
            errors.append("e164_number is not parseable")

    if cc and national and _digits(e164) != f"{cc}{national}":
        errors.append("e164_number does not equal country_calling_code + national_number")

    if iso2 and cc:
        iso_cc = iso_to_cc(iso2)
        if iso_cc and iso_cc != cc:
            errors.append("country_iso2 does not match country_calling_code")
        try:
            regions = phonenumbers.COUNTRY_CODE_TO_REGION_CODE.get(int(cc), ())
        except ValueError:
            regions = ()
        if regions and iso2.upper() not in regions:
            errors.append("country_iso2 is not valid for country_calling_code")

    if errors:
        raise PhoneNormalizationError("; ".join(errors))
    result.country_calling_code = cc
    result.country_iso2 = iso2
    return result
