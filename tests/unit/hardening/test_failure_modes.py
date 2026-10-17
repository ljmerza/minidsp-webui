"""docs/03 §10 failure modes, end to end through a session on the fake daemon."""

import asyncio
import json

import httpx
import pytest

from app.api.v1.events import event_stream
from app.bus import DESIRED, LEVELS, Bus
from app.sync.session import RevisionConflict

from .conftest import settle


def fast_retries(session) -> None:
    session.writer.backoff_min_s = 0.005
    session.writer.backoff_max_s = 0.01


class Manager:
    def __init__(self, *sessions) -> None:
        self.sessions = list(sessions)

    def __iter__(self):
        return iter(self.sessions)


async def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def test_daemon_down_queues_only_the_latest_value_per_setting(session, fake):
    """Bounded write queue: hundreds of edits while minidspd is down hold one value per path, and once it's back
    only the final values are written."""
    fast_retries(session)
    fake.fail = httpx.ConnectError("connection refused")
    sent: list[float] = []
    for step in range(300):
        value = -0.1 * step
        sent.append(value)
        await session.edit(0, {"outputs": {str(step % 4): {"gain_db": round(value, 1)}}})
    await asyncio.sleep(0.1)
    assert fake.configs == []
    state = session.writer.state(0)
    # what's still owed to the device: the edited paths, nothing per edit
    assert {p for p in state.dirty | set(state.failed) if p.endswith("gain_db")} <= {
        f"outputs.{n}.gain_db" for n in range(4)
    }

    fake.fail = None
    session.writer.kick()  # what a reconnect does
    assert (await settle(session)).ok
    written = {o["index"]: o["gain"] for o in fake.outputs_written() if "gain" in o}
    assert written == {n: round(-0.1 * (296 + n), 1) for n in range(4)}
    assert len(fake.configs) <= 4
    assert all(session.sync_status(0)[f"outputs.{n}.gain_db"] == "synced" for n in range(4))


async def test_rejected_payload_gives_up_after_five_attempts(session, fake):
    fast_retries(session)
    fake.fail_when = lambda body: 500 if any(o.get("index") == 1 for o in body.get("outputs", [])) else None
    await session.edit(0, {"outputs": {"1": {"gain_db": -4.0}}})
    await wait_for(lambda: session.sync_status(0)["outputs.1.gain_db"] == "failed")
    assert len(fake.rejected) == 5
    summary = session.sync_summary(0)
    assert summary["failed"]["outputs.1.gain_db"] == "Specified channel or peq is out of range"  # the daemon's text
    assert "outputs.1.gain_db" not in session.writer.state(0).applied  # the failed call's paths are unknown

    await asyncio.sleep(0.1)
    assert len(fake.rejected) == 5  # no endless retry loop

    await session.edit(0, {"outputs": {"2": {"gain_db": -2.0}}})  # other settings keep flowing
    await wait_for(lambda: session.sync_status(0)["outputs.2.gain_db"] == "synced")

    fake.fail_when = None
    await session.edit(0, {"outputs": {"1": {"gain_db": -4.5}}})  # editing it again re-arms it
    await wait_for(lambda: session.sync_status(0)["outputs.1.gain_db"] == "synced")


async def test_two_tabs_converge_through_desired_events(session, bus):
    manager = Manager(session)
    tab_a = event_stream(bus, manager, {DESIRED}, serial=session.serial)
    tab_b = event_stream(bus, manager, {DESIRED}, serial=session.serial)
    first_a = asyncio.ensure_future(anext(tab_a))
    first_b = asyncio.ensure_future(anext(tab_b))
    await asyncio.sleep(0)  # both streams subscribed

    await session.edit(0, {"outputs": {"3": {"gain_db": -7.5}}}, source="ui")
    for message in await asyncio.wait_for(asyncio.gather(first_a, first_b), 2):
        assert message["event"] == "desired"
        data = json.loads(message["data"])
        assert data["preset"] == 0 and data["source"] == "ui"
        assert data["values"] == {"outputs.3.gain_db": -7.5}
    await tab_a.aclose()
    await tab_b.aclose()


async def test_stale_tab_gets_a_revision_conflict(session):
    base = session.revisions.get(0, 0)
    await session.edit(0, {"outputs": {"0": {"gain_db": -1.0}}}, expected_revision=base, commit=True)
    with pytest.raises(RevisionConflict):
        await session.edit(0, {"outputs": {"0": {"gain_db": -9.0}}}, expected_revision=base)
    assert session.desired(0).outputs[0].gain_db == -1.0


def test_slow_event_consumers_are_bounded():
    """A browser tab that stops reading can't grow memory: its buffer keeps the newest events only."""
    bus = Bus(maxsize=64)
    stalled = bus.subscribe()
    for i in range(5000):
        bus.publish(DESIRED, {"i": i})
        bus.publish(LEVELS, {"i": i})
    assert stalled.pending() <= 64
    assert stalled.dropped > 0
    stalled.close()
