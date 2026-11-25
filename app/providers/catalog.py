"""SMS 接码平台目录元数据。

当前以 SMSBower 为主活平台；5sim 与 HeroSMS 代码完整保留并默认归档禁用，
日后仅需在环境变量 ACTIVE_PROVIDERS 中加入即可重新启用测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderDescriptor:
    id: str
    label: str
    tier: str  # "primary" | "legacy"
    api_key_config_key: str
    base_url_config_key: str = ""
    default_base_url: str = ""
    supports_resend: bool = False
    supports_price_lock: bool = True
    supports_custom_service: bool = True
    note: str = ""

    def to_dict(self, *, active: bool, configured: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "tier": self.tier,
            "active": active,
            "configured": configured,
            "default_base_url": self.default_base_url,
            "capabilities": {
                "supports_resend": self.supports_resend,
                "supports_price_lock": self.supports_price_lock,
                "supports_custom_service": self.supports_custom_service,
            },
            "note": self.note,
        }


PROVIDER_CATALOG: dict[str, ProviderDescriptor] = {
    "smsbower": ProviderDescriptor(
        id="smsbower",
        label="SMSBower",
        tier="primary",
        api_key_config_key="smsbower_api_key",
        base_url_config_key="smsbower_base_url",
        default_base_url="https://smsbower.page/stubs/handler_api.php",
        supports_resend=True,
        supports_price_lock=True,
        supports_custom_service=True,
        note="当前主用平台，支持价格档位查询、锁价、重发短信与即时退款。",
    ),
    "5sim": ProviderDescriptor(
        id="5sim",
        label="5sim",
        tier="legacy",
        api_key_config_key="five_sim_api_key",
        default_base_url="https://5sim.net/v1",
        supports_resend=False,
        supports_price_lock=True,
        supports_custom_service=True,
        note="已保留适配代码，默认未启用；如需日后测试请在 ACTIVE_PROVIDERS 中加入 5sim。",
    ),
    "herosms": ProviderDescriptor(
        id="herosms",
        label="HeroSMS",
        tier="legacy",
        api_key_config_key="hero_sms_api_key",
        base_url_config_key="hero_sms_base_url",
        default_base_url="https://hero-sms.com/stubs/handler_api.php",
        supports_resend=False,
        supports_price_lock=True,
        supports_custom_service=True,
        note="已保留适配代码，默认未启用；如需日后测试请在 ACTIVE_PROVIDERS 中加入 herosms。",
    ),
}


def get_provider_descriptor(provider_id: str) -> ProviderDescriptor | None:
    return PROVIDER_CATALOG.get((provider_id or "").strip().lower())
