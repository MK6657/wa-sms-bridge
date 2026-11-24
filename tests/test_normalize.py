"""号码归一化测试 — SmsBowerProvider.normalize_phone。"""

import pytest
from unittest.mock import AsyncMock
from app.providers.smsbower import SmsBowerProvider
from app.models import PhoneInput
from app.phone_normalizer import PhoneNormalizationError, validate_phone_input


@pytest.fixture
def provider():
    p = SmsBowerProvider(api_key_getter=AsyncMock(return_value="test"))
    return p


@pytest.mark.parametrize("raw,country_id,expected_e164,expected_cc,expected_iso", [
    ("79001234567", 0, "+79001234567", "7", "ru"),       # 俄罗斯，含国家码
    ("+79001234567", 0, "+79001234567", "7", "ru"),      # 俄罗斯，已 E.164
    ("66987761343", 6, "+6266987761343", "62", "id"),    # 印尼 ID 优先，不静默切到泰国
    ("62877654321", 6, "+62877654321", "62", "id"),      # 印尼，带 cc
    ("27705551234", 31, "+27705551234", "27", "za"),     # 南非
    ("+27630445764", 31, "+27630445764", "27", "za"),    # 南非，已 E.164
    ("8613800138000", 3, "+8613800138000", "86", "cn"),  # 中国
    ("+525512345678", 52, "+66525512345678", "66", "th"), # 泰国 ID 优先，不静默切到墨西哥
])
@pytest.mark.asyncio
async def test_normalize_phone(provider, raw, country_id, expected_e164, expected_cc, expected_iso):
    result = await provider.normalize_phone(raw, country_id)
    assert result.e164_number == expected_e164
    assert result.country_calling_code == expected_cc
    assert result.country_iso2 == expected_iso


@pytest.mark.asyncio
async def test_normalize_south_africa_fallback_has_required_country_fields(provider):
    result = await provider.normalize_phone("276523227", 31)
    assert result.e164_number == "+276523227"
    assert result.country_calling_code == "27"
    assert result.country_iso2 == "za"


@pytest.mark.asyncio
async def test_normalize_short_phone(provider):
    """极短号码不会崩溃。"""
    result = await provider.normalize_phone("1234", 0)
    assert result.phone == "1234"


@pytest.mark.asyncio
async def test_normalize_empty_phone(provider):
    """空号码不会崩溃。"""
    result = await provider.normalize_phone("", 0)
    assert result.phone == ""


def test_validate_rejects_short_possible_false_number():
    with pytest.raises(PhoneNormalizationError, match="possible"):
        validate_phone_input(PhoneInput(
            phone="1234",
            e164_number="+71234",
            country_calling_code="7",
            country_iso2="ru",
        ))


def test_validate_rejects_iso_calling_code_mismatch():
    with pytest.raises(PhoneNormalizationError, match="country_iso2"):
        validate_phone_input(PhoneInput(
            phone="705551234",
            e164_number="+27705551234",
            country_calling_code="27",
            country_iso2="ru",
        ))


def test_validate_rejects_national_mismatch():
    with pytest.raises(PhoneNormalizationError, match="national_number"):
        validate_phone_input(PhoneInput(
            phone="9001234567",
            e164_number="+79001234568",
            country_calling_code="7",
            country_iso2="ru",
        ))
