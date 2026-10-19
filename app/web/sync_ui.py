"""Device page additions (docs 05 §8, phase 9 design §11): the status overview, the settings table grouped by
channel with a per-row send, push-all, the adopt wizard and the event log. These are HTMX partials loaded into the
Device & sync page; the page itself is served by `app.web.routes`. Statuses come from the one status model
(`app.sync.status`).
"""

import json
import math
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.datastructures import FormData, UploadFile

from app.api.deps import require_same_origin
from app.dsp.recognize import describe_filter, recognize_biquad
from app.formats.cli_script import ParsedScript
from app.model.config_doc import default_config_doc
from app.model.payload import device_values
from app.sync.session import (
    BULK_TIMEOUT_S,
    AdoptMode,
    BigRaise,
    ConfirmationRequired,
    DesiredValidationError,
    DeviceSession,
    MasterValidationError,
    StateUnavailable,
    factory_defaults_summary,
    undo_unit,
)
from app.sync.status import CHANGED, CSS, GLYPHS, ORDER, SENT, UNKNOWN, StatusView, status_view, word
from app.sync.verify import last_report, memory_map_for, zero_filter
from app.web.adopt_sample import ADOPT_SAMPLE
from app.web.params import syncbar_context
from app.web.ui_common import (
    channel_label,
    device_preset,
    is_htmx,
    load_state,
    manager_of,
    render,
    repo_of,
    resolve_preset,
    url,
)

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_same_origin)])

EVENT_LIMIT = 500
MAX_SCRIPT_BYTES = 200_000
DEVICE_PAGE = "/device"
SYNC_CHANGED = "mdsp-sync-changed"  # HX-Trigger event: the sync table and bar reload


# --- formatting -------------------------------------------------------------------------------------------------------

_FIELD_LABELS = {
    "gain_db": "gain",
    "mute": "mute",
    "invert": "polarity",
    "delay_ms": "delay",
    "bypass": "bypass",
    "compiled": "coefficients",
    "enabled": "enabled",
    "threshold_db": "threshold",
    "ratio": "ratio",
    "attack_ms": "attack",
    "release_ms": "release",
    "blob_id": "FIR taps",
}


# Channel-level settings read as one phrase ("Shakers output level"); see the phase9 design §2 vocabulary.
_CHANNEL_FIELDS = {"mute": "mute", "invert": "polarity", "delay_ms": "delay"}


def describe_path(session: DeviceSession | None, path: str) -> str:
    """A setting's plain name, 1-based like the UI and with the channel names from Settings:

    ``outputs.0.gain_db`` -> "Shakers output level", ``inputs.0.routing.1.gain_db`` -> "In 1 → Sub route level",
    ``outputs.0.peq.2`` -> "Shakers · EQ band 3", ``outputs.0.crossover.1.compiled.2`` -> "Shakers · band filter 2 ·
    stage 3". Anything else comes back unchanged."""
    parts = path.split(".")
    if len(parts) < 3 or parts[0] not in ("inputs", "outputs") or not parts[1].isdigit():
        return path
    kind = "input" if parts[0] == "inputs" else "output"
    name = channel_label(session, kind, int(parts[1]))
    rest = parts[2:]
    if len(rest) == 1 and rest[0] == "gain_db":
        return f"{name} {kind} level"
    if len(rest) == 1 and rest[0] in _CHANNEL_FIELDS:
        return f"{name} {_CHANNEL_FIELDS[rest[0]]}"
    if rest[0] == "routing" and len(rest) >= 2 and rest[1].isdigit():
        route = f"{name} → {channel_label(session, 'output', int(rest[1]))} route"
        field = rest[2:]
        if field == ["gain_db"]:
            return f"{route} level"
        if not field or field == ["enabled"]:
            return route
        return " · ".join([route, *(_FIELD_LABELS.get(key, key) for key in field)])
    words = [name]
    while rest:
        key = rest.pop(0)
        if key in ("peq", "crossover") and rest and rest[0].isdigit():
            words.append(f"{'EQ band' if key == 'peq' else 'band filter'} {int(rest.pop(0)) + 1}")
        elif key == "compiled" and rest and rest[0].isdigit():
            words.append(f"stage {int(rest.pop(0)) + 1}")
        elif key in ("compressor", "fir"):
            words.append(key if key == "compressor" else "FIR")
        elif key in ("peq", "crossover"):
            words.append("EQ" if key == "peq" else "band filters")
        else:
            words.append(_FIELD_LABELS.get(key, key))
    return " · ".join(words)


_PATH_IN_TEXT = re.compile(r"\b(?:inputs|outputs)\.\d+(?:\.\w+)*")


def humanize_paths(session: DeviceSession | None, text: str) -> str:
    """``text`` with every internal path in it (``outputs.0.gain_db``) replaced by its `describe_path` name."""
    return _PATH_IN_TEXT.sub(lambda m: describe_path(session, m.group(0)), text)


def _db_amount(value: float) -> str:
    return f"{round(value, 2):g} dB"


def raise_label(session: DeviceSession | None, item: BigRaise | Mapping[str, Any]) -> str:
    """ "Shakers output level" or, for a filter whose peak rises, "Shakers · EQ band 4 peak"."""
    path, kind = (item.path, item.kind) if isinstance(item, BigRaise) else (item["path"], item.get("kind", "gain"))
    return describe_path(session, path) + (" peak" if kind == "peak" else "")


def raise_rows(session: DeviceSession | None, raises: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``raises`` (`BigRaise.as_json`) with a ``label`` and a ``text`` ("Shakers output level up 12 dB")."""
    rows = []
    for item in raises:
        label = raise_label(session, item)
        rows.append({**item, "label": label, "text": f"{label} up {_db_amount(float(item['delta_db']))}"})
    return rows


def confirmation_text(session: DeviceSession | None, exc: ConfirmationRequired) -> str:
    """The question behind a 409 ``confirmation_required`` in words, with channel names and no internal paths. The
    pages show it as "<text>.\n\nApply it anyway?", so it has no closing full stop."""
    raises = ""
    if exc.raises:
        parts = [f"{raise_label(session, r)} by {_db_amount(r.delta_db)}" for r in exc.raises]
        limit = f" (more than {_db_amount(exc.limit_db)} in one step)" if exc.limit_db else ""
        raises = f"This raises {_join_words(parts)} at once{limit}"
        if str(exc).startswith("raising "):
            return raises
    text = humanize_paths(session, str(exc)).rstrip(".")
    text = text[:1].upper() + text[1:]
    return f"{text}. {raises}" if raises else text


def _join_words(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def format_value(session: DeviceSession, path: str, value: Any, *, known: bool = True) -> str:
    if not known:
        return "—"
    if isinstance(value, bool):
        if path.endswith(".invert"):
            return "180°" if value else "0°"
        return "on" if value else "off"
    if path.endswith(".delay_ms") and isinstance(value, int | float):
        samples = session.profile.delay_samples(float(value))
        return f"{float(value):.3f} ms ({samples} samples)"
    if path.endswith("_db") and isinstance(value, int | float):
        return f"{float(value):+.2f} dB".replace("-", "−")
    if isinstance(value, dict) and set(value) == {"b0", "b1", "b2", "a1", "a2"}:
        return " ".join(f"{k}={value[k]:.6g}" for k in ("b0", "b1", "b2", "a1", "a2"))
    if isinstance(value, float):
        return f"{value:g}"
    if value is None:
        return "none"
    return str(value)


# --- sync panel / table -----------------------------------------------------------------------------------------------


async def _session(request: Request, serial: int) -> DeviceSession | Response:
    session = manager_of(request).get(serial)
    if session is None:
        return PlainTextResponse(f"Unknown device {serial}", status_code=404)
    error = await load_state(session)
    if error:
        return render(
            request,
            "partials/state_error.html",
            {"state_error": error},
            status_code=503,
            error="preset state unavailable",
        )
    return session


def push_confirmation(session: DeviceSession, preset: int, path: str) -> str | None:
    """The question a per-row push must confirm first (docs 05 interaction rules), or None. Pushing writes a value
    whose current device value is unknown, so un-muting an output or writing a gain can't be checked against what
    the device plays now."""
    state = session.writer.state(preset)
    if path in state.applied:
        return None
    value = state.values(session.profile).get(path)
    parts = path.split(".")
    if parts[0] == "outputs" and parts[-1] == "mute" and len(parts) == 3 and value is False:
        name = channel_label(session, "output", int(parts[1]))
        return f"Un-mute {name}? It has never been synced, so the rest of its settings may not be on the device."
    if path.endswith(".gain_db"):
        shown = format_value(session, path, value)
        return f"Write {describe_path(session, path)} = {shown}? Its current device value is unknown."
    return None


_BIQUAD_KEYS = ("b0", "b1", "b2", "a1", "a2")
_PLAIN_FILTER = {"Pass-through": "flat", "Raw": "custom filter"}


@lru_cache(maxsize=1024)
def _describe_biquad(coeffs: tuple[float, ...]) -> str:
    try:
        text = describe_filter(recognize_biquad(coeffs))
    except ValueError:
        return "custom filter"
    text = _PLAIN_FILTER.get(text, text)
    return text.replace("PEQ ", "Peaking ", 1).replace(" -", " −").replace("+-", "−")


def plain_value(session: DeviceSession, path: str, value: Any, *, known: bool = True) -> str:
    """A setting's value in words, for the Device & sync table: "−4.00 dB", "on", "bypassed", "Peaking 40 Hz Q 4
    −4.0 dB", "flat", "all zeros". Raw coefficients only appear in Technical details (`format_value`)."""
    if not known:
        return "—"
    if path.endswith(".bypass") and isinstance(value, bool):
        return "bypassed" if value else "in use"
    if isinstance(value, dict) and set(value) == set(_BIQUAD_KEYS):
        coeffs = tuple(float(value[k]) for k in _BIQUAD_KEYS)
        if not all(math.isfinite(c) for c in coeffs):
            return "custom filter"
        if all(c == 0 for c in coeffs):
            return "all zeros"
        return _describe_biquad(coeffs)
    return format_value(session, path, value)


def _channel_key(path: str) -> tuple[str, int] | None:
    """The channel a path is listed under (a route belongs to its input, as `describe_path` names it)."""
    parts = path.split(".")
    if len(parts) < 2 or parts[0] not in ("inputs", "outputs") or not parts[1].isdigit():
        return None
    return ("input" if parts[0] == "inputs" else "output"), int(parts[1])


def _report_items(session: DeviceSession, preset: int) -> dict[str, Any]:
    report = last_report(session.serial)
    if report is None or report.preset != preset:
        return {}
    return {i.path: i for i in report.items}


def sync_rows(
    session: DeviceSession, preset: int, *, show_all: bool, view: StatusView | None = None
) -> list[dict[str, Any]]:
    """One row per device path of the preset (only those that aren't simply sent, unless ``show_all``), with its
    status from the one status model (app/sync/status.py) and its values in words; ``tech`` holds the raw ones."""
    view = view if view is not None else status_view(session, preset)
    state = session.writer.state(preset)
    values = state.values(session.profile)
    statuses = view.states if view is not None else {}
    reasons = view.reasons if view is not None else dict(state.failed)
    items = _report_items(session, preset)
    rows = []
    for path, value in values.items():
        status = statuses.get(path, UNKNOWN)
        if not show_all and status == SENT:
            continue
        known = path in state.applied
        item = items.get(path)
        # What the device reported: shown for a changed setting, and for an unknown one that differs from the app.
        reading = item is not None and item.actual is not None
        show_device = reading and (status == CHANGED or (status == UNKNOWN and item.matches_desired is False))
        device = plain_value(session, path, item.actual) if show_device else None
        if show_device and zero_filter(item):
            device = "all zeros (looks like a read error)"
        rows.append(
            {
                "path": path,
                "label": describe_path(session, path),
                "status": status,
                "css": CSS[status],
                "glyph": GLYPHS[status],
                "word": word(status, preset),
                "desired": plain_value(session, path, value),
                "applied": plain_value(session, path, state.applied.get(path), known=known),
                "device": device,
                "tech": {
                    "app": format_value(session, path, value),
                    "applied": format_value(session, path, state.applied.get(path), known=known),
                    "device": format_value(session, path, item.actual) if reading else None,
                },
                "reason": reasons.get(path),
                "confirm": push_confirmation(session, preset, path),
                "channel": _channel_key(path),
            }
        )
    return rows


def _state_list(counts: Mapping[str, int], preset: int) -> list[dict[str, Any]]:
    """The non-sent statuses of a channel, worst first: ``[{"state": "changed", "glyph": "≠", "text": "2 changed on
    device"}, …]``."""
    out = []
    for state in ORDER:
        n = counts.get(state, 0)
        if state == SENT or not n:
            continue
        text = {
            "failed": f"{n} failed",
            "changed": f"{n} changed on device",
            "sending": f"{n} sending",
            "waiting": f"{n} waiting for preset {preset + 1}",
            "unknown": f"{n} unknown",
        }[state]
        out.append({"state": state, "css": CSS[state], "glyph": GLYPHS[state], "text": text})
    return out


def sync_groups(
    session: DeviceSession, preset: int, rows: list[dict[str, Any]], view: StatusView | None
) -> list[dict[str, Any]]:
    """The table's rows grouped by channel (outputs first, then inputs), each with its status counts and, when the last
    check found changes on it, the paths for its "Use the device's values" and "Forget these values" buttons."""
    profile = session.profile
    keys = [("output", n) for n in range(profile.n_outputs)] + [("input", i) for i in range(profile.n_inputs)]
    counts: dict[tuple[str, int], dict[str, int]] = {k: {} for k in keys}
    for path, state in (view.states if view is not None else {}).items():
        key = _channel_key(path)
        if key in counts:
            counts[key][state] = counts[key].get(state, 0) + 1
    report = last_report(session.serial)
    acceptable = {i.path for i in report.acceptable} if report is not None and report.preset == preset else set()
    groups = []
    for kind, index in keys:
        mine = [r for r in rows if r["channel"] == (kind, index)]
        changed = [r["path"] for r in mine if r["status"] == CHANGED]
        use = [p for p in changed if p in acceptable]
        states = _state_list(counts[(kind, index)], preset)
        groups.append(
            {
                "id": f"sync-{kind}-{index}",
                "kind": kind,
                "index": index,
                "name": channel_label(session, kind, index),
                "rows": mine,
                "states": states,
                "problems": bool(states),
                "changed": changed,
                "use": use,
                "use_count": len(dict.fromkeys(undo_unit(p) for p in use)),
            }
        )
    return groups


def sync_table_context(request: Request, session: DeviceSession, preset: int, show: str) -> dict[str, Any]:
    show_all = show == "all"
    serial = session.serial
    view = status_view(session, preset)
    rows = sync_rows(session, preset, show_all=show_all, view=view)
    report = last_report(serial)
    return {
        "serial": serial,
        "preset": preset,
        "show": "all" if show_all else "unsynced",
        "rows": rows,
        "groups": sync_groups(session, preset, rows, view),
        "view": view,
        "counts": session.sync_summary(preset)["counts"],
        "problem_count": view.problems if view is not None else len(rows),
        "total": view.total if view is not None else len(rows),
        "checked": report is not None and report.preset == preset,
        "table_url": url(request, f"/ui/devices/{serial}/sync-table", preset=preset, show="all" if show_all else None),
        "all_url": url(request, f"/ui/devices/{serial}/sync-table", preset=preset, show="all"),
        "unsynced_url": url(request, f"/ui/devices/{serial}/sync-table", preset=preset),
        "push_path_url": url(
            request, f"/ui/devices/{serial}/presets/{preset}/paths/push", show="all" if show_all else None
        ),
        "confirm_url": url(request, f"/ui/devices/{serial}/verify/accept/confirm"),
        "mark_url": url(request, f"/ui/devices/{serial}/verify/mark-unknown"),
    }


def sync_panel_context(request: Request, session: DeviceSession, preset: int) -> dict[str, Any]:
    """The Device & sync status: the overview (`params.syncbar_context` plus the status view) and the action row. The
    table (`sync_table_context`) and the last check (`app.api.v1.verify`) load on their own."""
    bar = syncbar_context(request, session, preset, DEVICE_PAGE)
    serial = session.serial
    return {
        "bar": bar,
        "view": status_view(session, preset),
        "message": None,
        "verify_url": url(request, f"/ui/devices/{serial}/verify"),
        "check_supported": memory_map_for(session.profile.product_name) is not None,
    }


@router.get("/ui/devices/{serial}/sync-panel", response_class=HTMLResponse)
async def sync_panel(request: Request, serial: int, preset: int | None = None) -> Response:
    """The Device & sync status overview and its action row (Check device, Send all, Set up this device). ``preset``
    is 1-based."""
    session = await _session(request, serial)
    if isinstance(session, Response):
        return session
    p = resolve_preset(session, preset)
    if p is None:
        return PlainTextResponse(f"Unknown preset {preset}", status_code=404)
    return render(request, "partials/sync_panel.html", sync_panel_context(request, session, p))


@router.get("/ui/devices/{serial}/sync-table", response_class=HTMLResponse)
async def sync_table(request: Request, serial: int, preset: int, show: str = "unsynced") -> Response:
    """The sync table of one preset (0-based); ``show=all`` includes synced rows."""
    session = await _session(request, serial)
    if isinstance(session, Response):
        return session
    if not 0 <= preset < session.profile.n_presets:
        return PlainTextResponse(f"Unknown preset {preset}", status_code=404)
    return render(request, "partials/sync_table.html", {"t": sync_table_context(request, session, preset, show)})


@router.post("/ui/devices/{serial}/presets/{preset}/paths/push", response_class=HTMLResponse)
async def push_path(request: Request, serial: int, preset: int, show: str = "unsynced") -> Response:
    """Write one path again whatever the shadow says (a per-row "push"). Inactive presets stage it."""
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = await _session(request, serial)
    if isinstance(session, Response):
        return session
    if not 0 <= preset < session.profile.n_presets:
        return PlainTextResponse(f"Unknown preset {preset}", status_code=404)
    form = await request.form()
    path = form.get("path")
    values = session.writer.state(preset).values(session.profile)
    if not isinstance(path, str) or path not in values:
        ctx = {"t": sync_table_context(request, session, preset, show)}
        return render(request, "partials/sync_table.html", ctx, status_code=422, error="Unknown path")
    if push_confirmation(session, preset, path) and form.get("confirm") not in ("1", "true", "on"):
        ctx = {"t": sync_table_context(request, session, preset, show)}
        error = f"Pushing {path} needs confirmation"
        return render(request, "partials/sync_table.html", ctx, status_code=422, error=error)
    # Forget the shadow value and queue the path: the writer sends it on its next cycle (or when the preset is active).
    session.writer.invalidate(preset, [path])
    session.writer.notify(preset, [path])
    session.log_event("info", f"push {path} (preset {preset + 1})", kind="push", preset=preset, path=path)
    return render(request, "partials/sync_table.html", {"t": sync_table_context(request, session, preset, show)})


# --- adopt wizard -----------------------------------------------------------------------------------------------------


def adopt_context(
    request: Request, session: DeviceSession, *, form: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    serial = session.serial
    active = device_preset(session)
    defaults = {
        "mode": "trust",
        "source": "script",
        "preset": active if active is not None else 0,
        "script": "",
        "trust_all": False,
    }
    return {
        "serial": serial,
        "session": session,
        "adoption": session.adoption,
        "needs_adoption": session.needs_adoption,
        "presets": range(session.profile.n_presets),
        "default_preset": active if active is not None else 0,
        "active_preset": active,
        "sample": ADOPT_SAMPLE,
        "defaults_summary": factory_defaults_summary(session.profile),
        "modes": [m.value for m in AdoptMode],
        "preview_url": url(request, f"/ui/devices/{serial}/adopt/preview"),
        "adopt_url": url(request, f"/ui/devices/{serial}/adopt"),
        "mute_wrap": request.app.state.settings.mute_wrap_bulk_apply,
        "form": {**defaults, **(form or {})},
        "preview": None,
        "result": None,
        "errors": [],
        **extra,
    }


async def _script_from_form(form: FormData) -> str | None:
    """The script text: an uploaded file wins over the text area. None when the source isn't a script."""
    if form.get("source", "script") != "script":
        return None
    upload = form.get("script_file")
    if isinstance(upload, UploadFile) and upload.filename:
        data = await upload.read(MAX_SCRIPT_BYTES + 1)
        if len(data) > MAX_SCRIPT_BYTES:
            raise DesiredValidationError(f"the script file is larger than {MAX_SCRIPT_BYTES // 1000} kB")
        return data.decode("utf-8-sig", errors="replace")
    text = form.get("script")
    text = text if isinstance(text, str) else ""
    if len(text.encode()) > MAX_SCRIPT_BYTES:
        raise DesiredValidationError(f"the script is larger than {MAX_SCRIPT_BYTES // 1000} kB")
    if not text.strip():
        raise DesiredValidationError("paste a command script or choose a file (or start from the defaults)")
    return text


def preview_context(session: DeviceSession, parsed: ParsedScript) -> dict[str, Any]:
    total = len(device_values(default_config_doc(session.profile), session.profile))
    return {
        "ok": parsed.ok,
        "ops": len(parsed.ops),
        "device_paths": len(parsed.device_paths),
        "total_paths": total,
        "warnings": parsed.warnings,
        "errors": parsed.errors,
    }


def _form_snapshot(form: FormData, script: str | None) -> dict[str, Any]:
    """What the user entered, to re-render the wizard after an error."""
    raw_script = form.get("script")
    snapshot: dict[str, Any] = {
        "mode": form.get("mode") if form.get("mode") in {m.value for m in AdoptMode} else "trust",
        "source": "defaults" if form.get("source") == "defaults" else "script",
        "script": script if script is not None else (raw_script if isinstance(raw_script, str) else ""),
        "trust_all": form.get("trust_all") in ("on", "true", "1"),
    }
    preset = str(form.get("preset", ""))
    if preset.isdigit():
        snapshot["preset"] = int(preset)
    return snapshot


@router.get("/ui/devices/{serial}/adopt", response_class=HTMLResponse)
async def adopt_wizard(request: Request, serial: int) -> Response:
    session = await _session(request, serial)
    if isinstance(session, Response):
        return session
    return render(request, "partials/adopt.html", adopt_context(request, session))


@router.post("/ui/devices/{serial}/adopt/preview", response_class=HTMLResponse)
async def adopt_preview(request: Request, serial: int) -> Response:
    """Parse the script and show what it sets, plus warnings and errors. Nothing is stored or written."""
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = await _session(request, serial)
    if isinstance(session, Response):
        return session
    form = await request.form()
    try:
        script = await _script_from_form(form)
    except DesiredValidationError as exc:
        ctx = {"preview": None, "errors": exc.problems}
        return render(request, "partials/adopt_preview.html", ctx, status_code=422, error=str(exc))
    if script is None:
        return render(request, "partials/adopt_preview.html", {"preview": None, "defaults": True, "errors": []})
    parsed = session.parse_cli_script(script)
    ctx = {"preview": preview_context(session, parsed), "errors": [], "script_text": script}
    return render(request, "partials/adopt_preview.html", ctx)


def _parse_preset(session: DeviceSession, raw: Any) -> int:
    try:
        preset = int(str(raw))
    except (TypeError, ValueError):
        raise DesiredValidationError("preset: expected a number") from None
    if not 0 <= preset < session.profile.n_presets:
        raise DesiredValidationError(f"preset {preset + 1} doesn't exist")
    return preset


@router.post("/ui/devices/{serial}/adopt", response_class=HTMLResponse)
async def adopt(request: Request, serial: int) -> Response:
    """Run the adopt: ``trust`` writes nothing, ``unknown`` writes nothing, ``push`` writes the whole preset and needs
    the ``confirm`` checkbox. Trusting the defaults needs the ``confirm_defaults`` checkbox."""
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = await _session(request, serial)
    if isinstance(session, Response):
        return session
    form = await request.form()
    script: str | None = None
    try:
        script = await _script_from_form(form)
        preset = _parse_preset(session, form.get("preset"))
        mode = form.get("mode")
        if mode not in {m.value for m in AdoptMode}:
            raise DesiredValidationError("choose how to adopt (trust, push or unknown)")
        result = await session.adopt(
            str(mode),
            preset=preset,
            script=script,
            confirm=form.get("confirm") in ("on", "true", "1"),
            confirm_defaults=form.get("confirm_defaults") in ("on", "true", "1"),
            trust_all=form.get("trust_all") in ("on", "true", "1"),
            timeout=BULK_TIMEOUT_S,
        )
    except ConfirmationRequired as exc:
        ctx = adopt_context(request, session, form=_form_snapshot(form, script), errors=[str(exc)])
        return render(request, "partials/adopt.html", ctx, status_code=409, error=str(exc))
    except (DesiredValidationError, MasterValidationError) as exc:
        problems = exc.problems if isinstance(exc, DesiredValidationError) else [str(exc)]
        ctx = adopt_context(request, session, form=_form_snapshot(form, script), errors=problems)
        return render(request, "partials/adopt.html", ctx, status_code=422, error=str(exc).split(":")[0])
    except StateUnavailable as exc:
        ctx = adopt_context(request, session, form=_form_snapshot(form, script), errors=[str(exc)])
        return render(request, "partials/adopt.html", ctx, status_code=503, error=str(exc))
    flush = result.flush
    outcome = {
        "preset": result.preset,
        "mode": result.mode.value,
        "known": result.known,
        "unknown": result.unknown,
        "writes": result.writes,
        "read_back": result.read_back,
        "read_error": result.read_error,
        "warnings": result.warnings,
        "flush_ok": flush.ok if flush is not None else None,
        "flush_error": flush.error if flush is not None else None,
    }
    response = render(request, "partials/adopt.html", adopt_context(request, session, result=outcome))
    response.headers["HX-Trigger"] = json.dumps({SYNC_CHANGED: {"preset": result.preset}})
    return response


# --- event log --------------------------------------------------------------------------------------------------------


def _event_serial(data: Any) -> Any:
    return data.get("serial") if isinstance(data, dict) else None


@router.get("/ui/devices/{serial}/events", response_class=HTMLResponse)
async def event_log(request: Request, serial: int, limit: int = EVENT_LIMIT) -> Response:
    """The newest persisted events for this device (and app-wide ones), newest first."""
    session = manager_of(request).get(serial)
    if session is None:
        return PlainTextResponse(f"Unknown device {serial}", status_code=404)
    repo = repo_of(request, session)
    ctx: dict[str, Any] = {"events": [], "repo": repo is not None, "error": None}
    if repo is not None:
        try:
            events = await repo.list_events(limit=max(1, min(limit, EVENT_LIMIT)))
        except Exception as exc:
            ctx["error"] = str(exc) or type(exc).__name__
        else:
            ctx["events"] = [e for e in events if _event_serial(e.data) in (serial, None)]
    return render(request, "partials/event_log.html", ctx)


__all__ = [
    "confirmation_text",
    "describe_path",
    "format_value",
    "humanize_paths",
    "plain_value",
    "raise_label",
    "raise_rows",
    "router",
]
