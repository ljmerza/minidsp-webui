"""The global shell (phase 9 design §3, PR 8): nav groups, the problems pill, Help me…, the CLIP light, the phone's
short status word, the amber editing band and the Settings channel cards."""

import re
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app.sync.status import Progress
from app.web import shell
from app.web.nav import NavItem, nav_groups

from .conftest import UI


def _nav(html: str) -> str:
    return html.split('id="sidenav"', 1)[1].split("</nav>", 1)[0]


# --- navigation -------------------------------------------------------------------------------------------------------


def test_nav_groups_split_everyday_settings_and_help():
    items = [
        NavItem("Home", "/"),
        NavItem("Guided tasks", "/tasks"),
        NavItem("Profiles", "/profiles"),
        NavItem("Meters & graph", "/meters"),
        NavItem("Routing", "/routing"),
        NavItem("Settings", "/settings"),
        NavItem("Glossary", "/help"),
    ]
    groups = nav_groups(items)
    assert [(g.label, [i.label for i in g.items]) for g in groups] == [
        ("Everyday", ["Home", "Guided tasks", "Profiles"]),
        ("All settings", ["Meters & graph", "Routing", "Settings"]),
        ("Help", ["Glossary"]),
    ]
    assert [g.id for g in groups] == ["nav-everyday", "nav-all-settings", "nav-help"]


async def test_sidebar_has_the_three_groups_and_keeps_every_page(ctx):
    nav = _nav((await ctx.http.get("/routing")).text)
    headings = re.findall(r'<h2 class="nav-group[^"]*" id="([^"]+)">([^<]+)</h2>', nav)
    assert [h[1] for h in headings] == ["Everyday", "All settings", "Help", "Help me&hellip;"]
    everyday = nav.split('id="nav-everyday"', 1)[1].split('id="nav-all-settings"', 1)[0]
    assert '<a href="/">Home</a>' in everyday and "Profiles" in everyday  # this test app doesn't mount /profiles
    settings = nav.split('id="nav-all-settings"', 1)[1].split('id="nav-help"', 1)[0]
    for label, path in (("Routing", "/routing"), ("Device &amp; sync", "/device"), ("Settings", "/settings")):
        assert f">{label}</a>" in settings and f'href="{path}"' in settings, label
    assert "Combined response" in settings  # listed (not mounted in this test app)
    assert 'href="/outputs/1"' in settings and 'href="/inputs/2"' in settings
    # Pages later PRs add aren't listed until they exist; Meters & graph (PR 9) is.
    assert "Guided tasks" not in nav
    assert '<a href="/meters">Meters &amp; graph</a>' in settings and '<a href="/outputs">Outputs</a>' in settings
    assert '<a href="/routing" aria-current="page">Routing</a>' in nav


async def test_menu_button_sits_outside_the_re_rendered_header(ctx):
    html = (await ctx.http.get("/settings")).text
    header = html.split('<header class="app-header">', 1)[1].split('id="app-header"', 1)[0]
    assert 'class="nav-toggle"' in header and 'aria-controls="sidenav"' in header
    assert 'x-data="navSheet"' in html


# --- Help me… ---------------------------------------------------------------------------------------------------------


def _request_with(*paths: str) -> SimpleNamespace:
    app = FastAPI()
    for path in paths:
        app.add_api_route(path, lambda: None)
    return SimpleNamespace(app=app, scope={"root_path": ""})


def test_help_me_lists_seven_tasks_then_the_glossary_with_fallbacks_until_the_wizards_exist():
    entries = shell.help_menu(_request_with())
    assert [e["label"] for e in entries] == [
        "Something wrong?",
        "Make one louder",
        "Fix a boom",
        "Line up shakers",
        "Set levels",
        "Save or load a setup",
        "Set up this device",
        "Glossary",
    ]
    assert entries[-1]["href"] == "/help"
    today = {"/", "/outputs", "/meters", "/profiles", "/device#adopt", "/outputs/1?tab=peq", "/outputs/1?tab=delay"}
    assert {e["href"] for e in entries[:-1]} <= today


def test_help_me_links_a_wizard_once_its_page_is_served():
    request = _request_with("/tasks/{task_id}")
    assert shell.task_href(request, "eq-cut") == "/tasks/eq-cut"
    assert shell.task_href(request, "nope") is None
    assert shell.meters_href(_request_with()) == "/#levels-title"
    assert shell.meters_href(_request_with("/meters")) == "/meters"


# --- the phone's status word ------------------------------------------------------------------------------------------


def test_short_status_fits_the_phone_header():
    def view(state, **counts):
        return SimpleNamespace(state=state, counts=counts, progress=None)

    assert shell.short_status(view("sent")) == "Sent"
    assert shell.short_status(view("failed", failed=2)) == "2 failed"
    assert shell.short_status(view("changed", changed=3)) == "3 changed"
    assert shell.short_status(view("waiting", waiting=216)) == "216 waiting"
    assert shell.short_status(view("unknown", unknown=12)) == "12 unknown"
    sending = SimpleNamespace(state="sending", counts={"sending": 64}, progress=Progress(120, 184))
    assert shell.short_status(sending) == "120/184"
    assert shell.short_status(None) == ""


# --- header -----------------------------------------------------------------------------------------------------------


async def _silence_out2(ctx) -> None:
    values = {f"inputs.{i}.routing.1.enabled": False for i in range(2)}
    assert (await ctx.edit(0, values, commit=True)).status_code == 200


async def test_problems_pill_counts_problems_and_lists_them_with_fixes(ctx):
    await _silence_out2(ctx)
    html = (await ctx.http.get("/ui/header")).text
    pill = re.search(
        r'<button type="button" class="hdr-sheet-toggle hdr-pill pill-warn"[^>]*>(.*?)</button>', html, re.S
    )
    assert pill, "no problems pill"
    count = int(re.search(r'<span class="pill-count">(\d+)</span>', pill.group(1)).group(1))
    assert count >= 1 and "problem" in pill.group(1)
    panel = html.split('id="hdr-problems-list"', 1)[1]
    assert f"Problems ({count})" in panel
    assert "Out 2 is silent" in panel
    # The inline fix posts to the editors' endpoint; a task action falls back to today's page.
    assert re.search(r'data-shell-fix data-url="/ui/devices/\d+/presets/0/edit" data-preset="0"', panel)
    assert "inputs.0.routing.1.enabled" in panel
    assert "Something wrong? (full check)" in panel
    announce = re.search(r'data-problems-announce="([^"]*)"', html).group(1)
    assert announce.startswith(f"{count} problem") and "silent" in announce


async def test_header_rows_mute_all_clip_and_help_me(ctx):
    html = (await ctx.http.get("/ui/header")).text
    top, bottom = html.split('class="hdr-row hdr-top"', 1)[1].split('class="hdr-row hdr-bottom"', 1)
    assert 'id="hdr-presets"' in top and 'id="master-source"' in top and 'class="hdr-status"' in top
    assert 'id="hdr-helpme-list"' in top and "Set up this device" in top and 'href="/help">Glossary' in top
    assert 'id="master-mute"' in bottom and '<span class="mute-word">Mute all</span>' in bottom
    assert 'id="master-volume"' in bottom and 'class="hdr-sheet-toggle vol-open"' in bottom
    assert "hdr-clip-led" in bottom and 'href="/meters">Show the meters' in bottom  # Meters & graph (design §12.3)
    # The phone's "P1 ▾" and short status word.
    assert re.search(r"P1 &#9662;", top)
    assert '<span class="st-short" aria-hidden="true">' in top


async def test_every_page_listens_to_levels_for_the_clip_light(ctx):
    for path in ("/settings", "/device", "/help"):
        html = (await ctx.http.get(path)).text
        feed = re.search(r'sse-connect="[^"]*topics=([^&"]+)', html).group(1)
        assert "levels" in feed.split("%2C"), path
        assert 'data-clip-feed hx-trigger="sse:levels"' in html
        assert '"o": ["Out 1", "Out 2", "Out 3", "Out 4"]' in html.replace("&#34;", '"')


# --- the amber band ---------------------------------------------------------------------------------------------------


async def test_amber_band_when_editing_a_preset_that_isnt_playing(ctx):
    bar = (await ctx.http.get(f"{UI}/presets/1/syncbar", params={"page": "/outputs/1?tab=delay"})).text
    band = bar.split("data-editing-band", 1)[1]
    assert "You're editing preset 2. It isn't playing (preset 1 is)." in band
    assert "Switch to preset 2 now" in band and f'hx-post="{UI}/presets/1/activate' in band
    assert 'href="/outputs/1?tab=delay&amp;preset=1">Edit preset 1 instead</a>' in band
    playing = (await ctx.http.get(f"{UI}/presets/0/syncbar", params={"page": "/routing"})).text
    assert "data-editing-band" not in playing and "Switch to preset" not in playing


async def test_amber_band_on_the_edit_pages(ctx):
    for path in ("/routing?preset=2", "/inputs/1?preset=2", "/outputs/2?preset=2&tab=peq"):
        html = (await ctx.http.get(path)).text
        assert "data-editing-band" in html and "Edit preset 1 instead" in html, path


# --- Settings: channel cards on phones ---------------------------------------------------------------------------


async def test_settings_channel_cells_carry_their_labels_for_the_phone_cards(ctx):
    html = (await ctx.http.get("/settings")).text
    form = html.split('id="settings-channels"', 1)[1].split("</form>", 1)[0]
    assert form.count('data-label="Name"') == 6 and form.count('data-label="Colour"') == 6
    assert form.count('data-label="Level limit"') == 6 and form.count('class="no-limit"') == 2


async def test_shell_assets_are_served(ctx):
    for path in ("/static/js/shell.js", "/static/css/shell.css"):
        response = await ctx.http.get(path)
        assert response.status_code == 200, path
    assert isinstance(ctx.http, httpx.AsyncClient)
