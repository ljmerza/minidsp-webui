"""Phase 9 PR 3 (P11 part, P20, P21 part, P22) in a real browser: one profile-load confirm, a safe mono sum, mute
with Undo, and confirms that name channels instead of internal keys."""

from collections.abc import Callable

import pytest
from playwright.sync_api import Dialog, Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import visit


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def state(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        movies = api.desired(0)
        movies["outputs"][0]["gain_db"] = 12.0  # a +12 dB raise for any preset where Shakers is at 0 dB
        api.create_profile("Movies", movies)

    return state


def answer_dialogs(page: Page, accept: bool) -> list[str]:
    """Answers every confirm() on the page and returns their texts as they come."""
    seen: list[str] = []

    def handle(dialog: Dialog) -> None:
        seen.append(dialog.message)
        if accept:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("dialog", handle)
    return seen


def toast(page: Page, text: str):
    return page.locator("#toasts .toast").filter(has_text=text)


def test_loading_a_profile_with_a_big_raise_asks_once_and_keeps_the_target(desktop: Page, api: Api):
    asked = answer_dialogs(desktop, accept=True)
    visit(desktop, "/profiles")
    movies = desktop.locator("[data-profile-name='Movies']")
    movies.get_by_label("Load Movies into").select_option(label="Preset 2")
    movies.get_by_role("button", name="Load", exact=True).click()
    expect(toast(desktop, "Loaded “Movies” into preset 2")).to_be_visible()
    assert len(asked) == 1, asked
    assert "Shakers output level" in asked[0] and "+12.00 dB" in asked[0]
    assert "outputs." not in asked[0]
    expect(desktop.locator("[data-profile-name='Movies']").get_by_label("Load Movies into")).to_have_value("1")
    assert api.desired(1)["outputs"][0]["gain_db"] == 12.0


def test_mono_sum_needs_an_output_then_previews_applies_and_undoes(screen: Page, api: Api):
    visit(screen, "/routing")
    apply = screen.locator("[data-mono-sum]")
    preview = screen.locator("[data-mono-preview]")
    expect(apply).to_be_disabled()
    expect(preview).to_contain_text("Nothing is sent until you press Apply")
    before = api.desired()
    routes_to_sub = [ch["routing"][1] for ch in before["inputs"]]

    sends: list[str] = []
    screen.on("request", lambda r: sends.append(r.url) if r.method == "POST" else None)
    screen.get_by_label("Output", exact=True).select_option(label="Sub")
    expect(apply).to_be_enabled()
    expect(preview).to_contain_text("Sub")
    assert sends == []  # choosing an output only previews

    apply.click()
    done = toast(screen, "Sub now plays every input at")
    expect(done).to_be_visible()
    wait_until(lambda: all(ch["routing"][1]["enabled"] for ch in api.desired()["inputs"]), what="the mono sum")
    done.get_by_role("button", name="Undo").click()
    wait_until(lambda: [ch["routing"][1] for ch in api.desired()["inputs"]] == routes_to_sub, what="the undo")


def test_muting_from_a_home_card_offers_undo(screen: Page, api: Api):
    visit(screen, "/")
    mute = screen.get_by_role("button", name="Mute Shakers")
    expect(mute).to_have_attribute("aria-pressed", "false")
    mute.click()
    muted = toast(screen, "Shakers muted.")
    expect(muted).to_be_visible()
    wait_until(lambda: api.desired()["outputs"][0]["mute"] is True, what="the mute")
    muted.get_by_role("button", name="Undo").click()
    wait_until(lambda: api.desired()["outputs"][0]["mute"] is False, what="the undo")
    expect(screen.get_by_role("button", name="Mute Shakers")).to_have_attribute("aria-pressed", "false")


def test_muting_on_the_output_page_offers_undo(desktop: Page, api: Api):
    visit(desktop, "/outputs/2")
    switch = desktop.get_by_role("switch", name="Mute")
    expect(switch).not_to_be_checked()
    desktop.locator("label.switch", has=switch).click()
    muted = toast(desktop, "Sub muted.")
    expect(muted).to_be_visible()
    wait_until(lambda: api.desired()["outputs"][1]["mute"] is True, what="the mute")
    muted.get_by_role("button", name="Undo").click()
    wait_until(lambda: api.desired()["outputs"][1]["mute"] is False, what="the undo")
    expect(switch).not_to_be_checked()


def test_confirms_name_the_channel_not_an_internal_key(desktop: Page, api: Api):
    asked = answer_dialogs(desktop, accept=False)

    # A big EQ boost (more than 6 dB at once) asks first.
    visit(desktop, "/outputs/2?tab=peq")
    expect(desktop.get_by_label("Band 10 type")).to_be_visible()
    row = desktop.locator(".peq-table tbody").nth(3)
    with desktop.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/outputs/1/peq/3")):
        row.get_by_label("Band 4 type").select_option("peaking")
    row.get_by_role("radio", name="Boost").check()
    field = row.locator("input.mag")
    field.fill("")
    field.press_sequentially("10", delay=40)
    field.press("Tab")
    wait_until(lambda: asked, what="the EQ boost confirm", page=desktop)
    assert "Sub · EQ band 4" in asked[0], asked
    expect(field).not_to_have_value("10")  # declined: put back
    assert api.peq_band("outputs", 1, 3)["design"]["gain_db"] == 0

    # Turning off the last route into an output asks first.
    visit(desktop, "/routing")
    cell = desktop.locator('[data-path="inputs.0.routing.0.enabled"]')
    expect(cell).to_be_checked()
    cell.click()
    wait_until(lambda: len(asked) == 2, what="the route-off confirm", page=desktop)
    assert asked[1].startswith("Turn off In 1 → Shakers?"), asked
    expect(cell).to_be_checked()  # declined: nothing changed

    assert not [text for text in asked if "outputs." in text or "inputs." in text], asked
