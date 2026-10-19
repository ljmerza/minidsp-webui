"""Phones never scroll sideways on a device that isn't set up yet.

Before adoption every setting is unknown, so each chip in an output or input page's "Settings on this output" strip
carries extra screen-reader words (a ``.visually-hidden`` span, which is ``position: absolute``). On a phone that strip
scrolls sideways inside itself; the spoken words must stay clipped by it rather than widen the whole page. The
walkthrough state the other modules use is adopted, so it never renders those words.
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api
from tests.e2e.pages import has_horizontal_scroll, visit
from tests.e2e.test_harness import PAGES


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def not_set_up(api: Api) -> None:
        """A fresh database: the device is never adopted."""

    return not_set_up


@pytest.mark.parametrize("path", PAGES)
def test_phone_pages_never_scroll_sideways_before_setup(phone: Page, path: str):
    visit(phone, path)
    assert not has_horizontal_scroll(phone), path


def test_chain_strip_spoken_words_stay_inside_the_strip(phone: Page, shoot):
    visit(phone, "/outputs/1")
    spoken = phone.locator(".chain-list .chain-chip .visually-hidden")
    expect(spoken.first).to_be_attached()
    assert spoken.count() > 1
    assert phone.evaluate("() => getComputedStyle(document.querySelector('.chain-list')).position") != "static"
    assert not has_horizontal_scroll(phone)
    shoot(phone, "fu-phone-unadopted-output")
