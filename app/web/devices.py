"""Multi-device support for the HTML pages (docs/03 §12).

Everything is keyed by device serial. A page shows the device named by its ``?serial=`` parameter, else the one
picked in the header's device switcher (remembered in a cookie), else the first discovered device. The switcher only
appears when more than one supported device is present, so a single-device setup looks exactly as before.
"""

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from app.sync.health import storage_problem
from app.sync.session import DeviceSession, SessionManager

router = APIRouter(include_in_schema=False)

COOKIE = "mdsp_serial"
COOKIE_MAX_AGE_S = 365 * 24 * 3600


def _manager(request: Request) -> SessionManager:
    return request.app.state.manager


def _root(request: Request) -> str:
    return request.scope.get("root_path", "").rstrip("/")


def cookie_serial(request: Request) -> int | None:
    raw = request.cookies.get(COOKIE, "")
    return int(raw) if raw.isascii() and raw.isdigit() else None


def pick_session(request: Request, serial: int | None) -> DeviceSession | None:
    """The device a page shows: an explicit serial (None if it's unknown), else the switcher's choice if that device
    is still present, else the first one."""
    manager = _manager(request)
    if serial is not None:
        return manager.get(serial)
    chosen = cookie_serial(request)
    if chosen is not None and (session := manager.get(chosen)) is not None:
        return session
    return manager.default()


def device_choices(manager: SessionManager, current: DeviceSession | None) -> list[dict[str, Any]]:
    """Switcher entries, in discovery order. Empty unless there is more than one device to choose from."""
    sessions = list(manager)
    if len(sessions) < 2:
        return []
    return [
        {
            "serial": s.serial,
            "label": f"{s.profile.product_name} #{s.serial}",
            "current": current is not None and s.serial == current.serial,
        }
        for s in sessions
    ]


def header_extras(request: Request, session: DeviceSession | None) -> dict[str, Any]:
    """Extra context for ``partials/header.html``: the device switcher and the storage banner."""
    manager = _manager(request)
    return {
        "devices": device_choices(manager, session),
        "storage_problem": storage_problem(manager),
    }


def safe_next(request: Request, target: str | None, serial: int) -> str:
    """A same-site path to go back to, with ``serial`` set. Anything that could leave the site falls back to the
    dashboard."""
    root = _root(request)
    parts = urlsplit(target or "")
    path = parts.path
    if parts.scheme or parts.netloc or not path.startswith("/") or path.startswith("//") or "\\" in path:
        path, query = f"{root}/", ""
    else:
        query = parts.query
    params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "serial"]
    params.append(("serial", str(serial)))
    return urlunsplit(("", "", path, urlencode(params), ""))


@router.get("/ui/devices/select")
async def select_device(request: Request, serial: int, next: str | None = None) -> Response:
    """Remember ``serial`` as the device to show and go back to ``next`` (the page the switcher was on)."""
    if _manager(request).get(serial) is None:
        return PlainTextResponse(f"device {serial} is not connected", status_code=404)
    response = RedirectResponse(safe_next(request, next, serial), status_code=303)
    response.set_cookie(
        COOKIE,
        str(serial),
        max_age=COOKIE_MAX_AGE_S,
        path=f"{_root(request)}/",
        httponly=True,
        samesite="lax",
    )
    return response
