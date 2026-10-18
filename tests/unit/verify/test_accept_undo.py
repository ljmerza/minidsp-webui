"""Safe "Use the device's values" (phase9 design §11.2, P02): all-zero filters are refused, and an accept can be undone
for 30 seconds from a server-side snapshot that writes nothing and queues no write. On a real session over the fake
frame bridge."""

import pytest

from app.daemon.rawframes import M2X4HD_MAP
from app.dsp.design import peaking
from app.sync import session as session_mod
from app.sync import verify as svc
from app.sync.session import UndoUnavailable

from .conftest import f32
from .test_readback_sync import biquad, reads  # noqa: F401
from .test_verify import shaker  # noqa: F401

ZERO = dict.fromkeys(("b0", "b1", "b2", "a1", "a2"), 0.0)
XOVER = "outputs.0.crossover.0.compiled.0"  # Shakers' LR4 low-pass, on


def shadow(session, preset=0):
    state = session.writer.state(preset)
    return dict(state.applied), state.desired, dict(state.failed)


async def drifted_report(session, bridge, reader_factory):
    """Shakers' gain changed on the device, its delay never confirmed (and different), its crossover reads zeros."""
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    session.writer.invalidate(0, ["outputs.0.delay_ms"])
    bridge.memory[M2X4HD_MAP.output_delay[0]] = f32(0.0)
    biquad(bridge.memory, M2X4HD_MAP.crossover_biquad(0, 0, 0), ZERO)
    return await svc.verify_device(session, reader_factory=reader_factory)


# --- refusal ------------------------------------------------------------------------------------------------------


async def test_all_zero_filters_are_refused(reads, bridge, reader_factory, fake):  # noqa: F811
    before = reads.desired(0).outputs[0].crossover[0].compiled[0]
    report = await drifted_report(reads, bridge, reader_factory)
    assert [i.path for i in report.refused] == [XOVER]
    assert XOVER not in {i.path for i in report.acceptable}
    assert report.as_json()["refused"] == [XOVER]

    accepted = await svc.accept_device_values(reads, report, [XOVER, "outputs.0.gain_db"])  # asked for explicitly
    assert accepted == ["outputs.0.gain_db"]
    assert reads.desired(0).outputs[0].crossover[0].compiled[0] == before
    assert fake.configs == []


async def test_zero_filter_detection():
    item = svc.VerifyItem(XOVER, svc.ItemStatus.DRIFT, actual={**ZERO, "a1": 1.5})
    assert svc.zero_filter(item)
    assert not svc.zero_filter(svc.VerifyItem(XOVER, svc.ItemStatus.OK, actual=ZERO))  # matches what was sent
    assert not svc.zero_filter(svc.VerifyItem(XOVER, svc.ItemStatus.DRIFT, actual={**ZERO, "b0": 1.0}))
    assert not svc.zero_filter(svc.VerifyItem("outputs.0.gain_db", svc.ItemStatus.DRIFT, actual=0.0))


# --- undo ---------------------------------------------------------------------------------------------------------


async def test_undo_restores_the_shadow_and_schedules_no_writes(reads, bridge, reader_factory, fake, repo):  # noqa: F811
    report = await drifted_report(reads, bridge, reader_factory)
    applied, desired, failed = shadow(reads)
    statuses = reads.sync_status(0)
    items = list(report.items)

    accepted = await svc.accept_device_values(reads, report)
    assert accepted == ["outputs.0.delay_ms", "outputs.0.gain_db"]
    assert reads.desired(0).outputs[0].gain_db == -6.02
    token = reads.accept_undo.token

    result = await svc.undo_accept(reads, 0, token)
    # Plus the designs the accept recognized on the way (the shaker doc stores bare coefficients).
    assert {"outputs.0.delay_ms", "outputs.0.gain_db"} <= set(result.restored)
    assert result.skipped == [] and result.unknown == []
    assert shadow(reads) == (applied, desired, failed)
    assert reads.sync_status(0) == statuses
    assert reads.writer.pending_paths(0) == []  # the writer queue is empty: nothing will be sent
    assert fake.configs == []
    assert reads.accept_undo is None
    assert report.items == items and report.accepted == []  # the check result is back too
    [rev, *_] = await repo.list_revisions(reads.serial, 0)
    assert rev.source == "verify" and "undid" in rev.summary

    with pytest.raises(UndoUnavailable):
        await svc.undo_accept(reads, 0, token)  # only once


async def test_undo_leaves_settings_changed_since_alone(reads, bridge, reader_factory, fake):  # noqa: F811
    report = await drifted_report(reads, bridge, reader_factory)
    await svc.accept_device_values(reads, report)
    await reads.set_value(0, "outputs.0.gain_db", 3.0, confirm=True)  # someone edits it after the accept

    result = await svc.undo_accept(reads, 0)
    assert "outputs.0.delay_ms" in result.restored and "outputs.0.gain_db" not in result.restored
    assert result.skipped == ["outputs.0.gain_db"]
    doc = reads.desired(0)
    assert doc.outputs[0].gain_db == 3.0  # the later edit stands
    assert doc.outputs[0].delay_ms == 9.0
    assert reads.writer.pending_paths(0) == ["outputs.0.gain_db"]  # only the foreign edit's own write


async def test_undo_skips_a_setting_forgotten_since(reads, bridge, reader_factory):  # noqa: F811
    report = await drifted_report(reads, bridge, reader_factory)
    await svc.accept_device_values(reads, report)
    svc.mark_drift_unknown(reads, 0, ["outputs.0.gain_db"])  # the shadow changed, the desired value didn't
    result = await svc.undo_accept(reads, 0)
    assert result.skipped == ["outputs.0.gain_db"]
    assert reads.writer.pending_paths(0) == []


async def test_undo_restores_a_peq_band_as_a_whole(reads, bridge, reader_factory, fake):  # noqa: F811
    biquad(bridge.memory, M2X4HD_MAP.input_peq[0][3], peaking(40.0, 2.0, -3.0).model_dump())
    report = await svc.verify_device(reads, reader_factory=reader_factory)
    assert {i.path for i in report.acceptable} == {"inputs.0.peq.3.compiled"}
    band = reads.desired(0).inputs[0].peq[3]

    await svc.accept_device_values(reads, report)
    changed = reads.desired(0).inputs[0].peq[3]
    assert changed.compiled != band.compiled and changed.design.gain_db == pytest.approx(-3.0)

    result = await svc.undo_accept(reads, 0)
    assert "inputs.0.peq.3" in result.restored
    assert reads.desired(0).inputs[0].peq[3] == band  # coefficients and design together
    assert reads.writer.pending_paths(0) == [] and fake.configs == []


async def test_undo_expires(reads, bridge, reader_factory, monkeypatch):  # noqa: F811
    report = await drifted_report(reads, bridge, reader_factory)
    await svc.accept_device_values(reads, report)
    monkeypatch.setattr(session_mod, "ACCEPT_UNDO_S", -1.0)
    with pytest.raises(UndoUnavailable) as exc:
        await svc.undo_accept(reads, 0)
    assert exc.value.code == "expired"
    assert reads.desired(0).outputs[0].gain_db == -6.02


async def test_undo_needs_the_matching_accept(reads, bridge, reader_factory):  # noqa: F811
    with pytest.raises(UndoUnavailable):
        await svc.undo_accept(reads, 0)
    report = await drifted_report(reads, bridge, reader_factory)
    await svc.accept_device_values(reads, report, ["outputs.0.gain_db"])
    with pytest.raises(UndoUnavailable):
        await svc.undo_accept(reads, 0, "not-the-token")
    with pytest.raises(UndoUnavailable):
        await svc.undo_accept(reads, 1)  # another preset


def test_writer_restore_never_queues_a_write(reads):  # noqa: F811
    """A restored shadow value that differs from the restored desired value would be a write: it becomes unknown."""
    state = reads.writer.state(0)
    doc = state.desired
    dropped = reads.writer.restore(0, doc, {"outputs.0.gain_db": 1.0, "outputs.1.gain_db": -6.0}, unknown=["x.y"])
    assert dropped == ["outputs.0.gain_db"]
    assert "outputs.0.gain_db" not in state.applied
    assert state.applied["outputs.1.gain_db"] == -6.0
    assert reads.writer.pending_paths(0) == []
    reads.writer.restore(0, doc, {"outputs.0.gain_db": 1.0}, failed={"outputs.0.gain_db": "rejected"})
    assert state.applied["outputs.0.gain_db"] == 1.0 and state.failed["outputs.0.gain_db"] == "rejected"
    assert reads.writer.pending_paths(0) == []  # a failed path is not retried by itself
