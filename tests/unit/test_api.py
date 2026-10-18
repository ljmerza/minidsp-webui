import asyncio
import json

import httpx
import pytest

from app.api.v1.events import event_stream
from app.bus import LEVELS, LOG
from app.main import create_app

from .conftest import DEVICE_JSON, MASTER_JSON, SERIAL


@pytest.fixture
async def app_ctx(settings, client, daemon):
    """The real app (lifespan included) against a respx-mocked daemon."""
    daemon.get("/devices").respond(json=[DEVICE_JSON])
    app = create_app(settings, client=client)
    async with app.router.lifespan_context(app):
        manager = app.state.manager
        for _ in range(100):
            if manager.get(SERIAL):
                break
            await asyncio.sleep(0.01)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            yield app, http


async def test_healthz(app_ctx):
    _, http = app_ctx
    response = await http.get("/healthz")
    assert response.status_code == 200 and response.json()["status"] == "ok"


async def test_health_reports_daemon_and_device(app_ctx):
    _, http = app_ctx
    data = (await http.get("/api/v1/health")).json()
    assert data["app"] == "ok"
    assert data["daemon"] == "up"
    assert data["device"] == "connected"
    assert data["devices"][0]["serial"] == SERIAL


async def test_devices(app_ctx):
    _, http = app_ctx
    devices = (await http.get("/api/v1/devices")).json()
    assert len(devices) == 1
    device = devices[0]
    assert device["serial"] == SERIAL and device["supported"] is True
    assert device["fw"] == "1.75" and device["daemon_index"] == 0
    assert device["profile"]["n_outputs"] == 4
    assert (await http.get(f"/api/v1/devices/{SERIAL}")).json()["serial"] == SERIAL
    assert (await http.get("/api/v1/devices/1")).status_code == 404


async def test_status_from_cache(app_ctx):
    app, http = app_ctx
    session = app.state.manager.get(SERIAL)
    session.monitor.levels_enabled = True
    session.monitor.handle_message(json.dumps({"master": MASTER_JSON, "input_levels": [-3.0, -4.0]}))
    data = (await http.get(f"/api/v1/devices/{SERIAL}/status")).json()
    assert data["master"]["volume"] == -20.0
    assert data["input_levels"] == [-3.0, -4.0]
    assert data["preset_switching"] is None


async def test_put_master(app_ctx, daemon):
    _, http = app_ctx
    daemon.get("/devices/0").respond(json={"master": MASTER_JSON})
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -40.0})
    response = await http.put(f"/api/v1/devices/{SERIAL}/master", json={"volume": -40.0})
    assert response.status_code == 200
    assert response.json()["volume"] == -40.0
    assert json.loads(route.calls.last.request.content) == {"volume": -40.0}


async def test_put_master_validation(app_ctx, daemon):
    _, http = app_ctx
    route = daemon.post("/devices/0")
    assert (await http.put(f"/api/v1/devices/{SERIAL}/master", json={"volume": 5})).status_code == 422
    assert (await http.put(f"/api/v1/devices/{SERIAL}/master", json={"source": "Hdmi"})).status_code == 422
    assert (await http.put(f"/api/v1/devices/{SERIAL}/master", json={"loud": True})).status_code == 422
    assert not route.called


async def test_volume_and_mute_routes(app_ctx, daemon):
    _, http = app_ctx
    daemon.get("/devices/0").respond(json={"master": MASTER_JSON})
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -19.5})
    assert (await http.post(f"/api/v1/devices/{SERIAL}/master/volume/up")).json()["volume"] == -19.5
    assert json.loads(route.calls.last.request.content) == {"volume": -19.5}
    route.respond(json={**MASTER_JSON, "mute": True})
    assert (await http.post(f"/api/v1/devices/{SERIAL}/master/mute/toggle")).json()["mute"] is True
    assert (await http.post(f"/api/v1/devices/{SERIAL}/master/volume/sideways")).status_code == 422


async def test_daemon_errors_map_to_http(app_ctx, daemon):
    _, http = app_ctx
    daemon.post("/devices/0").mock(side_effect=httpx.ConnectError("refused"))
    response = await http.put(f"/api/v1/devices/{SERIAL}/master", json={"mute": True})
    assert response.status_code == 503 and response.json()["error"] == "daemon_unavailable"

    daemon.post("/devices/0").respond(500, json={"message": "bad", "error": {"type": "InternalError"}})
    response = await http.put(f"/api/v1/devices/{SERIAL}/master", json={"mute": True})
    assert response.status_code == 502
    assert response.json()["daemon_error"] == "InternalError"


async def test_app_serves_when_daemon_down(settings, client, daemon):
    daemon.get("/devices").mock(side_effect=httpx.ConnectError("refused"))
    app = create_app(settings, client=client)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            assert (await http.get("/healthz")).status_code == 200
            data = (await http.get("/api/v1/health")).json()
            assert data["daemon"] == "down" and data["device"] == "absent"
            assert (await http.get("/api/v1/devices")).json() == []


async def test_event_stream_snapshot_and_filtering(app_ctx):
    app, _ = app_ctx
    bus, manager = app.state.bus, app.state.manager
    manager.get(SERIAL).monitor.handle_message(json.dumps({"master": MASTER_JSON}))
    stream = event_stream(bus, manager, {"device", "master", LEVELS, LOG}, serial=SERIAL)
    first = await anext(stream)
    second = await anext(stream)
    assert first["event"] == "device" and json.loads(first["data"])["serial"] == SERIAL
    assert second["event"] == "master" and json.loads(second["data"])["master"]["volume"] == -20.0

    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    bus.publish(LEVELS, {"i": [1.0], "o": [2.0]}, serial=999)  # other device: filtered out
    bus.publish(LEVELS, {"i": [-40.1, -104.0], "o": [-50.0, -60.0, -120.0, -120.0]}, serial=SERIAL)
    event = await asyncio.wait_for(pending, 1)
    assert event == {"event": "levels", "data": '{"i":[-40.1,-104.0],"o":[-50.0,-60.0,-120.0,-120.0]}'}
    assert bus.subscriber_count(LEVELS) >= 1
    await stream.aclose()
    assert bus.subscriber_count(LEVELS) == 0


async def test_events_route_is_registered(app_ctx):
    app, _ = app_ctx
    assert "/api/v1/events" in app.openapi()["paths"]


async def test_put_master_big_raise_needs_confirm(app_ctx, daemon):
    _, http = app_ctx
    daemon.get("/devices/0").respond(json={"master": {**MASTER_JSON, "volume": -60.0}})
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": 0.0})
    response = await http.put(f"/api/v1/devices/{SERIAL}/master", json={"volume": 0})
    assert response.status_code == 409
    assert response.json()["error"] == "confirmation_required"
    assert not route.called
    response = await http.put(f"/api/v1/devices/{SERIAL}/master", json={"volume": 0, "confirm": True})
    assert response.status_code == 200
    assert json.loads(route.calls.last.request.content) == {"volume": 0.0}


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://evil.example", "Content-Type": "text/plain"},
    ],
)
async def test_cross_site_writes_are_refused(app_ctx, daemon, headers):
    _, http = app_ctx
    route = daemon.post("/devices/0").respond(json=MASTER_JSON)
    for path in ("/master/volume/up", "/master/mute/toggle"):
        response = await http.post(f"/api/v1/devices/{SERIAL}{path}", headers=headers)
        assert response.status_code == 403, path
    response = await http.put(f"/api/v1/devices/{SERIAL}/master", json={"mute": True}, headers=headers)
    assert response.status_code == 403
    assert not route.called
    # Reads stay open (e.g. for dashboards).
    assert (await http.get(f"/api/v1/devices/{SERIAL}/status", headers=headers)).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},  # Home Assistant, curl, scripts
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://proxy.example"},  # /docs behind a proxy
        {"Sec-Fetch-Site": "none"},
        {"Origin": "http://localhost"},
        {"Origin": "http://public.example", "X-Forwarded-Host": "public.example"},
    ],
)
async def test_same_origin_and_non_browser_writes_are_allowed(app_ctx, daemon, headers):
    _, http = app_ctx
    daemon.get("/devices/0").respond(json={"master": MASTER_JSON})
    daemon.post("/devices/0").respond(json={**MASTER_JSON, "mute": True})
    response = await http.post(f"/api/v1/devices/{SERIAL}/master/mute/toggle", headers=headers)
    assert response.status_code == 200, response.text
