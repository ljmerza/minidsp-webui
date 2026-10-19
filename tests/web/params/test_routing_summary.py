"""The Routing page's summary parts (phase 9 design §10.1 / §10.2, P21): column badges, the Total row, the input notes
and the rule text, all from the summary engine, plus the out-of-band refresh endpoint."""

import json
import re

from app.sync.session import AdoptMode
from app.sync.summary import session_summary
from app.web.routing_ui import routing_context, rule_text

from .conftest import SERIAL

# The walkthroughs' state (design 4.1): In 1 feeds Shakers (+4 route, +12 output), Out 3 and Out 4 (muted); the Sub's
# route is off; In 2 feeds nothing.
WALKTHROUGH = {
    "inputs": {
        "0": {"routing": {"0": {"enabled": True, "gain_db": 4}, "1": {"enabled": False}}},
        "1": {"routing": {str(o): {"enabled": False} for o in range(4)}},
    },
    "outputs": {"0": {"gain_db": 12}, "2": {"mute": True}, "3": {"mute": True}},
}


async def _walkthrough(ctx) -> None:
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    await ctx.session.set_channel_meta("output", 0, name="Shakers")
    await ctx.session.set_channel_meta("output", 1, name="Sub")
    await ctx.session.edit(0, WALKTHROUGH, confirm=True)


def _inner(html: str, element_id: str) -> str:
    match = re.search(rf'id="{element_id}"[^>]*>(.*?)</(?:span|ul)>\s*</div>', html, re.S)
    assert match, f"no element {element_id}"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()


async def test_total_row_badges_notes_and_rule(ctx):
    await _walkthrough(ctx)
    html = (await ctx.http.get("/routing")).text
    assert "routing.css" in html
    shakers = _inner(html, "route-total-0")
    assert shakers.startswith("+16.0 dB from In 1 route +4 + output +12")
    assert "Peaks above about −16 dBFS on In 1 will overload Shakers." in shakers
    sub = _inner(html, "route-total-1")
    assert sub.startswith("nothing No input is routed to Sub, so it is silent.")
    assert "Route In 1 → Sub" in sub and "data-values='{\"inputs.0.routing.1.enabled\": true}'" in html
    assert _inner(html, "route-total-2").endswith("Muted Would be 0.0 dB from In 1 (route 0 + output 0).")
    # column heads carry the summary's badge
    assert 'id="route-badge-1" data-health-badge="silent"' in html
    assert 'id="route-badge-2" data-health-badge="muted"' in html
    # the instant "Silent" stand-in is shown for the unrouted Sub only
    assert html.count("data-silent-output=") == 4
    assert "In 2 isn&#39;t sent to any output." in html or "In 2 isn't sent to any output." in html
    # P21: what the app enforces, in words
    assert "limited on its own to the output&#39;s level limit (+12 dB), not as a sum" in html or (
        "limited on its own to the output's level limit (+12 dB), not as a sum" in html
    )
    # phone: muted outputs start with their route switches folded
    fold = r'aria-expanded="{}"\s+:aria-expanded="open.toString\(\)" aria-label="Show the routes to {}"'
    assert re.search(fold.format("false", "Out 3"), html)
    assert re.search(fold.format("true", "Shakers"), html)
    # it re-fetches the summary after changes, without re-rendering the switches
    url = f"/ui/devices/{SERIAL}/presets/0/routing/summary"
    assert f'hx-get="{url}" hx-swap="none"' in html


async def test_route_levels_have_steppers_disabled_with_the_route(ctx):
    await _walkthrough(ctx)
    html = (await ctx.http.get("/routing")).text
    up = r'<button[^>]*data-step-for="inputs\.0\.routing\.0\.gain_db" data-step-db="0\.5"[^>]*>'
    up_on = re.search(up, html).group(0)
    assert "disabled" not in up_on and 'aria-label="Route level from In 1 to Shakers up 0.5 dB"' in up_on
    down_off = re.search(r'<button[^>]*data-step-for="inputs\.0\.routing\.1\.gain_db" data-step-db="-0\.5"[^>]*>', html)
    assert "disabled" in down_off.group(0)


async def test_summary_endpoint_swaps_the_parts_out_of_band(ctx):
    await _walkthrough(ctx)
    await ctx.session.edit(0, {"inputs": {"0": {"routing": {"1": {"enabled": True}}}}}, confirm=True)
    response = await ctx.http.get(f"/ui/devices/{SERIAL}/presets/0/routing/summary")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    html = response.text
    assert html.count('hx-swap-oob="true" class="route-badge') == 4
    for o in range(4):
        assert f'hx-swap-oob="innerHTML:#route-total-{o}"' in html
    assert 'id="route-notes" hx-swap-oob="true"' in html
    assert "0.0 dB" in html and "from In 1" in html and "Route In 1 → Sub" not in html
    assert 'data-path="' not in html  # no switch or field is re-rendered
    assert (await ctx.http.get(f"/ui/devices/{SERIAL}/presets/9/routing/summary")).status_code == 404


async def test_total_row_matches_the_home_cards(ctx):
    """Acceptance (design §17 PR 16): the Total row reads what the Home cards read, from the same summary (the
    browser test test_m4_routing_volume compares the two pages on the mock)."""
    await _walkthrough(ctx)
    summary = session_summary(ctx.session, 0)
    view = routing_context(summary, [12.0] * 4, ["In 1", "In 2"], ["Shakers", "Sub", "Out 3", "Out 4"])
    shakers = view["route_outputs"][0]
    assert (shakers["text"], shakers["detail"]) == (summary.outputs[0].total.text, summary.outputs[0].total.breakdown)
    assert [r["badge"] for r in view["route_outputs"]] == [o.badge for o in summary.outputs]


def test_rule_text_names_each_limit_when_they_differ():
    same = rule_text([12.0, 12.0], ["Shakers", "Sub"])
    assert "(+12 dB), not as a sum: route +12 + output +12 gives a total of up to +24 dB" in same
    mixed = rule_text([12.0, 6.0], ["Shakers", "Sub"])
    assert "(Shakers +12 dB and Sub +6 dB)" in mixed
    assert rule_text([], []) == ""


async def test_row_heads_carry_input_meters_on_the_levels_stream(ctx):
    """Design §10.1 drawing: each input's live meter in its row head (and in the phone cards' input headers), bound to
    a levelMeters component on the page's existing levels stream, with the Settings no-signal floor."""
    await _walkthrough(ctx)
    ctx.settings.guidance_no_signal_dbfs = -80.0
    html = (await ctx.http.get("/routing")).text
    assert "/static/js/meters.js" in html
    match = re.search(r"x-data='levelMeters\((\{.*?\})\)'", html)
    assert match, "no levelMeters component"
    cfg = json.loads(match.group(1))
    assert cfg["inputs"] == ["In 1", "In 2"] and cfg["outputs"] == [] and cfg["floor"] == -80.0
    assert "levels" in re.search(r'sse-connect="([^"]+)"', html).group(1)  # the stream the routing guard already uses
    grid = html.split('class="route-grid"', 1)[1].split('id="route-notes"', 1)[0]
    for key, name in (("i0", "In 1"), ("i1", "In 2")):
        # one in the row head and one in each of the four output cards, named for screen readers
        assert len(re.findall(rf":aria-valuetext=\"namedReading\((?:'|&#39;){key}(?:'|&#39;)\)\"", grid)) == 1 + 4
        assert grid.count(f'aria-label="{name} level"') == 1 + 4
    heads = re.findall(r'<span class="rg-in" .*?</button>', grid, re.S)
    assert len(heads) == 2 and all('role="meter"' in h and "rg-clip" in h for h in heads)
    assert "aria-live" not in grid  # meters are never live regions
