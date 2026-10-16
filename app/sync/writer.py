"""Coalescing device writer: one per device, single in-flight write (docs 03 §3-4).

Edits only change the desired document and wake the writer. After a short gather window the writer snapshots the
desired state, diffs it against the applied shadow and sends the minimal payloads, so intermediate slider values are
never sent (last write wins per path).

Rules:

- **Preset guard.** Before every call the active preset is re-checked; on a mismatch the flush is abandoned and the
  changes stay staged until `kick` (the session calls it when the preset becomes active).
- **Failures.** A failed call may have been partly applied (minidspd stops at the first error, no rollback), so every
  path in it becomes unknown and the whole payload is retried; all writes are absolute, so retrying is idempotent.
  Retries back off 0.5 s -> 8 s; a path that failed ``max_attempts`` times is marked failed and left alone until it
  is edited again or `kick` re-arms it. A rejected payload backs off only its own paths, so other edits keep flowing;
  an unreachable daemon or device backs off the whole writer.
- **Exclusive calls.** The preset guard and the call it clears run under one lock; `paused` takes it, so a preset
  switch can wait for an in-flight call and keep new ones out until the device reports the new preset.
- **Mute wrap.** A bulk apply can hold the affected outputs muted, apply everything, then restore their desired mute.
  If the apply can't complete the outputs stay muted.

Per-path sync status (synced / pending / failed / unknown) is published on the bus topic ``sync``.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.bus import SYNC, Bus
from app.daemon.client import DaemonRejected, DaemonUnavailable, DeviceNotFound
from app.model.config_doc import ConfigDoc
from app.model.device_profile import DeviceProfile
from app.model.payload import INPUTS, OUTPUTS, Payload, build_plan, device_values, mute_payload, same_value

log = logging.getLogger(__name__)

MAX_ROUNDS = 4  # main call(s), then deferred un-bypasses / post-bypass coefficients; 2 are normally enough


class SyncStatus(StrEnum):
    SYNCED = "synced"
    PENDING = "pending"
    FAILED = "failed"
    UNKNOWN = "unknown"


type PostConfig = Callable[[dict[str, Any]], Awaitable[Any]]
type ActivePreset = Callable[[], Awaitable[int]]
type FirTaps = Callable[[str], Awaitable[Sequence[float] | None]]
type OnApplied = Callable[[int, "PresetState"], Any]
type OnLog = Callable[..., Any]


@dataclass
class PresetState:
    """Desired document, applied shadow and sync bookkeeping for one preset."""

    desired: ConfigDoc
    applied: dict[str, Any] = field(default_factory=dict)  # last confirmed value per path; missing = unknown
    dirty: set[str] = field(default_factory=set)  # write even though the shadow doesn't know the path
    force: set[str] = field(default_factory=set)  # write even though the shadow says it's already there
    failed: dict[str, str] = field(default_factory=dict)  # given up: path -> reason
    attempts: dict[str, int] = field(default_factory=dict)
    wrap: set[int] = field(default_factory=set)  # outputs held muted by a bulk apply
    retry_at: dict[str, float] = field(default_factory=dict)  # rejected path -> monotonic time of its next retry
    wrap_requested: bool = False
    staged: bool = False
    failures: int = 0  # consecutive rounds that failed for the daemon/device being gone, drives the global backoff
    last_error: str | None = None
    published: dict[str, SyncStatus] = field(default_factory=dict)
    published_key: tuple | None = None
    _values_for: ConfigDoc | None = field(default=None, repr=False)
    _values: dict[str, Any] = field(default_factory=dict, repr=False)

    def values(self, profile: DeviceProfile) -> dict[str, Any]:
        if self._values_for is not self.desired:
            self._values = device_values(self.desired, profile)
            self._values_for = self.desired
        return self._values

    def unknown_paths(self, profile: DeviceProfile) -> list[str]:
        return [p for p in self.values(profile) if p not in self.applied]


@dataclass(frozen=True)
class FlushResult:
    preset: int
    staged: bool = False
    pending: tuple[str, ...] = ()
    failed: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not (self.staged or self.pending or self.failed)


_MESSAGE_RE = re.compile(r'message: "((?:[^"\\]|\\.)*)"')
_CAUSED_RE = re.compile(r"Caused by:(?:\s|\\n)*(.*?)\)\s*\}", re.S)


def error_text(exc: BaseException) -> str:
    """Readable text for a failed write. minidspd's 500 bodies are often not JSON (a serialization bug), so the
    message is dug out of the Rust debug output."""
    if isinstance(exc, DaemonRejected):
        if exc.message:
            return exc.message
        body = exc.body or ""
        caused = _CAUSED_RE.search(body)
        message = _MESSAGE_RE.search(body)
        if caused and (message is None or message.group(1) == "Internal error"):
            return caused.group(1).replace("\\n", " ").strip()
        if message:
            return message.group(1)
        return body.strip()[:300] or f"HTTP {exc.status_code}"
    return str(exc) or type(exc).__name__


class DeviceWriter:
    def __init__(
        self,
        serial: int,
        profile: DeviceProfile,
        *,
        post_config: PostConfig,
        active_preset: ActivePreset,
        bus: Bus | None = None,
        fir_taps: FirTaps | None = None,
        on_applied: OnApplied | None = None,
        on_log: OnLog | None = None,
        coalesce_ms: float = 50,
        backoff_min_s: float = 0.5,
        backoff_max_s: float = 8.0,
        max_attempts: int = 5,
        split_channels: bool = True,
        value_check: Callable[[str, Any], str | None] | None = None,
    ) -> None:
        self.serial = serial
        self.profile = profile
        self._post = post_config
        self._active_preset = active_preset
        self.bus = bus
        self._fir_taps = fir_taps
        self._on_applied = on_applied
        self._on_log = on_log
        self.coalesce_s = coalesce_ms / 1000.0
        self.backoff_min_s = backoff_min_s
        self.backoff_max_s = backoff_max_s
        self.max_attempts = max_attempts
        self.split_channels = split_channels
        self._value_check = value_check
        self.presets: dict[int, PresetState] = {}
        self.writes_sent = 0
        self._last_active: int | None = None
        self._wake = asyncio.Event()
        self._kicked = asyncio.Event()
        self._io_lock = asyncio.Lock()
        self._waiters: dict[int, list[asyncio.Future[FlushResult]]] = {}
        self._task: asyncio.Task | None = None

    # --- lifecycle ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"writer-{self.serial}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for preset in list(self._waiters):
            self._resolve(preset, force=True)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @contextlib.asynccontextmanager
    async def paused(self) -> AsyncIterator[None]:
        """Wait for the call in flight (if any) and send nothing until the block exits."""
        async with self._io_lock:
            yield

    # --- state ----------------------------------------------------------------------------------------------------

    def attach(
        self,
        preset: int,
        desired: ConfigDoc,
        *,
        applied: dict[str, Any] | None = None,
        unknown: Iterable[str] = (),
        wrap: Iterable[int] = (),
    ) -> PresetState:
        """Register a preset's desired document and shadow (``applied`` minus ``unknown``). ``wrap`` restores the
        outputs an unfinished bulk apply holds muted."""
        shadow = dict(applied or {})
        for path in unknown:
            shadow.pop(path, None)
        held = {n for n in wrap if isinstance(n, int) and 0 <= n < self.profile.n_outputs}
        state = PresetState(desired=desired, applied=shadow, wrap=held)
        self.presets[preset] = state
        self._publish(preset)
        return state

    def state(self, preset: int) -> PresetState:
        try:
            return self.presets[preset]
        except KeyError:
            raise KeyError(f"preset {preset} is not attached to the writer") from None

    def set_desired(self, preset: int, desired: ConfigDoc) -> list[str]:
        """Replace the desired document; the changed device paths are queued. Returns them."""
        state = self.state(preset)
        old = state.values(self.profile)
        state.desired = desired
        new = state.values(self.profile)
        changed = [p for p, v in new.items() if p not in old or not same_value(p, old[p], v, self.profile)]
        self.notify(preset, changed)
        return changed

    def notify(self, preset: int, paths: Iterable[str] = ()) -> None:
        """Queue ``paths`` (written even if the shadow doesn't know them) and wake the writer. Paths whose desired
        value differs from a known shadow value are written anyway, so a bare ``notify(preset)`` also works."""
        state = self.state(preset)
        for path in paths:
            state.dirty.add(path)
            state.failed.pop(path, None)
            state.attempts.pop(path, None)
            state.retry_at.pop(path, None)
        self._publish(preset)
        self._wake.set()

    def trust(self, preset: int) -> None:
        """Adopt: declare the device holds exactly the desired state. Nothing is written."""
        state = self.state(preset)
        state.applied = dict(state.values(self.profile))
        state.dirty.clear()
        state.force.clear()
        state.failed.clear()
        state.attempts.clear()
        state.retry_at.clear()
        state.wrap.clear()
        state.wrap_requested = False
        self._changed(preset)
        self._resolve(preset)

    def invalidate(self, preset: int, paths: Iterable[str] | None = None) -> None:
        """Forget what the device holds (e.g. after a possible power cycle). Nothing is written."""
        state = self.state(preset)
        if paths is None:
            state.applied.clear()
        else:
            for path in paths:
                state.applied.pop(path, None)
        self._changed(preset)

    def kick(self, preset: int | None = None) -> None:
        """Retry now: re-arm failed paths, release staged changes, cut any backoff short."""
        for p in [preset] if preset is not None else list(self.presets):
            state = self.presets.get(p)
            if state is None:
                continue
            state.dirty.update(state.failed)
            state.failed.clear()
            state.attempts.clear()
            state.retry_at.clear()
            state.staged = False
            state.failures = 0
            self._publish(p)
        self._kicked.set()
        self._wake.set()

    async def flush(
        self,
        preset: int,
        *,
        mute_wrap: bool = False,
        force_all: bool = False,
        timeout: float | None = None,
    ) -> FlushResult:
        """Write now and wait until the preset is synced, staged or has given up.

        ``force_all`` writes every device path even where the shadow says it's there (push-all). ``mute_wrap`` holds
        the affected outputs muted for the duration of the apply.
        """
        state = self.state(preset)
        if force_all:
            state.force.update(state.values(self.profile))
            state.failed.clear()
            state.attempts.clear()
            state.retry_at.clear()
        if mute_wrap:
            state.wrap_requested = True
        state.staged = False
        self._publish(preset)
        return await self.wait_idle(preset, timeout=timeout)

    async def push_all(self, preset: int, *, mute_wrap: bool = True, timeout: float | None = None) -> FlushResult:
        return await self.flush(preset, mute_wrap=mute_wrap, force_all=True, timeout=timeout)

    async def wait_idle(self, preset: int, *, timeout: float | None = None) -> FlushResult:
        """Wake the writer and wait until ``preset`` has nothing left it can send."""
        state = self.state(preset)
        if not self._has_work(state) and not state.wrap_requested:
            return self._result(preset)
        future: asyncio.Future[FlushResult] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(preset, []).append(future)
        self._wake.set()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            return self._result(preset)

    # --- status ---------------------------------------------------------------------------------------------------

    def pending_paths(self, preset: int) -> list[str]:
        state = self.state(preset)
        pending = self._pending(state)
        return [p for p in state.values(self.profile) if p in pending]

    def status(self, preset: int) -> dict[str, SyncStatus]:
        state = self.state(preset)
        values = state.values(self.profile)
        pending = self._pending(state)
        out: dict[str, SyncStatus] = {}
        for path in values:
            if path in state.failed:
                out[path] = SyncStatus.FAILED
            elif path in pending:
                out[path] = SyncStatus.PENDING
            elif path not in state.applied:
                out[path] = SyncStatus.UNKNOWN
            else:
                out[path] = SyncStatus.SYNCED
        return out

    def summary(self, preset: int) -> dict[str, Any]:
        state = self.state(preset)
        counts = {s.value: 0 for s in SyncStatus}
        for status in self.status(preset).values():
            counts[status.value] += 1
        return {
            "preset": preset,
            "counts": counts,
            "staged": state.staged,
            "error": state.last_error,
            "failed": dict(state.failed),
        }

    # --- internals ------------------------------------------------------------------------------------------------

    def _held_mutes(self, state: PresetState) -> set[str]:
        return {f"{OUTPUTS}.{n}.mute" for n in state.wrap}

    def _pending(self, state: PresetState, values: dict[str, Any] | None = None) -> set[str]:
        """Paths to write: forced, known-different from the shadow, or explicitly queued while unknown."""
        out: set[str] = set()
        for path, value in (state.values(self.profile) if values is None else values).items():
            if path in state.failed:
                continue
            if path in state.force:
                out.add(path)
            elif path in state.applied:
                if not same_value(path, state.applied[path], value, self.profile):
                    out.add(path)
            elif path in state.dirty:
                out.add(path)
        return out

    def _backed_off(self, state: PresetState) -> set[str]:
        """Rejected paths still waiting out their retry backoff."""
        now = time.monotonic()
        return {p for p, at in state.retry_at.items() if at > now}

    def _sendable(self, state: PresetState, values: dict[str, Any] | None = None) -> set[str]:
        """Pending paths the writer may send now: not held muted by a wrapped apply, not backing off."""
        return self._pending(state, values) - self._held_mutes(state) - self._backed_off(state)

    def _apply_left(self, state: PresetState) -> set[str]:
        """Pending paths besides a wrapped apply's held mutes, backing off or not."""
        return self._pending(state) - self._held_mutes(state)

    def _has_work(self, state: PresetState) -> bool:
        return bool(self._apply_left(state)) or (bool(state.wrap) and not state.failed)

    def _next_retry(self) -> float | None:
        """Seconds until the earliest backed-off path may be retried (expired entries are dropped)."""
        now = time.monotonic()
        soonest: float | None = None
        for state in self.presets.values():
            for path, at in list(state.retry_at.items()):
                if at <= now:
                    del state.retry_at[path]
                elif path not in state.failed:
                    soonest = at if soonest is None else min(soonest, at)
        return None if soonest is None else soonest - now

    def _result(self, preset: int) -> FlushResult:
        state = self.state(preset)
        return FlushResult(
            preset=preset,
            staged=state.staged,
            pending=tuple(self.pending_paths(preset)),
            failed=dict(state.failed),
            error=state.last_error,
        )

    def _resolve(self, preset: int, *, force: bool = False) -> None:
        state = self.presets.get(preset)
        if state is None:
            return
        if not force and not state.staged and self._has_work(state):
            return
        waiters = self._waiters.pop(preset, [])
        if not waiters:
            return
        result = self._result(preset)
        for future in waiters:
            if not future.done():
                future.set_result(result)

    def _changed(self, preset: int) -> None:
        if self._on_applied is not None:
            try:
                self._on_applied(preset, self.presets[preset])
            except Exception:
                log.exception("writer on_applied callback failed")
        self._publish(preset)

    def _log(self, level: str, message: str, **data: Any) -> None:
        getattr(log, "warning" if level == "warning" else "info")(message)
        if self._on_log is not None:
            try:
                self._on_log(level, message, **data)
            except Exception:
                log.exception("writer on_log callback failed")

    def _publish(self, preset: int) -> None:
        if self.bus is None or preset not in self.presets:
            return
        state = self.presets[preset]
        status = self.status(preset)
        changes = {p: s.value for p, s in status.items() if state.published.get(p) != s}
        removed = [p for p in state.published if p not in status]
        state.published = status
        summary = self.summary(preset)
        key = (summary["staged"], summary["error"], tuple(sorted(summary["failed"].items())))
        if not changes and not removed and state.published_key == key:
            return
        state.published_key = key
        self.bus.publish(SYNC, {**summary, "changes": changes, "removed": removed}, serial=self.serial)

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self.coalesce_s > 0:
                await asyncio.sleep(self.coalesce_s)
            try:
                delay, fatal = await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("writer cycle crashed")
                delay, fatal = self.backoff_max_s, True
            if delay is None:
                continue
            if fatal:
                # The daemon or device is gone: only a kick (reconnect, retry button) cuts the backoff short.
                self._kicked.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._kicked.wait(), delay)
                self._kicked.clear()
            else:
                # Only some paths are backing off: any new edit runs a cycle right away.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), delay)
            self._wake.set()

    def _backoff(self, failures: int) -> float:
        return min(self.backoff_max_s, self.backoff_min_s * 2 ** max(0, failures - 1))

    async def _cycle(self) -> tuple[float | None, bool]:
        """One pass over every preset with work. Returns the delay before the next retry (None: wait for an edit)
        and whether it is a global backoff (daemon/device gone) rather than a per-path one."""
        work = [p for p, st in self.presets.items() if self._has_work(st) or st.wrap_requested]
        for preset in [p for p in self._waiters if p not in work]:
            self._resolve(preset, force=True)
        if not work:
            return None, False
        try:
            active = await self._active_preset()
        except Exception as exc:
            # Nothing was sent, so nothing becomes unknown; the attempt still counts toward giving up.
            targets = [self._last_active] if self._last_active in work else work
            delays = [self._fail_round(p, exc, paths=self._sendable(self.presets[p])) for p in targets]
            retry = [d for d in delays if d is not None]
            return (min(retry) if retry else None), True
        self._last_active = active
        retry_delay: float | None = None
        for preset in work:
            if preset != active:
                self._stage(preset)
                continue
            delay = await self._flush(preset)
            if delay is not None:
                retry_delay = delay if retry_delay is None else min(retry_delay, delay)
        if retry_delay is not None:
            return retry_delay, True
        return self._next_retry(), False

    def _stage(self, preset: int) -> None:
        state = self.presets[preset]
        if not state.staged:
            state.staged = True
            self._log("info", f"preset {preset} is not active: {len(self._pending(state))} change(s) staged")
        self._publish(preset)
        self._resolve(preset)

    async def _guard(self, preset: int) -> bool | BaseException:
        try:
            active = await self._active_preset()
        except Exception as exc:
            return exc
        self._last_active = active
        return active == preset

    async def _send(self, preset: int, payload: Payload) -> str:
        """Send one payload. Returns ``ok``, ``staged`` (preset guard), ``failed`` or ``fatal`` (daemon/device gone)."""
        state = self.presets[preset]
        async with self._io_lock:
            guard = await self._guard(preset)
            if isinstance(guard, BaseException):
                self._fail_payload(state, payload, guard, sent=False)
                return "fatal"
            if not guard:
                return "staged"
            try:
                await self._post(payload.body)
            except asyncio.CancelledError:
                # Stopped mid-call: the request may already have reached minidspd, so its paths become unknown.
                for path in payload.values:
                    state.applied.pop(path, None)
                    state.dirty.add(path)
                self._changed(preset)
                raise
            except Exception as exc:
                fatal = isinstance(exc, DaemonUnavailable | DeviceNotFound)
                self._fail_payload(state, payload, exc, sent=True, backoff=not fatal)
                self._changed(preset)
                return "fatal" if fatal else "failed"
        self.writes_sent += 1
        values = state.values(self.profile)
        state.applied.update(payload.values)
        for path, sent in payload.values.items():
            state.attempts.pop(path, None)
            state.failed.pop(path, None)
            state.retry_at.pop(path, None)
            state.force.discard(path)
            if path in values and same_value(path, values[path], sent, self.profile):
                state.dirty.discard(path)
        log.debug("wrote %d path(s) to preset %d: %s", len(payload.values), preset, payload.body)
        self._changed(preset)
        return "ok"

    def _fail_payload(
        self, state: PresetState, payload: Payload, exc: BaseException, *, sent: bool, backoff: bool = False
    ) -> None:
        state.last_error = error_text(exc)
        now = time.monotonic()
        for path in payload.values:
            if sent:
                state.applied.pop(path, None)  # may or may not have landed
                state.dirty.add(path)
            state.attempts[path] = state.attempts.get(path, 0) + 1
            if backoff:
                state.retry_at[path] = now + self._backoff(state.attempts[path])
        self._log(
            "warning",
            f"write failed ({len(payload.values)} path(s)): {state.last_error}",
            paths=list(payload.values),
            sent=sent,
        )

    def _fail_round(
        self, preset: int, exc: BaseException | None, *, paths: Iterable[str] = (), fatal: bool = True
    ) -> float | None:
        """Account for a failed round; give up on exhausted paths. Returns the global retry delay after a ``fatal``
        failure, else None (rejected paths retry on their own backoff)."""
        state = self.presets[preset]
        if exc is not None:
            state.last_error = error_text(exc)
            for path in paths:
                state.attempts[path] = state.attempts.get(path, 0) + 1
        exhausted = [p for p in self._pending(state) if state.attempts.get(p, 0) >= self.max_attempts]
        for path in exhausted:
            state.failed[path] = state.last_error or "write failed"
        if exhausted:
            self._log("warning", f"gave up on {len(exhausted)} path(s) after {self.max_attempts} attempts")
        held = self._held_mutes(state)
        if state.wrap and held & set(state.failed):
            # Couldn't mute (or un-mute) the outputs: never run the rest of the bulk apply unprotected.
            for path in self._apply_left(state):
                state.failed[path] = "not applied: the outputs could not be muted first"
        if state.wrap and state.failed and not self._apply_left(state):
            self._fail_held_mutes(state)
        self._publish(preset)
        if not self._has_work(state):
            state.failures = 0
            self._resolve(preset, force=True)
            return None
        if fatal:
            state.failures += 1
            return self._backoff(state.failures)
        if self._sendable(state):
            self._wake.set()  # e.g. independent payloads planned for the next round
        return None

    def _fail_held_mutes(self, state: PresetState) -> None:
        """A wrapped apply gave up: its outputs stay muted. Flag the ones that should be playing."""
        values = state.values(self.profile)
        for n in sorted(state.wrap):
            path = f"{OUTPUTS}.{n}.mute"
            if values.get(path) is False:
                state.failed.setdefault(path, "held muted: the bulk apply did not complete")

    def _wrap_targets(self, state: PresetState) -> set[int]:
        pending = self._pending(state)
        if any(p.startswith(f"{INPUTS}.") for p in pending):
            return set(range(self.profile.n_outputs))
        return {int(p.split(".")[1]) for p in pending if p.startswith(f"{OUTPUTS}.") and not p.endswith(".mute")}

    async def _load_taps(self, state: PresetState) -> dict[str, Sequence[float]]:
        if not self.profile.fir_supported or self._fir_taps is None:
            return {}
        values = state.values(self.profile)
        blobs = {v for p, v in values.items() if p.endswith(".fir.blob_id") and v is not None}
        taps: dict[str, Sequence[float]] = {}
        for blob in sorted(blobs):
            try:
                loaded = await self._fir_taps(blob)
            except Exception:
                log.exception("loading FIR blob %s failed", blob)
                continue
            if loaded is not None:
                taps[blob] = loaded
        return taps

    async def _flush(self, preset: int) -> float | None:
        state = self.presets[preset]
        state.staged = False

        if state.wrap_requested:
            state.wrap_requested = False
            state.wrap |= self._wrap_targets(state)
        if state.wrap and self._apply_left(state):
            # Once the apply itself is done only the final restore is retried: no need to mute again first.
            to_mute = {
                n: True
                for n in sorted(state.wrap)
                if (path := f"{OUTPUTS}.{n}.mute") in state.force or state.applied.get(path) is not True
            }
            if to_mute:
                if self._backed_off(state) & {f"{OUTPUTS}.{n}.mute" for n in to_mute}:
                    self._publish(preset)
                    return None
                outcome = await self._send(preset, mute_payload(to_mute))
                if outcome != "ok":
                    return self._after(preset, outcome)

        # Plan every round from one snapshot: edits that arrive while a call is in flight wait for the next cycle and
        # its gather window, so a continuous drag keeps coalescing instead of chasing every value.
        snapshot = state.desired
        planned = state.values(self.profile)
        unschedulable: set[str] = set()
        for _ in range(MAX_ROUNDS):
            current = state.values(self.profile)
            pending = {
                p
                for p in self._sendable(state, planned)
                if p in current and same_value(p, current[p], planned[p], self.profile)
            }
            if not pending:
                break
            plan = build_plan(
                snapshot,
                state.applied,
                pending,
                profile=self.profile,
                fir_taps=await self._load_taps(state),
                skip=state.failed,
                split_channels=self.split_channels,
                check=self._value_check,
            )
            for path, reason in plan.blocked.items():
                state.failed[path] = reason
                self._log("warning", f"cannot write {path}: {reason}", path=path)
            if not plan.payloads:
                unschedulable = pending - set(state.failed)
                break
            # A payload that pulls in a backed-off prerequisite (e.g. coefficients for an un-bypass) waits for it.
            backed_off = self._backed_off(state)
            payloads = [pl for pl in plan.payloads if not backed_off & set(pl.values)]
            if not payloads:
                break
            outcome = "ok"
            for payload in payloads:
                result = await self._send(preset, payload)
                if result == "staged" or result == "fatal":
                    outcome = result
                    break
                if result == "failed":
                    outcome = "failed"  # the daemon is up: carry on with the independent payloads
            if outcome != "ok":
                return self._after(preset, outcome)

        for path in unschedulable:
            state.failed[path] = "could not be scheduled"
        if self._apply_left(state):
            # Edited while writing (carry on in the next cycle, after another gather window), or some paths are
            # waiting out a retry backoff (the next cycle is timed for them). A wrapped apply stays muted meanwhile.
            self._publish(preset)
            if self._sendable(state):
                self._wake.set()
            return None

        if state.wrap and not state.failed:
            values = state.values(self.profile)
            restore = {
                n: values[path]
                for n in sorted(state.wrap)
                if (path := f"{OUTPUTS}.{n}.mute") in state.force or state.applied.get(path) != values[path]
            }
            if restore:
                if self._backed_off(state) & {f"{OUTPUTS}.{n}.mute" for n in restore}:
                    self._publish(preset)
                    return None
                outcome = await self._send(preset, mute_payload(restore))
                if outcome != "ok":
                    return self._after(preset, outcome)
            state.wrap.clear()
            self._changed(preset)  # the held outputs are persisted with the shadow
        elif state.wrap:
            self._fail_held_mutes(state)

        state.failures = 0
        if not state.failed:
            state.last_error = None
        self._publish(preset)
        self._resolve(preset)
        if self._sendable(state):
            self._wake.set()  # e.g. edited during the final mute restore
        return None

    def _after(self, preset: int, outcome: str) -> float | None:
        if outcome == "staged":
            self._stage(preset)
            return None
        return self._fail_round(preset, None, fatal=outcome == "fatal")
