"""系统设置、SMSBower 配置、验证码留存策略与脱敏日志诊断（严格管理员鉴权 + 上游 URL 校验）。"""

from __future__ import annotations

import logging
import os
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from app.api.schemas import SettingsUpdateRequest
from app.config import env
from app.core.auth_service import AuthService, CallerContext
from app.core.pricing_service import PricingService
from app.core.retention_service import RetentionService
from app.core.url_guard import validate_upstream_base_url
from app.db import Database
from app.dependencies import (
    AppContainer,
    get_auth_service,
    get_caller,
    get_container,
    get_db,
    get_plugin_manager,
    get_pricing_service,
    get_registry,
)
from app.errors import AppError
from app.log_redaction import redact_text
from app.plugins.base import PluginManager
from app.providers.hero_sms import DEFAULT_BASE_URL as HERO_SMS_DEFAULT_BASE_URL
from app.providers.registry import ProviderRegistry
from app.providers.smsbower import SmsBowerProvider

router = APIRouter(prefix="/api/v1", tags=["settings"])


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "****"
    return key[:4] + "****" + key[-4:]


@router.get("/settings")
async def get_settings(
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
    auth_service: AuthService = Depends(get_auth_service),
    registry: ProviderRegistry = Depends(get_registry),
    plugin_manager: PluginManager = Depends(get_plugin_manager),
):
    AuthService.ensure_admin(caller, "仅管理员可读取系统配置")
    sb_key = await db.config.get("smsbower_api_key", "")
    sb_url = await db.config.get("smsbower_base_url", "")
    default_country_str = await db.config.get("smsbower_default_country", str(env.smsbower_default_country))
    try:
        default_country = int(default_country_str)
    except ValueError:
        default_country = env.smsbower_default_country

    five_key = await db.config.get("five_sim_api_key", "")
    hero_key = await db.config.get("hero_sms_api_key", "")
    hero_url = await db.config.get("hero_sms_base_url", "")

    retention_raw = await db.config.get("sms_code_retention_hours", "")
    try:
        retention_hours = int(retention_raw) if retention_raw != "" else env.sms_code_retention_hours
    except ValueError:
        retention_hours = env.sms_code_retention_hours

    mask_raw = await db.config.get("sms_code_mask_in_history", "")
    mask_in_history = (
        mask_raw.strip().lower() in {"1", "true", "yes", "on"}
        if mask_raw != ""
        else env.sms_code_mask_in_history
    )

    configured = {
        "smsbower": bool(sb_key),
        "5sim": bool(five_key),
        "herosms": bool(hero_key),
    }

    return {
        "active_providers": registry.list_providers(),
        "default_provider": registry.default_provider,
        "provider_catalog": registry.list_catalog(configured_map=configured),
        "smsbower_base_url": sb_url or SmsBowerProvider.BASE_URL,
        "smsbower_default_country": default_country,
        "smsbower_api_key_masked": _mask_key(sb_key),
        "five_sim_api_key_masked": _mask_key(five_key),
        "hero_sms_api_key_masked": _mask_key(hero_key),
        "hero_sms_base_url": hero_url or HERO_SMS_DEFAULT_BASE_URL,
        "api_auth_required": await auth_service.is_auth_required(),
        "admin_password_configured": auth_service.password_configured(),
        "sms_code_retention_hours": retention_hours,
        "sms_code_mask_in_history": mask_in_history,
        "encryption_active": env.has_secret_key,
        "plugins": plugin_manager.list_plugins(),
    }


@router.put("/settings")
async def update_settings(
    req: SettingsUpdateRequest,
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
    auth_service: AuthService = Depends(get_auth_service),
    registry: ProviderRegistry = Depends(get_registry),
    pricing_service: PricingService = Depends(get_pricing_service),
):
    AuthService.ensure_admin(caller, "仅管理员可修改系统配置")
    if req.smsbower_base_url is not None and req.smsbower_base_url.strip():
        validate_upstream_base_url("smsbower", req.smsbower_base_url)
    if req.hero_sms_base_url is not None and req.hero_sms_base_url.strip():
        validate_upstream_base_url("herosms", req.hero_sms_base_url)

    try:
        if req.smsbower_api_key is not None:
            await db.config.set("smsbower_api_key", req.smsbower_api_key.strip(), encrypt=True)
        if req.smsbower_base_url is not None:
            await db.config.set("smsbower_base_url", req.smsbower_base_url.strip().rstrip("/"))
        if req.smsbower_default_country is not None:
            await db.config.set("smsbower_default_country", str(req.smsbower_default_country))
        if req.five_sim_api_key is not None:
            await db.config.set("five_sim_api_key", req.five_sim_api_key.strip(), encrypt=True)
        if req.hero_sms_api_key is not None:
            await db.config.set("hero_sms_api_key", req.hero_sms_api_key.strip(), encrypt=True)
        if req.hero_sms_base_url is not None:
            await db.config.set("hero_sms_base_url", req.hero_sms_base_url.strip().rstrip("/"))
        if req.api_auth_required is not None:
            await auth_service.set_auth_required(req.api_auth_required)
        if req.sms_code_retention_hours is not None:
            await db.config.set("sms_code_retention_hours", str(req.sms_code_retention_hours))
        if req.sms_code_mask_in_history is not None:
            await db.config.set("sms_code_mask_in_history", "true" if req.sms_code_mask_in_history else "false")
    except RuntimeError as exc:
        raise AppError(str(exc), status_code=500, code="SECRET_KEY_REQUIRED") from exc

    if any(
        v is not None
        for v in (
            req.smsbower_api_key,
            req.smsbower_base_url,
            req.five_sim_api_key,
            req.hero_sms_api_key,
            req.hero_sms_base_url,
        )
    ):
        from app.country_index import index as global_country_index

        pricing_service.invalidate()
        global_country_index.invalidate()
        for pid in registry.list_providers():
            prov = registry.get(pid)
            if hasattr(prov, "invalidate_countries_cache"):
                prov.invalidate_countries_cache()

    return {"success": True}


@router.post("/settings/scrub-codes")
async def scrub_historical_codes(
    caller: CallerContext = Depends(get_caller),
    container: AppContainer = Depends(get_container),
):
    AuthService.ensure_admin(caller, "仅管理员可执行批量验证码清理")
    retention: RetentionService = container.retention_service
    scrubbed = await retention.run_scrub_once(retention_hours=0)
    return {"success": True, "scrubbed_orders": scrubbed}


@router.get("/logs/recent")
async def get_recent_logs(
    lines: int = Query(default=200, ge=1, le=1000),
    level: str = Query(default="", description="DEBUG / INFO / WARNING / ERROR / CRITICAL"),
    caller: CallerContext = Depends(get_caller),
):
    AuthService.ensure_admin(caller, "仅管理员可查看诊断日志")
    lvl = (level or "").strip().upper()
    allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if lvl and lvl not in allowed:
        raise AppError(f"level 必须为: {', '.join(sorted(allowed))}", status_code=422, code="VALIDATION_ERROR")

    from app.main import flush_file_logging, get_current_log_path

    # 先将 MemoryHandler 缓冲刷入磁盘（RotatingFileHandler 启用了 delay=True，未 flush 前文件可能尚未创建！）
    flush_file_logging()
    env_log = os.environ.get("BRIDGE_LOG_FILE")
    log_path = Path(env_log) if env_log else (get_current_log_path() or (Path(env.bridge_data_dir) / "bridge.log"))
    if not log_path.exists():
        return {"path": str(log_path), "level": lvl or "ALL", "counts": {}, "entries": []}
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        entries = [line.rstrip("\n") for line in deque(handle, maxlen=lines * 5 if lvl else lines)]
    if lvl:
        marker = f"[{lvl}]"
        entries = [line for line in entries if marker in line][-lines:]
    entries = [redact_text(line) for line in entries]
    counts = {name: sum(f"[{name}]" in line for line in entries) for name in allowed}
    return {"path": str(log_path), "level": lvl or "ALL", "counts": counts, "entries": entries}
