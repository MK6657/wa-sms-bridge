"""SQLite WAL 读写分离连接与取消安全事务封装（修复补充评估 P1-04 / P2-02 与 v5 复核 P1-03）。

设计保证：
1. 写连接 `_conn` 由 `_write_lock` 串行化，取消或异常时自动回滚；
2. 文件型数据库在迁移完成后打开独立只读连接 `_read_conn` (`PRAGMA query_only=ON`)，
   利用 WAL 实现已提交快照隔离，并发读绝不可见未提交的写事务中间态；
3. 内存数据库 (`:memory:`) 不支持跨连接共享同一内存库，读操作与写锁协调，同样杜绝脏读；
4. `read_transaction()` 将 `BEGIN DEFERRED` 纳入清理保护，无论在 BEGIN 等待、查询途中还是连续取消，
   都会在释放读锁前完成回滚；若连接状态无法确认则立即摘除并重建干净的只读连接；
5. `close()` 可安全处理半初始化状态并等待已退役连接关闭，确保无句柄泄漏。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)


def _is_memory_path(db_path: str) -> bool:
    norm = (db_path or "").strip().lower()
    return norm == ":memory:" or norm.startswith("file::memory:") or "mode=memory" in norm


class DatabaseConnection:
    """管理 SQLite 串行写连接与独立只读快照连接。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._read_conn: Optional[aiosqlite.Connection] = None
        self._read_enabled: bool = False
        self._write_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._retired_tasks: set[asyncio.Task[Any]] = set()
        self._pending_write_cleanup: Optional[asyncio.Task[Any]] = None

    @property
    def raw(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database connection is not open")
        return self._conn

    @property
    def read_raw(self) -> aiosqlite.Connection:
        if self._read_conn is not None:
            return self._read_conn
        if self._conn is None:
            raise RuntimeError("Database connection is not open")
        return self._conn

    @property
    def is_open(self) -> bool:
        return self._conn is not None or self._read_conn is not None

    async def open(self) -> None:
        """仅打开写连接（迁移完成后再调用 open_read_connection() 对外提供只读快照）。"""
        conn = await aiosqlite.connect(self.db_path)
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
            logger.info("database write connection opened: %s (WAL mode)", self.db_path)
        except BaseException:
            try:
                await conn.close()
            except Exception:
                pass
            raise

    async def _connect_read_only(self) -> aiosqlite.Connection:
        rconn = await aiosqlite.connect(self.db_path)
        try:
            rconn.row_factory = aiosqlite.Row
            await rconn.execute("PRAGMA busy_timeout=5000")
            await rconn.execute("PRAGMA foreign_keys=ON")
            await rconn.execute("PRAGMA query_only=ON")
            return rconn
        except BaseException:
            try:
                await rconn.close()
            except Exception:
                pass
            raise

    async def open_read_connection(self) -> None:
        """在迁移完成后打开独立只读连接（仅文件型 SQLite；内存库复用写连接并受锁保护）。"""
        if _is_memory_path(self.db_path):
            self._read_conn = None
            self._read_enabled = False
            return
        async with self._read_lock:
            self._read_enabled = True
            if self._read_conn is not None:
                return
            self._read_conn = await self._connect_read_only()
            logger.debug("database read-only connection opened: %s", self.db_path)

    async def _ensure_read_conn_locked(self) -> aiosqlite.Connection:
        """在已持有 _read_lock 的前提下确保只读连接存在且处于干净的非事务状态。"""
        if self._conn is None:
            raise RuntimeError("Database connection is closed")
        if self._read_conn is None:
            self._read_conn = await self._connect_read_only()
            return self._read_conn

        rconn = self._read_conn
        if getattr(rconn, "in_transaction", False):
            await self._reset_or_rebuild_read_conn_locked(rconn)
            assert self._read_conn is not None
            return self._read_conn
        return rconn

    def _retire_connection_async(
        self,
        rconn: aiosqlite.Connection,
        pending_rollback: Optional[asyncio.Task[Any]] = None,
    ) -> None:
        """当清理过程自身被二次取消时，后台安全等待并关闭已摘除的旧只读连接。"""
        async def _retire() -> None:
            if pending_rollback is not None:
                try:
                    await pending_rollback
                except BaseException:
                    pass
            try:
                await rconn.close()
            except BaseException:
                pass

        task = asyncio.create_task(_retire())
        self._retired_tasks.add(task)
        task.add_done_callback(self._retired_tasks.discard)

    async def _reset_or_rebuild_read_conn_locked(self, rconn: aiosqlite.Connection) -> None:
        """尝试回滚脏读连接；若失败或仍残留事务，则关闭并重建独立只读连接。"""
        try:
            await rconn.rollback()
            if not getattr(rconn, "in_transaction", False):
                return
        except asyncio.CancelledError:
            if self._read_conn is rconn:
                self._read_conn = None
            self._retire_connection_async(rconn)
            raise
        except Exception:
            pass

        if self._read_conn is rconn:
            self._read_conn = None
        try:
            await rconn.close()
        except asyncio.CancelledError:
            self._retire_connection_async(rconn)
            raise
        except Exception:
            pass

        if self._conn is not None and self._read_enabled and not _is_memory_path(self.db_path):
            self._read_conn = await self._connect_read_only()
            logger.info("database read-only connection rebuilt after uncertain transaction state")

    async def _finish_read_tx_locked(self, rconn: aiosqlite.Connection) -> None:
        """在释放 _read_lock 之前确保只读事务完全回滚；若清理被中断或状态不可确认则立刻摘除并重建。"""
        needs_rebuild = False
        rollback_task = asyncio.ensure_future(rconn.rollback())
        try:
            await asyncio.shield(rollback_task)
        except asyncio.CancelledError:
            # 第二次取消打断了回滚等待：立刻摘除连接，交由后台退役并让下一次查询重建干净连接
            if self._read_conn is rconn:
                self._read_conn = None
            self._retire_connection_async(rconn, rollback_task)
            raise
        except Exception:
            needs_rebuild = True

        if not needs_rebuild and getattr(rconn, "in_transaction", False):
            needs_rebuild = True

        if needs_rebuild:
            if self._read_conn is rconn:
                self._read_conn = None
            try:
                await rconn.close()
            except asyncio.CancelledError:
                self._retire_connection_async(rconn)
                raise
            except Exception:
                pass
            if self._conn is not None and self._read_enabled and not _is_memory_path(self.db_path):
                try:
                    self._read_conn = await self._connect_read_only()
                    logger.info("database read-only connection rebuilt after failed rollback")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("rebuilding read-only connection failed; will retry on next read", exc_info=True)

    async def close(self) -> None:
        """安全释放已打开的只读连接、写连接以及退役连接任务（支持半初始化回退）。"""
        self._read_enabled = False
        read_conn = self._read_conn
        write_conn = self._conn
        self._read_conn = None
        self._conn = None

        retired = list(self._retired_tasks)
        if retired:
            await asyncio.gather(*retired, return_exceptions=True)

        errors: list[Exception] = []
        if read_conn is not None:
            try:
                await read_conn.close()
            except Exception as exc:
                errors.append(exc)
        if write_conn is not None:
            try:
                await write_conn.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            logger.warning("closing sqlite connections encountered error: %s", errors[0])

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        return await self.raw.execute(sql, params)

    @asynccontextmanager
    async def read_transaction(self):
        """在独立只读连接上开启取消安全的短只读快照事务（内存库回退到受写锁保护的已提交读）。"""
        if self._conn is None:
            raise RuntimeError("Database connection is not open")

        if self._read_enabled and not _is_memory_path(self.db_path):
            async with self._read_lock:
                rconn = await self._ensure_read_conn_locked()
                try:
                    await rconn.execute("BEGIN DEFERRED")
                    yield rconn
                finally:
                    await self._finish_read_tx_locked(rconn)
        else:
            # 内存库或迁移期回退：与写锁互斥，保证看不到未提交的写事务中间态
            async with self._write_lock:
                conn = await self._ensure_clean_write_conn_locked()
                yield conn

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if self._read_enabled and not _is_memory_path(self.db_path):
            async with self._read_lock:
                rconn = await self._ensure_read_conn_locked()
                async with rconn.execute(sql, params) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row is not None else None
        async with self._write_lock:
            conn = await self._ensure_clean_write_conn_locked()
            async with conn.execute(sql, params) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._read_enabled and not _is_memory_path(self.db_path):
            async with self._read_lock:
                rconn = await self._ensure_read_conn_locked()
                async with rconn.execute(sql, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(r) for r in rows]
        async with self._write_lock:
            conn = await self._ensure_clean_write_conn_locked()
            async with conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def _ensure_clean_write_conn_locked(self) -> aiosqlite.Connection:
        conn = self.raw
        if self._pending_write_cleanup is not None:
            pending = self._pending_write_cleanup
            self._pending_write_cleanup = None
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                self._pending_write_cleanup = pending
                raise
            except BaseException:
                pass
        if getattr(conn, "in_transaction", False) is True:
            rb_task = asyncio.ensure_future(conn.rollback())
            try:
                await asyncio.shield(rb_task)
            except asyncio.CancelledError:
                self._pending_write_cleanup = rb_task
                raise
            except BaseException:
                pass
        return conn

    async def _rollback_write_safely_locked(self, conn: aiosqlite.Connection) -> None:
        rb_task = asyncio.ensure_future(conn.rollback())
        try:
            await asyncio.shield(rb_task)
        except asyncio.CancelledError:
            self._pending_write_cleanup = rb_task
            raise
        except BaseException:
            pass

    async def write(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        """Serialize execute+commit so cancellation cannot leak a transaction."""
        async with self._write_lock:
            conn = await self._ensure_clean_write_conn_locked()
            try:
                cursor = await conn.execute(sql, params)
                await conn.commit()
                return cursor
            except BaseException:
                await self._rollback_write_safely_locked(conn)
                raise

    @asynccontextmanager
    async def transaction(self):
        """Atomic multi-statement write transaction with cancellation-safe rollback."""
        async with self._write_lock:
            conn = await self._ensure_clean_write_conn_locked()
            try:
                await conn.execute("BEGIN IMMEDIATE")
                yield conn
                await conn.commit()
            except BaseException:
                await self._rollback_write_safely_locked(conn)
                raise

    async def health(self) -> bool:
        try:
            row = await self.fetchone("SELECT 1 AS ok")
            return bool(row and row.get("ok") == 1)
        except Exception:
            return False
