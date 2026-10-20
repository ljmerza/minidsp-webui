"""Noisy SSE events are rate-limited without blocking a part's other triggers (static/js/app.js), on the embedded mock.

htmx's "throttle:" throttles the element, not the trigger: for a second after any "master" event (one per Volume step)
Home's "Presets & profiles" box ignored all its triggers, its "refresh" and sse:sync too, and the throttle drops the
last event of a burst, so the box could show the old preset for a second or keep a stale state until the next event.
Each test makes the box's state change right after a "master" event and measures when the box shows it.
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Page

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import visit


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def state(api: Api) -> None:
        api.adopt_factory_defaults()
        api.master(mute=False, volume=-30, confirm=True)

    return state


BOX = ".home-presets-box"

# Records every "master" event (from the Home cards' section, which gets each one) and every swap of the box.
_PROBE = """(box) => {
  const part = document.querySelector(box);
  const probe = window.__probe = { masters: [], swaps: [] };
  document.querySelector('.home-outputs').addEventListener('sse:master', (e) => {
    let data = null;
    try { data = JSON.parse(e.detail.data); } catch {}
    probe.masters.push({ at: performance.now(), master: data && data.master });
  });
  document.addEventListener('htmx:afterSwap', (e) => {
    if (e.detail.target === part) probe.swaps.push({ at: performance.now(), text: part.textContent });
  });
}"""

# Triggers the box's "refresh" and resolves with the ms until it shows `text` (null if it doesn't within 1.5 s).
_REFRESH_SHOWS = """([box, text]) => new Promise((resolve) => {
  const part = document.querySelector(box);
  const start = performance.now();
  const timer = setInterval(() => {
    const elapsed = performance.now() - start;
    if (part.textContent.includes(text)) { clearInterval(timer); resolve(elapsed); }
    else if (elapsed > 1500) { clearInterval(timer); resolve(null); }
  }, 5);
  htmx.trigger(part, 'refresh');
})"""


def _open_home(page: Page) -> None:
    visit(page, "/")
    # Let a second pass after the page's own first events, so the master event a test sends opens a fresh window.
    page.wait_for_timeout(1200)
    page.evaluate(_PROBE, BOX)


def _masters(page: Page) -> list[dict]:
    return page.evaluate("() => window.__probe.masters")


def _swap_showing(page: Page, text: str, timeout: float = 3.0) -> dict | None:
    """The first swap of the box that shows `text`, or None if none did within `timeout` seconds."""
    try:
        wait_until(
            lambda: page.evaluate("(t) => window.__probe.swaps.some((s) => s.text.includes(t))", text),
            timeout=timeout,
            what=text,
            page=page,
        )
    except AssertionError:
        return None
    return page.evaluate("(t) => window.__probe.swaps.find((s) => s.text.includes(t))", text)


def _preset(api: Api) -> int:
    return api.http.get(f"{api.dev}/status").json()["master"]["preset"]


def test_a_refresh_right_after_a_master_event_is_fetched_at_once(desktop: Page, api: Api):
    _open_home(desktop)
    api.master(mute=True)
    wait_until(lambda: _masters(desktop), what="the master event", page=desktop)
    try:
        # Something the box shows changed, and the page asks for it (as after a task's write): inside the second
        # after the master event, which used to swallow it.
        api.create_profile("Refresh probe", api.desired(0))
        elapsed = desktop.evaluate(_REFRESH_SHOWS, [BOX, "Refresh probe"])
        assert elapsed is not None, "the box ignored its refresh after a master event"
        assert elapsed < 300, f"the refresh took {elapsed:.0f} ms"
    finally:
        api.master(mute=False)


def test_a_preset_switch_right_after_a_master_event_shows_at_once(desktop: Page, api: Api):
    _open_home(desktop)
    api.master(mute=True)
    wait_until(lambda: _masters(desktop), what="the master event", page=desktop)
    try:
        api.master(preset=1)
        switched = wait_until(
            lambda: next((m for m in _masters(desktop) if (m["master"] or {}).get("preset") == 1), None),
            what="the master event of the switch",
            page=desktop,
        )
        shown = _swap_showing(desktop, "Device preset 2 is playing")
        assert shown is not None, "the box never showed the new preset"
        assert shown["at"] - switched["at"] < 300, f"the new preset showed {shown['at'] - switched['at']:.0f} ms late"
    finally:
        api.master(preset=0, mute=False)
        wait_until(lambda: _preset(api) == 0, what="preset 1 again")


def test_a_burst_of_master_events_ends_on_the_final_state(desktop: Page, api: Api):
    _open_home(desktop)
    api.master(volume=-31)
    wait_until(lambda: _masters(desktop), what="the first master event", page=desktop)
    # The box's fetch for the first event has landed; what it shows changes, and the burst goes on.
    wait_until(lambda: desktop.evaluate("() => window.__probe.swaps.length"), what="the first re-fetch", page=desktop)
    api.create_profile("Burst probe", api.desired(0))
    for volume in (-32, -33, -34):
        api.master(volume=volume)
    try:
        wait_until(lambda: len(_masters(desktop)) >= 4, what="the burst's master events", page=desktop)
        assert _swap_showing(desktop, "Burst probe") is not None, "the box kept the state from before the burst's end"
    finally:
        api.master(volume=-30)  # back up 4 dB: under the gain-step guard
