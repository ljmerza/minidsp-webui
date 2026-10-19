"""Combined response (phase 9 design §12.3): the data route carries the one-line reading and the tactile outputs, and
the page offers the "Show the sum" toggle with its caveat and folds rounding notes through the shared macro."""

from .conftest import SERIAL, UI


async def test_data_reading_and_tactile(ctx):
    await ctx.session.set_channel_meta("output", 0, name="Shakers", tactile=True)
    await ctx.session.set_channel_meta("output", 1, name="Sub")
    data = (await ctx.http.get(f"{UI}/0/system?outputs=0,1")).json()
    assert data["reading"] == "Shakers: full range. Sub: silent here (In 1 isn't routed to it)."
    assert data["tactile"] == [0]
    data = (await ctx.http.get(f"{UI}/0/system?outputs=1&inputs=all")).json()
    assert data["reading"] == "Sub: full range."
    assert data["tactile"] == []


async def test_page_has_the_sum_toggle_and_the_folded_notes(ctx):
    html = (await ctx.http.get(f"/system?serial={SERIAL}")).text
    assert "<h1>Combined response</h1>" in html
    assert 'x-model="showSum"' in html and "Show the sum" in html
    assert "The sum adds outputs as if they played from one spot." in html
    assert 'class="graph-reading"' in html
    assert "mdsp.graph.classifyNotes(warnings)" in html and "What is a rounding note?" not in html
