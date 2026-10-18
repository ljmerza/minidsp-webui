"""Profiles and preset history (docs 06): CRUD, save a preset as a profile, load a profile into a preset, diff,
JSON export/import, and revert a preset to a history revision.

- ``GET/POST /profiles``, ``GET/PATCH/DELETE /profiles/{id}``, ``POST /profiles/{id}/duplicate``
- ``POST /profiles/{id}/apply?preset=p`` loads it (default: the active preset); ``activate=true`` switches to the
  preset and applies it; ``preview=true`` only reports what would change.
- ``GET /profiles/diff?a=…&b=…``: sources are ``profile:ID``, ``preset:P`` (desired state) or ``revision:P:R``.
- ``GET /profiles/{id}/export`` and ``POST /profiles/import``: one profile as JSON.
- ``GET /profiles/{id}/export/cli-script`` and ``POST /profiles/import/cli-script``: one profile as a ``minidsp -f``
  script.
- ``GET …/presets/{p}/history/{rev}`` and ``POST …/presets/{p}/history/{rev}/revert``.

Presets are 0-based like the rest of the API. Loads and reverts are mute-wrapped bulk applies; a gain raise above the
big-step threshold or a FIR load answers 409 ``confirmation_required`` unless ``confirm=true``.
"""

import functools
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_manager, get_session, refresh_mqtt_after_write
from app.api.v1.adopt import MAX_SCRIPT_CHARS
from app.api.v1.presets import IfMatch, edit_json, parse_if_match, translate_errors
from app.model.device_profile import M2X4HD
from app.state import ProfileNameTaken, Repo
from app.state.repo import UNSET
from app.sync import profiles as svc
from app.sync.session import DeviceSession
from app.web.sync_ui import raise_rows

router = APIRouter(tags=["profiles"], dependencies=[Depends(refresh_mqtt_after_write)])

Session = Annotated[DeviceSession, Depends(get_session)]
Confirm = Annotated[bool, Query(description="Allow guarded changes (big gain raises, FIR loads)")]
MAX_IMPORT_BYTES = 1 << 20


class ProfileCreate(BaseModel):
    """Either ``config`` (a ConfigDoc) or ``serial`` + ``preset`` (save that preset's desired state)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=svc.MAX_NAME * 2)
    description: str | None = Field(default=None, max_length=svc.MAX_DESCRIPTION * 2)
    config: dict[str, Any] | None = None
    product_name: str | None = Field(default=None, description="Device model of ``config`` (default 2x4HD)")
    serial: int | None = None
    preset: int | None = None


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=svc.MAX_NAME * 2)
    description: str | None = Field(default=None, max_length=svc.MAX_DESCRIPTION * 2)
    config: dict[str, Any] | None = None


class ProfileScriptImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_SCRIPT_CHARS, description="A `minidsp -f` script")
    name: str = Field(max_length=svc.MAX_NAME * 2)
    description: str | None = Field(default=None, max_length=svc.MAX_DESCRIPTION * 2)
    product_name: str | None = Field(default=None, description="Device model the script is for (default 2x4HD)")


class ProfileDuplicate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=svc.MAX_NAME * 2)


def _error(status: int, error: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "error": error, **extra})


def profile_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R | JSONResponse]]:
    """Profile errors on top of `translate_errors` (session errors)."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | JSONResponse:
        try:
            return await fn(*args, **kwargs)
        except svc.ProfileNotFound as exc:
            return _error(404, "unknown_profile", str(exc))
        except svc.RevisionNotFound as exc:
            return _error(404, "unknown_revision", str(exc))
        except ProfileNameTaken as exc:
            return _error(409, "name_taken", str(exc))
        except svc.ProfileError as exc:
            return _error(422, "invalid", str(exc))
        except svc.ProfilesUnavailable as exc:
            return _error(503, "state_unavailable", str(exc))

    return translate_errors(wrapper)


def attachment(name: str, suffix: str) -> dict[str, str]:
    """``Content-Disposition`` for ``minidsp-profile-<name><suffix>``: an ASCII ``filename`` (header values are
    Latin-1) plus the full name as RFC 5987 ``filename*``."""

    def stem(keep: Callable[[str], bool]) -> str:
        return "".join(c if keep(c) or c in "-_" else "-" for c in name).strip("-") or "profile"

    ascii_name = f"minidsp-profile-{stem(lambda c: c.isascii() and c.isalnum())}{suffix}"
    full_name = f"minidsp-profile-{stem(str.isalnum)}{suffix}"
    return {
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(full_name, safe='')}"
    }


def _repo(request: Request) -> Repo | None:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        repo = getattr(get_manager(request), "repo", None)
    return repo


def _session(request: Request, serial: int | None) -> DeviceSession | None:
    manager = get_manager(request)
    return manager.get(serial) if serial is not None else manager.default()


def _device_session(request: Request, serial: int | None) -> DeviceSession:
    session = _session(request, serial)
    if session is None:
        raise HTTPException(404, f"unknown device {serial}" if serial is not None else "no device is connected")
    return session


@router.get("/profiles")
@profile_errors
async def list_profiles(
    request: Request,
    product_name: Annotated[str | None, Query(description="Only profiles for this device model")] = None,
) -> list[dict]:
    """Every profile (without its document) and the preset slots it was last loaded into."""
    return [v.to_json() for v in await svc.list_profiles(_repo(request), product_name=product_name)]


@router.post("/profiles", status_code=201)
@profile_errors
async def create_profile(request: Request, body: ProfileCreate) -> dict:
    """Create a profile from a document, or save a preset's current desired state (``serial`` + ``preset``)."""
    if body.preset is not None:
        if body.config is not None:
            raise svc.ProfileError("send either config or a preset, not both")
        session = _device_session(request, body.serial)
        profile = await svc.save_preset(session, body.preset, body.name, description=body.description)
    else:
        if body.config is None:
            raise svc.ProfileError("send a config document, or a preset to save")
        profile = await svc.create_profile(
            _repo(request),
            body.name,
            body.config,
            description=body.description,
            product_name=body.product_name or M2X4HD.product_name,
        )
    return (await svc.get_profile(_repo(request), profile.id)).to_json(include_config=True)


@router.get("/profiles/diff")
@profile_errors
async def diff_profiles(
    request: Request,
    a: Annotated[str, Query(description="profile:ID, preset:P or revision:P:R")],
    b: Annotated[str, Query(description="profile:ID, preset:P or revision:P:R")],
    serial: Annotated[int | None, Query(description="Device for preset/revision sources (default: the first)")] = None,
) -> dict:
    """Field differences from ``a`` to ``b``. A PEQ band or crossover group is one row with both objects."""
    repo = _repo(request)
    session = _session(request, serial)
    if serial is not None and session is None:
        raise svc.SourceError(f"unknown device {serial}")
    side_a = await svc.resolve_source(a, repo=repo, session=session)
    side_b = await svc.resolve_source(b, repo=repo, session=session)
    rows = svc.diff_docs(side_a.doc, side_b.doc)
    return {
        "a": {"source": side_a.source, "label": side_a.label},
        "b": {"source": side_b.source, "label": side_b.label},
        "count": len(rows),
        "rows": [r.to_json() for r in rows],
    }


@router.post("/profiles/import", status_code=201)
@profile_errors
async def import_profile(
    request: Request,
    name: Annotated[str | None, Query(description="Name for the profile (default: the one in the file)")] = None,
) -> dict:
    """Create a profile from a ``GET /profiles/{id}/export`` file (a taken name gets a "(copy)" suffix)."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_IMPORT_BYTES:
            return _error(413, "too_large", f"a profile export is at most {MAX_IMPORT_BYTES} bytes")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise svc.ProfileError("the file is not JSON") from None
    profile, warnings = await svc.import_profile(_repo(request), data, name=name)
    view = await svc.get_profile(_repo(request), profile.id)
    return {**view.to_json(include_config=True), "warnings": warnings}


@router.post("/profiles/import/cli-script", status_code=201)
@profile_errors
async def import_profile_script(request: Request, body: ProfileScriptImport) -> dict:
    """Create a profile from a ``minidsp -f`` script applied to the defaults (a taken name gets a "(copy)" suffix).
    Scripts with errors are refused; ``warnings`` lists the ignored lines."""
    profile, warnings = await svc.import_profile_script(
        _repo(request), body.text, body.name, description=body.description, product_name=body.product_name
    )
    view = await svc.get_profile(_repo(request), profile.id)
    return {**view.to_json(include_config=True), "warnings": warnings}


@router.get("/profiles/{profile_id}")
@profile_errors
async def get_profile(request: Request, profile_id: int) -> dict:
    view = await svc.get_profile(_repo(request), profile_id)
    data = view.to_json(include_config=True)
    session = _session(request, None)
    data["matches"] = svc.matching_presets(session, svc.profile_doc(view.profile)) if session else []
    return data


@router.patch("/profiles/{profile_id}")
@profile_errors
async def update_profile(request: Request, profile_id: int, body: ProfileUpdate) -> dict:
    """Rename, change the description, or replace the document."""
    fields: dict[str, Any] = {}
    for key in body.model_fields_set:
        fields[key] = getattr(body, key)
    if "name" in fields and fields["name"] is None:
        raise svc.ProfileError("a profile needs a name")
    if "config" in fields and fields["config"] is None:
        raise svc.ProfileError("config can't be null")
    await svc.update_profile(
        _repo(request),
        profile_id,
        name=fields.get("name", UNSET),
        description=fields.get("description", UNSET),
        config=fields.get("config", UNSET),
    )
    return (await svc.get_profile(_repo(request), profile_id)).to_json(include_config=True)


@router.delete("/profiles/{profile_id}", status_code=204)
@profile_errors
async def delete_profile(request: Request, profile_id: int) -> Response:
    await svc.delete_profile(_repo(request), profile_id)
    return Response(status_code=204)


@router.post("/profiles/{profile_id}/duplicate", status_code=201)
@profile_errors
async def duplicate_profile(request: Request, profile_id: int, body: ProfileDuplicate | None = None) -> dict:
    profile = await svc.duplicate_profile(_repo(request), profile_id, name=body.name if body else None)
    return (await svc.get_profile(_repo(request), profile.id)).to_json(include_config=True)


@router.get("/profiles/{profile_id}/export")
@profile_errors
async def export_profile(request: Request, profile_id: int) -> JSONResponse:
    view = await svc.get_profile(_repo(request), profile_id)
    return JSONResponse(svc.export_profile(view.profile), headers=attachment(view.profile.name, ".json"))


@router.get("/profiles/{profile_id}/export/cli-script", response_class=PlainTextResponse)
@profile_errors
async def export_profile_script(
    request: Request,
    profile_id: int,
    mute_wrap: Annotated[bool, Query(description="Mute every output first, restore the mutes at the end")] = True,
) -> PlainTextResponse:
    """The profile as a script for ``minidsp -f`` (full state; it writes to the device's active preset)."""
    view = await svc.get_profile(_repo(request), profile_id)
    text = svc.export_profile_script(view.profile, mute_wrap=mute_wrap)
    return PlainTextResponse(text, headers=attachment(view.profile.name, ".txt"))


@router.post("/profiles/{profile_id}/apply")
@profile_errors
async def apply_profile(
    request: Request,
    profile_id: int,
    preset: Annotated[int | None, Query(description="Preset slot (0-based; default: the active preset)")] = None,
    serial: Annotated[int | None, Query(description="Device (default: the first)")] = None,
    activate: Annotated[bool, Query(description="Switch the device to the preset and apply it")] = False,
    preview: Annotated[bool, Query(description="Only report what would change")] = False,
    confirm: Confirm = False,
    if_match: IfMatch = None,
) -> dict:
    """Load the profile into a preset. An inactive preset is staged (written when it becomes active) unless
    ``activate``; the response carries the new revision and the flush result."""
    session = _device_session(request, serial)
    if preview:
        info = await svc.load_preview(session, profile_id, preset)
        return {"serial": session.serial, **info, "raises": raise_rows(session, info["raises"])}
    result = await svc.load_profile(
        session,
        profile_id,
        preset,
        activate=activate,
        confirm=confirm,
        expected_revision=parse_if_match(if_match),
    )
    return {
        **edit_json(session, result.edit),
        "profile": result.profile.id,
        "activated": result.activated,
        "master": result.master.model_dump() if result.master else None,
    }


@router.get("/devices/{serial}/presets/{preset}/history/{revision}")
@profile_errors
async def get_revision(preset: int, revision: int, session: Session) -> dict:
    """One history revision with its document."""
    row = await svc.revision(session, preset, revision)
    return {
        "serial": session.serial,
        "preset": preset,
        "revision": row.revision,
        "summary": row.summary,
        "source": row.source,
        "created_at": row.created_at,
        "desired": row.desired,
    }


@router.post("/devices/{serial}/presets/{preset}/history/{revision}/revert")
@profile_errors
async def revert_revision(
    preset: int,
    revision: int,
    session: Session,
    preview: Annotated[bool, Query(description="Only report what would change")] = False,
    confirm: Confirm = False,
    if_match: IfMatch = None,
) -> dict:
    """Make the revision's document the preset's desired state again (recorded as a new revision)."""
    if preview:
        return {"serial": session.serial, **await svc.revert_preview(session, preset, revision)}
    result = await svc.revert_to(session, preset, revision, confirm=confirm, expected_revision=parse_if_match(if_match))
    return {**edit_json(session, result), "reverted_to": revision}
