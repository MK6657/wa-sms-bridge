from app.price_policy import effective_price_max, locked_price_available, polling_interval_seconds


def test_effective_price_max_lock_ignores_float():
    assert effective_price_max(
        target_price=0.1,
        price_lock_enabled=True,
        price_float_enabled=True,
        price_float_percent=20,
    ) == 0.1


def test_effective_price_max_float_when_enabled():
    assert effective_price_max(
        target_price=0.1,
        price_float_enabled=True,
        price_float_percent=10,
    ) == 0.11


def test_effective_price_max_keeps_backwards_price_max():
    assert effective_price_max(price_max=0.23) == 0.23


def test_locked_price_available_requires_matching_stock():
    entries = [{"cost": 0.1, "count": 0}, {"cost": 0.11, "count": 9}]
    assert locked_price_available(entries, 0.1) == (False, 0)
    assert locked_price_available(entries, 0.11) == (True, 9)


def test_polling_interval_defaults():
    assert polling_interval_seconds(continuous_poll=False) == 0
    assert polling_interval_seconds(continuous_poll=True) == 1.0
    assert polling_interval_seconds(request_interval_enabled=True, request_interval=0.01) == 0.1
