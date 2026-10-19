"""Phase 9 PR 5 (design §13.1) in a real browser: the "?" toggletips open on click, tap and Enter, never on hover;
Esc closes one and puts focus back on its button; one is open at a time; /help works without JavaScript."""

from collections.abc import Callable

from playwright.sync_api import BrowserContext, Page, expect

from tests.e2e.axe import assert_accessible
from tests.e2e.conftest import DEFAULT_TIMEOUT_MS, SCREENS
from tests.e2e.pages import visit
from tests.e2e.server import AppServer


def toggletip(page: Page, term: str):
    """The "?" button for a term, by its accessible name ("What is Routing?"), and its panel."""
    button = page.get_by_role("button", name=f"What is {term}?").first
    panel = page.locator(f"#{button.get_attribute('aria-controls')}")
    return button, panel


def test_a_toggletip_opens_on_click_or_tap_and_closes_on_a_second_one(screen: Page, shoot):
    visit(screen, "/routing")
    button, panel = toggletip(screen, "Routing")
    expect(button).to_have_attribute("aria-expanded", "false")
    expect(panel).to_be_hidden()
    if screen.screen_name == "phone":  # type: ignore[attr-defined]
        button.tap()
    else:
        button.click()
    expect(button).to_have_attribute("aria-expanded", "true")
    expect(panel).to_be_visible()
    expect(panel).to_have_attribute("role", "dialog")
    expect(panel).to_contain_text("Chooses which inputs feed each output")
    expect(panel.get_by_role("link", name="More")).to_have_attribute("href", "/help#routing")
    assert panel.evaluate("(el) => el.getAttribute('aria-labelledby')") == f"{panel.get_attribute('id')}-t"
    # it stays inside the screen, next to its button
    box, view = panel.bounding_box(), screen.viewport_size
    assert box and view and box["x"] >= 0 and box["x"] + box["width"] <= view["width"]
    shoot(screen, "p9-help-toggletip-open")
    button.click()
    expect(panel).to_be_hidden()
    expect(button).to_have_attribute("aria-expanded", "false")


def test_hover_never_opens_a_toggletip(desktop: Page):
    visit(desktop, "/routing")
    button, panel = toggletip(desktop, "Routing")
    button.hover()
    desktop.wait_for_timeout(400)
    expect(panel).to_be_hidden()
    expect(button).to_have_attribute("aria-expanded", "false")


def test_keyboard_enter_opens_and_escape_closes_with_focus_back_on_the_button(desktop: Page):
    visit(desktop, "/routing")
    button, panel = toggletip(desktop, "Mono sum")
    button.focus()
    desktop.keyboard.press("Enter")
    expect(panel).to_be_visible()
    expect(button).to_be_focused()
    desktop.keyboard.press("Tab")  # into the panel: its "More" link follows the button
    expect(panel.get_by_role("link", name="More")).to_be_focused()
    desktop.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    expect(button).to_be_focused()
    desktop.keyboard.press("Space")
    expect(panel).to_be_visible()
    desktop.keyboard.press("Escape")
    expect(panel).to_be_hidden()


def test_one_toggletip_at_a_time_and_a_click_outside_closes_it(desktop: Page):
    visit(desktop, "/routing")
    first, first_panel = toggletip(desktop, "Routing")
    second, second_panel = toggletip(desktop, "Volume")
    first.click()
    expect(first_panel).to_be_visible()
    second.click()
    expect(second_panel).to_be_visible()
    expect(first_panel).to_be_hidden()
    expect(first).to_have_attribute("aria-expanded", "false")
    desktop.locator("h1").click()
    expect(second_panel).to_be_hidden()


def test_toggletips_inside_alpine_lists_and_the_swapped_header_work(desktop: Page):
    visit(desktop, "/outputs/1?tab=crossover")
    buttons = desktop.get_by_role("button", name="What is Steepness?")
    expect(buttons).to_have_count(2)  # one per band filter, each with its own panel
    ids = [b.get_attribute("aria-controls") for b in buttons.all()]
    assert len(set(ids)) == 2 and all(ids)
    buttons.nth(1).click()
    expect(desktop.locator(f"#{ids[1]}")).to_be_visible()
    expect(desktop.locator(f"#{ids[0]}")).to_be_hidden()
    desktop.keyboard.press("Escape")
    # the header is re-rendered by htmx; its toggletips keep working afterwards
    desktop.evaluate("() => htmx.trigger('#app-header', 'refresh')")
    desktop.wait_for_timeout(500)
    button, panel = toggletip(desktop, "Device preset")
    button.click()
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("One of 4 complete setups stored inside the miniDSP")


def test_more_link_lands_on_the_glossary_entry(desktop: Page):
    visit(desktop, "/outputs/1?tab=peq")
    button, panel = toggletip(desktop, "Width (Q)")
    button.click()
    panel.get_by_role("link", name="More").click()
    desktop.wait_for_url("**/help#q")
    entry = desktop.locator("#q")
    expect(entry).to_be_in_viewport()
    expect(entry).to_contain_text("Wide = Q 1.4")


def test_glossary_page_works_without_javascript(new_context: Callable[..., BrowserContext], app: AppServer, shoot):
    for screen in SCREENS:
        context = new_context(base_url=app.url, java_script_enabled=False, **SCREENS[screen])
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        page = context.new_page()
        page.screen_name = screen  # type: ignore[attr-defined]
        page.goto("/help#st-cant-check")
        expect(page.get_by_role("heading", name="Glossary", level=1)).to_be_visible()
        expect(page.get_by_role("heading", name="Sync and the device")).to_be_visible()
        expect(page.locator("#st-cant-check")).to_be_in_viewport()
        expect(page.locator("#st-cant-check")).to_contain_text("The device can't report this setting back")
        page.get_by_role("link", name="Health badges").click()
        expect(page.locator("#b-overload")).to_contain_text("This output clipped")
        shoot(page, "p9-help-glossary-nojs")
        context.close()


def test_pages_with_toggletips_open_have_no_accessibility_violations(screen: Page, shoot):
    visit(screen, "/help")
    shoot(screen, "p9-help-glossary")
    assert_accessible(screen)
    visit(screen, "/device")
    button, panel = toggletip(screen, "Check device")
    button.click()
    expect(panel).to_be_visible()
    assert_accessible(screen)
    shoot(screen, "p9-help-device-toggletip")


def test_terms_have_toggletips_on_the_editing_pages(screen: Page, shoot):
    """A sweep over the pages this PR touched: each shows its "?"s (screenshots for the PR when enabled)."""
    for path, name, terms in [
        ("/outputs/1", "output-delay", ["Output level", "Polarity", "Samples", "Delay"]),
        ("/outputs/1?tab=crossover", "output-band", ["Band (crossover)", "Templates", "Steepness"]),
        ("/outputs/1?tab=peq", "output-eq", ["EQ", "Filter type", "Width (Q)", "Cut / Boost"]),
        ("/outputs/1?tab=compressor", "output-compressor", ["Compressor (limiter)", "dBFS"]),
        ("/meters", "dashboard", ["Meter / peak hold", "CLIP light", "Calculated response"]),
        ("/profiles", "profiles", ["Profile", "Store / Store and play now"]),
        ("/system", "combined-response", ["Calculated response", "Magnitude / phase"]),
        ("/settings", "settings", ["Level limit"]),
    ]:
        visit(screen, path)
        for term in terms:
            expect(screen.get_by_role("button", name=f"What is {term}?").first).to_be_attached()
        shoot(screen, f"p9-help-{name}")
