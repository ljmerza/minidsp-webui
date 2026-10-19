"""Phase 9 M2 review fixes in a real browser, on the embedded mock.

- A channel name with quotes never runs as code (it used to break out of the flow diagram's CLIP label expression).
- On a phone, a preset switch or Volume −/+ made from inside the open header sheet shows its result at once.
- After a device check finds a change, the editing page's sync bar and badges say ≠ as the header does.
- [Unmute all] fixes "Everything is muted", with Undo.
"""

import re

from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import settle, visit
from tests.e2e.server import AppServer

EVIL = "'+(document.title='PWNED')+'\""


def test_a_channel_name_with_quotes_never_runs_as_code(desktop: Page, api: Api):
    api.name_channel("output", 3, EVIL)
    try:
        visit(desktop, "/")
        title = desktop.title()
        assert "PWNED" not in title
        chip = desktop.locator("[data-flow] .flow-clip").last
        expect(chip).to_have_attribute("aria-label", f"{EVIL} not clipping")
        visit(desktop, "/meters")
        assert "PWNED" not in desktop.title()
    finally:
        api.name_channel("output", 3, "")


def test_phone_preset_switch_from_the_sheet_shows_the_new_preset(phone: Page, api: Api):
    visit(phone, "/")
    toggle = phone.locator(".hdr-preset-toggle")
    expect(toggle).to_contain_text("P1")
    toggle.tap()
    expect(phone.locator("#hdr-presets")).to_be_visible()
    phone.once("dialog", lambda d: d.accept())
    try:
        phone.locator("#preset-2").tap()
        # The reply to the switch is shown at once: P2 plays, its button is pressed, and the sheet has closed.
        expect(toggle).to_contain_text("P2", timeout=15_000)
        expect(phone.locator("#preset-2")).to_have_attribute("aria-pressed", "true")
        expect(phone.locator("#preset-1")).to_have_attribute("aria-pressed", "false")
        expect(phone.locator("#hdr-presets")).to_be_hidden()
        expect(toggle).to_be_focused()
    finally:
        api.master(preset=0)
        wait_until(lambda: api.http.get(f"{api.dev}/status").json()["master"]["preset"] == 0, what="preset 1 back")


def test_phone_volume_steps_with_the_sheet_open_update_it(phone: Page, api: Api):
    api.master(volume=-30.0)
    visit(phone, "/")
    phone.locator(".vol-open").tap()
    sheet = phone.locator("#hdr-volume-sheet")
    expect(sheet).to_be_visible()
    for _ in range(3):
        before = phone.locator("#master-volume-num").input_value()
        phone.locator("#volume-up").tap()
        expect(phone.locator("#master-volume-num")).not_to_have_value(before)
    expect(phone.locator("#master-volume-num")).to_have_value("-28.5")
    expect(phone.locator(".vol-open-value")).to_contain_text("28.5")
    expect(sheet).to_be_visible()  # it stays open for the next step
    phone.keyboard.press("Escape")
    api.master(volume=-30.0)


def test_editing_page_says_changed_on_device_after_a_check(desktop: Page, api: Api, app: AppServer):
    api.wait_synced()
    app.write_behind_the_apps_back({"outputs": [{"index": 1, "gain": -6.0}]})
    api._ok(api.http.post(f"{api.dev}/verify"))
    visit(desktop, "/outputs/2")
    expect(desktop.locator(".hdr-status")).to_contain_text(re.compile(r"\d+ changed on device"))
    bar = desktop.locator("#syncbar .syncbar-status")
    expect(bar).to_contain_text("changed on device")
    expect(bar).not_to_contain_text("all ")
    badge = desktop.locator('[data-sync-for="outputs.1.gain_db"]').first
    expect(badge).to_contain_text("changed on device")
    expect(badge).to_have_class(re.compile(r"\bsync-changed\b"))
    # /meters reads the same model.
    visit(desktop, "/meters")
    expect(desktop.locator('[data-output="1"] [data-sync-badge]')).to_contain_text("changed on device")


def test_unmute_all_fixes_everything_is_muted_with_undo(desktop: Page, api: Api):
    api.master(mute=True)
    try:
        visit(desktop, "/")
        desktop.locator(".hdr-pill").click()
        item = desktop.locator("#hdr-problems-list .pill-item", has_text="Everything is muted")
        button = item.get_by_role("button", name="Unmute all")
        expect(button).to_be_visible()
        button.click()
        wait_until(lambda: api.http.get(f"{api.dev}/status").json()["master"]["mute"] is False, what="unmuted")
        expect(desktop.locator("#master-mute")).to_have_attribute("aria-pressed", "false")
        toast = desktop.get_by_role("button", name="Undo")
        expect(toast).to_be_visible()
        toast.click()
        wait_until(lambda: api.http.get(f"{api.dev}/status").json()["master"]["mute"] is True, what="muted again")
        settle(desktop)
    finally:
        api.master(mute=False)
