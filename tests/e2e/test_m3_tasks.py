"""Phase 9 PR 11 (the wizard shell, Something wrong? and Make one louder; design §14.1, §14.3, §14.4) in a real browser.

The state is the walkthroughs' (design 4.1): In 1 feeds Shakers (+4 dB route, +12 dB output: at its level limit),
Out 3 and Out 4 (muted, Not in use); the Sub's route is off, so the Sub is silent.
"""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_m2_home import route_level, seed  # noqa: F401  (seed: the same walkthrough state)
from tests.e2e.test_m2_shell import SMALL_TARGETS


def dialog(page: Page):
    return page.locator("#task-dialog")


def heading(page: Page):
    return dialog(page).locator(".task-heading")


def wait_step(page: Page, title: str) -> None:
    expect(heading(page)).to_contain_text(title)
    settle(page, 150)


def sub_route(api: Api) -> dict:
    return api.desired(0)["inputs"][0]["routing"][1]


def test_something_wrong_finds_the_silent_sub_fixes_it_and_undo_removes_the_route(desktop: Page, api: Api, shoot):
    visit(desktop, "/")
    desktop.locator(".home-health-card").get_by_role("link", name="Something wrong?").click()
    expect(dialog(desktop)).to_be_visible()
    wait_step(desktop, "What's wrong?")
    # Focus moves to the step heading (design §14.1), which names the wizard and the step.
    expect(heading(desktop)).to_be_focused()
    expect(heading(desktop)).to_contain_text("Something wrong?: step 1 of 3")
    # The silent Sub is preselected.
    expect(dialog(desktop).get_by_label("One output is silent:")).to_be_checked()
    expect(dialog(desktop).locator("select[name=output]")).to_have_value("2")
    assert "task=diagnose" in desktop.url

    dialog(desktop).get_by_role("button", name=re.compile("Run the checks")).click()
    wait_step(desktop, "Checks")
    expect(heading(desktop)).to_be_focused()
    failed = dialog(desktop).locator(".task-check.is-fail").first
    expect(failed).to_contain_text("No input is routed to Sub")
    # The first problem is open, with its fix in words.
    expect(failed.locator("details")).to_have_attribute("open", "")
    expect(failed).to_contain_text("Fix: Route In 1 → Sub at 0 dB. Sub will then play full range from In 1.")
    shoot(desktop, "p9-chunk11-diagnose-checks")
    assert_accessible(desktop, include="#task-dialog")

    failed.get_by_role("button", name=re.compile("Apply fix")).click()
    wait_step(desktop, "Checks")
    expect(dialog(desktop).locator(".task-msg")).to_contain_text("Fixed: In 1 → Sub at 0 dB")
    assert wait_until(lambda: sub_route(api)["enabled"], what="the route In 1 → Sub")
    expect(dialog(desktop).locator('[data-check="o1-route"]')).to_contain_text("Sub is fed by In 1")

    dialog(desktop).get_by_role("button", name=re.compile("Finish")).click()
    wait_step(desktop, "Done")
    expect(dialog(desktop)).to_contain_text("Sub was silent because no input was routed. Fixed: In 1 → Sub at 0 dB.")
    shoot(desktop, "p9-chunk11-diagnose-done")
    dialog(desktop).get_by_role("button", name="Undo").click()
    wait_step(desktop, "Checks")
    expect(dialog(desktop).locator(".task-msg")).to_contain_text("Undone")
    assert wait_until(lambda: not sub_route(api)["enabled"], what="the route to be removed again")
    expect(dialog(desktop).locator(".task-check.is-fail").first).to_contain_text("No input is routed to Sub")


def test_esc_and_back_close_the_wizard_and_focus_returns(desktop: Page):
    visit(desktop, "/")
    menu = desktop.locator("#app-header .hdr-helpme")
    menu.get_by_role("button", name=re.compile("Help me")).click()
    menu.get_by_role("link", name="Something wrong?").click()
    wait_step(desktop, "What's wrong?")
    desktop.keyboard.press("Escape")
    expect(dialog(desktop)).to_be_hidden()
    wait_until(lambda: "task=" not in desktop.url, what="the URL to lose ?task", page=desktop)
    expect(menu.get_by_role("button", name=re.compile("Help me"))).to_be_focused()

    menu.get_by_role("button", name=re.compile("Help me")).click()
    menu.get_by_role("link", name="Make one louder").click()
    wait_step(desktop, "Which output?")
    desktop.go_back()  # the browser's Back closes the wizard and stays on Home
    expect(dialog(desktop)).to_be_hidden()
    assert "task=" not in desktop.url
    expect(desktop).to_have_title(re.compile(r"^Home · "))


def test_refresh_on_a_step_resumes_it(desktop: Page):
    # The page fallback (/tasks/<id>?step=n) ...
    visit(desktop, "/tasks/diagnose?step=2")
    expect(desktop.locator(".task-page .task-heading")).to_contain_text("Checks")
    expect(desktop.locator(".task-page")).to_contain_text("Sub is silent")
    desktop.reload()
    settle(desktop)
    expect(desktop.locator(".task-page .task-heading")).to_contain_text("Checks")
    # ... and the dialog: a step's URL (?task=…&step=…) reopens it at that step.
    visit(desktop, "/")
    desktop.locator(".home-health-card").get_by_role("link", name="Something wrong?").click()
    wait_step(desktop, "What's wrong?")
    dialog(desktop).get_by_role("button", name=re.compile("Run the checks")).click()
    wait_step(desktop, "Checks")
    desktop.reload()
    settle(desktop)
    expect(dialog(desktop)).to_be_visible()
    wait_step(desktop, "Checks")
    desktop.keyboard.press("Escape")
    expect(dialog(desktop)).to_be_hidden()


def test_make_louder_at_the_limit_preselects_the_route_and_undo_puts_it_back(screen: Page, api: Api, shoot):
    visit(screen, "/")
    card = screen.locator("#home-out1")
    card.locator("[data-adjust] summary").click()
    card.get_by_role("link", name="Make louder…").click()
    wait_step(screen, "Which output?")
    expect(dialog(screen).get_by_role("radio", name=re.compile(r"Shakers now: \+16\.0 dB"))).to_be_checked()
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()
    wait_step(screen, "How much?")
    dialog(screen).get_by_text("3 dB", exact=True).click()
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()
    wait_step(screen, "How?")
    # The output is at its +12 dB limit, so raising the route is preselected and the output level isn't possible.
    expect(dialog(screen).locator("input[name=method][value=routes]")).to_be_checked()
    expect(dialog(screen).locator("input[name=method][value=output]")).to_be_disabled()
    expect(dialog(screen)).to_contain_text("Raise the route In 1 → Shakers from +4 to +7 dB")
    expect(dialog(screen)).to_contain_text("Not possible now: Shakers' output level is at its +12 dB limit.")
    shoot(screen, "p9-chunk11-louder-how")
    assert not has_horizontal_scroll(screen)
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()
    wait_step(screen, "Check")
    expect(dialog(screen)).to_contain_text("Shakers: +16 → +19 dB total.")
    expect(dialog(screen)).to_contain_text("Peaks above about −19 dBFS on In 1 will overload it.")
    expect(dialog(screen)).to_contain_text("Nothing is sent until you press Apply.")
    assert route_level(api) == 4.0  # nothing sent yet
    shoot(screen, "p9-chunk11-louder-check")
    if screen.screen_name == "phone":  # type: ignore[attr-defined]
        # A full-screen sheet with the Apply bar in reach.
        box = dialog(screen).bounding_box()
        assert box is not None and box["width"] >= 389 and box["height"] >= 843
        apply = dialog(screen).get_by_role("button", name="Apply")
        expect(apply).to_be_in_viewport()
        assert apply.bounding_box()["height"] >= 44
    dialog(screen).get_by_role("button", name="Apply").click()
    wait_step(screen, "Done")
    assert wait_until(lambda: route_level(api) == 7.0, what="the route at +7 dB")
    expect(dialog(screen)).to_contain_text("Shakers is louder: +19.0 dB total (route +7 + output +12).")
    expect(dialog(screen).get_by_role("button", name=re.compile("A bit more"))).to_be_visible()
    dialog(screen).get_by_role("button", name="Undo").click()
    expect(dialog(screen).locator(".task-msg")).to_contain_text("Undone")
    assert wait_until(lambda: route_level(api) == 4.0, what="the route back at +4 dB")
    dialog(screen).get_by_role("button", name="Done").click()
    expect(dialog(screen)).to_be_hidden()


def test_the_guided_tasks_page_lists_every_task(desktop: Page):
    visit(desktop, "/tasks")
    expect(desktop.get_by_role("heading", name="Guided tasks", level=1)).to_be_visible()
    for name in ("Something wrong?", "Make one louder", "Fix a boom", "Line up shakers", "Set levels"):
        expect(desktop.locator(".task-index").get_by_role("link", name=name)).to_be_visible()
    # The sidebar lists it under Everyday.
    expect(desktop.locator("#sidenav").get_by_role("link", name="Guided tasks")).to_be_visible()
    assert_accessible(desktop)


TASK_PAGES = [
    "/tasks",
    "/tasks/diagnose",
    "/tasks/diagnose?step=2&what=one&output=2",
    "/tasks/diagnose?step=2&what=distorted",
    "/tasks/diagnose?step=3&what=one&output=2",
    "/tasks/louder?step=1",
    "/tasks/louder?step=2&output=1",
    "/tasks/louder?step=3&output=1&dir=up&amount=3",
    "/tasks/louder?step=4&output=2&dir=up&amount=9&method=output",
]


@pytest.mark.parametrize("path", TASK_PAGES)
def test_task_pages_on_a_phone(phone: Page, path: str):
    visit(phone, path)
    assert not has_horizontal_scroll(phone)
    small = phone.evaluate(SMALL_TARGETS)
    assert not small, f"touch targets under 44 px on {path}: {small}"
    assert_accessible(phone)


def test_the_dialog_on_a_phone_is_a_sheet_with_44_px_targets(phone: Page, shoot):
    visit(phone, "/?task=louder&step=4&output=2&dir=up&amount=9&method=output")
    wait_step(phone, "Check")
    box = dialog(phone).bounding_box()
    assert box is not None and box["x"] == 0 and box["width"] >= 389 and box["height"] >= 843
    expect(dialog(phone)).to_contain_text("I confirm raising Sub by 9 dB.")
    small = phone.evaluate(SMALL_TARGETS)
    assert not small, small
    assert_accessible(phone, include="#task-dialog")
    shoot(phone, "p9-chunk11-louder-big-raise")


def test_closing_after_a_change_offers_undo_in_a_toast_and_the_diagram_shows_the_targets(desktop: Page, api: Api):
    visit(desktop, "/?task=louder&step=3&output=1&dir=up&amount=1")
    wait_step(desktop, "How?")
    # The diagram behind the dialog highlights the block the chosen way changes (design §5.2 "Wizard target").
    expect(desktop.locator('.flow-block.is-wizard-target[data-flow-key="o0:level"]').first).to_be_attached()
    dialog(desktop).get_by_role("button", name=re.compile("Next")).click()
    wait_step(desktop, "Check")
    dialog(desktop).get_by_role("button", name="Apply").click()
    wait_step(desktop, "Done")
    assert wait_until(lambda: route_level(api) == 5.0, what="the route at +5 dB")
    dialog(desktop).get_by_role("button", name="Done").click()
    expect(dialog(desktop)).to_be_hidden()
    expect(desktop.locator(".is-wizard-target")).to_have_count(0)
    toast = desktop.locator("#toasts .toast").filter(has_text="Shakers is louder")
    toast.get_by_role("button", name="Undo").click()
    assert wait_until(lambda: route_level(api) == 4.0, what="the route back at +4 dB")
    expect(desktop.locator("#toasts")).to_contain_text("Undone")
