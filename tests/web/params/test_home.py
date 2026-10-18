"""Home and the Inputs / Outputs overviews (phase 9 design §4, PR 9): the §4.3 level rule, the page and its parts,
Meters & graph at /meters, and the overview pages."""

import re

from app.sync import profiles as svc
from app.sync.session import AdoptMode
from app.sync.summary import RouteTotal, _total, session_summary
from app.web.home import card_view, level_step

from .conftest import SERIAL, UI

# --- the level rule (design §4.3) ------------------------------------------------------------------------------------


def _routes(*levels: float, input_db: float = 0.0) -> tuple[RouteTotal, ...]:
    return tuple(
        RouteTotal(i, f"In {i + 1}", input_db, route, input_db + route, muted=False) for i, route in enumerate(levels)
    )


def _with_output(routes: tuple[RouteTotal, ...], output: float, ceiling: float = 12.0):
    fixed = tuple(
        RouteTotal(r.input, r.name, r.input_db, r.route_db, r.input_db + r.route_db + output, r.muted) for r in routes
    )
    return _total(fixed, output, ceiling)


def test_plus_below_the_limit_raises_the_output_level():
    step = level_step(_with_output(_routes(4.0), 11.0), 0, +1, gain_min=-72.0)
    assert step.target == "output" and step.values == {"outputs.0.gain_db": 11.5}
    # it stops exactly at the limit
    assert level_step(_with_output(_routes(4.0), 11.8), 0, +1, gain_min=-72.0).values == {"outputs.0.gain_db": 12.0}


def test_plus_at_the_limit_raises_every_enabled_route_by_the_same_step():
    total = _with_output(_routes(4.0, -2.0), 12.0)
    step = level_step(total, 1, +1, gain_min=-72.0)
    assert step.target == "routes"
    assert step.values == {"inputs.0.routing.1.gain_db": 4.5, "inputs.1.routing.1.gain_db": -1.5}
    # a route already at the limit stays where it is; the others still rise
    step = level_step(_with_output(_routes(12.0, 11.8), 12.0), 1, +1, gain_min=-72.0)
    assert step.values == {"inputs.1.routing.1.gain_db": 12.0}


def test_plus_with_both_at_their_limits_is_disabled_and_says_why():
    total = _with_output(_routes(12.0), 12.0)
    assert total.at_limit and total.route_at_limit
    step = level_step(total, 0, +1, gain_min=-72.0)
    assert step.disabled and step.values == {} and step.target == "none"
    # nothing routed: nothing to raise past the output's limit either
    assert level_step(_with_output((), 12.0), 0, +1, gain_min=-72.0).disabled


def test_minus_lowers_the_output_level_only():
    total = _with_output(_routes(12.0), 12.0)
    step = level_step(total, 0, -1, gain_min=-72.0)
    assert step.target == "output" and step.values == {"outputs.0.gain_db": 11.5}
    assert not any(path.startswith("inputs.") for path in step.values)
    assert level_step(_with_output(_routes(4.0), -71.8), 0, -1, gain_min=-72.0).values == {"outputs.0.gain_db": -72.0}
    assert level_step(_with_output(_routes(4.0), -72.0), 0, -1, gain_min=-72.0).disabled


def test_a_lower_settings_limit_caps_the_output_and_its_routes():
    total = _with_output(_routes(3.0), 3.0, ceiling=3.0)
    assert level_step(total, 0, +1, gain_min=-72.0).disabled
    assert level_step(_with_output(_routes(1.0), 3.0, ceiling=3.0), 0, +1, gain_min=-72.0).values == {
        "inputs.0.routing.0.gain_db": 1.5
    }


# --- pages -----------------------------------------------------------------------------------------------------------

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
    await ctx.session.edit(0, {"inputs": {"0": {"routing": {"3": {"enabled": True}}}}}, confirm=True)
    await ctx.settle()


def _card(html: str, number: int) -> str:
    match = re.search(
        rf'<li class="output-card home-card" id="home-out{number}".*?(?=<li class="(?:output-card|home-fold)|</ul>)',
        html,
        re.S,
    )
    assert match, f"no card for output {number}"
    return match.group(0)


def _visible_notes(card: str) -> list[str]:
    notes = re.findall(r'<p class="home-note[^"]*" data-note="(\w+)"([^>]*)>', card)
    return [name for name, attrs in notes if "hidden" not in attrs]


async def test_home_is_the_start_page_with_every_part_in_order(ctx):
    await _walkthrough(ctx)
    response = await ctx.http.get("/")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    html = response.text
    main = html.split('<main id="main"', 1)[1]
    order = [
        main.index("home-health-card"),
        main.index('class="card flow-card"'),
        main.index('id="home-outputs-title"'),
        main.index('id="home-strip-title"'),
        main.index('id="home-presets-title"'),
        main.index('id="home-tasks-title"'),
    ]
    assert order == sorted(order)
    assert "<title>Home · minidsp-webui</title>" in html
    assert '<a href="/" aria-current="page">Home</a>' in html
    assert "/static/js/start.js" in html.split("</head>", 1)[0]  # the Start page check runs before the page draws
    # the health line: the silent Sub with its inline fix, and the Shakers caution
    health = main.split('class="home-health-box"', 1)[1].split("</section>", 1)[0]
    assert "Sub is silent: no input is routed to it." in health
    fix = re.search(r"<button[^>]*data-shell-fix[^>]*>Route In 1 → Sub</button>", health, re.S).group(0)
    assert f'data-url="{UI}/presets/0/edit"' in fix and 'data-preset="0"' in fix
    assert "data-values='{\"inputs.0.routing.1.enabled\": true}'" in fix
    assert "Shakers: +16 dB total." in health and "notes (not problems)" in health
    # the diagram's Level blocks scroll to the cards
    assert 'href="#home-out1"' in main


async def test_the_shakers_card_explains_the_limit_and_the_sub_offers_its_fix(ctx):
    await _walkthrough(ctx)
    html = (await ctx.http.get("/")).text
    shakers = _card(html, 1)
    assert "Plays 20–80 Hz" not in shakers  # no band filter in this state
    assert "Plays full range, fed by In 1. Waits 8.75 ms." in shakers
    assert "data-total-text>+16.0 dB<" in shakers and "route +4 + output +12" in shakers
    assert 'data-health-badge="check"' in shakers
    assert _visible_notes(shakers) == ["routes"]
    assert "Output is at its +12 dB limit: + now raises the route In 1 → Shakers." in shakers
    assert "Peaks above about −16 dBFS on In 1 will overload Shakers." in shakers
    plus = re.search(r'<button[^>]*data-action="level"[^>]*data-step="0.5"[^>]*>', shakers).group(0)
    assert "disabled" not in plus
    assert 'data-routes=\'[{"in": 0.0, "input": 0, "name": "In 1", "route": 4.0}]\'' in shakers
    assert 'aria-label="Mute Shakers"' in shakers and 'aria-pressed="false"' in shakers
    assert 'href="/outputs/1?tab=crossover">Band (crossover)</a>' in shakers and ">Rename</a>" in shakers

    sub = _card(html, 2)
    assert 'data-health-badge="silent"' in sub and "Sub is silent: no input is routed to it." in sub
    assert "data-shell-fix" in sub and "Route In 1 → Sub" in sub
    assert _visible_notes(sub) == []


async def test_plus_at_the_limit_raises_the_route_and_the_card_follows(ctx):
    await _walkthrough(ctx)
    summary = session_summary(ctx.session)
    view = card_view(summary.outputs[0], gain_min=-72.0, root="")
    assert view["plus"].values == {"inputs.0.routing.0.gain_db": 4.5}
    # the card's + posts exactly that through the editors' endpoint
    response = await ctx.edit(0, view["plus"].values, commit=True)
    assert response.status_code == 200
    await ctx.settle()
    shakers = _card((await ctx.http.get(f"/ui/home/cards?serial={SERIAL}")).text, 1)
    assert "data-total-text>+16.5 dB<" in shakers and "route +4.5 + output +12" in shakers
    assert ctx.session.desired(0).outputs[0].gain_db == 12.0  # the output stayed at its limit

    # both at the limit: + is disabled and the card says why, with a way out
    await ctx.session.edit(0, {"inputs": {"0": {"routing": {"0": {"gain_db": 12.0}}}}}, confirm=True)
    shakers = _card((await ctx.http.get(f"/ui/home/cards?serial={SERIAL}")).text, 1)
    plus = re.search(r'<button[^>]*data-action="level"[^>]*data-step="0.5"[^>]*>', shakers).group(0)
    assert "disabled" in plus
    assert _visible_notes(shakers) == ["max"]
    assert "Shakers is at its maximum (+24.0 dB total)." in shakers and "Lower the other outputs" in shakers
    minus = re.search(r'<button[^>]*data-action="level"[^>]*data-step="-0.5"[^>]*>', shakers).group(0)
    assert "disabled" not in minus


async def test_outputs_marked_not_in_use_fold_into_one_line(ctx):
    await _walkthrough(ctx)
    await ctx.session.set_channel_meta("output", 2, unused=True)
    await ctx.session.set_channel_meta("output", 3, unused=True)
    html = (await ctx.http.get("/")).text
    fold = re.search(r'<li class="home-fold" data-fold>(.*?)</li>', html, re.S).group(1)
    assert "Out 3 · Out 4" in fold and "Not in use" in fold and "Muted." in fold
    assert 'aria-expanded="false"' in fold and "Show" in fold
    assert re.search(r'id="home-out3"[^>]*x-show="showUnused" x-cloak data-folded', html)
    # /outputs shows every card unfolded, each with a link to its full page
    overview = (await ctx.http.get("/outputs")).text
    assert "data-fold" not in overview and "data-folded" not in overview
    assert overview.count('class="output-card home-card"') == 4
    assert 'href="/outputs/3">All settings for Out 3' in overview


async def test_first_run_replaces_the_cards_until_the_device_is_set_up(ctx):
    html = (await ctx.http.get("/")).text  # never adopted
    assert "data-first-run" in html and "The app doesn't know what your miniDSP holds yet." in html
    assert 'href="/device#adopt">Set up this device&hellip;</a>' in html
    assert 'class="output-card home-card"' not in html


async def test_meter_strip_lists_every_channel_and_links_meters_and_graph(ctx):
    await _walkthrough(ctx)
    html = (await ctx.http.get("/")).text
    strip = html.split('id="home-strip-title"', 1)[1].split("</section>", 1)[0]
    for name in ("In 1", "In 2", "Shakers", "Sub", "Out 3", "Out 4"):
        assert f'aria-label="{name} level"' in strip and f'aria-label="{name} CLIP light"' in strip
    assert strip.count("(muted)") == 2  # Out 3 and Out 4
    assert 'href="/meters">Meters &amp; graph' in strip


async def test_presets_and_profiles_say_what_plays_and_what_waits(ctx):
    await _walkthrough(ctx)
    await svc.save_preset(ctx.session, 0, "Movies")
    await ctx.session.edit(1, {"outputs": {"0": {"gain_db": -3.0}}})
    html = (await ctx.http.get(f"/ui/home/presets?serial={SERIAL}")).text
    assert "Device preset 1 is playing. Same as &ldquo;Movies&rdquo;." in html
    assert "Preset 2: 1 change waiting" in html and 'href="/device?preset=2#sync">What\'s waiting?' in html
    switch = re.search(r'<button[^>]*hx-post="[^"]*/master/preset"[^>]*>', html, re.S).group(0)
    assert "hx-confirm=" in switch and '"preset": 1' in switch and 'hx-target="#app-header"' in switch
    # a profile button opens the load step (the Profiles page until the wizard exists); it never loads in one tap
    link = re.search(r'<a class="btn-link home-profile" href="([^"]+)"', html).group(1)
    assert re.fullmatch(r"/profiles#profile-\d+", link)
    assert "hx-post" not in html.split("home-profile-list", 1)[1]


async def test_guided_tasks_card_lists_the_seven_tasks(ctx):
    html = (await ctx.http.get("/")).text
    tasks = html.split('id="home-tasks-title"', 1)[1].split("</section>", 1)[0]
    labels = re.findall(r'data-task="[\w-]+">([^<]+)</a>', tasks)
    assert labels == [
        "Something wrong?",
        "Make one louder",
        "Fix a boom",
        "Line up shakers",
        "Set levels",
        "Save or load a setup",
        "Set up this device",
    ]
    # until its wizard exists, "Something wrong?" is Home's own health line, not a link back to Home
    assert 'href="#home-health-title" data-task="diagnose"' in tasks


async def test_meters_and_graph_keep_the_old_dashboard(ctx):
    html = (await ctx.http.get("/meters")).text
    assert "<title>Meters &amp; graph · minidsp-webui</title>" in html
    assert 'id="levels-title"' in html and "responseView(" in html and 'id="outputs-title"' in html
    assert '<a href="/meters" aria-current="page">Meters &amp; graph</a>' in html
    assert "flow-card" not in html  # the diagram lives on Home now


async def test_inputs_overview_says_where_each_input_goes(ctx):
    await _walkthrough(ctx)
    html = (await ctx.http.get("/inputs")).text
    assert "<h1>Inputs</h1>" in html and '<a href="/inputs" aria-current="page">Inputs</a>' in html
    assert "Level 0 dB, no EQ. Sent to Shakers (+4 dB), Out 3 and Out 4." in html
    assert "Not sent to any output." in html and "In 2 isn&#39;t sent to any output." in html
    assert 'href="/inputs/1">All settings for In 1' in html


async def test_home_parts_are_served_as_fragments(ctx):
    await _walkthrough(ctx)
    for part in ("health", "cards", "outputs", "presets", "inputs"):
        response = await ctx.http.get(f"/ui/home/{part}?serial={SERIAL}")
        assert response.status_code == 200, part
        assert "<html" not in response.text and response.headers["cache-control"] == "no-store"
    assert (await ctx.http.get("/ui/home/nope")).status_code == 404
    cards = (await ctx.http.get(f"/ui/home/cards?serial={SERIAL}")).text
    assert f'data-edit-url="{UI}/presets/0/edit"' in cards and 'data-preset="0"' in cards
