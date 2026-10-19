"""``GET /api/v1/devices/{serial}/presets/{preset}/summary``: the plain-language summary and health of a preset.

Read-only. The same `PresetSummary` the pages render (app/sync/summary.py): one sentence, total level, headroom
line, badge and setting blocks per output, one sentence per input, and the merged health list (problem > caution >
info) with each item's fix actions. ``?live=true`` adds the rules that read the meters (the device's latest levels);
they are off by default because the meter thresholds are unverified (phase 9 design, section 19).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_session
from app.api.v1.presets import translate_errors
from app.sync.session import DeviceSession, StateUnavailable
from app.sync.summary import live_levels, session_summary

router = APIRouter(tags=["presets"])

Session = Annotated[DeviceSession, Depends(get_session)]


@router.get("/devices/{serial}/presets/{preset}/summary")
@translate_errors
async def preset_summary(
    preset: int,
    session: Session,
    live: Annotated[bool, Query(description="Also run the live-meter rules (latest levels)")] = False,
) -> dict[str, Any]:
    """Plain-language sentences, totals, headroom, badges and health items of one preset."""
    await session.get_desired(preset)  # loads the state; 404 for an unknown preset, 503 while unavailable
    summary = session_summary(session, preset, levels=live_levels(session) if live else None)
    if summary is None:
        raise StateUnavailable("the preset state is not available")
    return {"serial": session.serial, **summary.as_json()}
