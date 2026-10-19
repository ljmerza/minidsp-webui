"""Coalesced commits: a burst of +/- presses on the same setting is one revision row (and one SQLite write)."""

import asyncio

import pytest

from app.sync import profiles as svc
from app.sync.session import COMMIT_COALESCE_S

from ..conftest import SERIAL
from .conftest import drain, settle

WINDOW = 0.15  # the tests' coalescing window (the app's is COMMIT_COALESCE_S); idle commits are pushed out of the way


@pytest.fixture
def commits(repo, monkeypatch):
    """Every `Repo.commit_revision` call (each is one SQLite transaction)."""
    calls: list[str] = []
    real = repo.commit_revision

    async def counted(*args, **kwargs):
        calls.append(kwargs.get("summary", ""))
        return await real(*args, **kwargs)

    monkeypatch.setattr(repo, "commit_revision", counted)
    return calls


async def press(session, preset: int, path: str, value, *, source: str = "ui"):
    """One +/- press as the editors' endpoint sends it: a committed, coalescing edit."""
    return await session.set_value(preset, path, value, commit=True, coalesce=True, source=source)


async def wait_window(session) -> None:
    await asyncio.sleep(WINDOW * 2)
    await drain(session)


def test_default_window_is_about_two_seconds():
    assert COMMIT_COALESCE_S == 2.0


async def test_burst_is_one_revision_with_first_old_and_last_new(make_session, repo, fake, commits):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=WINDOW)
    await s.set_value(0, "outputs.0.gain_db", 12.0, commit=True, confirm=True)  # an explicit commit: revision 1
    assert commits == ["outputs.0.gain_db: 0 → 12"]
    for gain in range(11, 1, -1):  # ten presses, 12 → 2
        result = await press(s, 0, "outputs.0.gain_db", float(gain))
        assert result.revision == 1  # the burst isn't recorded yet
    assert await repo.count_revisions(SERIAL, 0) == 1
    assert s.sync_summary(0)["uncommitted"] is True
    # The device writes didn't wait for the revision.
    await settle(s)
    assert {"index": 0, "gain": 2.0} in fake.outputs_written()
    assert await repo.count_revisions(SERIAL, 0) == 1

    await wait_window(s)
    rows = await repo.list_revisions(SERIAL, 0)
    assert [(r.revision, r.summary, r.source) for r in rows] == [
        (2, "outputs.0.gain_db: 12 → 2", "ui"),
        (1, "outputs.0.gain_db: 0 → 12", "api"),
    ]
    assert len(commits) == 2  # one write for the whole burst
    assert s.revisions[0] == 2 and s.sync_summary(0)["uncommitted"] is False
    assert (await repo.get_preset_state(SERIAL, 0)).desired["outputs"][0]["gain_db"] == 2.0


async def test_device_writes_are_not_coalesced(make_session, fake):
    """Every press reaches the device as it did before coalescing (the writer's own coalescing aside)."""
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=WINDOW)
    for gain in (-1.0, -2.0, -3.0):
        await press(s, 0, "outputs.0.gain_db", gain)
        await settle(s)
    gains = [o["gain"] for o in fake.outputs_written() if o.get("index") == 0 and "gain" in o]
    assert gains == [-1.0, -2.0, -3.0]


async def test_presses_further_apart_than_the_window_are_separate_revisions(make_session, repo):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=WINDOW)
    await press(s, 0, "outputs.0.gain_db", -1.0)
    await wait_window(s)
    await press(s, 0, "outputs.0.gain_db", -2.0)
    await wait_window(s)
    rows = await repo.list_revisions(SERIAL, 0)
    assert [r.summary for r in rows] == ["outputs.0.gain_db: -1 → -2", "outputs.0.gain_db: 0 → -1"]


async def test_each_press_restarts_the_window(make_session, repo):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=WINDOW)
    for gain in (-1.0, -2.0, -3.0, -4.0):
        await press(s, 0, "outputs.0.gain_db", gain)
        await asyncio.sleep(WINDOW * 0.6)  # each gap is shorter than the window; the burst is longer than it
    assert await repo.count_revisions(SERIAL, 0) == 0
    await wait_window(s)
    [row] = await repo.list_revisions(SERIAL, 0)
    assert row.summary == "outputs.0.gain_db: 0 → -4"


async def test_shutdown_writes_the_held_burst(make_session, repo, commits):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=60.0)
    for gain in (-1.0, -2.0, -3.0):
        await press(s, 0, "outputs.0.gain_db", gain)
    assert commits == []
    await s.stop()
    [row] = await repo.list_revisions(SERIAL, 0)
    assert row.summary == "outputs.0.gain_db: 0 → -3"
    assert commits == ["outputs.0.gain_db: 0 → -3"]
    assert s._bursts == {}


async def test_explicit_commits_are_immediate_and_come_after_the_burst(make_session, repo):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=60.0)
    await press(s, 0, "outputs.0.gain_db", -1.0)
    await press(s, 0, "outputs.0.gain_db", -2.0)
    # Apply-style commit (no coalescing) of another setting: the burst is written first, as its own revision.
    result = await s.set_value(0, "outputs.1.gain_db", -5.0, commit=True)
    assert result.revision == 2
    rows = await repo.list_revisions(SERIAL, 0)
    assert [r.summary for r in rows] == ["outputs.1.gain_db: 0 → -5", "outputs.0.gain_db: 0 → -2"]
    # A plain commit() and a whole-document replace (profile load) are immediate too.
    await press(s, 0, "outputs.0.gain_db", -3.0)
    assert await s.commit(0) == 3
    await press(s, 0, "outputs.0.gain_db", -4.0)
    doc = s.desired(0).model_copy(deep=True)
    doc.outputs[2].gain_db = -6.0
    replaced = await s.replace_desired(0, doc, source="profile", summary="Loaded profile X", bulk=False)
    assert replaced.revision == 5
    rows = await repo.list_revisions(SERIAL, 0, limit=2)
    assert [(r.summary, r.source) for r in rows] == [
        ("Loaded profile X", "profile"),
        ("outputs.0.gain_db: -3 → -4", "ui"),
    ]
    assert s._bursts == {}


async def test_another_setting_or_source_closes_the_burst(make_session, repo):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=60.0)
    await press(s, 0, "outputs.0.gain_db", -1.0)
    await press(s, 0, "outputs.0.gain_db", -2.0)
    await press(s, 0, "outputs.1.gain_db", -1.0)  # another setting: output 0's burst is written now
    assert [r.summary for r in await repo.list_revisions(SERIAL, 0)] == ["outputs.0.gain_db: 0 → -2"]
    await press(s, 0, "outputs.1.gain_db", -2.0, source="mqtt")  # same setting, another source
    rows = await repo.list_revisions(SERIAL, 0)
    assert [(r.summary, r.source) for r in rows] == [
        ("outputs.1.gain_db: 0 → -1", "ui"),
        ("outputs.0.gain_db: 0 → -2", "ui"),
    ]
    await s.commit(0)
    assert (await repo.list_revisions(SERIAL, 0))[0].summary == "outputs.1.gain_db: -1 → -2"


async def test_bursts_are_per_preset(make_session, repo):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=WINDOW)
    await press(s, 0, "outputs.0.gain_db", -1.0)
    await press(s, 1, "outputs.0.gain_db", -1.0)
    await press(s, 0, "outputs.0.gain_db", -2.0)
    await press(s, 1, "outputs.0.gain_db", -2.0)
    await wait_window(s)
    assert [r.summary for r in await repo.list_revisions(SERIAL, 0)] == ["outputs.0.gain_db: 0 → -2"]
    assert [r.summary for r in await repo.list_revisions(SERIAL, 1)] == ["outputs.0.gain_db: 0 → -2"]


async def test_press_that_changes_nothing_extends_the_burst(make_session, repo):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=60.0)
    await press(s, 0, "outputs.0.gain_db", -1.0)
    result = await press(s, 0, "outputs.0.gain_db", -1.0)  # e.g. a press at the end of the range
    assert result.paths == [] and await repo.count_revisions(SERIAL, 0) == 0
    await press(s, 0, "outputs.0.gain_db", -2.0)
    await s.commit(0)
    assert [r.summary for r in await repo.list_revisions(SERIAL, 0)] == ["outputs.0.gain_db: 0 → -2"]


async def test_burst_back_to_where_it_started_writes_nothing(make_session, repo, commits):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=WINDOW)
    await press(s, 0, "outputs.0.gain_db", -1.0)
    await press(s, 0, "outputs.0.gain_db", 0.0)
    await wait_window(s)
    assert commits == [] and s.sync_summary(0)["uncommitted"] is False


async def test_drag_after_the_burst_stays_uncommitted(make_session, repo):
    """The burst's revision records the doc as of its last press; a later uncommitted drag elsewhere isn't in it."""
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=WINDOW)
    await press(s, 0, "outputs.0.gain_db", -1.0)
    await s.set_value(0, "outputs.1.gain_db", -4.0, source="ui")  # a drag tick: no commit
    await wait_window(s)
    [row] = await repo.list_revisions(SERIAL, 0)
    assert row.summary == "outputs.0.gain_db: 0 → -1"
    stored = (await repo.get_revision(SERIAL, 0, 1)).desired
    assert stored["outputs"][1]["gain_db"] == 0.0
    assert s.sync_summary(0)["uncommitted"] is True
    assert await s.commit(0) == 2
    assert (await repo.list_revisions(SERIAL, 0))[0].summary == "outputs.1.gain_db: 0 → -4"


async def test_history_shows_the_burst_and_reverting_before_it_undoes_all_of_it(make_session, repo):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=60.0)
    await s.set_value(0, "outputs.0.gain_db", -6.0, commit=True)  # revision 1
    for gain in (-5.0, -4.0, -3.0, -2.0):
        await press(s, 0, "outputs.0.gain_db", gain)
    history = await s.history(0)
    assert [(h.revision, h.summary) for h in history] == [
        (2, "outputs.0.gain_db: -6 → -2"),
        (1, "outputs.0.gain_db: 0 → -6"),
    ]
    result = await svc.revert_to(s, 0, 1, timeout=3.0)
    assert result.revision == 3
    assert s.desired(0).outputs[0].gain_db == -6.0
    assert (await repo.list_revisions(SERIAL, 0))[0].summary == "Reverted to revision 1"


async def test_revert_right_after_a_burst_records_the_burst_first(make_session, repo):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=60.0)
    await s.set_value(0, "outputs.0.gain_db", -6.0, commit=True)
    for gain in (-5.0, -4.0):
        await press(s, 0, "outputs.0.gain_db", gain)
    await svc.revert_to(s, 0, 1, timeout=3.0)  # without reading the history first
    rows = await repo.list_revisions(SERIAL, 0)
    assert [r.summary for r in rows] == [
        "Reverted to revision 1",
        "outputs.0.gain_db: -6 → -4",
        "outputs.0.gain_db: 0 → -6",
    ]
    assert s.desired(0).outputs[0].gain_db == -6.0


async def test_if_match_counts_the_held_burst(make_session):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=60.0)
    await press(s, 0, "outputs.0.gain_db", -1.0)
    result = await s.set_value(0, "outputs.1.gain_db", -1.0, expected_revision=1)
    assert result.revision == 1


async def test_zero_window_commits_at_once(make_session, repo):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=0)
    await press(s, 0, "outputs.0.gain_db", -1.0)
    await press(s, 0, "outputs.0.gain_db", -2.0)
    assert await repo.count_revisions(SERIAL, 0) == 2


async def test_failed_burst_write_keeps_the_edits_uncommitted(make_session, repo, monkeypatch):
    s = await make_session(commit_idle_s=10.0, commit_coalesce_s=WINDOW)

    async def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(repo, "commit_revision", broken)
    await press(s, 0, "outputs.0.gain_db", -1.0)
    await wait_window(s)
    assert s.sync_summary(0)["uncommitted"] is True
    monkeypatch.undo()
    assert await s.commit(0) == 1
    assert [r.summary for r in await repo.list_revisions(SERIAL, 0)] == ["outputs.0.gain_db: 0 → -1"]
