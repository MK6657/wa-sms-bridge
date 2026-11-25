"""Provider 注册表 — 显式注册，仅激活配置中启用的平台。"""

from __future__ import annotations

from typing import Any, Optional

from app.providers.base import BaseSmsProvider
from app.providers.catalog import PROVIDER_CATALOG


class ProviderRegistry:
    """SMS Provider 注册表。

    默认仅将 ACTIVE_PROVIDERS 中的平台初始化为活跃实例；
    已归档的旧平台代码仍保留在 PROVIDER_CATALOG 中，零运行时开销。
    """

    def __init__(self, default: str = "smsbower"):
        self._default = default
        self._providers: dict[str, type[BaseSmsProvider]] = {}
        self._instances: dict[str, BaseSmsProvider] = {}

    @property
    def default_provider(self) -> str:
        return self._default

    def register(self, name: str, cls: type[BaseSmsProvider]) -> None:
        self._providers[name] = cls

    def set_default(self, name: str) -> None:
        if name in self._providers or name in self._instances:
            self._default = name

    def is_active(self, name: str) -> bool:
        return (name or "").strip().lower() in self._instances

    def get(self, name: Optional[str] = None) -> BaseSmsProvider:
        """获取活跃 provider 实例。name=None 时返回全局默认。"""
        key = (name or self._default).strip().lower()
        if key not in self._instances:
            if key in PROVIDER_CATALOG:
                raise ValueError(f"SMS 平台 '{key}' 当前未启用（仅激活: {', '.join(self.list_providers()) or '无'}）")
            cls = self._providers.get(key)
            if not cls:
                raise ValueError(f"未知 provider: {key}")
            raise RuntimeError(
                f"Provider '{key}' 尚未初始化，请在 lifespan 中调用 init_provider()"
            )
        return self._instances[key]

    def init_provider(self, name: str, provider: BaseSmsProvider) -> None:
        """在 lifespan 中注入已初始化的活跃 provider 实例。"""
        key = name.strip().lower()
        provider.provider_id = key
        self._instances[key] = provider
        if key not in self._providers:
            self._providers[key] = type(provider)

    def all_instances(self) -> dict[str, BaseSmsProvider]:
        return dict(self._instances)

    def list_providers(self) -> list[str]:
        """返回所有已激活的 provider 名称（无实例时退回已注册列表以兼容旧测试）。"""
        if self._instances:
            return list(self._instances.keys())
        return list(self._providers.keys())

    def list_catalog(self, configured_map: Optional[dict[str, bool]] = None) -> list[dict[str, Any]]:
        """返回包含活跃与归档平台在内的完整目录视图。"""
        configured_map = configured_map or {}
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pid, desc in PROVIDER_CATALOG.items():
            seen.add(pid)
            items.append(desc.to_dict(active=pid in self._instances, configured=bool(configured_map.get(pid, False))))
        for pid in self._instances:
            if pid not in seen:
                items.append({
                    "id": pid,
                    "label": pid,
                    "tier": "custom",
                    "active": True,
                    "configured": bool(configured_map.get(pid, True)),
                    "default_base_url": "",
                    "capabilities": {
                        "supports_resend": getattr(self._instances[pid].capabilities, "supports_resend", False),
                        "supports_price_lock": True,
                        "supports_custom_service": True,
                    },
                    "note": "",
                })
        return items

    def reset(self) -> None:
        self._instances.clear()
        self._providers.clear()


# 全局单例
registry = ProviderRegistry()
