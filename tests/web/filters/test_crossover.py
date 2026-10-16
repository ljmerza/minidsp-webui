"""Crossover designer routes on a real session: state, group designs, presets, raw biquads, flatten and refusals."""

import pytest

from app.dsp.design import design_crossover
from app.model.filters import CrossoverDesign

from .conftest import SERIAL, post

XO = f"/ui/devices/{SERIAL}/presets/0/outputs/1/crossover"
LR4_LP80 = {"kind": "lp", "family": "lr", "order": 4, "freq": 80}
LR4_HP20 = {"kind": "hp", "family": "lr", "order": 4, "freq": 20}


def _xover_writes(ctx) -> list[dict]:
    return [g for ch in ctx.fake.outputs_written() for g in ch.get("crossover", [])]


async def test_state(ctx):
    data = (await ctx.http.get(XO)).json()
    assert data["prefix"] == "outputs.1.crossover" and data["n_biquads"] == 4 and data["name"] == "Out 2"
    assert [g["label"] for g in data["groups"]] == ["Flat", "Flat"]
    assert [g["biquads_used"] for g in data["groups"]] == [0, 0]
    assert data["groups"][1]["path"] == "outputs.1.crossover.1" and data["groups"][1]["bypass"] is True
    assert [p["key"] for p in data["presets"]] == ["sub_lp80", "tactile", "flat"]


async def test_design_and_enable_in_one_device_call(ctx):
    response = await post(ctx, f"{XO}/0", {"design": LR4_LP80, "bypass": False})
    assert response.status_code == 200, response.text
    group = response.json()["group"]
    assert group["label"] == "LR4 LP 80 Hz" and group["biquads_used"] == 2 and group["bypass"] is False
    expected = design_crossover(CrossoverDesign(**LR4_LP80), 4)
    assert group["compiled"] == [pytest.approx(bq.model_dump()) for bq in expected]
    await ctx.settle()
    writes = _xover_writes(ctx)
    together = [w for w in writes if w.get("index") == 0 and "coeff" in w and w.get("bypass") is False]
    assert together, writes  # coefficients and the un-bypass travel together (docs 06: safe in one call)


async def test_redesigning_identical_parameters_writes_nothing(ctx):
    """Phase 3 acceptance: re-designing the shaker crossover with identical parameters is a no-op."""
    await post(ctx, XO, {"preset": "tactile"})
    await ctx.settle()
    before = len(ctx.fake.configs)
    for g, design in ((0, LR4_LP80), (1, LR4_HP20)):
        response = await post(ctx, f"{XO}/{g}", {"design": design})
        assert response.status_code == 200 and response.json()["changed"] == []
    again = await post(ctx, XO, {"preset": "tactile"})
    assert again.json()["changed"] == []
    await ctx.settle()
    assert len(ctx.fake.configs) == before


async def test_moving_the_lowpass_rewrites_only_its_group(ctx):
    await post(ctx, XO, {"preset": "tactile"})
    await ctx.settle()
    before = len(ctx.fake.configs)
    response = await post(ctx, f"{XO}/0", {"design": {**LR4_LP80, "freq": 70}})
    assert response.json()["group"]["label"] == "LR4 LP 70 Hz"
    assert all(p.startswith("outputs.1.crossover.0.") for p in response.json()["changed"])
    await ctx.settle()
    new = [g for body in ctx.fake.configs[before:] for ch in body.get("outputs", []) for g in ch.get("crossover", [])]
    assert new and {g["index"] for g in new} == {0}


async def test_presets(ctx):
    data = (await post(ctx, XO, {"preset": "tactile"})).json()
    assert [g["label"] for g in data["groups"]] == ["LR4 LP 80 Hz", "LR4 HP 20 Hz"]
    assert [g["bypass"] for g in data["groups"]] == [False, False]

    data = (await post(ctx, XO, {"preset": "flat"})).json()
    assert [g["bypass"] for g in data["groups"]] == [True, True]
    assert data["groups"][0]["label"] == "LR4 LP 80 Hz"  # bypassed, the design is kept for later

    data = (await post(ctx, XO, {"preset": "sub_lp80"})).json()
    assert [g["bypass"] for g in data["groups"]] == [False, True]
    assert (await post(ctx, XO, {"preset": "nope"})).status_code == 422


async def test_flatten_and_raw_biquads(ctx):
    await post(ctx, f"{XO}/1", {"design": LR4_HP20})
    group = (await post(ctx, f"{XO}/1", {"biquads": []})).json()["group"]
    assert group["label"] == "Flat" and group["biquads_used"] == 0 and group["design"] is None

    sections = [bq.model_dump() for bq in design_crossover(CrossoverDesign(**LR4_LP80), 4)[:2]]
    group = (await post(ctx, f"{XO}/1", {"biquads": sections})).json()["group"]
    assert group["label"] == "LR4 LP 80 Hz"  # recognised from the raw sections

    odd = [{"b0": 0.5, "b1": 0.1, "b2": 0.0, "a1": 0.3, "a2": 0.0}]
    group = (await post(ctx, f"{XO}/1", {"biquads": odd})).json()["group"]
    assert group["label"] == "Custom" and group["design"] is None and group["biquads_used"] == 1
    assert len(group["compiled"]) == 4


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"design": {"kind": "bp", "family": "lr", "order": 8, "freq": 20, "freq_hi": 80}}, "biquads"),
        ({"design": {"kind": "lp", "family": "lr", "order": 3, "freq": 80}}, "even order"),
        ({"biquads": [[1, 0, 0, 2.5, 0.0]]}, "unstable"),
        ({"biquads": [[1, 0, 0, 0, 0]] * 5}, "5 given"),
    ],
)
async def test_refusals(ctx, body, fragment):
    response = await post(ctx, f"{XO}/0", body)
    assert response.status_code == 422
    data = response.json()
    assert fragment in data["detail"]
    if "group" in data:
        assert data["group"]["label"] == "Flat"


async def test_bessel_is_designed_by_the_server(ctx):
    group = (await post(ctx, f"{XO}/0", {"design": {**LR4_LP80, "family": "bessel"}})).json()["group"]
    assert "Bessel" in group["label"] and group["biquads_used"] == 2


async def test_unknown_group_and_channel(ctx):
    assert (await post(ctx, f"{XO}/2", {"design": LR4_LP80})).status_code == 422
    assert (await ctx.http.get(f"/ui/devices/{SERIAL}/presets/0/outputs/9/crossover")).status_code == 404
