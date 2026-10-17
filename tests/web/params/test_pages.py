"""The Phase 2 pages on a real session: routing matrix, input and output pages, nav, header sync summary."""

import re

from app.sync.session import AdoptMode

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
    assert "/static/js/params.js" in html
    assert "topics=master%2Cdevice%2Csync%2Cdesired" in html


async def test_silent_output_warning(ctx):
    await ctx.session.edit(0, {"inputs": {"0": {"routing": {"0": {"enabled": False}}}}})
    html = (await ctx.http.get("/routing")).text
    assert "hidden" not in _tag(html, 'data-silent-output="0"')


async def test_pages_edit_the_active_preset_by_default_and_any_preset_on_request(ctx):
    ctx.fake.master["preset"] = 2
    await ctx.session.refresh_status()
    html = (await ctx.http.get("/routing")).text
    assert 'data-preset="2"' in html
    html = (await ctx.http.get("/routing?preset=1")).text
    assert 'data-preset="0"' in html
    assert "isn&#39;t active on the device" in html or "isn't active on the device" in html
    assert (await ctx.http.get("/routing?preset=9")).status_code == 404


async def test_input_page_header_strip(ctx):
    await ctx.session.set_channel_meta("input", 0, name="Main L", max_gain_db=6.0)
    response = await ctx.http.get("/inputs/1")
    assert response.status_code == 200
    html = response.text
    assert "Main L" in html
    gain = _tag(html, r'type="range"[^>]*data-path="inputs\.0\.gain_db"')
    assert 'max="6.0"' in gain and 'min="-72.0"' in gain
    assert "ceiling +6.0 dB" in html
    assert 'data-path="inputs.0.mute"' in html
    assert 'data-path="inputs.0.invert"' not in html  # outputs only
    assert "PEQ" in html


async def test_output_page_strip_and_delay_tab(ctx):
    await ctx.session.edit(0, {"outputs": {"1": {"delay_ms": 9.0, "invert": True}}})
    html = (await ctx.http.get("/outputs/2")).text
    for field in ("gain_db", "mute", "invert", "delay_ms"):
        assert f'data-path="outputs.1.{field}"' in html
    assert "checked" in _tag(html, r'data-path="outputs\.1\.invert"')
    assert 'data-samples-for="outputs.1.delay_ms">864<' in html
    assert "data-confirm-unmute" in _tag(html, r'data-path="outputs\.1\.mute"')
    # tabs: Delay is current, the others are live links, FIR is hidden without FIR support
    assert 'class="tab is-current"' in html and ">Delay</a>" in html
    assert re.search(r'<a class="tab"[^>]*>Compressor</a>', html)
    assert ">FIR<" not in html
    # helpers: default distance 3 m at 343 m/s, the other outputs as references, the real device limit
    assert '"distance": 3.0' in html and '"speed": 343.0' in html
    assert '"name": "Out 1"' in html and '"name": "Out 2"' not in html
    assert "79.833 ms (7664 samples" in html


async def test_unknown_channel_and_redirects(ctx):
    assert (await ctx.http.get("/outputs/5")).status_code == 404
    assert (await ctx.http.get("/inputs/0")).status_code == 404
    response = await ctx.http.get("/outputs?preset=2", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/outputs/1?preset=2"
    response = await ctx.http.get("/inputs", follow_redirects=False)
    assert response.headers["location"] == "/inputs/1"


async def test_unknown_serial_is_404(ctx):
    response = await ctx.http.get("/routing?serial=5")
    assert response.status_code == 404 and "Unknown device" in response.text


async def test_nav_links_the_new_pages(ctx):
    html = (await ctx.http.get("/routing")).text
    for path in ("/routing", "/inputs/1", "/outputs/4", "/settings", "/device"):
        assert f'<a href="{path}"' in html, path
    assert '<a href="/routing" aria-current="page"' in html


async def test_adopt_banner_until_adopted(ctx):
    assert "hasn't been adopted yet" in (await ctx.http.get("/outputs/1")).text
    await ctx.session.adopt(AdoptMode.UNKNOWN, preset=0)
    assert "hasn't been adopted yet" not in (await ctx.http.get("/outputs/1")).text


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
    assert re.search(r'class="hdr-sync" href="/device\?preset=1#sync"', html)
    assert 'hx-trigger="sse:sync delay:500ms"' in html
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    assert "all synced" in (await ctx.http.get("/ui/header")).text
    # an edit to inactive preset 2 is staged: its header button and its editing tab get a dot
    await ctx.session.edit(1, {"outputs": {"0": {"gain_db": -3.0}}})
    html = (await ctx.http.get("/routing")).text
    button = _tag(html, 'id="preset-2"')
    assert "has-staged" in button and "1 staged change" in button
    assert "has-staged" not in _tag(html, 'id="preset-1"')
    assert re.search(r'aria-label="Preset 2, 1 staged change\(s\)"', html)
