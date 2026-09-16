"""Database connection management + Unit of Work.

One writable connection (serialized by a lock so a document, its stock moves,
its journal entry and its audit row all commit together) and one read-only
connection (PRAGMA query_only=ON) so reads are isolated and never blocked.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite


class Database:
    def __init__(self, path: str):
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._ro: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connection(self) -> aiosqlite.Connection:
        """The single read/write connection (used inside a Unit of Work)."""
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    async def readonly(self) -> aiosqlite.Connection:
        """A separate connection the engine refuses to write through."""
        if self._ro is None:
            self._ro = await aiosqlite.connect(self._path)
            self._ro.row_factory = aiosqlite.Row
            await self._ro.execute("PRAGMA foreign_keys=ON")
            await self._ro.execute("PRAGMA query_only=ON")
        return self._ro

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[aiosqlite.Connection]:
        """One transaction per use case: serialize writers, commit on success,
        roll back on any error. A receipt that fails to post its journal entry
        leaves no stock move behind."""
        async with self._write_lock:
            conn = await self.connection()
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        if self._ro is not None:
            await self._ro.close()
            self._ro = None


async def fetch_all(
    conn: aiosqlite.Connection, sql: str, params: tuple | list = ()
) -> list[aiosqlite.Row]:
    """Materialize fetchall as a real list (aiosqlite types it as a bare Iterable)."""
    return list(await conn.execute_fetchall(sql, params))
