"""Phase 9 PR 14 (output page: chain tabs and plain editors; design §8, §9, P13, P14, P19) in a real browser.

The state is the walkthroughs' (design 4.1): In 1 feeds Shakers (+4 dB route, +12 dB output at its limit, 20-80 Hz
LR4, a 4 dB cut at 40 Hz, 8.75 ms), Out 3 and Out 4 (muted); the Sub's route is off. The Sub's first band filter is a
Butterworth 18 dB/oct low-pass at 80 Hz, a design none of the three Steepness choices names.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_m2_summary import CUT, ROUTING

SHAKERS = "/outputs/1"
PARAMS = {"confirm": "true", "commit": "true"}
BW18 = {"kind": "lp", "family": "bw", "order": 3, "freq": 80}


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        api._ok(api.http.patch(f"{api.dev}/presets/0", params=PARAMS, json=ROUTING))
        for group, (kind, freq) in enumerate((("hp", 20), ("lp", 80))):
            design = {"kind": kind, "family": "lr", "order": 4, "freq": freq}
            body = {"design": design, "bypass": False}
            api._ok(api.http.put(f"{api.dev}/presets/0/outputs/0/crossover/{group}", params=PARAMS, json=body))
        api._ok(
            api.http.put(
                f"{api.dev}/presets/0/outputs/1/crossover/0", params=PARAMS, json={"design": BW18, "bypass": False}
            )
        )
        api.put_peq_band("outputs", 0, 0, CUT)
        api.wait_synced()

    return walkthrough


def chip(page: Page, key: str):
    return page.locator(f'a[data-chain-tab="{key}"]')


def panel_heading(page: Page):
    return page.locator("#tab-panel h2").first


def open_chip(page: Page, key: str, heading: str) -> None:
    chip(page, key).click()
    expect(chip(page, key)).to_have_attribute("aria-current", "page")
    expect(panel_heading(page)).to_contain_text(heading)
    settle(page, 150)


def test_the_chain_shows_each_settings_value(desktop: Page, shoot):
    visit(desktop, SHAKERS)
    labels = desktop.locator(".chain-list a[data-chain-tab]")
    expect(labels).to_have_text(
        [
            re.compile(r"Level \+16"),
            "Band 20–80 Hz",
            "EQ 1 cut",
            "Delay 8.75 ms",
            "Polarity 0°",
            "Compressor off",
            "Response",
        ]
    )
    expect(chip(desktop, "level")).to_have_attribute("aria-current", "page")
    head = desktop.locator(".chain-head")
    expect(head.locator(".chain-sentence")).to_have_text(
        "Plays 20–80 Hz (steep), fed by In 1. 1 EQ cut at 40 Hz. Waits 8.75 ms."
    )
    expect(head.locator(".chain-fed")).to_contain_text("Fed by In 1 at +4 dB")
    expect(head.locator(".chain-headroom")).to_contain_text("Peaks above about −16 dBFS on In 1 will overload Shakers")
    shoot(desktop, "p9-chunk14-output-level")


def test_switching_tabs_swaps_in_place_keeps_scroll_and_back_returns(desktop: Page):
    visit(desktop, f"{SHAKERS}?tab=peq")
    desktop.evaluate("() => { window.__noReload = true; }")
    top = desktop.evaluate(
        "() => document.querySelector('.chain-list').getBoundingClientRect().top + window.scrollY - 300"
    )
    desktop.evaluate(f"() => window.scrollTo(0, {top})")
    y = desktop.evaluate("() => window.scrollY")
    assert y > 100, "the page must scroll for this test to mean anything"

    open_chip(desktop, "crossover", "Band (crossover)")
    assert re.search(r"/outputs/1\?.*tab=crossover", desktop.url)
    assert abs(desktop.evaluate("() => window.scrollY") - y) <= 2
    expect(chip(desktop, "crossover")).to_be_focused()

    open_chip(desktop, "delay", "Delay")
    assert re.search(r"tab=delay", desktop.url)
    assert abs(desktop.evaluate("() => window.scrollY") - y) <= 2

    desktop.go_back()
    expect(chip(desktop, "crossover")).to_have_attribute("aria-current", "page")
    expect(panel_heading(desktop)).to_contain_text("Band (crossover)")
    assert re.search(r"tab=crossover", desktop.url)
    desktop.go_back()
    expect(chip(desktop, "peq")).to_have_attribute("aria-current", "page")
    expect(panel_heading(desktop)).to_contain_text("EQ")
    desktop.go_forward()
    expect(chip(desktop, "crossover")).to_have_attribute("aria-current", "page")
    assert desktop.evaluate("() => window.__noReload === true"), "a tab switch reloaded the page"


def test_no_duplicate_scripts_or_ids_after_five_swaps(desktop: Page):
    visit(desktop, SHAKERS)
    before = desktop.evaluate("() => document.querySelectorAll('script[src]').length")
    for key, heading in (
        ("crossover", "Band (crossover)"),
        ("peq", "EQ"),
        ("compressor", "Compressor (limiter)"),
        ("response", "Calculated response"),
        ("delay", "Delay"),
        ("level", "Level"),
    ):
        open_chip(desktop, key, heading)
    scripts = desktop.evaluate("() => [...document.querySelectorAll('script[src]')].map((s) => s.src)")
    assert len(scripts) == before and len(scripts) == len(set(scripts)), scripts
    ids = desktop.evaluate("() => [...document.querySelectorAll('[id]')].map((e) => e.id)")
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, duplicates
    # The swapped-in editors work: the Level tab's slider is live, and one levelMeters, one chain, one panel.
    assert desktop.locator("#tab-panel").count() == 1 and desktop.locator(".chain-list").count() == 1
    expect(desktop.locator('#tab-panel input[type="range"][data-path="outputs.0.gain_db"]')).to_be_visible()


def test_the_delay_is_editable_only_on_the_delay_tab(desktop: Page, api: Api):
    for key in ("level", "crossover", "peq", "polarity", "compressor", "response"):
        visit(desktop, f"{SHAKERS}?tab={key}")
        expect(desktop.locator('[data-path="outputs.0.delay_ms"]')).to_have_count(0)
    expect(chip(desktop, "delay")).to_have_text("Delay 8.75 ms")  # shown read-only, as the tab's label

    visit(desktop, f"{SHAKERS}?tab=delay")
    expect(desktop.locator('[data-path="outputs.0.delay_ms"]')).to_have_count(1)
    helper = desktop.locator(".line-up")
    helper.get_by_label("How much closer or farther, in metres or feet").fill("2")
    helper.get_by_label("Other output").select_option(label="Sub")
    expect(helper.locator(".line-up-result")).to_have_text("→ Shakers will wait 5.83 ms, so both arrive together.")
    helper.get_by_role("button", name="Apply").click()
    wait_until(lambda: abs(api.desired(0)["outputs"][0]["delay_ms"] - 5.8333) < 0.01, what="the delay", page=desktop)
    expect(chip(desktop, "delay")).to_have_text("Delay 5.83 ms")
    # 3 m reads 8.75 ms again (the design's example), and Apply puts it back
    helper.get_by_label("How much closer or farther, in metres or feet").fill("3")
    expect(helper.locator(".line-up-result")).to_have_text("→ Shakers will wait 8.75 ms, so both arrive together.")
    helper.get_by_role("button", name="Apply").click()
    wait_until(lambda: abs(api.desired(0)["outputs"][0]["delay_ms"] - 8.75) < 0.01, what="the delay", page=desktop)
    # the folded absolute helper's feet field is valid (P13)
    desktop.locator(".helper-more summary").click()
    feet = desktop.get_by_label("Distance in feet")
    assert feet.evaluate("(el) => el.validity.valid")


def test_steepness_lr4_writes_the_same_design_as_lr_24(desktop: Page, api: Api):
    visit(desktop, "/outputs/2?tab=crossover")
    group = desktop.locator(".xover-group").first
    expect(group.locator("h3")).to_have_text("Filter 1 · Low-pass")
    steep = group.locator("select[data-steepness]")
    expect(steep.locator("option:checked")).to_have_text("Custom: Butterworth 18 dB/oct")
    expect(group.locator("details.xover-advanced")).to_have_js_property("open", True)
    expect(desktop.locator(".xover-sentence")).to_have_text(
        "This output plays everything below 80 Hz. Above it, sound fades by 18 dB per octave."
    )
    steep.select_option(label="Steep, 24 dB/oct (LR4)")

    def stored() -> dict | None:
        design = api.desired(0)["outputs"][1]["crossover"][0]["design"]
        return design if design and design["family"] == "lr" else None

    design = wait_until(stored, what="the LR4 design", page=desktop)
    assert (design["kind"], design["family"], design["order"], design["freq"]) == ("lp", "lr", 4, 80)
    # the same as choosing Linkwitz-Riley and 24 dB/oct under Advanced (today's two selects) on the other filter
    other = desktop.locator(".xover-group").nth(1)
    other.locator("select[data-steepness]").select_option(label="Other…")
    expect(other.locator("details.xover-advanced")).to_have_js_property("open", True)
    expect(desktop.locator(".xover-sentence")).to_contain_text("fades by 24 dB per octave")


def test_the_band_tab_has_one_legend(screen: Page):
    """The graph draws its own legend (following the traces); the template must not add a second, static one."""
    visit(screen, f"{SHAKERS}?tab=crossover")
    legends = screen.locator("#tab-panel .graph-legend")
    expect(legends.locator("li").first).to_be_visible()
    expect(legends).to_have_count(1)


def test_the_eq_tab_explains_the_width_in_hz(screen: Page):
    """Design §8.2: the width hint under the table, for the 4 dB cut at 40 Hz (Q 4)."""
    visit(screen, f"{SHAKERS}?tab=peq")
    expect(screen.locator("#tab-panel .peq-width-hint")).to_have_text("Width (Q 4) at 40 Hz: affects about 35–45 Hz")


def test_at_the_limit_the_level_tab_raises_the_route(desktop: Page, api: Api):
    visit(desktop, SHAKERS)
    notes = desktop.locator("#level-notes")
    expect(notes).to_contain_text("At this output's limit. To make Shakers louder:")
    expect(notes.locator(".level-total").first).to_have_text(
        re.compile(r"Total from In 1: In 1 level 0 \+ route \+4 \+ output \+12 = \+16\.0 dB")
    )
    expect(desktop.locator('[data-chain-step][data-step="0.5"]')).to_be_disabled()  # the output is at its limit
    notes.get_by_role("button", name="Raise route In 1 → Shakers to +5 dB").click()
    wait_until(lambda: api.desired(0)["inputs"][0]["routing"][0]["gain_db"] == 5, what="the route", page=desktop)
    expect(desktop.locator("#level-notes .level-total").first).to_contain_text("= +17.0 dB")
    expect(chip(desktop, "level")).to_contain_text("Level +17")
    desktop.locator("#toasts").get_by_role("button", name="Undo").click()
    wait_until(lambda: api.desired(0)["inputs"][0]["routing"][0]["gain_db"] == 4, what="the undo", page=desktop)


def test_the_input_page_has_the_same_chain(desktop: Page):
    visit(desktop, "/inputs/1")
    expect(desktop.locator(".chain-sentence")).to_have_text(
        "In 1: Level 0 dB, no EQ. Sent to Shakers (+4 dB), Out 3 and Out 4."
    )
    expect(desktop.locator(".chain-list a[data-chain-tab]")).to_have_text(["Level 0", "EQ none"])
    expect(chip(desktop, "peq")).to_have_attribute("aria-current", "page")
    open_chip(desktop, "level", "Level")
    expect(desktop.locator('#tab-panel [data-path="inputs.0.mute"]')).to_have_count(1)


@pytest.mark.parametrize("tab", ["level", "crossover", "delay", "compressor"])
def test_the_output_page_is_accessible(desktop: Page, tab: str):
    visit(desktop, f"{SHAKERS}?tab={tab}")
    assert_accessible(desktop, include="#chain-box")


def test_on_a_phone_a_chip_swaps_the_panel_to_just_under_the_header(phone: Page, shoot):
    visit(phone, SHAKERS)
    assert not has_horizontal_scroll(phone)
    shoot(phone, "p9-chunk14-output-level")
    chips = phone.locator(".chain-list")
    assert chips.evaluate("(el) => getComputedStyle(el).overflowX") == "auto"
    chip(phone, "peq").scroll_into_view_if_needed()
    chip(phone, "peq").tap()
    expect(chip(phone, "peq")).to_have_attribute("aria-current", "page")
    settle(phone, 200)
    top = phone.evaluate("() => document.getElementById('tab-panel').getBoundingClientRect().top")
    header = phone.evaluate("() => document.querySelector('.app-header').getBoundingClientRect().bottom")
    assert header - 1 <= top <= header + 24, (top, header)
    assert not has_horizontal_scroll(phone)
    # the EQ table is one card per band, and the unused bands fold away
    expect(phone.locator(".peq-table > tbody:visible")).to_have_count(1)
    phone.get_by_role("button", name="Show all ▸").tap()
    expect(phone.locator(".peq-table > tbody:visible")).to_have_count(10)
    assert phone.evaluate("() => document.querySelector('.peq-editor .table-scroll').scrollWidth") <= 390
    shoot(phone, "p9-chunk14-output-eq")
    open_chip(phone, "crossover", "Band (crossover)")
    shoot(phone, "p9-chunk14-output-band")
