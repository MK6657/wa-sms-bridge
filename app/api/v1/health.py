"""健康检查与存活/就绪探针（修复 P2-01 默认国家透出 & P2-03 DB 不可用返回 503）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.config import APP_VERSION, env
from app.db import Database
from app.dependencies import get_db, get_plugin_manager, get_registry
from app.plugins.base import PluginManager
from app.providers.registry import ProviderRegistry

router = APIRouter(tags=["health"])


@router.get("/api/v1/health/live")
async def api_v1_liveness():
    """进程存活探针。"""
    return {"ok": True, "status": "alive"}


async def _build_readiness_response(
    db: Database,
    registry: ProviderRegistry,
    plugin_manager: PluginManager,
) -> JSONResponse:
    db_ok = await db.health()
    active_providers = registry.list_providers()
    default_provider = registry.default_provider
    provider_configured: dict[str, bool] = {}
    default_country = env.smsbower_default_country

    if db_ok:
        try:
            provider_configured["smsbower"] = bool(await db.config.get("smsbower_api_key", ""))
            provider_configured["5sim"] = bool(await db.config.get("five_sim_api_key", ""))
            provider_configured["herosms"] = bool(await db.config.get("hero_sms_api_key", ""))
            raw_dc = await db.config.get("smsbower_default_country", "")
            if raw_dc != "":
                default_country = int(raw_dc)
        except Exception:
            pass
    else:
        for pid in ("smsbower", "5sim", "herosms"):
            provider_configured[pid] = False

    plugins = await plugin_manager.health_summary()
    payload = {
        "ok": db_ok,
        "service": "sms-hub",
        "version": APP_VERSION,
        "db": db_ok,
        "default_provider": default_provider,
        "default_country": default_country,
        "active_providers": active_providers,
        "provider_configured": provider_configured,
        "plugins": plugins,
    }
    return JSONResponse(status_code=200 if db_ok else 503, content=payload)


@router.get("/api/v1/health")
async def api_v1_health(
    db: Database = Depends(get_db),
    registry: ProviderRegistry = Depends(get_registry),
    plugin_manager: PluginManager = Depends(get_plugin_manager),
):
    return await _build_readiness_response(db, registry, plugin_manager)


@router.get("/api/health", include_in_schema=False)
async def api_legacy_health(
    db: Database = Depends(get_db),
    registry: ProviderRegistry = Depends(get_registry),
    plugin_manager: PluginManager = Depends(get_plugin_manager),
):
    """兼容旧版 Docker healthcheck 路径，DB 不可用时同样返回 503。"""
    return await _build_readiness_response(db, registry, plugin_manager)
