"""HeroSmsProvider 基础测试。"""

import pytest
from unittest.mock import AsyncMock
from app.providers.hero_sms import HeroSmsProvider
from app.providers.base import NoNumbersError


@pytest.fixture
def provider():
    p = HeroSmsProvider(api_key_getter=AsyncMock(return_value="test-key"))
    return p


@pytest.mark.asyncio
async def test_resolve_country_uses_hero_specific_us_virtual_id(provider):
    cid, label = await provider.resolve_country("美国")
    assert cid == 12
    assert label == "United States"


@pytest.mark.asyncio
async def test_resolve_country_supports_south_africa_aliases(provider):
    assert await provider.resolve_country("South Africa") == (31, "South Africa")
    assert await provider.resolve_country("南非") == (31, "South Africa")


@pytest.mark.asyncio
async def test_resolve_country_rejects_unknown_smsbower_id(provider):
    with pytest.raises(ValueError):
        await provider.resolve_country("187")


@pytest.mark.asyncio
async def test_normalize_phone(provider):
    """号码归一化。"""
    result = await provider.normalize_phone("79001234567", 0)
    assert result.e164_number.startswith("+7")


@pytest.mark.asyncio
async def test_normalize_phone_empty(provider):
    """空号码不崩溃。"""
    result = await provider.normalize_phone("", 0)
    assert result.phone == ""


@pytest.mark.asyncio
async def test_get_sms_code_status_ok(provider):
    """STATUS_OK: 正确提取验证码。"""
    # mock 内部 _get 方法
    async def mock_get(**kw):
        return "STATUS_OK:482262"
    provider._get = mock_get
    status, code = await provider.get_sms_code("123")
    assert status == "OK"
    assert code == "482262"


@pytest.mark.asyncio
async def test_get_sms_code_no_activation(provider):
    """STATUS_NO_ACTIVATION 返回 EXPIRED。"""
    async def mock_get(**kw):
        return "STATUS_NO_ACTIVATION"
    provider._get = mock_get
    status, code = await provider.get_sms_code("123")
    assert status == "EXPIRED"


@pytest.mark.asyncio
async def test_get_sms_code_wait_retry(provider):
    """STATUS_WAIT_RETRY 返回 WAIT。"""
    async def mock_get(**kw):
        return "STATUS_WAIT_RETRY"
    provider._get = mock_get
    status, code = await provider.get_sms_code("123")
    assert status == "WAIT"


@pytest.mark.asyncio
async def test_normalize_phone_prefix(provider):
    """号码已带 + 前缀也能正确解析。"""
    result = await provider.normalize_phone("+27630445764", 31)
    assert "+27" in result.e164_number
