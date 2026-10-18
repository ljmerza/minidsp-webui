"""The harness itself: the app under test, the two screens and the accessibility helper."""

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.axe import assert_accessible, axe_violations
from tests.e2e.pages import visit
from tests.e2e.server import AppServer

# Every page, as it is linked from the navigation.
PAGES = [
    "/",
    "/outputs/1",
    "/outputs/1?tab=crossover",
    "/outputs/1?tab=peq",
    "/outputs/1?tab=compressor",
    "/outputs/1?tab=response",
    "/inputs/1",
    "/routing",
    "/system",
    "/profiles",
    "/device",
    "/settings",
]

# axe findings in today's UI that later phase 9 PRs fix (design §16.2: "dimmed blocks use a lighter text colour, not
# opacity"; PR 15 reworks the graphs, PR 17 runs axe on every page). Anything else on these pages fails the test.
KNOWN_VIOLATIONS: dict[str, set[str]] = {
    "/": {"color-contrast"},  # the dim CLIP lamps on the meters
    "/outputs/1?tab=peq": {"color-contrast"},  # dimmed (bypassed) PEQ rows
    "/inputs/1": {"color-contrast"},  # dimmed (bypassed) PEQ rows
    "/system": {"aria-prohibited-attr"},  # aria-label on the graph's plain <div>
}


def test_the_app_runs_the_bundled_daemon_with_only_the_mock(app: AppServer):
    health = httpx.get(f"{app.url}/api/v1/health", timeout=5).json()
    assert health["daemon_mode"] == "embedded" and health["daemon_mock"] is True
    assert [d["serial"] for d in health["devices"]] == [app.serial]
    assert app.url.startswith("http://127.0.0.1:")


def test_the_walkthrough_state_is_seeded(api):
    adopt = api.http.get(f"{api.dev}/adopt").json()
    assert adopt["adopted"] is True
    assert api.desired(0)["outputs"][0]["gain_db"] == 0.0


def test_desktop_is_1440_by_900_with_a_mouse(desktop: Page):
    visit(desktop, "/")
    size = desktop.evaluate("() => [innerWidth, innerHeight, matchMedia('(pointer: fine)').matches]")
    assert size == [1440, 900, True]


def test_phone_is_390_by_844_with_touch(phone: Page):
    visit(phone, "/")
    info = phone.evaluate(
        """() => [innerWidth, innerHeight, devicePixelRatio,
                  matchMedia('(pointer: coarse)').matches, 'ontouchstart' in window]"""
    )
    assert info == [390, 844, 3, True, True]


def test_the_header_shows_the_mock_device_on_both_screens(screen: Page, shoot):
    visit(screen, "/")
    expect(screen.locator("body")).to_have_attribute("data-serial", "911111")
    expect(screen.get_by_role("main")).to_be_visible()
    shoot(screen, "e2e-harness-home")


@pytest.mark.parametrize("path", PAGES)
def test_pages_have_no_new_accessibility_violations(screen: Page, path: str):
    visit(screen, path)
    assert_accessible(screen, allow=KNOWN_VIOLATIONS.get(path, ()))


def test_the_axe_helper_finds_a_violation(desktop: Page):
    visit(desktop, "/settings")
    desktop.evaluate(
        """() => {
          const button = document.createElement('button');
          button.id = 'e2e-unnamed';
          document.querySelector('main').append(button);
        }"""
    )
    found = axe_violations(desktop, include="#e2e-unnamed")
    assert [v["id"] for v in found] == ["button-name"]
    assert found[0]["nodes"] == ["#e2e-unnamed"]
    assert axe_violations(desktop, include="#e2e-unnamed", allow={"button-name"}) == []
