"""Phase 9 PR 15 (graphs, P09) in a real browser: Meters & graph, Combined response and the output Response tab get a
legend, rounding notes folded with the stronger wording at 1 dB or more, a cursor placed by tap, click or keyboard with
a fixed readout box, a one-line reading, a "Show the sum" toggle that starts off for Shakers (tactile), and the meter
scale labelled evenly.

The state is the walkthroughs' (Shakers 20-80 Hz with a 40 Hz cut and fed by In 1; the Sub unrouted with its own cut),
plus a 5 Hz low shelf on Shakers whose rounding moves its curve by more than 1 dB, and Shakers marked tactile.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, visit
from tests.e2e.test_m2_summary import CUT, ROUTING

# A low shelf this low and deep is where float32 rounding shows most (about 3 dB here).
SHELF = {"design": {"type": "lowshelf", "freq": 5, "q": 2, "gain_db": -12}, "bypass": False}
READING = (
    "Shakers: 20–80 Hz (steep), 1 EQ cut at 40 Hz and bass shelf −12 dB below 5 Hz. "
    "Sub: silent here (In 1 isn't routed to it)."
)


def mark_tactile(api: Api, outputs: tuple[int, ...]) -> None:
    """Settings → Channels: names kept, the given outputs marked Shakers (tactile)."""
    names = {("output", 0): "Shakers", ("output", 1): "Sub"}
    form: dict[str, str] = {"channel_flags": "1"}
    for kind, count in (("input", 2), ("output", 4)):
        for idx in range(count):
            form[f"{kind}-{idx}-name"] = names.get((kind, idx), "")
            if kind == "output" and idx in outputs:
                form[f"{kind}-{idx}-tactile"] = "on"
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
        api.put_peq_band("outputs", 0, 1, SHELF)
        api.put_peq_band("outputs", 1, 0, CUT)
        mark_tactile(api, (0,))
        api.wait_synced()

    return walkthrough


def _graph(page: Page, card: str):
    box = page.locator(f"{card} .graph-box").first
    expect(box.locator(".graph-legend li").first).to_be_visible()
    return box


def _legend(box) -> list[str]:
    return box.locator(".graph-legend-label").all_inner_texts()


def test_meters_graph_legend_reading_and_hidden_sum(screen: Page, shoot):
    visit(screen, "/meters")
    card = screen.locator(".response-card")
    box = _graph(screen, ".response-card")
    expect(card.locator(".graph-reading")).to_have_text(READING)
    # Shakers is tactile: the sum starts hidden, and the legend follows the toggle.
    toggle = card.get_by_label("Show the sum")
    expect(toggle).not_to_be_checked()
    assert _legend(box) == ["Shakers", "Sub"]
    expect(card.get_by_text("The sum adds outputs as if they played from one spot.")).to_be_visible()
    toggle.check()
    expect(box.locator(".graph-legend-label")).to_have_text(["Shakers", "Sub", "Sum"])
    shoot(screen, "c15-meters-graph")
    toggle.uncheck()  # remembered per browser; leave it as the default for the next test
    expect(box.locator(".graph-legend-label")).to_have_text(["Shakers", "Sub"])


def test_rounding_notes_fold_with_the_strong_wording(desktop: Page, shoot):
    visit(desktop, "/meters")
    notes = desktop.locator(".response-card .graph-rounding")
    expect(notes).to_be_visible()
    expect(notes).to_have_class("graph-rounding is-strong")
    summary = notes.locator("summary")
    expect(summary).to_contain_text("rounding notes")
    expect(summary).to_contain_text("real curves are noticeably off")
    detail = notes.locator("li").first
    expect(detail).to_be_hidden()  # folded
    summary.click()
    expect(notes.locator("li", has_text="noticeably off").first).to_be_visible()
    # The real warning (Sub silent here) stays in view, outside the fold.
    expect(desktop.locator(".response-card .graph-warnings li", has_text="not routed to Sub")).to_be_visible()
    expect(notes.locator(".tt-btn")).to_have_attribute("aria-label", "What is Rounding note?")
    shoot(desktop, "c15-rounding-notes")


def test_phone_tap_shows_the_value_in_the_readout_box(phone: Page, shoot):
    visit(phone, "/meters")
    box = _graph(phone, ".response-card")
    readout = box.locator(".graph-readout")
    expect(readout).to_have_text("Tap or click the graph for values")
    canvas = box.locator(".graph-canvas")
    canvas.scroll_into_view_if_needed()
    area = box.locator(".u-over").bounding_box()
    assert area
    phone.touchscreen.tap(area["x"] + area["width"] * 0.5, area["y"] + area["height"] * 0.4)
    expect(readout.locator(".graph-readout-freq")).to_contain_text("Hz")
    expect(readout).to_contain_text("Shakers")
    expect(readout).to_contain_text(" dB")
    # A fixed box under the plot, not a floating tooltip; it stays after the tap.
    plot = canvas.bounding_box()
    shown = readout.bounding_box()
    assert plot and shown and shown["y"] >= plot["y"] + plot["height"] - 1
    phone.wait_for_timeout(400)
    expect(readout.locator(".graph-readout-freq")).to_contain_text("Hz")
    assert not has_horizontal_scroll(phone)
    shoot(phone, "c15-tap-readout")


def test_keyboard_moves_the_cursor_and_the_reading_is_spoken(desktop: Page):
    visit(desktop, "/meters")
    box = _graph(desktop, ".response-card")
    slider = box.get_by_role("slider")
    expect(slider).to_have_attribute("aria-valuetext", "no cursor placed")
    slider.focus()
    desktop.keyboard.press("Home")
    expect(slider).to_have_attribute("aria-valuetext", re.compile(r"^5\.0 Hz: Shakers −\d+\.\d dB, Sub −200\.0 dB$"))
    expect(slider).to_have_attribute("aria-valuenow", "5")
    desktop.keyboard.press("PageUp")  # half an octave up, then about 1/12 octave more: about 7.5 Hz
    desktop.keyboard.press("ArrowRight")
    expect(slider).to_have_attribute("aria-valuetext", re.compile(r"^7\.\d Hz: Shakers −\d+\.\d dB, Sub −200\.0 dB$"))
    expect(slider).to_have_attribute("aria-valuenow", re.compile(r"^[78]$"))
    expect(box.locator(".graph-readout-freq")).to_have_text(re.compile(r"^7\.\d Hz$"))
    assert desktop.evaluate("() => document.activeElement.getAttribute('role')") == "slider"
    desktop.keyboard.press("Escape")
    expect(slider).to_have_attribute("aria-valuetext", "no cursor placed")
    expect(box.locator(".graph-readout")).to_have_text("Tap or click the graph for values")


def test_meter_scale_labels_are_evenly_spaced(desktop: Page, shoot):
    visit(desktop, "/meters")
    labels = desktop.locator(".meter-scale span")
    expect(labels).to_have_text(["0", "−6", "−12", "−20", "−30", "−40", "−60", "−120"])
    ys = [b["y"] for b in (labels.nth(k).bounding_box() for k in range(8)) if b]
    gaps = [b - a for a, b in zip(ys, ys[1:], strict=False)]
    assert len(gaps) == 7 and max(gaps) - min(gaps) < 1.5, gaps
    shoot(desktop, "c15-meter-scale")


def test_combined_response_reading_sum_and_notes(screen: Page, shoot):
    visit(screen, "/system")
    expect(screen.locator("h1")).to_have_text("Combined response")
    card = screen.locator(".system-view section").first
    box = _graph(screen, ".system-view section")
    expect(card.locator(".graph-reading")).to_contain_text("Shakers: 20–80 Hz (steep)")
    toggle = card.get_by_label("Show the sum")
    expect(toggle).not_to_be_checked()
    assert "Sum" not in _legend(box)
    expect(card.locator(".graph-rounding summary")).to_contain_text("noticeably off")
    shoot(screen, "c15-combined-response")
    assert_accessible(screen, include="#main")


def test_response_tab_has_legend_reading_and_cursor(desktop: Page, shoot):
    visit(desktop, "/outputs/1?tab=response")
    view = desktop.locator(".response-view")
    box = _graph(desktop, ".response-view")
    expect(view.locator(".graph-reading")).to_have_text(
        "Shakers: 20–80 Hz (steep), 1 EQ cut at 40 Hz and bass shelf −12 dB below 5 Hz."
    )
    assert _legend(box)[0] == "Shakers (total)"
    area = box.locator(".u-over").bounding_box()
    assert area
    desktop.mouse.click(area["x"] + area["width"] * 0.6, area["y"] + area["height"] * 0.5)
    desktop.mouse.move(5, 5)  # the pointer leaves; the placed cursor keeps the readout
    expect(box.locator(".graph-readout-freq")).to_contain_text("Hz")
    shoot(desktop, "c15-response-tab")


def test_meters_graph_card_is_accessible(screen: Page):
    # Scoped to the graph card: the dimmed CLIP labels of the Levels card fail colour contrast on their own (a
    # pre-existing issue outside this PR, left for the QA pass).
    visit(screen, "/meters")
    _graph(screen, ".response-card")
    screen.locator(".response-card .graph-rounding summary").click()
    assert_accessible(screen, include=".response-card")
