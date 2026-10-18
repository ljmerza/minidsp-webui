"""Plain-language summaries and health rules for one preset (phase 9 design, sections 6 and 7).

`summarize` is pure: it reads a desired document, the device profile, the channel metadata (names and the Settings
flags), the guidance thresholds, the per-path sync statuses and the last device check, and returns a `PresetSummary`.
Every screen that explains a preset (the dashboard cards today; later Home, the flow diagram, the problems pill, the
output page header, Routing totals, "Something wrong?" and the profile-save check) reads from it, so they can't
disagree. ``GET /api/v1/devices/{serial}/presets/{preset}/summary`` serves it as JSON and `session_summary` is the
helper the pages use.

Wording follows the design's vocabulary (section 2): Level (not gain), Band, EQ, Volume, Sent / Sending / Failed /
Unknown / Changed on device. Numbers use a real minus sign (U+2212).

Things the app can't know are not claimed: the badge says "Ready" (never "Playing"), the headroom line says "about",
and live-meter rules only run when the caller passes `Levels` (the meter thresholds are unverified, section 19).
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from app.model.config_doc import ConfigDoc, OutputCh, PeqBand
from app.model.device_profile import DeviceProfile
from app.model.filters import Biquad

MINUS = "−"
EN_DASH = "–"
ARROW = "→"

# --- thresholds -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    """The guidance thresholds (Settings, section 12.2). Starting guesses, not device facts (section 19)."""

    high_total_db: float = 12.0  # a total level at or above this is a caution (headroom)
    no_signal_dbfs: float = -90.0  # a meter below this reads "no signal"
    volume_quiet_db: float = -80.0  # Volume below this is "very quiet"
    comp_threshold_dbfs: float = -60.0  # a compressor at or below this threshold ...
    comp_ratio: float = 10.0  # ... with at least this ratio may hold the output almost silent

    @classmethod
    def from_settings(cls, settings: Any) -> "Thresholds":
        """From the app settings (``guidance_*`` fields); missing fields keep the defaults."""
        if settings is None:
            return cls()
        if isinstance(settings, Thresholds):
            return settings
        defaults = cls()
        return cls(
            high_total_db=float(getattr(settings, "guidance_high_total_db", defaults.high_total_db)),
            no_signal_dbfs=float(getattr(settings, "guidance_no_signal_dbfs", defaults.no_signal_dbfs)),
            volume_quiet_db=float(getattr(settings, "guidance_volume_quiet_db", defaults.volume_quiet_db)),
            comp_threshold_dbfs=float(getattr(settings, "guidance_comp_threshold_dbfs", defaults.comp_threshold_dbfs)),
            comp_ratio=float(getattr(settings, "guidance_comp_ratio", defaults.comp_ratio)),
        )


@dataclass(frozen=True)
class Levels:
    """Live meter readings (dBFS) and latched CLIP lights, for the live rules. ``clipped`` holds ``(kind, index)``."""

    inputs: Sequence[float | None] = ()
    outputs: Sequence[float | None] = ()
    clipped: frozenset[tuple[str, int]] = frozenset()


# --- result types -----------------------------------------------------------------------------------------------------

PROBLEM, CAUTION, INFO = "problem", "caution", "info"
LEVEL_RANK = {PROBLEM: 0, CAUTION: 1, INFO: 2}

# Rule codes in the order of the section 7 table (problems are sorted by level, then this order, then channel).
RULES = (
    "no_route",
    "only_source_muted",
    "band_empty",
    "filter_empty",
    "master_muted",
    "clip",
    "send_failed",
    "changed_on_device",
    "high_total",
    "no_signal_out",
    "limiter_crushing",
    "volume_very_low",
    "muted_routed",
    "input_unused",
    "editing_inactive",
    "unknown_settings",
)
_RULE_RANK = {code: n for n, code in enumerate(RULES)}

# Problems that stop all sound on an output: its badge says Silent.
SILENCING = frozenset({"no_route", "only_source_muted", "band_empty", "filter_empty", "master_muted"})

# Badge id -> (glyph, word). The ids match the glossary's ``b-*`` entries (section 13.2).
BADGES: dict[str, tuple[str, str]] = {
    "silent": ("⛔", "Silent"),
    "overload": ("⚠", "Overload"),
    "muted": ("🔇", "Muted"),
    "not_in_use": ("◌", "Not in use"),
    "check": ("⚠", "Check"),
    "ready": ("✓", "Ready"),
}

# Sync words (section 2), worst first.
SYNC_ORDER = ("failed", "changed", "pending", "unknown", "synced")
SYNC_WORDS = {
    "failed": "Failed",
    "changed": "Changed on device",
    "pending": "Sending",
    "unknown": "Unknown",
    "synced": "Sent",
}


@dataclass(frozen=True)
class Action:
    """A fix button. ``href`` is app-relative (pages prefix the root path); ``task`` names a guided task; ``params``
    carries what an inline action needs (e.g. the input and output of a route to turn on)."""

    id: str
    label: str
    href: str | None = None
    task: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Problem:
    code: str
    level: str  # problem | caution | info
    text: str
    kind: str | None = None  # "input" / "output", None for the whole device
    index: int | None = None
    actions: tuple[Action, ...] = ()

    @property
    def silences(self) -> bool:
        return self.code in SILENCING


@dataclass(frozen=True)
class Part:
    """One setting block of an output (the flow diagram's chips, section 5.2)."""

    label: str
    state: str  # on | default | caution | problem
    clause: str | None = None  # its part of the sentence, None when left out


@dataclass(frozen=True)
class RouteTotal:
    input: int
    name: str
    input_db: float
    route_db: float
    total_db: float
    muted: bool  # the input is muted


@dataclass(frozen=True)
class Total:
    """Level, total = input level + route level + output level (section 4.3 / 6.3)."""

    min: float
    max: float
    input: int | None  # the main input (the loudest route), None when nothing is routed
    routes: tuple[RouteTotal, ...]
    output: float
    ceiling: float  # the output's level limit (it also caps every route into it)
    at_limit: bool  # the output level is at its limit
    route_at_limit: bool  # every enabled route into it is at the limit too
    text: str  # "+16.0 dB" or "+10 to +16 dB"
    breakdown: str  # "route +4 + output +12"


@dataclass(frozen=True)
class Headroom:
    peak_dbfs: float | None  # input peaks above this overload the output (about); None at a total of 0 dB or less
    text: str
    level: str  # none | info | caution | problem


@dataclass(frozen=True)
class SyncView:
    headline: str  # Sent / Sending / Failed / Unknown / Changed on device / Waiting for preset N
    worst: str  # one of SYNC_ORDER
    by_block: dict[str, str]  # block -> worst status of its settings


@dataclass(frozen=True)
class OutputSummary:
    index: int
    name: str
    badge: str  # a BADGES key
    badge_glyph: str
    badge_word: str
    badge_reason: str | None  # the line that explains a Silent / Overload / Check badge
    sentence: str
    parts: dict[str, Part]
    total: Total
    headroom: Headroom
    problems: tuple[Problem, ...]
    sync: SyncView
    muted: bool
    unused: bool
    tactile: bool


@dataclass(frozen=True)
class SendTo:
    output: int
    name: str
    route_db: float


@dataclass(frozen=True)
class InputSummary:
    index: int
    name: str
    sentence: str
    sends_to: tuple[SendTo, ...]
    muted: bool
    level_db: float
    eq: int  # EQ bands that change the sound
    unused: bool
    problems: tuple[Problem, ...]


@dataclass(frozen=True)
class PresetSummary:
    preset: int | None
    outputs: tuple[OutputSummary, ...]
    inputs: tuple[InputSummary, ...]
    problems: tuple[Problem, ...]  # merged and ordered: problem > caution > info

    @property
    def counts(self) -> dict[str, int]:
        counts = {PROBLEM: 0, CAUTION: 0, INFO: 0}
        for p in self.problems:
            counts[p.level] += 1
        return counts

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["counts"] = self.counts
        for problem in data["problems"]:
            problem["silences"] = problem["code"] in SILENCING
        for out in data["outputs"]:
            for problem in out["problems"]:
                problem["silences"] = problem["code"] in SILENCING
        return data


# --- number and name formatting ---------------------------------------------------------------------------------------


def _clean(value: float, digits: int = 2) -> float:
    rounded = round(value, digits)
    return 0.0 if rounded == 0 else rounded


def signed(value: float, digits: int = 2) -> str:
    """``+4``, ``−2.5``, ``0`` (a real minus sign, no trailing zeros)."""
    v = _clean(value, digits)
    if v > 0:
        return f"+{v:g}"
    if v < 0:
        return f"{MINUS}{-v:g}"
    return "0"


def signed1(value: float) -> str:
    """One decimal: ``+16.0``, ``−3.5``, ``0.0``."""
    v = _clean(value, 1)
    if v > 0:
        return f"+{v:.1f}"
    if v < 0:
        return f"{MINUS}{-v:.1f}"
    return "0.0"


def plain(value: float, digits: int = 2) -> str:
    v = _clean(value, digits)
    return f"{MINUS}{-v:g}" if v < 0 else f"{v:g}"


def _hz_number(freq: float) -> tuple[str, str]:
    if freq >= 1000:
        return f"{_clean(freq / 1000, 2):g}", "kHz"
    return f"{_clean(freq, 1):g}", "Hz"


def hz(freq: float) -> str:
    number, unit = _hz_number(freq)
    return f"{number} {unit}"


def hz_range(low: float, high: float) -> str:
    """``20–80 Hz``; ``500 Hz–2 kHz`` when the units differ."""
    (a, ua), (b, ub) = _hz_number(low), _hz_number(high)
    return f"{a}{EN_DASH}{b} {ub}" if ua == ub else f"{a} {ua}{EN_DASH}{b} {ub}"


def hz_list(freqs: Sequence[float]) -> str:
    """``40, 63 Hz`` (one unit at the end when they share it)."""
    parts = [_hz_number(f) for f in freqs]
    if len({unit for _, unit in parts}) == 1:
        return ", ".join(n for n, _ in parts) + f" {parts[0][1]}"
    return ", ".join(f"{n} {u}" for n, u in parts)


def join_words(items: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def default_label(kind: str, index: int) -> str:
    return f"{'In' if kind == 'input' else 'Out'} {index + 1}"


def _meta(channel_meta: Mapping[tuple[str, int], Any] | None, kind: str, index: int) -> Any:
    return (channel_meta or {}).get((kind, index))


def channel_name(channel_meta: Mapping[tuple[str, int], Any] | None, kind: str, index: int) -> str:
    """The name from Settings, else "In 1" / "Out 1" (the same rule as the pages' channel labels)."""
    meta = _meta(channel_meta, kind, index)
    name = getattr(meta, "name", None) if meta is not None else None
    return name or default_label(kind, index)


def _flag(channel_meta: Mapping[tuple[str, int], Any] | None, kind: str, index: int, flag: str) -> bool:
    meta = _meta(channel_meta, kind, index)
    return bool(getattr(meta, flag, False)) if meta is not None else False


def output_ceiling(profile: DeviceProfile, channel_meta: Mapping[tuple[str, int], Any] | None, index: int) -> float:
    """The output's level limit: the profile maximum, lowered by its Settings ceiling (it caps routes into it too)."""
    meta = _meta(channel_meta, "output", index)
    ceiling = profile.gain_max_db
    limit = getattr(meta, "max_gain_db", None) if meta is not None else None
    if limit is not None:
        ceiling = min(ceiling, limit)
    return ceiling


# --- filter analysis --------------------------------------------------------------------------------------------------


def is_empty(biquad: Biquad) -> bool:
    """An all-zero filter (``b0 = b1 = b2 = 0``) passes no sound at all."""
    return biquad.b0 == 0.0 and biquad.b1 == 0.0 and biquad.b2 == 0.0


def steepness(order: int) -> str:
    """6 dB/oct per order: up to 12 "gentle", 18 to 30 "steep", 36 and more "very steep"."""
    slope = 6 * order
    if slope <= 12:
        return "gentle"
    if slope < 36:
        return "steep"
    return "very steep"


@dataclass(frozen=True)
class _Band:
    hp: tuple[float, int] | None = None  # (freq, order) of the effective high-pass
    lp: tuple[float, int] | None = None
    custom: bool = False  # an active filter without a recognised design
    empty: tuple[int, ...] = ()  # indexes of active all-zero band filters

    @property
    def nothing_passes(self) -> bool:
        return self.hp is not None and self.lp is not None and self.hp[0] >= self.lp[0]

    @property
    def default(self) -> bool:
        return self.hp is None and self.lp is None and not self.custom and not self.empty


def analyse_band(out: OutputCh) -> _Band:
    hps: list[tuple[float, int]] = []
    lps: list[tuple[float, int]] = []
    custom = False
    empty: list[int] = []
    for g, group in enumerate(out.crossover):
        if group.bypass:
            continue
        if any(is_empty(bq) for bq in group.compiled):
            empty.append(g)
            continue
        design = group.design
        if design is None:
            if group.compiled and not all(bq.is_passthrough for bq in group.compiled):
                custom = True
            continue
        if design.kind == "hp":
            hps.append((design.freq, design.order))
        elif design.kind == "lp":
            lps.append((design.freq, design.order))
        else:  # band-pass: a high-pass at freq and a low-pass at freq_hi
            hps.append((design.freq, design.order))
            lps.append((design.freq_hi or design.freq, design.order))
    hp = max(hps, key=lambda e: e[0]) if hps else None
    lp = min(lps, key=lambda e: e[0]) if lps else None
    return _Band(hp=hp, lp=lp, custom=custom, empty=tuple(empty))


# PEQ design types other than cuts, boosts and shelves: what to call them in a sentence.
_EQ_NOUNS = {
    "lp1": "low-pass",
    "lp2": "low-pass",
    "hp1": "high-pass",
    "hp2": "high-pass",
    "bandpass": "band-pass",
    "notch": "notch",
    "ap1": "all-pass",
    "ap2": "all-pass",
    "lt": "Linkwitz transform",
    "gain": "level step",
}


@dataclass(frozen=True)
class _Eq:
    cuts: tuple[float, ...] = ()
    boosts: tuple[float, ...] = ()
    shelves: tuple[str, ...] = ()  # finished phrases ("bass shelf +3 dB below 30 Hz")
    others: tuple[str, ...] = ()  # finished phrases ("EQ notch at 60 Hz", "custom EQ filter")
    empty: tuple[int, ...] = ()  # indexes of active all-zero bands

    @property
    def count(self) -> int:
        return len(self.cuts) + len(self.boosts) + len(self.shelves) + len(self.others)


def analyse_eq(bands: Sequence[PeqBand]) -> _Eq:
    cuts: list[float] = []
    boosts: list[float] = []
    shelves: list[str] = []
    others: list[str] = []
    empty: list[int] = []
    for k, band in enumerate(bands):
        if band.bypass:
            continue
        if is_empty(band.compiled):
            empty.append(k)
            continue
        d = band.design
        if d.type == "passthrough":
            if not band.compiled.is_passthrough:
                others.append("custom EQ filter")
            continue
        if d.type == "raw":
            if not band.compiled.is_passthrough:
                others.append("custom EQ filter")
            continue
        if d.type == "peaking":
            gain = d.gain_db or 0.0
            if _clean(gain) < 0:
                cuts.append(d.freq or 0.0)
            elif _clean(gain) > 0:
                boosts.append(d.freq or 0.0)
            continue
        if d.type in ("lowshelf", "highshelf"):
            gain = d.gain_db or 0.0
            if _clean(gain) == 0:
                continue
            where = "below" if d.type == "lowshelf" else "above"
            noun = "bass shelf" if d.type == "lowshelf" else "treble shelf"
            shelves.append(f"{noun} {signed(gain)} dB {where} {hz(d.freq or 0.0)}")
            continue
        noun = _EQ_NOUNS.get(d.type, "custom")
        if d.type == "gain":
            others.append(f"EQ level step {signed(d.gain_db or 0.0)} dB")
        elif d.type == "lt":
            others.append("EQ Linkwitz transform")
        elif d.freq is not None:
            others.append(f"EQ {noun} at {hz(d.freq)}")
        else:
            others.append(f"EQ {noun}")
    return _Eq(tuple(cuts), tuple(boosts), tuple(shelves), tuple(others), tuple(empty))


def eq_clause(eq: _Eq) -> str | None:
    """``1 EQ cut at 40 Hz``, ``2 EQ cuts (40, 63 Hz)``, ``1 EQ boost at 25 Hz``, ``bass shelf +3 dB below 30 Hz``;
    more than three bands: ``5 EQ bands on``. None when no band changes the sound."""
    n = eq.count
    if n == 0:
        return None
    if n > 3:
        return f"{n} EQ bands on"
    phrases: list[str] = []
    for freqs, word in ((eq.cuts, "cut"), (eq.boosts, "boost")):
        if len(freqs) == 1:
            phrases.append(f"1 EQ {word} at {hz(freqs[0])}")
        elif freqs:
            phrases.append(f"{len(freqs)} EQ {word}s ({hz_list(sorted(freqs))})")
    phrases.extend(eq.shelves)
    phrases.extend(f"1 {o}" if not o.startswith(("bass", "treble")) else o for o in eq.others)
    return join_words(phrases)


def eq_label(eq: _Eq) -> str:
    """The flow diagram's EQ block: ``EQ none``, ``EQ 1 cut``, ``EQ 2 cuts``, ``EQ 1 boost``, ``EQ 3 bands``."""
    n = eq.count
    if n == 0:
        return "EQ none"
    if n == len(eq.cuts):
        return f"EQ {_plural(n, 'cut')}"
    if n == len(eq.boosts):
        return f"EQ {_plural(n, 'boost')}"
    return f"EQ {_plural(n, 'band')}"


# --- sync -------------------------------------------------------------------------------------------------------------

_OUTPUT_BLOCKS = {
    "gain_db": "level",
    "mute": "mute",
    "invert": "polarity",
    "delay_ms": "delay",
    "peq": "eq",
    "crossover": "band",
    "compressor": "compressor",
    "fir": "fir",
}


def _block_of(path: str) -> tuple[str, int, str] | None:
    """``(kind, index, block)`` of a device path. A route's settings belong to the output's ``sources`` block."""
    parts = path.split(".")
    if len(parts) < 3 or not parts[1].isdigit():
        return None
    if parts[0] == "outputs":
        return "output", int(parts[1]), _OUTPUT_BLOCKS.get(parts[2], parts[2])
    if parts[0] == "inputs":
        if parts[2] == "routing" and len(parts) > 3 and parts[3].isdigit():
            return "output", int(parts[3]), "sources"
        return "input", int(parts[1]), {"gain_db": "level", "mute": "mute", "peq": "eq"}.get(parts[2], parts[2])
    return None


def _owner(path: str) -> tuple[str, int] | None:
    """The channel a path belongs to (route paths belong to their input, like `describe_path`)."""
    parts = path.split(".")
    if len(parts) < 2 or not parts[1].isdigit() or parts[0] not in ("inputs", "outputs"):
        return None
    return ("input" if parts[0] == "inputs" else "output"), int(parts[1])


def _worst(statuses: Iterable[str]) -> str:
    seen = set(statuses)
    return next((s for s in SYNC_ORDER if s in seen), "synced")


def _verify_paths(last_verify: Any, preset: int | None) -> set[str]:
    """Drifted paths of the last device check of this preset, minus those since used or forgotten.

    ``last_verify`` is a `VerifyReport`, its ``as_json()`` / ``event()`` dict, or None."""
    if last_verify is None:
        return set()
    if hasattr(last_verify, "as_json"):
        last_verify = last_verify.as_json()
    if not isinstance(last_verify, Mapping):
        return set()
    report_preset = last_verify.get("preset")
    if preset is not None and report_preset is not None and report_preset != preset:
        return set()
    drifted = set(last_verify.get("paths") or ())
    settled = set(last_verify.get("accepted") or ()) | set(last_verify.get("marked_unknown") or ())
    return {p for p in drifted - settled if not any(p == s or p.startswith(s + ".") for s in settled)}


# --- the summary ------------------------------------------------------------------------------------------------------


def _master_value(master: Any, name: str) -> Any:
    if master is None:
        return None
    if isinstance(master, Mapping):
        return master.get(name)
    return getattr(master, name, None)


def _out_href(index: int, tab: str | None = None) -> str:
    return f"/outputs/{index + 1}" + (f"?tab={tab}" if tab else "")


def _sort_key(p: Problem) -> tuple[int, int, int, int]:
    kind_rank = {"output": 0, "input": 1}.get(p.kind or "", 2)
    return (LEVEL_RANK[p.level], _RULE_RANK.get(p.code, len(RULES)), kind_rank, p.index if p.index is not None else -1)


@dataclass(frozen=True)
class _Ctx:
    """What every channel's summary needs: names, flags, thresholds, live levels and the per-channel sync facts."""

    profile: DeviceProfile
    channel_meta: Mapping[tuple[str, int], Any] | None
    th: Thresholds
    levels: Levels | None
    master_mute: bool
    preset: int | None
    staged: bool
    route_hints: Mapping[int, int]
    in_names: list[str]
    out_names: list[str]
    failed_by: dict[tuple[str, int], int]
    changed_by: dict[tuple[str, int], int]
    block_status: dict[tuple[str, int], dict[str, list[str]]]

    def sync_problems(self, kind: str, index: int, name: str) -> list[Problem]:
        found = []
        if n := self.failed_by.get((kind, index)):
            found.append(
                Problem(
                    "send_failed",
                    PROBLEM,
                    f"{_plural(n, 'setting')} on {name} didn't reach the device.",
                    kind,
                    index,
                    (Action("retry", "Retry"), Action("device_sync", "Device & sync", href="/device")),
                )
            )
        if n := self.changed_by.get((kind, index)):
            found.append(
                Problem(
                    "changed_on_device",
                    CAUTION,
                    f"{_plural(n, 'setting')} on {name} changed on the device since the app sent them.",
                    kind,
                    index,
                    (
                        Action("send_app_values", "Send the app's values"),
                        Action("use_device_values", "Use the device's values…", href="/device"),
                    ),
                )
            )
        return found


def summarize(
    doc: ConfigDoc,
    profile: DeviceProfile,
    channel_meta: Mapping[tuple[str, int], Any] | None,
    settings: Any,
    statuses: Mapping[str, str] | None,
    last_verify: Any,
    levels: Levels | None = None,
    *,
    preset: int | None = None,
    master: Any = None,
    staged: bool = False,
    route_hints: Mapping[int, int] | None = None,
) -> PresetSummary:
    """The plain-language summary and health of one preset (sections 6 and 7). Pure: no I/O.

    ``statuses`` maps device paths to synced / pending / failed / unknown (`DeviceSession.sync_status`);
    ``last_verify`` is the last device check (see `_verify_paths`); ``levels`` turns on the live-meter rules;
    ``master`` (a `MasterStatus` or dict) adds Mute all, Volume and "editing a preset that isn't playing";
    ``staged`` says the preset's pending writes wait for it to be played; ``route_hints`` maps an output to the
    input to suggest in its "Route In N → Out" fix (default In 1)."""
    th = Thresholds.from_settings(settings)
    statuses = statuses or {}
    volume = _master_value(master, "volume")
    playing = _master_value(master, "preset")

    # Per-channel sync facts.
    failed_by: dict[tuple[str, int], int] = {}
    changed_by: dict[tuple[str, int], int] = {}
    block_status: dict[tuple[str, int], dict[str, list[str]]] = {}
    for path, status in statuses.items():
        owner = _owner(path)
        if owner is not None and status == "failed":
            failed_by[owner] = failed_by.get(owner, 0) + 1
        located = _block_of(path)
        if located is not None:
            kind, index, block = located
            block_status.setdefault((kind, index), {}).setdefault(block, []).append(status)
    for path in _verify_paths(last_verify, preset):
        owner = _owner(path)
        if owner is not None:
            changed_by[owner] = changed_by.get(owner, 0) + 1
        located = _block_of(path)
        if located is not None:
            kind, index, block = located
            block_status.setdefault((kind, index), {}).setdefault(block, []).append("changed")

    ctx = _Ctx(
        profile=profile,
        channel_meta=channel_meta,
        th=th,
        levels=levels,
        master_mute=_master_value(master, "mute") is True,
        preset=preset,
        staged=staged,
        route_hints=route_hints or {},
        in_names=[channel_name(channel_meta, "input", i) for i in range(len(doc.inputs))],
        out_names=[channel_name(channel_meta, "output", n) for n in range(len(doc.outputs))],
        failed_by=failed_by,
        changed_by=changed_by,
        block_status=block_status,
    )
    inputs = [_input_summary(ctx, doc, i) for i in range(len(doc.inputs))]
    outputs = [_output_summary(ctx, doc, n) for n in range(len(doc.outputs))]
    problems = [p for item in (*inputs, *outputs) for p in item.problems]

    # --- device-wide rules --------------------------------------------------------------------------------------------
    if ctx.master_mute:
        problems.append(_master_problem())
    if isinstance(volume, int | float) and math.isfinite(volume) and volume < th.volume_quiet_db:
        problems.append(
            Problem(
                "volume_very_low",
                INFO,
                f"Volume is at {plain(volume, 1)} dB, which is very quiet.",
                None,
                None,
                (Action("set_volume", f"Set {MINUS}30 dB", params={"volume": -30.0}),),
            )
        )
    if preset is not None and isinstance(playing, int) and playing != preset:
        problems.append(
            Problem(
                "editing_inactive",
                INFO,
                f"You're editing preset {preset + 1}, which isn't playing.",
                None,
                None,
                (
                    Action("switch_preset", f"Switch to preset {preset + 1}…", params={"preset": preset}),
                    Action("edit_preset", f"Edit preset {playing + 1} instead", params={"preset": playing}),
                ),
            )
        )
    unknown = sum(1 for s in statuses.values() if s == "unknown")
    if unknown:
        problems.append(
            Problem(
                "unknown_settings",
                INFO,
                f"{_plural(unknown, 'setting')} {'is' if unknown == 1 else 'are'} unknown: "
                "the app hasn't sent or read them yet.",
                None,
                None,
                (Action("check_device", "Check device", href="/device"), Action("send_all", "Send all…")),
            )
        )

    return PresetSummary(preset, tuple(outputs), tuple(inputs), tuple(sorted(problems, key=_sort_key)))


def _input_summary(ctx: _Ctx, doc: ConfigDoc, i: int) -> InputSummary:
    ch = doc.inputs[i]
    in_names, out_names, th, levels, channel_meta = ctx.in_names, ctx.out_names, ctx.th, ctx.levels, ctx.channel_meta
    name = in_names[i]
    unused = _flag(channel_meta, "input", i, "unused")
    sends = tuple(
        SendTo(o, out_names[o] if o < len(out_names) else default_label("output", o), cell.gain_db)
        for o, cell in enumerate(ch.routing)
        if cell.enabled and o < len(doc.outputs)
    )
    eq = analyse_eq(ch.peq)
    mine: list[Problem] = []
    for k in eq.empty:
        mine.append(
            Problem(
                "filter_empty",
                PROBLEM,
                f"{name} is silent: its EQ band {k + 1} is empty (all zeros).",
                "input",
                i,
                (
                    Action("reset_filter", "Reset this filter to flat", params={"path": f"inputs.{i}.peq.{k}"}),
                    Action("open", "Open", href=f"/inputs/{i + 1}"),
                ),
            )
        )
    mine.extend(ctx.sync_problems("input", i, name))
    if levels is not None and ("input", i) in levels.clipped:
        mine.append(
            Problem(
                "clip",
                PROBLEM,
                f"{name} clipped.",
                "input",
                i,
                (Action("set_levels", "Set levels…", task="set-levels"), Action("reset_clip", "Reset CLIP")),
            )
        )
    if not sends and not unused:
        has_signal = _has_signal(levels.inputs if levels else (), i, th)
        mine.append(
            Problem(
                "input_unused",
                CAUTION if has_signal else INFO,
                f"{name} isn't sent to any output.",
                "input",
                i,
                (
                    Action("open_routing", "Open Routing", href="/routing"),
                    Action(
                        "mark_unused",
                        f"Mark {name} not in use",
                        href="/settings",
                        params={"kind": "input", "index": i},
                    ),
                ),
            )
        )
    if sends:
        to = join_words([s.name + (f" ({signed(s.route_db)} dB)" if _clean(s.route_db) else "") for s in sends])
        eq_words = "no EQ" if eq.count == 0 else _plural(eq.count, "EQ band")
        sentence = f"Level {signed(ch.gain_db)} dB, {eq_words}. Sent to {to}."
    else:
        sentence = "Not sent to any output."
    if ch.mute:
        sentence = "Muted. " + sentence
    return InputSummary(
        i, name, sentence, sends, ch.mute, ch.gain_db, eq.count, unused, tuple(sorted(mine, key=_sort_key))
    )


def _output_summary(ctx: _Ctx, doc: ConfigDoc, n: int) -> OutputSummary:
    out = doc.outputs[n]
    profile, in_names, out_names, th, levels = ctx.profile, ctx.in_names, ctx.out_names, ctx.th, ctx.levels
    channel_meta, master_mute, preset, staged = ctx.channel_meta, ctx.master_mute, ctx.preset, ctx.staged
    route_hints = ctx.route_hints
    block_status = ctx.block_status
    name = out_names[n]
    unused = _flag(channel_meta, "output", n, "unused")
    tactile = _flag(channel_meta, "output", n, "tactile")
    ceiling = output_ceiling(profile, channel_meta, n)
    route_cells = [(i, ch, ch.routing[n]) for i, ch in enumerate(doc.inputs) if n < len(ch.routing)]
    routes = tuple(
        RouteTotal(i, in_names[i], ch.gain_db, cell.gain_db, ch.gain_db + cell.gain_db + out.gain_db, ch.mute)
        for i, ch, cell in route_cells
        if cell.enabled
    )
    total = _total(routes, out.gain_db, ceiling)
    band = analyse_band(out)
    eq = analyse_eq(out.peq)
    clipped = levels is not None and ("output", n) in levels.clipped
    main_input = in_names[total.input] if total.input is not None else None
    fir_on = not out.fir.bypass and out.fir.blob_id is not None
    comp = out.compressor
    mine: list[Problem] = []

    # Silencing problems (section 7, in table order).
    if not routes and not unused:
        hint = (route_hints or {}).get(n, 0)
        hint = hint if 0 <= hint < len(in_names) else 0
        route_label = f"Route {in_names[hint]} {ARROW} {name}" if in_names else "Open Routing"
        mine.append(
            Problem(
                "no_route",
                PROBLEM,
                f"{name} is silent: no input is routed to it.",
                "output",
                n,
                (
                    Action("route", route_label, params={"input": hint, "output": n}),
                    Action("open_routing", "Open Routing", href="/routing"),
                ),
            )
        )
    muted_sources = [r for r in routes if r.muted]
    if routes and len(muted_sources) == len(routes) and not unused:
        names = [r.name for r in muted_sources]
        which = "which is" if len(names) == 1 else "which are"
        mine.append(
            Problem(
                "only_source_muted",
                PROBLEM,
                f"{name} only gets {join_words(names)}, {which} muted.",
                "output",
                n,
                tuple(Action("unmute_input", f"Unmute {r.name}", params={"input": r.input}) for r in muted_sources),
            )
        )
    if band.nothing_passes and band.hp is not None and band.lp is not None:
        mine.append(
            Problem(
                "band_empty",
                PROBLEM,
                f"{name} plays nothing: its high-pass ({hz(band.hp[0])}) is above its low-pass ({hz(band.lp[0])}).",
                "output",
                n,
                (Action("open_band", "Open Band", href=_out_href(n, "crossover")),),
            )
        )
    for g in band.empty:
        mine.append(_empty_problem(name, n, f"band filter {g + 1}", f"outputs.{n}.crossover.{g}", "crossover"))
    for k in eq.empty:
        mine.append(_empty_problem(name, n, f"EQ band {k + 1}", f"outputs.{n}.peq.{k}", "peq"))
    if clipped:
        mine.append(
            Problem(
                "clip",
                PROBLEM,
                f"{name} clipped.",
                "output",
                n,
                (Action("set_levels", "Set levels…", task="set-levels"), Action("reset_clip", "Reset CLIP")),
            )
        )
    mine.extend(ctx.sync_problems("output", n, name))
    audible = bool(routes) and not out.mute and not master_mute
    if audible and total.max >= th.high_total_db and main_input is not None:
        t = plain(total.max, 1)
        mine.append(
            Problem(
                "high_total",
                CAUTION,
                f"{name}: +{t} dB total. Peaks above about {MINUS}{t} dBFS on {main_input} will overload it.",
                "output",
                n,
                (Action("set_levels", "Set levels…", task="set-levels"),),
            )
        )
    if levels is not None and audible and not any(p.silences for p in mine):
        fed = [r for r in routes if not r.muted and _has_signal(levels.inputs, r.input, th, margin=30.0)]
        out_level = _level(levels.outputs, n)
        if fed and out_level is not None and out_level < th.no_signal_dbfs:
            mine.append(
                Problem(
                    "no_signal_out",
                    CAUTION,
                    f"{fed[0].name} has signal but {name} shows none.",
                    "output",
                    n,
                    (Action("diagnose", "Something wrong?", task="diagnose"),),
                )
            )
    crushing = not comp.bypass and comp.threshold_db <= th.comp_threshold_dbfs and comp.ratio >= th.comp_ratio
    if crushing:
        mine.append(
            Problem(
                "limiter_crushing",
                CAUTION,
                f"{name}'s compressor starts at {plain(comp.threshold_db, 1)} dBFS with a {plain(comp.ratio, 1)}:1 "
                "ratio, so it may hold it almost silent.",
                "output",
                n,
                (Action("open_compressor", "Open Compressor", href=_out_href(n, "compressor")),),
            )
        )
    if out.mute and routes and not unused:
        mine.append(
            Problem(
                "muted_routed",
                INFO,
                f"{name} is muted, but {join_words([r.name for r in routes])} "
                f"{'is' if len(routes) == 1 else 'are'} sent to it.",
                "output",
                n,
                (
                    Action("unmute_output", "Unmute", params={"output": n}),
                    Action(
                        "mark_unused",
                        f"Mark {name} not in use",
                        href="/settings",
                        params={"kind": "output", "index": n},
                    ),
                ),
            )
        )

    # Headroom (6.3).
    headroom = _headroom(name, total, main_input, clipped, th)

    # Sentence (6.1, 6.2).
    sources = _sources_clause(routes)
    verb = "Would play" if out.mute else "Plays"
    problem_clause = None
    if band.empty:
        problem_clause = "Silent: its band filter is empty"
    elif eq.empty:
        problem_clause = f"Silent: EQ band {eq.empty[0] + 1} is empty"
    elif band.nothing_passes and band.hp is not None and band.lp is not None:
        problem_clause = f"{verb} nothing: its high-pass ({hz(band.hp[0])}) is above its low-pass ({hz(band.lp[0])})"
    band_clause = problem_clause or _band_clause(band, verb)
    eq_text = eq_clause(eq)
    delay_text = f"Waits {plain(out.delay_ms, 2)} ms" if _clean(out.delay_ms) > 0 else None
    polarity_text = "Polarity flipped (180°)" if out.invert else None
    comp_text = (
        f"Compressor on above {plain(comp.threshold_db, 1)} dBFS ({plain(comp.ratio, 1)}:1)"
        if not comp.bypass
        else None
    )
    fir_text = "FIR filter loaded" if fir_on else None
    extras = [t for t in (eq_text, delay_text, polarity_text, comp_text, fir_text) if t]
    if out.mute and unused:
        sentence = "Not in use: muted."
    else:
        unprocessed = bool(routes) and not out.mute and problem_clause is None and band.default and not extras
        head = band_clause + (", unprocessed" if unprocessed else "")
        head = f"{head}. Fed by {sources}." if problem_clause else f"{head}, fed by {sources}."
        sentence = " ".join([head, *(_sentence(t) for t in extras)])
        if out.mute:
            sentence = "Muted. " + sentence

    # Blocks (5.1 / 5.2).
    level_state = "on"
    if clipped:
        level_state = "problem"
    elif headroom.level == CAUTION or total.at_limit:
        level_state = "caution"
    level_label = f"Level {signed(total.max, 1)}" + (" ▲max" if total.at_limit else "")
    if band.empty:
        band_part = Part("Band: filter empty", "problem", problem_clause)
    elif band.nothing_passes:
        band_part = Part("Band: nothing passes", "problem", band_clause)
    elif band.custom:
        band_part = Part("Band: custom", "on", band_clause)
    elif band.default:
        band_part = Part("Full range", "default", band_clause)
    else:
        band_part = Part("Band " + _band_short(band), "on", band_clause)
    parts = {
        "level": Part(level_label, level_state, None),
        "sources": Part(
            f"Fed by {sources}",
            "problem" if any(p.code in ("no_route", "only_source_muted") for p in mine) else "on",
            f"fed by {sources}",
        ),
        "band": band_part,
        "eq": Part(
            "EQ: filter empty" if eq.empty else eq_label(eq),
            "problem" if eq.empty else ("default" if eq.count == 0 else "on"),
            eq_text,
        ),
        "delay": Part(f"Delay {plain(out.delay_ms, 2)}", "on" if delay_text else "default", delay_text),
        "polarity": Part(
            "Polarity 180°" if out.invert else "Polarity 0°", "on" if out.invert else "default", polarity_text
        ),
        "compressor": Part(
            "Comp. off" if comp.bypass else f"Comp. {plain(comp.threshold_db, 1)} dBFS {plain(comp.ratio, 1)}:1",
            "caution" if crushing else ("on" if not comp.bypass else "default"),
            comp_text,
        ),
        "mute": Part("Muted" if out.mute else "Unmuted", "on" if out.mute else "default", None),
    }
    if fir_on:
        parts["fir"] = Part("FIR on", "on", fir_text)

    # Sync view.
    blocks = block_status.get(("output", n), {})
    by_block = {block: _worst(values) for block, values in blocks.items()}
    worst = _worst(by_block.values()) if by_block else "synced"
    headline = SYNC_WORDS[worst]
    if worst == "pending" and staged and preset is not None:
        headline = f"Waiting for preset {preset + 1}"
    sync = SyncView(headline, worst, by_block)

    # Badge (6.4).
    silencing = [p for p in mine if p.silences]
    if master_mute:
        silencing.append(_master_problem())
    own_issues = [p for p in mine if p.level in (PROBLEM, CAUTION) and not p.silences and p.code != "clip"]
    not_sent = sum(1 for values in blocks.values() for s in values if s in ("unknown", "failed"))
    reason: str | None = None
    if out.mute and unused:
        badge = "not_in_use"
    elif silencing:
        badge, reason = "silent", silencing[0].text
    elif clipped:
        badge, reason = "overload", headroom.text
    elif out.mute:
        badge = "muted"
    elif own_issues:
        badge, reason = "check", own_issues[0].text
    elif not_sent:
        badge, reason = "check", f"{_plural(not_sent, 'setting')} on {name} not sent yet."
    else:
        badge = "ready"
    glyph, word = BADGES[badge]
    return OutputSummary(
        index=n,
        name=name,
        badge=badge,
        badge_glyph=glyph,
        badge_word=word,
        badge_reason=reason,
        sentence=sentence,
        parts=parts,
        total=total,
        headroom=headroom,
        problems=tuple(sorted(mine, key=_sort_key)),
        sync=sync,
        muted=out.mute,
        unused=unused,
        tactile=tactile,
    )


# --- helpers ----------------------------------------------------------------------------------------------------------


def _level(values: Sequence[float | None], index: int) -> float | None:
    if index >= len(values):
        return None
    value = values[index]
    return value if isinstance(value, int | float) and math.isfinite(value) else None


def _has_signal(values: Sequence[float | None], index: int, th: Thresholds, margin: float = 0.0) -> bool:
    level = _level(values, index)
    return level is not None and level > th.no_signal_dbfs + margin


def _master_problem() -> Problem:
    return Problem(
        "master_muted",
        PROBLEM,
        "Everything is muted (Mute all).",
        None,
        None,
        (Action("unmute_all", "Unmute all"),),
    )


def _empty_problem(name: str, n: int, what: str, path: str, tab: str) -> Problem:
    return Problem(
        "filter_empty",
        PROBLEM,
        f"{name} is silent: its {what} is empty (all zeros).",
        "output",
        n,
        (
            Action("reset_filter", "Reset this filter to flat", params={"path": path}),
            Action("open", "Open", href=_out_href(n, tab)),
        ),
    )


def _total(routes: Sequence[RouteTotal], output_db: float, ceiling: float) -> Total:
    eps = 1e-9
    at_limit = output_db >= ceiling - eps
    if not routes:
        return Total(
            min=output_db,
            max=output_db,
            input=None,
            routes=(),
            output=output_db,
            ceiling=ceiling,
            at_limit=at_limit,
            route_at_limit=False,
            text=f"{signed1(output_db)} dB",
            breakdown=f"output {signed(output_db)}",
        )
    totals = [r.total_db for r in routes]
    lo, hi = min(totals), max(totals)
    main = max(routes, key=lambda r: r.total_db).input
    text = f"{signed1(hi)} dB" if _clean(hi - lo) == 0 else f"{signed(lo, 1)} to {signed(hi, 1)} dB"

    def chain(r: RouteTotal, *, labelled: bool) -> str:
        level = f"level {signed(r.input_db)} + " if _clean(r.input_db) else ""
        body = f"{level}route {signed(r.route_db)} + output {signed(output_db)}"
        if labelled:
            return f"{r.name}: {body}"
        return (f"{r.name} " + body) if level else body

    if len(routes) == 1:
        breakdown = chain(routes[0], labelled=False)
    else:
        breakdown = " · ".join(chain(r, labelled=True) for r in routes)
    return Total(
        min=lo,
        max=hi,
        input=main,
        routes=tuple(routes),
        output=output_db,
        ceiling=ceiling,
        at_limit=at_limit,
        route_at_limit=all(r.route_db >= ceiling - eps for r in routes),
        text=text,
        breakdown=breakdown,
    )


def _headroom(name: str, total: Total, main_input: str | None, clipped: bool, th: Thresholds) -> Headroom:
    if clipped:
        peak = -total.max if total.max > 0 else None
        return Headroom(peak, f"{name} clipped: lower it or the source.", PROBLEM)
    if main_input is None or _clean(total.max, 1) <= 0:
        return Headroom(None, "", "none")
    t = plain(total.max, 1)
    level = CAUTION if total.max >= th.high_total_db else INFO
    return Headroom(-total.max, f"Peaks above about {MINUS}{t} dBFS on {main_input} will overload {name}.", level)


def _sources_clause(routes: Sequence[RouteTotal]) -> str:
    """``In 1`` · ``In 1 and In 2 (mono)`` · ``In 1 (+4 dB) and In 2 (−6 dB)`` · ``nothing``; ``(muted)`` marks a
    muted input."""
    if not routes:
        return "nothing"
    if len(routes) == 1:
        r = routes[0]
        return r.name + (" (muted)" if r.muted else "")
    same = len({_clean(r.input_db + r.route_db) for r in routes}) == 1
    words = []
    for r in routes:
        notes = [] if same else [f"{signed(r.input_db + r.route_db)} dB"]
        if r.muted:
            notes.append("muted")
        words.append(r.name + (f" ({', '.join(notes)})" if notes else ""))
    return join_words(words) + (" (mono)" if same else "")


def _slopes(band: _Band) -> str:
    hp_word = steepness(band.hp[1]) if band.hp else None
    lp_word = steepness(band.lp[1]) if band.lp else None
    if hp_word and lp_word and hp_word != lp_word:
        return f"{hp_word} below, {lp_word} above"
    return hp_word or lp_word or ""


BASS_BELOW_HZ = 200.0  # a low-pass at or below this "plays bass below"; higher ones "play everything below"


def _band_clause(band: _Band, verb: str) -> str:
    if band.custom:
        return f"{verb} through a custom filter"
    if band.hp and band.lp:
        return f"{verb} {hz_range(band.hp[0], band.lp[0])} ({_slopes(band)})"
    if band.lp:
        what = "bass below" if band.lp[0] <= BASS_BELOW_HZ else "everything below"
        return f"{verb} {what} {hz(band.lp[0])} ({_slopes(band)})"
    if band.hp:
        return f"{verb} everything above {hz(band.hp[0])} ({_slopes(band)})"
    return f"{verb} full range"


def _band_short(band: _Band) -> str:
    if band.hp and band.lp:
        return hz_range(band.hp[0], band.lp[0])
    if band.lp:
        return f"below {hz(band.lp[0])}"
    if band.hp:
        return f"above {hz(band.hp[0])}"
    return "full range"


# --- the session helper -----------------------------------------------------------------------------------------------


def session_summary(session: Any, preset: int | None = None, *, levels: Levels | None = None) -> PresetSummary | None:
    """`summarize` for a `DeviceSession` (the active preset by default); None until its state is loaded.

    The pages call this (through the Jinja global ``preset_summary``) and so does the JSON API."""
    if session is None or not getattr(session, "loaded", False):
        return None
    master = getattr(session, "master", None)
    if preset is None:
        preset = master.preset if master is not None and master.preset is not None else 0
    if not 0 <= preset < session.profile.n_presets:
        return None
    from app.sync.session import StateUnavailable  # local imports: it imports the session module
    from app.sync.status import changed_paths

    try:
        doc = session.desired(preset)
        statuses = session.sync_status(preset)
        staged = bool(session.sync_summary(preset).get("staged"))
    except StateUnavailable:
        return None
    return summarize(
        doc,
        session.profile,
        getattr(session, "channel_meta", {}),
        getattr(session, "settings", None),
        statuses,
        # The last check's still-open changes, as the one status model (app/sync/status.py) counts them.
        {"preset": preset, "paths": sorted(changed_paths(session, preset))},
        levels,
        preset=preset,
        master=master,
        staged=staged,
    )


def live_levels(session: Any) -> Levels | None:
    """The session's latest meter readings (no CLIP latches: those live in the browser)."""
    status = getattr(session, "status", None)
    if status is None:
        return None
    return Levels(inputs=tuple(status.input_levels), outputs=tuple(status.output_levels))


__all__ = [
    "BADGES",
    "RULES",
    "SILENCING",
    "Action",
    "InputSummary",
    "Levels",
    "OutputSummary",
    "PresetSummary",
    "Problem",
    "Thresholds",
    "live_levels",
    "session_summary",
    "summarize",
]
