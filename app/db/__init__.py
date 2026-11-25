"""模块化 SQLite 持久化外观门面。"""

from __future__ import annotations

from typing import Any, Optional

from app.db.activations_repo import ActivationsRepo
from app.db.api_keys_repo import ApiKeysRepo
from app.db.blacklist_repo import BlacklistRepo
from app.db.config_repo import ConfigRepo
from app.db.connection import DatabaseConnection
from app.db.legacy_wa_repo import LegacyWaRepo
from app.db.migrations import run_migrations
from app.db.orders_repo import (
    ACTIVE_ORDER_STATUSES,
    CLOSED_ORDER_STATUSES,
    RECOVERABLE_ORDER_STATUSES,
    OrdersRepo,
)
from app.db.webhooks_repo import WebhooksRepo


class Database:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self.conn = DatabaseConnection(db_path)
        self.config = ConfigRepo(self.conn)
        self.blacklist = BlacklistRepo(self.conn)
        self.orders = OrdersRepo(self.conn)
        self.activations = ActivationsRepo(self.conn)
        self.webhooks = WebhooksRepo(self.conn)
        self.api_keys = ApiKeysRepo(self.conn)
        self.legacy_wa = LegacyWaRepo(self.conn)

    @property
    def _conn(self):
        return self.conn._conn

    @_conn.setter
    def _conn(self, value):
        self.conn._conn = value

    async def open(self) -> None:
        try:
            await self.conn.open()
            await run_migrations(self.conn.raw)
            await self.conn.open_read_connection()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        await self.conn.close()

    async def _write(self, sql: str, params: tuple = ()):
        return await self.conn.write(sql, params)

    async def health(self) -> bool:
        return await self.conn.health()

    # ── 向后兼容代理方法 ────────────────────────────────────────

    async def get_config(self, key: str, default: str = "") -> str:
        return await self.config.get(key, default)

    async def set_config(self, key: str, value: str, encrypt: bool = False) -> None:
        await self.config.set(key, value, encrypt=encrypt)

    async def get_all_config(self) -> dict[str, str]:
        return await self.config.get_all()

    async def is_blacklisted(self, phone: str, provider: str = "smsbower", *, project_id: str = "") -> bool:
        return await self.blacklist.is_blacklisted(phone, provider=provider, project_id=project_id)

    async def add_blacklist(self, phone: str, reason: str = "", provider: str = "smsbower", *, project_id: str = "") -> None:
        await self.blacklist.add(phone, reason=reason, provider=provider, project_id=project_id)

    async def list_blacklist(self, provider: str = "", limit: int = 200, *, project_id: Optional[str] = None) -> list[dict[str, Any]]:
        return await self.blacklist.list(provider=provider, limit=limit, project_id=project_id)

    async def remove_blacklist(self, phone: str, provider: str = "smsbower", *, project_id: str = "") -> None:
        await self.blacklist.remove(phone, provider=provider, project_id=project_id)

    async def create_task(self, task_id: str, country_id: Any, country_label: str,
                          provider: str = "smsbower", target_success_count: int = 1) -> dict[str, Any]:
        return await self.legacy_wa.create_task(
            task_id, country_id, country_label,
            provider=provider, target_success_count=target_success_count,
        )

    async def update_task(self, task_id: str, **kwargs: Any) -> None:
        await self.legacy_wa.update_task(task_id, **kwargs)

    async def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        return await self.legacy_wa.get_task(task_id)

    async def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.legacy_wa.list_tasks(limit=limit)

    async def get_pending_tasks(self) -> list[dict[str, Any]]:
        return await self.legacy_wa.get_pending_tasks()

    async def save_history(self, *args: Any, **kwargs: Any) -> None:
        await self.legacy_wa.save_history(*args, **kwargs)

    async def record_registration_success(self, **kwargs: Any) -> None:
        await self.legacy_wa.record_registration_success(**kwargs)

    async def list_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.legacy_wa.list_history(limit=limit)

    async def get_stats(self, timezone_offset_minutes: int = 0) -> dict[str, Any]:
        return await self.legacy_wa.get_stats(timezone_offset_minutes)

    async def cancel_stale_tasks(self) -> int:
        return await self.legacy_wa.cancel_stale_tasks()


__all__ = [
    "ACTIVE_ORDER_STATUSES",
    "ActivationsRepo",
    "ApiKeysRepo",
    "BlacklistRepo",
    "CLOSED_ORDER_STATUSES",
    "ConfigRepo",
    "Database",
    "DatabaseConnection",
    "LegacyWaRepo",
    "OrdersRepo",
    "RECOVERABLE_ORDER_STATUSES",
    "WebhooksRepo",
]
