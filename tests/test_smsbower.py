"""SMSBower Provider 测试 — 状态机映射、正则提取、国家解析。

覆盖：
- resolve_country_id 的多种解析路径
- get_sms_code 的状态码解析（OK / WAIT / CANCEL / EXPIRED / UNKNOWN）
- get_sms_code 的正则提取数字验证码
- request_number 的响应解析
- get_balance 的余额解析
- cancel_activation / complete_activation
- poll_sms_code 的成功和超时路径
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.providers.base import (
    Activation,
    BalanceError,
    NoNumbersError,
    SmsCancelledError,
    SmsExpiredError,
    PurchaseUncertainError,
    SmsProtocolError,
    SmsProviderError,
)
from app.providers.smsbower import (
    SmsBowerProvider,
    SmsTimeoutError,
    resolve_country_id,
)


# ════════════════════════════════════════════════════════
# resolve_country_id
# ════════════════════════════════════════════════════════

class TestResolveCountryId:
    """国家解析函数测试。"""

    def test_by_numeric_id(self):
        """数字 ID → 返回 (id, eng_label)。"""
        assert resolve_country_id(0) == (0, "Russian Federation")
        assert resolve_country_id(3) == (3, "China")
        assert resolve_country_id(187) == (187, "United States")

    def test_by_string_digit(self):
        """字符串数字 → 解析为 ID。"""
        assert resolve_country_id("0") == (0, "Russian Federation")
        assert resolve_country_id("187") == (187, "United States")

    def test_by_english_name(self):
        """英文国家名（大小写不敏感）→ 正确解析。"""
        assert resolve_country_id("russia") == (0, "Russian Federation")  # 别名 "russian federation" 变体
        result = resolve_country_id("united states")
        assert result[0] == 187
        assert result[1] == "United States"

    def test_by_english_name_exact(self):
        """精确英文名。"""
        assert resolve_country_id("Russian Federation") == (0, "Russian Federation")
        assert resolve_country_id("United Kingdom") == (16, "United Kingdom")
        assert resolve_country_id("India") == (22, "India")

    def test_by_chinese_name(self):
        """中文国家名 → 正确解析。"""
        assert resolve_country_id("俄罗斯联邦") == (0, "Russian Federation")
        assert resolve_country_id("美国") == (187, "United States")
        assert resolve_country_id("中国") == (3, "China")
        assert resolve_country_id("印度") == (22, "India")

    def test_by_partial_fuzzy_match(self):
        """部分英文名模糊匹配（key in eng.lower() 或 key in chn）。"""
        result = resolve_country_id("states")
        assert result[0] == 187

    def test_by_partial_chinese_fuzzy(self):
        """部分中文名模糊匹配。"""
        result = resolve_country_id("俄罗斯")
        assert result[0] == 0
        assert result[1] == "Russian Federation"

    def test_unknown_country_raises(self):
        """未知国家 → ValueError。"""
        with pytest.raises(ValueError, match="未知国家"):
            resolve_country_id("Atlantis")

    def test_unknown_numeric_id_returns_fallback_label(self):
        """未知数字 ID（不在 COUNTRIES 中）→ 返回 fallback label。"""
        cid, label = resolve_country_id(999)
        assert cid == 999
        assert "Country #999" in label

    def test_case_insensitive(self):
        """大小写不敏感。"""
        assert resolve_country_id("RUSSIAN FEDERATION") == (0, "Russian Federation")
        assert resolve_country_id("india") == (22, "India")

    def test_thailand(self):
        """泰国。"""
        assert resolve_country_id("Thailand") == (52, "Thailand")
        assert resolve_country_id("泰国") == (52, "Thailand")
        assert resolve_country_id(52) == (52, "Thailand")

    def test_south_africa_static_fallback(self):
        """南非静态兜底必须存在，供号码归一化补区号。"""
        assert resolve_country_id("South Africa") == (31, "South Africa")
        assert resolve_country_id("南非") == (31, "South Africa")
        assert resolve_country_id(31) == (31, "South Africa")

    def test_indonesia_fuzzy(self):
        """印尼模糊匹配。"""
        result = resolve_country_id("Indonesia")
        assert result[0] == 6


# ════════════════════════════════════════════════════════
# SmsBowerProvider — get_sms_code
# ════════════════════════════════════════════════════════

class TestGetSmsCode:
    """get_sms_code 状态机映射测试。"""

    @pytest.fixture
    def provider(self):
        """返回一个 _get 被 mock 的 SmsBowerProvider。"""
        async def get_key():
            return "test_key"
        p = SmsBowerProvider(get_key)
        p._get = AsyncMock()
        return p

    @pytest.mark.asyncio
    async def test_status_ok_with_code(self, provider):
        """STATUS_OK:123456 → ("OK", "123456")。"""
        provider._get.return_value = "STATUS_OK:123456"
        status, code = await provider.get_sms_code("act_1")
        assert status == "OK"
        assert code == "123456"

    @pytest.mark.asyncio
    async def test_status_ok_extract_digits(self, provider):
        """STATUS_OK:验证码是 123456 → 用正则提取纯数字。"""
        provider._get.return_value = "STATUS_OK:Your code is 987654"
        status, code = await provider.get_sms_code("act_1")
        assert status == "OK"
        assert code == "987654"

    @pytest.mark.asyncio
    async def test_status_ok_no_digits_returns_wait(self, provider):
        """STATUS_OK: 空/无数字 → 返回 WAIT（继续等）。"""
        provider._get.return_value = "STATUS_OK:"
        status, code = await provider.get_sms_code("act_1")
        assert status == "WAIT"
        assert code == ""

    @pytest.mark.asyncio
    async def test_status_ok_text_without_digits(self, provider):
        """STATUS_OK:SomeMessageNoDigits → WAIT（没有数字）。"""
        provider._get.return_value = "STATUS_OK:NoDigitsHere"
        status, code = await provider.get_sms_code("act_1")
        assert status == "WAIT"
        assert code == ""

    @pytest.mark.asyncio
    async def test_status_wait_code(self, provider):
        """STATUS_WAIT_CODE → ("WAIT", "")。"""
        provider._get.return_value = "STATUS_WAIT_CODE"
        status, code = await provider.get_sms_code("act_1")
        assert status == "WAIT"
        assert code == ""

    @pytest.mark.asyncio
    async def test_status_wait_retry(self, provider):
        """STATUS_WAIT_RETRY → ("WAIT", "")。"""
        provider._get.return_value = "STATUS_WAIT_RETRY"
        status, code = await provider.get_sms_code("act_1")
        assert status == "WAIT"
        assert code == ""

    @pytest.mark.asyncio
    async def test_status_cancel(self, provider):
        """STATUS_CANCEL → ("CANCEL", "")。"""
        provider._get.return_value = "STATUS_CANCEL"
        status, code = await provider.get_sms_code("act_1")
        assert status == "CANCEL"
        assert code == ""

    @pytest.mark.asyncio
    async def test_status_no_activation(self, provider):
        """STATUS_NO_ACTIVATION → ("EXPIRED", "")。"""
        provider._get.return_value = "STATUS_NO_ACTIVATION"
        status, code = await provider.get_sms_code("act_1")
        assert status == "EXPIRED"
        assert code == ""

    @pytest.mark.asyncio
    async def test_unknown_status(self, provider):
        """未知状态 → ("UNKNOWN", raw_text)。"""
        provider._get.return_value = "SOME_GARBAGE_RESPONSE"
        status, code = await provider.get_sms_code("act_1")
        assert status == "UNKNOWN"
        assert code == "SOME_GARBAGE_RESPONSE"

    @pytest.mark.asyncio
    async def test_multiple_digit_groups(self, provider):
        """多个数字组 → 提取纯数字（所有数字连起来）。"""
        provider._get.return_value = "STATUS_OK:Code 123 and 456"
        status, code = await provider.get_sms_code("act_1")
        assert status == "OK"
        assert code == "123456"


# ════════════════════════════════════════════════════════
# SmsBowerProvider — request_number
# ════════════════════════════════════════════════════════

class TestRequestNumber:
    """取号响应解析测试。"""

    @pytest.fixture
    def provider(self):
        async def get_key():
            return "test_key"
        p = SmsBowerProvider(get_key)
        p._get = AsyncMock()
        return p

    @pytest.mark.asyncio
    async def test_access_number_success(self, provider):
        """ACCESS_NUMBER:act_id:phone → Activation。"""
        provider._get.return_value = "ACCESS_NUMBER:123456:79001112233"
        act = await provider.request_number(0, "wa")
        assert isinstance(act, Activation)
        assert act.activation_id == "123456"
        assert act.phone_number == "79001112233"
        assert act.country_id == 0
        assert act.country_label == "Russian Federation"

    @pytest.mark.asyncio
    async def test_access_number_malformed(self, provider):
        """ACCESS_NUMBER 但 parts 不足 → SmsProtocolError。"""
        provider._get.return_value = "ACCESS_NUMBER:only_one_part"
        with pytest.raises(SmsProtocolError, match="响应格式异常"):
            await provider.request_number(0, "wa")

    @pytest.mark.asyncio
    async def test_no_numbers(self, provider):
        """NO_NUMBERS → NoNumbersError。"""
        provider._get.return_value = "NO_NUMBERS"
        with pytest.raises(NoNumbersError, match="无可用号码"):
            await provider.request_number(0, "wa")

    @pytest.mark.asyncio
    async def test_no_balance(self, provider):
        """NO_BALANCE → BalanceError。"""
        provider._get.return_value = "NO_BALANCE"
        with pytest.raises(BalanceError, match="余额不足"):
            await provider.request_number(0, "wa")

    @pytest.mark.asyncio
    async def test_unknown_error(self, provider):
        """其他错误 → SmsProviderError。"""
        provider._get.return_value = "BAD_SOMETHING"
        with pytest.raises(SmsProviderError, match="取号失败"):
            await provider.request_number(0, "wa")

    @pytest.mark.asyncio
    async def test_price_max_passed_to_params(self, provider):
        """price_max 参数传递给 _get 请求。"""
        provider._get.return_value = "ACCESS_NUMBER:act_1:79001112233"
        await provider.request_number(0, "wa", price_max=0.5)
        # _get 被调用时 maxPrice 在关键字参数中（**params 展开）
        call_kwargs = provider._get.call_args[1]
        assert call_kwargs.get("action") == "getNumber"
        assert call_kwargs.get("maxPrice") == "0.5"

    @pytest.mark.asyncio
    async def test_no_price_max(self, provider):
        """price_max 为 None 时不传 maxPrice。"""
        provider._get.return_value = "ACCESS_NUMBER:act_1:79001112233"
        await provider.request_number(0, "wa", price_max=None)
        # 检查调用参数
        call_kwargs = provider._get.call_args[1]
        params = call_kwargs.get("params", {})
        assert "maxPrice" not in params

    @pytest.mark.asyncio
    async def test_service_param_passed(self, provider):
        """service 参数正确传递。"""
        provider._get.return_value = "ACCESS_NUMBER:act_1:79001112233"
        await provider.request_number(0, "wa")
        call_kwargs = provider._get.call_args[1]
        assert call_kwargs.get("action") == "getNumber"
        assert call_kwargs.get("service") == "wa"
        assert call_kwargs.get("country") == 0


# ════════════════════════════════════════════════════════
# SmsBowerProvider — get_balance
# ════════════════════════════════════════════════════════

class TestGetBalance:
    """余额查询测试。"""

    @pytest.fixture
    def provider(self):
        async def get_key():
            return "test_key"
        p = SmsBowerProvider(get_key)
        p._get = AsyncMock()
        return p

    @pytest.mark.asyncio
    async def test_balance_ok(self, provider):
        """ACCESS_BALANCE:0.94 → 0.94。"""
        provider._get.return_value = "ACCESS_BALANCE:0.94"
        bal = await provider.get_balance()
        assert bal == 0.94

    @pytest.mark.asyncio
    async def test_balance_zero(self, provider):
        """ACCESS_BALANCE:0 → 0.0。"""
        provider._get.return_value = "ACCESS_BALANCE:0"
        bal = await provider.get_balance()
        assert bal == 0.0

    @pytest.mark.asyncio
    async def test_balance_failure(self, provider):
        """非 ACCESS_BALANCE 前缀 → SmsProviderError。"""
        provider._get.return_value = "ERROR:some_error"
        with pytest.raises(SmsProviderError, match="获取余额失败"):
            await provider.get_balance()


# ════════════════════════════════════════════════════════
# SmsBowerProvider — cancel / complete
# ════════════════════════════════════════════════════════

class TestCancelComplete:
    """取消和完成激活测试。"""

    @pytest.fixture
    def provider(self):
        async def get_key():
            return "test_key"
        p = SmsBowerProvider(get_key)
        p._get = AsyncMock(return_value="ACCESS_READY")
        return p

    @pytest.mark.asyncio
    async def test_cancel_calls_setStatus_status8(self, provider):
        """cancel_activation 调用 setStatus, status=8。"""
        await provider.cancel_activation("act_1")
        provider._get.assert_awaited_once_with(
            action="setStatus", id="act_1", status=8
        )

    @pytest.mark.asyncio
    async def test_complete_calls_setStatus_status6(self, provider):
        """complete_activation 调用 setStatus, status=6。"""
        await provider.complete_activation("act_1")
        provider._get.assert_awaited_once_with(
            action="setStatus", id="act_1", status=6
        )

    @pytest.mark.asyncio
    async def test_safe_cancel_swallows_exception(self, provider):
        """_safe_cancel 抛出异常时吞掉，不传播。"""
        provider._get.side_effect = SmsProviderError("network error")
        # 不应抛出
        result = await provider._safe_cancel("act_1")
        assert result == ""


# ════════════════════════════════════════════════════════
# SmsBowerProvider — poll_sms_code
# ════════════════════════════════════════════════════════

class TestPollSmsCode:
    """等码轮询测试。"""

    @pytest.fixture
    def provider(self):
        async def get_key():
            return "test_key"
        p = SmsBowerProvider(get_key)
        p.get_sms_code = AsyncMock()
        return p

    @pytest.mark.asyncio
    async def test_poll_success(self, provider):
        """第一次调用 get_sms_code 返回 OK → 直接返回验证码。"""
        provider.get_sms_code.return_value = ("OK", "123456")
        code = await provider.poll_sms_code("act_1", timeout=180)
        assert code == "123456"
        provider.get_sms_code.assert_awaited_once_with("act_1")

    @pytest.mark.asyncio
    async def test_poll_retry_then_ok(self, provider):
        """前几次 WAIT，最后 OK → 正确返回。"""
        provider.get_sms_code.side_effect = [
            ("WAIT", ""),
            ("WAIT", ""),
            ("OK", "987654"),
        ]
        code = await provider.poll_sms_code("act_1", timeout=180)
        assert code == "987654"
        assert provider.get_sms_code.await_count == 3

    @pytest.mark.asyncio
    async def test_poll_cancelled(self, provider):
        """CANCEL → SmsCancelledError。"""
        provider.get_sms_code.return_value = ("CANCEL", "")
        with pytest.raises(SmsCancelledError, match="已被 SMSBower 取消"):
            await provider.poll_sms_code("act_1", timeout=180)

    @pytest.mark.asyncio
    async def test_poll_expired(self, provider):
        """EXPIRED → SmsExpiredError。"""
        provider.get_sms_code.return_value = ("EXPIRED", "")
        with pytest.raises(SmsExpiredError, match="已过期/不存在"):
            await provider.poll_sms_code("act_1", timeout=180)

    @pytest.mark.asyncio
    async def test_poll_timeout(self, provider):
        """一直 WAIT 直到超时 → SmsTimeoutError。"""
        provider.get_sms_code.return_value = ("WAIT", "")
        with pytest.raises(SmsTimeoutError, match="等码超时"):
            await provider.poll_sms_code("act_1", timeout=1)  # 1秒超时

    @pytest.mark.asyncio
    async def test_poll_unknown_status_continues(self, provider):
        """UNKNOWN 状态 → 继续等待（不会退出）。"""
        provider.get_sms_code.side_effect = [
            ("UNKNOWN", "weird"),
            ("OK", "555666"),
        ]
        code = await provider.poll_sms_code("act_1", timeout=180)
        assert code == "555666"
        assert provider.get_sms_code.await_count == 2


# ════════════════════════════════════════════════════════
# SmsBowerProvider — 网络错误
# ════════════════════════════════════════════════════════

class TestNetworkErrors:
    """网络异常处理测试。"""

    @pytest.fixture
    def provider_with_real_get(self):
        """Provider 实例，但 httpx client 被 mock。api_key_getter 是 async。"""
        async def get_key():
            return "test_key"
        p = SmsBowerProvider(get_key)
        p._client = MagicMock()
        p._client.get = AsyncMock()
        p._retry_delays = (0, 0)
        return p

    @pytest.mark.asyncio
    async def test_timeout(self, provider_with_real_get):
        """httpx.TimeoutException → SmsProviderError。"""
        import httpx
        provider_with_real_get._client.get.side_effect = httpx.TimeoutException("timeout")
        with pytest.raises(SmsProviderError, match="请求超时"):
            await provider_with_real_get.get_balance()

    @pytest.mark.asyncio
    async def test_connect_error(self, provider_with_real_get):
        """httpx.ConnectError → SmsProviderError。"""
        import httpx
        provider_with_real_get._client.get.side_effect = httpx.ConnectError("connection refused")
        with pytest.raises(SmsProviderError, match="连接失败"):
            await provider_with_real_get.get_balance()
        assert provider_with_real_get._client.get.await_count == 3

    @pytest.mark.asyncio
    async def test_connect_error_retries_then_succeeds(self, provider_with_real_get):
        """瞬时连接失败会在适配层短重试。"""
        import httpx
        response = MagicMock()
        response.text = "ACCESS_BALANCE:4.92"
        response.status_code = 200
        response.is_success = True
        provider_with_real_get._client.get.side_effect = [
            httpx.ConnectError("temporary"),
            response,
        ]

        balance = await provider_with_real_get.get_balance()

        assert balance == 4.92
        assert provider_with_real_get._client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_get_number_connect_error_is_never_retried(self, provider_with_real_get):
        """取号不是幂等操作，响应丢失时必须停止而不是重复购买。"""
        import httpx
        provider_with_real_get._client.get.side_effect = httpx.ConnectError("connection lost")

        with pytest.raises(PurchaseUncertainError, match="停止自动重试"):
            await provider_with_real_get.request_number(31, "wa", price_max=0.1)

        assert provider_with_real_get._client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_response(self, provider_with_real_get):
        """空响应 → SmsProviderError。"""
        mock_resp = MagicMock()
        mock_resp.text = "   "
        provider_with_real_get._client.get.return_value = mock_resp
        with pytest.raises(SmsProviderError, match="空响应"):
            await provider_with_real_get.get_balance()


# ════════════════════════════════════════════════════════
# SmsBowerProvider — 附加功能
# ════════════════════════════════════════════════════════

class TestPrices:
    """价格响应解析。"""

    @pytest.fixture
    def provider(self):
        async def get_key():
            return "test_key"
        p = SmsBowerProvider(get_key)
        p._get = AsyncMock()
        return p

    @pytest.mark.asyncio
    async def test_get_prices_ignores_country_ids_and_large_inventory_keys(self, provider):
        provider._get.return_value = json.dumps({
            "52": {
                "wa": {
                    "0.1": {"count": 4},
                    "0.12": 7,
                    "2217": 30,
                    "3428": {"count": 2},
                }
            }
        })

        prices = await provider.get_prices(52, "wa")

        assert prices == [
            {"cost": 0.1, "count": 4},
            {"cost": 0.12, "count": 7},
        ]

    @pytest.mark.asyncio
    async def test_get_prices_parses_v3_provider_price_objects(self, provider):
        provider._get.return_value = json.dumps({
            "31": {
                "wa": {
                    "2217": {"count": 1, "price": 0.627, "provider_id": 2217},
                    "2260": {"count": 98, "price": 0.668, "provider_id": 2260},
                    "3160": {"count": 7148, "price": 0.334, "provider_id": 3160},
                }
            }
        })

        prices = await provider.get_prices(31, "wa")

        assert prices == [
            {"cost": 0.627, "count": 1, "provider_id": 2217},
            {"cost": 0.668, "count": 98, "provider_id": 2260},
            {"cost": 0.334, "count": 7148, "provider_id": 3160},
        ]

    @pytest.mark.asyncio
    async def test_get_prices_parses_cost_field_and_few_count(self, provider):
        provider._get.return_value = json.dumps({
            "31": {
                "wa": {
                    "2354": {"count": "few", "cost": 0.353, "id": 2354},
                }
            }
        })

        prices = await provider.get_prices(31, "wa")

        assert prices == [
            {"cost": 0.353, "count": 1, "provider_id": 2354},
        ]

    @pytest.mark.asyncio
    async def test_get_prices_keeps_explicit_price_above_legacy_cutoff(self, provider):
        provider._get.return_value = json.dumps({
            "31": {
                "wa": {
                    "9001": {"count": 2, "price": 6.5, "provider_id": 9001},
                    "9002": 3,
                }
            }
        })

        prices = await provider.get_prices(31, "wa")

        assert prices == [
            {"cost": 6.5, "count": 2, "provider_id": 9001},
        ]


class TestCountries:
    @pytest.fixture
    def provider(self):
        async def get_key():
            return "test_key"
        p = SmsBowerProvider(get_key)
        p._get = AsyncMock(return_value=json.dumps({
            "89": {"id": "89", "eng": "Tunisia", "chn": "突尼斯"},
            "91": {"id": "91", "eng": "Timor-Leste", "chn": "东帝汶"},
        }))
        return p

    @pytest.mark.asyncio
    async def test_get_countries_preserves_chinese_and_fills_iso_metadata(self, provider):
        countries = await provider.get_countries()

        assert countries == [
            {
                "id": 89,
                "name": "Tunisia",
                "name_zh": "突尼斯",
                "cc": "216",
                "iso": "tn",
                "aliases": ["tn", "89", "216"],
            },
            {
                "id": 91,
                "name": "Timor-Leste",
                "name_zh": "东帝汶",
                "cc": "670",
                "iso": "tl",
                "aliases": ["tl", "91", "670"],
            },
        ]


class TestDynamicCountryResolve:
    """动态国家列表解析。"""

    @pytest.fixture
    def provider(self):
        async def get_key():
            return "test_key"
        p = SmsBowerProvider(get_key)
        p.get_countries = AsyncMock(return_value=[
            {"id": 31, "name": "South Africa", "iso": "za", "cc": "27"},
            {"id": 187, "name": "United States", "iso": "us", "cc": "1"},
            {"id": 89, "name": "Tunisia", "name_zh": "突尼斯", "iso": "tn", "cc": "216"},
            {"id": 91, "name": "Timor-Leste", "name_zh": "东帝汶", "iso": "tl", "cc": "670"},
        ])
        return p

    @pytest.mark.asyncio
    async def test_resolve_dynamic_country_name(self, provider):
        assert await provider.resolve_country("South Africa") == (31, "South Africa")

    @pytest.mark.asyncio
    async def test_resolve_dynamic_numeric_label(self, provider):
        assert await provider.resolve_country("31") == (31, "South Africa")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value, expected", [
        ("突尼斯", (89, "Tunisia")),
        ("Tunisia", (89, "Tunisia")),
        ("tn", (89, "Tunisia")),
        ("+216", (89, "Tunisia")),
        ("东帝汶", (91, "Timor-Leste")),
        ("Timor Leste", (91, "Timor-Leste")),
        ("tl", (91, "Timor-Leste")),
    ])
    async def test_resolve_dynamic_country_aliases(self, provider, value, expected):
        assert await provider.resolve_country(value) == expected


class TestRequestAdditionalSms:
    """请求重新发送短信。"""

    @pytest.fixture
    def provider(self):
        async def get_key():
            return "test_key"
        p = SmsBowerProvider(get_key)
        p._get = AsyncMock()
        return p

    @pytest.mark.asyncio
    async def test_request_additional_sms(self, provider):
        """request_additional_sms → setStatus, status=3。"""
        await provider.request_additional_sms("act_1")
        provider._get.assert_awaited_once_with(
            action="setStatus", id="act_1", status=3
        )


class TestApiKeyValidation:
    """API Key 为空时的处理。"""

    @pytest.fixture
    def provider(self):
        async def empty_key():
            return ""
        return SmsBowerProvider(empty_key)

    @pytest.mark.asyncio
    async def test_empty_api_key_raises(self, provider):
        """API Key 为空 → SmsProviderError。"""
        with pytest.raises(SmsProviderError, match="API Key 未配置"):
            await provider._api_key()
