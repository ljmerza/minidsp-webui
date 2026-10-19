"""Phase 9 PR 17 (QA): axe and a keyboard-only walk on every page, in the state the real setup is in.

The app is seeded like the real device: Out 1 "Shakers" plays 20-80 Hz (Linkwitz-Riley 24 dB/oct high-pass at 20 Hz
and low-pass at 80 Hz) and waits 9 ms, fed by In 1 through a +3 dB route; Out 2 "Sub" plays full range from In 1;
In 2 feeds nothing; Out 3 and Out 4 are muted and marked Not in use. The mock's meters run the ``hot`` pattern, so
the loud outputs latch their CLIP lights and every page is checked with its lit (red) states too.

The harness (test_harness.py) runs axe on every page in the plain walkthrough state; this module adds the populated
state, the keyboard walk and the phone's no-horizontal-scroll rule on every page.
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import has_horizontal_scroll, settle, visit
from tests.e2e.test_harness import PAGES

ROUTING = {
    "inputs": {
        "0": {
            "routing": {
                "0": {"enabled": True, "gain_db": 3},
                "1": {"enabled": True, "gain_db": 0},
                "2": {"enabled": False},
                "3": {"enabled": False},
            }
        },
        "1": {"routing": {str(o): {"enabled": False} for o in range(4)}},
    },
    "outputs": {"0": {"delay_ms": 9}, "2": {"mute": True}, "3": {"mute": True}},
}


def seed_real_setup(api: Api) -> None:
    """The real setup on the mock (names, band, delay, routes, unused outputs); shared with the README screenshot."""
    api.adopt_factory_defaults()
    params = {"confirm": "true", "commit": "true"}
    api._ok(api.http.patch(f"{api.dev}/presets/0", params=params, json=ROUTING))
    for group, (kind, freq) in enumerate((("hp", 20), ("lp", 80))):
        body = {"design": {"kind": kind, "family": "lr", "order": 4, "freq": freq}, "bypass": False}
        api._ok(api.http.put(f"{api.dev}/presets/0/outputs/0/crossover/{group}", params=params, json=body))
    # Settings → Channels posts every channel: names, and Out 3/4 marked Not in use.
    form = {"channel_flags": "1", "output-0-name": "Shakers", "output-1-name": "Sub"}
    for kind, count in (("input", 2), ("output", 4)):
        for idx in range(count):
            form.setdefault(f"{kind}-{idx}-name", "")
    form["output-2-unused"] = form["output-3-unused"] = "on"
    response = api.http.post(f"/ui/devices/{api.serial}/channels", data=form, headers={"HX-Request": "true"})
    assert response.status_code == 200, response.text
    api.wait_synced()


@pytest.fixture(scope="module")
def app_settings() -> dict[str, str]:
    return {"MINIDSPD_MOCK_LEVELS": "hot"}


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    return seed_real_setup


def test_the_seed_reads_like_the_real_setup(api: Api):
    data = api._ok(api.http.get(f"{api.dev}/presets/0/summary"))
    by_name = {o["name"]: o for o in data["outputs"]}
    assert by_name["Shakers"]["sentence"].startswith("Plays 20–80 Hz (steep), fed by In 1")
    assert "9 ms" in by_name["Shakers"]["sentence"]
    assert by_name["Sub"]["sentence"] == "Plays full range, unprocessed, fed by In 1."
    assert [o["badge"] for o in data["outputs"]][2:] == ["not_in_use", "not_in_use"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_is_accessible_in_the_real_setup(screen: Page, path: str):
    visit(screen, path)
    assert_accessible(screen)


def test_lit_clip_lights_are_accessible(desktop: Page):
    """The hot pattern latches CLIP on the loud channels; the red lamps keep their text readable."""
    visit(desktop, "/meters")
    expect(desktop.locator(".clip-led.is-on").first).to_be_visible(timeout=15_000)
    assert_accessible(desktop, include=".meters-card")
    visit(desktop, "/")
    expect(desktop.locator(".home-clip.is-on").first).to_be_visible(timeout=15_000)
    assert_accessible(desktop)


@pytest.mark.parametrize("path", ["/", "/meters", "/outputs/3?tab=level"])
def test_muted_states_are_accessible(screen: Page, api: Api, path: str):
    """Red "muted" controls (the header's Mute all, a muted output's switch and buttons) keep readable text."""
    api.master(mute=True)
    try:
        visit(screen, path)
        expect(screen.locator(".hdr-mute.is-muted").first).to_be_attached()
        assert_accessible(screen)
    finally:
        api.master(mute=False)


@pytest.mark.parametrize("path", PAGES)
def test_phone_pages_never_scroll_sideways(phone: Page, path: str):
    visit(phone, path)
    assert not has_horizontal_scroll(phone), path


# The keyboard walk: Tab through the start of each page; every stop must be a visible control with a
# visible focus ring (its own outline or box-shadow, or one drawn by its label, wrapper or glyph).
FOCUS_STOP = """() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const box = el.getBoundingClientRect();
  const ringed = (node) => {
    if (!node || node === document.body) return false;
    const s = getComputedStyle(node);
    return (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) >= 1) || s.boxShadow !== 'none';
  };
  const ring = ringed(el) || ringed(el.closest('label')) || ringed(el.parentElement)
    || ringed(el.parentElement && el.parentElement.parentElement) || [...el.children].some(ringed);
  const name = (el.getAttribute('aria-label') || el.textContent || el.id || el.name || '').trim().slice(0, 40);
  return {tag: el.tagName.toLowerCase(), name, w: box.width, h: box.height, ring};
}"""

WALK_PAGES = ["/", "/outputs/1", "/routing", "/device", "/profiles", "/tasks/eq-cut"]


@pytest.mark.parametrize("path", WALK_PAGES)
def test_a_keyboard_walk_reaches_visible_controls_with_a_focus_ring(desktop: Page, path: str):
    visit(desktop, path)
    desktop.locator("body").focus()
    first = None
    problems = []
    for _ in range(40):
        desktop.keyboard.press("Tab")
        stop = desktop.evaluate(FOCUS_STOP)
        if stop is None:
            continue
        first = first or stop
        if stop["w"] < 1 or stop["h"] < 1:
            problems.append(f"invisible stop: {stop}")
        elif not stop["ring"]:
            problems.append(f"no focus ring: {stop}")
    assert first is not None and first["name"] == "Skip to content", first
    assert not problems, "\n".join(problems)


def test_the_skip_link_moves_focus_past_the_header(desktop: Page):
    visit(desktop, "/")
    desktop.keyboard.press("Tab")
    expect(desktop.locator(".skip-link")).to_be_focused()
    desktop.keyboard.press("Enter")
    settle(desktop, 100)
    desktop.keyboard.press("Tab")
    inside_main = desktop.evaluate("() => !!document.activeElement.closest('#main')")
    assert inside_main
