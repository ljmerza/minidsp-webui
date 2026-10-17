"""Placing REW filters in a PEQ bank or crossover, and choosing what to export (pure, no session)."""

from pathlib import Path

import pytest

from app.dsp.design import highpass2, lowpass2, peaking
from app.formats.rew import (
    Bank,
    RewImportError,
    bank_biquads,
    bank_patch,
    cleared_groups,
    parse_bank,
    parse_rew,
    place_crossover,
    place_peq,
)
from app.model.config_doc import default_config_doc, set_path
from app.model.device_profile import M2X4HD
from app.model.filters import PASSTHROUGH

FIXTURE = parse_rew((Path(__file__).parents[2] / "fixtures" / "rew-filters.txt").read_text()).biquads
OUT1_PEQ = Bank("outputs", 1, "peq")
OUT0_XO = Bank("outputs", 0, "crossover")
LP = lowpass2(80, 0.7071067811865476)
HP = highpass2(20, 0.7071067811865476)


@pytest.fixture
def doc():
    return default_config_doc(M2X4HD)


def test_parse_bank():
    assert parse_bank("outputs.1.peq", M2X4HD) == OUT1_PEQ
    assert parse_bank(" inputs.0.peq ", M2X4HD) == Bank("inputs", 0, "peq")
    assert parse_bank("outputs.3.crossover", M2X4HD).path == "outputs.3.crossover"
    for bad, message in [
        ("outputs.1", "unknown bank"),
        ("outputs.4.peq", "output 4 outside 0..3"),
        ("inputs.2.peq", "input 2 outside 0..1"),
        ("inputs.0.crossover", "only outputs have a crossover"),
        ("outputs.-1.peq", "unknown bank"),
    ]:
        with pytest.raises(RewImportError, match=message):
            parse_bank(bad, M2X4HD)


def test_peq_all_fills_in_order_unbypasses_and_recognizes(doc):
    changed, warnings = place_peq(doc, M2X4HD, OUT1_PEQ, FIXTURE, "all")
    assert warnings == []
    assert sorted(changed) == list(range(10))
    assert [changed[k].compiled for k in range(10)] == FIXTURE
    assert all(band.bypass is False for band in changed.values())
    assert {band.design.type for band in changed.values()} == {"peaking"}
    assert round(changed[0].design.freq) == 106 and changed[0].design.gain_db < 0


def test_peq_all_clears_the_remaining_bands(doc):
    doc = set_path(doc, "outputs.1.peq.5.compiled", peaking(1000, 1, -3))
    changed, _ = place_peq(doc, M2X4HD, OUT1_PEQ, FIXTURE[:3], "all")
    assert sorted(changed) == list(range(10))
    assert [changed[k].compiled for k in range(3)] == FIXTURE[:3]
    assert all(changed[k].compiled == PASSTHROUGH and changed[k].design.type == "passthrough" for k in range(3, 10))
    assert all(not band.bypass for band in changed.values())


def test_peq_one_band_leaves_the_others_alone(doc):
    changed, _ = place_peq(doc, M2X4HD, OUT1_PEQ, [FIXTURE[4]], 7)
    assert list(changed) == [7]
    assert changed[7].compiled == FIXTURE[4] and changed[7].bypass is False


def test_peq_keep_bypass(doc):
    changed, _ = place_peq(doc, M2X4HD, OUT1_PEQ, FIXTURE, "all", unbypass=False)
    assert all(band.bypass for band in changed.values())


def test_peq_too_many_filters(doc):
    with pytest.raises(RewImportError, match="10 filters don't fit in outputs.1.peq.2") as exc:
        place_peq(doc, M2X4HD, OUT1_PEQ, FIXTURE, 2)
    assert exc.value.problems == ["import into 'all' or pick one filter of the file"]
    with pytest.raises(RewImportError, match="11 filters don't fit"):
        place_peq(doc, M2X4HD, OUT1_PEQ, [*FIXTURE, FIXTURE[0]], "all")


def test_trailing_pass_through_filters_are_dropped(doc):
    changed, warnings = place_peq(doc, M2X4HD, OUT1_PEQ, [*FIXTURE, PASSTHROUGH, PASSTHROUGH], "all")
    assert [changed[k].compiled for k in range(10)] == FIXTURE
    assert warnings == ["2 trailing pass-through filter(s) dropped: outputs.1.peq (all bands) is full"]
    changed, warnings = place_peq(doc, M2X4HD, OUT1_PEQ, [FIXTURE[0], PASSTHROUGH], 0)
    assert changed[0].compiled == FIXTURE[0] and len(warnings) == 1


def test_peq_errors(doc):
    with pytest.raises(RewImportError, match="PEQ band 10 outside 0..9"):
        place_peq(doc, M2X4HD, OUT1_PEQ, FIXTURE[:1], 10)
    with pytest.raises(RewImportError, match="no filters"):
        place_peq(doc, M2X4HD, OUT1_PEQ, [], "all")
    with pytest.raises(RewImportError, match="not a PEQ bank"):
        place_peq(doc, M2X4HD, OUT0_XO, FIXTURE, "all")
    external = set_path(doc, "outputs.1.peq.3.external", True)
    with pytest.raises(RewImportError, match="read-only") as exc:
        place_peq(external, M2X4HD, OUT1_PEQ, FIXTURE, "all")
    assert exc.value.problems == ["outputs.1.peq.3"]
    assert place_peq(external, M2X4HD, OUT1_PEQ, FIXTURE[:1], 0)[0][0].compiled == FIXTURE[0]


def test_input_bank(doc):
    changed, _ = place_peq(doc, M2X4HD, Bank("inputs", 0, "peq"), FIXTURE[:2], "all")
    assert changed[1].compiled == FIXTURE[1] and len(changed) == M2X4HD.peq_per_input


def test_crossover_all_all_holds_eight_biquads(doc):
    eight = [LP, LP, PASSTHROUGH, PASSTHROUGH, HP, HP, PASSTHROUGH, PASSTHROUGH]
    changed, warnings = place_crossover(doc, M2X4HD, OUT0_XO, eight, "all", "all")
    assert warnings == []
    assert changed[0].compiled == eight[:4] and changed[1].compiled == eight[4:]
    assert (changed[0].design.kind, changed[0].design.family, changed[0].design.order) == ("lp", "lr", 4)
    assert (changed[1].design.kind, changed[1].design.order) == ("hp", 4)
    assert not changed[0].bypass and not changed[1].bypass
    with pytest.raises(RewImportError, match="9 filters don't fit in outputs.0.crossover group all biquad all"):
        place_crossover(doc, M2X4HD, OUT0_XO, [*eight, LP], "all", "all")


def test_crossover_group_fills_and_clears_its_slots(doc):
    doc = set_path(doc, "outputs.0.crossover.0.compiled", [HP, HP, HP, HP])
    changed, _ = place_crossover(doc, M2X4HD, OUT0_XO, [LP, LP], 0, "all")
    assert list(changed) == [0]
    assert changed[0].compiled == [LP, LP, PASSTHROUGH, PASSTHROUGH]
    assert changed[0].design.freq == pytest.approx(80)


def test_crossover_single_slot_keeps_the_other_slots(doc):
    doc = set_path(doc, "outputs.0.crossover.1.compiled", [HP, HP, PASSTHROUGH, PASSTHROUGH])
    changed, _ = place_crossover(doc, M2X4HD, OUT0_XO, [LP], 1, 3)
    assert changed[1].compiled == [HP, HP, PASSTHROUGH, LP]
    assert changed[1].design is None  # a high- and low-pass of different orders: custom
    assert not changed[1].bypass


def test_crossover_one_slot_in_both_groups(doc):
    changed, _ = place_crossover(doc, M2X4HD, OUT0_XO, [LP, HP], "all", 2)
    assert changed[0].compiled[2] == LP and changed[1].compiled[2] == HP


def test_crossover_errors(doc):
    with pytest.raises(RewImportError, match="crossover group 2 outside 0..1"):
        place_crossover(doc, M2X4HD, OUT0_XO, [LP], 2, 0)
    with pytest.raises(RewImportError, match="crossover biquad 4 outside 0..3"):
        place_crossover(doc, M2X4HD, OUT0_XO, [LP], 0, 4)
    with pytest.raises(RewImportError, match="not a crossover"):
        place_crossover(doc, M2X4HD, OUT1_PEQ, [LP], 0, 0)
    with pytest.raises(RewImportError, match="no filters"):
        place_crossover(doc, M2X4HD, OUT0_XO, [], 0, 0)


def test_bank_patch_shape(doc):
    changed, _ = place_peq(doc, M2X4HD, OUT1_PEQ, FIXTURE[:1], 4)
    patch = bank_patch(OUT1_PEQ, changed)
    band = patch["outputs"]["1"]["peq"]["4"]
    assert set(band) == {"design", "compiled", "bypass"}
    assert band["compiled"] == FIXTURE[0].model_dump(mode="json") and band["bypass"] is False


def test_export_selection(doc):
    changed, _ = place_peq(doc, M2X4HD, OUT1_PEQ, FIXTURE, "all")
    for k, band in changed.items():
        doc = set_path(doc, f"outputs.1.peq.{k}", band)
    assert bank_biquads(doc, M2X4HD, OUT1_PEQ) == FIXTURE
    assert bank_biquads(doc, M2X4HD, OUT1_PEQ, 3) == [FIXTURE[3]]
    doc = set_path(doc, "outputs.1.peq.2.bypass", True)
    assert bank_biquads(doc, M2X4HD, OUT1_PEQ)[2] == PASSTHROUGH
    assert bank_biquads(doc, M2X4HD, OUT1_PEQ, include_bypassed=True)[2] == FIXTURE[2]

    doc = set_path(doc, "outputs.0.crossover.0.compiled", [LP, LP, PASSTHROUGH, PASSTHROUGH])
    doc = set_path(doc, "outputs.0.crossover.0.bypass", False)
    doc = set_path(doc, "outputs.0.crossover.1.compiled", [HP, HP, PASSTHROUGH, PASSTHROUGH])
    everything = bank_biquads(doc, M2X4HD, OUT0_XO)
    assert everything == [LP, LP, PASSTHROUGH, PASSTHROUGH] + [PASSTHROUGH] * 4  # group 2 is bypassed
    assert bank_biquads(doc, M2X4HD, OUT0_XO, 1, "all", include_bypassed=True) == [HP, HP, PASSTHROUGH, PASSTHROUGH]
    assert bank_biquads(doc, M2X4HD, OUT0_XO, "all", 1) == [LP, PASSTHROUGH]
    with pytest.raises(RewImportError):
        bank_biquads(doc, M2X4HD, OUT1_PEQ, 12)


def test_cleared_groups_flags_an_active_group_turned_into_pass_through(doc):
    doc = set_path(doc, "outputs.0.crossover.1.compiled.0", HP)
    doc = set_path(doc, "outputs.0.crossover.1.bypass", False)
    changed, _ = place_crossover(doc, M2X4HD, OUT0_XO, [LP, LP])
    assert cleared_groups(doc, OUT0_XO, changed) == [1]
    changed, _ = place_crossover(doc, M2X4HD, OUT0_XO, [LP, LP], 0)
    assert cleared_groups(doc, OUT0_XO, changed) == []
    bypassed = set_path(doc, "outputs.0.crossover.1.bypass", True)  # a bypassed group filters nothing
    changed, _ = place_crossover(bypassed, M2X4HD, OUT0_XO, [LP, LP])
    assert cleared_groups(bypassed, OUT0_XO, changed) == []
