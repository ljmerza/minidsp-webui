"""Phase 9 PR 8 (the global shell, design §3 and §16) in a real browser, on the embedded mock.

The state is the walkthroughs' (design 4.1, as in test_m2_flow): the Sub's route is off, so the Sub is silent (a
problem), and Shakers has a high total (a caution).

Acceptance: at 390×844 the header is at most 48 px tall, the bottom bar is 64 px and hides while a number field has
focus, no interactive control is smaller than 44 px (inline links in sentences excepted), and no page scrolls sideways.
The amber band appears on every edit page when editing preset 2 while preset 1 plays.
"""

import json
import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_harness import PAGES
from tests.e2e.test_m2_flow import mark_unused
from tests.e2e.test_m2_summary import CUT, ROUTING

# The pages that edit one preset (routing, inputs, outputs and every output tab).
EDIT_PAGES = [
    "/routing",
    "/inputs/1",
    "/outputs/1",
    "/outputs/1?tab=crossover",
    "/outputs/1?tab=peq",
    "/outputs/1?tab=delay",
    "/outputs/1?tab=compressor",
    "/outputs/1?tab=response",
]

# Every visible interactive control smaller than 44×44 px. A checkbox or radio inside a <label> counts as the label's
# box. Links inside running text (display: inline) are allowed: they are the inline-link allowlist of the design.
SMALL_TARGETS = """() => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], '
    + '[tabindex]:not([tabindex="-1"])';
  const small = [];
  for (const el of document.querySelectorAll(sel)) {
    const own = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || own.width === 0 || own.height === 0) continue;
    if (el.closest('[inert]') || el.classList.contains('skip-link')) continue;
    if (el.tagName === 'A' && cs.display === 'inline') continue;
    const box = (el.type === 'checkbox' || el.type === 'radio') && el.closest('label') ? el.closest('label') : el;
    const r = box.getBoundingClientRect();
    if (r.width < 43.5 || r.height < 43.5) {
      const name = (el.getAttribute('aria-label') || el.textContent || el.name || '').trim().replace(/\\s+/g, ' ');
      const tag = `${el.tagName.toLowerCase()}.${[...el.classList].join('.')}`;
      small.push(`${tag} "${name.slice(0, 40)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
  }
  return small;
}"""


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        params = {"confirm": "true", "commit": "true"}
        api._ok(api.http.patch(f"{api.dev}/presets/0", params=params, json=ROUTING))
        api.put_peq_band("outputs", 0, 0, CUT)
        mark_unused(api, (2, 3))
        api.wait_synced()

    return walkthrough


def _box(page: Page, selector: str) -> dict:
    box = page.locator(selector).first.bounding_box()
    assert box, f"{selector} isn't displayed"
    return box


# --- phone: 48 px header, 64 px bottom bar, 44 px targets, no sideways scroll ---------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_phone_shell_on_every_page(phone: Page, path: str):
    visit(phone, path)
    assert _box(phone, "header.app-header")["height"] <= 48
    bar = _box(phone, ".hdr-bottom")
    assert bar["height"] == 64 and bar["y"] + bar["height"] == 844
    small = phone.evaluate(SMALL_TARGETS)
    assert not small, f"touch targets under 44 px on {path}: {small}"
    assert not has_horizontal_scroll(phone), f"{path} scrolls sideways"


def test_phone_header_stays_put_and_the_bar_hides_while_typing(phone: Page, shoot):
    visit(phone, "/outputs/1")
    shoot(phone, "p9-shell-output")
    phone.mouse.wheel(0, 600)
    phone.wait_for_timeout(200)
    assert _box(phone, "header.app-header")["y"] == 0  # sticky
    bar = phone.locator(".hdr-bottom")
    expect(bar).to_be_visible()
    field = phone.get_by_role("spinbutton", name="Output level in dB")
    field.focus()
    expect(bar).to_be_hidden()
    phone.locator("h1").click()
    expect(bar).to_be_visible()


def test_phone_volume_sheet_opens_from_the_bottom_bar(phone: Page, shoot):
    visit(phone, "/")
    slider = phone.locator("#master-volume")
    expect(slider).to_be_hidden()
    toggle = phone.locator(".vol-open")
    expect(toggle).to_contain_text(re.compile(r"Volume\s*−?\d+\.\d dB"))
    toggle.tap()
    expect(slider).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    sheet = _box(phone, "#hdr-volume-sheet")
    assert sheet["y"] + sheet["height"] <= 844 - 64 + 1  # just above the bar
    expect(phone.locator(".hdr-bottom")).to_be_visible()  # the sheet's own number field doesn't hide the bar
    phone.locator("#master-volume-num").focus()
    expect(phone.locator(".hdr-bottom")).to_be_visible()
    shoot(phone, "p9-shell-volume-sheet")
    phone.keyboard.press("Escape")
    expect(slider).to_be_hidden()
    expect(toggle).to_be_focused()


def test_phone_preset_sheet_says_what_each_preset_holds(phone: Page, shoot):
    visit(phone, "/")
    toggle = phone.locator(".hdr-preset-toggle")
    expect(toggle).to_contain_text("P1")
    expect(phone.locator("#preset-2")).to_be_hidden()
    toggle.tap()
    sheet = phone.locator("#hdr-presets")
    expect(sheet).to_be_visible()
    expect(phone.locator("#preset-1 .preset-desc")).to_be_visible()
    expect(phone.locator("#preset-1")).to_contain_text("Playing")
    expect(phone.locator("#preset-2")).to_contain_text("Not playing")
    assert _box(phone, "#hdr-presets")["y"] == 48
    shoot(phone, "p9-shell-preset-sheet")
    phone.keyboard.press("Escape")
    expect(sheet).to_be_hidden()


def test_phone_menu_sheet_has_the_groups_and_closes_with_escape(phone: Page, shoot):
    visit(phone, "/routing")
    menu = phone.get_by_role("button", name="Menu")
    nav = phone.get_by_role("navigation", name="Main")
    expect(nav).to_be_hidden()
    menu.tap()
    expect(nav).to_be_visible()
    for group in ("Everyday", "All settings", "Help", "Help me…"):
        expect(nav.get_by_role("heading", name=group, exact=True)).to_be_visible()
    expect(nav.get_by_role("link", name="Routing")).to_be_focused()  # the current page
    expect(nav.get_by_role("link", name="Something wrong?")).to_be_visible()
    expect(nav).to_contain_text("2x4HD")
    assert phone.evaluate("() => document.getElementById('main').inert")
    shoot(phone, "p9-shell-menu")
    phone.keyboard.press("Escape")
    expect(nav).to_be_hidden()
    expect(menu).to_be_focused()
    assert not phone.evaluate("() => document.getElementById('main').inert")


def test_phone_toasts_sit_above_the_bottom_bar(phone: Page):
    visit(phone, "/")
    phone.evaluate("() => window.mdsp.toast('Shakers muted.', 'info')")
    toast = _box(phone, "#toasts .toast")
    assert toast["y"] + toast["height"] <= 844 - 64


# --- desktop header --------------------------------------------------------------------------------------------------


def test_desktop_header_rows(desktop: Page, shoot):
    visit(desktop, "/")
    top, bottom = _box(desktop, ".hdr-top"), _box(desktop, ".hdr-bottom")
    assert bottom["y"] >= top["y"] + top["height"] - 1
    header = desktop.locator("#app-header")
    expect(header.locator(".hdr-top")).to_contain_text("Device preset")
    expect(header.locator(".hdr-top .hdr-pill")).to_contain_text(re.compile(r"⚠\s*\d+ problems?"))
    expect(header.locator(".hdr-top .hdr-status")).to_be_visible()
    expect(header.get_by_role("button", name="Help me…")).to_be_visible()
    expect(header.locator(".hdr-bottom #master-mute")).to_have_text(re.compile(r"Mute all"))
    expect(header.locator(".hdr-bottom #master-volume")).to_be_visible()
    expect(header.locator(".hdr-bottom .hdr-clip-led")).to_contain_text("CLIP")
    expect(desktop.get_by_role("button", name="Menu")).to_be_hidden()
    nav = desktop.get_by_role("navigation", name="Main")
    expect(nav).to_be_visible()
    headings = nav.get_by_role("heading").all_inner_texts()
    assert [h.strip().lower() for h in headings if h.strip()] == ["everyday", "all settings", "help"]
    shoot(desktop, "p9-shell-header")


def test_problems_pill_lists_problems_and_notes_and_fixes_inline(desktop: Page, api: Api, shoot):
    visit(desktop, "/routing")
    pill = desktop.locator(".hdr-pill")
    panel = desktop.locator("#hdr-problems-list")
    expect(panel).to_be_hidden()
    pill.click()
    expect(panel).to_be_visible()
    expect(pill).to_have_attribute("aria-expanded", "true")
    expect(panel.get_by_role("heading", name=re.compile(r"Problems \(\d+\)"))).to_be_visible()
    expect(panel).to_contain_text("Sub is silent")
    expect(panel.get_by_role("link", name="Something wrong? (full check)")).to_be_visible()
    assert_accessible(desktop, include="#app-header")
    shoot(desktop, "p9-shell-problems")

    # Esc closes it, with focus back on the pill.
    desktop.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    expect(pill).to_be_focused()

    # The inline fix routes In 1 to the Sub; Undo takes it back.
    pill.click()
    panel.get_by_role("button", name=re.compile(r"Route In 1 → Sub")).click()
    wait_until(lambda: api.desired(0)["inputs"][0]["routing"][1]["enabled"], what="the route to be on")
    toast = desktop.locator("#toasts .toast").last
    expect(toast).to_contain_text("now plays on Sub")
    toast.get_by_role("button", name="Undo").click()
    wait_until(lambda: not api.desired(0)["inputs"][0]["routing"][1]["enabled"], what="the route to be off again")


def _route_sub(api: Api, on: bool) -> None:
    body = {"inputs": {"0": {"routing": {"1": {"enabled": on}}}}}
    api._ok(api.http.patch(f"{api.dev}/presets/0", params={"confirm": "true", "commit": "true"}, json=body))


def test_problems_changes_are_announced_once(desktop: Page, api: Api):
    visit(desktop, "/routing")
    live = desktop.locator("#shell-live")
    expect(live).to_have_text("")
    desktop.evaluate("() => htmx.trigger('#app-header', 'refresh')")  # same problems: nothing to say
    settle(desktop)
    expect(live).to_have_text("")
    _route_sub(api, True)
    desktop.evaluate("() => htmx.trigger('#app-header', 'refresh')")
    expect(live).not_to_have_text("", timeout=5000)
    assert "Sub is silent" not in live.inner_text()
    _route_sub(api, False)
    api.wait_synced()


def test_help_me_menu_lists_the_guided_tasks_and_the_glossary(desktop: Page):
    visit(desktop, "/settings")
    button = desktop.get_by_role("button", name="Help me…")
    button.click()
    menu = desktop.locator("#hdr-helpme-list")
    expect(menu).to_be_visible()
    labels = [t.strip() for t in menu.get_by_role("link").all_inner_texts()]
    assert labels == [
        "Something wrong?",
        "Make one louder",
        "Fix a boom",
        "Line up shakers",
        "Set levels",
        "Save or load a setup",
        "Set up this device",
        "Glossary",
    ]
    # One pop-up at a time: opening the problems list closes the menu.
    desktop.locator(".hdr-pill").click()
    expect(menu).to_be_hidden()
    desktop.locator("main").click(position={"x": 5, "y": 5})  # a click outside closes it
    expect(desktop.locator("#hdr-problems-list")).to_be_hidden()


def test_header_is_not_re_rendered_under_an_open_pop_up(desktop: Page):
    visit(desktop, "/settings")
    desktop.get_by_role("button", name="Help me…").click()
    desktop.evaluate("() => { document.querySelector('#hdr-helpme-list').dataset.mark = 'kept'; }")
    desktop.evaluate("() => htmx.trigger('#app-header', 'refresh')")
    desktop.wait_for_timeout(500)
    expect(desktop.locator("#hdr-helpme-list")).to_have_attribute("data-mark", "kept")
    desktop.keyboard.press("Escape")
    # Closing it lets the held refresh through.
    expect(desktop.locator("#hdr-helpme-list")).not_to_have_attribute("data-mark", "kept")


# --- CLIP light ------------------------------------------------------------------------------------------------------


def _levels(page: Page, inputs: list[float], outputs: list[float]) -> None:
    data = json.dumps({"i": inputs, "o": outputs})
    page.evaluate(
        """(data) => {
          const feed = document.querySelector('[data-clip-feed]');
          feed.dispatchEvent(new CustomEvent('sse:levels', { detail: { data } }));
        }""",
        data,
    )


def test_clip_light_latches_on_any_page_and_resets(screen: Page, shoot):
    visit(screen, "/settings")
    led = screen.locator(".hdr-clip-led")
    expect(led).not_to_have_class(re.compile(r"\bis-on\b"))
    _levels(screen, [-30.0, -40.0], [-0.5, -20.0, -60.0, -60.0])
    expect(led).to_have_class(re.compile(r"\bis-on\b"))
    expect(screen.locator("#shell-live")).to_have_text("Shakers clipped")
    # It stays on across pages until Reset.
    visit(screen, "/routing")
    led = screen.locator(".hdr-clip-led")
    expect(led).to_have_class(re.compile(r"\bis-on\b"))
    led.click()
    pop = screen.locator("#hdr-clip-pop")
    expect(pop).to_contain_text("Shakers reached −1 dBFS")
    expect(pop.get_by_role("link", name="Show the meters")).to_be_visible()
    shoot(screen, "p9-shell-clip")
    pop.get_by_role("button", name="Reset").click()
    expect(led).not_to_have_class(re.compile(r"\bis-on\b"))
    expect(led).to_be_focused()


# --- the amber band --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", EDIT_PAGES)
def test_amber_band_on_every_edit_page_when_editing_a_preset_that_isnt_playing(screen: Page, path: str):
    joiner = "&" if "?" in path else "?"
    visit(screen, f"{path}{joiner}preset=2")
    band = screen.locator("[data-editing-band]")
    expect(band).to_be_visible()
    expect(band).to_contain_text("You're editing preset 2. It isn't playing (preset 1 is).")
    expect(band.get_by_role("button", name="Switch to preset 2 now…")).to_be_visible()
    edit = band.get_by_role("link", name="Edit preset 1 instead")
    expect(edit).to_have_attribute("href", re.compile(r"preset=1"))
    # Not shown when editing the playing preset.
    visit(screen, path)
    expect(screen.locator("[data-editing-band]")).to_have_count(0)


def test_amber_band_edit_the_playing_preset_instead(screen: Page, shoot):
    visit(screen, "/routing?preset=2")
    shoot(screen, "p9-shell-amber-band")
    assert_accessible(screen, include="[data-editing-band]")
    screen.locator("[data-editing-band]").get_by_role("link", name="Edit preset 1 instead").click()
    settle(screen)
    assert screen.url.endswith("/routing?preset=1")
    expect(screen.locator("[data-editing-band]")).to_have_count(0)


# --- accessibility of the open sheets --------------------------------------------------------------------------------


def test_open_sheets_have_no_accessibility_violations(phone: Page):
    visit(phone, "/")
    phone.locator(".hdr-preset-toggle").tap()
    assert_accessible(phone, include="#app-header")
    phone.keyboard.press("Escape")
    phone.locator(".vol-open").tap()
    assert_accessible(phone, include="#app-header")
    phone.keyboard.press("Escape")
    phone.get_by_role("button", name="Menu").tap()
    assert_accessible(phone, include="#sidenav")
