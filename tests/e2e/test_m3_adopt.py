"""Phase 9 PR 13: Set up this device (design §14.7) in a real browser.

The mock starts the way the walkthroughs left it (set up at factory defaults, Out 1 named "Shakers", Out 2 "Sub"). The
factory-defaults path keeps the adopt guard: it warns about the custom names and needs the explicit confirmation.
"""

import re

from playwright.sync_api import Page, expect

from tests.e2e.api import Api
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_m2_shell import SMALL_TARGETS


def dialog(page: Page):
    return page.locator("#task-dialog")


def wait_step(page: Page, title: str, timeout: float = 5000) -> None:
    expect(dialog(page).locator(".task-heading")).to_contain_text(title, timeout=timeout)
    settle(page, 150)


def test_the_factory_defaults_path_still_requires_the_explicit_confirmation(screen: Page, api: Api, shoot):
    visit(screen, "/device")
    screen.get_by_role("link", name=re.compile("Set up step by step")).click()
    wait_step(screen, "Why set it up?")
    expect(dialog(screen)).to_contain_text("Nothing is written until the last step, and only if you choose to.")
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()
    wait_step(screen, "How was it set up?")
    dialog(screen).get_by_role("radio", name="It's brand new or factory reset").check()
    warning = dialog(screen).locator(".task-named-warning")
    expect(warning).to_contain_text("You named Out 1 “Shakers” and Out 2 “Sub”, so it was probably set up before.")
    shoot(screen, "p9-chunk13-adopt-factory")
    assert not has_horizontal_scroll(screen)
    if screen.screen_name == "phone":  # type: ignore[attr-defined]
        small = screen.evaluate(SMALL_TARGETS)
        assert not small, small
    assert_accessible(screen, include="#task-dialog")
    # Without the tick it doesn't go on (the box is required; the server refuses it too).
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()
    screen.wait_for_timeout(300)
    expect(dialog(screen).locator(".task-heading")).to_contain_text("How was it set up?")
    dialog(screen).get_by_role("checkbox", name="My device really is at factory defaults.").check()
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()
    wait_step(screen, "Which presets?")
    expect(dialog(screen).get_by_role("checkbox", name=re.compile("Preset 1"))).to_be_checked()
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()
    wait_step(screen, "Preview")
    expect(dialog(screen)).to_contain_text("Does this match how it sounds today?")
    expect(dialog(screen)).to_contain_text("Shakers: Plays full range")
    dialog(screen).get_by_role("button", name=re.compile("Yes")).click()
    wait_step(screen, "Remember or write?")
    expect(dialog(screen).get_by_role("radio", name=re.compile("Remember"))).to_be_checked()
    expect(dialog(screen)).to_contain_text("You confirmed that your device really is at factory defaults.")
    shoot(screen, "p9-chunk13-adopt-choice")
    dialog(screen).get_by_role("button", name="Set up", exact=True).click()
    wait_step(screen, "Done", timeout=30_000)  # it reads the playing preset back, then checks the device
    expect(dialog(screen)).to_contain_text(re.compile(r"Preset 1: \d+ settings? known.*0 written\."))
    expect(dialog(screen)).to_contain_text("Check device")
    shoot(screen, "p9-chunk13-adopt-done")
    adoption = api._ok(api.http.get(f"{api.dev}/adopt"))
    assert adoption["adopted"] and adoption["adoption"]["mode"] == "trust"


def test_dont_know_yet_writes_nothing(desktop: Page, api: Api):
    visit(desktop, "/?task=adopt&step=2")
    wait_step(desktop, "How was it set up?")
    dialog(desktop).get_by_role("radio", name="With miniDSP's own software, or I don't know").check()
    dialog(desktop).get_by_role("button", name=re.compile("Next")).click()
    wait_step(desktop, "Which presets?")
    dialog(desktop).get_by_role("button", name=re.compile("Next")).click()
    wait_step(desktop, "Preview")
    dialog(desktop).get_by_role("button", name=re.compile("Next")).click()
    wait_step(desktop, "Remember or write?")
    expect(dialog(desktop)).to_contain_text("Nothing is written now.")
    dialog(desktop).get_by_role("button", name="Set up", exact=True).click()
    wait_step(desktop, "Done", timeout=30_000)
    expect(dialog(desktop)).to_contain_text(re.compile(r"Preset 1: 0 settings known, \d+ unknown, 0 written\."))
    assert api._ok(api.http.get(f"{api.dev}/adopt"))["adoption"]["mode"] == "unknown"
