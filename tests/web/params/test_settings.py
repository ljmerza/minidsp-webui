"""Settings page: channel names/colours/ceilings, runtime safety & sync options, Home Assistant removal."""

import asyncio
import re

import pytest

from app.web.settings_ui import RUNTIME_KEY, apply_stored_runtime_settings

from .conftest import HX, UI, make_settings


def _channels_form(**overrides: str) -> dict[str, str]:
    form = {}
    for kind, count in (("input", 2), ("output", 4)):
        for idx in range(count):
            form[f"{kind}-{idx}-name"] = ""
            form[f"{kind}-{idx}-color"] = (
                ("#4ea1ff", "#a78bfa")[idx]
                if kind == "input"
                else (
                    "#3ccf7a",
                    "#f0b429",
                    "#ff7a59",
                    "#22c3c3",
                )[idx]
            )
            if kind == "output":
                form[f"{kind}-{idx}-max_gain_db"] = ""
    form.update(overrides)
    return form


async def test_settings_page_renders(ctx):
    response = await ctx.http.get("/settings")
    assert response.status_code == 200
    html = response.text
    for key in ("input-0-name", "output-3-color", "output-1-max_gain_db"):
        assert f'name="{key}"' in html
    assert 'name="input-0-max_gain_db"' not in html  # ceilings are per output
    assert re.search(r'name="gain_step_confirm_db"[^>]*value="6.0"', html, re.S)
    assert 'name="mute_wrap_bulk_apply" checked' in html
    assert "MQTT_ENABLED" in html  # MQTT is off in tests


async def test_save_channel_names_colours_and_ceilings(ctx):
    form = _channels_form(**{"output-0-name": " Shakers ", "output-0-color": "#AA0000", "output-0-max_gain_db": "10"})
    response = await ctx.http.post(f"{UI}/channels", data=form, headers=HX)
    assert response.status_code == 200, response.text
    assert "Saved." in response.text
    meta = await ctx.repo.get_channel_meta(ctx.session.serial, "output", 0)
    assert (meta.name, meta.color, meta.max_gain_db) == ("Shakers", "#aa0000", 10.0)
    assert ctx.session.channel_ceiling("output", 0) == 10.0
    # untouched rows with default colours store nothing
    assert await ctx.repo.get_channel_meta(ctx.session.serial, "output", 1) is None
    # the name shows up on the pages
    html = (await ctx.http.get("/outputs/1")).text
    assert "Shakers" in html and 'max="10.0"' in html


async def test_bad_channel_values_are_422(ctx):
    form = _channels_form(**{"output-2-max_gain_db": "30", "input-1-name": "x" * 40})
    response = await ctx.http.post(f"{UI}/channels", data=form, headers=HX)
    assert response.status_code == 422
    assert "gain ceiling must be within" in response.text and "at most 32 characters" in response.text
    response = await ctx.http.post(f"{UI}/channels", data=_channels_form(**{"output-0-max_gain_db": "abc"}), headers=HX)
    assert response.status_code == 422


async def test_inline_rename(ctx):
    response = await ctx.http.post(f"{UI}/channels/output/1/name", data={"name": "Sub"}, headers=HX)
    assert response.status_code == 200 and 'value="Sub"' in response.text
    await ctx.session.set_channel_meta("output", 1, name="Sub", color="#123456", max_gain_db=3.0)
    await ctx.http.post(f"{UI}/channels/output/1/name", data={"name": "Subwoofer"}, headers=HX)
    meta = ctx.session.channel_meta[("output", 1)]
    assert (meta.name, meta.color, meta.max_gain_db) == ("Subwoofer", "#123456", 3.0)  # colour/ceiling kept
    assert (await ctx.http.post(f"{UI}/channels/output/1/name", data={"name": "y" * 33}, headers=HX)).status_code == 422
    assert (await ctx.http.post(f"{UI}/channels/output/7/name", data={"name": "a"}, headers=HX)).status_code == 422
    assert (await ctx.http.post(f"{UI}/channels/bogus/0/name", data={"name": "a"}, headers=HX)).status_code == 404
    assert (await ctx.http.post(f"{UI}/channels/output/1/name", data={"name": "a"})).status_code == 400


async def test_runtime_settings_apply_now_and_persist(ctx):
    form = {"gain_step_confirm_db": "3", "write_coalesce_ms": "120", "reapply_on_reconnect": "on"}
    response = await ctx.http.post("/ui/settings/runtime", data=form, headers=HX)
    assert response.status_code == 200, response.text
    assert "Saved" in response.text
    assert ctx.settings.gain_step_confirm_db == 3.0
    assert ctx.settings.mute_wrap_bulk_apply is False and ctx.settings.reapply_on_reconnect is True
    assert ctx.session.writer.coalesce_s == pytest.approx(0.12)
    assert await ctx.repo.get_setting(RUNTIME_KEY) == {
        "gain_step_confirm_db": 3.0,
        "volume_drop_confirm_db": 20.0,  # not in this form: the current value is kept
        "write_coalesce_ms": 120,
        "mute_wrap_bulk_apply": False,
        "reapply_on_reconnect": True,
        "graph_view": "auto",
        "graph_db_range": 24.0,
    }
    # the session uses the new threshold at once
    response = await ctx.edit(0, {"outputs.0.gain_db": 4.0})
    assert response.status_code == 409
    # and a fresh start picks the stored values up
    fresh = make_settings()
    assert await apply_stored_runtime_settings(ctx.repo, fresh)
    assert fresh.gain_step_confirm_db == 3.0 and fresh.write_coalesce_ms == 120


async def test_default_graph_range_is_saved_and_reaches_the_pages(ctx):
    html = (await ctx.http.get("/settings")).text
    assert '<option value="auto" selected>' in html and 'name="graph_db_range"' in html and 'value="24"' in html

    form = {"gain_step_confirm_db": "6", "write_coalesce_ms": "50", "graph_view": "full", "graph_db_range": "36"}
    response = await ctx.http.post("/ui/settings/runtime", data=form, headers=HX)
    assert response.status_code == 200, response.text
    assert '<option value="full" selected>' in response.text and 'value="36"' in response.text
    assert ctx.settings.graph_view == "full" and ctx.settings.graph_db_range == 36.0
    stored = await ctx.repo.get_setting(RUNTIME_KEY)
    assert stored["graph_view"] == "full" and stored["graph_db_range"] == 36.0

    for page in ("/", "/outputs/1?tab=peq", "/routing", "/settings"):
        html = (await ctx.http.get(page)).text
        assert 'data-graph-view="full" data-graph-db="36"' in html, page

    fresh = make_settings()
    assert await apply_stored_runtime_settings(ctx.repo, fresh)
    assert fresh.graph_view == "full" and fresh.graph_db_range == 36.0

    # a form from before the graph fields existed keeps them
    response = await ctx.http.post(
        "/ui/settings/runtime", data={"gain_step_confirm_db": "6", "write_coalesce_ms": "50"}, headers=HX
    )
    assert response.status_code == 200
    assert ctx.settings.graph_view == "full" and ctx.settings.graph_db_range == 36.0

    for bad in ({"graph_view": "wide"}, {"graph_db_range": "1"}, {"graph_db_range": "100"}):
        form = {"gain_step_confirm_db": "6", "write_coalesce_ms": "50", **bad}
        assert (await ctx.http.post("/ui/settings/runtime", data=form, headers=HX)).status_code == 422, bad
    assert ctx.settings.graph_view == "full" and ctx.settings.graph_db_range == 36.0


async def test_runtime_settings_validation(ctx):
    form = {"gain_step_confirm_db": "0", "write_coalesce_ms": "5000"}
    response = await ctx.http.post("/ui/settings/runtime", data=form, headers=HX)
    assert response.status_code == 422
    assert ctx.settings.gain_step_confirm_db == 6.0
    assert await ctx.repo.get_setting(RUNTIME_KEY) is None


async def test_stored_runtime_settings_are_ignored_when_broken(ctx):
    await ctx.repo.set_setting(RUNTIME_KEY, {"gain_step_confirm_db": -5})
    fresh = make_settings()
    assert not await apply_stored_runtime_settings(ctx.repo, fresh)
    assert fresh.gain_step_confirm_db == 6.0
    await ctx.repo.delete_setting(RUNTIME_KEY)
    assert not await apply_stored_runtime_settings(ctx.repo, fresh)


class FakeBridge:
    def __init__(self, connected: bool, manager=None) -> None:
        self.manager = manager
        self.connected = asyncio.Event()
        if connected:
            self.connected.set()
        self.last_error = None
        self.removed: list[int] = []

    async def remove_device(self, serial: int) -> bool:
        if not self.connected.is_set():
            return False
        self.removed.append(serial)
        return True


async def test_remove_from_home_assistant(ctx):
    ctx.settings.mqtt_enabled = True
    ctx.settings.mqtt_host = "broker.invalid"
    bridge = FakeBridge(connected=True, manager=ctx.app.state.manager)
    ctx.app.state.mqtt = bridge
    html = (await ctx.http.get("/settings")).text
    assert "Remove from Home Assistant" in html and "connected" in html
    response = await ctx.http.post(f"{UI}/mqtt/remove", headers=HX)
    assert response.status_code == 200 and "Removed." in response.text
    assert bridge.removed == [ctx.session.serial]
    bridge.connected.clear()
    response = await ctx.http.post(f"{UI}/mqtt/remove", headers=HX)
    assert response.status_code == 409


async def test_settings_page_without_a_device(ctx):
    ctx.app.state.manager.sessions.clear()
    html = (await ctx.http.get("/settings")).text
    assert "Channel settings appear once" in html and 'name="gain_step_confirm_db"' in html
