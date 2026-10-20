"""A control swapped in by a refresh works from the first event after the swap, on the embedded mock.

htmx binds a swapped-in element's hx-* triggers when the swap settles, 20 ms after it by default. A click in that gap
went nowhere: the button looked ready but sent nothing. The header got a per-swap fix first
(test_fu_volume_header_swap); base.html's htmx-config now settles every swap at once. Each test here refreshes a part of
a page and uses a control in it in the first task after the swap, then counts the writes: exactly one.

The htmx buttons (the syncbar's, Home's "Switch to preset") lost the click on the old setting. The controls driven by
page scripts through event delegation (the Home cards' −/+ and Mute, the routing switches, the Level tab's steppers)
never depended on htmx binding them; they are here so the global setting can't break them either (e.g. by running
settle-time code before Alpine has started the new content).
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Dialog, Page, Request, expect

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


# Click `selector` in the first task after the next swap into an element matching `target` (before htmx would have
# settled it on the old 20 ms delay). Records whether the control was there and enabled in window.__clicked.
_CLICK_AFTER_THE_NEXT_SWAP = """([target, selector]) => {
  window.__clicked = null;
  const swapped = (e) => {
    if (!(e.detail.target instanceof Element) || !e.detail.target.matches(target)) return;
    document.removeEventListener('htmx:afterSwap', swapped);
    setTimeout(() => {
      const el = document.querySelector(selector);
      window.__clicked = !!el && !el.disabled;
      if (el) el.click();
    }, 0);
  };
  document.addEventListener('htmx:afterSwap', swapped);
}"""


def _writes(page: Page, *parts: str) -> list[str]:
    """POSTs the page sends whose URL contains one of `parts`, as their path."""
    sent: list[str] = []

    def record(request: Request) -> None:
        if request.method == "POST" and any(p in request.url for p in parts):
            sent.append(request.url.split("?", 1)[0])

    page.on("request", record)
    return sent


def _answer(page: Page) -> list[str]:
    dialogs: list[str] = []

    def answer(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    page.on("dialog", answer)
    return dialogs


def _click_after_swap(page: Page, target: str, selector: str, refresh: str) -> None:
    page.evaluate(_CLICK_AFTER_THE_NEXT_SWAP, [target, selector])
    page.evaluate(refresh)
    wait_until(lambda: page.evaluate("() => window.__clicked") is not None, what="the swap and the click", page=page)
    assert page.evaluate("() => window.__clicked"), f"{selector} wasn't there (or was disabled) after the swap"


def _wait_for_writes(page: Page, sent: list[str], n: int = 1) -> None:
    wait_until(lambda: len(sent) >= n, what=f"{n} write(s)", page=page)
    settle(page, 500)  # nothing more follows
    assert len(sent) == n, f"{n} write(s) expected, got {sent}"


# --- htmx buttons: lost on the old 20 ms settle -----------------------------------------------------------------------


def test_send_all_right_after_the_syncbar_refresh_asks_and_sends(desktop: Page, api: Api):
    visit(desktop, "/routing")
    sent = _writes(desktop, "/push")
    dialogs = _answer(desktop)
    _click_after_swap(
        desktop,
        "#syncbar",
        "#syncbar .syncbar-actions button[hx-post*='/push']",
        "() => document.body.dispatchEvent(new CustomEvent('mdsp-sync-changed'))",
    )
    _wait_for_writes(desktop, sent)
    assert len(dialogs) == 1 and dialogs[0].startswith("Send every setting of preset 1"), dialogs
    api.wait_synced(timeout=90)  # every setting of the preset, one by one


def test_home_switch_to_preset_right_after_the_presets_refresh_asks_and_switches(desktop: Page, api: Api):
    visit(desktop, "/")
    switch = desktop.locator(".home-presets-box button", has_text="Switch to preset 2")
    expect(switch).to_be_visible()
    sent = _writes(desktop, "/master/preset")
    dialogs = _answer(desktop)
    try:
        _click_after_swap(
            desktop,
            ".home-presets-box",
            ".home-presets-box button[hx-post]",
            "() => htmx.trigger('.home-presets-box', 'refresh')",
        )
        _wait_for_writes(desktop, sent)
        assert len(dialogs) == 1 and dialogs[0].startswith("Switch to preset 2?"), dialogs
        wait_until(
            lambda: api.http.get(f"{api.dev}/status").json()["master"]["preset"] == 1, what="preset 2", page=desktop
        )
    finally:
        api.master(preset=0)
        wait_until(lambda: api.http.get(f"{api.dev}/status").json()["master"]["preset"] == 0, what="preset 1 again")


# --- controls driven by page scripts: must keep working ---------------------------------------------------------------


@pytest.mark.parametrize("action", ["level", "mute"])
def test_home_card_buttons_right_after_the_cards_refresh_send_one_write(desktop: Page, api: Api, action: str):
    visit(desktop, "/")
    selector = (
        "[data-output-cards] [data-output='0'] [data-action='level'][data-step='-0.5']"
        if action == "level"
        else "[data-output-cards] [data-output='0'] [data-action='mute']"
    )
    before = api.desired(0)["outputs"][0]
    sent = _writes(desktop, "/edit")
    _answer(desktop)
    _click_after_swap(desktop, "[data-output-cards]", selector, "() => htmx.trigger('[data-output-cards]', 'refresh')")
    _wait_for_writes(desktop, sent)
    after = api.desired(0)["outputs"][0]
    if action == "level":
        assert after["gain_db"] == before["gain_db"] - 0.5
        api.patch_output(0, {"gain_db": before["gain_db"]})
    else:
        assert after["mute"] is (not before["mute"])
        api.patch_output(0, {"mute": before["mute"]})
    api.wait_synced()


def test_a_routing_switch_right_after_the_syncbar_refresh_sends_one_write(desktop: Page, api: Api):
    visit(desktop, "/routing")
    box = desktop.locator(".route-toggle input:not(:checked)").first
    cell = box.get_attribute("id")
    assert cell
    sent = _writes(desktop, "/edit")
    _answer(desktop)
    _click_after_swap(
        desktop, "#syncbar", f"#{cell}", "() => document.body.dispatchEvent(new CustomEvent('mdsp-sync-changed'))"
    )
    _wait_for_writes(desktop, sent)
    expect(desktop.locator(f"#{cell}")).to_be_checked()
    i, o = (int(n) - 1 for n in cell.removeprefix("in").split("-out"))
    assert api.desired(0)["inputs"][i]["routing"][o]["enabled"] is True
    api._ok(
        api.http.patch(
            f"{api.dev}/presets/0",
            params={"confirm": "true", "commit": "true"},
            json={"inputs": {str(i): {"routing": {str(o): {"enabled": False}}}}},
        )
    )
    api.wait_synced()


def test_the_level_stepper_right_after_the_level_tab_swaps_in_sends_one_write(desktop: Page, api: Api):
    visit(desktop, "/outputs/2?tab=peq")
    before = api.desired(0)["outputs"][1]["gain_db"]
    sent = _writes(desktop, "/edit")
    _answer(desktop)
    desktop.evaluate(_CLICK_AFTER_THE_NEXT_SWAP, ["[data-chain-box]", "[data-chain-step][data-step='-0.5']"])
    desktop.locator("[data-chain-tab='level']").click()
    wait_until(
        lambda: desktop.evaluate("() => window.__clicked") is not None, what="the tab and the click", page=desktop
    )
    assert desktop.evaluate("() => window.__clicked")
    _wait_for_writes(desktop, sent)
    wait_until(lambda: api.desired(0)["outputs"][1]["gain_db"] == before - 0.5, what="the step", page=desktop)
    api.patch_output(1, {"gain_db": before})
    api.wait_synced()
