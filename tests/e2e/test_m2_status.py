"""Phase 9 PR 7 (one status model + Device & sync page) in a real browser, on the embedded mock.

The mock stores levels but not filter coefficients, so a check reads every filter back as all zeros: those settings
come back "changed on device", and routing, switches, compressor timing and FIR can't be checked (design §11, the
"92 changed" of the walkthroughs). Acceptance: the header never claims a match; the check headline reads "N changed on
device; 100 can't be checked"; no b0…a2 is visible unless Technical details is opened.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.server import AppServer

CHECK = re.compile(r"\d+ changed on device; 100 can't be checked")


CUT = {"design": {"type": "peaking", "freq": 40, "q": 4, "gain_db": -4}, "bypass": False}


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def state(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        # EQ cuts: the mock acknowledges their coefficients but reads them back as all zeros.
        api.put_peq_band("outputs", 0, 0, CUT)
        api.put_peq_band("outputs", 1, 0, CUT)
        api.wait_synced()

    return state


@pytest.fixture(scope="module", autouse=True)
def sub_changed_on_the_device(app: AppServer) -> None:
    app.write_behind_the_apps_back({"outputs": [{"index": 1, "gain": -6.0}]})


def _check(page: Page, api: Api) -> None:
    # The finished check is announced over the event stream and the panel reloads 300 ms later; wait for that reload.
    with page.expect_response(
        lambda r: r.request.method == "GET" and r.url.endswith(f"/ui/devices/{api.serial}/verify")
    ):
        page.get_by_role("button", name="Check device", exact=True).click()
    expect(page.locator("#verify-result")).to_contain_text(CHECK)
    settle(page)


def _visible_text(page: Page) -> str:
    return page.locator("body").inner_text()


def test_header_before_a_check_never_claims_a_match(screen: Page, api: Api):
    api.wait_synced()
    visit(screen, "/routing")
    status = screen.locator(".hdr-status")
    # Adopting the mock at factory defaults leaves a few settings it reads back differently unknown.
    expect(status).to_contain_text(re.compile(r"All sent|\d+ unknown"))
    assert "match" not in status.inner_text().lower() and "sync" not in status.inner_text().lower()


def test_check_device_headline_header_and_table(screen: Page, api: Api, shoot):
    api.wait_synced()
    visit(screen, "/device")
    expect(screen.locator("#sync-overview")).to_be_visible()
    _check(screen, api)

    # The status overview and the header agree, and neither claims a match.
    expect(screen.locator("#sync-overview .sync-headline")).to_contain_text(re.compile(r"\d+ changed on device"))
    header = screen.locator(".hdr-status")
    expect(header).to_contain_text(re.compile(r"≠\s*\d+ changed on device"))
    assert "match" not in header.inner_text().lower()
    expect(screen.locator(".verify-cant-check")).to_contain_text(
        "100 settings can't be checked: routing, on/off switches, compressor timing and FIR."
    )

    # Problems only, grouped by channel; a changed filter says what the device read in words.
    table = screen.locator("#sync-table")
    expect(table.get_by_role("button", name=re.compile(r"^Problems only"))).to_have_attribute("aria-pressed", "true")
    shakers = table.locator("details#sync-output-0")
    expect(shakers).to_have_attribute("open", "")
    expect(shakers.locator("summary")).to_contain_text("changed on device")
    expect(shakers).to_contain_text("all zeros (looks like a read error)")
    expect(shakers.get_by_role("button", name=re.compile(r"^Forget these values"))).to_be_visible()

    # No raw coefficients or internal names until Technical details is opened.
    text = _visible_text(screen)
    assert "b0" not in text and "outputs." not in text and "Daemon index" not in text, "technical detail is showing"
    shoot(screen, "e2e-m2-device-sync")
    assert_accessible(screen, include="#main")
    if screen.screen_name == "phone":  # type: ignore[attr-defined]
        assert not has_horizontal_scroll(screen)

    table.get_by_text("Show technical details").click()
    expect(shakers.locator(".row-tech").first).to_be_visible()
    assert "b0=" in _visible_text(screen)
    shoot(screen, "e2e-m2-device-sync-technical")


def test_the_technical_switch_and_folds_survive_a_reload_of_the_table(desktop: Page, api: Api):
    visit(desktop, "/device")
    table = desktop.locator("#sync-table")
    expect(table).to_be_visible()
    table.get_by_text("Show technical details").click()
    with desktop.expect_response(lambda r: "/sync-table" in r.url and "show=all" in r.url):
        table.get_by_role("button", name=re.compile(r"^Everything")).click()
    settle(desktop)
    # The table reloaded (everything), and the switch stayed on.
    expect(desktop.locator("#sync-tech")).to_be_checked()
    expect(desktop.locator("#sync-output-0 .row-tech").first).to_be_visible()


def test_send_the_apps_value_clears_the_change(desktop: Page, api: Api):
    visit(desktop, "/device")
    if not desktop.locator("#verify-result").count():
        _check(desktop, api)
    row = desktop.locator("#sync-output-0 tr.row-changed").first
    expect(row).to_be_visible()
    label = row.locator(".param-name").inner_text()
    before = desktop.locator("#sync-output-0 tr.row-changed").count()
    with desktop.expect_response(lambda r: "/paths/push" in r.url):
        row.get_by_role("button", name=re.compile(r"^Send app's value")).click()
    # The row is sent again, so it is no longer "changed on device" (it may show Sending for a moment).
    expect(desktop.locator("#sync-output-0 tr.row-changed")).to_have_count(before - 1, timeout=10_000)
    assert label not in [n.inner_text() for n in desktop.locator("#sync-output-0 tr.row-changed .param-name").all()]
