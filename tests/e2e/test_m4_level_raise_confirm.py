"""The Level tab's "Raise route …" button behind the big-step confirmation (review fix for Phase 9 PR 14).

With a confirm step of 0.5 dB the button's +1 dB asks first. Dismissing that question must leave the route alone, show
no "Raised route …" toast or Undo, and give the button back; accepting it raises the route and offers Undo.
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Dialog, Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import settle, visit
from tests.e2e.test_m2_summary import ROUTING

SHAKERS = "/outputs/1"
RAISE = "Raise route In 1 → Shakers to +5 dB"


@pytest.fixture(scope="module")
def app_settings() -> dict[str, str]:
    return {"GAIN_STEP_CONFIRM_DB": "0.5"}


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        api._ok(api.http.patch(f"{api.dev}/presets/0", params={"confirm": "true", "commit": "true"}, json=ROUTING))
        api.wait_synced()

    return walkthrough


def answer(page: Page, accept: bool) -> list[str]:
    asked: list[str] = []

    def handle(dialog: Dialog) -> None:
        asked.append(dialog.message)
        if accept:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("dialog", handle)
    return asked


def route_db(api: Api) -> float:
    return api.desired(0)["inputs"][0]["routing"][0]["gain_db"]


def test_a_dismissed_confirmation_raises_nothing_and_claims_nothing(desktop: Page, api: Api):
    visit(desktop, SHAKERS)
    asked = answer(desktop, accept=False)
    button = desktop.locator("#level-notes").get_by_role("button", name=RAISE)
    button.click()
    wait_until(lambda: asked, what="the confirmation", page=desktop)
    assert "route level" in asked[0], asked
    expect(button).to_be_enabled()  # given back for another try
    settle(desktop, 300)
    expect(desktop.locator("#toasts")).not_to_contain_text("Raised route")
    expect(desktop.locator("#toasts").get_by_role("button", name="Undo")).to_have_count(0)
    assert route_db(api) == 4


def test_an_accepted_confirmation_raises_the_route_and_offers_undo(desktop: Page, api: Api):
    visit(desktop, SHAKERS)
    asked = answer(desktop, accept=True)
    desktop.locator("#level-notes").get_by_role("button", name=RAISE).click()
    wait_until(lambda: route_db(api) == 5, what="the route", page=desktop)
    assert asked, "the 1 dB raise should have asked first"
    toast = desktop.locator("#toasts .toast").filter(has_text="Raised route")
    expect(toast).to_be_visible()
    toast.get_by_role("button", name="Undo").click()  # a 1 dB drop: no question needed, but answered yes if asked
    wait_until(lambda: route_db(api) == 4, what="the undo", page=desktop)
