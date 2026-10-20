"""A Home card's −/+ catches up on a refresh it held back, however the press ends (static/js/dashboard.js), on the mock.

While a card's −/+ is down the cards box drops its re-fetches (one would replace the button under the pointer and lose
the click). A click sends a change whose own re-fetch follows, but a press that ended without a click (the pointer slid
off the button, pointercancel, Escape, the window losing focus) left the card on the old values until the next event.
Each test holds a re-fetch rendered after an output changed elsewhere until the button is down, lets it land during the
press, ends the press without a click and checks that the card shows the new value and that nothing was written.
"""

import pytest
from playwright.sync_api import Locator, Page, Request, Route, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import visit

CARD = "[data-output-cards] [data-output='0']"
MINUS = f"{CARD} [data-action='level'][data-step='-0.5']"

# Counts the cards box's swaps that were dropped (the press held them back).
_COUNT_DROPS = """() => {
  window.__dropped = 0;
  document.addEventListener('htmx:beforeSwap', (e) => {
    if (e.detail.target.matches('[data-output-cards]') && !e.detail.shouldSwap) window.__dropped += 1;
  });
}"""


def _writes(page: Page) -> list[str]:
    sent: list[str] = []

    def record(request: Request) -> None:
        if request.method == "POST" and "/edit" in request.url:
            sent.append(request.url.split("?", 1)[0])

    page.on("request", record)
    return sent


class _Gate:
    """Holds the cards' re-fetches in the browser (the server answers at once) until `open()`."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.held: list[tuple[Route, object]] = []
        self.is_open = False
        page.route(lambda url: "/ui/home/" in url, self._route)

    def _route(self, route: Route) -> None:
        if self.is_open or route.request.method != "GET":
            route.continue_()
        else:
            self.held.append((route, route.fetch()))

    def open(self) -> None:
        self.is_open = True
        for route, response in self.held:
            route.fulfill(response=response)  # type: ignore[arg-type]


def _press(page: Page, button: Locator) -> None:
    button.scroll_into_view_if_needed()
    box = button.bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()


def _changed_elsewhere_during_a_press(page: Page, api: Api) -> list[str]:
    """Output 1 goes from −3 to −6 through the API; its re-fetch lands while − is held down (and is dropped)."""
    api.patch_output(0, {"gain_db": -3})
    api.wait_synced()
    visit(page, "/")
    card = page.locator(CARD)
    expect(card).to_have_attribute("data-gain", "-3.0")
    page.evaluate(_COUNT_DROPS)
    sent = _writes(page)
    gate = _Gate(page)
    api.patch_output(0, {"gain_db": -6})
    api.wait_synced()
    wait_until(lambda: gate.held, what="the cards' re-fetch", page=page)
    page.wait_for_timeout(600)  # every event of the change has been seen (later re-fetches queue behind the held one)
    _press(page, page.locator(MINUS))
    gate.open()
    # The repeat starts 450 ms after the press: this stays well inside it.
    page.wait_for_function(
        "() => window.__dropped > 0 && !document.querySelector('[data-output-cards].htmx-request')", timeout=300
    )
    expect(card).to_have_attribute("data-gain", "-3.0")  # held back: the press is still on
    return sent


def _release_off_the_button(page: Page) -> None:
    page.mouse.move(5, 890)
    page.mouse.up()


def _end_and_check(page: Page, api: Api, sent: list[str]) -> None:
    expect(page.locator(CARD)).to_have_attribute("data-gain", "-6.0", timeout=1500)
    page.wait_for_timeout(300)
    assert sent == [], f"a press that ended without a click wrote {sent}"
    assert api.desired(0)["outputs"][0]["gain_db"] == -6
    page.unroute_all(behavior="ignoreErrors")


def test_a_release_off_the_button_fetches_the_held_refresh(desktop: Page, api: Api):
    sent = _changed_elsewhere_during_a_press(desktop, api)
    _release_off_the_button(desktop)
    _end_and_check(desktop, api, sent)


@pytest.mark.parametrize(
    "end",
    [
        "(sel) => document.querySelector(sel).dispatchEvent(new PointerEvent('pointercancel', {bubbles: true}))",
        "() => window.dispatchEvent(new Event('blur'))",
        "Escape",
    ],
    ids=["pointercancel", "blur", "escape"],
)
def test_a_press_cut_short_fetches_the_held_refresh_at_once(desktop: Page, api: Api, end: str):
    sent = _changed_elsewhere_during_a_press(desktop, api)
    if end == "Escape":
        desktop.keyboard.press(end)
    else:
        desktop.evaluate(end, MINUS)
    # Caught up while the pointer is still down (after a blur no release may come at all).
    expect(desktop.locator(CARD)).to_have_attribute("data-gain", "-6.0", timeout=1500)
    _release_off_the_button(desktop)
    _end_and_check(desktop, api, sent)


def test_a_click_through_a_held_refresh_writes_once(desktop: Page, api: Api):
    api.patch_output(0, {"gain_db": -3})
    api.wait_synced()
    visit(desktop, "/")
    card = desktop.locator(CARD)
    expect(card).to_have_attribute("data-gain", "-3.0")
    desktop.evaluate(_COUNT_DROPS)
    sent = _writes(desktop)
    _press(desktop, desktop.locator(MINUS))
    with desktop.expect_response(lambda r: "/ui/home/" in r.url):
        desktop.evaluate("() => htmx.trigger(document.querySelector('[data-output-cards]'), 'refresh')")
    desktop.wait_for_function("() => window.__dropped > 0", timeout=300)
    desktop.mouse.up()
    wait_until(lambda: sent, what="the write", page=desktop)
    wait_until(lambda: api.desired(0)["outputs"][0]["gain_db"] == -3.5, what="the step", page=desktop)
    expect(card).to_have_attribute("data-gain", "-3.5")
    desktop.wait_for_timeout(500)
    assert len(sent) == 1, f"one write expected, got {sent}"
