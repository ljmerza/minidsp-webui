"""The header status word and the Device & sync page on the one status model (phase 9 design §11, PR 7).

The check here is built by hand the way the embedded mock answers one: every readable setting reads zero, so every
sent gain and filter is "changed on device", and routing, switches and compressor timing can't be checked."""

import re

import pytest

from app.model.config_doc import set_path
from app.sync import verify as svc
from app.sync.session import AdoptMode

from .conftest import HX, SERIAL, UI

ZERO = dict.fromkeys(("b0", "b1", "b2", "a1", "a2"), 0.0)


def zero_readback_report(session, preset: int = 0) -> svc.VerifyReport:
    state = session.writer.state(preset)
    items = []
    for path, value in state.values(session.profile).items():
        unreadable = path.endswith((".bypass", ".mute", ".invert")) or ".routing." in path or ".compressor." in path
        if unreadable:
            items.append(svc.VerifyItem(path, svc.ItemStatus.UNVERIFIABLE, expected=value, note="no register"))
        elif isinstance(value, dict):
            items.append(svc.VerifyItem(path, svc.ItemStatus.DRIFT, expected=value, actual=dict(ZERO)))
        elif value == 0.0:
            items.append(svc.VerifyItem(path, svc.ItemStatus.OK, expected=value, actual=0.0))
        else:
            items.append(svc.VerifyItem(path, svc.ItemStatus.DRIFT, expected=value, actual=0.0))
    return svc.VerifyReport(
        SERIAL, preset, "2026-10-18T14:02:00+00:00", 3000.0, 51, items, writes_at=session.writer.writes_sent
    )


@pytest.fixture(autouse=True)
def _no_reports():
    svc.forget_reports()
    yield
    svc.forget_reports()


@pytest.fixture
async def checked(ctx, monkeypatch):
    """Adopted (trusted) with a −6 dB level on Out 2 and a cut on Out 1, then checked against a zero-reading device."""
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    ctx.session.writer.set_desired(0, set_path(ctx.session.desired(0), "outputs.1.gain_db", -6.0))
    ctx.session.writer.trust(0)
    report = zero_readback_report(ctx.session)
    monkeypatch.setitem(svc._last_reports, SERIAL, report)
    return report


def visible(html: str) -> str:
    """The page text a user sees with Technical details closed: no row-tech parts, folds or tags."""
    html = re.sub(r'<(code|span|div)[^>]*class="[^"]*row-tech[^"]*"[^>]*>.*?</\1>', "", html, flags=re.S)
    html = re.sub(r"<details[^>]*>(?:(?!<details).)*?</details>", "", html, flags=re.S)
    html = re.sub(r"<input[^>]*>", "", html)
    return re.sub(r"<[^>]+>", " ", html)


async def test_header_says_all_sent_and_never_claims_a_match(ctx):
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    html = (await ctx.http.get("/ui/header")).text
    assert "All sent" in html and 'data-sync-state="sent"' in html
    text = visible(html).lower()
    assert "match" not in text and "in sync" not in text and "synced" not in text


async def test_header_headline_is_changed_on_device_after_a_check(ctx, checked):
    html = (await ctx.http.get("/ui/header")).text
    assert 'data-sync-state="changed"' in html
    assert re.search(r"≠</span>\s*\d+ changed on device", html)
    assert "match" not in visible(html).lower()


async def test_header_failed_beats_everything(ctx, checked):
    ctx.session.writer.state(0).failed["outputs.2.gain_db"] = "rejected"
    html = (await ctx.http.get("/ui/header")).text
    assert 'data-sync-state="failed"' in html and "1 failed" in html


async def test_overview_headline_actions_and_check_line(ctx, checked):
    html = (await ctx.http.get(f"{UI}/sync-panel")).text
    assert 'id="sync-overview"' in html and 'data-sync-state="changed"' in html
    assert "changed on device" in html
    for action in ("Check device", "Send all", "Set up this device"):
        assert action in html
    assert 'href="#adopt"' in html and 'hx-target="#verify-panel"' in html
    # The overview reloads through the sync bar URL of the Device page and answers with itself.
    reload = re.search(r'id="sync-overview" hx-get="([^"]+)"', html).group(1).replace("&amp;", "&")
    again = (await ctx.http.get(reload)).text
    assert 'id="sync-overview"' in again and 'id="syncbar"' not in again


async def test_push_all_from_the_device_page_answers_with_the_overview(ctx):
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    response = await ctx.http.post(f"{UI}/presets/0/push", params={"page": "/device"}, headers=HX)
    assert response.status_code == 200
    assert 'id="sync-overview"' in response.text and "All sent" in response.text


async def test_table_groups_problems_by_channel_with_plain_values(ctx, checked):
    html = (await ctx.http.get(f"{UI}/sync-table", params={"preset": 0})).text
    out2 = re.search(r'<details class="sync-group has-problems" id="sync-output-1".*?</details>', html, re.S).group(0)
    assert "Out 2" in out2 and re.search(r"≠</span> \d+ changed on device", out2)
    assert "Out 2 output level" in out2 and "Device reads: <strong>+0.00 dB</strong>" in out2
    assert "Send app&#39;s value" in out2 or "Send app's value" in out2
    # Only the level is offered: the all-zero filters are refused.
    assert re.search(r"Use the device(&#39;|')s values for\s+this setting", out2)
    assert "Forget these values" in out2
    assert 'name="path" value="outputs.1.gain_db"' in out2
    # Every readable filter reads all zeros: shown in words, never offered for "Use the device's values".
    out1 = re.search(r'id="sync-output-0".*?</details>', html, re.S).group(0)
    assert "all zeros (looks like a read error)" in out1
    assert "Use the device" not in out1 and "Forget these values" in out1
    assert "Problems only (" in html and "Everything (216)" in html


async def test_no_raw_coefficients_or_internal_names_unless_technical_details(ctx, checked):
    html = (await ctx.http.get(f"{UI}/sync-table", params={"preset": 0, "show": "all"})).text
    assert "b0=" in html and "outputs.0.gain_db" in html  # there, under Technical details
    text = visible(html)
    assert "b0" not in text and "outputs." not in text and "inputs." not in text
    assert 'class="sync-tech-toggle"' in html


async def test_sent_rows_show_no_mark(ctx):
    await ctx.session.adopt(AdoptMode.TRUST, preset=0, confirm_defaults=True)
    html = (await ctx.http.get(f"{UI}/sync-table", params={"preset": 0, "show": "all"})).text
    row = re.search(r'<tr class="row-synced">.*?</tr>', html, re.S).group(0)
    assert '<span class="visually-hidden">Sent</span>' in row and "✓" not in row


async def test_waiting_rows_on_a_preset_that_isnt_playing(ctx):
    await ctx.session.edit(1, {"outputs": {"0": {"gain_db": -3.0}}})
    html = (await ctx.http.get(f"{UI}/sync-panel", params={"preset": 2})).text
    assert "isn&#39;t playing" in html or "isn't playing" in html
    table = (await ctx.http.get(f"{UI}/sync-table", params={"preset": 1})).text
    assert "Waiting for preset 2" in table and "sync-waiting" in table


async def test_device_page_layout(ctx, checked):
    html = (await ctx.http.get("/device")).text
    assert 'id="sync-settings"' in html and f'hx-get="{UI}/sync-table?preset=0"' in html
    # Device, daemon and connection details and the event log start folded.
    assert re.search(r'<details id="technical" class="card fold" data-keep-open>', html)
    assert re.search(r'<details id="events" class="card fold" data-keep-open>', html)
    assert html.index('id="sync-settings"') < html.index('id="technical"') < html.index('id="events"')
    for word in ("Sending", "Waiting for preset N", "Failed", "Unknown", "Changed on device", "Can't check"):
        assert word in html.replace("&#39;", "'")
    assert "/static/js/device.js" in html and "/static/css/sync.css" in html
