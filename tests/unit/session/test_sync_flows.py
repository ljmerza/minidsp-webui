"""Session ↔ writer flows against the fake daemon: staging, activation, push-all, reconnects and failures."""

import asyncio

import httpx
import pytest

from app.model.master import MasterStatus
from app.sync.session import NO_PRESET, SessionManager, SessionState

from ..conftest import SERIAL
from .conftest import drain, settle, switch_externally


def fast_retries(session) -> None:
    session.writer.backoff_min_s = 0.01
    session.writer.backoff_max_s = 0.02


async def test_staged_edit_flushes_when_the_preset_is_activated_elsewhere(session, fake):
    await session.edit(1, {"outputs": {"1": {"gain_db": -3.0}}})
    assert (await settle(session, 1)).staged
    assert fake.configs == []
    await switch_externally(session, fake, 1)
    result = await settle(session, 1)
    assert result.ok
    assert fake.configs == [{"outputs": [{"index": 1, "gain": -3.0}]}]
    assert session.sync_status(1)["outputs.1.gain_db"] == "synced"


async def test_switch_and_apply_now(session, fake, repo):
    await session.edit(2, {"inputs": {"0": {"gain_db": -2.0}}})
    master = await session.activate_preset(2)
    assert master.preset == 2
    assert fake.masters == [{"preset": 2}]
    assert fake.configs == [{"inputs": [{"index": 0, "gain": -2.0}]}]
    assert session.sync_summary(2)["active"] is True
    assert session.sync_summary(2)["counts"]["pending"] == 0
    await drain(session)
    messages = [e.message for e in await repo.list_events(kind="preset")]
    assert "switching to preset 3" in messages


async def test_activate_the_active_preset_only_flushes(session, fake):
    await session.activate_preset(0)
    assert fake.masters == []


async def test_writes_wait_while_the_preset_switches(session):
    session.preset_switching = 1
    assert await session._writer_active_preset() == NO_PRESET
    session.preset_switching = None
    assert await session._writer_active_preset() == 0


async def test_push_all_is_mute_wrapped(session, fake, repo):
    result = await session.push_all(0)
    assert result.ok
    first, last = fake.configs[0], fake.configs[-1]
    assert first == {"outputs": [{"index": n, "mute": True} for n in range(4)]}
    assert last == {"outputs": [{"index": n, "mute": False} for n in range(4)]}
    assert set(session.sync_status(0).values()) == {"synced"}
    await drain(session)
    [done] = [e for e in await repo.list_events(kind="push") if "done" in e.message]
    assert done.data["writes"] == len(fake.configs)


async def test_push_all_without_mute_wrap(session, fake):
    await session.push_all(0, mute_wrap=False)
    assert fake.configs[0] != {"outputs": [{"index": n, "mute": True} for n in range(4)]}


async def test_push_all_on_an_inactive_preset_is_staged(session, fake):
    result = await session.push_all(3)
    assert result.staged and fake.configs == []
    await switch_externally(session, fake, 3)
    assert (await settle(session, 3, timeout=5)).ok
    assert fake.configs[0] == {"outputs": [{"index": n, "mute": True} for n in range(4)]}


async def test_reconnect_marks_the_active_preset_unknown(session, fake, repo):
    await session.adopt("trust")
    await session.adopt("trust", preset=1)
    assert set(session.sync_status(0).values()) == {"synced"}
    await session._on_monitor_connected(True)
    await drain(session)
    assert set(session.sync_status(0).values()) == {"unknown"}
    assert set(session.sync_status(1).values()) == {"synced"}  # only the active preset is suspect
    assert fake.configs == []  # REAPPLY_ON_RECONNECT is off by default
    messages = [e.message for e in await repo.list_events(kind="reconnect")]
    assert any("marked unknown" in m for m in messages)


async def test_reconnect_reapplies_when_configured(make_session, fake):
    s = await make_session(reapply_on_reconnect=True)
    await s.adopt("trust")
    await s._on_monitor_connected(True)
    await drain(s)
    assert set(s.sync_status(0).values()) == {"synced"}
    assert fake.configs[0] == {"outputs": [{"index": n, "mute": True} for n in range(4)]}
    assert len(fake.configs) > 2


async def test_rejected_write_becomes_unknown_then_failed_then_converges(session, fake, repo):
    fast_retries(session)
    fake.fail = 500
    await session.edit(0, {"outputs": {"2": {"gain_db": -3.0}}})
    result = await settle(session)
    assert result.failed == {"outputs.2.gain_db": "Specified channel or peq is out of range"}
    assert session.sync_status(0)["outputs.2.gain_db"] == "failed"
    assert session.sync_summary(0)["error"] == "Specified channel or peq is out of range"
    fake.fail = None
    session.writer.kick(0)
    assert (await settle(session)).ok
    assert fake.configs == [{"outputs": [{"index": 2, "gain": -3.0}]}]
    await drain(session)
    assert any(e.level == "warning" for e in await repo.list_events(kind="sync"))


async def test_daemon_down_mid_edit_then_reconnect_converges(session, fake):
    fast_retries(session)
    lost = []
    session.on_lost = lambda: lost.append(True)
    fake.fail = httpx.ConnectError("refused")
    await session.edit(0, {"outputs": {"0": {"gain_db": -1.0}}})
    await settle(session)
    assert session.state == SessionState.DAEMON_DOWN and lost
    assert session.sync_status(0)["outputs.0.gain_db"] in ("failed", "pending")
    fake.fail = None
    await session._on_monitor_connected(True)  # the monitor reconnects once minidspd is back
    await drain(session)
    assert (await settle(session)).ok
    assert fake.configs[-1] == {"outputs": [{"index": 0, "gain": -1.0}]}
    assert session.sync_status(0)["outputs.0.gain_db"] == "synced"


async def test_drag_is_coalesced(make_session, fake):
    s = await make_session(write_coalesce_ms=50)
    await s.adopt("trust")
    for step in range(1, 21):
        await s.edit(0, {"outputs": {"0": {"gain_db": step * 0.1}}}, source="ui")
        await asyncio.sleep(0.005)
    assert (await settle(s)).ok
    # Intermediate values are dropped: far fewer calls than edits (how many depends on the machine's speed).
    assert 1 <= len(fake.configs) <= 10
    assert fake.configs[-1] == {"outputs": [{"index": 0, "gain": 2.0}]}


async def test_master_changes_do_not_touch_desired_state(session, fake):
    session.monitor.update_master(MasterStatus(volume=-30.0, mute=True))
    await asyncio.sleep(0.01)
    assert fake.configs == []
    assert session.sync_summary(0)["counts"]["pending"] == 0


async def test_manager_loads_state_and_records_the_device(client, bus, settings, fake, repo):
    manager = SessionManager(client, bus, settings, repo=repo)
    try:
        await manager.discover()
        session = manager.get(SERIAL)
        assert session is not None and session.loaded and session.repo is repo
        device = await repo.get_device(SERIAL)
        assert device is not None and device.product_name == "2x4HD" and device.fw == "1.75"
    finally:
        await manager.stop()


async def test_manager_survives_an_unreadable_database(client, bus, settings, fake, repo):
    await repo.db.close()
    manager = SessionManager(client, bus, settings, repo=repo)
    try:
        await manager.discover()
        session = manager.get(SERIAL)
        assert session is not None and not session.loaded and session.load_error
        assert session.info()["adopted"] is None
    finally:
        await manager.stop()


@pytest.mark.parametrize("touch", [False, True])
async def test_record_device_never_raises(session, repo, touch):
    await repo.db.close()
    await session.record_device(touch=touch)
