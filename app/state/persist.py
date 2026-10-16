"""Debounced, coalescing persistence for desired state (SD-card write budget, docs/01).

Each key (e.g. ``(serial, preset)``) holds at most one pending value. A write happens ``delay`` seconds after the last
``schedule`` for that key, but no later than ``max_delay`` after the first unwritten one, so a long slider drag still
persists periodically. Only the latest value is written. A failed write keeps the value pending and retries with
backoff; the in-memory state stays authoritative meanwhile.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import Any

from app.state.repo import Repo

log = logging.getLogger(__name__)

DEFAULT_DELAY_S = 1.0
DEFAULT_MAX_DELAY_S = 5.0
DEFAULT_RETRY_MAX_S = 30.0


class DebouncedPersister[K: Hashable, V]:
    def __init__(
        self,
        write: Callable[[K, V], Awaitable[Any]],
        *,
        delay: float = DEFAULT_DELAY_S,
        max_delay: float = DEFAULT_MAX_DELAY_S,
        retry_max_delay: float = DEFAULT_RETRY_MAX_S,
        on_error: Callable[[K, BaseException], Any] | None = None,
    ) -> None:
        if delay < 0 or max_delay < delay:
            raise ValueError("need 0 <= delay <= max_delay")
        self._write = write
        self.delay = delay
        self.max_delay = max_delay
        self.retry_max_delay = retry_max_delay
        self._on_error = on_error
        self._pending: dict[K, V] = {}
        self._first: dict[K, float] = {}
        self._last: dict[K, float] = {}
        self._retry_at: dict[K, float] = {}
        self._failures: dict[K, int] = {}
        self._tasks: dict[K, asyncio.Task[None]] = {}
        self._locks: dict[K, asyncio.Lock] = {}
        self._closed = False

    @property
    def pending_keys(self) -> set[K]:
        return set(self._pending)

    def is_pending(self, key: K) -> bool:
        return key in self._pending

    def failures(self, key: K) -> int:
        """Consecutive failed writes for ``key`` (0 once a write succeeds)."""
        return self._failures.get(key, 0)

    def schedule(self, key: K, value: V) -> None:
        """Replace the pending value for ``key`` and (re)start its debounce window. Never blocks or writes."""
        if self._closed:
            raise RuntimeError("persister is closed")
        now = asyncio.get_running_loop().time()
        self._pending[key] = value
        self._first.setdefault(key, now)
        self._last[key] = now
        self._ensure_task(key)

    async def flush(self, keys: Iterable[K] | None = None) -> bool:
        """Write pending values now (e.g. before a preset switch or on shutdown). Returns False if any write failed;
        failed values stay pending and are retried in the background."""
        targets = list(self._pending) if keys is None else list(keys)
        ok = True
        for key in targets:
            ok = await self._write_now(key) and ok
        return ok

    async def aclose(self) -> bool:
        """Flush everything and stop. Returns False if something could not be written (it is logged)."""
        self._closed = True
        # Stop the background tasks first. One cancelled mid-write puts its value back (`_write_now`), and the
        # flush below writes it again; writes are idempotent, so a value is never dropped.
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        ok = await self.flush()
        if not ok:
            log.error("unsaved state on shutdown for %s", sorted(map(repr, self._pending)))
        return ok

    # --- internals --------------------------------------------------------------------------------------------

    def _ensure_task(self, key: K) -> None:
        if key not in self._tasks and not self._closed:
            self._tasks[key] = asyncio.create_task(self._run(key), name=f"persist:{key!r}")

    def _deadline(self, key: K) -> float:
        deadline = min(self._last[key] + self.delay, self._first[key] + self.max_delay)
        return max(deadline, self._retry_at.get(key, 0.0))

    async def _run(self, key: K) -> None:
        loop = asyncio.get_running_loop()
        try:
            while key in self._pending:
                wait = self._deadline(key) - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
                await self._write_now(key)
        finally:
            if self._tasks.get(key) is asyncio.current_task():
                del self._tasks[key]

    async def _write_now(self, key: K) -> bool:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self._pending:
                return True
            value = self._pending.pop(key)
            self._first.pop(key, None)
            self._last.pop(key, None)
            try:
                await self._write(key, value)
            except asyncio.CancelledError:
                if key not in self._pending:  # the write may not have happened: keep the value
                    now = asyncio.get_running_loop().time()
                    self._pending[key] = value
                    self._first.setdefault(key, now)
                    self._last.setdefault(key, now)
                raise
            except Exception as exc:
                failures = self._failures.get(key, 0) + 1
                self._failures[key] = failures
                log.warning("persisting %r failed (attempt %d): %s", key, failures, exc)
                if self._on_error is not None:
                    try:
                        self._on_error(key, exc)
                    except Exception:
                        log.exception("persist on_error callback failed")
                now = asyncio.get_running_loop().time()
                if key not in self._pending:  # nothing newer was scheduled while writing: keep this value
                    self._pending[key] = value
                    self._first[key] = now
                    self._last[key] = now
                self._retry_at[key] = now + min(self.delay * 2**failures, self.retry_max_delay)
                if asyncio.current_task() is not self._tasks.get(key):
                    self._ensure_task(key)
                return False
            self._failures.pop(key, None)
            self._retry_at.pop(key, None)
            return True


# --- preset state -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PresetSnapshot:
    desired: Any
    applied: Any = None
    unknown_paths: Iterable[str] = ()


PresetKey = tuple[int, int]  # (serial, preset)
PresetValue = PresetSnapshot | Callable[[], PresetSnapshot]


def preset_state_persister(
    repo: Repo,
    *,
    delay: float = DEFAULT_DELAY_S,
    max_delay: float = DEFAULT_MAX_DELAY_S,
    on_error: Callable[[PresetKey, BaseException], Any] | None = None,
) -> DebouncedPersister[PresetKey, PresetValue]:
    """A persister for ``preset_state`` rows keyed by ``(serial, preset)``.

    Schedule either a ``PresetSnapshot`` or a zero-argument callable returning one; a callable is evaluated at write
    time, so the row reflects the latest in-memory state rather than the state at schedule time.
    """

    async def write(key: PresetKey, value: PresetValue) -> None:
        snap = value() if callable(value) else value
        serial, preset = key
        await repo.save_preset_state(
            serial,
            preset,
            desired=snap.desired,
            applied=snap.applied,
            unknown_paths=snap.unknown_paths,
        )

    return DebouncedPersister(write, delay=delay, max_delay=max_delay, on_error=on_error)
