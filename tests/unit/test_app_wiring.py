"""The app factory wires the Phase 2 state layer, routers and UI into the lifespan."""

import asyncio
from contextlib import asynccontextmanager

import httpx

from app.main import create_app
from app.state import Repo

from .conftest import DEVICE_JSON, MASTER_JSON, SERIAL


@asynccontextmanager
async def running(settings, client, daemon):
    daemon.get("/devices").respond(json=[DEVICE_JSON])
    daemon.get("/devices/0").respond(json={"master": MASTER_JSON, "input_levels": [], "output_levels": []})
    app = create_app(settings, client=client)
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if app.state.manager.get(SERIAL):
                break
            await asyncio.sleep(0.01)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield app, http


async def test_state_layer_and_routers_are_wired(settings, client, daemon):
    async with running(settings, client, daemon) as (app, http):
        assert isinstance(app.state.repo, Repo)
        assert app.state.manager.repo is app.state.repo
        assert app.state.mqtt is None  # MQTT is off unless MQTT_ENABLED and MQTT_HOST are set
        assert (await http.get("/api/v1/health")).json()["state"] == "persistent"
        presets = await http.get(f"/api/v1/devices/{SERIAL}/presets")
        assert presets.status_code == 200
        assert (await http.get(f"/api/v1/devices/{SERIAL}/adopt")).status_code == 200
        script = await http.get(f"/api/v1/devices/{SERIAL}/presets/0/export/cli-script")
        assert script.status_code == 200 and "output 0 gain" in script.text
        for page in ("/", "/routing", "/inputs/1", "/outputs/1", "/outputs/1?tab=delay", "/settings", "/device"):
            response = await http.get(page)
            assert response.status_code == 200, page


async def test_stored_runtime_settings_apply_at_startup(settings, client, daemon):
    async with running(settings, client, daemon) as (app, _):
        await app.state.repo.set_setting("ui.runtime_settings", {"gain_step_confirm_db": 3.0})
    assert settings.gain_step_confirm_db == 6.0
    async with running(settings, client, daemon):
        assert settings.gain_step_confirm_db == 3.0


async def test_app_serves_without_a_database(settings, client, daemon, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    settings.db_path = str(blocker / "state.sqlite3")
    async with running(settings, client, daemon) as (app, http):
        assert app.state.repo is None
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.get("/api/v1/health")).json()["state"] == "memory"
        # preset state still works, in memory only
        assert (await http.get(f"/api/v1/devices/{SERIAL}/presets")).status_code == 200
