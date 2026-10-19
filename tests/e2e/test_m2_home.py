"""Phase 9 PR 9 (Home and the overview pages, design §4) in a real browser.

The state is the walkthroughs' (design 4.1): In 1 feeds Shakers (+4 dB route, +12 dB output, 20-80 Hz LR4, a 4 dB cut
at 40 Hz, 8.75 ms), Out 3 and Out 4 (muted, and marked Not in use in Settings); the Sub's route is off (it keeps its
40 Hz cut); In 2 feeds nothing.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_m2_flow import mark_unused
from tests.e2e.test_m2_summary import CUT, ROUTING

BOTTOM_BAR_PX = 64  # the phone's sticky bottom bar (design §3.2)


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        params = {"confirm": "true", "commit": "true"}
        api._ok(api.http.patch(f"{api.dev}/presets/0", params=params, json=ROUTING))
        for group, (kind, freq) in enumerate((("hp", 20), ("lp", 80))):
            design = {"kind": kind, "family": "lr", "order": 4, "freq": freq}
            body = {"design": design, "bypass": False}
            api._ok(api.http.put(f"{api.dev}/presets/0/outputs/0/crossover/{group}", params=params, json=body))
        api.put_peq_band("outputs", 0, 0, CUT)
        api.put_peq_band("outputs", 1, 0, CUT)
        mark_unused(api, (2, 3))
        api.create_profile("Movies", api.desired(0))
        api.wait_synced()

    return walkthrough


def card(page: Page, number: int):
    return page.locator(f"#home-out{number}")


def route_level(api: Api, output: int = 0, input_: int = 0) -> float:
    return api.desired(0)["inputs"][input_]["routing"][output]["gain_db"]


def test_the_first_phone_screen_shows_the_health_line_the_flow_and_the_shakers_level(phone: Page, shoot):
    visit(phone, "/")
    height = phone.viewport_size["height"] - BOTTOM_BAR_PX

    def on_first_screen(locator) -> None:
        expect(locator).to_be_visible()
        box = locator.bounding_box()
        assert box is not None and box["y"] >= 0 and box["y"] + box["height"] <= height, (locator, box)

    health = phone.locator(".home-health-list").first
    on_first_screen(health.locator(".home-health-item").first)
    expect(health).to_contain_text("Sub is silent: no input is routed to it.")
    on_first_screen(phone.locator(".flow-list"))
    shakers = card(phone, 1)
    on_first_screen(shakers.locator("[data-total-text]"))
    on_first_screen(shakers.get_by_role("button", name="Shakers: raise the level by 0.5 dB"))
    assert not has_horizontal_scroll(phone)
    shoot(phone, "p9-chunk9-home-first-screen")
    # The −/+ are 56 px on a phone (design §16).
    size = shakers.get_by_role("button", name="Shakers: raise the level by 0.5 dB").bounding_box()
    assert size["width"] >= 56 and size["height"] >= 56


def test_the_home_screen_reads_top_to_bottom(desktop: Page, shoot):
    visit(desktop, "/")
    expect(desktop).to_have_title(re.compile(r"^Home · "))
    tops = [
        desktop.locator(selector).first.bounding_box()["y"]
        for selector in (".home-health-card", ".flow-card", ".home-outputs", ".home-strip", ".home-presets")
    ]
    assert tops == sorted(tops)
    shakers = card(desktop, 1)
    expect(shakers.locator("[data-health-badge]")).to_have_text("⚠ Check")
    expect(shakers.locator(".output-sentence")).to_have_text(
        "Plays 20–80 Hz (steep), fed by In 1. 1 EQ cut at 40 Hz. Waits 8.75 ms."
    )
    expect(shakers.locator("[data-total-text]")).to_have_text("+16.0 dB")
    expect(shakers.locator("[data-breakdown]")).to_have_text("route +4 + output +12")
    expect(shakers.locator('[data-note="routes"]')).to_contain_text(
        "Output is at its +12 dB limit: + now raises the route In 1 → Shakers."
    )
    expect(shakers.locator(".home-headroom")).to_contain_text(
        "Peaks above about −16 dBFS on In 1 will overload Shakers."
    )
    sub = card(desktop, 2)
    expect(sub.locator("[data-health-badge]")).to_have_text("⛔ Silent")
    expect(sub.get_by_role("button", name="Route In 1 → Sub")).to_be_visible()
    # Out 3 and Out 4 are marked Not in use: one folded line; Show opens their cards.
    fold = desktop.locator("[data-fold]")
    expect(fold).to_contain_text("Out 3 · Out 4")
    expect(card(desktop, 3)).to_be_hidden()
    shoot(desktop, "p9-chunk9-home")
    fold.get_by_role("button", name=re.compile(r"^Show")).click()
    expect(card(desktop, 3)).to_be_visible()
    expect(fold.get_by_role("button", name=re.compile(r"^Hide"))).to_have_attribute("aria-expanded", "true")
    assert_accessible(desktop)


def test_plus_at_the_output_limit_raises_the_route_and_the_card_follows(desktop: Page, api: Api):
    visit(desktop, "/")
    shakers = card(desktop, 1)
    shakers.get_by_role("button", name="Shakers: raise the level by 0.5 dB").click()
    expect(shakers.locator("[data-total-text]")).to_have_text("+16.5 dB")
    expect(shakers.locator("[data-breakdown]")).to_have_text("route +4.5 + output +12")
    wait_until(lambda: route_level(api) == 4.5, what="the route to rise")
    assert api.desired(0)["outputs"][0]["gain_db"] == 12.0  # the output stays at its limit
    settle(desktop)
    expect(card(desktop, 1).locator("[data-total-text]")).to_have_text("+16.5 dB")  # the server's re-render agrees

    # − lowers the output level only.
    card(desktop, 1).get_by_role("button", name="Shakers: lower the output level by 0.5 dB").click()
    wait_until(lambda: api.desired(0)["outputs"][0]["gain_db"] == 11.5, what="the output to drop")
    assert route_level(api) == 4.5
    expect(card(desktop, 1).locator("[data-total-text]")).to_have_text("+16.0 dB")
    expect(card(desktop, 1).locator('[data-note="routes"]')).to_be_hidden()

    # Put the walkthrough state back for the module's other tests.
    patch = {"inputs": {"0": {"routing": {"0": {"gain_db": 4}}}}, "outputs": {"0": {"gain_db": 12}}}
    api._ok(api.http.patch(f"{api.dev}/presets/0", params={"confirm": "true", "commit": "true"}, json=patch))
    api.wait_synced()


def test_at_both_limits_plus_is_disabled_and_says_why(desktop: Page, api: Api):
    patch = {"inputs": {"0": {"routing": {"0": {"gain_db": 12}}}}}
    params = {"confirm": "true", "commit": "true"}
    api._ok(api.http.patch(f"{api.dev}/presets/0", params=params, json=patch))
    api.wait_synced()
    try:
        visit(desktop, "/")
        shakers = card(desktop, 1)
        expect(shakers.get_by_role("button", name="Shakers: raise the level by 0.5 dB")).to_be_disabled()
        expect(shakers.locator('[data-note="max"]')).to_contain_text("Shakers is at its maximum (+24.0 dB total).")
        expect(shakers.get_by_role("link", name="Lower the other outputs…")).to_be_visible()
    finally:
        api._ok(
            api.http.patch(
                f"{api.dev}/presets/0", params=params, json={"inputs": {"0": {"routing": {"0": {"gain_db": 4}}}}}
            )
        )
        api.wait_synced()


def test_profile_buttons_open_the_load_step_and_never_load(desktop: Page, api: Api):
    visit(desktop, "/")
    presets = desktop.locator(".home-presets")
    expect(presets).to_contain_text("Device preset 1 is playing. Same as “Movies”.")
    before = api.desired(1)
    presets.get_by_role("link", name=re.compile(r"^Movies…")).click()
    # The load wizard opens (design §14.8) with the profile preselected; nothing is loaded.
    dialog = desktop.locator("#task-dialog")
    expect(dialog.locator(".task-heading")).to_contain_text("Which profile?")
    expect(dialog.get_by_role("radio", name=re.compile("Movies"))).to_be_checked()
    assert api.desired(1) == before  # nothing was loaded


def test_the_overview_pages_show_every_input_and_output(screen: Page, shoot):
    visit(screen, "/outputs")
    expect(screen.get_by_role("heading", level=1)).to_have_text("Outputs")
    expect(screen.locator(".home-card")).to_have_count(4)
    expect(screen.get_by_role("link", name=re.compile(r"^All settings for Out 3"))).to_be_visible()
    assert not has_horizontal_scroll(screen)
    shoot(screen, "p9-chunk9-outputs")
    visit(screen, "/inputs")
    expect(screen.get_by_role("heading", level=1)).to_have_text("Inputs")
    expect(screen.locator(".input-card").first).to_contain_text(
        "Level 0 dB, no EQ. Sent to Shakers (+4 dB), Out 3 and Out 4."
    )
    expect(screen.locator(".input-card").nth(1)).to_contain_text("Not sent to any output.")
    assert not has_horizontal_scroll(screen)
    shoot(screen, "p9-chunk9-inputs")


def test_the_start_page_setting_sends_this_browser_to_meters_and_graph(desktop: Page, shoot):
    visit(desktop, "/settings")
    card_ = desktop.locator(".start-page-card")
    card_.scroll_into_view_if_needed()
    card_.get_by_label("Meters & graph").check()
    expect(card_.locator(".form-saved")).to_have_text("Saved for this browser.")
    shoot(desktop, "p9-chunk9-settings-start-page")
    try:
        # Opened from outside the app (a bookmark): it lands on Meters & graph.
        desktop.goto("/")
        expect(desktop).to_have_url(re.compile(r"/meters$"))
        settle(desktop)
        # Home stays one click away: the menu's Home link opens it.
        desktop.locator("#sidenav").get_by_role("link", name="Home", exact=True).click()
        expect(desktop).to_have_url(re.compile(r"/$"))
        expect(desktop.locator("[data-home]")).to_be_visible()
    finally:
        visit(desktop, "/settings")
        desktop.locator(".start-page-card").get_by_label("Home").check()
    desktop.goto("/")
    expect(desktop).to_have_url(re.compile(r"/$"))
