"""FastAPI dependencies shared by the JSON API and the HTML pages."""

from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.bus import Bus
from app.config import Settings
from app.sync.session import DeviceSession, SessionManager


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_bus(request: Request) -> Bus:
    return request.app.state.bus


def get_manager(request: Request) -> SessionManager:
    return request.app.state.manager


def get_session(serial: int, request: Request) -> DeviceSession:
    session = get_manager(request).get(serial)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown device {serial}")
    return session


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_same_origin(request: Request) -> None:
    """Reject state-changing requests that a browser sent on behalf of another site.

    Bodyless POSTs (volume up, mute toggle) are CORS "simple" requests, so any page open in a LAN browser could fire
    them without a preflight. Browsers mark such requests with ``Sec-Fetch-Site`` (and ``Origin``); clients that send
    neither (Home Assistant, curl, scripts) are not browsers and are allowed.
    """
    if request.method in _SAFE_METHODS:
        return
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        if fetch_site in ("same-origin", "none"):
            return
        raise HTTPException(status_code=403, detail="cross-site request refused")
    origin = request.headers.get("origin")
    if origin is None:
        return
    hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")} - {None}
    if origin == "null" or urlsplit(origin).netloc not in hosts:
        raise HTTPException(status_code=403, detail="cross-origin request refused")


async def refresh_mqtt_after_write(request: Request) -> AsyncIterator[None]:
    """After a successful state-changing request, let the MQTT bridge re-read profiles and channel names right away
    (it would otherwise notice within its periodic refresh). Only what actually changed is re-published."""
    yield
    if request.method not in _SAFE_METHODS:
        bridge = getattr(request.app.state, "mqtt", None)
        if bridge is not None:
            bridge.refresh()
