"""The embedded mock's level pattern (MINIDSPD_MOCK_LEVELS=signal; phase 9 design §17, PR 11) drives the meter checks
of Something wrong?: In 1 carries a programme around −20 dBFS and each output follows the routing."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import settle, visit
from tests.e2e.test_m2_home import seed  # noqa: F401  (the walkthrough state)


@pytest.fixture(scope="module")
def app_settings() -> dict[str, str]:
    return {"MINIDSPD_MOCK_LEVELS": "signal"}


def levels(api: Api) -> dict:
    return api._ok(api.http.get(f"{api.dev}/status"))


def test_the_meter_checks_see_the_pattern(desktop: Page, api: Api):
    # Any page subscribes to the meters, so the level frames start flowing.
    visit(desktop, "/tasks/diagnose?step=2&what=one&output=1")
    status = wait_until(
        lambda: (s := levels(api))["input_levels"] and s["input_levels"][0] > -30 and s,
        timeout=20,
        what="the mock's In 1 programme",
        page=desktop,
    )
    assert status["input_levels"][1] == -120.0  # In 2: no signal
    assert status["output_levels"][1] == -120.0  # the Sub: nothing routed
    assert status["output_levels"][0] > -20.0  # Shakers: In 1 + 4 + 12
    desktop.reload()
    settle(desktop)
    checks = desktop.locator(".task-page")
    expect(checks.locator('[data-check="dev-signal"]')).to_contain_text("In 1 shows signal")
    expect(checks.locator('[data-check="o0-meter"]')).to_contain_text("The miniDSP is sending sound on Shakers")
