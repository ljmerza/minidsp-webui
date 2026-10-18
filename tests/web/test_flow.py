"""The signal-flow diagram's geometry, block states and rendering (app/web/flow.py; phase 9 design §5)."""

import re

import pytest

from app.model.config_doc import ConfigDoc, default_config_doc
from app.model.device_profile import M2X4HD, DeviceProfile
from app.sync.summary import summarize
from app.web import flow
from app.web.flow import (
    BLOCK_ORDER,
    FOLD_H,
    IN_H,
    ROUTE_W,
    ROW_GAP,
    ROW_H,
    block_href,
    block_sync,
    build_flow,
    flow_layout,
    output_blocks,
    route_path,
)
from app.web.templating import templates
from tests.unit.sync.test_summary import NAMES, base, meta, route, run, walkthrough, xo

# A hypothetical bigger model: nothing in the diagram may assume 2 inputs and 4 outputs.
P4X8 = DeviceProfile(product_name="4x8 (test)", n_inputs=4, n_outputs=8, sources=("Analog",))
UNUSED = {**NAMES, ("output", 2): meta("output", 2, unused=True), ("output", 3): meta("output", 3, unused=True)}


def _no_overlap(slots):
    ordered = sorted(slots, key=lambda s: s.top)
    for a, b in zip(ordered, ordered[1:], strict=False):
        assert a.bottom <= b.top, (a, b)


# --- geometry ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", [M2X4HD, P4X8], ids=["2x4", "4x8"])
def test_every_channel_of_the_profile_gets_a_box(profile):
    layout = flow_layout(profile)
    assert len(layout.inputs) == profile.n_inputs
    assert sorted(layout.outputs) == list(range(profile.n_outputs))
    assert layout.fold is None
    assert layout.width == ROUTE_W
    _no_overlap(layout.inputs)
    _no_overlap(layout.outputs.values())
    rows_h = profile.n_outputs * ROW_H + (profile.n_outputs - 1) * ROW_GAP
    assert layout.height == max(rows_h, profile.n_inputs * IN_H + (profile.n_inputs - 1) * 12)
    for slot in (*layout.inputs, *layout.outputs.values()):
        assert slot.top >= 0 and slot.bottom <= layout.height
    # Inputs are spread evenly over the height, in order.
    centers = [s.center for s in layout.inputs]
    gaps = {round(b - a, 6) for a, b in zip(centers, centers[1:], strict=False)}
    assert len(gaps) <= 1 and centers == sorted(centers)


def test_rows_are_stacked_in_output_order_at_a_fixed_pitch():
    layout = flow_layout(P4X8)
    assert [layout.outputs[o].top for o in range(8)] == [o * (ROW_H + ROW_GAP) for o in range(8)]


@pytest.mark.parametrize("profile", [M2X4HD, P4X8], ids=["2x4", "4x8"])
def test_folded_outputs_share_one_line_after_the_rows(profile):
    folded = {profile.n_outputs - 2, profile.n_outputs - 1}
    layout = flow_layout(profile, folded)
    assert set(layout.outputs) == set(range(profile.n_outputs)) - folded
    assert layout.fold is not None and layout.fold.height == FOLD_H
    assert layout.fold.top >= max(s.bottom for s in layout.outputs.values()) + ROW_GAP
    _no_overlap([*layout.outputs.values(), layout.fold])
    # The "shown" state keeps every row and the line with the Hide button.
    shown = flow_layout(profile, (), fold_line=True)
    assert sorted(shown.outputs) == list(range(profile.n_outputs)) and shown.fold is not None
    assert shown.height > layout.height


def test_more_inputs_than_rows_sets_the_height():
    tall = DeviceProfile(product_name="8x1 (test)", n_inputs=8, n_outputs=1, sources=("Analog",))
    layout = flow_layout(tall)
    assert layout.height == 8 * IN_H + 7 * 12
    _no_overlap(layout.inputs)


def test_a_route_line_runs_from_the_input_to_just_short_of_the_row():
    d = route_path(42, 120)
    assert d.startswith("M0 42 C")
    assert d.endswith(f"{ROUTE_W - flow.ARROW} 120")


# --- blocks -----------------------------------------------------------------------------------------------------------


def _shakers(**kwargs):
    return run(walkthrough(), **kwargs).outputs[0]


def test_blocks_come_in_design_order_with_their_states_and_links():
    blocks = output_blocks(_shakers(), root="/mdsp")
    assert [b.key for b in blocks] == list(BLOCK_ORDER[:6])
    got = {b.key: (b.label, b.state, b.href) for b in blocks}
    assert got == {
        "level": ("Level +16 ▲max", "caution", "/mdsp/outputs/1#gain-output-0"),
        "band": ("Band 20–80 Hz", "on", "/mdsp/outputs/1?tab=crossover"),
        "eq": ("EQ 1 cut", "on", "/mdsp/outputs/1?tab=peq"),
        "delay": ("Delay 8.75", "on", "/mdsp/outputs/1?tab=delay"),
        "polarity": ("Polarity 0°", "default", "/mdsp/outputs/1#invert-0"),
        "compressor": ("Comp. off", "default", "/mdsp/outputs/1?tab=compressor"),
    }
    by_key = {b.key: b for b in blocks}
    assert by_key["level"].glyph == "⚠"
    assert by_key["polarity"].glyph == ""


def test_block_names_say_value_state_and_what_opens():
    names = {b.key: b.aria_label for b in output_blocks(_shakers())}
    assert names["band"] == "Band, 20 to 80 hertz, on. Open band settings."
    assert names["level"] == "Level, +16 dB total, at its limit, check. Go to its level."
    assert names["delay"] == "Delay, 8.75 milliseconds, on. Open delay settings."
    assert names["polarity"] == "Polarity, 0 degrees, off. Go to its polarity switch."
    assert names["compressor"] == "Compressor, off, off. Open compressor settings."
    assert names["eq"] == "EQ, 1 cut, on. Open EQ settings."


def test_a_problem_block_is_red_with_its_word():
    data = base()
    route(data, 0, 0)
    data["outputs"][0]["crossover"] = [xo("hp", 100), xo("lp", 80)]
    band = next(b for b in output_blocks(run(data).outputs[0]) if b.key == "band")
    assert (band.label, band.state, band.glyph) == ("Band: nothing passes", "problem", "⛔")
    assert band.aria_label.startswith("Band, nothing passes, problem.")


def test_unsent_blocks_get_a_corner_glyph():
    statuses = {
        "outputs.0.delay_ms": "pending",
        "outputs.0.crossover.0": "failed",
        "inputs.0.routing.0.gain_db": "unknown",
        "outputs.0.peq.0": "synced",
    }
    blocks = {b.key: b for b in output_blocks(_shakers(statuses=statuses))}
    assert (blocks["delay"].sync, blocks["delay"].sync_glyph) == ("pending", "⟳")
    assert (blocks["band"].sync, blocks["band"].sync_glyph) == ("failed", "⚠")
    assert (blocks["level"].sync, blocks["level"].sync_glyph) == ("unknown", "?")  # a route level is part of Level
    assert blocks["eq"].sync is None and blocks["eq"].sync_glyph == ""
    assert blocks["delay"].aria_label.endswith("on, sending. Open delay settings.")
    # Pending writes held for a preset that isn't playing are "Waiting".
    staged = {b.key: b for b in output_blocks(_shakers(statuses=statuses, preset=1, staged=True), staged=True)}
    assert (staged["delay"].sync, staged["delay"].sync_glyph) == ("waiting", "⏸")


def test_block_sync_picks_the_worst_status():
    assert block_sync("level", {"level": "pending", "sources": "failed"}, False) == "failed"
    assert block_sync("eq", {"eq": "synced"}, False) is None
    assert block_sync("eq", {}, False) is None
    assert block_sync("delay", {"delay": "pending"}, True) == "waiting"
    assert block_sync("band", {"band": "changed"}, True) == "changed"


def test_on_home_the_level_block_scrolls_to_the_card():
    assert block_href("level", 1, "/r", level_anchor="output-card-") == "#output-card-2"
    assert block_href("eq", 1, "/r", level_anchor="output-card-") == "/r/outputs/2?tab=peq"


def test_fir_shows_as_a_block_only_when_loaded():
    assert "fir" not in [b.key for b in output_blocks(_shakers())]


# --- the whole diagram ------------------------------------------------------------------------------------------------


def test_the_walkthrough_draws_the_silent_sub_and_folds_out_3_and_4():
    view = build_flow(run(walkthrough(), UNUSED), M2X4HD, root="/r")
    sub = view["outputs"][1]
    assert sub["stub"] is not None and sub["stub"].code == "no_route"
    assert sub["route_fix"].label == "Route In 1 → Sub"
    assert sub["route_fix"].values == {"inputs.0.routing.1.enabled": True}
    assert sub["route_fix"].undo == {"inputs.0.routing.1.enabled": False}
    assert [f.href for f in sub["stub_fixes"] if f.href] == ["/r/routing"]
    assert view["fold"]["label"] == "Out 3 · Out 4" and view["fold"]["muted"] is True
    folded, shown = view["drawings"]["folded"], view["drawings"]["open"]
    # In 1 → Shakers, and one shared line into the folded Out 3 · Out 4 (both at 0 dB, so it keeps its label).
    assert [(line["input"], line["label"]) for line in folded["lines"]] == [(0, "+4 dB"), (0, "0 dB")]
    assert len(shown["lines"]) == 3
    assert [s["output"] for s in folded["stubs"]] == [1]
    assert view["unrouted"] == [sub]
    assert view["outputs"][0]["sources"] == [
        {"input": 0, "name": "In 1", "level": "+4 dB", "muted": False, "href": "/r/routing#in1-out1"}
    ]
    in2 = view["inputs"][1]
    assert in2["note"].code == "input_unused" and in2["details"] == "EQ off"
    assert [f.label for f in in2["note_fixes"]] == ["Open Routing", "Mark In 2 not in use"]


def test_without_unused_outputs_there_is_one_state():
    view = build_flow(run(walkthrough()), M2X4HD)
    assert list(view["drawings"]) == ["folded"] and view["fold"] is None
    assert len(view["drawings"]["folded"]["lines"]) == 3
    out3 = view["outputs"][2]
    assert out3["info"].code == "muted_routed"
    assert out3["info_short"] == "Muted, but In 1 is sent here"
    assert [f.values for f in out3["info_fixes"] if f.values] == [{"outputs.2.mute": False}]


def test_a_route_from_a_muted_input_is_labelled_muted():
    data = walkthrough()
    data["inputs"][0]["mute"] = True
    view = build_flow(run(data, UNUSED), M2X4HD)
    first = view["drawings"]["folded"]["lines"][0]
    assert first["muted"] is True and first["label"] == "(In 1 muted)"
    shakers = view["outputs"][0]
    assert [f.label for f in shakers["badge_fixes"]] == ["Unmute In 1"]
    assert view["inputs"][0]["muted"] is True


def test_a_4x8_profile_draws_every_route():
    profile = P4X8
    doc = default_config_doc(profile).model_dump()
    for i in range(4):
        for o in range(8):
            doc["inputs"][i]["routing"][o] = {"enabled": (o % 4) == i, "gain_db": 0.0}
    summary = summarize(ConfigDoc.model_validate(doc), profile, {}, None, {}, None)
    view = build_flow(summary, profile)
    assert len(view["inputs"]) == 4 and len(view["outputs"]) == 8
    assert len(view["drawings"]["folded"]["lines"]) == 8
    assert view["drawings"]["folded"]["height"] == f"{8 * ROW_H + 7 * ROW_GAP:g}"


# --- rendering --------------------------------------------------------------------------------------------------------


def _render(view, **extra) -> str:
    ctx = {
        "flow": view,
        "flow_preset": 0,
        "flow_master_mute": False,
        "flow_edit_url": "/ui/devices/1/presets/0/edit",
        "root": "",
        **extra,
    }
    return templates.get_template("partials/flow.html").render(ctx)


def test_the_svg_is_decorative_and_routes_are_text_once():
    html = _render(build_flow(run(walkthrough(), UNUSED), M2X4HD))
    svgs = re.findall(r"<svg[^>]*>", html)
    assert svgs and all('aria-hidden="true"' in s for s in svgs)
    outside = re.sub(r"<svg.*?</svg>", "", html, flags=re.S)
    assert outside.count("+4 dB") == 1  # "Fed by In 1 at +4 dB"
    # Links inside the SVG are for the mouse only.
    for link in re.findall(r"<svg.*?</svg>", html, flags=re.S):
        assert all('tabindex="-1"' in a for a in re.findall(r"<a [^>]*>", link))


def test_rows_and_input_boxes_are_sections_with_headings():
    html = _render(build_flow(run(walkthrough(), UNUSED), M2X4HD))
    assert html.count('<section class="flow-row') == 4
    assert html.count('<section class="flow-in') == 2
    assert html.count('role="toolbar"') == 4
    assert 'aria-label="Settings on Shakers"' in html
    # One tab stop per row of blocks.
    for toolbar in re.findall(r'role="toolbar".*?</div>', html, flags=re.S):
        assert toolbar.count('tabindex="0"') == 1
    assert "title=" not in html


def test_the_popover_ids_are_unique_and_match_their_buttons():
    html = _render(build_flow(run(walkthrough(), UNUSED), M2X4HD))
    ids = re.findall(r' id="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    for target in re.findall(r'aria-controls="([^"]+)"', html):
        assert target in ids


def test_without_state_it_says_so():
    html = _render(None)
    assert "appears once the app has loaded" in html


async def test_the_dashboard_and_the_fragment_render_without_a_loaded_session(http):
    page = (await http.get("/")).text
    assert 'class="card flow-card"' in page and "/static/js/flow.js" in page and "/static/css/flow.css" in page
    fragment = await http.get("/ui/flow")
    assert fragment.status_code == 200
    assert fragment.headers["cache-control"] == "no-store"
    assert "appears once the app has loaded" in fragment.text


async def test_the_diagram_refreshes_after_a_device_check(http):
    # The ≠ glyphs come from the last check (app/sync/status.py), which only publishes a "verify" event.
    page = (await http.get("/")).text
    assert "sse:verify delay:300ms" in page.split("data-flow-box", 1)[1].split(">", 1)[0]
    assert "topics=levels%2Cmaster%2Cdevice%2Csync%2Cdesired%2Cverify" in page


async def test_the_fragment_only_takes_a_plain_level_anchor(http):
    response = await http.get("/ui/flow", params={"level_anchor": 'x"><script>'})
    assert response.status_code == 200
    assert "<script>" not in response.text
