"""Desired-state diff -> minidspd ``POST /devices/{i}/config`` payloads (docs 03 §4-5, docs 06 mapping table).

The applied shadow is a flat ``{path: value}`` map of what was last confirmed written; a missing path is unknown.
Values are the JSON-mode leaves of `flatten` (biquads as ``{b0, b1, b2, a1, a2}`` dicts).

Ordering rules that keep writes glitch-free, derived from the order minidspd applies each block:

- PEQ, compressor and FIR apply ``bypass`` *before* their coefficients/parameters, so an un-bypass is never sent in
  the same call as (or before) its coefficients: it waits for a later call, after the coefficients are confirmed.
- A crossover group applies its coefficients first, then ``bypass``, so an un-bypass may share the call with its
  coefficients. A re-bypass, the other way round, goes first and the new coefficients follow in a later call.
- Nothing is ever un-bypassed unless its coefficients are confirmed in the shadow (or written in this plan): missing
  ones are pulled into the plan from the desired state.
- FIR coefficients (up to 4096 floats) always travel alone.

By default every channel gets its own call. minidspd applies a payload sequentially and stops at the first error with
no rollback, so one call per channel keeps the "unknown" blast radius of a failure small (spike 0.6).
"""

import math
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.dsp.stability import biquad_problem
from app.model.config_doc import ConfigDoc, flatten, is_device_path
from app.model.device_profile import DeviceProfile

INPUTS = "inputs"
OUTPUTS = "outputs"

_COMPRESSOR_FIELDS = {
    "bypass": "bypass",
    "threshold_db": "threshold",
    "ratio": "ratio",
    "attack_ms": "attack",
    "release_ms": "release",
}
# Ranges the app accepts for compressor settings, on every write path. Threshold: spike results §5 (stored as plain
# dB, never clamped by the device). Ratio, attack and release can't be read back, so their units are the documented
# ones (x:1, ms) and the limits are conservative: a ratio below 1 would expand (add gain above the threshold).
COMPRESSOR_RANGES: dict[str, tuple[float, float]] = {
    "threshold_db": (-90.0, 0.0),
    "ratio": (1.0, 100.0),
    "attack_ms": (0.1, 2000.0),
    "release_ms": (1.0, 5000.0),
}
_COMPRESSOR_UNITS = {"threshold_db": " dB", "ratio": "", "attack_ms": " ms", "release_ms": " ms"}
_BIQUAD_KEYS = ("b0", "b1", "b2", "a1", "a2")


class PayloadError(ValueError):
    """A path or value can't be turned into a device write."""


@dataclass(frozen=True)
class Payload:
    """One ``POST /config`` body and the shadow values it confirms when it succeeds."""

    body: dict[str, Any]
    values: dict[str, Any]
    kind: str = "config"  # config | fir | mute

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(self.values)


@dataclass
class Plan:
    """Calls to send now, in order. ``deferred`` paths become sendable once these succeed; ``blocked`` never will."""

    payloads: list[Payload] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    blocked: dict[str, str] = field(default_factory=dict)

    @property
    def paths(self) -> list[str]:
        return [p for payload in self.payloads for p in payload.values]


# --- values and comparison --------------------------------------------------------------------------------------


def device_values(doc: ConfigDoc, profile: DeviceProfile) -> dict[str, Any]:
    """Every leaf path that is written to the device, in document order, with its JSON value.

    Design intent and metadata are skipped, as are PEQ bands marked ``external`` and FIR paths on a profile
    without FIR support.
    """
    external = {
        f"{side}.{n}.peq.{k}."
        for side, channels in ((INPUTS, doc.inputs), (OUTPUTS, doc.outputs))
        for n, ch in enumerate(channels)
        for k, band in enumerate(ch.peq)
        if band.external
    }
    values: dict[str, Any] = {}
    for path, value in flatten(doc).items():
        if not is_device_path(path):
            continue
        if not profile.fir_supported and ".fir." in path:
            continue
        if external and any(path.startswith(prefix) for prefix in external):
            continue
        values[path] = value
    return values


def same_value(path: str, a: Any, b: Any, profile: DeviceProfile) -> bool:
    """Whether two values of ``path`` end up identical on the device (delays compare as whole samples)."""
    if path.endswith(".delay_ms"):
        try:
            return profile.delay_samples(float(a)) == profile.delay_samples(float(b))
        except (TypeError, ValueError, OverflowError):
            return False
    return a == b


def diff(
    desired: ConfigDoc,
    applied: Mapping[str, Any],
    profile: DeviceProfile,
    *,
    include_unknown: bool = True,
) -> list[str]:
    """Device paths whose desired value isn't confirmed in the shadow, in document order.

    With ``include_unknown=False`` paths missing from the shadow are left out (they are unknown, not known-different).
    """
    out = []
    for path, value in device_values(desired, profile).items():
        if path not in applied:
            if include_unknown:
                out.append(path)
        elif not same_value(path, applied[path], value, profile):
            out.append(path)
    return out


def delay_duration(delay_ms: float, profile: DeviceProfile) -> dict[str, int]:
    """serde ``Duration`` for a delay, rounded to whole samples first.

    minidspd truncates to whole microseconds and then rounds to samples, so ``nanos = round(samples / fs * 1e9)``
    always lands back on the same sample count.
    """
    samples = profile.delay_samples(delay_ms)
    total_ns = round(samples * 1_000_000_000 / profile.sample_rate)
    secs, nanos = divmod(total_ns, 1_000_000_000)
    return {"secs": secs, "nanos": nanos}


# --- path parsing & validation ----------------------------------------------------------------------------------


def _parts(path: str) -> list[str | int]:
    return [int(p) if p.isdigit() else p for p in path.split(".")]


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def value_problem(path: str, value: Any, profile: DeviceProfile) -> str | None:
    """Why ``value`` can't be written at ``path`` on this device, or None. Last line of defence before the daemon:
    the daemon accepts out-of-range gains with HTTP 200 and fails half-way through a payload on bad indexes."""
    parts = _parts(path)
    if len(parts) < 3 or parts[0] not in (INPUTS, OUTPUTS) or not isinstance(parts[1], int):
        return "not a device path"
    side, n = parts[0], parts[1]
    count = profile.n_inputs if side == INPUTS else profile.n_outputs
    if not 0 <= n < count:
        return f"{side[:-1]} index {n} outside 0..{count - 1}"
    rest = parts[2:]
    key = rest[0]

    def gain() -> str | None:
        if not _finite(value):
            return "gain must be a finite number"
        if not profile.gain_min_db <= value <= profile.gain_max_db:
            return f"gain {value} dB outside {profile.gain_min_db:g}..{profile.gain_max_db:g}"
        return None

    def boolean() -> str | None:
        return None if isinstance(value, bool) else "expected true/false"

    def index(i: Any, limit: int, what: str) -> str | None:
        if not isinstance(i, int) or not 0 <= i < limit:
            return f"{what} index {i} outside 0..{limit - 1}"
        return None

    def biquad() -> str | None:
        if not isinstance(value, Mapping) or set(value) != set(_BIQUAD_KEYS):
            return "expected a biquad {b0, b1, b2, a1, a2}"
        return biquad_problem(value)

    match rest:
        case ["gain_db"]:
            return gain()
        case ["mute"]:
            return boolean()
        case ["invert"] if side == OUTPUTS:
            return boolean()
        case ["delay_ms"] if side == OUTPUTS:
            if not _finite(value) or value < 0:
                return "delay must be a finite, non-negative number of ms"
            if profile.delay_samples(value) > profile.delay_max_samples:
                return f"delay {value} ms above the device maximum of {profile.delay_max_ms:.3f} ms"
            return None
        case ["peq", k, ("bypass" | "compiled") as leaf]:
            limit = profile.peq_per_input if side == INPUTS else profile.peq_per_output
            return index(k, limit, "PEQ") or (boolean() if leaf == "bypass" else biquad())
        case ["routing", m, ("enabled" | "gain_db") as leaf] if side == INPUTS:
            return index(m, profile.n_outputs, "routing output") or (boolean() if leaf == "enabled" else gain())
        case ["crossover", g, "bypass"] if side == OUTPUTS:
            return index(g, profile.crossover_groups, "crossover group") or boolean()
        case ["crossover", g, "compiled", k] if side == OUTPUTS:
            return (
                index(g, profile.crossover_groups, "crossover group")
                or index(k, profile.crossover_biquads, "crossover biquad")
                or biquad()
            )
        case ["compressor", leaf] if side == OUTPUTS and leaf in _COMPRESSOR_FIELDS:
            if leaf == "bypass":
                return boolean()
            if not _finite(value):
                return f"compressor {leaf} must be a finite number"
            low, high = COMPRESSOR_RANGES[leaf]
            if not low <= value <= high:
                name = leaf.removesuffix("_db").removesuffix("_ms")
                return f"{name} {value:g}{_COMPRESSOR_UNITS[leaf]} outside {low:g}..{high:g}"
            return None
        case ["fir", ("bypass" | "blob_id") as leaf] if side == OUTPUTS:
            if not profile.fir_supported:
                return "this device has no FIR support"
            if leaf == "bypass":
                return boolean()
            return None if value is None or isinstance(value, str) else "expected a FIR blob id"
    return f"unknown device path {key!r}"


def fir_problem(taps: Sequence[float] | None, profile: DeviceProfile) -> str | None:
    if taps is None:
        return "FIR taps are not available"
    if len(taps) > profile.fir_max_taps:
        return f"FIR has {len(taps)} taps, the device takes at most {profile.fir_max_taps}"
    if not all(_finite(t) for t in taps):
        return "FIR taps must be finite numbers"
    return None


# --- prerequisites ----------------------------------------------------------------------------------------------


def _unbypass_prerequisites(path: str, profile: DeviceProfile) -> tuple[list[str], bool]:
    """For a ``…bypass`` path: the paths that must be on the device before it may be ``false``, and whether they can
    share the call (crossover only: coefficients are applied before the bypass flag)."""
    parts = path.split(".")
    base = ".".join(parts[:-1])
    if len(parts) == 5 and parts[2] == "peq":
        return [f"{base}.compiled"], False
    if len(parts) == 5 and parts[2] == "crossover":
        return [f"{base}.compiled.{k}" for k in range(profile.crossover_biquads)], True
    if len(parts) == 4 and parts[2] == "compressor":
        return [f"{base}.{f}" for f in _COMPRESSOR_FIELDS if f != "bypass"], False
    if len(parts) == 4 and parts[2] == "fir":
        return [f"{base}.blob_id"], False
    return [], True


def _is_bypass(path: str) -> bool:
    return path.endswith(".bypass")


def _crossover_group(path: str) -> str | None:
    parts = path.split(".")
    if len(parts) == 6 and parts[2] == "crossover" and parts[4] == "compiled":
        return ".".join(parts[:4])
    return None


# --- payload assembly -------------------------------------------------------------------------------------------


def _biquad(value: Mapping[str, Any], index: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {} if index is None else {"index": index}
    out.update({k: float(value[k]) for k in _BIQUAD_KEYS})
    return out


class _Channel:
    """Fragments for one input/output, rendered in the order minidspd applies them."""

    def __init__(self, side: str, index: int) -> None:
        self.side = side
        self.index = index
        self.scalars: dict[str, Any] = {}
        self.peq: dict[int, dict[str, Any]] = {}
        self.routing: dict[int, dict[str, Any]] = {}
        self.crossover: dict[int, dict[str, Any]] = {}
        self.compressor: dict[str, Any] = {}
        self.fir: dict[str, Any] = {}
        self.values: dict[str, Any] = {}

    def add(self, path: str, rest: list[str | int], value: Any, profile: DeviceProfile) -> None:
        match rest:
            case ["gain_db"]:
                self.scalars["gain"] = float(value)
            case ["mute"]:
                self.scalars["mute"] = value
            case ["invert"]:
                self.scalars["invert"] = value
            case ["delay_ms"]:
                self.scalars["delay"] = delay_duration(value, profile)
            case ["peq", int(k), "compiled"]:
                self.peq.setdefault(k, {})["coeff"] = _biquad(value)
            case ["peq", int(k), "bypass"]:
                self.peq.setdefault(k, {})["bypass"] = value
            case ["routing", int(m), "enabled"]:
                self.routing.setdefault(m, {})["mute"] = not value  # minidspd routing "mute" is inverted enable
            case ["routing", int(m), "gain_db"]:
                self.routing.setdefault(m, {})["gain"] = float(value)
            case ["crossover", int(g), "compiled", int(k)]:
                self.crossover.setdefault(g, {}).setdefault("coeff", {})[k] = _biquad(value, k)
            case ["crossover", int(g), "bypass"]:
                self.crossover.setdefault(g, {})["bypass"] = value
            case ["compressor", str(leaf)]:
                self.compressor[_COMPRESSOR_FIELDS[leaf]] = value if leaf == "bypass" else float(value)
            case ["fir", "bypass"]:
                self.fir["bypass"] = value
            case _:
                raise PayloadError(f"{path}: no payload mapping")
        self.values[path] = value

    def render(self) -> dict[str, Any]:
        out: dict[str, Any] = {"index": self.index}
        for key in ("mute", "gain"):
            if key in self.scalars:
                out[key] = self.scalars[key]
        if self.peq:
            out["peq"] = [{"index": k, **self.peq[k]} for k in sorted(self.peq)]
        if self.side == INPUTS:
            if self.routing:
                out["routing"] = [{"index": m, **self.routing[m]} for m in sorted(self.routing)]
            return out
        for key in ("invert", "delay"):
            if key in self.scalars:
                out[key] = self.scalars[key]
        if self.crossover:
            groups = []
            for g in sorted(self.crossover):
                frag: dict[str, Any] = {"index": g}
                coeffs = self.crossover[g].get("coeff")
                if coeffs:
                    frag["coeff"] = [coeffs[k] for k in sorted(coeffs)]
                if "bypass" in self.crossover[g]:
                    frag["bypass"] = self.crossover[g]["bypass"]
                groups.append(frag)
            out["crossover"] = groups
        if self.compressor:
            out["compressor"] = {k: self.compressor[k] for k in _COMPRESSOR_FIELDS.values() if k in self.compressor}
        if self.fir:
            out["fir"] = dict(self.fir)
        return out


def mute_payload(mutes: Mapping[int, bool]) -> Payload:
    """Set output mutes only (used to wrap bulk applies)."""
    outputs = sorted(mutes)
    return Payload(
        body={OUTPUTS: [{"index": n, "mute": mutes[n]} for n in outputs]},
        values={f"{OUTPUTS}.{n}.mute": mutes[n] for n in outputs},
        kind="mute",
    )


def build_plan(
    desired: ConfigDoc,
    applied: Mapping[str, Any],
    pending: Iterable[str],
    *,
    profile: DeviceProfile,
    fir_taps: Mapping[str, Sequence[float]] | None = None,
    skip: Collection[str] = (),
    split_channels: bool = True,
    check: Callable[[str, Any], str | None] | None = None,
) -> Plan:
    """Turn the ``pending`` paths into the calls to send now.

    ``applied`` is the shadow. ``skip`` lists paths that must not be pulled in as prerequisites (e.g. given up after
    failures). ``fir_taps`` maps FIR blob ids to their taps. ``check`` is an extra per-value check (e.g. per-channel
    gain ceilings); a path it objects to is blocked. Call again after the payloads succeed to get the
    deferred writes; the plan is empty once everything is on the device.
    """
    values = device_values(desired, profile)
    taps = fir_taps or {}
    plan = Plan()
    order = {p: i for i, p in enumerate(values)}

    def problem(path: str) -> str | None:
        if path not in values:
            return "not a device path for this device"
        found = value_problem(path, values[path], profile)
        if found is None and path.endswith(".fir.blob_id") and values[path] is not None:
            found = fir_problem(taps.get(values[path]), profile)
        if found is None and check is not None:
            found = check(path, values[path])
        return found

    def confirmed(path: str) -> bool:
        return path in applied and same_value(path, applied[path], values[path], profile)

    send: set[str] = set()
    for path in dict.fromkeys(pending):
        reason = problem(path)
        if reason is None:
            send.add(path)
        else:
            plan.blocked[path] = reason

    deferred: set[str] = set()
    for path in sorted((p for p in send if _is_bypass(p) and values[p] is False), key=order.__getitem__):
        prereqs, same_call = _unbypass_prerequisites(path, profile)
        needed = [q for q in prereqs if q in send or not confirmed(q)]
        unwritable = (
            (q, plan.blocked.get(q) or ("given up after failed writes" if q in skip else problem(q)))
            for q in needed
            if q not in send
        )
        bad = next(((q, reason) for q, reason in unwritable if reason is not None), None)
        if bad is not None:
            send.discard(path)
            plan.blocked[path] = f"needs {bad[0]} on the device first, which can't be written: {bad[1]}"
            continue
        send.update(needed)
        if needed and not same_call:
            send.discard(path)
            deferred.add(path)

    # A crossover re-bypass goes first; its new coefficients follow once the group is bypassed.
    rebypassed = {p.rsplit(".", 1)[0] for p in send if p.endswith(".bypass") and values[p] is True and "crossover" in p}
    for path in [p for p in send if _crossover_group(p) in rebypassed]:
        send.discard(path)
        deferred.add(path)

    channels: dict[tuple[str, int], _Channel] = {}
    fir_payloads: list[Payload] = []
    for path in sorted(send, key=order.__getitem__):
        parts = _parts(path)
        side, n, rest = str(parts[0]), int(parts[1]), parts[2:]
        if rest == ["fir", "blob_id"]:
            blob = values[path]
            coefficients = [] if blob is None else [float(t) for t in taps[blob]]
            fir_payloads.append(
                Payload(
                    body={OUTPUTS: [{"index": n, "fir": {"coefficients": coefficients}}]},
                    values={path: blob},
                    kind="fir",
                )
            )
            continue
        channels.setdefault((side, n), _Channel(side, n)).add(path, rest, values[path], profile)

    ordered = sorted(channels.values(), key=lambda c: (c.side != INPUTS, c.index))
    if split_channels:
        plan.payloads = [Payload(body={ch.side: [ch.render()]}, values=dict(ch.values)) for ch in ordered]
    elif ordered:
        body: dict[str, Any] = {}
        merged: dict[str, Any] = {}
        for ch in ordered:
            body.setdefault(ch.side, []).append(ch.render())
            merged.update(ch.values)
        plan.payloads = [Payload(body=body, values=merged)]
    plan.payloads.extend(fir_payloads)
    plan.deferred = sorted(deferred, key=order.__getitem__)
    return plan
