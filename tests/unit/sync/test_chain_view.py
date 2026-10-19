"""The output page's header block, chain chips and Level tab words (app/web/chain.py; phase 9 design §8)."""

from app.model.config_doc import ConfigDoc
from app.web import chain
from tests.unit.sync.test_summary import run, walkthrough, xo

TABS = [
    {"key": k, "label": label, "enabled": True}
    for k, label in (
        ("level", "Level"),
        ("crossover", "Band (crossover)"),
        ("peq", "EQ"),
        ("delay", "Delay"),
        ("polarity", "Polarity"),
        ("compressor", "Compressor (limiter)"),
        ("response", "Response"),
    )
]


def link(key: str) -> str:
    return f"/outputs/1?tab={key}"


def test_chips_show_each_settings_value_in_chain_order():
    shakers = run(walkthrough()).outputs[0]
    chips = chain.output_chips(shakers, TABS, link)
    assert [(c.key, c.label) for c in chips] == [
        ("level", "Level +16 ▲max"),
        ("crossover", "Band 20–80 Hz"),
        ("peq", "EQ 1 cut"),
        ("delay", "Delay 8.75 ms"),
        ("polarity", "Polarity 0°"),
        ("compressor", "Compressor off"),
        ("response", "Response"),
    ]
    assert chips[0].state == "caution" and chips[0].glyph == "⚠" and chips[0].spoken.startswith("check")
    assert chips[4].state == "default" and chips[1].href == "/outputs/1?tab=crossover"


def test_fir_chip_reads_off_until_a_filter_is_loaded():
    sub = run(walkthrough()).outputs[1]
    chips = chain.output_chips(sub, [*TABS, {"key": "fir", "label": "FIR", "enabled": True}], link)
    assert chips[-1].key == "fir" and chips[-1].label == "FIR off"


def test_fed_by_names_each_route_and_its_level():
    summary = run(walkthrough())
    assert chain.fed_by(summary.outputs[0]) == "Fed by In 1 at +4 dB"
    assert chain.fed_by(summary.outputs[1]) == "Fed by nothing"


def test_level_view_at_the_limit_offers_the_route():
    view = chain.level_view(run(walkthrough()).outputs[0], -72.0)
    assert view.at_limit
    assert view.raise_values == {"inputs.0.routing.0.gain_db": 5.0}
    assert view.raise_label == "Raise route In 1 → Shakers to +5 dB"
    assert view.totals == ("Total from In 1: In 1 level 0 + route +4 + output +12 = +16.0 dB",)


def test_level_view_with_everything_at_the_limit_says_so():
    data = walkthrough()
    data["inputs"][0]["routing"][0]["gain_db"] = 12.0
    view = chain.level_view(run(data).outputs[0], -72.0)
    assert view.at_limit and not view.raise_values
    assert "at its maximum" in view.max_note


def test_level_view_below_the_limit_and_without_a_route():
    summary = run(walkthrough())
    sub = chain.level_view(summary.outputs[1], -72.0)
    assert not sub.at_limit and sub.no_route == "Nothing is routed to Sub, so it plays nothing."


def test_badge_reason_leaves_out_what_the_headroom_line_says():
    shakers = run(walkthrough()).outputs[0]
    assert shakers.badge == "check" and chain.badge_reason(shakers) is None
    sub = run(walkthrough()).outputs[1]
    assert chain.badge_reason(sub) == "Sub is silent: no input is routed to it."


def test_bass_outputs_open_their_graphs_at_5_to_300_hz():
    data = walkthrough()
    doc = ConfigDoc.model_validate(data)
    assert chain.is_bass(doc.outputs[0], tactile=False)  # low-pass at 80 Hz
    assert not chain.is_bass(doc.outputs[1], tactile=False)  # full range
    assert chain.is_bass(doc.outputs[1], tactile=True)
    data["outputs"][1]["crossover"] = [xo("lp", 2000), xo("hp", 20)]
    assert not chain.is_bass(ConfigDoc.model_validate(data).outputs[1], tactile=False)
