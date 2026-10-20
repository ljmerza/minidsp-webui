import math

import numpy as np
import pytest

from app.model.config_doc import ConfigDoc, default_config_doc
from app.model.device_profile import profile_for
from app.state.repo import (
    EVENT_RETENTION,
    REVISION_RETENTION,
    ProfileNameTaken,
    RevisionConflict,
    dumps,
)

SERIAL = 911111


def doc(gain: float = 0.0) -> dict:
    return {"schema_version": 1, "outputs": [{"gain_db": gain}]}


# --- device ---------------------------------------------------------------------------------------------------


async def test_device_upsert_only_writes_on_change(repo):
    ident = {"product_name": "2x4HD", "hw_id": 10, "dsp_version": 100, "fw": "1.75", "daemon_url": "http://d"}
    assert await repo.upsert_device(SERIAL, **ident) is True
    first = await repo.get_device(SERIAL)
    assert first.first_seen == first.last_seen
    assert first.product_name == "2x4HD" and first.hw_id == 10

    assert await repo.upsert_device(SERIAL, **ident) is False
    assert await repo.get_device(SERIAL) == first

    assert await repo.upsert_device(SERIAL, **ident, touch=True) is True
    touched = await repo.get_device(SERIAL)
    assert touched.first_seen == first.first_seen and touched.last_seen > first.last_seen

    assert await repo.upsert_device(SERIAL, **{**ident, "fw": "1.76"}) is True
    assert (await repo.get_device(SERIAL)).fw == "1.76"


async def test_device_list_and_delete(repo):
    await repo.upsert_device(2)
    await repo.upsert_device(1, product_name="x")
    assert [d.serial for d in await repo.list_devices()] == [1, 2]
    assert await repo.delete_device(1) is True
    assert await repo.delete_device(1) is False
    assert await repo.get_device(1) is None


# --- preset state ---------------------------------------------------------------------------------------------


async def test_preset_state_roundtrip_and_noop(repo):
    assert await repo.get_preset_state(SERIAL, 0) is None
    assert await repo.save_preset_state(SERIAL, 0, desired=doc(1), applied=None, unknown_paths=["b", "a", "a"])
    st = await repo.get_preset_state(SERIAL, 0)
    assert st.desired == doc(1)
    assert st.applied is None
    assert st.unknown_paths == ["a", "b"]
    assert st.revision == 0

    assert await repo.save_preset_state(SERIAL, 0, desired=doc(1), unknown_paths=["a", "b"]) is False
    assert (await repo.get_preset_state(SERIAL, 0)).updated_at == st.updated_at

    assert await repo.save_preset_state(SERIAL, 0, desired=doc(1), applied=doc(1), unknown_paths=[]) is True
    st2 = await repo.get_preset_state(SERIAL, 0)
    assert st2.applied == doc(1) and st2.unknown_paths == []
    assert st2.revision == 0


async def test_preset_state_accepts_config_doc(repo):
    cfg = default_config_doc(profile_for("2x4HD"))
    await repo.save_preset_state(SERIAL, 1, desired=cfg, applied=cfg)
    st = await repo.get_preset_state(SERIAL, 1)
    assert ConfigDoc.model_validate(st.desired) == cfg
    assert ConfigDoc.model_validate(st.applied) == cfg


async def test_preset_state_list_and_delete(repo):
    for p in (2, 0, 3):
        await repo.save_preset_state(SERIAL, p, desired=doc(p))
    await repo.save_preset_state(SERIAL + 1, 0, desired=doc())
    assert [s.preset for s in await repo.list_preset_states(SERIAL)] == [0, 2, 3]
    assert await repo.delete_preset_state(SERIAL, 2) is True
    assert await repo.get_preset_state(SERIAL, 2) is None


@pytest.mark.parametrize("preset", [-1, 4])
async def test_preset_out_of_range(repo, preset):
    with pytest.raises(ValueError, match="preset"):
        await repo.save_preset_state(SERIAL, preset, desired=doc())
    with pytest.raises(ValueError, match="preset"):
        await repo.get_preset_state(SERIAL, preset)


async def test_nan_is_rejected(repo):
    with pytest.raises(ValueError):
        await repo.save_preset_state(SERIAL, 0, desired={"gain": math.nan})


def test_dumps_is_canonical():
    assert dumps({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


# --- revisions ------------------------------------------------------------------------------------------------


async def test_commit_revision_creates_history(repo):
    assert await repo.commit_revision(SERIAL, 0, doc(1), summary="first", source="adopt") == 1
    assert await repo.commit_revision(SERIAL, 0, doc(2), summary="second") == 2
    st = await repo.get_preset_state(SERIAL, 0)
    assert st.revision == 2 and st.desired == doc(2)
    assert st.applied is None and st.unknown_paths == []

    revs = await repo.list_revisions(SERIAL, 0)
    assert [(r.revision, r.summary, r.source) for r in revs] == [(2, "second", "ui"), (1, "first", "adopt")]
    r1 = await repo.get_revision(SERIAL, 0, 1)
    assert r1.desired == doc(1) and r1.summary == "first"
    assert await repo.get_revision(SERIAL, 0, 99) is None


async def test_commit_revision_skips_unchanged(repo):
    await repo.commit_revision(SERIAL, 0, doc(1))
    assert await repo.commit_revision(SERIAL, 0, doc(1)) == 1
    assert await repo.count_revisions(SERIAL, 0) == 1
    assert await repo.commit_revision(SERIAL, 0, doc(1), skip_if_unchanged=False) == 2


async def test_commit_revision_can_store_a_newer_state_than_its_history_row(repo):
    """A held-back burst records an earlier doc; the stored preset state keeps the current one."""
    assert await repo.commit_revision(SERIAL, 0, doc(1), state=doc(2)) == 1
    assert (await repo.get_revision(SERIAL, 0, 1)).desired == doc(1)
    st = await repo.get_preset_state(SERIAL, 0)
    assert st.revision == 1 and st.desired == doc(2)


async def test_commit_after_debounced_persist_of_same_doc(repo):
    """The debounced persist may already hold the new doc; the commit must still record a revision."""
    await repo.commit_revision(SERIAL, 0, doc(1))
    await repo.save_preset_state(SERIAL, 0, desired=doc(2))
    assert await repo.commit_revision(SERIAL, 0, doc(2)) == 2


async def test_commit_revision_keeps_or_sets_shadow(repo):
    await repo.save_preset_state(SERIAL, 0, desired=doc(0), applied=doc(0), unknown_paths=["outputs.0.gain_db"])
    await repo.commit_revision(SERIAL, 0, doc(1))
    st = await repo.get_preset_state(SERIAL, 0)
    assert st.applied == doc(0) and st.unknown_paths == ["outputs.0.gain_db"]

    await repo.commit_revision(SERIAL, 0, doc(2), applied=None, unknown_paths=[])
    st = await repo.get_preset_state(SERIAL, 0)
    assert st.applied is None and st.unknown_paths == []

    await repo.commit_revision(SERIAL, 0, doc(3), applied=doc(3))
    assert (await repo.get_preset_state(SERIAL, 0)).applied == doc(3)


async def test_commit_revision_optimistic_check(repo):
    await repo.commit_revision(SERIAL, 0, doc(1), expected_revision=0)
    with pytest.raises(RevisionConflict) as exc:
        await repo.commit_revision(SERIAL, 0, doc(2), expected_revision=0)
    assert (exc.value.expected, exc.value.actual) == (0, 1)
    assert (await repo.get_preset_state(SERIAL, 0)).desired == doc(1)
    assert await repo.commit_revision(SERIAL, 0, doc(2), expected_revision=1) == 2


async def test_commit_revision_rejects_unknown_source(repo):
    with pytest.raises(ValueError, match="source"):
        await repo.commit_revision(SERIAL, 0, doc(), source="bogus")


async def test_revision_retention(repo):
    total = REVISION_RETENTION + 5
    for i in range(total):
        await repo.commit_revision(SERIAL, 0, doc(i))
    await repo.commit_revision(SERIAL, 1, doc())
    assert await repo.count_revisions(SERIAL, 0) == REVISION_RETENTION
    assert await repo.count_revisions(SERIAL, 1) == 1
    oldest = (await repo.list_revisions(SERIAL, 0, limit=1000))[-1]
    assert oldest.revision == total - REVISION_RETENTION + 1
    assert (await repo.get_preset_state(SERIAL, 0)).revision == total


async def test_revision_paging(repo):
    for i in range(10):
        await repo.commit_revision(SERIAL, 0, doc(i))
    page1 = await repo.list_revisions(SERIAL, 0, limit=4)
    assert [r.revision for r in page1] == [10, 9, 8, 7]
    page2 = await repo.list_revisions(SERIAL, 0, limit=4, before=page1[-1].revision)
    assert [r.revision for r in page2] == [6, 5, 4, 3]


async def test_revision_numbering_survives_state_delete(repo):
    await repo.commit_revision(SERIAL, 0, doc(1))
    await repo.commit_revision(SERIAL, 0, doc(2))
    await repo.delete_preset_state(SERIAL, 0)
    assert await repo.commit_revision(SERIAL, 0, doc(3)) == 3

    await repo.delete_preset_state(SERIAL, 0, with_history=True)
    assert await repo.count_revisions(SERIAL, 0) == 0
    assert await repo.commit_revision(SERIAL, 0, doc(4)) == 1


# --- profiles -------------------------------------------------------------------------------------------------


async def test_profile_crud(repo):
    games = await repo.create_profile(" Games ", doc(8), description="shakers +8", product_name="2x4HD")
    movies = await repo.create_profile("movies", doc(0))
    assert games.name == "Games" and games.config == doc(8) and games.created_at == games.updated_at
    assert (await repo.get_profile(games.id)) == games
    assert (await repo.get_profile_by_name("movies")).id == movies.id
    assert [p.name for p in await repo.list_profiles()] == ["Games", "movies"]
    assert [p.name for p in await repo.list_profiles(product_name="2x4HD")] == ["Games"]

    updated = await repo.update_profile(games.id, config=doc(9), description=None)
    assert updated.config == doc(9) and updated.description is None and updated.name == "Games"
    assert updated.updated_at > games.updated_at
    assert await repo.update_profile(games.id) == updated

    with pytest.raises(ProfileNameTaken):
        await repo.create_profile("Games", doc())
    with pytest.raises(ProfileNameTaken):
        await repo.update_profile(movies.id, name="Games")
    with pytest.raises(ValueError, match="empty"):
        await repo.create_profile("  ", doc())

    assert await repo.update_profile(999, name="x") is None
    assert await repo.delete_profile(games.id) is True
    assert await repo.delete_profile(games.id) is False
    assert await repo.get_profile(games.id) is None


# --- FIR blobs ------------------------------------------------------------------------------------------------


async def test_fir_blob_roundtrip_and_dedupe(repo):
    rng = np.random.default_rng(1)
    taps = rng.standard_normal(4096) * 0.1
    blob = await repo.put_fir_blob(taps, name="room.wav")
    assert blob.n_taps == 4096 and len(blob.sha256) == 64 and blob.taps is None

    got = await repo.get_fir_blob(blob.id)
    assert got.name == "room.wav"
    np.testing.assert_array_equal(got.taps, taps)
    assert got.taps.dtype == np.float64

    again = await repo.put_fir_blob(list(taps), name="other name")
    assert again.id == blob.id and again.name == "room.wav"
    assert [b.id for b in await repo.list_fir_blobs()] == [blob.id]

    assert await repo.delete_fir_blob(blob.id) is True
    assert await repo.get_fir_blob(blob.id) is None


@pytest.mark.parametrize("bad", [[], [[1.0, 2.0]], [1.0, float("nan")], [float("inf")]])
async def test_fir_blob_rejects_bad_taps(repo, bad):
    with pytest.raises(ValueError):
        await repo.put_fir_blob(bad)


# --- channel meta ---------------------------------------------------------------------------------------------


async def test_channel_meta(repo):
    assert await repo.set_channel_meta(SERIAL, "output", 0, name=" Shakers ", color="#ff8800", max_gain_db=12.0)
    meta = await repo.get_channel_meta(SERIAL, "output", 0)
    assert (meta.name, meta.color, meta.max_gain_db) == ("Shakers", "#ff8800", 12.0)
    assert await repo.set_channel_meta(SERIAL, "output", 0, name="Shakers", color="#ff8800", max_gain_db=12.0) is False
    assert await repo.set_channel_meta(SERIAL, "output", 0, name="Shakers") is True
    assert (await repo.get_channel_meta(SERIAL, "output", 0)).color is None

    await repo.set_channel_meta(SERIAL, "input", 1, name="TV")
    await repo.set_channel_meta(SERIAL, "output", 2, name="")
    assert [(m.kind, m.idx) for m in await repo.list_channel_meta(SERIAL)] == [
        ("input", 1),
        ("output", 0),
        ("output", 2),
    ]
    assert (await repo.get_channel_meta(SERIAL, "output", 2)).name is None
    assert [m.idx for m in await repo.list_channel_meta(SERIAL, "output")] == [0, 2]
    assert await repo.delete_channel_meta(SERIAL, "input", 1) is True
    assert await repo.get_channel_meta(SERIAL, "input", 1) is None


async def test_channel_meta_flags(repo):
    """The Settings flags (migration 0002): None keeps them, so a rename or a ceiling change never clears them."""
    assert await repo.set_channel_meta(SERIAL, "output", 2, unused=True)
    meta = await repo.get_channel_meta(SERIAL, "output", 2)
    assert (meta.unused, meta.tactile) == (True, False)
    assert await repo.set_channel_meta(SERIAL, "output", 2, unused=True) is False  # unchanged: no write
    assert await repo.set_channel_meta(SERIAL, "output", 2, name="Rear", max_gain_db=6.0)
    meta = await repo.get_channel_meta(SERIAL, "output", 2)
    assert (meta.name, meta.max_gain_db, meta.unused, meta.tactile) == ("Rear", 6.0, True, False)
    assert await repo.set_channel_meta(SERIAL, "output", 2, name="Rear", max_gain_db=6.0, tactile=True)
    assert await repo.set_channel_meta(SERIAL, "output", 2, name="Rear", max_gain_db=6.0, unused=False)
    meta = (await repo.list_channel_meta(SERIAL, "output"))[0]
    assert (meta.unused, meta.tactile) == (False, True)
    assert isinstance(meta.unused, bool) and isinstance(meta.tactile, bool)
    # a new row without flags starts in use and not tactile
    await repo.set_channel_meta(SERIAL, "input", 0, name="TV")
    meta = await repo.get_channel_meta(SERIAL, "input", 0)
    assert (meta.unused, meta.tactile) == (False, False)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"kind": "bogus"}, "kind"),
        ({"color": "red"}, "color"),
        ({"max_gain_db": math.inf}, "finite"),
        ({"idx": -1}, "index"),
    ],
)
async def test_channel_meta_validation(repo, kwargs, match):
    args = {"kind": "output", "idx": 0, **kwargs}
    kind, idx = args.pop("kind"), args.pop("idx")
    with pytest.raises(ValueError, match=match):
        await repo.set_channel_meta(SERIAL, kind, idx, **args)


# --- settings -------------------------------------------------------------------------------------------------


async def test_settings(repo):
    assert await repo.get_setting("missing") is None
    assert await repo.get_setting("missing", 5) == 5
    assert await repo.set_setting("adopted", {"serial": SERIAL, "mode": "trust"}) is True
    assert await repo.set_setting("adopted", {"mode": "trust", "serial": SERIAL}) is False
    assert await repo.get_setting("adopted") == {"serial": SERIAL, "mode": "trust"}
    assert await repo.set_setting("flag", False) is True
    assert await repo.get_setting("flag", True) is False
    assert await repo.all_settings() == {"adopted": {"mode": "trust", "serial": SERIAL}, "flag": False}
    assert await repo.delete_setting("flag") is True
    assert await repo.delete_setting("flag") is False


# --- event log ------------------------------------------------------------------------------------------------


async def test_event_log(repo):
    a = await repo.log_event("adopt", "adopted via trust", data={"serial": SERIAL})
    b = await repo.log_event("apply", "write failed", level="error", data={"paths": ["outputs.0.gain_db"]})
    c = await repo.log_event("daemon", "reconnected", level="warning")
    events = await repo.list_events()
    assert [e.id for e in events] == [c, b, a]
    assert events[2].data == {"serial": SERIAL} and events[0].data is None
    assert [e.id for e in await repo.list_events(kind="apply")] == [b]
    assert [e.id for e in await repo.list_events(min_level="warning")] == [c, b]
    assert [e.id for e in await repo.list_events(before_id=c, limit=1)] == [b]
    with pytest.raises(ValueError):
        await repo.log_event("x", "y", level="loud")
    with pytest.raises(ValueError):
        await repo.list_events(min_level="loud")


async def test_event_data_falls_back_to_str(repo):
    await repo.log_event("x", "y", data={"blob": b"\x00"})
    assert (await repo.list_events())[0].data == {"blob": "b'\\x00'"}


async def test_trim_events(repo):
    ids = [await repo.log_event("k", f"m{i}") for i in range(30)]
    assert await repo.trim_events(keep=100) == 0
    assert await repo.trim_events(keep=10) == 20
    assert await repo.count_events() == 10
    assert [e.id for e in await repo.list_events()] == ids[::-1][:10]
    assert await repo.trim_events(keep=0) == 10
    with pytest.raises(ValueError):
        await repo.trim_events(keep=-1)


async def test_trim_events_default_retention(repo, db):
    async with db.transaction() as tx:
        await tx.executemany(
            "INSERT INTO event_log (ts, level, kind, message) VALUES ('t', 'info', 'k', ?)",
            [(str(i),) for i in range(EVENT_RETENTION + 3)],
        )
    assert await repo.trim_events() == 3
    assert await repo.count_events() == EVENT_RETENTION
    assert (await repo.list_events(limit=EVENT_RETENTION))[-1].message == "3"


# --- SD-card write budget -------------------------------------------------------------------------------------


async def test_noop_upserts_do_not_grow_the_wal(repo, db_path):
    await repo.save_preset_state(SERIAL, 0, desired=doc(1), applied=doc(1))
    await repo.set_setting("k", 1)
    await repo.set_channel_meta(SERIAL, "output", 0, name="Sub")
    await repo.upsert_device(SERIAL, product_name="2x4HD")
    await repo.commit_revision(SERIAL, 0, doc(1))
    wal = db_path.with_name(db_path.name + "-wal")
    size = wal.stat().st_size

    for _ in range(3):
        assert await repo.save_preset_state(SERIAL, 0, desired=doc(1), applied=doc(1)) is False
        assert await repo.set_setting("k", 1) is False
        assert await repo.set_channel_meta(SERIAL, "output", 0, name="Sub") is False
        assert await repo.upsert_device(SERIAL, product_name="2x4HD") is False
        assert await repo.commit_revision(SERIAL, 0, doc(1)) == 1
        await repo.get_preset_state(SERIAL, 0)
        await repo.list_events()
    assert wal.stat().st_size == size
