"""Small page helpers shared by the browser tests."""

from playwright.sync_api import Page

# Every page keeps a server-sent-events stream open, so Playwright's "networkidle" never comes. Ready instead means:
# Alpine has started, and htmx has no request in flight (it marks the requesting element .htmx-request).
_READY = """() => !!(window.Alpine && document.body && document.body.hasAttribute('data-serial'))
  && !document.querySelector('.htmx-request')"""


def settle(page: Page, quiet_ms: int = 300) -> None:
    """Wait until the page has loaded, Alpine is up and htmx is idle, then for ``quiet_ms`` more (x-init fetches)."""
    page.wait_for_load_state("load")
    page.wait_for_function(_READY)
    page.wait_for_timeout(quiet_ms)
    page.wait_for_function(_READY)


def visit(page: Page, path: str) -> Page:
    page.goto(path)
    settle(page)
    return page


def has_horizontal_scroll(page: Page) -> bool:
    return page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
