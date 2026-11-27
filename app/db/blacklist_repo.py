"""分平台 + 分项目隔离的号码黑名单仓储（修复 P1-10）。

project_id == '' 表示管理员维护的平台全局黑名单；
project_id != '' 表示指定项目的私有黑名单，不影响其他项目。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional
from app.db.connection import DatabaseConnection


def _canonical_phone(phone: str) -> str:
    raw = str(phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if digits:
        return f"+{digits}" if raw.startswith("+") else digits
    return raw


def _phone_lookup_variants(phone: str) -> tuple[str, ...]:
    raw = str(phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    candidates = [c for c in dict.fromkeys((raw, f"+{digits}" if digits else "", digits)) if c]
    return tuple(candidates) if candidates else (raw,)


def _norm_provider(provider: str, *, default: str = "smsbower") -> str:
    cleaned = str(provider or "").strip().lower()
    return cleaned or default


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
        """检查号码是否在全局黑名单或指定项目私有黑名单中（自动兼容 +E.164 与无加号纯数字格式）。"""
        prov = _norm_provider(provider)
        variants = _phone_lookup_variants(phone)
        ph = ",".join("?" for _ in variants)
        if project_id:
            row = await self._conn.fetchone(
                f"SELECT 1 AS ok FROM blacklist WHERE phone IN ({ph}) AND provider=? AND project_id IN ('', ?)",
                (*variants, prov, project_id),
            )
        else:
            row = await self._conn.fetchone(
                f"SELECT 1 AS ok FROM blacklist WHERE phone IN ({ph}) AND provider=? AND project_id=''",
                (*variants, prov),
            )
        return row is not None

    async def add(
        self,
        phone: str,
        reason: str = "",
        provider: str = "smsbower",
        *,
        project_id: str = "",
    ) -> bool:
        prov = _norm_provider(provider)
        pid = project_id or ""
        canonical = _canonical_phone(phone)
        variants = _phone_lookup_variants(phone)
        ph = ",".join("?" for _ in variants)
        async with self._conn.transaction() as raw:
            dup_cur = await raw.execute(
                f"SELECT 1 FROM blacklist WHERE phone IN ({ph}) AND provider=? AND project_id=? LIMIT 1",
                (*variants, prov, pid),
            )
            if (await dup_cur.fetchone()) is not None:
                return False
            cur = await raw.execute(
                "INSERT OR IGNORE INTO blacklist (phone, provider, project_id, reason) VALUES (?, ?, ?, ?)",
                (canonical, prov, pid, reason),
            )
            return int(cur.rowcount or 0) > 0

    async def add_batch(
        self,
        phones: Iterable[str],
        *,
        reason: str = "",
        provider: str = "smsbower",
        project_id: str = "",
    ) -> int:
        prov = _norm_provider(provider)
        pid = project_id or ""
        added = 0
        async with self._conn.transaction() as raw:
            for phone in phones:
                canonical = _canonical_phone(phone)
                if not canonical:
                    continue
                variants = _phone_lookup_variants(phone)
                ph = ",".join("?" for _ in variants)
                dup_cur = await raw.execute(
                    f"SELECT 1 FROM blacklist WHERE phone IN ({ph}) AND provider=? AND project_id=? LIMIT 1",
                    (*variants, prov, pid),
                )
                if (await dup_cur.fetchone()) is not None:
                    continue
                cur = await raw.execute(
                    "INSERT OR IGNORE INTO blacklist (phone, provider, project_id, reason) VALUES (?, ?, ?, ?)",
                    (canonical, prov, pid, reason),
                )
                if int(cur.rowcount or 0) > 0:
                    added += 1
        return added

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
        if provider and provider.strip():
            clauses.append("provider=?")
            params.append(provider.strip().lower())
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
        prov = _norm_provider(provider)
        variants = _phone_lookup_variants(phone)
        ph = ",".join("?" for _ in variants)
        cur = await self._conn.write(
            f"DELETE FROM blacklist WHERE phone IN ({ph}) AND provider=? AND project_id=?",
            (*variants, prov, project_id or ""),
        )
        return int(cur.rowcount or 0)

    async def count(self, provider: str = "", *, project_id: Optional[str] = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if provider and provider.strip():
            clauses.append("provider=?")
            params.append(provider.strip().lower())
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = await self._conn.fetchone(f"SELECT COUNT(*) AS cnt FROM blacklist{where_sql}", tuple(params))
        return int((row or {}).get("cnt") or 0)
