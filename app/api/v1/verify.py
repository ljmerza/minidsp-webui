"""Verify device (docs 03 §9, docs 05 §8): read the active preset back from DSP memory and diff it against the
applied shadow. Read-only on the device.

JSON (``router``, mounted under ``/api/v1``):

- ``POST /devices/{serial}/verify``: run a verify; ``?mark_unknown=true`` also drops the drifted paths from the shadow
  (they become "unknown"; nothing is written). ``?preset=p`` (0-based) refuses with 409 unless ``p`` is active.
- ``GET /devices/{serial}/verify``: the last report since the app started (404 before the first run).

HTML partial (``ui_router``, mounted at the root): ``/ui/devices/{serial}/verify`` (GET: last report and the button;
POST: run) and ``/ui/devices/{serial}/verify/mark-unknown`` (POST ``preset`` plus ``path`` fields).
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from app.api.deps import get_session, require_same_origin
from app.daemon.client import DaemonError, DaemonRejected, DaemonUnavailable, DeviceNotFound
from app.daemon.rawframes import RawFrameError
from app.sync import verify as svc
from app.sync.session import DeviceSession, PresetSwitchInProgress, StateUnavailable
from app.web.sync_ui import SYNC_CHANGED, describe_path, format_value
from app.web.ui_common import manager_of, render, url

router = APIRouter(tags=["verify"])
ui_router = APIRouter(include_in_schema=False, dependencies=[Depends(require_same_origin)])

Session = Annotated[DeviceSession, Depends(get_session)]


def _error(status: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "error": error})


def error_response(exc: Exception) -> tuple[int, str, str]:
    """``(HTTP status, error code, message)`` for anything `verify_device` raises."""
    message = str(exc) or type(exc).__name__
    match exc:
        case svc.VerifyUnsupported():
            return 501, "unsupported", message
        case PresetSwitchInProgress() | svc.VerifyError():
            return 409, "busy", message
        case StateUnavailable():
            return 503, "state_unavailable", message
        case RawFrameError():
            return 502, "readback_failed", message
        case DaemonUnavailable():
            return 503, "daemon_unavailable", message
        case DeviceNotFound():
            return 503, "device_absent", message
        case DaemonRejected() | DaemonError():
            return 502, "daemon_rejected", message
    raise exc


_EXPECTED = (svc.VerifyError, PresetSwitchInProgress, StateUnavailable, DaemonError)


@router.post("/devices/{serial}/verify")
async def run_verify(
    session: Session,
    mark_unknown: Annotated[bool, Query(description="Mark drifted paths unknown in the shadow")] = False,
    preset: Annotated[int | None, Query(description="Only verify if this (0-based) preset is active")] = None,
) -> Any:
    """Read back the active preset's readable settings and compare them with what the app last wrote."""
    try:
        if preset is not None and preset != await session.active_preset():
            return _error(409, "preset_not_active", f"preset {preset} is not active; only the active one can be read")
        report = await svc.verify_device(session, mark_unknown=mark_unknown, source="api")
    except _EXPECTED as exc:
        return _error(*error_response(exc))
    return report.as_json()


@router.get("/devices/{serial}/verify")
async def get_verify(session: Session) -> Any:
    """The last verify report of this device (kept in memory until the app restarts)."""
    report = svc.last_report(session.serial)
    if report is None:
        return _error(404, "not_verified", "this device has not been verified since the app started")
    return report.as_json()


# --- HTML partial -------------------------------------------------------------------------------------------------

_BADGE = {"ok": "synced", "drift": "failed", "unknown": "unknown", "unverifiable": "unknown"}
_ICON = {"ok": "✓", "drift": "⚠", "unknown": "?", "unverifiable": "–"}
_TEXT = {"ok": "matches", "drift": "drift", "unknown": "unknown", "unverifiable": "not readable"}
_ORDER = {"drift": 0, "unknown": 1, "ok": 2, "unverifiable": 3}


def _row(session: DeviceSession, item: svc.VerifyItem) -> dict[str, Any]:
    status = item.status.value
    note = item.note
    if item.status == svc.ItemStatus.UNKNOWN:
        relation = "matches" if item.matches_desired else "differs from"
        note = f"Never confirmed written; the device value {relation} the desired one." + (f" {note}" if note else "")
    return {
        "path": item.path,
        "label": describe_path(session, item.path),
        "status": status,
        "badge": _BADGE[status],
        "icon": _ICON[status],
        "text": _TEXT[status],
        "expected": format_value(session, item.path, item.expected),
        "actual": "—" if item.actual is None else format_value(session, item.path, item.actual),
        "note": note,
    }


def verify_context(
    request: Request,
    session: DeviceSession,
    *,
    error: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    report = svc.last_report(session.serial)
    rows = sorted((_row(session, i) for i in report.items), key=lambda r: _ORDER[r["status"]]) if report else []
    base = f"/ui/devices/{session.serial}/verify"
    return {
        "serial": session.serial,
        "report": report,
        "counts": report.counts if report else None,
        "rows": [r for r in rows if r["status"] != "unverifiable"],
        "unverifiable": [r for r in rows if r["status"] == "unverifiable"],
        "drift_paths": [i.path for i in report.drifted] if report else [],
        "mismatch_count": len(report.mismatched) if report else 0,
        "verify_url": url(request, base),
        "mark_url": url(request, f"{base}/mark-unknown"),
        "supported": svc.memory_map_for(session.profile.product_name) is not None,
        "error": error,
        "message": message,
    }


def _ui_session(request: Request, serial: int) -> DeviceSession | Response:
    session = manager_of(request).get(serial)
    if session is None:
        return PlainTextResponse(f"Unknown device {serial}", status_code=404)
    return session


@ui_router.get("/ui/devices/{serial}/verify", response_class=HTMLResponse)
async def verify_partial(request: Request, serial: int) -> Response:
    session = _ui_session(request, serial)
    if isinstance(session, Response):
        return session
    return render(request, "partials/verify.html", verify_context(request, session))


@ui_router.post("/ui/devices/{serial}/verify", response_class=HTMLResponse)
async def verify_run(request: Request, serial: int) -> Response:
    session = _ui_session(request, serial)
    if isinstance(session, Response):
        return session
    try:
        await svc.verify_device(session, source="ui")
    except _EXPECTED as exc:
        _status, _code, message = error_response(exc)
        return render(request, "partials/verify.html", verify_context(request, session, error=message))
    return render(request, "partials/verify.html", verify_context(request, session))


@ui_router.post("/ui/devices/{serial}/verify/mark-unknown", response_class=HTMLResponse)
async def verify_mark_unknown(request: Request, serial: int) -> Response:
    """Mark the drifted paths of the last report unknown, so the sync table offers to push them again."""
    session = _ui_session(request, serial)
    if isinstance(session, Response):
        return session
    form = await request.form()
    report = svc.last_report(serial)
    preset = form.get("preset")
    if report is None or not isinstance(preset, str) or preset != str(report.preset):
        ctx = verify_context(request, session, error="The report changed; verify again.")
        return render(request, "partials/verify.html", ctx)
    drifted = {i.path for i in report.drifted}
    paths = [p for p in form.getlist("path") if isinstance(p, str) and p in drifted]
    marked = svc.mark_drift_unknown(session, report.preset, paths)
    report.marked_unknown = sorted(set(report.marked_unknown) | set(marked))
    message = f"Marked {len(marked)} setting(s) unknown. Push them from the sync table." if marked else None
    response = render(request, "partials/verify.html", verify_context(request, session, message=message))
    if marked:
        response.headers["HX-Trigger"] = json.dumps({SYNC_CHANGED: {"preset": report.preset}})
    return response
