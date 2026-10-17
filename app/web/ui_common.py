"""Shared helpers for the preset-editing pages (routing, inputs, outputs, device sync, settings).

URL conventions: page URLs are 1-based like the labels (``/outputs/1?preset=2`` is "Out 1" of "Preset 2");
``/ui/…`` action and partial routes use the 0-based indexes of the session layer and the JSON API.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.state import Repo
from app.sync.session import DeviceSession, SessionManager, StateUnavailable
from app.web import devices
from app.web.routes import page_context
from app.web.templating import templates

ERROR_HEADER = "X-Mdsp-Error"

# Live topics for the editing pages: the header (master, device) plus sync status and desired-state broadcasts.
EDIT_TOPICS = ("master", "device", "sync", "desired")

KINDS = ("input", "output")
STATUS_ORDER = ("failed", "pending", "unknown", "synced")  # worst first
STATUS_ICON = {"synced": "✓", "pending": "⟳", "failed": "⚠", "unknown": "?"}
STATUS_TEXT = {"synced": "synced", "pending": "pending", "failed": "failed", "unknown": "unknown"}

# Default channel colours (Settings can override them), one per channel kind and index.
DEFAULT_COLORS = {
    "input": ("#4ea1ff", "#a78bfa"),
    "output": ("#3ccf7a", "#f0b429", "#ff7a59", "#22c3c3"),
}
FALLBACK_COLOR = "#8c96a3"


def manager_of(request: Request) -> SessionManager:
    return request.app.state.manager


def pick_session(request: Request, serial: int | None) -> DeviceSession | None:
    """An explicit serial, else the header switcher's choice, else the first device (see app.web.devices)."""
    return devices.pick_session(request, serial)


def repo_of(request: Request, session: DeviceSession | None = None) -> Repo | None:
    repo = getattr(request.app.state, "repo", None)
    if repo is None and session is not None:
        repo = session.repo
    if repo is None:
        repo = getattr(manager_of(request), "repo", None)
    return repo


def url(request: Request, path: str, **params: Any) -> str:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    root = request.scope.get("root_path", "").rstrip("/")
    return f"{root}{path}" + (f"?{query}" if query else "")


def default_label(kind: str, index: int) -> str:
    return f"{'In' if kind == 'input' else 'Out'} {index + 1}"


def channel_label(session: DeviceSession | None, kind: str, index: int) -> str:
    """The name from Settings ("Shakers"), else "In 1" / "Out 1"."""
    meta = getattr(session, "channel_meta", {}).get((kind, index)) if session is not None else None
    return (meta.name if meta is not None and meta.name else None) or default_label(kind, index)


def default_color(kind: str, index: int) -> str:
    palette = DEFAULT_COLORS.get(kind, ())
    return palette[index] if index < len(palette) else FALLBACK_COLOR


def channel_color(session: DeviceSession | None, kind: str, index: int) -> str:
    meta = getattr(session, "channel_meta", {}).get((kind, index)) if session is not None else None
    return (meta.color if meta is not None and meta.color else None) or default_color(kind, index)


def channel_names(session: DeviceSession, kind: str) -> list[str]:
    count = session.profile.n_inputs if kind == "input" else session.profile.n_outputs
    return [channel_label(session, kind, i) for i in range(count)]


def worst_status(statuses: list[str]) -> str:
    for status in STATUS_ORDER:
        if status in statuses:
            return status
    return "synced"


def device_preset(session: DeviceSession) -> int | None:
    master = session.master
    return master.preset if master is not None else None


def resolve_preset(session: DeviceSession, preset_1based: int | None) -> int | None:
    """0-based preset from a 1-based ``?preset=`` (default: the active preset, else the first). None if invalid."""
    if preset_1based is None:
        active = device_preset(session)
        return active if active is not None and 0 <= active < session.profile.n_presets else 0
    preset = preset_1based - 1
    return preset if 0 <= preset < session.profile.n_presets else None


@dataclass(frozen=True)
class PresetTab:
    index: int  # 0-based
    label: str
    active: bool  # the device's active preset
    selected: bool  # the preset this page edits
    staged: int  # changes waiting for this (inactive) preset to become active
    failed: int


def preset_tabs(session: DeviceSession, selected: int) -> list[PresetTab]:
    active = device_preset(session)
    tabs = []
    for p in range(session.profile.n_presets):
        summary = session.sync_summary(p) if session.loaded else None
        counts = summary["counts"] if summary else {}
        staged = counts.get("pending", 0) if p != active else 0
        tabs.append(PresetTab(p, f"Preset {p + 1}", p == active, p == selected, staged, counts.get("failed", 0)))
    return tabs


async def load_state(session: DeviceSession) -> str | None:
    """Make sure the preset state is loaded; returns an error message when it can't be."""
    try:
        await session.ensure_loaded()
    except StateUnavailable as exc:
        return str(exc)
    return None


def edit_page_context(request: Request, session: DeviceSession | None, title: str) -> dict[str, Any]:
    ctx = page_context(request, session, EDIT_TOPICS)
    ctx["title"] = title
    return ctx


def render(
    request: Request, template: str, ctx: dict[str, Any], *, status_code: int = 200, error: str | None = None
) -> HTMLResponse:
    response = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    if error:
        response.headers[ERROR_HEADER] = quote(error, safe=" ")
    return response


def not_found_page(request: Request, serial: int) -> HTMLResponse:
    ctx = page_context(request, None, ("device",))
    ctx["missing_serial"] = serial
    return templates.TemplateResponse(request, "not_found.html", ctx, status_code=404)


def is_htmx(request: Request) -> bool:
    """State-changing UI routes require the HTMX header (params.js sends it too), so a plain cross-site form post
    can't reach them; `require_same_origin` covers browsers that send Origin / Sec-Fetch-Site."""
    return request.headers.get("HX-Request") == "true"


templates.env.globals.update(
    status_icon=STATUS_ICON,
    status_text=STATUS_TEXT,
    worst_status=worst_status,
)
