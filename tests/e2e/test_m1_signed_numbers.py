"""Phase 9 PR 1 (P01, P07 part) in a real browser: signed numbers and the EQ table basics.

The bug: typing "-4" key by key into a PEQ gain sent +4 dB (a number field reports a lone "-" as empty, and Alpine's
x-model.number wiped it). These type the way a person does, one key at a time, and check what reaches the server.
"""

from playwright.sync_api import Locator, Page, Response, expect

from tests.e2e.pages import visit

SHAKERS_PEQ = "/outputs/1?tab=peq"


def open_peq(page: Page) -> None:
    visit(page, SHAKERS_PEQ)
    show_all = page.get_by_role("button", name="Show all ▸")  # phones fold the bands not in use (design §8.3)
    if show_all.is_visible():
        show_all.click()
    expect(page.get_by_label("Band 10 type")).to_be_visible()  # the bands have loaded


def band(page: Page, k: int) -> Locator:
    """Band k (from 0) of the PEQ table: one <tbody> per band."""
    return page.locator(".peq-table tbody").nth(k)


def band_post(page: Page, k: int):
    """Waits for the editor's POST of band k (Shakers, preset 1) and its answer."""
    return page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith(f"/presets/0/outputs/0/peq/{k}") and r.ok
    )


def sent(response: Response) -> dict:
    return response.request.post_data_json


def test_typing_minus_4_key_by_key_sends_a_4_db_cut(screen: Page, api):
    k = 0 if screen.screen_name == "desktop" else 4  # each screen gets its own unused band
    open_peq(screen)
    row = band(screen, k)
    with band_post(screen, k) as turned_on:
        row.get_by_label(f"Band {k + 1} type").select_option("peaking")
    assert sent(turned_on.value)["design"]["type"] == "peaking"
    # Boost first: a swallowed "-" would then send +4, not a cut that only looks right because Cut was preselected.
    row.get_by_role("radio", name="Boost").check()
    field = row.locator("input.mag")
    field.fill("")
    field.press_sequentially("-4", delay=60)
    with band_post(screen, k) as committed:
        field.press("Tab")
    assert sent(committed.value)["design"]["gain_db"] == -4
    assert api.peq_band("outputs", 0, k)["design"]["gain_db"] == -4
    expect(row.get_by_role("radio", name="Cut")).to_be_checked()  # the typed "-" picked Cut
    expect(field).to_have_value("4")


def test_choosing_peaking_on_an_unused_band_turns_it_on(desktop: Page, api):
    open_peq(desktop)
    row = band(desktop, 1)
    expect(row.get_by_label("Band 2 on")).not_to_be_checked()
    with band_post(desktop, 1) as posted:
        row.get_by_label("Band 2 type").select_option("peaking")
    assert sent(posted.value)["bypass"] is False
    expect(row.get_by_label("Band 2 on")).to_be_checked()
    assert api.peq_band("outputs", 0, 1)["bypass"] is False


def test_cut_plus_a_magnitude_stores_a_negative_gain(desktop: Page, api):
    open_peq(desktop)
    row = band(desktop, 2)
    with band_post(desktop, 2):
        row.get_by_label("Band 3 type").select_option("peaking")
    row.get_by_role("radio", name="Boost").check()
    field = row.locator("input.mag")
    field.fill("")
    field.press_sequentially("4", delay=60)
    with band_post(desktop, 2) as boosted:
        field.press("Tab")
    assert sent(boosted.value)["design"]["gain_db"] == 4
    with band_post(desktop, 2) as cut:
        row.get_by_role("radio", name="Cut").check()
    assert sent(cut.value)["design"]["gain_db"] == -4
    expect(field).to_have_value("4")  # the field holds the magnitude; Cut carries the sign
    assert api.peq_band("outputs", 0, 2)["design"]["gain_db"] == -4


def test_unused_bands_say_off(screen: Page):
    open_peq(screen)
    for k in range(5, 10):  # bands no other test here touches
        status = band(screen, k).locator(".band-status")
        expect(status.locator(".band-off")).to_be_visible()
        expect(status.locator(".band-off")).to_have_text("off")
        expect(status.locator(".sync-badge")).to_be_hidden()


def test_typing_a_negative_compressor_threshold_key_by_key(desktop: Page, api):
    visit(desktop, "/outputs/1?tab=compressor")
    field = desktop.get_by_label("Starts at (threshold)", exact=True)
    expect(field).to_have_value("0")
    field.fill("")
    field.press_sequentially("-10", delay=60)
    with desktop.expect_response(lambda r: r.request.method == "PUT" and "/outputs/0/compressor" in r.url) as put:
        field.press("Tab")
    assert put.value.ok and put.value.request.post_data_json["threshold_db"] == -10
    assert api.compressor(0)["compressor"]["threshold_db"] == -10
    # The − stepper needs no minus key at all.
    with desktop.expect_response(lambda r: r.request.method == "PUT" and "/outputs/0/compressor" in r.url):
        desktop.get_by_role("button", name="Threshold 1 dB lower").click()
    expect(field).to_have_value("-11")
