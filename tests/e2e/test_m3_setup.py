"""Phase 9 PR 13: Save or load a setup (design §14.8) in a real browser, on the walkthroughs' state (design 4.1).

In 1 feeds Shakers (+4 dB route, +12 dB output); the Sub's route is off, so the Sub is silent; Out 3 and Out 4 are Not
in use. The profile "Movies" holds that preset. Preset 2 was never set up, so it holds the defaults (0 dB levels):
loading "Movies" into it raises Shakers' output level by 12 dB, which needs a confirmation.
"""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_m2_home import seed  # noqa: F401  (the walkthrough state)
from tests.e2e.test_m2_shell import SMALL_TARGETS


def dialog(page: Page):
    return page.locator("#task-dialog")


def wait_step(page: Page, title: str) -> None:
    expect(dialog(page).locator(".task-heading")).to_contain_text(title)
    settle(page, 150)


def no_browser_dialogs(page: Page) -> list[str]:
    seen: list[str] = []

    def handle(d) -> None:
        seen.append(d.message)
        d.dismiss()

    page.on("dialog", handle)
    return seen


def test_save_shows_the_health_check_for_the_silent_sub_then_saves(screen: Page, api: Api, shoot):
    visit(screen, "/")
    screen.locator(".home-presets").get_by_role("link", name=re.compile("Save current as")).click()
    wait_step(screen, "Check")
    expect(dialog(screen).locator(".task-heading")).to_be_focused()
    expect(dialog(screen)).to_contain_text("Sub is silent: no input is routed to it.")
    expect(dialog(screen).get_by_role("link", name="Fix first")).to_be_visible()
    shoot(screen, "p9-chunk13-save-check")
    assert not has_horizontal_scroll(screen)
    if screen.screen_name == "phone":  # type: ignore[attr-defined]
        small = screen.evaluate(SMALL_TARGETS)
        assert not small, small
    assert_accessible(screen, include="#task-dialog")
    dialog(screen).get_by_role("button", name=re.compile("Save anyway")).click()
    wait_step(screen, "Name")
    field = dialog(screen).get_by_label("Name (for example Movies, Music, Night)")
    expect(field).to_have_value("")
    assert field.get_attribute("placeholder") is None
    name = f"Night {screen.screen_name}"  # type: ignore[attr-defined]
    field.fill(name)
    dialog(screen).get_by_role("button", name="Save", exact=True).click()
    wait_step(screen, "Saved")
    expect(dialog(screen)).to_contain_text(f"Saved “{name}” from preset 1. Same as preset 1 right now.")
    shoot(screen, "p9-chunk13-save-done")
    dialog(screen).get_by_role("button", name="Done").click()
    expect(dialog(screen)).to_be_hidden()
    # The profile appears on Home.
    expect(screen.locator(".home-profile-list")).to_contain_text(name)
    assert any(p["name"] == name for p in api._ok(api.http.get("/api/v1/profiles")))


def test_load_into_preset_2_needs_one_confirm_with_the_raise_checkbox_and_keeps_the_target(
    desktop: Page, api: Api, shoot
):
    dialogs = no_browser_dialogs(desktop)
    before = api.desired(1)
    visit(desktop, "/profiles")
    desktop.locator(".profile-item", has_text="Movies").get_by_role("link", name=re.compile("^Load…")).click()
    wait_step(desktop, "Which profile?")
    expect(dialog(desktop).get_by_role("radio", name=re.compile("Movies"))).to_be_checked()
    dialog(desktop).get_by_role("button", name=re.compile("Next")).click()
    wait_step(desktop, "Into which preset?")
    dialog(desktop).get_by_role("radio", name=re.compile(r"^Preset 2")).check()
    dialog(desktop).get_by_role("button", name=re.compile("Next")).click()
    wait_step(desktop, "When?")
    expect(dialog(desktop).get_by_role("radio", name=re.compile("Store in preset 2"))).to_be_checked()
    dialog(desktop).get_by_role("button", name=re.compile("Next")).click()
    wait_step(desktop, "Check")
    box = dialog(desktop).get_by_role("checkbox", name="I confirm raising Shakers output level by 12 dB.")
    expect(box).to_be_visible()
    expect(dialog(desktop)).to_contain_text("Shakers output level")
    expect(dialog(desktop)).to_contain_text("Nothing is sent until you press Load.")
    assert "outputs." not in dialog(desktop).inner_text()
    shoot(desktop, "p9-chunk13-load-check")
    assert_accessible(desktop, include="#task-dialog")
    # Load without the tick: the browser keeps the form (the box is required) and nothing is loaded.
    dialog(desktop).get_by_role("button", name="Load", exact=True).click()
    desktop.wait_for_timeout(300)
    assert api.desired(1) == before
    box.check()
    dialog(desktop).get_by_role("button", name="Load", exact=True).click()
    wait_step(desktop, "Result")
    expect(dialog(desktop)).to_contain_text("“Movies” is in preset 2 (waiting until you switch to it).")
    assert wait_until(lambda: api.desired(1)["outputs"][0]["gain_db"] == 12.0, what="Movies in preset 2")
    assert dialogs == [], f"a browser dialog appeared: {dialogs}"  # the one confirm was inside the wizard
    shoot(desktop, "p9-chunk13-load-result")
    # The chosen preset is kept for the next load (P11).
    dialog(desktop).get_by_role("link", name=re.compile("Load another profile into preset 2")).click()
    wait_step(desktop, "Which profile?")
    dialog(desktop).get_by_role("button", name=re.compile("Next")).click()
    wait_step(desktop, "Into which preset?")
    expect(dialog(desktop).get_by_role("radio", name=re.compile(r"^Preset 2"))).to_be_checked()
    desktop.keyboard.press("Escape")
    expect(dialog(desktop)).to_be_hidden()


def test_undo_after_a_load_puts_preset_2_back(desktop: Page, api: Api):
    before = api.desired(2)
    visit(desktop, "/?task=profiles&profile=1&target=3&when=store&step=5")
    wait_step(desktop, "Check")
    dialog(desktop).get_by_role("checkbox", name=re.compile("I confirm raising Shakers output level")).check()
    dialog(desktop).get_by_role("button", name="Load", exact=True).click()
    wait_step(desktop, "Result")
    assert wait_until(lambda: api.desired(2)["outputs"][0]["gain_db"] == 12.0, what="Movies in preset 3")
    dialog(desktop).get_by_role("button", name="Undo").click()
    expect(dialog(desktop).locator(".task-msg").first).to_contain_text("Undone: preset 3 holds what it had before.")
    assert wait_until(lambda: api.desired(2) == before, what="preset 3 back as it was")


PAGES = [
    "/tasks/profiles",
    "/tasks/profiles?do=save&step=3",
    "/tasks/profiles?do=load&profile=1&step=3",
    "/tasks/profiles?do=load&profile=1&target=2&step=4",
    "/tasks/profiles?do=load&profile=1&target=2&when=store&step=5",
]


@pytest.mark.parametrize("path", PAGES)
def test_the_profiles_wizard_pages_on_a_phone(phone: Page, path: str):
    visit(phone, path)
    assert not has_horizontal_scroll(phone)
    small = phone.evaluate(SMALL_TARGETS)
    assert not small, f"touch targets under 44 px on {path}: {small}"
    assert_accessible(phone)
