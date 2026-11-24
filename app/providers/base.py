"""SMS Provider 抽象基类。

所有 SMS 接码平台必须实现 BaseSmsProvider。
"""

from abc import ABC, abstractmethod
from typing import Any, Optional

from app.models import PhoneInput
from app.log_redaction import mask_identifier, mask_phone


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
    """取号请求可能已被上游处理，但 bridge 未拿到订单句柄。"""


# ── 公共 Activation ─────────────────────────────────────


class Activation:
    """一次取号激活的封装。"""

    def __init__(self, activation_id: str, phone_number: str,
                 country_id: Any, country_label: str,
                 provider: str = "", price: Optional[float] = None,
                 price_is_estimated: bool = False, currency: str = "USD"):
        self.activation_id = activation_id
        self.phone_number = phone_number
        self.country_id = country_id
        self.country_label = country_label
        self.provider = provider
        self.price: Optional[float] = price
        self.price_is_estimated = bool(price_is_estimated)
        self.currency = currency or "USD"

    def __repr__(self):
        return f"<Activation {mask_identifier(self.activation_id)} phone={mask_phone(self.phone_number)}>"


class BaseSmsProvider(ABC):
    """所有 SMS 接码平台必须实现的抽象基类。"""

    provider_id: str = ""

    async def resolve_country(self, country: str) -> tuple[Any, str]:
        """解析国家字符串为 (country_id, country_label)。

        默认实现尝试用 SMSBower 的国家映射。
        各 provider 可重写用自己的国家解析逻辑。
        """
        from app.providers.smsbower import resolve_country_id as smsbower_resolve
        try:
            return smsbower_resolve(country)
        except ValueError:
            s = country.strip().lower()
            return s, s

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
    async def request_number(self, country_id: Any, service: str = "whatsapp",
                              price_max: Optional[float] = None) -> Activation:
        ...

    @abstractmethod
    async def get_sms_code(self, activation_id: str) -> tuple[str, str]:
        """返回 (status, code_or_empty)。status: OK / WAIT / CANCEL / EXPIRED"""
        ...

    @abstractmethod
    async def cancel_activation(self, activation_id: str) -> str:
        """取消激活（平台按各自规则处理取消时机）。"""
        ...

    @abstractmethod
    async def complete_activation(self, activation_id: str) -> str:
        ...

    @abstractmethod
    async def poll_sms_code(self, activation_id: str, timeout: int = 180) -> str:
        """阶梯轮询等码，返回纯数字验证码。"""
        ...

    async def normalize_phone(self, raw_phone: str, country_id: Any) -> PhoneInput:
        """将平台原始号码格式转为 wa-app PhoneInput。

        默认实现用 phonenumbers 库处理。子类可重写。
        """
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
        """Validate metadata required by the WA registration path before buying."""
        return None

    async def _safe_cancel(self, activation_id: str):
        """安全取消激活（吞异常）。"""
        try:
            return await self.cancel_activation(activation_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "safe cancel activation %s failed: %s", activation_id, e)
            return ""
