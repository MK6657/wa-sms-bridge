"""价格汇总与短期 Singleflight 缓存服务。"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Optional

from app.providers.registry import ProviderRegistry
from app.service_codes import canonical_service, provider_service_code


def _price_value(entry: dict[str, Any]) -> Optional[float]:
    for key in ("cost", "price", "Price"):
        value = entry.get(key)
        if value is None:
            continue
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(price) and price > 0:
            return round(price, 4)
    return None


def _price_count(entry: dict[str, Any]) -> int:
    for key in ("count", "qty", "Qty", "quantity"):
        value = entry.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip().lower() == "few":
            return 1
        try:
            num = float(value)
            if not math.isfinite(num):
                continue
            return max(0, int(num))
        except (TypeError, ValueError, OverflowError):
            continue
    return 0


def summarize_prices(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """将原始价格条目聚合为有序价档与低/中/高参考价。"""
    buckets: dict[float, int] = {}
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        cost = _price_value(item)
        if cost is None:
            continue
        count = _price_count(item)
        buckets[cost] = buckets.get(cost, 0) + count

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


class PricingService:
    """带短期 TTL 缓存和并发请求合并（Singleflight）的价格查询服务。"""

    def __init__(self, registry: ProviderRegistry, *, cache_ttl: float = 10.0):
        self._registry = registry
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._generation: int = 0

    def invalidate(self) -> None:
        self._generation += 1
        self._cache.clear()

    async def get_price_snapshot(
        self,
        provider_name: str,
        country: str,
        service: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        provider_key = (provider_name or self._registry.default_provider).strip().lower()
        provider = self._registry.get(provider_key)
        cid, label = await provider.resolve_country(country)
        service_code = provider_service_code(provider_key, service)
        canonical = canonical_service(service)
        cache_key = f"{provider_key}:{cid}:{canonical}:{service_code}"

        now = time.monotonic()
        if not force and self._cache_ttl > 0:
            cached = self._cache.get(cache_key)
            if cached and (now - cached[0]) < self._cache_ttl:
                return dict(cached[1], cached=True)

        existing = self._inflight.get(cache_key)
        if not force and existing is not None and not existing.done():
            result = await asyncio.shield(existing)
            return dict(result, cached=True)

        if force:
            self._generation += 1
        gen = self._generation

        async def _fetch() -> dict[str, Any]:
            raw_entries = await provider.get_prices(cid, service_code)
            summary = summarize_prices(raw_entries)
            snapshot = {
                "provider": provider_key,
                "country": str(cid),
                "country_label": label,
                "service": canonical,
                "service_code": service_code,
                **summary,
            }
            if self._cache_ttl > 0 and gen == self._generation:
                now_mono = time.monotonic()
                if len(self._cache) >= 512 and cache_key not in self._cache:
                    expired_keys = [k for k, (ts, _) in self._cache.items() if (now_mono - ts) >= self._cache_ttl]
                    for k in expired_keys:
                        self._cache.pop(k, None)
                    if len(self._cache) >= 512:
                        self._cache.pop(next(iter(self._cache)), None)
                self._cache[cache_key] = (now_mono, snapshot)
            return snapshot

        task = asyncio.create_task(_fetch())
        self._inflight[cache_key] = task

        def _cleanup(done: asyncio.Task) -> None:
            if self._inflight.get(cache_key) is done:
                self._inflight.pop(cache_key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(_cleanup)
        result = await asyncio.shield(task)
        return dict(result, cached=False)
