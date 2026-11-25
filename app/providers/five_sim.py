"""5sim provider — 接码平台适配层。

API 文档: https://5sim.net/docs
Base URL: https://5sim.net/v1
认证: Bearer Token (JWT)
响应: JSON
"""

import asyncio
import logging
import math
import re
import time
import urllib.parse
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

# ── 常量 ────────────────────────────────────────────────

BASE_URL = "https://5sim.net/v1"
DEFAULT_PRODUCT = "whatsapp"
DEFAULT_OPERATOR = "any"
DEFAULT_TIMEOUT = 30

# ── 国家中文名 ─────────────────────────────────────────

COUNTRY_CN = {
    "russia": "俄罗斯", "indonesia": "印度尼西亚", "thailand": "泰国",
    "england": "英国", "usa": "美国", "japan": "日本", "germany": "德国",
    "vietnam": "越南", "india": "印度", "china": "中国", "brazil": "巴西",
    "southafrica": "南非", "turkey": "土耳其", "ukraine": "乌克兰",
    "kazakhstan": "哈萨克斯坦", "philippines": "菲律宾", "myanmar": "缅甸",
}

COUNTRY_ALIASES = {
    "ru": "russia", "russianfederation": "russia", "俄罗斯": "russia", "俄罗斯联邦": "russia",
    "id": "indonesia", "印尼": "indonesia", "印度尼西亚": "indonesia",
    "th": "thailand", "泰国": "thailand",
    "gb": "england", "uk": "england", "unitedkingdom": "england", "英国": "england",
    "us": "usa", "unitedstates": "usa", "america": "usa", "美国": "usa",
    "jp": "japan", "日本": "japan",
    "de": "germany", "德国": "germany",
    "vn": "vietnam", "越南": "vietnam",
    "in": "india", "印度": "india",
    "cn": "china", "中国": "china",
    "br": "brazil", "巴西": "brazil",
    "za": "southafrica", "southafrica": "southafrica", "南非": "southafrica",
    "tr": "turkey", "土耳其": "turkey",
    "ua": "ukraine", "乌克兰": "ukraine",
    "kz": "kazakhstan", "哈萨克斯坦": "kazakhstan",
    "ph": "philippines", "菲律宾": "philippines",
    "mm": "myanmar", "缅甸": "myanmar",
}


def normalize_country_id(value: Any, fallback: str = "") -> str:
    """归一化国家 ID（纯小写字母数字）。"""
    raw = str(value or "").strip()
    raw_lower = raw.lower()
    alias = COUNTRY_ALIASES.get(raw_lower) or COUNTRY_ALIASES.get(re.sub(r"[^a-z0-9]+", "", raw_lower))
    if alias:
        return alias

    from app.countries import cc_to_iso, iso_to_name, name_to_iso

    iso_hint = ""
    if raw_lower.startswith("+"):
        cc_digits = re.sub(r"\D", "", raw_lower)
        if cc_digits:
            iso_hint = cc_to_iso(cc_digits)
    elif raw_lower:
        iso_hint = name_to_iso(raw)
    if iso_hint:
        mapped = COUNTRY_ALIASES.get(iso_hint)
        if mapped:
            return mapped
        en_name = iso_to_name(iso_hint, "en")
        slug = re.sub(r"[^a-z0-9]+", "", en_name.lower())
        if slug:
            return COUNTRY_ALIASES.get(slug, slug)

    s = re.sub(r"[^a-z0-9_-]+", "", raw_lower)
    return s or re.sub(r"[^a-z0-9_-]+", "", str(fallback if fallback else "").strip().lower())


def format_country_label(country_id: str, english: str = "") -> str:
    """格式化国家显示名：中文 (English)。"""
    cn = COUNTRY_CN.get(country_id, "")
    en = english or country_id
    if cn and cn.lower() != en.lower():
        return f"{cn} ({en})"
    return cn or en


def extract_verification_code(text: str) -> str:
    """从响应中提取验证码（智能过滤分钟数、号码与签名哈希）。"""
    return extract_otp_code(text)


def _normalize_product_slug(value: Any, fallback: str = DEFAULT_PRODUCT) -> str:
    """归一化 5sim 产品标识，绝不套用国家别名映射（防止 in->india, mm->myanmar）。"""
    raw = str(value or "").strip()
    if raw:
        s = re.sub(r"[^a-z0-9_-]+", "", raw.lower())
        if not s:
            raise ValueError(f"无效的 5sim 服务代码: {raw}")
        return s
    return re.sub(r"[^a-z0-9_-]+", "", str(fallback or DEFAULT_PRODUCT).strip().lower())


class FiveSimProvider(BaseSmsProvider):
    """5sim API 封装。"""

    provider_id = "5sim"

    def __init__(self, api_key_getter, base_url: str = BASE_URL, *, country_cache_ttl: float = 1800.0):
        self._api_key_getter = api_key_getter
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._country_cache_ttl = max(0.0, float(country_cache_ttl))
        self._countries_cache: list[dict] = []
        self._countries_fetched_at: float = 0.0
        self._countries_generation: int = 0
        self._countries_lock = asyncio.Lock()

    def invalidate_countries_cache(self) -> None:
        self._countries_generation += 1
        self._countries_cache = []
        self._countries_fetched_at = 0.0

    async def close(self):
        await self._client.aclose()

    async def _api_key(self) -> str:
        key = await self._api_key_getter()
        if not key:
            raise SmsProviderError("5sim API Key 未配置，请先在配置页设置")
        return key

    async def _get(self, path: str, require_auth: bool = True) -> Any:
        """发起 GET 请求并返回 JSON。"""
        url = f"{self._base_url}{path}"
        headers = {"Accept": "application/json"}
        if require_auth:
            headers["Authorization"] = f"Bearer {await self._api_key()}"

        try:
            resp = await self._client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise SmsProviderError("5sim 请求超时") from exc
        except httpx.RequestError as exc:
            raise SmsProviderError("5sim 连接失败，请检查网络") from exc

        if not resp.is_success:
            msg = ""
            try:
                err_payload = resp.json()
                if isinstance(err_payload, dict):
                    msg = str(err_payload.get("message") or err_payload.get("error") or "")
            except Exception:
                pass
            if not msg:
                msg = resp.text.strip()[:200] or str(resp.status_code)
            self._raise_api_error(resp.status_code, msg)

        try:
            payload = resp.json()
        except Exception as exc:
            raise SmsProviderError(f"5sim 返回非 JSON: {resp.text[:200]}") from exc

        return payload

    def _raise_api_error(self, status: int, msg: str):
        """根据状态码和消息抛出统一异常。"""
        msg_lower = str(msg).lower()
        if "balance" in msg_lower and ("not enough" in msg_lower or "insufficient" in msg_lower):
            raise BalanceError(f"5sim 余额不足: {msg}")
        if "no free" in msg_lower or "no numbers" in msg_lower:
            raise NoNumbersError(f"5sim 无可用号码: {msg}")
        if "not found" in msg_lower:
            raise SmsExpiredError(f"5sim 未找到: {msg}")
        if status == 429 or "rate limit" in msg_lower:
            raise SmsProviderError(f"5sim 限流: {msg}")
        raise SmsProviderError(f"5sim API 错误 ({status}): {msg}")

    # ── 核心接口实现 ──────────────────────────────────

    async def get_balance(self) -> float:
        payload = await self._get("/user/profile")
        if not isinstance(payload, dict):
            raise SmsProviderError("5sim 余额响应格式异常")
        bal = float(payload.get("balance", 0))
        if not math.isfinite(bal):
            raise SmsProviderError("5sim 余额数值无效")
        return bal

    async def resolve_country(self, country: str) -> tuple[Any, str]:
        """5sim 国家 ID 是字符串（如 philippines），不是数字。"""
        raw = str(country or "").strip()
        if not raw or raw.isdigit():
            raise ValueError(f"无效的 5sim 国家: {country}")
        cid = normalize_country_id(raw, "")
        if not cid:
            raise ValueError(f"无效的 5sim 国家: {country}")
        label = COUNTRY_CN.get(cid, cid)
        return cid, label

    async def get_countries(self, *, force: bool = False) -> list[dict]:
        """获取国家列表，统一格式 {id, name, name_zh, cc, iso}，带 TTL 内存缓存。"""
        now = time.monotonic()
        if (
            not force
            and self._countries_cache
            and self._country_cache_ttl > 0
            and (now - self._countries_fetched_at) < self._country_cache_ttl
        ):
            return list(self._countries_cache)

        async with self._countries_lock:
            now = time.monotonic()
            if (
                not force
                and self._countries_cache
                and self._country_cache_ttl > 0
                and (now - self._countries_fetched_at) < self._country_cache_ttl
            ):
                return list(self._countries_cache)

            if force:
                self._countries_generation += 1
            gen = self._countries_generation
            payload = await self._get("/guest/countries", require_auth=False)
            from app.countries import cc_to_iso, iso_to_name, name_to_iso

            countries = []
            for slug, entry in (payload or {}).items():
                cid = re.sub(r"[^a-z0-9_-]+", "", str(slug or "").strip().lower())
                if not cid:
                    continue
                te = (entry or {}).get("text_en", "") if isinstance(entry, dict) else ""
                name = te or cid
                prefix = (entry or {}).get("prefix", {}) if isinstance(entry, dict) else {}
                cc = ""
                if isinstance(prefix, dict):
                    for k in prefix:
                        digits = re.sub(r"\D", "", str(k))
                        if digits:
                            cc = digits
                            break
                iso = ""
                if name:
                    iso = name_to_iso(name)
                if not iso and cc:
                    iso = cc_to_iso(cc)
                name_zh = COUNTRY_CN.get(cid) or (iso_to_name(iso, "zh") if iso else "")
                countries.append({
                    "id": cid,
                    "name": name,
                    "name_zh": name_zh,
                    "cc": cc if cc else "",
                    "iso": iso,
                })
            countries.sort(key=lambda c: c["name"])
            if countries and gen == self._countries_generation:
                self._countries_cache = countries
                self._countries_fetched_at = time.monotonic()
            return list(countries)

    async def get_prices(self, country_id: Any, product: str = DEFAULT_PRODUCT) -> list[dict]:
        cid = normalize_country_id(country_id, "")
        if not cid:
            raise ValueError(f"无效的 5sim 国家: {country_id}")
        prod = _normalize_product_slug(product, DEFAULT_PRODUCT)
        qs = urllib.parse.urlencode({"country": cid, "product": prod})
        payload = await self._get(f"/guest/prices?{qs}", require_auth=False)
        prices = []
        self._collect_prices(payload, prices)
        return prices

    def _collect_prices(self, payload: Any, entries: list):
        """递归遍历价格 JSON 树，提取价格条目。"""
        if isinstance(payload, dict):
            raw_cost = next((payload.get(k) for k in ("cost", "Price", "price") if payload.get(k) is not None), None)
            cost = self._normalize_price(raw_cost)
            if cost is not None:
                raw_cnt = next((payload.get(k) for k in ("count", "Qty", "qty", "quantity") if payload.get(k) is not None), 0)
                try:
                    num_cnt = float(raw_cnt)
                    count = max(0, int(num_cnt)) if math.isfinite(num_cnt) else 0
                except (TypeError, ValueError, OverflowError):
                    count = 1 if isinstance(raw_cnt, str) and raw_cnt.strip().lower() == "few" else 0
                entries.append({
                    "cost": cost,
                    "count": count,
                    "in_stock": count > 0,
                })
                return
            for v in payload.values():
                self._collect_prices(v, entries)
        elif isinstance(payload, list):
            for item in payload:
                self._collect_prices(item, entries)

    def _normalize_price(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            p = float(value)
            return round(p, 4) if math.isfinite(p) and p > 0 else None
        except (ValueError, TypeError, OverflowError):
            return None

    async def request_number(self, country_id: Any, service: str = DEFAULT_PRODUCT,
                              price_max: Optional[float] = None) -> Activation:
        cid = normalize_country_id(country_id, "")
        if not cid:
            raise ValueError(f"无效的 5sim 国家: {country_id}")

        product = _normalize_product_slug(service, DEFAULT_PRODUCT)
        path = f"/user/buy/activation/{cid}/{DEFAULT_OPERATOR}/{product}"

        query = {}
        if price_max is not None:
            query["maxPrice"] = str(round(price_max, 4))

        url = f"{self._base_url}{path}"
        if query:
            import urllib.parse
            url += "?" + urllib.parse.urlencode(query)

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {await self._api_key()}",
        }

        try:
            resp = await self._client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise PurchaseUncertainError("5sim 取号结果未知，已停止自动重试以避免重复扣费") from exc
        except httpx.RequestError as exc:
            raise PurchaseUncertainError(f"5sim 取号连接中断，结果未知，已停止自动重试以避免重复扣费: {exc}") from exc

        if not resp.is_success:
            text = resp.text.strip()
            msg = text[:100]
            if resp.status_code >= 500:
                raise PurchaseUncertainError(
                    f"5sim 取号遇到服务端异常 (HTTP {resp.status_code})，结果未知，已停止自动重试以避免重复扣费"
                )
            if "country" in msg.lower() and "incorrect" in msg.lower():
                raise NoNumbersError(f"5sim 该国家无可用产品: {msg}")
            self._raise_api_error(resp.status_code, msg)

        # 5sim 有时返回纯文本（如"no free phones"）；但空响应或非法 JSON 可能是网关截断，按不确定购买处理防重复扣费！
        ct = resp.headers.get("content-type", "")
        if "application/json" not in ct and not ct.startswith("application/json"):
            text = resp.text.strip()
            if not text or text.lstrip().startswith("<"):
                raise PurchaseUncertainError("5sim 取号返回空响应或网关 HTML，结果未知，已停止自动重试以避免重复扣费")
            self._raise_api_error(resp.status_code, text[:200])

        try:
            payload = resp.json()
        except Exception as exc:
            raise PurchaseUncertainError(f"5sim 取号响应解析失败，结果未知，已停止自动重试以避免重复扣费: {exc}") from exc
        if not isinstance(payload, dict):
            raise PurchaseUncertainError("5sim 取号返回非对象响应，结果未知，已停止自动重试以避免重复扣费")
        activation_id = str(payload.get("id", "")).strip()
        phone = str(payload.get("phone", "")).strip()
        if not activation_id or not phone:
            raise NoNumbersError(f"5sim 取号返回空: {resp.text[:200]}")

        raw_price = payload.get("price", payload.get("cost"))
        actual_price = self._normalize_price(raw_price)
        raw_ccy = str(payload.get("currency") or "").strip().upper()
        if raw_ccy:
            act_ccy = raw_ccy
            act_ccy_source = "provider_response"
        else:
            act_ccy = "USD"
            act_ccy_source = "provider_default"
        return Activation(
            activation_id=activation_id,
            phone_number=phone,
            country_id=cid,
            country_label=format_country_label(cid, country_id if isinstance(country_id, str) else ""),
            provider=self.provider_id,
            price=actual_price if actual_price is not None else price_max,
            price_is_estimated=actual_price is None and price_max is not None,
            currency=act_ccy,
            currency_source=act_ccy_source,
        )

    async def get_sms_code(self, activation_id: str) -> tuple[str, str]:
        """查码。返回 (status, code_or_empty)，严格区分已完成(FINISHED)、取消(CANCEL)与过期(EXPIRED)（修复 v6 复核 P1-04）。"""
        payload = await self._get(f"/user/check/{activation_id}")
        status = str((payload or {}).get("status", "")).strip().lower()
        sms_list = (payload or {}).get("sms", []) or []

        # 5sim 返回 sms 数组，优先取上游预解析的 sms["code"]，避免拼接 sms["text"] 中的有效分钟数等无关数字
        code = ""
        raw_sms_text = ""
        for sms in reversed(sms_list):
            if not isinstance(sms, dict):
                continue
            raw_sms_text = str(sms.get("text") or sms.get("code") or "")
            code = extract_verification_code(str(sms.get("code") or ""))
            if not code:
                code = extract_verification_code(str(sms.get("text") or ""))
            if code:
                break

        if code:
            self.record_raw_sms(activation_id, raw_sms_text or code)
            return ("OK", code)
        if status in {"finish", "finished"}:
            # 5sim FINISH 代表上游已结束而非退款取消，绝不可误报 CANCEL 导致误记已确认退款
            return ("FINISHED", "")
        if status in {"cancelled", "canceled"}:
            return ("CANCEL", "")
        if status in {"expired", "timeout", "banned"}:
            return ("EXPIRED", "")
        return ("WAIT", "")

    async def cancel_activation(self, activation_id: str) -> str:
        try:
            payload = await self._get(f"/user/cancel/{activation_id}")
            return str((payload or {}).get("status", ""))
        except SmsExpiredError:
            return "EXPIRED"
        except SmsProviderError as e:
            if "not found" in str(e).lower():
                return "NOT_FOUND"
            raise

    async def cancel_activation_structured(self, activation_id: str):
        """5sim 结构化取消：仅上游显式返回 CANCELED/CANCELLED 才确认退款；FINISH/EXPIRED/TIMEOUT/BANNED/NOT_FOUND 绝不误判为退款确认。"""
        from app.providers.base import CancelOutcome, parse_cancel_outcome

        try:
            payload = await self._get(f"/user/cancel/{activation_id}")
        except SmsExpiredError as exc:
            return CancelOutcome(
                settled=True,
                status="not_found_or_expired",
                retryable=False,
                raw=str(exc) or "EXPIRED",
                refund_confirmed=False,
            )
        except SmsProviderError as exc:
            msg = str(exc)
            msg_low = msg.lower()
            if "not found" in msg_low:
                return CancelOutcome(
                    settled=True,
                    status="not_found_or_expired",
                    retryable=False,
                    raw=msg or "NOT_FOUND",
                    refund_confirmed=False,
                )
            if "order has sms" in msg_low or "already finished" in msg_low:
                return CancelOutcome(
                    settled=True,
                    status="already_completed",
                    retryable=False,
                    raw=msg,
                    refund_confirmed=False,
                )
            # 当取消报错不确定（例如 400 状态冲突）时复核 check 接口，确认是否已取消/已收码/已过期
            try:
                chk = await self._get(f"/user/check/{activation_id}")
                if isinstance(chk, dict):
                    chk_st = str(chk.get("status") or "").strip().upper()
                    chk_sms = chk.get("sms") or []
                    if chk_st in {"CANCELED", "CANCELLED"}:
                        return CancelOutcome(
                            settled=True,
                            status="refund_confirmed",
                            retryable=False,
                            raw=f"{msg} (check={chk_st})",
                            refund_confirmed=True,
                        )
                    if chk_st in {"FINISH", "FINISHED"} or chk_sms:
                        return CancelOutcome(
                            settled=True,
                            status="already_completed",
                            retryable=False,
                            raw=f"{msg} (check={chk_st or 'HAS_SMS'})",
                            refund_confirmed=False,
                        )
                    if chk_st in {"EXPIRED", "TIMEOUT", "BANNED"}:
                        return CancelOutcome(
                            settled=True,
                            status="not_found_or_expired",
                            retryable=False,
                            raw=f"{msg} (check={chk_st})",
                            refund_confirmed=False,
                        )
            except Exception:
                pass
            return CancelOutcome(settled=False, status="error", retryable=True, raw=msg, refund_confirmed=False)
        except Exception as exc:
            return CancelOutcome(settled=False, status="error", retryable=True, raw=str(exc), refund_confirmed=False)

        raw_status = str((payload or {}).get("status", "")).strip()
        upper = raw_status.upper()
        if upper in {"CANCELED", "CANCELLED"}:
            return CancelOutcome(
                settled=True,
                status="refund_confirmed",
                retryable=False,
                raw=raw_status,
                refund_confirmed=True,
                refund_amount=None,
            )
        if upper in {"FINISH", "FINISHED"}:
            return CancelOutcome(
                settled=True,
                status="already_completed",
                retryable=False,
                raw=raw_status,
                refund_confirmed=False,
            )
        if upper in {"EXPIRED", "TIMEOUT", "BANNED"}:
            return CancelOutcome(
                settled=True,
                status="not_found_or_expired",
                retryable=False,
                raw=raw_status,
                refund_confirmed=False,
            )
        return parse_cancel_outcome(raw_status)

    async def complete_activation(self, activation_id: str) -> str:
        payload = await self._get(f"/user/finish/{activation_id}")
        if not isinstance(payload, dict):
            raise SmsProviderError("5sim 完成激活响应格式异常")
        status = str(payload.get("status", "")).strip()
        if status.upper() not in {"FINISH", "FINISHED"}:
            raise SmsProviderError(f"5sim 完成激活状态异常: {status or 'EMPTY'}")
        return status

    async def complete_activation_structured(self, activation_id: str):
        from app.providers.base import ActionOutcome, parse_complete_outcome
        try:
            payload = await self._get(f"/user/finish/{activation_id}")
            raw = str((payload or {}).get("status", "")) if isinstance(payload, dict) else ""
            return parse_complete_outcome(raw)
        except SmsExpiredError as exc:
            return ActionOutcome(ok=False, status="rejected", retryable=False, raw=str(exc) or "NOT_FOUND")
        except Exception as exc:
            return ActionOutcome(ok=False, status="error", retryable=True, raw=str(exc))

    async def poll_sms_code(self, activation_id: str, timeout: int = 180) -> str:
        """阶梯轮询等码，返回纯数字验证码。"""
        start = asyncio.get_running_loop().time()

        while True:
            elapsed = asyncio.get_running_loop().time() - start
            if elapsed >= timeout:
                raise SmsTimeoutError(f"5sim 等码超时 ({timeout}s)")

            try:
                status, code = await self.get_sms_code(activation_id)
            except SmsExpiredError as e:
                raise SmsExpiredError(f"5sim 激活已失效: {e}")
            except SmsCancelledError:
                raise

            if status == "OK" and code:
                return code
            if status == "CANCEL":
                raise SmsCancelledError("5sim 激活已被取消")
            if status in {"EXPIRED", "FINISHED"}:
                raise SmsExpiredError(f"5sim 激活已结束 ({status})")

            elapsed = asyncio.get_running_loop().time() - start
            if elapsed >= timeout:
                raise SmsTimeoutError(f"5sim 等码超时 ({timeout}s)")

            delay = 3 if elapsed < 36 else (5 if elapsed < 96 else 8)
            await asyncio.sleep(delay)

    async def normalize_phone(self, raw_phone: str, country_id: Any) -> "PhoneInput":
        """5sim 号码格式：国际格式直接 + 前缀。"""
        from app.phone_normalizer import build_phone_input, resolve_country_metadata

        rows = []
        try:
            rows = await self.get_countries()
        except Exception:
            rows = []

        normalized_country = normalize_country_id(country_id, "")
        label = COUNTRY_CN.get(normalized_country, normalized_country or str(country_id or ""))
        meta = resolve_country_metadata(country_id, country_label=label, countries=rows)
        return build_phone_input(
            raw_phone,
            country_calling_code=meta.get("calling_code", ""),
            country_iso2=meta.get("iso2", ""),
            country_label=meta.get("name", "") or label,
        )
