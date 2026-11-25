"""FastAPI 标准依赖注入容器与 OpenAPI 安全方案声明（修复 P2-05）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

if TYPE_CHECKING:
    from app.core.auth_service import AuthService, CallerContext
    from app.core.event_bus import EventBus
    from app.core.order_service import OrderService
    from app.core.pricing_service import PricingService
    from app.core.retention_service import RetentionService
    from app.core.webhook_dispatcher import WebhookDispatcher
    from app.db import Database
    from app.plugins.base import PluginManager
    from app.providers.registry import ProviderRegistry

api_key_header_scheme = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="外部项目 API Key（如 sms_k_...）或管理员令牌",
)
bearer_auth_scheme = HTTPBearer(
    auto_error=False,
    description="Bearer 方式传入项目 API Key 或管理员令牌",
)


@dataclass
class AppContainer:
    """应用级依赖集合，由 lifespan 注入至 app.state.container。"""

    db: "Database"
    registry: "ProviderRegistry"
    event_bus: "EventBus"
    pricing_service: "PricingService"
    order_service: "OrderService"
    auth_service: "AuthService"
    webhook_dispatcher: "WebhookDispatcher"
    retention_service: "RetentionService"
    plugin_manager: "PluginManager"
    extras: dict[str, Any] = field(default_factory=dict)


def get_container(request: Request) -> AppContainer:
    container: Optional[AppContainer] = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="服务尚未初始化")
    return container


def get_db(container: AppContainer = Depends(get_container)) -> "Database":
    return container.db


def get_registry(container: AppContainer = Depends(get_container)) -> "ProviderRegistry":
    return container.registry


def get_order_service(container: AppContainer = Depends(get_container)) -> "OrderService":
    return container.order_service


def get_pricing_service(container: AppContainer = Depends(get_container)) -> "PricingService":
    return container.pricing_service


def get_event_bus(container: AppContainer = Depends(get_container)) -> "EventBus":
    return container.event_bus


def get_auth_service(container: AppContainer = Depends(get_container)) -> "AuthService":
    return container.auth_service


def get_webhook_dispatcher(container: AppContainer = Depends(get_container)) -> "WebhookDispatcher":
    return container.webhook_dispatcher


def get_plugin_manager(container: AppContainer = Depends(get_container)) -> "PluginManager":
    return container.plugin_manager


async def get_caller(
    request: Request,
    _api_key: Optional[str] = Depends(api_key_header_scheme),
    _bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_auth_scheme),
    auth_service: "AuthService" = Depends(get_auth_service),
) -> "CallerContext":
    return await auth_service.authenticate_request(request)


async def require_admin_caller(
    caller: "CallerContext" = Depends(get_caller),
) -> "CallerContext":
    from app.core.auth_service import AuthService
    AuthService.ensure_admin(caller)
    return caller
