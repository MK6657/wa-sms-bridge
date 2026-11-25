"""动态配置表仓储（支持敏感字段 Fernet 加密）。"""

from __future__ import annotations

import logging
from app.config import env
from app.db.connection import DatabaseConnection

logger = logging.getLogger(__name__)


class ConfigRepo:
    def __init__(self, conn: DatabaseConnection):
        self._conn = conn

    async def get(self, key: str, default: str = "") -> str:
        row = await self._conn.fetchone("SELECT value FROM config WHERE key=?", (key,))
        if row is None:
            return default
        val = str(row["value"])
        if val.startswith("e_"):
            try:
                return env.decrypt_value(val[2:])
            except Exception:
                logger.warning("failed to decrypt config key %s, failing closed with empty string", key)
                return ""
        return val

    async def set(self, key: str, value: str, encrypt: bool = False) -> None:
        stored = f"e_{env.encrypt_value(value)}" if (encrypt and value) else value
        await self._conn.write(
            "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, stored),
        )

    async def delete(self, key: str) -> None:
        await self._conn.write("DELETE FROM config WHERE key=?", (key,))

    async def get_all(self) -> dict[str, str]:
        rows = await self._conn.fetchall("SELECT key, value FROM config")
        result: dict[str, str] = {}
        for row in rows:
            val = str(row["value"])
            if val.startswith("e_"):
                try:
                    val = env.decrypt_value(val[2:])
                except Exception:
                    val = "(encrypted)"
            result[str(row["key"])] = val
        return result
