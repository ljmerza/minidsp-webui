"""Per-device service layer: master controls, cached status, and (from Phase 2) desired state and the writer.

The UI pages, the JSON API and MQTT all go through `DeviceSession`, so validation and safety rules live in one place.
Sessions are keyed by device serial; the daemon index is re-resolved on every (re)connect.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from app.bus import DEVICE, LOG, Bus
from app.config import Settings
from app.daemon.client import DaemonClient, DaemonError, DaemonUnavailable, DeviceNotFound
from app.daemon.monitor import DeviceMonitor
from app.model.config_doc import ConfigDoc
from app.model.device_profile import DeviceProfile, profile_for
from app.model.master import DaemonDevice, MasterStatus, MasterUpdate, StatusSummary

log = logging.getLogger(__name__)


class SessionState(StrEnum):
    STARTING = "starting"
    CONNECTED = "connected"  # daemon up, device listed
    ABSENT = "absent"  # daemon up, device not listed / not ready
    DAEMON_DOWN = "daemon_down"


class MasterValidationError(ValueError):
    """A master change was rejected before reaching the device."""


class ConfirmationRequired(MasterValidationError):
    """The change is allowed but guarded (e.g. a big volume raise): repeat it with ``confirm`` set."""


class PresetSwitchInProgress(RuntimeError):
    """Writes are blocked while the device switches presets."""


class PresetSwitchTimeout(TimeoutError):
    """The device did not report the requested preset in time."""


class DeviceSession:
    def __init__(
        self,
        serial: int,
        profile: DeviceProfile,
        *,
        client: DaemonClient,
        bus: Bus,
        settings: Settings,
        device: DaemonDevice | None = None,
        daemon_index: int | None = None,
    ) -> None:
        self.serial = serial
        self.profile = profile
        self.client = client
        self.bus = bus
        self.settings = settings
        self.device = device
        self.daemon_index = daemon_index
        self.state = SessionState.STARTING
        self.preset_switching: int | None = None  # target preset while a switch is in progress
        self._master_lock = asyncio.Lock()
        self.monitor = DeviceMonitor(
            serial=serial,
            bus=bus,
            client=client,
            resolve_index=self.resolve_index,
            backoff_min_s=settings.monitor_backoff_min_s,
            backoff_max_s=settings.monitor_backoff_max_s,
            levels_idle_grace_s=settings.levels_idle_grace_s,
            on_connected=self._on_monitor_connected,
            on_disconnected=self._on_monitor_disconnected,
        )
        self.on_lost: Callable[[], None] | None = None  # set by SessionManager: called when the device seems gone

    # --- lifecycle ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        self.monitor.start()

    async def stop(self) -> None:
        await self.monitor.stop()

    # --- state ----------------------------------------------------------------------------------------------------

    @property
    def status(self) -> StatusSummary | None:
        return self.monitor.status

    @property
    def master(self) -> MasterStatus | None:
        return self.monitor.master

    @property
    def available_sources(self) -> list[str]:
        return list(self.profile.sources)

    def set_state(self, state: SessionState, *, force_event: bool = False) -> None:
        if state != self.state or force_event:
            self.state = state
            self.bus.publish(DEVICE, self.device_event(), serial=self.serial)

    def device_event(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "state": str(self.state),
            "monitor_connected": self.monitor.connected,
            "preset_switching": self.preset_switching,
        }

    def info(self) -> dict[str, Any]:
        """JSON-ready description of the device for the API and the UI."""
        version = self.device.version if self.device else None
        return {
            "serial": self.serial,
            "product_name": self.profile.product_name,
            "url": self.device.url if self.device else None,
            "daemon_index": self.daemon_index,
            "hw_id": version.hw_id if version else None,
            "dsp_version": version.dsp_version if version else None,
            "fw": version.fw if version else None,
            "state": str(self.state),
            "monitor_connected": self.monitor.connected,
            "last_error": self.monitor.last_error,
            "profile": self.profile.model_dump(),
        }

    def log_event(self, level: str, message: str, **data: Any) -> None:
        getattr(log, level if level in ("debug", "info", "warning", "error") else "info")(
            "[%s] %s", self.serial, message
        )
        self.bus.publish(LOG, {"serial": self.serial, "level": level, "message": message, **data}, serial=self.serial)

    def update_device(self, index: int, device: DaemonDevice) -> None:
        changed = index != self.daemon_index
        self.daemon_index = index
        self.device = device
        self.set_state(SessionState.CONNECTED)
        if changed and self.monitor.connected:
            self.monitor.kick()

    async def resolve_index(self) -> int:
        """Look the device up by serial on the daemon and remember its current index."""
        try:
            index, device = await self.client.resolve_index(self.serial)
        except DeviceNotFound:
            self.daemon_index = None
            self.set_state(SessionState.ABSENT)
            raise
        except DaemonUnavailable:
            self.set_state(SessionState.DAEMON_DOWN)
            raise
        self.update_device(index, device)
        return index

    async def _index(self) -> int:
        return self.daemon_index if self.daemon_index is not None else await self.resolve_index()

    async def _on_monitor_connected(self, recovered: bool) -> None:
        self.set_state(SessionState.CONNECTED, force_event=True)
        if recovered:
            # Possibly a power cycle (docs 03 §8). Phase 2 marks the shadow unknown / re-applies here.
            self.log_event("warning", "device stream reconnected")

    async def _on_monitor_disconnected(self) -> None:
        self.bus.publish(DEVICE, self.device_event(), serial=self.serial)
        self.log_event("warning", f"device stream lost: {self.monitor.last_error or 'disconnected'}")
        if self.on_lost:
            self.on_lost()

    async def refresh_status(self) -> StatusSummary:
        """Fetch a fresh status over HTTP and merge it into the cache."""
        status = await self._call(lambda i: self.client.get_status(i))
        self._apply_master(status.master)
        return status

    async def active_preset(self, max_age_s: float = 2.0) -> int:
        """Active preset from the cached status, refreshed over HTTP when older than ``max_age_s``."""
        age = self.monitor.status_age()
        master = self.master
        if master is None or master.preset is None or age is None or age > max_age_s:
            master = (await self.refresh_status()).master
        if master.preset is None:
            raise DaemonError("minidspd did not report the active preset")
        return master.preset

    # --- master controls ------------------------------------------------------------------------------------------

    def validate_master(self, update: MasterUpdate) -> MasterUpdate:
        """Check ranges and normalize names. Raises `MasterValidationError`."""
        p = self.profile
        changes: dict[str, Any] = {}
        if update.volume is not None and not p.master_volume_min_db <= update.volume <= p.master_volume_max_db:
            raise MasterValidationError(
                f"volume {update.volume} dB outside {p.master_volume_min_db}..{p.master_volume_max_db}"
            )
        if update.source is not None:
            canonical = p.canonical_source(update.source)
            if canonical is None:
                raise MasterValidationError(f"unknown source {update.source!r}; expected one of {', '.join(p.sources)}")
            changes["source"] = canonical
        if update.preset is not None and not 0 <= update.preset < p.n_presets:
            raise MasterValidationError(f"preset {update.preset} outside 0..{p.n_presets - 1}")
        return update.model_copy(update=changes)

    async def set_master(self, update: MasterUpdate) -> MasterStatus:
        """Apply any combination of volume/mute/source/preset. A preset change is done first, on the slow path."""
        update = self.validate_master(update)
        if update.preset is not None:
            await self.set_preset(update.preset)
        rest = MasterStatus(volume=update.volume, mute=update.mute, source=update.source)
        if not rest.model_dump(exclude_none=True):
            return self.master or MasterStatus()
        self._check_not_switching()
        async with self._master_lock:
            if rest.volume is not None and not update.confirm:
                await self._check_volume_raise(rest.volume)
            result = await self._call(lambda i: self.client.post_master(i, rest))
        return self._apply_master(result)

    async def _check_volume_raise(self, target: float) -> None:
        """Gain-step guard (docs 03 §5): raising the master volume by more than ``gain_step_confirm_db`` in one change
        needs ``confirm``. A volume we can't read counts as the bottom of the range."""
        limit = self.settings.gain_step_confirm_db
        if limit <= 0:
            return
        current = self._live_master().volume
        if current is None:
            current = (await self.refresh_status()).master.volume
        reference = current if current is not None else self.profile.master_volume_min_db
        if target - reference > limit:
            raise ConfirmationRequired(
                f"raising the master volume by {target - reference:g} dB (to {target:g} dB) needs confirmation"
            )

    async def set_volume(self, volume_db: float, *, confirm: bool = False) -> MasterStatus:
        return await self.set_master(MasterUpdate(volume=volume_db, confirm=confirm))

    async def set_mute(self, mute: bool) -> MasterStatus:
        return await self.set_master(MasterUpdate(mute=mute))

    async def set_source(self, source: str) -> MasterStatus:
        return await self.set_master(MasterUpdate(source=source))

    async def toggle_mute(self) -> MasterStatus:
        self._check_not_switching()
        async with self._master_lock:
            current = self._live_master().mute
            result = await self._call(lambda i: self.client.toggle_mute(i, current=current))
        return self._apply_master(result)

    async def volume_step(self, direction: int) -> MasterStatus:
        """±0.5 dB as an absolute write from the current volume, clamped to the profile's range."""
        if direction not in (1, -1):
            raise MasterValidationError("direction must be +1 or -1")
        self._check_not_switching()
        p = self.profile
        async with self._master_lock:
            current = self._live_master().volume
            if current is None:
                current = (await self.refresh_status()).master.volume
            if current is None:
                raise DaemonError("minidspd did not report the master volume")
            target = min(
                p.master_volume_max_db, max(p.master_volume_min_db, current + direction * p.master_volume_step_db)
            )
            result = await self._call(lambda i: self.client.post_master(i, MasterStatus(volume=target)))
        return self._apply_master(result)

    def _live_master(self) -> MasterStatus:
        """The cached master status if the monitor is connected (poll frames keep it current), else an empty one so
        callers read it fresh from the daemon."""
        if self.monitor.connected and self.master is not None:
            return self.master
        return MasterStatus()

    async def set_preset(self, preset: int) -> MasterStatus:
        """Switch preset and wait until the device reports it. Writes are blocked meanwhile."""
        self.validate_master(MasterUpdate(preset=preset))
        self._check_not_switching()
        timeout = self.settings.preset_switch_timeout_s
        deadline = time.monotonic() + timeout
        self.preset_switching = preset
        self.bus.publish(DEVICE, self.device_event(), serial=self.serial)
        self.log_event("info", f"switching to preset {preset}", preset=preset)
        try:
            async with self._master_lock:
                result = await self._call(lambda i: self.client.set_preset(i, preset))
                self._apply_master(result)
                while self.master is None or self.master.preset != preset:
                    if time.monotonic() >= deadline:
                        raise PresetSwitchTimeout(f"device did not report preset {preset} within {timeout:g} s")
                    await asyncio.sleep(0.5)
                    # The device can be briefly unresponsive while it resets.
                    with contextlib.suppress(DaemonError):
                        await self.refresh_status()
        finally:
            self.preset_switching = None
            self.bus.publish(DEVICE, self.device_event(), serial=self.serial)
        return self.master or MasterStatus(preset=preset)

    def _check_not_switching(self) -> None:
        if self.preset_switching is not None:
            raise PresetSwitchInProgress(f"preset switch to {self.preset_switching} in progress")

    def _apply_master(self, master: MasterStatus) -> MasterStatus:
        return self.monitor.update_master(master)

    async def _call(self, fn: Callable[[int], Awaitable[Any]]) -> Any:
        """Run ``fn(index)`` against the daemon, keeping session state in step with failures."""
        index = await self._index()
        try:
            result = await fn(index)
        except DeviceNotFound:
            self.daemon_index = None
            self.set_state(SessionState.ABSENT)
            if self.on_lost:
                self.on_lost()
            raise
        except DaemonUnavailable:
            self.set_state(SessionState.DAEMON_DOWN)
            if self.on_lost:
                self.on_lost()  # let discovery re-check with its short backoff instead of the full interval
            raise
        self.set_state(SessionState.CONNECTED)
        return result

    # --- preset configuration (Phase 2) ---------------------------------------------------------------------------

    async def get_desired(self, preset: int) -> ConfigDoc:
        raise NotImplementedError("Phase 2: desired-state store")

    async def patch_desired(self, preset: int, patch: dict[str, Any], *, confirm: bool = False) -> ConfigDoc:
        raise NotImplementedError("Phase 2: desired-state store + writer")

    def sync_status(self, preset: int) -> dict[str, str]:
        raise NotImplementedError("Phase 2: per-path sync status")

    async def push_all(self, preset: int, *, mute_wrap: bool | None = None) -> None:
        raise NotImplementedError("Phase 2: push-all")

    async def activate_preset(self, preset: int, *, flush: bool = True) -> MasterStatus:
        raise NotImplementedError("Phase 2: switch preset and flush staged changes")


class SessionManager:
    """Owns one `DeviceSession` per supported device serial. Survives the daemon being down: it keeps retrying."""

    def __init__(self, client: DaemonClient, bus: Bus, settings: Settings) -> None:
        self.client = client
        self.bus = bus
        self.settings = settings
        self.sessions: dict[int, DeviceSession] = {}
        self.devices: list[DaemonDevice] = []
        self.unsupported: list[DaemonDevice] = []
        self.daemon_up: bool | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    def get(self, serial: int) -> DeviceSession | None:
        return self.sessions.get(serial)

    def default(self) -> DeviceSession | None:
        return next(iter(self.sessions.values()), None)

    def __iter__(self):
        return iter(list(self.sessions.values()))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="session-discovery")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await asyncio.gather(*(s.stop() for s in self.sessions.values()), return_exceptions=True)

    def request_rescan(self) -> None:
        self._wake.set()

    def _set_daemon_up(self, up: bool, error: str | None = None) -> None:
        self.last_error = error
        if up != self.daemon_up:
            self.daemon_up = up
            message = "minidspd reachable" if up else f"minidspd unreachable: {error}"
            self.bus.publish(LOG, {"serial": None, "level": "info" if up else "warning", "message": message})
            self.bus.publish(DEVICE, {"serial": None, "daemon": "up" if up else "down"})

    async def discover(self) -> list[DaemonDevice]:
        """List devices on the daemon, creating/updating sessions. Never raises for daemon failures."""
        try:
            devices = await self.client.list_devices()
        except DaemonError as exc:
            self._set_daemon_up(False, str(exc))
            for session in self.sessions.values():
                session.set_state(SessionState.DAEMON_DOWN)
            return []
        self._set_daemon_up(True)
        self.devices = devices
        self.unsupported = []
        seen: set[int] = set()
        for index, device in enumerate(devices):
            serial = device.serial
            if serial is None:
                continue  # still being probed by the daemon
            profile = profile_for(
                device.product_name, gain_min_db=self.settings.gain_min_db, gain_max_db=self.settings.gain_max_db
            )
            if profile is None:
                self.unsupported.append(device)
                continue
            seen.add(serial)
            session = self.sessions.get(serial)
            if session is None:
                session = DeviceSession(
                    serial,
                    profile,
                    client=self.client,
                    bus=self.bus,
                    settings=self.settings,
                    device=device,
                    daemon_index=index,
                )
                session.on_lost = self.request_rescan
                self.sessions[serial] = session
                session.set_state(SessionState.CONNECTED)
                session.log_event("info", f"found {device.product_name} at daemon index {index}")
                session.start()
            else:
                session.update_device(index, device)
        for serial, session in self.sessions.items():
            if serial not in seen:
                session.daemon_index = None
                session.set_state(SessionState.ABSENT)
        return devices

    async def _run(self) -> None:
        retry_cap = max(1.0, min(10.0, self.settings.discovery_interval_s))
        backoff = 1.0
        while True:
            try:
                await self.discover()
            except Exception as exc:
                # discover() handles daemon errors itself; anything else is a bug, but must not end discovery.
                log.exception("device discovery failed")
                self._set_daemon_up(False, str(exc) or type(exc).__name__)
            healthy = bool(self.daemon_up and self.sessions) and all(
                s.state == SessionState.CONNECTED for s in self.sessions.values()
            )
            if healthy:
                delay, backoff = self.settings.discovery_interval_s, 1.0
            else:
                delay, backoff = backoff, min(backoff * 2, retry_cap)
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
