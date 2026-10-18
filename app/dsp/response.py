"""Frequency response of biquad chains, output channels and electrical sums (docs 04 §4).

Every transfer function is carried as ``(H, dH/dω)`` on the frequency grid, with ω in radians per sample. The
derivative is propagated exactly through products, sums and delays, so group delay is ``−Im(H'/H)`` rather than a
numerical gradient. A 512-point log grid is too coarse to unwrap the phase of a multi-millisecond delay at high
frequencies, so the unwrapped phase is integrated from the same derivative and only snapped to the wrapped value.

Coefficients are rounded to float32 before evaluation by default, because that's what the device stores.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.dsp.design import FS
from app.model.config_doc import ConfigDoc, CrossoverGroup, PeqBand
from app.model.filters import Biquad

N_POINTS = 512
F_MIN = 5.0
F_MAX = 24_000.0
VIEWS: dict[str, tuple[float, float]] = {"sub": (5.0, 300.0), "full": (10.0, 20_000.0)}
DB_FLOOR = -200.0
FLOAT32_WARN_DB = 0.1
# Float32 deviations are only judged where the float64 response is above this level: far down a stop band the
# dB difference of two tiny numbers says nothing audible.
FLOAT32_RELEVANT_DB = -100.0
# Lowest non-zero frequency of the full-band check grid (DC itself is always included).
CHECK_F_MIN = 0.1

type BiquadLike = Biquad | Sequence[float] | Mapping[str, float]


def freq_grid(f_min: float = F_MIN, f_max: float = F_MAX, n: int = N_POINTS) -> np.ndarray:
    """``n`` log-spaced frequencies from ``f_min`` to ``f_max`` (Hz), both included."""
    if not (0 < f_min < f_max) or n < 2:
        raise ValueError(f"bad frequency grid {f_min}..{f_max} Hz with {n} points")
    return np.geomspace(f_min, f_max, n)


def full_band_grid(fs: float = FS, n: int = N_POINTS) -> np.ndarray:
    """DC plus ``n`` log-spaced frequencies from 0.1 Hz to Nyquist: everywhere the device can boost or deviate, not
    just the 5 Hz-24 kHz display range (a Linkwitz Transform peaks at DC, a 40 kHz shelf above 24 kHz)."""
    return np.concatenate([[0.0], np.geomspace(CHECK_F_MIN, fs / 2, n)])


def view_grid(view: str, n: int = N_POINTS) -> np.ndarray:
    """Grid for a named view preset (``sub``: 5-300 Hz, ``full``: 10 Hz-20 kHz)."""
    return freq_grid(*VIEWS[view], n=n)


def _coeffs(biquad: BiquadLike) -> tuple[float, float, float, float, float]:
    if isinstance(biquad, Biquad):
        return biquad.as_tuple()
    if isinstance(biquad, Mapping):
        return tuple(float(biquad[k]) for k in ("b0", "b1", "b2", "a1", "a2"))  # type: ignore[return-value]
    values = tuple(float(v) for v in biquad)
    if len(values) != 5:
        raise ValueError(f"a biquad has 5 coefficients (b0 b1 b2 a1 a2), got {len(values)}")
    return values  # type: ignore[return-value]


def to_float32(biquad: BiquadLike) -> tuple[float, float, float, float, float]:
    """Coefficients as the device stores them."""
    return tuple(float(x) for x in np.array(_coeffs(biquad), dtype=np.float32))  # type: ignore[return-value]


# --- transfer functions with derivative --------------------------------------------------------------------------


@dataclass(frozen=True)
class Tf:
    """``H`` and ``dH/dω`` (ω in rad/sample) sampled on one grid."""

    h: np.ndarray
    dh: np.ndarray

    @classmethod
    def unity(cls, n: int) -> "Tf":
        return cls(np.ones(n, dtype=complex), np.zeros(n, dtype=complex))

    @classmethod
    def zero(cls, n: int) -> "Tf":
        return cls(np.zeros(n, dtype=complex), np.zeros(n, dtype=complex))

    def __mul__(self, other: "Tf | complex | float") -> "Tf":
        if isinstance(other, Tf):
            return Tf(self.h * other.h, self.dh * other.h + self.h * other.dh)
        return Tf(self.h * other, self.dh * other)

    __rmul__ = __mul__

    def __add__(self, other: "Tf") -> "Tf":
        return Tf(self.h + other.h, self.dh + other.dh)


def _omega(freqs: np.ndarray, fs: float) -> np.ndarray:
    return 2 * np.pi * np.asarray(freqs, dtype=float) / fs


def biquad_tf(biquad: BiquadLike, freqs: np.ndarray, fs: float = FS, *, float32: bool = True) -> Tf:
    """``H = (b0 + b1·z⁻¹ + b2·z⁻²) / (1 − a1·z⁻¹ − a2·z⁻²)`` at ``z = e^{jω}`` (device convention)."""
    b0, b1, b2, a1, a2 = to_float32(biquad) if float32 else _coeffs(biquad)
    z1 = np.exp(-1j * _omega(freqs, fs))
    z2 = z1 * z1
    num = b0 + b1 * z1 + b2 * z2
    den = 1 - a1 * z1 - a2 * z2
    dnum = -1j * (b1 * z1 + 2 * b2 * z2)
    dden = 1j * (a1 * z1 + 2 * a2 * z2)
    return Tf(num / den, (dnum * den - num * dden) / (den * den))


def cascade_tf(biquads: Iterable[BiquadLike], freqs: np.ndarray, fs: float = FS, *, float32: bool = True) -> Tf:
    out = Tf.unity(len(freqs))
    for bq in biquads:
        out = out * biquad_tf(bq, freqs, fs, float32=float32)
    return out


def _pole_freqs(biquads: Sequence[BiquadLike], fs: float) -> list[float]:
    """Frequencies of each biquad's (float32) pole angles, where a sharp resonance peaks."""
    out = []
    for bq in biquads:
        _, _, _, a1, a2 = to_float32(bq)
        with np.errstate(over="ignore", invalid="ignore"):
            roots = np.roots([1.0, -a1, -a2])
        out += [abs(float(np.angle(p))) * fs / (2 * np.pi) for p in roots if np.isfinite(p)]
    return out


def peak_gain_db(biquads: Iterable[BiquadLike], fs: float = FS, n: int = 2048) -> float:
    """Highest gain of a biquad cascade (float32 coefficients), in dB: on a dense grid from DC to Nyquist plus every
    pole angle, so boosts below 5 Hz or above 24 kHz count too."""
    biquads = list(biquads)
    freqs = np.concatenate([full_band_grid(fs, n), _pole_freqs(biquads, fs)])
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        peak = float(np.max(np.abs(cascade_tf(biquads, freqs, fs).h)))
    if not math.isfinite(peak):
        return math.inf
    return 20 * math.log10(peak) if peak > 0 else DB_FLOOR


def delay_tf(samples: float, freqs: np.ndarray, fs: float = FS) -> Tf:
    h = np.exp(-1j * _omega(freqs, fs) * samples)
    return Tf(h, -1j * samples * h)


def fir_tf(taps: Sequence[float] | np.ndarray, freqs: np.ndarray, fs: float = FS, *, float32: bool = True) -> Tf:
    """DFT of FIR taps at arbitrary frequencies (chunked to keep memory flat for 4096 taps)."""
    h_taps = np.asarray(taps, dtype=np.float32 if float32 else float).astype(float)
    k = np.arange(len(h_taps))
    w = _omega(freqs, fs)
    h = np.empty(len(w), dtype=complex)
    dh = np.empty(len(w), dtype=complex)
    for start in range(0, len(w), 64):
        e = np.exp(-1j * np.outer(w[start : start + 64], k))
        h[start : start + 64] = e @ h_taps
        dh[start : start + 64] = e @ (-1j * k * h_taps)
    return Tf(h, dh)


def fir_peak_gain_db(taps: Sequence[float] | np.ndarray, fs: float = FS, n: int = 1024) -> float:
    """Highest gain of a FIR filter (float32 taps) on a 5 Hz-24 kHz grid, in dB."""
    with np.errstate(over="ignore", invalid="ignore"):
        peak = float(np.max(np.abs(fir_tf(taps, freq_grid(n=n), fs).h)))
    if not math.isfinite(peak):
        return math.inf
    return 20 * math.log10(peak) if peak > 0 else DB_FLOOR


def gain_factor(gain_db: float) -> float:
    return 10 ** (gain_db / 20)


def delay_samples(delay_ms: float, fs: float = FS) -> int:
    """Delay rounded to whole samples, as the device stores it (same rounding as `DeviceProfile.delay_samples`)."""
    return round(delay_ms * fs / 1000.0)


# --- results ----------------------------------------------------------------------------------------------------


@dataclass
class Response:
    """A sampled response plus the derived curves the UI plots."""

    freqs: np.ndarray
    h: np.ndarray
    dh: np.ndarray
    fs: float = FS
    float32_deviation_db: float | None = None
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_tf(cls, tf: Tf, freqs: np.ndarray, fs: float = FS, **kwargs: Any) -> "Response":
        return cls(np.asarray(freqs, dtype=float), tf.h, tf.dh, fs, **kwargs)

    @property
    def _silent(self) -> np.ndarray:
        return np.abs(self.h) == 0

    @property
    def magnitude(self) -> np.ndarray:
        return np.abs(self.h)

    @property
    def magnitude_db(self) -> np.ndarray:
        with np.errstate(divide="ignore"):
            db = 20 * np.log10(np.abs(self.h))
        return np.maximum(db, DB_FLOOR)

    @property
    def phase_deg(self) -> np.ndarray:
        """Wrapped to (−180, 180]; 0 where the response is exactly zero (muted)."""
        return np.degrees(np.where(self._silent, 0.0, np.angle(self.h)))

    @property
    def _dphase(self) -> np.ndarray:
        """dφ/dω in radians per (rad/sample), i.e. minus the group delay in samples."""
        silent = self._silent
        with np.errstate(divide="ignore", invalid="ignore"):
            d = np.imag(self.dh / np.where(silent, 1, self.h))
        return np.where(silent, 0.0, d)

    @property
    def phase_unwrapped_deg(self) -> np.ndarray:
        wrapped = np.where(self._silent, 0.0, np.angle(self.h))
        if len(wrapped) == 0:
            return wrapped
        w = _omega(self.freqs, self.fs)
        d = self._dphase
        out = np.empty_like(wrapped)
        out[0] = wrapped[0]
        for i in range(1, len(wrapped)):
            predicted = out[i - 1] + 0.5 * (d[i - 1] + d[i]) * (w[i] - w[i - 1])
            out[i] = wrapped[i] + 2 * np.pi * np.round((predicted - wrapped[i]) / (2 * np.pi))
        return np.degrees(out)

    @property
    def group_delay_ms(self) -> np.ndarray:
        return -self._dphase / self.fs * 1000.0

    def at(self, freq: float) -> complex:
        """H at one grid frequency (nearest point)."""
        return complex(self.h[int(np.argmin(np.abs(self.freqs - freq)))])

    def to_dict(self) -> dict[str, Any]:
        return {
            "freqs": self.freqs.tolist(),
            "magnitude_db": self.magnitude_db.tolist(),
            "phase_deg": self.phase_deg.tolist(),
            "phase_unwrapped_deg": self.phase_unwrapped_deg.tolist(),
            "group_delay_ms": self.group_delay_ms.tolist(),
            "float32_deviation_db": self.float32_deviation_db,
            "warnings": list(self.warnings),
        }


def max_db_difference(reference: Tf, other: Tf, floor_db: float = FLOAT32_RELEVANT_DB) -> float:
    """Largest |dB difference| between two responses, over the points where ``reference`` is above ``floor_db``."""
    with np.errstate(divide="ignore"):
        db_ref = 20 * np.log10(np.abs(reference.h))
        db_other = 20 * np.log10(np.maximum(np.abs(other.h), 1e-300))
    relevant = db_ref >= floor_db
    if not relevant.any():
        return 0.0
    return float(np.max(np.abs(db_other[relevant] - db_ref[relevant])))


def float32_deviation_db(exact: Tf, rounded: Tf) -> float:
    """How far float32 coefficient rounding moves the response, where the exact response is relevant."""
    return max_db_difference(exact, rounded)


def _finish(
    build: Any, freqs: np.ndarray, fs: float, float32: bool, label: str
) -> Response:  # build(float32: bool, freqs: np.ndarray) -> Tf
    tf = build(float32, freqs)
    # The deviation is judged on the plotted grid and on the full band, so float32 rounding that only shows below
    # 5 Hz (a Linkwitz Transform's DC gain) or above 24 kHz still warns.
    check = full_band_grid(fs)
    deviation = max(
        float32_deviation_db(build(False, freqs), tf if float32 else build(True, freqs)),
        float32_deviation_db(build(False, check), build(True, check)),
    )
    warnings = []
    if deviation > FLOAT32_WARN_DB:
        warnings.append(
            f"{label}: rounding note: the device stores filters as float32 numbers, which moves the response by up "
            f"to {deviation:.2f} dB"
        )
    return Response.from_tf(tf, freqs, fs, float32_deviation_db=deviation, warnings=warnings)


def filter_response(
    biquads: Iterable[BiquadLike],
    freqs: np.ndarray | None = None,
    fs: float = FS,
    *,
    float32: bool = True,
    label: str = "filter",
) -> Response:
    """Response of a biquad cascade (one band, one crossover group, or any list)."""
    freqs = freq_grid() if freqs is None else np.asarray(freqs, dtype=float)
    biquads = list(biquads)
    return _finish(lambda f32, grid: cascade_tf(biquads, grid, fs, float32=f32), freqs, fs, float32, label)


# --- channel chains -----------------------------------------------------------------------------------------------


def peq_tf(bands: Iterable[PeqBand], freqs: np.ndarray, fs: float = FS, *, float32: bool = True) -> Tf:
    """Cascade of the non-bypassed bands."""
    return cascade_tf((b.compiled for b in bands if not b.bypass), freqs, fs, float32=float32)


def crossover_tf(groups: Iterable[CrossoverGroup], freqs: np.ndarray, fs: float = FS, *, float32: bool = True) -> Tf:
    """Cascade of every biquad of each non-bypassed group."""
    return cascade_tf((bq for g in groups if not g.bypass for bq in g.compiled), freqs, fs, float32=float32)


def input_tf(doc: ConfigDoc, i: int, freqs: np.ndarray, fs: float = FS, *, float32: bool = True) -> Tf:
    """Input ``i``: PEQ · gain · mute."""
    ch = doc.inputs[i]
    if ch.mute:
        return Tf.zero(len(freqs))
    return peq_tf(ch.peq, freqs, fs, float32=float32) * gain_factor(ch.gain_db)


def output_stages(
    doc: ConfigDoc,
    n: int,
    freqs: np.ndarray,
    fs: float = FS,
    *,
    inputs: Sequence[int] | None = (0,),
    float32: bool = True,
    fir_taps: Sequence[float] | np.ndarray | None = None,
    master_db: float | None = None,
) -> dict[str, Tf]:
    """Each stage of output ``n``'s chain plus ``total`` (their product), in processing order.

    Stages: ``input`` (the routed input sum), ``peq``, ``crossover``, ``fir``, ``polarity`` (invert), ``delay``
    (whole samples), ``gain`` (output gain, mute and the optional master volume).

    ``inputs`` picks which inputs feed the sum (default: Input 1 only; None = all). The sum is complex, i.e. it
    assumes correlated (mono) inputs. A non-bypassed FIR without ``fir_taps`` contributes 1.
    """
    m = len(freqs)
    out = doc.outputs[n]
    selected = range(len(doc.inputs)) if inputs is None else inputs
    routed = Tf.zero(m)
    for i in selected:
        cell = doc.inputs[i].routing[n]
        if cell.enabled:
            routed = routed + input_tf(doc, i, freqs, fs, float32=float32) * gain_factor(cell.gain_db)
    stages = {
        "input": routed,
        "peq": peq_tf(out.peq, freqs, fs, float32=float32),
        "crossover": crossover_tf(out.crossover, freqs, fs, float32=float32),
        "fir": (
            fir_tf(fir_taps, freqs, fs, float32=float32) if fir_taps is not None and not out.fir.bypass else Tf.unity(m)
        ),
        "polarity": Tf.unity(m) * (-1.0 if out.invert else 1.0),
        "delay": delay_tf(delay_samples(out.delay_ms, fs), freqs, fs),
        "gain": Tf.unity(m) * (0.0 if out.mute else gain_factor(out.gain_db + (master_db or 0.0))),
    }
    total = Tf.unity(m)
    for tf in stages.values():
        total = total * tf
    stages["total"] = total
    return stages


def _fir_warning(doc: ConfigDoc, outputs: Iterable[int], fir_taps: Mapping[int, Any] | None) -> list[str]:
    return [
        f"output {n + 1}: the FIR is active but its taps are unknown, so it is plotted as flat"
        for n in outputs
        if not doc.outputs[n].fir.bypass and (fir_taps or {}).get(n) is None
    ]


def output_response(
    doc: ConfigDoc,
    n: int,
    freqs: np.ndarray | None = None,
    fs: float = FS,
    *,
    inputs: Sequence[int] | None = (0,),
    float32: bool = True,
    fir_taps: Mapping[int, Sequence[float] | np.ndarray] | None = None,
    master_db: float | None = None,
) -> Response:
    """Full-chain response of output ``n`` (what the per-output Response tab plots)."""
    return system_response(doc, [n], freqs, fs, inputs=inputs, float32=float32, fir_taps=fir_taps, master_db=master_db)


def system_response(
    doc: ConfigDoc,
    outputs: Sequence[int],
    freqs: np.ndarray | None = None,
    fs: float = FS,
    *,
    inputs: Sequence[int] | None = (0,),
    float32: bool = True,
    fir_taps: Mapping[int, Sequence[float] | np.ndarray] | None = None,
    master_db: float | None = None,
) -> Response:
    """Electrical sum of the selected outputs: a complex sum, so delay and polarity matter.

    It ignores drivers, transducers and the room.
    """
    freqs = freq_grid() if freqs is None else np.asarray(freqs, dtype=float)
    taps = fir_taps or {}

    def build(f32: bool, grid: np.ndarray) -> Tf:
        total = Tf.zero(len(grid))
        for n in outputs:
            stages = output_stages(
                doc, n, grid, fs, inputs=inputs, float32=f32, fir_taps=taps.get(n), master_db=master_db
            )
            total = total + stages["total"]
        return total

    label = ", ".join(f"output {n + 1}" for n in outputs) or "sum"
    resp = _finish(build, freqs, fs, float32, label)
    resp.warnings.extend(_fir_warning(doc, outputs, fir_taps))
    return resp


def input_response(
    doc: ConfigDoc, i: int, freqs: np.ndarray | None = None, fs: float = FS, *, float32: bool = True
) -> Response:
    """Input ``i``'s PEQ only (gain and mute excluded), as the Input page plots it."""
    freqs = freq_grid() if freqs is None else np.asarray(freqs, dtype=float)
    bands = doc.inputs[i].peq
    return _finish(lambda f32, grid: peq_tf(bands, grid, fs, float32=f32), freqs, fs, float32, f"input {i + 1} PEQ")


# --- compressor (docs 04 §6) -------------------------------------------------------------------------------------


def compressor_curve(threshold_db: float, ratio: float, in_db: np.ndarray | Sequence[float]) -> np.ndarray:
    """Static hard-knee curve: ``out = in`` up to the threshold, ``T + (in − T)/ratio`` above. Display only."""
    if not (math.isfinite(ratio) and ratio >= 1):
        raise ValueError(f"compressor ratio must be >= 1, got {ratio}")
    x = np.asarray(in_db, dtype=float)
    return np.where(x <= threshold_db, x, threshold_db + (x - threshold_db) / ratio)
