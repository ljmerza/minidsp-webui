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
from app.web.nav import build_nav
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

ERROR_HEADER = "X-Mdsp-Error"

DASHBOARD_TOPICS = ("levels", "master", "device", "sync")
DEVICE_TOPICS = ("master", "device")


# --- context helpers --------------------------------------------------------------------------------------------------


def _manager(request: Request) -> SessionManager:
    return request.app.state.manager


def _pick_session(request: Request, serial: int | None) -> DeviceSession | None:
    manager = _manager(request)
    return manager.get(serial) if serial is not None else manager.default()


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
    }


def _url(request: Request, path: str, **params: Any) -> str:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    root = request.scope.get("root_path", "").rstrip("/")
    return f"{root}{path}" + (f"?{query}" if query else "")


def page_context(request: Request, session: DeviceSession | None, topics: tuple[str, ...]) -> dict[str, Any]:
    serial = session.serial if session else None
    return {
        **header_context(request, session),
        "nav": build_nav(
            request, session.profile if session else None, getattr(session, "channel_meta", None) if session else None
        ),
        "sse_url": _url(request, "/api/v1/events", topics=",".join(topics), serial=serial),
        "header_url": _url(request, "/ui/header", serial=serial),
    }


# Same order and glyphs as the editing pages' sync badges (app/web/ui_common.py).
_SYNC_ORDER = ("failed", "pending", "unknown", "synced")
_SYNC_ICON = {"synced": "✓", "pending": "⟳", "failed": "⚠", "unknown": "?"}


async def output_cards(session: DeviceSession | None) -> list[dict[str, Any]] | None:
    """Per-output gain/delay/crossover of the active preset plus a worst-of sync badge; None until state is loaded."""
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
    cards = []
    for i, out in enumerate(doc.outputs):
        prefix = f"outputs.{i}."
        statuses = {status for path, status in sync.items() if path.startswith(prefix)}
        worst = next((s for s in _SYNC_ORDER if s in statuses), "synced")
        cards.append(
            {
                "gain_db": out.gain_db,
                "mute": out.mute,
                "delay_ms": out.delay_ms,
                "crossover": "on" if any(not g.bypass for g in out.crossover) else "bypassed",
                "sync": worst,
                "sync_icon": _SYNC_ICON[worst],
            }
        )
    return cards


def _not_found_page(request: Request, serial: int) -> HTMLResponse:
    ctx = page_context(request, None, ("device",))
    ctx["missing_serial"] = serial
    return templates.TemplateResponse(request, "not_found.html", ctx, status_code=404)


# --- pages ------------------------------------------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, name="dashboard")
async def dashboard(request: Request, serial: int | None = None) -> HTMLResponse:
    session = _pick_session(request, serial)
    if serial is not None and session is None:
        return _not_found_page(request, serial)
    ctx = page_context(request, session, DASHBOARD_TOPICS)
    status = session.status if session else None
    outputs_nav = next((item.children for item in ctx["nav"] if item.path == "/outputs"), ())
    inputs_nav = next((item.children for item in ctx["nav"] if item.path == "/inputs"), ())
    ctx.update(
        title="Dashboard",
        output_links=[child.path if child.enabled else None for child in outputs_nav],
        input_names=[child.label for child in inputs_nav],  # names saved in Settings, else "In 1" / "Out 1"
        output_names=[child.label for child in outputs_nav],
        output_state=await output_cards(session),
        initial_levels={
            "i": list(status.input_levels) if status else [],
            "o": list(status.output_levels) if status else [],
        },
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx)


def device_info_context(request: Request, session: DeviceSession | None) -> dict[str, Any]:
    manager = _manager(request)
    return {
        "info": session.info() if session else None,
        "daemon_url": request.app.state.settings.minidspd_url,
        "daemon_up": manager.daemon_up,
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
    ctx.update(title="Device", **device_info_context(request, session))
    return templates.TemplateResponse(request, "device.html", ctx)


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
        raise MasterValidationError(f"{name}: expected a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise MasterValidationError(f"{name}: expected a finite number")
    return value


def _form_int(form: FormData, name: str) -> int:
    raw = form.get(name)
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        raise MasterValidationError(f"{name}: expected an integer, got {raw!r}") from None


def _form_bool(form: FormData, name: str) -> bool | None:
    raw = form.get(name)
    if raw is None or raw == "":
        return None
    lowered = str(raw).strip().lower()
    if lowered in ("true", "1", "on", "yes"):
        return True
    if lowered in ("false", "0", "off", "no"):
        return False
    raise MasterValidationError(f"{name}: expected true or false, got {raw!r}")


@router.post("/ui/devices/{serial}/master/volume", response_class=HTMLResponse)
async def set_volume(request: Request, serial: int) -> Response:
    async def action(session: DeviceSession, form: FormData) -> None:
        # master.js asks before a big raise and then sends confirm=true; the session enforces the guard.
        await session.set_volume(_form_float(form, "volume"), confirm=_form_bool(form, "confirm") is True)

    return await _master_action(request, serial, action)


@router.post("/ui/devices/{serial}/master/volume/{direction}", response_class=HTMLResponse)
async def step_volume(request: Request, serial: int, direction: Literal["up", "down"]) -> Response:
    async def action(session: DeviceSession, _: FormData) -> None:
        await session.volume_step(1 if direction == "up" else -1)

    return await _master_action(request, serial, action)


@router.post("/ui/devices/{serial}/master/mute", response_class=HTMLResponse)
async def set_mute(request: Request, serial: int) -> Response:
    """``mute=true|false`` sets the state explicitly; without it the mute is toggled."""

    async def action(session: DeviceSession, form: FormData) -> None:
        mute = _form_bool(form, "mute")
        if mute is None:
            await session.toggle_mute()
        else:
            await session.set_mute(mute)

    return await _master_action(request, serial, action)


@router.post("/ui/devices/{serial}/master/source", response_class=HTMLResponse)
async def set_source(request: Request, serial: int) -> Response:
    async def action(session: DeviceSession, form: FormData) -> None:
        source = form.get("source")
        if not isinstance(source, str) or not source:
            raise MasterValidationError("source: missing")
        await session.set_source(source)

    return await _master_action(request, serial, action)


@router.post("/ui/devices/{serial}/master/preset", response_class=HTMLResponse)
async def set_preset(request: Request, serial: int) -> Response:
    """``preset`` is the 0-based device preset index; the UI labels it 1-4."""

    async def action(session: DeviceSession, form: FormData) -> None:
        await session.set_preset(_form_int(form, "preset"))

    return await _master_action(request, serial, action)
