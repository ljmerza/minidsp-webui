"""Per-device service layer: master controls, cached status, desired state per preset and the writer.

The UI pages, the JSON API and MQTT all go through `DeviceSession`, so validation and safety rules live in one place.
Sessions are keyed by device serial; the daemon index is re-resolved on every (re)connect.

Preset state (docs 03 §1-3, §8):

- Each preset has a desired `ConfigDoc` and an applied shadow, held by the `DeviceWriter` and persisted (debounced)
  in ``preset_state``. Master settings are not part of it: they are device-truth, read from the monitor.
- Edits are JSON merge patches validated here (shape, ranges, per-channel gain ceilings, the big-step guard) and then
  handed to the writer. Edits to an inactive preset are staged by the writer and flushed when it becomes active.
- Revision rows are written at commit boundaries only: an explicit commit (form submit, slider release), or after
  ``commit_idle_s`` without edits.
"""

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from app.bus import DESIRED, DEVICE, LOG, MASTER, Bus
from app.config import Settings
from app.daemon.client import DaemonClient, DaemonError, DaemonUnavailable, DeviceNotFound
from app.daemon.monitor import DeviceMonitor
from app.formats.cli_script import ParsedScript, ScriptIssue, export_script, parse_script
from app.model.config_doc import ConfigDoc, PathError, default_config_doc, diff_paths, flatten, set_path, validate_doc
from app.model.device_profile import DeviceProfile, profile_for
from app.model.master import DaemonDevice, MasterStatus, MasterUpdate, StatusSummary
from app.model.payload import device_values, same_value, value_problem
from app.state import ChannelMeta, PresetSnapshot, Repo, RevisionConflict, RevisionInfo, preset_state_persister
from app.state.db import utcnow_iso
from app.state.repo import REVISION_SOURCES
from app.sync.writer import DeviceWriter, FlushResult, PresetState, SyncStatus

log = logging.getLogger(__name__)

COMMIT_IDLE_S = 5.0
BULK_TIMEOUT_S = 60.0  # longest a bulk apply (push-all, import, adopt) is awaited by the caller
COMPRESSOR_THRESHOLD_RANGE = (-90.0, 0.0)  # spike-results §5: the device stores any float, keep it sane
_MISSING = object()
NO_PRESET = -1  # what the writer sees as "active" while a preset switch is in progress: nothing matches it
# Reserved key in the stored applied shadow (never a dotted path): paths that were edited while their device value was
# unknown and still have to be written. Without it a staged edit to an unknown path would be forgotten on restart.
PENDING_KEY = "#pending"
# Reserved key: outputs an unfinished mute-wrapped bulk apply holds muted. Restored on load, so a restart doesn't
# un-mute them before the rest of the apply has landed.
WRAP_KEY = "#wrap"


class SessionState(StrEnum):
    STARTING = "starting"
    CONNECTED = "connected"  # daemon up, device listed
    ABSENT = "absent"  # daemon up, device not listed / not ready
    DAEMON_DOWN = "daemon_down"


class AdoptMode(StrEnum):
    TRUST = "trust"  # the device holds this config: shadow = desired, nothing is written
    PUSH = "push"  # write all of it (mute-wrapped)
    UNKNOWN = "unknown"  # nothing is known: each field is written the first time it is touched


class MasterValidationError(ValueError):
    """A master change was rejected before reaching the device."""


class ConfirmationRequired(MasterValidationError):
    """The change is allowed but guarded (e.g. a big volume raise): repeat it with ``confirm`` set."""


class DesiredValidationError(MasterValidationError):
    """A desired-state edit was rejected; ``problems`` lists every reason (``path: message``)."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        self.problems = list(problems) or [message]
        super().__init__(f"{message}: {'; '.join(problems)}" if problems else message)


class UnknownPreset(MasterValidationError):
    """The preset index is outside the device's range."""


class StateUnavailable(RuntimeError):
    """The stored preset state could not be loaded (database error); preset operations are refused meanwhile."""


class PresetSwitchInProgress(RuntimeError):
    """Writes are blocked while the device switches presets."""


class PresetSwitchTimeout(TimeoutError):
    """The device did not report the requested preset in time."""


@dataclass(frozen=True)
class EditResult:
    """Outcome of a desired-state edit. ``paths`` are the changed document paths (design included), ``device_paths``
    the changed paths that are written to the device."""

    preset: int
    revision: int
    doc: ConfigDoc
    paths: list[str] = field(default_factory=list)
    device_paths: list[str] = field(default_factory=list)
    flush: FlushResult | None = None  # set when the edit was applied as a bulk apply and awaited


@dataclass(frozen=True)
class ImportResult:
    edit: EditResult
    parsed: ParsedScript


@dataclass(frozen=True)
class AdoptResult:
    preset: int
    mode: AdoptMode
    revision: int
    known: int  # device paths the shadow now holds
    unknown: int
    writes: int  # calls sent to the daemon by the adopt itself
    flush: FlushResult | None = None
    warnings: list[ScriptIssue] = field(default_factory=list)


@dataclass
class _Uncommitted:
    source: str
    summary: str | None = None


def flush_json(result: FlushResult | None) -> dict[str, Any] | None:
    """JSON-ready form of a writer `FlushResult`."""
    if result is None:
        return None
    return {
        "ok": result.ok,
        "staged": result.staged,
        "pending": list(result.pending),
        "failed": dict(result.failed),
        "error": result.error,
    }


def merge_patch(target: Any, patch: Any, path: str = "") -> Any:
    """JSON merge patch (RFC 7386) adapted to ConfigDoc, which is mostly fixed-length lists.

    - An object patches an object key by key; unknown keys are an error (ConfigDoc has no optional keys).
    - An object patches a list by index: ``{"outputs": {"1": {"gain_db": 3}}}``. A JSON array replaces the list.
    - ``null`` sets the value to null (it does not delete), so ``{"design": null}`` clears a crossover design.
    """
    if not isinstance(patch, Mapping):
        return patch
    if isinstance(target, Mapping):
        out = dict(target)
        for key, value in patch.items():
            where = f"{path}.{key}" if path else str(key)
            if key not in target:
                raise DesiredValidationError("invalid patch", [f"{where}: unknown field"])
            out[key] = merge_patch(target[key], value, where)
        return out
    if isinstance(target, list):
        items = list(target)
        for key, value in patch.items():
            where = f"{path}.{key}" if path else str(key)
            if not (isinstance(key, str) and key.isascii() and key.isdigit()) or int(key) >= len(items):
                raise DesiredValidationError("invalid patch", [f"{where}: no such list index (0..{len(items) - 1})"])
            items[int(key)] = merge_patch(items[int(key)], value, where)
        return items
    return dict(patch)  # e.g. setting a design where there was none


def parse_doc(data: Any) -> ConfigDoc:
    """Validate a JSON document strictly (no "1" -> true coercion); errors become `DesiredValidationError`."""
    try:
        return ConfigDoc.model_validate(data, strict=True)
    except ValidationError as exc:
        problems = [f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in exc.errors()]
        raise DesiredValidationError("invalid preset document", problems) from None


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def describe_change(old: ConfigDoc | None, new: ConfigDoc, paths: Sequence[str]) -> str:
    """Short human summary of a set of changed paths, used for revision rows."""
    if old is None:
        return "initial state"
    if len(paths) == 1:
        a, b = flatten(old).get(paths[0]), flatten(new).get(paths[0])
        if not isinstance(a, dict | list) and not isinstance(b, dict | list):
            return f"{paths[0]}: {_fmt(a)} → {_fmt(b)}"
        return f"{paths[0]} changed"
    head = ", ".join(paths[:3])
    return f"{len(paths)} changes: {head}{', …' if len(paths) > 3 else ''}"


def _shape_problems(doc: ConfigDoc, profile: DeviceProfile) -> list[str]:
    """Only the length checks of `validate_doc`: a stored doc with the wrong shape can't be used at all."""
    return [p for p in validate_doc(doc, profile) if "expected" in p and "entries" in p]


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
        repo: Repo | None = None,
        commit_idle_s: float = COMMIT_IDLE_S,
    ) -> None:
        self.serial = serial
        self.profile = profile
        self.client = client
        self.bus = bus
        self.settings = settings
        self.device = device
        self.daemon_index = daemon_index
        self.repo = repo
        self.commit_idle_s = commit_idle_s
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

        self.writer = DeviceWriter(
            serial,
            profile,
            post_config=self._post_config,
            active_preset=self._writer_active_preset,
            bus=bus,
            fir_taps=self._fir_taps if repo is not None else None,
            on_applied=self._on_applied,
            on_log=self._writer_log,
            coalesce_ms=settings.write_coalesce_ms,
            value_check=self._ceiling_problem,
        )
        self._persister = preset_state_persister(repo) if repo is not None else None
        self.loaded = False
        self.load_error: str | None = None
        self.adoption: dict[str, Any] | None = None
        self.channel_meta: dict[tuple[str, int], ChannelMeta] = {}
        self.revisions: dict[int, int] = {}
        self._baseline: dict[int, ConfigDoc] = {}  # desired doc at the last commit
        self._flat_cache: dict[int, tuple[ConfigDoc, dict[str, Any]]] = {}
        self._uncommitted: dict[int, _Uncommitted] = {}
        self._commit_timers: dict[int, asyncio.TimerHandle] = {}
        self._load_lock = asyncio.Lock()
        self._edit_lock = asyncio.Lock()
        self._commit_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._watcher: asyncio.Task | None = None
        self._seen_preset: int | None = None
        self._stopped = False

    # --- lifecycle ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        self._stopped = False
        self.monitor.start()
        self.writer.start()
        if self._watcher is None:
            self._watcher = asyncio.create_task(self._watch_master(), name=f"session-master-{self.serial}")

    async def stop(self) -> None:
        self._stopped = True
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
            self._watcher = None
        await self.monitor.stop()
        await self.writer.stop()
        for handle in self._commit_timers.values():
            handle.cancel()
        self._commit_timers.clear()
        if self.loaded:
            for preset in list(self._uncommitted):
                await self._commit_quietly(preset)
        if self._persister is not None:
            await self._persister.aclose()
        tasks = [t for t in self._tasks if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=5)

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=f"{name}-{self.serial}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("[%s] background task %s failed", self.serial, task.get_name(), exc_info=task.exception())

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

    @property
    def needs_adoption(self) -> bool:
        """True until one of the adopt flows has been run for this device."""
        return self.adoption is None

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
            "state_loaded": self.loaded,
            "adopted": self.adoption is not None if self.loaded else None,
        }

    def log_event(
        self, level: str, message: str, *, kind: str = "device", persist: bool | None = None, **data: Any
    ) -> None:
        """Log, publish on the bus (``log``) and, for meaningful events, append to the persistent event log.

        ``persist`` defaults to every level but debug. Never call it per tick or per write (SD-card budget).
        """
        getattr(log, level if level in ("debug", "info", "warning", "error") else "info")(
            "[%s] %s", self.serial, message
        )
        self.bus.publish(
            LOG, {"serial": self.serial, "level": level, "kind": kind, "message": message, **data}, serial=self.serial
        )
        if persist is None:
            persist = level != "debug"
        if persist and self.repo is not None and not self._stopped:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return
            repo_level = level if level in ("debug", "info", "warning", "error") else "info"
            self._spawn(
                self.repo.log_event(kind, message, level=repo_level, data={"serial": self.serial, **data}),
                "event-log",
            )

    def update_device(self, index: int, device: DaemonDevice) -> None:
        changed = index != self.daemon_index
        self.daemon_index = index
        self.device = device
        self.set_state(SessionState.CONNECTED)
        if changed and self.monitor.connected:
            self.monitor.kick()

    async def record_device(self, *, touch: bool = False) -> None:
        """Store the device identity (writes only when it changed, or with ``touch``). Never raises."""
        if self.repo is None:
            return
        version = self.device.version if self.device else None
        try:
            await self.repo.upsert_device(
                self.serial,
                product_name=self.device.product_name if self.device else self.profile.product_name,
                hw_id=version.hw_id if version else None,
                dsp_version=version.dsp_version if version else None,
                fw=version.fw if version else None,
                daemon_url=self.settings.minidspd_url,
                touch=touch,
            )
        except Exception:
            log.exception("[%s] storing the device failed", self.serial)

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
            self.log_event("warning", "device stream reconnected", kind="reconnect")
            self._spawn(self._handle_reconnect(), "reconnect")

    async def _handle_reconnect(self) -> None:
        """Possibly a power cycle (docs 03 §8): the active preset's shadow becomes unknown, then optionally re-apply."""
        try:
            await self.ensure_loaded()
            active = await self.active_preset(max_age_s=0)
        except (DaemonError, StateUnavailable) as exc:
            log.warning("[%s] reconnect handling skipped: %s", self.serial, exc)
            self.writer.kick()
            return
        await self.record_device(touch=True)
        self.writer.invalidate(active)
        if not self.settings.reapply_on_reconnect:
            self.log_event(
                "warning",
                f"device reconnected: preset {active + 1} marked unknown "
                "(use push-all if the device lost its settings)",
                kind="reconnect",
                preset=active,
            )
            self.writer.kick()
            return
        self.log_event("warning", f"device reconnected: re-applying preset {active + 1}", kind="reconnect")
        self.writer.kick()
        try:
            result = await self.push_all(active, timeout=BULK_TIMEOUT_S)
        except DesiredValidationError as exc:
            self.log_event("error", f"re-applying preset {active + 1} refused: {exc}", kind="reconnect")
            return
        if not result.ok:
            self.log_event("error", f"re-applying preset {active + 1} did not complete", kind="reconnect")

    async def _on_monitor_disconnected(self) -> None:
        self.bus.publish(DEVICE, self.device_event(), serial=self.serial)
        self.log_event("warning", f"device stream lost: {self.monitor.last_error or 'disconnected'}", kind="reconnect")
        if self.on_lost:
            self.on_lost()

    async def _watch_master(self) -> None:
        """Flush staged edits when a preset becomes active, wherever the switch came from (app, IR remote, CLI)."""
        async with self.bus.subscribe([MASTER]) as sub:
            async for event in sub:
                if event.serial != self.serial:
                    continue
                preset = (event.data.get("master") or {}).get("preset")
                if preset is None or preset == self._seen_preset:
                    continue
                previous, self._seen_preset = self._seen_preset, preset
                if previous is not None and self.preset_switching is None:
                    self.log_event("info", f"preset {preset + 1} is now active", kind="preset", preset=preset)
                if preset in self.writer.presets:
                    self.writer.kick(preset)

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
        """Switch preset and wait until the device reports it. Writes are blocked meanwhile; afterwards the writer
        flushes whatever is staged for the now-active preset."""
        self.validate_master(MasterUpdate(preset=preset))
        self._check_not_switching()
        timeout = self.settings.preset_switch_timeout_s
        deadline = time.monotonic() + timeout
        self.preset_switching = preset
        self.bus.publish(DEVICE, self.device_event(), serial=self.serial)
        self.log_event("info", f"switching to preset {preset + 1}", kind="preset", preset=preset)
        try:
            # Wait for a config call already past the writer's preset guard, and send none until the device reports
            # the new preset: a write cleared for the old preset must not land on the new one (docs 03 §2).
            async with self.writer.paused(), self._master_lock:
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
            active = self.master.preset if self.master else None
            if active is not None:
                self.writer.kick(active)  # staged during the switch
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

    # --- writer hooks ---------------------------------------------------------------------------------------------

    async def _post_config(self, body: dict[str, Any]) -> None:
        await self._call(lambda i: self.client.post_config(i, body))

    async def _writer_active_preset(self) -> int:
        # During a switch the device may already be on the new preset while the cache still shows the old one.
        if self.preset_switching is not None:
            return NO_PRESET
        return await self.active_preset()

    async def _fir_taps(self, blob_id: str) -> list[float] | None:
        assert self.repo is not None
        blob = await self.repo.get_fir_blob(blob_id)
        return None if blob is None or blob.taps is None else blob.taps.tolist()

    def _on_applied(self, preset: int, _state: PresetState) -> None:
        self._schedule_persist(preset)

    def _writer_log(self, level: str, message: str, **data: Any) -> None:
        self.log_event(level, message, kind="sync", **data)

    def _stored_shadow(self, preset: int) -> tuple[dict[str, Any], list[str]]:
        """The applied shadow as stored (plus `PENDING_KEY`) and the paths whose status is unknown."""
        state = self.writer.state(preset)
        applied: dict[str, Any] = dict(state.applied)
        pending = sorted(p for p in state.dirty if p not in state.applied)
        if pending:
            applied[PENDING_KEY] = pending
        if state.wrap:
            applied[WRAP_KEY] = sorted(state.wrap)
        statuses = self.writer.status(preset)
        unknown = [p for p, status in statuses.items() if status == SyncStatus.UNKNOWN]
        return applied, unknown

    def _snapshot(self, preset: int) -> PresetSnapshot:
        applied, unknown = self._stored_shadow(preset)
        return PresetSnapshot(desired=self.writer.state(preset).desired, applied=applied, unknown_paths=unknown)

    def _schedule_persist(self, preset: int) -> None:
        if self._persister is None or not self.loaded:
            return
        try:
            self._persister.schedule((self.serial, preset), lambda p=preset: self._snapshot(p))
        except RuntimeError:  # closed (shutting down) or no running loop
            log.debug("[%s] not persisting preset %d: persister closed", self.serial, preset)

    # --- loading --------------------------------------------------------------------------------------------------

    async def ensure_loaded(self) -> None:
        """Load desired/applied state, revisions, channel metadata and the adoption record (once).

        Raises `StateUnavailable` if the database can't be read; the next call tries again.
        """
        if self.loaded:
            return
        async with self._load_lock:
            if self.loaded:
                return
            try:
                await self._load()
            except Exception as exc:
                self.load_error = str(exc) or type(exc).__name__
                log.exception("[%s] loading preset state failed", self.serial)
                raise StateUnavailable(f"preset state could not be loaded: {self.load_error}") from exc
            self.loaded = True
            self.load_error = None
        self.writer.kick()  # converge anything that was still pending when the app stopped

    async def _load(self) -> None:
        rows = {}
        meta: list[ChannelMeta] = []
        adoption = None
        if self.repo is not None:
            rows = {row.preset: row for row in await self.repo.list_preset_states(self.serial)}
            meta = await self.repo.list_channel_meta(self.serial)
            adoption = await self.repo.get_setting(self._adoption_key())
        for preset in range(self.profile.n_presets):
            doc, applied, unknown, revision, baseline = default_config_doc(self.profile), {}, [], 0, None
            pending: list[str] = []
            wrap: list[int] = []
            row = rows.get(preset)
            if row is not None:
                revision = row.revision
                try:
                    stored = ConfigDoc.model_validate(row.desired)
                    problems = _shape_problems(stored, self.profile)
                except ValidationError as exc:
                    stored, problems = None, [str(exc)]
                if stored is None or problems:
                    self.log_event(
                        "error",
                        f"stored state of preset {preset + 1} is unusable; starting from defaults (all unknown)",
                        kind="state",
                        problems=problems[:5],
                    )
                else:
                    doc = stored
                    values = device_values(doc, self.profile)
                    stored_applied = dict(row.applied or {})
                    stored_pending = stored_applied.pop(PENDING_KEY, [])
                    stored_wrap = stored_applied.pop(WRAP_KEY, [])
                    applied = {p: v for p, v in stored_applied.items() if p in values}
                    unknown = row.unknown_paths
                    if isinstance(stored_pending, list):
                        pending = [p for p in stored_pending if p in values and p not in applied]
                    if isinstance(stored_wrap, list):
                        wrap = [n for n in stored_wrap if isinstance(n, int) and not isinstance(n, bool)]
                    baseline = await self._load_baseline(preset, revision) or doc
            self.writer.attach(preset, doc, applied=applied, unknown=unknown, wrap=wrap)
            if wrap:
                self.log_event(
                    "warning",
                    f"preset {preset + 1}: a bulk apply did not complete before the restart; "
                    f"output(s) {', '.join(str(n + 1) for n in wrap)} stay muted until it does",
                    kind="sync",
                    preset=preset,
                )
            if pending:
                self.writer.notify(preset, pending)
            self.revisions[preset] = revision
            self._baseline[preset] = baseline or doc
        self.channel_meta = {(m.kind, m.idx): m for m in meta}
        self.adoption = adoption if isinstance(adoption, dict) else None

    async def _load_baseline(self, preset: int, revision: int) -> ConfigDoc | None:
        if self.repo is None or revision <= 0:
            return None
        row = await self.repo.get_revision(self.serial, preset, revision)
        if row is None:
            return None
        try:
            return ConfigDoc.model_validate(row.desired)
        except ValidationError:
            return None

    def _adoption_key(self) -> str:
        return f"adopted.{self.serial}"

    # --- preset configuration -------------------------------------------------------------------------------------

    def _check_preset(self, preset: int) -> None:
        if not isinstance(preset, int) or not 0 <= preset < self.profile.n_presets:
            raise UnknownPreset(f"preset {preset} outside 0..{self.profile.n_presets - 1}")

    def _require_loaded(self) -> None:
        if not self.loaded:
            raise StateUnavailable(self.load_error or "preset state is not loaded yet")

    async def _ready(self, preset: int) -> PresetState:
        self._check_preset(preset)
        await self.ensure_loaded()
        return self.writer.state(preset)

    async def get_desired(self, preset: int) -> ConfigDoc:
        return (await self._ready(preset)).desired

    def desired(self, preset: int) -> ConfigDoc:
        """Synchronous `get_desired` for templates; raises `StateUnavailable` before the state is loaded."""
        self._check_preset(preset)
        self._require_loaded()
        return self.writer.state(preset).desired

    def sync_status(self, preset: int) -> dict[str, str]:
        """Per-path sync status (synced / pending / failed / unknown) of every device path of the preset."""
        self._check_preset(preset)
        self._require_loaded()
        return {path: status.value for path, status in self.writer.status(preset).items()}

    def sync_summary(self, preset: int) -> dict[str, Any]:
        """Counts per status, staged flag, last error and failed paths, plus revision bookkeeping."""
        self._check_preset(preset)
        self._require_loaded()
        master = self.master
        return {
            **self.writer.summary(preset),
            "serial": self.serial,
            "active": master is not None and master.preset == preset,
            "revision": self.revisions.get(preset, 0),
            "uncommitted": preset in self._uncommitted,
            "writes_sent": self.writer.writes_sent,
        }

    def preset_view(self, preset: int, *, include_sync: bool = True) -> dict[str, Any]:
        """Everything the API's ``GET …/presets/{p}`` returns."""
        view = {
            **self.sync_summary(preset),
            "desired": self.desired(preset).model_dump(mode="json"),
            "adopted": self.adoption is not None,
        }
        if include_sync:
            view["sync"] = self.sync_status(preset)
        return view

    def channel_ceiling(self, kind: str, index: int) -> float:
        """Highest gain allowed on a channel: the profile maximum, lowered by the channel's ``max_gain_db``."""
        meta = self.channel_meta.get((kind, index))
        ceiling = self.profile.gain_max_db
        if meta is not None and meta.max_gain_db is not None:
            ceiling = min(ceiling, meta.max_gain_db)
        return ceiling

    async def set_channel_meta(
        self,
        kind: str,
        index: int,
        *,
        name: str | None = None,
        color: str | None = None,
        max_gain_db: float | None = None,
    ) -> ChannelMeta:
        """Name/colour/gain ceiling of one channel. The ceiling must lie inside the profile's gain range."""
        await self.ensure_loaded()
        count = {"input": self.profile.n_inputs, "output": self.profile.n_outputs}.get(kind)
        if count is None or not 0 <= index < count:
            raise DesiredValidationError(f"no {kind} channel {index}")
        p = self.profile
        if max_gain_db is not None and not (
            math.isfinite(max_gain_db) and p.gain_min_db <= max_gain_db <= p.gain_max_db
        ):
            raise DesiredValidationError(f"gain ceiling {max_gain_db} dB outside {p.gain_min_db:g}..{p.gain_max_db:g}")
        old = self.channel_meta.get((kind, index))
        old_ceiling = old.max_gain_db if old is not None else None
        if max_gain_db is not None and (old_ceiling is None or max_gain_db < old_ceiling):
            above = [
                f"preset {preset + 1}: {path} is {value:g} dB"
                for preset, state in sorted(self.writer.presets.items())
                for path, value in state.values(self.profile).items()
                if self._ceiling_channel(path) == (kind, index)
                and isinstance(value, int | float)
                and value > max_gain_db
            ]
            if above:
                raise DesiredValidationError(
                    f"gain ceiling {max_gain_db:g} dB is below gains already set on {kind} {index + 1}; "
                    "lower them first",
                    above,
                )
        if self.repo is not None:
            try:
                await self.repo.set_channel_meta(
                    self.serial, kind, index, name=name, color=color, max_gain_db=max_gain_db
                )
            except ValueError as exc:
                raise DesiredValidationError(str(exc)) from None
            stored = await self.repo.get_channel_meta(self.serial, kind, index)
            assert stored is not None
            meta = stored
        else:
            meta = ChannelMeta(self.serial, kind, index, (name or "").strip() or None, color, max_gain_db)
        self.channel_meta[(kind, index)] = meta
        return meta

    # --- validation -----------------------------------------------------------------------------------------------

    def _normalize(self, old: ConfigDoc, new: ConfigDoc) -> ConfigDoc:
        """Round changed delays to whole samples, so the desired doc says exactly what the device will hold."""
        for n, (a, b) in enumerate(zip(old.outputs, new.outputs, strict=False)):
            if a.delay_ms != b.delay_ms and math.isfinite(b.delay_ms) and b.delay_ms >= 0:
                rounded = self.profile.delay_ms_from_samples(self.profile.delay_samples(b.delay_ms))
                if rounded != b.delay_ms:
                    new = set_path(new, f"outputs.{n}.delay_ms", rounded)
        return new

    @staticmethod
    def _ceiling_channel(path: str) -> tuple[str, int] | None:
        """The channel whose gain ceiling applies to ``path`` (a route's gain counts toward the output it feeds)."""
        match path.split("."):
            case ["outputs", n, "gain_db"] if n.isdigit():
                return "output", int(n)
            case ["inputs", n, "gain_db"] if n.isdigit():
                return "input", int(n)
            case ["inputs", n, "routing", m, "gain_db"] if n.isdigit() and m.isdigit():
                return "output", int(m)
        return None

    def _ceiling_for(self, path: str) -> float | None:
        channel = self._ceiling_channel(path)
        return None if channel is None else self.channel_ceiling(*channel)

    def _ceiling_problem(self, path: str, value: Any) -> str | None:
        """Also the writer's last-line check, so no write path can exceed a ceiling."""
        ceiling = self._ceiling_for(path)
        if ceiling is not None and isinstance(value, int | float) and value > ceiling:
            return f"{value:g} dB is above this channel's ceiling of {ceiling:g} dB"
        return None

    def _ceiling_problems(self, doc: ConfigDoc) -> list[str]:
        return [
            f"{path}: {reason}"
            for path, value in device_values(doc, self.profile).items()
            if (reason := self._ceiling_problem(path, value))
        ]

    def _validate(
        self,
        old: ConfigDoc,
        new: ConfigDoc,
        *,
        confirm: bool,
        all_paths: bool = False,
        old_values: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Raise `DesiredValidationError` or `ConfirmationRequired`; returns the changed device paths."""
        problems = validate_doc(new, self.profile)
        flagged = [p.split(":", 1)[0] for p in problems]
        if old_values is None:
            old_values = device_values(old, self.profile)
        new_values = device_values(new, self.profile)
        changed = [
            p for p, v in new_values.items() if p not in old_values or not same_value(p, old_values[p], v, self.profile)
        ]
        for path in new_values if all_paths else changed:
            value = new_values[path]
            if any(f == path or f.startswith(f"{path}.") for f in flagged):
                continue
            if reason := value_problem(path, value, self.profile):
                problems.append(f"{path}: {reason}")
                continue
            if reason := self._ceiling_problem(path, value):
                problems.append(f"{path}: {reason}")
            if path.endswith(".compressor.threshold_db") and isinstance(value, int | float):
                low, high = COMPRESSOR_THRESHOLD_RANGE
                if not low <= value <= high:
                    problems.append(f"{path}: threshold {value:g} dB outside {low:g}..{high:g}")
        if problems:
            raise DesiredValidationError("invalid settings", problems)
        step = self.settings.gain_step_confirm_db
        if not confirm and step > 0:
            raises = [
                (p, new_values[p] - old_values[p])
                for p in changed
                if p.endswith("gain_db") and isinstance(old_values.get(p), int | float)
                if new_values[p] - old_values[p] > step
            ]
            if raises:
                text = ", ".join(f"{p} by {delta:g} dB" for p, delta in raises)
                raise ConfirmationRequired(f"raising {text} needs confirmation (more than {step:g} dB at once)")
        return changed

    # --- edits ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _check_source(source: str) -> None:
        if source not in REVISION_SOURCES:
            raise ValueError(f"unknown edit source {source!r}")

    async def edit(
        self,
        preset: int,
        patch: Mapping[str, Any],
        *,
        confirm: bool = False,
        expected_revision: int | None = None,
        source: str = "api",
        commit: bool = False,
        summary: str | None = None,
    ) -> EditResult:
        """Apply a merge patch (see `merge_patch`) to a preset's desired state and queue the device writes.

        Raises `DesiredValidationError` (422), `ConfirmationRequired` (a gain raise above the big-step threshold
        without ``confirm``), `RevisionConflict` (``expected_revision`` is stale) or `UnknownPreset`.
        """
        self._check_source(source)
        await self._ready(preset)
        if not isinstance(patch, Mapping):
            raise DesiredValidationError("a patch must be a JSON object")
        async with self._edit_lock:
            await self._check_revision(preset, expected_revision)
            current = self.writer.state(preset).desired
            if patch.get("schema_version", current.schema_version) != current.schema_version:
                raise DesiredValidationError("invalid patch", ["schema_version: can't be changed"])
            new = parse_doc(merge_patch(current.model_dump(mode="json"), patch))
            change = self._stage(preset, new, confirm=confirm, source=source, summary=summary)
        return await self._finish(preset, change, commit=commit)

    async def patch_desired(
        self,
        preset: int,
        patch: Mapping[str, Any],
        *,
        confirm: bool = False,
        expected_revision: int | None = None,
        source: str = "api",
        commit: bool = False,
    ) -> ConfigDoc:
        """`edit`, returning just the new desired document."""
        result = await self.edit(
            preset, patch, confirm=confirm, expected_revision=expected_revision, source=source, commit=commit
        )
        return result.doc

    async def edit_channel(
        self, preset: int, kind: str, index: int, patch: Mapping[str, Any], **kwargs: Any
    ) -> EditResult:
        """Channel-scoped `edit`: ``patch`` applies to ``inputs[index]`` or ``outputs[index]``."""
        count = {"input": self.profile.n_inputs, "output": self.profile.n_outputs}.get(kind)
        if count is None:
            raise DesiredValidationError(f"unknown channel kind {kind!r}")
        if not 0 <= index < count:
            raise DesiredValidationError(f"{kind} {index} outside 0..{count - 1}")
        if not isinstance(patch, Mapping):
            raise DesiredValidationError("a patch must be a JSON object")
        return await self.edit(preset, {f"{kind}s": {str(index): dict(patch)}}, **kwargs)

    async def set_value(self, preset: int, path: str, value: Any, **kwargs: Any) -> EditResult:
        """Set one dotted path (``outputs.0.gain_db``), with the same validation as `edit`."""
        patch: Any = value
        for part in reversed(path.split(".")):
            if not part:
                raise DesiredValidationError(f"bad path {path!r}")
            patch = {part: patch}
        return await self.edit(preset, patch, **kwargs)

    async def replace_desired(
        self,
        preset: int,
        doc: ConfigDoc,
        *,
        confirm: bool = False,
        expected_revision: int | None = None,
        source: str = "api",
        summary: str | None = None,
        commit: bool = True,
        bulk: bool = True,
        timeout: float | None = BULK_TIMEOUT_S,
    ) -> EditResult:
        """Replace the whole desired document (profile load, restore). With ``bulk`` it is applied as a bulk apply
        (mute-wrapped when ``MUTE_WRAP_BULK_APPLY``) and awaited up to ``timeout``."""
        self._check_source(source)
        await self._ready(preset)
        async with self._edit_lock:
            await self._check_revision(preset, expected_revision)
            new = parse_doc(doc.model_dump(mode="json"))
            change = self._stage(preset, new, confirm=confirm, source=source, summary=summary, check_all=True)
        return await self._finish(preset, change, commit=commit, bulk=bulk, timeout=timeout)

    def _stage(
        self,
        preset: int,
        new: ConfigDoc,
        *,
        confirm: bool,
        source: str,
        summary: str | None,
        check_all: bool = False,
    ) -> tuple[ConfigDoc, list[str], list[str]]:
        """Validate and hand the new doc to the writer. Synchronous on purpose: no other edit can interleave.
        ``check_all`` validates every path, not just the changed ones (whole-document replacements)."""
        state = self.writer.state(preset)
        old = state.desired
        new = self._normalize(old, new)
        self._validate(old, new, confirm=confirm, all_paths=check_all, old_values=state.values(self.profile))
        old_flat, new_flat = self._flat(preset, old), flatten(new)
        paths = [p for p, v in new_flat.items() if old_flat.get(p, _MISSING) != v]
        if not paths:
            return old, [], []
        self._flat_cache[preset] = (new, new_flat)
        device_paths = self.writer.set_desired(preset, new)
        self._schedule_persist(preset)
        self._mark_uncommitted(preset, source, summary)
        self._publish_desired(preset, new, paths, source, flat=new_flat)
        return new, paths, device_paths

    def _flat(self, preset: int, doc: ConfigDoc) -> dict[str, Any]:
        """`flatten(doc)`, cached for the preset's latest desired doc (edits flatten it again otherwise)."""
        cached = self._flat_cache.get(preset)
        if cached is not None and cached[0] is doc:
            return cached[1]
        flat = flatten(doc)
        self._flat_cache[preset] = (doc, flat)
        return flat

    async def _finish(
        self,
        preset: int,
        change: tuple[ConfigDoc, list[str], list[str]],
        *,
        commit: bool,
        bulk: bool = False,
        timeout: float | None = BULK_TIMEOUT_S,
    ) -> EditResult:
        doc, paths, device_paths = change
        flush = None
        if bulk and device_paths:
            # Called right after `_stage` with no await in between, so the mute wrap is requested before the
            # writer's next cycle can pick the changes up.
            flush = await self.writer.flush(preset, mute_wrap=self.settings.mute_wrap_bulk_apply, timeout=timeout)
        revision = await self.commit(preset) if commit else self.revisions.get(preset, 0)
        return EditResult(preset, revision, doc, paths, device_paths, flush)

    def _publish_desired(
        self,
        preset: int,
        doc: ConfigDoc,
        paths: Sequence[str],
        source: str,
        *,
        flat: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        values = flatten(doc) if flat is None else flat
        self.bus.publish(
            DESIRED,
            {
                "serial": self.serial,
                "preset": preset,
                "event": "edit",
                "source": source,
                "revision": self.revisions.get(preset, 0),
                "paths": list(paths),
                "values": {p: values.get(p) for p in paths},
                **extra,
            },
            serial=self.serial,
        )

    # --- revisions ------------------------------------------------------------------------------------------------

    def _mark_uncommitted(self, preset: int, source: str, summary: str | None) -> None:
        pending = self._uncommitted.get(preset)
        if pending is None:
            self._uncommitted[preset] = _Uncommitted(source, summary)
        else:
            pending.source = source
            pending.summary = summary or pending.summary
        self._arm_commit_timer(preset)

    def _arm_commit_timer(self, preset: int) -> None:
        if handle := self._commit_timers.pop(preset, None):
            handle.cancel()
        if self._stopped:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._commit_timers[preset] = loop.call_later(
            self.commit_idle_s, lambda: self._spawn(self._commit_quietly(preset), "idle-commit")
        )

    async def _commit_quietly(self, preset: int) -> None:
        with contextlib.suppress(StateUnavailable):
            await self.commit(preset)

    async def _check_revision(self, preset: int, expected: int | None) -> None:
        if expected is None:
            return
        if preset in self._uncommitted:
            await self.commit(preset)  # someone else's pending edits count as a revision
        actual = self.revisions.get(preset, 0)
        if expected != actual:
            raise RevisionConflict(expected, actual)

    async def commit(
        self, preset: int, *, summary: str | None = None, source: str | None = None, force: bool = False
    ) -> int:
        """Commit boundary: write a revision row if the desired doc changed since the last one. Returns the
        revision. ``force`` writes even without a change relative to the last commit (the repo still skips a doc
        identical to the latest history row)."""
        self._check_preset(preset)
        await self.ensure_loaded()
        if handle := self._commit_timers.pop(preset, None):
            handle.cancel()
        async with self._commit_lock:
            pending = self._uncommitted.pop(preset, None)
            doc = self.writer.state(preset).desired
            baseline = self._baseline.get(preset)
            paths = diff_paths(baseline, doc) if baseline is not None else []
            if not paths and not force:
                return self.revisions.get(preset, 0)
            text = summary or (pending.summary if pending else None) or describe_change(baseline, doc, paths)
            origin = source or (pending.source if pending else "api")
            revision = self.revisions.get(preset, 0)
            if self.repo is not None:
                applied, unknown = self._stored_shadow(preset)
                try:
                    revision = await self.repo.commit_revision(
                        self.serial, preset, doc, summary=text, source=origin, applied=applied, unknown_paths=unknown
                    )
                except Exception as exc:
                    self._uncommitted.setdefault(preset, pending or _Uncommitted(origin, summary))
                    self._arm_commit_timer(preset)
                    self.log_event("error", f"saving a revision of preset {preset + 1} failed: {exc}", persist=False)
                    raise StateUnavailable(f"saving the revision failed: {exc}") from exc
            elif paths or force:
                revision += 1
            self.revisions[preset] = revision
            self._baseline[preset] = doc
        self.bus.publish(
            DESIRED,
            {
                "serial": self.serial,
                "preset": preset,
                "event": "commit",
                "source": origin,
                "revision": revision,
                "summary": text,
                "paths": [],
                "values": {},
            },
            serial=self.serial,
        )
        return revision

    async def history(self, preset: int, *, limit: int = 50, before: int | None = None) -> list[RevisionInfo]:
        """Revision rows, newest first."""
        await self._ready(preset)
        if self.repo is None:
            return []
        return await self.repo.list_revisions(self.serial, preset, limit=limit, before=before)

    # --- sync -----------------------------------------------------------------------------------------------------

    async def push_all(
        self, preset: int, *, mute_wrap: bool | None = None, timeout: float | None = BULK_TIMEOUT_S
    ) -> FlushResult:
        """Write every device path of the preset, whatever the shadow says (mute-wrapped by default). An inactive
        preset is staged and pushed when it becomes active."""
        state = await self._ready(preset)
        if problems := self._ceiling_problems(state.desired):
            raise DesiredValidationError("gains above a channel's ceiling; lower them before pushing", problems)
        wrap = self.settings.mute_wrap_bulk_apply if mute_wrap is None else mute_wrap
        before = self.writer.writes_sent
        self.log_event("info", f"push-all preset {preset + 1}{' (mute-wrapped)' if wrap else ''}", kind="push")
        result = await self.writer.push_all(preset, mute_wrap=wrap, timeout=timeout)
        self._log_flush(f"push-all preset {preset + 1}", result, self.writer.writes_sent - before, kind="push")
        return result

    def _log_flush(self, what: str, result: FlushResult, writes: int, *, kind: str) -> None:
        if result.ok:
            self.log_event("info", f"{what}: done ({writes} write(s))", kind=kind, writes=writes)
        elif result.staged:
            self.log_event("info", f"{what}: staged until the preset is active", kind=kind, writes=writes)
        else:
            self.log_event(
                "warning",
                f"{what}: incomplete ({len(result.pending)} pending, {len(result.failed)} failed): {result.error}",
                kind=kind,
                writes=writes,
                failed=sorted(result.failed),
            )

    async def activate_preset(self, preset: int, *, flush: bool = True, timeout: float | None = None) -> MasterStatus:
        """Switch preset ("switch & apply now"), then wait for its staged edits to be written (``flush``)."""
        await self._ready(preset)
        if self._persister is not None:
            await self._persister.flush([(self.serial, preset)])
        try:
            current = await self.active_preset()
        except DaemonError:
            current = None
        master = await self.set_preset(preset) if current != preset else (self.master or MasterStatus(preset=preset))
        self.writer.kick(preset)
        if flush:
            limit = timeout if timeout is not None else self.settings.preset_switch_timeout_s
            await self.writer.wait_idle(preset, timeout=limit)
        return master

    # --- adopt / import / export ----------------------------------------------------------------------------------

    def parse_cli_script(self, text: str) -> ParsedScript:
        return parse_script(text, self.profile)

    async def adopt(
        self,
        mode: AdoptMode | str,
        *,
        preset: int | None = None,
        script: str | None = None,
        doc: ConfigDoc | None = None,
        confirm: bool = False,
        trust_all: bool = False,
        timeout: float | None = BULK_TIMEOUT_S,
    ) -> AdoptResult:
        """Adopt the device (docs 03 §8). The starting document is a CLI script applied to defaults, an explicit
        doc, or the defaults. Then:

        - ``trust``: the shadow says the device holds it, nothing is written. For a script only the paths the script
          sets are trusted (everything else stays unknown) unless ``trust_all``.
        - ``push``: write all of it, mute-wrapped. Destructive, so it needs ``confirm``.
        - ``unknown``: nothing is known and nothing is written.

        ``preset`` defaults to the active preset.
        """
        try:
            mode = AdoptMode(mode)
        except ValueError:
            raise DesiredValidationError(f"unknown adopt mode {mode!r}; expected trust, push or unknown") from None
        if script is not None and doc is not None:
            raise DesiredValidationError("give either a script or a document, not both")
        await self.ensure_loaded()
        if preset is None:
            preset = await self.active_preset()
        self._check_preset(preset)
        parsed: ParsedScript | None = None
        if script is not None:
            parsed = self.parse_cli_script(script)
            if parsed.errors:
                raise DesiredValidationError(
                    "the script has errors", [f"line {e.line}: {e.message} ({e.text})" for e in parsed.errors]
                )
            try:
                new = parsed.apply(default_config_doc(self.profile))
            except (PathError, ValidationError) as exc:
                raise DesiredValidationError(f"the script can't be applied: {exc}") from None
        elif doc is not None:
            new = parse_doc(doc.model_dump(mode="json"))
        elif mode == AdoptMode.PUSH:
            new = self._defaults_for_push()
        else:
            new = default_config_doc(self.profile)
        if mode == AdoptMode.PUSH and not confirm:
            raise ConfirmationRequired(f"pushing overwrites every setting of preset {preset + 1}; repeat with confirm")

        async with self._edit_lock:
            old = self.writer.state(preset).desired
            new = self._normalize(old, new)
            problems = validate_doc(new, self.profile)
            if problems:
                raise DesiredValidationError("invalid settings", problems)
            if mode == AdoptMode.PUSH:
                self._validate(old, new, confirm=True, all_paths=True)  # ceilings, compressor ranges
            elif ceilings := self._ceiling_problems(new):
                raise DesiredValidationError(
                    "gains above a channel's ceiling (raise the ceiling in Settings if the device really has them)",
                    ceilings,
                )
            values = device_values(new, self.profile)
            if mode == AdoptMode.TRUST:
                trusted = values if parsed is None or trust_all else parsed.device_paths
                known = {p: values[p] for p in trusted if p in values}
            else:
                known = {}
            before = self.writer.writes_sent
            self.writer.attach(preset, new, applied=dict(known))
            self._schedule_persist(preset)
            self._mark_uncommitted(preset, "adopt", f"adopt: {mode}{' from a CLI script' if parsed else ''}")
            self._publish_desired(preset, new, diff_paths(old, new), "adopt", adopt=str(mode))
        flush = None
        if mode == AdoptMode.PUSH:
            flush = await self.writer.push_all(preset, mute_wrap=self.settings.mute_wrap_bulk_apply, timeout=timeout)
        revision = await self.commit(preset, force=True)
        self.adoption = {"mode": str(mode), "preset": preset, "script": parsed is not None, "at": utcnow_iso()}
        if self.repo is not None:
            await self.repo.set_setting(self._adoption_key(), self.adoption)
        writes = self.writer.writes_sent - before
        n_known = sum(1 for p in values if p in self.writer.state(preset).applied)
        self.log_event(
            "info",
            f"adopted preset {preset + 1} ({mode}): {n_known} known, {len(values) - n_known} unknown, "
            f"{writes} write(s)",
            kind="adopt",
            preset=preset,
            mode=str(mode),
            writes=writes,
        )
        if flush is not None:
            self._log_flush(f"adopt push preset {preset + 1}", flush, writes, kind="adopt")
        return AdoptResult(
            preset=preset,
            mode=mode,
            revision=revision,
            known=n_known,
            unknown=len(values) - n_known,
            writes=writes,
            flush=flush,
            warnings=list(parsed.warnings) if parsed else [],
        )

    def _defaults_for_push(self) -> ConfigDoc:
        """docs 03 §8 option 2: the defaults with every unused output muted. An output counts as used once it has a
        name in the channel settings, so on a fresh install every output starts muted and is un-muted by hand."""
        doc = default_config_doc(self.profile)
        for n in range(self.profile.n_outputs):
            meta = self.channel_meta.get(("output", n))
            if meta is None or not meta.name:
                doc = set_path(doc, f"outputs.{n}.mute", True)
        return doc

    async def import_cli_script(
        self,
        preset: int,
        text: str,
        *,
        base: str = "current",
        confirm: bool = False,
        expected_revision: int | None = None,
        commit: bool = True,
        timeout: float | None = BULK_TIMEOUT_S,
    ) -> ImportResult:
        """Apply a ``minidsp -f`` script to a preset's desired state (on top of the current doc or the defaults) as
        a mute-wrapped bulk apply. Scripts with errors are refused."""
        if base not in ("current", "default"):
            raise DesiredValidationError(f"unknown base {base!r}; expected current or default")
        await self._ready(preset)
        parsed = self.parse_cli_script(text)
        if parsed.errors:
            raise DesiredValidationError(
                "the script has errors", [f"line {e.line}: {e.message} ({e.text})" for e in parsed.errors]
            )
        if not parsed.ops:
            raise DesiredValidationError("the script contains no preset settings")
        async with self._edit_lock:
            await self._check_revision(preset, expected_revision)
            start = self.writer.state(preset).desired if base == "current" else default_config_doc(self.profile)
            try:
                new = parsed.apply(start)
            except (PathError, ValidationError) as exc:
                raise DesiredValidationError(f"the script can't be applied: {exc}") from None
            summary = f"import CLI script ({len(parsed.ops)} setting(s))"
            change = self._stage(preset, new, confirm=confirm, source="import", summary=summary)
        result = await self._finish(preset, change, commit=commit, bulk=True, timeout=timeout)
        self.log_event(
            "info",
            f"imported a CLI script into preset {preset + 1}: {len(result.device_paths)} device setting(s) changed",
            kind="import",
            preset=preset,
        )
        return ImportResult(result, parsed)

    async def export_cli_script(self, preset: int, *, mute_wrap: bool = True) -> str:
        doc = await self.get_desired(preset)
        return export_script(doc, self.profile, mute_wrap=mute_wrap)


class SessionManager:
    """Owns one `DeviceSession` per supported device serial. Survives the daemon being down: it keeps retrying."""

    def __init__(self, client: DaemonClient, bus: Bus, settings: Settings, *, repo: Repo | None = None) -> None:
        self.client = client
        self.bus = bus
        self.settings = settings
        self.repo = repo
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
                    repo=self.repo,
                )
                session.on_lost = self.request_rescan
                self.sessions[serial] = session
                session.set_state(SessionState.CONNECTED)
                session.log_event("info", f"found {device.product_name} at daemon index {index}")
                await session.record_device()
                with contextlib.suppress(StateUnavailable):  # retried on first use
                    await session.ensure_loaded()
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


__all__ = [
    "AdoptMode",
    "AdoptResult",
    "ConfirmationRequired",
    "DesiredValidationError",
    "DeviceSession",
    "EditResult",
    "ImportResult",
    "MasterValidationError",
    "PresetSwitchInProgress",
    "PresetSwitchTimeout",
    "RevisionConflict",
    "SessionManager",
    "SessionState",
    "StateUnavailable",
    "UnknownPreset",
    "flush_json",
    "merge_patch",
    "parse_doc",
]
