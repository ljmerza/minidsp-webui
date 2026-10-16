"""Device page: sync table with per-row push, adopt wizard (trust / push / unknown, script preview and upload) and
the event log."""

import json
from pathlib import Path

from app.sync.session import AdoptMode
from app.web.adopt_sample import ADOPT_SAMPLE
from app.web.sync_ui import describe_path, format_value

from .conftest import HX, SERIAL, UI

FIXTURE = Path(__file__).parents[2] / "fixtures" / "pi_notes_commands.txt"


def test_adopt_sample_is_the_test_fixture():
    assert FIXTURE.read_text() == ADOPT_SAMPLE


async def test_device_page_loads_the_new_sections(ctx):
    html = (await ctx.http.get("/device?preset=2")).text
    assert f'hx-get="/ui/devices/{SERIAL}/sync-panel?preset=2"' in html
    assert f'hx-get="/ui/devices/{SERIAL}/adopt"' in html
    assert f'hx-get="/ui/devices/{SERIAL}/events"' in html
    assert 'sse-connect="/api/v1/events?topics=sync%2Clog%2Cmaster&amp;serial=' in html
    assert 'id="sync"' in html and 'id="adopt"' in html


async def test_sync_table_lists_unknown_paths_until_adopted(ctx):
    html = (await ctx.http.get(f"{UI}/sync-panel")).text
    assert 'id="syncbar"' in html and 'id="sync-table"' in html
    assert "outputs.0.gain_db" in html and "Out 1 · gain" in html
    assert "sync-unknown" in html
    await ctx.session.adopt(AdoptMode.TRUST, preset=0)
    html = (await ctx.http.get(f"{UI}/sync-table", params={"preset": 0})).text
    assert "Every setting of preset 1 is synced" in html
    html = (await ctx.http.get(f"{UI}/sync-table", params={"preset": 0, "show": "all"})).text
    assert "outputs.3.delay_ms" in html and "sync-synced" in html and 'aria-pressed="true"' in html


async def test_sync_panel_preset_and_errors(ctx):
    html = (await ctx.http.get(f"{UI}/sync-panel", params={"preset": 3})).text
    assert 'aria-current="page"' in html and 'href="/device?preset=3"' in html
    assert (await ctx.http.get(f"{UI}/sync-panel", params={"preset": 7})).status_code == 404
    assert (await ctx.http.get(f"{UI}/sync-table", params={"preset": 4})).status_code == 404
    assert (await ctx.http.get("/ui/devices/5/sync-panel")).status_code == 404


async def test_per_row_push_writes_one_path_again(ctx):
    await ctx.session.adopt(AdoptMode.TRUST, preset=0)
    response = await ctx.http.post(f"{UI}/presets/0/paths/push", data={"path": "outputs.2.mute"}, headers=HX)
    assert response.status_code == 200
    await ctx.settle()
    assert ctx.fake.outputs_written() == [{"index": 2, "mute": False}]
    response = await ctx.http.post(f"{UI}/presets/0/paths/push", data={"path": "outputs.0.bogus"}, headers=HX)
    assert response.status_code == 422 and "X-Mdsp-Error" in response.headers


async def test_trust_adopt_from_the_sample_writes_nothing(ctx):
    form = {"preset": "0", "source": "script", "script": ADOPT_SAMPLE, "mode": "trust"}
    response = await ctx.http.post(f"{UI}/adopt", data=form, headers=HX)
    assert response.status_code == 200, response.text
    assert "Adopted preset 1 (trust)" in response.text and "0 write(s)" in response.text
    assert json.loads(response.headers["HX-Trigger"]) == {"mdsp-sync-changed": {"preset": 0}}
    await ctx.settle()
    assert ctx.fake.configs == []
    assert ctx.session.desired(0).outputs[0].gain_db == 12.0
    assert ctx.session.sync_status(0)["outputs.0.gain_db"] == "synced"
    assert ctx.session.sync_status(0)["outputs.0.peq.0.bypass"] == "unknown"  # not in the script
    await ctx.drain()
    log = (await ctx.http.get(f"{UI}/events")).text
    assert "adopted preset 1 (trust)" in log and "0 write(s)" in log


async def test_trust_all_and_unknown_modes(ctx):
    form = {"preset": "1", "source": "script", "script": ADOPT_SAMPLE, "mode": "trust", "trust_all": "on"}
    assert (await ctx.http.post(f"{UI}/adopt", data=form, headers=HX)).status_code == 200
    assert set(ctx.session.sync_status(1).values()) == {"synced"}
    form = {"preset": "2", "source": "defaults", "mode": "unknown"}
    response = await ctx.http.post(f"{UI}/adopt", data=form, headers=HX)
    assert response.status_code == 200 and "Adopted preset 3 (unknown)" in response.text
    assert set(ctx.session.sync_status(2).values()) == {"unknown"}
    assert ctx.fake.configs == []


async def test_push_adopt_needs_the_confirm_box(ctx):
    form = {"preset": "0", "source": "defaults", "mode": "push"}
    response = await ctx.http.post(f"{UI}/adopt", data=form, headers=HX)
    assert response.status_code == 409
    assert "Nothing was adopted" in response.text and 'value="push"' in response.text
    assert ctx.fake.configs == []
    response = await ctx.http.post(f"{UI}/adopt", data={**form, "confirm": "on"}, headers=HX)
    assert response.status_code == 200, response.text
    assert "Adopted preset 1 (push)" in response.text
    assert ctx.fake.configs and all(o.get("mute") is True for o in ctx.fake.configs[0]["outputs"])


async def test_adopt_from_an_uploaded_file(ctx):
    files = {"script_file": ("notes.txt", b"\xef\xbb\xbfoutput 1 gain -- -4\noutput 3 mute on\n", "text/plain")}
    data = {"preset": "0", "source": "script", "mode": "trust", "script": ""}
    response = await ctx.http.post(f"{UI}/adopt", data=data, files=files, headers=HX)
    assert response.status_code == 200, response.text
    assert ctx.session.desired(0).outputs[1].gain_db == -4.0


async def test_adopt_errors_are_shown_and_keep_the_form(ctx):
    form = {"preset": "0", "source": "script", "script": "output 0 gain loud\nfor i in 1 2; do", "mode": "trust"}
    response = await ctx.http.post(f"{UI}/adopt", data=form, headers=HX)
    assert response.status_code == 422
    assert "line 1" in response.text and "output 0 gain loud" in response.text
    form = {"preset": "0", "source": "script", "script": "   ", "mode": "trust"}
    assert (await ctx.http.post(f"{UI}/adopt", data=form, headers=HX)).status_code == 422
    form = {"preset": "9", "source": "defaults", "mode": "trust"}
    assert (await ctx.http.post(f"{UI}/adopt", data=form, headers=HX)).status_code == 422
    form = {"preset": "0", "source": "defaults", "mode": "sideways"}
    assert (await ctx.http.post(f"{UI}/adopt", data=form, headers=HX)).status_code == 422
    assert ctx.session.needs_adoption


async def test_adopt_preview(ctx):
    response = await ctx.http.post(f"{UI}/adopt/preview", data={"source": "script", "script": ADOPT_SAMPLE}, headers=HX)
    assert response.status_code == 200
    assert "readable" in response.text and "covering" in response.text
    script = "output 0 gain 3\nminidsp gain -- -20\noutput 0 gain\n"
    html = (await ctx.http.post(f"{UI}/adopt/preview", data={"script": script}, headers=HX)).text
    assert "1 error(s)" in html and "Ignored lines" in html
    html = (await ctx.http.post(f"{UI}/adopt/preview", data={"source": "defaults"}, headers=HX)).text
    assert "no script to preview" in html
    assert ctx.session.needs_adoption  # previews change nothing


async def test_adopt_wizard_shows_the_sample_and_status(ctx):
    html = (await ctx.http.get(f"{UI}/adopt")).text
    assert "Not adopted yet" in html and "Use the sample" in html
    assert "crossover 0 0 set -- 6.828594198269291e-06" in html
    await ctx.session.adopt(AdoptMode.UNKNOWN, preset=0)
    assert "Adopted with <strong>unknown</strong>" in (await ctx.http.get(f"{UI}/adopt")).text


async def test_event_log_filters_other_devices(ctx):
    await ctx.repo.log_event("adopt", "other device event", data={"serial": 1})
    await ctx.repo.log_event("app", "app-wide event")
    ctx.session.log_event("info", "this device event", kind="push")
    await ctx.drain()
    html = (await ctx.http.get(f"{UI}/events")).text
    assert "this device event" in html and "app-wide event" in html and "other device event" not in html
    assert html.index("this device event") < html.index("app-wide event")  # newest first


async def test_event_log_without_a_database(ctx):
    ctx.app.state.repo = None
    ctx.session.repo = None
    ctx.app.state.manager.repo = None
    html = (await ctx.http.get(f"{UI}/events")).text
    assert "doesn't have" in html


def test_describe_and_format(ctx):
    s = ctx.session
    assert describe_path(s, "outputs.0.crossover.1.compiled.2") == "Out 1 · crossover 2 · biquad 3"
    assert describe_path(s, "inputs.1.routing.3.enabled") == "In 2 · → Out 4 · enabled"
    assert describe_path(s, "outputs.2.compressor.threshold_db") == "Out 3 · compressor · threshold"
    assert format_value(s, "outputs.0.delay_ms", 9.0) == "9.000 ms (864 samples)"
    assert format_value(s, "outputs.0.gain_db", -3.0) == "−3.00 dB"
    assert format_value(s, "outputs.0.invert", True) == "180°"
    assert format_value(s, "outputs.0.mute", False) == "off"
    assert format_value(s, "outputs.0.mute", None, known=False) == "—"
    assert format_value(s, "outputs.0.peq.0.compiled", {"b0": 1, "b1": 0, "b2": 0, "a1": 0, "a2": 0}).startswith("b0=1")


async def test_adopt_wizard_comes_first_until_adopted(ctx):
    html = (await ctx.http.get("/device")).text
    assert html.index('id="adopt"') < html.index('id="sync"')
    await ctx.session.adopt(AdoptMode.UNKNOWN, preset=0)
    html = (await ctx.http.get("/device")).text
    assert html.index('id="sync"') < html.index('id="adopt"')


async def test_per_row_push_of_a_never_synced_unmute_needs_confirmation(ctx):
    # Not adopted: every path is unknown, so pushing outputs.1.mute (desired off) un-mutes a never-synced output.
    html = (await ctx.http.get(f"{UI}/sync-table", params={"preset": 0})).text
    assert "hx-confirm=" in html and "Un-mute Out 2?" in html
    response = await ctx.http.post(f"{UI}/presets/0/paths/push", data={"path": "outputs.1.mute"}, headers=HX)
    assert response.status_code == 422 and "X-Mdsp-Error" in response.headers
    response = await ctx.http.post(f"{UI}/presets/0/paths/push", data={"path": "outputs.1.gain_db"}, headers=HX)
    assert response.status_code == 422
    await ctx.settle()
    assert ctx.fake.configs == []
    data = {"path": "outputs.1.mute", "confirm": "1"}
    response = await ctx.http.post(f"{UI}/presets/0/paths/push", data=data, headers=HX)
    assert response.status_code == 200
    await ctx.settle()
    assert ctx.fake.outputs_written() == [{"index": 1, "mute": False}]
