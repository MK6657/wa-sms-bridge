"""GlobalCountryIndex — 跨平台国家索引。

运行时从各 provider 的 get_countries() 拉取实时数据，
以 ISO 国家码为 key 合并成统一的跨平台索引。

特性:
- 实时获取，不硬编码
- 各平台国家增删自动适应
- 内存缓存 + TTL
- 跳过未配置/不可用的 provider
"""

import asyncio
import logging
import time
from typing import Any, Optional

from app.countries import normalize_name
from app.providers.registry import registry

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # 5 分钟


class GlobalCountryIndex:
    """跨平台国家索引。"""

    def __init__(self):
        self._cache: dict[str, Any] = {}  # iso → {平台名 → {id, name, cc}}
        self._last_fetch: Optional[float] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_task_forced: bool = False
        self._generation: int = 0

    def invalidate(self) -> None:
        self._generation += 1
        self._last_fetch = None

    async def refresh(self, force: bool = False):
        """刷新索引。从所有已注册 provider 拉取实时国家列表。"""
        if not force and self._last_fetch is not None and (time.monotonic() - self._last_fetch) < CACHE_TTL:
            return

        while True:
            running = self._refresh_task
            if not (running and not running.done()):
                break
            running_was_forced = self._refresh_task_forced
            await asyncio.shield(running)
            if not force or running_was_forced:
                return

        task = asyncio.create_task(self._refresh_once(force=force))
        self._refresh_task = task
        self._refresh_task_forced = bool(force)

        def clear_refresh_task(done: asyncio.Task):
            if self._refresh_task is done:
                self._refresh_task = None
                self._refresh_task_forced = False
            if not done.cancelled():
                done.exception()

        task.add_done_callback(clear_refresh_task)
        await asyncio.shield(task)

    async def _refresh_once(self, *, force: bool = False):
        start_gen = self._generation
        provider_names = registry.list_providers()

        async def fetch_provider(name: str):
            try:
                prov = registry.get(name)
                if force and hasattr(prov, "invalidate_countries_cache"):
                    prov.invalidate_countries_cache()
                countries = await asyncio.wait_for(prov.get_countries(), timeout=20)
                return name, countries, None
            except Exception as exc:
                return name, [], exc

        results = await asyncio.gather(*(fetch_provider(name) for name in provider_names))
        new_cache: dict[str, Any] = {}
        transient_failed_providers: set[str] = set()

        for name, countries, error in results:
            if error is not None:
                err_text = str(error)
                is_unconfigured = "API Key 未配置" in err_text or "鉴权失败" in err_text
                if not is_unconfigured:
                    transient_failed_providers.add(name)
                level = logging.INFO if is_unconfigured else logging.WARNING
                logger.log(level, "GlobalCountryIndex: 跳过 provider '%s': %s", name, error)
                continue
            for country in countries:
                iso = (country.get("iso") or "").lower()
                if not iso:
                    continue
                shared_aliases = [
                    a for a in (country.get("aliases") or [])
                    if a and not str(a).isdigit()
                ]
                entry = new_cache.setdefault(
                    iso,
                    {
                        "iso": iso,
                        "name": country.get("name", ""),
                        "name_zh": country.get("name_zh", ""),
                        "aliases": list(shared_aliases),
                        "providers": {},
                    },
                )
                prov_record = {
                    "id": country.get("id"),
                    "name": country.get("name", ""),
                    "name_zh": country.get("name_zh", ""),
                    "cc": country.get("cc", ""),
                    "aliases": list(country.get("aliases") or []),
                    "available": True,
                }
                existing_primary = entry["providers"].get(name)
                is_curr_virtual = "virtual" in str(prov_record["name"]).lower() or "虚拟" in str(prov_record["name_zh"])
                if existing_primary is None:
                    entry["providers"][name] = prov_record
                else:
                    was_virtual = "virtual" in str(existing_primary.get("name", "")).lower() or "虚拟" in str(existing_primary.get("name_zh", ""))
                    if was_virtual and not is_curr_virtual:
                        entry["providers"][name] = prov_record
                        if country.get("name"):
                            entry["name"] = country["name"]
                        if country.get("name_zh"):
                            entry["name_zh"] = country["name_zh"]
                variants = entry.setdefault("provider_variants", {}).setdefault(name, [])
                if not any(str(v.get("id")) == str(prov_record["id"]) for v in variants):
                    variants.append(prov_record)
                if not entry["name"] and country.get("name"):
                    entry["name"] = country["name"]
                if not entry["name_zh"] and country.get("name_zh"):
                    entry["name_zh"] = country["name_zh"]
                entry["aliases"] = list(dict.fromkeys([
                    *entry.get("aliases", []),
                    *shared_aliases,
                ]))

        # Provider 暂时网络不可用时保留它上一次成功的国家映射，避免 UI 瞬间清空（已清除密钥或鉴权失败则不保留旧缓存）。
        for iso, previous in self._cache.items():
            for name in transient_failed_providers:
                provider_entry = previous.get("providers", {}).get(name)
                if not provider_entry:
                    continue
                entry = new_cache.setdefault(
                    iso,
                    {
                        "iso": iso,
                        "name": previous.get("name", ""),
                        "name_zh": previous.get("name_zh", ""),
                        "aliases": list(previous.get("aliases") or []),
                        "providers": {},
                    },
                )
                entry["providers"].setdefault(name, provider_entry)
                prev_variants = previous.get("provider_variants", {}).get(name) or [provider_entry]
                entry.setdefault("provider_variants", {}).setdefault(name, list(prev_variants))

        if start_gen != self._generation:
            return
        if new_cache or not self._cache or not transient_failed_providers:
            self._cache = new_cache
        else:
            logger.warning(
                "GlobalCountryIndex 刷新结果为空，保留上次成功缓存: %d 个国家",
                len(self._cache),
            )
        # 空索引不进入长 TTL，便于用户刚保存 API Key 后立即重试。
        self._last_fetch = time.monotonic() if self._cache else None
        logger.info(
            "GlobalCountryIndex 已刷新: %d 个国家, %d 个平台",
            len(self._cache),
            len(provider_names),
        )

    def get_countries(self, provider: str = "") -> list[dict]:
        """获取国家列表。provider 指定时只返回该平台有的国家（含同 ISO 多线路 ID，如 US 187 与虚拟号 12）。"""
        if provider:
            result = []
            for iso, entry in self._cache.items():
                variants = entry.get("provider_variants", {}).get(provider)
                if not variants:
                    p_single = entry["providers"].get(provider)
                    variants = [p_single] if p_single else []
                for p in variants:
                    if p and p.get("available"):
                        result.append({
                            "iso": iso,
                            "name": p.get("name") or entry["name"],
                            "name_zh": p.get("name_zh") or entry.get("name_zh", ""),
                            "cc": p.get("cc", ""),
                            "id": p.get("id"),
                            "aliases": list(dict.fromkeys([
                                *(entry.get("aliases") or []),
                                *(p.get("aliases") or []),
                            ])),
                            "available": True,
                        })
            return sorted(result, key=lambda x: (x["name"], str(x["id"])))
        return [{"iso": k, **v} for k, v in sorted(self._cache.items(), key=lambda x: x[1].get("name", ""))]

    def search(self, query: str) -> list[dict]:
        """搜索国家（支持名称、ISO、区号）。"""
        q = normalize_name(query)
        if not q:
            return []
        result = []
        for iso, entry in self._cache.items():
            fields = [iso, entry.get("name"), entry.get("name_zh"), *(entry.get("aliases") or [])]
            for provider in entry.get("providers", {}).values():
                fields.extend([
                    provider.get("id"), provider.get("name"), provider.get("name_zh"), provider.get("cc"),
                    *(provider.get("aliases") or []),
                ])
            for variants in entry.get("provider_variants", {}).values():
                for v in variants or []:
                    fields.extend([
                        v.get("id"), v.get("name"), v.get("name_zh"), v.get("cc"),
                        *(v.get("aliases") or []),
                    ])
            normalized = [normalize_name(value) for value in fields if value not in (None, "")]
            if any(q == value or q in value for value in normalized):
                result.append({"iso": iso, **entry})
        return result

    def get_providers_for(self, iso: str) -> dict:
        """查询某国家在哪些平台可用。"""
        entry = self._cache.get(iso.lower(), {})
        return entry.get("providers", {})


# 全局单例
index = GlobalCountryIndex()
