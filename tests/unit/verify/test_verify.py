"""Verify service: a real session and shadow, read back through the fake frame bridge."""

import asyncio
import struct
from datetime import datetime, time

import numpy as np
import pytest

from app.bus import LOG
from app.daemon.client import DaemonUnavailable
from app.daemon.rawframes import M2X4HD_MAP, RawFrameTimeout, parse_float_data
from app.dsp.design import peaking
from app.model.config_doc import default_config_doc, set_path
from app.model.device_profile import M2X4HD
from app.model.master import DaemonDevice, MasterStatus
from app.sync import verify as svc
from app.sync.session import PresetSwitchInProgress, SessionManager, SessionState

from .conftest import LR4_80_LP, RESP_BLOCK_1C, RESP_DELAYS, RESP_GATES, f32, memory_from, u32

LR4 = dict(zip(("b0", "b1", "b2", "a1", "a2"), LR4_80_LP, strict=True))


def shaker_doc():
    """Out 1 like the shakers on the Pi (+12 dB, 9 ms, LR4 80 Hz), Out 2 at -6 dB, Outs 3/4 muted, a PEQ on In 1."""
    doc = default_config_doc(M2X4HD)
    peq = peaking(40.0, 2.0, -4.5)
    for path, value in {
        "outputs.0.gain_db": 12.0,
        "outputs.0.delay_ms": 9.0,
        "outputs.0.crossover.0.compiled.0": LR4,
        "outputs.0.crossover.0.bypass": False,
        "outputs.0.compressor.threshold_db": -30.0,
        "outputs.1.gain_db": -6.0,
        "outputs.1.invert": True,
        "outputs.2.mute": True,
        "outputs.3.mute": True,
        "inputs.0.peq.3.compiled": peq.model_dump(),
        "inputs.0.peq.3.bypass": False,
        "inputs.1.gain_db": -1.0,
    }.items():
        doc = set_path(doc, path, value)
    return doc


async def trusted(session, doc):
    """The device holds ``doc`` and the app knows it (adopted with "trust")."""
    session.writer.set_desired(0, doc)
    session.writer.trust(0)
    session.adoption = {"mode": "trust", "preset": 0}


@pytest.fixture
async def shaker(make_session, bridge):
    s = await make_session(start=False)
    doc = shaker_doc()
    await trusted(s, doc)
    bridge.memory.update(memory_from(doc))
    return s


async def run(session, reader_factory, **kwargs):
    return await svc.verify_device(session, reader_factory=reader_factory, **kwargs)


def by_path(report):
    return {i.path: i for i in report.items}


# --- clean device -------------------------------------------------------------------------------------------------


async def test_trusted_device_verifies_clean(shaker, reader_factory, bridge, fake):
    report = await run(shaker, reader_factory)
    assert report.status == "ok"
    assert report.counts["drift"] == 0
    assert report.counts["unknown"] == 0
    items = by_path(report)
    gain = items["outputs.0.gain_db"]
    assert gain.status == svc.ItemStatus.OK
    assert gain.actual == pytest.approx(11.765434)  # quantized and biased low, within the tolerance
    assert items["outputs.0.delay_ms"].raw == 864
    assert items["outputs.0.delay_ms"].actual == pytest.approx(9.0)
    assert items["outputs.1.invert"].actual is True
    assert items["outputs.2.mute"].actual is True
    assert items["outputs.0.crossover.0.compiled.0"].status == svc.ItemStatus.OK
    assert items["inputs.0.peq.3.compiled"].status == svc.ItemStatus.OK
    assert items["outputs.0.compressor.threshold_db"].actual == -30.0
    assert fake.configs == []  # read-only
    assert bridge.urls == ["ws://127.0.0.1:1/devices/0/ws"]


async def test_unreadable_settings_are_reported_not_compared(shaker, reader_factory):
    items = by_path(await run(shaker, reader_factory))
    unverifiable = {p: i.note for p, i in items.items() if i.status == svc.ItemStatus.UNVERIFIABLE}
    assert unverifiable["inputs.0.routing.0.enabled"] == svc.UNVERIFIABLE_ROUTING
    assert unverifiable["inputs.1.routing.3.gain_db"] == svc.UNVERIFIABLE_ROUTING_GAIN  # own registers, not status
    assert unverifiable["inputs.0.peq.3.bypass"] == svc.UNVERIFIABLE_BYPASS
    assert unverifiable["outputs.0.crossover.0.bypass"] == svc.UNVERIFIABLE_BYPASS
    assert unverifiable["outputs.0.compressor.bypass"] == svc.UNVERIFIABLE_COMPRESSOR_BYPASS
    for field in ("ratio", "attack_ms", "release_ms"):
        assert unverifiable[f"outputs.0.compressor.{field}"] == svc.UNVERIFIABLE_COMPRESSOR
    readable = {p for p, i in items.items() if i.status != svc.ItemStatus.UNVERIFIABLE}
    assert not any(".routing." in p or p.endswith(".bypass") for p in readable)


async def test_reads_are_small_aligned_and_cover_one_preset(shaker, reader_factory, bridge):
    report = await run(shaker, reader_factory)
    # 5 scalar blocks; 60 PEQ biquads and 32 crossover biquads read two at a time.
    assert report.reads == len(bridge.requests) == 5 + 30 + 16
    for addr, count in bridge.requests:
        assert count <= 14
        if addr >= 8280:
            assert (addr - 8280) % 5 == 0 and count in (5, 10)


async def test_writer_is_paused_while_reading(shaker, reader_factory, bridge):
    seen = []
    bridge.on_request = lambda *_: seen.append(shaker.writer._io_lock.locked())
    await run(shaker, reader_factory)
    assert seen and all(seen)


# --- drift --------------------------------------------------------------------------------------------------------


async def test_cli_gain_change_flags_exactly_that_path(shaker, reader_factory, bridge):
    """docs/08 Phase 6 acceptance: the Shakers gain changed behind the app's back."""
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)  # `minidsp output 0 gain -- -6`
    report = await run(shaker, reader_factory)
    assert report.status == "drift"
    assert [i.path for i in report.drifted] == ["outputs.0.gain_db"]
    item = report.drifted[0]
    assert item.expected == 12.0
    assert item.actual == pytest.approx(-6.0206)


@pytest.mark.parametrize(
    ("addr", "raw", "path", "note"),
    [
        (M2X4HD_MAP.output_gate[2], u32(2), "outputs.2.mute", None),
        (M2X4HD_MAP.output_gate[0], u32(0), "outputs.0.mute", "unexpected gate register value 0"),
        (M2X4HD_MAP.input_gate[1], u32(1), "inputs.1.mute", None),
        (M2X4HD_MAP.output_delay[0], u32(865), "outputs.0.delay_ms", None),
        (M2X4HD_MAP.output_delay[1], u32(9000), "outputs.1.delay_ms", "above the device maximum"),
        (M2X4HD_MAP.output_invert[1], u32(0), "outputs.1.invert", None),
        (M2X4HD_MAP.output_invert[0], u32(5), "outputs.0.invert", "unexpected polarity register value 5"),
        (M2X4HD_MAP.compressor_threshold[0], f32(-20.0), "outputs.0.compressor.threshold_db", None),
        (M2X4HD_MAP.input_gain[1], f32(6.287154), "inputs.1.gain_db", "below about -100 dB"),
        (M2X4HD_MAP.crossover_biquad(0, 0, 0) + 3, f32(1.99), "outputs.0.crossover.0.compiled.0", None),
        (M2X4HD_MAP.output_peq[2][9], f32(0.5), "outputs.2.peq.9.compiled", None),
    ],
)
async def test_each_readable_kind_detects_drift(shaker, reader_factory, bridge, addr, raw, path, note):
    bridge.memory[addr] = raw
    report = await run(shaker, reader_factory)
    assert [i.path for i in report.drifted] == [path]
    if note:
        assert note in report.drifted[0].note


async def test_gain_tolerance(shaker, reader_factory, bridge):
    addr = M2X4HD_MAP.output_gain[1]  # -6 dB in the shadow
    bridge.memory[addr] = f32(-6.36)
    assert (await run(shaker, reader_factory)).status == "ok"
    bridge.memory[addr] = f32(-6.6)
    assert [i.path for i in (await run(shaker, reader_factory)).drifted] == ["outputs.1.gain_db"]


async def test_gain_reading_above_the_sent_value_is_drift(shaker, reader_factory, bridge):
    """The device's gain quantization only ever reads low (spike 0.2), so a reading above the sent value is drift."""
    addr = M2X4HD_MAP.output_gain[1]  # -6 dB in the shadow
    bridge.memory[addr] = f32(-5.8)
    assert [i.path for i in (await run(shaker, reader_factory)).drifted] == ["outputs.1.gain_db"]
    bridge.memory[addr] = f32(-5.995)  # an accepted reading rounded to 0.01 dB may sit just below the device's value
    assert (await run(shaker, reader_factory)).status == "ok"


async def test_biquads_allow_one_float32_ulp(shaker, reader_factory, bridge):
    addr = M2X4HD_MAP.crossover_biquad(0, 0, 0)
    b0 = np.float32(LR4["b0"])
    bridge.memory[addr] = struct.pack("<f", np.nextafter(b0, np.float32(1)))
    assert (await run(shaker, reader_factory)).status == "ok"
    bridge.memory[addr] = struct.pack("<f", np.nextafter(np.nextafter(b0, np.float32(1)), np.float32(1)))
    assert (await run(shaker, reader_factory)).status == "drift"


# --- unknown shadow paths and marking -----------------------------------------------------------------------------


async def test_paths_unknown_to_the_shadow_compare_with_desired(shaker, reader_factory, bridge):
    shaker.writer.invalidate(0, ["outputs.1.gain_db", "outputs.0.delay_ms"])
    bridge.memory[M2X4HD_MAP.output_delay[0]] = u32(0)
    report = await run(shaker, reader_factory)
    items = by_path(report)
    assert report.status == "mismatch"  # unknown isn't drift, but the delay differs from the desired value
    assert [i.path for i in report.mismatched] == ["outputs.0.delay_ms"]
    assert report.event()["mismatch_paths"] == ["outputs.0.delay_ms"] and report.event()["paths"] == []
    assert items["outputs.1.gain_db"].status == svc.ItemStatus.UNKNOWN
    assert items["outputs.1.gain_db"].matches_desired is True
    assert items["outputs.1.gain_db"].expected == -6.0
    assert items["outputs.0.delay_ms"].matches_desired is False
    assert report.counts["unknown"] == 2


async def test_power_cycle_invalidation_is_not_reported_ok(shaker, reader_factory, bridge, bus):
    """After a reconnect invalidates the active preset, nothing is confirmed: never ``ok``."""
    shaker.writer.invalidate(0)
    report = await run(shaker, reader_factory)
    assert report.counts["ok"] == 0 and report.counts["drift"] == 0 and not report.mismatched
    assert report.status == "unverified"
    assert "nothing verified" in report.summary()

    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(0.0)  # the device lost Out 1's +12 dB
    async with bus.subscribe([svc.VERIFY, LOG]) as sub:
        report = await run(shaker, reader_factory)
        events = [sub.get_nowait() for _ in range(sub.pending())]
    assert report.status == "mismatch" and report.drifted == []
    assert [i.path for i in report.mismatched] == ["outputs.0.gain_db"]
    event = next(e.data for e in events if e.topic == svc.VERIFY)
    assert event["status"] == "mismatch" and event["mismatch"] == 1
    state = svc.drift_state(event)
    assert state["status"] == "mismatch" and state["mismatch_paths"] == ["outputs.0.gain_db"]
    assert next(e.data for e in events if e.topic == LOG)["level"] == "warning"
    assert "1 never-confirmed setting(s) differ" in report.summary()


async def test_mark_unknown_drops_drifted_paths_without_writing(shaker, reader_factory, bridge, fake):
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    report = await run(shaker, reader_factory, mark_unknown=True)
    assert report.marked_unknown == ["outputs.0.gain_db"]
    assert "outputs.0.gain_db" not in shaker.writer.state(0).applied
    assert shaker.sync_status(0)["outputs.0.gain_db"] == "unknown"
    assert fake.configs == []
    # Only drifted, known paths are dropped.
    assert svc.mark_drift_unknown(shaker, 0, ["outputs.0.gain_db", "outputs.9.nope"]) == []


async def test_records_report_log_and_bus_event(shaker, reader_factory, bridge, bus):
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(0.0)
    async with bus.subscribe([svc.VERIFY, LOG]) as sub:
        report = await run(shaker, reader_factory)
        events = [sub.get_nowait() for _ in range(sub.pending())]
    assert svc.last_report(shaker.serial) is report
    verify_events = [e for e in events if e.topic == svc.VERIFY]
    assert verify_events[0].data["status"] == "drift"
    assert verify_events[0].data["paths"] == ["outputs.0.gain_db"]
    assert verify_events[0].serial == shaker.serial
    logs = [e.data for e in events if e.topic == LOG]
    assert logs[0]["kind"] == "verify" and logs[0]["level"] == "warning"
    assert "1 setting(s) differ" in logs[0]["message"]
    data = report.as_json()
    assert data["drift"] == 1 and data["items"][0]["status"] in {"ok", "drift", "unverifiable"}


# --- refusals -----------------------------------------------------------------------------------------------------


async def test_preset_change_during_reads_aborts(shaker, reader_factory, bridge):
    def switch(*_):
        shaker.monitor.update_master(MasterStatus(preset=1))

    bridge.on_request = switch
    with pytest.raises(svc.VerifyError, match="changed"):
        await run(shaker, reader_factory)
    assert svc.last_report(shaker.serial) is None


async def test_preset_change_by_another_client_during_reads_aborts(shaker, reader_factory, bridge, fake):
    """A CLI or front-panel switch reaches the monitor cache only on its poll: the daemon is asked again."""

    def switch(*_):
        fake.master["preset"] = 1  # the daemon has switched; the cached status still says preset 1 (index 0)

    bridge.on_request = switch
    applied = dict(shaker.writer.state(0).applied)
    with pytest.raises(svc.VerifyError, match="changed"):
        await run(shaker, reader_factory, mark_unknown=True)
    assert svc.last_report(shaker.serial) is None
    assert shaker.writer.state(0).applied == applied


async def test_refuses_during_a_preset_switch(shaker, reader_factory):
    shaker.preset_switching = 2
    with pytest.raises(PresetSwitchInProgress):
        await run(shaker, reader_factory)


async def test_unsupported_model(shaker, reader_factory, monkeypatch):
    monkeypatch.setattr(svc, "memory_map_for", lambda _name: None)
    with pytest.raises(svc.VerifyUnsupported):
        await run(shaker, reader_factory)


async def test_bridge_unreachable(shaker, reader_factory, bridge):
    bridge.fail = OSError("connection refused")
    with pytest.raises(DaemonUnavailable):
        await run(shaker, reader_factory)
    assert svc.last_report(shaker.serial) is None


async def test_verifies_the_active_preset(make_session, reader_factory, bridge, fake):
    s = await make_session(start=False)
    doc = set_path(default_config_doc(M2X4HD), "outputs.3.gain_db", -20.0)
    s.writer.set_desired(1, doc)
    s.writer.trust(1)
    bridge.memory.update(memory_from(doc))
    fake.master["preset"] = 1
    s.monitor.update_master(MasterStatus(preset=1))
    report = await run(s, reader_factory)
    assert report.preset == 1
    assert report.status == "ok"
    assert by_path(report)["outputs.3.gain_db"].actual == pytest.approx(-20.205997)


# --- recorded frames ----------------------------------------------------------------------------------------------


def test_recorded_spike_frames_verify_against_the_shaker_shadow():
    blocks = [parse_float_data(m) for m in (RESP_BLOCK_1C, RESP_GATES, RESP_DELAYS)]
    values = {
        "outputs.0.gain_db": 12.0,
        "outputs.0.mute": False,
        "outputs.0.delay_ms": 9.0,
        "outputs.0.compressor.threshold_db": -30.0,
        "outputs.1.gain_db": 0.0,
        "outputs.2.mute": True,
        "outputs.3.delay_ms": 0.0,
    }
    probes, unreadable, reads = svc.plan(values, M2X4HD_MAP)
    assert not unreadable
    items = svc.build_items(values, values, probes, unreadable, blocks, M2X4HD)
    assert {i.path: i.status for i in items} == dict.fromkeys(values, svc.ItemStatus.OK)
    drifted = svc.build_items(values, {**values, "outputs.2.mute": False}, probes, unreadable, blocks, M2X4HD)
    assert [i.path for i in drifted if i.status == svc.ItemStatus.DRIFT] == ["outputs.2.mute"]


@pytest.mark.parametrize(
    "path",
    ["outputs.0.fir.bypass", "outputs.0.gain", "outputs.x.gain_db", "inputs.0.invert", "master.volume", "outputs"],
)
def test_unmapped_paths_are_unverifiable(path):
    assert isinstance(svc.probe_for(path, M2X4HD_MAP), str)


def test_corrupt_shadow_values_count_as_drift():
    probe = svc.Probe("biquad", 8580)
    decoded = svc.decode(probe, bytes(20), M2X4HD)
    assert svc.matches(probe, decoded, {"b0": 0.0}, M2X4HD) is False
    gain = svc.Probe("gain", 28)
    assert svc.matches(gain, svc.decode(gain, f32(0.0), M2X4HD), "loud", M2X4HD) is False
    with pytest.raises(ValueError):
        svc.decode(svc.Probe("meter", 0), bytes(4), M2X4HD)


# --- nightly ------------------------------------------------------------------------------------------------------


@pytest.fixture
def manager(shaker, client, bus, settings):
    m = SessionManager(client, bus, settings)
    m.sessions[shaker.serial] = shaker
    shaker.state = SessionState.CONNECTED
    return m


async def test_verify_all_runs_connected_adopted_devices(manager, shaker, reader_factory, bridge):
    async def verify(session, **kwargs):
        return await svc.verify_device(session, reader_factory=reader_factory, **kwargs)

    results = await svc.verify_all(manager, verify=verify)
    report = results[shaker.serial]
    assert report.source == "nightly" and report.status == "ok"

    shaker.adoption = None
    assert await svc.verify_all(manager, verify=verify) == {}
    shaker.adoption = {"mode": "trust"}
    shaker.state = SessionState.ABSENT
    assert await svc.verify_all(manager, verify=verify) == {}


async def test_verify_all_reports_failures(manager, shaker, bus):
    async def broken(session, **kwargs):
        raise DaemonUnavailable("minidspd unreachable")

    async with bus.subscribe([svc.VERIFY]) as sub:
        results = await svc.verify_all(manager, verify=broken)
        event = sub.get_nowait()
    assert results == {shaker.serial: "minidspd unreachable"}
    assert event.data["status"] == "error"
    assert event.data["error"] == "minidspd unreachable"
    assert svc.drift_state(event.data)["status"] == "error"


async def test_nightly_ok_runs_are_not_persisted(manager, shaker, reader_factory, repo):
    async def verify(session, **kwargs):
        return await svc.verify_device(session, reader_factory=reader_factory, **kwargs)

    await svc.verify_all(manager, verify=verify)
    await asyncio.gather(*shaker._tasks, return_exceptions=True)
    assert [e for e in await repo.list_events(limit=50) if e.kind == "verify"] == []


async def test_nightly_mismatch_is_persisted(manager, shaker, reader_factory, bridge, repo):
    async def verify(session, **kwargs):
        return await svc.verify_device(session, reader_factory=reader_factory, **kwargs)

    shaker.writer.invalidate(0, ["outputs.0.gain_db"])
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(0.0)
    await svc.verify_all(manager, verify=verify)
    await asyncio.gather(*shaker._tasks, return_exceptions=True)
    events = [e for e in await repo.list_events(limit=50) if e.kind == "verify"]
    assert len(events) == 1 and "never-confirmed" in events[0].message


async def test_nightly_loop_runs_at_the_configured_time(manager):
    calls = []
    done = asyncio.Event()

    async def verify(session, **kwargs):
        calls.append(kwargs["source"])
        done.set()
        raise svc.VerifyError("stop")

    task = asyncio.create_task(
        svc.nightly_verify_loop(
            manager, at=time(4, 15), clock=lambda: datetime(2026, 10, 17, 4, 14, 59, 950000), verify=verify
        )
    )
    await asyncio.wait_for(done.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["nightly"]


def test_parse_at():
    assert svc.parse_at(" 04:15 ") == time(4, 15)
    with pytest.raises(ValueError):
        svc.parse_at("25:00")


# --- MQTT drift sensor --------------------------------------------------------------------------------------------


def test_drift_sensor_discovery_and_state():
    topic, config = svc.drift_discovery(
        discovery_prefix="homeassistant/",
        base_topic="minidsp-webui",
        serial=123,
        device={"identifiers": ["minidsp_123"]},
        origin={"name": "minidsp-webui"},
    )
    assert topic == "homeassistant/sensor/minidsp_123/drift/config"
    assert config["state_topic"] == svc.drift_topic("minidsp-webui", 123) == "minidsp-webui/123/verify"
    assert config["unique_id"] == "minidsp_123_drift"
    assert config["options"] == ["ok", "drift", "mismatch", "unverified", "error"]
    assert config["device"] == {"identifiers": ["minidsp_123"]}
    assert config["origin"] == {"name": "minidsp-webui"}
    assert config["availability"][1] == {"topic": "minidsp-webui/123/availability"}

    state = svc.drift_state(
        {"status": "drift", "drift": 1, "paths": ["outputs.0.gain_db"], "preset": 0, "checked_at": "t", "source": "x"}
    )
    assert state == {
        "status": "drift",
        "drift": 1,
        "paths": ["outputs.0.gain_db"],
        "mismatch": None,
        "mismatch_paths": [],
        "preset": 1,
        "checked_at": "t",
        "source": "x",
        "error": None,
    }


# --- fail fast when the device can't be read back -------------------------------------------------------------------


async def test_an_unanswered_first_read_aborts_the_whole_run_at_once(shaker, reader_factory, bridge):
    bridge.drop = 10_000  # a bridge (or mock) that never answers ReadFloats
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(svc.ReadbackUnavailable, match="The device can't be read back: it didn't answer a memory read"):
        await run(shaker, reader_factory)
    assert loop.time() - started < 1.5  # one read and its retry (0.2 s each), not ~50 timeouts
    assert len(bridge.requests) == 2
    assert svc.last_report(shaker.serial) is None

    # Remembered: the next check, and an adopt's read-back, refuse at once without touching the bridge.
    connects = len(bridge.urls)
    started = loop.time()
    with pytest.raises(svc.ReadbackUnavailable):
        await run(shaker, reader_factory)
    with pytest.raises(svc.ReadbackUnavailable):
        await svc.read_back(shaker, {"outputs.0.gain_db": 12.0}, reader_factory=reader_factory)
    assert loop.time() - started < 0.1
    assert len(bridge.urls) == connects and len(bridge.requests) == 2

    # A reconnect tries again; a run that reads clears the hold.
    bridge.drop = 0
    report = await svc.reconfirm(shaker, power_cycle=False, reader_factory=reader_factory)
    assert report.status == "ok"
    assert (await run(shaker, reader_factory)).status == "ok"


async def test_the_hold_expires(shaker, reader_factory, bridge, monkeypatch):
    monkeypatch.setattr(svc, "UNREADABLE_HOLD_S", 0.0)
    bridge.drop = 2
    with pytest.raises(svc.ReadbackUnavailable):
        await run(shaker, reader_factory)
    assert (await run(shaker, reader_factory)).status == "ok"


async def test_the_mock_is_named(shaker, reader_factory, bridge):
    shaker.device = DaemonDevice.model_validate({"url": "mock:?serial=911111", "product_name": "2x4HD"})
    bridge.drop = 10_000
    with pytest.raises(svc.ReadbackUnavailable, match="^The mock device can't be read back"):
        await run(shaker, reader_factory)


async def test_a_slow_device_is_stopped_by_the_read_budget(shaker, reader_factory, bridge, monkeypatch):
    monkeypatch.setattr(svc, "READ_BUDGET_S", 0.5)

    def answer_only_retries(addr, count):
        bridge.drop = 1 if len(bridge.requests) % 2 else 0  # every read waits out one 0.2 s timeout

    bridge.on_request = answer_only_retries
    with pytest.raises(svc.ReadbackUnavailable, match=r"reading it took longer than 0.5 s \(\d+ of \d+ reads done\)"):
        await run(shaker, reader_factory)
    assert len(bridge.requests) < 10  # the plan has ~50 reads


async def test_a_timeout_after_answered_reads_is_a_glitch_not_unreadable(shaker, reader_factory, bridge):
    def drop_the_fourth(addr, count):
        if len(bridge.requests) == 4:
            bridge.drop = 2  # the read and its retry

    bridge.on_request = drop_the_fourth
    with pytest.raises(RawFrameTimeout):
        await run(shaker, reader_factory)
    bridge.on_request = None
    assert (await run(shaker, reader_factory)).status == "ok"  # not held


async def test_api_reports_unreadable(shaker, reader_factory, bridge):
    from app.api.v1.verify import error_response

    bridge.drop = 10_000
    with pytest.raises(svc.ReadbackUnavailable) as caught:
        await run(shaker, reader_factory)
    assert error_response(caught.value)[:2] == (503, "unreadable")
