"""SQLite access: one aiosqlite connection, WAL, and a plain-SQL migration runner.

The Pi boots from an SD card, so this layer never writes on its own: opening an up-to-date database only reads.
Migrations live in ``migrations/NNNN_name.sql`` and are applied in version order, each in its own transaction,
and recorded in ``schema_migrations``.
"""

import asyncio
import contextlib
import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
DEFAULT_BUSY_TIMEOUT_MS = 5000

_MIGRATION_NAME = re.compile(r"^(\d+)_([\w-]+)\.sql$")

Params = Sequence[Any] | dict[str, Any]


class MigrationError(RuntimeError):
    """The migration files or the database's migration history are inconsistent."""


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    def statements(self) -> list[str]:
        return split_statements(self.path.read_text(encoding="utf-8"))


def split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements (handles ``;`` inside strings and trigger bodies)."""
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if _strip_comments(stmt):
                statements.append(stmt)
            buf = ""
    if _strip_comments(buf):
        raise MigrationError(f"incomplete SQL statement at end of script: {buf.strip()[:80]!r}")
    return statements


def _strip_comments(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--")).strip().strip(";")


def discover_migrations(directory: Path) -> list[Migration]:
    migrations: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise MigrationError(f"migration file name must look like 0001_name.sql: {path.name}")
        version = int(match.group(1))
        if version in migrations:
            raise MigrationError(f"duplicate migration version {version}: {migrations[version].path.name}, {path.name}")
        migrations[version] = Migration(version, match.group(2), path)
    return [migrations[v] for v in sorted(migrations)]


@dataclass(frozen=True)
class ExecResult:
    rowcount: int
    lastrowid: int | None


class Connection:
    """Query helpers over the raw connection. Inside ``Database.transaction()`` you get one of these directly."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Params = ()) -> ExecResult:
        async with self._conn.execute(sql, params) as cur:
            return ExecResult(cur.rowcount, cur.lastrowid)

    async def executemany(self, sql: str, params: Iterable[Params]) -> int:
        async with self._conn.executemany(sql, params) as cur:
            return cur.rowcount

    async def fetchone(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchval(self, sql: str, params: Params = ()) -> Any:
        row = await self.fetchone(sql, params)
        return None if row is None else row[0]


class Database:
    """A single serialized connection. Every call takes an asyncio lock, so a transaction is never interleaved
    with another coroutine's statements on the shared connection."""

    def __init__(self, conn: aiosqlite.Connection, path: str) -> None:
        self.path = path
        self._conn = conn
        self._raw = Connection(conn)
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(
        cls,
        path: str | Path,
        *,
        migrations_dir: Path | None = MIGRATIONS_DIR,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> "Database":
        """Open (creating the parent directory if needed), set the PRAGMAs and apply pending migrations.

        ``migrations_dir=None`` skips migrations (tests of the runner itself).
        """
        path = str(path)
        if path != ":memory:" and not path.startswith("file:"):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path, isolation_level=None)  # autocommit; transactions are explicit
        conn.row_factory = sqlite3.Row
        db = cls(conn, path)
        try:
            await conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            mode = await db._raw.fetchval("PRAGMA journal_mode = WAL")
            if mode != "wal" and path != ":memory:":
                log.warning("SQLite journal_mode is %s, not wal", mode)
            await conn.execute("PRAGMA synchronous = NORMAL")
            await conn.execute("PRAGMA foreign_keys = ON")
            if migrations_dir is not None:
                await db.migrate(migrations_dir)
        except BaseException:
            await conn.close()
            raise
        return db

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            await self._conn.close()

    @property
    def closed(self) -> bool:
        return self._closed

    # --- queries ------------------------------------------------------------------------------------------------

    async def execute(self, sql: str, params: Params = ()) -> ExecResult:
        async with self._lock:
            return await self._raw.execute(sql, params)

    async def fetchone(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        async with self._lock:
            return await self._raw.fetchone(sql, params)

    async def fetchall(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        async with self._lock:
            return await self._raw.fetchall(sql, params)

    async def fetchval(self, sql: str, params: Params = ()) -> Any:
        async with self._lock:
            return await self._raw.fetchval(sql, params)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        """``BEGIN IMMEDIATE`` … ``COMMIT``; rolls back on any exception. Not re-entrant: inside the block use the
        yielded connection, never the ``Database`` methods."""
        async with self._lock:
            try:
                await self._control("BEGIN IMMEDIATE")
            except asyncio.CancelledError:
                if self._conn.in_transaction:  # BEGIN ran anyway: don't leave the shared connection inside it
                    await self._control("ROLLBACK")
                raise
            try:
                yield self._raw
            except BaseException:
                await self._control("ROLLBACK")
                raise
            else:
                await self._control("COMMIT")

    async def _control(self, sql: str) -> None:
        """Run a transaction-control statement to completion even if the caller is cancelled meanwhile (aiosqlite
        runs a queued statement regardless); the cancellation is re-raised afterwards."""
        statement = asyncio.ensure_future(self._conn.execute(sql))
        try:
            await asyncio.shield(statement)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await statement
            raise

    # --- migrations ---------------------------------------------------------------------------------------------

    async def applied_migrations(self) -> dict[int, str]:
        exists = await self.fetchval("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'")
        if not exists:
            return {}
        rows = await self.fetchall("SELECT version, name FROM schema_migrations ORDER BY version")
        return {row["version"]: row["name"] for row in rows}

    async def migrate(self, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
        """Apply pending migrations; returns the versions applied (empty when up to date, with nothing written)."""
        available = discover_migrations(migrations_dir)
        applied = await self.applied_migrations()
        known = {m.version for m in available}
        unknown = sorted(set(applied) - known)
        if unknown:
            raise MigrationError(f"database has migrations this code does not know about: {unknown} (downgrade?)")
        pending = [m for m in available if m.version not in applied]
        if not pending:
            return []
        if not applied:
            await self.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
        if pending[0].version < max(applied, default=0):
            raise MigrationError(f"migration {pending[0].version} is older than the newest applied one")
        done: list[int] = []
        for migration in pending:
            statements = migration.statements()
            try:
                async with self.transaction() as tx:
                    for stmt in statements:
                        await tx.execute(stmt)
                    await tx.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, utcnow_iso()),
                    )
            except sqlite3.Error as exc:
                raise MigrationError(f"migration {migration.path.name} failed: {exc}") from exc
            log.info("applied migration %s", migration.path.name)
            done.append(migration.version)
        return done
