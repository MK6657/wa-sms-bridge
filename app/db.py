"""SQLite 持久化层：黑名单、配置表、任务状态、注册历史。"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiosqlite

from app.config import env

logger = logging.getLogger(__name__)

# ── Schema ─────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'smsbower',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(phone, provider)
);

CREATE TABLE IF NOT EXISTS task_state (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'running',
    provider TEXT NOT NULL DEFAULT 'smsbower',
    country_id TEXT NOT NULL DEFAULT '',
    country_label TEXT NOT NULL DEFAULT '',
    activation_id TEXT NOT NULL DEFAULT '',
    phone_number TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    wa_account_id TEXT NOT NULL DEFAULT '',
    verification_request_id TEXT NOT NULL DEFAULT '',
    wa_status TEXT NOT NULL DEFAULT '',
    registration_phase TEXT NOT NULL DEFAULT '',
    failure_category TEXT NOT NULL DEFAULT '',
    failure_code TEXT NOT NULL DEFAULT '',
    step TEXT NOT NULL DEFAULT 'init',
    error TEXT NOT NULL DEFAULT '',
    attempts_used INTEGER NOT NULL DEFAULT 0,
    successes_done INTEGER NOT NULL DEFAULT 0,
    target_success_count INTEGER NOT NULL DEFAULT 1,
    cost_total REAL NOT NULL DEFAULT 0,
    account_commit_state TEXT NOT NULL DEFAULT 'none',
    attempt_request_id TEXT NOT NULL DEFAULT '',
    sms_finalize_state TEXT NOT NULL DEFAULT '',
    reconciliation_required INTEGER NOT NULL DEFAULT 0,
    reconciliation_error TEXT NOT NULL DEFAULT '',
    activation_compensated INTEGER NOT NULL DEFAULT 0,
    account_compensated INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS registration_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'smsbower',
    phone TEXT NOT NULL,
    country_id TEXT NOT NULL DEFAULT '',
    country_label TEXT NOT NULL DEFAULT '',
    activation_id TEXT NOT NULL DEFAULT '',
    wa_account_id TEXT NOT NULL DEFAULT '',
    verification_request_id TEXT NOT NULL DEFAULT '',
    sms_code TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'completed',
    cost REAL NOT NULL DEFAULT 0,
    cost_known INTEGER NOT NULL DEFAULT 0,
    cost_is_estimated INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_state_status ON task_state(status);
CREATE INDEX IF NOT EXISTS idx_blacklist_phone ON blacklist(phone);
CREATE INDEX IF NOT EXISTS idx_history_phone ON registration_history(phone);
CREATE INDEX IF NOT EXISTS idx_history_created ON registration_history(created_at);
"""


class Database:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def open(self):
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

        # 迁移：为旧数据库添加 provider 列（如果缺失）
        for table in ["task_state", "blacklist"]:
            try:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN provider TEXT NOT NULL DEFAULT 'smsbower'")
                await self._conn.commit()
            except Exception:
                pass  # 列已存在

        # 迁移：为解耦适配层补充上游关联字段。
        for table, columns in {
            "task_state": [
                ("verification_request_id", "TEXT NOT NULL DEFAULT ''"),
                ("wa_status", "TEXT NOT NULL DEFAULT ''"),
                ("registration_phase", "TEXT NOT NULL DEFAULT ''"),
                ("failure_category", "TEXT NOT NULL DEFAULT ''"),
                ("failure_code", "TEXT NOT NULL DEFAULT ''"),
                ("successes_done", "INTEGER NOT NULL DEFAULT 0"),
                ("target_success_count", "INTEGER NOT NULL DEFAULT 1"),
                ("cost_total", "REAL NOT NULL DEFAULT 0"),
                ("account_commit_state", "TEXT NOT NULL DEFAULT 'none'"),
                ("attempt_request_id", "TEXT NOT NULL DEFAULT ''"),
                ("sms_finalize_state", "TEXT NOT NULL DEFAULT ''"),
                ("reconciliation_required", "INTEGER NOT NULL DEFAULT 0"),
                ("reconciliation_error", "TEXT NOT NULL DEFAULT ''"),
                ("activation_compensated", "INTEGER NOT NULL DEFAULT 0"),
                ("account_compensated", "INTEGER NOT NULL DEFAULT 0"),
                ("completed_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            "registration_history": [
                ("provider", "TEXT NOT NULL DEFAULT 'smsbower'"),
                ("verification_request_id", "TEXT NOT NULL DEFAULT ''"),
                ("cost_known", "INTEGER NOT NULL DEFAULT 0"),
                ("cost_is_estimated", "INTEGER NOT NULL DEFAULT 0"),
                ("currency", "TEXT NOT NULL DEFAULT 'USD'"),
            ],
        }.items():
            for column, definition in columns:
                try:
                    await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    await self._conn.commit()
                except Exception:
                    pass

        # 迁移：将旧数据 country_id 从整数转为字符串
        try:
            await self._conn.execute(
                "UPDATE task_state SET country_id = CAST(country_id AS TEXT) WHERE country_id != ''")
            await self._conn.commit()
        except Exception:
            pass
        try:
            await self._conn.execute(
                "UPDATE registration_history SET sms_code='' WHERE sms_code != ''")
            await self._conn.commit()
        except Exception:
            pass
        logger.info("database opened: %s (WAL mode)", self._db_path)

    async def _write(self, sql: str, params: tuple = ()):
        """Serialize execute+commit so cancellation cannot leak a transaction."""
        async with self._write_lock:
            try:
                cursor = await self._conn.execute(sql, params)
                await self._conn.commit()
                return cursor
            except BaseException:
                await self._conn.rollback()
                raise

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── Config（加密存储） ──────────────────────────────

    async def get_config(self, key: str, default: str = "") -> str:
        cursor = await self._conn.execute("SELECT value FROM config WHERE key=?", (key,))
        row = await cursor.fetchone()
        if row is None:
            return default
        val = row["value"]
        # 如果是加密值（以 e_ 开头），自动解密
        if val.startswith("e_"):
            try:
                return env.decrypt_value(val[2:])
            except Exception:
                logger.warning("failed to decrypt config key %s, returning raw", key)
                return val
        return val

    async def set_config(self, key: str, value: str, encrypt: bool = False):
        if encrypt and value:
            value = "e_" + env.encrypt_value(value)
        await self._write(
            "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, value),
        )

    async def get_all_config(self) -> dict:
        cursor = await self._conn.execute("SELECT key, value FROM config")
        rows = await cursor.fetchall()
        result = {}
        for row in rows:
            val = row["value"]
            if val.startswith("e_"):
                try:
                    val = env.decrypt_value(val[2:])
                except Exception:
                    val = "(encrypted)"
            result[row["key"]] = val
        return result

    # ── Blacklist ──────────────────────────────────────

    async def is_blacklisted(self, phone: str, provider: str = "smsbower") -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM blacklist WHERE phone=? AND provider=?", (phone, provider))
        return await cursor.fetchone() is not None

    async def add_blacklist(self, phone: str, reason: str = "", provider: str = "smsbower"):
        try:
            await self._write(
                "INSERT OR IGNORE INTO blacklist (phone, provider, reason) VALUES (?, ?, ?)",
                (phone, provider, reason),
            )
        except Exception:
            pass

    async def list_blacklist(self, provider: str = "", limit: int = 200) -> list[dict]:
        if provider:
            cursor = await self._conn.execute(
                "SELECT id, phone, provider, reason, created_at FROM blacklist "
                "WHERE provider=? ORDER BY id DESC LIMIT ?", (provider, limit))
        else:
            cursor = await self._conn.execute(
                "SELECT id, phone, provider, reason, created_at FROM blacklist "
                "ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def remove_blacklist(self, phone: str, provider: str = "smsbower"):
        await self._write(
            "DELETE FROM blacklist WHERE phone=? AND provider=?", (phone, provider))

    # ── Task State ─────────────────────────────────────

    async def create_task(self, task_id: str, country_id: Any, country_label: str,
                          provider: str = "smsbower", target_success_count: int = 1) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        await self._write(
            "INSERT INTO task_state (task_id, status, provider, country_id, country_label, "
            "target_success_count, created_at, updated_at) "
            "VALUES (?, 'running', ?, ?, ?, ?, ?, ?)",
            (task_id, provider, country_id, country_label, max(1, int(target_success_count)), now, now),
        )
        return await self.get_task(task_id)

    async def update_task(self, task_id: str, **kwargs):
        if not kwargs:
            return
        sets = []
        values = []
        for k, v in kwargs.items():
            sets.append(f"{k}=?")
            values.append(v)
        values.append(task_id)
        sets.append("updated_at=datetime('now')")
        await self._write(
            f"UPDATE task_state SET {', '.join(sets)} WHERE task_id=?",
            tuple(values),
        )

    async def get_task(self, task_id: str) -> Optional[dict]:
        cursor = await self._conn.execute("SELECT * FROM task_state WHERE task_id=?", (task_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_tasks(self, limit: int = 50) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM task_state ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_pending_tasks(self) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM task_state WHERE status IN ('running', 'compensation_pending') "
            "ORDER BY created_at ASC"
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ── Registration History ───────────────────────────

    async def save_history(self, task_id: str, phone: str, country_id: Any, country_label: str,
                            activation_id: str, wa_account_id: str, sms_code: str,
                            cost: Optional[float], provider: str = "smsbower",
                            verification_request_id: str = "", cost_is_estimated: bool = False,
                            currency: str = "USD"):
        await self._write(
            "INSERT INTO registration_history (task_id, provider, phone, country_id, country_label, "
            "activation_id, wa_account_id, verification_request_id, sms_code, status, cost, "
            "cost_known, cost_is_estimated, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)",
            (task_id, provider, phone, country_id, country_label, activation_id,
             wa_account_id, verification_request_id, "", float(cost or 0),
             int(cost is not None), int(cost_is_estimated), currency or "USD"),
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
    ):
        """Atomically persist the success ledger and aggregate task progress."""
        now = datetime.now(timezone.utc).isoformat()
        numeric_cost = float(cost or 0)
        async with self._write_lock:
            try:
                await self._conn.execute(
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
                await self._conn.execute(
                    "UPDATE task_state SET successes_done=?, target_success_count=?, "
                    "cost_total=cost_total+?, account_commit_state='committed', "
                    "wa_account_id=?, verification_request_id=?, updated_at=? WHERE task_id=?",
                    (
                        int(success_count), int(target_success_count), numeric_cost,
                        wa_account_id, verification_request_id, now, task_id,
                    ),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

    async def list_history(self, limit: int = 50) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM registration_history ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_stats(self, timezone_offset_minutes: int = 0) -> dict:
        """Aggregate success/cost from the durable history ledger."""
        offset = max(-840, min(840, int(timezone_offset_minutes or 0)))
        now_utc = datetime.now(timezone.utc)
        local_now = now_utc - timedelta(minutes=offset)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = local_start + timedelta(minutes=offset)
        end_utc = start_utc + timedelta(days=1)
        start_text = start_utc.strftime("%Y-%m-%d %H:%M:%S")
        end_text = end_utc.strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._conn.execute(
            "SELECT provider, COUNT(*) AS success_count, "
            "SUM(CASE WHEN cost_known=1 THEN cost ELSE 0 END) AS cost_total, "
            "AVG(CASE WHEN cost_known=1 THEN cost END) AS avg_cost, "
            "SUM(CASE WHEN cost_is_estimated=1 THEN 1 ELSE 0 END) AS estimated_count "
            "FROM registration_history WHERE created_at>=? AND created_at<? "
            "GROUP BY provider",
            (start_text, end_text),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        providers = {
            row["provider"]: {
                "success_count": int(row["success_count"] or 0),
                "cost_total": float(row["cost_total"] or 0),
                "avg_cost": float(row["avg_cost"]) if row["avg_cost"] is not None else None,
                "estimated_count": int(row["estimated_count"] or 0),
            }
            for row in rows
        }
        return {
            "timezone_offset_minutes": offset,
            "day_start_utc": start_utc.isoformat(),
            "providers": providers,
            "success_count": sum(item["success_count"] for item in providers.values()),
            "cost_total": sum(item["cost_total"] for item in providers.values()),
            "estimated_count": sum(item["estimated_count"] for item in providers.values()),
        }

    async def health(self) -> bool:
        try:
            cursor = await self._conn.execute("SELECT 1")
            row = await cursor.fetchone()
            return bool(row and row[0] == 1)
        except Exception:
            return False

    # ── Recovery ───────────────────────────────────────

    async def cancel_stale_tasks(self):
        """启动时扫描所有 running 任务，标记为 cancelled（容器重启导致丢失）。"""
        cursor = await self._conn.execute(
            "SELECT task_id, activation_id FROM task_state WHERE status='running'"
        )
        stale = await cursor.fetchall()
        for row in stale:
            logger.warning("recovery: marking stale task %s as cancelled (activation=%s)",
                           row["task_id"], row["activation_id"])
            await self.update_task(row["task_id"], status="cancelled", error="container restart recovery")
        return len(stale)
