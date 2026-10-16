import asyncio

import pytest

from app.bus import LEVELS, LOG, MASTER, Bus, SubscriptionClosed


async def test_topic_filtering_and_wildcard():
    bus = Bus()
    masters = bus.subscribe([MASTER])
    everything = bus.subscribe()
    bus.publish(MASTER, {"v": 1}, serial=7)
    bus.publish(LOG, {"m": "x"})
    assert (await masters.get()).data == {"v": 1}
    assert masters.pending() == 0
    assert [(await everything.get()).topic for _ in range(2)] == [MASTER, LOG]
    event = bus.publish(MASTER, {"v": 2}, serial=7)
    assert event.serial == 7


async def test_levels_are_lossy_drop_oldest():
    bus = Bus(lossy={LEVELS: 2})
    sub = bus.subscribe()
    for i in range(5):
        bus.publish(LEVELS, {"n": i})
    bus.publish(MASTER, {"keep": True})
    received = [sub.get_nowait() for _ in range(sub.pending())]
    assert [e.data for e in received] == [{"n": 3}, {"n": 4}, {"keep": True}]


async def test_bounded_queue_drops_oldest():
    bus = Bus(maxsize=3, lossy={})
    sub = bus.subscribe()
    for i in range(5):
        bus.publish(LOG, {"n": i})
    assert [sub.get_nowait().data["n"] for _ in range(3)] == [2, 3, 4]
    assert sub.dropped == 2


async def test_subscriber_count_and_watchers():
    bus = Bus()
    changes = []
    unregister = bus.on_subscribers_changed(lambda: changes.append(bus.subscriber_count(LEVELS)))
    assert bus.subscriber_count(LEVELS) == 0
    async with bus.subscribe([LEVELS]):
        assert bus.subscriber_count(LEVELS) == 1
        assert bus.subscriber_count(MASTER) == 0
    assert changes == [1, 0]
    unregister()
    bus.subscribe()
    assert changes == [1, 0]


async def test_get_waits_and_close_ends_iteration():
    bus = Bus()
    sub = bus.subscribe([MASTER])

    async def consume():
        return [event.data async for event in sub]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    bus.publish(MASTER, {"a": 1})
    await asyncio.sleep(0)
    sub.close()
    assert await asyncio.wait_for(task, 1) == [{"a": 1}]
    with pytest.raises(SubscriptionClosed):
        await sub.get()
    bus.publish(MASTER, {"after": True})
    assert sub.pending() == 0
