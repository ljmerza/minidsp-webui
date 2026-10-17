"""The daily retention job (docs/06: newest 5000 events, newest 200 revisions per preset; docs/01 SD-card rule)."""

import asyncio

import pytest

from app.model.config_doc import default_config_doc
from app.model.device_profile import M2X4HD
from app.state import Database, Repo
from app.sync import health

SERIAL = 911111


@pytest.fixture
async def repo(tmp_path):
    db = await Database.open(tmp_path / "retention.sqlite3")
    yield Repo(db)
    await db.close()


async def commit_revisions(repo: Repo, preset: int, n: int) -> None:
    doc = default_config_doc(M2X4HD)
    for i in range(n):
        doc.outputs[0].gain_db = -float(i)
        await repo.commit_revision(SERIAL, preset, doc.model_dump(mode="json"), summary=f"rev {i}")


async def test_events_are_trimmed_to_the_newest(repo):
    for i in range(30):
        await repo.log_event("test", f"event {i}")
    deleted = await health.run_retention(repo, events=10)
    assert deleted["events"] == 20
    events = await repo.list_events(limit=100)
    assert [e.message for e in events] == [f"event {i}" for i in range(29, 19, -1)]


async def test_revisions_are_trimmed_per_preset(repo):
    await commit_revisions(repo, 0, 15)
    await commit_revisions(repo, 1, 4)
    assert await health.trim_revisions(repo, keep=10) == 5
    assert await repo.count_revisions(SERIAL, 0) == 10
    assert await repo.count_revisions(SERIAL, 1) == 4
    newest = await repo.list_revisions(SERIAL, 0, limit=100)
    assert [r.revision for r in newest] == list(range(15, 5, -1))
    assert await health.trim_revisions(repo, keep=10) == 0  # idempotent
    with pytest.raises(ValueError):
        await health.trim_revisions(repo, keep=0)


async def test_one_failing_step_does_not_skip_the_others(repo, monkeypatch, caplog):
    await commit_revisions(repo, 0, 6)

    async def locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "trim_events", locked)
    deleted = await health.run_retention(repo, revisions=2)
    assert deleted == {"events": 0, "revisions": 4}
    assert "event log trim failed" in caplog.text


async def test_wal_is_checkpointed(repo, tmp_path):
    for i in range(200):
        await repo.log_event("test", "x" * 200, data={"i": i})
    wal = tmp_path / "retention.sqlite3-wal"
    await health.run_retention(repo)
    assert not wal.exists() or wal.stat().st_size == 0


async def test_maintenance_loop_runs_and_repeats(repo, monkeypatch):
    runs: list[Repo] = []

    async def fake_retention(r):
        runs.append(r)
        if len(runs) == 1:
            raise RuntimeError("boom")  # a failing run doesn't end the loop

    monkeypatch.setattr(health, "run_retention", fake_retention)
    task = asyncio.create_task(health.maintenance_loop(repo, interval_s=0.01, first_delay_s=0))
    for _ in range(100):
        if len(runs) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(runs) >= 3 and runs[0] is repo
