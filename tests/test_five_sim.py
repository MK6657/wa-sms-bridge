"""FiveSimProvider 基础测试。"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from app.providers.five_sim import FiveSimProvider
from app.providers.base import NoNumbersError, BalanceError


@pytest.fixture
def provider():
    p = FiveSimProvider(api_key_getter=AsyncMock(return_value="test-key"))
    return p


@pytest.mark.asyncio
async def test_resolve_country_string(provider):
    """5sim 国家 ID 返回字符串。"""
    cid, label = await provider.resolve_country("russia")
    assert cid == "russia"
    assert label


@pytest.mark.asyncio
async def test_resolve_country_invalid(provider):
    """无效国家抛异常。"""
    with pytest.raises(ValueError):
        await provider.resolve_country("")


@pytest.mark.asyncio
async def test_normalize_phone(provider):
    """号码归一化。"""
    result = await provider.normalize_phone("79001234567", "russia")
    assert result.e164_number.startswith("+7")


@pytest.mark.asyncio
async def test_normalize_phone_empty(provider):
    """空号码不崩溃。"""
    result = await provider.normalize_phone("", "")
    assert result.phone == ""
