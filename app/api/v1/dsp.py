"""DSP computations: stateless design and recognition, and server-computed responses of a preset (docs 04, 06).

- ``POST /dsp/design``: a filter design, crossover design or raw biquad list -> device-convention biquads, a label
  and the response. The UI calls it on drag release; nothing is stored.
- ``POST /dsp/recognize``: raw biquads -> the editable designs they implement.
- ``GET /devices/{serial}/presets/{p}/response``: full-chain response of outputs of a preset's desired state, and
  their electrical sum. ``…/inputs/{i}/response``: an input's PEQ.

Responses are evaluated with float32-rounded coefficients (what the device stores); ``warnings`` reports where that
moves the curve by more than 0.1 dB, and FIRs whose taps are unknown.
"""

import asyncio
import math
from collections.abc import Sequence
from typing import Annotated, Any, Literal

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.deps import get_session
from app.api.v1.presets import translate_errors
from app.dsp import response as resp
from app.dsp.design import FS, DesignError, biquads_needed, design_crossover, design_filter
from app.dsp.design import raw as raw_biquad
from app.dsp.recognize import describe_crossover, describe_filter, recognize_biquad, recognize_crossover
from app.model.config_doc import ConfigDoc
from app.model.filters import Biquad, CrossoverDesign, FilterDesign
from app.sync.session import DeviceSession

router = APIRouter(tags=["dsp"])

Session = Annotated[DeviceSession, Depends(get_session)]
BiquadIn = Biquad | Annotated[list[float], Field(min_length=5, max_length=5)]
MAX_POINTS = 4096
MAX_BIQUADS = 64


class Grid(BaseModel):
    view: Literal["sub", "full"] | None = Field(default=None, description="sub: 5-300 Hz, full: 10 Hz-20 kHz")
    f_min: float | None = Field(default=None, gt=0)
    f_max: float | None = Field(default=None, gt=0)
    points: int = Field(default=resp.N_POINTS, ge=2, le=MAX_POINTS)


class DesignRequest(Grid):
    model_config = ConfigDict(extra="forbid")

    filter: FilterDesign | None = Field(default=None, description="One PEQ band design")
    crossover: CrossoverDesign | None = Field(default=None, description="One crossover group design")
    biquads: list[BiquadIn] | None = Field(default=None, max_length=MAX_BIQUADS, description="Raw biquads to evaluate")
    n_biquads: int = Field(default=4, ge=1, le=8, description="Crossover group size (padded with pass-through)")
    float32: bool = Field(default=True, description="Evaluate with float32-rounded coefficients")

    @model_validator(mode="after")
    def _one_input(self) -> "DesignRequest":
        given = [name for name in ("filter", "crossover", "biquads") if getattr(self, name) is not None]
        if len(given) != 1:
            raise ValueError("give exactly one of filter, crossover or biquads")
        return self


class RecognizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    biquads: list[BiquadIn] = Field(min_length=1, max_length=MAX_BIQUADS)
    group: bool = Field(default=False, description="Also recognize them together as one crossover group")


def _invalid(detail: str, problems: Sequence[str] = ()) -> JSONResponse:
    return JSONResponse(
        status_code=422, content={"detail": detail, "error": "invalid", "problems": list(problems) or [detail]}
    )


def _grid(f_min: float | None, f_max: float | None, view: str | None, points: int, fs: float = FS) -> np.ndarray:
    """The frequency grid; raises ValueError with a readable message."""
    if view is not None:
        if f_min is not None or f_max is not None:
            raise ValueError("give a view or a frequency range, not both")
        lo, hi = resp.VIEWS[view]
    else:
        lo = resp.F_MIN if f_min is None else f_min
        hi = resp.F_MAX if f_max is None else f_max
    if not (math.isfinite(lo) and math.isfinite(hi) and 0 < lo < hi <= fs / 2):
        raise ValueError(f"frequency range {lo:g}-{hi:g} Hz must satisfy 0 < low < high <= {fs / 2:g} Hz")
    return resp.freq_grid(lo, hi, points)


def _biquads(values: Sequence[BiquadIn]) -> list[Biquad]:
    out = []
    for k, value in enumerate(values):
        try:
            out.append(raw_biquad(value))
        except (DesignError, ValueError) as exc:
            raise DesignError(f"biquads.{k}: {exc}") from None
    return out


def _design(body: DesignRequest) -> dict[str, Any]:
    freqs = _grid(body.f_min, body.f_max, body.view, body.points)
    if body.filter is not None:
        biquads = [design_filter(body.filter)]
        label, used = describe_filter(body.filter), 1
    elif body.crossover is not None:
        biquads = design_crossover(body.crossover, body.n_biquads)
        label, used = describe_crossover(body.crossover), biquads_needed(body.crossover)
    else:
        biquads = _biquads(body.biquads or [])
        label, used = None, sum(1 for bq in biquads if not bq.is_passthrough)
    response = resp.filter_response(biquads, freqs, float32=body.float32, label=label or "filter")
    return {
        "biquads": [bq.model_dump() for bq in biquads],
        "label": label,
        "biquads_used": used,
        "response": response.to_dict(),
    }


@router.post("/dsp/design")
async def design(body: DesignRequest) -> Any:
    """Stateless: design -> biquads (device convention, fs = 96 kHz) + response. 422 for impossible or unstable
    designs and crossovers needing more than ``n_biquads`` sections."""
    try:
        return await asyncio.to_thread(_design, body)
    except ValueError as exc:  # DesignError included
        return _invalid(str(exc))


@router.post("/dsp/recognize")
async def recognize(body: RecognizeRequest) -> Any:
    """Raw biquads -> the designs they implement (``raw`` when unrecognized). With ``group``, the crossover design
    of all of them together (None = custom)."""

    def run() -> dict[str, Any]:
        biquads = _biquads(body.biquads)
        designs = [recognize_biquad(bq) for bq in biquads]
        out: dict[str, Any] = {
            "filters": [{"design": d.model_dump(mode="json"), "label": describe_filter(d)} for d in designs]
        }
        if body.group:
            group = recognize_crossover(biquads)
            out["crossover"] = {
                "design": None if group is None else group.model_dump(mode="json"),
                "label": describe_crossover(group),
            }
        return out

    try:
        return await asyncio.to_thread(run)
    except ValueError as exc:
        return _invalid(str(exc))


# --- preset responses -------------------------------------------------------------------------------------------


def _parse_range(text: str | None) -> tuple[float | None, float | None, str | None]:
    """``5-300`` -> (5, 300, None); ``sub``/``full`` -> (None, None, view); None -> defaults."""
    if text is None or not text.strip():
        return None, None, None
    text = text.strip()
    if text in resp.VIEWS:
        return None, None, text
    lo, sep, hi = text.partition("-")
    try:
        if not sep:
            raise ValueError
        return float(lo), float(hi), None
    except ValueError:
        detail = "range must be LOW-HIGH in Hz, sub or full"
        raise HTTPException(status_code=422, detail=detail) from None


def _parse_indexes(text: str | None, count: int, what: str) -> list[int] | None:
    """``0,2`` -> [0, 2]; ``all`` -> every index; None/empty -> None (caller's default)."""
    if text is None or not text.strip():
        return None
    if text.strip() == "all":
        return list(range(count))
    try:
        values = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{what} must be comma-separated indexes or all") from None
    bad = [v for v in values if not 0 <= v < count]
    if bad:
        raise HTTPException(status_code=422, detail=f"{what} {bad} outside 0..{count - 1}")
    return list(dict.fromkeys(values))


async def _fir_taps(session: DeviceSession, doc: ConfigDoc, outputs: Sequence[int]) -> dict[int, Any]:
    """Taps of the active FIRs of ``outputs`` that are stored (others are plotted flat with a warning)."""
    taps: dict[int, Any] = {}
    if session.repo is None:
        return taps
    for n in outputs:
        fir = doc.outputs[n].fir
        if not fir.bypass and fir.blob_id is not None:
            blob = await session.repo.get_fir_blob(fir.blob_id)
            if blob is not None and blob.taps is not None:
                taps[n] = blob.taps
    return taps


Range = Annotated[str | None, Query(description="LOW-HIGH in Hz (e.g. 5-300), or a view: sub, full")]
Points = Annotated[int, Query(ge=2, le=MAX_POINTS)]
Float32 = Annotated[bool, Query(description="Evaluate with float32-rounded coefficients")]


@router.get("/devices/{serial}/presets/{preset}/response")
@translate_errors
async def preset_response(
    preset: int,
    session: Session,
    outputs: Annotated[str | None, Query(description="e.g. 0,1 or all (default: every un-muted output)")] = None,
    range: Range = None,
    inputs: Annotated[str | None, Query(description="Inputs feeding the sum: e.g. 0, 0,1 or all (default 0)")] = None,
    master: Annotated[bool, Query(description="Include the current master volume")] = False,
    channels: Annotated[bool, Query(description="Also return each selected output on its own")] = False,
    points: Points = resp.N_POINTS,
    float32: Float32 = True,
) -> Any:
    """Full-chain response of the selected outputs of the preset's desired state, and their electrical sum
    (complex: delay and polarity count; no drivers or room)."""
    doc = await session.get_desired(preset)
    profile = session.profile
    selected = _parse_indexes(outputs, profile.n_outputs, "outputs")
    if selected is None:
        selected = [n for n, ch in enumerate(doc.outputs) if not ch.mute]
    feeding = _parse_indexes(inputs, profile.n_inputs, "inputs")
    if feeding is None:
        feeding = [0]
    f_min, f_max, view = _parse_range(range)
    try:
        freqs = _grid(f_min, f_max, view, points, profile.sample_rate)
    except ValueError as exc:
        return _invalid(str(exc))
    master_db: float | None = None
    notes: list[str] = []
    if master:
        live = session.master
        if live is None or live.volume is None:
            raise HTTPException(status_code=503, detail="the master volume is not known yet")
        master_db = live.volume
        if live.mute:
            notes.append("the master is muted (not shown in the plot)")
    taps = await _fir_taps(session, doc, selected)
    fs = profile.sample_rate

    def run() -> dict[str, Any]:
        kwargs: dict[str, Any] = {"inputs": feeding, "float32": float32, "fir_taps": taps, "master_db": master_db}
        total = resp.system_response(doc, selected, freqs, fs, **kwargs)
        out: dict[str, Any] = {
            "serial": session.serial,
            "preset": preset,
            "outputs": selected,
            "inputs": feeding,
            "range": [float(freqs[0]), float(freqs[-1])],
            "master_db": master_db,
            "sum": total.to_dict(),
        }
        warnings = [*total.warnings, *notes]
        if channels:
            out["channels"] = {}
            for n in selected:
                single = resp.output_response(doc, n, freqs, fs, **kwargs)
                out["channels"][str(n)] = single.to_dict()
                warnings.extend(single.warnings)
        out["warnings"] = list(dict.fromkeys(warnings))
        return out

    return await asyncio.to_thread(run)


@router.get("/devices/{serial}/presets/{preset}/inputs/{index}/response")
@translate_errors
async def input_response(
    preset: int,
    index: int,
    session: Session,
    range: Range = None,
    points: Points = resp.N_POINTS,
    float32: Float32 = True,
) -> Any:
    """An input's PEQ response (gain and mute excluded), as the Input page plots it."""
    doc = await session.get_desired(preset)
    if not 0 <= index < session.profile.n_inputs:
        raise HTTPException(status_code=422, detail=f"input {index} outside 0..{session.profile.n_inputs - 1}")
    f_min, f_max, view = _parse_range(range)
    try:
        freqs = _grid(f_min, f_max, view, points, session.profile.sample_rate)
    except ValueError as exc:
        return _invalid(str(exc))
    fs = session.profile.sample_rate
    response = await asyncio.to_thread(resp.input_response, doc, index, freqs, fs, float32=float32)
    return {"serial": session.serial, "preset": preset, "input": index, **response.to_dict()}
