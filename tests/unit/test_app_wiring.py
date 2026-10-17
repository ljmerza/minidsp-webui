"""The app factory wires the Phase 2 state layer, routers and UI into the lifespan."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
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


async def test_phase5_profiles_and_backup_are_wired(settings, client, daemon):
    async with running(settings, client, daemon) as (app, http):
        assert (await http.get("/api/v1/profiles")).json() == []
        assert (await http.get("/api/v1/backup/db/files")).status_code == 200
        profiles = await http.get("/profiles")
        assert profiles.status_code == 200
        assert 'href="/profiles"' in profiles.text  # the sidebar item is enabled once the page is served
        assert (await http.get("/profiles?tab=history&preset=1")).status_code == 200
        page = await http.get("/settings")
        assert page.status_code == 200 and 'id="backup-title"' in page.text
        assert (await http.get("/ui/settings/backup")).status_code == 200


async def test_profile_writes_refresh_the_mqtt_bridge(settings, client, daemon):
    class Bridge:
        refreshed = 0

        def refresh(self, serial=None):
            Bridge.refreshed += 1

        async def stop(self):
            pass

    async with running(settings, client, daemon) as (app, http):
        app.state.mqtt = Bridge()
        await http.get("/api/v1/profiles")
        assert Bridge.refreshed == 0
        created = await http.post("/api/v1/profiles", json={"name": "Movies", "serial": SERIAL, "preset": 0})
        assert created.status_code in (200, 201)
        assert Bridge.refreshed == 1
        await http.patch(f"/api/v1/profiles/{created.json()['id']}", json={"name": "Films"})
        assert Bridge.refreshed == 2
        app.state.mqtt = None


async def test_a_staged_database_restore_is_swapped_in_at_startup(settings, client, daemon):
    async with running(settings, client, daemon) as (_, http):
        await http.post("/api/v1/profiles", json={"name": "Before", "serial": SERIAL, "preset": 0})
        snapshot = (await http.get("/api/v1/backup/db")).content
        await http.post("/api/v1/profiles", json={"name": "After", "serial": SERIAL, "preset": 0})
        staged = await http.post("/api/v1/backup/db/restore", content=snapshot)
        assert staged.status_code == 200, staged.text
        assert [p["name"] for p in (await http.get("/api/v1/profiles")).json()] == ["After", "Before"]
    async with running(settings, client, daemon) as (_, http):
        assert [p["name"] for p in (await http.get("/api/v1/profiles")).json()] == ["Before"]


async def test_phase6_verify_devices_and_health_are_wired(settings, client, daemon):
    async with running(settings, client, daemon) as (app, http):
        verify = await http.get(f"/api/v1/devices/{SERIAL}/verify")
        assert verify.status_code == 404 and verify.json()["error"] == "not_verified"  # wired; nothing run yet
        panel = await http.get(f"/ui/devices/{SERIAL}/verify")
        assert panel.status_code == 200 and 'id="verify-panel"' in panel.text
        device = await http.get("/device")
        assert f'hx-get="/ui/devices/{SERIAL}/verify"' in device.text
        select = await http.get("/ui/devices/select", params={"serial": SERIAL, "next": "/routing"})
        assert select.status_code == 303 and select.headers["location"] == f"/routing?serial={SERIAL}"
        assert "mdsp_serial" in select.headers["set-cookie"]
        settings_page = await http.get("/settings")
        assert 'name="volume_drop_confirm_db"' in settings_page.text
        health = (await http.get("/api/v1/health")).json()
        assert health["status"] in ("ok", "degraded", "starting") and "problems" in health and "storage" in health
        healthz = (await http.get("/healthz")).json()
        assert healthz["status"] == "ok" and healthz["health"] == health["status"]
        tasks = {t.get_name() for t in asyncio.all_tasks()}
        assert "maintenance" in tasks and "nightly-verify" not in tasks, "nightly verify is opt-in"


async def test_strict_health_answers_503_when_degraded(settings, client, daemon):
    async with running(settings, client, daemon) as (app, http):
        app.state.manager.daemon_up = False
        assert (await http.get("/api/v1/health")).status_code == 200
        strict = await http.get("/api/v1/health", params={"strict": "true"})
        assert strict.status_code == 503 and strict.json()["status"] == "degraded"
        assert (await http.get("/healthz")).status_code == 200, "liveness never fails on a degraded app"


async def test_nightly_verify_starts_when_enabled(settings, client, daemon):
    settings.verify_nightly = True
    settings.verify_at = "03:30"
    async with running(settings, client, daemon):
        assert "nightly-verify" in {t.get_name() for t in asyncio.all_tasks()}
    assert "nightly-verify" not in {t.get_name() for t in asyncio.all_tasks() if not t.done()}


def test_verify_at_must_be_a_time():
    assert Settings(_env_file=None, verify_at=" 05:00 ").verify_at == "05:00"
    with pytest.raises(ValidationError, match="VERIFY_AT"):
        Settings(_env_file=None, verify_at="late")
