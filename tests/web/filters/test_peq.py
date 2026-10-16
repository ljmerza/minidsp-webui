"""PEQ editor routes on a real session: state, band edits (design, raw, bypass), bank actions and refusals."""

import pytest

from app.dsp.design import design_filter, peaking
from app.model.filters import FilterDesign

from .conftest import HX, SERIAL, post

BASE = f"/ui/devices/{SERIAL}/presets/0"
OUT1 = f"{BASE}/outputs/1/peq"
PK = {"type": "peaking", "freq": 42.0, "q": 4.0, "gain_db": -3.5}


def _peq_writes(ctx, side: str = "outputs") -> list[dict]:
    """Every PEQ entry the daemon received, in order: [{"index": k, "coeff"?: …, "bypass"?: …}, …]."""
    writes = ctx.fake.outputs_written() if side == "outputs" else ctx.fake.inputs_written()
    return [entry for ch in writes for entry in ch.get("peq", [])]


async def test_state_lists_every_band_with_labels_and_sync(ctx):
    response = await ctx.http.get(OUT1)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["prefix"] == "outputs.1.peq" and data["side"] == "outputs" and data["channel"] == 1
    assert data["name"] == "Out 2" and data["fs"] == 96000
    assert len(data["bands"]) == 10
    band = data["bands"][3]
    assert band["index"] == 3 and band["path"] == "outputs.1.peq.3"
    assert band["bypass"] is True and band["source"] == "flat" and band["design"]["type"] == "passthrough"
    assert band["compiled"] == {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}
    assert data["sync"]["outputs.1.peq.3.compiled"] in ("unknown", "synced", "pending")
    assert all(p.startswith("outputs.1.peq.") for p in data["sync"])


async def test_design_then_enable_writes_coefficients_before_the_unbypass(ctx):
    response = await post(ctx, f"{OUT1}/0", {"design": PK})
    assert response.status_code == 200, response.text
    data = response.json()
    band = data["band"]
    assert band["design"] == {**PK, "lt": None, "raw": None}
    assert band["compiled"] == pytest.approx(design_filter(FilterDesign(**PK)).model_dump())
    assert band["bypass"] is True and band["source"] == "designed"
    assert "PK" in band["label"] or "42" in band["label"]
    assert "outputs.1.peq.0.compiled" in data["changed"]
    assert set(data["sync"]) <= {"outputs.1.peq.0.compiled", "outputs.1.peq.0.bypass"}
    await ctx.settle()

    response = await post(ctx, f"{OUT1}/0", {"bypass": False})
    assert response.status_code == 200
    assert response.json()["band"]["bypass"] is False
    await ctx.settle()
    writes = _peq_writes(ctx)
    coeff_at = next(i for i, w in enumerate(writes) if w.get("index") == 0 and "coeff" in w)
    unbypass_at = next(i for i, w in enumerate(writes) if w.get("index") == 0 and w.get("bypass") is False)
    assert coeff_at < unbypass_at
    state = (await ctx.http.get(OUT1)).json()
    assert state["sync"]["outputs.1.peq.0.compiled"] == "synced"
    assert state["sync"]["outputs.1.peq.0.bypass"] == "synced"


async def test_resending_the_same_design_changes_and_writes_nothing(ctx):
    await post(ctx, f"{OUT1}/2", {"design": PK, "bypass": False})
    await ctx.settle()
    before = len(ctx.fake.configs)
    response = await post(ctx, f"{OUT1}/2", {"design": PK})
    assert response.status_code == 200
    assert response.json()["changed"] == []
    await ctx.settle()
    assert len(ctx.fake.configs) == before


async def test_unstable_raw_biquad_is_refused_with_the_stored_band(ctx):
    response = await post(ctx, f"{OUT1}/1", {"raw": [1, 0, 0, 1.5, 0.2]})
    assert response.status_code == 422
    data = response.json()
    assert data["error"] == "invalid" and "unstable" in data["detail"]
    assert data["band"]["source"] == "flat"  # what the editor puts back
    assert ctx.fake.configs == []


async def test_raw_coefficients_are_stored_exactly_and_recognised(ctx):
    bq = peaking(1000.0, 2.0, 6.0)
    response = await post(ctx, f"{OUT1}/4", {"raw": bq.model_dump()})
    assert response.status_code == 200
    band = response.json()["band"]
    assert band["compiled"] == bq.model_dump()
    assert band["design"]["type"] == "peaking" and band["source"] == "designed"

    odd = {"b0": 0.9, "b1": -1.7, "b2": 0.81, "a1": 1.7, "a2": -0.75}
    band = (await post(ctx, f"{OUT1}/5", {"raw": odd})).json()["band"]
    assert band["compiled"] == odd and band["source"] == "raw" and band["design"]["type"] == "raw"


async def test_reset_band(ctx):
    await post(ctx, f"{OUT1}/6", {"design": PK, "bypass": False})
    response = await post(ctx, f"{OUT1}/6", {"design": {"type": "passthrough"}, "bypass": True})
    band = response.json()["band"]
    assert band["source"] == "flat" and band["bypass"] is True


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"design": {"type": "peaking", "freq": 100, "gain_db": 3}}, "requires q"),
        ({"design": {"type": "peaking", "freq": 60000, "q": 1, "gain_db": 3}}, "Nyquist"),
        ({"design": PK, "raw": [1, 0, 0, 0, 0]}, "not both"),
        ({}, "nothing to change"),
        ({"design": PK, "bogus": 1}, "bogus"),
    ],
)
async def test_bad_bodies_are_422(ctx, body, fragment):
    response = await post(ctx, f"{OUT1}/0", body)
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


async def test_input_bands(ctx):
    response = await post(ctx, f"{BASE}/inputs/0/peq/9", {"design": PK, "bypass": False})
    assert response.status_code == 200
    assert response.json()["band"]["path"] == "inputs.0.peq.9"
    await ctx.settle()
    assert any(w.get("index") == 9 for w in _peq_writes(ctx, "inputs"))


async def test_bypass_all_and_reset_all_leave_external_bands_alone(ctx):
    for k in (0, 1, 2):
        await post(ctx, f"{OUT1}/{k}", {"design": PK, "bypass": False})
    await ctx.session.edit(0, {"outputs": {"1": {"peq": {"2": {"external": True}}}}})

    response = await post(ctx, OUT1, {"action": "bypass_all"})
    assert response.status_code == 200
    data = response.json()
    bypassed = [b["bypass"] for b in data["bands"]]
    assert bypassed[0] and bypassed[1] and not bypassed[2]
    assert sorted(data["changed"]) == ["outputs.1.peq.0.bypass", "outputs.1.peq.1.bypass"]
    assert (await post(ctx, OUT1, {"action": "bypass_all"})).json()["changed"] == []

    data = (await post(ctx, OUT1, {"action": "reset_all"})).json()
    sources = [b["source"] for b in data["bands"]]
    assert sources[:3] == ["flat", "flat", "external"] and set(sources[3:]) == {"flat"}

    # an external band can't be edited here
    response = await post(ctx, f"{OUT1}/2", {"bypass": True})
    assert response.status_code == 422 and "read-only" in response.json()["detail"]


async def test_bank_action_is_validated(ctx):
    assert (await post(ctx, OUT1, {"action": "explode"})).status_code == 422


async def test_unknown_device_preset_channel_and_band(ctx):
    assert (await ctx.http.get("/ui/devices/5/presets/0/outputs/0/peq")).status_code == 404
    assert (await ctx.http.get(f"/ui/devices/{SERIAL}/presets/7/outputs/0/peq")).status_code == 404
    assert (await ctx.http.get(f"{BASE}/outputs/4/peq")).status_code == 404
    assert (await ctx.http.get(f"{BASE}/inputs/2/peq")).status_code == 404
    assert (await ctx.http.get(f"{BASE}/sideways/0/peq")).status_code == 422
    response = await post(ctx, f"{OUT1}/10", {"design": PK})
    assert response.status_code == 422 and "band 10" in response.json()["detail"]


async def test_writes_need_the_htmx_header_and_same_origin(ctx):
    response = await ctx.http.post(f"{OUT1}/0", json={"design": PK})
    assert response.status_code == 400
    response = await ctx.http.post(f"{OUT1}/0", json={"design": PK}, headers={**HX, "Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    response = await ctx.http.post(f"{OUT1}/0", content=b"not json", headers=HX)
    assert response.status_code == 400


async def test_edits_are_logged_as_ui_edits(ctx):
    await post(ctx, f"{OUT1}/0", {"design": PK})
    await ctx.session.commit(0)
    history = await ctx.session.history(0)
    assert history and history[0].source == "ui"
