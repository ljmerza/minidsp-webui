import asyncio
import json
from collections.abc import Callable
from typing import Any

import aiomqtt
import pytest

from app.bus import DEVICE, LOG, SYNC
from app.model.device_profile import M2X4HD
from app.model.master import DaemonDevice, MasterStatus
from app.mqtt import MqttBridge, create_bridge
from app.sync.session import DeviceSession, SessionManager, SessionState
from tests.unit.conftest import DEVICE_JSON, MASTER_JSON, SERIAL

BASE = "minidsp-webui"
STATE = f"{BASE}/{SERIAL}/state"
AVAIL = f"{BASE}/{SERIAL}/availability"
BRIDGE_AVAIL = f"{BASE}/availability"


class Dropped(Exception):
    """Pushed into the message stream to simulate a lost connection."""


class FakeBroker:
    """Records what the bridge publishes; behaves like a broker for retained messages."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, int, bool]] = []
        self.retained: dict[str, str] = {}
        self.subscriptions: list[str] = []
        self.fail_connects = 0
        self.connects = 0
        self.will: aiomqtt.Will | None = None
        self.client: FakeClient | None = None

    def factory(self, will: aiomqtt.Will) -> "FakeClient":
        self.will = will
        return FakeClient(self)

    def payloads(self, topic: str) -> list[str]:
        return [payload for t, payload, _, _ in self.published if t == topic]

    def state(self) -> dict[str, Any]:
        return json.loads(self.retained[STATE])

    def send(self, topic: str, payload: str, *, retain: bool = False) -> None:
        assert self.client is not None
        self.client.queue.put_nowait(aiomqtt.Message(topic, payload.encode(), 1, retain, 1, None))

    def drop(self) -> None:
        assert self.client is not None
        self.client.queue.put_nowait(Dropped("connection lost"))


class FakeClient:
    def __init__(self, broker: FakeBroker) -> None:
        self.broker = broker
        self.queue: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> "FakeClient":
        self.broker.connects += 1
        if self.broker.fail_connects:
            self.broker.fail_connects -= 1
            raise aiomqtt.MqttError("connection refused")
        self.broker.client = self
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.broker.client = None

    @property
    def messages(self):
        return self._messages()

    async def _messages(self):
        while True:
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise aiomqtt.MqttError(str(item))
            yield item

    async def publish(self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False) -> None:
        payload = "" if payload is None else str(payload)
        self.broker.published.append((topic, payload, qos, retain))
        if retain:
            if payload:
                self.broker.retained[topic] = payload
            else:
                self.broker.retained.pop(topic, None)

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.broker.subscriptions.append(topic)


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
def mqtt_settings(settings):
    settings.mqtt_enabled = True
    settings.mqtt_host = "broker.invalid"
    settings.mqtt_volume_floor_db = -80.0
    settings.public_url = "http://minidsp.invalid:8480"
    return settings


@pytest.fixture
def session(client, bus, mqtt_settings):
    device = DaemonDevice.model_validate(DEVICE_JSON)
    s = DeviceSession(SERIAL, M2X4HD, client=client, bus=bus, settings=mqtt_settings, device=device, daemon_index=0)
    s.state = SessionState.CONNECTED
    s.monitor.connected = True  # the cached master is live; the monitor itself is never started
    s.monitor.update_master(MasterStatus.model_validate(MASTER_JSON))
    return s


@pytest.fixture
def manager(client, bus, mqtt_settings, session):
    m = SessionManager(client, bus, mqtt_settings)
    m.sessions[SERIAL] = session
    return m


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
async def bridge(manager, bus, mqtt_settings, broker):
    b = MqttBridge(
        manager,
        bus,
        mqtt_settings,
        client_factory=broker.factory,
        debounce_s=0.03,
        max_delay_s=0.2,
        backoff_min_s=0.01,
        backoff_max_s=0.05,
    )
    b.start()
    await asyncio.wait_for(b.connected.wait(), 2)
    yield b
    await b.stop()


def body(route) -> dict:
    return json.loads(route.calls.last.request.content)


# --- connect / discovery / state ----------------------------------------------------------------------------------


async def test_connect_publishes_discovery_availability_and_state(bridge, broker):
    assert broker.will is not None
    assert (broker.will.topic, broker.will.payload, broker.will.retain) == (BRIDGE_AVAIL, "offline", True)
    assert broker.subscriptions == [f"{BASE}/+/cmd/+"]
    assert broker.retained[BRIDGE_AVAIL] == "online"
    assert broker.retained[AVAIL] == "online"
    discovery = {t: json.loads(v) for t, v in broker.retained.items() if t.startswith("homeassistant/")}
    assert len(discovery) == 25  # 6 master/health + push-all + 4 x (gain, mute, delay) + 6 level sensors
    volume = discovery[f"homeassistant/number/minidsp_{SERIAL}/volume/config"]
    assert volume["min"] == -80.0 and volume["device"]["sw_version"] == "fw 1.75"
    assert volume["device"]["configuration_url"] == "http://minidsp.invalid:8480"
    assert all(retain and qos == 1 for _, _, qos, retain in broker.published)
    assert broker.state() == {
        "connected": True,
        "state": "connected",
        "volume": -20.0,
        "mute": False,
        "source": "Analog",
        "preset": "Preset 1",
        "preset_index": 0,
        "preset_switching": None,
        "sync": "unknown",
        "sync_counts": {"synced": 0, "pending": 0, "failed": 0, "unknown": 0},
        "outputs": [{"gain": None, "mute": None, "delay": None}] * 4,  # the preset state isn't loaded
        "profile": None,
    }
    # Discovery is announced before the state the entities read.
    topics = [t for t, *_ in broker.published]
    assert topics.index(STATE) > max(i for i, t in enumerate(topics) if t.startswith("homeassistant/"))


async def test_state_is_debounced_and_deduplicated(bridge, broker, session):
    before = len(broker.payloads(STATE))
    for volume in (-21.0, -22.0, -23.0):
        session.monitor.update_master(MasterStatus(volume=volume))
    await until(lambda: len(broker.payloads(STATE)) > before)
    await asyncio.sleep(0.1)
    assert len(broker.payloads(STATE)) == before + 1
    assert broker.state()["volume"] == -23.0

    session.monitor.update_master(MasterStatus(volume=-23.0))  # no change -> no event
    bridge.schedule_state(SERIAL)  # an event with identical state -> nothing published
    await asyncio.sleep(0.1)
    assert len(broker.payloads(STATE)) == before + 1


async def test_debounce_has_a_max_delay(bridge, broker, session):
    before = len(broker.payloads(STATE))
    for step in range(12):  # changes every 25 ms never leave a 30 ms quiet gap
        session.monitor.update_master(MasterStatus(volume=-30.0 - step))
        await asyncio.sleep(0.025)
    assert len(broker.payloads(STATE)) > before


async def test_connectivity_follows_session_state(bridge, broker, session):
    session.set_state(SessionState.DAEMON_DOWN)
    await until(lambda: broker.state()["connected"] is False)
    assert broker.state()["state"] == "daemon_down"
    session.set_state(SessionState.CONNECTED)
    await until(lambda: broker.state()["connected"] is True)


async def test_sync_counts_from_bus_events(bridge, broker, bus):
    counts = {"synced": 40, "pending": 2, "failed": 0, "unknown": 0}
    bus.publish(SYNC, {"preset": 0, "counts": counts, "changes": {}, "removed": []}, serial=SERIAL)
    await until(lambda: broker.state()["sync"] == "pending")
    assert broker.state()["sync_counts"] == counts
    # Counts for an inactive preset don't change the sensor.
    bus.publish(SYNC, {"preset": 1, "counts": {**counts, "failed": 1}}, serial=SERIAL)
    await asyncio.sleep(0.1)
    assert broker.state()["sync"] == "pending"


async def test_sync_counts_prefer_the_session(bridge, broker, session, monkeypatch):
    monkeypatch.setattr(session, "sync_status", lambda preset: {"a": "synced", "b": "failed"})
    bridge.schedule_state(SERIAL)
    await until(lambda: broker.state()["sync"] == "failed")

    def broken(preset):
        raise KeyError(preset)

    monkeypatch.setattr(session, "sync_status", broken)
    bridge.schedule_state(SERIAL)
    await until(lambda: broker.state()["sync"] == "unknown")


async def test_new_session_is_announced(bridge, broker, manager, client, bus, mqtt_settings):
    other = DeviceSession(4242, M2X4HD, client=client, bus=bus, settings=mqtt_settings)
    manager.sessions[4242] = other
    other.set_state(SessionState.ABSENT)
    await until(lambda: f"{BASE}/4242/state" in broker.retained)
    assert broker.retained[f"{BASE}/4242/availability"] == "online"
    assert "homeassistant/select/minidsp_4242/source/config" in broker.retained
    assert json.loads(broker.retained[f"{BASE}/4242/state"])["connected"] is False


async def test_discovery_republished_when_device_info_changes(bridge, broker, session):
    topic = f"homeassistant/number/minidsp_{SERIAL}/volume/config"
    before = len(broker.payloads(topic))
    await bridge.publish_now(SERIAL)
    assert len(broker.payloads(topic)) == before
    session.device = DaemonDevice.model_validate({**DEVICE_JSON, "version": {**DEVICE_JSON["version"], "fw_minor": 76}})
    await bridge.publish_now(SERIAL)
    assert json.loads(broker.retained[topic])["device"]["sw_version"] == "fw 1.76"


# --- commands -----------------------------------------------------------------------------------------------------


async def test_volume_command_goes_through_the_session(bridge, broker, daemon):
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -25.5})
    broker.send(f"{BASE}/{SERIAL}/cmd/volume", "-25.5")
    await until(lambda: route.called)
    assert body(route) == {"volume": -25.5}
    await until(lambda: broker.state()["volume"] == -25.5)


async def test_out_of_range_volume_is_rejected_and_state_republished(bridge, broker, daemon, bus):
    route = daemon.post("/devices/0").respond(json=MASTER_JSON)
    logs = bus.subscribe([LOG])
    before = len(broker.payloads(STATE))
    assert await bridge.handle_command(SERIAL, "volume", "-90") is False  # below the -80 floor
    assert await bridge.handle_command(SERIAL, "volume", "3") is False
    assert not route.called
    await until(lambda: len(broker.payloads(STATE)) > before)  # same state, forced so HA snaps back
    assert broker.state()["volume"] == -20.0
    event = logs.get_nowait()
    assert event.data["level"] == "warning" and "rejected" in event.data["message"]
    assert event.data["source"] == "mqtt" and event.data["entity"] == "volume"


async def test_big_volume_raise_is_rejected(bridge, daemon, bus):
    route = daemon.post("/devices/0").respond(json=MASTER_JSON)
    logs = bus.subscribe([LOG])
    assert await bridge.handle_command(SERIAL, "volume", "-5") is False  # +15 dB from -20
    assert not route.called
    assert "no confirmation from Home Assistant" in logs.get_nowait().data["message"]


async def test_mute_and_source_commands(bridge, broker, daemon):
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "mute": True})
    assert await bridge.handle_command(SERIAL, "mute", "ON")
    assert body(route) == {"mute": True}
    route.respond(json={**MASTER_JSON, "mute": True, "source": "Toslink"})
    assert await bridge.handle_command(SERIAL, "source", "toslink")
    assert body(route) == {"source": "Toslink"}
    await until(lambda: broker.state()["source"] == "Toslink" and broker.state()["mute"] is True)


async def test_invalid_commands_never_reach_the_daemon(bridge, daemon):
    route = daemon.post("/devices/0").respond(json=MASTER_JSON)
    assert await bridge.handle_command(SERIAL, "mute", "toggle") is False
    assert await bridge.handle_command(SERIAL, "source", "Hdmi") is False
    assert await bridge.handle_command(SERIAL, "preset", "Preset 9") is False
    assert await bridge.handle_command(SERIAL, "volume", "nan") is False
    assert await bridge.handle_command(SERIAL, "gain", "1") is False
    assert await bridge.handle_command(999, "mute", "ON") is False
    assert not route.called


async def test_preset_command_switches_and_waits(bridge, broker, daemon):
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "preset": 2})
    assert await bridge.handle_command(SERIAL, "preset", "Preset 3")
    assert body(route) == {"preset": 2}
    await until(lambda: broker.state()["preset"] == "Preset 3")


async def test_preset_command_uses_activate_preset_when_available(bridge, session, monkeypatch):
    calls = []

    async def activate(preset, *, flush=True):
        calls.append(preset)
        return MasterStatus(preset=preset)

    monkeypatch.setattr(session, "activate_preset", activate)
    assert await bridge.handle_command(SERIAL, "preset", "2")
    assert calls == [1]


async def test_command_during_preset_switch_is_rejected(bridge, session, daemon):
    route = daemon.post("/devices/0").respond(json=MASTER_JSON)
    session.preset_switching = 1
    assert await bridge.handle_command(SERIAL, "mute", "ON") is False
    assert not route.called


async def test_daemon_failure_is_reported(bridge, broker, daemon, bus):
    daemon.post("/devices/0").respond(500, text="boom")
    logs = bus.subscribe([LOG])
    assert await bridge.handle_command(SERIAL, "mute", "ON") is False
    messages = [e.data for e in iter(logs.get_nowait, None)]
    assert any(m["level"] == "error" and "device write failed" in m["message"] for m in messages)


async def test_retained_and_foreign_messages_are_ignored(bridge, broker, daemon):
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "mute": True})
    broker.send(f"{BASE}/{SERIAL}/cmd/mute", "ON", retain=True)
    broker.send(f"{BASE}/{SERIAL}/state", "{}")
    await asyncio.sleep(0.1)
    assert not route.called
    broker.send(f"{BASE}/{SERIAL}/cmd/mute", "ON")
    await until(lambda: route.called)


async def test_commands_for_one_entity_coalesce(bridge, daemon, session):
    sent: list[float] = []
    release = asyncio.Event()
    original = session.set_volume

    async def slow_set_volume(volume, *, confirm=False):
        sent.append(volume)
        await release.wait()
        return await original(volume, confirm=confirm)

    session.set_volume = slow_set_volume
    daemon.post("/devices/0").respond(json=MASTER_JSON)
    bridge.submit_command(SERIAL, "volume", "-21")
    await until(lambda: sent == [-21.0])
    for value in ("-22", "-23", "-24"):  # arrive while -21 is in flight: only the newest is applied
        bridge.submit_command(SERIAL, "volume", value)
    release.set()
    await until(lambda: sent == [-21.0, -24.0])
    await asyncio.sleep(0.05)
    assert sent == [-21.0, -24.0]


# --- connection lifecycle -----------------------------------------------------------------------------------------


async def test_reconnects_with_backoff_and_republishes(manager, bus, mqtt_settings, broker):
    broker.fail_connects = 2
    b = MqttBridge(manager, bus, mqtt_settings, client_factory=broker.factory, backoff_min_s=0.01, backoff_max_s=0.02)
    b.start()
    try:
        await asyncio.wait_for(b.connected.wait(), 2)
        assert broker.connects == 3
        first = len(broker.payloads(STATE))
        broker.drop()
        await until(lambda: broker.connects == 4 and b.connected.is_set())
        await until(lambda: len(broker.payloads(STATE)) > first)  # a fresh connection re-sends everything
        assert len(broker.payloads(f"homeassistant/switch/minidsp_{SERIAL}/mute/config")) == 2
    finally:
        await b.stop()


async def test_stop_publishes_offline(manager, bus, mqtt_settings, broker):
    b = MqttBridge(manager, bus, mqtt_settings, client_factory=broker.factory)
    b.start()
    await asyncio.wait_for(b.connected.wait(), 2)
    await b.stop()
    assert broker.retained[AVAIL] == "offline"
    assert broker.retained[BRIDGE_AVAIL] == "offline"
    assert broker.client is None and not b.connected.is_set()
    await b.stop()  # idempotent


async def test_stop_while_broker_is_unreachable(manager, bus, mqtt_settings, broker):
    broker.fail_connects = 10**6
    b = MqttBridge(manager, bus, mqtt_settings, client_factory=broker.factory, backoff_min_s=0.01)
    b.start()
    await until(lambda: broker.connects >= 2)
    assert b.last_error == "connection refused"
    await asyncio.wait_for(b.stop(), 1)


async def test_remove_device_clears_retained_topics(bridge, broker):
    assert await bridge.remove_device(SERIAL)
    assert not [t for t in broker.retained if t.startswith("homeassistant/")]
    assert STATE not in broker.retained and AVAIL not in broker.retained
    assert broker.retained[BRIDGE_AVAIL] == "online"


async def test_remove_device_when_disconnected(manager, bus, mqtt_settings):
    assert await MqttBridge(manager, bus, mqtt_settings).remove_device(SERIAL) is False


def test_create_bridge_respects_settings(manager, bus, mqtt_settings, caplog):
    assert isinstance(create_bridge(manager, bus, mqtt_settings), MqttBridge)
    mqtt_settings.mqtt_host = ""
    assert create_bridge(manager, bus, mqtt_settings) is None
    assert "MQTT_HOST is empty" in caplog.text
    mqtt_settings.mqtt_enabled = False
    mqtt_settings.mqtt_host = "broker.invalid"
    assert create_bridge(manager, bus, mqtt_settings) is None


async def test_device_events_without_serial_refresh_every_device(bridge, broker, bus, session):
    before = len(broker.payloads(STATE))
    status = session.monitor.status
    session.monitor.status = status.model_copy(update={"master": status.master.merged(MasterStatus(volume=-40.0))})
    bus.publish(DEVICE, {"serial": None, "daemon": "down"})
    await until(lambda: len(broker.payloads(STATE)) > before)
    assert broker.state()["volume"] == -40.0


async def test_removed_device_stays_removed_until_reconnect(bridge, broker, session):
    assert await bridge.remove_device(SERIAL)
    session.monitor.update_master(MasterStatus(volume=-31.0))
    await asyncio.sleep(0.15)  # well past the debounce
    assert not [t for t in broker.retained if t.startswith("homeassistant/")]
    assert STATE not in broker.retained and AVAIL not in broker.retained
    broker.drop()  # the next connection announces it again
    await until(lambda: any(t.startswith("homeassistant/") for t in broker.retained))
    await until(lambda: STATE in broker.retained)


async def test_state_changed_during_a_publish_is_published_afterwards(bridge, broker, session):
    assert broker.client is not None
    client = broker.client
    original = client.publish
    gate = asyncio.Event()
    held = asyncio.Event()

    async def slow_publish(topic, payload=None, qos=0, retain=False):
        if topic == STATE and not gate.is_set():
            held.set()
            await gate.wait()
        await original(topic, payload, qos, retain)

    client.publish = slow_publish
    session.monitor.update_master(MasterStatus(volume=-30.0))
    await asyncio.wait_for(held.wait(), 2)
    session.monitor.update_master(MasterStatus(volume=-35.0))  # arrives while the -30 publish is in flight
    await asyncio.sleep(0.05)
    gate.set()
    await until(lambda: broker.state()["volume"] == -35.0)
