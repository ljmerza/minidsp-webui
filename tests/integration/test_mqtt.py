"""The MQTT bridge against a real broker: a throwaway eclipse-mosquitto container on a random loopback port.

Self-contained (it doesn't need the dev stack); the daemon is mocked with respx. The container is removed afterwards.
"""

import asyncio
import contextlib
import itertools
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import aiomqtt
import pytest
import respx

from app.bus import Bus
from app.config import Settings
from app.daemon.client import DaemonClient
from app.model.device_profile import M2X4HD
from app.model.master import DaemonDevice, MasterStatus
from app.mqtt import MqttBridge
from app.sync.session import DeviceSession, SessionManager, SessionState

pytestmark = pytest.mark.integration

IMAGE = os.environ.get("MOSQUITTO_IMAGE", "eclipse-mosquitto:2")
DAEMON_URL = "http://127.0.0.1:1"  # never contacted: respx answers every request
SERIAL = 911111
DEVICE_JSON = {
    "url": "mock:?serial=911111",
    "product_name": "2x4HD",
    "version": {"hw_id": 10, "dsp_version": 100, "serial": SERIAL, "fw_major": 1, "fw_minor": 75},
}
MASTER_JSON = {"preset": 0, "source": "Analog", "volume": -20.0, "mute": False, "dirac": False}

_ids = itertools.count()


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=120)


def _wait_for_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return
        time.sleep(0.2)
    raise TimeoutError(f"mosquitto did not open 127.0.0.1:{port}")


@pytest.fixture(scope="module")
def broker_port(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    config: Path = tmp_path_factory.mktemp("mosquitto") / "mosquitto.conf"
    config.write_text("listener 1883 0.0.0.0\nallow_anonymous true\npersistence false\n")
    config.chmod(0o644)
    name = f"minidsp-webui-test-mosquitto-{uuid.uuid4().hex[:8]}"
    _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-p",
        "127.0.0.1::1883",  # random host port, loopback only
        "-v",
        f"{config}:/mosquitto/config/mosquitto.conf:ro",
        IMAGE,
    )
    try:
        mapping = _docker("port", name, "1883/tcp").stdout.strip().splitlines()[0]
        port = int(mapping.rsplit(":", 1)[1])
        _wait_for_port(port)
        yield port
    finally:
        _docker("rm", "-f", name, check=False)


class Observer:
    """A plain MQTT client that records everything under the test's topics."""

    def __init__(self, port: int, *filters: str) -> None:
        self.port = port
        self.filters = filters
        self.messages: list[tuple[str, str, bool]] = []
        self._task: asyncio.Task | None = None
        self.client: aiomqtt.Client | None = None
        self._ready = asyncio.Event()

    async def __aenter__(self) -> "Observer":
        self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(self._ready.wait(), 10)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        async with aiomqtt.Client("127.0.0.1", self.port, identifier=f"observer-{uuid.uuid4().hex[:6]}") as client:
            self.client = client
            for topic_filter in self.filters:
                await client.subscribe(topic_filter, qos=1)
            self._ready.set()
            async for message in client.messages:
                payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
                self.messages.append((message.topic.value, payload, message.retain))

    def latest(self, topic: str) -> str | None:
        found = [payload for t, payload, _ in self.messages if t == topic]
        return found[-1] if found else None

    def count(self, topic: str) -> int:
        return sum(1 for t, *_ in self.messages if t == topic)

    async def publish(self, topic: str, payload: str, *, retain: bool = False) -> None:
        assert self.client is not None
        await self.client.publish(topic, payload, qos=1, retain=retain)


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.fixture
def prefixes() -> tuple[str, str]:
    """Fresh base/discovery topics per test, so retained messages from one test never leak into the next."""
    n = next(_ids)
    return f"mdw-test-{n}", f"ha-test-{n}"


@pytest.fixture
def daemon() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=DAEMON_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def stack(broker_port, prefixes, daemon) -> AsyncIterator[tuple[MqttBridge, DeviceSession]]:
    base, discovery = prefixes
    settings = Settings(
        _env_file=None,
        minidspd_url=DAEMON_URL,
        mqtt_enabled=True,
        mqtt_host="127.0.0.1",
        mqtt_port=broker_port,
        mqtt_base_topic=base,
        mqtt_discovery_prefix=discovery,
        mqtt_volume_floor_db=-80.0,
        preset_switch_timeout_s=2.0,
    )
    bus = Bus()
    client = DaemonClient(DAEMON_URL, timeout=1.0, preset_timeout=2.0)
    manager = SessionManager(client, bus, settings)
    session = DeviceSession(
        SERIAL,
        M2X4HD,
        client=client,
        bus=bus,
        settings=settings,
        device=DaemonDevice.model_validate(DEVICE_JSON),
        daemon_index=0,
    )
    session.state = SessionState.CONNECTED
    session.monitor.connected = True
    session.monitor.update_master(MasterStatus.model_validate(MASTER_JSON))
    manager.sessions[SERIAL] = session
    bridge = MqttBridge(manager, bus, settings, backoff_min_s=0.2, backoff_max_s=1.0)
    bridge.start()
    try:
        await asyncio.wait_for(bridge.connected.wait(), 10)
        yield bridge, session
    finally:
        await bridge.stop()
        await client.aclose()


async def test_discovery_state_and_availability_are_retained(stack, broker_port, prefixes):
    bridge, _ = stack
    base, discovery = prefixes
    # A client connecting afterwards gets everything from the broker's retained store.
    async with Observer(broker_port, f"{base}/#", f"{discovery}/#") as late:
        await until(lambda: late.latest(f"{base}/{SERIAL}/state") is not None)
        await until(lambda: len([t for t, *_ in late.messages if t.startswith(f"{discovery}/")]) == 6)
        assert all(retain for *_, retain in late.messages)
        assert late.latest(f"{base}/availability") == "online"
        assert late.latest(f"{base}/{SERIAL}/availability") == "online"
        state = json.loads(late.latest(f"{base}/{SERIAL}/state"))
        assert state["connected"] is True and state["volume"] == -20.0 and state["preset"] == "Preset 1"
        volume = json.loads(late.latest(f"{discovery}/number/minidsp_{SERIAL}/volume/config"))
        assert volume["command_topic"] == f"{base}/{SERIAL}/cmd/volume"
        assert volume["state_topic"] == f"{base}/{SERIAL}/state"
        assert volume["device"]["identifiers"] == [f"minidsp_{SERIAL}"]
        assert (volume["min"], volume["max"], volume["step"]) == (-80.0, 0.0, 0.5)


async def test_commands_round_trip(stack, broker_port, prefixes, daemon):
    _, session = stack
    base, _ = prefixes
    state_topic = f"{base}/{SERIAL}/state"
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -30.0})
    async with Observer(broker_port, f"{base}/#") as obs:
        await until(lambda: obs.latest(state_topic) is not None)
        await obs.publish(f"{base}/{SERIAL}/cmd/volume", "-30")
        await until(lambda: json.loads(obs.latest(state_topic))["volume"] == -30.0)
        assert json.loads(route.calls.last.request.content) == {"volume": -30.0}
        assert session.master.volume == -30.0

        # Out of range: rejected (the daemon is not called) and the unchanged state is re-published.
        calls, seen = route.call_count, obs.count(state_topic)
        await obs.publish(f"{base}/{SERIAL}/cmd/volume", "6")
        await until(lambda: obs.count(state_topic) > seen)
        assert route.call_count == calls
        assert json.loads(obs.latest(state_topic))["volume"] == -30.0

        route.respond(json={**MASTER_JSON, "volume": -30.0, "mute": True})
        await obs.publish(f"{base}/{SERIAL}/cmd/mute", "ON")
        await until(lambda: json.loads(obs.latest(state_topic))["mute"] is True)
        assert json.loads(route.calls.last.request.content) == {"mute": True}


async def test_lwt_on_connection_loss_then_reconnect(stack, broker_port, prefixes):
    bridge, _ = stack
    base, _ = prefixes
    async with Observer(broker_port, f"{base}/availability") as obs:
        await until(lambda: obs.latest(f"{base}/availability") == "online")
        paho = bridge._client._client  # the underlying paho client of the live aiomqtt connection
        paho.socket().shutdown(socket.SHUT_RDWR)  # drop the TCP connection without an MQTT DISCONNECT
        await until(lambda: obs.latest(f"{base}/availability") == "offline")  # the broker fired the LWT
        await until(lambda: obs.latest(f"{base}/availability") == "online", timeout=10)  # reconnected
        assert bridge.connected.is_set()


async def test_retained_command_is_not_replayed_on_reconnect(stack, broker_port, prefixes, daemon):
    bridge, _ = stack
    base, _ = prefixes
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -31.0})
    command = f"{base}/{SERIAL}/cmd/volume"
    async with Observer(broker_port, f"{base}/availability") as obs:
        try:
            # Live subscribers get it without the retain flag (MQTT 3.1.1 §3.3.1.3), so it is applied once...
            await obs.publish(command, "-31", retain=True)
            await until(lambda: route.call_count == 1)
            # ...but after a reconnect the broker replays it flagged as retained, and the bridge ignores it.
            bridge._client._client.socket().shutdown(socket.SHUT_RDWR)
            await until(lambda: obs.latest(f"{base}/availability") == "offline")
            await until(lambda: obs.latest(f"{base}/availability") == "online", timeout=10)
            await asyncio.sleep(0.5)
            assert route.call_count == 1
        finally:
            await obs.publish(command, "", retain=True)  # clear it from the broker


async def test_clean_stop_and_remove(stack, broker_port, prefixes):
    bridge, _ = stack
    base, discovery = prefixes
    assert await bridge.remove_device(SERIAL)
    await bridge.stop()
    async with Observer(broker_port, f"{base}/#", f"{discovery}/#") as late:
        await until(lambda: late.latest(f"{base}/availability") is not None)
        await asyncio.sleep(0.3)
        assert late.latest(f"{base}/availability") == "offline"
        assert not [t for t, *_ in late.messages if t.startswith(f"{discovery}/")]
        assert late.latest(f"{base}/{SERIAL}/state") is None
