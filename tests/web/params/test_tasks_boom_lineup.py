"""Fix a boom and Line up shakers on a real session (phase 9 design §14.2, §14.5)."""

import html
import re

from app.model.filters import FilterDesign
from app.sync.filters import set_peq_band

from .conftest import HX

ORIGIN = {**HX, "Origin": "http://test"}


def _undo(text: str) -> str:
    found = re.search(r'name="undo" value="([^"]*)"', text)
    assert found, "no Undo in the step"
    return html.unescape(found.group(1))


async def _post(ctx, task: str, **form):
    return await ctx.http.post(f"/ui/tasks/{task}", data={"mode": "dialog", **form}, headers=ORIGIN)


async def test_both_wizards_are_served_and_their_links_light_up(ctx):
    for path in ("/tasks/eq-cut", "/tasks/line-up"):
        assert (await ctx.http.get(path)).status_code == 200, path
    home = (await ctx.http.get("/")).text
    served = re.search(r'data-tasks="([^"]*)"', home)  # the dialog on every page opens both
    assert served and {"eq-cut", "line-up"} <= set(served.group(1).split())
    assert 'href="/tasks/eq-cut"' in home and 'href="/tasks/line-up"' in home
    # The EQ and Delay tabs link to them for their output.
    assert 'href="/tasks/eq-cut?output=2"' in (await ctx.http.get("/outputs/2?tab=peq")).text
    assert 'href="/tasks/line-up?output=1"' in (await ctx.http.get("/outputs/1?tab=delay")).text


async def test_fix_a_boom_steps_say_what_they_will_do(ctx):
    amount = await ctx.http.get("/ui/tasks/eq-cut?step=3&output=2&freq=40&mode=dialog", headers=HX)
    text = html.unescape(amount.text)
    assert "Medium (Q 4): about 35–45 Hz, good for booms" in text
    assert "Narrow (Q 8): about 38–43 Hz" in text and "Wide (Q 1.4): about 28–57 Hz" in text
    assert "Lowers about 35–45 Hz, deepest −4 dB at 40 Hz." in text
    assert 'class="eqcut-graph"' in text and "data-highlight='[\"o1:eq\"]'" in amount.text
    check = html.unescape((await ctx.http.get("/ui/tasks/eq-cut?step=4&output=2&freq=40", headers=HX)).text)
    assert "Out 2: cut 4 dB at 40 Hz, medium width (Q 4)." in check
    assert "Uses EQ band 1 (free), switched on." in check and "Nothing is sent until you press Apply." in check
    # A missing frequency sends the user back to step 2 with a reason.
    back = html.unescape((await ctx.http.get("/ui/tasks/eq-cut?step=3&output=2", headers=HX)).text)
    assert "Which frequency?" in back and "Enter a frequency" in back


async def test_fix_a_boom_applies_one_band_in_one_edit_and_undo_puts_it_back(ctx):
    before = ctx.session.desired(0).outputs[1].peq[0].model_dump(mode="json")
    revisions = len(await ctx.session.history(0))
    form = {"output": "2", "freq": "40", "kind": "cut", "amount": "4", "width": "medium", "step": "4", "op": "apply"}
    response = await _post(ctx, "eq-cut", **form)
    assert response.status_code == 200
    assert "Out 2 now has 1 EQ cut: −4 dB at 40 Hz (new)." in html.unescape(response.text)
    band = ctx.session.desired(0).outputs[1].peq[0]
    assert not band.bypass
    assert (band.design.type, band.design.freq, band.design.q, band.design.gain_db) == ("peaking", 40.0, 4.0, -4.0)
    # One edit (one revision) carries the whole band, named after the wizard.
    history = await ctx.session.history(0)
    assert len(history) == revisions + 1 and "Fix a boom" in (history[0].summary or "")
    record = _undo(response.text)
    # The next cut goes into the next free band.
    check = html.unescape((await ctx.http.get("/ui/tasks/eq-cut?step=4&output=2&freq=63", headers=HX)).text)
    assert "Uses EQ band 2 (free), switched on." in check
    undone = await _post(ctx, "eq-cut", **{**form, "step": "5", "op": "undo", "undo": record})
    assert "Undone: Out 2 · EQ band 1 is back." in html.unescape(undone.text)
    assert ctx.session.desired(0).outputs[1].peq[0].model_dump(mode="json") == before


async def test_fix_a_boom_with_every_band_in_use_says_so(ctx):
    for k in range(10):
        design = FilterDesign(type="peaking", freq=30.0 + k, q=4.0, gain_db=-1.0)
        await set_peq_band(ctx.session, 0, "outputs", 1, k, design=design, bypass=False, commit=True, source="ui")
    step = html.unescape((await ctx.http.get("/ui/tasks/eq-cut?step=1&output=2", headers=HX)).text)
    assert "Not possible now: All 10 EQ bands on Out 2 are in use." in step
    assert re.search(r'value="2"[^>]*disabled', step)
    refused = await _post(ctx, "eq-cut", output="2", freq="40", amount="4", step="4", op="apply")
    assert "All 10 EQ bands on Out 2 are in use" in html.unescape(refused.text)


async def test_a_big_boost_needs_the_checkbox(ctx):
    form = {"output": "2", "freq": "40", "kind": "boost", "amount": "9", "width": "medium", "step": "4"}
    refused = await _post(ctx, "eq-cut", **form, op="apply")
    text = html.unescape(refused.text)
    assert "Tick the box" in text and "I confirm boosting Out 2 by 9 dB at 40 Hz." in text
    assert ctx.session.desired(0).outputs[1].peq[0].bypass
    applied = await _post(ctx, "eq-cut", **form, op="apply", confirm="1")
    assert "Out 2 now has 1 EQ boost: +9 dB at 40 Hz (new)." in html.unescape(applied.text)
    assert ctx.session.desired(0).outputs[1].peq[0].design.gain_db == 9.0


async def test_line_up_sets_8_75_ms_for_3_m_then_nudges_and_undo_all(ctx):
    check = await ctx.http.get(
        "/ui/tasks/line-up?step=3&felt=1&heard=2&seat=1&heard_dist=3&unit=m&mode=dialog", headers=HX
    )
    text = html.unescape(check.text)
    assert "Sound from Out 2 takes 8.75 ms to reach you." in text
    assert "Out 1 will wait 8.75 ms (840 samples) so both arrive together. Out 2 keeps its 0 ms." in text
    assert ctx.session.desired(0).outputs[0].delay_ms == 0.0  # nothing sent yet
    base = {"felt": "1", "heard": "2", "seat": "1", "heard_dist": "3", "unit": "m"}
    applied = await _post(ctx, "line-up", **base, step="3", op="apply")
    assert "Out 1 waits 8.75 ms." in html.unescape(applied.text)
    assert ctx.session.desired(0).outputs[0].delay_ms == 8.75
    record = _undo(applied.text)
    nudged = await _post(ctx, "line-up", **base, target="felt", step="4", op="nudge", by="1", undo=record)
    assert "Applied: Out 1 waits 9.75 ms." in html.unescape(nudged.text)
    record = _undo(nudged.text)
    flipped = await _post(ctx, "line-up", **base, target="felt", step="4", op="polarity", invert="1", undo=record)
    assert "Applied: Out 1 polarity 180°." in html.unescape(flipped.text)
    assert ctx.session.desired(0).outputs[0].invert
    record = _undo(flipped.text)
    done = await ctx.http.get(
        "/ui/tasks/line-up",
        params={**base, "target": "felt", "step": "5", "undo": record, "mode": "dialog"},
        headers=HX,
    )
    assert "Out 1 waits 9.75 ms, polarity 180°." in html.unescape(done.text)
    undone = await _post(ctx, "line-up", **base, target="felt", step="5", op="undo", undo=record)
    assert "Undone" in html.unescape(undone.text)
    out = ctx.session.desired(0).outputs[0]
    assert out.delay_ms == 0.0 and not out.invert


async def test_line_up_step_1_leaves_the_unit_to_step_2_so_the_remembered_one_applies(ctx):
    step1 = (await ctx.http.get("/ui/tasks/line-up?mode=dialog", headers=HX)).text
    assert 'name="unit"' not in step1
    step2 = (await ctx.http.get("/ui/tasks/line-up?step=2&seat=1&felt=1&heard=2&mode=dialog", headers=HX)).text
    assert "localStorage.getItem('mdsp.distanceUnit')" in step2
    chosen = (await ctx.http.get("/ui/tasks/line-up?step=2&seat=1&felt=1&heard=2&unit=m&mode=dialog", headers=HX)).text
    assert "localStorage.getItem('mdsp.distanceUnit')" not in chosen  # chosen on step 2 (Back from step 3) wins


async def test_line_up_feet_and_a_farther_felt_output(ctx):
    feet = await ctx.http.get("/ui/tasks/line-up?step=3&felt=1&heard=2&seat=1&heard_dist=10&unit=ft", headers=HX)
    assert "Out 1 will wait 8.89 ms (853 samples)" in html.unescape(feet.text)
    farther = await ctx.http.get(
        "/ui/tasks/line-up?step=3&felt=1&heard=2&seat=0&felt_dist=4&heard_dist=1&unit=m", headers=HX
    )
    text = html.unescape(farther.text)
    assert "Out 1 is farther from you than Out 2, so Out 2 waits instead." in text
    assert "Out 2 will wait 8.75 ms" in text
    too_far = html.unescape(
        (await ctx.http.get("/ui/tasks/line-up?step=3&felt=1&heard=2&seat=1&heard_dist=30&unit=m", headers=HX)).text
    )
    assert "the device can wait at most 79.8 ms" in too_far and 'value="apply"' not in too_far
    # The second distance starts empty and is required.
    empty = html.unescape((await ctx.http.get("/ui/tasks/line-up?step=3&felt=1&heard=2", headers=HX)).text)
    assert "How far from your seat?" in empty and "Enter how far" in empty
    same = html.unescape((await ctx.http.get("/ui/tasks/line-up?step=2&felt=1&heard=1", headers=HX)).text)
    assert "Choose two different outputs." in same
