"""The Phase 2 pages on a real session: routing matrix, input and output pages, nav, header sync summary."""

import re

from app.main import STATIC_DIR
from app.model.master import StatusSummary
from app.sync.session import AdoptMode
from app.web.params import ROUTE_LIVE_DBFS, output_routed, would_silence_output

from .conftest import SERIAL


def _tag(html: str, pattern: str) -> str:
    match = re.search(rf"<[a-z]+[^>]*{pattern}[^>]*>", html, re.S)
    assert match, f"no tag matching {pattern}"
    return match.group(0)


async def test_routing_matrix_shows_the_desired_routes(ctx):
    response = await ctx.http.get("/routing")
    assert response.status_code == 200
    html = response.text
    assert "data-params" in html and f'data-edit-url="/ui/devices/{SERIAL}/presets/0/edit"' in html
    # default routing: In 1 -> Out 1/3, In 2 -> Out 2/4
    assert "checked" in _tag(html, r'data-path="inputs\.0\.routing\.0\.enabled"')
    assert "checked" not in _tag(html, r'data-path="inputs\.0\.routing\.1\.enabled"')
    # gains of disabled routes can't be edited; enabled ones can, up to the destination's ceiling
    assert "disabled" in _tag(html, r'data-path="inputs\.0\.routing\.1\.gain_db"')
    enabled_gain = _tag(html, r'data-path="inputs\.0\.routing\.0\.gain_db"')
    assert "disabled" not in enabled_gain and 'max="12.0"' in enabled_gain
    # every output is fed, so no silence warning is visible
    assert html.count("data-silent-output=") == 4
    assert all("hidden" in _tag(html, rf'data-silent-output="{o}"') for o in range(4))
    assert "data-mono-sum" in html and "data-mono-target" in html
    assert "/static/js/params.js" in html and "/static/js/routing.js" in html
    # the routing guard needs live output levels, so this page also subscribes to them
    assert "topics=master%2Cdevice%2Csync%2Cdesired%2Cverify%2Clevels" in html
    assert 'hx-trigger="sse:sync, sse:desired, sse:verify, sse:levels"' in html
    assert "data-output-levels='[]'" in html


async def test_silent_output_warning(ctx):
    await ctx.session.edit(0, {"inputs": {"0": {"routing": {"0": {"enabled": False}}}}})
    html = (await ctx.http.get("/routing")).text
    assert "hidden" not in _tag(html, 'data-silent-output="0"')


async def test_route_cells_carry_what_the_routing_guard_needs(ctx):
    await ctx.session.set_channel_meta("output", 1, name="Sub")
    html = (await ctx.http.get("/routing")).text
    cell = _tag(html, r'data-path="inputs\.1\.routing\.1\.enabled"')
    assert 'data-route-output="1"' in cell and 'data-route-input="1"' in cell
    assert 'data-route-name="Sub"' in cell and 'data-route-input-name="In 2"' in cell
    assert 'data-route-name="Out 1"' in _tag(html, r'data-path="inputs\.0\.routing\.0\.enabled"')


async def test_mono_sum_has_no_default_output_and_starts_disabled(ctx):
    html = (await ctx.http.get("/routing")).text
    assert "Play both inputs on one output (mono sum)" in html
    select = _tag(html, r"data-mono-target")
    assert 'id="mono-target"' in select
    assert '<option value="" selected>Choose an output&hellip;</option>' in html
    assert "disabled" in _tag(html, r"data-mono-sum")
    assert "data-mono-preview" in html and 'aria-live="polite"' in _tag(html, r"data-mono-preview")


async def test_routing_page_passes_the_current_output_levels(ctx):
    ctx.session.monitor.status = StatusSummary(
        master=ctx.session.master, input_levels=[-18.0, -104.0], output_levels=[-6.5, -20.0, -120.0, -95.0]
    )
    html = (await ctx.http.get("/routing")).text
    assert "data-output-levels='[-6.5, -20.0, -120.0, -95.0]'" in html


def _routes(*enabled: tuple[int, int]) -> dict[str, bool]:
    values = {f"inputs.{i}.routing.{o}.enabled": False for i in range(2) for o in range(4)}
    values.update({f"inputs.{i}.routing.{o}.enabled": True for i, o in enabled})
    return values


def test_output_routed():
    values = _routes((0, 0), (1, 0), (0, 1))
    assert output_routed(values, 2, 0) and output_routed(values, 2, 1)
    assert not output_routed(values, 2, 2)
    assert output_routed(values, 2, 0, without=0)  # In 2 still feeds Out 1
    assert not output_routed(values, 2, 1, without=0)
    assert not output_routed({}, 2, 0)  # missing paths count as off


def test_would_silence_output():
    values = _routes((0, 0), (1, 0), (0, 1))
    assert not would_silence_output(values, 2, 0, 0)  # Out 1 keeps In 2
    assert would_silence_output(values, 2, 0, 1)  # In 1 is Out 2's only route
    # Out 2 fed by In 2 only: turning off In 1 (already off) still leaves In 2, turning off In 2 silences it
    values = _routes((1, 1))
    assert not would_silence_output(values, 2, 0, 1)
    assert would_silence_output(values, 2, 1, 1)
    # an output nothing routes to is silent whatever is turned off
    assert would_silence_output(values, 2, 0, 3)


def test_live_threshold_matches_the_js_mirror():
    js = (STATIC_DIR / "js" / "routing.js").read_text()
    assert re.search(rf"const LIVE_DBFS = {int(ROUTE_LIVE_DBFS)};", js)


async def test_pages_edit_the_active_preset_by_default_and_any_preset_on_request(ctx):
    ctx.fake.master["preset"] = 2
    await ctx.session.refresh_status()
    html = (await ctx.http.get("/routing")).text
    assert 'data-preset="2"' in html
    html = (await ctx.http.get("/routing?preset=1")).text
    assert 'data-preset="0"' in html
    assert "isn&#39;t playing" in html or "isn't playing" in html
    assert (await ctx.http.get("/routing?preset=9")).status_code == 404


async def test_input_page_level_tab(ctx):
    # Phase 9 design §9: Level · EQ; the bare URL keeps showing the EQ editor, the level is one chip away.
    await ctx.session.set_channel_meta("input", 0, name="Main L", max_gain_db=6.0)
    bare = (await ctx.http.get("/inputs/1")).text
    assert "peqEditor(" in bare and 'data-chain-tab="level"' in bare
    response = await ctx.http.get("/inputs/1?tab=level")
    assert response.status_code == 200
    html = response.text
    assert "Main L" in html
    gain = _tag(html, r'type="range"[^>]*data-path="inputs\.0\.gain_db"')
    assert 'max="6.0"' in gain and 'min="-72.0"' in gain
    assert "level limit +6.0 dB" in html
    assert 'data-path="inputs.0.mute"' in html
    assert 'data-path="inputs.0.invert"' not in html  # outputs only
    assert "EQ" in html


async def test_output_page_level_tab_and_delay_tab(ctx):
    # Phase 9 design §8: the chain's Level tab opens first; the delay is edited only on its own tab.
    await ctx.session.edit(0, {"outputs": {"1": {"delay_ms": 9.0, "invert": True}}})
    html = (await ctx.http.get("/outputs/2")).text
    for field in ("gain_db", "mute", "invert"):
        assert f'data-path="outputs.1.{field}"' in html
    assert 'data-path="outputs.1.delay_ms"' not in html
    assert "checked" in _tag(html, r'data-path="outputs\.1\.invert"')
    assert "data-confirm-unmute" in _tag(html, r'data-path="outputs\.1\.mute"')
    # chips: Level is current, the others are live links, FIR is hidden without FIR support
    assert re.search(r'<a class="chain-chip[^"]*is-current"[^>]*tab=level[^>]*aria-current="page"', html)
    assert re.search(r'<a class="chain-chip[^"]*"[^>]*tab=compressor', html)
    assert "tab=fir" not in html
    assert '<span class="chain-text">Delay 9 ms</span>' in html

    delay = (await ctx.http.get("/outputs/2?tab=delay")).text
    assert 'data-path="outputs.1.delay_ms"' in delay and 'data-path="outputs.1.gain_db"' not in delay
    assert 'data-samples-for="outputs.1.delay_ms">864<' in delay
    # helpers: default distance 3 m at 343 m/s, the other outputs as references, the real device limit
    assert '"distance": 3.0' in delay and '"speed": 343.0' in delay
    assert '"others": [{"index": 0, "name": "Out 1"}, {"index": 2' in delay  # the other outputs, not itself
    assert "79.833 ms (7664 samples" in delay
    assert "lineUpHelper(" in delay and 'step="0.1" x-model.number="feet"' not in delay


async def test_unknown_channel_and_redirects(ctx):
    assert (await ctx.http.get("/outputs/5")).status_code == 404
    assert (await ctx.http.get("/inputs/0")).status_code == 404
    # a link that names a preset still opens that preset's first channel; the bare URL is the overview (design §4.5)
    response = await ctx.http.get("/outputs?preset=2", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/outputs/1?preset=2"
    response = await ctx.http.get("/inputs?preset=1", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/inputs/1?preset=1"
    response = await ctx.http.get("/inputs", follow_redirects=False)
    assert response.status_code == 200 and "<h1>Inputs</h1>" in response.text


async def test_unknown_serial_is_404(ctx):
    response = await ctx.http.get("/routing?serial=5")
    assert response.status_code == 404 and "Unknown device" in response.text


async def test_nav_links_the_new_pages(ctx):
    html = (await ctx.http.get("/routing")).text
    for path in ("/routing", "/inputs/1", "/outputs/4", "/settings", "/device"):
        assert f'<a href="{path}"' in html, path
    assert '<a href="/routing" aria-current="page"' in html


async def test_adopt_banner_until_adopted(ctx):
    assert "isn't set up yet" in (await ctx.http.get("/outputs/1")).text
    await ctx.session.adopt(AdoptMode.UNKNOWN, preset=0)
    assert "isn't set up yet" not in (await ctx.http.get("/outputs/1")).text


async def test_sync_badges_and_initial_state(ctx):
    html = (await ctx.http.get("/outputs/1")).text
    badge = _tag(html, r'data-sync-for="outputs\.0\.gain_db"')
    assert "sync-unknown" in badge
    state = re.search(r"<script type=\"application/json\" data-sync-state>(.*?)</script>", html, re.S).group(1)
    assert '"outputs.0.gain_db": "unknown"' in state
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    html = (await ctx.http.get("/outputs/1")).text
    assert "sync-synced" in _tag(html, r'data-sync-for="outputs\.0\.gain_db"')


async def test_header_sync_summary_and_staged_dots(ctx):
    html = (await ctx.http.get("/routing")).text
    assert "not tracked yet" not in html
    assert re.search(r'class="hdr-status" href="/device\?preset=1#sync"', html)
    assert "sse:sync throttle:1s, sse:sync delay:500ms, sse:verify delay:300ms, mdsp-sync-changed from:body" in html
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    assert "All sent" in (await ctx.http.get("/ui/header")).text
    # an edit to inactive preset 2 waits: its header button and its editing tab get a ⏸, and the header says so in
    # words (no hover-only title, phase 9 §13.1)
    await ctx.session.edit(1, {"outputs": {"0": {"gain_db": -3.0}}})
    html = (await ctx.http.get("/routing")).text
    button = _tag(html, 'id="preset-2"')
    assert "has-staged" in button and "title=" not in button
    # Named by its content ("Preset 2, 1 change waiting"), which holds the phone sheet's visible line (WCAG 2.5.3).
    body = html.split('id="preset-2"', 1)[1].split("</button>", 1)[0]
    assert '<span class="visually-hidden">Preset </span>2' in body
    assert '<span class="preset-desc"><span class="visually-hidden">, </span>1 change waiting</span>' in body
    assert "has-staged" not in _tag(html, 'id="preset-1"')
    assert re.search(r'aria-label="Preset 2, 1 change\(s\) waiting"', html)
    assert "Preset 2: 1 waiting" in html
