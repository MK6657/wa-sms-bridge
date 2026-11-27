"""可选业务集成插件基础协议与管理器。

核心接码服务不依赖任何下游业务插件；插件按配置显式启用并挂载。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

from fastapi import APIRouter

logger = logging.getLogger(__name__)


class IntegrationPlugin(ABC):
    plugin_id: str = ""
    display_name: str = ""
    description: str = ""

    def __init__(self, *, enabled: bool = False):
        self.enabled = bool(enabled)
        self._started = False

    @property
    def is_started(self) -> bool:
        return self._started

    @abstractmethod
    async def on_startup(self, *, db: Any, registry: Any) -> None:
        ...

    @abstractmethod
    async def on_shutdown(self) -> None:
        ...

    @abstractmethod
    async def health(self) -> dict[str, Any]:
        ...

    def get_router(self) -> Optional[APIRouter]:
        return None

    def descriptor(self) -> dict[str, Any]:
        return {
            "id": self.plugin_id,
            "name": self.display_name,
            "description": self.description,
            "enabled": self.enabled,
            "started": self._started,
        }


class PluginManager:
    def __init__(self):
        self._plugins: dict[str, IntegrationPlugin] = {}

    def register(self, plugin: IntegrationPlugin) -> None:
        self._plugins[plugin.plugin_id] = plugin

    def get(self, plugin_id: str) -> Optional[IntegrationPlugin]:
        return self._plugins.get(plugin_id)

    def list_plugins(self) -> list[dict[str, Any]]:
        return [p.descriptor() for p in self._plugins.values()]

    async def startup_all(self, *, db: Any, registry: Any) -> None:
        for plugin in self._plugins.values():
            if not plugin.enabled:
                logger.info("插件 '%s' 默认未启用，跳过初始化", plugin.plugin_id)
                continue
            try:
                await plugin.on_startup(db=db, registry=registry)
                plugin._started = True
                logger.info("插件 '%s' 已启动", plugin.plugin_id)
            except Exception as exc:
                logger.warning("插件 '%s' 启动失败（不影响核心接码服务）: %s", plugin.plugin_id, exc)

    async def shutdown_all(self) -> None:
        for plugin in self._plugins.values():
            if not plugin._started:
                continue
            try:
                await plugin.on_shutdown()
            except Exception:
                logger.warning("插件 '%s' 关闭异常", plugin.plugin_id, exc_info=True)
            finally:
                plugin._started = False

    async def health_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for pid, plugin in self._plugins.items():
            if not plugin.enabled:
                summary[pid] = {"enabled": False, "started": False, "status": "disabled"}
                continue
            try:
                raw_health = await asyncio.wait_for(plugin.health(), timeout=1.5)
                ok = bool(raw_health.get("ok"))
                summary[pid] = {
                    "enabled": True,
                    "started": plugin._started,
                    "ok": ok,
                    "status": "healthy" if ok else "unhealthy",
                }
            except Exception:
                summary[pid] = {
                    "enabled": True,
                    "started": plugin._started,
                    "ok": False,
                    "status": "unhealthy",
                }
        return summary
