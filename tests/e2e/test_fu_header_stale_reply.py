"""A header refresh answered after a write never puts the old value back (static/js/master.js), on the embedded mock.

A refresh of #app-header that went out before a write (Mute all, a Volume step, a preset switch) can be rendered by the
server before the write lands and arrive after the write's own reply has shown the new value: the header then flipped
back to the old value until the next event re-rendered it. Each test holds such a refresh in the browser (Playwright
routing) until the write's reply is on screen, then lets it through and records what every header swap showed.
"""

from playwright.sync_api import Page, Route, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import settle, visit

# What each #app-header swap shows: Mute all's state and the Volume field, one entry per swap.
_RECORD = """() => {
  window.__shown = [];
  document.addEventListener('htmx:afterSwap', (e) => {
    if (!e.detail.target || e.detail.target.id !== 'app-header') return;
    const mute = document.getElementById('master-mute');
    const volume = document.getElementById('master-volume-num');
    window.__shown.push([mute && mute.getAttribute('aria-pressed'), volume && volume.value]);
  });
}"""


def _hold_the_next_header_refresh(page: Page) -> list[tuple[Route, object]]:
    """The next GET of the header is answered by the server at once (so it renders the state from before the write)
    but reaches the page only when the test fulfils it. Other header GETs go through."""
    held: list[tuple[Route, object]] = []

    def hold(route: Route) -> None:
        if not held and route.request.method == "GET":
            held.append((route, route.fetch()))
        else:
            route.continue_()

    page.route(lambda url: "/ui/header" in url, hold)
    return held


def _status(api: Api) -> dict:
    return api.http.get(f"{api.dev}/status").json()["master"]


def _start(page: Page, api: Api) -> list[tuple[Route, object]]:
    api.master(mute=False, volume=-10, confirm=True)
    visit(page, "/")
    page.evaluate(_RECORD)
    held = _hold_the_next_header_refresh(page)
    page.evaluate("() => htmx.trigger('#app-header', 'refresh')")
    wait_until(lambda: held, what="the header refresh to be held", page=page)
    return held


def _release(page: Page, held: list[tuple[Route, object]]) -> list[list[str]]:
    route, response = held[0]
    route.fulfill(response=response)  # type: ignore[arg-type]
    settle(page, 600)
    page.unroute_all(behavior="ignoreErrors")
    return page.evaluate("() => window.__shown")


def test_mute_all_isnt_undone_by_a_refresh_sent_before_it(desktop: Page, api: Api):
    held = _start(desktop, api)
    desktop.locator("#master-mute").click()
    wait_until(lambda: _status(api)["mute"] is True, what="Mute all", page=desktop)
    expect(desktop.locator("#master-mute")).to_have_attribute("aria-pressed", "true")  # the write's reply
    shown = _release(desktop, held)
    first = [s[0] for s in shown].index("true")
    assert all(s[0] == "true" for s in shown[first:]), f"the header went back to the old Mute all state: {shown}"
    expect(desktop.locator("#master-mute")).to_have_attribute("aria-pressed", "true")
    api.master(mute=False)


def test_a_volume_step_isnt_undone_by_a_refresh_sent_before_it(desktop: Page, api: Api):
    held = _start(desktop, api)
    desktop.locator("#volume-up").click()
    wait_until(lambda: _status(api)["volume"] == -9.5, what="the step", page=desktop)
    expect(desktop.locator("#master-volume-num")).to_have_value("-9.5")
    desktop.wait_for_timeout(300)  # past master.js's hold after a Volume request
    shown = _release(desktop, held)
    first = [s[1] for s in shown].index("-9.5")
    assert all(s[1] == "-9.5" for s in shown[first:]), f"the header went back to the old Volume: {shown}"
    expect(desktop.locator("#master-volume-num")).to_have_value("-9.5")
