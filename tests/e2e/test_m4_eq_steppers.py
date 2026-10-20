"""The EQ width hint and the phone's band-card steppers (phase 9 design §8.2, §8.3, §16 phone rule 4) in a real browser.

Band 1 of Shakers is the walkthroughs' 4 dB cut at 40 Hz, Q 4 (reset before each test). On a phone each band card has
− / + steppers for frequency (1/6 octave), width (a ladder of round Q values) and the Cut / Boost magnitude (0.5 dB);
quick presses are sent once, like a typed value. Every band in use shows the range its width affects, in hertz.
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_m2_summary import CUT

EQ = "/outputs/1?tab=peq"
BAND = '.peq-table > tbody:has(input[aria-label="Band 1 Q"])'


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.wait_synced()

    return walkthrough


@pytest.fixture(autouse=True)
def cut_at_40(api: Api) -> None:
    api.put_peq_band("outputs", 0, 0, CUT)
    api.wait_synced()


def stored(api: Api) -> dict:
    return api.peq_band("outputs", 0, 0)["design"]


def band_posts(page: Page) -> list[str]:
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/peq/0") else None)
    return posts


def test_each_band_shows_the_range_its_width_affects(desktop: Page, api: Api, shoot):
    visit(desktop, EQ)
    band = desktop.locator(BAND)
    expect(band.locator(".q-range")).to_have_text("about 35–45 Hz")
    expect(desktop.locator(".peq-width-hint")).to_have_text("Width (Q 4) at 40 Hz: affects about 35–45 Hz")
    # the desktop table keeps plain fields: no steppers
    expect(band.locator(".peq-step")).to_have_count(6)
    for k in range(6):
        expect(band.locator(".peq-step").nth(k)).to_be_hidden()
    # the range follows typing at once, before anything is sent
    q = band.get_by_label("Band 1 Q", exact=True)
    q.fill("8")
    expect(band.locator(".q-range")).to_have_text("about 38–43 Hz")
    expect(desktop.locator(".peq-width-hint")).to_have_text("Width (Q 8) at 40 Hz: affects about 38–43 Hz")
    desktop.locator(".peq-width-hint").scroll_into_view_if_needed()
    shoot(desktop, "fu-eq-width-hint")
    q.press("Tab")
    wait_until(lambda: stored(api)["q"] == 8, what="the typed Q", page=desktop)


def test_on_a_phone_the_band_card_has_steppers(phone: Page, api: Api, shoot):
    visit(phone, EQ)
    posts = band_posts(phone)
    band = phone.locator(BAND)
    band.scroll_into_view_if_needed()
    steps = band.locator(".peq-step")
    expect(steps).to_have_count(6)
    for k in range(6):
        box = steps.nth(k).bounding_box()
        assert box and box["width"] >= 43.5 and box["height"] >= 43.5, box
    assert not has_horizontal_scroll(phone)
    expect(band.locator(".q-range")).to_have_text("about 35–45 Hz")
    shoot(phone, "fu-eq-steppers")

    freq = band.get_by_label("Band 1 frequency in Hz")
    band.get_by_role("button", name="Band 1: frequency 1/6 octave higher").tap()
    expect(freq).to_have_value("44.9")
    expect(band.locator(".q-range")).to_have_text("about 40–51 Hz")  # follows at once
    wait_until(lambda: stored(api)["freq"] == pytest.approx(44.9, abs=0.05), what="the stepped frequency", page=phone)

    # quick presses are sent once: Q 4 → 4.8 → 5.6 (the ladder)
    sent = len(posts)
    wider = band.get_by_role("button", name="Band 1: wider (lower Q)")
    narrower = band.get_by_role("button", name="Band 1: narrower (higher Q)")
    narrower.tap()
    narrower.tap()
    expect(band.get_by_label("Band 1 Q", exact=True)).to_have_value("5.6")
    wait_until(lambda: stored(api)["q"] == pytest.approx(5.6), what="the stepped Q", page=phone)
    settle(phone, 300)
    assert len(posts) - sent == 1, posts[sent:]
    wider.tap()
    wait_until(lambda: stored(api)["q"] == pytest.approx(4.8), what="the wider Q", page=phone)


def test_gain_steps_move_the_magnitude_and_keep_cut(phone: Page, api: Api, shoot):
    visit(phone, EQ)
    band = phone.locator(BAND)
    band.scroll_into_view_if_needed()
    mag = band.get_by_label("Band 1: dB to cut")
    more = band.get_by_role("button", name="Band 1: cut 0.5 dB more")
    less = band.get_by_role("button", name="Band 1: cut 0.5 dB less")
    more.tap()
    expect(mag).to_have_value("4.5")
    wait_until(lambda: stored(api)["gain_db"] == pytest.approx(-4.5), what="a deeper cut", page=phone)
    for _ in range(9):
        less.tap()
    expect(mag).to_have_value("0")
    expect(less).to_be_disabled()  # 0 dB is the floor: no step flips a cut into a boost
    wait_until(lambda: stored(api)["gain_db"] == 0, what="the cut stepped to 0", page=phone)
    expect(band.get_by_role("radio", name="Cut")).to_be_checked()
    more.tap()
    wait_until(lambda: stored(api)["gain_db"] == pytest.approx(-0.5), what="a cut again", page=phone)
    # Boost: the same steps raise
    band.locator("label", has_text="Boost").tap()
    wait_until(lambda: stored(api)["gain_db"] == pytest.approx(0.5), what="the boost", page=phone)
    band.get_by_role("button", name="Band 1: boost 0.5 dB more").tap()
    expect(band.get_by_label("Band 1: dB to boost")).to_have_value("1")
    wait_until(lambda: stored(api)["gain_db"] == pytest.approx(1.0), what="a bigger boost", page=phone)
    shoot(phone, "fu-eq-steppers-boost")
