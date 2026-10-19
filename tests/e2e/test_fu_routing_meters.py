"""Routing's per-input meters (phase 9 design §10.1 drawing, §5.3) on the embedded mock with the ``signal`` level
pattern: In 1 carries a programme around −20 dBFS and In 2 reads nothing, so the row heads (desktop) and each phone
card's input header show which inputs actually carry signal.

The meters ride the page's existing levels stream (the routing guard already subscribes to it); nothing here writes.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, visit
from tests.e2e.test_m2_summary import ROUTING

LIVE = re.compile(r"^In 1: −(1[6-9]|2[0-4])\.\d dBFS$")  # −20 ± 4 dB
READING = re.compile(r"^−(1[6-9]|2[0-4])\.\d dBFS$")


@pytest.fixture(scope="module")
def app_settings() -> dict[str, str]:
    return {"MINIDSPD_MOCK_LEVELS": "signal"}


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        api._ok(api.http.patch(f"{api.dev}/presets/0", params={"confirm": "true", "commit": "true"}, json=ROUTING))
        api.wait_synced()

    return walkthrough


def _send_levels(page: Page, inputs: list[float]) -> None:
    """One synthetic level frame, delivered the way the htmx sse extension does (a DOM event on the listener)."""
    page.evaluate(
        """(i) => document.querySelector('[data-params]').dispatchEvent(
             new CustomEvent('sse:levels', { detail: { data: JSON.stringify({ i, o: [] }) } }))""",
        inputs,
    )


def test_row_heads_show_each_inputs_live_level(desktop: Page, shoot):
    visit(desktop, "/routing")
    heads = desktop.locator(".rg-in")
    in1 = heads.nth(0).get_by_role("meter", name="In 1 level")
    in2 = heads.nth(1).get_by_role("meter", name="In 2 level")
    # the accessible text names the input and its reading; In 2 is below the no-signal floor
    expect(in1).to_have_attribute("aria-valuetext", LIVE, timeout=15_000)
    expect(in2).to_have_attribute("aria-valuetext", "In 2: no signal")
    expect(in1).not_to_have_attribute("aria-live", re.compile(".+"))
    expect(heads.nth(0).locator(".rg-meter-value")).to_have_text(READING)
    expect(heads.nth(1).locator(".rg-meter-value")).to_have_text("no signal")
    now = float(in1.get_attribute("aria-valuenow") or "-120")
    assert -25 < now < -15
    # the bar is filled and the peak-hold tick is drawn
    bar = heads.nth(0).locator(".rg-meter-bar").evaluate("el => getComputedStyle(el).clipPath")
    assert bar.startswith("inset(") and not bar.startswith("inset(0px 100%")
    expect(heads.nth(0).locator(".rg-meter-peak")).to_be_visible()
    expect(heads.nth(0).get_by_role("button", name="In 1 not clipping")).to_be_visible()
    # the phone copies stay out of the way on a wide screen
    expect(desktop.locator(".rg-cell-meter").first).to_be_hidden()
    assert_accessible(desktop, include="main")
    shoot(desktop, "fu-routing-meters")


def test_clip_light_latches_and_resets(desktop: Page):
    visit(desktop, "/routing")
    head = desktop.locator(".rg-in").nth(0)
    expect(head.get_by_role("meter", name="In 1 level")).to_have_attribute("aria-valuetext", LIVE, timeout=15_000)
    _send_levels(desktop, [-0.5, -120])
    clip = head.get_by_role("button", name="In 1 clipped, press to reset")
    expect(clip).to_have_class(re.compile(r"\bis-on\b"))
    clip.click()
    expect(head.get_by_role("button", name="In 1 not clipping")).not_to_have_class(re.compile(r"\bis-on\b"))


def test_phone_cards_show_the_meter_in_each_input_header(phone: Page, shoot):
    visit(phone, "/routing")
    shakers = phone.locator(".rg-out").nth(0)
    cells = shakers.locator("[data-route-cell]")
    in1 = cells.nth(0).get_by_role("meter", name="In 1 level")
    expect(in1).to_be_visible()
    expect(in1).to_have_attribute("aria-valuetext", LIVE, timeout=15_000)
    expect(cells.nth(1).locator(".rg-meter-value")).to_have_text("no signal")
    # the meter sits on the input's header line, beside its name, above the route level
    name_box = cells.nth(0).locator(".rg-cell-in").bounding_box()
    meter_box = in1.bounding_box()
    level_box = cells.nth(0).locator(".route-level").bounding_box()
    assert name_box and meter_box and level_box
    assert abs((meter_box["y"] + meter_box["height"] / 2) - (name_box["y"] + name_box["height"] / 2)) < 12
    assert level_box["y"] >= meter_box["y"] + meter_box["height"] - 1
    # the desktop row heads are hidden; a card's CLIP light only shows once it has latched
    expect(phone.locator(".rg-in").first).to_be_hidden()
    expect(cells.nth(0).locator(".rg-clip")).to_be_hidden()
    assert not has_horizontal_scroll(phone), "/routing scrolls sideways at 390 px"
    shoot(phone, "fu-routing-meters")
    _send_levels(phone, [-0.5, -120])
    clip = cells.nth(0).get_by_role("button", name="In 1 clipped, press to reset")
    expect(clip).to_be_visible()
    clip.tap()
    expect(cells.nth(0).locator(".rg-clip")).to_be_hidden()
    assert not has_horizontal_scroll(phone)
