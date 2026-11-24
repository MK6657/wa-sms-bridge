"""Provider 注册表 — 显式注册，非动态加载。"""

from typing import Optional

from app.providers.base import BaseSmsProvider
from app.providers.smsbower import SmsBowerProvider


class ProviderRegistry:
    """SMS Provider 注册表。

    使用方式:
        registry = ProviderRegistry()
        registry.register("smsbower", SmsBowerProvider)
        # 后续: registry.register("5sim", FiveSimProvider)
        #       registry.register("herosms", HeroSmsProvider)

        provider = registry.get("5sim")       # 按名称获取
        provider = registry.get()             # 获取全局默认
    """

    def __init__(self, default: str = "smsbower"):
        self._default = default
        self._providers: dict[str, type[BaseSmsProvider]] = {}
        self._instances: dict[str, BaseSmsProvider] = {}

    def register(self, name: str, cls: type[BaseSmsProvider]):
        """注册一个 provider 类型。"""
        self._providers[name] = cls

    def set_default(self, name: str):
        """设置全局默认 provider。"""
        if name in self._providers or name in self._instances:
            self._default = name

    def get(self, name: Optional[str] = None) -> BaseSmsProvider:
        """获取 provider 实例。name=None 时返回全局默认。"""
        key = name or self._default
        if key not in self._instances:
            cls = self._providers.get(key)
            if not cls:
                raise ValueError(f"未知 provider: {key}")
            # 实例化（config_loader 由 main.py 在 lifespan 中注入）
            raise RuntimeError(
                f"Provider '{key}' 尚未初始化，请在 lifespan 中调用 init_provider()")
        return self._instances[key]

    def init_provider(self, name: str, provider: BaseSmsProvider):
        """在 lifespan 中注入已初始化的 provider 实例。"""
        provider.provider_id = name
        self._instances[name] = provider
        if name not in self._providers:
            self._providers[name] = type(provider)

    def all_instances(self) -> dict[str, BaseSmsProvider]:
        """返回所有已初始化的 provider 实例（用于 shutdown 清理）。"""
        return dict(self._instances)

    def list_providers(self) -> list[str]:
        """返回所有已注册的 provider 名称。"""
        return list(self._providers.keys())


# 全局单例
registry = ProviderRegistry()
