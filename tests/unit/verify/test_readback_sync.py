"""Readback feeding the shadow: re-confirming after a reconnect, seeding an adopt with "trust", and accepting the
device values of a verify report. All on a real session over the fake frame bridge; nothing is ever written."""

import numpy as np
import pytest

from app.daemon.rawframes import M2X4HD_MAP
from app.dsp.design import highpass2, lowpass2
from app.model.config_doc import default_config_doc
from app.model.device_profile import M2X4HD
from app.sync import verify as svc
from app.sync.session import DesiredValidationError

from ..conftest import SERIAL
from ..session.conftest import drain
from .conftest import f32, memory_from
from .test_verify import shaker  # noqa: F401

LP = lowpass2(100.0, 0.7071)
HP = highpass2(100.0, 0.7071)
UNREADABLE = ("inputs.0.routing.0.enabled", "outputs.0.crossover.0.bypass", "inputs.0.peq.3.bypass")


def biquad(mem: dict[int, bytes], addr: int, coefficients: dict[str, float]) -> None:
    for k, key in enumerate(("b0", "b1", "b2", "a1", "a2")):
        mem[addr + k] = f32(coefficients[key])


def f32_dict(bq) -> dict[str, float]:
    return {k: float(np.float32(v)) for k, v in bq.model_dump().items() if k in ("b0", "b1", "b2", "a1", "a2")}


def factory_memory() -> dict[int, bytes]:
    """A factory 2x4HD: flat gains, all-zero PEQ biquads, a 100 Hz LP+HP crossover on Out 3/4, -30 dBFS thresholds."""
    mem = memory_from(default_config_doc(M2X4HD))
    zero = dict.fromkeys(("b0", "b1", "b2", "a1", "a2"), 0.0)
    for table in (M2X4HD_MAP.input_peq, M2X4HD_MAP.output_peq):
        for bands in table:
            for addr in bands:
                biquad(mem, addr, zero)
    for n in (2, 3):
        for b in (0, 1):
            biquad(mem, M2X4HD_MAP.crossover_biquad(n, 0, b), LP.model_dump())
            biquad(mem, M2X4HD_MAP.crossover_biquad(n, 1, b), HP.model_dump())
    for n in range(4):
        mem[M2X4HD_MAP.compressor_threshold[n]] = f32(-30.0)
    return mem


@pytest.fixture
def reads(shaker, reader_factory):  # noqa: F811
    shaker.raw_reader_factory = reader_factory
    return shaker


async def reconnect(session, *, usb_before, usb_after) -> None:
    usb = iter([usb_before, usb_after])
    session.usb_identity = lambda: next(usb)
    await session._on_monitor_connected(False)  # the first connect records the USB identity
    await session._on_monitor_connected(True)
    await drain(session)


# --- reconnect (sync-1) -------------------------------------------------------------------------------------------


async def test_daemon_restart_keeps_the_whole_shadow(reads, fake, repo):
    """Same USB device number: only minidspd restarted. Readable settings are re-confirmed, the rest is kept."""
    await reconnect(reads, usb_before=((1, 5),), usb_after=((1, 5),))
    assert set(reads.sync_status(0).values()) == {"synced"}
    assert fake.configs == []
    report = svc.last_report(SERIAL)
    assert report.source == "reconnect" and report.status == "ok"
    messages = [e.message for e in await repo.list_events(kind="reconnect")]
    assert any("unreadable setting(s) kept" in m for m in messages)


async def test_possible_power_cycle_forgets_only_the_unreadable_settings(reads, fake):
    await reconnect(reads, usb_before=((1, 5),), usb_after=((1, 6),))  # re-enumerated
    status = reads.sync_status(0)
    assert status["outputs.0.gain_db"] == "synced"
    assert status["outputs.0.crossover.0.compiled.0"] == "synced"
    assert status["inputs.0.peq.3.compiled"] == "synced"
    for path in UNREADABLE:
        assert status[path] == "unknown"
    assert fake.configs == []


async def test_without_a_usb_identity_a_power_cycle_is_assumed(reads):
    await reconnect(reads, usb_before=None, usb_after=None)  # external daemon: can't tell
    status = reads.sync_status(0)
    assert status["outputs.0.gain_db"] == "synced"
    assert status["inputs.0.routing.0.enabled"] == "unknown"


async def test_reconnect_reconfirms_unknown_paths_and_forgets_drifted_ones(reads, bridge):
    reads.writer.invalidate(0, ["outputs.1.gain_db"])  # unknown, but the device holds the desired value
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)  # changed behind the app's back
    await reconnect(reads, usb_before=((1, 5),), usb_after=((1, 5),))
    status = reads.sync_status(0)
    assert status["outputs.1.gain_db"] == "synced"
    assert reads.writer.state(0).applied["outputs.1.gain_db"] == -6.0
    assert status["outputs.0.gain_db"] == "unknown"
    assert svc.last_report(SERIAL).status == "drift"


async def test_reconnect_without_a_readback_marks_the_preset_unknown(reads, bridge, repo):
    bridge.fail = OSError("refused")
    await reconnect(reads, usb_before=((1, 5),), usb_after=((1, 5),))
    assert set(reads.sync_status(0).values()) == {"unknown"}
    messages = [e.message for e in await repo.list_events(kind="reconnect")]
    assert any("could not be read back" in m and "marked unknown" in m for m in messages)


# --- adopt "trust" (sync-2) ---------------------------------------------------------------------------------------


async def test_trusting_the_defaults_reads_the_factory_values(reads, bridge, fake, reader_factory):
    bridge.memory.clear()
    bridge.memory.update(factory_memory())
    result = await reads.adopt("trust", confirm_defaults=True)
    assert fake.configs == []
    assert result.read_back > 0 and result.read_error is None
    doc = reads.desired(0)
    assert doc.inputs[0].peq[0].compiled.model_dump() == dict.fromkeys(("b0", "b1", "b2", "a1", "a2"), 0.0)
    assert doc.inputs[0].peq[0].bypass is True  # unreadable: from the defaults
    assert doc.outputs[2].crossover[0].compiled[0].model_dump() == f32_dict(LP)
    assert doc.outputs[3].crossover[1].compiled[1].model_dump() == f32_dict(HP)
    assert doc.outputs[0].compressor.threshold_db == -30.0
    assert set(reads.sync_status(0).values()) == {"synced"}

    report = await svc.verify_device(reads, reader_factory=reader_factory)
    assert report.status == "ok" and report.counts["drift"] == 0 and report.counts["unknown"] == 0


async def test_script_trust_reads_the_paths_the_script_does_not_set(reads, bridge):
    bridge.memory.clear()
    bridge.memory.update(factory_memory())
    result = await reads.adopt("trust", script="output 0 gain -- -3\noutput 1 peq 0 bypass off\n")
    status = reads.sync_status(0)
    assert status["outputs.2.crossover.0.compiled.0"] == "synced"  # read from the device
    assert status["outputs.1.peq.0.bypass"] == "synced"  # set by the script (unreadable)
    assert status["outputs.0.peq.0.bypass"] == "unknown"  # unreadable and not in the script
    assert reads.desired(0).outputs[0].gain_db == 0.0  # the device reads 0 dB, not the script's -3
    assert result.unknown == sum(1 for s in status.values() if s == "unknown")


async def test_a_gain_reading_that_is_not_a_value_stays_unknown(reads, bridge):
    bridge.memory[M2X4HD_MAP.output_gain[1]] = f32(svc.GAIN_UNDERFLOW_READBACK_DB)
    await reads.adopt("trust", doc=reads.desired(0), trust_all=True)
    assert reads.sync_status(0)["outputs.1.gain_db"] == "unknown"
    assert reads.desired(0).outputs[1].gain_db == -6.0  # not seeded with the bogus +6.29 dB


async def test_an_inactive_preset_is_trusted_without_reads(reads, bridge):
    result = await reads.adopt("trust", preset=1, confirm_defaults=True)
    assert bridge.requests == []
    assert result.read_back == 0 and result.read_error is None
    assert set(reads.sync_status(1).values()) == {"synced"}


async def test_trust_falls_back_when_the_readback_fails(reads, bridge, repo):
    bridge.fail = OSError("refused")
    result = await reads.adopt("trust", confirm_defaults=True)
    assert result.read_back == 0 and "unreachable" in result.read_error
    assert set(reads.sync_status(0).values()) == {"synced"}
    await drain(reads)
    assert any("could not be read back" in e.message for e in await repo.list_events(kind="adopt"))


# --- accept device values (sync-3) --------------------------------------------------------------------------------


async def test_accepting_device_values_confirms_them_without_writing(reads, bridge, fake, repo, reader_factory):
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)  # drift
    reads.writer.invalidate(0, ["outputs.0.delay_ms"])
    bridge.memory[M2X4HD_MAP.output_delay[0]] = f32(0.0)  # never confirmed and different (0 samples)
    report = await svc.verify_device(reads, reader_factory=reader_factory)
    assert report.status == "drift" and len(report.mismatched) == 1
    assert {i.path for i in report.acceptable} == {"outputs.0.gain_db", "outputs.0.delay_ms"}

    accepted = await svc.accept_device_values(reads, report)
    assert accepted == ["outputs.0.delay_ms", "outputs.0.gain_db"]
    assert fake.configs == []
    doc = reads.desired(0)
    assert doc.outputs[0].gain_db == -6.02 and doc.outputs[0].delay_ms == 0.0
    status = reads.sync_status(0)
    assert status["outputs.0.gain_db"] == "synced" and status["outputs.0.delay_ms"] == "synced"
    assert report.status == "ok" and report.accepted == accepted
    [rev, *_] = await repo.list_revisions(SERIAL, 0)
    assert rev.source == "verify"

    again = await svc.verify_device(reads, reader_factory=reader_factory)
    assert again.status == "ok"


async def test_accepting_a_value_that_matches_desired_keeps_the_desired_value(reads, bridge, reader_factory):
    reads.writer.invalidate(0, ["outputs.0.crossover.0.compiled.0"])
    before = reads.desired(0)
    report = await svc.verify_device(reads, reader_factory=reader_factory)
    assert await svc.accept_device_values(reads, report) == ["outputs.0.crossover.0.compiled.0"]
    assert reads.desired(0) == before  # full-precision coefficients and the recognized design survive
    assert reads.sync_status(0)["outputs.0.crossover.0.compiled.0"] == "synced"


async def test_accepting_a_gain_above_a_ceiling_is_refused(reads, bridge, reader_factory):
    await reads.set_channel_meta("output", 1, name="Sub", max_gain_db=0.0)
    bridge.memory[M2X4HD_MAP.output_gain[1]] = f32(5.7448344)  # +6 dB written elsewhere
    report = await svc.verify_device(reads, reader_factory=reader_factory)
    with pytest.raises(DesiredValidationError):
        await svc.accept_device_values(reads, report)
    assert reads.desired(0).outputs[1].gain_db == -6.0
