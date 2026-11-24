import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.country_index import GlobalCountryIndex
from app.providers.registry import registry


@pytest.mark.asyncio
async def test_concurrent_refresh_waits_for_inflight_result(monkeypatch):
    index = GlobalCountryIndex()
    started = asyncio.Event()
    release = asyncio.Event()

    async def get_countries():
        started.set()
        await release.wait()
        return [{"id": 31, "name": "South Africa", "iso": "za", "cc": "27"}]

    provider = MagicMock()
    provider.get_countries = AsyncMock(side_effect=get_countries)
    monkeypatch.setattr(registry, "list_providers", lambda: ["smsbower"])
    monkeypatch.setattr(registry, "get", lambda name: provider)

    first = asyncio.create_task(index.refresh(force=True))
    await asyncio.wait_for(started.wait(), timeout=1)
    second = asyncio.create_task(index.refresh(force=True))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert provider.get_countries.await_count == 1
    assert index.get_countries("smsbower") == [{
        "iso": "za",
        "name": "South Africa",
        "name_zh": "",
        "cc": "27",
        "id": 31,
        "aliases": [],
        "available": True,
    }]


@pytest.mark.asyncio
async def test_country_index_preserves_chinese_aliases_and_provider_ids(monkeypatch):
    index = GlobalCountryIndex()
    provider = MagicMock()
    provider.get_countries = AsyncMock(return_value=[{
        "id": 89,
        "name": "Tunisia",
        "name_zh": "突尼斯",
        "iso": "tn",
        "cc": "216",
        "aliases": ["Tunisia", "突尼斯", "tn", "89", "216"],
    }])
    monkeypatch.setattr(registry, "list_providers", lambda: ["smsbower"])
    monkeypatch.setattr(registry, "get", lambda name: provider)

    await index.refresh(force=True)

    country = index.get_countries("smsbower")[0]
    assert country["name_zh"] == "突尼斯"
    assert country["id"] == 89
    assert index.search("突尼斯")[0]["iso"] == "tn"
    assert index.search("89")[0]["iso"] == "tn"


@pytest.mark.asyncio
async def test_failed_provider_keeps_its_last_successful_mapping(monkeypatch):
    index = GlobalCountryIndex()
    provider = MagicMock()
    provider.get_countries = AsyncMock(return_value=[
        {"id": 31, "name": "South Africa", "iso": "za", "cc": "27"},
    ])
    monkeypatch.setattr(registry, "list_providers", lambda: ["smsbower"])
    monkeypatch.setattr(registry, "get", lambda name: provider)

    await index.refresh(force=True)
    provider.get_countries.side_effect = RuntimeError("temporary outage")
    await index.refresh(force=True)

    countries = index.get_countries("smsbower")
    assert len(countries) == 1
    assert countries[0]["id"] == 31


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_start_duplicate_refresh(monkeypatch):
    index = GlobalCountryIndex()
    started = asyncio.Event()
    release = asyncio.Event()

    async def get_countries():
        started.set()
        await release.wait()
        return [{"id": 31, "name": "South Africa", "iso": "za", "cc": "27"}]

    provider = MagicMock()
    provider.get_countries = AsyncMock(side_effect=get_countries)
    monkeypatch.setattr(registry, "list_providers", lambda: ["smsbower"])
    monkeypatch.setattr(registry, "get", lambda name: provider)

    first = asyncio.create_task(index.refresh(force=True))
    await asyncio.wait_for(started.wait(), timeout=1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(index.refresh(force=True))
    await asyncio.sleep(0)
    assert provider.get_countries.await_count == 1
    release.set()
    await second
