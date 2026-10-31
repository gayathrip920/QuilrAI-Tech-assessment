"""Token-aware sliding window rate limiter stored in SQLite on disk.

Each request writes one row: (tenant, timestamp, tokens). The tokens used by
a tenant "in the last minute" is the sum of its rows newer than now - 60s.
This is a sliding window log, so there is no burst at window edges like a
fixed per-minute counter has.

Concurrency: the read (sum) and the write (insert) happen inside one
BEGIN IMMEDIATE transaction. SQLite gives that transaction the write lock
up front, so two requests can never both read "49,000 used" and both get
in. This also works if several gateway processes share the same file.

Inside one process all requests share one connection, and SQLite does not
allow two open transactions on one connection. An asyncio.Lock makes the
coroutines take turns. BEGIN IMMEDIATE still protects across processes.
"""
import asyncio
import time
import uuid
from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id          TEXT PRIMARY KEY,
    tenant      TEXT NOT NULL,
    ts          REAL NOT NULL,
    tokens      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_ts ON token_usage (tenant, ts);
"""


@dataclass
class Decision:
    allowed: bool
    reservation_id: str | None
    used: int
    limit: int
    retry_after_s: float = 0.0


class SlidingWindowLimiter:
    def __init__(self, db_path: str, window_s: float = 60.0, clock=time.time):
        self.db_path = db_path
        self.window_s = window_s
        self.clock = clock
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.db_path, isolation_level=None, timeout=5.0)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def reserve(self, tenant: str, tokens: int, limit: int) -> Decision:
        """Atomically check the window and, if there is room, record `tokens`."""
        async with self._lock:
            return await self._reserve_locked(tenant, tokens, limit)

    async def _reserve_locked(self, tenant: str, tokens: int, limit: int) -> Decision:
        db = self._db
        now = self.clock()
        window_start = now - self.window_s
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Evict this tenant's expired rows so the table does not grow forever.
            await db.execute("DELETE FROM token_usage WHERE tenant = ? AND ts <= ?", (tenant, window_start))
            async with db.execute("SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ?", (tenant,)) as cur:
                (used,) = await cur.fetchone()

            if used + tokens > limit:
                retry_after = await self._retry_after(tenant, used, tokens, limit, now)
                await db.execute("COMMIT")
                return Decision(False, None, used, limit, retry_after)

            rid = uuid.uuid4().hex
            await db.execute(
                "INSERT INTO token_usage (id, tenant, ts, tokens) VALUES (?, ?, ?, ?)", (rid, tenant, now, tokens)
            )
            await db.execute("COMMIT")
            return Decision(True, rid, used + tokens, limit)
        except BaseException:
            await db.execute("ROLLBACK")
            raise

    async def _retry_after(self, tenant: str, used: int, tokens: int, limit: int, now: float) -> float:
        """Seconds until enough old rows expire for this request to fit."""
        if tokens > limit:
            return self.window_s  # can never fit; tell the client to back off a full window
        need_to_free = used + tokens - limit
        freed = 0
        async with self._db.execute(
            "SELECT ts, tokens FROM token_usage WHERE tenant = ? ORDER BY ts", (tenant,)
        ) as cur:
            async for ts, t in cur:
                freed += t
                if freed >= need_to_free:
                    return max(0.0, ts + self.window_s - now)
        return self.window_s

    async def reconcile(self, reservation_id: str, actual_tokens: int) -> None:
        """Replace the estimate with the real token count from the provider."""
        async with self._lock:
            await self._db.execute("UPDATE token_usage SET tokens = ? WHERE id = ?", (max(0, int(actual_tokens)), reservation_id))

    async def release(self, reservation_id: str) -> None:
        """Give tokens back when no provider produced any output."""
        async with self._lock:
            await self._db.execute("DELETE FROM token_usage WHERE id = ?", (reservation_id,))

    async def evict_all(self) -> int:
        """Global cleanup for tenants that went quiet. Returns rows removed."""
        async with self._lock:
            cur = await self._db.execute("DELETE FROM token_usage WHERE ts <= ?", (self.clock() - self.window_s,))
            return cur.rowcount

    async def used(self, tenant: str) -> int:
        async with self._db.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ? AND ts > ?",
            (tenant, self.clock() - self.window_s),
        ) as cur:
            (n,) = await cur.fetchone()
        return n
