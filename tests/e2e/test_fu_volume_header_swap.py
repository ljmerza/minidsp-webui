"""A header refresh never swallows a Volume change (static/js/master.js), on the embedded mock.

Every master or device event re-renders #app-header, and so does the reply to each Volume step. htmx binds the new
controls' triggers when the swap settles, 20 ms after it by default, so a tap, an arrow key or a −/+ click landing in
that gap went nowhere: the slider moved, the device didn't (test_m4_routing_volume's slider tap timed out ~1 in 6
runs on it). A swap in the middle of a press lost the click the same way, and one while a value was being typed
wiped it. Each case here forces the refresh at the bad moment instead of waiting for it to happen by chance.
"""

from playwright.sync_api import Dialog, Page, Request, expect

from tests.e2e.api import Api, wait_until
from tests.e2e.pages import settle, visit

# Fire `action` (window.__act) in the first task after the next #app-header swap, i.e. before htmx has settled it.
_AFTER_THE_NEXT_SWAP = """(action) => {
  const swapped = (e) => {
    if (!e.detail.target || e.detail.target.id !== 'app-header') return;
    document.removeEventListener('htmx:afterSwap', swapped);
    setTimeout(() => window.__act(action), 0);
  };
  document.addEventListener('htmx:afterSwap', swapped);
}"""
_REFRESH = "() => htmx.trigger('#app-header', 'refresh')"

_ACTIONS = """() => {
  window.__act = (action) => {
    const slider = document.getElementById('master-volume');
    if (action === 'tap') {  // a tap far down the track, as test_m4_routing_volume does it
      slider.value = String(window.mdsp.volumeScale.toPos(-60));
      slider.dispatchEvent(new Event('input', { bubbles: true }));
    } else if (action === 'key') {
      slider.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
    } else {
      document.getElementById(action).click();
    }
  };
}"""

_SWAPPED = """() => new Promise((resolve) => {
  const swapped = (e) => {
    if (!e.detail.target || e.detail.target.id !== 'app-header') return;
    document.removeEventListener('htmx:afterSettle', swapped);
    resolve(true);
  };
  document.addEventListener('htmx:afterSettle', swapped);
  setTimeout(() => resolve(false), 400);
  htmx.trigger('#app-header', 'refresh');
})"""


def _volume(api: Api) -> float:
    return float(api.http.get(f"{api.dev}/status").json()["master"]["volume"])


def _volume_posts(page: Page) -> list[str]:
    """The Volume writes the page sends (slider, field and −/+), as "volume" or "volume/down" etc."""
    sent: list[str] = []

    def record(request: Request) -> None:
        if request.method == "POST" and "/master/volume" in request.url:
            sent.append(request.url.split("/master/", 1)[1])

    page.on("request", record)
    return sent


def _answer(page: Page) -> list[str]:
    dialogs: list[str] = []

    def answer(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    page.on("dialog", answer)
    return dialogs


def _start(page: Page, api: Api, volume: float = -10) -> None:
    api.master(volume=volume, confirm=True)
    visit(page, "/")
    expect(page.locator("#master-volume-num")).to_have_value(f"{volume:.1f}")
    page.evaluate(_ACTIONS)


def test_a_slider_tap_right_after_a_header_refresh_is_sent_and_asks_from_the_right_value(desktop: Page, api: Api):
    _start(desktop, api)
    sent = _volume_posts(desktop)
    dialogs = _answer(desktop)
    desktop.evaluate(_AFTER_THE_NEXT_SWAP, "tap")
    desktop.evaluate(_REFRESH)
    wait_until(lambda: _volume(api) == -60, what="the tapped −60 dB", page=desktop)
    assert dialogs and dialogs[0].startswith("Drop the volume from −10 to −60 dB?")
    settle(desktop)
    assert sent == ["volume"], f"one write expected, got {sent}"
    expect(desktop.locator("#master-volume-num")).to_have_value("-60.0")


def test_a_step_or_arrow_key_right_after_a_header_refresh_is_sent(desktop: Page, api: Api):
    sent = _volume_posts(desktop)
    for action, want in (("volume-down", -10.5), ("volume-up", -9.5), ("key", -10.5)):
        _start(desktop, api)
        sent.clear()
        desktop.evaluate(_AFTER_THE_NEXT_SWAP, action)
        desktop.evaluate(_REFRESH)
        wait_until(lambda want=want: _volume(api) == want, what=f"{action} to land", page=desktop)
        settle(desktop)
        assert len(sent) == 1, f"{action}: one write expected, got {sent}"
        expect(desktop.locator("#master-volume-num")).to_have_value(f"{want:.1f}")


def test_phone_steps_right_after_a_header_refresh_keep_the_sheet_open(phone: Page, api: Api):
    _start(phone, api)
    phone.locator(".vol-open").tap()
    sheet = phone.locator("#hdr-volume-sheet")
    expect(sheet).to_be_visible()
    sent = _volume_posts(phone)
    # The open sheet holds unrelated refreshes, so the swap here is the reply to a step: the next step comes right
    # after it, on the header that reply rendered.
    phone.evaluate(_AFTER_THE_NEXT_SWAP, "volume-up")
    phone.locator("#volume-up").tap()
    wait_until(lambda: _volume(api) == -9, what="both steps to land", page=phone)
    settle(phone)
    expect(phone.locator("#master-volume-num")).to_have_value("-9.0")
    expect(sheet).to_be_visible()
    assert len(sent) == 2, sent
    phone.keyboard.press("Escape")


def test_a_header_refresh_during_a_press_waits_for_the_click(desktop: Page, api: Api):
    _start(desktop, api)
    button = desktop.locator("#volume-down")
    box = button.bounding_box()
    assert box
    desktop.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    desktop.mouse.down()
    assert not desktop.evaluate(_SWAPPED), "the header was replaced under the pressed button"
    desktop.mouse.up()
    wait_until(lambda: _volume(api) == -10.5, what="the click to land", page=desktop)
    expect(desktop.locator("#master-volume-num")).to_have_value("-10.5")  # and the held refresh came through


def test_a_header_refresh_doesnt_wipe_a_volume_being_typed(desktop: Page, api: Api):
    _start(desktop, api)
    field = desktop.locator("#master-volume-num")
    field.click()
    field.press("Control+a")
    desktop.keyboard.type("-14")
    desktop.wait_for_timeout(300)  # a pause well past master.js's IDLE_MS
    assert not desktop.evaluate(_SWAPPED), "the header was replaced under the field being typed in"
    expect(field).to_have_value("-14")
    expect(field).to_be_focused()
    field.press("Enter")
    wait_until(lambda: _volume(api) == -14, what="the typed −14", page=desktop)
    expect(field).to_have_value("-14.0")


def test_a_refresh_while_the_write_or_the_confirm_is_pending_loses_nothing(desktop: Page, api: Api):
    _start(desktop, api)
    sent = _volume_posts(desktop)
    dialogs: list[str] = []

    def answer(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        api.master(volume=-12, confirm=True)  # someone else moves it while the question is up
        dialog.accept()

    desktop.on("dialog", answer)

    def slow(route) -> None:  # the write is still in flight when the refresh comes
        desktop.evaluate("() => htmx.trigger('#app-header', 'refresh')")
        desktop.wait_for_timeout(300)
        route.continue_()

    desktop.route("**/master/volume", slow)
    desktop.evaluate("() => window.__act('tap')")
    wait_until(lambda: _volume(api) == -60, what="the confirmed −60 dB", page=desktop)
    assert dialogs and dialogs[0].startswith("Drop the volume from −10 to −60 dB?")
    settle(desktop)
    assert sent == ["volume"], sent
    expect(desktop.locator("#master-volume-num")).to_have_value("-60.0")


def test_an_outside_change_still_shows_when_the_volume_is_idle(desktop: Page, api: Api):
    _start(desktop, api)
    api.master(volume=-20, confirm=True)
    expect(desktop.locator("#master-volume-num")).to_have_value("-20.0", timeout=3000)
