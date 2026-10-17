"""Desired-state edits: merge patch, validation, guards, persistence and revisions."""

import asyncio

import pytest

from app.bus import DESIRED
from app.model.config_doc import ConfigDoc, default_config_doc
from app.model.device_profile import M2X4HD
from app.state import RevisionConflict
from app.sync.session import (
    ConfirmationRequired,
    DesiredValidationError,
    StateUnavailable,
    UnknownPreset,
    describe_change,
    merge_patch,
    parse_doc,
)

from ..conftest import SERIAL
from .conftest import drain, settle

# --- merge patch ---------------------------------------------------------------------------------------------------


def test_merge_patch_objects_and_list_indexes():
    target = {"a": 1, "b": {"c": 2, "d": 3}, "l": [{"x": 1}, {"x": 2}]}
    out = merge_patch(target, {"b": {"c": 5}, "l": {"1": {"x": 9}}})
    assert out == {"a": 1, "b": {"c": 5, "d": 3}, "l": [{"x": 1}, {"x": 9}]}
    assert target["l"][1] == {"x": 2}  # not modified in place


def test_merge_patch_array_replaces_and_null_sets_null():
    assert merge_patch({"l": [1, 2]}, {"l": [3]}) == {"l": [3]}
    assert merge_patch({"d": {"k": 1}}, {"d": None}) == {"d": None}
    assert merge_patch({"d": None}, {"d": {"k": 1}}) == {"d": {"k": 1}}


@pytest.mark.parametrize(
    ("patch", "where"),
    [
        ({"nope": 1}, "nope: unknown field"),
        ({"b": {"zz": 1}}, "b.zz: unknown field"),
        ({"l": {"5": {}}}, "l.5: no such list index"),
        ({"l": {"-1": {}}}, "l.-1: no such list index"),
        ({"l": {"x": {}}}, "l.x: no such list index"),
    ],
)
def test_merge_patch_rejects_unknown_targets(patch, where):
    with pytest.raises(DesiredValidationError) as err:
        merge_patch({"b": {"c": 1}, "l": [{}, {}]}, patch)
    assert where in err.value.problems[0]


def test_parse_doc_is_strict():
    data = default_config_doc(M2X4HD).model_dump(mode="json")
    data["outputs"][0]["mute"] = "true"
    with pytest.raises(DesiredValidationError) as err:
        parse_doc(data)
    assert err.value.problems[0].startswith("outputs.0.mute:")


def test_describe_change():
    a = default_config_doc(M2X4HD)
    b = a.model_copy(deep=True)
    b.outputs[0].gain_db = 3.0
    assert describe_change(a, b, ["outputs.0.gain_db"]) == "outputs.0.gain_db: 0 → 3"
    assert describe_change(a, b, ["outputs.0.peq.1.compiled"]) == "outputs.0.peq.1.compiled changed"
    assert describe_change(a, b, list("abcd")) == "4 changes: a, b, c, …"
    assert describe_change(None, b, []) == "initial state"


# --- loading -------------------------------------------------------------------------------------------------------


async def test_fresh_device_loads_defaults_unknown_and_writes_nothing(session, repo, fake):
    assert session.loaded and session.needs_adoption
    assert session.desired(0) == default_config_doc(M2X4HD)
    assert set(session.sync_status(0).values()) == {"unknown"}
    assert session.revisions == {0: 0, 1: 0, 2: 0, 3: 0}
    await asyncio.sleep(0.05)
    assert fake.configs == []
    assert await repo.db.fetchval("SELECT COUNT(*) FROM preset_state") == 0
    info = session.info()
    assert info["state_loaded"] is True and info["adopted"] is False


async def test_state_unavailable_until_loaded(make_session, repo):
    s = await make_session(start=False)
    s.loaded = False
    with pytest.raises(StateUnavailable):
        s.sync_status(0)
    await repo.db.close()
    with pytest.raises(StateUnavailable):
        await s.get_desired(0)
    assert s.load_error


async def test_unknown_preset(session):
    with pytest.raises(UnknownPreset):
        await session.get_desired(4)
    with pytest.raises(UnknownPreset):
        session.sync_status(-1)


async def test_corrupt_stored_state_falls_back_to_defaults(make_session, repo):
    await repo.save_preset_state(SERIAL, 1, desired={"inputs": [], "outputs": []})
    await repo.save_preset_state(SERIAL, 2, desired={"outputs": "nope"})
    s = await make_session()
    assert s.desired(1) == default_config_doc(M2X4HD)
    assert s.desired(2) == default_config_doc(M2X4HD)
    await drain(s)
    events = await repo.list_events(kind="state")
    assert len(events) == 2 and all(e.level == "error" for e in events)


# --- edits ---------------------------------------------------------------------------------------------------------


async def test_edit_active_preset_writes_and_commits(session, fake, repo, bus):
    sub = bus.subscribe([DESIRED])
    result = await session.edit(0, {"outputs": {"0": {"gain_db": 3.0}}}, commit=True)
    assert result.paths == ["outputs.0.gain_db"] and result.device_paths == ["outputs.0.gain_db"]
    assert result.revision == 1
    assert (await settle(session)).ok
    assert fake.configs == [{"outputs": [{"index": 0, "gain": 3.0}]}]
    assert session.sync_status(0)["outputs.0.gain_db"] == "synced"
    edit_event = sub.get_nowait().data
    assert edit_event["event"] == "edit" and edit_event["values"] == {"outputs.0.gain_db": 3.0}
    assert sub.get_nowait().data["event"] == "commit"
    [rev] = await repo.list_revisions(SERIAL, 0)
    assert (rev.revision, rev.source, rev.summary) == (1, "api", "outputs.0.gain_db: 0 → 3")


async def test_routing_enable_is_sent_as_inverted_mute(session, fake):
    await session.edit_channel(0, "input", 1, {"routing": {"2": {"enabled": True, "gain_db": -3.0}}})
    await settle(session)
    assert fake.configs == [{"inputs": [{"index": 1, "routing": [{"index": 2, "mute": False, "gain": -3.0}]}]}]


async def test_set_value_and_patch_desired(session, fake):
    doc = await session.patch_desired(0, {"outputs": {"1": {"invert": True}}})
    assert doc.outputs[1].invert is True
    result = await session.set_value(0, "outputs.1.mute", True)
    assert result.device_paths == ["outputs.1.mute"]
    with pytest.raises(DesiredValidationError):
        await session.set_value(0, "outputs..mute", True)
    await settle(session)
    written = {k: v for out in fake.outputs_written() for k, v in out.items()}
    assert written == {"index": 1, "invert": True, "mute": True}  # usually coalesced into one call


async def test_noop_edit_changes_nothing(session, fake, bus):
    sub = bus.subscribe([DESIRED])
    result = await session.edit(0, {"outputs": {"0": {"gain_db": 0.0}}})
    assert result.paths == [] and result.revision == 0
    assert sub.get_nowait() is None
    assert fake.configs == []


async def test_delay_is_rounded_to_whole_samples(session, fake):
    result = await session.edit(0, {"outputs": {"0": {"delay_ms": 9.004}}})
    assert result.doc.outputs[0].delay_ms == 9.0  # 864 samples
    await settle(session)
    assert fake.outputs_written() == [{"index": 0, "delay": {"secs": 0, "nanos": 9_000_000}}]


@pytest.mark.parametrize(
    ("patch", "problem"),
    [
        ({"outputs": {"0": {"gain_db": 13.0}}}, "outputs.0.gain_db: 13.0 dB outside -72.0..12.0"),
        ({"inputs": {"0": {"routing": {"1": {"gain_db": -80.0}}}}}, "inputs.0.routing.1.gain_db"),
        ({"outputs": {"3": {"delay_ms": 80.0}}}, "outputs.3.delay_ms"),
        ({"outputs": {"0": {"compressor": {"threshold_db": 5.0}}}}, "threshold 5 dB outside -90..0"),
        ({"outputs": {"0": {"compressor": {"ratio": -1.0}}}}, "ratio -1 outside 1..100"),
        ({"outputs": {"0": {"compressor": {"ratio": 0.5}}}}, "ratio 0.5 outside 1..100"),
        ({"outputs": {"0": {"compressor": {"attack_ms": 0.0}}}}, "attack 0 ms outside 0.1..2000"),
        ({"outputs": {"0": {"compressor": {"release_ms": 0.0}}}}, "release 0 ms outside 1..5000"),
        ({"outputs": {"0": {"peq": {"0": {"compiled": {"a2": 1.5}}}}}}, "outputs.0.peq.0.compiled"),
        ({"outputs": {"0": {"mute": "yes"}}}, "outputs.0.mute"),
        ({"outputs": [{"gain_db": 0.0}]}, "outputs: expected 4 entries"),
        ({"outputs": {"9": {"gain_db": 0.0}}}, "no such list index"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
async def test_invalid_edits_never_reach_the_daemon(session, fake, patch, problem):
    with pytest.raises(DesiredValidationError) as err:
        await session.edit(0, patch)
    assert any(problem in p for p in err.value.problems), err.value.problems
    assert session.desired(0) == default_config_doc(M2X4HD)
    await asyncio.sleep(0.03)
    assert fake.configs == []


async def test_patch_must_be_an_object(session):
    with pytest.raises(DesiredValidationError):
        await session.edit(0, [1, 2])  # type: ignore[arg-type]
    with pytest.raises(DesiredValidationError):
        await session.edit_channel(0, "output", 4, {"gain_db": 1.0})
    with pytest.raises(DesiredValidationError):
        await session.edit_channel(0, "speaker", 0, {"gain_db": 1.0})
    with pytest.raises(ValueError):
        await session.edit(0, {}, source="hacker")


async def test_gain_step_guard(session, fake):
    with pytest.raises(ConfirmationRequired) as err:
        await session.edit(0, {"outputs": {"2": {"gain_db": 6.5}}})
    assert "outputs.2.gain_db by 6.5 dB" in str(err.value)
    with pytest.raises(ConfirmationRequired):
        await session.edit(0, {"inputs": {"0": {"routing": {"0": {"gain_db": 7.0}}}}})
    await session.edit(0, {"outputs": {"2": {"gain_db": 6.0}}})  # exactly the threshold is fine
    await session.edit(0, {"outputs": {"2": {"gain_db": -20.0}}})  # lowering is never guarded
    await session.edit(0, {"outputs": {"2": {"gain_db": 12.0}}}, confirm=True)
    await settle(session)
    assert fake.outputs_written()[-1] == {"index": 2, "gain": 12.0}


async def test_channel_ceiling_from_channel_meta(session, repo):
    await session.set_channel_meta("output", 0, name="Shakers", max_gain_db=10.0)
    assert (await repo.get_channel_meta(SERIAL, "output", 0)).name == "Shakers"
    assert session.channel_ceiling("output", 0) == 10.0
    assert session.channel_ceiling("output", 1) == M2X4HD.gain_max_db
    with pytest.raises(DesiredValidationError) as err:
        await session.edit(0, {"outputs": {"0": {"gain_db": 10.5}}})
    assert "ceiling of 10 dB" in str(err.value)
    with pytest.raises(DesiredValidationError):  # routes into output 0 share its ceiling
        await session.edit(0, {"inputs": {"1": {"routing": {"0": {"gain_db": 11.0}}}}})
    await session.edit(0, {"outputs": {"0": {"gain_db": 10.0}}}, confirm=True)
    with pytest.raises(DesiredValidationError):
        await session.set_channel_meta("output", 0, max_gain_db=30.0)
    with pytest.raises(DesiredValidationError):
        await session.set_channel_meta("output", 7, name="x")
    with pytest.raises(DesiredValidationError):
        await session.set_channel_meta("input", 0, color="red")


async def test_channel_meta_without_repo(make_session):
    s = await make_session(with_repo=False)
    meta = await s.set_channel_meta("input", 1, name="  ", max_gain_db=0.0)
    assert meta.name is None and s.channel_ceiling("input", 1) == 0.0


# --- inactive presets ----------------------------------------------------------------------------------------------


async def test_edit_inactive_preset_is_staged(session, fake):
    result = await session.edit(2, {"outputs": {"1": {"gain_db": -3.0}}})
    assert result.device_paths == ["outputs.1.gain_db"]
    flush = await settle(session, 2)
    assert flush.staged and flush.pending == ("outputs.1.gain_db",)
    assert fake.configs == []
    assert session.sync_summary(2)["staged"] is True
    assert session.sync_summary(2)["active"] is False


# --- revisions -----------------------------------------------------------------------------------------------------


async def test_idle_commit_after_drag(make_session, repo):
    s = await make_session(commit_idle_s=0.1)
    for gain in (-10.0, -9.0, -8.0):
        await s.edit(0, {"outputs": {"0": {"gain_db": gain}}}, source="ui")
        assert s.sync_summary(0)["uncommitted"] is True
    assert await repo.count_revisions(SERIAL, 0) == 0
    await asyncio.sleep(0.25)
    await drain(s)
    [rev] = await repo.list_revisions(SERIAL, 0)
    assert rev.summary == "outputs.0.gain_db: 0 → -8" and rev.source == "ui"
    assert s.revisions[0] == 1 and s.sync_summary(0)["uncommitted"] is False


async def test_explicit_commit_and_summary_of_several_changes(session, repo):
    await session.edit(1, {"outputs": {"0": {"gain_db": -1.0}, "1": {"gain_db": -2.0}}})
    assert await session.commit(1) == 1
    assert await session.commit(1) == 1  # nothing new
    [rev] = await repo.list_revisions(SERIAL, 1)
    assert rev.summary == "2 changes: outputs.0.gain_db, outputs.1.gain_db"
    history = await session.history(1)
    assert [h.revision for h in history] == [1]


async def test_if_match(session):
    await session.edit(0, {"outputs": {"0": {"gain_db": -1.0}}}, commit=True)
    with pytest.raises(RevisionConflict) as err:
        await session.edit(0, {"outputs": {"0": {"gain_db": -2.0}}}, expected_revision=0)
    assert (err.value.expected, err.value.actual) == (0, 1)
    await session.edit(0, {"outputs": {"0": {"gain_db": -2.0}}}, expected_revision=1)
    # Someone else's uncommitted edit is committed first, so a client holding revision 1 is now stale.
    with pytest.raises(RevisionConflict):
        await session.edit(0, {"outputs": {"0": {"gain_db": -3.0}}}, expected_revision=1)
    assert session.revisions[0] == 2


async def test_revisions_without_repo(make_session):
    s = await make_session(with_repo=False)
    result = await s.edit(0, {"outputs": {"0": {"gain_db": -1.0}}}, commit=True)
    assert result.revision == 1
    assert await s.history(0) == []


async def test_failed_revision_write_keeps_edits_uncommitted(session, repo):
    await session.edit(0, {"outputs": {"0": {"gain_db": -1.0}}})
    await repo.db.close()
    with pytest.raises(StateUnavailable):
        await session.commit(0)
    assert session.sync_summary(0)["uncommitted"] is True


# --- persistence ---------------------------------------------------------------------------------------------------


async def test_state_survives_a_restart_without_writes(make_session, fake, repo):
    s = await make_session()
    await s.edit(0, {"outputs": {"0": {"gain_db": -4.0}}})
    await s.edit(1, {"outputs": {"3": {"mute": True}}})  # staged: preset 1 is inactive
    await settle(s)
    await s.stop()  # flushes the debounced persist and commits pending edits
    writes = len(fake.configs)
    assert writes == 1

    again = await make_session()
    assert again.desired(0).outputs[0].gain_db == -4.0
    assert again.sync_status(0)["outputs.0.gain_db"] == "synced"  # the shadow is trusted across restarts
    assert again.desired(1).outputs[3].mute is True
    assert again.revisions == {0: 1, 1: 1, 2: 0, 3: 0}
    await asyncio.sleep(0.05)
    assert len(fake.configs) == writes  # nothing re-written on startup
    assert (await settle(again, 1)).staged  # the staged edit is still staged


async def test_load_recovers_pending_writes(make_session, fake, repo):
    doc = default_config_doc(M2X4HD)
    doc.outputs[2].gain_db = -5.0
    applied = {"outputs.2.gain_db": 0.0}
    await repo.save_preset_state(SERIAL, 0, desired=doc, applied=applied)
    s = await make_session()
    await settle(s)
    assert fake.outputs_written() == [{"index": 2, "gain": -5.0}]
    assert s.sync_status(0)["outputs.2.gain_db"] == "synced"


async def test_replace_desired_is_a_mute_wrapped_bulk_apply(session, fake):
    doc = ConfigDoc.model_validate(default_config_doc(M2X4HD).model_dump())
    doc.outputs[1].gain_db = -6.0
    result = await session.replace_desired(0, doc, source="profile", summary="load profile Movies")
    assert result.flush is not None and result.flush.ok
    assert result.revision == 1
    outputs = fake.outputs_written()
    assert outputs[0] == {"index": 1, "mute": True}
    assert {"index": 1, "gain": -6.0} in outputs
    assert outputs[-1] == {"index": 1, "mute": False}
