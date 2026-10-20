"""A refresh in the middle of a press never loses the click (static/js/app.js), on the embedded mock.

A click needs the same element under the press and the release. Mute all, the header's preset buttons, the Home cards'
Mute and Home's "Switch to preset N" sit in parts that re-render on every event, so a swap between pointerdown and
pointerup replaced the pressed button and the click went nowhere. Each test presses the button, forces its part's
refresh while the button is held down, releases, and counts the writes (and the confirm, for a preset switch). The
last ones check what the hold must not do: let a second refresh during the press stand in for the catch-up after it, or
hold back the reply to a write (it carries what no refresh does).
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Dialog, Locator, Page, Request, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import settle, visit


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough_state(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        api.patch_output(1, {"gain_db": -1}, preset=1)  # a change waiting for preset 2: Home offers to switch to it

    return walkthrough_state


# Refresh the part `target` and report whether it was swapped within 400 ms (resolves false if the swap was held).
_REFRESHED = """(target) => new Promise((resolve) => {
  const part = document.querySelector(target);
  const swapped = (e) => {
    if (e.detail.target !== part) return;
    document.removeEventListener('htmx:afterSwap', swapped);
    resolve(true);
  };
  document.addEventListener('htmx:afterSwap', swapped);
  setTimeout(() => { document.removeEventListener('htmx:afterSwap', swapped); resolve(false); }, 400);
  htmx.trigger(part, 'refresh');
})"""


def _writes(page: Page, *parts: str) -> list[str]:
    sent: list[str] = []

    def record(request: Request) -> None:
        if request.method == "POST" and any(p in request.url for p in parts):
            sent.append(request.url.split("?", 1)[0])

    page.on("request", record)
    return sent


def _answer(page: Page, accept: bool = True) -> list[str]:
    dialogs: list[str] = []

    def answer(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        if accept:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("dialog", answer)
    return dialogs


def _press(page: Page, button: Locator) -> None:
    button.scroll_into_view_if_needed()
    box = button.bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()


def _press_through_a_refresh(page: Page, button: Locator, target: str) -> bool:
    """Whether `target` was swapped while the button was down (it shouldn't be: the click would be lost)."""
    _press(page, button)
    swapped = page.evaluate(_REFRESHED, target)
    page.mouse.up()
    return swapped


def _status(api: Api) -> dict:
    return api.http.get(f"{api.dev}/status").json()["master"]


def _one(page: Page, sent: list[str]) -> None:
    wait_until(lambda: sent, what="the write", page=page)
    settle(page, 500)
    assert len(sent) == 1, f"one write expected, got {sent}"


def test_mute_all_survives_a_header_refresh_during_the_press(desktop: Page, api: Api):
    api.master(mute=False)
    visit(desktop, "/")
    sent = _writes(desktop, "/master/mute")
    swapped = _press_through_a_refresh(desktop, desktop.locator("#master-mute"), "#app-header")
    _one(desktop, sent)
    assert not swapped, "the part was replaced under the pressed button"
    wait_until(lambda: _status(api)["mute"] is True, what="Mute all", page=desktop)
    expect(desktop.locator("#master-mute")).to_have_attribute("aria-pressed", "true")
    api.master(mute=False)


def test_a_header_preset_button_survives_a_header_refresh_during_the_press(desktop: Page, api: Api):
    visit(desktop, "/")
    sent = _writes(desktop, "/master/preset")
    dialogs = _answer(desktop)
    try:
        swapped = _press_through_a_refresh(desktop, desktop.locator("#preset-2"), "#app-header")
        _one(desktop, sent)
        assert not swapped, "the part was replaced under the pressed button"
        assert len(dialogs) == 1 and dialogs[0].startswith("Switch to preset 2?"), dialogs
        wait_until(lambda: _status(api)["preset"] == 1, what="preset 2", page=desktop)
    finally:
        api.master(preset=0)
        wait_until(lambda: _status(api)["preset"] == 0, what="preset 1 again")


def test_a_cancelled_preset_switch_still_gets_the_refresh_held_during_the_press(desktop: Page, api: Api):
    api.master(mute=False)
    visit(desktop, "/")
    sent = _writes(desktop, "/master/preset")
    dialogs = _answer(desktop, accept=False)
    _press(desktop, desktop.locator("#preset-2"))
    # Changed elsewhere while the button is held: the header's refresh for it is held back, not swapped.
    with desktop.expect_response(lambda r: "/ui/header" in r.url):
        api.master(mute=True)
    desktop.wait_for_timeout(200)
    expect(desktop.locator("#master-mute")).to_have_attribute("aria-pressed", "false")
    desktop.mouse.up()
    wait_until(lambda: dialogs, what="the question", page=desktop)
    # Nothing was sent, so no reply re-renders the header: the held refresh is fetched again once the press is over.
    expect(desktop.locator("#master-mute")).to_have_attribute("aria-pressed", "true")
    settle(desktop)
    assert sent == [] and len(dialogs) == 1, (sent, dialogs)
    api.master(mute=False)


def test_a_home_card_mute_survives_a_cards_refresh_during_the_press(desktop: Page, api: Api):
    visit(desktop, "/")
    before = api.desired(0)["outputs"][0]["mute"]
    sent = _writes(desktop, "/edit")
    mute = desktop.locator("[data-output-cards] [data-output='0'] [data-action='mute']")
    swapped = _press_through_a_refresh(desktop, mute, "[data-output-cards]")
    _one(desktop, sent)
    assert not swapped, "the part was replaced under the pressed button"
    wait_until(lambda: api.desired(0)["outputs"][0]["mute"] is (not before), what="the card's Mute", page=desktop)
    expect(mute).to_have_attribute("aria-pressed", str(not before).lower())
    api.patch_output(0, {"mute": before})
    api.wait_synced()


def test_home_switch_to_preset_survives_a_presets_refresh_during_the_press(desktop: Page, api: Api):
    api.patch_output(1, {"gain_db": -2}, preset=1)  # waiting again (a switch above sent the seeded change)
    visit(desktop, "/")
    switch = desktop.locator(".home-presets-box button", has_text="Switch to preset 2")
    expect(switch).to_be_visible()
    # htmx throttles per element: for a second after the page's first "master" event the box ignores "refresh" too.
    desktop.wait_for_function("() => !document.querySelector('.home-presets-box')['htmx-internal-data'].throttle")
    sent = _writes(desktop, "/master/preset")
    dialogs = _answer(desktop)
    try:
        swapped = _press_through_a_refresh(desktop, switch, ".home-presets-box")
        _one(desktop, sent)
        assert not swapped, "the part was replaced under the pressed button"
        assert len(dialogs) == 1 and dialogs[0].startswith("Switch to preset 2?"), dialogs
        wait_until(lambda: _status(api)["preset"] == 1, what="preset 2", page=desktop)
    finally:
        api.master(preset=0)
        wait_until(lambda: _status(api)["preset"] == 0, what="preset 1 again")


def test_two_refreshes_during_a_cancelled_preset_switch_still_end_on_the_new_state(desktop: Page, api: Api):
    api.master(mute=False)
    visit(desktop, "/")
    sent = _writes(desktop, "/master/preset")
    dialogs = _answer(desktop, accept=False)
    mute = desktop.locator("#master-mute")
    _press(desktop, desktop.locator("#preset-2"))
    with desktop.expect_response(lambda r: "/ui/header" in r.url):
        api.master(mute=True)
    # A second refresh while the button is still down is held too; it doesn't stand in for the catch-up after release.
    with desktop.expect_response(lambda r: "/ui/header" in r.url):
        desktop.evaluate("htmx.trigger(document.querySelector('#app-header'), 'refresh')")
    desktop.wait_for_timeout(200)
    expect(mute).to_have_attribute("aria-pressed", "false")
    desktop.mouse.up()
    wait_until(lambda: dialogs, what="the question", page=desktop)
    expect(mute).to_have_attribute("aria-pressed", "true")
    settle(desktop)
    assert sent == [] and len(dialogs) == 1, (sent, dialogs)
    api.master(mute=False)


def test_a_press_released_off_mute_through_two_changes_ends_on_the_new_state(desktop: Page, api: Api):
    api.master(mute=False, volume=-30, confirm=True)
    visit(desktop, "/")
    sent = _writes(desktop, "/master/")
    mute = desktop.locator("#master-mute")
    _press(desktop, mute)
    for change in ({"mute": True}, {"volume": -34}):
        with desktop.expect_response(lambda r: "/ui/header" in r.url):
            api.master(**change)
    desktop.wait_for_timeout(200)
    expect(mute).to_have_attribute("aria-pressed", "false")
    desktop.mouse.move(5, 890)  # slid off the button: the release is no click
    desktop.mouse.up()
    expect(mute).to_have_attribute("aria-pressed", "true")
    expect(desktop.locator("#master-volume-num")).to_have_value("-34.0")
    settle(desktop)
    assert sent == [], sent
    api.master(mute=False, volume=-30)  # back up 4 dB: under the gain-step guard


def test_a_write_reply_lands_during_a_press_in_the_same_part(desktop: Page, api: Api):
    """Only refreshes are held: Duplicate's reply carries a message no refresh of the list does."""
    api.create_profile("Press probe", api.desired(0))
    visit(desktop, "/profiles")
    item = desktop.locator(".profile-item[data-profile-name='Press probe']")
    _press(desktop, item.get_by_role("button", name="Compare"))
    with desktop.expect_response(lambda r: r.request.method == "POST" and "/duplicate" in r.url):
        item.get_by_role("button", name="Duplicate").evaluate("(b) => b.click()")
    message = desktop.locator("#profile-list .profile-msg")
    expect(message).to_have_text("Created “Press probe (copy)”.")
    desktop.mouse.up()
    settle(desktop, 500)
    expect(message).to_have_text("Created “Press probe (copy)”.")
