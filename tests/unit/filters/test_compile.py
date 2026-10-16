"""Pure band/group compilation: designs, raw coefficients, recognition, capacity and keep-stored rules."""

import random

import pytest

from app.dsp import design as d
from app.model.config_doc import CrossoverGroup, PeqBand, default_config_doc
from app.model.device_profile import M2X4HD
from app.model.filters import PASSTHROUGH, Biquad, CrossoverDesign, FilterDesign
from app.sync.filters import (
    FilterEditError,
    band_view,
    check_band_index,
    check_group_index,
    compile_band,
    compile_group,
    group_view,
    recognize_doc,
)
from app.sync.session import DesiredValidationError
from tests.unit.dsp.vectors_gen import GOLDEN_HP_20, GOLDEN_LP_80

LP = Biquad(**dict(zip(("b0", "b1", "b2", "a1", "a2"), GOLDEN_LP_80, strict=True)))
HP = Biquad(**dict(zip(("b0", "b1", "b2", "a1", "a2"), GOLDEN_HP_20, strict=True)))
LR4_LP_80 = CrossoverDesign(kind="lp", family="lr", order=4, freq=80)
LR4_HP_20 = CrossoverDesign(kind="hp", family="lr", order=4, freq=20)
UNSTABLE = [1.0, 0.0, 0.0, 1.5, -0.2]  # |a1| >= 1 - a2


def flat_group() -> CrossoverGroup:
    return CrossoverGroup(compiled=[PASSTHROUGH] * 4)


def test_error_is_a_validation_error():
    assert issubclass(FilterEditError, DesiredValidationError)


# --- bands --------------------------------------------------------------------------------------------------------


def test_band_from_design():
    design = FilterDesign(type="peaking", freq=1000, q=1, gain_db=6)
    band = compile_band(PeqBand(), design=design, bypass=False)
    assert band.design == design
    assert band.compiled == d.peaking(1000, 1, 6)
    assert band.bypass is False


def test_band_bypass_kept_when_omitted():
    current = PeqBand(bypass=False)
    band = compile_band(current, design=FilterDesign(type="lp2", freq=80, q=0.7071))
    assert band.bypass is False
    assert compile_band(PeqBand(), design=FilterDesign(type="lp1", freq=80)).bypass is True


def test_band_bypass_only():
    current = PeqBand(design=FilterDesign(type="peaking", freq=42, q=4, gain_db=-3.5), compiled=d.peaking(42, 4, -3.5))
    band = compile_band(current, bypass=False)
    assert band.design == current.design and band.compiled == current.compiled and band.bypass is False


def test_band_needs_something():
    with pytest.raises(FilterEditError, match="nothing to change"):
        compile_band(PeqBand())


def test_band_design_and_raw_conflict():
    with pytest.raises(FilterEditError, match="not both"):
        compile_band(PeqBand(), design=FilterDesign(type="passthrough"), raw=[1, 0, 0, 0, 0])


def test_band_raw_recognized_and_kept_exactly():
    coeffs = d.peaking(42, 4, -3.5)
    band = compile_band(PeqBand(), raw=list(coeffs.as_tuple()))
    assert band.compiled == coeffs
    assert band.design == FilterDesign(type="peaking", freq=42, q=4, gain_db=-3.5)


def test_band_raw_unrecognized_stays_raw():
    coeffs = [0.9, 0.1, 0.05, 0.3, -0.2]
    band = compile_band(PeqBand(), raw=coeffs)
    assert band.design.type == "raw" and band.design.raw.as_tuple() == tuple(coeffs)
    assert band.compiled.as_tuple() == tuple(coeffs)
    assert band_view(band)["label"] == "Raw"


def test_band_raw_via_design_type_raw():
    raw = Biquad(b0=0.9, b1=0.1, b2=0.05, a1=0.3, a2=-0.2)
    band = compile_band(PeqBand(), design=FilterDesign(type="raw", raw=raw))
    assert band.compiled == raw


@pytest.mark.parametrize("coeffs", [UNSTABLE, [1, 0, 0, 0, 1.0], [1, 0, 0, 0.5, 0.9999999999]])
def test_band_unstable_raw_refused(coeffs):
    with pytest.raises(FilterEditError) as info:
        compile_band(PeqBand(), raw=coeffs, where="outputs.0.peq.3")
    assert info.value.problems[0].startswith("outputs.0.peq.3.raw: ")
    assert "unstable" in info.value.problems[0]


def test_band_bad_raw_shape_refused():
    with pytest.raises(FilterEditError, match="5 coefficients"):
        compile_band(PeqBand(), raw=[1, 0, 0])


def test_band_impossible_design_refused():
    with pytest.raises(FilterEditError) as info:
        compile_band(PeqBand(), design=FilterDesign(type="lp2", freq=60_000, q=0.7), where="inputs.1.peq.0")
    assert "Nyquist" in info.value.problems[0] and info.value.problems[0].startswith("inputs.1.peq.0.design")


def test_band_redesign_keeps_near_identical_coefficients():
    exact = d.peaking(42, 4, -3.5)
    stored = Biquad(**{k: v * (1 + 1e-9) for k, v in exact.model_dump().items()})
    current = PeqBand(bypass=False, design=FilterDesign(type="raw", raw=stored), compiled=stored)
    band = compile_band(current, design=FilterDesign(type="peaking", freq=42, q=4, gain_db=-3.5))
    assert band.compiled is stored or band.compiled == stored
    assert band.design.type == "peaking"


def test_band_real_change_replaces_coefficients():
    current = compile_band(PeqBand(), design=FilterDesign(type="peaking", freq=42, q=4, gain_db=-3.5))
    band = compile_band(current, design=FilterDesign(type="peaking", freq=42, q=4, gain_db=-3.0))
    assert band.compiled == d.peaking(42, 4, -3.0)


# --- groups -------------------------------------------------------------------------------------------------------


def test_group_from_design_is_golden_and_padded():
    group = compile_group(flat_group(), 4, design=LR4_LP_80, bypass=False)
    assert group.compiled == [LP, LP, PASSTHROUGH, PASSTHROUGH]  # bit for bit
    assert group.design == LR4_LP_80 and group.bypass is False
    view = group_view(group)
    assert view["label"] == "LR4 LP 80 Hz" and view["biquads_used"] == 2


def test_group_redesign_identical_is_unchanged():
    current = compile_group(flat_group(), 4, design=LR4_HP_20, bypass=False)
    again = compile_group(current, 4, design=LR4_HP_20)
    assert again == current


def test_group_redesign_keeps_shuffled_order():
    designed = d.design_crossover(CrossoverDesign(kind="bp", family="bw", order=4, freq=20, freq_hi=80))
    shuffled = list(designed)
    random.Random(3).shuffle(shuffled)
    assert shuffled != designed
    current = CrossoverGroup(bypass=False, compiled=shuffled)
    group = compile_group(current, 4, design=CrossoverDesign(kind="bp", family="bw", order=4, freq=20, freq_hi=80))
    assert group.compiled == shuffled


def test_group_capacity_refused():
    with pytest.raises(FilterEditError) as info:
        compile_group(
            flat_group(), 4, design=CrossoverDesign(kind="bp", family="bw", order=6, freq=20, freq_hi=80), where="g"
        )
    assert "needs" in info.value.problems[0] and "the group has 4" in info.value.problems[0]
    with pytest.raises(FilterEditError, match="too many biquads"):
        compile_group(flat_group(), 4, biquads=[[1, 0, 0, 0, 0]] * 5)


def test_group_raw_recognized_as_lr4():
    group = compile_group(flat_group(), 4, biquads=[LP, list(LP.as_tuple())])
    assert group.compiled == [LP, LP, PASSTHROUGH, PASSTHROUGH]
    assert group.design == LR4_LP_80
    assert group_view(group)["label"] == "LR4 LP 80 Hz"
    hp = compile_group(flat_group(), 4, biquads=[HP, HP])
    assert group_view(hp)["label"] == "LR4 HP 20 Hz"


def test_group_raw_custom_and_flatten():
    custom = compile_group(flat_group(), 4, biquads=[d.peaking(50, 2, 3)])
    assert custom.design is None and group_view(custom)["label"] == "Custom"
    lr = compile_group(flat_group(), 4, design=LR4_LP_80, bypass=False)
    flat = compile_group(lr, 4, biquads=[])
    assert flat.compiled == [PASSTHROUGH] * 4 and flat.design is None and flat.bypass is False
    assert group_view(flat)["biquads_used"] == 0


def test_group_unstable_biquad_refused():
    with pytest.raises(FilterEditError) as info:
        compile_group(flat_group(), 4, biquads=[LP, UNSTABLE], where="outputs.0.crossover.1")
    assert info.value.problems[0].startswith("outputs.0.crossover.1.compiled.1: unstable")


def test_group_bypass_only_and_nothing():
    current = compile_group(flat_group(), 4, design=LR4_LP_80, bypass=False)
    toggled = compile_group(current, 4, bypass=True)
    assert toggled.bypass is True and toggled.compiled == current.compiled and toggled.design == current.design
    with pytest.raises(FilterEditError, match="nothing to change"):
        compile_group(current, 4)
    with pytest.raises(FilterEditError, match="not both"):
        compile_group(current, 4, design=LR4_LP_80, biquads=[])


# --- indexes and documents ----------------------------------------------------------------------------------------


def test_index_checks():
    assert check_band_index(M2X4HD, "outputs", 3, 9) == "outputs.3.peq.9"
    assert check_band_index(M2X4HD, "inputs", 1, 0) == "inputs.1.peq.0"
    assert check_group_index(M2X4HD, 0, 1) == "outputs.0.crossover.1"
    for args in [("inputs", 2, 0), ("outputs", 4, 0), ("outputs", 0, 10), ("outputs", -1, 0), ("bogus", 0, 0)]:
        with pytest.raises(FilterEditError):
            check_band_index(M2X4HD, *args)
    for args in [(4, 0), (0, 2), (0, -1)]:
        with pytest.raises(FilterEditError):
            check_group_index(M2X4HD, *args)


def test_recognize_doc_labels_shaker_groups():
    doc = default_config_doc(M2X4HD)
    doc.outputs[0].crossover[0].compiled = [LP, LP, PASSTHROUGH, PASSTHROUGH]
    doc.outputs[0].crossover[1].compiled = [HP, HP, PASSTHROUGH, PASSTHROUGH]
    doc.outputs[0].crossover[0].bypass = False
    got = recognize_doc(doc)
    assert got.outputs[0].crossover[0].design == LR4_LP_80
    assert got.outputs[0].crossover[1].design == LR4_HP_20
    assert got.outputs[0].crossover[0].compiled == doc.outputs[0].crossover[0].compiled
    assert got.outputs[0].crossover[0].bypass is False
    assert doc.outputs[0].crossover[0].design is None  # input untouched
