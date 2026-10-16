"""The web router on the real app and session layer, with the daemon's HTTP API mocked by respx."""

import asyncio
import json
from urllib.parse import unquote

import httpx
import pytest
import respx

from app import web
from app.config import Settings
from app.daemon.client import DaemonClient
from app.main import create_app

DAEMON_URL = "http://127.0.0.1:1"  # nothing listens here, so the monitor's websocket fails fast
SERIAL = 922222
DEVICE_JSON = {
    "url": "mock:?serial=922222",
    "product_name": "2x4HD",
    "version": {"hw_id": 10, "dsp_version": 100, "serial": SERIAL, "fw_major": 1, "fw_minor": 75},
}
MASTER_JSON = {"preset": 0, "source": "Analog", "volume": -20.0, "mute": False, "dirac": False}
HX = {"HX-Request": "true"}


@pytest.fixture
async def ctx():
    settings = Settings(
        _env_file=None,
        minidspd_url=DAEMON_URL,
        preset_switch_timeout_s=2.0,
        monitor_backoff_min_s=0.05,
        monitor_backoff_max_s=0.2,
    )
    with respx.mock(base_url=DAEMON_URL, assert_all_called=False) as daemon:
        daemon.get("/devices").respond(json=[DEVICE_JSON])
        daemon.get("/devices/0").respond(json={"master": MASTER_JSON, "input_levels": [], "output_levels": []})
        client = DaemonClient(DAEMON_URL, timeout=1.0, preset_timeout=2.0)
        app = create_app(settings, client=client)
        app.include_router(web.router)
        try:
            async with app.router.lifespan_context(app):
                manager = app.state.manager
                for _ in range(100):
                    if manager.get(SERIAL):
                        break
                    await asyncio.sleep(0.01)
                session = manager.get(SERIAL)
                await session.refresh_status()
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                    yield http, daemon, session
        finally:
            await client.aclose()


async def test_dashboard_on_the_real_app(ctx):
    http, _, _ = ctx
    html = (await http.get("/")).text
    assert f'data-serial="{SERIAL}"' in html
    assert 'value="-20.0"' in html
    assert '<a href="/"' in html  # the dashboard is served, so its nav entry is a link


async def test_volume_write_goes_through_the_session(ctx):
    http, daemon, session = ctx
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -14.5})
    response = await http.post(f"/ui/devices/{SERIAL}/master/volume", data={"volume": "-14.5"}, headers=HX)
    assert response.status_code == 200
    assert json.loads(route.calls.last.request.content) == {"volume": -14.5}
    assert session.master.volume == -14.5
    assert 'value="-14.5"' in response.text


async def test_out_of_range_volume_is_rejected_before_the_daemon(ctx):
    http, daemon, _ = ctx
    route = daemon.post("/devices/0").respond(json=MASTER_JSON)
    response = await http.post(f"/ui/devices/{SERIAL}/master/volume", data={"volume": "6"}, headers=HX)
    assert response.status_code == 422
    assert "outside" in unquote(response.headers["X-Mdsp-Error"])
    assert not route.called


async def test_daemon_down_during_write(ctx):
    http, daemon, _ = ctx
    daemon.get("/devices/0").mock(side_effect=httpx.ConnectError("refused"))
    daemon.post("/devices/0").mock(side_effect=httpx.ConnectError("refused"))
    response = await http.post(f"/ui/devices/{SERIAL}/master/volume/up", headers=HX)
    assert response.status_code == 503
    assert "X-Mdsp-Error" in response.headers
    assert "minidspd unreachable" in response.text


async def test_source_is_canonicalized(ctx):
    http, daemon, session = ctx
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "source": "Toslink"})
    response = await http.post(f"/ui/devices/{SERIAL}/master/source", data={"source": "toslink"}, headers=HX)
    assert response.status_code == 200
    assert json.loads(route.calls.last.request.content) == {"source": "Toslink"}
    assert session.master.source == "Toslink"
    assert '<option value="Toslink" selected>' in response.text


async def test_big_volume_raise_needs_the_confirm_flag(ctx):
    http, daemon, session = ctx
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -2.0})
    response = await http.post(f"/ui/devices/{SERIAL}/master/volume", data={"volume": "-2"}, headers=HX)
    assert response.status_code == 409
    assert "confirmation" in unquote(response.headers["X-Mdsp-Error"])
    assert not route.called
    data = {"volume": "-2", "confirm": "true"}
    response = await http.post(f"/ui/devices/{SERIAL}/master/volume", data=data, headers=HX)
    assert response.status_code == 200
    assert session.master.volume == -2.0
