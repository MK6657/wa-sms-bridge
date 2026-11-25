"""汇总 API v1 全部路由。"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.blacklist import router as blacklist_router
from app.api.v1.events import router as events_router
from app.api.v1.health import router as health_router
from app.api.v1.orders import router as orders_router
from app.api.v1.projects import router as projects_router
from app.api.v1.providers import router as providers_router
from app.api.v1.settings import router as settings_router
from app.api.v1.stats import router as stats_router

v1_router = APIRouter()
v1_router.include_router(auth_router)
v1_router.include_router(health_router)
v1_router.include_router(providers_router)
v1_router.include_router(orders_router)
v1_router.include_router(events_router)
v1_router.include_router(blacklist_router)
v1_router.include_router(projects_router)
v1_router.include_router(settings_router)
v1_router.include_router(stats_router)

__all__ = ["v1_router"]
