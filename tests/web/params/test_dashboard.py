"""Dashboard output cards and sidebar names backed by the real preset state."""

import re

from app.model.master import MasterStatus
from app.sync.session import AdoptMode

from .conftest import SERIAL

FRAGMENT = f"/ui/dashboard/outputs?serial={SERIAL}"


def _card(html: str, n: int) -> str:
    cards = re.findall(r'<li class="output-card"[^>]*>(.*?)</li>', html, re.S)
    return cards[n]


def _card_tag(html: str, n: int) -> str:
    return re.findall(r'<li class="output-card"[^>]*>', html, re.S)[n]


def _button(card: str, action: str, step: str | None = None) -> str:
    pattern = (
        rf'<button[^>]*data-action="{action}"' + (rf'[^>]*data-step="{re.escape(step)}"' if step else "") + "[^>]*>"
    )
    match = re.search(pattern, card, re.S)
    assert match, (action, step)
    return match.group(0)


async def test_output_cards_show_desired_values_and_sync(ctx):
    html = (await ctx.http.get("/")).text
    assert "not tracked yet" not in html
    first = _card(html, 0)
    assert "sync-unknown" in first and "bypassed" in first

    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    await ctx.session.edit(0, {"outputs": {"1": {"gain_db": -4.5, "delay_ms": 1.5}}})
    await ctx.settle()
    html = (await ctx.http.get("/")).text
    second = _card(html, 1)
    assert "−4.5 dB" in second and "1.50 ms" in second
    assert "sync-synced" in second


async def test_saved_channel_names_appear_in_nav_and_dashboard(ctx):
    await ctx.session.set_channel_meta("output", 3, name="Shakers")
    html = (await ctx.http.get("/")).text
    assert 'href="/outputs/4"' in html
    assert re.search(r'href="/outputs/4"[^>]*>\s*Shakers', html)
    assert "<h3>Shakers</h3>" in html


async def test_output_cards_have_quick_mute_and_gain_nudge_for_the_active_preset(ctx):
    html = (await ctx.http.get("/")).text
    assert f'data-edit-url="/ui/devices/{SERIAL}/presets/0/edit"' in html and 'data-preset="0"' in html
    tag = _card_tag(html, 0)
    assert 'data-output="0"' in tag and 'data-gain="0.0"' in tag and 'data-mute="false"' in tag
    assert 'data-ceiling="12.0"' in tag and 'data-gain-min="-72.0"' in tag
    assert 'data-never-synced="true"' in tag  # un-muting asks first until the device confirmed gain and mute
    card = _card(html, 0)
    assert 'aria-pressed="false"' in _button(card, "mute")
    assert "disabled" not in _button(card, "nudge", "0.5") and "disabled" not in _button(card, "nudge", "-0.5")

    # the buttons post through the output pages' edit endpoint, so the same guards apply
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    assert (await ctx.edit(0, {"outputs.0.gain_db": 0.5, "outputs.0.mute": True}, commit=True)).status_code == 200
    await ctx.settle()
    html = (await ctx.http.get(FRAGMENT)).text
    tag, card = _card_tag(html, 0), _card(html, 0)
    assert 'data-gain="0.5"' in tag and 'data-mute="true"' in tag and 'data-never-synced="false"' in tag
    assert "+0.5 dB (muted)" not in card and "0.5 dB (muted)" in card
    mute = _button(card, "mute")
    assert 'aria-pressed="true"' in mute and "is-muted" in mute


async def test_gain_nudges_stop_at_the_ceiling_and_the_floor(ctx):
    await ctx.session.set_channel_meta("output", 1, max_gain_db=3.0)
    await ctx.session.edit(0, {"outputs": {"1": {"gain_db": 3.0}, "2": {"gain_db": -72.0}}}, confirm=True)
    html = (await ctx.http.get(FRAGMENT)).text
    assert 'data-ceiling="3.0"' in _card_tag(html, 1)
    at_ceiling = _button(_card(html, 1), "nudge", "0.5")
    assert "disabled" in at_ceiling and "At its level limit (+3.0 dB)" in _card(html, 1)
    assert "disabled" in _button(_card(html, 2), "nudge", "-0.5")
    assert "disabled" not in _button(_card(html, 2), "nudge", "0.5")
    # a raise past the ceiling is refused by the edit endpoint even if a stale card offered it
    response = await ctx.edit(0, {"outputs.1.gain_db": 3.5}, commit=True)
    assert response.status_code == 422 and "ceiling" in response.json()["detail"]


async def test_cards_fragment_follows_edits_from_elsewhere_and_the_active_preset(ctx):
    html = (await ctx.http.get("/")).text
    assert "−6.0 dB" not in _card(html, 3)

    await ctx.session.edit(0, {"outputs": {"3": {"gain_db": -6.0}}}, source="mqtt")  # Home Assistant, another tab...
    html = (await ctx.http.get(FRAGMENT)).text
    assert "<html" not in html and "−6.0 dB" in _card(html, 3)
    assert 'data-preset="0"' in html

    await ctx.session.edit(1, {"outputs": {"3": {"gain_db": -9.0}}})
    ctx.session.monitor.update_master(MasterStatus(preset=1))  # preset switch from the IR remote
    html = (await ctx.http.get(FRAGMENT)).text
    assert 'data-preset="1"' in html and f'data-edit-url="/ui/devices/{SERIAL}/presets/1/edit"' in html
    assert "−9.0 dB" in _card(html, 3)


async def test_cards_without_a_known_active_preset_have_no_quick_controls(ctx):
    ctx.session.monitor.status = ctx.session.monitor.status.model_copy(
        update={"master": ctx.session.monitor.status.master.model_copy(update={"preset": None})}
    )
    html = (await ctx.http.get(FRAGMENT)).text
    assert 'data-preset=""' in html and "data-edit-url" not in html and 'data-action="mute"' not in html
    assert html.count('class="output-card"') == 4


# --- phase 9 summary engine: the cards' sentence and health badge (design 6, 17 PR 6 "early win") ---------------------

WALKTHROUGH_PATCH = {
    "inputs": {
        "0": {"routing": {"0": {"enabled": True, "gain_db": 4.0}, "1": {"enabled": False}, "2": {"enabled": True}}},
        "1": {"routing": {"1": {"enabled": False}, "3": {"enabled": False}}},
    },
    "outputs": {"0": {"gain_db": 12.0, "delay_ms": 8.75}, "2": {"mute": True}, "3": {"mute": True}},
}


async def _walkthrough(ctx) -> None:
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    await ctx.session.set_channel_meta("output", 0, name="Shakers")
    await ctx.session.set_channel_meta("output", 1, name="Sub")
    await ctx.session.edit(0, WALKTHROUGH_PATCH, confirm=True)
    route_in1_out4 = {"inputs": {"0": {"routing": {"3": {"enabled": True}}}}}
    await ctx.session.edit(0, route_in1_out4, confirm=True)
    await ctx.settle()


async def test_output_cards_say_what_each_output_does_and_flag_the_silent_one(ctx):
    await _walkthrough(ctx)
    html = (await ctx.http.get(FRAGMENT)).text
    shakers, sub, out3 = _card(html, 0), _card(html, 1), _card(html, 2)
    assert "Plays full range, fed by In 1. Waits 8.75 ms." in shakers
    assert 'data-health-badge="check"' in shakers  # +16 dB total: a headroom caution
    assert "Peaks above about −16 dBFS on In 1 will overload it." in shakers
    assert "Plays full range, fed by nothing." in sub
    assert 'data-health-badge="silent"' in sub and "⛔" in sub and "Silent" in sub
    assert "Sub is silent: no input is routed to it." in sub and 'href="/routing">Open Routing</a>' in sub
    assert "Muted. Would play full range, fed by In 1." in out3 and 'data-health-badge="muted"' in out3

    # "Not in use" folds the muted output's note into its badge
    await ctx.session.set_channel_meta("output", 2, unused=True)
    out3 = _card((await ctx.http.get("/")).text, 2)
    assert "Not in use: muted." in out3 and 'data-health-badge="not_in_use"' in out3

    # fixing the route turns the Sub ready
    await ctx.session.edit(0, {"inputs": {"0": {"routing": {"1": {"enabled": True}}}}})
    await ctx.settle()
    sub = _card((await ctx.http.get(FRAGMENT)).text, 1)
    assert 'data-health-badge="ready"' in sub and "✓" in sub and "Plays full range, unprocessed, fed by In 1." in sub


async def test_summary_api(ctx):
    from app.api import v1

    ctx.app.include_router(v1.build_router())
    await _walkthrough(ctx)
    response = await ctx.http.get(f"/api/v1/devices/{SERIAL}/presets/0/summary")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["serial"] == SERIAL and data["preset"] == 0
    assert [o["badge"] for o in data["outputs"]] == ["check", "silent", "muted", "muted"]
    assert data["outputs"][1]["sentence"] == "Plays full range, fed by nothing."
    assert data["outputs"][0]["total"]["text"] == "+16.0 dB"
    assert data["inputs"][1]["sentence"] == "Not sent to any output."
    assert data["problems"][0]["code"] == "no_route" and data["counts"]["problem"] == 1
    assert data["problems"][0]["actions"][0] == {
        "id": "route",
        "label": "Route In 1 → Sub",
        "href": None,
        "task": None,
        "params": {"input": 0, "output": 1},
    }
    live = await ctx.http.get(f"/api/v1/devices/{SERIAL}/presets/0/summary", params={"live": "true"})
    assert live.status_code == 200
    assert (await ctx.http.get(f"/api/v1/devices/{SERIAL}/presets/7/summary")).status_code == 404
    assert (await ctx.http.get("/api/v1/devices/1/presets/0/summary")).status_code == 404
