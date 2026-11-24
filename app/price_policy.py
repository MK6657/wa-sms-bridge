"""Price and polling policy helpers for registration tasks."""

from __future__ import annotations

from typing import Any, Optional


PRICE_EPSILON = 0.0001


def _positive_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def effective_price_max(
    *,
    target_price: Any = None,
    price_max: Any = None,
    price_lock_enabled: bool = False,
    price_float_enabled: bool = False,
    price_float_percent: Any = 0,
) -> Optional[float]:
    target = _positive_float(target_price)
    fallback_max = _positive_float(price_max)
    if target is None:
        return round(fallback_max, 4) if fallback_max is not None else None
    if price_lock_enabled:
        return round(target, 4)
    if price_float_enabled:
        try:
            percent = max(0.0, float(price_float_percent or 0))
        except (TypeError, ValueError):
            percent = 0.0
        return round(target * (1 + percent / 100), 4)
    return round(target, 4)


def locked_price_available(entries: list[dict[str, Any]], target_price: Any) -> tuple[bool, int]:
    target = _positive_float(target_price)
    if target is None:
        return True, 0
    stock = 0
    for entry in entries or []:
        try:
            cost = float(entry.get("cost"))
            count = int(float(entry.get("count") or 0))
        except (TypeError, ValueError):
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
            return max(0.1, min(float(request_interval), 3600.0))
        except (TypeError, ValueError):
            return 1.0
    return 1.0 if continuous_poll else 0.0
