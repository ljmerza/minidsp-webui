"""PEQ bands and crossover groups of a preset: set from design parameters or raw biquads (docs 06).

``PUT …/outputs/{n}/peq/{k}`` (and ``…/inputs/{n}/peq/{k}``) takes ``{design}`` or ``{raw}``, optionally with
``bypass``; ``PUT …/outputs/{n}/crossover/{g}`` takes ``{design}`` or ``{biquads}`` (up to the group size, padded
with pass-through; ``[]`` flattens the group), optionally with ``bypass``. A body with only ``bypass`` toggles the
band or group. An omitted ``bypass`` keeps the current flag.

The server compiles designs, refuses unstable biquads and designs that don't fit (422 ``invalid`` with
``problems``), recognizes raw coefficients as editable designs, and queues the device writes. Re-sending the same
design changes nothing and writes nothing. Query parameters and ``If-Match`` work as for ``PATCH …/presets/{p}``.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_session
from app.api.v1.presets import Commit, Confirm, IfMatch, edit_json, parse_if_match, translate_errors
from app.model.filters import Biquad, CrossoverDesign, FilterDesign
from app.sync import filters as svc
from app.sync.session import DeviceSession

router = APIRouter(tags=["filters"])

Session = Annotated[DeviceSession, Depends(get_session)]
Side = Literal["inputs", "outputs"]
BiquadIn = Biquad | Annotated[list[float], Field(min_length=5, max_length=5)]
# Generous upper bound for the request; the profile's group size is checked by the service with a clear message.
MAX_GROUP_BIQUADS = 16


class PeqBandPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    design: FilterDesign | None = Field(default=None, description="Design parameters (type, freq, q, gain_db, …)")
    raw: BiquadIn | None = Field(default=None, description="Raw coefficients {b0,b1,b2,a1,a2} or [b0,b1,b2,a1,a2]")
    bypass: bool | None = Field(default=None, description="Omit to keep the current flag")


class CrossoverGroupPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    design: CrossoverDesign | None = Field(default=None, description="{kind, family, order, freq, freq_hi?}")
    biquads: list[BiquadIn] | None = Field(
        default=None, max_length=MAX_GROUP_BIQUADS, description="Raw biquads in device convention; [] flattens"
    )
    bypass: bool | None = Field(default=None, description="Omit to keep the current flag")


def _edit_kwargs(confirm: bool, commit: bool, if_match: str | None) -> dict[str, Any]:
    return {"confirm": confirm, "commit": commit, "expected_revision": parse_if_match(if_match), "source": "api"}


def _with_sync(session: DeviceSession, preset: int, path: str, view: dict[str, Any]) -> dict[str, Any]:
    status = session.sync_status(preset)
    prefix = f"{path}."
    return {
        "serial": session.serial,
        "preset": preset,
        "path": path,
        **view,
        "sync_status": {p: s for p, s in status.items() if p.startswith(prefix)},
    }


@router.get("/devices/{serial}/presets/{preset}/{side}/{channel}/peq/{band}")
@translate_errors
async def get_peq_band(preset: int, side: Side, channel: int, band: int, session: Session) -> dict:
    """One band's design, compiled coefficients, label and per-path sync status."""
    current = await svc.get_band(session, preset, side, channel, band)
    path = svc.check_band_index(session.profile, side, channel, band)
    return _with_sync(session, preset, path, svc.band_view(current))


@router.put("/devices/{serial}/presets/{preset}/{side}/{channel}/peq/{band}")
@translate_errors
async def put_peq_band(
    preset: int,
    side: Side,
    channel: int,
    band: int,
    body: PeqBandPut,
    session: Session,
    confirm: Confirm = False,
    commit: Commit = False,
    if_match: IfMatch = None,
) -> dict:
    result = await svc.set_peq_band(
        session,
        preset,
        side,
        channel,
        band,
        design=body.design,
        raw=body.raw,
        bypass=body.bypass,
        **_edit_kwargs(confirm, commit, if_match),
    )
    stored = getattr(result.doc, side)[channel].peq[band]
    return {**edit_json(session, result), "band": svc.band_view(stored)}


@router.get("/devices/{serial}/presets/{preset}/outputs/{channel}/crossover/{group}")
@translate_errors
async def get_crossover_group(preset: int, channel: int, group: int, session: Session) -> dict:
    """One group's design, compiled biquads, label, slots in use and per-path sync status."""
    current = await svc.get_group(session, preset, channel, group)
    path = svc.check_group_index(session.profile, channel, group)
    return _with_sync(session, preset, path, svc.group_view(current))


@router.put("/devices/{serial}/presets/{preset}/outputs/{channel}/crossover/{group}")
@translate_errors
async def put_crossover_group(
    preset: int,
    channel: int,
    group: int,
    body: CrossoverGroupPut,
    session: Session,
    confirm: Confirm = False,
    commit: Commit = False,
    if_match: IfMatch = None,
) -> dict:
    result = await svc.set_crossover_group(
        session,
        preset,
        channel,
        group,
        design=body.design,
        biquads=body.biquads,
        bypass=body.bypass,
        **_edit_kwargs(confirm, commit, if_match),
    )
    stored = result.doc.outputs[channel].crossover[group]
    return {**edit_json(session, result), "group": svc.group_view(stored)}
