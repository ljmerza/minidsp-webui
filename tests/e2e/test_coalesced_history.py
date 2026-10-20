"""A burst of − presses on a Home card is one history entry, and the pages have a favicon (no /favicon.ico 404)."""

from playwright.sync_api import Page

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import visit

PRESSES = 6


def history(api: Api) -> list[dict]:
    """Note: reading the history records a held burst at once (so the timeline always shows it)."""
    return api._ok(api.http.get(f"{api.dev}/presets/0/history"))["revisions"]


def test_a_burst_of_presses_is_one_revision(desktop: Page, api: Api, shoot):
    visit(desktop, "/")
    start = api.preset()
    assert not start["uncommitted"]
    old = api.desired(0)["outputs"][0]["gain_db"]
    new = old - 0.5 * PRESSES
    minus = desktop.locator("#home-out1").get_by_role("button", name="Shakers: lower the output level by 0.5 dB")
    for _ in range(PRESSES):
        minus.click()
    wait_until(lambda: api.desired(0)["outputs"][0]["gain_db"] == new, what="the presses to reach the server")
    api.wait_synced()  # the device writes went out without waiting for the revision

    # The revision is written once the presses have stopped for COMMIT_COALESCE_S (2 s), by the app on its own.
    def recorded() -> dict | None:
        view = api.preset()
        return view if not view["uncommitted"] and view["revision"] > start["revision"] else None

    view = wait_until(recorded, what="the burst's revision")
    assert view["revision"] == start["revision"] + 1
    [latest, *_] = history(api)
    assert latest["revision"] == view["revision"]
    assert latest["summary"] == f"outputs.0.gain_db: {old:g} → {new:g}" and latest["source"] == "ui"

    # Put the level back for the module's other tests.
    api._ok(
        api.http.patch(f"{api.dev}/presets/0", params={"commit": "true"}, json={"outputs": {"0": {"gain_db": old}}})
    )
    api.wait_synced()


def test_pages_have_a_favicon_and_favicon_ico_answers(screen: Page, shoot):
    statuses: dict[str, int] = {}
    screen.on("response", lambda r: statuses.setdefault(r.url.split("?")[0].rsplit("/", 1)[-1], r.status))
    visit(screen, "/")
    href = screen.locator('link[rel="icon"]').get_attribute("href")
    assert href is not None and href.startswith("/static/img/favicon.svg")
    icon = screen.request.get(href)
    assert icon.ok and icon.headers["content-type"].startswith("image/svg+xml")
    ico = screen.request.get("/favicon.ico")
    assert ico.ok and ico.body() == icon.body()
    assert statuses.get("favicon.ico", 200) == 200  # if the browser asks for it anyway, it isn't a 404
    assert not [name for name, status in statuses.items() if status == 404]
    shoot(screen, "fu-favicon")
