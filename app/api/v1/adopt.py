"""Adopt wizard backend (docs 03 §8): tell the app what the device currently holds.

- ``trust``: "this is what's on the device". The shadow is set from the document; nothing is written. Trusting the
  defaults (no script or doc) needs ``confirm_defaults``: the device must really be at factory defaults.
- ``push``: write the whole document (mute-wrapped). Destructive: needs ``confirm``.
- ``unknown``: start from the document with nothing known; each setting is written the first time it is touched.

The document is a ``minidsp -f`` command script applied to the defaults, an explicit ConfigDoc, or the defaults.
Preview a script first with ``POST /import/cli-script``.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_session
from app.api.v1.presets import translate_errors
from app.sync.session import BULK_TIMEOUT_S, AdoptMode, DeviceSession, flush_json, parse_doc

router = APIRouter(tags=["adopt"])

Session = Annotated[DeviceSession, Depends(get_session)]

MAX_SCRIPT_CHARS = 200_000


class AdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AdoptMode
    preset: int | None = Field(default=None, description="Preset to adopt (default: the active one)")
    script: str | None = Field(default=None, max_length=MAX_SCRIPT_CHARS, description="A `minidsp -f` script")
    doc: dict[str, Any] | None = Field(default=None, description="A full ConfigDoc (instead of a script)")
    confirm: bool = Field(default=False, description="Required for `push`, which overwrites the device")
    confirm_defaults: bool = Field(
        default=False,
        description="Required for `trust` with no script or doc: the device really is at factory defaults",
    )
    trust_all: bool = Field(
        default=False,
        description="With a script and `trust`: also trust the defaults for settings the script doesn't mention",
    )


@router.get("/devices/{serial}/adopt")
@translate_errors
async def adopt_status(session: Session) -> dict:
    """Whether the device has been adopted, and how."""
    await session.ensure_loaded()
    return {
        "serial": session.serial,
        "adopted": not session.needs_adoption,
        "adoption": session.adoption,
        "modes": [m.value for m in AdoptMode],
    }


@router.post("/devices/{serial}/adopt")
@translate_errors
async def adopt(request: AdoptRequest, session: Session) -> dict:
    doc = parse_doc(request.doc) if request.doc is not None else None
    result = await session.adopt(
        request.mode,
        preset=request.preset,
        script=request.script,
        doc=doc,
        confirm=request.confirm,
        confirm_defaults=request.confirm_defaults,
        trust_all=request.trust_all,
        timeout=BULK_TIMEOUT_S,
    )
    return {
        "serial": session.serial,
        "preset": result.preset,
        "mode": result.mode.value,
        "revision": result.revision,
        "known": result.known,
        "unknown": result.unknown,
        "writes": result.writes,
        "flush": flush_json(result.flush),
        "warnings": [{"line": w.line, "text": w.text, "message": w.message} for w in result.warnings],
        "summary": session.sync_summary(result.preset),
    }
