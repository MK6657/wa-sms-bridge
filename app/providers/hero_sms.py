"""HeroSMS provider — 接码平台适配层。

API 与 SMSBower 兼容（同类型的 handler_api.php 协议）。
Base URL: https://hero-sms.com/stubs/handler_api.php
认证: api_key 查询参数
响应: 纯文本（ACCESS_NUMBER / STATUS_OK / ACCESS_BALANCE 等）

注意：HeroSMS 取消有约 2 分钟冷却期限制，
提前取消会返回错误，_safe_cancel 会吞异常处理。
"""

import asyncio
import logging
import re
import time
from typing import Any, Optional

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
    extract_otp_code,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
DEFAULT_SERVICE = "wa"
DEFAULT_TIMEOUT = 30

# ── HeroSMS 独立国家表。多数 ID 与 SMSBower 相同，但美国虚拟号是 12。─────

HERO_COUNTRIES = {
    0: {"eng": "Russian Federation", "chn": "俄罗斯", "cc": "7", "iso": "ru"},
    1: {"eng": "Ukraine", "chn": "乌克兰", "cc": "380", "iso": "ua"},
    2: {"eng": "Kazakhstan", "chn": "哈萨克斯坦", "cc": "7", "iso": "kz"},
    3: {"eng": "China", "chn": "中国", "cc": "86", "iso": "cn"},
    4: {"eng": "Philippines", "chn": "菲律宾", "cc": "63", "iso": "ph"},
    5: {"eng": "Myanmar", "chn": "缅甸", "cc": "95", "iso": "mm"},
    6: {"eng": "Indonesia", "chn": "印度尼西亚", "cc": "62", "iso": "id"},
    12: {"eng": "United States", "chn": "美国（虚拟）", "cc": "1", "iso": "us"},
    22: {"eng": "India", "chn": "印度", "cc": "91", "iso": "in"},
    30: {"eng": "Yemen", "chn": "也门", "cc": "967", "iso": "ye"},
    31: {"eng": "South Africa", "chn": "南非", "cc": "27", "iso": "za"},
    33: {"eng": "Colombia", "chn": "哥伦比亚", "cc": "57", "iso": "co"},
    41: {"eng": "Netherlands", "chn": "荷兰", "cc": "31", "iso": "nl"},
    52: {"eng": "Thailand", "chn": "泰国", "cc": "66", "iso": "th"},
    68: {"eng": "Saudi Arabia", "chn": "沙特阿拉伯", "cc": "966", "iso": "sa"},
    71: {"eng": "South Korea", "chn": "韩国", "cc": "82", "iso": "kr"},
    74: {"eng": "Afghanistan", "chn": "阿富汗", "cc": "93", "iso": "af"},
    91: {"eng": "Timor-Leste", "chn": "东帝汶", "cc": "670", "iso": "tl"},
}

HERO_COUNTRY_NAMES = {cid: info["chn"] for cid, info in HERO_COUNTRIES.items()}

HERO_ALIASES: dict[str, int] = {}
for _cid, _info in HERO_COUNTRIES.items():
    HERO_ALIASES[str(_cid)] = _cid
    for _value in (_info["eng"], _info["chn"], _info["iso"]):
        HERO_ALIASES[_value.strip().lower()] = _cid
    HERO_ALIASES[re.sub(r"[^a-z0-9]+", "", _info["eng"].lower())] = _cid
for _cid, _info in HERO_COUNTRIES.items():
    HERO_ALIASES.setdefault(_info["cc"].strip().lower(), _cid)
HERO_ALIASES.update({
    "usa": 12,
    "america": 12,
    "美国": 12,
    "us": 12,
    "nanfei": 31,
    "southafrica": 31,
    "taiguo": 52,
    "yindunixiya": 6,
    "yinni": 6,
})


def _hero_static_rows() -> list[dict]:
    return [
        {
            "id": cid,
            "name": info["eng"],
            "cn": info["chn"],
            "cc": info["cc"],
            "iso": info["iso"],
        }
        for cid, info in sorted(HERO_COUNTRIES.items(), key=lambda item: item[1]["eng"])
    ]


def _hero_label(cid: int) -> str:
    info = HERO_COUNTRIES.get(cid)
    return info["eng"] if info else f"Country #{cid}"


class HeroSmsProvider(BaseSmsProvider):
    """HeroSMS API 封装。"""

    provider_id = "herosms"

    def __init__(self, api_key_getter, base_url_getter=None):
        self._api_key_getter = api_key_getter
        self._base_url_getter = base_url_getter or (lambda: DEFAULT_BASE_URL)
        self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    async def close(self):
        await self._client.aclose()

    async def _api_key(self) -> str:
        key = await self._api_key_getter()
        if not key:
            raise SmsProviderError("HeroSMS API Key 未配置，请先在配置页设置")
        return key

    async def _get(self, **params) -> str:
        """发起 GET 请求并返回纯文本响应。"""
        params["api_key"] = await self._api_key()
        raw = self._base_url_getter()
        if asyncio.iscoroutine(raw):
            raw = await raw
        url = raw or DEFAULT_BASE_URL
        safe_params = {key: value for key, value in params.items() if key != "api_key"}
        started = time.perf_counter()
        try:
            resp = await self._client.get(url, params=params)
            text = resp.text.strip()
        except httpx.TimeoutException as exc:
            raise SmsProviderError("HeroSMS 请求超时") from exc
        except httpx.RequestError as exc:
            raise SmsProviderError(f"HeroSMS 连接失败，请检查网络: {exc}") from exc
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.debug(
            "HeroSMS request action=%s params=%s status=%s duration_ms=%s response_len=%s",
            params.get("action"),
            safe_params,
            resp.status_code,
            duration_ms,
            len(text),
        )
        if not resp.is_success:
            if resp.status_code in {401, 403}:
                raise SmsProviderError(f"HeroSMS 鉴权失败 (HTTP {resp.status_code})")
            if resp.status_code == 429:
                raise SmsProviderError("HeroSMS 请求过于频繁 (HTTP 429)")
            raise SmsProviderError(f"HeroSMS 上游异常 (HTTP {resp.status_code}): {text[:120]}")
        if not text:
            raise SmsProviderError("HeroSMS 返回空响应")
        return text

    def _parse_access_number(self, text: str) -> tuple[str, str]:
        """解析 ACCESS_NUMBER 响应。返回 (activation_id, phone)。"""
        m = re.match(r"^ACCESS_NUMBER:\s*([^:]+):\s*(.+)$", text)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        if "NO_NUMBERS" in text or "WRONG_MAX_PRICE" in text:
            raise NoNumbersError(f"HeroSMS 无满足条件号码: {text[:100]}")
        if "NO_BALANCE" in text or "BALANCE" in text.upper():
            raise BalanceError("HeroSMS 余额不足")
        raise SmsProviderError(f"HeroSMS 取号异常: {text[:100]}")

    async def get_balance(self) -> float:
        text = await self._get(action="getBalance")
        m = re.search(r"ACCESS_BALANCE:\s*([\d.]+)", text)
        if m:
            return float(m.group(1))
        raise SmsProviderError(f"HeroSMS 余额解析失败: {text[:100]}")

    async def get_countries(self) -> list[dict]:
        """获取国家列表，统一格式 {id, name, cc, iso}。"""
        text = await self._get(action="getPricesV3")
        if not text.startswith("{"):
            return _hero_static_rows()
        try:
            import json
            data = json.loads(text)
            result = []
            for cid_str in (data or {}).keys():
                if not str(cid_str).isdigit():
                    continue
                cid = int(cid_str)
                static = HERO_COUNTRIES.get(cid, {})
                name = static.get("eng") or f"Country #{cid}"
                cc = static.get("cc", "")
                iso = static.get("iso", "")
                result.append({
                    "id": cid,
                    "name": name,
                    "cc": cc,
                    "iso": iso,
                })
            known_ids = {int(item["id"]) for item in result if str(item.get("id", "")).isdigit()}
            for row in _hero_static_rows():
                if row["id"] not in known_ids:
                    result.append(row)
            return sorted(result, key=lambda item: item.get("name", ""))
        except json.JSONDecodeError:
            return _hero_static_rows()

    async def resolve_country(self, country: str) -> tuple[Any, str]:
        raw = str(country or "").strip()
        if not raw:
            raise ValueError("未知 HeroSMS 国家: empty")

        key = raw.lower()
        compact = re.sub(r"[^a-z0-9]+", "", key)
        cid = HERO_ALIASES.get(key)
        if cid is None and compact:
            cid = HERO_ALIASES.get(compact)
        if cid is not None:
            return cid, _hero_label(cid)

        if raw.isdigit():
            cid = int(raw)
            try:
                countries = await self.get_countries()
                if any(str(item.get("id", "")) == str(cid) for item in countries):
                    return cid, _hero_label(cid)
            except Exception:
                pass
            raise ValueError(f"未知 HeroSMS 国家 ID: {country}")

        allow_sub = len(key) >= 4 or any(ord(ch) > 127 for ch in key)
        if allow_sub:
            for item in _hero_static_rows():
                fields = [
                    str(item.get("name", "")).lower(),
                    str(item.get("cn", "")).lower(),
                ]
                if any(
                    (key and key in field)
                    or (len(compact) >= 4 and compact in re.sub(r"[^a-z0-9]+", "", field))
                    for field in fields
                    if field
                ):
                    return item["id"], _hero_label(item["id"])

        try:
            countries = await self.get_countries()
            for item in countries:
                fields = [
                    str(item.get("name", "")).strip().lower(),
                    str(item.get("iso", "")).strip().lower(),
                    str(item.get("id", "")).strip().lower(),
                    str(item.get("cc", "")).strip().lower(),
                ]
                if key in fields:
                    cid = int(item["id"]) if str(item.get("id", "")).isdigit() else item.get("id")
                    return cid, item.get("name") or str(cid)
        except Exception:
            pass

        raise ValueError(f"未知 HeroSMS 国家: {country}")

    async def get_prices(self, country_id: Any, product: str = DEFAULT_SERVICE) -> list[dict]:
        text = await self._get(action="getPricesV3", service=product, country=country_id)
        try:
            import json
            data = json.loads(text)
            prices = []
            def _as_count(raw_cnt: Any) -> int:
                if isinstance(raw_cnt, str) and raw_cnt.strip().lower() == "few":
                    return 1
                try:
                    return max(0, int(float(raw_cnt)))
                except (TypeError, ValueError):
                    return 0

            def walk(obj):
                if isinstance(obj, dict):
                    cost = None
                    for k in ("cost", "Price", "price"):
                        v = obj.get(k)
                        if v is not None:
                            try:
                                parsed_cost = round(float(v), 4)
                                if parsed_cost > 0:
                                    cost = parsed_cost
                                break
                            except (ValueError, TypeError):
                                pass
                    if cost is not None and not isinstance(obj.get("providers"), (dict, list)):
                        raw_cnt = next((obj.get(ck) for ck in ("count", "Qty", "qty", "quantity") if ck in obj), 0)
                        count = _as_count(raw_cnt)
                        prices.append({"cost": cost, "count": count, "in_stock": count > 0})
                        return
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            walk(v)
                elif isinstance(obj, list):
                    for item in obj:
                        walk(item)
            walk(data)
            return prices
        except json.JSONDecodeError as exc:
            raise SmsProviderError(f"HeroSMS 价格解析失败: {text[:200]}") from exc

    async def request_number(self, country_id: Any, service: str = DEFAULT_SERVICE,
                              price_max: Optional[float] = None) -> Activation:
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
                "上游异常 (HTTP 5",
                "返回空响应",
            )):
                raise PurchaseUncertainError(
                    f"HeroSMS 取号结果未知，已停止自动重试以避免重复扣费: {message}"
                ) from exc
            raise
        activation_id, phone = self._parse_access_number(text)
        label = _hero_label(int(country_id)) if str(country_id).isdigit() else str(country_id)
        return Activation(
            activation_id=activation_id,
            phone_number=phone,
            country_id=country_id,
            country_label=label,
            provider=self.provider_id,
            price=price_max,
            price_is_estimated=price_max is not None,
            currency="USD",
            currency_source="provider_default",
        )

    async def validate_registration_country(self, country_id: Any):
        try:
            cid = int(country_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"HeroSMS 国家 ID 无效: {country_id}") from exc
        metadata = HERO_COUNTRIES.get(cid, {})
        if not metadata.get("cc") or not metadata.get("iso"):
            raise ValueError(
                f"HeroSMS 国家 {country_id} 缺少区号/ISO 映射，已在取号前阻止 WA 注册"
            )

    async def get_sms_code(self, activation_id: str) -> tuple[str, str]:
        text = await self._get(action="getStatus", id=activation_id)
        if text.startswith("STATUS_OK:"):
            parts = text.split(":", 1)
            raw_code = parts[1].strip() if len(parts) > 1 else ""
            code = extract_otp_code(raw_code)
            if code:
                self.record_raw_sms(activation_id, raw_code)
                return ("OK", code)
            return ("WAIT", "")
        if (
            text == "STATUS_WAIT_CODE"
            or text.startswith("STATUS_WAIT_RETRY")
            or text.startswith("STATUS_WAIT_RESEND")
        ):
            return ("WAIT", "")
        if text == "STATUS_CANCEL":
            return ("CANCEL", "")
        if text in ("STATUS_NO_ACTIVATION", "NO_ACTIVATION"):
            return ("EXPIRED", "")
        raise SmsProviderError(f"HeroSMS 查码状态未知: {text[:100]}")

    async def cancel_activation(self, activation_id: str) -> str:
        """取消激活。HeroSMS 有约 2 分钟冷却期，提前取消会返回 early_cancel_denied。"""
        text = await self._get(action="setStatus", id=activation_id, status="8")
        if "ACCESS_CANCEL" in text:
            return "cancelled"
        if "ACCESS_READY" in text or "ACCESS_ACTIVATION" in text:
            return "ACCESS_READY"
        if "NO_ACTIVATION" in text:
            return "NO_ACTIVATION"
        if "EARLY_CANCEL_DENIED" in text or "BAD_STATUS" in text:
            from app.log_redaction import mask_identifier
            logger.warning("HeroSMS 冷却期或状态暂拒取消 (activation=%s): 标记为待补偿", mask_identifier(activation_id))
            return "early_cancel_denied"
        raise SmsProviderError(f"HeroSMS 取消失败: {text[:100]}")

    async def cancel_activation_structured(self, activation_id: str):
        """结构化取消结果：准确识别 ACCESS_READY/ACCESS_ACTIVATION/NO_ACTIVATION 为已完结不可退，绝不陷入无限补偿循环。"""
        from app.providers.base import CancelOutcome, parse_cancel_outcome
        try:
            raw = await self._get(action="setStatus", id=activation_id, status="8")
        except Exception as exc:
            return CancelOutcome(settled=False, status="error", retryable=True, raw=str(exc), refund_confirmed=False)
        if "BAD_STATUS" in str(raw or "").upper():
            try:
                st = await self._get(action="getStatus", id=activation_id)
            except Exception:
                st = ""
            if st.startswith("STATUS_OK:"):
                return CancelOutcome(
                    settled=True,
                    status="already_completed",
                    retryable=False,
                    raw=f"{raw} (getStatus=STATUS_OK)",
                    refund_confirmed=False,
                )
            if st == "STATUS_CANCEL":
                return CancelOutcome(
                    settled=True,
                    status="refund_confirmed",
                    retryable=False,
                    raw=f"{raw} (getStatus=STATUS_CANCEL)",
                    refund_confirmed=True,
                )
            if st in ("STATUS_NO_ACTIVATION", "NO_ACTIVATION"):
                return CancelOutcome(
                    settled=True,
                    status="not_found_or_expired",
                    retryable=False,
                    raw=f"{raw} (getStatus={st})",
                    refund_confirmed=False,
                )
        return parse_cancel_outcome(raw)

    async def complete_activation(self, activation_id: str) -> str:
        text = await self._get(action="setStatus", id=activation_id, status="6")
        if "ACCESS_READY" in text or "ACCESS_ACTIVATION" in text:
            return "completed"
        raise SmsProviderError(f"HeroSMS 完成失败: {text[:100]}")

    async def poll_sms_code(self, activation_id: str, timeout: int = 180) -> str:
        intervals = [3] * 12 + [5] * 12 + [8] * 15
        start = asyncio.get_running_loop().time()
        for delay in intervals:
            status, code = await self.get_sms_code(activation_id)
            if status == "OK" and code:
                return code
            if status == "CANCEL":
                raise SmsCancelledError("HeroSMS 激活被取消")
            if status == "EXPIRED":
                raise SmsExpiredError("HeroSMS 激活已过期")
            elapsed = asyncio.get_running_loop().time() - start
            if elapsed >= timeout:
                raise SmsTimeoutError(f"HeroSMS 等码超时 ({timeout}s)")
            await asyncio.sleep(delay)
        raise SmsTimeoutError(f"HeroSMS 等码超时 ({timeout}s)")

    async def normalize_phone(self, raw_phone: str, country_id: Any) -> "PhoneInput":
        from app.phone_normalizer import build_phone_input, resolve_country_metadata

        rows = []
        try:
            rows = await self.get_countries()
        except Exception:
            rows = []

        static = HERO_COUNTRIES.get(int(country_id), {}) if str(country_id).isdigit() else {}
        static_rows = [{
            "id": country_id,
            "name": static.get("eng", ""),
            "cc": static.get("cc", ""),
            "iso": static.get("iso", ""),
        }] if static else []
        label = static.get("eng", "") or str(country_id or "")
        meta = resolve_country_metadata(country_id, country_label=label, countries=static_rows + rows)
        return build_phone_input(
            raw_phone,
            country_calling_code=meta.get("calling_code", ""),
            country_iso2=meta.get("iso2", ""),
            country_label=meta.get("name", "") or label,
        )
