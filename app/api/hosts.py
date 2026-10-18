"""ALLOWED_HOSTS: refuse requests addressed to a host name the user didn't list.

The API has no login and its CSRF guard (`require_same_origin`) compares ``Origin`` with ``Host``. DNS rebinding beats
that: a page on ``attacker.example`` re-points its name at this app, and the browser then sends same-origin requests
with ``Host: attacker.example``. With ``ALLOWED_HOSTS`` set, only IP literals, ``localhost`` and the listed names are
served, which a rebinding page can't produce.
"""

import ipaddress
from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def parse_allowed_hosts(value: str) -> frozenset[str]:
    """``ALLOWED_HOSTS`` (comma-separated names) → normalised names. Empty means "don't check"."""
    return frozenset(_normalise(name) for name in value.split(",") if name.strip())


def _normalise(name: str) -> str:
    return name.strip().lower().rstrip(".")


def _strip_port(host: str) -> str:
    if host.startswith("["):  # [v6]:port
        return host[1 : host.find("]")] if "]" in host else host
    name, sep, port = host.rpartition(":")
    return name if sep and port.isdigit() and ":" not in name else host


def host_allowed(host: str, allowed: Iterable[str]) -> bool:
    """IP literals and localhost are always fine (a rebinding page can't make the browser send them); names must be
    listed. A request without a Host header isn't from a browser."""
    name = _normalise(_strip_port(host.strip()))
    if not name or name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name in allowed
    return True


class AllowedHostsMiddleware:
    def __init__(self, app: ASGIApp, allowed: Iterable[str]) -> None:
        self.app = app
        self.allowed = frozenset(allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":  # the app serves no websockets
            host = Headers(scope=scope).get("host", "")
            if not host_allowed(host, self.allowed):
                response = PlainTextResponse("host not allowed (add it to ALLOWED_HOSTS)", status_code=400)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
