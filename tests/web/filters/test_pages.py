"""The filter UI's page shells: output tabs, the input page's PEQ editor and the Dashboard response widget."""

import json
import re
from html import unescape

from .conftest import SERIAL

UI = f"/ui/devices/{SERIAL}/presets/0"


def _component(html: str, name: str) -> dict:
    """The config object passed to an Alpine component: x-data='name({...})'."""
    match = re.search(rf"x-data='{name}\((.*?)\)'", html, re.S)
    assert match, f"no {name} component"
    return json.loads(unescape(match.group(1)))


def _script_order(html: str) -> list[str]:
    return re.findall(r'<script[^>]*src="[^"]*/static/(?:js|vendor/[a-z]+)/([\w.]+?)\?', html)


async def test_output_tabs_light_up_with_their_templates(ctx):
    html = (await ctx.http.get("/outputs/2")).text
    for label in ("Crossover", "PEQ", "Response"):
        assert re.search(rf'<a class="tab"[^>]*tab={label.lower()}[^>]*>{label}</a>', html), label
    assert re.search(r'class="tab is-disabled"[^>]*>Compressor<', html)


async def test_peq_tab_mounts_the_editor_with_its_routes(ctx):
    html = (await ctx.http.get("/outputs/2?tab=peq")).text
    cfg = _component(html, "peqEditor")
    assert cfg["side"] == "outputs" and cfg["channel"] == 1 and cfg["preset"] == 0
    assert cfg["stateUrl"] == f"{UI}/outputs/1/peq"
    assert cfg["bandUrl"] == f"{UI}/outputs/1/peq/{{band}}"
    assert cfg["fs"] == 96000 and cfg["color"].startswith("#")
    assert 'hx-trigger="sse:sync, sse:desired"' in html
    assert "uPlot.min.css" in html
    order = _script_order(html)
    # graph libraries before the editor; Alpine (deferred) last so the components are registered in time
    positions = [order.index(name) for name in ("uPlot.iife.min.js", "dsp.js", "graph.js", "peq_editor.js")]
    assert positions == sorted(positions)
    assert order[-1] == "alpine.min.js"
    assert "crossover.js" not in order


async def test_crossover_tab(ctx):
    html = (await ctx.http.get("/outputs/1?tab=crossover")).text
    cfg = _component(html, "crossoverEditor")
    assert cfg["stateUrl"] == f"{UI}/outputs/0/crossover"
    assert cfg["groupUrl"] == f"{UI}/outputs/0/crossover/{{group}}"
    assert cfg["nBiquads"] == 4
    order = _script_order(html)
    assert order.index("peq_editor.js") < order.index("crossover.js") < order.index("alpine.min.js")
    assert "Raw coefficients" in html and "Flatten" in html


async def test_response_tab_offers_modes_contributions_and_the_feeding_input(ctx):
    await ctx.session.set_channel_meta("input", 1, name="Right")
    html = (await ctx.http.get("/outputs/3?tab=response")).text
    cfg = _component(html, "responseView")
    assert cfg["url"] == f"{UI}/outputs/2/response"
    assert cfg["refresh"] == ["desired"] and cfg["inputs"] is True
    for text in ("Magnitude", "Phase", "Group delay", "Show contributions", "Unwrap phase"):
        assert text in html
    assert '<option value="1">Right</option>' in html and "(mono sum)" in html


async def test_tabs_follow_the_selected_preset(ctx):
    html = (await ctx.http.get("/outputs/1?tab=peq&preset=3")).text
    cfg = _component(html, "peqEditor")
    assert cfg["preset"] == 2 and cfg["stateUrl"].startswith(f"/ui/devices/{SERIAL}/presets/2/")


async def test_input_page_has_the_peq_editor(ctx):
    html = (await ctx.http.get("/inputs/2")).text
    cfg = _component(html, "peqEditor")
    assert cfg["side"] == "inputs" and cfg["channel"] == 1
    assert cfg["stateUrl"] == f"{UI}/inputs/1/peq"
    assert "arrives with the DSP phase" not in html
    assert "peq_editor.js" in _script_order(html)


async def test_dashboard_response_widget(ctx):
    html = (await ctx.http.get("/")).text
    cfg = _component(html, "responseView")
    assert cfg["url"] == f"/ui/devices/{SERIAL}/response"
    assert cfg["view"] == "sub" and cfg["preset"] is None
    assert set(cfg["refresh"]) == {"sync", "master"}
    assert 'hx-trigger="sse:sync, sse:master"' in html
    order = _script_order(html)
    assert order.index("meters.js") < order.index("peq_editor.js") < order.index("alpine.min.js")


async def test_static_files_are_served(ctx):
    for path in ("/static/js/peq_editor.js", "/static/js/crossover.js", "/static/css/app.css"):
        response = await ctx.http.get(path)
        assert response.status_code == 200, path
