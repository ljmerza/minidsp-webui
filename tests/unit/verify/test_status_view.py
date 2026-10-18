"""The one status model on a real session (phase 9 design §11.1, PR 7): the last check's "changed on device" stays until
the app sends the setting again, uses the device's value or forgets it; the writer's batch counts "Sending n of N"."""

import asyncio

from app.daemon.rawframes import M2X4HD_MAP
from app.sync import verify as svc
from app.sync.status import status_view

from .conftest import f32
from .test_verify import run, shaker  # noqa: F401

GAIN_0, GAIN_1, DELAY_0 = "outputs.0.gain_db", "outputs.1.gain_db", "outputs.0.delay_ms"


async def _sent(session, path, timeout=2.0):
    """Send ``path`` again, as the table's per-row Send does, and wait until it went out."""
    session.writer.invalidate(0, [path])
    session.writer.notify(0, [path])
    await session.writer.flush(0, timeout=timeout)


async def test_before_any_check_the_view_never_claims_a_match(shaker):  # noqa: F811
    view = status_view(shaker, 0)
    assert (view.state, view.text, view.glyph) == ("sent", "All sent", "✓")
    assert view.check is None and not view.changed
    assert view.counts["sent"] == view.total == 216


async def test_a_change_stays_until_sent_used_or_forgotten(shaker, reader_factory, bridge):  # noqa: F811
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    bridge.memory[M2X4HD_MAP.output_gain[1]] = f32(-20.205997)
    bridge.memory[M2X4HD_MAP.output_delay[0]] = (0).to_bytes(4, "little")
    report = await run(shaker, reader_factory)
    assert {i.path for i in report.drifted} == {GAIN_0, GAIN_1, DELAY_0}

    view = status_view(shaker, 0)
    assert view.changed == {GAIN_0, GAIN_1, DELAY_0}
    assert (view.state, view.text, view.glyph) == ("changed", "3 changed on device", "≠")
    assert view.states[GAIN_0] == "changed" and view.counts["changed"] == 3
    assert view.check.result.startswith("3 changed on device; ") and "can't be checked" in view.check.result

    # Sending something else doesn't close it.
    shaker.writer.start()
    await shaker.edit(0, {"outputs": {"3": {"gain_db": -3.0}}})
    await shaker.writer.flush(0, timeout=2)
    assert status_view(shaker, 0).changed == {GAIN_0, GAIN_1, DELAY_0}

    # Sent again: the app's value overwrote the device's.
    await _sent(shaker, GAIN_0)
    view = status_view(shaker, 0)
    assert view.changed == {GAIN_1, DELAY_0} and view.states[GAIN_0] == "sent"

    # Used: the app takes the device's value.
    await svc.accept_device_values(shaker, report, [GAIN_1])
    view = status_view(shaker, 0)
    assert view.changed == {DELAY_0} and view.states[GAIN_1] == "sent"

    # Undone: it is open again.
    await svc.undo_accept(shaker, 0)
    assert status_view(shaker, 0).changed == {GAIN_1, DELAY_0}

    # Forgotten: the app no longer assumes it knows the value, so it is Unknown rather than changed.
    report.marked_unknown = svc.mark_drift_unknown(shaker, 0, [GAIN_1, DELAY_0])
    view = status_view(shaker, 0)
    assert not view.changed
    assert view.states[DELAY_0] == "unknown"
    assert (view.state, view.text) == ("unknown", "2 unknown")
    # The check isn't claimed to match afterwards: the device wasn't read again.
    assert view.check.result.startswith("3 changed on device, all sent or forgotten since")


async def test_a_check_of_another_preset_doesnt_count(shaker, reader_factory, bridge):  # noqa: F811
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    await run(shaker, reader_factory)
    assert status_view(shaker, 0).changed == {GAIN_0}
    assert not status_view(shaker, 1).changed


async def test_the_summary_engine_counts_the_same_changes(shaker, reader_factory, bridge):  # noqa: F811
    from app.sync.summary import session_summary

    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    await run(shaker, reader_factory)
    codes = [p.code for p in session_summary(shaker, 0).problems]
    assert "changed_on_device" in codes
    shaker.writer.start()
    await _sent(shaker, GAIN_0)
    assert "changed_on_device" not in [p.code for p in session_summary(shaker, 0).problems]


async def test_bulk_send_counts_up_and_says_outputs_are_muted(shaker, fake):  # noqa: F811
    shaker.writer.start()
    gate = asyncio.Event()
    seen: list[str] = []
    real_post = shaker.writer._post

    async def slow_post(body):
        view = status_view(shaker, 0)
        if view.state == "sending":
            seen.append(view.text)
        await gate.wait()
        return await real_post(body)

    shaker.writer._post = slow_post
    task = asyncio.create_task(shaker.writer.push_all(0, mute_wrap=True, timeout=5))
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen and seen[0].startswith("Sending 0 of 216")
    gate.set()
    await task
    texts = [t for t in seen if "of 216" in t]
    assert any(t.endswith("· outputs muted") for t in texts)
    view = status_view(shaker, 0)
    assert (view.state, view.text) == ("sent", "All sent")
    assert shaker.writer.state(0).batch_total == 0
