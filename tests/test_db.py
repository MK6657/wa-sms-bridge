import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db import Database


@pytest.mark.asyncio
async def test_cancelled_write_rolls_back_before_next_writer_runs():
    db = Database(":memory:")
    connection = MagicMock()
    sequence = []
    first_commit_started = asyncio.Event()
    block_first_commit = asyncio.Event()
    commit_count = 0

    async def execute(sql, params=()):
        sequence.append(f"execute:{sql}")
        return MagicMock()

    async def commit():
        nonlocal commit_count
        commit_count += 1
        sequence.append(f"commit:{commit_count}")
        if commit_count == 1:
            first_commit_started.set()
            await block_first_commit.wait()

    async def rollback():
        sequence.append("rollback")

    connection.execute = AsyncMock(side_effect=execute)
    connection.commit = AsyncMock(side_effect=commit)
    connection.rollback = AsyncMock(side_effect=rollback)
    db._conn = connection

    first = asyncio.create_task(db._write("A"))
    await asyncio.wait_for(first_commit_started.wait(), timeout=1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    await db._write("B")

    assert sequence == [
        "execute:A",
        "commit:1",
        "rollback",
        "execute:B",
        "commit:2",
    ]


@pytest.mark.asyncio
async def test_success_ledger_updates_task_and_stats_atomically(tmp_path):
    db = Database(str(tmp_path / "bridge.db"))
    await db.open()
    try:
        await db.create_task(
            "task_stats",
            "31",
            "South Africa",
            provider="smsbower",
            target_success_count=2,
        )
        await db.record_registration_success(
            task_id="task_stats",
            phone="+27600000000",
            country_id="31",
            country_label="South Africa",
            activation_id="act_stats",
            wa_account_id="wa_stats",
            sms_code="123456",
            cost=0.11,
            provider="smsbower",
            verification_request_id="vreq_stats",
            success_count=1,
            target_success_count=2,
            cost_is_estimated=True,
        )

        task = await db.get_task("task_stats")
        stats = await db.get_stats(0)
        history = await db.list_history()

        assert task["successes_done"] == 1
        assert task["target_success_count"] == 2
        assert task["cost_total"] == pytest.approx(0.11)
        assert stats["success_count"] == 1
        assert stats["cost_total"] == pytest.approx(0.11)
        assert stats["estimated_count"] == 1
        assert history[0]["sms_code"] == ""
    finally:
        await db.close()
