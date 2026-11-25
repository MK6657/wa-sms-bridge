"""分平台 + 分项目隔离的号码黑名单仓储（修复 P1-10）。

project_id == '' 表示管理员维护的平台全局黑名单；
project_id != '' 表示指定项目的私有黑名单，不影响其他项目。
"""

from __future__ import annotations

from typing import Any, Optional
from app.db.connection import DatabaseConnection


class BlacklistRepo:
    def __init__(self, conn: DatabaseConnection):
        self._conn = conn

    async def is_blacklisted(
        self,
        phone: str,
        provider: str = "smsbower",
        *,
        project_id: str = "",
    ) -> bool:
        """检查号码是否在全局黑名单或指定项目私有黑名单中。"""
        if project_id:
            row = await self._conn.fetchone(
                "SELECT 1 AS ok FROM blacklist WHERE phone=? AND provider=? AND project_id IN ('', ?)",
                (phone, provider, project_id),
            )
        else:
            row = await self._conn.fetchone(
                "SELECT 1 AS ok FROM blacklist WHERE phone=? AND provider=? AND project_id=''",
                (phone, provider),
            )
        return row is not None

    async def add(
        self,
        phone: str,
        reason: str = "",
        provider: str = "smsbower",
        *,
        project_id: str = "",
    ) -> None:
        await self._conn.write(
            "INSERT OR IGNORE INTO blacklist (phone, provider, project_id, reason) VALUES (?, ?, ?, ?)",
            (phone, provider, project_id or "", reason),
        )

    async def list(
        self,
        provider: str = "",
        limit: int = 200,
        *,
        project_id: Optional[str] = None,
        project_only: bool = False,
    ) -> list[dict[str, Any]]:
        """project_id=None 返回全部（管理员）；project_id='' 仅全局；project_only=True 仅非空项目；project_id='proj' 仅该项目。"""
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        elif project_only:
            clauses.append("project_id != ''")
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self._conn.fetchall(
            f"SELECT id, phone, provider, project_id, reason, created_at FROM blacklist{where_sql} "
            "ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )

    async def remove(
        self,
        phone: str,
        provider: str = "smsbower",
        *,
        project_id: str = "",
    ) -> int:
        cur = await self._conn.write(
            "DELETE FROM blacklist WHERE phone=? AND provider=? AND project_id=?",
            (phone, provider, project_id or ""),
        )
        return int(cur.rowcount or 0)

    async def count(self, provider: str = "", *, project_id: Optional[str] = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = await self._conn.fetchone(f"SELECT COUNT(*) AS cnt FROM blacklist{where_sql}", tuple(params))
        return int((row or {}).get("cnt") or 0)
