"""Phase 9 PR 2 (P02) in a real browser: "Use the device's values…" asks first, skips all-zero filters and can be
undone without writing to the device.

The state: the app gave Shakers an EQ cut (the mock acknowledges coefficients but doesn't store them, so it reads
back all zeros: a filter that must never be used), and Sub's level was changed on the device behind the app's back
(−6 dB, which the mock does store and reports).
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.axe import assert_accessible
from tests.e2e.pages import visit
from tests.e2e.server import AppServer

CUT = {"design": {"type": "peaking", "freq": 40, "q": 4, "gain_db": -4}, "bypass": False}


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def state(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        api.put_peq_band("outputs", 0, 0, CUT)
        api.wait_synced()

    return state


@pytest.fixture(scope="module", autouse=True)
def sub_changed_on_the_device(app: AppServer) -> None:
    app.write_behind_the_apps_back({"outputs": [{"index": 1, "gain": -6.0}]})


def test_use_the_devices_values_confirms_names_and_undo_writes_nothing(screen: Page, api: Api, shoot):
    before = api.wait_synced()
    visit(screen, "/device")
    # A finished check is announced over the page's event stream, and the panel reloads itself 300 ms later
    # (device.html, hx-trigger "sse:verify delay:300ms"). Wait for that reload: it would close a dialog opened
    # before it (see the PR notes; a fast tap can lose the dialog the same way).
    with screen.expect_response(
        lambda r: r.request.method == "GET" and r.url.endswith(f"/ui/devices/{api.serial}/verify")
    ):
        screen.get_by_role("button", name="Check device", exact=True).click()
    expect(screen.locator("#verify-result")).to_contain_text("changed on device")

    screen.get_by_role("button", name="Use the device's values…", exact=True).click()
    dialog = screen.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", level=2)).to_have_text("Use the device's values for 1 setting?")
    expect(dialog).to_contain_text("Sub output level")
    expect(dialog).to_contain_text("−6.00 dB")
    # The all-zero filter is listed as not used, by name, and not offered.
    expect(dialog.locator(".confirm-refused")).to_contain_text("Shakers · EQ band 1")
    expect(dialog.locator(".confirm-lines")).not_to_contain_text("EQ band")
    text = dialog.inner_text()
    assert "outputs." not in text and "b0" not in text
    assert_accessible(screen, include="#verify-accept-dialog")
    shoot(screen, "e2e-m1-use-device-values")

    dialog.get_by_role("button", name="Use the device's values", exact=True).click()
    toast = screen.locator("#toasts .toast").filter(has_text="Using the device's values for 1 setting")
    expect(toast).to_be_visible()
    used = api.desired()
    assert used["outputs"][1]["gain_db"] == -6.0
    assert used["outputs"][0]["peq"][0] == before["desired"]["outputs"][0]["peq"][0]  # the cut is still the cut

    with screen.expect_response(lambda r: r.url.endswith("/verify/accept/undo")) as undo:
        toast.get_by_role("button", name="Undo").click()
    assert undo.value.ok
    expect(screen.locator("#toasts")).to_contain_text("Undone")
    after = wait_until(lambda: (p := api.preset(0))["desired"] == before["desired"] and p, what="the undo")
    assert after["writes_sent"] == before["writes_sent"]  # nothing was sent to the device, either way
    assert after["counts"]["pending"] == 0
