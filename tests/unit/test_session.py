import asyncio
import json

import httpx
import pytest

from app.bus import DEVICE, MASTER
from app.daemon.client import DaemonUnavailable, DeviceNotFound
from app.model.device_profile import M2X4HD
from app.model.master import MasterStatus, MasterUpdate
from app.sync.session import (
    ConfirmationRequired,
    DeviceSession,
    MasterValidationError,
    PresetSwitchInProgress,
    PresetSwitchTimeout,
    SessionManager,
    SessionState,
)

from .conftest import DEVICE_JSON, MASTER_JSON, SERIAL


@pytest.fixture
def session(client, bus, settings):
    s = DeviceSession(SERIAL, M2X4HD, client=client, bus=bus, settings=settings, daemon_index=0)
    s.monitor.update_master(MasterStatus.model_validate(MASTER_JSON))
    return s


def body(route):
    return json.loads(route.calls.last.request.content)


@pytest.mark.parametrize(
    "update",
    [
        MasterUpdate(volume=0.5),
        MasterUpdate(volume=-127.5),
        MasterUpdate(source="Hdmi"),
        MasterUpdate(preset=4),
        MasterUpdate(preset=-1),
    ],
)
def test_validate_master_rejects(session, update):
    with pytest.raises(MasterValidationError):
        session.validate_master(update)


def test_validate_master_normalizes_source(session):
    assert session.validate_master(MasterUpdate(source="toslink")).source == "Toslink"
    assert session.validate_master(MasterUpdate(volume=-127.0, mute=True)).volume == -127.0


async def test_set_master_posts_and_publishes(session, daemon, bus):
    session.monitor.connected = True
    sub = bus.subscribe([MASTER])
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -30.0, "mute": True})
    result = await session.set_master(MasterUpdate(volume=-30.0, mute=True))
    assert body(route) == {"volume": -30.0, "mute": True}
    assert result.volume == -30.0 and session.master.mute is True
    assert sub.get_nowait().data["master"]["volume"] == -30.0


async def test_invalid_master_never_reaches_daemon(session, daemon):
    route = daemon.post("/devices/0").respond(json=MASTER_JSON)
    with pytest.raises(MasterValidationError):
        await session.set_volume(3.0)
    assert not route.called


async def test_set_source_canonical(session, daemon):
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "source": "Usb"})
    await session.set_source("usb")
    assert body(route) == {"source": "Usb"}


async def test_volume_step_from_live_cache(session, daemon):
    session.monitor.connected = True
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -19.5})
    status = daemon.get("/devices/0")
    assert (await session.volume_step(1)).volume == -19.5
    assert body(route) == {"volume": -19.5}
    route.respond(json={**MASTER_JSON, "volume": -20.0})
    assert (await session.volume_step(-1)).volume == -20.0
    assert body(route) == {"volume": -20.0}
    assert not status.called


async def test_volume_step_reads_fresh_status_without_monitor(session, daemon):
    daemon.get("/devices/0").respond(json={"master": {**MASTER_JSON, "volume": -40.0}})
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -40.5})
    await session.volume_step(-1)
    assert body(route) == {"volume": -40.5}


async def test_volume_step_clamps_at_limits(session, daemon):
    session.monitor.connected = True
    master = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": 0.0})
    session.monitor.update_master(MasterStatus(volume=-0.25))
    await session.volume_step(1)
    assert body(master) == {"volume": 0.0}

    master.respond(json={**MASTER_JSON, "volume": -127.0})
    session.monitor.update_master(MasterStatus(volume=-127.0))
    await session.volume_step(-1)
    assert body(master) == {"volume": -127.0}


async def test_toggle_mute(session, daemon):
    session.monitor.connected = True
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "mute": True})
    assert (await session.toggle_mute()).mute is True
    assert body(route) == {"mute": True}


async def test_preset_switch_waits_for_status(session, daemon, bus):
    session.state = SessionState.CONNECTED
    sub = bus.subscribe([DEVICE])
    daemon.post("/devices/0").respond(json=MASTER_JSON)  # device still reports the old preset
    status = daemon.get("/devices/0").mock(
        side_effect=[
            httpx.Response(500, json={"message": "busy", "error": {"type": "InternalError"}}),
            httpx.Response(200, json={"master": {**MASTER_JSON, "preset": 2}}),
        ]
    )
    result = await session.set_preset(2)
    assert result.preset == 2
    assert status.call_count == 2
    assert session.preset_switching is None
    switching = [e.data["preset_switching"] for e in (sub.get_nowait() for _ in range(sub.pending()))]
    assert switching == [2, None]


async def test_preset_switch_timeout(session, daemon, settings):
    settings.preset_switch_timeout_s = 0.6
    daemon.post("/devices/0").respond(json=MASTER_JSON)
    daemon.get("/devices/0").respond(json={"master": MASTER_JSON})
    with pytest.raises(PresetSwitchTimeout):
        await session.set_preset(1)
    assert session.preset_switching is None


async def test_writes_blocked_while_switching(session, daemon):
    daemon.post("/devices/0").respond(json=MASTER_JSON)
    daemon.get("/devices/0").respond(json={"master": {**MASTER_JSON, "preset": 3}})
    switch = asyncio.create_task(session.set_preset(3))
    await asyncio.sleep(0.05)
    with pytest.raises(PresetSwitchInProgress):
        await session.set_volume(-10.0)
    with pytest.raises(PresetSwitchInProgress):
        await session.set_preset(1)
    await switch


async def test_set_master_with_preset_switches_first(session, daemon):
    calls = []

    def record(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={**MASTER_JSON, "preset": 1})

    session.monitor.connected = True
    daemon.post("/devices/0").mock(side_effect=record)
    await session.set_master(MasterUpdate(preset=1, volume=-16.0))
    assert calls == [{"preset": 1}, {"volume": -16.0}]


async def test_active_preset_uses_fresh_cache_else_refreshes(session, daemon):
    route = daemon.get("/devices/0").respond(json={"master": {**MASTER_JSON, "preset": 3}})
    assert await session.active_preset() == 0
    assert not route.called
    assert await session.active_preset(max_age_s=-1) == 3
    assert route.called


async def test_device_gone_marks_absent_and_requests_rescan(session, daemon):
    lost = []
    session.on_lost = lambda: lost.append(True)
    session.state = SessionState.CONNECTED
    daemon.post("/devices/0").respond(500, json={"message": "nope", "error": {"type": "DeviceNotReady"}})
    with pytest.raises(DeviceNotFound):
        await session.set_mute(True)
    assert session.state == SessionState.ABSENT
    assert session.daemon_index is None
    assert lost == [True]


async def test_resolve_index_updates_state(session, daemon):
    daemon.get("/devices").respond(json=[{"url": "x", "product_name": None, "version": None}, DEVICE_JSON])
    assert await session.resolve_index() == 1
    assert session.daemon_index == 1 and session.state == SessionState.CONNECTED
    daemon.get("/devices").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(DaemonUnavailable):
        await session.resolve_index()
    assert session.state == SessionState.DAEMON_DOWN


async def test_manager_discovery(client, bus, settings, daemon):
    manager = SessionManager(client, bus, settings)
    daemon.get("/devices").mock(side_effect=httpx.ConnectError("refused"))
    assert await manager.discover() == []
    assert manager.daemon_up is False and manager.default() is None

    flex = {"url": "usb:9", "product_name": "Flex", "version": {**DEVICE_JSON["version"], "serial": 5}}
    daemon.get("/devices").mock(return_value=httpx.Response(200, json=[flex, DEVICE_JSON]))
    try:
        await manager.discover()
        assert manager.daemon_up is True
        session = manager.get(SERIAL)
        assert session is not None and session.daemon_index == 1 and session.state == SessionState.CONNECTED
        assert [d.product_name for d in manager.unsupported] == ["Flex"]
        assert manager.default() is session

        daemon.get("/devices").mock(return_value=httpx.Response(200, json=[DEVICE_JSON]))
        await manager.discover()
        assert manager.get(SERIAL) is session and session.daemon_index == 0

        daemon.get("/devices").mock(return_value=httpx.Response(200, json=[]))
        await manager.discover()
        assert session.state == SessionState.ABSENT

        daemon.get("/devices").mock(side_effect=httpx.ConnectError("refused"))
        await manager.discover()
        assert session.state == SessionState.DAEMON_DOWN
    finally:
        await manager.stop()


async def test_manager_reads_the_daemon_version_once_per_daemon_start(client, bus, settings, daemon):
    manager = SessionManager(client, bus, settings)
    devices = daemon.get("/devices").mock(return_value=httpx.Response(200, json=[]))
    version = daemon.get("/openapi.json").respond(json={"info": {"version": "0.1.12"}})
    try:
        await manager.discover()
        await manager.discover()
        assert manager.daemon_version == "0.1.12" and version.call_count == 1

        devices.mock(side_effect=httpx.ConnectError("refused"))
        await manager.discover()
        assert manager.daemon_version is None  # it may come back as another version

        devices.mock(return_value=httpx.Response(200, json=[]))
        version.respond(json={"info": {"version": "0.1.13"}})
        await manager.discover()
        assert manager.daemon_version == "0.1.13" and version.call_count == 2
    finally:
        await manager.stop()


async def test_manager_without_a_daemon_version(client, bus, settings, daemon):
    manager = SessionManager(client, bus, settings)
    daemon.get("/devices").mock(return_value=httpx.Response(200, json=[]))
    version = daemon.get("/openapi.json").respond(404, text="no such route")
    try:
        await manager.discover()
        await manager.discover()
        assert manager.daemon_up is True and manager.daemon_version is None
        assert version.call_count == 1  # display-only: not retried every scan
    finally:
        await manager.stop()


async def test_manager_background_loop_survives_daemon_down(client, bus, settings, daemon):
    route = daemon.get("/devices").mock(side_effect=httpx.ConnectError("refused"))
    manager = SessionManager(client, bus, settings)
    manager.start()
    await asyncio.sleep(0.05)
    manager.request_rescan()
    await asyncio.sleep(0.05)
    await manager.stop()
    assert route.call_count >= 2
    assert manager.daemon_up is False


async def test_big_volume_raise_needs_confirmation(session, daemon):
    session.monitor.connected = True  # cached volume -20 is live
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -10.0})
    with pytest.raises(ConfirmationRequired):
        await session.set_volume(-10.0)
    with pytest.raises(ConfirmationRequired):
        await session.set_master(MasterUpdate(volume=0.0, mute=False))
    assert not route.called

    await session.set_volume(-14.0)  # +6 dB is within the step
    assert body(route) == {"volume": -14.0}
    await session.set_master(MasterUpdate(volume=-10.0, confirm=True))
    assert body(route) == {"volume": -10.0}
    route.respond(json={**MASTER_JSON, "volume": -80.0})
    await session.set_volume(-80.0)  # lowering is never guarded
    assert body(route) == {"volume": -80.0}


async def test_volume_raise_guard_reads_the_device_when_the_stream_is_down(session, daemon):
    # The cache says -20, but without a live stream the guard asks the device, which is at -60.
    daemon.get("/devices/0").respond(json={"master": {**MASTER_JSON, "volume": -60.0}})
    route = daemon.post("/devices/0").respond(json=MASTER_JSON)
    with pytest.raises(ConfirmationRequired):
        await session.set_volume(-20.0)
    assert not route.called


async def test_volume_raise_guard_can_be_disabled(session, daemon, settings):
    settings.gain_step_confirm_db = 0
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": 0.0})
    await session.set_volume(0.0)
    assert body(route) == {"volume": 0.0}


async def test_transient_timeout_does_not_leave_the_session_down(session, daemon):
    lost = []
    session.on_lost = lambda: lost.append(True)
    session.state = SessionState.CONNECTED
    daemon.post("/devices/0").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={**MASTER_JSON, "mute": True})]
    )
    with pytest.raises(DaemonUnavailable):
        await session.set_mute(True)
    assert session.state == SessionState.DAEMON_DOWN
    assert lost == [True]  # discovery re-checks right away
    await session.set_mute(True)
    assert session.state == SessionState.CONNECTED


async def test_manager_loop_survives_unexpected_errors(client, bus, settings, monkeypatch):
    manager = SessionManager(client, bus, settings)
    calls = []

    async def broken() -> list:
        calls.append(1)
        raise RuntimeError("bug")

    monkeypatch.setattr(manager, "discover", broken)
    manager.start()
    try:
        await asyncio.sleep(0.05)
        manager.request_rescan()
        await asyncio.sleep(0.05)
        assert len(calls) >= 2
        assert manager._task is not None and not manager._task.done()
        assert manager.daemon_up is False and manager.last_error == "bug"
    finally:
        await manager.stop()


async def test_discovery_handles_a_malformed_device_list(client, bus, settings, daemon):
    manager = SessionManager(client, bus, settings)
    daemon.get("/devices").respond(json=[{"product_name": "2x4HD"}])
    assert await manager.discover() == []
    assert manager.daemon_up is False and "unexpected response" in manager.last_error
