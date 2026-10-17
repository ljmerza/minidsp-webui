"""The System page, its data route and the JSON API, against a real DeviceSession with a mocked daemon."""

import math
import re

import pytest

from app.model.filters import CrossoverDesign
from app.sync.filters import set_crossover_group

from .conftest import API, SERIAL, UI

LR4_LP80 = {"kind": "lp", "family": "lr", "order": 4, "freq": 80.0}


async def route_in1_to_sub(ctx, preset: int = 0) -> None:
    await ctx.session.edit(preset, {"inputs": {"0": {"routing": {"1": {"enabled": True}}}}})


def _ids(data: dict) -> list[str]:
    return [t["id"] for t in data["traces"]]


def _db_at(response: dict, freq: float) -> float:
    i = min(range(len(response["freqs"])), key=lambda k: abs(response["freqs"][k] - freq))
    return response["magnitude_db"][i]


# --- page -----------------------------------------------------------------------------------------------------------


async def test_page_renders(ctx):
    r = await ctx.http.get("/system")
    assert r.status_code == 200
    html = r.text
    assert "<h1>System</h1>" in html
    assert "Electrical sum" in html and "electrical sum" in html.lower()
    assert "Sub/Tactile 5&ndash;300 Hz" in html and "Full 10 Hz&ndash;20 kHz" in html
    assert "x-data='systemView(" in html
    assert "/static/js/system.js" in html and "/static/js/graph.js" in html and "uPlot.iife.min.js" in html
    assert f"/ui/devices/{SERIAL}/presets/0/system" in html
    assert 'aria-current="page"' in html and ">System</a>" in html  # the sidebar entry lights up
    assert "20&ndash;100 Hz" in html and "120&deg;" in html
    # one checkbox per output, plus the pair selects
    assert html.count('type="checkbox" :checked="isSelected(') == 4
    assert html.count('<option value="0">Out 1</option>') == 2


async def test_page_names_mutes_and_preset(ctx):
    await ctx.session.set_channel_meta("output", 0, name="Shakers")
    await ctx.session.set_channel_meta("output", 1, name="Sub")
    await ctx.session.edit(1, {"outputs": {"2": {"mute": True}}})
    r = await ctx.http.get("/system?preset=2")
    assert r.status_code == 200
    html = r.text
    assert f"/ui/devices/{SERIAL}/presets/1/system" in html
    assert re.search(r'class="preset-tab is-selected"[^>]*href="[^"]*preset=2"', html)
    assert "Shakers" in html and "Sub" in html
    assert "&#34;pair&#34;: [0, 1]" in html or '"pair": [0, 1]' in html
    assert '"muted": [2]' in html
    # the muted output's checkbox starts disabled, the others checked
    assert html.count(" disabled>") == 1 and html.count(" checked>") == 3


async def test_page_errors(ctx):
    assert (await ctx.http.get("/system?preset=9")).status_code == 404
    r = await ctx.http.get("/system?serial=123")
    assert r.status_code == 404


# --- UI data route --------------------------------------------------------------------------------------------------


async def test_data_defaults(ctx):
    await route_in1_to_sub(ctx)
    r = await ctx.http.get(f"{UI}/0/system")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["outputs"] == [0, 1, 2, 3] and data["inputs"] == [0] and data["muted"] == []
    assert data["range"] == [5.0, 300.0]
    assert _ids(data) == ["o0", "o1", "o2", "o3", "sum"]
    total = data["traces"][-1]
    assert total["label"] == "Electrical sum" and total["bold"] and total["kind"] == "sum"
    # In 1 feeds Out 1-3, Out 4 is silent: three coherent unity outputs
    assert _db_at(total["response"], 50) == pytest.approx(20 * math.log10(3), abs=1e-3)
    assert "no selected input is routed to Out 4, so it is silent" in data["warnings"]
    integ = data["integration"]
    assert (integ["a"], integ["b"]) == (0, 1) and (integ["label_a"], integ["label_b"]) == ("Out 1", "Out 2")
    assert integ["range"] == [pytest.approx(20), pytest.approx(100)] and integ["threshold_deg"] == 120
    assert integ["regions"] == [] and integ["color_a"].startswith("#")


async def test_data_delay_and_polarity(ctx):
    await route_in1_to_sub(ctx)
    await ctx.session.set_channel_meta("output", 0, name="Shakers")
    await ctx.session.set_channel_meta("output", 1, name="Sub")
    await ctx.session.edit(0, {"outputs": {"0": {"delay_ms": 9.0}}})
    data = (await ctx.http.get(f"{UI}/0/system?outputs=0,1")).json()
    assert _ids(data) == ["o0", "o1", "sum"]
    (region,) = data["integration"]["regions"]
    assert region["f_lo"] == pytest.approx(37.04, abs=0.2) and region["f_hi"] == pytest.approx(74.07, abs=0.2)
    assert data["integration"]["label_a"] == "Shakers"
    # the electrical sum dips where the delayed output is 180° out
    assert _db_at(data["traces"][-1]["response"], 55.6) < -20

    await ctx.session.edit(0, {"outputs": {"0": {"delay_ms": 0.0, "invert": True}}})
    data = (await ctx.http.get(f"{UI}/0/system?outputs=0,1&pair=1,0&range=full")).json()
    assert data["range"] == [10.0, 20000.0]
    (region,) = data["integration"]["regions"]
    assert (region["f_lo"], region["f_hi"]) == (pytest.approx(20), pytest.approx(100))
    assert data["integration"]["label_a"] == "Sub"


async def test_data_muted_selection_and_pair_off(ctx):
    await route_in1_to_sub(ctx)
    await ctx.session.edit(0, {"outputs": {"1": {"mute": True}}})
    data = (await ctx.http.get(f"{UI}/0/system")).json()
    assert data["outputs"] == [0, 2, 3] and data["muted"] == [1]
    assert "Out 2 is muted; the integration panel treats it as un-muted" in data["warnings"]

    data = (await ctx.http.get(f"{UI}/0/system?outputs=0&pair=none")).json()
    assert _ids(data) == ["o0"] and data["integration"] is None  # one output: no separate sum trace

    data = (await ctx.http.get(f"{UI}/0/system?outputs=none&pair=none")).json()
    assert data["traces"] == [] and data["outputs"] == []

    data = (await ctx.http.get(f"{UI}/0/system?inputs=all&outputs=all&pair=none")).json()
    assert data["inputs"] == [0, 1] and data["outputs"] == [0, 1, 2, 3]
    assert "Out 2 is muted, so it adds nothing to the sum" in data["warnings"]


async def test_data_follows_filters(ctx):
    await route_in1_to_sub(ctx)
    await set_crossover_group(ctx.session, 0, 0, 0, design=CrossoverDesign(**LR4_LP80), bypass=False)
    data = (await ctx.http.get(f"{UI}/0/system?outputs=0,1")).json()
    o0 = next(t for t in data["traces"] if t["id"] == "o0")
    assert _db_at(o0["response"], 80) == pytest.approx(-6.02, abs=0.2)
    assert _db_at(o0["response"], 300) < -40


@pytest.mark.parametrize(
    "query",
    ["range=lots", "outputs=7", "outputs=x", "inputs=3", "pair=1", "pair=0,0", "pair=0,9"],
)
async def test_data_bad_queries(ctx, query):
    r = await ctx.http.get(f"{UI}/0/system?{query}")
    assert r.status_code == 422 and r.json()["error"] == "invalid"


async def test_data_unknown_device_and_preset(ctx):
    assert (await ctx.http.get("/ui/devices/1/presets/0/system")).status_code == 404
    r = await ctx.http.get(f"{UI}/4/system")
    assert r.status_code == 404 and r.json()["error"] == "unknown_preset"


# --- JSON API -------------------------------------------------------------------------------------------------------


async def test_api_system(ctx):
    await route_in1_to_sub(ctx)
    await ctx.session.edit(0, {"outputs": {"0": {"delay_ms": 9.0}, "2": {"mute": True}}})
    r = await ctx.http.get(f"{API}/0/system")
    assert r.status_code == 200
    data = r.json()
    assert data["label"] == "electrical sum"
    assert data["outputs"] == [0, 1, 3] and data["range"] == [5.0, 300.0]
    assert sorted(data["channels"]) == ["0", "1", "3"]
    assert len(data["sum"]["freqs"]) == 512 and "group_delay_ms" in data["sum"]
    (region,) = data["integration"]["regions"]
    assert region["f_lo"] == pytest.approx(37.04, abs=0.1)
    assert len(data["integration"]["freqs"]) == 256

    data = (await ctx.http.get(f"{API}/0/system?outputs=none&pair=none&points=16&range=20-200")).json()
    assert data["sum"] is None and data["channels"] == {} and data["integration"] is None
    assert data["range"] == [pytest.approx(20), pytest.approx(200)]

    data = (await ctx.http.get(f"{API}/0/system?outputs=0,1&threshold=170")).json()
    (region,) = data["integration"]["regions"]
    assert data["integration"]["threshold_deg"] == 170
    assert region["f_lo"] > 50 and region["f_hi"] < 61


@pytest.mark.parametrize(
    ("query", "status"),
    [("pair=3,3", 422), ("outputs=9", 422), ("range=10", 422), ("range=1-90000", 422), ("threshold=0", 422)],
)
async def test_api_bad_queries(ctx, query, status):
    assert (await ctx.http.get(f"{API}/0/system?{query}")).status_code == status


async def test_api_unknown(ctx):
    assert (await ctx.http.get(f"{API}/7/system")).status_code == 404
    assert (await ctx.http.get("/api/v1/devices/1/presets/0/system")).status_code == 404
