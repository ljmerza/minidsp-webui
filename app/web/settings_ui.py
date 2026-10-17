"""Settings page (docs 05 §9): channel names and colours, per-output gain ceilings, the big-step threshold and the
sync options.

Sync options and the threshold are app-wide runtime settings. They start from the environment (``app.config``);
values saved here are stored in the ``setting`` table under `RUNTIME_KEY` and override the environment, both
immediately (the shared `Settings` object is updated in place, which the sessions read on every use) and on the next
start through `apply_stored_runtime_settings`.
"""

import logging
import math
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import FormData

from app.api.deps import require_same_origin
from app.config import Settings
from app.state import Repo
from app.sync import fir as fir_svc
from app.sync.session import DesiredValidationError, DeviceSession, SessionManager
from app.web.ui_common import (
    KINDS,
    channel_color,
    default_color,
    default_label,
    edit_page_context,
    is_htmx,
    load_state,
    manager_of,
    not_found_page,
    pick_session,
    render,
    repo_of,
    url,
)

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_same_origin)])

RUNTIME_KEY = "ui.runtime_settings"
MAX_NAME_LEN = 32


class RuntimeSettings(BaseModel):
    """The app-wide settings the Settings page can change at runtime."""

    gain_step_confirm_db: float = Field(ge=1.0, le=24.0)
    write_coalesce_ms: int = Field(ge=0, le=1000)
    mute_wrap_bulk_apply: bool
    reapply_on_reconnect: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> "RuntimeSettings":
        return cls.model_construct(**{name: getattr(settings, name) for name in cls.model_fields})


def apply_runtime(settings: Settings, values: RuntimeSettings, manager: SessionManager | None = None) -> None:
    """Update the shared settings object in place, and the writers' gather window."""
    for name in RuntimeSettings.model_fields:
        setattr(settings, name, getattr(values, name))
    if manager is not None:
        for session in manager:
            session.writer.coalesce_s = values.write_coalesce_ms / 1000.0


async def apply_stored_runtime_settings(repo: Repo, settings: Settings) -> bool:
    """Startup hook: apply the values saved on the Settings page (if any) before the sessions are created.

    Returns whether stored values were applied. A broken stored value is logged and ignored.
    """
    stored = await repo.get_setting(RUNTIME_KEY)
    if not isinstance(stored, dict):
        return False
    try:
        values = RuntimeSettings.model_validate({**RuntimeSettings.from_settings(settings).model_dump(), **stored})
    except ValidationError as exc:
        log.warning("ignoring stored runtime settings: %s", exc)
        return False
    apply_runtime(settings, values)
    return True


# --- page -------------------------------------------------------------------------------------------------------------


def _channel_rows(session: DeviceSession) -> list[dict[str, Any]]:
    rows = []
    for kind in KINDS:
        count = session.profile.n_inputs if kind == "input" else session.profile.n_outputs
        for idx in range(count):
            meta = session.channel_meta.get((kind, idx))
            rows.append(
                {
                    "kind": kind,
                    "idx": idx,
                    "key": f"{kind}-{idx}",
                    "default_name": default_label(kind, idx),
                    "name": meta.name if meta is not None and meta.name else "",
                    "color": channel_color(session, kind, idx),
                    "default_color": default_color(kind, idx),
                    "max_gain_db": meta.max_gain_db if meta is not None else None,
                }
            )
    return rows


def channels_context(request: Request, session: DeviceSession, **extra: Any) -> dict[str, Any]:
    p = session.profile
    return {
        "serial": session.serial,
        "rows": _channel_rows(session),
        "gain_min": p.gain_min_db,
        "gain_max": p.gain_max_db,
        "channels_url": url(request, f"/ui/devices/{session.serial}/channels"),
        "errors": [],
        "saved": False,
        **extra,
    }


def runtime_context(request: Request, **extra: Any) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return {
        "current": RuntimeSettings.from_settings(settings),
        "runtime_url": url(request, "/ui/settings/runtime"),
        "persisted": repo_of(request) is not None,
        "errors": [],
        "saved": False,
        **extra,
    }


def mqtt_context(request: Request, session: DeviceSession | None) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    bridge = getattr(request.app.state, "mqtt", None)
    connected = bool(bridge is not None and bridge.connected.is_set())
    return {
        "mqtt_enabled": bool(settings.mqtt_enabled and settings.mqtt_host),
        "mqtt_running": bridge is not None,
        "mqtt_connected": connected,
        "mqtt_error": getattr(bridge, "last_error", None) if bridge is not None else None,
        "mqtt_remove_url": url(request, f"/ui/devices/{session.serial}/mqtt/remove") if session else None,
        "mqtt_message": None,
    }


def fir_context(request: Request, **extra: Any) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return {
        "enabled": fir_svc.fir_experimental(settings),
        "fir_url": url(request, "/ui/settings/fir"),
        "persisted": repo_of(request) is not None,
        "error": None,
        "saved": False,
        **extra,
    }


@router.get("/settings", response_class=HTMLResponse, name="settings_page")
async def settings_page(request: Request, serial: int | None = None) -> Response:
    session = pick_session(request, serial)
    if serial is not None and session is None:
        return not_found_page(request, serial)
    ctx = edit_page_context(request, session, "Settings")
    ctx.update(runtime=runtime_context(request), mqtt=mqtt_context(request, session), fir=fir_context(request))
    if session is not None:
        error = await load_state(session)
        if error:
            ctx["state_error"] = error
        else:
            ctx["c"] = channels_context(request, session)
    return render(request, "settings.html", ctx)


# --- channel names / colours / ceilings -------------------------------------------------------------------------------


def _form_text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _parse_ceiling(raw: str, session: DeviceSession, label: str) -> float | None:
    if raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        raise DesiredValidationError(f"{label}: gain ceiling must be a number, got {raw!r}") from None
    p = session.profile
    if not (math.isfinite(value) and p.gain_min_db <= value <= p.gain_max_db):
        raise DesiredValidationError(f"{label}: gain ceiling must be within {p.gain_min_db:g}..{p.gain_max_db:g} dB")
    return value


async def _save_channel(session: DeviceSession, kind: str, idx: int, form: FormData) -> None:
    key = f"{kind}-{idx}"
    label = default_label(kind, idx)
    meta = session.channel_meta.get((kind, idx))
    name = _form_text(form, f"{key}-name")
    if len(name) > MAX_NAME_LEN:
        raise DesiredValidationError(f"{label}: names are at most {MAX_NAME_LEN} characters")
    color = _form_text(form, f"{key}-color").lower() or None
    if color == default_color(kind, idx) and (meta is None or meta.color is None):
        color = None  # untouched default: keep following the palette
    if kind == "output":
        ceiling = _parse_ceiling(_form_text(form, f"{key}-max_gain_db"), session, label)
    else:
        ceiling = meta.max_gain_db if meta is not None else None
    current = (meta.name, meta.color, meta.max_gain_db) if meta is not None else (None, None, None)
    if (name or None, color, ceiling) == current:
        return  # unchanged: no database write
    await session.set_channel_meta(kind, idx, name=name or None, color=color, max_gain_db=ceiling)


@router.post("/ui/devices/{serial}/channels", response_class=HTMLResponse)
async def save_channels(request: Request, serial: int) -> Response:
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = manager_of(request).get(serial)
    if session is None:
        return PlainTextResponse(f"Unknown device {serial}", status_code=404)
    if error := await load_state(session):
        return PlainTextResponse(error, status_code=503)
    form = await request.form()
    errors: list[str] = []
    for kind in KINDS:
        count = session.profile.n_inputs if kind == "input" else session.profile.n_outputs
        for idx in range(count):
            try:
                await _save_channel(session, kind, idx, form)
            except DesiredValidationError as exc:
                errors.extend(exc.problems)
    ctx = {"c": channels_context(request, session, errors=errors, saved=not errors)}
    if errors:
        return render(request, "partials/settings_channels.html", ctx, status_code=422, error="; ".join(errors))
    return render(request, "partials/settings_channels.html", ctx)


@router.post("/ui/devices/{serial}/channels/{kind}/{idx}/name", response_class=HTMLResponse)
async def rename_channel(request: Request, serial: int, kind: str, idx: int) -> Response:
    """Inline rename from a channel page's header strip; keeps the colour and ceiling."""
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = manager_of(request).get(serial)
    if session is None:
        return PlainTextResponse(f"Unknown device {serial}", status_code=404)
    if error := await load_state(session):
        return PlainTextResponse(error, status_code=503)
    if kind not in KINDS:
        return PlainTextResponse(f"Unknown channel kind {kind!r}", status_code=404)
    name = _form_text(await request.form(), "name")
    meta = session.channel_meta.get((kind, idx))
    error = None
    if len(name) > MAX_NAME_LEN:
        error = f"Names are at most {MAX_NAME_LEN} characters"
    else:
        try:
            if (name or None) != (meta.name if meta is not None else None):
                await session.set_channel_meta(
                    kind,
                    idx,
                    name=name or None,
                    color=meta.color if meta is not None else None,
                    max_gain_db=meta.max_gain_db if meta is not None else None,
                )
        except DesiredValidationError as exc:
            error = str(exc)
    meta = session.channel_meta.get((kind, idx))
    ctx = {
        "channel": {
            "kind": kind,
            "index": idx,
            "custom_name": meta.name if meta is not None and meta.name else "",
            "name": (meta.name if meta is not None and meta.name else None) or default_label(kind, idx),
            "name_url": url(request, f"/ui/devices/{serial}/channels/{kind}/{idx}/name"),
        },
        "name_error": error,
    }
    status = 422 if error else 200
    response = render(request, "partials/channel_name.html", ctx, status_code=status, error=error)
    return response


# --- runtime settings -------------------------------------------------------------------------------------------------


def _runtime_from_form(form: FormData) -> RuntimeSettings:
    def number(name: str) -> str:
        return _form_text(form, name)

    return RuntimeSettings.model_validate(
        {
            "gain_step_confirm_db": number("gain_step_confirm_db"),
            "write_coalesce_ms": number("write_coalesce_ms"),
            "mute_wrap_bulk_apply": form.get("mute_wrap_bulk_apply") in ("on", "true", "1"),
            "reapply_on_reconnect": form.get("reapply_on_reconnect") in ("on", "true", "1"),
        }
    )


@router.post("/ui/settings/runtime", response_class=HTMLResponse)
async def save_runtime(request: Request) -> Response:
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    form = await request.form()
    try:
        values = _runtime_from_form(form)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        ctx = {"runtime": runtime_context(request, errors=errors)}
        return render(request, "partials/settings_runtime.html", ctx, status_code=422, error="; ".join(errors))
    settings: Settings = request.app.state.settings
    before = RuntimeSettings.from_settings(settings).model_dump()
    repo = repo_of(request)
    if repo is not None:
        try:
            await repo.set_setting(RUNTIME_KEY, values.model_dump())
        except Exception as exc:
            message = f"Saving failed: {exc}"
            ctx = {"runtime": runtime_context(request, errors=[message])}
            return render(request, "partials/settings_runtime.html", ctx, status_code=503, error=message)
    apply_runtime(settings, values, manager_of(request))
    changed = {k: v for k, v in values.model_dump().items() if before.get(k) != v}
    if changed:
        session = manager_of(request).default()
        if session is not None:
            session.log_event("info", f"settings changed: {changed}", kind="settings", changes=changed)
    return render(request, "partials/settings_runtime.html", {"runtime": runtime_context(request, saved=True)})


@router.post("/ui/settings/fir", response_class=HTMLResponse)
async def save_fir(request: Request) -> Response:
    """The experimental FIR toggle (also at ``PUT /api/v1/fir/capability``). The page reloads so the tab follows."""
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    form = await request.form()
    enabled = form.get("fir_experimental") is not None
    settings: Settings = request.app.state.settings
    before = fir_svc.fir_experimental(settings)
    manager = manager_of(request)
    try:
        await fir_svc.set_fir_capability(enabled, settings=settings, sessions=manager, repo=repo_of(request))
    except Exception as exc:
        message = f"Saving failed: {exc}"
        ctx = {"fir": fir_context(request, error=message)}
        return render(request, "partials/settings_fir.html", ctx, status_code=503, error=message)
    if before != enabled:
        session = manager.default()
        if session is not None:
            state = "on" if enabled else "off"
            session.log_event("info", f"experimental FIR turned {state}", kind="settings", changes={"fir": enabled})
    response = render(request, "partials/settings_fir.html", {"fir": fir_context(request, saved=True)})
    if before != enabled:
        response.headers["HX-Refresh"] = "true"  # output pages and the nav pick up the FIR tab on the next render
    return response


# --- Home Assistant ---------------------------------------------------------------------------------------------------


@router.post("/ui/devices/{serial}/mqtt/remove", response_class=HTMLResponse)
async def mqtt_remove(request: Request, serial: int) -> Response:
    """Remove this device's entities from Home Assistant (they come back on the next reconnect or restart)."""
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = manager_of(request).get(serial)
    if session is None:
        return PlainTextResponse(f"Unknown device {serial}", status_code=404)
    bridge = getattr(request.app.state, "mqtt", None)
    ctx = mqtt_context(request, session)
    removed = bool(bridge is not None and await bridge.remove_device(serial))
    if removed:
        session.log_event("info", "removed the device's entities from Home Assistant", kind="mqtt")
        ctx["mqtt_message"] = "Removed. The entities come back after the next MQTT reconnect or app restart."
        return render(request, "partials/settings_mqtt.html", {"mqtt": ctx})
    message = "MQTT is disabled or not connected; nothing was removed."
    ctx["mqtt_message"] = message
    return render(request, "partials/settings_mqtt.html", {"mqtt": ctx}, status_code=409, error=message)


__all__ = ["RUNTIME_KEY", "RuntimeSettings", "apply_runtime", "apply_stored_runtime_settings", "router"]
