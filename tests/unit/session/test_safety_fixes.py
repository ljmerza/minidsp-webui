"""Session safety rules: a wrapped apply stays muted across restarts, gain ceilings hold on every write path, a
preset switch waits for the call in flight, and adopting from the defaults mutes unused outputs."""

import asyncio
import json

import pytest

from app.model.config_doc import set_path
from app.sync.session import DesiredValidationError
from tests.unit.sync.conftest import PEAK

from .conftest import settle


def peq_doc(session):
    doc = session.desired(0)
    doc = set_path(doc, "outputs.0.peq.0.compiled", PEAK)
    return set_path(doc, "outputs.0.peq.0.bypass", False)


def unmutes(bodies) -> list[dict]:
    return [o for body in bodies for o in body.get("outputs", []) if o.get("mute") is False]


async def test_held_mutes_survive_a_restart(make_session, fake):
    s = await make_session()
    s.writer.max_attempts = 2
    s.writer.backoff_min_s = s.writer.backoff_max_s = 0.01
    await s.adopt("trust", confirm_defaults=True)
    fake.fail_when = lambda body: 500 if "peq" in json.dumps(body) else None
    result = await s.replace_desired(0, peq_doc(s), timeout=3)
    assert not result.flush.ok
    assert fake.configs[0] == {"outputs": [{"index": 0, "mute": True}]}
    assert unmutes(fake.configs) == []
    await s.stop()

    seen = len(fake.configs)
    s2 = await make_session()
    assert s2.writer.state(0).wrap == {0}
    await asyncio.sleep(0.2)  # the reloaded session retries the PEQ write
    assert fake.rejected[-1]["outputs"][0]["index"] == 0
    assert unmutes(fake.configs[seen:] + fake.rejected) == []  # still held muted

    fake.fail_when = None
    s2.writer.kick()
    assert (await settle(s2)).ok
    assert fake.configs[-1] == {"outputs": [{"index": 0, "mute": False}]}
    assert s2.writer.state(0).wrap == set()


async def test_a_ceiling_below_a_current_gain_is_refused(session):
    await session.edit(2, {"outputs": {"1": {"gain_db": 6.0}}})
    await session.edit(0, {"inputs": {"0": {"routing": {"1": {"gain_db": 3.0}}}}})
    with pytest.raises(DesiredValidationError) as err:
        await session.set_channel_meta("output", 1, name="Sub", max_gain_db=2.0)
    assert any("preset 3: outputs.1.gain_db is 6 dB" in p for p in err.value.problems)
    assert any("preset 1: inputs.0.routing.1.gain_db is 3 dB" in p for p in err.value.problems)
    await session.set_channel_meta("output", 1, name="Sub", max_gain_db=6.0)
    await session.set_channel_meta("output", 1, name="Sub 2", max_gain_db=6.0)  # unchanged ceiling: no check


async def test_trust_adopt_above_a_ceiling_is_refused(session):
    await session.set_channel_meta("output", 0, name="Shakers", max_gain_db=0.0)
    with pytest.raises(DesiredValidationError) as err:
        await session.adopt("trust", script="output 0 gain 10\n")
    assert "outputs.0.gain_db" in err.value.problems[0]


async def test_push_all_and_the_writer_refuse_gains_above_a_ceiling(session, fake):
    await session.set_channel_meta("output", 0, name="Shakers", max_gain_db=0.0)
    state = session.writer.state(0)
    session.writer.set_desired(0, set_path(state.desired, "outputs.0.gain_db", 10.0))  # e.g. legacy stored state
    with pytest.raises(DesiredValidationError):
        await session.push_all(0)
    result = await settle(session)
    assert "ceiling" in result.failed["outputs.0.gain_db"]
    assert not any(o.get("gain") == 10.0 for o in fake.outputs_written())


async def test_adopt_push_from_defaults_mutes_unnamed_outputs(session, fake):
    await session.set_channel_meta("output", 0, name="Shakers")
    result = await session.adopt("push", confirm=True)
    assert result.flush.ok
    doc = session.desired(0)
    assert [o.mute for o in doc.outputs] == [False, True, True, True]
    assert fake.configs[-1] == {"outputs": [{"index": 0, "mute": False}]}


async def test_preset_switch_waits_for_the_write_in_flight(session, fake):
    release = asyncio.Event()
    original = session.writer._post
    order: list[str] = []

    async def slow_post(body):
        order.append("config start")
        await release.wait()
        await original(body)
        order.append("config done")

    session.writer._post = slow_post
    await session.edit(0, {"outputs": {"0": {"gain_db": -1.0}}})
    while not order:
        await asyncio.sleep(0.01)
    switch = asyncio.create_task(session.set_preset(1))
    await asyncio.sleep(0.1)
    assert fake.masters == []  # the switch waits for the config call
    release.set()
    await switch
    assert order == ["config start", "config done"]
    assert fake.masters == [{"preset": 1}]
