"""HTTP API 路由。"""

import logging
import asyncio
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from app.config import env
from app.models import RegisterRequest, ConfigUpdate, ConfigResponse, ProviderBalance
from app.providers.smsbower import SmsBowerProvider
from app.providers.hero_sms import DEFAULT_BASE_URL as HERO_SMS_DEFAULT_BASE_URL
from app.log_redaction import redact_text
from app.price_policy import effective_price_max, locked_price_available
from app.service_codes import (
    canonical_service as _canonical_service,
    provider_service_code as _provider_service_code,
)

logger = logging.getLogger(__name__)

router = APIRouter()


RECOMMEND_COUNTRIES = [
    "Thailand",
    "Indonesia",
    "South Africa",
    "Russia",
    "India",
    "Philippines",
    "Vietnam",
    "United States",
]


def _init(orchestrator, registry, waapi, db):
    """由 main.py 在 lifespan 中注入依赖。"""
    router._orch = orchestrator
    router._registry = registry
    router._waapi = waapi
    router._db = db


def _price_value(entry: dict[str, Any]) -> Optional[float]:
    for key in ("cost", "price", "Price"):
        value = entry.get(key)
        if value is None:
            continue
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price >= 0:
            return round(price, 4)
    return None


def _price_count(entry: dict[str, Any]) -> int:
    for key in ("count", "qty", "Qty", "quantity"):
        value = entry.get(key)
        if value is None:
            continue
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            continue
    return 0


def _summarize_prices(entries: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[float, int] = {}
    normalized = []
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        cost = _price_value(item)
        if cost is None:
            continue
        count = _price_count(item)
        buckets[cost] = buckets.get(cost, 0) + count
        normalized.append({"cost": cost, "count": count, "in_stock": count > 0})

    prices = sorted(buckets)
    available_prices = [cost for cost in prices if buckets[cost] > 0]
    total_stock = sum(buckets.values())
    middle = available_prices[len(available_prices) // 2] if available_prices else None
    return {
        "entries": [{"cost": cost, "count": buckets[cost], "in_stock": buckets[cost] > 0} for cost in prices],
        "low": available_prices[0] if available_prices else None,
        "middle": middle,
        "high": available_prices[-1] if available_prices else None,
        "stock": total_stock,
        "available": total_stock > 0,
    }


async def _provider_price_snapshot(registry, provider_name: str, country: str, service: str) -> dict[str, Any]:
    provider = registry.get(provider_name)
    cid, label = await provider.resolve_country(country)
    service_code = _provider_service_code(provider_name, service)
    prices = await provider.get_prices(cid, service_code)
    summary = _summarize_prices(prices)
    return {
        "provider": provider_name,
        "country": str(cid),
        "country_label": label,
        "service": _canonical_service(service),
        "service_code": service_code,
        **summary,
    }


# ── 健康检查 ────────────────────────────────────────────


@router.get("/api/health")
async def api_health(wa_timeout: float = 2.5):
    """Bridge 自身健康检查，并尽力探测 WA 上游能力。"""
    reg = getattr(router, "_registry", None)
    db = getattr(router, "_db", None)
    waapi = getattr(router, "_waapi", None)
    db_ok = bool(db) and await db.health()
    result = {
        "ok": db_ok,
        "bridge": {"db": db_ok, "providers": reg.list_providers() if reg else []},
        "wa_app": {"ok": False, "health": {}},
    }
    if waapi:
        try:
            health = await asyncio.wait_for(waapi.health(), timeout=max(0.5, wa_timeout))
            wa_ok = bool(health.get("ok", True)) if isinstance(health, dict) else True
            result["wa_app"] = {"ok": wa_ok, "health": health}
            result["ok"] = bool(result["ok"] and wa_ok)
        except asyncio.TimeoutError:
            result["ok"] = False
            result["wa_app"] = {"ok": False, "error": f"wa-app 健康检查超时 ({wa_timeout:.1f}s)"}
        except Exception as e:
            result["ok"] = False
            result["wa_app"] = {"ok": False, "error": str(e)}
    return result


# ── 注册控制 ────────────────────────────────────────────


@router.post("/api/register")
async def api_register(req: RegisterRequest):
    orch = getattr(router, "_orch", None)
    if not orch:
        raise HTTPException(503, "桥接器尚未初始化")
    if _canonical_service(req.service) != "whatsapp":
        raise HTTPException(
            400,
            "自动注册链路仅支持 WhatsApp；Google/Telegram 仅可用于价格查询",
        )

    task_id = f"task_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    result = await orch.start_registration(
        task_id=task_id,
        country=req.country or "0",
        country_fallback=req.country_fallback,
        max_attempts=req.max_attempts,
        price_max=req.price_max,
        wait_timeout=req.wait_timeout,
        provider_name=req.provider or "smsbower",
        service=req.service or "whatsapp",
        target_price=req.target_price,
        price_lock_enabled=req.price_lock_enabled,
        price_float_enabled=req.price_float_enabled,
        price_float_percent=req.price_float_percent,
        continuous_poll=req.continuous_poll,
        target_success_count=req.target_success_count,
        request_interval_enabled=req.request_interval_enabled,
        request_interval=req.request_interval,
        proxy=req.proxy or "",
    )
    return result


@router.get("/api/task/{task_id}")
async def api_get_task(task_id: str):
    db = getattr(router, "_db", None)
    if not db:
        raise HTTPException(503, "数据库未初始化")
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    return task


@router.post("/api/task/{task_id}/stop")
async def api_stop_task(task_id: str):
    orch = getattr(router, "_orch", None)
    if not orch:
        raise HTTPException(503, "桥接器未初始化")
    task = await orch.stop_task(task_id)
    if not task:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    return task


@router.get("/api/tasks")
async def api_list_tasks():
    orch = getattr(router, "_orch", None)
    if not orch:
        raise HTTPException(503, "桥接器未初始化")
    return await orch.list_tasks()


# ── Provider 管理 ────────────────────────────────────────


@router.get("/api/providers")
async def api_providers():
    """返回所有已注册的 provider 名称列表。"""
    reg = getattr(router, "_registry", None)
    if reg is not None:
        return reg.list_providers()
    from app.providers.registry import registry
    return registry.list_providers()


@router.get("/api/provider/balance")
async def api_provider_balance(provider: str = ""):
    reg = getattr(router, "_registry", None)
    if not reg:
        raise HTTPException(503, "Registry 未初始化")
    try:
        prov = reg.get(provider or None)
        bal = await prov.get_balance()
        return ProviderBalance(balance=bal).model_dump()
    except Exception as e:
        raise HTTPException(502, f"获取余额失败: {e}")


@router.get("/api/provider/countries")
async def api_provider_countries(provider: str = ""):
    """获取国家列表。provider 为空时返回所有平台合并的索引。"""
    from app.country_index import index
    try:
        await index.refresh()
        return index.get_countries(provider=provider)
    except Exception as e:
        raise HTTPException(502, f"获取国家列表失败: {e}")


@router.get("/api/countries/search")
async def api_countries_search(q: str = ""):
    """搜索国家。"""
    if not q:
        return []
    from app.country_index import index
    await index.refresh()
    return index.search(q)


@router.post("/api/countries/refresh")
async def api_countries_refresh():
    """强制刷新国家索引。"""
    from app.country_index import index
    await index.refresh(force=True)
    return {"count": len(index._cache)}


@router.post("/api/provider/test-number")
async def api_test_number(data: dict | None = Body(default=None),
                          provider: str = "", country: str = "0",
                          service: str = "whatsapp",
                          target_price: Optional[float] = None,
                          price_lock_enabled: bool = False,
                          price_float_enabled: bool = False,
                          price_float_percent: float = 0,
                          price_max: Optional[float] = None):
    reg = getattr(router, "_registry", None)
    if not reg:
        raise HTTPException(503, "Registry 未初始化")
    if data:
        provider = data.get("provider", provider)
        country = str(data.get("country", country))
        service = data.get("service", service)
        target_price = data.get("target_price", target_price)
        price_lock_enabled = bool(data.get("price_lock_enabled", price_lock_enabled))
        price_float_enabled = bool(data.get("price_float_enabled", price_float_enabled))
        price_float_percent = data.get("price_float_percent", price_float_percent)
        price_max = data.get("price_max", price_max)
    try:
        provider_name = provider or None
        prov = reg.get(provider_name)
        resolved_country, _ = await prov.resolve_country(country)
        service_code = _provider_service_code(provider or getattr(prov, "provider_id", ""), service)
        max_price = effective_price_max(
            target_price=target_price,
            price_max=price_max,
            price_lock_enabled=price_lock_enabled,
            price_float_enabled=price_float_enabled,
            price_float_percent=price_float_percent,
        )
        if price_lock_enabled and target_price is not None:
            entries = await prov.get_prices(resolved_country, service_code)
            available, stock = locked_price_available(entries, target_price)
            if not available:
                return {"success": False, "error": f"锁定价格 {target_price} 暂无库存", "stock": stock}
        act = await prov.request_number(resolved_country, service_code, price_max=max_price)
        await prov._safe_cancel(act.activation_id)
        return {"success": True, "phone": act.phone_number, "activation_id": act.activation_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/provider/prices")
async def api_provider_prices(provider: str = "smsbower", country: str = "", service: str = "whatsapp"):
    """查询单个平台某国家/服务的价格档位。"""
    reg = getattr(router, "_registry", None)
    if not reg:
        raise HTTPException(503, "Registry 未初始化")
    if not country or not country.strip():
        raise HTTPException(422, "country is required")
    try:
        return await _provider_price_snapshot(reg, provider, country, service)
    except Exception as e:
        raise HTTPException(502, f"获取价格失败: {e}")


@router.get("/api/prices/recommendations")
async def api_price_recommendations(service: str = "whatsapp", country: str = "", countries: str = ""):
    """跨平台价格推荐。

    country 传入时按一个国家横向比较所有平台；否则用常用国家样本生成价带。
    单个平台/国家失败会作为 entry error 返回，不中断整体响应。
    """
    reg = getattr(router, "_registry", None)
    if not reg:
        raise HTTPException(503, "Registry 未初始化")

    providers = reg.list_providers()
    targets = list(dict.fromkeys(c.strip() for c in countries.split(",") if c.strip()))[:20]
    if country.strip():
        targets = [country.strip()]
    if not targets:
        targets = RECOMMEND_COUNTRIES

    semaphore = asyncio.Semaphore(4)

    async def fetch_entry(target: str, provider_name: str) -> dict[str, Any]:
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    _provider_price_snapshot(reg, provider_name, target, service),
                    timeout=12,
                )
            except asyncio.TimeoutError:
                error = "价格查询超时"
            except Exception as e:
                error = str(e)
            return {
                "provider": provider_name,
                "country": target,
                "country_label": target,
                "service": _canonical_service(service),
                "entries": [],
                "low": None,
                "middle": None,
                "high": None,
                "stock": 0,
                "available": False,
                "error": error,
            }

    entries = await asyncio.gather(*(
        fetch_entry(target, provider_name)
        for target in targets
        for provider_name in providers
    ))

    bands: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("error") or entry.get("low") is None:
            continue
        key = (entry.get("country_label") or entry.get("country") or "").strip()
        band = bands.setdefault(key, {
            "country_label": key,
            "providers": set(),
            "prices": [],
            "stock": 0,
        })
        band["providers"].add(entry["provider"])
        band["stock"] += int(entry.get("stock") or 0)
        for price_key in ("low", "middle", "high"):
            price = entry.get(price_key)
            if price is not None:
                band["prices"].append(float(price))

    country_bands = []
    for band in bands.values():
        prices = sorted(band["prices"])
        country_bands.append({
            "country_label": band["country_label"],
            "provider_count": len(band["providers"]),
            "providers": sorted(band["providers"]),
            "low": prices[0] if prices else None,
            "middle": prices[len(prices) // 2] if prices else None,
            "high": prices[-1] if prices else None,
            "stock": band["stock"],
        })
    country_bands.sort(key=lambda item: (item["low"] is None, item["low"] or 9999, -item["stock"]))

    available_entries = [item for item in entries if not item.get("error") and item.get("low") is not None]
    available_entries.sort(key=lambda item: (item["low"], -int(item.get("stock") or 0)))
    return {
        "service": _canonical_service(service),
        "entries": entries,
        "best": available_entries[:8],
        "country_bands": country_bands[:8],
    }


# ── 账号 ────────────────────────────────────────────────


@router.get("/api/accounts")
async def api_list_accounts():
    waapi = getattr(router, "_waapi", None)
    if not waapi:
        raise HTTPException(503, "wa-app 客户端未初始化")
    try:
        accounts = await waapi.list_accounts()
        return {"accounts": accounts}
    except Exception as e:
        raise HTTPException(502, f"获取账号列表失败: {e}")


@router.get("/api/blacklist")
async def api_blacklist(provider: str = ""):
    db = getattr(router, "_db", None)
    if not db:
        raise HTTPException(503, "数据库未初始化")
    return {"entries": await db.list_blacklist(provider=provider or "")}


@router.post("/api/blacklist")
async def api_add_blacklist(data: dict):
    phone = data.get("phone", "").strip()
    reason = data.get("reason", "")
    provider = data.get("provider", "smsbower")
    if not phone:
        raise HTTPException(422, "phone is required")
    db = getattr(router, "_db", None)
    if not db:
        raise HTTPException(503, "数据库未初始化")
    await db.add_blacklist(phone, reason, provider=provider)
    return {"success": True}


@router.delete("/api/blacklist")
async def api_remove_blacklist(phone: str, provider: str = "smsbower"):
    if not phone or not phone.strip():
        raise HTTPException(422, "phone 不能为空")
    db = getattr(router, "_db", None)
    if not db:
        raise HTTPException(503, "数据库未初始化")
    await db.remove_blacklist(phone.strip(), provider=provider)
    return {"success": True}


# ── 日志 ────────────────────────────────────────────────


@router.get("/api/logs/recent")
async def api_recent_logs(lines: int = 200, level: str = ""):
    """读取本地滚动日志尾部，便于快速排查 bridge/provider 问题。"""
    limit = max(1, min(int(lines or 200), 1000))
    level = (level or "").strip().upper()
    allowed_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if level and level not in allowed_levels:
        raise HTTPException(422, f"level must be one of: {', '.join(sorted(allowed_levels))}")
    log_path = Path(os.environ.get("BRIDGE_LOG_FILE") or Path(env.bridge_data_dir) / "bridge.log")
    if not log_path.exists():
        return {"path": str(log_path), "entries": []}
    try:
        for handler in logging.getLogger().handlers:
            if getattr(handler, "_bridge_file_handler", False):
                handler.flush()
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            entries = [line.rstrip("\n") for line in deque(handle, maxlen=limit * 5 if level else limit)]
        if level:
            marker = f"[{level}]"
            entries = [line for line in entries if marker in line][-limit:]
        entries = [redact_text(line) for line in entries]
        counts = {name: sum(f"[{name}]" in line for line in entries) for name in allowed_levels}
        return {"path": str(log_path), "level": level or "ALL", "counts": counts, "entries": entries}
    except OSError as e:
        raise HTTPException(500, f"读取日志失败: {e}") from e


# ── 配置 ────────────────────────────────────────────────


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return key[:2] + "****"
    return key[:4] + "****" + key[-4:]


@router.get("/api/config")
async def api_get_config():
    db = getattr(router, "_db", None)
    if not db:
        raise HTTPException(503, "数据库未初始化")
    # 优先从 SQLite 读取，fallback 到环境变量
    api_key = await db.get_config("smsbower_api_key", "")
    base_url = await db.get_config("smsbower_base_url", "")
    default_country_str = await db.get_config(
        "smsbower_default_country",
        str(env.smsbower_default_country),
    )
    try:
        default_country = int(default_country_str)
    except ValueError:
        default_country = 0

    five_sim_key = await db.get_config("five_sim_api_key", "")
    hero_sms_key = await db.get_config("hero_sms_api_key", "")
    hero_sms_base_url = await db.get_config("hero_sms_base_url", "")

    return ConfigResponse(
        wa_app_url=getattr(router, "_waapi", None) and getattr(router, "_waapi")._base_url or "",
        smsbower_base_url=base_url or SmsBowerProvider.BASE_URL,
        hero_sms_base_url=hero_sms_base_url or HERO_SMS_DEFAULT_BASE_URL,
        smsbower_default_country=default_country,
        smsbower_api_key_masked=_mask_key(api_key) if api_key else "",
        five_sim_api_key_masked=_mask_key(five_sim_key) if five_sim_key else "",
        hero_sms_api_key_masked=_mask_key(hero_sms_key) if hero_sms_key else "",
        wa_app_auth_type="",
    ).model_dump()


@router.put("/api/config")
async def api_update_config(cfg: ConfigUpdate):
    from app.core.url_guard import validate_upstream_base_url, validate_wa_app_url
    from app.errors import AppError

    db = getattr(router, "_db", None)
    if not db:
        raise HTTPException(503, "数据库未初始化")
    try:
        if cfg.smsbower_base_url is not None and cfg.smsbower_base_url.strip():
            validate_upstream_base_url("smsbower", cfg.smsbower_base_url)
        if cfg.hero_sms_base_url is not None and cfg.hero_sms_base_url.strip():
            validate_upstream_base_url("herosms", cfg.hero_sms_base_url)
        if cfg.wa_app_url is not None and cfg.wa_app_url.strip():
            validate_wa_app_url(cfg.wa_app_url)
    except AppError as e:
        raise HTTPException(e.status_code, e.message) from e
    try:
        if cfg.smsbower_api_key is not None:
            await db.set_config("smsbower_api_key", cfg.smsbower_api_key.strip(), encrypt=True)
        if cfg.smsbower_base_url is not None:
            await db.set_config("smsbower_base_url", cfg.smsbower_base_url.strip().rstrip("/"))
        if cfg.smsbower_default_country is not None:
            await db.set_config("smsbower_default_country", str(cfg.smsbower_default_country))
        if cfg.five_sim_api_key is not None:
            await db.set_config("five_sim_api_key", cfg.five_sim_api_key.strip(), encrypt=True)
        if cfg.hero_sms_api_key is not None:
            await db.set_config("hero_sms_api_key", cfg.hero_sms_api_key.strip(), encrypt=True)
        if cfg.hero_sms_base_url is not None:
            await db.set_config("hero_sms_base_url", cfg.hero_sms_base_url.strip().rstrip("/"))
        if cfg.wa_app_url is not None:
            clean_wa = cfg.wa_app_url.strip().rstrip("/")
            await db.set_config("wa_app_url", clean_wa)
            waapi = getattr(router, "_waapi", None)
            if waapi:
                waapi.set_base_url(clean_wa)
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e
    if any(value is not None for value in (
        cfg.smsbower_api_key,
        cfg.smsbower_base_url,
        cfg.five_sim_api_key,
        cfg.hero_sms_api_key,
        cfg.hero_sms_base_url,
    )):
        from app.country_index import index
        asyncio.create_task(index.refresh(force=True))
    return {"success": True}


# ── 历史 ────────────────────────────────────────────────


@router.get("/api/history")
async def api_history():
    db = getattr(router, "_db", None)
    if not db:
        raise HTTPException(503, "数据库未初始化")
    return {"entries": await db.list_history()}


@router.get("/api/stats")
async def api_stats(timezone_offset_minutes: int = 0):
    db = getattr(router, "_db", None)
    if not db:
        raise HTTPException(503, "数据库未初始化")
    return await db.get_stats(timezone_offset_minutes)
