"""Browser tests (phase 9 design §17, PR 4): Playwright (pytest-playwright, Chromium) against the real app.

Every test module gets its own app (`tests/e2e/server.py`): ``python -m app.main`` in embedded mode with minidspd's
mock 2x4HD, on ephemeral loopback ports, with a fresh database. By default it is set up the way the phase 9
walkthroughs left it: adopted at factory defaults, Out 1 named "Shakers" and Out 2 "Sub". A module can change that by
overriding ``app_settings`` (extra app settings) or ``seed`` (what is done through the API before its tests).

Pages: ``desktop`` (1440×900), ``phone`` (390×844, touch, DPR 3), or ``screen`` to run a test at both. Each page's
base URL is the app, so ``page.goto("/routing")`` works. An uncaught JavaScript error on a page fails the test.
``shoot(page, name)`` saves a screenshot to ``$MDW_E2E_SCREENSHOTS`` when that is set (UI PRs attach these).

Not part of the default ``pytest`` run (marker ``e2e``): ``make test-e2e``, or ``uv run pytest -m e2e tests/e2e``.
Setup once: ``uv run playwright install chromium`` and ``scripts/fetch-minidspd.sh`` (``make e2e-setup``).
Useful pytest-playwright options: ``--headed``, ``--slowmo 200``, ``--tracing retain-on-failure``,
``--screenshot only-on-failure`` (artifacts in ``test-results/``).
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Page

from tests.e2e.api import Api
from tests.e2e.server import FETCH_HINT, AppServer, find_minidspd

HERE = Path(__file__).resolve().parent

DESKTOP: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
PHONE: dict[str, Any] = {
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
}
SCREENS = {"desktop": DESKTOP, "phone": PHONE}

# Generous for a loaded CI runner; a test that waits on something specific should still say what it waits for.
DEFAULT_TIMEOUT_MS = 10_000


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Before -m deselects: everything under tests/e2e is marked e2e.
    for item in items:
        if HERE in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def minidspd() -> Path:
    path = find_minidspd()
    if path is None:
        message = f"no minidspd binary for the browser tests: {FETCH_HINT}"
        if os.environ.get("MDW_E2E_REQUIRED"):
            pytest.fail(message)  # CI: a missing binary must not pass as "skipped"
        pytest.skip(message)
    return path


@pytest.fixture(scope="module")
def app_settings() -> dict[str, str]:
    """Extra app settings (environment variables) for this module's app."""
    return {}


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    """What is done through the API before this module's tests run (override in a module to change it)."""

    def walkthrough_state(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")

    return walkthrough_state


@pytest.fixture(scope="module")
def app(minidspd: Path, app_settings: dict[str, str], seed: Callable[[Api], None]) -> Iterator[AppServer]:
    with AppServer(minidspd, settings=app_settings) as server:
        api = Api(server.url, server.serial)
        try:
            seed(api)
        finally:
            api.close()
        yield server


@pytest.fixture(scope="module")
def api(app: AppServer) -> Iterator[Api]:
    client = Api(app.url, app.serial)
    yield client
    client.close()


def _open(new_context: Callable[..., BrowserContext], app: AppServer, screen: str) -> Iterator[Page]:
    context = new_context(base_url=app.url, **SCREENS[screen])
    context.set_default_timeout(DEFAULT_TIMEOUT_MS)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"{exc.name}: {exc.message}"))
    page.screen_name = screen  # type: ignore[attr-defined]  # for shoot() and messages
    yield page
    assert not errors, f"uncaught JavaScript error(s) on {page.url}: {errors}"


@pytest.fixture
def desktop(new_context: Callable[..., BrowserContext], app: AppServer) -> Iterator[Page]:
    yield from _open(new_context, app, "desktop")


@pytest.fixture
def phone(new_context: Callable[..., BrowserContext], app: AppServer) -> Iterator[Page]:
    yield from _open(new_context, app, "phone")


@pytest.fixture(params=list(SCREENS))
def screen(
    request: pytest.FixtureRequest, new_context: Callable[..., BrowserContext], app: AppServer
) -> Iterator[Page]:
    """The test runs twice: at 1440×900 and at 390×844 (touch)."""
    yield from _open(new_context, app, request.param)


@pytest.fixture(scope="session")
def shoot() -> Callable[[Page, str], Path | None]:
    target = os.environ.get("MDW_E2E_SCREENSHOTS")

    def save(page: Page, name: str) -> Path | None:
        if not target:
            return None
        path = Path(target) / f"{name}-{getattr(page, 'screen_name', 'page')}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))
        return path

    return save
