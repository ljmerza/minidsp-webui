"""Profiles page (docs 05 §7): saved profiles, save / load / diff / duplicate / rename / delete / export / import, and a
History tab with each preset's revision timeline and revert-to-here.

- ``GET /profiles``: the page. ``?tab=history`` shows the timeline of ``?preset=`` (1-based; default: the active
  preset). ``?a=`` / ``?b=`` preselect the diff (``profile:ID``, ``preset:P``, ``revision:P:R``, 0-based P).
- ``/ui/profiles/…`` partials and actions (0-based presets). List actions answer with the re-rendered list; load and
  revert answer JSON for static/js/profiles.js, which asks for confirmation first (``preview``) and again when the
  big-step guard or a FIR load needs it (409 ``confirmation_required``).

Everything goes through `app.sync.profiles`, the same service layer as the JSON API.
"""

import contextlib
import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.datastructures import UploadFile

from app.api.deps import refresh_mqtt_after_write, require_same_origin
from app.api.v1.adopt import MAX_SCRIPT_CHARS
from app.daemon.client import DaemonError
from app.dsp.recognize import describe_crossover, describe_filter
from app.model.filters import CrossoverDesign, FilterDesign
from app.state import ProfileNameTaken, RevisionConflict
from app.sync import profiles as svc
from app.sync.session import (
    ConfirmationRequired,
    DesiredValidationError,
    DeviceSession,
    MasterValidationError,
    PresetSwitchInProgress,
    PresetSwitchTimeout,
    StateUnavailable,
    UnknownPreset,
    flush_json,
)
from app.sync.writer import FlushResult
from app.web.sync_ui import describe_path, format_value
from app.web.ui_common import (
    device_preset,
    edit_page_context,
    is_htmx,
    load_state,
    manager_of,
    pick_session,
    render,
    repo_of,
    resolve_preset,
    url,
)

router = APIRouter(
    include_in_schema=False, dependencies=[Depends(require_same_origin), Depends(refresh_mqtt_after_write)]
)

PAGE = "/profiles"
TABS = (("profiles", "Profiles"), ("history", "History"))
HISTORY_PAGE = 25
MAX_IMPORT_BYTES = 1 << 20
RELOAD_EVENT = "mdsp-profiles-reload"  # profiles.js fires it after a load or revert: the list and timeline reload
NO_STORE = {"Cache-Control": "no-store"}


# --- formatting -------------------------------------------------------------------------------------------------------


def short_time(iso: str | None) -> str:
    """``2026-10-16T20:01:02.345+00:00`` -> ``2026-10-16 20:01 UTC`` (profiles.js shows it in local time)."""
    if not iso:
        return "—"
    match = re.match(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d)", iso)
    return f"{match.group(1)} {match.group(2)} UTC" if match else iso


def _unit_kind(path: str) -> str | None:
    parts = path.split(".")
    return parts[2] if len(parts) == 4 and parts[2] in ("peq", "crossover") else None


def _coeffs(value: Any) -> str:
    if isinstance(value, dict) and {"b0", "b1", "b2", "a1", "a2"} <= set(value):
        return " ".join(f"{value[k]:.6g}" for k in ("b0", "b1", "b2", "a1", "a2"))
    if isinstance(value, list):
        return " | ".join(_coeffs(v) for v in value)
    return str(value)


def _band_text(band: dict[str, Any]) -> str:
    design = band.get("design") or {}
    try:
        text = describe_filter(FilterDesign.model_validate(design))
    except ValidationError:
        text = "Custom"
    if design.get("type") in (None, "passthrough", "raw") and band.get("compiled") not in (None, {}):
        compiled = band["compiled"]
        is_flat = all(compiled.get(k) == v for k, v in {"b0": 1, "b1": 0, "b2": 0, "a1": 0, "a2": 0}.items())
        text = "Flat" if is_flat else "Raw biquad"
    return text


def _group_text(group: dict[str, Any]) -> str:
    design = group.get("design")
    if design is None:
        compiled = group.get("compiled") or []
        flat = all(bq == {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0} for bq in compiled)
        return "Flat" if flat else "Custom"
    try:
        return describe_crossover(CrossoverDesign.model_validate(design))
    except ValidationError:
        return "Custom"


def format_unit(path: str, value: Any, other: Any) -> str:
    """A PEQ band or crossover group in a diff: its design (or "off"), with the coefficients when the two sides
    only differ there."""
    if not isinstance(value, dict):
        return "—"
    kind = _unit_kind(path)
    text = _band_text(value) if kind == "peq" else _group_text(value)
    if value.get("bypass"):
        text = f"off ({text})" if text not in ("Flat", "Pass-through") else "off"
    if isinstance(other, dict):
        other_text = _band_text(other) if kind == "peq" else _group_text(other)
        same_shape = other_text == (_band_text(value) if kind == "peq" else _group_text(value))
        if same_shape and other.get("bypass") == value.get("bypass") and other.get("compiled") != value.get("compiled"):
            text += f" [{_coeffs(value.get('compiled'))}]"
    return text


def format_leaf(session: DeviceSession | None, path: str, value: Any) -> str:
    if session is not None:
        text = format_value(session, path, value)
    elif isinstance(value, bool):
        text = ("180°" if value else "0°") if path.endswith(".invert") else ("on" if value else "off")
    elif isinstance(value, int | float) and path.endswith(".delay_ms"):
        text = f"{float(value):.3f} ms"
    elif isinstance(value, int | float) and path.endswith("_db"):
        text = f"{float(value):+.2f} dB".replace("-", "−")
    else:
        text = "none" if value is None else str(value)
    if path.endswith(("attack_ms", "release_ms")) and isinstance(value, int | float):
        text += " ms"
    return text


def diff_rows(session: DeviceSession | None, rows: list[svc.DiffRow]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        if row.unit:
            a, b = format_unit(row.path, row.a, row.b), format_unit(row.path, row.b, row.a)
        else:
            a, b = format_leaf(session, row.path, row.a), format_leaf(session, row.path, row.b)
        out.append({"path": row.path, "label": describe_path(session, row.path), "a": a, "b": b})
    return out


_ONE_CHANGE = re.compile(r"^((?:inputs|outputs)\.[\w.]+): (.+) → (.+)$")
_ONE_UNIT = re.compile(r"^((?:inputs|outputs)\.[\w.]+) changed$")
_MANY = re.compile(r"^(\d+) changes: (.+?)(, …)?$")


def _unit_suffix(path: str) -> str:
    if path.endswith("_db"):
        return " dB"
    if path.endswith("_ms"):
        return " ms"
    return ""


def _minus(text: str) -> str:
    return text.replace("-", "−", 1) if re.fullmatch(r"-\d+(?:\.\d+)?(?:e-?\d+)?", text) else text


def pretty_summary(session: DeviceSession | None, text: str | None) -> str:
    """Revision summaries name raw paths (``outputs.0.gain_db: 10 → 12``); show channel names instead
    ("Shakers · gain 10 → 12 dB")."""
    if not text:
        return "—"
    if match := _ONE_CHANGE.match(text):
        path, a, b = match.groups()
        return f"{describe_path(session, path)} {_minus(a)} → {_minus(b)}{_unit_suffix(path)}"
    if match := _ONE_UNIT.match(text):
        return f"{describe_path(session, match.group(1))} changed"
    if match := _MANY.match(text):
        count, paths, more = match.groups()
        names = ", ".join(describe_path(session, p.strip()) for p in paths.split(","))
        return f"{count} changes: {names}{more or ''}"
    return text


SOURCE_LABELS = {
    "ui": "web UI",
    "api": "API",
    "import": "import",
    "profile": "profile",
    "adopt": "adopt",
    "revert": "revert",
    "mqtt": "Home Assistant",
}


# --- context ------------------------------------------------------------------------------------------------------


def _session(request: Request, serial: int | None) -> DeviceSession | None:
    return pick_session(request, serial)


def _preset_options(session: DeviceSession | None) -> list[dict[str, Any]]:
    if session is None:
        return []
    active = device_preset(session)
    return [{"index": p, "label": f"Preset {p + 1}", "active": p == active} for p in range(session.profile.n_presets)]


async def list_context(request: Request, session: DeviceSession | None) -> dict[str, Any]:
    repo = repo_of(request, session)
    ctx: dict[str, Any] = {
        "profiles": [],
        "profiles_error": None,
        "serial": session.serial if session else None,
        "preset_options": _preset_options(session),
        "active_preset": device_preset(session) if session else None,
        "list_url": url(request, "/ui/profiles/list", serial=session.serial if session else None),
    }
    try:
        views = await svc.list_profiles(repo)
    except svc.ProfilesUnavailable as exc:
        ctx["profiles_error"] = str(exc)
        return ctx
    state_ok = session is not None and (await load_state(session)) is None
    items = []
    for view in views:
        p = view.profile
        matches: list[int] = []
        doc = None
        with contextlib.suppress(DesiredValidationError):
            doc = svc.profile_doc(p)
        if state_ok and doc is not None:
            matches = svc.matching_presets(session, doc)
        loaded = []
        for slot in view.loaded:
            if session is None or slot.serial != session.serial:
                continue
            loaded.append(
                {"preset": slot.preset, "at": slot.at, "at_text": short_time(slot.at), "same": slot.preset in matches}
            )
        loaded_presets = {slot["preset"] for slot in loaded}
        model_ok = session is None or not p.product_name or p.product_name == session.profile.product_name
        items.append(
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "product_name": p.product_name,
                "created_at": p.created_at,
                "created_text": short_time(p.created_at),
                "updated_at": p.updated_at,
                "updated_text": short_time(p.updated_at),
                "edited": p.updated_at != p.created_at,
                "loaded": loaded,
                "matches": [m for m in matches if m not in loaded_presets],
                "valid": doc is not None,
                "loadable": doc is not None and model_ok and state_ok,
                "model_ok": model_ok,
                "export_url": url(request, f"/api/v1/profiles/{p.id}/export"),
                "script_url": url(request, f"/api/v1/profiles/{p.id}/export/cli-script"),
                "rename_url": url(request, f"/ui/profiles/{p.id}/rename", serial=ctx["serial"]),
                "duplicate_url": url(request, f"/ui/profiles/{p.id}/duplicate", serial=ctx["serial"]),
                "delete_url": url(request, f"/ui/profiles/{p.id}/delete", serial=ctx["serial"]),
            }
        )
    ctx["profiles"] = items
    return ctx


def _list_response(
    request: Request, ctx: dict[str, Any], *, status_code: int = 200, error: str | None = None, message: str = ""
) -> HTMLResponse:
    return render(
        request, "profiles/_list.html", {"l": {**ctx, "message": message}}, status_code=status_code, error=error
    )


async def _list_with(
    request: Request, serial: int | None, *, status_code: int = 200, error: str | None = None, message: str = ""
) -> HTMLResponse:
    session = _session(request, serial)
    ctx = await list_context(request, session)
    return _list_response(request, ctx, status_code=status_code, error=error, message=message)


async def diff_sources(request: Request, session: DeviceSession | None) -> list[dict[str, Any]]:
    """Options of the diff selects: every profile and every preset's desired state."""
    options = []
    try:
        views = await svc.list_profiles(repo_of(request, session))
    except svc.ProfilesUnavailable:
        views = []
    options += [{"value": f"profile:{v.profile.id}", "label": f"Profile “{v.profile.name}”"} for v in views]
    if session is not None:
        active = device_preset(session)
        options += [
            {"value": f"preset:{p}", "label": f"Preset {p + 1}" + (" (active)" if p == active else "")}
            for p in range(session.profile.n_presets)
        ]
    return options


# --- page ---------------------------------------------------------------------------------------------------------


@router.get(PAGE, response_class=HTMLResponse, name="profiles_page")
async def profiles_page(
    request: Request,
    serial: int | None = None,
    tab: str = "profiles",
    preset: int | None = None,
    a: str | None = None,
    b: str | None = None,
) -> Response:
    session = _session(request, serial)
    ctx = edit_page_context(request, session, "Profiles")
    ctx["profiles_cfg"] = {
        "serial": session.serial if session else None,
        "loadUrl": url(request, "/ui/profiles/{id}/load"),
        "revertUrl": url(request, "/ui/devices/{serial}/presets/{preset}/history/{revision}/revert"),
        "reloadEvent": RELOAD_EVENT,
    }
    if serial is not None and session is None:
        ctx["page_error"] = f"Unknown device {serial}."
        return render(request, "profiles.html", ctx, status_code=404)
    tab = tab if tab in dict(TABS) else "profiles"
    query = {"serial": serial}
    ctx.update(
        tab=tab,
        tabs=[{"key": k, "label": label, "url": url(request, PAGE, tab=k, **query)} for k, label in TABS],
        repo_ok=repo_of(request, session) is not None,
        state_error=(await load_state(session)) if session is not None else None,
    )
    p = resolve_preset(session, preset) if session is not None else None
    if session is not None and p is None:
        ctx["page_error"] = f"There is no preset {preset} on this device."
        return render(request, "profiles.html", ctx, status_code=404)
    ctx["preset"] = p
    ctx["preset_options"] = _preset_options(session)
    sources = await diff_sources(request, session)
    values = {o["value"] for o in sources}
    default_b = f"preset:{p}" if p is not None else None
    ctx["diff"] = {
        "sources": sources,
        "a": a if a else (sources[0]["value"] if sources else None),
        "b": b if b else default_b,
        "url": url(request, "/ui/profiles/diff", serial=serial),
        "extra": [s for s in (a, b) if s and s not in values],  # e.g. a revision picked from the history
    }
    if tab == "profiles":
        ctx["list"] = await list_context(request, session)
    elif session is not None and not ctx["state_error"]:
        ctx["history"] = await history_context(request, session, p)
        ctx["history_tabs"] = [
            {
                "index": i,
                "label": f"Preset {i + 1}",
                "url": url(request, PAGE, tab="history", preset=i + 1, serial=serial),
                "selected": i == p,
                "active": i == device_preset(session),
            }
            for i in range(session.profile.n_presets)
        ]
    return render(request, "profiles.html", ctx)


# --- profile list actions -----------------------------------------------------------------------------------------


@router.get("/ui/profiles/list", response_class=HTMLResponse)
async def profile_list(request: Request, serial: int | None = None) -> Response:
    session = _session(request, serial)
    return render(request, "profiles/_list.html", {"l": await list_context(request, session)})


async def _form(request: Request) -> dict[str, Any]:
    form = await request.form()
    return {k: v for k, v in form.items() if not isinstance(v, UploadFile)}


def _refused(request: Request) -> Response | None:
    if not is_htmx(request):
        return Response("HTMX request expected", status_code=400)
    return None


def _profile_error(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, svc.ProfileNotFound | svc.RevisionNotFound):
        return 404, str(exc)
    if isinstance(exc, ProfileNameTaken):
        return 409, f"There is already a profile named “{exc.name}”"
    if isinstance(exc, svc.ProfilesUnavailable | StateUnavailable):
        return 503, str(exc)
    if isinstance(exc, DesiredValidationError):
        return 422, "; ".join(exc.problems) or str(exc)
    if isinstance(exc, UnknownPreset):
        return 404, str(exc)
    return 422, str(exc)


LIST_ERRORS = (svc.ProfileError, ProfileNameTaken, svc.ProfilesUnavailable, StateUnavailable, MasterValidationError)


@router.post("/ui/profiles/save", response_class=HTMLResponse)
async def save_profile(request: Request, serial: int | None = None) -> Response:
    """Form: ``preset`` (0-based), ``name``, ``description``."""
    if refused := _refused(request):
        return refused
    form = await _form(request)
    session = _session(request, serial)
    if session is None:
        return await _list_with(request, serial, status_code=404, error="No device: there is no preset to save")
    preset = str(form.get("preset", ""))
    try:
        if not preset.isdigit():
            raise svc.ProfileError("pick a preset to save")
        profile = await svc.save_preset(
            session, int(preset), str(form.get("name", "")), description=str(form.get("description", "")) or None
        )
    except LIST_ERRORS as exc:
        status, message = _profile_error(exc)
        return await _list_with(request, serial, status_code=status, error=message)
    return await _list_with(request, serial, message=f"Saved preset {int(preset) + 1} as “{profile.name}”.")


@router.post("/ui/profiles/{profile_id}/rename", response_class=HTMLResponse)
async def rename_profile(request: Request, profile_id: int, serial: int | None = None) -> Response:
    """Form: ``name`` and ``description``."""
    if refused := _refused(request):
        return refused
    form = await _form(request)
    try:
        profile = await svc.update_profile(
            repo_of(request, _session(request, serial)),
            profile_id,
            name=str(form.get("name", "")),
            description=str(form.get("description", "")) or None,
        )
    except LIST_ERRORS as exc:
        status, message = _profile_error(exc)
        return await _list_with(request, serial, status_code=status, error=message)
    return await _list_with(request, serial, message=f"Saved “{profile.name}”.")


@router.post("/ui/profiles/{profile_id}/duplicate", response_class=HTMLResponse)
async def duplicate_profile(request: Request, profile_id: int, serial: int | None = None) -> Response:
    if refused := _refused(request):
        return refused
    try:
        profile = await svc.duplicate_profile(repo_of(request, _session(request, serial)), profile_id)
    except LIST_ERRORS as exc:
        status, message = _profile_error(exc)
        return await _list_with(request, serial, status_code=status, error=message)
    return await _list_with(request, serial, message=f"Created “{profile.name}”.")


@router.post("/ui/profiles/{profile_id}/delete", response_class=HTMLResponse)
async def delete_profile(request: Request, profile_id: int, serial: int | None = None) -> Response:
    if refused := _refused(request):
        return refused
    repo = repo_of(request, _session(request, serial))
    try:
        view = await svc.get_profile(repo, profile_id)
        await svc.delete_profile(repo, profile_id)
    except LIST_ERRORS as exc:
        status, message = _profile_error(exc)
        return await _list_with(request, serial, status_code=status, error=message)
    return await _list_with(request, serial, message=f"Deleted “{view.profile.name}”.")


@router.post("/ui/profiles/import", response_class=HTMLResponse)
async def import_profile(request: Request, serial: int | None = None) -> Response:
    """Multipart form: ``file``, a profile export (JSON) or a ``minidsp -f`` script (named after the file)."""
    if refused := _refused(request):
        return refused
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_IMPORT_BYTES + 64 * 1024:
        return await _list_with(request, serial, status_code=413, error="That file is too large for a profile")
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return await _list_with(request, serial, status_code=422, error="Choose a profile file to import")
    raw = await upload.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        return await _list_with(request, serial, status_code=413, error="That file is too large for a profile")
    session = _session(request, serial)
    repo = repo_of(request, session)
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        data = None
    try:
        if data is not None:
            profile, warnings = await svc.import_profile(repo, data)
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if "\x00" in text or not text.strip() or len(text) > MAX_SCRIPT_CHARS:
                raise svc.ProfileError("That file is neither a profile export (JSON) nor a minidsp -f script")
            name = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", upload.filename or "").strip() or "Imported script"
            profile, warnings = await svc.import_profile_script(
                repo, text, name[: svc.MAX_NAME], product_name=session.profile.product_name if session else None
            )
    except LIST_ERRORS as exc:
        status, message = _profile_error(exc)
        return await _list_with(request, serial, status_code=status, error=message)
    message = f"Imported “{profile.name}”." + "".join(f" {w}." for w in warnings)
    return await _list_with(request, serial, message=message)


# --- diff -----------------------------------------------------------------------------------------------------------


@router.get("/ui/profiles/diff", response_class=HTMLResponse)
async def diff_partial(
    request: Request,
    a: Annotated[str, Query()] = "",
    b: Annotated[str, Query()] = "",
    serial: int | None = None,
) -> Response:
    session = _session(request, serial)
    ctx: dict[str, Any] = {"a": None, "b": None, "rows": [], "error": None}
    if not a or not b:
        ctx["error"] = "Pick two things to compare."
        return render(request, "profiles/_diff.html", ctx)
    try:
        if session is not None and (problem := await load_state(session)):
            raise StateUnavailable(problem)
        repo = repo_of(request, session)
        side_a = await svc.resolve_source(a, repo=repo, session=session)
        side_b = await svc.resolve_source(b, repo=repo, session=session)
    except (svc.ProfileError, svc.ProfilesUnavailable, StateUnavailable, MasterValidationError) as exc:
        status, message = _profile_error(exc)
        ctx["error"] = message
        return render(request, "profiles/_diff.html", ctx, status_code=status, error=message)
    rows = svc.diff_docs(side_a.doc, side_b.doc)
    ctx.update(a=side_a, b=side_b, rows=diff_rows(session, rows), same=side_a.source == side_b.source)
    return render(request, "profiles/_diff.html", ctx)


# --- load / revert (JSON for profiles.js) -----------------------------------------------------------------------------


class LoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: int
    activate: bool = False
    confirm: bool = False
    preview: bool = False
    serial: int | None = None


class RevertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    preview: bool = False


def _json(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status_code, headers=NO_STORE)


def _fail(status_code: int, error: str, detail: str, **extra: Any) -> JSONResponse:
    return _json({"error": error, "detail": detail, **extra}, status_code)


def _action_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, ConfirmationRequired):
        return _fail(409, "confirmation_required", str(exc))
    if isinstance(exc, RevisionConflict):
        return _fail(412, "revision_conflict", str(exc))
    if isinstance(exc, PresetSwitchInProgress):
        return _fail(409, "switching", str(exc))
    if isinstance(exc, PresetSwitchTimeout):
        return _fail(504, "switch_timeout", str(exc))
    if isinstance(exc, DaemonError):
        return _fail(502, "daemon", f"The device did not accept the change: {exc}")
    status, message = _profile_error(exc)
    problems = exc.problems if isinstance(exc, DesiredValidationError) else []
    return _fail(status, "invalid" if status == 422 else "error", message, problems=problems)


ACTION_ERRORS = (
    svc.ProfileError,
    svc.ProfilesUnavailable,
    ProfileNameTaken,
    MasterValidationError,  # includes ConfirmationRequired, DesiredValidationError, UnknownPreset
    RevisionConflict,
    StateUnavailable,
    PresetSwitchInProgress,
    PresetSwitchTimeout,
    DaemonError,
)


async def _body[M: BaseModel](request: Request, model: type[M]) -> M | JSONResponse:
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError):
        return _fail(400, "bad_request", "malformed request")


def _apply_note(preset: int, active: bool, mute_wrap: bool, activate: bool) -> str:
    if activate and not active:
        return f"The device switches to preset {preset + 1} and applies it; audio may drop out for a few seconds" + (
            " and the changed outputs stay muted until it is done." if mute_wrap else "."
        )
    if active:
        return f"Preset {preset + 1} is active, so the changes are written now" + (
            " (the changed outputs are muted while they land)." if mute_wrap else "."
        )
    return f"Preset {preset + 1} isn't active: the changes are staged and written when it becomes active."


def outcome_note(session: DeviceSession, preset: int, flush: FlushResult | None) -> tuple[str, str]:
    """(text, level) describing where a bulk apply stands now."""
    summary = session.sync_summary(preset)
    counts = summary["counts"]
    if flush is not None and flush.staged and not summary["active"]:
        return f" Staged until preset {preset + 1} is active.", "info"
    if not (counts.get("pending") or counts.get("failed")):
        return "", "info"
    error = summary.get("error")
    return (
        f" Not finished: {counts.get('pending', 0)} pending, {counts.get('failed', 0)} failed"
        + (f" ({error})" if error else "")
        + ". The sync status shows what is left.",
        "warning",
    )


@router.post("/ui/profiles/{profile_id}/load")
async def load_profile(request: Request, profile_id: int) -> Response:
    """JSON ``{preset, activate, confirm, preview, serial?}``. ``preview`` answers the confirmation question."""
    if refused := _refused(request):
        return refused
    body = await _body(request, LoadRequest)
    if isinstance(body, JSONResponse):
        return body
    session = _session(request, body.serial)
    if session is None:
        return _fail(404, "unknown_device", "No device to load the profile into")
    try:
        if body.preview:
            info = await svc.load_preview(session, profile_id, body.preset)
            if info["changes"] == 0 and not info["unknown"]:
                question = f"Preset {body.preset + 1} already matches “{info['name']}”."
                if body.activate and not info["active"]:
                    question += f" Switch the device to preset {body.preset + 1}?"
            else:
                unknown = (
                    f"{info['unknown']} setting(s) not yet confirmed on the device are written too. "
                    if info["unknown"]
                    else ""
                )
                question = (
                    f"Load “{info['name']}” into preset {body.preset + 1}? "
                    f"{info['changes']} setting(s) change. {unknown}"
                    + _apply_note(body.preset, info["active"], info["mute_wrap"], body.activate)
                )
            return _json({**info, "question": question})
        result = await svc.load_profile(session, profile_id, body.preset, activate=body.activate, confirm=body.confirm)
    except ACTION_ERRORS as exc:
        return _action_error(exc)
    flush = result.edit.flush
    note, level = outcome_note(session, body.preset, flush)
    switched = f" Switched to preset {body.preset + 1}." if result.activated else ""
    message = f"Loaded “{result.profile.name}” into preset {body.preset + 1}.{switched}{note}"
    return _json(
        {
            "ok": True,
            "message": message,
            "level": level,
            "preset": body.preset,
            "revision": result.edit.revision,
            "changed": len(result.edit.paths),
            "flush": flush_json(flush),
        }
    )


@router.post("/ui/devices/{serial}/presets/{preset}/history/{revision}/revert")
async def revert_revision(request: Request, serial: int, preset: int, revision: int) -> Response:
    """JSON ``{confirm, preview}``."""
    if refused := _refused(request):
        return refused
    body = await _body(request, RevertRequest)
    if isinstance(body, JSONResponse):
        return body
    session = manager_of(request).get(serial)
    if session is None:
        return _fail(404, "unknown_device", f"Unknown device {serial}")
    try:
        if body.preview:
            info = await svc.revert_preview(session, preset, revision)
            if info["changes"] == 0:
                question = f"Preset {preset + 1} already matches revision {revision}. Nothing would change."
            else:
                question = (
                    f"Revert preset {preset + 1} to revision {revision}? {info['changes']} setting(s) change; "
                    "the revert is recorded as a new revision. "
                    + _apply_note(preset, info["active"], info["mute_wrap"], False)
                )
            return _json({**info, "question": question})
        result = await svc.revert_to(session, preset, revision, confirm=body.confirm)
    except ACTION_ERRORS as exc:
        return _action_error(exc)
    note, level = outcome_note(session, preset, result.flush)
    if result.paths:
        message = f"Preset {preset + 1} reverted to revision {revision} (now revision {result.revision}).{note}"
    else:
        message = f"Preset {preset + 1} already matched revision {revision}."
    return _json(
        {
            "ok": True,
            "message": message,
            "level": level,
            "preset": preset,
            "revision": result.revision,
            "changed": len(result.paths),
            "flush": flush_json(result.flush),
        }
    )


# --- history ------------------------------------------------------------------------------------------------------


async def history_context(
    request: Request, session: DeviceSession, preset: int, *, before: int | None = None
) -> dict[str, Any]:
    rows = await session.history(preset, limit=HISTORY_PAGE + 1, before=before)
    more = len(rows) > HISTORY_PAGE
    rows = rows[:HISTORY_PAGE]
    current = session.revisions.get(preset, 0)
    summary = session.sync_summary(preset)
    serial = session.serial
    entries = [
        {
            "revision": r.revision,
            "current": r.revision == current,
            "created_at": r.created_at,
            "created_text": short_time(r.created_at),
            "source": r.source,
            "source_label": SOURCE_LABELS.get(r.source or "", r.source or "—"),
            "summary": pretty_summary(session, r.summary),
            "diff_a": f"revision:{preset}:{r.revision}",
        }
        for r in rows
    ]
    return {
        "serial": serial,
        "preset": preset,
        "entries": entries,
        "current": current,
        "uncommitted": summary.get("uncommitted", False),
        "first_page": before is None,
        "more_url": url(
            request, f"/ui/devices/{serial}/presets/{preset}/history", before=rows[-1].revision if more else None
        )
        if more
        else None,
        "history_url": url(request, f"/ui/devices/{serial}/presets/{preset}/history"),
        "diff_b": f"preset:{preset}",
    }


@router.get("/ui/devices/{serial}/presets/{preset}/history", response_class=HTMLResponse)
async def history_partial(request: Request, serial: int, preset: int, before: int | None = None) -> Response:
    session = manager_of(request).get(serial)
    if session is None:
        return Response(f"Unknown device {serial}", status_code=404)
    if problem := await load_state(session):
        return render(request, "partials/state_error.html", {"state_error": problem}, status_code=503, error=problem)
    if not 0 <= preset < session.profile.n_presets:
        return Response(f"Unknown preset {preset}", status_code=404)
    ctx = await history_context(request, session, preset, before=before)
    template = "profiles/_history_rows.html" if before is not None else "profiles/_history.html"
    return render(request, template, {"h": ctx})
