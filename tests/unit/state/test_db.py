import asyncio
import contextlib
import hashlib
import sqlite3

import pytest

from app.state.db import MIGRATIONS_DIR, Database, MigrationError, discover_migrations, split_statements

TABLES = {"device", "preset_state", "revision", "profile", "fir_blob", "channel_meta", "setting", "event_log"}


def _write(directory, name, sql):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(sql)


async def _tables(db: Database) -> set[str]:
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


async def test_open_creates_parent_dir_and_sets_pragmas(db, db_path):
    assert db_path.exists()
    assert await db.fetchval("PRAGMA journal_mode") == "wal"
    assert await db.fetchval("PRAGMA synchronous") == 1  # NORMAL
    assert await db.fetchval("PRAGMA busy_timeout") == 5000


async def test_initial_migration_creates_schema(db):
    assert TABLES | {"schema_migrations"} <= await _tables(db)
    assert await db.applied_migrations() == {1: "initial", 2: "channel_flags"}


def test_repo_migrations_are_well_formed():
    migrations = discover_migrations(MIGRATIONS_DIR)
    assert [m.version for m in migrations][0] == 1
    for m in migrations:
        assert m.statements()


async def test_preset_check_constraint(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("INSERT INTO preset_state (serial, preset, desired_json) VALUES (1, 4, '{}')")


async def test_reopen_up_to_date_db_writes_nothing(db_path):
    db = await Database.open(db_path)
    await db.close()
    before = (db_path.stat().st_mtime_ns, hashlib.sha256(db_path.read_bytes()).hexdigest())

    db = await Database.open(db_path)
    assert await db.migrate() == []
    assert await _tables(db) >= TABLES
    await db.close()

    after = (db_path.stat().st_mtime_ns, hashlib.sha256(db_path.read_bytes()).hexdigest())
    assert after == before


async def test_channel_flags_migration_keeps_existing_rows(tmp_path):
    """0002 adds the Settings flags to a database made before them: existing channels read as in use, not tactile."""
    mig = tmp_path / "mig"
    mig.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name.startswith("0001_"):
            (mig / path.name).write_text(path.read_text())
    db_file = tmp_path / "old.sqlite3"
    db = await Database.open(db_file, migrations_dir=mig)
    await db.execute(
        "INSERT INTO channel_meta (serial, kind, idx, name, color, max_gain_db)"
        " VALUES (7, 'output', 0, 'Shakers', NULL, 6)"
    )
    await db.close()

    db = await Database.open(db_file)  # the real migrations directory: applies 0002
    try:
        assert await db.applied_migrations() == {1: "initial", 2: "channel_flags"}
        row = await db.fetchone("SELECT * FROM channel_meta WHERE serial = 7")
        assert (row["name"], row["max_gain_db"], row["unused"], row["tactile"]) == ("Shakers", 6.0, 0, 0)
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("UPDATE channel_meta SET unused = 2 WHERE serial = 7")
    finally:
        await db.close()


async def test_incremental_migrations(tmp_path):
    mig = tmp_path / "mig"
    _write(mig, "0001_a.sql", "CREATE TABLE a (x INTEGER);\nINSERT INTO a VALUES (1);")
    _write(mig, "0002_b.sql", "-- comment only header\nCREATE TABLE b (y TEXT);")
    db = await Database.open(tmp_path / "x.sqlite3", migrations_dir=mig)
    assert await db.applied_migrations() == {1: "a", 2: "b"}

    _write(mig, "0003_c.sql", "ALTER TABLE b ADD COLUMN z INTEGER DEFAULT 7;")
    assert await db.migrate(mig) == [3]
    assert await db.migrate(mig) == []
    await db.execute("INSERT INTO b (y) VALUES ('q')")
    assert await db.fetchval("SELECT z FROM b") == 7
    await db.close()


async def test_failed_migration_rolls_back(tmp_path):
    mig = tmp_path / "mig"
    _write(mig, "0001_ok.sql", "CREATE TABLE ok (x INTEGER);")
    _write(mig, "0002_bad.sql", "CREATE TABLE half (x INTEGER);\nINSERT INTO nope VALUES (1);")
    with pytest.raises(MigrationError, match="0002_bad.sql"):
        await Database.open(tmp_path / "x.sqlite3", migrations_dir=mig)

    db = await Database.open(tmp_path / "x.sqlite3", migrations_dir=None)
    assert await db.applied_migrations() == {1: "ok"}
    assert "half" not in await _tables(db)
    await db.close()


async def test_bad_file_name_rejected(tmp_path):
    mig = tmp_path / "mig"
    _write(mig, "initial.sql", "CREATE TABLE a (x);")
    with pytest.raises(MigrationError, match="file name"):
        await Database.open(tmp_path / "x.sqlite3", migrations_dir=mig)


async def test_duplicate_version_rejected(tmp_path):
    mig = tmp_path / "mig"
    _write(mig, "0001_a.sql", "CREATE TABLE a (x);")
    _write(mig, "1_b.sql", "CREATE TABLE b (x);")
    with pytest.raises(MigrationError, match="duplicate"):
        discover_migrations(mig)


async def test_unknown_applied_version_rejected(tmp_path):
    mig = tmp_path / "mig"
    _write(mig, "0001_a.sql", "CREATE TABLE a (x);")
    _write(mig, "0002_b.sql", "CREATE TABLE b (x);")
    db = await Database.open(tmp_path / "x.sqlite3", migrations_dir=mig)
    await db.close()
    (mig / "0002_b.sql").unlink()
    with pytest.raises(MigrationError, match="downgrade"):
        await Database.open(tmp_path / "x.sqlite3", migrations_dir=mig)


async def test_out_of_order_migration_rejected(tmp_path):
    mig = tmp_path / "mig"
    _write(mig, "0002_b.sql", "CREATE TABLE b (x);")
    db = await Database.open(tmp_path / "x.sqlite3", migrations_dir=mig)
    _write(mig, "0001_a.sql", "CREATE TABLE a (x);")
    with pytest.raises(MigrationError, match="older"):
        await db.migrate(mig)
    await db.close()


def test_split_statements_handles_strings_and_triggers():
    script = """
    -- leading comment
    CREATE TABLE t (x TEXT);
    INSERT INTO t VALUES ('a;b');
    CREATE TRIGGER trg AFTER INSERT ON t BEGIN
      UPDATE t SET x = x || ';' WHERE rowid = new.rowid;
    END;
    -- trailing comment
    """
    stmts = split_statements(script)
    assert len(stmts) == 3
    assert stmts[1].endswith("('a;b');")
    assert stmts[2].startswith("CREATE TRIGGER") and stmts[2].endswith("END;")


def test_split_statements_rejects_incomplete():
    with pytest.raises(MigrationError, match="incomplete"):
        split_statements("CREATE TABLE t (x);\nINSERT INTO t VALUES (1)")


async def test_transaction_commits_and_rolls_back(db):
    await db.execute("CREATE TABLE t (x INTEGER)")
    async with db.transaction() as tx:
        await tx.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError("boom")
    assert [r["x"] for r in await db.fetchall("SELECT x FROM t")] == [1]


async def test_concurrent_transactions_are_serialized(db):
    await db.execute("CREATE TABLE c (n INTEGER)")
    await db.execute("INSERT INTO c VALUES (0)")

    async def bump():
        async with db.transaction() as tx:
            n = await tx.fetchval("SELECT n FROM c")
            await asyncio.sleep(0)
            await tx.execute("UPDATE c SET n = ?", (n + 1,))

    await asyncio.gather(*(bump() for _ in range(25)))
    assert await db.fetchval("SELECT n FROM c") == 25


async def test_execute_result_and_close_is_idempotent(db_path):
    db = await Database.open(db_path)
    await db.execute("CREATE TABLE t (x INTEGER)")
    res = await db.execute("INSERT INTO t VALUES (5)")
    assert res.rowcount == 1 and res.lastrowid == 1
    await db.close()
    await db.close()
    assert db.closed


async def test_memory_database(tmp_path):
    db = await Database.open(":memory:")
    assert await _tables(db) >= TABLES
    await db.close()


async def test_cancelled_transaction_does_not_leave_the_connection_inside_it(db_path):
    db = await Database.open(db_path)
    try:
        await db.execute("CREATE TABLE t (v INTEGER)")
        for ticks in range(6):  # cancel at different points around BEGIN

            async def tx_body() -> None:
                async with db.transaction() as tx:
                    await tx.execute("INSERT INTO t (v) VALUES (?)", (-1,))

            task = asyncio.create_task(tx_body())
            for _ in range(ticks):
                await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert not db._conn.in_transaction
            await db.execute("INSERT INTO t (v) VALUES (?)", (ticks,))
            async with db.transaction() as tx:  # must not fail with "transaction within a transaction"
                await tx.execute("INSERT INTO t (v) VALUES (?)", (100 + ticks,))
    finally:
        await db.close()
    db = await Database.open(db_path)
    try:
        values = {row["v"] for row in await db.fetchall("SELECT v FROM t")}
    finally:
        await db.close()
    assert set(range(6)) | set(range(100, 106)) <= values
