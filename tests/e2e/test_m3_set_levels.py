"""Phase 9 PR 13: Set levels (design §14.6) in a real browser, driven by the embedded mock's level pattern.

With ``MINIDSPD_MOCK_LEVELS=hot`` In 1 carries a programme around −6 dBFS (±4 dB) and each output follows the routing,
so Shakers (+4 dB route, +12 dB output: +16 dB total) reaches CLIP. The watch is shortened with ``secs`` (default 30 s).
"""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_m2_home import seed  # noqa: F401  (the walkthrough state)
from tests.e2e.test_m2_shell import SMALL_TARGETS


@pytest.fixture(scope="module")
def app_settings() -> dict[str, str]:
    return {"MINIDSPD_MOCK_LEVELS": "hot"}


def dialog(page: Page):
    return page.locator("#task-dialog")


def wait_step(page: Page, title: str, timeout: float = 5000) -> None:
    expect(dialog(page).locator(".task-heading")).to_contain_text(title, timeout=timeout)
    settle(page, 150)


def shakers_level(api: Api) -> float:
    return api.desired(0)["outputs"][0]["gain_db"]


def phone_checks(page: Page) -> None:
    assert not has_horizontal_scroll(page)
    if page.screen_name == "phone":  # type: ignore[attr-defined]
        small = page.evaluate(SMALL_TARGETS)
        assert not small, small
    assert_accessible(page, include="#task-dialog")


def test_set_levels_sees_the_hot_input_and_the_clipping_shakers_and_undo_all(screen: Page, api: Api, shoot):
    api.master(volume=-20.0)
    visit(screen, "/?task=set-levels&secs=4")
    wait_step(screen, "Start quiet")
    expect(dialog(screen).locator(".task-heading")).to_be_focused()
    dialog(screen).get_by_role("button", name=re.compile(r"Turn the volume down from −20 dB to −40 dB")).click()
    wait_step(screen, "Start quiet")
    expect(dialog(screen)).to_contain_text("quiet enough to start")
    assert wait_until(lambda: api._ok(api.http.get(f"{api.dev}/status"))["master"]["volume"] == -40.0)
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()

    wait_step(screen, "Play something you know")
    expect(dialog(screen).locator(".task-wait")).to_contain_text("Signal found", timeout=15_000)
    expect(dialog(screen).locator(".lv-peak").first).to_contain_text("dBFS")
    screen.wait_for_timeout(1500)  # let a few frames in
    shoot(screen, "p9-chunk13-levels-play")
    phone_checks(screen)
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()

    wait_step(screen, "Check the inputs")
    expect(dialog(screen).locator('[data-reading="i0"]')).to_contain_text(re.compile(r"In 1 (peaks at|hits CLIP)"))
    expect(dialog(screen).locator('[data-reading="i1"]')).to_contain_text("In 2: no signal.")
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()

    wait_step(screen, "Watch the outputs")
    expect(dialog(screen)).to_contain_text("s left")
    # After the watch the results come by themselves.
    expect(dialog(screen).locator('[data-reading="o0"]')).to_contain_text("Shakers clipped", timeout=15_000)
    settle(screen, 150)
    expect(dialog(screen).locator('[data-reading="o1"]')).to_contain_text("Sub: no signal.")
    shoot(screen, "p9-chunk13-levels-results")
    phone_checks(screen)
    dialog(screen).locator('[data-reading="o0"]').get_by_role("button", name="Lower Shakers by 3 dB").click()
    wait_step(screen, "Watch the outputs")
    expect(dialog(screen).locator(".task-msg").first).to_contain_text("Done: Shakers output level +12 dB → +9 dB.")
    assert wait_until(lambda: shakers_level(api) == 9.0, what="Shakers at +9 dB")
    expect(dialog(screen).locator(".task-changes-box")).to_contain_text("Volume −20 dB → −40 dB")
    dialog(screen).get_by_role("button", name="Undo all").click()
    expect(dialog(screen).locator(".task-msg").first).to_contain_text("Undone")
    assert wait_until(lambda: shakers_level(api) == 12.0, what="Shakers back at +12 dB")
    assert wait_until(lambda: api._ok(api.http.get(f"{api.dev}/status"))["master"]["volume"] == -20.0)
    screen.keyboard.press("Escape")
    expect(dialog(screen)).to_be_hidden()


def test_everyday_volume_and_done(desktop: Page, api: Api, shoot):
    api.master(volume=-30.0)
    visit(desktop, "/?task=set-levels&step=6")
    wait_step(desktop, "Everyday volume")
    expect(desktop.locator(".lv-volume")).to_contain_text("−30 dB")
    dialog(desktop).get_by_role("button", name="+3 dB Volume").click()
    wait_step(desktop, "Everyday volume")
    expect(desktop.locator(".lv-volume")).to_contain_text("−27 dB")
    dialog(desktop).get_by_role("button", name="Remember this as my usual level").click()
    wait_step(desktop, "Everyday volume")
    expect(dialog(desktop)).to_contain_text("Remembered −27 dB as your usual level.")
    shoot(desktop, "p9-chunk13-levels-volume")
    dialog(desktop).get_by_role("button", name=re.compile("Next")).click()
    wait_step(desktop, "Done")
    expect(dialog(desktop)).to_contain_text("Usual volume −27 dB.")
    expect(dialog(desktop).get_by_role("link", name=re.compile("Save as a profile"))).to_be_visible()
    shoot(desktop, "p9-chunk13-levels-done")
    api.master(volume=-30.0)
