import asyncio
import copy
import logging
import time
from typing import Any

import pytest

from app.bus import SYNC, Bus
from app.daemon.client import DaemonRejected, DaemonUnavailable, DeviceNotFound
from app.model.config_doc import default_config_doc, set_path
from app.sync.writer import DeviceWriter, SyncStatus, error_text
from tests.unit.sync.conftest import FIR_PROFILE, PEAK, PROFILE, doc_with, trusted

SERIAL = 911111
DEFAULT = default_config_doc(PROFILE)

SPIKE_BODY = (
    'the error: \'FormattedError { message: "Internal error", error: InternalError(Internal error\n\nCaused by:\n'
    "    Delay should be within [0, 80] ms was 8006) }' couldn't be serialized as json: Error(\"cannot serialize "
    'tagged newtype variant Error::InternalError containing a string", line: 0, column: 0)'
)
RANGE_BODY = (
    'the error: \'FormattedError { message: "Specified channel or peq is out of range", error: InternalError('
    "Specified channel or peq is out of range) }' couldn't be serialized as json"
)


class FakeDaemon:
    """Records config posts. ``failures`` is a queue of exceptions raised by the next posts."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.times: list[float] = []
        self.active = 0
        self.failures: list[BaseException] = []
        self.fail_when = None  # optional predicate(body) -> exception | None
        self.latency = 0.0
        self.guard_error: BaseException | None = None
        self.guard_calls = 0
        self.on_post = None

    async def post(self, body: dict[str, Any]) -> None:
        self.calls.append(copy.deepcopy(body))
        self.times.append(time.monotonic())
        if self.on_post is not None:
            self.on_post(body)
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.failures:
            raise self.failures.pop(0)
        if self.fail_when is not None and (exc := self.fail_when(body)) is not None:
            raise exc

    async def active_preset(self) -> int:
        self.guard_calls += 1
        if self.guard_error is not None:
            raise self.guard_error
        return self.active


def rejected(body: str = RANGE_BODY) -> DaemonRejected:
    return DaemonRejected(500, body)


@pytest.fixture
def daemon() -> FakeDaemon:
    return FakeDaemon()


@pytest.fixture
async def make_writer(daemon):
    writers: list[DeviceWriter] = []

    def make(profile=PROFILE, **kwargs) -> DeviceWriter:
        options = {"coalesce_ms": 20, "backoff_min_s": 0.01, "backoff_max_s": 0.04} | kwargs
        writer = DeviceWriter(SERIAL, profile, post_config=daemon.post, active_preset=daemon.active_preset, **options)
        writers.append(writer)
        writer.start()
        return writer

    yield make
    for writer in writers:
        await writer.stop()


async def settle(writer: DeviceWriter, preset: int = 0, timeout: float = 3.0):
    return await writer.wait_idle(preset, timeout=timeout)


def attach_trusted(writer: DeviceWriter, preset: int = 0, doc=DEFAULT):
    return writer.attach(preset, doc, applied=trusted(doc))


# --- coalescing -------------------------------------------------------------------------------------------------


async def test_rapid_edits_coalesce_to_one_write_with_the_last_value(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    doc = DEFAULT
    for gain in (10.0, 10.5, 11.0, 11.5, 12.0):
        doc = set_path(doc, "outputs.0.gain_db", gain)
        writer.set_desired(0, doc)
    result = await settle(writer)
    assert result.ok
    assert daemon.calls == [{"outputs": [{"index": 0, "gain": 12.0}]}]
    assert writer.status(0)["outputs.0.gain_db"] is SyncStatus.SYNCED
    assert writer.writes_sent == 1
    await writer.stop()


async def test_slider_drag_sends_few_writes_and_ends_on_the_final_value(daemon, make_writer):
    daemon.latency = 0.03  # each USB round trip is slow
    writer = make_writer(coalesce_ms=50)
    attach_trusted(writer)
    doc = DEFAULT
    steps = [10 + i * 0.1 for i in range(21)]  # 10 -> 12 dB over ~400 ms
    for gain in steps:
        doc = set_path(doc, "outputs.0.gain_db", round(gain, 2))
        writer.set_desired(0, doc)
        await asyncio.sleep(0.02)
    await settle(writer)
    assert 1 <= len(daemon.calls) <= 7
    assert daemon.calls[-1] == {"outputs": [{"index": 0, "gain": 12.0}]}
    assert writer.state(0).applied["outputs.0.gain_db"] == 12.0
    await writer.stop()


async def test_gather_window_delays_the_first_write(daemon, make_writer):
    writer = make_writer(coalesce_ms=80)
    attach_trusted(writer)
    start = time.monotonic()
    writer.set_desired(0, set_path(DEFAULT, "outputs.1.mute", True))
    await settle(writer)
    assert daemon.times[0] - start >= 0.075
    await writer.stop()


async def test_reverting_within_the_window_sends_nothing(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", 12.0))
    writer.set_desired(0, DEFAULT)
    result = await settle(writer)
    await asyncio.sleep(0.05)
    assert result.ok and daemon.calls == []
    await writer.stop()


async def test_edit_during_inflight_write_is_sent_next(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    holder = {}

    def edit_once(body):
        if "doc" not in holder:
            holder["doc"] = set_path(set_path(DEFAULT, "outputs.0.gain_db", 1.0), "outputs.0.gain_db", 2.0)
            writer.set_desired(0, holder["doc"])

    daemon.on_post = edit_once
    daemon.latency = 0.02
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", 1.0))
    await asyncio.sleep(0.01)
    await settle(writer)
    await asyncio.sleep(0.1)
    await settle(writer)
    assert daemon.calls == [
        {"outputs": [{"index": 0, "gain": 1.0}]},
        {"outputs": [{"index": 0, "gain": 2.0}]},
    ]
    assert writer.status(0)["outputs.0.gain_db"] is SyncStatus.SYNCED
    await writer.stop()


async def test_unknown_paths_are_only_written_when_touched(daemon, make_writer):
    writer = make_writer()
    writer.attach(0, DEFAULT)  # adopt "mark everything unknown"
    assert set(writer.status(0).values()) == {SyncStatus.UNKNOWN}
    writer.notify(0)
    result = await settle(writer)
    assert result.ok and daemon.calls == []

    writer.set_desired(0, set_path(DEFAULT, "outputs.2.gain_db", -6.0))
    await settle(writer)
    assert daemon.calls == [{"outputs": [{"index": 2, "gain": -6.0}]}]
    writer.notify(0, ["outputs.3.invert"])  # touched without changing: still written once
    await settle(writer)
    assert daemon.calls[-1] == {"outputs": [{"index": 3, "invert": False}]}
    status = writer.status(0)
    assert status["outputs.2.gain_db"] is SyncStatus.SYNCED
    assert status["outputs.3.invert"] is SyncStatus.SYNCED
    assert status["outputs.3.gain_db"] is SyncStatus.UNKNOWN
    await writer.stop()


async def test_trust_writes_nothing(daemon, make_writer):
    writer = make_writer()
    doc = doc_with({"outputs.0.gain_db": 12.0, "outputs.0.delay_ms": 9.0})
    writer.attach(0, doc)
    writer.trust(0)
    result = await settle(writer)
    assert result.ok and daemon.calls == []
    assert set(writer.status(0).values()) == {SyncStatus.SYNCED}
    await writer.stop()


async def test_invalidate_marks_unknown_without_writing(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    writer.invalidate(0, ["outputs.0.gain_db"])
    assert writer.status(0)["outputs.0.gain_db"] is SyncStatus.UNKNOWN
    writer.invalidate(0)
    assert set(writer.status(0).values()) == {SyncStatus.UNKNOWN}
    await asyncio.sleep(0.05)
    assert daemon.calls == []
    await writer.stop()


# --- preset guard -----------------------------------------------------------------------------------------------


async def test_inactive_preset_edits_are_staged_then_flushed_on_kick(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer, 0)
    attach_trusted(writer, 2)
    writer.set_desired(2, set_path(DEFAULT, "outputs.1.delay_ms", 1.5))
    result = await settle(writer, 2)
    assert result.staged and result.pending == ("outputs.1.delay_ms",)
    assert daemon.calls == []
    assert writer.status(2)["outputs.1.delay_ms"] is SyncStatus.PENDING
    assert writer.summary(2)["staged"] is True

    daemon.active = 2
    writer.kick(2)
    result = await settle(writer, 2)
    assert result.ok
    assert daemon.calls == [{"outputs": [{"index": 1, "delay": {"secs": 0, "nanos": 1_500_000}}]}]
    await writer.stop()


async def test_guard_is_checked_before_every_call_and_aborts_on_mismatch(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)

    def switch_preset(body):
        daemon.active = 1  # someone switched presets on the remote mid-flush

    daemon.on_post = switch_preset
    writer.set_desired(0, doc_with({"outputs.0.gain_db": -1.0, "outputs.1.gain_db": -2.0}))
    result = await settle(writer)
    assert result.staged
    assert daemon.calls == [{"outputs": [{"index": 0, "gain": -1.0}]}]
    assert result.pending == ("outputs.1.gain_db",)
    assert daemon.guard_calls >= 3  # cycle + one per call attempted
    await writer.stop()


async def test_guard_errors_count_as_attempts_without_marking_unknown(daemon, make_writer):
    writer = make_writer(max_attempts=3)
    attach_trusted(writer)
    daemon.guard_error = DaemonUnavailable("minidspd unreachable")
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -1.0))
    result = await settle(writer)
    assert daemon.calls == []
    assert result.failed == {"outputs.0.gain_db": "minidspd unreachable"}
    status = writer.status(0)
    assert status["outputs.0.gain_db"] is SyncStatus.FAILED
    assert status["outputs.0.mute"] is SyncStatus.SYNCED  # nothing was sent, so nothing became unknown

    daemon.guard_error = None
    writer.kick()
    assert (await settle(writer)).ok
    assert daemon.calls == [{"outputs": [{"index": 0, "gain": -1.0}]}]
    await writer.stop()


# --- failures ---------------------------------------------------------------------------------------------------


async def test_failed_call_marks_its_paths_unknown_and_retries_the_whole_payload(daemon, make_writer):
    writer = make_writer(backoff_min_s=0.2, backoff_max_s=0.2)
    attach_trusted(writer)
    daemon.failures = [rejected()]
    doc = doc_with({"outputs.2.gain_db": -3.0, "outputs.2.delay_ms": 1.5})
    writer.set_desired(0, doc)
    await asyncio.sleep(0.1)  # first attempt failed, backing off
    state = writer.state(0)
    assert "outputs.2.gain_db" not in state.applied and "outputs.2.delay_ms" not in state.applied
    status = writer.status(0)
    assert status["outputs.2.gain_db"] is SyncStatus.PENDING
    assert status["outputs.2.mute"] is SyncStatus.SYNCED
    assert writer.summary(0)["error"] == "Specified channel or peq is out of range"

    result = await settle(writer)
    assert result.ok
    expected = {"outputs": [{"index": 2, "gain": -3.0, "delay": {"secs": 0, "nanos": 1_500_000}}]}
    assert daemon.calls == [expected, expected]
    assert writer.summary(0)["error"] is None
    await writer.stop()


async def test_failed_write_of_unchanged_value_is_retried_while_unknown(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    daemon.failures = [rejected()]
    writer.notify(0, ["outputs.0.gain_db"])  # known and unchanged: nothing to write
    await settle(writer)
    assert daemon.calls == []
    writer.invalidate(0, ["outputs.0.mute"])
    writer.notify(0, ["outputs.0.mute"])  # unknown + touched: written, and retried after the failure
    await settle(writer)
    assert daemon.calls == [{"outputs": [{"index": 0, "mute": False}]}] * 2
    assert writer.status(0)["outputs.0.mute"] is SyncStatus.SYNCED
    await writer.stop()


async def test_gives_up_after_max_attempts_with_backoff(daemon, make_writer):
    writer = make_writer(backoff_min_s=0.01, backoff_max_s=0.04, max_attempts=5)
    attach_trusted(writer)
    daemon.fail_when = lambda body: rejected(SPIKE_BODY)
    writer.set_desired(0, set_path(DEFAULT, "outputs.2.delay_ms", 79.0))
    result = await settle(writer)
    assert len(daemon.calls) == 5
    gaps = [b - a for a, b in zip(daemon.times, daemon.times[1:], strict=False)]
    assert gaps[0] >= 0.009 and gaps[1] >= 0.019 and gaps[2] >= 0.039 and gaps[3] >= 0.039
    assert result.failed == {"outputs.2.delay_ms": "Delay should be within [0, 80] ms was 8006"}
    assert writer.status(0)["outputs.2.delay_ms"] is SyncStatus.FAILED
    await asyncio.sleep(0.1)
    assert len(daemon.calls) == 5  # left alone after giving up

    daemon.fail_when = None
    writer.kick()
    assert (await settle(writer)).ok
    assert len(daemon.calls) == 6
    assert writer.status(0)["outputs.2.delay_ms"] is SyncStatus.SYNCED
    await writer.stop()


async def test_editing_a_failed_path_rearms_it(daemon, make_writer):
    writer = make_writer(max_attempts=2)
    attach_trusted(writer)
    daemon.fail_when = lambda body: rejected()
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -1.0))
    assert (await settle(writer)).failed
    daemon.fail_when = None
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -2.0))
    assert (await settle(writer)).ok
    assert daemon.calls[-1] == {"outputs": [{"index": 0, "gain": -2.0}]}
    await writer.stop()


async def test_rejected_payload_does_not_stop_independent_channels(daemon, make_writer):
    writer = make_writer(max_attempts=1)
    attach_trusted(writer)
    daemon.fail_when = lambda body: rejected() if body.get("outputs", [{}])[0].get("index") == 1 else None
    writer.set_desired(0, doc_with({"outputs.0.gain_db": -1.0, "outputs.1.gain_db": -1.0, "outputs.2.gain_db": -1.0}))
    result = await settle(writer)
    assert [c["outputs"][0]["index"] for c in daemon.calls] == [0, 1, 2]
    assert set(result.failed) == {"outputs.1.gain_db"}
    status = writer.status(0)
    assert status["outputs.0.gain_db"] is SyncStatus.SYNCED and status["outputs.2.gain_db"] is SyncStatus.SYNCED
    await writer.stop()


@pytest.mark.parametrize("exc", [DaemonUnavailable("down"), DeviceNotFound("gone")])
async def test_unreachable_daemon_stops_the_flush(daemon, make_writer, exc):
    writer = make_writer(backoff_min_s=0.3, backoff_max_s=0.3)
    attach_trusted(writer)
    daemon.failures = [exc]
    writer.set_desired(0, doc_with({"outputs.0.gain_db": -1.0, "outputs.1.gain_db": -1.0}))
    await asyncio.sleep(0.1)
    assert len(daemon.calls) == 1
    status = writer.status(0)
    assert status["outputs.0.gain_db"] is SyncStatus.PENDING and "outputs.0.gain_db" not in writer.state(0).applied
    writer.kick()  # e.g. the monitor reconnected: retry without waiting out the backoff
    assert (await settle(writer, timeout=0.25)).ok
    assert len(daemon.calls) == 3
    await writer.stop()


async def test_daemon_killed_mid_drag_converges_after_restart(daemon, make_writer):
    writer = make_writer(max_attempts=3)
    attach_trusted(writer)
    daemon.fail_when = lambda body: DaemonUnavailable("connection refused")
    doc = DEFAULT
    for gain in (10.0, 11.0, 12.0):
        doc = set_path(doc, "outputs.0.gain_db", gain)
        writer.set_desired(0, doc)
        await asyncio.sleep(0.03)
    result = await settle(writer)
    assert result.failed and writer.status(0)["outputs.0.gain_db"] is SyncStatus.FAILED
    daemon.fail_when = None
    writer.kick()
    assert (await settle(writer)).ok
    assert daemon.calls[-1] == {"outputs": [{"index": 0, "gain": 12.0}]}
    assert writer.state(0).applied["outputs.0.gain_db"] == 12.0
    await writer.stop()


async def test_invalid_value_fails_without_reaching_the_daemon(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    bad = DEFAULT.model_copy(deep=True)
    bad.outputs[0].gain_db = 40.0  # bypasses API validation; the builder is the last line of defence
    writer.set_desired(0, bad)
    result = await settle(writer)
    assert daemon.calls == []
    assert "outside" in result.failed["outputs.0.gain_db"]
    await writer.stop()


# --- glitch-safety through the writer ---------------------------------------------------------------------------


async def test_peq_unbypass_reaches_the_daemon_after_its_coefficients(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    writer.set_desired(0, doc_with({"outputs.1.peq.0.compiled": PEAK, "outputs.1.peq.0.bypass": False}))
    assert (await settle(writer)).ok
    assert daemon.calls == [
        {"outputs": [{"index": 1, "peq": [{"index": 0, "coeff": PEAK}]}]},
        {"outputs": [{"index": 1, "peq": [{"index": 0, "bypass": False}]}]},
    ]
    await writer.stop()


async def test_peq_unbypass_is_never_sent_when_its_coefficients_failed(daemon, make_writer):
    writer = make_writer(max_attempts=2)
    attach_trusted(writer)
    daemon.fail_when = lambda body: rejected() if "coeff" in str(body) else None
    writer.set_desired(0, doc_with({"outputs.1.peq.0.compiled": PEAK, "outputs.1.peq.0.bypass": False}))
    result = await settle(writer)
    assert all("bypass" not in str(call) for call in daemon.calls)
    assert set(result.failed) == {"outputs.1.peq.0.compiled", "outputs.1.peq.0.bypass"}
    await writer.stop()


# --- mute wrap & push-all ---------------------------------------------------------------------------------------


async def test_push_all_is_mute_wrapped(daemon, make_writer):
    writer = make_writer()
    desired = doc_with({"outputs.1.gain_db": 6.0, "outputs.3.mute": True, "outputs.0.peq.0.compiled": PEAK})
    writer.attach(0, desired, applied=trusted(desired))
    result = await writer.push_all(0, timeout=3)
    assert result.ok
    first, last = daemon.calls[0], daemon.calls[-1]
    assert first == {"outputs": [{"index": n, "mute": True} for n in range(4)]}
    assert last == {"outputs": [{"index": n, "mute": False} for n in range(3)]}
    middle = daemon.calls[1:-1]
    for call in middle:
        for output in call.get("outputs", []):
            assert "mute" not in output  # outputs are held muted until the end
    assert {
        "inputs": [
            {
                "index": 0,
                "mute": False,
                "gain": 0.0,
                "peq": [
                    {"index": k, "coeff": {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}, "bypass": True}
                    for k in range(10)
                ],
                "routing": [{"index": m, "mute": m % 2 != 0, "gain": 0.0} for m in range(4)],
            }
        ]
    } in middle
    assert set(writer.status(0).values()) == {SyncStatus.SYNCED}
    assert writer.state(0).wrap == set()
    await writer.stop()


async def test_mute_wrap_only_touches_affected_outputs(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    writer.set_desired(0, doc_with({"outputs.2.delay_ms": 9.0}))
    result = await writer.flush(0, mute_wrap=True, timeout=3)
    assert result.ok
    assert daemon.calls == [
        {"outputs": [{"index": 2, "mute": True}]},
        {"outputs": [{"index": 2, "delay": {"secs": 0, "nanos": 9_000_000}}]},
        {"outputs": [{"index": 2, "mute": False}]},
    ]
    await writer.stop()


async def test_failed_bulk_apply_leaves_outputs_muted(daemon, make_writer):
    writer = make_writer(max_attempts=2)
    attach_trusted(writer)
    daemon.fail_when = lambda body: rejected() if "delay" in str(body) else None
    writer.set_desired(0, doc_with({"outputs.2.delay_ms": 9.0, "outputs.1.gain_db": -1.0}))
    result = await writer.flush(0, mute_wrap=True, timeout=3)
    assert not result.ok
    assert daemon.calls[0] == {"outputs": [{"index": 1, "mute": True}, {"index": 2, "mute": True}]}
    assert not any(o.get("mute") is False for c in daemon.calls for o in c.get("outputs", []))
    assert writer.state(0).applied["outputs.1.mute"] is True
    assert set(result.failed) == {"outputs.2.delay_ms", "outputs.1.mute", "outputs.2.mute"}
    assert writer.status(0)["outputs.1.gain_db"] is SyncStatus.SYNCED

    daemon.fail_when = None
    writer.kick()
    assert (await settle(writer)).ok
    assert daemon.calls[-1] == {"outputs": [{"index": 1, "mute": False}, {"index": 2, "mute": False}]}
    await writer.stop()


async def test_input_changes_wrap_every_output(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    writer.set_desired(0, doc_with({"inputs.0.routing.1.enabled": True}))
    await writer.flush(0, mute_wrap=True, timeout=3)
    assert daemon.calls[0] == {"outputs": [{"index": n, "mute": True} for n in range(4)]}
    assert daemon.calls[-1] == {"outputs": [{"index": n, "mute": False} for n in range(4)]}
    await writer.stop()


# --- FIR, callbacks, events -------------------------------------------------------------------------------------


async def test_fir_blob_is_loaded_and_sent_alone(daemon, make_writer):
    loads = []

    async def fir_taps(blob_id):
        loads.append(blob_id)
        return [1.0, 0.5] if blob_id == "blob" else None

    writer = make_writer(FIR_PROFILE, fir_taps=fir_taps)
    base = default_config_doc(FIR_PROFILE)
    writer.attach(0, base, applied=trusted(base, FIR_PROFILE))
    writer.set_desired(0, doc_with({"outputs.0.fir.blob_id": "blob", "outputs.0.fir.bypass": False}, base, FIR_PROFILE))
    assert (await settle(writer)).ok
    assert daemon.calls == [
        {"outputs": [{"index": 0, "fir": {"coefficients": [1.0, 0.5]}}]},
        {"outputs": [{"index": 0, "fir": {"bypass": False}}]},
    ]
    assert "blob" in loads
    await writer.stop()


async def test_callbacks_and_bus_events(daemon, make_writer):
    bus = Bus()
    applied_calls, logs = [], []
    writer = make_writer(
        bus=bus,
        on_applied=lambda preset, state: applied_calls.append((preset, dict(state.applied))),
        on_log=lambda level, message, **data: logs.append((level, message)),
        max_attempts=1,
    )
    async with bus.subscribe([SYNC]) as sub:
        attach_trusted(writer)
        writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -1.0))
        await settle(writer)
        events = []
        while (event := sub.get_nowait()) is not None:
            events.append(event)
    assert all(e.serial == SERIAL and e.topic == SYNC for e in events)
    changes = [e.data["changes"].get("outputs.0.gain_db") for e in events]
    assert "pending" in changes and changes[-1] == "synced"
    assert events[-1].data["counts"]["synced"] == len(writer.status(0))
    assert applied_calls and applied_calls[-1][1]["outputs.0.gain_db"] == -1.0

    daemon.fail_when = lambda body: rejected()
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -2.0))
    await settle(writer)
    assert any(level == "warning" and "write failed" in message for level, message in logs)
    await writer.stop()


async def test_messages_handed_to_on_log_are_not_logged_twice(daemon, make_writer, caplog):
    logs = []
    writer = make_writer(on_log=lambda level, message, **data: logs.append((level, message)), max_attempts=1)
    attach_trusted(writer)
    daemon.fail_when = lambda body: rejected()
    caplog.set_level(logging.DEBUG, logger="app.sync.writer")
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -2.0))
    await settle(writer)
    assert any(level == "warning" and "write failed" in message for level, message in logs)
    assert not [r for r in caplog.records if r.name == "app.sync.writer" and "write failed" in r.getMessage()]
    await writer.stop()


async def test_no_events_when_nothing_changes(daemon, make_writer):
    bus = Bus()
    writer = make_writer(bus=bus)
    attach_trusted(writer)
    async with bus.subscribe([SYNC]) as sub:
        writer.notify(0)
        await settle(writer)
        await asyncio.sleep(0.05)
        assert sub.pending() == 0
    await writer.stop()


async def test_stop_releases_waiters(daemon, make_writer):
    writer = make_writer(backoff_min_s=5, backoff_max_s=5)
    attach_trusted(writer)
    daemon.fail_when = lambda body: DaemonUnavailable("down")
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -1.0))
    waiter = asyncio.create_task(writer.wait_idle(0))
    await asyncio.sleep(0.1)
    assert not waiter.done()
    await writer.stop()
    result = await asyncio.wait_for(waiter, 1)
    assert result.pending == ("outputs.0.gain_db",)
    assert not writer.running


async def test_wait_idle_timeout_returns_current_state(daemon, make_writer):
    writer = make_writer(backoff_min_s=5, backoff_max_s=5)
    attach_trusted(writer)
    daemon.fail_when = lambda body: DaemonUnavailable("down")
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -1.0))
    result = await writer.wait_idle(0, timeout=0.1)
    assert result.pending == ("outputs.0.gain_db",) and not result.ok
    await writer.stop()


async def test_unattached_preset_raises(make_writer):
    writer = make_writer()
    with pytest.raises(KeyError, match="not attached"):
        writer.notify(3)
    await writer.stop()


# --- error text -------------------------------------------------------------------------------------------------


def test_error_text_from_non_json_bodies():
    assert error_text(rejected(SPIKE_BODY)) == "Delay should be within [0, 80] ms was 8006"
    assert error_text(rejected(RANGE_BODY)) == "Specified channel or peq is out of range"
    assert error_text(DaemonRejected(500, "{}", message="the request could not be parsed")) == (
        "the request could not be parsed"
    )
    assert error_text(DaemonRejected(502, "")) == "HTTP 502"
    assert error_text(DaemonRejected(500, "plain text")) == "plain text"
    assert error_text(DaemonUnavailable("minidspd unreachable")) == "minidspd unreachable"
    assert error_text(RuntimeError()) == "RuntimeError"


async def test_guard_error_between_calls_keeps_unsent_paths_known(daemon, make_writer):
    writer = make_writer(backoff_min_s=0.3, backoff_max_s=0.3)
    attach_trusted(writer)

    def daemon_dies(body):
        daemon.guard_error = DaemonUnavailable("status read failed")

    daemon.on_post = daemon_dies
    writer.set_desired(0, doc_with({"outputs.0.gain_db": -1.0, "outputs.1.gain_db": -1.0}))
    await asyncio.sleep(0.1)
    assert len(daemon.calls) == 1
    state = writer.state(0)
    assert state.applied["outputs.0.gain_db"] == -1.0
    assert state.applied["outputs.1.gain_db"] == 0.0  # never sent: still known
    assert state.attempts["outputs.1.gain_db"] == 1
    assert writer.status(0)["outputs.1.gain_db"] is SyncStatus.PENDING
    daemon.on_post = None
    daemon.guard_error = None
    writer.kick()
    assert (await settle(writer)).ok
    assert daemon.calls[-1] == {"outputs": [{"index": 1, "gain": -1.0}]}


async def test_failed_wrap_mute_sends_nothing_else(daemon, make_writer):
    writer = make_writer(max_attempts=2)
    attach_trusted(writer)
    daemon.fail_when = lambda body: rejected()
    writer.set_desired(0, doc_with({"outputs.2.delay_ms": 9.0}))
    result = await writer.flush(0, mute_wrap=True, timeout=3)
    assert daemon.calls == [{"outputs": [{"index": 2, "mute": True}]}] * 2
    assert result.failed == {
        "outputs.2.mute": "Specified channel or peq is out of range",
        "outputs.2.delay_ms": "not applied: the outputs could not be muted first",
    }
    await asyncio.sleep(0.1)
    assert len(daemon.calls) == 2  # gave up: no endless retries, and the delay was never sent unmuted

    daemon.fail_when = None
    writer.kick()
    assert (await settle(writer)).ok
    assert daemon.calls[2:] == [
        {"outputs": [{"index": 2, "mute": True}]},
        {"outputs": [{"index": 2, "delay": {"secs": 0, "nanos": 9_000_000}}]},
        {"outputs": [{"index": 2, "mute": False}]},
    ]


async def test_transient_failure_of_the_final_unmute_is_retried(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    unmute_failures = [rejected()]

    def fail_first_unmute(body):
        outputs = body.get("outputs", [])
        if unmute_failures and outputs and all(o.get("mute") is False for o in outputs):
            return unmute_failures.pop()
        return None

    daemon.fail_when = fail_first_unmute
    writer.set_desired(0, doc_with({"outputs.0.gain_db": 3.0}))
    result = await writer.flush(0, mute_wrap=True, timeout=3)
    assert result.ok, result
    assert daemon.calls == [
        {"outputs": [{"index": 0, "mute": True}]},
        {"outputs": [{"index": 0, "gain": 3.0}]},
        {"outputs": [{"index": 0, "mute": False}]},
        {"outputs": [{"index": 0, "mute": False}]},  # retried, without muting again first
    ]
    assert writer.status(0)["outputs.0.mute"] is SyncStatus.SYNCED
    assert writer.state(0).wrap == set()


async def test_rejected_path_does_not_delay_other_edits(daemon, make_writer):
    writer = make_writer(backoff_min_s=0.5, backoff_max_s=8.0, max_attempts=10)
    attach_trusted(writer)
    daemon.fail_when = lambda body: rejected() if body.get("outputs", [{}])[0].get("index") == 0 else None
    doc = set_path(DEFAULT, "outputs.0.gain_db", -1.0)
    writer.set_desired(0, doc)
    await asyncio.sleep(0.1)  # the first attempt was rejected: output 0 is backing off
    assert len(daemon.calls) == 1
    started = time.monotonic()
    writer.set_desired(0, set_path(doc, "outputs.1.gain_db", -2.0))
    while not any(c["outputs"][0]["index"] == 1 for c in daemon.calls):
        assert time.monotonic() - started < 0.3, "the healthy edit waited for the other path's backoff"
        await asyncio.sleep(0.01)
    assert writer.status(0)["outputs.1.gain_db"] is SyncStatus.SYNCED
    assert writer.status(0)["outputs.0.gain_db"] is SyncStatus.PENDING
    assert len(daemon.calls) == 2  # output 0 is not retried before its backoff


async def test_backed_off_coefficients_hold_back_their_unbypass(daemon, make_writer):
    writer = make_writer(backoff_min_s=0.3, backoff_max_s=0.3)
    attach_trusted(writer)
    daemon.failures = [rejected()]
    writer.set_desired(0, doc_with({"outputs.1.peq.0.compiled": PEAK, "outputs.1.peq.0.bypass": False}))
    await asyncio.sleep(0.15)
    assert len(daemon.calls) == 1  # not pulled back in as a prerequisite while backing off
    assert (await settle(writer)).ok
    assert daemon.calls[1:] == [
        {"outputs": [{"index": 1, "peq": [{"index": 0, "coeff": PEAK}]}]},
        {"outputs": [{"index": 1, "peq": [{"index": 0, "bypass": False}]}]},
    ]


async def test_stopping_mid_call_marks_the_payload_unknown(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    daemon.latency = 1.0
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -4.0))
    while not daemon.calls:
        await asyncio.sleep(0.01)
    await writer.stop()
    state = writer.state(0)
    assert "outputs.0.gain_db" not in state.applied
    assert "outputs.0.gain_db" in state.dirty
    assert writer.status(0)["outputs.0.gain_db"] is SyncStatus.PENDING

    # Setting the old value back must still be written: the device may hold -4 dB.
    writer.set_desired(0, DEFAULT)
    assert writer.pending_paths(0) == ["outputs.0.gain_db"]


async def test_paused_waits_for_the_call_in_flight_and_blocks_new_ones(daemon, make_writer):
    writer = make_writer()
    attach_trusted(writer)
    daemon.latency = 0.2
    writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -1.0))
    while not daemon.calls:
        await asyncio.sleep(0.01)
    async with writer.paused():
        assert writer.state(0).applied["outputs.0.gain_db"] == -1.0  # the in-flight call finished first
        writer.set_desired(0, set_path(DEFAULT, "outputs.0.gain_db", -2.0))
        await asyncio.sleep(0.15)
        assert len(daemon.calls) == 1
    assert (await settle(writer)).ok
    assert daemon.calls[-1] == {"outputs": [{"index": 0, "gain": -2.0}]}


async def test_attach_restores_held_mutes(daemon, make_writer):
    writer = make_writer()
    desired = doc_with({"outputs.0.peq.0.compiled": PEAK, "outputs.0.peq.0.bypass": False})
    applied = trusted(DEFAULT) | {"outputs.0.mute": True}  # held muted by an apply that didn't finish
    writer.attach(0, desired, applied=applied, wrap=[0])
    daemon.fail_when = lambda body: rejected() if "coeff" in str(body) else None
    writer.kick()
    await asyncio.sleep(0.2)
    assert daemon.calls and not any(o.get("mute") is False for c in daemon.calls for o in c.get("outputs", []))
    daemon.fail_when = None
    writer.kick()
    assert (await settle(writer)).ok
    assert daemon.calls[-1] == {"outputs": [{"index": 0, "mute": False}]}
