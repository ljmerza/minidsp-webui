"""A held Home card −/+ keeps repeating while the pointer moves within the button (static/js/dashboard.js), on the mock.

The hold stopped early on a `pointerleave` from anything inside the card, and the −/+ glyph is a <span> inside the
button: sliding from the glyph onto the button's padding (still on the button) ended the repeat. Only leaving the
pressed button itself ends it now. The page's clock is driven by hand (the repeat starts 450 ms after the press, then
steps every 150 ms), so the number of steps, and of writes, is exact.
"""

import re

from playwright.sync_api import Page, Request, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import visit

CARD = "[data-output-cards] [data-output='0']"
MINUS = f"{CARD} [data-action='level'][data-step='-0.5']"


def _gain(steps: int) -> re.Pattern[str]:
    """The card's data-gain after `steps` steps down from −3 dB (the card writes −4 as "-4", the server "-4.0")."""
    value = -3 - 0.5 * steps
    return re.compile(rf"^{value:g}(\.0)?$")


def test_moving_within_a_held_button_keeps_stepping_and_leaving_it_stops(desktop: Page, api: Api):
    api.patch_output(0, {"gain_db": -3})
    api.wait_synced()
    desktop.clock.install()
    visit(desktop, "/")
    card = desktop.locator(CARD)
    expect(card).to_have_attribute("data-gain", "-3.0")
    sent: list[Request] = []
    desktop.on("request", lambda r: sent.append(r) if r.method == "POST" and "/edit" in r.url else None)
    desktop.clock.pause_at(desktop.evaluate("() => Date.now()") + 1000)

    button = desktop.locator(MINUS)
    button.scroll_into_view_if_needed()
    outer = button.bounding_box()
    glyph = button.locator("span").bounding_box()
    assert outer and glyph
    on_glyph = (glyph["x"] + glyph["width"] / 2, glyph["y"] + glyph["height"] / 2)
    # Points on the button but off the glyph: its padding, left, right, above and below the glyph.
    padding = [
        (outer["x"] + 3, outer["y"] + outer["height"] / 2),
        (outer["x"] + outer["width"] - 3, outer["y"] + outer["height"] / 2),
        (on_glyph[0], outer["y"] + 3),
        (on_glyph[0], outer["y"] + outer["height"] - 3),
    ]
    for x, y in padding:  # each one really is off the glyph and on the button
        hit = desktop.evaluate(
            "([x, y, sel]) => document.elementFromPoint(x, y) === document.querySelector(sel)", [x, y, MINUS]
        )
        assert hit, f"({x}, {y}) is not on the button's padding"

    def step(n: int) -> None:
        """Lets the clock reach the n-th step, which is written and answered before the next one."""
        desktop.clock.run_for(450 if n == 1 else 150)
        expect(card).to_have_attribute("data-gain", _gain(n))
        wait_until(lambda: len(sent) == n and sent[-1].response() is not None, what=f"write {n}", page=desktop)

    desktop.mouse.move(*on_glyph)
    desktop.mouse.down()
    step(1)
    # Wander between the glyph and the button's padding, in several small moves, between the steps.
    for n, (x, y) in enumerate(padding * 2, start=2):
        desktop.mouse.move(x, y, steps=4)
        desktop.mouse.move(*on_glyph, steps=4)
        desktop.mouse.move(x, y, steps=4)
        step(n)
    steps = 1 + len(padding) * 2

    # Leaving the button stops the repeat; the release off the button sends nothing more.
    desktop.mouse.move(outer["x"] + outer["width"] / 2, outer["y"] - 40, steps=4)
    desktop.clock.run_for(1500)
    desktop.mouse.up()
    desktop.clock.run_for(1500)
    desktop.clock.resume()
    expect(card).to_have_attribute("data-gain", _gain(steps))
    wait_until(lambda: api.desired(0)["outputs"][0]["gain_db"] == -3 - 0.5 * steps, what="the last step", page=desktop)
    desktop.wait_for_timeout(500)
    assert len(sent) == steps, f"{steps} writes expected, got {len(sent)}"
    assert api.desired(0)["outputs"][0]["gain_db"] == -3 - 0.5 * steps
