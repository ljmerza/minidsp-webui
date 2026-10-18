"""HTML pages and the HTMX partials behind the persistent header.

Pages call the session layer directly (not the JSON API). Every master-control action returns the re-rendered
header fragment, with an HTTP status that reflects the outcome; on failure the message is also sent percent-encoded
in the ``X-Mdsp-Error`` response header so the page can show it as a toast even if the header swap is deferred.
"""

import math
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.datastructures import FormData

from app.daemon.client import DaemonError, DaemonUnavailable, DeviceNotFound
from app.sync.session import (
    ConfirmationRequired,
    DeviceSession,
    MasterValidationError,
    PresetSwitchInProgress,
    PresetSwitchTimeout,
    SessionManager,
    SessionState,
)
from app.sync.summary import session_summary
from app.web import devices, flow, glossary, shell
from app.web.nav import build_nav, nav_groups
from app.web.templating import graph_defaults, templates

router = APIRouter(include_in_schema=False)
router.include_router(flow.router)  # /ui/flow, the signal-flow diagram (phase 9 design §5)

ERROR_HEADER = "X-Mdsp-Error"

# "sync" and "desired" refresh the output cards (other tabs, Home Assistant, the writer); "master" a preset switch.
DASHBOARD_TOPICS = ("levels", "master", "device", "sync", "desired", "verify")
# One feed per page: each EventSource holds one of the browser's ~6 HTTP/1.1 connections per host, and the header
# (outside any nested feed) needs "sync" to keep its badge current while the sync table pushes.
DEVICE_TOPICS = ("master", "device", "sync", "log", "verify")


# --- context helpers --------------------------------------------------------------------------------------------------


def _manager(request: Request) -> SessionManager:
    return request.app.state.manager


def _pick_session(request: Request, serial: int | None) -> DeviceSession | None:
    return devices.pick_session(request, serial)


def connection(manager: SessionManager, session: DeviceSession | None) -> tuple[str, str]:
    """(level, label) for the header's connection indicator. Levels: ok, warn, bad, busy."""
    if session is None:
        if manager.daemon_up is None:
            return "warn", "Connecting to minidspd…"
        if not manager.daemon_up:
            return "bad", "minidspd unreachable"
        return "bad", "No supported device found"
    if session.preset_switching is not None:
        return "busy", f"Switching to preset {session.preset_switching + 1}…"
    state = SessionState(session.state)
    if state == SessionState.CONNECTED:
        if session.device_event().get("monitor_connected"):
            return "ok", "Connected"
        return "warn", "Connecting to device stream…"
    if state == SessionState.ABSENT:
        return "bad", "Device not found on minidspd"
    if state == SessionState.DAEMON_DOWN:
        return "bad", "minidspd unreachable"
    return "warn", "Starting…"


def header_context(request: Request, session: DeviceSession | None) -> dict[str, Any]:
    manager = _manager(request)
    settings = request.app.state.settings
    level, label = connection(manager, session)
    master = session.master if session else None
    profile = session.profile if session else None
    controls_enabled = (
        session is not None
        and master is not None
        and SessionState(session.state) == SessionState.CONNECTED
        and session.preset_switching is None
    )
    return {
        "session": session,
        "serial": session.serial if session else None,
        "profile": profile,
        "master": master,
        "conn_level": level,
        "conn_label": label,
        "controls_enabled": controls_enabled,
        "presets": range(profile.n_presets if profile else 0),
        "sources": list(profile.sources) if profile else [],
        "volume_confirm_db": settings.gain_step_confirm_db,
        "unsupported": [d.product_name or "unknown" for d in manager.unsupported],
        # The global shell (phase 9 design §3): the problems pill of the playing preset and the Help me… menu.
        "problems": shell.problems_view(request, session) if session is not None else None,
        "help_menu": shell.help_menu(request),
        "meters_href": shell.meters_href(request),
        "short_status": shell.short_status,
        **devices.header_extras(request, session),  # device switcher and storage banner
    }


def _url(request: Request, path: str, **params: Any) -> str:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    root = request.scope.get("root_path", "").rstrip("/")
    return f"{root}{path}" + (f"?{query}" if query else "")


def page_context(request: Request, session: DeviceSession | None, topics: tuple[str, ...]) -> dict[str, Any]:
    serial = session.serial if session else None
    nav = build_nav(
        request, session.profile if session else None, getattr(session, "channel_meta", None) if session else None
    )
    # Every page listens to the meters too: the header's CLIP light latches on any page (design §3.1).
    if "levels" not in topics:
        topics = (*topics, "levels")
    return {
        **header_context(request, session),
        "nav": nav,
        "nav_groups": nav_groups(nav),
        # Channel names for the header's CLIP light ("Shakers clipped").
        "clip_names": {
            "i": [c.label for c in _nav_children(nav, "/inputs")],
            "o": [c.label for c in _nav_children(nav, "/outputs")],
        },
        "sse_url": _url(request, "/api/v1/events", topics=",".join(topics), serial=serial),
        "header_url": _url(request, "/ui/header", serial=serial),
    }


# Same order and glyphs as the editing pages' sync badges (app/web/ui_common.py).
_SYNC_ORDER = ("failed", "pending", "unknown", "synced")
_SYNC_ICON = {"synced": "✓", "pending": "⟳", "failed": "⚠", "unknown": "?"}


async def output_cards(session: DeviceSession | None) -> list[dict[str, Any]] | None:
    """Per-output gain/delay/crossover of the active preset plus a worst-of sync badge; None until state is loaded.

    ``summary`` is the output's `OutputSummary` (its plain-language sentence and health badge).

    ``ceiling`` and ``gain_min`` bound the gain nudge buttons; ``never_synced`` (gain or mute never confirmed on the
    device) makes un-muting ask first, as on the output page.
    """
    if session is None or not hasattr(session, "ensure_loaded"):
        return None
    try:
        await session.ensure_loaded()
    except Exception:
        return None
    master = session.master
    preset = master.preset if master is not None and master.preset is not None else 0
    if not 0 <= preset < session.profile.n_presets:
        return None
    doc = session.desired(preset)
    sync = session.sync_status(preset)
    summary = session_summary(session, preset)  # the plain-language sentence and badge (phase 9 design 6)
    cards = []
    for i, out in enumerate(doc.outputs):
        prefix = f"outputs.{i}."
        statuses = {status for path, status in sync.items() if path.startswith(prefix)}
        worst = next((s for s in _SYNC_ORDER if s in statuses), "synced")
        cards.append(
            {
                "index": i,
                "gain_db": out.gain_db,
                "ceiling": session.channel_ceiling("output", i),
                "gain_min": session.profile.gain_min_db,
                "never_synced": any(sync.get(prefix + f, "unknown") == "unknown" for f in ("gain_db", "mute")),
                "mute": out.mute,
                "delay_ms": out.delay_ms,
                "crossover": "on" if any(not g.bypass for g in out.crossover) else "bypassed",
                "sync": worst,
                "sync_icon": _SYNC_ICON[worst],
                "summary": summary.outputs[i] if summary is not None and i < len(summary.outputs) else None,
            }
        )
    return cards


def _not_found_page(request: Request, serial: int) -> HTMLResponse:
    ctx = page_context(request, None, ("device",))
    ctx["missing_serial"] = serial
    return templates.TemplateResponse(request, "not_found.html", ctx, status_code=404)


# --- pages ------------------------------------------------------------------------------------------------------------


def _nav_children(nav: list[Any], path: str) -> tuple[Any, ...]:
    return next((item.children for item in nav if item.path == path), ())


async def dashboard_outputs_context(
    request: Request, session: DeviceSession | None, nav: list[Any] | None = None
) -> dict[str, Any]:
    """The dashboard's output cards: names and edit links from the nav, values of the active preset."""
    if nav is None:
        profile = session.profile if session else None
        nav = build_nav(request, profile, getattr(session, "channel_meta", None) if session else None)
    outputs_nav = _nav_children(nav, "/outputs")
    cards = await output_cards(session)
    master = session.master if session else None
    # Quick controls edit the active preset, so they need to know which one that is.
    preset = master.preset if cards is not None and master is not None else None
    serial = session.serial if session else None
    return {
        "output_names": [child.label for child in outputs_nav],  # names saved in Settings, else "Out 1"
        "output_links": [child.path if child.enabled else None for child in outputs_nav],
        "output_state": cards,
        "outputs_preset": preset,
        "outputs_master_mute": master.mute if cards is not None and master is not None else None,  # badges say Silent
        "outputs_edit_url": None if preset is None else _url(request, f"/ui/devices/{serial}/presets/{preset}/edit"),
        "outputs_url": _url(request, "/ui/dashboard/outputs", serial=serial),
    }


@router.get("/", response_class=HTMLResponse, name="dashboard")
async def dashboard(request: Request, serial: int | None = None) -> HTMLResponse:
    session = _pick_session(request, serial)
    if serial is not None and session is None:
        return _not_found_page(request, serial)
    ctx = page_context(request, session, DASHBOARD_TOPICS)
    status = session.status if session else None
    ctx.update(
        title="Dashboard",
        input_names=[child.label for child in _nav_children(ctx["nav"], "/inputs")],
        response_view=graph_defaults(request)["view"] or "sub",
        initial_levels={
            "i": list(status.input_levels) if status else [],
            "o": list(status.output_levels) if status else [],
        },
        **await dashboard_outputs_context(request, session, ctx["nav"]),
        **await flow.flow_context(request, session),
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx)


def device_info_context(request: Request, session: DeviceSession | None) -> dict[str, Any]:
    manager = _manager(request)
    return {
        "info": session.info() if session else None,
        "daemon_url": request.app.state.settings.minidspd_url,
        "daemon_up": manager.daemon_up,
        "daemon_version": getattr(manager, "daemon_version", None),
        "daemon_error": manager.last_error,
        "unsupported_devices": list(manager.unsupported),
        "device_info_url": _url(request, "/ui/device-info", serial=session.serial if session else None),
    }


@router.get("/device", response_class=HTMLResponse, name="device_page")
async def device_page(request: Request, serial: int | None = None) -> HTMLResponse:
    session = _pick_session(request, serial)
    if serial is not None and session is None:
        return _not_found_page(request, serial)
    ctx = page_context(request, session, DEVICE_TOPICS)
    ctx.update(
        title="Device & sync", sync_preset=_shown_preset(request, session), **device_info_context(request, session)
    )
    return templates.TemplateResponse(request, "device.html", ctx)


def _shown_preset(request: Request, session: DeviceSession | None) -> int | None:
    """The 0-based preset the Device & sync page shows: ``?preset=`` (1-based), else the playing one, else the first.
    None for an unknown preset (the sync panel then answers 404)."""
    if session is None:
        return None
    raw = request.query_params.get("preset")
    if raw is None or not raw.isdigit():
        playing = session.master.preset if session.master is not None else None
        return playing if isinstance(playing, int) and 0 <= playing < session.profile.n_presets else 0
    preset = int(raw) - 1
    return preset if 0 <= preset < session.profile.n_presets else None


# The header (master, device) is the only live part of the glossary page.
HELP_TOPICS = ("master", "device")


@router.get("/help", response_class=HTMLResponse, name="help_page")
async def help_page(request: Request) -> HTMLResponse:
    """The glossary (design §13): every term from app/web/glossary.py, grouped, each with an ``id`` anchor that the
    toggletips' "More →" links point at. Plain HTML, so it works without JavaScript."""
    session = _pick_session(request, None)
    ctx = page_context(request, session, HELP_TOPICS)
    ctx.update(title="Glossary", groups=glossary.grouped(), terms=glossary.TERMS)
    return templates.TemplateResponse(request, "help.html", ctx)


# --- partials ---------------------------------------------------------------------------------------------------------


def header_response(
    request: Request, session: DeviceSession | None, *, status_code: int = 200, error: str | None = None
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request, "partials/header.html", header_context(request, session), status_code=status_code
    )
    response.headers["Cache-Control"] = "no-store"
    if error:
        response.headers[ERROR_HEADER] = quote(error, safe=" ")
    return response


@router.get("/ui/header", response_class=HTMLResponse)
async def header_fragment(request: Request, serial: int | None = None) -> HTMLResponse:
    return header_response(request, _pick_session(request, serial))


@router.get("/ui/dashboard/outputs", response_class=HTMLResponse)
async def dashboard_outputs_fragment(request: Request, serial: int | None = None) -> HTMLResponse:
    """The dashboard's output cards, re-fetched on SSE ``sync`` / ``desired`` events and active-preset changes."""
    session = _pick_session(request, serial)
    ctx = await dashboard_outputs_context(request, session)
    response = templates.TemplateResponse(request, "partials/dashboard_outputs.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/ui/device-info", response_class=HTMLResponse)
async def device_info_fragment(request: Request, serial: int | None = None) -> HTMLResponse:
    session = _pick_session(request, serial)
    response = templates.TemplateResponse(request, "partials/device_info.html", device_info_context(request, session))
    response.headers["Cache-Control"] = "no-store"
    return response


Action = Callable[[DeviceSession, FormData], Awaitable[Any]]


def _error_status(exc: Exception) -> int:
    match exc:
        case ConfirmationRequired():
            return 409
        case MasterValidationError():
            return 422
        case PresetSwitchInProgress():
            return 409
        case PresetSwitchTimeout():
            return 504
        case DaemonUnavailable() | DeviceNotFound():
            return 503
        case _:  # DaemonRejected and anything else the daemon threw at us
            return 502


async def _master_action(request: Request, serial: int, action: Action) -> Response:
    # Requiring the HTMX header keeps plain cross-site form posts from changing the volume.
    if request.headers.get("HX-Request") != "true":
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = _manager(request).get(serial)
    if session is None:
        return header_response(request, None, status_code=404, error=f"Unknown device {serial}")
    form = await request.form()
    try:
        await action(session, form)
    except (MasterValidationError, PresetSwitchInProgress, PresetSwitchTimeout, DaemonError) as exc:
        if not isinstance(exc, MasterValidationError):
            session.log_event("warning", f"master control failed: {exc}")
        return header_response(request, session, status_code=_error_status(exc), error=str(exc) or type(exc).__name__)
    return header_response(request, session)


def _form_float(form: FormData, name: str) -> float:
    raw = form.get(name)
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        raise MasterValidationError(f"{name}: expected a number") from None
    if not math.isfinite(value):
        raise MasterValidationError(f"{name}: expected a finite number")
    return value


def _form_int(form: FormData, name: str) -> int:
    raw = form.get(name)
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        raise MasterValidationError(f"{name}: expected an integer") from None


def _form_bool(form: FormData, name: str) -> bool | None:
    raw = form.get(name)
    if raw is None or raw == "":
        return None
    lowered = str(raw).strip().lower()
    if lowered in ("true", "1", "on", "yes"):
        return True
    if lowered in ("false", "0", "off", "no"):
        return False
    raise MasterValidationError(f"{name}: expected true or false")


@router.post("/ui/devices/{serial}/master/volume", response_class=HTMLResponse)
async def set_volume(request: Request, serial: int) -> Response:
    async def action(session: DeviceSession, form: FormData) -> None:
        # master.js asks before a big raise or drop and then sends confirm=true; the session enforces the guard.
        await session.set_volume(_form_float(form, "volume"), confirm=_form_bool(form, "confirm") is True, source="ui")

    return await _master_action(request, serial, action)


@router.post("/ui/devices/{serial}/master/volume/{direction}", response_class=HTMLResponse)
async def step_volume(request: Request, serial: int, direction: Literal["up", "down"]) -> Response:
    async def action(session: DeviceSession, _: FormData) -> None:
        await session.volume_step(1 if direction == "up" else -1, source="ui")

    return await _master_action(request, serial, action)


@router.post("/ui/devices/{serial}/master/mute", response_class=HTMLResponse)
async def set_mute(request: Request, serial: int) -> Response:
    """``mute=true|false`` sets the state explicitly; without it the mute is toggled."""

    async def action(session: DeviceSession, form: FormData) -> None:
        mute = _form_bool(form, "mute")
        if mute is None:
            await session.toggle_mute(source="ui")
        else:
            await session.set_mute(mute, source="ui")

    return await _master_action(request, serial, action)


@router.post("/ui/devices/{serial}/master/source", response_class=HTMLResponse)
async def set_source(request: Request, serial: int) -> Response:
    async def action(session: DeviceSession, form: FormData) -> None:
        source = form.get("source")
        if not isinstance(source, str) or not source:
            raise MasterValidationError("source: missing")
        await session.set_source(source, source="ui")

    return await _master_action(request, serial, action)


@router.post("/ui/devices/{serial}/master/preset", response_class=HTMLResponse)
async def set_preset(request: Request, serial: int) -> Response:
    """``preset`` is the 0-based device preset index; the UI labels it 1-4."""

    async def action(session: DeviceSession, form: FormData) -> None:
        await session.set_preset(_form_int(form, "preset"), source="ui")

    return await _master_action(request, serial, action)
