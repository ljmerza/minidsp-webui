"""Filter design: parameters → biquads in the device convention (docs 04 §2-3).

Every function returns `Biquad` with a1/a2 negated relative to the RBJ denominator:
``b0 = B0/A0, b1 = B1/A0, b2 = B2/A0, a1 = -A1/A0, a2 = -A2/A0``. The arithmetic order matters: the RBJ recipes
below reproduce the coefficients loaded on the device bit for bit (the golden vectors in docs 04 §8.1), which is
what lets a re-design with identical parameters produce an empty diff.

The sample rate is always 96 kHz on the device; ``fs`` exists for tests and future models.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from app.dsp.stability import biquad_problem
from app.model.filters import PASSTHROUGH, Biquad, CrossoverDesign, FilterDesign, LinkwitzTransformParams

FS = 96_000
# Design gains beyond this are refused outright (they overflow or round to silence long before they are useful). The
# boost a band may actually add is limited separately, by the session (``PEQ_MAX_BOOST_DB``).
GAIN_DB_LIMIT = 120.0

# Bessel crossovers are gated on this flag (docs 04 §3, docs 09 U7). Verified 2026-10-16: section Qs and relative
# section frequencies match Analog Devices MT-204 Figure 7 to its 4 published digits, orders 2-8; the absolute
# normalizations come from their definitions (exact -3.01 dB at the corner; order 2 checked in closed form, where MT-204
# itself is 0.3% off). Pinned by tests/unit/dsp/test_bessel.py.
BESSEL_VERIFIED = True

BesselNorm = Literal["phase", "mag", "delay"]
SectionKind = Literal["lp1", "hp1", "lp2", "hp2"]


class DesignError(ValueError):
    """The parameters don't describe a filter that can be built (out of range, unstable, too many sections)."""


# --- helpers ------------------------------------------------------------------------------------------------------


def from_rbj(b0: float, b1: float, b2: float, a0: float, a1: float, a2: float) -> Biquad:
    """Normalize raw RBJ ``(B0, B1, B2, A0, A1, A2)`` into the device convention."""
    return Biquad(b0=b0 / a0, b1=b1 / a0, b2=b2 / a0, a1=-a1 / a0, a2=-a2 / a0)


def _check_freq(freq: float, fs: float, name: str = "frequency") -> None:
    if not (math.isfinite(freq) and 0 < freq < fs / 2):
        raise DesignError(f"{name} {freq} Hz must be between 0 and {fs / 2:g} Hz (Nyquist)")


def _check_q(q: float, name: str = "Q") -> None:
    if not (math.isfinite(q) and q > 0):
        raise DesignError(f"{name} must be a positive number, got {q}")


def _check_gain(gain_db: float) -> None:
    if not math.isfinite(gain_db):
        raise DesignError(f"gain must be a finite number of dB, got {gain_db}")
    if abs(gain_db) > GAIN_DB_LIMIT:
        raise DesignError(f"gain {gain_db:g} dB is outside ±{GAIN_DB_LIMIT:g} dB")


def _terms(freq: float, q: float, fs: float) -> tuple[float, float, float]:
    """``cos w0``, ``sin w0`` and ``alpha`` for the RBJ cookbook."""
    _check_freq(freq, fs)
    _check_q(q)
    w0 = 2 * math.pi * freq / fs
    sw = math.sin(w0)
    return math.cos(w0), sw, sw / (2 * q)


# --- second-order RBJ recipes -------------------------------------------------------------------------------------


def peaking(freq: float, q: float, gain_db: float, fs: float = FS) -> Biquad:
    _check_gain(gain_db)
    cw, _, alpha = _terms(freq, q, fs)
    a = 10 ** (gain_db / 40)
    return from_rbj(1 + alpha * a, -2 * cw, 1 - alpha * a, 1 + alpha / a, -2 * cw, 1 - alpha / a)


def low_shelf(freq: float, q: float, gain_db: float, fs: float = FS) -> Biquad:
    _check_gain(gain_db)
    cw, _, alpha = _terms(freq, q, fs)
    a = 10 ** (gain_db / 40)
    k = 2 * math.sqrt(a) * alpha
    return from_rbj(
        a * ((a + 1) - (a - 1) * cw + k),
        2 * a * ((a - 1) - (a + 1) * cw),
        a * ((a + 1) - (a - 1) * cw - k),
        (a + 1) + (a - 1) * cw + k,
        -2 * ((a - 1) + (a + 1) * cw),
        (a + 1) + (a - 1) * cw - k,
    )


def high_shelf(freq: float, q: float, gain_db: float, fs: float = FS) -> Biquad:
    _check_gain(gain_db)
    cw, _, alpha = _terms(freq, q, fs)
    a = 10 ** (gain_db / 40)
    k = 2 * math.sqrt(a) * alpha
    return from_rbj(
        a * ((a + 1) + (a - 1) * cw + k),
        -2 * a * ((a - 1) + (a + 1) * cw),
        a * ((a + 1) + (a - 1) * cw - k),
        (a + 1) - (a - 1) * cw + k,
        2 * ((a - 1) - (a + 1) * cw),
        (a + 1) - (a - 1) * cw - k,
    )


def shelf_q_from_slope(slope: float, gain_db: float) -> float:
    """RBJ shelf slope S (1 = steepest monotonic) → the equivalent Q."""
    if not (math.isfinite(slope) and slope > 0):
        raise DesignError(f"shelf slope must be positive, got {slope}")
    _check_gain(gain_db)
    a = 10 ** (gain_db / 40)
    inv_q_sq = (a + 1 / a) * (1 / slope - 1) + 2
    if inv_q_sq <= 0:
        raise DesignError(f"shelf slope {slope} is too steep for {gain_db} dB")
    return 1 / math.sqrt(inv_q_sq)


def lowpass2(freq: float, q: float, fs: float = FS) -> Biquad:
    cw, _, alpha = _terms(freq, q, fs)
    return from_rbj((1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)


def highpass2(freq: float, q: float, fs: float = FS) -> Biquad:
    cw, _, alpha = _terms(freq, q, fs)
    return from_rbj((1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)


def bandpass(freq: float, q: float, fs: float = FS) -> Biquad:
    """Constant 0 dB peak gain band-pass."""
    cw, _, alpha = _terms(freq, q, fs)
    return from_rbj(alpha, 0.0, -alpha, 1 + alpha, -2 * cw, 1 - alpha)


def notch(freq: float, q: float, fs: float = FS) -> Biquad:
    cw, _, alpha = _terms(freq, q, fs)
    return from_rbj(1.0, -2 * cw, 1.0, 1 + alpha, -2 * cw, 1 - alpha)


def allpass2(freq: float, q: float, fs: float = FS) -> Biquad:
    cw, _, alpha = _terms(freq, q, fs)
    return from_rbj(1 - alpha, -2 * cw, 1 + alpha, 1 + alpha, -2 * cw, 1 - alpha)


# --- first-order sections (one biquad slot, b2 = a2 = 0) ----------------------------------------------------------


def _k(freq: float, fs: float) -> float:
    _check_freq(freq, fs)
    return math.tan(math.pi * freq / fs)


def lowpass1(freq: float, fs: float = FS) -> Biquad:
    k = _k(freq, fs)
    return from_rbj(k, k, 0.0, 1 + k, k - 1, 0.0)


def highpass1(freq: float, fs: float = FS) -> Biquad:
    k = _k(freq, fs)
    return from_rbj(1.0, -1.0, 0.0, 1 + k, k - 1, 0.0)


def allpass1(freq: float, fs: float = FS) -> Biquad:
    """+1 at DC, −90° at ``freq``, −180° towards Nyquist (same phase sense as `allpass2`)."""
    k = _k(freq, fs)
    return from_rbj(k - 1, 1 + k, 0.0, 1 + k, k - 1, 0.0)


# --- bilinear transform / Linkwitz Transform ---------------------------------------------------------------------


def bilinear(
    num: tuple[float, float, float], den: tuple[float, float, float], prewarp_hz: float, fs: float = FS
) -> Biquad:
    """Analog ``(n2·s² + n1·s + n0) / (d2·s² + d1·s + d0)`` → biquad, exact at ``prewarp_hz``.

    Uses ``s = g·(1 − z⁻¹)/(1 + z⁻¹)`` with ``g = ωc / tan(ωc / 2fs)``.
    """
    _check_freq(prewarp_hz, fs, "pre-warp frequency")
    wc = 2 * math.pi * prewarp_hz
    g = wc / math.tan(wc / (2 * fs))
    g2 = g * g
    n2, n1, n0 = num
    d2, d1, d0 = den
    return from_rbj(
        n2 * g2 + n1 * g + n0,
        2 * (n0 - n2 * g2),
        n2 * g2 - n1 * g + n0,
        d2 * g2 + d1 * g + d0,
        2 * (d0 - d2 * g2),
        d2 * g2 - d1 * g + d0,
    )


def linkwitz_transform(f0: float, q0: float, fp: float, qp: float, fs: float = FS) -> Biquad:
    """Move a sealed driver's (f0, Q0) response to the target (fp, Qp).

    Analog prototype ``(s² + (ω0/Q0)s + ω0²) / (s² + (ωp/Qp)s + ωp²)``, bilinear with pre-warping at the geometric
    mean of f0 and fp. DC gain is exactly ``(f0/fp)²`` and the gain tends to 1 at high frequencies.
    """
    for name, f in (("f0", f0), ("fp", fp)):
        _check_freq(f, fs, name)
    _check_q(q0, "Q0")
    _check_q(qp, "Qp")
    w0 = 2 * math.pi * f0
    wp = 2 * math.pi * fp
    return bilinear((1.0, w0 / q0, w0 * w0), (1.0, wp / qp, wp * wp), math.sqrt(f0 * fp), fs)


# --- trivial sections ---------------------------------------------------------------------------------------------


def gain(gain_db: float) -> Biquad:
    _check_gain(gain_db)
    return Biquad(b0=10 ** (gain_db / 20))


def passthrough() -> Biquad:
    return PASSTHROUGH


def raw(coeffs: Biquad | Mapping[str, float] | Any) -> Biquad:
    """A user-entered biquad, accepted only when finite and stable (also after float32 rounding)."""
    if not isinstance(coeffs, Biquad):
        if isinstance(coeffs, Mapping):
            coeffs = Biquad(**coeffs)
        else:
            values = [float(v) for v in coeffs]
            if len(values) != 5:
                raise DesignError(f"a biquad has 5 coefficients (b0 b1 b2 a1 a2), got {len(values)}")
            coeffs = Biquad(**dict(zip(("b0", "b1", "b2", "a1", "a2"), values, strict=True)))
    problem = biquad_problem(coeffs)
    if problem is not None:
        raise DesignError(problem)
    return coeffs


# --- single-slot dispatch -----------------------------------------------------------------------------------------


def design_filter(design: FilterDesign | Mapping[str, Any], fs: float = FS) -> Biquad:
    """Compile one PEQ band design into a biquad. Raises `DesignError` for impossible or unstable designs."""
    if not isinstance(design, FilterDesign):
        try:
            design = FilterDesign.model_validate(design)
        except ValueError as exc:
            raise DesignError(str(exc)) from None
    try:
        return _design_filter(design, fs)
    except (ArithmeticError, ValueError) as exc:
        if isinstance(exc, DesignError):
            raise
        raise DesignError(f"the parameters are out of range ({exc})") from None


def _design_filter(design: FilterDesign, fs: float) -> Biquad:
    f, q, g = design.freq, design.q, design.gain_db
    match design.type:
        case "peaking":
            bq = peaking(f, q, g, fs)
        case "lowshelf":
            bq = low_shelf(f, q, g, fs)
        case "highshelf":
            bq = high_shelf(f, q, g, fs)
        case "lp1":
            bq = lowpass1(f, fs)
        case "hp1":
            bq = highpass1(f, fs)
        case "lp2":
            bq = lowpass2(f, q, fs)
        case "hp2":
            bq = highpass2(f, q, fs)
        case "bandpass":
            bq = bandpass(f, q, fs)
        case "notch":
            bq = notch(f, q, fs)
        case "ap1":
            bq = allpass1(f, fs)
        case "ap2":
            bq = allpass2(f, q, fs)
        case "lt":
            lt: LinkwitzTransformParams = design.lt
            bq = linkwitz_transform(lt.f0, lt.q0, lt.fp, lt.qp, fs)
        case "gain":
            bq = gain(g)
        case "passthrough":
            return PASSTHROUGH
        case "raw":
            return raw(design.raw)
        case other:  # pragma: no cover - FilterType is a closed Literal
            raise DesignError(f"unknown filter type {other!r}")
    problem = biquad_problem(bq)
    if problem is not None:
        raise DesignError(problem)
    return bq


# --- crossover prototypes -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    """One crossover section, before discretization.

    ``freq`` is the section's own corner/natural frequency in Hz; ``q`` is None for first-order sections.
    """

    kind: SectionKind
    freq: float
    q: float | None = None

    def biquad(self, fs: float = FS) -> Biquad:
        match self.kind:
            case "lp1":
                return lowpass1(self.freq, fs)
            case "hp1":
                return highpass1(self.freq, fs)
            case "lp2":
                return lowpass2(self.freq, self.q, fs)
            case _:
                return highpass2(self.freq, self.q, fs)


def butterworth_qs(order: int) -> list[float]:
    """Second-order Qs of a Butterworth filter, ascending: ``Q_k = 1 / (2 sin((2k−1)π / 2n))``.

    Odd orders add one first-order section, which is not listed.
    """
    if not 1 <= order <= 8:
        raise DesignError(f"Butterworth order must be 1..8, got {order}")
    return sorted(1 / (2 * math.sin((2 * k - 1) * math.pi / (2 * order))) for k in range(1, order // 2 + 1))


def linkwitz_riley_qs(order: int) -> list[float]:
    """Second-order Qs of an LR crossover of ``order`` (2, 4, 6, 8), ascending. LR never needs a first-order section.

    LR2n is BWn cascaded twice; the two first-order sections of an odd BWn merge into one Q = 0.5 biquad.
    LR2 is that merged section alone.
    """
    if order not in (2, 4, 6, 8):
        raise DesignError(f"Linkwitz-Riley order must be 2, 4, 6 or 8, got {order}")
    half = order // 2
    qs = butterworth_qs(half) * 2
    if half % 2:
        qs.append(0.5)
    return sorted(qs)


def reverse_bessel_coefficients(order: int) -> list[int]:
    """Coefficients of the reverse Bessel polynomial θ_n(s), highest power first.

    ``a_k = (2n − k)! / (2^(n−k) · k! · (n − k)!)`` for the ``s^k`` term.
    """
    n = order
    return [
        math.factorial(2 * n - k) // (2 ** (n - k) * math.factorial(k) * math.factorial(n - k))
        for k in range(n, -1, -1)
    ]


def _bessel_magnitude_sq(poles: np.ndarray, w: float) -> float:
    """``|H(jω)|²`` of the all-pole low-pass with these poles, normalized to 1 at DC."""
    return float(np.prod(np.abs(poles) ** 2 / np.abs(1j * w - poles) ** 2))


def bessel_poles(order: int, norm: BesselNorm = "phase") -> np.ndarray:
    """Low-pass Bessel prototype poles for a 1 rad/s corner.

    - ``delay``: θ_n(s) as is (group delay 1 s at DC).
    - ``phase``: scaled so the product of pole magnitudes is 1 (θ_n's constant term becomes 1), the "phase-matched"
      crossover normalization, same as scipy's ``besselap(norm="phase")``. The low-pass phase at the corner is
      within 10° of its midpoint −n·45°, and the high-pass mirrors it.
    - ``mag``: scaled so the magnitude is −3.01 dB at the corner.
    """
    if not 1 <= order <= 8:
        raise DesignError(f"Bessel order must be 1..8, got {order}")
    coeffs = reverse_bessel_coefficients(order)
    poles = np.roots(np.array(coeffs, dtype=float))
    if norm == "delay":
        return poles
    if norm == "phase":
        return poles / coeffs[-1] ** (1 / order)
    if norm == "mag":
        lo, hi = 0.0, 1.0
        while _bessel_magnitude_sq(poles, hi) > 0.5:
            hi *= 2
        for _ in range(200):
            mid = (lo + hi) / 2
            if _bessel_magnitude_sq(poles, mid) > 0.5:
                lo = mid
            else:
                hi = mid
        return poles / ((lo + hi) / 2)
    raise DesignError(f"unknown Bessel normalization {norm!r}")


def bessel_sections(order: int, norm: BesselNorm = "phase") -> list[tuple[float, float | None]]:
    """``(ω_k, Q_k)`` per section of the normalized low-pass prototype; Q is None for the real pole.

    Sorted by ascending Q with the first-order section first.
    """
    out: list[tuple[float, float | None]] = []
    for p in bessel_poles(order, norm):
        if abs(p.imag) < 1e-9:
            out.append((float(-p.real), None))
        elif p.imag > 0:
            w = float(abs(p))
            out.append((w, w / (-2 * float(p.real))))
    return sorted(out, key=lambda s: -1.0 if s[1] is None else s[1])


def prototype_sections(
    kind: Literal["lp", "hp"], family: str, order: int, freq: float, bessel_norm: BesselNorm
) -> list[Section]:
    first: SectionKind = "lp1" if kind == "lp" else "hp1"
    second: SectionKind = "lp2" if kind == "lp" else "hp2"
    if family == "bw":
        qs = butterworth_qs(order)
        sections = [Section(first, freq)] if order % 2 else []
        return sections + [Section(second, freq, q) for q in qs]
    if family == "lr":
        qs = linkwitz_riley_qs(order)
        return [Section(second, freq, q) for q in qs]
    if family == "bessel":
        if order < 2:
            raise DesignError(f"Bessel order must be 2..8, got {order}")
        sections = []
        for w, q in bessel_sections(order, bessel_norm):
            # The high-pass is the s → 1/s transform: each pole maps to its reciprocal, so ω_k → 1/ω_k, same Q.
            f = freq * w if kind == "lp" else freq / w
            sections.append(Section(first, f) if q is None else Section(second, f, q))
        return sections
    raise DesignError(f"unknown crossover family {family!r}")


def crossover_sections(design: CrossoverDesign, *, bessel_norm: BesselNorm | None = None) -> list[Section]:
    """Analog sections of a crossover group, in the order they are written (band-pass: high-pass first)."""
    norm = bessel_norm or getattr(design, "bessel_norm", None) or "phase"
    if design.kind == "bp":
        high_pass = prototype_sections("hp", design.family, design.order, design.freq, norm)
        return high_pass + prototype_sections("lp", design.family, design.order, design.freq_hi, norm)
    return prototype_sections(design.kind, design.family, design.order, design.freq, norm)


def design_crossover(
    design: CrossoverDesign | Mapping[str, Any],
    n_biquads: int = 4,
    fs: float = FS,
    *,
    bessel_norm: BesselNorm | None = None,
    allow_unverified: bool = False,
) -> list[Biquad]:
    """Compile a crossover group into exactly ``n_biquads`` biquads, padding unused slots with pass-through.

    Bessel is refused unless `BESSEL_VERIFIED` or ``allow_unverified``. ``bessel_norm`` defaults to the design's
    ``bessel_norm`` attribute when it has one, else ``phase``.
    """
    if not isinstance(design, CrossoverDesign):
        try:
            design = CrossoverDesign.model_validate(design)
        except ValueError as exc:
            raise DesignError(str(exc)) from None
    try:
        return _design_crossover(design, n_biquads, fs, bessel_norm, allow_unverified)
    except (ArithmeticError, ValueError) as exc:
        if isinstance(exc, DesignError):
            raise
        raise DesignError(f"the parameters are out of range ({exc})") from None


def _design_crossover(
    design: CrossoverDesign, n_biquads: int, fs: float, bessel_norm: BesselNorm | None, allow_unverified: bool
) -> list[Biquad]:
    if design.family == "bessel" and not (BESSEL_VERIFIED or allow_unverified):
        raise DesignError("Bessel crossovers are disabled until the design is verified against a published table")
    for f in (design.freq, design.freq_hi):
        if f is not None:
            _check_freq(f, fs)
    sections = crossover_sections(design, bessel_norm=bessel_norm)
    if len(sections) > n_biquads:
        raise DesignError(
            f"{design.family} order {design.order} {design.kind} needs {len(sections)} biquads, "
            f"the group has {n_biquads}"
        )
    for s in sections:
        _check_freq(s.freq, fs, "section frequency")
    biquads = [s.biquad(fs) for s in sections]
    for bq in biquads:
        problem = biquad_problem(bq)
        if problem is not None:
            raise DesignError(problem)
    return biquads + [PASSTHROUGH] * (n_biquads - len(biquads))


def biquads_needed(design: CrossoverDesign) -> int:
    """How many biquad slots the design occupies."""
    return len(crossover_sections(design))
