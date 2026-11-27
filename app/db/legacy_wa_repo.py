"""原有 WhatsApp 自动注册插件所需的 task_state / registration_history 仓储。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db.connection import DatabaseConnection

logger = logging.getLogger(__name__)


ALLOWED_TASK_STATE_FIELDS = frozenset({
    "status",
    "provider",
    "country_id",
    "country_label",
    "activation_id",
    "phone_number",
    "order_id",
    "wa_account_id",
    "verification_request_id",
    "wa_status",
    "registration_phase",
    "failure_category",
    "failure_code",
    "step",
    "error",
    "attempts_used",
    "successes_done",
    "target_success_count",
    "cost_total",
    "account_commit_state",
    "attempt_request_id",
    "sms_finalize_state",
    "reconciliation_required",
    "reconciliation_error",
    "activation_compensated",
    "account_compensated",
    "completed_at",
})


class LegacyWaRepo:
    def __init__(self, conn: DatabaseConnection):
        self._conn = conn

    async def create_task(
        self,
        task_id: str,
        country_id: Any,
        country_label: str,
        provider: str = "smsbower",
        target_success_count: int = 1,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.write(
            "INSERT INTO task_state (task_id, status, provider, country_id, country_label, "
            "target_success_count, created_at, updated_at) "
            "VALUES (?, 'running', ?, ?, ?, ?, ?, ?)",
            (task_id, provider, country_id, country_label, max(1, int(target_success_count)), now, now),
        )
        task = await self.get_task(task_id)
        assert task is not None
        return task

    async def update_task(self, task_id: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        for k in kwargs:
            if k not in ALLOWED_TASK_STATE_FIELDS:
                raise ValueError(f"Unsupported task_state column: {k}")
        now = datetime.now(timezone.utc).isoformat()
        sets = [f"{k}=?" for k in kwargs]
        values = list(kwargs.values())
        sets.append("updated_at=?")
        values.append(now)
        values.append(task_id)
        await self._conn.write(f"UPDATE task_state SET {', '.join(sets)} WHERE task_id=?", tuple(values))

    async def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        return await self._conn.fetchone("SELECT * FROM task_state WHERE task_id=?", (task_id,))

    async def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._conn.fetchall(
            "SELECT * FROM task_state ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    async def get_pending_tasks(self) -> list[dict[str, Any]]:
        return await self._conn.fetchall(
            "SELECT * FROM task_state WHERE status IN ('running', 'compensation_pending') ORDER BY created_at ASC"
        )

    async def save_history(
        self,
        task_id: str,
        phone: str,
        country_id: Any,
        country_label: str,
        activation_id: str,
        wa_account_id: str,
        sms_code: str,
        cost: Optional[float],
        provider: str = "smsbower",
        verification_request_id: str = "",
        cost_is_estimated: bool = False,
        currency: str = "USD",
    ) -> None:
        await self._conn.write(
            "INSERT INTO registration_history (task_id, provider, phone, country_id, country_label, "
            "activation_id, wa_account_id, verification_request_id, sms_code, status, cost, "
            "cost_known, cost_is_estimated, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)",
            (
                task_id, provider, phone, country_id, country_label, activation_id,
                wa_account_id, verification_request_id, "", float(cost or 0),
                int(cost is not None), int(cost_is_estimated), currency or "USD",
            ),
        )

    async def record_registration_success(
        self,
        *,
        task_id: str,
        phone: str,
        country_id: Any,
        country_label: str,
        activation_id: str,
        wa_account_id: str,
        sms_code: str,
        cost: Optional[float],
        provider: str,
        verification_request_id: str,
        success_count: int,
        target_success_count: int,
        cost_is_estimated: bool = False,
        currency: str = "USD",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        numeric_cost = round(float(cost or 0), 4)
        async with self._conn.transaction() as raw:
            await raw.execute(
                "INSERT INTO registration_history (task_id, provider, phone, country_id, country_label, "
                "activation_id, wa_account_id, verification_request_id, sms_code, status, cost, "
                "cost_known, cost_is_estimated, currency) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)",
                (
                    task_id, provider, phone, country_id, country_label, activation_id,
                    wa_account_id, verification_request_id, "", numeric_cost,
                    int(cost is not None), int(cost_is_estimated), currency or "USD",
                ),
            )
            await raw.execute(
                "UPDATE task_state SET successes_done=?, target_success_count=?, "
                "cost_total=ROUND(cost_total+?, 4), account_commit_state='committed', "
                "wa_account_id=?, verification_request_id=?, updated_at=? WHERE task_id=?",
                (
                    int(success_count), int(target_success_count), numeric_cost,
                    wa_account_id, verification_request_id, now, task_id,
                ),
            )

    async def list_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._conn.fetchall(
            "SELECT * FROM registration_history ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    async def get_stats(self, timezone_offset_minutes: int = 0) -> dict[str, Any]:
        offset = max(-840, min(840, int(timezone_offset_minutes or 0)))
        now_utc = datetime.now(timezone.utc)
        local_now = now_utc - timedelta(minutes=offset)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = local_start + timedelta(minutes=offset)
        end_utc = start_utc + timedelta(days=1)
        start_text = start_utc.strftime("%Y-%m-%d %H:%M:%S")
        end_text = end_utc.strftime("%Y-%m-%d %H:%M:%S")
        rows = await self._conn.fetchall(
            "SELECT provider, COUNT(*) AS success_count, "
            "SUM(CASE WHEN cost_known=1 THEN cost ELSE 0 END) AS cost_total, "
            "AVG(CASE WHEN cost_known=1 THEN cost END) AS avg_cost, "
            "SUM(CASE WHEN cost_is_estimated=1 THEN 1 ELSE 0 END) AS estimated_count "
            "FROM registration_history WHERE created_at>=? AND created_at<? "
            "GROUP BY provider",
            (start_text, end_text),
        )
        providers = {
            row["provider"]: {
                "success_count": int(row["success_count"] or 0),
                "cost_total": round(float(row["cost_total"] or 0), 4),
                "avg_cost": round(float(row["avg_cost"]), 4) if row["avg_cost"] is not None else None,
                "estimated_count": int(row["estimated_count"] or 0),
            }
            for row in rows
        }
        return {
            "timezone_offset_minutes": offset,
            "day_start_utc": start_utc.isoformat(),
            "providers": providers,
            "success_count": sum(item["success_count"] for item in providers.values()),
            "cost_total": round(sum(item["cost_total"] for item in providers.values()), 4),
            "estimated_count": sum(item["estimated_count"] for item in providers.values()),
        }

    async def cancel_stale_tasks(self) -> int:
        stale = await self._conn.fetchall("SELECT task_id, activation_id FROM task_state WHERE status='running'")
        if not stale:
            return 0
        for row in stale:
            logger.warning(
                "recovery: marking stale task %s as cancelled (activation=%s)",
                row["task_id"],
                row["activation_id"],
            )
        now = datetime.now(timezone.utc).isoformat()
        cur = await self._conn.write(
            "UPDATE task_state SET status='cancelled', error='container restart recovery', updated_at=? "
            "WHERE status='running'",
            (now,),
        )
        return int(cur.rowcount or len(stale))
