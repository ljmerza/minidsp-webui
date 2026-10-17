"""The JSON edit endpoint behind params.js, and the sync bar actions (retry, push all, switch & apply)."""

import asyncio

import pytest

from app.sync.session import AdoptMode, DesiredValidationError
from app.web.params import patch_from_values

from .conftest import HX, SERIAL, UI


async def test_edit_writes_and_reports_status(ctx):
    response = await ctx.edit(0, {"outputs.0.gain_db": -3.5})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["values"] == {"outputs.0.gain_db": -3.5}
    assert data["changed"] == ["outputs.0.gain_db"]
    assert data["sync"]["outputs.0.gain_db"] in ("pending", "synced")
    await ctx.settle()
    assert {"index": 0, "gain": -3.5} in ctx.fake.outputs_written()
    assert ctx.session.sync_status(0)["outputs.0.gain_db"] == "synced"
    assert ctx.session.desired(0).outputs[0].gain_db == -3.5


async def test_drag_edits_dont_commit_but_release_does(ctx):
    for value in (-10.0, -9.5, -9.0):
        assert (await ctx.edit(0, {"outputs.0.gain_db": value})).json()["revision"] == 0
    response = await ctx.edit(0, {"outputs.0.gain_db": -9.0}, commit=True)
    assert response.json()["revision"] == 1
    rows = await ctx.session.history(0)
    assert rows[0].source == "ui"


async def test_delay_is_rounded_to_whole_samples(ctx):
    data = (await ctx.edit(0, {"outputs.1.delay_ms": 9.004})).json()
    assert data["values"]["outputs.1.delay_ms"] == pytest.approx(9.0)
    await ctx.settle()
    assert {"index": 1, "delay": {"secs": 0, "nanos": 9_000_000}} in ctx.fake.outputs_written()


async def test_big_raise_needs_confirmation(ctx):
    response = await ctx.edit(0, {"outputs.0.gain_db": 10.0})
    assert response.status_code == 409
    data = response.json()
    assert data["error"] == "confirmation_required" and "confirmation" in data["detail"]
    assert data["values"] == {"outputs.0.gain_db": 0.0}  # the client puts this back on cancel
    await asyncio.sleep(0.05)
    assert ctx.fake.configs == []
    response = await ctx.edit(0, {"outputs.0.gain_db": 10.0}, confirm=True)
    assert response.status_code == 200
    await ctx.settle()
    assert {"index": 0, "gain": 10.0} in ctx.fake.outputs_written()


async def test_ceiling_and_range_are_enforced(ctx):
    await ctx.session.set_channel_meta("output", 2, max_gain_db=3.0)
    response = await ctx.edit(0, {"outputs.2.gain_db": 4.0}, confirm=True)
    assert response.status_code == 422
    data = response.json()
    assert "ceiling of 3 dB" in data["detail"] and data["values"] == {"outputs.2.gain_db": 0.0}
    # a route into output 2 is capped by the same ceiling
    response = await ctx.edit(0, {"inputs.0.routing.2.gain_db": 5.0}, confirm=True)
    assert response.status_code == 422
    response = await ctx.edit(0, {"outputs.0.delay_ms": 80.0})
    assert response.status_code == 422
    await asyncio.sleep(0.05)
    assert ctx.fake.configs == []


@pytest.mark.parametrize(
    ("values", "status"),
    [
        ({"outputs.9.gain_db": 1.0}, 422),
        ({"outputs.0.nonsense": 1.0}, 422),
        ({"outputs.0.mute": "yes"}, 422),  # strict: no string -> bool coercion
        ({"outputs..gain_db": 1.0}, 422),
        ({}, 400),
    ],
)
async def test_bad_edits(ctx, values, status):
    response = await ctx.edit(0, values)
    assert response.status_code == status, response.text


async def test_request_guards(ctx):
    url = f"{UI}/presets/0/edit"
    body = {"values": {"outputs.0.gain_db": -1.0}}
    assert (await ctx.http.post(url, json=body)).status_code == 400  # no HX-Request header
    assert (await ctx.http.post(url, content=b"nope", headers=HX)).status_code == 400
    cross = {**HX, "Sec-Fetch-Site": "cross-site"}
    assert (await ctx.http.post(url, json=body, headers=cross)).status_code == 403
    assert (await ctx.http.post("/ui/devices/5/presets/0/edit", json=body, headers=HX)).status_code == 404
    assert (await ctx.edit(7, {"outputs.0.gain_db": -1.0})).status_code == 404


async def test_routing_edits_and_mono_sum_in_one_request(ctx):
    response = await ctx.edit(
        0,
        {
            "inputs.0.routing.1.enabled": True,
            "inputs.0.routing.1.gain_db": -6.0,
            "inputs.1.routing.1.enabled": True,
            "inputs.1.routing.1.gain_db": -6.0,
        },
        commit=True,
    )
    assert response.status_code == 200, response.text
    await ctx.settle()
    routes = [r for i in ctx.fake.inputs_written() for r in i.get("routing", [])]
    # the device's routing "mute" is the inverse of enabled
    assert any(r["index"] == 1 and r.get("mute") is False and r.get("gain") == -6.0 for r in routes), routes


async def test_edit_to_inactive_preset_is_staged_until_it_is_active(ctx):
    response = await ctx.edit(1, {"outputs.0.gain_db": -2.0})
    assert response.status_code == 200
    await asyncio.sleep(0.1)
    assert ctx.fake.configs == []
    bar = (await ctx.http.get(f"{UI}/presets/1/syncbar", params={"page": "/outputs/1"})).text
    assert "Switch &amp; apply now" in bar and "staged" in bar
    assert 'href="/outputs/1?preset=2"' in bar
    # switching from elsewhere (remote, other app) flushes it
    ctx.fake.master["preset"] = 1
    await ctx.session.refresh_status()
    await ctx.settle(1)
    assert {"index": 0, "gain": -2.0} in ctx.fake.outputs_written()


async def test_failed_sync_keeps_the_desired_value_and_offers_retry(ctx):
    ctx.fake.fail = 500
    ctx.session.writer.max_attempts = 1
    response = await ctx.edit(0, {"outputs.3.gain_db": -4.0})
    assert response.status_code == 200
    await ctx.settle(timeout=2)
    status = ctx.session.sync_status(0)["outputs.3.gain_db"]
    assert status in ("failed", "unknown")
    assert ctx.session.desired(0).outputs[3].gain_db == -4.0  # kept, not reverted
    html = (await ctx.http.get("/outputs/4")).text
    assert 'value="-4.0"' in html
    bar = (await ctx.http.get(f"{UI}/presets/0/syncbar")).text
    if status == "failed":
        assert "Retry now" in bar
    ctx.fake.fail = None
    response = await ctx.http.post(f"{UI}/presets/0/retry", headers=HX)
    assert response.status_code == 200 and "Retrying." in response.text
    for _ in range(50):
        if ctx.session.sync_status(0)["outputs.3.gain_db"] == "synced":
            break
        await asyncio.sleep(0.05)
    assert ctx.session.sync_status(0)["outputs.3.gain_db"] == "synced"


async def test_push_all_from_the_sync_bar(ctx):
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    response = await ctx.http.post(f"{UI}/presets/0/push", params={"page": "/routing"}, headers=HX)
    assert response.status_code == 200
    assert "Push complete" in response.text
    assert len(ctx.fake.configs) > 4
    # mute-wrapped: the first call mutes the outputs
    first = ctx.fake.configs[0]
    assert all(o.get("mute") is True for o in first["outputs"])


async def test_switch_and_apply_now(ctx):
    await ctx.edit(2, {"outputs.1.gain_db": -1.0})
    response = await ctx.http.post(f"{UI}/presets/2/activate", headers=HX)
    assert response.status_code == 200, response.text
    assert response.headers.get("HX-Refresh") == "true"
    assert ctx.fake.master["preset"] == 2
    assert {"index": 1, "gain": -1.0} in ctx.fake.outputs_written()


async def test_actions_require_htmx(ctx):
    for action in ("retry", "push", "activate"):
        assert (await ctx.http.post(f"{UI}/presets/0/{action}")).status_code == 400
    assert (await ctx.http.post(f"{UI}/presets/9/retry", headers=HX)).status_code == 404


def test_patch_from_values():
    assert patch_from_values({"outputs.0.gain_db": 1, "outputs.0.mute": True, "inputs.1.routing.2.enabled": False}) == {
        "outputs": {"0": {"gain_db": 1, "mute": True}},
        "inputs": {"1": {"routing": {"2": {"enabled": False}}}},
    }
    with pytest.raises(DesiredValidationError):
        patch_from_values({"outputs.0": {}, "outputs.0.gain_db": 1})
    with pytest.raises(DesiredValidationError):
        patch_from_values({"outputs.0.gain_db": 1, "outputs.0": {}})


async def test_desired_events_carry_ui_edits(ctx):
    async with ctx.bus.subscribe(["desired"]) as sub:
        await ctx.edit(0, {"outputs.0.mute": True})
        event = await asyncio.wait_for(sub.get(), 1)
    assert event.data["source"] == "ui" and event.data["values"] == {"outputs.0.mute": True}
    assert event.serial == SERIAL
