"""Per-preset desired state: read, merge-patch, commit, push-all, activate and history (docs 06, Phase 2 subset).

Edits are JSON merge patches on the preset's ConfigDoc; lists are patched by index with an object
(``{"outputs": {"0": {"gain_db": 9.5}}}``). The response carries the new revision and the sync status of the changed
device paths; the full state is ``GET …/presets/{p}``.

``If-Match: <revision>`` makes an edit conditional (412 on a stale revision). ``?confirm=true`` allows a gain raise
above the big-step threshold (409 ``confirmation_required`` otherwise). Edits are recorded in the history at commit
boundaries only (SD-card budget): ``?commit=true`` (form submit, slider release), ``POST …/commit``, or 5 s without
edits. Use ``commit=true`` together with ``If-Match``, otherwise your own uncommitted edit makes the next one stale.
"""

import functools
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from app.api.deps import get_session
from app.state import RevisionConflict
from app.sync.session import (
    BULK_TIMEOUT_S,
    ConfirmationRequired,
    DesiredValidationError,
    DeviceSession,
    EditResult,
    StateUnavailable,
    UnknownPreset,
    flush_json,
)

router = APIRouter(tags=["presets"])

Session = Annotated[DeviceSession, Depends(get_session)]
Confirm = Annotated[bool, Query(description="Allow guarded changes (a gain raise above the big-step threshold)")]
Commit = Annotated[bool, Query(description="Write a history revision now (else after 5 s without edits)")]
IfMatch = Annotated[str | None, Header(description="Expected revision; the edit fails with 412 if it is stale")]


def _error(status: int, error: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "error": error, **extra})


def translate_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R | JSONResponse]]:
    """Map session errors to HTTP responses (kept local so the routers also work on a bare FastAPI app)."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | JSONResponse:
        try:
            return await fn(*args, **kwargs)
        except ConfirmationRequired as exc:
            return _error(409, "confirmation_required", str(exc))
        except UnknownPreset as exc:
            return _error(404, "unknown_preset", str(exc))
        except DesiredValidationError as exc:
            return _error(422, "invalid", str(exc), problems=exc.problems)
        except RevisionConflict as exc:
            return _error(412, "revision_conflict", str(exc), expected=exc.expected, revision=exc.actual)
        except StateUnavailable as exc:
            return _error(503, "state_unavailable", str(exc))

    return wrapper


def parse_if_match(value: str | None) -> int | None:
    """``3``, ``"3"`` or ``W/"3"`` -> 3; absent or ``*`` -> None."""
    if value is None or value.strip() in ("", "*"):
        return None
    text = value.strip().removeprefix("W/").strip('"')
    if not text.isdigit():
        raise HTTPException(status_code=400, detail=f"If-Match must be a revision number, got {value!r}")
    return int(text)


def edit_json(session: DeviceSession, result: EditResult) -> dict[str, Any]:
    status = session.sync_status(result.preset)
    return {
        "serial": session.serial,
        "preset": result.preset,
        "revision": result.revision,
        "changed": result.paths,
        "device_paths": result.device_paths,
        "sync": {p: status[p] for p in result.device_paths if p in status},
        "summary": session.sync_summary(result.preset),
        "flush": flush_json(result.flush),
    }


def _etag(response: Response, revision: int) -> None:
    response.headers["ETag"] = f'"{revision}"'


@router.get("/devices/{serial}/presets")
@translate_errors
async def list_presets(session: Session) -> list[dict]:
    """Sync summary and revision of every preset."""
    await session.ensure_loaded()
    return [session.sync_summary(p) for p in range(session.profile.n_presets)]


@router.get("/devices/{serial}/presets/{preset}")
@translate_errors
async def get_preset(
    preset: int,
    session: Session,
    response: Response,
    sync: Annotated[bool, Query(description="Include the per-path sync status")] = True,
) -> dict:
    """Desired ConfigDoc, revision, sync summary and (by default) the sync status of every device path."""
    await session.get_desired(preset)
    view = session.preset_view(preset, include_sync=sync)
    _etag(response, view["revision"])
    return view


@router.patch("/devices/{serial}/presets/{preset}")
@translate_errors
async def patch_preset(
    preset: int,
    session: Session,
    patch: Annotated[dict[str, Any], Body(description="JSON merge patch on the preset's ConfigDoc")],
    confirm: Confirm = False,
    commit: Commit = False,
    if_match: IfMatch = None,
) -> dict:
    result = await session.edit(
        preset, patch, confirm=confirm, commit=commit, expected_revision=parse_if_match(if_match), source="api"
    )
    return edit_json(session, result)


async def _patch_channel(
    session: DeviceSession, preset: int, kind: str, index: int, patch: dict, confirm: bool, commit: bool, if_match
) -> dict:
    result = await session.edit_channel(
        preset,
        kind,
        index,
        patch,
        confirm=confirm,
        commit=commit,
        expected_revision=parse_if_match(if_match),
        source="api",
    )
    return edit_json(session, result)


@router.patch("/devices/{serial}/presets/{preset}/outputs/{index}")
@translate_errors
async def patch_output(
    preset: int,
    index: int,
    session: Session,
    patch: Annotated[dict[str, Any], Body(description="JSON merge patch on one output channel")],
    confirm: Confirm = False,
    commit: Commit = False,
    if_match: IfMatch = None,
) -> dict:
    return await _patch_channel(session, preset, "output", index, patch, confirm, commit, if_match)


@router.patch("/devices/{serial}/presets/{preset}/inputs/{index}")
@translate_errors
async def patch_input(
    preset: int,
    index: int,
    session: Session,
    patch: Annotated[dict[str, Any], Body(description="JSON merge patch on one input channel")],
    confirm: Confirm = False,
    commit: Commit = False,
    if_match: IfMatch = None,
) -> dict:
    return await _patch_channel(session, preset, "input", index, patch, confirm, commit, if_match)


@router.post("/devices/{serial}/presets/{preset}/commit")
@translate_errors
async def commit_preset(preset: int, session: Session) -> dict:
    """Commit boundary (slider release, form submit): record pending edits as a history revision."""
    revision = await session.commit(preset)
    return {"serial": session.serial, "preset": preset, "revision": revision}


@router.post("/devices/{serial}/presets/{preset}/push")
@translate_errors
async def push_preset(
    preset: int,
    session: Session,
    mute_wrap: Annotated[bool | None, Query(description="Hold the outputs muted (default: settings)")] = None,
) -> dict:
    """Write every setting of the preset, whatever the shadow says. Waits until it is done, staged (inactive
    preset: pushed when it becomes active) or has given up."""
    result = await session.push_all(preset, mute_wrap=mute_wrap, timeout=BULK_TIMEOUT_S)
    return {"serial": session.serial, "preset": preset, "flush": flush_json(result), **_summary(session, preset)}


@router.post("/devices/{serial}/presets/{preset}/activate")
@translate_errors
async def activate_preset(
    preset: int,
    session: Session,
    flush: Annotated[bool, Query(description="Wait for the preset's staged edits to be written")] = True,
) -> dict:
    """Switch to the preset (slow: the device resets for a few seconds) and apply its staged edits."""
    master = await session.activate_preset(preset, flush=flush)
    return {"serial": session.serial, "preset": preset, "master": master.model_dump(), **_summary(session, preset)}


def _summary(session: DeviceSession, preset: int) -> dict[str, Any]:
    return {"summary": session.sync_summary(preset)}


@router.get("/devices/{serial}/presets/{preset}/history")
@translate_errors
async def preset_history(
    preset: int,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[int | None, Query(description="Page back from this revision (exclusive)")] = None,
) -> dict:
    """Revisions, newest first (without their documents)."""
    rows = await session.history(preset, limit=limit, before=before)
    return {
        "serial": session.serial,
        "preset": preset,
        "revision": session.revisions.get(preset, 0),
        "revisions": [
            {
                "revision": r.revision,
                "summary": r.summary,
                "source": r.source,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }
