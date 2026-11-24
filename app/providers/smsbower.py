"""SMSBower provider — 接码平台适配层。

API 协议：
  getNumber   → ACCESS_NUMBER:{activationId}:{phone}
  getStatus   → STATUS_OK:{code} / STATUS_WAIT_CODE / STATUS_CANCEL / ...
  setStatus   → ACCESS_READY / ACCESS_CANCEL
  getBalance  → ACCESS_BALANCE:{amount}
  getCountries → JSON { "id": { "eng": "...", "chn": "..." }, ... }
"""

import asyncio
import logging
import re
import time
from typing import Optional, Any

import httpx

from app.providers.base import (
    BaseSmsProvider,
    Activation,
    NoNumbersError,
    BalanceError,
    PurchaseUncertainError,
    SmsCancelledError,
    SmsExpiredError,
    SmsTimeoutError,
    SmsProviderError,
    SmsProtocolError,
)
from app.models import PhoneInput

logger = logging.getLogger(__name__)


# ── 国家映射表（SMSBower ID → 名称/区号） ───────────────

# 由 SMSBower API getCountries 实测生成
COUNTRIES = {
    0: {"eng": "Russian Federation", "chn": "俄罗斯联邦", "cc": "7"},
    1: {"eng": "Ukraine", "chn": "乌克兰", "cc": "380"},
    2: {"eng": "Kazakhstan", "chn": "哈萨克斯坦", "cc": "7"},
    3: {"eng": "China", "chn": "中国", "cc": "86"},
    4: {"eng": "Philippines", "chn": "菲律宾", "cc": "63"},
    5: {"eng": "Myanmar", "chn": "缅甸", "cc": "95"},
    6: {"eng": "Indonesia", "chn": "印度尼西亚", "cc": "62"},
    7: {"eng": "Malaysia", "chn": "马来西亚", "cc": "60"},
    8: {"eng": "Kenya", "chn": "肯尼亚", "cc": "254"},
    9: {"eng": "Tanzania", "chn": "坦桑尼亚", "cc": "255"},
    10: {"eng": "Viet nam", "chn": "越南", "cc": "84"},
    13: {"eng": "Israel", "chn": "以色列", "cc": "972"},
    16: {"eng": "United Kingdom", "chn": "英国", "cc": "44"},
    22: {"eng": "India", "chn": "印度", "cc": "91"},
    31: {"eng": "South Africa", "chn": "南非", "cc": "27"},
    52: {"eng": "Thailand", "chn": "泰国", "cc": "66"},
    187: {"eng": "United States", "chn": "美国", "cc": "1"},
}

# 国家 ISO2 映射（用于号码归一化兜底）
COUNTRY_ISO2: dict[int, str] = {
    0: "ru", 1: "ua", 2: "kz", 3: "cn", 4: "ph", 5: "mm", 6: "id",
    7: "my", 8: "ke", 9: "tz", 10: "vn", 13: "il", 16: "gb",
    22: "in", 31: "za", 52: "th", 187: "us",
}

# 别名映射（中英文 → ID）
ALIASES: dict[str, int] = {}
for _id, info in COUNTRIES.items():
    ALIASES[info["eng"].lower()] = _id
    ALIASES[info["chn"]] = _id
    ALIASES[str(_id)] = _id


def resolve_country_id(value: str | int) -> tuple[int, str]:
    """将国家名/ID 解析为 (id, label)。"""
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        cid = int(value)
        info = COUNTRIES.get(cid)
        if info:
            return cid, info["eng"]
        return cid, f"Country #{cid}"
    key = str(value).strip().lower()
    cid = ALIASES.get(key)
    if cid is not None:
        info = COUNTRIES.get(cid, {})
        return cid, info.get("eng", f"Country #{cid}")
    # 模糊匹配
    for cid, info in COUNTRIES.items():
        if key in info["eng"].lower() or key in info["chn"]:
            return cid, info["eng"]
    raise ValueError(f"未知国家: {value}")


# ── Provider ────────────────────────────────────────────


class SmsBowerProvider(BaseSmsProvider):
    """SMSBower API 封装。"""

    provider_id = "smsbower"
    BASE_URL = "https://smsbower.page/stubs/handler_api.php"

    def __init__(self, api_key_getter, base_url_getter=None):
        self._api_key_getter = api_key_getter
        self._base_url_getter = base_url_getter or (lambda: self.BASE_URL)
        self._client = httpx.AsyncClient(timeout=20.0)
        self._retry_delays = (0.5, 1.5)

    async def close(self):
        await self._client.aclose()

    async def resolve_country(self, country: str) -> tuple[Any, str]:
        from app.countries import name_to_iso, normalize_name

        raw = str(country or "").strip()
        if not raw:
            raise ValueError("未知国家: empty")

        if raw.isdigit():
            cid = int(raw)
            try:
                countries = await self.get_countries()
                for item in countries:
                    if str(item.get("id", "")) == str(cid):
                        return cid, item.get("name") or f"Country #{cid}"
            except Exception:
                pass
            return resolve_country_id(cid)

        try:
            return resolve_country_id(raw)
        except ValueError:
            pass

        key = normalize_name(raw)
        iso_hint = name_to_iso(raw)
        countries = await self.get_countries()
        for item in countries:
            fields = [item.get("name"), item.get("name_zh"), item.get("iso"), item.get("id"), item.get("cc")]
            fields.extend(item.get("aliases") or [])
            normalized_fields = {normalize_name(value) for value in fields if value not in (None, "")}
            if key in normalized_fields or (iso_hint and iso_hint == str(item.get("iso", "")).lower()):
                cid = int(item["id"]) if str(item.get("id", "")).isdigit() else item.get("id")
                return cid, item.get("name") or str(cid)

        for item in countries:
            names = [item.get("name"), item.get("name_zh"), *(item.get("aliases") or [])]
            normalized_names = [normalize_name(value) for value in names if value]
            if key and any(key in value or value in key for value in normalized_names if value):
                cid = int(item["id"]) if str(item.get("id", "")).isdigit() else item.get("id")
                return cid, item.get("name") or str(cid)

        raise ValueError(f"未知国家: {country}")

    async def normalize_phone(self, raw_phone: str, country_id: Any) -> "PhoneInput":
        """SMSBower 号码归一化，动态国家数据优先、静态表兜底。"""
        from app.phone_normalizer import build_phone_input, resolve_country_metadata

        cid = int(country_id) if isinstance(country_id, (int, str)) and str(country_id).isdigit() else 0
        info = COUNTRIES.get(cid, {})
        static_rows = [{
            "id": cid,
            "name": info.get("eng", ""),
            "cc": info.get("cc", ""),
            "iso": COUNTRY_ISO2.get(cid, ""),
        }] if info else []

        rows = static_rows
        if not info or not info.get("cc") or not COUNTRY_ISO2.get(cid, ""):
            try:
                rows = static_rows + await self.get_countries()
            except Exception:
                rows = static_rows

        meta = resolve_country_metadata(
            cid,
            country_label=info.get("eng", ""),
            countries=rows,
        )
        return build_phone_input(
            raw_phone,
            country_calling_code=meta.get("calling_code", ""),
            country_iso2=meta.get("iso2", ""),
            country_label=meta.get("name", ""),
        )

    async def _api_key(self) -> str:
        key = await self._api_key_getter()
        if not key:
            raise SmsProviderError("SMSBower API Key 未配置，请先在配置页设置")
        return key

    async def _base_url(self) -> str:
        raw = self._base_url_getter()
        if asyncio.iscoroutine(raw):
            raw = await raw
        return raw or self.BASE_URL

    async def _get(self, **params) -> str:
        """发起 GET 请求并返回纯文本响应。"""
        params["api_key"] = await self._api_key()
        url = await self._base_url()
        safe_params = {k: v for k, v in params.items() if k != "api_key"}
        started = time.perf_counter()
        # getNumber is not idempotent: retrying after a lost response can buy twice.
        delays = () if params.get("action") == "getNumber" else tuple(self._retry_delays)
        for attempt in range(len(delays) + 1):
            try:
                resp = await self._client.get(url, params=params)
                text = resp.text.strip()
            except httpx.TimeoutException as exc:
                message = "SMSBower 请求超时"
                retryable_error = exc
            except httpx.RequestError as exc:
                message = "SMSBower 连接失败，请检查网络"
                retryable_error = exc
            else:
                retryable_error = None
                if resp.status_code not in {502, 503, 504}:
                    break
                message = f"SMSBower 上游暂时异常 (HTTP {resp.status_code})"

            if attempt >= len(delays):
                raise SmsProviderError(message) from retryable_error
            delay = max(0.0, float(delays[attempt]))
            logger.warning(
                "SMSBower transient failure action=%s retry=%s/%s delay=%.1fs error=%s",
                params.get("action"),
                attempt + 1,
                len(delays),
                delay,
                message,
            )
            await asyncio.sleep(delay)

        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.debug(
            "SMSBower request action=%s params=%s status=%s duration_ms=%s response_len=%s",
            params.get("action"),
            safe_params,
            getattr(resp, "status_code", "unknown"),
            duration_ms,
            len(text),
        )
        if not resp.is_success:
            if resp.status_code in {401, 403}:
                raise SmsProviderError(f"SMSBower 鉴权失败 (HTTP {resp.status_code})")
            if resp.status_code == 429:
                raise SmsProviderError("SMSBower 请求过于频繁 (HTTP 429)")
            raise SmsProviderError(f"SMSBower 上游异常 (HTTP {resp.status_code}): {text[:120]}")
        if not text:
            raise SmsProviderError("SMSBower 返回空响应")
        return text

    async def _safe_cancel(self, activation_id: str) -> str:
        """安全取消激活（吞异常）。"""
        try:
            return await self._get(action="setStatus", id=activation_id, status=8)
        except Exception as e:
            from app.log_redaction import mask_identifier
            logger.warning("safe cancel activation %s failed: %s", mask_identifier(activation_id), e)
            return ""

    # ── 核心 API ───────────────────────────────────────

    async def get_balance(self) -> float:
        """获取余额。"""
        text = await self._get(action="getBalance")
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1].strip())
        raise SmsProviderError(f"获取余额失败: {text[:100]}")

    async def get_countries(self) -> list[dict]:
        """获取国家列表，统一格式 {id, name, cc, iso}。"""
        text = await self._get(action="getCountries")
        import json
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise SmsProviderError(f"解析国家列表失败: {text[:200]}")
        result = []
        for cid_str, info in data.items():
            if not cid_str or not cid_str.isdigit():
                continue
            cid = int(cid_str)
            eng = info.get("eng", "")
            name_zh = info.get("chn", "")
            name = eng or f"Country #{cid}"
            # cc 从本地表或 ISO 反查补齐（API 不返回 cc）
            cc = COUNTRIES.get(cid, {}).get("cc", "")
            # iso 从本地映射表补，没有则用国家工具反查
            iso = COUNTRY_ISO2.get(cid, "")
            if not iso:
                from app.countries import name_to_iso
                iso = name_to_iso(name) or name_to_iso(name_zh)
            if not cc and iso:
                from app.countries import iso_to_cc
                cc = iso_to_cc(iso)
            if not name_zh and iso:
                from app.countries import iso_to_name
                name_zh = iso_to_name(iso, "zh")
            aliases = list(dict.fromkeys(filter(None, (info.get("rus", ""), iso, str(cid), cc))))
            result.append({
                "id": cid,
                "name": name,
                "name_zh": name_zh,
                "cc": cc,
                "iso": iso,
                "aliases": aliases,
            })
        return sorted(result, key=lambda x: x["id"])

    async def get_prices(self, country_id: int, service: str = "wa") -> list[dict]:
        """获取某国家某服务的价格与库存。"""
        text = await self._get(action="getPricesV3", service=service, country=country_id)
        import json
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise SmsProviderError(f"解析价格失败: {text[:200]}")

        entries: list[dict] = []
        stats = {"explicit": 0, "legacy": 0, "rejected": 0}

        def as_float(value) -> Optional[float]:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def as_count(value) -> int:
            if isinstance(value, str) and value.strip().lower() == "few":
                return 1
            try:
                return max(0, int(float(value)))
            except (TypeError, ValueError):
                return 0

        def first_float(obj: dict, names: tuple[str, ...]) -> Optional[float]:
            for name in names:
                if name in obj:
                    parsed = as_float(obj.get(name))
                    if parsed is not None:
                        return parsed
            return None

        def first_count(obj: dict, names: tuple[str, ...]) -> int:
            for name in names:
                if name in obj:
                    return as_count(obj.get(name))
            return 0

        def add_entry(cost: Optional[float], count: int, provider_id=None, mode: str = "explicit"):
            if cost is None or cost <= 0:
                stats["rejected"] += 1
                return
            # 旧格式用数字 key 表示价格，同时响应里也可能混入几千位的 provider ID。
            # 显式 price/cost 字段可信度更高，不应用旧格式的 $5 截断规则。
            if mode == "legacy" and cost >= 5:
                stats["rejected"] += 1
                return
            entry = {"cost": round(cost, 4), "count": count}
            if provider_id is not None:
                entry["provider_id"] = provider_id
            entries.append(entry)
            stats[mode] += 1

        def walk(obj, parent_key=None):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, dict):
                        cost = first_float(v, ("price", "cost", "Price", "Cost"))
                        if cost is not None:
                            count = first_count(v, ("count", "qty", "Qty", "quantity", "Quantity"))
                            provider_id = v.get("provider_id") or v.get("id") or k
                            add_entry(cost, count, provider_id=provider_id, mode="explicit")
                            continue

                    is_price_key = bool(re.fullmatch(r"\d+(?:\.\d+)?", str(k)))
                    has_count = isinstance(v, dict) and any(name in v for name in ("count", "qty", "Qty", "quantity", "Quantity"))
                    if is_price_key and (isinstance(v, (int, float, str)) or has_count):
                        cost = as_float(k)
                        count = as_count(v) if isinstance(v, (int, float, str)) else first_count(v, ("count", "qty", "Qty", "quantity", "Quantity"))
                        add_entry(cost, count, mode="legacy")
                    else:
                        walk(v, k)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item, parent_key)
        walk(data)
        if not entries:
            top_keys = list(data.keys())[:8] if isinstance(data, dict) else []
            level = logging.WARNING if stats["rejected"] else logging.DEBUG
            logger.log(
                level,
                "SMSBower getPricesV3 parsed no entries country=%s service=%s raw_type=%s top_keys=%s response_len=%s sample=%r stats=%s",
                country_id,
                service,
                type(data).__name__,
                top_keys,
                len(text),
                text[:300],
                stats,
            )
        else:
            logger.debug(
                "SMSBower getPricesV3 parsed country=%s service=%s entries=%s stats=%s",
                country_id,
                service,
                len(entries),
                stats,
            )
        return entries

    async def request_number(self, country_id: int, service: str = "wa",
                              price_max: Optional[float] = None) -> Activation:
        """取号。返回 (activation_id, phone_number) 或抛出异常。"""
        params = {"action": "getNumber", "service": service, "country": country_id}
        if price_max is not None:
            params["maxPrice"] = str(round(price_max, 4))

        try:
            text = await self._get(**params)
        except SmsProviderError as exc:
            message = str(exc)
            if any(token in message for token in (
                "请求超时",
                "连接失败",
                "上游暂时异常",
                "返回空响应",
            )):
                raise PurchaseUncertainError(
                    f"SMSBower 取号结果未知，已停止自动重试以避免重复扣费: {message}"
                ) from exc
            raise

        if text.startswith("ACCESS_NUMBER:"):
            parts = text.split(":", 2)
            if len(parts) >= 3:
                cid = int(country_id)
                label = COUNTRIES.get(cid, {}).get("eng", f"Country #{cid}")
                act = Activation(
                    activation_id=parts[1],
                    phone_number=parts[2],
                    country_id=cid,
                    country_label=label,
                    provider=self.provider_id,
                    price=price_max,
                    price_is_estimated=price_max is not None,
                )
                return act
            raise SmsProtocolError(f"取号响应格式异常: {text[:100]}")

        if "NO_NUMBERS" in text:
            raise NoNumbersError(f"国家 {country_id} 无可用号码: {text[:100]}")
        if "NO_BALANCE" in text:
            raise BalanceError(f"SMSBower 余额不足: {text[:100]}")

        raise SmsProviderError(f"取号失败: {text[:200]}")

    async def get_sms_code(self, activation_id: str) -> tuple[str, str]:
        """查码。返回 (status_code, code_or_empty)。status_code:
          - 'OK' = 收到验证码，code_or_empty 为验证码（纯数字）
          - 'WAIT' = 等待中
          - 'CANCEL' = 被取消
          - 'EXPIRED' = 过期
        """
        text = await self._get(action="getStatus", id=activation_id)

        if text.startswith("STATUS_OK:"):
            raw = text.split(":", 1)[1].strip()
            # 用正则提取纯数字验证码
            digits = re.sub(r"\D", "", raw)
            if digits:
                return "OK", digits
            # 没有数字，可能 STATUS_OK: 空响应，继续等待
            logger.warning("SMSBower STATUS_OK 中未找到数字验证码 (raw=%r), 继续等待", raw)
            return "WAIT", ""
        if text == "STATUS_WAIT_CODE" or text == "STATUS_WAIT_RETRY":
            return "WAIT", ""
        if text == "STATUS_CANCEL":
            return "CANCEL", ""
        if text in ("STATUS_NO_ACTIVATION",):
            return "EXPIRED", ""

        logger.warning("SMSBower getStatus 未知应答: %s (activation=%s)", text, activation_id)
        return "UNKNOWN", text

    async def cancel_activation(self, activation_id: str) -> str:
        """取消激活（status=8，退款）。"""
        return await self._get(action="setStatus", id=activation_id, status=8)

    async def complete_activation(self, activation_id: str) -> str:
        """完成激活（status=6）。"""
        return await self._get(action="setStatus", id=activation_id, status=6)

    async def poll_sms_code(self, activation_id: str, timeout: int = 180) -> str:
        """阶梯轮询等码。返回验证码字符串，超时抛出 SmsTimeoutError。

        轮询策略：
          前 60 秒 每 3 秒
          60-180 秒 每 5 秒
          180+  秒 每 8 秒（但总超时由 timeout 控制）
        """
        start = asyncio.get_running_loop().time()
        while True:
            elapsed = asyncio.get_running_loop().time() - start
            if elapsed >= timeout:
                raise SmsTimeoutError(f"等码超时 ({timeout}s), activation={activation_id}")

            status, code = await self.get_sms_code(activation_id)

            if status == "OK":
                return code
            if status == "CANCEL":
                raise SmsCancelledError(f"激活已被 SMSBower 取消: {activation_id}")
            if status == "EXPIRED":
                raise SmsExpiredError(f"激活已过期/不存在: {activation_id}")
            if status == "UNKNOWN":
                logger.warning("SMSBower 未知状态码，继续等待: %s", code)

            # 阶梯延时
            if elapsed < 60:
                delay = 3
            elif elapsed < 180:
                delay = 5
            else:
                delay = 8

            await asyncio.sleep(delay)

    async def request_additional_sms(self, activation_id: str) -> str:
        """请求重新发送短信（status=3）。"""
        return await self._get(action="setStatus", id=activation_id, status=3)
