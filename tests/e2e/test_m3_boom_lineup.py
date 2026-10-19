"""Phase 9 PR 12 (Fix a boom and Line up shakers; design §14.2, §14.5) in a real browser.

The state is the walkthroughs' (design 4.1), except that Shakers start with no delay: In 1 feeds Shakers (+4 dB route,
+12 dB output, 20-80 Hz LR4, a 4 dB cut at 40 Hz), Out 3 and Out 4 (muted, Not in use); the Sub's route is off and
it keeps its 40 Hz cut in EQ band 1.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_m2_flow import mark_unused
from tests.e2e.test_m2_shell import SMALL_TARGETS
from tests.e2e.test_m2_summary import CUT, ROUTING
from tests.e2e.test_m3_tasks import dialog, heading, wait_step


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        params = {"confirm": "true", "commit": "true"}
        api._ok(api.http.patch(f"{api.dev}/presets/0", params=params, json=ROUTING))
        api._ok(api.http.patch(f"{api.dev}/presets/0", params=params, json={"outputs": {"0": {"delay_ms": 0}}}))
        for group, (kind, freq) in enumerate((("hp", 20), ("lp", 80))):
            design = {"kind": kind, "family": "lr", "order": 4, "freq": freq}
            body = {"design": design, "bypass": False}
            api._ok(api.http.put(f"{api.dev}/presets/0/outputs/0/crossover/{group}", params=params, json=body))
        api.put_peq_band("outputs", 0, 0, CUT)
        api.put_peq_band("outputs", 1, 0, CUT)
        mark_unused(api, (2, 3))
        api.wait_synced()

    return walkthrough


def sub_band(api: Api, k: int) -> dict:
    return api.desired(0)["outputs"][1]["peq"][k]


def shakers(api: Api) -> dict:
    return api.desired(0)["outputs"][0]


def test_fix_a_boom_on_the_sub_adds_one_band_in_one_request_and_undo_removes_it(screen: Page, api: Api, shoot):
    before = sub_band(api, 1)
    assert before["bypass"]
    visit(screen, "/")
    card = screen.locator("#home-out2")
    card.locator("[data-adjust] summary").click()
    card.get_by_role("link", name="Fix a boom…").click()
    wait_step(screen, "Which output booms?")
    expect(heading(screen)).to_be_focused()
    expect(dialog(screen).get_by_role("radio", name=re.compile(r"^Sub: 1 EQ cut at 40 Hz"))).to_be_checked()
    expect(dialog(screen)).to_contain_text("9 of 10 EQ bands free")
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()

    wait_step(screen, "Which frequency?")
    field = dialog(screen).get_by_role("spinbutton", name="Frequency of the boom")
    expect(field).to_have_value("")  # starts empty (P13)
    field.fill("40")
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()

    wait_step(screen, "How much, and how wide?")
    expect(heading(screen)).to_contain_text("Fix a boom: step 3 of 5")
    expect(dialog(screen).get_by_role("radio", name="Cut")).to_be_checked()
    expect(dialog(screen).get_by_role("spinbutton", name="How many dB?")).to_have_value("4")
    expect(dialog(screen).get_by_role("radio", name=re.compile(r"Medium \(Q 4\): about 35–45 Hz"))).to_be_checked()
    expect(dialog(screen).locator(".eqcut-reading")).to_have_text("Lowers about 35–45 Hz, deepest −4 dB at 40 Hz.")
    expect(dialog(screen).locator(".eqcut-graph")).to_be_visible()
    if screen.screen_name == "phone":  # type: ignore[attr-defined]
        assert dialog(screen).locator(".eqcut-plot").bounding_box()["height"] >= 179  # 180 px tall in wizards
        assert not has_horizontal_scroll(screen)
    shoot(screen, "p9-chunk12-boom-amount")
    # The preview follows the choices without leaving the step: a wider cut, then 6 dB.
    dialog(screen).get_by_role("radio", name=re.compile(r"Wide \(Q 1\.4\)")).check()
    expect(dialog(screen).locator(".eqcut-reading")).to_have_text("Lowers about 28–57 Hz, deepest −4 dB at 40 Hz.")
    dialog(screen).get_by_role("button", name="6 dB").click()
    expect(dialog(screen).locator(".eqcut-reading")).to_have_text("Lowers about 28–57 Hz, deepest −6 dB at 40 Hz.")
    dialog(screen).get_by_role("button", name="1 dB less").click()
    dialog(screen).get_by_role("button", name="1 dB less").click()
    dialog(screen).get_by_role("radio", name=re.compile(r"Medium \(Q 4\)")).check()
    expect(dialog(screen).locator(".eqcut-reading")).to_have_text("Lowers about 35–45 Hz, deepest −4 dB at 40 Hz.")
    assert sub_band(api, 1) == before  # nothing sent yet
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()

    wait_step(screen, "Check")
    expect(dialog(screen)).to_contain_text("Sub: cut 4 dB at 40 Hz, medium width (Q 4).")
    expect(dialog(screen)).to_contain_text("Uses EQ band 2 (free), switched on.")
    expect(dialog(screen)).to_contain_text("Nothing is sent until you press Apply.")
    shoot(screen, "p9-chunk12-boom-check")
    posts: list[str] = []
    screen.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    dialog(screen).get_by_role("button", name="Apply").click()
    wait_step(screen, "Done")
    expect(dialog(screen).locator(".task-msg")).to_contain_text(
        "Sub now has 2 EQ cuts: −4 dB at 40 Hz (new) and −4 dB at 40 Hz."
    )
    assert [u for u in posts if "/peq" in u or "/tasks/" in u or "/edit" in u] == [
        u for u in posts if u.endswith("/ui/tasks/eq-cut")
    ]
    assert len([u for u in posts if u.endswith("/ui/tasks/eq-cut")]) == 1  # one request carries the whole band
    assert wait_until(lambda: not sub_band(api, 1)["bypass"], what="EQ band 2 on")
    band = sub_band(api, 1)
    assert band["design"]["type"] == "peaking" and band["design"]["freq"] == 40
    assert band["design"]["q"] == 4 and band["design"]["gain_db"] == -4
    expect(dialog(screen).get_by_role("link", name="Fine-tune in the EQ table")).to_have_attribute(
        "href", "/outputs/2?tab=peq"
    )
    shoot(screen, "p9-chunk12-boom-done")
    dialog(screen).get_by_role("button", name="Undo").click()
    expect(dialog(screen).locator(".task-msg")).to_contain_text("Undone: Sub · EQ band 2 is back.")
    assert wait_until(lambda: sub_band(api, 1)["bypass"], what="EQ band 2 off again")
    assert sub_band(api, 1) == before
    dialog(screen).get_by_role("button", name="Done").click()
    expect(dialog(screen)).to_be_hidden()


def test_line_up_sets_8_75_ms_for_3_m_fine_tunes_and_undo_all(screen: Page, api: Api, shoot):
    assert shakers(api)["delay_ms"] == 0
    visit(screen, "/outputs/1?tab=delay")
    screen.get_by_role("link", name="Line up shakers with the sub…").click()
    wait_step(screen, "Which two?")
    expect(dialog(screen).get_by_role("combobox", name="Felt in my seat")).to_have_value("1")
    expect(dialog(screen).get_by_role("combobox", name="Heard from across the room")).to_have_value("2")
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()

    wait_step(screen, "How far from your seat?")
    expect(dialog(screen).get_by_role("radio", name="In or under my seat")).to_be_checked()
    sub = dialog(screen).get_by_role("spinbutton", name=re.compile("^Sub"))
    expect(sub).to_have_value("")  # starts empty (P13)
    dialog(screen).get_by_role("radio", name="metres").check()
    sub.fill("3")
    shoot(screen, "p9-chunk12-lineup-distances")
    dialog(screen).get_by_role("button", name=re.compile("Next")).click()

    wait_step(screen, "Check")
    expect(dialog(screen)).to_contain_text("Sound from Sub takes 8.75 ms to reach you.")
    expect(dialog(screen)).to_contain_text(
        "Shakers will wait 8.75 ms (840 samples) so both arrive together. Sub keeps its 0 ms."
    )
    expect(dialog(screen)).to_contain_text("Nothing is sent until you press Apply.")
    assert shakers(api)["delay_ms"] == 0
    shoot(screen, "p9-chunk12-lineup-check")
    dialog(screen).get_by_role("button", name="Apply").click()

    wait_step(screen, "Fine-tune by feel")
    assert wait_until(lambda: shakers(api)["delay_ms"] == 8.75, what="Shakers delay 8.75 ms")
    expect(dialog(screen).locator(".lineup-now")).to_have_text("Shakers waits 8.75 ms")
    expect(dialog(screen)).to_contain_text("These apply now.")
    dialog(screen).get_by_role("button", name="+1 ms").click()
    expect(dialog(screen).locator(".lineup-now")).to_have_text("Shakers waits 9.75 ms")
    assert wait_until(lambda: shakers(api)["delay_ms"] == 9.75, what="Shakers delay 9.75 ms")
    dialog(screen).get_by_role("button", name="Try polarity 180° on Shakers").click()
    expect(dialog(screen).get_by_role("button", name="Back to 0° on Shakers")).to_be_visible()
    assert wait_until(lambda: shakers(api)["invert"], what="Shakers polarity 180°")
    shoot(screen, "p9-chunk12-lineup-tune")
    if screen.screen_name == "phone":  # type: ignore[attr-defined]
        small = screen.evaluate(SMALL_TARGETS)
        assert not small, small
    dialog(screen).get_by_role("button", name=re.compile("Finish")).click()

    wait_step(screen, "Done")
    expect(dialog(screen)).to_contain_text("Shakers waits 9.75 ms, polarity 180°.")
    expect(dialog(screen)).to_contain_text("The Combined response graph doesn't know the distance to your seat")
    shoot(screen, "p9-chunk12-lineup-done")
    dialog(screen).get_by_role("button", name="Undo all").click()
    expect(dialog(screen).locator(".task-msg")).to_contain_text("Undone")
    assert wait_until(lambda: shakers(api)["delay_ms"] == 0 and not shakers(api)["invert"], what="delay and polarity")
    dialog(screen).get_by_role("button", name="Done").click()
    expect(dialog(screen)).to_be_hidden()


def test_the_distance_unit_is_remembered(desktop: Page):
    visit(desktop, "/?task=line-up&step=2&felt=1&heard=2")
    wait_step(desktop, "How far from your seat?")
    dialog(desktop).get_by_role("radio", name="feet").check()
    expect(dialog(desktop).locator(".lineup-heard .unit")).to_have_text("ft")
    desktop.keyboard.press("Escape")
    expect(dialog(desktop)).to_be_hidden()
    visit(desktop, "/?task=line-up&step=2&felt=1&heard=2")
    wait_step(desktop, "How far from your seat?")
    expect(dialog(desktop).get_by_role("radio", name="feet")).to_be_checked()
    dialog(desktop).get_by_role("spinbutton", name=re.compile("^Sub")).fill("10")
    dialog(desktop).get_by_role("button", name=re.compile("Next")).click()
    wait_step(desktop, "Check")
    expect(dialog(desktop)).to_contain_text("Shakers will wait 8.89 ms (853 samples)")
    desktop.evaluate("() => localStorage.removeItem('mdsp.distanceUnit')")


def test_entry_links_on_the_eq_tab_and_the_guided_tasks_page(desktop: Page):
    visit(desktop, "/outputs/2?tab=peq")
    desktop.get_by_role("link", name="+ Add a cut").click()
    wait_step(desktop, "Which output booms?")
    expect(dialog(desktop).get_by_role("radio", name=re.compile(r"^Sub:"))).to_be_checked()
    desktop.keyboard.press("Escape")
    visit(desktop, "/tasks")
    expect(desktop.locator(".task-index").get_by_role("link", name="Fix a boom")).to_have_attribute(
        "href", "/tasks/eq-cut"
    )
    expect(desktop.locator(".task-index").get_by_role("link", name="Line up shakers")).to_have_attribute(
        "href", "/tasks/line-up"
    )


TASK_PAGES = [
    "/tasks/eq-cut",
    "/tasks/eq-cut?step=2&output=2",
    "/tasks/eq-cut?step=3&output=2&freq=40",
    "/tasks/eq-cut?step=4&output=2&freq=40&kind=boost&amount=9",
    "/tasks/line-up",
    "/tasks/line-up?step=2&felt=1&heard=2",
    "/tasks/line-up?step=3&felt=1&heard=2&seat=1&heard_dist=3&unit=m",
    "/tasks/line-up?step=3&felt=1&heard=2&seat=1&heard_dist=30&unit=m",
    "/tasks/line-up?step=4&felt=1&heard=2&seat=1&heard_dist=3&unit=m&target=felt",
]


@pytest.mark.parametrize("path", TASK_PAGES)
def test_task_pages_on_a_phone(phone: Page, path: str):
    visit(phone, path)
    assert not has_horizontal_scroll(phone)
    small = phone.evaluate(SMALL_TARGETS)
    assert not small, f"touch targets under 44 px on {path}: {small}"
    assert_accessible(phone)


@pytest.mark.parametrize("path", ["/?task=eq-cut&step=3&output=2&freq=40", "/?task=line-up&step=2&felt=1&heard=2"])
def test_the_dialogs_are_accessible(screen: Page, path: str):
    visit(screen, path)
    settle(screen)
    expect(dialog(screen)).to_be_visible()
    expect(heading(screen)).to_be_focused()
    assert_accessible(screen, include="#task-dialog")
    if screen.screen_name == "phone":  # type: ignore[attr-defined]
        small = screen.evaluate(SMALL_TARGETS)
        assert not small, small
