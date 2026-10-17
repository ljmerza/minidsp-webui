"""System view: the selected outputs, their electrical sum and the integration (phase difference) analysis of a pair
of outputs (docs 05 §6, docs 04 §4 "System / summed view").

- ``GET /devices/{serial}/presets/{p}/system``: each selected output's full-chain response, their complex sum and,
  for a pair (default: Shakers vs Sub), the phase difference over 20-100 Hz with the regions where |Δφ| > 120°.

Everything is electrical: delay, polarity and gains count, drivers, transducers and the room don't. The analysis
helpers here are pure (numpy) so the web UI route reuses them.
"""

import asyncio
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_session
from app.api.v1.dsp import MAX_POINTS, _fir_taps, _grid, _invalid, _parse_indexes, _parse_range
from app.api.v1.presets import translate_errors
from app.dsp import response as resp
from app.dsp.design import FS
from app.model.config_doc import ConfigDoc
from app.sync.session import DeviceSession

router = APIRouter(tags=["system"])

Session = Annotated[DeviceSession, Depends(get_session)]

INTEGRATION_RANGE: tuple[float, float] = (20.0, 100.0)
INTEGRATION_POINTS = 256
CANCEL_THRESHOLD_DEG = 120.0
# Below this an output is treated as silent and its phase as undefined (the response module's −200 dB floor).
SILENT = 1e-10

SHAKER_NAME = re.compile(r"shaker|tactile|transducer|butt ?kick", re.IGNORECASE)
SUB_NAME = re.compile(r"\bsub", re.IGNORECASE)


# --- integration analysis -------------------------------------------------------------------------------------------


def default_pair(names: Sequence[str]) -> tuple[int, int] | None:
    """The outputs the integration panel compares: the one named like shakers vs the one named like a sub, else
    outputs 1 and 2. None with fewer than two outputs."""
    if len(names) < 2:
        return None
    shaker = next((n for n, name in enumerate(names) if SHAKER_NAME.search(name)), None)
    sub = next((n for n, name in enumerate(names) if SUB_NAME.search(name) and n != shaker), None)
    if shaker is not None and sub is not None:
        return shaker, sub
    return 0, 1


def phase_difference_deg(h_a: np.ndarray, h_b: np.ndarray) -> np.ndarray:
    """``∠H_a − ∠H_b`` wrapped to (−180°, 180°]; NaN where either output is silent."""
    silent = (np.abs(h_a) <= SILENT) | (np.abs(h_b) <= SILENT)
    dphi = np.degrees(np.angle(h_a * np.conj(h_b)))
    dphi = np.where(dphi <= -180.0, dphi + 360.0, dphi)
    return np.where(silent, np.nan, dphi)


def level_difference_db(h_a: np.ndarray, h_b: np.ndarray) -> np.ndarray:
    """``20·log10(|H_a| / |H_b|)``; NaN where either output is silent."""
    a, b = np.abs(h_a), np.abs(h_b)
    silent = (a <= SILENT) | (b <= SILENT)
    with np.errstate(divide="ignore", invalid="ignore"):
        diff = 20 * np.log10(a / b)
    return np.where(silent, np.nan, diff)


def summation_deficit_db(h_a: np.ndarray, h_b: np.ndarray) -> np.ndarray:
    """How far the complex sum falls below the in-phase sum: ``20·log10(|H_a + H_b| / (|H_a| + |H_b|))``.

    0 dB when the two are in phase; very negative where they cancel. NaN where either output is silent.
    """
    a, b = np.abs(h_a), np.abs(h_b)
    silent = (a <= SILENT) | (b <= SILENT)
    with np.errstate(divide="ignore", invalid="ignore"):
        deficit = 20 * np.log10(np.maximum(np.abs(h_a + h_b), 1e-300) / (a + b))
    return np.where(silent, np.nan, np.maximum(deficit, resp.DB_FLOOR))


@dataclass(frozen=True)
class Region:
    """A run of frequencies where |Δφ| exceeds the threshold (the outputs partly cancel)."""

    f_lo: float
    f_hi: float
    peak_freq: float
    peak_dphi_deg: float  # signed Δφ at the largest |Δφ|
    worst_deficit_db: float | None  # lowest summation deficit in the region
    level_diff_db: float | None  # level of A relative to B at the peak

    def to_dict(self) -> dict[str, Any]:
        return {
            "f_lo": round(self.f_lo, 2),
            "f_hi": round(self.f_hi, 2),
            "peak_freq": round(self.peak_freq, 2),
            "peak_dphi_deg": round(self.peak_dphi_deg, 1),
            "worst_deficit_db": _num(self.worst_deficit_db, 2),
            "level_diff_db": _num(self.level_diff_db, 2),
        }


def _num(value: float | None, digits: int | None = None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits) if digits is not None else float(value)


def _crossing(freqs: np.ndarray, mag: np.ndarray, inside: int, outside: int, threshold: float) -> float:
    """Log-frequency where |Δφ| crosses ``threshold`` between grid points ``outside`` and ``inside``."""
    if outside < 0 or outside >= len(freqs) or not math.isfinite(mag[outside]):
        return float(freqs[inside])
    m0, m1 = float(mag[outside]), float(mag[inside])
    t = 0.0 if m1 == m0 else (threshold - m0) / (m1 - m0)
    t = min(1.0, max(0.0, t))
    lo, hi = math.log(freqs[outside]), math.log(freqs[inside])
    return math.exp(lo + (hi - lo) * t)


def cancellation_regions(
    freqs: np.ndarray,
    dphi_deg: np.ndarray,
    threshold_deg: float = CANCEL_THRESHOLD_DEG,
    *,
    level_diff_db: np.ndarray | None = None,
    deficit_db: np.ndarray | None = None,
) -> list[Region]:
    """Contiguous runs where |Δφ| > ``threshold_deg``, edges interpolated in log-frequency. Silent points (NaN)
    end a run. |Δφ| is continuous across the ±180° wrap, so the wrap never splits a region."""
    freqs = np.asarray(freqs, dtype=float)
    mag = np.abs(np.asarray(dphi_deg, dtype=float))
    over = np.nan_to_num(mag, nan=-1.0) > threshold_deg
    regions: list[Region] = []
    n = len(freqs)
    i = 0
    while i < n:
        if not over[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and over[j + 1]:
            j += 1
        k = i + int(np.argmax(mag[i : j + 1]))
        worst = None
        if deficit_db is not None:
            segment = np.asarray(deficit_db[i : j + 1], dtype=float)
            if np.isfinite(segment).any():
                worst = float(np.nanmin(segment))
        regions.append(
            Region(
                f_lo=_crossing(freqs, mag, i, i - 1, threshold_deg),
                f_hi=_crossing(freqs, mag, j, j + 1, threshold_deg),
                peak_freq=float(freqs[k]),
                peak_dphi_deg=float(dphi_deg[k]),
                worst_deficit_db=worst,
                level_diff_db=None if level_diff_db is None else _num(level_diff_db[k]),
            )
        )
        i = j + 1
    return regions


@dataclass
class Integration:
    """Phase difference of outputs ``a`` and ``b`` (``∠a − ∠b``) on its own grid."""

    a: int
    b: int
    freqs: np.ndarray
    phase_diff_deg: np.ndarray
    level_diff_db: np.ndarray
    deficit_db: np.ndarray
    threshold_deg: float
    regions: list[Region]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, digits: int | None = None) -> dict[str, Any]:
        def values(arr: np.ndarray) -> list[float | None]:
            return [_num(v, digits) for v in arr]

        return {
            "a": self.a,
            "b": self.b,
            "range": [float(self.freqs[0]), float(self.freqs[-1])],
            "threshold_deg": self.threshold_deg,
            "freqs": [float(f"{f:.6g}") for f in self.freqs] if digits is not None else self.freqs.tolist(),
            "phase_diff_deg": values(self.phase_diff_deg),
            "level_diff_db": values(self.level_diff_db),
            "deficit_db": values(self.deficit_db),
            "regions": [r.to_dict() for r in self.regions],
            "warnings": list(self.warnings),
        }


def integration(
    doc: ConfigDoc,
    a: int,
    b: int,
    fs: float = FS,
    *,
    names: Sequence[str] | None = None,
    inputs: Sequence[int] | None = (0,),
    fir_taps: Mapping[int, Any] | None = None,
    f_range: tuple[float, float] = INTEGRATION_RANGE,
    points: int = INTEGRATION_POINTS,
    threshold_deg: float = CANCEL_THRESHOLD_DEG,
    float32: bool = True,
) -> Integration:
    """Compare outputs ``a`` and ``b`` over ``f_range``. A muted output is analysed as if it were un-muted (the
    panel is for tuning delay and polarity), with a warning."""
    names = list(names) if names is not None else [f"Out {n + 1}" for n in range(len(doc.outputs))]
    taps = fir_taps or {}
    warnings: list[str] = []
    muted = [n for n in (a, b) if doc.outputs[n].mute]
    if muted:
        doc = doc.model_copy(deep=True)
        for n in muted:
            doc.outputs[n].mute = False
            warnings.append(f"{names[n]} is muted; the integration panel treats it as un-muted")
    freqs = resp.freq_grid(*f_range, n=points)

    def total(n: int) -> np.ndarray:
        stages = resp.output_stages(doc, n, freqs, fs, inputs=inputs, float32=float32, fir_taps=taps.get(n))
        return stages["total"].h

    h_a, h_b = total(a), total(b)
    for n, h in ((a, h_a), (b, h_b)):
        if np.all(np.abs(h) <= SILENT):
            warnings.append(f"{names[n]} is silent here (no selected input is routed to it), so there is no phase")
    dphi = phase_difference_deg(h_a, h_b)
    level = level_difference_db(h_a, h_b)
    deficit = summation_deficit_db(h_a, h_b)
    regions = cancellation_regions(freqs, dphi, threshold_deg, level_diff_db=level, deficit_db=deficit)
    return Integration(a, b, freqs, dphi, level, deficit, threshold_deg, regions, warnings)


# --- multi-output response ------------------------------------------------------------------------------------------


@dataclass
class SystemResult:
    outputs: list[int]
    channels: dict[int, resp.Response]
    total: resp.Response | None  # None when no output is selected
    warnings: list[str]


def system_view(
    doc: ConfigDoc,
    outputs: Sequence[int],
    freqs: np.ndarray,
    fs: float = FS,
    *,
    names: Sequence[str] | None = None,
    inputs: Sequence[int] | None = (0,),
    fir_taps: Mapping[int, Any] | None = None,
    float32: bool = True,
) -> SystemResult:
    """Each selected output's full chain and their electrical (complex) sum."""
    names = list(names) if names is not None else [f"Out {n + 1}" for n in range(len(doc.outputs))]
    feeding = range(len(doc.inputs)) if inputs is None else inputs
    channels: dict[int, resp.Response] = {}
    warnings: list[str] = []
    for n in outputs:
        single = resp.output_response(doc, n, freqs, fs, inputs=inputs, float32=float32, fir_taps=fir_taps)
        channels[n] = single
        warnings.extend(single.warnings)
        if doc.outputs[n].mute:
            warnings.append(f"{names[n]} is muted, so it adds nothing to the sum")
        elif not any(doc.inputs[i].routing[n].enabled for i in feeding):
            warnings.append(f"no selected input is routed to {names[n]}, so it is silent")
    total = None
    if outputs:
        total = resp.system_response(doc, list(outputs), freqs, fs, inputs=inputs, float32=float32, fir_taps=fir_taps)
        warnings.extend(total.warnings)
    return SystemResult(list(outputs), channels, total, list(dict.fromkeys(warnings)))


def output_names(session: DeviceSession) -> list[str]:
    meta = getattr(session, "channel_meta", {}) or {}
    names = []
    for n in range(session.profile.n_outputs):
        m = meta.get(("output", n))
        names.append(getattr(m, "name", None) or f"Out {n + 1}")
    return names


def parse_pair(text: str | None, count: int, names: Sequence[str]) -> tuple[int, int] | None:
    """``0,1`` -> (0, 1); ``none`` -> None; empty -> `default_pair`. ValueError when malformed."""
    if text is None or not text.strip():
        return default_pair(names)
    if text.strip() == "none":
        return None
    try:
        a, b = (int(part) for part in text.split(","))
    except ValueError:
        raise ValueError("pair must be two comma-separated output indexes, e.g. 0,1, or none") from None
    if not (0 <= a < count and 0 <= b < count):
        raise ValueError(f"pair outputs must be within 0..{count - 1}")
    if a == b:
        raise ValueError("pair needs two different outputs")
    return a, b


# --- route ----------------------------------------------------------------------------------------------------------


@router.get("/devices/{serial}/presets/{preset}/system")
@translate_errors
async def system(
    preset: int,
    session: Session,
    outputs: Annotated[str | None, Query(description="e.g. 0,1, all or none (default: every un-muted output)")] = None,
    range: Annotated[str | None, Query(description="LOW-HIGH in Hz (e.g. 5-300), or a view: sub, full")] = "sub",
    inputs: Annotated[str | None, Query(description="Inputs feeding the outputs: 0, 0,1 or all (default 0)")] = None,
    pair: Annotated[
        str | None, Query(description="Integration pair a,b (Δφ = ∠a − ∠b); none to skip; default Shakers vs Sub")
    ] = None,
    threshold: Annotated[float, Query(gt=0, le=180, description="Cancellation threshold |Δφ| in degrees")] = (
        CANCEL_THRESHOLD_DEG
    ),
    points: Annotated[int, Query(ge=2, le=MAX_POINTS)] = resp.N_POINTS,
    float32: bool = True,
) -> Any:
    """The selected outputs of the preset's desired state, their electrical sum, and the integration analysis.
    Electrical only: delays and polarity count; drivers, transducers and the room are ignored."""
    doc = await session.get_desired(preset)
    profile = session.profile
    names = output_names(session)
    if outputs is not None and outputs.strip() == "none":
        selected: list[int] = []
    else:
        selected = _parse_indexes(outputs, profile.n_outputs, "outputs") or []
        if outputs is None or not outputs.strip():
            selected = [n for n, ch in enumerate(doc.outputs) if not ch.mute]
    feeding = _parse_indexes(inputs, profile.n_inputs, "inputs") or [0]
    try:
        the_pair = parse_pair(pair, profile.n_outputs, names)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    f_min, f_max, view = _parse_range(range)
    fs = profile.sample_rate
    try:
        freqs = _grid(f_min, f_max, view, points, fs)
    except ValueError as exc:
        return _invalid(str(exc))
    wanted = sorted(set(selected) | set(the_pair or ()))
    taps = await _fir_taps(session, doc, wanted)

    def run() -> dict[str, Any]:
        view_result = system_view(doc, selected, freqs, fs, names=names, inputs=feeding, fir_taps=taps, float32=float32)
        out: dict[str, Any] = {
            "serial": session.serial,
            "preset": preset,
            "outputs": selected,
            "inputs": feeding,
            "names": names,
            "range": [float(freqs[0]), float(freqs[-1])],
            "label": "electrical sum",
            "channels": {str(n): r.to_dict() for n, r in view_result.channels.items()},
            "sum": None if view_result.total is None else view_result.total.to_dict(),
            "integration": None,
        }
        warnings = list(view_result.warnings)
        if the_pair is not None:
            a, b = the_pair
            analysis = integration(
                doc, a, b, fs, names=names, inputs=feeding, fir_taps=taps, threshold_deg=threshold, float32=float32
            )
            out["integration"] = analysis.to_dict()
            warnings.extend(analysis.warnings)
        out["warnings"] = list(dict.fromkeys(warnings))
        return out

    return await asyncio.to_thread(run)
