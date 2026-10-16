"""Dashboard output cards and sidebar names backed by the real preset state."""

import re

from app.sync.session import AdoptMode


def _card(html: str, n: int) -> str:
    cards = re.findall(r'<li class="output-card">(.*?)</li>', html, re.S)
    return cards[n]


async def test_output_cards_show_desired_values_and_sync(ctx):
    html = (await ctx.http.get("/")).text
    assert "not tracked yet" not in html
    first = _card(html, 0)
    assert "sync-unknown" in first and "bypassed" in first

    await ctx.session.adopt(AdoptMode.TRUST, preset=0)
    await ctx.session.edit(0, {"outputs": {"1": {"gain_db": -4.5, "delay_ms": 1.5}}})
    await ctx.settle()
    html = (await ctx.http.get("/")).text
    second = _card(html, 1)
    assert "−4.5 dB" in second and "1.50 ms" in second
    assert "sync-synced" in second


async def test_saved_channel_names_appear_in_nav_and_dashboard(ctx):
    await ctx.session.set_channel_meta("output", 3, name="Shakers")
    html = (await ctx.http.get("/")).text
    assert 'href="/outputs/4"' in html
    assert re.search(r'href="/outputs/4"[^>]*>\s*Shakers', html)
    assert "<h3>Shakers</h3>" in html
