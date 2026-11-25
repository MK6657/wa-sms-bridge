"""Price and polling policy helpers for SMS acquisition & registration tasks.

修复 P1-05 & 复核 P1-03：
1. 明确拒绝非正价格（如 price_max=0 / target_price=0），绝不静默省去价格上限；
2. 严禁向上四舍五入突破原始 price_max（使用 Decimal ROUND_DOWN 向下截断到 4 位小数）；
3. target_price > price_max 零容差报错；浮动结果受 price_max 硬顶向下截断约束。
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Optional


PRICE_EPSILON = 0.0001
_QUANT_4 = Decimal("0.0001")


def _parse_decimal(value: Any, *, field_name: str, reject_non_positive: bool = True) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是有效数字") from exc
    if not dec.is_finite():
        raise ValueError(f"{field_name} 必须是有限数字")
    if dec <= 0:
        if reject_non_positive:
            raise ValueError(f"{field_name} 必须大于 0（禁止传 0 绕过价格上限）")
        return None
    return dec


def _floor_4(dec: Decimal, *, field_name: str) -> float:
    floored = dec.quantize(_QUANT_4, rounding=ROUND_DOWN)
    if floored <= 0:
        raise ValueError(f"{field_name} ({dec}) 低于最小支持计价精度 0.0001")
    return float(floored)


def _positive_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def effective_price_max(
    *,
    target_price: Any = None,
    price_max: Any = None,
    price_lock_enabled: bool = False,
    price_float_enabled: bool = False,
    price_float_percent: Any = 0,
) -> Optional[float]:
    target = _parse_decimal(target_price, field_name="target_price", reject_non_positive=True)
    hard_cap = _parse_decimal(price_max, field_name="price_max", reject_non_positive=True)

    if target is None:
        return _floor_4(hard_cap, field_name="price_max") if hard_cap is not None else None

    if hard_cap is not None and target > hard_cap:
        raise ValueError(f"目标价格 target_price ({target}) 不能高于绝对价格上限 price_max ({hard_cap})")

    if price_lock_enabled:
        computed = target
    elif price_float_enabled:
        if price_float_percent is None or (isinstance(price_float_percent, str) and not price_float_percent.strip()):
            pct = Decimal("0")
        else:
            try:
                pct = Decimal(str(price_float_percent).strip())
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("price_float_percent 必须是有效数字") from exc
            if not pct.is_finite() or pct < 0:
                raise ValueError("price_float_percent 必须是大于等于 0 的有限数字")
        computed = target * (Decimal("1") + pct / Decimal("100"))
    else:
        computed = target

    if hard_cap is not None:
        final_dec = min(computed, hard_cap)
        return _floor_4(final_dec, field_name="price_max")
    return _floor_4(computed, field_name="target_price")


def locked_price_available(entries: list[dict[str, Any]], target_price: Any) -> tuple[bool, int]:
    if target_price is None:
        return True, 0
    target = _positive_float(target_price)
    if target is None:
        return False, 0
    stock = 0
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        try:
            cost = float(entry.get("cost"))  # type: ignore[arg-type]
            raw_cnt = float(entry.get("count") or 0)
            if not math.isfinite(cost) or not math.isfinite(raw_cnt):
                continue
            count = int(raw_cnt)
        except (TypeError, ValueError, OverflowError):
            continue
        if abs(cost - target) <= PRICE_EPSILON:
            stock += max(0, count)
    return stock > 0, stock


def polling_interval_seconds(
    *,
    request_interval_enabled: bool = False,
    request_interval: Any = None,
    continuous_poll: bool = False,
) -> float:
    if request_interval_enabled:
        try:
            val = float(request_interval)
            if not math.isfinite(val):
                return 1.0
            return max(0.1, min(val, 3600.0))
        except (TypeError, ValueError, OverflowError):
            return 1.0
    return 1.0 if continuous_poll else 0.0
