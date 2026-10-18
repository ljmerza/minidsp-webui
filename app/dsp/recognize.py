"""Coefficient recognition: raw biquads → editable designs (docs 04 §5).

Parameters are recovered in closed form from the coefficients, then the filter is re-designed and accepted only when
every coefficient matches within `TOL_ABS` and `TOL_REL` *and* the responses agree within `TOL_DB`. The response check
is needed because at low frequencies the response hangs on near-cancelling sums such as ``1 − a1 − a2`` (≈ 2e-6 for a
20 Hz high-pass), so a 1e-6 coefficient tolerance alone can accept a shelf as a peaking filter. Recovered values are
snapped to round numbers (80 Hz, Q = 1/√2, +6.0 dB) when the snapped design still matches. Anything unrecognized stays
``raw``.

Recognition never changes coefficients: callers keep the imported biquads as ``compiled`` and only attach the design.
"""

import functools
import itertools
import math
import statistics
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.dsp import design as d
from app.dsp import response as r
from app.dsp.design import BESSEL_VERIFIED, FS, DesignError
from app.model.config_doc import ConfigDoc
from app.model.filters import PASSTHROUGH, Biquad, CrossoverDesign, FilterDesign, LinkwitzTransformParams

TOL_ABS = 1e-6
TOL_REL = 1e-5
TOL_DB = 0.01
_ZERO = 1e-12
# A normalized-frequency check (5 Hz-24 kHz at 96 kHz), used whatever ``fs`` the coefficients were designed for.
_CHECK_FREQS = r.freq_grid()

type Coeffs = tuple[float, float, float, float, float]


def _tuple(bq: Biquad | Sequence[float]) -> Coeffs:
    return bq.as_tuple() if isinstance(bq, Biquad) else tuple(float(v) for v in bq)  # type: ignore[return-value]


def coeffs_match(
    a: Biquad | Sequence[float], b: Biquad | Sequence[float], tol_abs: float = TOL_ABS, tol_rel: float = TOL_REL
) -> bool:
    """Every coefficient of ``a`` within ``tol_abs`` of ``b`` *and* within ``tol_rel`` relative to ``b``.

    Both limits apply: the absolute one alone would accept a 15% error on the 7e-6 numerator of a low-frequency
    low-pass. Coefficients that are zero in ``b`` must be (almost exactly) zero in ``a``.
    """
    for x, y in zip(_tuple(a), _tuple(b), strict=True):
        diff = abs(x - y)
        if not (math.isfinite(x) and diff <= tol_abs and diff <= tol_rel * abs(y) + _ZERO):
            return False
    return True


# --- single biquads -----------------------------------------------------------------------------------------------


def responses_match(designed: Sequence[Biquad | Sequence[float]], given: Sequence[Biquad | Sequence[float]]) -> bool:
    """The cascades' magnitudes agree within `TOL_DB` wherever ``given`` is above −100 dB.

    ``given`` is compared with ``designed`` both as is and rounded to float32, so values read back from the device
    (or copied from a float32 dump) match the design they came from.
    """
    tg = r.cascade_tf(given, _CHECK_FREQS, float32=False)
    if r.max_db_difference(tg, r.cascade_tf(designed, _CHECK_FREQS, float32=False)) <= TOL_DB:
        return True
    return r.max_db_difference(tg, r.cascade_tf(designed, _CHECK_FREQS, float32=True)) <= TOL_DB


def _try(build: Callable[[], Biquad], target: Coeffs) -> bool:
    try:
        bq = build()
        return coeffs_match(bq, target) and responses_match([bq], [target])
    except (DesignError, ValueError, ZeroDivisionError, OverflowError):
        return False


def _crossover_qs() -> tuple[float, ...]:
    qs = {math.sqrt(0.5), 0.5}
    for n in range(2, 9):
        qs.update(d.butterworth_qs(n))
        qs.update(q for _, q in d.bessel_sections(n, "phase") if q is not None)
    return tuple(sorted(qs))


# Q values worth snapping to exactly: 1/√2, 0.5 and every BW / LR / Bessel section Q.
_SPECIAL_QS = _crossover_qs()
# How far a closed-form estimate may be from a candidate. Float32 inputs can put low-frequency estimates ~1% off;
# the coefficient comparison is what decides.
_SNAP_WINDOW = 0.02
# (coarse roundings, special values, fine roundings) per parameter, tried in that order.
_SNAP_RULES: dict[str, tuple[Sequence[int], Sequence[float], Sequence[int]]] = {
    "freq": ((-1, 0, 1), (), (2, 3)),
    "q": ((1, 2), _SPECIAL_QS, (3, 4, 6)),
    "gain_db": ((0, 1), (), (2, 3)),
}


def _snap_candidates(
    value: float, coarse: Sequence[int], specials: Sequence[float], fine: Sequence[int]
) -> list[float]:
    """Coarsest first: short roundings, special values, longer roundings, then the estimate itself.

    Short roundings go before the specials so that a Q of exactly 1 isn't reported as BW3's 1.0000000000000002.
    """
    out = [round(value, n) for n in coarse]
    out += [x for x in specials if abs(x - value) <= _SNAP_WINDOW * abs(value)]
    out += [round(value, n) for n in fine]
    out.append(value)
    return list(dict.fromkeys(x for x in out if x != 0))


def _combos(names: list[str], lists: list[list[float]]) -> Iterator[dict[str, float]]:
    """Joint parameter combinations, coarsest (lowest summed candidate index) first."""
    combos = sorted(itertools.product(*(list(enumerate(c)) for c in lists)), key=lambda c: sum(i for i, _ in c))
    for combo in combos:
        yield dict(zip(names, (v for _, v in combo), strict=True))


def _is_float32(c: Coeffs) -> bool:
    return r.to_float32(c) == tuple(c)


def _float32_exact(bq: Biquad, target: Coeffs) -> bool:
    """``bq`` rounded to float32 is ``target`` (within 2 ulps): what a readback of exactly this design looks like."""
    got = np.array(bq.as_tuple(), dtype=np.float32).astype(float)
    want = np.array(target, dtype=np.float32)
    return bool(np.all(np.abs(got - want.astype(float)) <= 2 * np.spacing(np.abs(want)).astype(float)))


# How far (relative) a round frequency may be from a float32 input's estimate and still be tried, and the steps (Hz)
# whose nearest multiples on each side are tried.
_FLOAT32_FREQ_WINDOW = 0.25
_FLOAT32_FREQ_STEPS = (10.0, 5.0, 1.0, 0.5, 0.1)


def _round_neighbours(value: float, levels: Sequence[int]) -> list[float]:
    """The multiples of 10^-n just below and above ``value``, for each level ``n``."""
    out = []
    for n in levels:
        step = 10.0**-n
        low = math.floor(value / step) * step
        out += [round(low, n), round(low + step, n)]
    return out


def _float32_freqs(f: float) -> list[float]:
    """Round frequencies near a float32 input's estimate: two multiples of each step on either side, coarse first."""
    out = []
    for step in _FLOAT32_FREQ_STEPS:
        low = math.floor(f / step)
        out += [round(k * step, 1) for k in (low, low + 1, low - 1, low + 2)]
    return [x for x in dict.fromkeys(out) if x > 0 and abs(x - f) <= _FLOAT32_FREQ_WINDOW * f]


def _float32_trials(params: dict[str, float]) -> Iterator[dict[str, float]]:
    """Extra candidates for float32 inputs: round frequencies near the estimate, each with Q scaled to keep the
    estimated bandwidth (f/Q), which float32 coefficients pin down far better than f or Q alone at low frequencies."""
    if "freq" not in params:
        return
    f = params["freq"]
    names = list(params)
    for fc in _float32_freqs(f):
        lists = {"freq": [fc]}
        if "q" in params:
            lists["q"] = _snap_candidates(params["q"] * fc / f, (0, 1, 2), _SPECIAL_QS, (3, 4))
        if "gain_db" in params:
            lists["gain_db"] = _snap_candidates(params["gain_db"], (0, 1), (), (2,))
        yield from _combos(names, [lists[n] for n in names])


def _matching(build: Callable[[dict[str, float]], Biquad], trial: dict[str, float], target: Coeffs) -> Biquad | None:
    """The design for ``trial`` when it matches ``target`` within tolerance, else None."""
    try:
        bq = build(trial)
    except (DesignError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return bq if coeffs_match(bq, target) and responses_match([bq], [target]) else None


def _snap(params: dict[str, float], build: Callable[[dict[str, float]], Biquad], target: Coeffs) -> dict | None:
    """The coarsest rounding of the estimated parameters whose re-design matches ``target``, or None.

    The parameters are searched jointly (an off estimate of Q can hide a perfectly round frequency), coarsest
    combination first.

    Float32 inputs (device readbacks, dumps) are ambiguous at low frequencies: the estimate can be a few % off and
    several nearby designs match, some even bit for bit (Notch 19.74 Hz Q 4.935 and 20 Hz Q 5 round to the same
    float32 coefficients). There the roundest design whose float32 rounding reproduces the input wins, searched over
    more candidates; without such a design, the first tolerance match, as for float64 inputs.
    """
    names = list(params)
    trials = list(_combos(names, [_snap_candidates(params[n], *_SNAP_RULES[n]) for n in names]))
    if not _is_float32(target):
        return next((t for t in trials if _matching(build, t, target) is not None), None)
    fallback_rank: dict[tuple[float, ...], int] = {}
    for k, t in enumerate(trials):
        fallback_rank.setdefault(tuple(t.values()), k)
    # The extra candidates go first so that, between equally round exact matches, a step-5 frequency (31.5 Hz) beats
    # the estimate's own rounding (31.7 Hz). Duplicates hold equal values, so the dict keeps first positions.
    candidates = list({tuple(t.values()): t for t in [*_float32_trials(params), *trials]}.values())
    fallback: tuple[int, dict[str, float]] | None = None
    for trial in sorted(candidates, key=_roundness):
        bq = _matching(build, trial, target)
        if bq is None:
            continue
        if _float32_exact(bq, target):
            return trial
        rank = fallback_rank.get(tuple(trial.values()), len(trials))
        if fallback is None or rank < fallback[0]:
            fallback = (rank, trial)
    return fallback[1] if fallback is not None else None


@functools.lru_cache(maxsize=4096)
def _digits(name: str, value: float) -> tuple[float, float]:
    """How "typed" a value looks: (significant digits, decimals), e.g. 20 → (1, 0), 19.74 → (4, 2), 0.45 → (2, 2).
    Special Qs (1/√2, BW / Bessel section Qs) count as (2.5, 2.5)."""
    if name == "q" and value in _SPECIAL_QS:
        return 2.5, 2.5
    for digits in range(1, 9):
        if float(f"{value:.{digits}g}") == value:
            return digits, next(n for n in range(10) if round(value, n) == value)
    return 10, 10


def _roundness(trial: dict[str, float]) -> tuple[float, float]:
    """Lower is rounder: total significant digits, then total decimals (10 Hz Q 0.5 before 9 Hz Q 0.45)."""
    scores = [_digits(name, value) for name, value in trial.items()]
    return sum(s for s, _ in scores), sum(n for _, n in scores)


def _shared_denominator(a1: float, a2: float, fs: float) -> tuple[float, float, float] | None:
    """``(f0, Q, cos w0)`` from an RBJ ``1 ± α`` denominator, or None when it isn't one."""
    r = -a2
    if not -1 < r < 1:
        return None
    alpha = (1 - r) / (1 + r)
    cw = a1 * (1 + alpha) / 2
    if not -1 < cw < 1:
        return None
    w0 = math.acos(cw)
    return w0 * fs / (2 * math.pi), math.sin(w0) / (2 * alpha), cw


def _shelf_params(c: Coeffs, high: bool, fs: float) -> dict[str, float] | None:
    b0, b1, b2, a1, a2 = c
    end = (b0 - b1 + b2) / (1 + a1 - a2) if high else (b0 + b1 + b2) / (1 - a1 - a2)
    r = -a2
    if not (end > 0 and -1 < r < 1):
        return None
    a = math.sqrt(end)
    rho = (1 - r) / (1 + r)
    m = (-a1 if high else a1) * (1 + rho) / 2
    denom = m * (a - 1) - (a + 1)
    if denom == 0:
        return None
    c_ = ((a - 1) - m * (a + 1)) / denom
    if not -1 < c_ < 1:
        return None
    cw = -c_ if high else c_
    s = (a + 1) + (a - 1) * c_
    alpha = rho * s / (2 * math.sqrt(a))
    if alpha <= 0:
        return None
    w0 = math.acos(cw)
    return {"freq": w0 * fs / (2 * math.pi), "q": math.sin(w0) / (2 * alpha), "gain_db": 40 * math.log10(a)}


def _second_order(c: Coeffs, fs: float) -> FilterDesign | None:
    b0, _, b2, a1, a2 = c
    shared = _shared_denominator(a1, a2, fs)
    if shared is not None:
        f0, q, _ = shared
        builders: dict[str, Callable[[dict[str, float]], Biquad]] = {
            "lp2": lambda p: d.lowpass2(p["freq"], p["q"], fs),
            "hp2": lambda p: d.highpass2(p["freq"], p["q"], fs),
            "bandpass": lambda p: d.bandpass(p["freq"], p["q"], fs),
            "notch": lambda p: d.notch(p["freq"], p["q"], fs),
            "ap2": lambda p: d.allpass2(p["freq"], p["q"], fs),
        }
        for kind, build in builders.items():
            params = _snap({"freq": f0, "q": q}, build, c)
            if params is not None:
                return FilterDesign(type=kind, **params)
        # Peaking: the denominator's α is really α/A; the numerator gives α·A.
        _, _, cw = shared
        alpha_over_a = (1 + a2) / (1 - a2)
        alpha_times_a = (b0 - b2) * (1 + alpha_over_a) / 2
        if alpha_times_a > 0:
            a = math.sqrt(alpha_times_a / alpha_over_a)
            w0 = math.acos(cw)
            params = {
                "freq": w0 * fs / (2 * math.pi),
                "q": math.sin(w0) / (2 * alpha_over_a * a),
                "gain_db": 40 * math.log10(a),
            }
            snapped = _snap(params, lambda p: d.peaking(p["freq"], p["q"], p["gain_db"], fs), c)
            if snapped is not None:
                return FilterDesign(type="peaking", **snapped)
    for kind, high, fn in (("lowshelf", False, d.low_shelf), ("highshelf", True, d.high_shelf)):
        params = _shelf_params(c, high, fs)
        if params is not None:
            snapped = _snap(params, lambda p, fn=fn: fn(p["freq"], p["q"], p["gain_db"], fs), c)
            if snapped is not None:
                return FilterDesign(type=kind, **snapped)
    return None


def _first_order(c: Coeffs, fs: float) -> FilterDesign | None:
    a1 = c[3]
    if not -1 < a1 < 1:
        return None
    k = (1 - a1) / (1 + a1)
    f0 = 2 * math.atan(k) * fs / (2 * math.pi)
    for kind, fn in (("lp1", d.lowpass1), ("hp1", d.highpass1), ("ap1", d.allpass1)):
        params = _snap({"freq": f0}, lambda p, fn=fn: fn(p["freq"], fs), c)
        if params is not None:
            return FilterDesign(type=kind, **params)
    return None


def recognize_biquad(biquad: Biquad | Sequence[float], fs: float = FS) -> FilterDesign:
    """The design that reproduces ``biquad`` (within tolerance), or ``FilterDesign(type="raw")`` holding it.

    Linkwitz Transforms are not recovered (they come back as raw). Raises ValueError for non-finite coefficients.
    """
    c = _tuple(biquad)
    if not all(math.isfinite(x) for x in c):
        raise ValueError(f"biquad has a non-finite coefficient: {list(c)}")
    raw = FilterDesign(type="raw", raw=Biquad(**dict(zip(("b0", "b1", "b2", "a1", "a2"), c, strict=True))))
    b0, b1, b2, a1, a2 = c
    if all(abs(x) <= _ZERO for x in (b1, b2, a1, a2)) and b0 > 0:
        if coeffs_match(c, PASSTHROUGH):
            return FilterDesign(type="passthrough")
        params = _snap({"gain_db": 20 * math.log10(b0)}, lambda p: d.gain(p["gain_db"]), c)
        if params is not None:
            return FilterDesign(type="gain", **params)
        return raw
    if abs(b2) <= _ZERO and abs(a2) <= _ZERO:
        return _first_order(c, fs) or raw
    return _second_order(c, fs) or raw


# --- crossover groups ---------------------------------------------------------------------------------------------


def _match_multiset(designed: list[Biquad], given: list[Coeffs]) -> bool:
    if len(designed) != len(given):
        return False
    remaining = list(given)
    for bq in designed:
        hit = next((k for k, g in enumerate(remaining) if coeffs_match(g, bq)), None)
        if hit is None:
            return False
        remaining.pop(hit)
    return responses_match(designed, given)


def _bessel_norms() -> list[str]:
    return ["phase", "mag"] if "bessel_norm" in CrossoverDesign.model_fields else ["phase"]


@dataclass(frozen=True)
class _Section:
    """A crossover section's shape and rough (unsnapped) corner, read from its coefficients alone."""

    type: str  # lp1 / lp2 / hp1 / hp2
    freq: float
    q: float | None


def _near(x: float, y: float) -> bool:
    return abs(x - y) <= TOL_REL * max(abs(x), abs(y))


def _section_shape(c: Coeffs, fs: float) -> _Section | None:
    """Low- or high-pass section by its numerator zeros (z = −1 or z = 1), with the pole estimate of its corner.

    No snapping and no per-section match: crossover sections (Bessel's especially, or any float32 readback) often
    aren't at round values, so `_recognize_part` checks the whole group against each candidate design instead.
    """
    b0, b1, b2, a1, a2 = c
    if b0 == 0:
        return None
    if abs(b2) <= _ZERO and abs(a2) <= _ZERO:
        if not -1 < a1 < 1:
            return None
        f0 = 2 * math.atan((1 - a1) / (1 + a1)) * fs / (2 * math.pi)
        kind = "lp1" if _near(b1, b0) else "hp1" if _near(b1, -b0) else None
        return _Section(kind, f0, None) if kind else None
    shared = _shared_denominator(a1, a2, fs)
    if shared is None or not _near(b2, b0):
        return None
    f0, q, _ = shared
    kind = "lp2" if _near(b1, 2 * b0) else "hp2" if _near(b1, -2 * b0) else None
    return _Section(kind, f0, q) if kind else None


def _part_candidates(
    kind: str, sections: list[_Section], include_bessel: bool
) -> Iterable[tuple[str, int, float, str | None]]:
    """``(family, order, fc, bessel_norm)`` guesses for a run of all-LP or all-HP sections."""
    n2 = sum(1 for s in sections if s.type in ("lp2", "hp2"))
    n1 = len(sections) - n2
    if n1 > 1:
        return
    order_bw = 2 * n2 + n1
    fc = statistics.median(s.freq for s in sections)  # every BW / LR section sits at fc; the median damps noise
    yield "bw", order_bw, fc, None
    if n1 == 0:
        yield "lr", 2 * len(sections), fc, None
    if include_bessel and order_bw >= 2:
        top = max((s for s in sections if s.q is not None), key=lambda s: s.q, default=None)
        if top is not None:
            for norm in _bessel_norms():
                w_top = d.bessel_sections(order_bw, norm)[-1][0]  # type: ignore[arg-type]
                yield "bessel", order_bw, (top.freq / w_top if kind == "lp" else top.freq * w_top), norm


def _freq_tiers(fc: float) -> list[list[float]]:
    """Corner-frequency candidates, coarsest tier first: multiples of 10, 5, 1, 0.5 and 0.1 Hz on either side of the
    estimate (float32 inputs can put it a few % off at low frequencies), then finer roundings and the estimate."""
    tiers = []
    for step in _FLOAT32_FREQ_STEPS:
        low = math.floor(fc / step)
        tiers.append([round(k * step, 1) for k in (low, low + 1, low - 1, low + 2)])
    tiers += [[round(fc, n), *_round_neighbours(fc, (n,))] for n in (2, 3)]
    tiers.append([fc])
    return [[f for f in dict.fromkeys(t) if f > 0 and abs(f - fc) <= _FLOAT32_FREQ_WINDOW * fc] for t in tiers]


def _pole_size(c: Coeffs) -> float:
    """``1 + a2`` (second order) or ``1 − a1`` (first order): roughly proportional to the corner frequency, and stored
    precisely even in float32, unlike the pole angle, which hangs on ``1 − a1 − a2``."""
    return 1 + c[4] if abs(c[4]) > _ZERO else 1 - c[3]


def _refine_fc(
    kind: str, family: str, order: int, fc: float, norm: str | None, given: list[Coeffs], fs: float
) -> float:
    """The corner at which the candidate design's summed `_pole_size` equals the given sections' (a few
    multiplicative secant steps from the estimate ``fc``); ``fc`` itself when the design can't be built or sized."""
    target = sum(_pole_size(c) for c in given)
    f = fc
    for _ in range(8):
        try:
            proto = d.prototype_sections(kind, family, order, f, norm or "phase")  # type: ignore[arg-type]
        except (DesignError, ValueError):
            return fc
        if len(proto) != len(given):
            return fc
        size = sum(_pole_size(s.biquad(fs).as_tuple()) for s in proto)
        if not (size > 0 and target > 0):
            return fc
        new = min(max(f * target / size, f / 2), f * 2)
        if abs(new - f) <= 1e-12 * f:
            break
        f = new
    return f


def _recognize_part(
    kind: str, sections: list[_Section], given: list[Coeffs], fs: float, include_bessel: bool
) -> tuple[str, int, float, str | None] | None:
    """The first candidate design matching the whole run of sections, at the coarsest frequency rounding.

    Roundings are tried tier by tier across *all* candidates, closest to its own estimate first within a tier: the two
    Bessel normalizations differ only by a frequency scale, so both match, and only one of them sits at a round corner
    (Bessel4 −3 dB at 80 Hz is phase-normalized at 121.14 Hz).
    """
    candidates = [
        (family, order, _refine_fc(kind, family, order, fc, norm, given, fs), norm)
        for family, order, fc, norm in _part_candidates(kind, sections, include_bessel)
        if 1 <= order <= 8
    ]

    def check(family: str, order: int, f: float, norm: str | None) -> bool:
        try:
            proto = d.prototype_sections(kind, family, order, f, norm or "phase")  # type: ignore[arg-type]
            return _match_multiset([s.biquad(fs) for s in proto], given)
        except (DesignError, ValueError):
            return False

    tiers = [_freq_tiers(fc) for _, _, fc, _ in candidates]
    for level in range(len(tiers[0]) if tiers else 0):
        trials = sorted((abs(f - fc) / fc, k, f) for k, (_, _, fc, _) in enumerate(candidates) for f in tiers[k][level])
        for _, k, f in trials:
            family, order, _, norm = candidates[k]
            if check(family, order, f, norm):
                return family, order, f, norm
    return None


def recognize_crossover(
    biquads: Sequence[Biquad | Sequence[float]], fs: float = FS, *, include_bessel: bool = BESSEL_VERIFIED
) -> CrossoverDesign | None:
    """The BW / LR / Bessel design a group's biquads implement, or None (all pass-through, or custom/raw).

    Section order doesn't matter. A group mixing low- and high-pass sections of the same family and order is a
    band-pass. Two identical BW2 sections are LR4, not "BW2 twice".
    """
    given = [_tuple(b) for b in biquads]
    active = [c for c in given if not coeffs_match(c, PASSTHROUGH)]
    if not active:
        return None
    shapes = [_section_shape(c, fs) for c in active]
    if any(x is None for x in shapes):
        return None
    designs: list[_Section] = shapes  # type: ignore[assignment]
    parts: dict[str, Any] = {}
    for kind in ("lp", "hp"):
        idx = [k for k, x in enumerate(designs) if x.type.startswith(kind)]
        if idx:
            found = _recognize_part(kind, [designs[k] for k in idx], [active[k] for k in idx], fs, include_bessel)
            if found is None:
                return None
            parts[kind] = found
    extra: dict[str, Any] = {}
    if len(parts) == 2:
        (fam_l, ord_l, f_lp, norm_l), (fam_h, ord_h, f_hp, norm_h) = parts["lp"], parts["hp"]
        if (fam_l, ord_l, norm_l) != (fam_h, ord_h, norm_h) or f_lp <= f_hp:
            return None
        family, order, norm = fam_l, ord_l, norm_l
        fields: dict[str, Any] = {"kind": "bp", "freq": f_hp, "freq_hi": f_lp}
    else:
        kind = next(iter(parts))
        family, order, freq, norm = parts[kind]
        fields = {"kind": kind, "freq": freq}
    if norm is not None and "bessel_norm" in CrossoverDesign.model_fields:
        extra["bessel_norm"] = norm
    try:
        return CrossoverDesign.model_validate({**fields, "family": family, "order": order, **extra})
    except ValueError:
        return None


# --- labels -------------------------------------------------------------------------------------------------------


def format_hz(freq: float) -> str:
    if freq >= 1000:
        return f"{freq / 1000:.3g} kHz"
    return f"{freq:.2f}".rstrip("0").rstrip(".") + " Hz"


def _q(q: float) -> str:
    return f"Q {q:.3g}"


def _db(gain_db: float) -> str:
    return f"{gain_db:+.1f} dB"


def describe_filter(design: FilterDesign) -> str:
    """Short label for a band, e.g. ``PEQ 1 kHz Q 1 +6.0 dB``."""
    f, q, g = design.freq, design.q, design.gain_db
    match design.type:
        case "peaking":
            return f"PEQ {format_hz(f)} {_q(q)} {_db(g)}"
        case "lowshelf" | "highshelf":
            name = "Low shelf" if design.type == "lowshelf" else "High shelf"
            return f"{name} {format_hz(f)} {_q(q)} {_db(g)}"
        case "lp1" | "hp1" | "ap1":
            return f"{design.type.upper()} {format_hz(f)}"
        case "lp2" | "hp2" | "ap2":
            return f"{design.type.upper()} {format_hz(f)} {_q(q)}"
        case "bandpass":
            return f"BP {format_hz(f)} {_q(q)}"
        case "notch":
            return f"Notch {format_hz(f)} {_q(q)}"
        case "lt":
            lt: LinkwitzTransformParams = design.lt
            return f"LT {format_hz(lt.f0)} {_q(lt.q0)} → {format_hz(lt.fp)} {_q(lt.qp)}"
        case "gain":
            return f"Gain {_db(g)}"
        case "passthrough":
            return "Pass-through"
        case _:
            return "Raw"


_FAMILY = {"bw": "BW", "lr": "LR", "bessel": "Bessel"}


def describe_crossover(design: CrossoverDesign | None) -> str:
    """Short label for a group, e.g. ``LR4 LP 80 Hz``; ``Custom`` for None. Bessel labels name the normalization
    (``Bessel4 LP 80 Hz (phase)`` or ``(−3 dB)``), because the two put the corner in different places."""
    if design is None:
        return "Custom"
    name = f"{_FAMILY[design.family]}{design.order}"
    if design.kind == "bp":
        label = f"{name} BP {format_hz(design.freq)}–{format_hz(design.freq_hi)}"
    else:
        label = f"{name} {design.kind.upper()} {format_hz(design.freq)}"
    if design.family == "bessel":
        label += " (−3 dB)" if design.bessel_norm == "mag" else " (phase)"
    return label


# --- whole documents ----------------------------------------------------------------------------------------------


def _design_still_matches(design: FilterDesign, compiled: Biquad, fs: float) -> bool:
    if design.type == "raw":
        return design.raw is not None and design.raw.as_tuple() == compiled.as_tuple()
    return _try(lambda: d.design_filter(design, fs), compiled.as_tuple())


def annotate_designs(doc: ConfigDoc, fs: float = FS, *, include_bessel: bool = BESSEL_VERIFIED) -> ConfigDoc:
    """A copy of ``doc`` whose ``design`` fields describe its ``compiled`` coefficients (adopt / import).

    A design that already compiles to the stored coefficients is kept (so a Linkwitz Transform stays one);
    the rest are recognized. Coefficients and bypass flags are never touched.
    """
    data = doc.model_copy(deep=True)
    for ch in [*data.inputs, *data.outputs]:
        for band in ch.peq:
            if not _design_still_matches(band.design, band.compiled, fs):
                band.design = recognize_biquad(band.compiled, fs)
    for out in data.outputs:
        for group in out.crossover:
            if group.design is not None:
                try:
                    redesigned = d.design_crossover(group.design, len(group.compiled), fs, allow_unverified=True)
                    if _match_multiset(redesigned, [b.as_tuple() for b in group.compiled]):
                        continue
                except DesignError:
                    pass
            group.design = recognize_crossover(group.compiled, fs, include_bessel=include_bessel)
    return data
