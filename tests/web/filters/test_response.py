"""Server-computed responses for the Response tab and the Dashboard widget, and their small helpers."""

import pytest

from app.dsp import response as resp
from app.web.filters import compact, parse_indexes, parse_range

from .conftest import SERIAL, post

BASE = f"/ui/devices/{SERIAL}/presets/0"
LR4_LP80 = {"kind": "lp", "family": "lr", "order": 4, "freq": 80}


def _ids(data: dict) -> list[str]:
    return [t["id"] for t in data["traces"]]


def _db_at(trace: dict, freq: float) -> float:
    r = trace["response"]
    i = min(range(len(r["freqs"])), key=lambda k: abs(r["freqs"][k] - freq))
    return r["magnitude_db"][i]


async def test_output_response_total_and_contributions(ctx):
    data = (await ctx.http.get(f"{BASE}/outputs/0/response")).json()
    assert _ids(data) == ["total", "input"]
    total = data["traces"][0]
    assert total["bold"] and total["kind"] == "total" and total["label"] == "Out 1 (total)"
    assert len(total["response"]["freqs"]) == resp.N_POINTS
    assert total["response"]["freqs"][0] == pytest.approx(10) and total["response"]["freqs"][-1] == pytest.approx(20000)
    assert _db_at(total, 1000) == pytest.approx(0, abs=1e-6)

    await post(ctx, f"{BASE}/outputs/0/crossover/0", {"design": LR4_LP80, "bypass": False})
    await post(ctx, f"{BASE}/outputs/0/peq/0", {"design": {"type": "gain", "gain_db": -6}, "bypass": False})
    data = (await ctx.http.get(f"{BASE}/outputs/0/response?range=sub")).json()
    assert _ids(data) == ["total", "input", "peq", "crossover.0"]
    parts = {t["id"]: t for t in data["traces"]}
    assert parts["crossover.0"]["label"] == "Crossover group 1: LR4 LP 80 Hz" and parts["peq"]["dash"]
    assert _db_at(parts["peq"], 50) == pytest.approx(-6, abs=0.01)
    assert _db_at(parts["total"], 80) == pytest.approx(-6 - 6.02, abs=0.1)
    assert parts["total"]["response"]["freqs"][-1] == pytest.approx(300)


async def test_output_response_notes(ctx):
    unroute = {"inputs": {"0": {"routing": {"2": {"enabled": False}}}}}
    await ctx.session.edit(0, {"outputs": {"2": {"mute": True}}, **unroute})
    data = (await ctx.http.get(f"{BASE}/outputs/2/response")).json()
    assert any("muted" in w for w in data["warnings"])
    assert any("no selected input is routed" in w for w in data["warnings"])
    # Out 3 is fed by In 1 only (factory routing); route In 2 to it and plot both inputs
    await ctx.session.edit(0, {"inputs": {"1": {"routing": {"2": {"enabled": True}}}}})
    data = (await ctx.http.get(f"{BASE}/outputs/2/response?inputs=all")).json()
    assert data["inputs"] == [0, 1]
    assert not any("no selected input" in w for w in data["warnings"])
    assert data["traces"][1]["label"].startswith("Input: In 2 (")


async def test_output_response_bad_queries(ctx):
    assert (await ctx.http.get(f"{BASE}/outputs/0/response?range=lots")).status_code == 422
    assert (await ctx.http.get(f"{BASE}/outputs/0/response?inputs=5")).status_code == 422
    assert (await ctx.http.get(f"{BASE}/outputs/7/response")).status_code == 404


async def test_dashboard_response_follows_the_unmuted_outputs_of_the_active_preset(ctx):
    await ctx.session.set_channel_meta("output", 0, name="Shakers")
    await ctx.session.set_channel_meta("output", 1, name="Sub")
    await ctx.session.edit(0, {"outputs": {"2": {"mute": True}, "3": {"mute": True}}})
    response = await ctx.http.get(f"/ui/devices/{SERIAL}/response")
    assert response.status_code == 200
    data = response.json()
    assert data["preset"] == 0 and data["outputs"] == [0, 1] and data["title"] == "Shakers + Sub"
    assert _ids(data) == ["o0", "o1", "sum"]
    assert data["traces"][2]["bold"] and data["traces"][0]["label"] == "Shakers"
    freqs = data["traces"][0]["response"]["freqs"]
    assert len(freqs) == 256 and freqs[0] == pytest.approx(5) and freqs[-1] == pytest.approx(300)
    # Out 1 and Out 2 are fed by different inputs; only In 1 is summed, so Out 2 is silent and the sum is Out 1
    assert _db_at(data["traces"][2], 50) == pytest.approx(0, abs=1e-6)
    assert data["warnings"] == ["Input 1 is not routed to Sub, so it is plotted as silent (this graph feeds Input 1)"]


async def test_dashboard_response_options(ctx):
    data = (await ctx.http.get(f"/ui/devices/{SERIAL}/response?outputs=2&preset=2&range=20-200")).json()
    assert data["preset"] == 2 and _ids(data) == ["o2"]  # one output: no sum trace
    await ctx.session.edit(0, {"outputs": {str(n): {"mute": True} for n in range(4)}})
    data = (await ctx.http.get(f"/ui/devices/{SERIAL}/response")).json()
    assert data["traces"] == [] and data["title"] == "no un-muted output"
    assert (await ctx.http.get(f"/ui/devices/{SERIAL}/response?preset=9")).status_code == 404
    assert (await ctx.http.get("/ui/devices/1/response")).status_code == 404


def test_parse_range():
    assert parse_range(None, "sub") == (5.0, 300.0)
    assert parse_range("full", "sub") == (10.0, 20000.0)
    assert parse_range("20-80.5", "sub") == (20.0, 80.5)
    for bad in ("80-20", "0-100", "x", "10-30000"):
        with pytest.raises(ValueError):
            parse_range(bad, "sub")


def test_parse_indexes():
    assert parse_indexes(None, 4, "outputs") is None
    assert parse_indexes("all", 3, "outputs") == [0, 1, 2]
    assert parse_indexes("2,0,2", 4, "outputs") == [2, 0]
    for bad in ("4", "a"):
        with pytest.raises(ValueError):
            parse_indexes(bad, 4, "outputs")


def test_compact_rounds_but_keeps_every_key():
    r = resp.filter_response([], resp.freq_grid(5, 300, 16))
    d = compact(r)
    assert set(d) == set(r.to_dict())
    assert len(d["freqs"]) == 16 and all(isinstance(v, float) for v in d["magnitude_db"])
