"""Master change audit and the UI-only big volume-drop guard (docs/02 §A, docs/08 Phase 6 acceptance)."""

import asyncio

import pytest

from app.model.master import MasterStatus
from app.sync.session import ConfirmationRequired, MasterAudit, describe_master_change

from .conftest import drain

QUIET = 0.05


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_audit(**kwargs):
    emitted: list[tuple] = []
    audit = MasterAudit(lambda *entry: emitted.append(entry), quiet_s=QUIET, **kwargs)
    audit.observe({"volume": -20.0, "mute": False, "source": "Analog", "preset": 0})  # baseline
    return audit, emitted


async def quiet() -> None:
    await asyncio.sleep(QUIET * 3)


def test_describe_master_change():
    assert describe_master_change("ui", "volume", -20.0, -40.0) == "ui: volume −20.0 → −40.0 dB"
    assert describe_master_change("ha", "mute", False, True) == "ha: mute off → on"
    assert describe_master_change("external", "preset", 0, 1) == "external: preset 1 → 2"
    assert describe_master_change("api", "source", "Analog", "Toslink") == "api: source Analog → Toslink"


async def test_first_status_is_a_baseline_not_a_change():
    emitted = []
    audit = MasterAudit(lambda *e: emitted.append(e), quiet_s=QUIET)
    audit.observe({"volume": -20.0, "mute": None})
    audit.observe({"volume": -20.0, "mute": True})  # mute was unknown: its first value is a baseline too
    await quiet()
    assert emitted == [] and audit.open_fields == []


async def test_a_drag_is_one_entry_after_the_quiet_period():
    audit, emitted = make_audit()
    with audit.acting(["volume"], "ui"):
        for v in (-22.0, -25.5, -31.0, -38.0, -40.0):
            audit.observe({"volume": v})
            await asyncio.sleep(QUIET / 5)
    assert emitted == []  # still moving: nothing written yet
    await quiet()
    assert emitted == [("ui", "volume", -20.0, -40.0)]


async def test_unmarked_change_is_external():
    audit, emitted = make_audit()
    audit.observe({"volume": -30.0, "preset": 2})
    await quiet()
    assert sorted(emitted) == [("external", "preset", 0, 2), ("external", "volume", -20.0, -30.0)]


async def test_a_drag_back_to_the_start_writes_nothing():
    audit, emitted = make_audit()
    with audit.acting(["volume"], "ui"):
        audit.observe({"volume": -30.0})
        audit.observe({"volume": -20.0})
    await quiet()
    assert emitted == []


async def test_another_source_closes_the_open_entry():
    clock = Clock()
    audit, emitted = make_audit(clock=clock, grace_s=1.0)
    with audit.acting(["volume"], "ha"):
        audit.observe({"volume": -30.0})
    clock.now += 5  # the HA call's grace window is over
    audit.observe({"volume": -35.0})
    assert emitted == [("ha", "volume", -20.0, -30.0)]
    await quiet()
    assert emitted[1] == ("external", "volume", -30.0, -35.0)


async def test_intent_covers_the_poll_that_reports_the_change_late():
    clock = Clock()
    audit, emitted = make_audit(clock=clock, grace_s=4.0)
    with audit.acting(["mute"], "ui"):
        pass  # the call returned without the new value
    clock.now += 2.0  # the 2 s status poll reports it
    audit.observe({"mute": True})
    await quiet()
    assert emitted == [("ui", "mute", False, True)]


def test_acting_rejects_unknown_sources():
    audit, _ = make_audit()
    for bad in ("external", "mqtt", ""):
        with pytest.raises(ValueError), audit.acting(["volume"], bad):
            pass


async def test_flush_emits_open_entries():
    audit, emitted = make_audit()
    audit.observe({"source": "Toslink"})
    audit.flush()
    assert emitted == [("external", "source", "Analog", "Toslink")]
    await quiet()
    assert len(emitted) == 1


# --- through the session ------------------------------------------------------------------------------------------


async def master_events(repo):
    return [e for e in await repo.list_events(kind="master")]


@pytest.fixture
async def audited(make_session):
    s = await make_session()
    s.audit.quiet_s = 0.15
    await asyncio.sleep(0.02)  # the watcher records the baseline
    return s


async def test_ui_drag_writes_exactly_one_event_and_nothing_during_the_drag(audited, fake, repo):
    before = await repo.count_events()
    for v in (-21.0, -24.5, -29.0, -33.5, -37.0, -40.0):
        await audited.set_volume(v, source="ui")
        await asyncio.sleep(0.02)
    assert await repo.count_events() == before  # zero DB writes during the drag
    await asyncio.sleep(0.4)
    await drain(audited)
    events = await master_events(repo)
    assert [e.message for e in events] == ["ui: volume −20.0 → −40.0 dB"]
    assert events[0].data["source"] == "ui" and events[0].data["old"] == -20.0 and events[0].data["new"] == -40.0
    assert fake.master["volume"] == -40.0


async def test_external_change_from_the_poll_stream(audited, repo):
    audited.monitor.update_master(MasterStatus(volume=-30.0))  # e.g. `minidsp gain -- -30` on the host
    await asyncio.sleep(0.4)
    await drain(audited)
    assert [e.message for e in await master_events(repo)] == ["external: volume −20.0 → −30.0 dB"]


async def test_ui_big_drop_needs_confirmation(audited, fake):
    with pytest.raises(ConfirmationRequired, match="lowering the master volume by 100"):
        await audited.set_volume(-120.0, source="ui")
    assert fake.masters == []  # nothing reached the device
    await audited.set_volume(-120.0, source="ui", confirm=True)
    assert fake.master["volume"] == -120.0


async def test_ui_drop_at_the_threshold_passes(audited, fake):
    await audited.set_volume(-40.0, source="ui")  # exactly 20 dB
    assert fake.master["volume"] == -40.0


@pytest.mark.parametrize("source", ["ha", "api"])
async def test_ha_and_api_drops_go_through_and_are_audited(audited, fake, repo, source):
    await audited.set_volume(-110.0, source=source)
    assert fake.master["volume"] == -110.0
    await asyncio.sleep(0.4)
    await drain(audited)
    assert [e.message for e in await master_events(repo)] == [f"{source}: volume −20.0 → −110.0 dB"]


async def test_drop_threshold_comes_from_settings(make_session, fake):
    s = await make_session(volume_drop_confirm_db=50.0)
    assert s.volume_drop_confirm_db == 50.0
    await s.set_volume(-60.0, source="ui")
    with pytest.raises(ConfirmationRequired):
        await s.set_volume(-120.0, source="ui")
    off = await make_session(volume_drop_confirm_db=0.0)
    await off.set_volume(-127.0, source="ui")
    assert fake.master["volume"] == -127.0


async def test_mute_source_and_preset_are_audited(audited, fake, repo):
    await audited.toggle_mute(source="ui")
    await audited.set_source("Toslink", source="ha")
    await audited.set_preset(1, source="ui")
    await asyncio.sleep(0.4)
    await drain(audited)
    messages = sorted(e.message for e in await master_events(repo))
    assert messages == ["ha: source Analog → Toslink", "ui: mute off → on", "ui: preset 1 → 2"]


async def test_activate_preset_passes_its_source(audited, repo):
    await audited.activate_preset(2, source="ha")
    await asyncio.sleep(0.4)
    await drain(audited)
    assert [e.message for e in await master_events(repo)] == ["ha: preset 1 → 3"]


async def test_stop_writes_open_entries(audited, repo):
    await audited.set_volume(-25.0, source="ui")
    await asyncio.sleep(0.02)
    await audited.stop()
    assert [e.message for e in await master_events(repo)] == ["ui: volume −20.0 → −25.0 dB"]
