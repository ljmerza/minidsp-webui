"""SQLite snapshots, the nightly job (skip-unchanged, retention), restore validation, staging and the startup swap."""

import asyncio
import os
import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from app.model.config_doc import default_config_doc
from app.model.device_profile import M2X4HD
from app.state import Database, Repo
from app.state.db import MIGRATIONS_DIR, discover_migrations
from app.sync import backup as svc

from ..conftest import SERIAL

LATEST_SCHEMA = discover_migrations(MIGRATIONS_DIR)[-1].version


def rows(data: bytes, sql: str) -> list[tuple]:
    conn = svc._open_bytes(data)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


async def make_db(path: Path, gain: float = 0.0) -> bytes:
    """A database at ``path`` with one device and preset; returns a snapshot of it."""
    db = await Database.open(path)
    repo = Repo(db)
    await repo.upsert_device(SERIAL, product_name="2x4HD")
    doc = default_config_doc(M2X4HD).model_copy(deep=True)
    doc.outputs[0].gain_db = gain
    await repo.commit_revision(SERIAL, 0, doc, source="ui")
    await db.close()
    return svc.snapshot(path)


# --- snapshot / nightly -----------------------------------------------------------------------------------------------


async def test_snapshot_of_a_live_wal_database(repo, settings):
    await repo.upsert_device(SERIAL, product_name="2x4HD")
    first = await asyncio.to_thread(svc.snapshot, settings.db_path)
    assert first.startswith(svc.SQLITE_MAGIC)
    assert rows(first, "SELECT serial FROM device") == [(SERIAL,)]
    assert await asyncio.to_thread(svc.snapshot, settings.db_path) == first, "an unchanged database copies identically"
    await repo.set_setting("k", 1)
    assert await asyncio.to_thread(svc.snapshot, settings.db_path) != first


def test_snapshot_needs_a_file(tmp_path):
    with pytest.raises(svc.BackupError, match="not a plain file"):
        svc.snapshot(":memory:")
    with pytest.raises(svc.BackupError, match="no database"):
        svc.snapshot(tmp_path / "missing.sqlite3")
    assert not (tmp_path / "missing.sqlite3").exists()


async def test_run_backup_skips_unchanged_and_keeps_the_newest(repo, settings, tmp_path):
    out = tmp_path / "backups"
    t0 = datetime(2026, 10, 1, 3, 30, tzinfo=UTC)
    first = svc.run_backup(settings.db_path, out, keep=3, now=t0)
    assert first.path is not None and first.path.name == "minidsp-webui-20261001T033000Z.sqlite3"
    assert first.path.read_bytes() == svc.snapshot(settings.db_path)
    skipped = svc.run_backup(settings.db_path, out, keep=3, now=t0 + timedelta(days=1))
    assert skipped.path is None and skipped.sha256 == first.sha256, "found the newest file's hash on disk"
    for day in range(1, 6):
        await repo.set_setting("day", day)
        assert svc.run_backup(settings.db_path, out, keep=3, now=t0 + timedelta(days=day)).path is not None
    names = [f.name for f in svc.list_backups(out)]
    assert names == [f"minidsp-webui-202610{d:02d}T033000Z.sqlite3" for d in (6, 5, 4)]
    assert not list(out.glob("*.part"))
    again = svc.run_backup(settings.db_path, out, keep=3, now=t0 + timedelta(days=9))
    assert again.path is None, "unchanged since day 6"


def test_seconds_until():
    now = datetime(2026, 10, 16, 1, 0, 0)
    assert svc.seconds_until(time(3, 30), now) == 2.5 * 3600
    assert svc.seconds_until(time(3, 30), now.replace(hour=3, minute=30)) == 24 * 3600
    assert svc.seconds_until(time(3, 30), now.replace(hour=23)) == 4.5 * 3600


async def test_nightly_loop_backs_up_and_survives_errors(repo, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "seconds_until", lambda at, now: 0.01)
    calls = []
    real = svc.run_backup

    def flaky(*args, **kwargs):
        calls.append(kwargs.get("last_sha256"))
        if len(calls) == 1:
            raise OSError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(svc, "run_backup", flaky)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(svc.asyncio, "sleep", lambda s: real_sleep(min(s, 0.01)))
    out = tmp_path / "nightly"
    task = svc.start_nightly_backups(settings.db_path, out)
    for _ in range(200):
        if len(calls) >= 3:
            break
        await real_sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(svc.list_backups(out)) == 1, "written once, then skipped while unchanged"
    assert calls[0] is None and calls[2] is not None


def test_list_and_resolve_backup_files(tmp_path):
    out = tmp_path / "b"
    assert svc.list_backups(out) == []
    out.mkdir()
    for name in ("minidsp-webui-20261001T033000Z.sqlite3", "pre-restore-20261002T000000Z.sqlite3", "notes.txt"):
        (out / name).write_bytes(b"x")
    (out / "minidsp-webui-20261003T033000Z.sqlite3").mkdir()
    files = svc.list_backups(out)
    assert [(f.name, f.kind) for f in files] == [
        ("pre-restore-20261002T000000Z.sqlite3", "pre-restore"),
        ("minidsp-webui-20261001T033000Z.sqlite3", "nightly"),
    ]
    assert files[0].as_json()["created"] == "2026-10-02T00:00:00+00:00"
    assert svc.backup_file(out, files[1].name) == out / files[1].name
    for bad in ("notes.txt", "../minidsp-webui-20261001T033000Z.sqlite3", "minidsp-webui-20261003T033000Z.sqlite3"):
        assert svc.backup_file(out, bad) is None


def test_default_backup_dir():
    assert svc.default_backup_dir("/data/minidsp-webui.sqlite3") == Path("/data/backups")


# --- restore ----------------------------------------------------------------------------------------------------------


async def test_validate_a_good_database(tmp_path):
    data = await make_db(tmp_path / "a.sqlite3")
    info = svc.validate_db_bytes(data)
    assert info.schema_version == LATEST_SCHEMA and info.serials == [SERIAL] and info.warnings == []
    assert info.counts["preset_state"] == 1 and info.counts["revision"] == 1
    assert info.as_json()["size"] == len(data)


async def test_validate_and_stage_an_uploaded_file(tmp_path, monkeypatch):
    data = await make_db(tmp_path / "a.sqlite3")
    assert data[18:20] == b"\x02\x02", "the snapshot is a WAL-mode file"
    live = tmp_path / "data" / "state.sqlite3"
    upload = svc.upload_path(live)
    assert upload.parent == live.parent and upload.name.endswith(".upload")
    upload.write_bytes(data)
    info = svc.stage_restore_file(live, upload, dry_run=True)
    assert info.serials == [SERIAL] and info.size == len(data)
    assert sorted(p.name for p in live.parent.iterdir()) == [], "a dry run leaves nothing behind"
    upload.write_bytes(data)
    svc.stage_restore_file(live, upload)
    assert sorted(p.name for p in live.parent.iterdir()) == ["state.sqlite3.restore"]
    assert svc.restore_path(live).read_bytes() == data, "validation never modifies the file"

    bad = svc.upload_path(live)
    bad.write_bytes(b"hello")
    with pytest.raises(svc.BackupError, match="not an SQLite database"):
        svc.stage_restore_file(live, bad)
    assert not bad.exists()
    monkeypatch.setattr(svc, "MAX_DB_BYTES", 100)
    big = svc.upload_path(live)
    big.write_bytes(data)
    with pytest.raises(svc.BackupError, match="larger than"):
        svc.validate_db_file(big)
    assert svc.upload_path(":memory:").parent != live.parent


async def test_validate_warns_about_unusable_preset_state(tmp_path):
    path = tmp_path / "a.sqlite3"
    await make_db(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE preset_state SET desired_json = '{\"outputs\": 5}'")
    conn.commit()
    conn.close()
    info = svc.validate_db_bytes(svc.snapshot(path))
    assert info.warnings == [f"device {SERIAL} preset 1: unusable stored state (it would start empty)"]


def _db_bytes(tmp_path: Path, script: str) -> bytes:
    path = tmp_path / "x.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(script)
    conn.close()
    return path.read_bytes()


async def test_validate_rejects_bad_files(tmp_path, monkeypatch):
    with pytest.raises(svc.BackupError, match="not an SQLite database"):
        svc.validate_db_bytes(b"hello")
    with pytest.raises(svc.BackupError, match="can't be opened|unreadable"):
        svc.validate_db_bytes(svc.SQLITE_MAGIC + b"\x00" * 50)
    with pytest.raises(svc.BackupError, match="not a minidsp-webui database") as info:
        svc.validate_db_bytes(_db_bytes(tmp_path, "CREATE TABLE t (x);"))
    assert "missing table device" in info.value.problems

    good = await make_db(tmp_path / "good.sqlite3")
    conn = svc._open_bytes(good)
    conn.execute("INSERT INTO schema_migrations VALUES (999, 'future', 'now')")
    conn.commit()
    newer = conn.serialize()
    conn.execute("DELETE FROM schema_migrations")
    conn.commit()
    empty = conn.serialize()
    conn.close()
    with pytest.raises(svc.BackupError, match="schema") as info:
        svc.validate_db_bytes(newer)
    assert "migration 999 (future) is unknown" in info.value.problems[0]
    with pytest.raises(svc.BackupError, match="no migration history"):
        svc.validate_db_bytes(empty)

    monkeypatch.setattr(svc, "MAX_DB_BYTES", 100)
    with pytest.raises(svc.BackupError, match="larger than"):
        svc.validate_db_bytes(good)


async def test_validate_rejects_a_corrupt_database(tmp_path):
    good = bytearray(await make_db(tmp_path / "good.sqlite3"))
    page = 4096
    for offset in range(page, len(good), page):  # wreck every page but the header page
        good[offset : offset + 16] = b"\xff" * 16
    with pytest.raises(svc.BackupError, match="corrupt|unreadable|can't be opened"):
        svc.validate_db_bytes(bytes(good))


async def test_stage_and_apply_a_restore(tmp_path):
    live = tmp_path / "data" / "state.sqlite3"
    await make_db(live, gain=-1.0)
    # Leave committed data in the WAL, like a running app would.
    db = await Database.open(live)
    await Repo(db).set_setting("in-wal", True)
    wanted = await make_db(tmp_path / "wanted.sqlite3", gain=-9.0)

    assert svc.pending_restore(live) is None
    info = svc.stage_restore(live, wanted)
    assert info.serials == [SERIAL]
    assert svc.pending_restore(live)["size"] == len(wanted)
    await db.close()

    assert svc.apply_staged_restore(live) == live
    assert svc.pending_restore(live) is None
    assert not live.with_name(live.name + "-wal").exists()
    restored = svc.snapshot(live)
    assert rows(restored, "SELECT desired_json FROM preset_state")[0][0].count('"gain_db":-9.0') == 1
    (kept,) = svc.list_backups(live.parent / "backups")
    assert kept.kind == "pre-restore"
    assert rows(kept.path.read_bytes(), "SELECT key FROM setting") == [("in-wal",)], "the WAL content was kept"
    db = await Database.open(live)  # opens and migrates cleanly
    assert (await Repo(db).get_device(SERIAL)) is not None
    await db.close()
    assert svc.apply_staged_restore(live) is None, "nothing staged"


async def test_a_staged_restore_into_a_missing_database(tmp_path):
    live = tmp_path / "state.sqlite3"
    svc.stage_restore(live, await make_db(tmp_path / "w.sqlite3"))
    assert svc.apply_staged_restore(live) == live
    assert svc.list_backups(tmp_path / "backups") == []


async def test_an_invalid_staged_file_is_set_aside(tmp_path):
    live = tmp_path / "state.sqlite3"
    original = await make_db(live)
    svc.restore_path(live).write_bytes(b"junk")
    assert svc.apply_staged_restore(live) is None
    assert svc.snapshot(live) == original
    assert (tmp_path / "state.sqlite3.restore.rejected").read_bytes() == b"junk"


async def test_pre_restore_copies_are_pruned(tmp_path):
    live = tmp_path / "state.sqlite3"
    await make_db(live)
    out = tmp_path / "backups"
    out.mkdir()
    for day in range(1, 6):
        (out / f"pre-restore-202601{day:02d}T000000Z.sqlite3").write_bytes(b"old")
    svc.stage_restore(live, await make_db(tmp_path / "w.sqlite3"))
    svc.apply_staged_restore(live)
    kept = [f.name for f in svc.list_backups(out)]
    assert len(kept) == svc.KEEP_PRE_RESTORE and "pre-restore-20260105T000000Z.sqlite3" in kept


async def test_unreadable_current_database_is_kept_raw(tmp_path):
    live = tmp_path / "state.sqlite3"
    live.write_bytes(b"this is not sqlite")
    svc.stage_restore(live, await make_db(tmp_path / "w.sqlite3"))
    assert svc.apply_staged_restore(live) == live
    (kept,) = svc.list_backups(tmp_path / "backups")
    assert kept.path.read_bytes() == b"this is not sqlite"


def test_cancel_restore(tmp_path):
    live = tmp_path / "state.sqlite3"
    assert svc.cancel_restore(live) is False
    svc.restore_path(live).write_bytes(b"x")
    assert svc.cancel_restore(live) is True and svc.pending_restore(live) is None
    assert svc.cancel_restore(":memory:") is False and svc.pending_restore(":memory:") is None
    assert svc.apply_staged_restore(":memory:") is None


def test_write_is_atomic(tmp_path, monkeypatch):
    target = tmp_path / "d" / "f.bin"
    svc._write_atomically(target, b"one")

    def boom(*_args):
        raise OSError("no space")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        svc._write_atomically(target, b"two")
    assert target.read_bytes() == b"one"
