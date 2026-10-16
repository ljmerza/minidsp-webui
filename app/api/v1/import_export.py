"""``minidsp -f`` command scripts: preview an import, apply one to a preset, export a preset as one."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.deps import get_manager, get_session
from app.api.v1.adopt import MAX_SCRIPT_CHARS
from app.api.v1.presets import IfMatch, edit_json, parse_if_match, translate_errors
from app.formats.cli_script import ParsedScript, ScriptIssue, parse_script
from app.model.config_doc import ConfigDoc, PathError, default_config_doc, validate_doc
from app.model.device_profile import M2X4HD
from app.sync.session import BULK_TIMEOUT_S, DeviceSession, SessionManager

router = APIRouter(tags=["import/export"])

Session = Annotated[DeviceSession, Depends(get_session)]


class ScriptPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_SCRIPT_CHARS)
    serial: int | None = Field(default=None, description="Use this device's profile (and preset, with base=current)")
    preset: int | None = None
    base: Literal["default", "current"] = Field(
        default="default", description="Apply the script to the defaults or to the preset's current desired state"
    )


class ScriptImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_SCRIPT_CHARS)
    base: Literal["default", "current"] = "current"
    confirm: bool = Field(default=False, description="Allow gain raises above the big-step threshold")


def _issue(issue: ScriptIssue) -> dict[str, Any]:
    return {"line": issue.line, "text": issue.text, "message": issue.message}


def _value(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def parsed_json(parsed: ParsedScript) -> dict[str, Any]:
    return {
        "ok": parsed.ok,
        "ops": [{"line": op.line, "path": op.path, "value": _value(op.value)} for op in parsed.ops],
        "warnings": [_issue(w) for w in parsed.warnings],
        "errors": [_issue(e) for e in parsed.errors],
        "device_paths": parsed.device_paths,
    }


@router.post("/import/cli-script")
@translate_errors
async def preview_cli_script(body: ScriptPreview, request: Request) -> dict:
    """Parse a script without changing anything: the operations, warnings (ignored lines), errors, and the document
    it produces with any range/stability problems."""
    profile = M2X4HD
    base: ConfigDoc | None = None
    if body.serial is not None:
        manager: SessionManager = get_manager(request)
        session = manager.get(body.serial)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown device {body.serial}")
        profile = session.profile
        if body.base == "current":
            if body.preset is None:
                raise HTTPException(status_code=422, detail="base=current needs a preset")
            base = await session.get_desired(body.preset)
    elif body.base == "current":
        raise HTTPException(status_code=422, detail="base=current needs a serial and a preset")
    parsed = parse_script(body.text, profile)
    result = parsed_json(parsed)
    try:
        doc = parsed.apply(base or default_config_doc(profile))
    except (PathError, ValidationError) as exc:
        return {**result, "doc": None, "problems": [f"the script can't be applied: {exc}"]}
    return {**result, "doc": doc.model_dump(mode="json"), "problems": validate_doc(doc, profile)}


@router.post("/devices/{serial}/presets/{preset}/import/cli-script")
@translate_errors
async def import_cli_script(
    preset: int,
    body: ScriptImport,
    session: Session,
    commit: Annotated[bool, Query()] = True,
    if_match: IfMatch = None,
) -> dict:
    """Apply a script to the preset's desired state as a (mute-wrapped) bulk apply. Scripts with errors are
    refused (422 with every error); the response includes the warnings for ignored lines."""
    result = await session.import_cli_script(
        preset,
        body.text,
        base=body.base,
        confirm=body.confirm,
        expected_revision=parse_if_match(if_match),
        commit=commit,
        timeout=BULK_TIMEOUT_S,
    )
    return {**edit_json(session, result.edit), "warnings": [_issue(w) for w in result.parsed.warnings]}


@router.get("/devices/{serial}/presets/{preset}/export/cli-script", response_class=PlainTextResponse)
@translate_errors
async def export_cli_script(
    preset: int,
    session: Session,
    mute_wrap: Annotated[bool, Query(description="Mute every output first, restore the mutes at the end")] = True,
    download: Annotated[bool, Query(description="Send as a file attachment")] = False,
) -> PlainTextResponse:
    """The preset's desired state as a script for ``minidsp -f`` (full state, writes to the active preset)."""
    text = await session.export_cli_script(preset, mute_wrap=mute_wrap)
    headers = {"Content-Disposition": f'attachment; filename="minidsp-preset-{preset + 1}.txt"'} if download else {}
    return PlainTextResponse(text, headers=headers)
