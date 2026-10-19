"""Phase 9 PR 10 (signal-flow diagram, design §5) in a real browser.

The state is the walkthroughs' (design 4.1): In 1 feeds Shakers (+4 dB route, +12 dB output, 20-80 Hz LR4, a 4 dB cut
at 40 Hz, 8.75 ms), Out 3 and Out 4 (muted, and marked Not in use in Settings); the Sub's route is off (it keeps its
40 Hz cut); In 2 feeds nothing.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_m2_summary import CUT, ROUTING


def mark_unused(api: Api, outputs: tuple[int, ...]) -> None:
    """Settings → Channels: names kept, the given outputs marked Not in use (the form posts every channel)."""
    names = {("output", 0): "Shakers", ("output", 1): "Sub"}
    form: dict[str, str] = {"channel_flags": "1"}
    for kind, count in (("input", 2), ("output", 4)):
        for idx in range(count):
            form[f"{kind}-{idx}-name"] = names.get((kind, idx), "")
            if kind == "output" and idx in outputs:
                form[f"{kind}-{idx}-unused"] = "on"
    response = api.http.post(f"/ui/devices/{api.serial}/channels", data=form, headers={"HX-Request": "true"})
    assert response.status_code == 200, response.text


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        params = {"confirm": "true", "commit": "true"}
        api._ok(api.http.patch(f"{api.dev}/presets/0", params=params, json=ROUTING))
        for group, (kind, freq) in enumerate((("hp", 20), ("lp", 80))):
            design = {"kind": kind, "family": "lr", "order": 4, "freq": freq}
            body = {"design": design, "bypass": False}
            api._ok(api.http.put(f"{api.dev}/presets/0/outputs/0/crossover/{group}", params=params, json=body))
        api.put_peq_band("outputs", 0, 0, CUT)
        api.put_peq_band("outputs", 1, 0, CUT)
        mark_unused(api, (2, 3))
        api.wait_synced()

    return walkthrough


def flow(page: Page):
    return page.locator("[data-flow]")


def row(page: Page, name: str):
    return page.locator("section.flow-row", has=page.get_by_role("heading", name=re.compile(rf"^{name}\b")))


def test_the_silent_sub_shows_the_red_stub_and_the_fix(desktop: Page, shoot):
    visit(desktop, "/")
    sub = row(desktop, "Sub")
    expect(sub.locator("[data-health-badge]")).to_have_text("⛔ Silent")
    expect(sub.locator(".flow-fed")).to_have_text("Fed by nothing")
    expect(sub.get_by_role("button", name="Route In 1 → Sub")).to_be_visible()
    stub = sub.locator(".flow-stub-btn")
    expect(stub).to_be_visible()
    expect(desktop.locator(".flow-svg-folded .flow-stub-path")).to_have_count(1)
    flow(desktop).scroll_into_view_if_needed()
    shoot(desktop, "p9-chunk10-flow")

    stub.click()
    pop = desktop.get_by_role("dialog", name="Sub has no input")
    expect(pop).to_be_visible()
    expect(pop).to_contain_text("Sub is silent: no input is routed to it.")
    expect(pop.get_by_role("button", name="Route In 1 → Sub")).to_be_visible()
    expect(pop.get_by_role("link", name="Open Routing")).to_have_attribute("href", "/routing")
    shoot(desktop, "p9-chunk10-flow-stub-popover")
    desktop.keyboard.press("Escape")
    expect(pop).to_be_hidden()
    expect(stub).to_be_focused()


def test_the_rows_say_what_feeds_them(desktop: Page):
    visit(desktop, "/")
    shakers = row(desktop, "Shakers")
    expect(shakers.locator(".flow-fed")).to_have_text("Fed by In 1 at +4 dB")
    expect(shakers.get_by_role("link", name="In 1 at +4 dB")).to_have_attribute("href", "/routing#in1-out1")
    in2 = desktop.locator("section.flow-in", has=desktop.get_by_role("heading", name="In 2"))
    expect(in2.get_by_role("button", name=re.compile("Not sent to any output"))).to_be_visible()
    # One line per enabled route that isn't folded (In 1 → Shakers), one shared line into the folded Out 3 · Out 4.
    expect(desktop.locator(".flow-svg-folded .flow-line")).to_have_count(2)
    expect(desktop.locator(".flow-svg-folded .flow-line-label").first).to_have_text("+4 dB")
    # A route link opens Routing with that cell focused.
    shakers.get_by_role("link", name="In 1 at +4 dB").click()
    desktop.wait_for_url(re.compile(r"/routing#in1-out1$"))
    settle(desktop)
    expect(desktop.locator("#in1-out1")).to_be_focused()


def test_outputs_marked_not_in_use_fold_into_one_line(screen: Page, shoot):
    visit(screen, "/")
    if screen.screen_name == "phone":  # type: ignore[attr-defined]
        screen.get_by_role("button", name="Details").click()
    fold = screen.locator(".flow-fold")
    expect(fold).to_contain_text("Out 3 · Out 4")
    expect(fold).to_contain_text("Not in use")
    expect(row(screen, "Out 3")).to_be_hidden()
    expect(row(screen, "Out 4")).to_be_hidden()
    show = fold.get_by_role("button", name=re.compile("^Show"))
    expect(show).to_have_attribute("aria-expanded", "false")
    show.click()
    expect(row(screen, "Out 3")).to_be_visible()
    expect(row(screen, "Out 4")).to_be_visible()
    expect(fold.get_by_role("button", name=re.compile("^Hide"))).to_have_attribute("aria-expanded", "true")
    expect(screen.locator(".flow-svg-open")).to_be_visible()
    expect(screen.locator(".flow-svg-folded")).to_be_hidden()
    expect(screen.locator(".flow-svg-open .flow-line")).to_have_count(3)  # In 1 → Shakers, Out 3, Out 4
    assert not has_horizontal_scroll(screen)
    fold.scroll_into_view_if_needed()
    shoot(screen, "p9-chunk10-flow-unused-shown")


EXPECTED_BLOCKS = [
    ("level", "Level +16 ▲max", "#home-out1"),  # on Home the Level block goes to the output's card (design §5.4)
    ("band", "Band 20–80 Hz", "/outputs/1?tab=crossover"),
    ("eq", "EQ 1 cut", "/outputs/1?tab=peq"),
    ("delay", "Delay 8.75", "/outputs/1?tab=delay"),
    ("polarity", "Polarity 0°", "/outputs/1#invert-0"),
    ("compressor", "Comp. off", "/outputs/1?tab=compressor"),
]


def test_every_block_opens_the_right_tab(desktop: Page):
    visit(desktop, "/")
    blocks = row(desktop, "Shakers").locator("[data-flow-block]")
    expect(blocks).to_have_count(len(EXPECTED_BLOCKS))
    for n, (key, label, href) in enumerate(EXPECTED_BLOCKS):
        block = blocks.nth(n)
        expect(block).to_have_attribute("data-block", key)
        expect(block).to_contain_text(label)
        expect(block).to_have_attribute("href", href)
    expect(blocks.nth(1)).to_have_accessible_name("Band, 20 to 80 hertz, on. Open band settings.")
    expect(blocks.nth(4)).to_have_class(re.compile(r"\bis-default\b"))
    expect(blocks.nth(0)).to_have_class(re.compile(r"\bis-caution\b"))

    tabs = {"band": "Band (crossover)", "eq": "EQ", "delay": "Delay", "compressor": "Compressor (limiter)"}
    for key, tab in tabs.items():
        visit(desktop, "/")
        row(desktop, "Shakers").locator(f'[data-block="{key}"]').click()
        desktop.wait_for_url(re.compile(r"/outputs/1\?tab="))
        settle(desktop)
        expect(desktop.locator(".tabs .tab.is-current")).to_have_text(tab)
    visit(desktop, "/")
    row(desktop, "Shakers").locator('[data-block="polarity"]').click()
    desktop.wait_for_url(re.compile(r"/outputs/1#invert-0$"))
    expect(desktop.locator("#invert-0")).to_be_attached()


def test_arrow_keys_move_between_the_blocks_of_a_row(desktop: Page):
    visit(desktop, "/")
    blocks = row(desktop, "Shakers").locator("[data-flow-block]")
    first = blocks.nth(0)
    first.focus()
    expect(first).to_have_attribute("tabindex", "0")
    desktop.keyboard.press("ArrowRight")
    expect(blocks.nth(1)).to_be_focused()
    expect(blocks.nth(1)).to_have_attribute("tabindex", "0")
    expect(first).to_have_attribute("tabindex", "-1")
    desktop.keyboard.press("End")
    expect(blocks.nth(5)).to_be_focused()
    desktop.keyboard.press("ArrowRight")  # wraps
    expect(first).to_be_focused()
    desktop.keyboard.press("ArrowLeft")
    expect(blocks.nth(5)).to_be_focused()
    desktop.keyboard.press("Home")
    expect(first).to_be_focused()
    # One tab stop per row: Tab leaves the blocks for the next row.
    desktop.keyboard.press("Tab")
    focused = desktop.evaluate("() => document.activeElement.dataset.flowKey || ''")
    assert focused.startswith("o1:"), focused
    assert row(desktop, "Sub").locator("[data-flow-block][tabindex='0']").count() == 1


def test_the_diagram_is_accessible_and_says_each_route_once(screen: Page):
    visit(screen, "/")
    if screen.screen_name == "phone":  # type: ignore[attr-defined]
        assert_accessible(screen, include=".flow-card")
        screen.get_by_role("button", name="Details").click()
    assert_accessible(screen, include=".flow-card")
    for svg in screen.locator(".flow-svg").all():
        expect(svg).to_have_attribute("aria-hidden", "true")
    tree = screen.locator(".flow-card").aria_snapshot()
    assert tree.count("+4 dB") == 1, tree  # in "Fed by In 1 at +4 dB", not again from the line's label
    meter = flow(screen).get_by_role("meter", name="Shakers level")  # Home's cards and strip have their own
    expect(meter).to_have_attribute("aria-valuetext", re.compile(r"dBFS$|^no signal$"))


def test_a_clip_is_announced_once(desktop: Page):
    visit(desktop, "/")
    desktop.evaluate("() => { Alpine.$data(document.querySelector('[data-home]')).clip.o0 = true; }")
    status = desktop.locator(".flow-card [role=status]")
    expect(status).to_have_text("Shakers clipped")
    chip = row(desktop, "Shakers").get_by_role("button", name="Shakers clipped, press to reset")
    expect(row(desktop, "Shakers").locator('[data-block="level"]')).to_have_class(re.compile(r"\bis-clipped\b"))
    chip.click()
    expect(row(desktop, "Shakers").get_by_role("button", name="Shakers not clipping")).to_be_visible()


def test_the_phone_shows_a_compact_list_and_the_full_diagram_on_request(phone: Page, shoot):
    visit(phone, "/")
    lst = phone.locator(".flow-list")
    expect(lst).to_be_visible()
    expect(phone.locator(".flow-scroll")).to_be_hidden()
    expect(lst).to_contain_text("Shakers")
    expect(lst.locator(".flow-list-unrouted")).to_contain_text("Sub")
    expect(lst.get_by_role("button", name="Route In 1 → Sub")).to_be_visible()
    expect(lst).to_contain_text("Not sent anywhere")
    assert not has_horizontal_scroll(phone)
    phone.locator(".flow-card").scroll_into_view_if_needed()
    shoot(phone, "p9-chunk10-flow-list")

    details = phone.get_by_role("button", name="Details")
    details.click()
    expect(phone.locator(".flow-scroll")).to_be_visible()
    expect(lst).to_be_hidden()
    assert not has_horizontal_scroll(phone)
    scroll = phone.evaluate(
        "() => { const b = document.querySelector('.flow-scroll'); return b.scrollWidth > b.clientWidth; }"
    )
    assert scroll  # the diagram scrolls inside its own box
    shoot(phone, "p9-chunk10-flow-details")


def test_the_inline_fix_routes_in_1_to_the_sub_and_undo_puts_it_back(desktop: Page, api: Api):
    visit(desktop, "/")
    desktop.on("dialog", lambda d: d.accept())  # turning the last route off again asks first (it has signal)
    row(desktop, "Sub").get_by_role("button", name="Route In 1 → Sub").click()
    wait_until(lambda: api.desired(0)["inputs"][0]["routing"][1]["enabled"], what="the route to be on")
    expect(row(desktop, "Sub").locator(".flow-fed")).to_have_text("Fed by In 1 at 0 dB")
    expect(row(desktop, "Sub").locator(".flow-stub-btn")).to_have_count(0)
    toast = desktop.locator(".toast", has_text="In 1 now plays on Sub.")
    expect(toast).to_be_visible()
    toast.get_by_role("button", name="Undo").click()
    wait_until(
        lambda: not api.desired(0)["inputs"][0]["routing"][1]["enabled"], what="the route to be off", page=desktop
    )
    expect(row(desktop, "Sub").locator(".flow-fed")).to_have_text("Fed by nothing")
    api.wait_synced()
