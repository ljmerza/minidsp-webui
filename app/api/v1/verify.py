"""Verify device (docs 03 §9, docs 05 §8): read the active preset back from DSP memory and diff it against the
applied shadow. Read-only on the device.

JSON (``router``, mounted under ``/api/v1``):

- ``POST /devices/{serial}/verify``: run a verify; ``?mark_unknown=true`` also drops the drifted paths from the shadow
  (they become "unknown"; nothing is written). ``?preset=p`` (0-based) refuses with 409 unless ``p`` is active.
- ``GET /devices/{serial}/verify``: the last report since the app started (404 before the first run).
- ``POST /devices/{serial}/verify/accept``: accept the device values of the last report's drifted and never-confirmed
  items (``{"preset": p, "paths": [...]}``, all of them without ``paths``): they become the desired values and are
  confirmed. Nothing is written to the device; a revision (source ``verify``) is committed. All-zero filters are
  never accepted (``refused``). The reply's ``undo.token`` undoes it for ``undo.seconds``.
- ``POST /devices/{serial}/verify/accept/undo``: put back what the last accept replaced (``{"preset": p, "token":
  "..."}``): the app's earlier values and its record of what the device holds. Writes nothing and queues no write;
  settings changed since the accept are left alone (``skipped``).

HTML partial (``ui_router``, mounted at the root): ``/ui/devices/{serial}/verify`` (GET: last report and the button;
POST: run), ``/ui/devices/{serial}/verify/mark-unknown`` ("Forget these values"),
``/ui/devices/{serial}/verify/accept/confirm`` (GET: the "Use the device's values" confirm dialog),
``/ui/devices/{serial}/verify/accept`` (POST ``preset``, ``checked_at`` and ``path`` fields) and
``/ui/devices/{serial}/verify/accept/undo`` (POST ``preset`` and ``token``).
"""

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import get_session, require_same_origin
from app.daemon.client import DaemonError, DaemonRejected, DaemonUnavailable, DeviceNotFound
from app.daemon.rawframes import RawFrameError
from app.dsp.recognize import describe_crossover, describe_filter
from app.model.config_doc import ConfigDoc, flatten, is_device_path
from app.sync import verify as svc
from app.sync.session import (
    ACCEPT_UNDO_S,
    DesiredValidationError,
    DeviceSession,
    PresetSwitchInProgress,
    StateUnavailable,
    UndoResult,
    UndoUnavailable,
    undo_unit,
)
from app.web.sync_ui import SYNC_CHANGED, describe_path, format_value
from app.web.ui_common import channel_label, manager_of, render, url

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
        case svc.ReadbackUnavailable():
            return 503, "unreadable", message
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


class AcceptRequest(BaseModel):
    preset: int = Field(description="The (0-based) preset of the last report; refused with 409 if it changed")
    paths: list[str] | None = Field(
        default=None, description="Paths to accept; default every drifted or never-confirmed path with a usable reading"
    )


@router.post("/devices/{serial}/verify/accept")
async def accept_values(request: AcceptRequest, session: Session) -> Any:
    """Make the device values of the last report the desired values, confirmed. Writes nothing to the device."""
    report = svc.last_report(session.serial)
    if report is None:
        return _error(404, "not_verified", "this device has not been verified since the app started")
    if request.preset != report.preset:
        return _error(409, "report_changed", f"the last report is for preset {report.preset}; verify again")
    try:
        accepted = await svc.accept_device_values(session, report, request.paths)
    except DesiredValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "error": "invalid", "problems": exc.problems})
    except StateUnavailable as exc:
        return _error(503, "state_unavailable", str(exc))
    chosen = None if request.paths is None else set(request.paths)
    snap = session.accept_undo
    return {
        "serial": session.serial,
        "preset": report.preset,
        "accepted": accepted,
        "refused": [i.path for i in report.refused if chosen is None or i.path in chosen],
        "revision": session.revisions.get(report.preset, 0),
        "undo": {"token": snap.token, "seconds": ACCEPT_UNDO_S} if accepted and snap is not None else None,
        "report": report.as_json(),
    }


class UndoRequest(BaseModel):
    preset: int = Field(description="The (0-based) preset the accept changed")
    token: str | None = Field(default=None, description="The accept's ``undo.token``; default the last accept")


def _undo_json(session: DeviceSession, result: UndoResult) -> dict[str, Any]:
    return {
        "serial": session.serial,
        "preset": result.preset,
        "restored": result.restored,
        "skipped": result.skipped,
        "unknown": result.unknown,
        "revision": result.revision,
    }


@router.post("/devices/{serial}/verify/accept/undo")
async def undo_accept(request: UndoRequest, session: Session) -> Any:
    """Undo the last accept of device values (within 30 seconds). Writes nothing to the device and queues no write;
    settings changed since the accept are left alone and listed in ``skipped``."""
    try:
        result = await svc.undo_accept(session, request.preset, request.token)
    except UndoUnavailable as exc:
        return _error(410 if exc.code == "expired" else 409, f"undo_{exc.code}", str(exc))
    except DesiredValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "error": "invalid", "problems": exc.problems})
    except StateUnavailable as exc:
        return _error(503, "state_unavailable", str(exc))
    return _undo_json(session, result)


# --- HTML partial -------------------------------------------------------------------------------------------------

_BADGE = {"ok": "synced", "drift": "failed", "unknown": "unknown", "unverifiable": "unknown"}
_ICON = {"ok": "✓", "drift": "⚠", "unknown": "?", "unverifiable": "–"}
# On-screen words (phase 9 design §2); the API keeps the status keys.
_TEXT = {"ok": "matches", "drift": "changed on device", "unknown": "unknown", "unverifiable": "can't check"}
_ORDER = {"drift": 0, "unknown": 1, "ok": 2, "unverifiable": 3}
TOAST = "mdsp-toast"  # HX-Trigger event: app.js shows a toast, optionally with an action button (Undo)


def _row(session: DeviceSession, item: svc.VerifyItem) -> dict[str, Any]:
    status = item.status.value
    note = item.note
    if item.status == svc.ItemStatus.UNKNOWN:
        relation = "matches" if item.matches_desired else "differs from"
        note = f"Not sent by the app yet; the device value {relation} the app's value." + (f" {note}" if note else "")
    if svc.zero_filter(item):
        note = "All zeros (looks like a read error), so it is never used." + (f" {note}" if note else "")
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


def _settings(n: int) -> str:
    return f"{n} setting" if n == 1 else f"{n} settings"


def _count_units(paths: list[str]) -> int:
    return len(dict.fromkeys(undo_unit(p) for p in paths))


def _channel(session: DeviceSession, unit: str) -> tuple[tuple[str, int], str]:
    side, index = unit.split(".")[:2]
    kind = "input" if side == "inputs" else "output"
    return (kind, int(index)), channel_label(session, kind, int(index))


def _filter_unit(doc: ConfigDoc, unit: str) -> Any:
    """The PEQ band or crossover group a filter unit (`undo_unit`) names, or None for a plain setting."""
    parts = unit.split(".")
    if len(parts) != 4 or parts[2] not in ("peq", "crossover"):
        return None
    channel = (doc.inputs if parts[0] == "inputs" else doc.outputs)[int(parts[1])]
    return getattr(channel, parts[2])[int(parts[3])]


_PLAIN_FILTER = {"Raw": "custom filter", "Custom": "custom filter", "Pass-through": "flat"}


def _unit_value(session: DeviceSession, doc: ConfigDoc, flat: dict[str, Any], unit: str) -> str:
    """A unit's value in words: a filter's design ("Peaking 40 Hz Q 4 −4.0 dB", "LR4 LP 80 Hz", "custom filter"),
    else the formatted setting."""
    node = _filter_unit(doc, unit)
    if node is None:
        return format_value(session, unit, flat.get(unit))
    if unit.split(".")[2] == "peq":
        text = describe_filter(node.design)
    elif node.design is None and all(bq.is_passthrough for bq in node.compiled):
        text = "Pass-through"
    else:
        text = describe_crossover(node.design)
    text = _PLAIN_FILTER.get(text, text).replace("PEQ ", "Peaking ", 1).replace(" -", " −")
    return f"{text} (off)" if node.bypass else text


_EFFECTS = {
    "peq": "EQ changes",
    "crossover": "band filter changes",
    "delay_ms": "delay changes",
    "invert": "polarity changes",
    "compressor": "compressor changes",
}


def _effect(before: dict[str, Any], after: dict[str, Any], units: list[str]) -> str:
    """What the accept does to one channel, in words: "2.0 dB quieter, EQ changes"."""
    phrases: list[str] = []
    for unit in units:
        key = unit.split(".")[2]
        if key == "gain_db" and isinstance(before.get(unit), int | float) and isinstance(after.get(unit), int | float):
            delta = float(after[unit]) - float(before[unit])
            if abs(delta) >= 0.05:
                phrases.append(f"{abs(delta):.1f} dB {'louder' if delta > 0 else 'quieter'}")
        elif key == "mute":
            phrases.append("becomes muted" if after.get(unit) else "becomes unmuted")
        elif key in _EFFECTS:
            phrases.append(_EFFECTS[key])
    text = ", ".join(dict.fromkeys(phrases))
    return text[:1].upper() + text[1:]


def refused_lines(session: DeviceSession, doc: ConfigDoc, items: list[svc.VerifyItem]) -> list[str]:
    """One sentence per refused (all-zero) PEQ band or crossover group that is on in the app, and one for all the
    refused ones that are off (a factory device stores zeros in unused slots, so there can be dozens)."""
    lines = []
    off: list[str] = []
    for unit in dict.fromkeys(undo_unit(i.path) for i in items):
        _key, name = _channel(session, unit)
        node = _filter_unit(doc, unit)
        label = describe_path(session, unit)
        if node is not None and not node.bypass:
            lines.append(f"{label} is all zeros, which would make {name} silent.")
        else:
            off.append(label)
    if len(off) == 1:
        lines.append(f"{off[0]} is all zeros. It is off in the app, so this is likely an unused slot or a read error.")
    elif 0 < len(off) <= 3:
        lines.append(
            f"{len(off)} filters that are off in the app are all zeros, likely unused slots or read errors: "
            + ", ".join(off)
            + "."
        )
    elif off:
        lines.append(f"{len(off)} filters that are off in the app are all zeros, likely unused slots or read errors.")
    return lines


async def accept_confirm_context(
    request: Request, session: DeviceSession, report: svc.VerifyReport, paths: list[str] | None
) -> dict[str, Any]:
    """The "Use the device's values" confirm: one line per changed setting, grouped by channel with the effect on
    each, the settings that only get confirmed, and the refused all-zero filters."""
    preview = await asyncio.to_thread(svc.accept_preview, session, report, paths)
    before, after = flatten(preview.before), flatten(preview.after)
    # Only settings that reach the device count: designs the accept recognizes on the way change no sound.
    changed = list(dict.fromkeys(undo_unit(p) for p, v in after.items() if before.get(p) != v and is_device_path(p)))
    confirm_only = [u for u in dict.fromkeys(undo_unit(p) for p in preview.values) if u not in changed]
    groups: dict[tuple[str, int], dict[str, Any]] = {}
    for unit in changed:
        key, name = _channel(session, unit)
        group = groups.setdefault(key, {"name": name, "units": [], "lines": []})
        old = _unit_value(session, preview.before, before, unit)
        new = _unit_value(session, preview.after, after, unit)
        group["units"].append(unit)
        group["lines"].append(
            {
                "label": describe_path(session, unit),
                "before": old,
                "after": new if new != old else f"{new} (slightly different on the device)",
            }
        )
    for group in groups.values():
        group["effect"] = _effect(before, after, group["units"])
    count = len(changed) + len(confirm_only)
    return {
        "serial": session.serial,
        "preset": report.preset,
        "checked_at": report.checked_at,
        "paths": [i.path for i in svc.accept_items(report, paths)],
        "count": count,
        "count_text": _settings(count),
        "groups": [groups[k] for k in sorted(groups)],
        "confirm_only": len(confirm_only),
        # Every refused filter of the check is listed, also when only some paths were chosen: the form posts only the
        # acceptable ones.
        "refused": refused_lines(session, preview.before, report.refused),
        "problems": preview.problems,
        "accept_url": url(request, f"/ui/devices/{session.serial}/verify/accept"),
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
    refused = report.refused if report else []
    return {
        "serial": session.serial,
        "report": report,
        "counts": report.counts if report else None,
        "rows": [r for r in rows if r["status"] != "unverifiable"],
        "unverifiable": [r for r in rows if r["status"] == "unverifiable"],
        "drift_paths": [i.path for i in report.drifted] if report else [],
        "accept_paths": [i.path for i in report.acceptable] if report else [],
        "accept_count": _count_units([i.path for i in report.acceptable]) if report else 0,
        "refused": refused_lines(session, session.writer.state(report.preset).desired, refused) if refused else [],
        "mismatch_count": len(report.mismatched) if report else 0,
        "verify_url": url(request, base),
        "mark_url": url(request, f"{base}/mark-unknown"),
        "confirm_url": url(request, f"{base}/accept/confirm"),
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


_REPORT_CHANGED = "The check result changed; check the device again."


@ui_router.post("/ui/devices/{serial}/verify/mark-unknown", response_class=HTMLResponse)
async def verify_mark_unknown(request: Request, serial: int) -> Response:
    """ "Forget these values": mark the drifted paths of the last report unknown, so the sync table offers to push
    them again. Nothing is written."""
    session = _ui_session(request, serial)
    if isinstance(session, Response):
        return session
    form = await request.form()
    report = svc.last_report(serial)
    preset = form.get("preset")
    if report is None or not isinstance(preset, str) or preset != str(report.preset):
        ctx = verify_context(request, session, error=_REPORT_CHANGED)
        return render(request, "partials/verify.html", ctx)
    drifted = {i.path for i in report.drifted}
    paths = [p for p in form.getlist("path") if isinstance(p, str) and p in drifted]
    marked = svc.mark_drift_unknown(session, report.preset, paths)
    report.marked_unknown = sorted(set(report.marked_unknown) | set(marked))
    message = (
        f"Forgot {_settings(len(marked))}: the app now treats them as Unknown. Nothing changed on the device. "
        "Push them from the sync table to overwrite the device."
        if marked
        else None
    )
    response = render(request, "partials/verify.html", verify_context(request, session, message=message))
    if marked:
        response.headers["HX-Trigger"] = json.dumps({SYNC_CHANGED: {"preset": report.preset}})
    return response


def _report_for(serial: int, preset: Any, checked_at: Any = None) -> svc.VerifyReport | None:
    """The last report if the form still matches it (same preset and, when given, the same check)."""
    report = svc.last_report(serial)
    if report is None or not isinstance(preset, str) or preset != str(report.preset):
        return None
    if checked_at is not None and checked_at != report.checked_at:
        return None
    return report


@ui_router.get("/ui/devices/{serial}/verify/accept/confirm", response_class=HTMLResponse)
async def verify_accept_confirm(request: Request, serial: int) -> Response:
    """The confirm dialog for "Use the device's values": what changes, per channel, and what is refused. Read-only."""
    session = _ui_session(request, serial)
    if isinstance(session, Response):
        return session
    params = request.query_params
    report = _report_for(serial, params.get("preset"))
    if report is None:
        ctx = verify_context(request, session, error=_REPORT_CHANGED)
        return render(request, "partials/verify.html", ctx, status_code=409, error=_REPORT_CHANGED)
    paths = params.getlist("path") or None
    ctx = await accept_confirm_context(request, session, report, paths)
    return render(request, "partials/verify_accept_confirm.html", ctx)


def _toast(message: str, *, level: str = "info", **extra: Any) -> dict[str, Any]:
    return {"message": message, "level": level, **extra}


@ui_router.post("/ui/devices/{serial}/verify/accept", response_class=HTMLResponse)
async def verify_accept(request: Request, serial: int) -> Response:
    """Use the device's values for the last report's drifted and never-confirmed settings. Nothing is written; an
    Undo toast offers to put the app's values back for `ACCEPT_UNDO_S`."""
    session = _ui_session(request, serial)
    if isinstance(session, Response):
        return session
    form = await request.form()
    report = _report_for(serial, form.get("preset"), form.get("checked_at"))
    if report is None:
        ctx = verify_context(request, session, error=_REPORT_CHANGED)
        return render(request, "partials/verify.html", ctx)
    paths = [p for p in form.getlist("path") if isinstance(p, str)]
    try:
        accepted = await svc.accept_device_values(session, report, paths)
    except (DesiredValidationError, StateUnavailable) as exc:
        problems = exc.problems if isinstance(exc, DesiredValidationError) else [str(exc)]
        ctx = verify_context(request, session, error="; ".join(problems))
        return render(request, "partials/verify.html", ctx)
    if not accepted:
        return render(request, "partials/verify.html", verify_context(request, session))
    text = f"Using the device's values for {_settings(_count_units(accepted))}. Nothing was sent to the device."
    response = render(request, "partials/verify.html", verify_context(request, session, message=text))
    trigger: dict[str, Any] = {SYNC_CHANGED: {"preset": report.preset}}
    snap = session.accept_undo
    if snap is not None:
        trigger[TOAST] = _toast(
            text,
            timeout_ms=int(ACCEPT_UNDO_S * 1000),
            action={
                "label": "Undo",
                "url": url(request, f"/ui/devices/{serial}/verify/accept/undo"),
                "values": {"preset": str(report.preset), "token": snap.token},
                "target": "#verify-panel",
                "swap": "outerHTML",
            },
        )
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


@ui_router.post("/ui/devices/{serial}/verify/accept/undo", response_class=HTMLResponse)
async def verify_accept_undo(request: Request, serial: int) -> Response:
    """Undo the last "Use the device's values" (within 30 seconds). Writes nothing and queues no write."""
    session = _ui_session(request, serial)
    if isinstance(session, Response):
        return session
    form = await request.form()
    preset, token = form.get("preset"), form.get("token")
    if not isinstance(preset, str) or not preset.isdigit():
        return PlainTextResponse("preset is required", status_code=422)
    try:
        result = await svc.undo_accept(session, int(preset), token if isinstance(token, str) and token else None)
    except (UndoUnavailable, DesiredValidationError, StateUnavailable) as exc:
        if isinstance(exc, UndoUnavailable) and exc.code == "expired":
            text = "Too late to undo: it can only be undone for 30 seconds."
        elif isinstance(exc, UndoUnavailable):
            text = "Nothing to undo: the device's values were already undone or used again since."
        else:
            text = f"Undo failed: {exc}"
        ctx = verify_context(request, session, error=text)
        return render(request, "partials/verify.html", ctx, status_code=409, error=text)
    text = f"Undone: the app's earlier values are back for {_settings(len(result.settings))}."
    if result.skipped:
        names = ", ".join(describe_path(session, u) for u in result.skipped)
        text += f" Left alone because they changed since: {names}."
    text += " Nothing was sent to the device."
    response = render(request, "partials/verify.html", verify_context(request, session, message=text))
    response.headers["HX-Trigger"] = json.dumps({SYNC_CHANGED: {"preset": result.preset}, TOAST: _toast(text)})
    return response
