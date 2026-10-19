"""One upstream websocket per device: ``/devices/{i}?levels=true&poll=true``.

The first message is a full status. After that the daemon sends level frames (~4 Hz) and master-status diffs
(2 s polling plus instant IR-remote changes). The monitor keeps a merged cached status and publishes ``levels`` and
``master`` events on the bus. When nobody subscribes to levels (after a grace period) it reconnects with ``poll=true``
only, sparing the USB bus; when a levels subscriber appears it reconnects with levels again.

minidspd (0.1.12) only ever writes to this socket and never reads it, so it answers neither pings nor close frames.
Websocket keepalive pings would therefore drop a healthy stream every ``ping_interval + ping_timeout`` seconds (and
each drop marks the active preset unknown). Instead, when the stream has been quiet for ``idle_probe_s`` the monitor
checks the daemon over HTTP and reconnects only if that fails twice in a row.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import connect as ws_connect

from app.bus import LEVELS, MASTER, Bus
from app.daemon.client import DaemonClient
from app.model.master import MasterStatus, StatusSummary

log = logging.getLogger(__name__)

Hook = Callable[[], Awaitable[None] | None]

IDLE_PROBE_FAILURES = 2  # consecutive failed HTTP probes before a quiet stream is treated as dead


class DeviceMonitor:
    def __init__(
        self,
        *,
        serial: int,
        bus: Bus,
        client: DaemonClient,
        resolve_index: Callable[[], Awaitable[int]],
        backoff_min_s: float = 0.5,
        backoff_max_s: float = 10.0,
        levels_idle_grace_s: float = 5.0,
        idle_probe_s: float = 30.0,
        on_connected: Callable[[bool], Awaitable[None] | None] | None = None,  # arg: recovered after a drop
        on_disconnected: Hook | None = None,
        connect: Callable[..., Any] = ws_connect,
        levels_hook: Callable[[list[float], list[float]], tuple[list[float], list[float]]] | None = None,
    ) -> None:
        self.serial = serial
        self._bus = bus
        self._client = client
        self._resolve_index = resolve_index
        self._backoff_min = backoff_min_s
        self._backoff_max = backoff_max_s
        self._grace = levels_idle_grace_s
        self._idle_probe = idle_probe_s
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._connect = connect
        # Rewrites each level frame (the embedded mock's level pattern, app/daemon/mock_levels.py); None: as received.
        self._levels_hook = levels_hook

        self.status: StatusSummary | None = None
        self.connected = False
        self.levels_enabled = False
        self.last_message_at: float | None = None  # time.monotonic()
        # time.monotonic() when the stream came up; kept across our own reconnects (levels on/off, a new daemon index)
        self.connected_since: float | None = None
        self.last_error: str | None = None
        self.connect_count = 0

        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self._first_message = True
        self._reconnect_now = False
        self._recovering = False
        self._grace_handle: asyncio.TimerHandle | None = None
        self._stop = asyncio.Event()
        self._unwatch: Callable[[], None] | None = None

    # --- lifecycle ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._unwatch = self._bus.on_subscribers_changed(self._check_demand)
            self._task = asyncio.create_task(self._run(), name=f"monitor-{self.serial}")

    async def stop(self) -> None:
        self._stop.set()
        if self._unwatch:
            self._unwatch()
            self._unwatch = None
        self._cancel_grace()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.connected = False
        self.connected_since = None

    def kick(self) -> None:
        """Drop the current connection and reconnect right away (e.g. after the device was re-discovered)."""
        self._reconnect_now = True
        self._close_ws()

    @property
    def master(self) -> MasterStatus | None:
        return self.status.master if self.status else None

    def status_age(self) -> float | None:
        return None if self.last_message_at is None else time.monotonic() - self.last_message_at

    def uptime(self) -> float | None:
        """Seconds the device stream has been up; None while it is down."""
        if not self.connected or self.connected_since is None:
            return None
        return time.monotonic() - self.connected_since

    # --- levels demand --------------------------------------------------------------------------------------------

    def levels_wanted(self) -> bool:
        return self._bus.subscriber_count(LEVELS) > 0

    def _check_demand(self) -> None:
        wanted = self.levels_wanted()
        if wanted:
            self._cancel_grace()
            if self.connected and not self.levels_enabled:
                self.kick()
        elif self.levels_enabled and self._grace_handle is None:
            loop = asyncio.get_running_loop()
            self._grace_handle = loop.call_later(self._grace, self._drop_levels)

    def _drop_levels(self) -> None:
        self._grace_handle = None
        if self.levels_enabled and not self.levels_wanted():
            log.debug("no level subscribers for device %s, reconnecting without levels", self.serial)
            self.kick()

    def _cancel_grace(self) -> None:
        if self._grace_handle is not None:
            self._grace_handle.cancel()
            self._grace_handle = None

    def _close_ws(self) -> None:
        if self._ws is not None:
            asyncio.ensure_future(self._ws.close())

    # --- main loop ------------------------------------------------------------------------------------------------

    async def _run(self) -> None:
        backoff = self._backoff_min
        while not self._stop.is_set():
            self._reconnect_now = False
            try:
                index = await self._resolve_index()
                levels = self.levels_wanted()
                url = self._client.ws_url(index, levels=levels, poll=True)
                # minidspd never answers pings or close frames (see the module docstring): no keepalive pings, and a
                # short close_timeout so reconnects and shutdown aren't delayed.
                async with self._connect(url, open_timeout=10, ping_interval=None, close_timeout=1) as ws:
                    self._ws = ws
                    await self._mark_connected(levels)
                    backoff = self._backoff_min
                    watchdog = asyncio.create_task(self._watch_idle(index, ws), name=f"monitor-idle-{self.serial}")
                    try:
                        async for message in ws:
                            self.handle_message(message)
                    finally:
                        watchdog.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await watchdog
                if not self._reconnect_now and not watchdog_closed(watchdog):
                    self.last_error = "connection closed by minidspd"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc) or type(exc).__name__
                log.debug("monitor for device %s: %s", self.serial, self.last_error)
            finally:
                self._ws = None

            await self._mark_disconnected()
            if self._reconnect_now:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, self._backoff_max)

    async def _watch_idle(self, index: int, ws: Any) -> bool:
        """Probe the daemon over HTTP while the stream is quiet; close the stream (returning True) if the daemon stops
        answering."""
        failures = 0
        while True:
            age = self.status_age()
            if age is None or age < self._idle_probe:
                await asyncio.sleep(self._idle_probe - (age or 0.0))
                continue
            try:
                status = await self._client.get_status(index)
            except Exception as exc:
                failures += 1
                self.last_error = str(exc) or type(exc).__name__
                log.debug("idle probe %d for device %s failed: %s", failures, self.serial, self.last_error)
                if failures >= IDLE_PROBE_FAILURES:
                    log.warning("device %s stream is quiet and the daemon isn't answering; reconnecting", self.serial)
                    await ws.close()
                    return True
                await asyncio.sleep(min(self._idle_probe, 5.0))
                continue
            failures = 0
            self.update_master(status.master)  # refreshes last_message_at

    async def _mark_connected(self, levels: bool) -> None:
        recovered, self._recovering = self._recovering, False
        self.connect_count += 1
        self.connected = True
        if self.connected_since is None:
            self.connected_since = time.monotonic()
        self.levels_enabled = levels
        self._first_message = True
        self.last_error = None
        if not levels and self.status is not None:
            self.status = self.status.model_copy(update={"input_levels": [], "output_levels": []})
        # Demand may have changed during the handshake: reconnect with levels if a subscriber appeared, or start the
        # grace timer if the last one left.
        self._check_demand()
        await _call(self._on_connected, recovered)

    async def _mark_disconnected(self) -> None:
        was_connected = self.connected
        self.connected = False
        self.levels_enabled = False
        if not self._reconnect_now:
            self.connected_since = None
        self._cancel_grace()
        if was_connected and not self._reconnect_now:
            self._recovering = True
            await _call(self._on_disconnected)

    def update_master(self, master: MasterStatus) -> MasterStatus:
        """Merge a master status read some other way (e.g. a write response) and publish it if it changed."""
        previous = self.status
        old = previous.master if previous else None
        merged = old.merged(master) if old else master
        if previous:
            self.status = previous.model_copy(update={"master": merged})
        else:
            self.status = StatusSummary(master=merged)
        self.last_message_at = time.monotonic()
        if merged != old:
            self._bus.publish(MASTER, {"serial": self.serial, "master": merged.model_dump()}, serial=self.serial)
        return merged

    # --- message handling -----------------------------------------------------------------------------------------

    def handle_message(self, message: str | bytes) -> None:
        """Parse one upstream message, update the cached status and publish events."""
        try:
            summary = StatusSummary.model_validate(json.loads(message))
        except ValueError:
            log.warning("ignoring unparseable status message from device %s", self.serial)
            return
        self.last_message_at = time.monotonic()

        previous = self.status
        old_master = previous.master if previous else None
        # Level frames carry an empty master object; only a non-empty one replaces (first message) or updates it.
        has_master = bool(summary.master.model_dump(exclude_none=True))
        if previous is None or (self._first_message and has_master):
            master = summary.master
        else:
            master = previous.master.merged(summary.master)
        if has_master:
            self._first_message = False

        input_levels = previous.input_levels if previous else []
        output_levels = previous.output_levels if previous else []
        has_levels = self.levels_enabled and bool(summary.input_levels or summary.output_levels)
        if has_levels:
            input_levels, output_levels = summary.input_levels, summary.output_levels
            if self._levels_hook is not None:
                try:
                    input_levels, output_levels = self._levels_hook(list(input_levels), list(output_levels))
                except Exception:  # a broken pattern must not stop the stream: keep the frame as received
                    log.exception("level hook failed for device %s", self.serial)

        self.status = StatusSummary(master=master, input_levels=input_levels, output_levels=output_levels)

        if master != old_master:
            self._bus.publish(MASTER, {"serial": self.serial, "master": master.model_dump()}, serial=self.serial)
        if has_levels:
            self._bus.publish(LEVELS, {"i": input_levels, "o": output_levels}, serial=self.serial)


def watchdog_closed(task: asyncio.Task) -> bool:
    """Whether the idle watchdog closed the stream (its probe error is then kept as ``last_error``)."""
    return task.done() and not task.cancelled() and task.exception() is None and task.result() is True


async def _call(hook: Callable[..., Awaitable[None] | None] | None, *args: Any) -> None:
    if hook is None:
        return
    try:
        result = hook(*args)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        log.exception("monitor hook failed")
