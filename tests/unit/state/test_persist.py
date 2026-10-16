import asyncio

import pytest

from app.state.persist import DebouncedPersister, PresetSnapshot, preset_state_persister

DELAY = 0.05


class Recorder:
    def __init__(self, fail_times: int = 0) -> None:
        self.writes: list[tuple] = []
        self.fail_times = fail_times
        self.attempts = 0

    async def __call__(self, key, value) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise OSError("disk full")
        self.writes.append((key, value))


async def wait_for(predicate, timeout: float = 2.0) -> None:
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


async def test_coalesces_bursts_into_one_write():
    rec = Recorder()
    p = DebouncedPersister(rec, delay=DELAY, max_delay=1.0)
    for i in range(10):
        p.schedule("k", i)
        await asyncio.sleep(0.005)
    assert rec.writes == []
    assert p.is_pending("k")
    await wait_for(lambda: rec.writes)
    await asyncio.sleep(DELAY * 2)
    assert rec.writes == [("k", 9)]
    assert not p.is_pending("k")
    await p.aclose()


async def test_write_waits_for_quiet_period():
    rec = Recorder()
    p = DebouncedPersister(rec, delay=DELAY, max_delay=1.0)
    loop = asyncio.get_running_loop()
    p.schedule("k", 1)
    await asyncio.sleep(DELAY * 0.6)
    last = loop.time()
    p.schedule("k", 2)
    await wait_for(lambda: rec.writes)
    assert loop.time() - last >= DELAY * 0.9
    await p.aclose()


async def test_max_delay_bounds_a_long_drag():
    rec = Recorder()
    p = DebouncedPersister(rec, delay=DELAY, max_delay=DELAY * 3)
    loop = asyncio.get_running_loop()
    end = loop.time() + DELAY * 8
    i = 0
    while loop.time() < end:
        p.schedule("k", i)
        i += 1
        await asyncio.sleep(DELAY / 4)
    assert 1 <= len(rec.writes) <= 4
    await p.aclose()
    assert rec.writes[-1] == ("k", i - 1)


async def test_keys_are_independent():
    rec = Recorder()
    p = DebouncedPersister(rec, delay=DELAY)
    p.schedule("a", 1)
    p.schedule("b", 2)
    p.schedule("a", 3)
    await wait_for(lambda: len(rec.writes) == 2)
    assert sorted(rec.writes) == [("a", 3), ("b", 2)]
    assert p.pending_keys == set()
    await p.aclose()


async def test_flush_writes_immediately():
    rec = Recorder()
    p = DebouncedPersister(rec, delay=10.0, max_delay=10.0)
    p.schedule("a", 1)
    p.schedule("b", 2)
    assert await p.flush(["a"]) is True
    assert rec.writes == [("a", 1)]
    assert p.pending_keys == {"b"}
    assert await p.flush() is True
    assert rec.writes == [("a", 1), ("b", 2)]
    assert await p.flush() is True  # nothing pending
    await p.aclose()
    assert len(rec.writes) == 2


async def test_aclose_flushes_and_rejects_new_values():
    rec = Recorder()
    p = DebouncedPersister(rec, delay=10.0, max_delay=10.0)
    p.schedule("k", "v")
    assert await p.aclose() is True
    assert rec.writes == [("k", "v")]
    with pytest.raises(RuntimeError, match="closed"):
        p.schedule("k", "w")


async def test_failed_write_is_retried_with_backoff():
    rec = Recorder(fail_times=2)
    errors = []
    p = DebouncedPersister(rec, delay=DELAY / 5, on_error=lambda key, exc: errors.append((key, str(exc))))
    p.schedule("k", "v")
    await wait_for(lambda: rec.writes)
    assert rec.writes == [("k", "v")]
    assert rec.attempts == 3
    assert errors == [("k", "disk full"), ("k", "disk full")]
    assert p.failures("k") == 0
    await p.aclose()


async def test_newer_value_wins_over_failed_one():
    rec = Recorder(fail_times=1)
    p = DebouncedPersister(rec, delay=10.0, max_delay=10.0)
    p.schedule("k", "old")
    assert await p.flush() is False
    assert p.failures("k") == 1 and p.is_pending("k")
    p.schedule("k", "new")
    assert await p.flush() is True
    assert rec.writes == [("k", "new")]
    await p.aclose()


async def test_failed_flush_keeps_value_and_background_retry_writes_it():
    rec = Recorder(fail_times=1)
    p = DebouncedPersister(rec, delay=DELAY / 5, max_delay=1.0)
    p.schedule("k", "v")
    assert await p.flush() is False
    await wait_for(lambda: rec.writes)
    assert rec.writes == [("k", "v")]
    await p.aclose()


async def test_aclose_reports_unsaved_state():
    rec = Recorder(fail_times=99)
    p = DebouncedPersister(rec, delay=10.0, max_delay=10.0)
    p.schedule("k", "v")
    assert await p.aclose() is False


async def test_on_error_exceptions_are_contained():
    def boom(key, exc):
        raise RuntimeError("callback broke")

    rec = Recorder(fail_times=1)
    p = DebouncedPersister(rec, delay=10.0, max_delay=10.0, on_error=boom)
    p.schedule("k", "v")
    assert await p.flush() is False
    assert await p.flush() is True
    await p.aclose()


def test_delay_validation():
    with pytest.raises(ValueError):
        DebouncedPersister(Recorder(), delay=2.0, max_delay=1.0)
    with pytest.raises(ValueError):
        DebouncedPersister(Recorder(), delay=-1.0)


async def test_preset_state_persister_writes_latest_state(repo):
    state = {"desired": {"g": 0}, "applied": None, "unknown": ["outputs.0.gain_db"]}

    def snapshot() -> PresetSnapshot:
        return PresetSnapshot(desired=dict(state["desired"]), applied=state["applied"], unknown_paths=state["unknown"])

    p = preset_state_persister(repo, delay=DELAY, max_delay=1.0)
    key = (911111, 2)
    for g in range(5):
        state["desired"] = {"g": g}
        p.schedule(key, snapshot)
    state["applied"] = {"g": 4}
    state["unknown"] = []
    await wait_for(lambda: not p.is_pending(key))
    st = await repo.get_preset_state(*key)
    assert st.desired == {"g": 4} and st.applied == {"g": 4} and st.unknown_paths == []
    assert st.revision == 0

    p.schedule(key, PresetSnapshot(desired={"g": 5}))
    await p.aclose()
    st = await repo.get_preset_state(*key)
    assert st.desired == {"g": 5} and st.applied is None


async def test_aclose_waits_for_a_write_in_flight():
    started = asyncio.Event()
    release = asyncio.Event()
    writes: list[tuple] = []

    async def write(key, value):
        if not writes and not release.is_set():
            started.set()
            await release.wait()  # e.g. waiting for the shared database lock
        writes.append((key, value))

    p = DebouncedPersister(write, delay=0.01, max_delay=0.05)
    p.schedule("k", "v1")
    await asyncio.wait_for(started.wait(), 1)
    closing = asyncio.create_task(p.aclose())
    await asyncio.sleep(0.01)
    release.set()
    assert await closing is True
    assert writes == [("k", "v1")]
    assert p.pending_keys == set()


async def test_cancelled_write_keeps_its_value_pending():
    started = asyncio.Event()

    async def write(key, value):
        started.set()
        await asyncio.sleep(10)

    p = DebouncedPersister(write, delay=0.0, max_delay=0.0)
    p.schedule("k", "v1")
    await asyncio.wait_for(started.wait(), 1)
    task = p._tasks["k"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert p.is_pending("k")
