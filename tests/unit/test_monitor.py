import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from app.bus import LEVELS, MASTER, Bus
from app.daemon.monitor import DeviceMonitor
from app.model.master import MasterStatus

from .conftest import MASTER_JSON

FULL = {"master": MASTER_JSON, "input_levels": [-40.0, -120.0], "output_levels": [-50.0, -60.0, -120.0, -120.0]}
LEVELS_ONLY = {"master": {}, "input_levels": [-10.0, -100.0], "output_levels": [-11.0, -12.0, -13.0, -14.0]}
VOLUME_CHANGE = {"master": {"volume": -25.5}}


class FakeClient:
    def __init__(self, base: str) -> None:
        self.base = base

    def ws_url(self, index: int, *, levels: bool, poll: bool) -> str:
        query = "&".join(n for n, on in (("levels=true", levels), ("poll=true", poll)) if on)
        return f"{self.base}/devices/{index}?{query}"


def make_monitor(bus: Bus, base: str = "ws://127.0.0.1:1", **kwargs) -> DeviceMonitor:
    async def resolve() -> int:
        return 0

    return DeviceMonitor(
        serial=1,
        bus=bus,
        client=FakeClient(base),
        resolve_index=resolve,
        backoff_min_s=0.02,
        backoff_max_s=0.1,
        levels_idle_grace_s=0.05,
        **kwargs,
    )


def drain(sub):
    return [sub.get_nowait() for _ in range(sub.pending())]


def test_full_then_partial_messages_merge():
    bus = Bus()
    sub = bus.subscribe()
    monitor = make_monitor(bus)
    monitor.levels_enabled = True

    monitor.handle_message(json.dumps(FULL))
    events = drain(sub)
    assert [e.topic for e in events] == [MASTER, LEVELS]
    assert events[1].data == {"i": [-40.0, -120.0], "o": [-50.0, -60.0, -120.0, -120.0]}

    monitor.handle_message(json.dumps(LEVELS_ONLY))
    events = drain(sub)
    assert [e.topic for e in events] == [LEVELS]  # master unchanged -> no master event
    assert monitor.status.master.volume == -20.0
    assert monitor.status.output_levels == [-11.0, -12.0, -13.0, -14.0]

    monitor.handle_message(json.dumps(VOLUME_CHANGE))
    events = drain(sub)
    assert [e.topic for e in events] == [MASTER]
    assert events[0].data["master"] == {**MASTER_JSON, "volume": -25.5}
    assert monitor.status.output_levels == [-11.0, -12.0, -13.0, -14.0]  # levels kept


def test_levels_ignored_when_not_enabled():
    bus = Bus()
    sub = bus.subscribe([LEVELS])
    monitor = make_monitor(bus)
    monitor.handle_message(json.dumps(FULL))
    assert drain(sub) == []
    assert monitor.status.input_levels == []


def test_garbage_message_is_ignored():
    monitor = make_monitor(Bus())
    monitor.handle_message("not json")
    monitor.handle_message(json.dumps({"master": {"volume": "loud"}}))
    assert monitor.status is None


def test_update_master_merges_and_publishes():
    bus = Bus()
    sub = bus.subscribe([MASTER])
    monitor = make_monitor(bus)
    monitor.handle_message(json.dumps(FULL))
    drain(sub)
    merged = monitor.update_master(MasterStatus(mute=True))
    assert merged.mute is True and merged.volume == -20.0
    assert len(drain(sub)) == 1
    monitor.update_master(MasterStatus(mute=True))
    assert drain(sub) == []


class FakeDaemonWS:
    """A websocket server that behaves like minidspd's status stream."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.connections = []

    async def handler(self, ws) -> None:
        self.paths.append(ws.request.path)
        self.connections.append(ws)
        await ws.send(json.dumps(FULL))
        try:
            if "levels=true" in ws.request.path:
                while True:
                    await asyncio.sleep(0.02)
                    await ws.send(json.dumps(LEVELS_ONLY))
            else:
                await ws.wait_closed()
        except Exception:
            pass


@pytest.fixture
async def fake_ws():
    fake = FakeDaemonWS()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        fake.base = f"ws://127.0.0.1:{port}"
        yield fake


async def wait_for(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_streams_levels_and_drops_them_without_subscribers(fake_ws):
    bus = Bus()
    connected = []
    monitor = make_monitor(bus, fake_ws.base, on_connected=connected.append)
    levels_sub = bus.subscribe([LEVELS])
    monitor.start()
    try:
        event = await asyncio.wait_for(levels_sub.get(), 3)
        assert event.data["o"] == [-50.0, -60.0, -120.0, -120.0] or event.data["o"] == [-11.0, -12.0, -13.0, -14.0]
        assert "levels=true" in fake_ws.paths[0] and "poll=true" in fake_ws.paths[0]
        assert monitor.connected and monitor.levels_enabled
        assert connected == [False]

        levels_sub.close()
        await wait_for(lambda: len(fake_ws.paths) >= 2 and monitor.connected)
        assert "levels" not in fake_ws.paths[1]
        assert not monitor.levels_enabled
        assert monitor.status.input_levels == []
        assert connected == [False, False]  # a demand reconnect is not a recovery

        bus.subscribe([LEVELS])
        await wait_for(lambda: len(fake_ws.paths) >= 3 and monitor.levels_enabled)
        assert "levels=true" in fake_ws.paths[2]
    finally:
        await monitor.stop()


async def test_reconnects_after_drop(fake_ws):
    bus = Bus()
    connected, lost = [], []
    monitor = make_monitor(bus, fake_ws.base, on_connected=connected.append, on_disconnected=lambda: lost.append(1))
    monitor.start()
    try:
        await wait_for(lambda: monitor.connected and monitor.status is not None)
        await fake_ws.connections[0].close()
        await wait_for(lambda: len(fake_ws.paths) >= 2 and monitor.connected)
        assert lost == [1]
        assert connected == [False, True]
    finally:
        await monitor.stop()
    assert not monitor.connected


async def test_keeps_retrying_when_unreachable():
    bus = Bus()
    monitor = make_monitor(bus)  # port 1: connection refused
    monitor.start()
    await asyncio.sleep(0.2)
    assert not monitor.connected
    assert monitor.last_error
    await monitor.stop()


class SlowHandshake:
    """A ``connect`` stand-in whose handshake takes ``delay`` seconds; the connection stays open until closed."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.urls: list[str] = []

    def __call__(self, url: str, **_: object) -> "SlowHandshake._Conn":
        self.urls.append(url)
        return self._Conn(self.delay)

    class _Conn:
        def __init__(self, delay: float) -> None:
            self.delay = delay
            self.closed = asyncio.Event()

        async def __aenter__(self):
            await asyncio.sleep(self.delay)
            return self

        async def __aexit__(self, *_: object) -> None:
            self.closed.set()

        async def close(self) -> None:
            self.closed.set()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.closed.wait()
            raise StopAsyncIteration


async def test_levels_subscriber_during_handshake_gets_levels():
    bus = Bus()
    connect = SlowHandshake(0.2)
    monitor = make_monitor(bus, connect=connect)
    monitor.start()
    try:
        await asyncio.sleep(0.05)
        bus.subscribe([LEVELS])  # arrives while the poll-only handshake is still in progress
        await wait_for(lambda: monitor.connected and monitor.levels_enabled)
        assert "levels=true" not in connect.urls[0]
        assert "levels=true" in connect.urls[-1]
    finally:
        await monitor.stop()


def test_empty_first_master_does_not_wipe_the_cache():
    bus = Bus()
    monitor = make_monitor(bus)
    monitor.handle_message(json.dumps(FULL))
    monitor._first_message = True  # as after a reconnect
    monitor.handle_message(json.dumps(LEVELS_ONLY))
    assert monitor.status.master.volume == -20.0
    monitor.handle_message(json.dumps({"master": {**MASTER_JSON, "volume": -30.0, "dirac": None}}))
    assert monitor.status.master.volume == -30.0 and monitor.status.master.dirac is None


class ProbeClient(FakeClient):
    """``get_status`` for the idle probe; ``fail`` makes it raise."""

    def __init__(self, base: str) -> None:
        super().__init__(base)
        self.fail = False
        self.probes = 0

    async def get_status(self, index: int):
        from app.model.master import StatusSummary

        self.probes += 1
        if self.fail:
            raise ConnectionError("daemon gone")
        return StatusSummary.model_validate({"master": {**MASTER_JSON, "volume": -31.0}})


def make_probe_monitor(bus: Bus, client: ProbeClient, **kwargs) -> DeviceMonitor:
    async def resolve() -> int:
        return 0

    return DeviceMonitor(
        serial=1,
        bus=bus,
        client=client,
        resolve_index=resolve,
        backoff_min_s=0.02,
        backoff_max_s=0.1,
        levels_idle_grace_s=0.05,
        **kwargs,
    )


async def test_connects_without_keepalive_pings():
    """minidspd never reads the socket, so pings would time out and drop a healthy stream."""
    connect = SlowHandshake(0.0)
    kwargs: list[dict] = []

    def capture(url, **kw):
        kwargs.append(kw)
        return connect(url, **kw)

    monitor = make_monitor(Bus(), connect=capture)
    monitor.start()
    try:
        await wait_for(lambda: monitor.connected)
        assert kwargs[0]["ping_interval"] is None
    finally:
        await monitor.stop()


async def test_quiet_stream_is_probed_over_http_and_kept(fake_ws):
    bus = Bus()
    client = ProbeClient(fake_ws.base)
    monitor = make_probe_monitor(bus, client, idle_probe_s=0.05)
    monitor.start()
    try:
        await wait_for(lambda: client.probes >= 3)
        assert monitor.connected and monitor.connect_count == 1
        assert monitor.status.master.volume == -31.0  # the probe's status is merged in
    finally:
        await monitor.stop()


async def test_quiet_stream_reconnects_when_the_daemon_stops_answering(fake_ws):
    bus = Bus()
    client = ProbeClient(fake_ws.base)
    monitor = make_probe_monitor(bus, client, idle_probe_s=0.05)
    monitor.start()
    try:
        await wait_for(lambda: monitor.connected)
        client.fail = True
        await wait_for(lambda: monitor.connect_count >= 2, timeout=10)
        assert len(fake_ws.paths) >= 2
    finally:
        await monitor.stop()
