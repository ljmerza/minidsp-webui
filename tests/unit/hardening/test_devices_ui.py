"""Multi-device readiness in the UI: page device selection, the header's device switcher and the storage banner."""

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.config import Settings
from app.model.device_profile import M2X4HD, DeviceProfile
from app.model.master import DaemonDevice, MasterStatus
from app.sync.health import STORAGE_MEMORY
from app.sync.session import DEFAULT_VOLUME_DROP_CONFIRM_DB, SessionState
from app.web import devices
from app.web.routes import header_context
from app.web.templating import templates

A, B = 911111, 922222


class Monitor:
    connected = True


@dataclass
class Session:
    serial: int
    profile: DeviceProfile = M2X4HD
    state: SessionState = SessionState.CONNECTED
    master: MasterStatus = field(default_factory=lambda: MasterStatus(preset=0, source="Analog", volume=-20.0))
    preset_switching: int | None = None
    volume_drop_confirm_db: float = DEFAULT_VOLUME_DROP_CONFIRM_DB
    monitor: Monitor = field(default_factory=Monitor)

    def device_event(self) -> dict[str, Any]:
        return {"serial": self.serial, "monitor_connected": True, "preset_switching": None}


@dataclass
class Manager:
    sessions: dict[int, Session] = field(default_factory=dict)
    daemon_up: bool | None = True
    last_error: str | None = None
    repo: Any = field(default_factory=object)
    unsupported: list[DaemonDevice] = field(default_factory=list)

    def get(self, serial: int) -> Session | None:
        return self.sessions.get(serial)

    def default(self) -> Session | None:
        return next(iter(self.sessions.values()), None)

    def __iter__(self):
        return iter(list(self.sessions.values()))


def make_app(manager: Manager) -> FastAPI:
    app = FastAPI()
    app.state.manager = manager
    app.state.settings = Settings(_env_file=None)
    app.include_router(devices.router)

    @app.get("/page")
    async def page(request: Request, serial: int | None = None) -> dict:
        session = devices.pick_session(request, serial)
        return {"serial": session.serial if session else None}

    @app.get("/header", response_class=HTMLResponse)
    async def header(request: Request, serial: int | None = None) -> HTMLResponse:
        # What routes.header_context looks like once it merges devices.header_extras.
        session = devices.pick_session(request, serial)
        ctx = {**header_context(request, session), **devices.header_extras(request, session)}
        return templates.TemplateResponse(request, "partials/header.html", ctx)

    return app


def two_devices(**kwargs) -> Manager:
    return Manager(sessions={A: Session(A), B: Session(B)}, **kwargs)


@pytest.fixture
async def http():
    clients = []

    async def make(manager: Manager) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(manager)), base_url="http://test")
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


async def test_select_remembers_the_device_and_goes_back(http):
    client = await http(two_devices())
    response = await client.get("/ui/devices/select", params={"serial": B, "next": "/outputs/2?preset=3&serial=1"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/outputs/2?preset=3&serial={B}"
    cookie = response.headers["set-cookie"]
    assert f"{devices.COOKIE}={B}" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()

    assert (await client.get("/page")).json() == {"serial": B}  # the cookie picks the device
    assert (await client.get("/page", params={"serial": A})).json() == {"serial": A}  # an explicit serial wins


async def test_select_unknown_device_is_404(http):
    client = await http(two_devices())
    response = await client.get("/ui/devices/select", params={"serial": 5})
    assert response.status_code == 404 and devices.COOKIE not in response.headers.get("set-cookie", "")


@pytest.mark.parametrize(
    "target", ["//evil.example/x", "https://evil.example/", "/\\evil.example", "outputs/1", "javascript:alert(1)", ""]
)
async def test_select_never_redirects_off_site(http, target):
    client = await http(two_devices())
    response = await client.get("/ui/devices/select", params={"serial": A, "next": target})
    assert response.headers["location"] == f"/?serial={A}"


async def test_stale_cookie_falls_back_to_the_first_device(http):
    client = await http(two_devices())
    client.cookies.set(devices.COOKIE, "123")
    assert (await client.get("/page")).json() == {"serial": A}
    client.cookies.set(devices.COOKIE, "not-a-number")
    assert (await client.get("/page")).json() == {"serial": A}


def test_device_choices_only_with_several_devices():
    one = Manager(sessions={A: Session(A)})
    assert devices.device_choices(one, one.get(A)) == []
    two = two_devices()
    assert devices.device_choices(two, two.get(B)) == [
        {"serial": A, "label": f"2x4HD #{A}", "current": False},
        {"serial": B, "label": f"2x4HD #{B}", "current": True},
    ]


async def test_header_shows_the_switcher_only_with_several_devices(http):
    single = await (await http(Manager(sessions={A: Session(A)}))).get("/header")
    assert 'action="/ui/devices/select"' not in single.text and f"#{A}" in single.text

    html = (await (await http(two_devices())).get("/header", params={"serial": B})).text
    assert 'action="/ui/devices/select"' in html
    assert f'<option value="{B}" selected>2x4HD #{B}</option>' in html
    assert f'<option value="{A}">2x4HD #{A}</option>' in html


async def test_header_carries_the_drop_threshold(http):
    html = (await (await http(Manager(sessions={A: Session(A)}))).get("/header")).text
    assert html.count('data-confirm-drop-db="20.0"') == 2  # slider and number field


class LegacySession:
    """A session object from before the drop guard (no ``volume_drop_confirm_db``)."""

    def __init__(self, serial: int) -> None:
        base = Session(serial)
        for name in ("serial", "profile", "state", "master", "preset_switching", "monitor", "device_event"):
            setattr(self, name, getattr(base, name))


async def test_header_without_a_drop_threshold_disables_the_check(http):
    html = (await (await http(Manager(sessions={A: LegacySession(A)}))).get("/header")).text
    assert html.count('data-confirm-drop-db="0"') == 2


async def test_storage_banner(http):
    html = (await (await http(Manager(sessions={A: Session(A)}, repo=None))).get("/header")).text
    assert STORAGE_MEMORY in html


async def test_daemon_down_banner(http):
    manager = Manager(sessions={A: Session(A, state=SessionState.DAEMON_DOWN)}, daemon_up=False)
    html = (await (await http(manager)).get("/header")).text
    assert "minidspd unreachable" in html and "the app keeps retrying" in html
    assert 'id="master-volume"' in html and "disabled" in html
