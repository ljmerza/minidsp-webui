"""In-process publish/subscribe used to fan device events out to SSE clients, MQTT and other listeners.

Every subscription has its own bounded buffer, so a slow consumer never blocks publishers. Lossy topics (levels)
keep only their newest few events per subscriber; when the whole buffer is full the oldest event is dropped.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

LEVELS = "levels"
MASTER = "master"
DEVICE = "device"
SYNC = "sync"
DESIRED = "desired"
LOG = "log"

DEFAULT_LOSSY = {LEVELS: 2}


class SubscriptionClosed(Exception):
    """Raised by `Subscription.get` once the subscription is closed and drained."""


@dataclass(frozen=True, slots=True)
class Event:
    topic: str
    data: dict[str, Any]
    serial: int | None = None
    ts: float = field(default_factory=time.time)


class Subscription:
    def __init__(self, bus: "Bus", topics: frozenset[str] | None, maxsize: int, lossy: dict[str, int]) -> None:
        self._bus = bus
        self.topics = topics
        self._maxsize = maxsize
        self._lossy = lossy
        self._items: deque[Event] = deque()
        self._ready = asyncio.Event()
        self.dropped = 0
        self.closed = False

    def wants(self, topic: str) -> bool:
        return self.topics is None or topic in self.topics

    def _push(self, event: Event) -> None:
        limit = self._lossy.get(event.topic)
        if limit is not None and sum(1 for e in self._items if e.topic == event.topic) >= limit:
            oldest = next(e for e in self._items if e.topic == event.topic)
            self._items.remove(oldest)
        elif len(self._items) >= self._maxsize:
            self._items.popleft()
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                log.warning("bus subscriber is slow, dropped %d events", self.dropped)
        self._items.append(event)
        self._ready.set()

    async def get(self) -> Event:
        while not self._items:
            if self.closed:
                raise SubscriptionClosed
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()

    def get_nowait(self) -> Event | None:
        return self._items.popleft() if self._items else None

    def pending(self) -> int:
        return len(self._items)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._ready.set()
            self._bus._remove(self)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self

    async def __anext__(self) -> Event:
        try:
            return await self.get()
        except SubscriptionClosed:
            raise StopAsyncIteration from None

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()


class Bus:
    def __init__(self, *, maxsize: int = 256, lossy: dict[str, int] | None = None) -> None:
        self._maxsize = maxsize
        self._lossy = dict(DEFAULT_LOSSY if lossy is None else lossy)
        self._subs: list[Subscription] = []
        self._watchers: list[Callable[[], None]] = []

    def subscribe(self, topics: Iterable[str] | None = None, *, maxsize: int | None = None) -> Subscription:
        """Subscribe to ``topics`` (None = every topic). Use as ``async with bus.subscribe(...) as sub``."""
        sub = Subscription(
            self, frozenset(topics) if topics is not None else None, maxsize or self._maxsize, self._lossy
        )
        self._subs.append(sub)
        self._notify()
        return sub

    def publish(self, topic: str, data: dict[str, Any], *, serial: int | None = None) -> Event:
        event = Event(topic, data, serial)
        for sub in self._subs:
            if sub.wants(topic):
                sub._push(event)
        return event

    def subscriber_count(self, topic: str) -> int:
        """Subscriptions that would receive ``topic`` (wildcard subscribers included)."""
        return sum(1 for s in self._subs if s.wants(topic))

    def on_subscribers_changed(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` whenever a subscription is added or removed. Returns an unregister function."""
        self._watchers.append(callback)
        return lambda: self._watchers.remove(callback) if callback in self._watchers else None

    def _remove(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)
            self._notify()

    def _notify(self) -> None:
        for callback in list(self._watchers):
            try:
                callback()
            except Exception:
                log.exception("bus subscriber watcher failed")
