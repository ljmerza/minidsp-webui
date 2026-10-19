"""Per-output compressor and FIR filter (docs 05 §4, docs 06).

Compressor: ``GET/PUT …/presets/{p}/outputs/{n}/compressor``. The body sets any of ``threshold_db``, ``ratio``,
``attack_ms``, ``release_ms`` and ``bypass``, or a ``preset`` (whose values explicit fields override). Only the
threshold reads back from the device (spike results §5): ratio, attack and release are write-only floats with
unverified units, so the shadow is the only record of them. The transfer curve is the hard-knee model of docs 04 §6
and is approximate: the device's knee and makeup gain aren't settable through the API.

FIR (hidden unless the device profile has FIR support, i.e. the experimental toggle is on):

- ``POST …/outputs/{n}/fir`` (multipart ``file``): validate the WAV, store it, return the blob and its preview.
  With ``?load=true&confirm=true`` it is also loaded.
- ``POST …/outputs/{n}/fir/load`` ``{blob_id, enable?}``: load a stored blob. Needs ``?confirm=true`` (409
  ``confirmation_required`` otherwise): the device mutes while the taps load.
- ``POST …/outputs/{n}/fir/clear``: bypass and clear the taps (``confirm`` as for load).
- ``PUT …/outputs/{n}/fir`` ``{bypass}``: enable or bypass.
- ``GET /fir/capability`` / ``PUT /fir/capability`` ``{enabled}``: the experimental FIR toggle.

FIR requests while FIR is off answer 409 ``fir_disabled``. ``If-Match`` and ``commit`` work as for
``PATCH …/presets/{p}``.
"""

import functools
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_manager, get_session
from app.api.v1.presets import Commit, Confirm, IfMatch, edit_json, parse_if_match, translate_errors
from app.model.payload import COMPRESSOR_RANGES
from app.sync import fir as fir_svc
from app.sync.session import ConfirmationRequired, DesiredValidationError, DeviceSession

router = APIRouter(tags=["compressor", "fir"])

Session = Annotated[DeviceSession, Depends(get_session)]

COMPRESSOR_FIELDS = tuple(COMPRESSOR_RANGES)
COMPRESSOR_PRESETS: dict[str, dict[str, Any]] = {
    "tactile_limiter": {
        "label": "Shaker protection",
        "values": {"threshold_db": -36.0, "ratio": 50.0, "attack_ms": 15.0, "release_ms": 30.0},
        "note": "From the minidsp-rs docs example: a near-limiter for shaker amps",
    },
    "beq_safety": {
        "label": "Safety limit",
        "values": {"threshold_db": 0.0, "ratio": 50.0, "attack_ms": 15.0, "release_ms": 30.0},
        "note": "Limits only at full scale: a guard against bass-EQ boosts clipping",
    },
}
COMPRESSOR_NOTES = (
    "Only the threshold can be read back from the device; ratio, attack and release are write-only and their units "
    "(x:1, ms) are unverified.",
    "The transfer curve is approximate (hard knee): the device's knee and makeup gain aren't set by the API.",
    "Whether the compressor is enabled can't be read back either; the app's record is the only source.",
)
CURVE_RANGE = (-90.0, 0.0)
CURVE_STEP_DB = 1.0


def transfer_curve(
    threshold_db: float,
    ratio: float,
    lo: float = CURVE_RANGE[0],
    hi: float = CURVE_RANGE[1],
    step: float = CURVE_STEP_DB,
) -> list[list[float]]:
    """Static curve (docs 04 §6): ``out = in`` up to the threshold, ``T + (in - T) / ratio`` above it."""
    ratio = max(ratio, 1.0)
    points = []
    n = round((hi - lo) / step)
    inputs = sorted({lo + k * step for k in range(n + 1)} | ({threshold_db} if lo < threshold_db < hi else set()))
    for x in inputs:
        y = x if x <= threshold_db else threshold_db + (x - threshold_db) / ratio
        points.append([round(x, 3), round(y, 3)])
    return points


def compressor_problems(values: dict[str, float]) -> list[str]:
    problems = []
    for key, value in values.items():
        low, high = COMPRESSOR_RANGES[key]
        if not low <= value <= high:
            problems.append(f"compressor.{key}: {value:g} outside {low:g}..{high:g}")
    return problems


class CompressorPut(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    preset: Literal["tactile_limiter", "beq_safety"] | None = Field(
        default=None, description="Fill in threshold/ratio/attack/release from a preset"
    )
    threshold_db: float | None = Field(default=None, description="dBFS")
    ratio: float | None = Field(default=None, description="x:1 (unverified units)")
    attack_ms: float | None = Field(default=None, description="ms (unverified units)")
    release_ms: float | None = Field(default=None, description="ms (unverified units)")
    bypass: bool | None = Field(default=None, description="Omit to keep the current flag")


class FirBypassPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bypass: bool


class FirLoad(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blob_id: str = Field(min_length=1, max_length=128)
    enable: bool | None = Field(default=None, description="Also enable (true) or bypass (false) the FIR")


class CapabilityPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _error(status: int, error: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "error": error, **extra})


def fir_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R | JSONResponse]]:
    """FIR-specific errors; everything else goes through `translate_errors`."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | JSONResponse:
        try:
            return await fn(*args, **kwargs)
        except fir_svc.FirDisabled as exc:
            return _error(409, "fir_disabled", str(exc))
        except fir_svc.FirBlobNotFound as exc:
            return _error(404, "fir_blob_not_found", str(exc))

    return wrapper


def _edit_kwargs(commit: bool, if_match: str | None) -> dict[str, Any]:
    return {"commit": commit, "expected_revision": parse_if_match(if_match), "source": "api"}


def _check_output(session: DeviceSession, channel: int) -> str:
    count = session.profile.n_outputs
    if not 0 <= channel < count:
        raise DesiredValidationError(f"output {channel} outside 0..{count - 1}")
    return f"outputs.{channel}.compressor"


# --- compressor -----------------------------------------------------------------------------------------------------


@router.get("/compressor/presets")
async def compressor_presets() -> dict:
    """Compressor presets, accepted ranges and caveats."""
    return {"presets": COMPRESSOR_PRESETS, "ranges": COMPRESSOR_RANGES, "notes": COMPRESSOR_NOTES}


async def compressor_view(session: DeviceSession, preset: int, channel: int) -> dict[str, Any]:
    path = _check_output(session, channel)
    comp = (await session.get_desired(preset)).outputs[channel].compressor
    status = session.sync_status(preset)
    return {
        "serial": session.serial,
        "preset": preset,
        "channel": channel,
        "path": path,
        "compressor": comp.model_dump(mode="json"),
        "sync_status": {p: s for p, s in status.items() if p.startswith(f"{path}.")},
        "curve": transfer_curve(comp.threshold_db, comp.ratio),
        "presets": COMPRESSOR_PRESETS,
        "ranges": COMPRESSOR_RANGES,
        "readable": ["threshold_db"],
        "notes": COMPRESSOR_NOTES,
    }


@router.get("/devices/{serial}/presets/{preset}/outputs/{channel}/compressor")
@translate_errors
async def get_compressor(preset: int, channel: int, session: Session) -> dict:
    """The output's compressor, its sync status and the (approximate) transfer curve."""
    return await compressor_view(session, preset, channel)


@router.put("/devices/{serial}/presets/{preset}/outputs/{channel}/compressor")
@translate_errors
async def put_compressor(
    preset: int,
    channel: int,
    body: CompressorPut,
    session: Session,
    confirm: Confirm = False,
    commit: Commit = False,
    if_match: IfMatch = None,
) -> dict:
    _check_output(session, channel)
    values: dict[str, Any] = dict(COMPRESSOR_PRESETS[body.preset]["values"]) if body.preset else {}
    values.update({k: getattr(body, k) for k in COMPRESSOR_FIELDS if getattr(body, k) is not None})
    if problems := compressor_problems(values):
        raise DesiredValidationError("compressor setting out of range", problems)
    fields: dict[str, Any] = dict(values)
    if body.bypass is not None:
        fields["bypass"] = body.bypass
    if not fields:
        raise DesiredValidationError("nothing to change: give a preset, a setting or bypass")
    patch = {"outputs": {str(channel): {"compressor": fields}}}
    result = await session.edit(preset, patch, confirm=confirm, **_edit_kwargs(commit, if_match))
    return {**edit_json(session, result), "view": await compressor_view(session, preset, channel)}


# --- FIR ------------------------------------------------------------------------------------------------------------


def _sessions(request: Request) -> list[DeviceSession]:
    return list(get_manager(request))


@router.get("/fir/capability")
async def get_fir_capability(request: Request) -> dict:
    """Whether experimental FIR is on, and which devices currently expose FIR."""
    sessions = _sessions(request)
    settings = request.app.state.settings
    enabled = fir_svc.fir_experimental(settings) or any(s.profile.fir_supported for s in sessions)
    return {"enabled": enabled, "devices": {str(s.serial): s.profile.fir_supported for s in sessions}}


@router.put("/fir/capability")
async def put_fir_capability(body: CapabilityPut, request: Request) -> dict:
    """Turn experimental FIR on or off (persisted). FIR paths are only written once touched."""
    sessions = _sessions(request)
    repo = getattr(request.app.state, "repo", None)
    changed = await fir_svc.set_fir_capability(
        body.enabled, settings=request.app.state.settings, sessions=sessions, repo=repo
    )
    return {
        "enabled": body.enabled,
        "changed": changed,
        "devices": {str(s.serial): s.profile.fir_supported for s in sessions},
    }


@router.get("/devices/{serial}/presets/{preset}/outputs/{channel}/fir")
@translate_errors
@fir_errors
async def get_fir(
    preset: int,
    channel: int,
    session: Session,
    preview: Annotated[bool, Query(description="Include the impulse and response of the loaded blob")] = True,
) -> dict:
    """The output's FIR: bypass, loaded blob (with preview), sync status. Answers even while FIR is off."""
    return await fir_svc.fir_state(session, preset, channel, with_preview=preview)


@router.post("/devices/{serial}/presets/{preset}/outputs/{channel}/fir")
@translate_errors
@fir_errors
async def upload_fir(
    preset: int,
    channel: int,
    session: Session,
    file: Annotated[UploadFile, File(description="Mono WAV at the device rate (96 kHz), at most 4096 taps")],
    load: Annotated[bool, Query(description="Also load it to the output (needs confirm)")] = False,
    enable: Annotated[bool | None, Query(description="With load: also enable (true) or bypass (false)")] = None,
    confirm: Confirm = False,
    commit: Commit = False,
    if_match: IfMatch = None,
) -> dict:
    fir_svc.require_fir(session.profile)
    fir_svc.check_output(session.profile, channel)
    data = await file.read(fir_svc.MAX_UPLOAD_BYTES + 1)
    name = (file.filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:120] or None
    if load and not confirm:  # refuse before storing anything, so the retry with confirm is the only upload
        raise ConfirmationRequired(fir_svc.LOAD_WARNING)
    blob, taps = await fir_svc.upload(session, data, name=name)
    out: dict[str, Any] = {
        "blob": fir_svc.blob_json(blob),
        "preview": fir_svc.preview(taps, session.profile.sample_rate),
    }
    if load:
        result = await fir_svc.load(
            session, preset, channel, blob.id, confirm=True, enable=enable, **_edit_kwargs(commit, if_match)
        )
        out.update(edit_json(session, result))
    return out


@router.post("/devices/{serial}/presets/{preset}/outputs/{channel}/fir/load")
@translate_errors
@fir_errors
async def load_fir(
    preset: int,
    channel: int,
    body: FirLoad,
    session: Session,
    confirm: Confirm = False,
    commit: Commit = False,
    if_match: IfMatch = None,
) -> dict:
    result = await fir_svc.load(
        session, preset, channel, body.blob_id, confirm=confirm, enable=body.enable, **_edit_kwargs(commit, if_match)
    )
    state = await fir_svc.fir_state(session, preset, channel, with_preview=False)
    return {**edit_json(session, result), "fir": state}


@router.post("/devices/{serial}/presets/{preset}/outputs/{channel}/fir/clear")
@translate_errors
@fir_errors
async def clear_fir(
    preset: int,
    channel: int,
    session: Session,
    confirm: Confirm = False,
    commit: Commit = False,
    if_match: IfMatch = None,
) -> dict:
    result = await fir_svc.clear(session, preset, channel, confirm=confirm, **_edit_kwargs(commit, if_match))
    state = await fir_svc.fir_state(session, preset, channel, with_preview=False)
    return {**edit_json(session, result), "fir": state}


@router.put("/devices/{serial}/presets/{preset}/outputs/{channel}/fir")
@translate_errors
@fir_errors
async def put_fir(
    preset: int,
    channel: int,
    body: FirBypassPut,
    session: Session,
    confirm: Confirm = False,
    commit: Commit = False,
    if_match: IfMatch = None,
) -> dict:
    result = await fir_svc.set_bypass(
        session, preset, channel, body.bypass, confirm=confirm, **_edit_kwargs(commit, if_match)
    )
    state = await fir_svc.fir_state(session, preset, channel, with_preview=False)
    return {**edit_json(session, result), "fir": state}


@router.get("/devices/{serial}/fir/blobs")
@translate_errors
async def list_fir_blobs(session: Session) -> list[dict]:
    """Stored FIR impulses (without taps)."""
    return await fir_svc.list_blobs(session)


@router.get("/devices/{serial}/fir/blobs/{blob_id}")
@translate_errors
@fir_errors
async def get_fir_blob(
    blob_id: str,
    session: Session,
    preview: Annotated[bool, Query(description="Include the impulse and response")] = True,
) -> dict:
    blob = await fir_svc.get_blob(session, blob_id)
    out: dict[str, Any] = {"blob": fir_svc.blob_json(blob)}
    if preview:
        out["preview"] = fir_svc.preview(blob.taps, session.profile.sample_rate)
    return out
