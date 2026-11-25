"""Provider 能力查询：余额、国家列表、服务目录与实时价格档位。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.auth_service import AuthService, CallerContext
from app.core.pricing_service import PricingService
from app.db import Database
from app.dependencies import get_caller, get_db, get_pricing_service, get_registry
from app.errors import AppError, UpstreamError
from app.providers.catalog import PROVIDER_CATALOG
from app.providers.registry import ProviderRegistry
from app.service_codes import list_services

router = APIRouter(prefix="/api/v1/providers", tags=["providers"])


@router.get("")
async def list_providers(
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
    registry: ProviderRegistry = Depends(get_registry),
):
    AuthService.ensure_scope(caller, "prices:r")
    configured: dict[str, bool] = {}
    for pid, desc in PROVIDER_CATALOG.items():
        configured[pid] = bool(await db.config.get(desc.api_key_config_key, ""))
    return {
        "default_provider": registry.default_provider,
        "active": registry.list_providers(),
        "catalog": registry.list_catalog(configured_map=configured),
    }


@router.get("/{provider}/balance")
async def get_provider_balance(
    provider: str,
    caller: CallerContext = Depends(get_caller),
    registry: ProviderRegistry = Depends(get_registry),
):
    AuthService.ensure_scope(caller, "prices:r")
    try:
        prov = registry.get(provider)
    except ValueError as exc:
        raise AppError(str(exc), status_code=400, code="PROVIDER_NOT_ACTIVE") from exc
    try:
        balance = await prov.get_balance()
    except Exception as exc:
        raise UpstreamError(f"获取余额失败: {exc}", code="BALANCE_FETCH_FAILED") from exc
    return {"provider": prov.provider_id, "balance": round(float(balance), 4), "currency": "USD"}


@router.get("/{provider}/services")
async def get_provider_services(
    provider: str,
    caller: CallerContext = Depends(get_caller),
    registry: ProviderRegistry = Depends(get_registry),
):
    AuthService.ensure_scope(caller, "prices:r")
    try:
        prov = registry.get(provider)
    except ValueError as exc:
        raise AppError(str(exc), status_code=400, code="PROVIDER_NOT_ACTIVE") from exc
    return {
        "provider": prov.provider_id,
        "supports_custom_service": bool(getattr(prov.capabilities, "supports_custom_service", True)),
        "services": list_services(prov.provider_id),
    }


@router.get("/{provider}/countries")
async def get_provider_countries(
    provider: str,
    force: bool = Query(default=False, description="是否跳过缓存强制刷新国家列表"),
    caller: CallerContext = Depends(get_caller),
    registry: ProviderRegistry = Depends(get_registry),
):
    AuthService.ensure_scope(caller, "prices:r")
    try:
        prov = registry.get(provider)
    except ValueError as exc:
        raise AppError(str(exc), status_code=400, code="PROVIDER_NOT_ACTIVE") from exc
    try:
        if force and caller.is_admin and hasattr(prov, "invalidate_countries_cache"):
            prov.invalidate_countries_cache()
        countries = await prov.get_countries()
    except Exception as exc:
        raise UpstreamError(f"获取国家列表失败: {exc}", code="COUNTRIES_FETCH_FAILED") from exc
    return {"provider": prov.provider_id, "count": len(countries), "countries": countries}


@router.post("/{provider}/countries/refresh")
async def refresh_provider_countries(
    provider: str,
    caller: CallerContext = Depends(get_caller),
    registry: ProviderRegistry = Depends(get_registry),
):
    AuthService.ensure_scope(caller, "prices:r")
    try:
        prov = registry.get(provider)
    except ValueError as exc:
        raise AppError(str(exc), status_code=400, code="PROVIDER_NOT_ACTIVE") from exc
    try:
        if caller.is_admin and hasattr(prov, "invalidate_countries_cache"):
            prov.invalidate_countries_cache()
        countries = await prov.get_countries()
    except Exception as exc:
        raise UpstreamError(f"刷新国家列表失败: {exc}", code="COUNTRIES_REFRESH_FAILED") from exc
    return {"provider": prov.provider_id, "count": len(countries)}


@router.get("/{provider}/prices")
async def get_provider_prices(
    provider: str,
    country: str = Query(..., min_length=1, description="国家名/ISO/ID"),
    service: str = Query(default="whatsapp", description="服务名或原生服务码"),
    force: bool = Query(default=False, description="是否绕过 10s 缓存"),
    caller: CallerContext = Depends(get_caller),
    pricing_service: PricingService = Depends(get_pricing_service),
):
    AuthService.ensure_scope(caller, "prices:r")
    try:
        return await pricing_service.get_price_snapshot(provider, country, service, force=bool(force and caller.is_admin))
    except ValueError as exc:
        raise AppError(str(exc), status_code=400, code="INVALID_PRICE_QUERY") from exc
    except Exception as exc:
        raise UpstreamError(f"获取价格失败: {exc}", code="PRICE_FETCH_FAILED") from exc
