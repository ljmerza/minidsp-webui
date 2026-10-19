"""Phase 4 wiring: the compressor/FIR, REW and system routers are registered, the output tabs and REW buttons are
mounted, and the Settings page drives the experimental FIR toggle."""

import re

import respx

from app.api import v1
from app.api.v1 import compressor_fir, rew, system
from app.bus import Bus
from app.daemon.client import DaemonClient
from app.main import create_app
from app.state import Database, Repo
from app.sync import fir as fir_svc
from app.sync.session import SessionManager
from app.web import system as system_ui
from tests.web.filters.conftest import HX, SERIAL
from tests.web.params.conftest import DAEMON_URL, FakeDaemon, make_settings

API = f"/api/v1/devices/{SERIAL}/presets/0"
SAME_ORIGIN = {**HX, "Origin": "http://test"}


def _mount(ctx) -> None:
    ctx.app.include_router(v1.build_router())
    ctx.app.include_router(system_ui.router)


def test_registry_has_the_phase4_routers():
    for router in (compressor_fir.router, rew.router, system.router):
        assert any(r is router for r in v1.ROUTERS)


async def test_compressor_tab_renders_the_editor(ctx):
    _mount(ctx)
    html = (await ctx.http.get("/outputs/2?tab=compressor")).text
    assert "compressorEditor(" in html and "/static/js/compressor.js" in html
    assert f"{API}/outputs/1/compressor" in html
    response = await ctx.http.get(f"{API}/outputs/1/compressor")
    assert response.status_code == 200


async def test_rew_buttons_sit_in_the_peq_and_crossover_editors(ctx):
    _mount(ctx)
    for page, bank in (
        ("/outputs/2?tab=peq", "outputs.1.peq"),
        ("/outputs/2?tab=crossover", "outputs.1.crossover"),
        ("/inputs/1", "inputs.0.peq"),
    ):
        html = (await ctx.http.get(page)).text
        assert 'class="rew"' in html, page
        assert f"export/rew?download=true&amp;bank={bank}" in html or f"bank={bank}" in html, page
        assert f"{API}/import/rew" in html, page


async def test_settings_toggle_turns_the_fir_tab_on_and_off(ctx):
    _mount(ctx)
    html = (await ctx.http.get("/settings")).text
    assert 'id="settings-fir"' in html and "Experimental FIR" in html
    assert 'data-chain-tab="fir"' not in (await ctx.http.get("/outputs/2")).text

    on = await ctx.http.post("/ui/settings/fir", data={"fir_experimental": "on"}, headers=SAME_ORIGIN)
    assert on.status_code == 200 and on.headers.get("HX-Refresh") == "true"
    assert ctx.session.profile.fir_supported and ctx.settings.fir_experimental
    assert await ctx.repo.get_setting(fir_svc.FIR_SETTING_KEY) is True
    page = (await ctx.http.get("/outputs/2")).text
    assert re.search(r'tab=fir"[^>]*data-chain-tab="fir"[^>]*>.*?FIR off', page, re.S)
    fir_html = (await ctx.http.get("/outputs/2?tab=fir")).text
    assert "firEditor(" in fir_html and "/static/js/fir.js" in fir_html
    assert (await ctx.http.get("/api/v1/fir/capability")).json()["enabled"] is True

    off = await ctx.http.post("/ui/settings/fir", data={}, headers=SAME_ORIGIN)
    assert off.status_code == 200 and off.headers.get("HX-Refresh") == "true"
    assert not ctx.session.profile.fir_supported
    assert 'data-chain-tab="fir"' not in (await ctx.http.get("/outputs/2")).text

    again = await ctx.http.post("/ui/settings/fir", data={}, headers=SAME_ORIGIN)
    assert "HX-Refresh" not in again.headers  # nothing changed, no reload
    assert (await ctx.http.post("/ui/settings/fir", data={})).status_code == 400  # HTMX only


async def test_system_page_is_mounted(ctx):
    _mount(ctx)
    page = await ctx.http.get("/system")
    assert page.status_code == 200 and "systemView" in page.text
    data = await ctx.http.get(f"/ui/devices/{SERIAL}/presets/0/system")
    assert data.status_code == 200
    api = await ctx.http.get(f"{API}/system")
    assert api.status_code == 200 and api.json()["label"] == "electrical sum"


async def test_discovery_honours_the_experimental_fir_flag(tmp_path):
    settings = make_settings()
    settings.fir_experimental = True
    db = await Database.open(tmp_path / "discover.sqlite3")
    client = DaemonClient(DAEMON_URL, timeout=1.0, preset_timeout=2.0)
    try:
        with respx.mock(base_url=DAEMON_URL, assert_all_called=False) as mock:
            FakeDaemon(mock)
            manager = SessionManager(client, Bus(), settings, repo=Repo(db))
            await manager.discover()
            session = manager.get(SERIAL)
            assert session is not None and session.profile.fir_supported
            await manager.stop()
    finally:
        await client.aclose()
        await db.close()


async def test_startup_restores_the_stored_fir_toggle(tmp_path):
    db_path = tmp_path / "startup.sqlite3"
    db = await Database.open(db_path)
    await Repo(db).set_setting(fir_svc.FIR_SETTING_KEY, True)
    await db.close()
    settings = make_settings()
    settings.db_path = str(db_path)
    client = DaemonClient("http://127.0.0.1:9", timeout=0.2, preset_timeout=0.2)
    app = create_app(settings, client=client)
    async with app.router.lifespan_context(app):
        assert settings.fir_experimental is True
    await client.aclose()
