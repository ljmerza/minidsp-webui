"""Phase 9 PR 16 (Routing + Volume, design §10) in a real browser, on the embedded mock.

The state is the walkthroughs' (design 4.1): In 1 feeds Shakers (+4 dB route, +12 dB output), Out 3 and Out 4 (both
muted); the Sub's route is off, so the Sub is silent; In 2 feeds nothing.

Acceptance: routing at 390 px has no horizontal scroll; the Total row matches the Home cards. Also: the dB-weighted
Volume slider sends dB (not its track position), arrow keys step 0.5 dB, and the one confirm rule asks with the
"Drop the volume from −10 to −36.5 dB?" wording.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Dialog, Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_m2_summary import ROUTING


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        api._ok(api.http.patch(f"{api.dev}/presets/0", params={"confirm": "true", "commit": "true"}, json=ROUTING))
        api.wait_synced()

    return walkthrough


def _route(api: Api, i: int, o: int) -> dict:
    return api.desired(0)["inputs"][i]["routing"][o]


def _volume(api: Api) -> float:
    return float(api.http.get(f"{api.dev}/status").json()["master"]["volume"])


def _text(page: Page, selector: str) -> str:
    return re.sub(r"\s+", " ", page.locator(selector).inner_text()).strip()


# --- routing ---------------------------------------------------------------------------------------------------------


def test_phone_routing_is_one_card_per_output_without_sideways_scroll(phone: Page, shoot):
    visit(phone, "/routing")
    assert not has_horizontal_scroll(phone), "/routing scrolls sideways at 390 px"
    cards = phone.locator(".rg-out")
    expect(cards).to_have_count(4)
    for n in range(4):
        box = cards.nth(n).bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390
    shakers = cards.nth(0)
    expect(shakers.get_by_role("heading", name="Shakers")).to_be_visible()
    expect(shakers.get_by_role("checkbox", name="Route In 1 to Shakers")).to_be_visible()
    expect(shakers.get_by_role("button", name="Route level from In 1 to Shakers up 0.5 dB")).to_be_visible()
    # the muted outputs fold their switches; the Total stays
    out3 = cards.nth(2)
    expect(out3.get_by_role("checkbox", name="Route In 1 to Out 3")).to_be_hidden()
    expect(out3.locator(".rg-total")).to_contain_text("Muted")
    fold = out3.get_by_role("button", name="Show the routes to Out 3")
    expect(fold).to_have_attribute("aria-expanded", "false")
    fold.tap()
    expect(out3.get_by_role("checkbox", name="Route In 1 to Out 3")).to_be_visible()
    assert not has_horizontal_scroll(phone)
    shoot(phone, "p9-routing")


def test_total_row_matches_the_home_cards(desktop: Page, shoot):
    visit(desktop, "/")
    home = _text(desktop, "#home-out1")
    visit(desktop, "/routing")
    total = _text(desktop, "#route-total-0")
    assert total.startswith("+16.0 dB from In 1 route +4 + output +12")
    assert "+16.0 dB" in home and "route +4 + output +12" in home
    expect(desktop.locator("#route-badge-1")).to_contain_text("Silent")
    expect(desktop.locator("#route-total-1")).to_contain_text("nothing")
    expect(desktop.locator("#route-notes")).to_contain_text("In 2 isn't sent to any output.")
    expect(desktop.locator(".card", has=desktop.locator(".route-grid"))).to_contain_text("not as a sum")
    assert_accessible(desktop, include="main")
    shoot(desktop, "p9-routing")


def test_the_total_fix_and_the_steppers_update_the_total_in_place(desktop: Page, api: Api):
    visit(desktop, "/routing")
    desktop.locator("#route-total-1").get_by_role("button", name="Route In 1 → Sub").click()
    wait_until(lambda: _route(api, 0, 1)["enabled"], what="the route to the Sub")
    expect(desktop.locator("#route-total-1")).to_contain_text("0.0 dB from In 1")
    expect(desktop.locator("#in1-out2")).to_be_checked()
    expect(desktop.locator('[data-silent-output="1"]')).to_be_hidden()

    up = desktop.get_by_role("button", name="Route level from In 1 to Sub up 0.5 dB")
    up.click()
    up.click()
    wait_until(lambda: _route(api, 0, 1)["gain_db"] == 1.0, what="the route level to reach +1 dB")
    expect(desktop.locator("#route-total-1")).to_contain_text("+1.0 dB from In 1")
    expect(desktop.locator("#route-total-1")).to_contain_text("route +1 + output 0")

    # back to the walkthrough state for the other tests
    params = {"confirm": "true", "commit": "true"}
    body = {"inputs": {"0": {"routing": {"1": {"enabled": False, "gain_db": 0}}}}}
    api._ok(api.http.patch(f"{api.dev}/presets/0", params=params, json=body))
    api.wait_synced()


# --- volume ----------------------------------------------------------------------------------------------------------


def test_db_weighted_slider_sends_db_and_steps_by_half_a_db(desktop: Page, api: Api, shoot):
    api.master(volume=-30, confirm=True)
    visit(desktop, "/")
    slider = desktop.locator("#master-volume")
    expect(slider).to_have_attribute("aria-valuetext", "−30.0 dB")
    assert float(slider.input_value()) == pytest.approx(575)  # −30 dB sits mid-track, not at 76%
    box = slider.bounding_box()
    band = desktop.locator(".vol-band").bounding_box()
    assert box and band and band["x"] < box["x"] + box["width"] / 2 < band["x"] + band["width"]
    expect(desktop.locator(".vol-tick")).to_have_text(["−60", "−40", "−30", "−20", "−10", "0"])
    slider.focus()
    desktop.keyboard.press("ArrowUp")
    desktop.keyboard.press("ArrowUp")
    wait_until(lambda: _volume(api) == -29.0, what="the Volume to reach −29 dB")
    expect(slider).to_have_attribute("aria-valuetext", "−29.0 dB")
    expect(desktop.locator("#master-volume-num")).to_have_value("-29.0")
    shoot(desktop, "p9-volume")


def test_one_confirm_rule_with_the_clearer_drop_wording(desktop: Page, api: Api):
    api.master(volume=-10, confirm=True)
    visit(desktop, "/")
    dialogs: list[str] = []

    def answer(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    desktop.on("dialog", answer)
    field = desktop.locator("#master-volume-num")
    field.fill("-36.5")
    field.press("Enter")
    wait_until(lambda: _volume(api) == -36.5, what="the confirmed drop", page=desktop)
    assert dialogs and dialogs[0].startswith("Drop the volume from −10 to −36.5 dB? That's 26.5 dB quieter")
    settle(desktop)

    # P12's "typed −10": from −3.5 it is a 6.5 dB drop, under the 20 dB threshold, so nothing asks.
    api.master(volume=-3.5, confirm=True)
    visit(desktop, "/")
    dialogs.clear()
    field = desktop.locator("#master-volume-num")
    field.fill("-10")
    field.press("Enter")
    wait_until(lambda: _volume(api) == -10, what="the small drop", page=desktop)
    assert dialogs == []
    # Let the header refresh that follows the −10 write finish first; otherwise the tap below can land on a slider that
    # is about to be swapped out, its debounced request is dropped, and the test times out (seen ~1 in 6 runs).
    settle(desktop)

    # A tap on the track far below asks too, and the slider sends dB. The slider is looked up and the input dispatched
    # in one task: a locator resolved first could hand over a slider that a header swap detaches before the input lands.
    dialogs.clear()
    tap = """() => {
      const el = document.getElementById('master-volume');
      el.value = String(window.mdsp.volumeScale.toPos(-60));
      el.dispatchEvent(new Event('input'));
    }"""
    desktop.evaluate(tap)
    wait_until(lambda: _volume(api) == -60, what="the confirmed slider drop", page=desktop)
    assert dialogs and dialogs[0].startswith("Drop the volume from −10 to −60 dB?")
    api.master(volume=-30, confirm=True)
