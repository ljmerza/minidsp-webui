"""Verify device: read the active preset's readable settings back from DSP memory and diff them against the applied
shadow (docs 03 §9, docs 08 Phase 6). Read-only: it never writes to the device.

What can be read back (spike results 0.2, 0.8 and the compressor table):

- gains (dB, quantized and biased low by up to ~0.36 dB, never high, so a reading may sit up to `GAIN_TOLERANCE_DB`
  below the sent value but at most `GAIN_EXCESS_TOLERANCE_DB` above it), mute gates,
  delay (whole samples), output polarity, PEQ and crossover coefficients (bit-exact float32) and the compressor
  threshold (plain float32 dB).
- Not readable, reported as unverifiable: every bypass flag (PEQ, crossover, compressor, FIR), the routing matrix
  (the enable status registers read 1 whatever was written; the routing gain registers were never checked against
  a write), compressor ratio/attack/release (always read 0) and FIR taps.

Only the active preset can be verified: DSP memory holds the active preset only. Paths the shadow doesn't know are
compared with the desired value and reported as ``unknown``; if any of them differ the report's status is ``mismatch``,
and with nothing confirmed at all it is ``unverified`` (``ok`` needs at least one confirmed setting). Optionally the
drifted paths are marked unknown in the shadow (`DeviceWriter.invalidate`), which writes nothing; the user then pushes
them from the sync table, or accepts the device values (`accept_device_values`: they become the desired values and are
confirmed, nothing is written).

The same reads re-confirm the shadow after a reconnect (`reconfirm`) and seed an adopt with "trust" (`read_back`).

Each run logs one event and publishes a ``verify`` bus event; `drift_discovery` / `drift_state` turn those into a Home
Assistant drift sensor. `nightly_verify_loop` runs it once a night.
"""

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from enum import StrEnum
from time import monotonic
from typing import Any

import numpy as np
from pydantic import ValidationError

from app.daemon.rawframes import (
    BIQUAD_LEN,
    GATE_MUTED,
    GATE_UNMUTED,
    FloatData,
    MemoryMap,
    RawFrameReader,
    RawFrameTimeout,
    as_floats,
    as_word,
    lookup,
    memory_map_for,
    plan_biquad_reads,
    plan_scalar_reads,
    raw_ws_url,
)
from app.dsp.recognize import annotate_designs
from app.model.config_doc import ConfigDoc, PathError, set_path, validate_doc
from app.model.device_profile import DeviceProfile
from app.sync.backup import seconds_until
from app.sync.session import (
    DeviceSession,
    PresetSwitchInProgress,
    SessionManager,
    SessionState,
    UndoResult,
    undo_unit,
)

log = logging.getLogger(__name__)

VERIFY = "verify"  # bus topic

GAIN_TOLERANCE_DB = 0.5  # how far below the sent value the device may read (its quantization only ever reads low)
# How far above: none from the device, only the 0.01 dB rounding of an accepted reading (`device_value`) and float32.
GAIN_EXCESS_TOLERANCE_DB = 0.01
# D_GAIN reads exactly this after a gain below about -100 dB was written (spike 0.2): out of range, maybe dangerous.
GAIN_UNDERFLOW_READBACK_DB = 6.287154
THRESHOLD_TOLERANCE_DB = 1e-3

_BIQUAD_KEYS = ("b0", "b1", "b2", "a1", "a2")

UNVERIFIABLE_BYPASS = "bypass state has no readable register"
UNVERIFIABLE_ROUTING = "routing status registers read the same whatever was written"
UNVERIFIABLE_ROUTING_GAIN = "routing gain registers have never been checked against a write, so they aren't trusted"
UNVERIFIABLE_COMPRESSOR = "write-only: reads back 0 whatever was written"
UNVERIFIABLE_COMPRESSOR_BYPASS = "the status register reads a factory value the API can't write back"
UNVERIFIABLE_FIR = "FIR taps and state are not read back"
UNVERIFIABLE_UNMAPPED = "no readable register for this setting"


class ItemStatus(StrEnum):
    OK = "ok"  # the device holds what the shadow says
    DRIFT = "drift"  # the device holds something else
    UNKNOWN = "unknown"  # the shadow doesn't know the path; compared with the desired value instead
    UNVERIFIABLE = "unverifiable"  # the device can't report it


class VerifyError(RuntimeError):
    """Verification can't run, or its result would be meaningless."""


class VerifyUnsupported(VerifyError):
    """No memory map for this device model."""


class ReadbackUnavailable(VerifyError):
    """The device (or the mock) didn't answer its memory reads, or answered too slowly: nothing can be read back.

    Raised after the first read goes unanswered (its own retry included) or when the whole read phase overruns
    `READ_BUDGET_S`, instead of waiting out a timeout on every read. Remembered for `UNREADABLE_HOLD_S`, so the
    next check, nightly run or adopt read-back fails at once; a reconnect clears it."""


# A real 2x4HD answers the ~51 reads of a full check in about 1 s, the embedded mock in about 3-4 s; a read phase
# this long means something is wrong (see `ReadbackUnavailable`).
READ_BUDGET_S = 15.0
UNREADABLE_HOLD_S = 60.0


# --- comparison ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """How one path is read: ``kind`` decides decoding and comparison; ``addr`` is a register or a biquad."""

    kind: str  # gain | gate | delay | invert | biquad | threshold
    addr: int


@dataclass(frozen=True)
class VerifyItem:
    path: str
    status: ItemStatus
    expected: Any = None  # applied shadow value (the desired value when status is unknown)
    actual: Any = None  # decoded device value, in the document's units
    raw: Any = None  # register contents (u32 or float32s)
    note: str | None = None
    matches_desired: bool | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status.value,
            "expected": self.expected,
            "actual": self.actual,
            "raw": self.raw,
            "note": self.note,
            "matches_desired": self.matches_desired,
        }


def probe_for(path: str, mmap: MemoryMap) -> Probe | str:
    """The register behind a device path, or the reason it can't be verified."""
    parts = path.split(".")
    try:
        side, ch = parts[0], int(parts[1])
        rest = parts[2:]
        if side == "inputs":
            return _input_probe(mmap, ch, rest)
        if side == "outputs":
            return _output_probe(mmap, ch, rest)
    except (IndexError, ValueError):
        pass
    return UNVERIFIABLE_UNMAPPED


def _peq_probe(table: tuple[tuple[int, ...], ...], ch: int, band: str, key: str) -> Probe | str:
    if key == "compiled":
        return Probe("biquad", table[ch][int(band)])
    if key == "bypass":
        return UNVERIFIABLE_BYPASS
    return UNVERIFIABLE_UNMAPPED


def _input_probe(mmap: MemoryMap, ch: int, rest: list[str]) -> Probe | str:
    match rest:
        case ["gain_db"]:
            return Probe("gain", mmap.input_gain[ch])
        case ["mute"]:
            return Probe("gate", mmap.input_gate[ch])
        case ["routing", _, "gain_db"]:
            return UNVERIFIABLE_ROUTING_GAIN
        case ["routing", *_]:
            return UNVERIFIABLE_ROUTING
        case ["peq", band, key]:
            return _peq_probe(mmap.input_peq, ch, band, key)
    return UNVERIFIABLE_UNMAPPED


def _output_probe(mmap: MemoryMap, ch: int, rest: list[str]) -> Probe | str:
    match rest:
        case ["gain_db"]:
            return Probe("gain", mmap.output_gain[ch])
        case ["mute"]:
            return Probe("gate", mmap.output_gate[ch])
        case ["delay_ms"]:
            return Probe("delay", mmap.output_delay[ch])
        case ["invert"]:
            return Probe("invert", mmap.output_invert[ch])
        case ["peq", band, key]:
            return _peq_probe(mmap.output_peq, ch, band, key)
        case ["crossover", group, "compiled", biquad]:
            return Probe("biquad", mmap.crossover_biquad(ch, int(group), int(biquad)))
        case ["crossover", _, "bypass"]:
            return UNVERIFIABLE_BYPASS
        case ["compressor", "threshold_db"]:
            return Probe("threshold", mmap.compressor_threshold[ch])
        case ["compressor", "bypass"]:
            return UNVERIFIABLE_COMPRESSOR_BYPASS
        case ["compressor", _]:
            return UNVERIFIABLE_COMPRESSOR
        case ["fir", _]:
            return UNVERIFIABLE_FIR
    return UNVERIFIABLE_UNMAPPED


def _close_f32(read: float, expected: float) -> bool:
    """Equal as float32, allowing one unit in the last place (the daemon may round JSON numbers through f64)."""
    target = np.float32(expected)
    value = np.float32(read)
    if not (np.isfinite(target) and np.isfinite(value)):
        return False
    return bool(abs(value - target) <= abs(np.spacing(target)))


@dataclass(frozen=True)
class Decoded:
    actual: Any
    raw: Any
    note: str | None = None  # set when the register holds something that isn't a valid value


def decode(probe: Probe, raw: bytes, profile: DeviceProfile) -> Decoded:
    if probe.kind in ("gain", "threshold"):
        value = as_floats(raw)[0]
        return Decoded(round(value, 6), value)
    if probe.kind == "biquad":
        values = as_floats(raw)
        return Decoded(dict(zip(_BIQUAD_KEYS, values, strict=True)), values)
    word = as_word(raw)
    if probe.kind == "gate":
        if word not in (GATE_MUTED, GATE_UNMUTED):
            return Decoded(None, word, f"unexpected gate register value {word}")
        return Decoded(word == GATE_MUTED, word)
    if probe.kind == "invert":
        if word not in (0, 1):
            return Decoded(None, word, f"unexpected polarity register value {word}")
        return Decoded(bool(word), word)
    if probe.kind == "delay":
        if word > profile.delay_max_samples:
            return Decoded(None, word, f"delay register holds {word} samples, above the device maximum")
        return Decoded(round(profile.delay_ms_from_samples(word), 6), word)
    raise ValueError(f"unknown probe kind {probe.kind}")


def matches(probe: Probe, decoded: Decoded, expected: Any, profile: DeviceProfile) -> bool:
    """Whether the decoded device value is what ``expected`` (a document value) puts on the device."""
    if decoded.actual is None:
        return False
    try:
        match probe.kind:
            case "gain":
                delta = decoded.actual - float(expected)
                return -GAIN_TOLERANCE_DB <= delta <= GAIN_EXCESS_TOLERANCE_DB
            case "threshold":
                return abs(decoded.actual - float(expected)) <= THRESHOLD_TOLERANCE_DB
            case "gate" | "invert":
                return isinstance(expected, bool) and decoded.actual is expected
            case "delay":
                return decoded.raw == profile.delay_samples(float(expected))
            case "biquad":
                return all(_close_f32(decoded.actual[k], float(expected[k])) for k in _BIQUAD_KEYS)
    except (TypeError, ValueError, KeyError, OverflowError):
        return False
    return False


def device_value(item: "VerifyItem") -> Any:
    """The document value a read item says the device holds, or None when the register holds nothing usable (an
    invalid value, or the readback of a gain below about -100 dB). Gains are rounded to 0.01 dB: the readback is
    quantized, so more digits would only be noise."""
    if item.status == ItemStatus.UNVERIFIABLE or item.actual is None or item.note:
        return None
    if item.path.endswith(".gain_db") and isinstance(item.actual, float):
        return round(item.actual, 2)
    return item.actual


def zero_filter(item: "VerifyItem") -> bool:
    """Whether a read biquad is all zeros (``b0 = b1 = b2 = 0``). Such a filter passes no sound at all; on a band the
    app has on it would silence the channel, and it is what a failed or empty read looks like, so it is never accepted
    as the device's value."""
    actual = item.actual
    if item.status not in (ItemStatus.DRIFT, ItemStatus.UNKNOWN) or not isinstance(actual, dict):
        return False
    try:
        return all(float(actual[k]) == 0.0 for k in ("b0", "b1", "b2"))
    except (KeyError, TypeError, ValueError):
        return False


def accepted_values(items: Iterable["VerifyItem"], desired: Mapping[str, Any]) -> dict[str, Any]:
    """Document value per readable item: the desired value where the device holds it (within the readback tolerance,
    so a recognized design and full-precision coefficients survive), else the decoded device value. Items without a
    usable reading are left out."""
    out: dict[str, Any] = {}
    for item in items:
        if item.status == ItemStatus.UNVERIFIABLE:
            continue
        if item.matches_desired and item.path in desired:
            out[item.path] = desired[item.path]
        elif (value := device_value(item)) is not None:
            out[item.path] = value
    return out


def _hint(probe: Probe, decoded: Decoded) -> str | None:
    if decoded.note:
        return decoded.note
    if probe.kind == "gain" and abs(decoded.actual - GAIN_UNDERFLOW_READBACK_DB) < 1e-4:
        return "the device reads this after a gain below about -100 dB was written"
    return None


def compare(
    path: str,
    probe: Probe,
    raw: bytes,
    *,
    applied: Mapping[str, Any],
    desired: Any,
    profile: DeviceProfile,
) -> VerifyItem:
    decoded = decode(probe, raw, profile)
    matches_desired = matches(probe, decoded, desired, profile)
    if path not in applied:
        return VerifyItem(
            path,
            ItemStatus.UNKNOWN,
            expected=desired,
            actual=decoded.actual,
            raw=decoded.raw,
            note=_hint(probe, decoded),
            matches_desired=matches_desired,
        )
    expected = applied[path]
    ok = matches(probe, decoded, expected, profile)
    return VerifyItem(
        path,
        ItemStatus.OK if ok else ItemStatus.DRIFT,
        expected=expected,
        actual=decoded.actual,
        raw=decoded.raw,
        note=None if ok else _hint(probe, decoded),
        matches_desired=matches_desired,
    )


# --- report -------------------------------------------------------------------------------------------------------


@dataclass
class VerifyReport:
    serial: int
    preset: int
    checked_at: str
    duration_ms: float
    reads: int
    items: list[VerifyItem] = field(default_factory=list)
    source: str = "ui"
    marked_unknown: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)
    # The writer's `writes_sent` when the shadow was read: a path the writer sent after it is no longer "changed on
    # device" (app/sync/status.py). None when unknown (e.g. a report built by hand).
    writes_at: int | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in ItemStatus}
        for item in self.items:
            counts[item.status.value] += 1
        return counts

    @property
    def drifted(self) -> list[VerifyItem]:
        return [i for i in self.items if i.status == ItemStatus.DRIFT]

    @property
    def mismatched(self) -> list[VerifyItem]:
        """Unknown paths whose device value differs from the desired one (e.g. after a power cycle invalidated the
        shadow): not drift from what the app wrote, but the device doesn't hold the desired config either."""
        return [i for i in self.items if i.status == ItemStatus.UNKNOWN and i.matches_desired is False]

    @property
    def acceptable(self) -> list[VerifyItem]:
        """Drifted and never-confirmed items with a usable reading: their device values can be accepted. All-zero
        filters (`zero_filter`) are not usable."""
        return [
            i
            for i in self.items
            if i.status in (ItemStatus.DRIFT, ItemStatus.UNKNOWN) and device_value(i) is not None and not zero_filter(i)
        ]

    @property
    def refused(self) -> list[VerifyItem]:
        """Drifted and never-confirmed filters that read all zeros: never accepted (`zero_filter`)."""
        return [i for i in self.items if zero_filter(i)]

    @property
    def status(self) -> str:
        """``drift`` beats ``mismatch``; ``unverified`` when nothing could be confirmed; ``ok`` only when at least one
        setting was confirmed and nothing differs."""
        if self.drifted:
            return "drift"
        if self.mismatched:
            return "mismatch"
        if not self.counts[ItemStatus.OK.value]:
            return "unverified"
        return "ok"

    def summary(self) -> str:
        c = self.counts
        head = f"verify preset {self.preset + 1}: "
        match self.status:
            case "drift":
                head += f"{c['drift']} setting(s) differ from what the app last wrote"
            case "mismatch":
                head += f"no drift; {len(self.mismatched)} never-confirmed setting(s) differ from the desired value"
            case "unverified":
                head += "nothing verified, no setting is confirmed on the device"
            case _:
                head += f"all {c['ok']} verified setting(s) match"
        extra = [
            f"{len(self.mismatched)} unknown differ from desired" if self.mismatched and self.drifted else "",
            f"{c['unknown']} unknown" if c["unknown"] else "",
            f"{c['unverifiable']} not readable",
        ]
        return head + " (" + ", ".join(e for e in extra if e) + ")"

    def event(self) -> dict[str, Any]:
        """The ``verify`` bus event (and the MQTT drift sensor's state)."""
        return {
            "serial": self.serial,
            "preset": self.preset,
            "status": self.status,
            "drift": len(self.drifted),
            "mismatch": len(self.mismatched),
            "counts": self.counts,
            "paths": [i.path for i in self.drifted],
            "mismatch_paths": [i.path for i in self.mismatched],
            "checked_at": self.checked_at,
            "source": self.source,
        }

    def as_json(self) -> dict[str, Any]:
        return {
            **self.event(),
            "duration_ms": round(self.duration_ms, 1),
            "reads": self.reads,
            "marked_unknown": self.marked_unknown,
            "accepted": self.accepted,
            "acceptable": [i.path for i in self.acceptable],
            "refused": [i.path for i in self.refused],
            "items": [i.as_json() for i in self.items],
        }


_last_reports: dict[int, VerifyReport] = {}
_locks: weakref.WeakKeyDictionary[DeviceSession, asyncio.Lock] = weakref.WeakKeyDictionary()

type ReaderFactory = Callable[[str], RawFrameReader]


def last_report(serial: int) -> VerifyReport | None:
    return _last_reports.get(serial)


def forget_reports() -> None:
    _last_reports.clear()


_unreadable: weakref.WeakKeyDictionary[DeviceSession, tuple[float, str]] = weakref.WeakKeyDictionary()


def _device_word(session: DeviceSession) -> str:
    device = getattr(session, "device", None)
    url = getattr(device, "url", None) or ""
    return "The mock device" if url.startswith("mock:") else "The device"


def _mark_unreadable(session: DeviceSession, why: str) -> ReadbackUnavailable:
    message = f"{_device_word(session)} can't be read back: {why}. Nothing was compared or changed"
    _unreadable[session] = (monotonic() + UNREADABLE_HOLD_S, message)
    log.warning("[%s] readback unavailable: %s", session.serial, why)
    return ReadbackUnavailable(message)


def forget_unreadable(session: DeviceSession) -> None:
    """Try the reads again next time (a reconnect, a new daemon)."""
    _unreadable.pop(session, None)


async def _read_blocks(
    session: DeviceSession, reader: RawFrameReader, reads: list[tuple[int, int]]
) -> dict[int, FloatData]:
    """Every read, failing fast: an unanswered first read or an overrun of `READ_BUDGET_S` aborts the whole run
    with `ReadbackUnavailable`. A read that times out after earlier ones were answered raises `RawFrameTimeout` as
    before (a glitch, not an unreadable device). Same reads, in the same order, as `RawFrameReader.read_many`."""
    blocks: dict[int, FloatData] = {}
    try:
        async with asyncio.timeout(READ_BUDGET_S):
            for n, (base, count) in enumerate(reads):
                try:
                    blocks[base] = await reader.read(base, count)
                except RawFrameTimeout as exc:
                    if n == 0:
                        raise _mark_unreadable(session, "it didn't answer a memory read") from exc
                    raise
    except TimeoutError as exc:
        raise _mark_unreadable(
            session, f"reading it took longer than {READ_BUDGET_S:g} s ({len(blocks)} of {len(reads)} reads done)"
        ) from exc
    _unreadable.pop(session, None)
    return blocks


def _lock(session: DeviceSession) -> asyncio.Lock:
    lock = _locks.get(session)
    if lock is None:
        lock = _locks[session] = asyncio.Lock()
    return lock


def plan(paths: Iterable[str], mmap: MemoryMap) -> tuple[dict[str, Probe], dict[str, str], list[tuple[int, int]]]:
    """Probes for the readable paths, reasons for the others, and the reads that cover every probe."""
    probes: dict[str, Probe] = {}
    unreadable: dict[str, str] = {}
    for path in paths:
        probe = probe_for(path, mmap)
        if isinstance(probe, Probe):
            probes[path] = probe
        else:
            unreadable[path] = probe
    scalars = [p.addr for p in probes.values() if p.kind != "biquad"]
    biquads = [p.addr for p in probes.values() if p.kind == "biquad"]
    return probes, unreadable, plan_scalar_reads(scalars) + plan_biquad_reads(biquads)


def build_items(
    values: Mapping[str, Any],
    applied: Mapping[str, Any],
    probes: Mapping[str, Probe],
    unreadable: Mapping[str, str],
    blocks: Iterable[FloatData],
    profile: DeviceProfile,
) -> list[VerifyItem]:
    """One item per device path, in document order."""
    blocks = list(blocks)
    items = []
    for path, desired in values.items():
        probe = probes.get(path)
        if probe is None:
            reason = unreadable.get(path, UNVERIFIABLE_UNMAPPED)
            items.append(VerifyItem(path, ItemStatus.UNVERIFIABLE, expected=applied.get(path, desired), note=reason))
            continue
        raw = lookup(blocks, probe.addr, BIQUAD_LEN if probe.kind == "biquad" else 1)
        items.append(compare(path, probe, raw, applied=applied, desired=desired, profile=profile))
    return items


@dataclass
class Readback:
    """One read of the active preset: the shadow and desired values it was taken against, and the raw blocks."""

    preset: int
    applied: dict[str, Any]
    values: dict[str, Any]
    probes: dict[str, Probe]
    unreadable: dict[str, str]
    blocks: list[FloatData]
    reads: int
    started: float
    checked_at: str
    writes_at: int | None = None

    def items(
        self,
        profile: DeviceProfile,
        *,
        values: Mapping[str, Any] | None = None,
        applied: Mapping[str, Any] | None = None,
    ) -> list[VerifyItem]:
        """Items compared against the shadow and desired values of the read, or the given ones."""
        return build_items(
            self.values if values is None else values,
            self.applied if applied is None else applied,
            self.probes,
            self.unreadable,
            self.blocks,
            profile,
        )

    def report(self, session: DeviceSession, items: list[VerifyItem], *, source: str) -> VerifyReport:
        return VerifyReport(
            serial=session.serial,
            preset=self.preset,
            checked_at=self.checked_at,
            duration_ms=(monotonic() - self.started) * 1000,
            reads=self.reads,
            items=items,
            source=source,
            writes_at=self.writes_at,
        )


async def _read(
    session: DeviceSession, reader_factory: ReaderFactory | None, *, values: Mapping[str, Any] | None = None
) -> Readback:
    """Read the active preset's readable registers (of ``values``' paths, default the preset's device paths). The
    caller holds `_lock`."""
    mmap = memory_map_for(session.profile.product_name)
    if mmap is None:
        raise VerifyUnsupported(f"readback is not supported on the {session.profile.product_name}")
    held = _unreadable.get(session)
    if held is not None:
        if monotonic() < held[0]:
            raise ReadbackUnavailable(held[1])
        _unreadable.pop(session, None)
    await session.ensure_loaded()
    if session.preset_switching is not None:
        raise PresetSwitchInProgress(f"preset switch to {session.preset_switching} in progress")
    preset = await session.active_preset(max_age_s=0)
    index = session.daemon_index if session.daemon_index is not None else await session.resolve_index()
    url = raw_ws_url(session.client.base_url, index)
    factory = reader_factory or session.raw_reader_factory or RawFrameReader
    started = monotonic()
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")
    async with factory(url) as reader:
        # No writes may land between the snapshot of the shadow and the reads (an app preset switch waits too).
        async with session.writer.paused():
            if session.preset_switching is not None or _cached_preset(session) not in (None, preset):
                raise VerifyError("the active preset changed; try again")
            state = session.writer.state(preset)
            applied = dict(state.applied)
            current = dict(state.values(session.profile))
            writes_at = session.writer.writes_sent
            probes, unreadable, reads = plan(current if values is None else values, mmap)
            blocks = await _read_blocks(session, reader, reads)
            if session.preset_switching is not None or _cached_preset(session) not in (None, preset):
                raise VerifyError("the active preset changed while reading; try again")
            # The cached preset only follows other clients' switches (CLI, front panel) on the monitor's poll,
            # so ask the daemon again before trusting the reads.
            if await session.active_preset(max_age_s=0) != preset:
                raise VerifyError("the active preset changed while reading; try again")
        read_count = reader.reads
    return Readback(
        preset,
        applied,
        current,
        probes,
        unreadable,
        list(blocks.values()),
        read_count,
        started,
        checked_at,
        writes_at=writes_at,
    )


async def verify_device(
    session: DeviceSession,
    *,
    mark_unknown: bool = False,
    source: str = "ui",
    reader_factory: ReaderFactory | None = None,
    persist_ok: bool = True,
) -> VerifyReport:
    """Read the active preset's readable settings and diff them against the applied shadow.

    Raises `VerifyUnsupported`, `StateUnavailable`, `PresetSwitchInProgress`, `VerifyError` (the preset changed
    while reading) and `DaemonError` subclasses (`RawFrameError` for bridge problems). Nothing is written to the
    device; with ``mark_unknown`` the drifted paths are dropped from the shadow so the sync table offers to push them.
    """
    async with _lock(session):
        read = await _read(session, reader_factory)
        report = read.report(session, read.items(session.profile), source=source)
        if mark_unknown and report.drifted:
            report.marked_unknown = mark_drift_unknown(session, read.preset, [i.path for i in report.drifted])
        _record(session, report, persist=persist_ok or report.status in _WARN_STATES)
        return report


async def reconfirm(
    session: DeviceSession, *, power_cycle: bool, reader_factory: ReaderFactory | None = None
) -> VerifyReport:
    """After the device stream reconnected: verify the active preset and settle its shadow from the reads.

    Readable paths that hold the shadow's value stay confirmed, unknown ones that hold the desired value become
    confirmed and drifted ones become unknown. Unreadable paths (bypass, routing, compressor timing) keep their shadow
    unless ``power_cycle`` is plausible, then they become unknown. Nothing is written. Raises like `verify_device`.
    """
    async with _lock(session):
        forget_unreadable(session)  # a reconnect is a new chance for the reads
        read = await _read(session, reader_factory)
        items = read.items(session.profile)
        report = read.report(session, items, source="reconnect")
        state = session.writer.state(read.preset)
        current = state.values(session.profile)
        # Only act on paths nothing touched since the read (a write may have landed after it).
        confirm = {
            i.path: read.values[i.path]
            for i in items
            if i.status == ItemStatus.UNKNOWN
            and i.matches_desired
            and i.path not in state.applied
            and i.path in current
            and current[i.path] == read.values[i.path]
        }
        forget = [i.path for i in report.drifted if state.applied.get(i.path, _NOTHING) == i.expected]
        if power_cycle:
            forget += [p for p in read.unreadable if p in state.applied]
        if forget:
            session.writer.invalidate(read.preset, forget)
        if confirm:
            session.writer.confirm(read.preset, confirm)
        _record(session, report, persist=True)
        return report


async def read_back(
    session: DeviceSession, values: Mapping[str, Any], *, reader_factory: ReaderFactory | None = None
) -> tuple[int, list[VerifyItem]]:
    """Read the active preset for an adopt: ``(preset, items)``, every readable path compared with ``values`` (all
    ``unknown``, ``matches_desired`` against ``values``). Nothing is recorded or changed. Raises like `verify_device`.
    """
    async with _lock(session):
        read = await _read(session, reader_factory, values=values)
    return read.preset, read.items(session.profile, values=values, applied={})


@dataclass(frozen=True)
class AcceptPreview:
    preset: int
    before: ConfigDoc
    after: ConfigDoc
    values: dict[str, Any]  # device path -> the value it would take
    refused: list[VerifyItem]
    problems: list[str]


def accept_items(report: VerifyReport, paths: Iterable[str] | None = None) -> list[VerifyItem]:
    """The acceptable items of ``report`` (all, or those in ``paths``). Refused ones (all-zero filters) never are."""
    chosen = None if paths is None else set(paths)
    return [i for i in report.acceptable if chosen is None or i.path in chosen]


def accept_preview(session: DeviceSession, report: VerifyReport, paths: Iterable[str] | None = None) -> AcceptPreview:
    """What `accept_device_values` would do with the same arguments, without doing it: the desired document before and
    after (designs recognized, as the accept does) and the values it would take. Nothing is changed."""
    items = accept_items(report, paths)
    state = session.writer.state(report.preset)
    before = state.desired
    values = accepted_values(items, state.values(session.profile))
    after = before
    problems: list[str] = []
    try:
        for path, value in values.items():
            after = set_path(after, path, value)
    except (PathError, ValidationError) as exc:
        problems.append(str(exc))
        after = before
    if after is not before:
        after = annotate_designs(after)
        # The checks `DeviceSession.accept_device_values` refuses with (a gain above a channel's ceiling, say).
        problems += validate_doc(after, session.profile) + session._ceiling_problems(after)
    chosen = None if paths is None else set(paths)
    refused = [i for i in report.refused if chosen is None or i.path in chosen]
    return AcceptPreview(report.preset, before, after, values, refused, problems)


_undo_items: dict[int, tuple[str, VerifyReport, dict[str, VerifyItem], list[str]]] = {}


async def accept_device_values(
    session: DeviceSession, report: VerifyReport, paths: Iterable[str] | None = None
) -> list[str]:
    """Accept the device values of ``report``'s acceptable items (all, or those in ``paths``): they become the desired
    values and are confirmed in the shadow. Nothing is written to the device. The report is updated (the items become
    ``ok``) and published again. Returns the accepted paths. Raises `DesiredValidationError` (e.g. a gain above a
    channel's ceiling). All-zero filters are never accepted (`VerifyReport.refused`). It can be undone for
    `ACCEPT_UNDO_S` with `undo_accept` (the token is ``session.accept_undo.token``)."""
    items = accept_items(report, paths)
    if not items:
        return []
    desired = session.writer.state(report.preset).values(session.profile)
    values = accepted_values(items, desired)
    previous = {i.path: i for i in report.items if i.path in values}
    previous_accepted = list(report.accepted)
    await session.accept_device_values(report.preset, values, summary=f"verify: accepted {len(values)} device value(s)")
    if session.accept_undo is not None:
        _undo_items[session.serial] = (session.accept_undo.token, report, previous, previous_accepted)
    confirmed = session.writer.state(report.preset).applied
    accepted = set(values)
    report.items = [
        VerifyItem(
            i.path,
            ItemStatus.OK,
            expected=confirmed.get(i.path, i.expected),
            actual=i.actual,
            raw=i.raw,
            matches_desired=True,
        )
        if i.path in accepted
        else i
        for i in report.items
    ]
    report.accepted = sorted(set(report.accepted) | accepted)
    _record(session, report, persist=False)
    return sorted(accepted)


async def undo_accept(session: DeviceSession, preset: int, token: str | None = None) -> UndoResult:
    """Undo the last `accept_device_values` within `ACCEPT_UNDO_S`: the app's earlier values and shadow come back
    (`DeviceSession.restore_accept_snapshot`; nothing is written or queued) and, while it is still the last report,
    the report's items for them too. Raises `UndoUnavailable`."""
    result = await session.restore_accept_snapshot(preset, token)
    saved = _undo_items.pop(session.serial, None)
    if saved is not None and (token is None or saved[0] == token):
        _token, report, previous, previous_accepted = saved
        if report is _last_reports.get(session.serial):
            units = set(result.restored)
            back = {p: item for p, item in previous.items() if undo_unit(p) in units}
            report.items = [back.get(i.path, i) for i in report.items]
            report.accepted = sorted(p for p in report.accepted if p not in back or p in previous_accepted)
            _record(session, report, persist=False)
    return result


def _cached_preset(session: DeviceSession) -> int | None:
    master = session.master
    return master.preset if master is not None else None


def mark_drift_unknown(session: DeviceSession, preset: int, paths: Iterable[str]) -> list[str]:
    """Drop ``paths`` from the shadow (they become "unknown"; nothing is written). Returns the paths dropped."""
    state = session.writer.state(preset)
    known = [p for p in paths if p in state.applied]
    if known:
        session.writer.invalidate(preset, known)
        session.log_event(
            "info", f"verify: marked {len(known)} drifted setting(s) unknown on preset {preset + 1}", kind="verify"
        )
    return known


_WARN_STATES = ("drift", "mismatch")
_NOTHING = object()


def _record(session: DeviceSession, report: VerifyReport, *, persist: bool) -> None:
    _last_reports[session.serial] = report
    level = "warning" if report.status in _WARN_STATES else "info"
    session.log_event(
        level, report.summary(), kind="verify", persist=persist, preset=report.preset, drift=report.event()["paths"]
    )
    session.bus.publish(VERIFY, report.event(), serial=session.serial)


def publish_failure(session: DeviceSession, error: BaseException, *, source: str) -> dict[str, Any]:
    """Log a verify that couldn't run and publish it (the drift sensor shows ``error``)."""
    event = {
        "serial": session.serial,
        "preset": _cached_preset(session),
        "status": "error",
        "drift": None,
        "mismatch": None,
        "counts": None,
        "paths": [],
        "mismatch_paths": [],
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
        "error": str(error) or type(error).__name__,
    }
    session.log_event("warning", f"verify failed: {event['error']}", kind="verify")
    session.bus.publish(VERIFY, event, serial=session.serial)
    return event


# --- nightly ------------------------------------------------------------------------------------------------------

NIGHTLY_AT = time(4, 15)

type Verify = Callable[..., Awaitable[VerifyReport]]


async def verify_all(
    manager: SessionManager, *, verify: Verify = verify_device, source: str = "nightly"
) -> dict[int, VerifyReport | str]:
    """Verify every connected, adopted device. Never raises; returns serial -> report or error text."""
    results: dict[int, VerifyReport | str] = {}
    for session in manager:
        if session.state != SessionState.CONNECTED or not session.loaded or session.needs_adoption:
            continue
        try:
            results[session.serial] = await verify(session, source=source, persist_ok=False)
        except Exception as exc:
            log.warning("[%s] nightly verify failed: %s", session.serial, exc)
            results[session.serial] = publish_failure(session, exc, source=source)["error"]
    return results


async def nightly_verify_loop(
    manager: SessionManager,
    *,
    at: time = NIGHTLY_AT,
    clock: Callable[[], datetime] = datetime.now,
    verify: Verify = verify_device,
) -> None:
    """Verify once a night at ``at`` (local time). Only drift and failures reach the persistent event log."""
    while True:
        await asyncio.sleep(seconds_until(at, clock()))
        await verify_all(manager, verify=verify)
        await asyncio.sleep(1)  # the clock may still read `at` right after a fast run


def start_nightly_verify(manager: SessionManager, *, at: time = NIGHTLY_AT) -> asyncio.Task:
    """Lifespan hook: start the nightly verify task (cancel it on shutdown)."""
    return asyncio.create_task(nightly_verify_loop(manager, at=at), name="nightly-verify")


def parse_at(text: str) -> time:
    """``"04:15"`` -> ``time(4, 15)``; raises ValueError."""
    return time.fromisoformat(text.strip())


# --- MQTT drift sensor --------------------------------------------------------------------------------------------

DRIFT_OBJECT_ID = "drift"
DRIFT_STATES = ("ok", "drift", "mismatch", "unverified", "error")


def drift_topic(base_topic: str, serial: int) -> str:
    """Retained state topic of the drift sensor: ``<base>/<serial>/verify``."""
    return f"{base_topic.strip('/')}/{serial}/verify"


def drift_state(event: Mapping[str, Any]) -> dict[str, Any]:
    """Retained JSON for the drift sensor from a ``verify`` bus event."""
    return {
        "status": event.get("status"),
        "drift": event.get("drift"),
        "paths": list(event.get("paths") or []),
        "mismatch": event.get("mismatch"),
        "mismatch_paths": list(event.get("mismatch_paths") or []),
        "preset": None if event.get("preset") is None else event["preset"] + 1,
        "checked_at": event.get("checked_at"),
        "source": event.get("source"),
        "error": event.get("error"),
    }


def drift_discovery(
    *,
    discovery_prefix: str,
    base_topic: str,
    serial: int,
    device: Mapping[str, Any],
    origin: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """``(discovery topic, config)`` of the "Config drift" enum sensor (`DRIFT_STATES`), with the drifted and
    mismatched paths and counts as attributes. ``device`` is the bridge's shared device block."""
    base = base_topic.strip("/")
    state = drift_topic(base, serial)
    config: dict[str, Any] = {
        "name": "Config drift",
        "unique_id": f"minidsp_{serial}_{DRIFT_OBJECT_ID}",
        "device": dict(device),
        "availability": [{"topic": f"{base}/availability"}, {"topic": f"{base}/{serial}/availability"}],
        "availability_mode": "all",
        "state_topic": state,
        "value_template": "{{ value_json.status }}",
        "device_class": "enum",
        "options": list(DRIFT_STATES),
        "json_attributes_topic": state,
        "json_attributes_template": "{{ value_json | tojson }}",
        "entity_category": "diagnostic",
        "icon": "mdi:file-compare",
        "qos": 1,
    }
    if origin:
        config["origin"] = dict(origin)
    topic = f"{discovery_prefix.strip('/')}/sensor/minidsp_{serial}/{DRIFT_OBJECT_ID}/config"
    return topic, config
