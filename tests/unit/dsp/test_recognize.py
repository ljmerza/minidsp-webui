import math
import random

import numpy as np
import pytest

from app.dsp import design as d
from app.dsp import recognize as rc
from app.model.config_doc import default_config_doc
from app.model.device_profile import M2X4HD
from app.model.filters import PASSTHROUGH, Biquad, CrossoverDesign, FilterDesign
from tests.unit.dsp.vectors_gen import GOLDEN_HP_20, GOLDEN_LP_80, REW_FILTERS

SQRT_HALF = 1 / math.sqrt(2)


def f32(bq: Biquad) -> list[float]:
    return [float(x) for x in np.array(bq.as_tuple(), dtype=np.float32)]


# --- single biquads -----------------------------------------------------------------------------------------------


def test_golden_lp_recovers_design_value():
    # docs 04 §5 worked check: a2 = −0.992622543 → α = 0.0037024 → f0 = 80 Hz, Q = 0.7071.
    r = -GOLDEN_LP_80[4]
    assert (1 - r) / (1 + r) == pytest.approx(0.0037024, abs=1e-7)
    for coeffs, kind, freq in ((GOLDEN_LP_80, "lp2", 80.0), (GOLDEN_HP_20, "hp2", 20.0)):
        got = rc.recognize_biquad(coeffs)
        assert (got.type, got.freq) == (kind, freq)
        assert got.q == pytest.approx(SQRT_HALF, abs=1e-15)
        assert list(d.design_filter(got).as_tuple()) == coeffs


ROUND_TRIPS = [
    FilterDesign(type="peaking", freq=1000.0, q=1.0, gain_db=6.0),
    FilterDesign(type="peaking", freq=42.0, q=4.0, gain_db=-3.5),
    FilterDesign(type="peaking", freq=15000.0, q=0.5, gain_db=12.0),
    FilterDesign(type="lowshelf", freq=100.0, q=0.7, gain_db=-4.5),
    FilterDesign(type="lowshelf", freq=35.0, q=1.2, gain_db=9.0),
    FilterDesign(type="highshelf", freq=3000.0, q=0.9, gain_db=3.0),
    FilterDesign(type="highshelf", freq=8000.0, q=0.5, gain_db=-6.0),
    FilterDesign(type="lp1", freq=123.4),
    FilterDesign(type="hp1", freq=30.0),
    FilterDesign(type="ap1", freq=200.0),
    FilterDesign(type="lp2", freq=2000.0, q=2.0),
    FilterDesign(type="hp2", freq=25.0, q=0.9),
    FilterDesign(type="bandpass", freq=500.0, q=2.0),
    FilterDesign(type="notch", freq=60.0, q=10.0),
    FilterDesign(type="ap2", freq=100.0, q=0.7),
    FilterDesign(type="gain", gain_db=-6.0),
    FilterDesign(type="passthrough"),
]


@pytest.mark.parametrize("design", ROUND_TRIPS, ids=lambda x: rc.describe_filter(x))
def test_round_trip(design):
    assert rc.recognize_biquad(d.design_filter(design)) == design


@pytest.mark.parametrize("design", ROUND_TRIPS, ids=lambda x: rc.describe_filter(x))
def test_round_trip_through_float32(design):
    # What a device readback / CLI script with float32 values looks like.
    assert rc.recognize_biquad(f32(d.design_filter(design))).type == design.type


def test_unsnapped_parameters_survive():
    design = FilterDesign(type="peaking", freq=1234.5678, q=3.21987, gain_db=-7.654321)
    got = rc.recognize_biquad(d.design_filter(design))
    assert got.type == "peaking"
    assert rc.coeffs_match(d.design_filter(got), d.design_filter(design))


def test_random_designs_round_trip():
    rng = random.Random(1234)
    builders = {
        "peaking": lambda f, q, g: d.peaking(f, q, g),
        "lowshelf": lambda f, q, g: d.low_shelf(f, q, g),
        "highshelf": lambda f, q, g: d.high_shelf(f, q, g),
        "lp2": lambda f, q, g: d.lowpass2(f, q),
        "hp2": lambda f, q, g: d.highpass2(f, q),
        "bandpass": lambda f, q, g: d.bandpass(f, q),
        "notch": lambda f, q, g: d.notch(f, q),
        "ap2": lambda f, q, g: d.allpass2(f, q),
    }
    for _ in range(300):
        kind = rng.choice(list(builders))
        f = 10 ** rng.uniform(1, 4.3)
        q = 10 ** rng.uniform(-0.5, 1)
        g = rng.choice([-1, 1]) * rng.uniform(0.5, 15)
        bq = builders[kind](f, q, g)
        got = rc.recognize_biquad(bq)
        assert got.type == kind, (kind, f, q, g)
        assert rc.coeffs_match(d.design_filter(got), bq)


def test_linkwitz_transform_stays_raw():
    bq = d.linkwitz_transform(40, 0.9, 20, 0.6)
    got = rc.recognize_biquad(bq)
    assert got.type == "raw"
    assert got.raw == bq


def test_unrecognizable_and_odd_inputs_stay_raw():
    assert rc.recognize_biquad([0.3, 0.1, 0.05, 0.2, -0.1]).type == "raw"
    assert rc.recognize_biquad([-1.0, 0, 0, 0, 0]).type == "raw"  # inverted gain isn't a design
    assert rc.recognize_biquad([0.5, 0.2, 0, 1.5, 0]).type == "raw"  # first order, pole outside
    assert rc.recognize_biquad([1, 0, 0, 0, 1.5]).type == "raw"


def test_non_finite_raises():
    with pytest.raises(ValueError, match="non-finite"):
        rc.recognize_biquad([math.nan, 0, 0, 0, 0])


def test_tolerance_is_absolute_and_relative():
    # Same denominator, numerator scaled by 1.05 (+0.42 dB): every b differs by < 1e-6 absolute, but it isn't an LP2.
    scaled = [GOLDEN_LP_80[0] * 1.05, GOLDEN_LP_80[1] * 1.05, GOLDEN_LP_80[2] * 1.05, *GOLDEN_LP_80[3:]]
    assert max(abs(a - b) for a, b in zip(scaled, GOLDEN_LP_80, strict=True)) < 1e-6
    assert rc.recognize_biquad(scaled).type == "raw"


def test_coeffs_match_rules():
    assert rc.coeffs_match([1, 0, 0, 0, 0], PASSTHROUGH)
    assert not rc.coeffs_match([1, 1e-9, 0, 0, 0], PASSTHROUGH)
    assert rc.coeffs_match([1.000001, 0, 0, 0, 0], PASSTHROUGH)
    assert not rc.coeffs_match([1.00002, 0, 0, 0, 0], PASSTHROUGH)
    assert not rc.coeffs_match([math.inf, 0, 0, 0, 0], PASSTHROUGH)


def test_rew_fixture_is_96k_peaking():
    labels = [rc.describe_filter(rc.recognize_biquad(c)) for c in REW_FILTERS]
    assert all(label.startswith("PEQ ") for label in labels)
    first = rc.recognize_biquad(REW_FILTERS[0])
    assert first.freq == 106.0


# --- crossover groups ---------------------------------------------------------------------------------------------


def test_shaker_groups_label_as_lr4():
    pad = [PASSTHROUGH, PASSTHROUGH]
    lp = rc.recognize_crossover(
        [Biquad(**dict(zip(["b0", "b1", "b2", "a1", "a2"], GOLDEN_LP_80, strict=True)))] * 2 + pad
    )
    hp = rc.recognize_crossover([GOLDEN_HP_20, GOLDEN_HP_20, *pad])
    assert rc.describe_crossover(lp) == "LR4 LP 80 Hz"
    assert rc.describe_crossover(hp) == "LR4 HP 20 Hz"
    assert lp == CrossoverDesign(kind="lp", family="lr", order=4, freq=80.0)


CASES = [("bw", n) for n in range(1, 9)] + [("lr", n) for n in (2, 4, 6, 8)] + [("bessel", n) for n in range(2, 9)]


@pytest.mark.parametrize(("family", "order"), CASES)
@pytest.mark.parametrize("kind", ["lp", "hp"])
def test_crossover_round_trip_any_order(family, order, kind):
    design = CrossoverDesign(kind=kind, family=family, order=order, freq=125.0)
    biquads = d.design_crossover(design)
    random.Random(order).shuffle(biquads)
    assert rc.recognize_crossover(biquads) == design


def test_crossover_from_float32_values():
    design = CrossoverDesign(kind="lp", family="bw", order=5, freq=70.0)
    assert rc.recognize_crossover([f32(b) for b in d.design_crossover(design)]) == design


def test_bandpass_group():
    design = CrossoverDesign(kind="bp", family="lr", order=4, freq=20.0, freq_hi=80.0)
    assert rc.recognize_crossover(d.design_crossover(design)) == design
    assert rc.describe_crossover(design) == "LR4 BP 20 Hz–80 Hz"


def test_unrecognized_groups():
    assert rc.recognize_crossover([PASSTHROUGH] * 4) is None
    assert rc.recognize_crossover([d.peaking(100, 1, 3), PASSTHROUGH]) is None
    # LP and HP of different families.
    mixed = d.design_crossover(CrossoverDesign(kind="lp", family="bw", order=2, freq=80))[:1]
    mixed += d.design_crossover(CrossoverDesign(kind="hp", family="lr", order=4, freq=20))[:2]
    assert rc.recognize_crossover(mixed) is None
    # LP below the HP isn't a band-pass.
    inverted = [d.lowpass2(20, SQRT_HALF), d.highpass2(80, SQRT_HALF)]
    assert rc.recognize_crossover(inverted) is None
    # Two first-order sections can't be any family here (LR merges them into one Q 0.5 biquad).
    assert rc.recognize_crossover([d.lowpass1(100), d.lowpass1(100)]) is None
    # LP2 sections at unrelated frequencies/Qs.
    assert rc.recognize_crossover([d.lowpass2(100, 0.6), d.lowpass2(300, 2.0)]) is None


def test_bessel_skipped_when_not_included():
    design = CrossoverDesign(kind="lp", family="bessel", order=4, freq=100.0)
    biquads = d.design_crossover(design)
    assert rc.recognize_crossover(biquads, include_bessel=False) is None


def test_describe_labels():
    assert rc.describe_filter(FilterDesign(type="peaking", freq=1000, q=1, gain_db=6)) == "PEQ 1 kHz Q 1 +6.0 dB"
    assert rc.describe_filter(FilterDesign(type="lowshelf", freq=100, q=0.707, gain_db=-3)) == (
        "Low shelf 100 Hz Q 0.707 -3.0 dB"
    )
    assert rc.describe_filter(FilterDesign(type="highshelf", freq=2500, q=0.5, gain_db=2)) == (
        "High shelf 2.5 kHz Q 0.5 +2.0 dB"
    )
    assert rc.describe_filter(FilterDesign(type="lp1", freq=80.5)) == "LP1 80.5 Hz"
    assert rc.describe_filter(FilterDesign(type="hp2", freq=20, q=SQRT_HALF)) == "HP2 20 Hz Q 0.707"
    assert rc.describe_filter(FilterDesign(type="bandpass", freq=500, q=2)) == "BP 500 Hz Q 2"
    assert rc.describe_filter(FilterDesign(type="notch", freq=60, q=10)) == "Notch 60 Hz Q 10"
    lt = FilterDesign.model_validate({"type": "lt", "lt": {"f0": 40, "q0": 0.9, "fp": 20, "qp": 0.6}})
    assert rc.describe_filter(lt) == "LT 40 Hz Q 0.9 → 20 Hz Q 0.6"
    assert rc.describe_filter(FilterDesign(type="gain", gain_db=-6)) == "Gain -6.0 dB"
    assert rc.describe_filter(FilterDesign()) == "Pass-through"
    assert rc.describe_filter(FilterDesign(type="raw", raw=PASSTHROUGH)) == "Raw"
    assert rc.describe_crossover(None) == "Custom"
    assert (
        rc.describe_crossover(CrossoverDesign(kind="hp", family="bessel", order=3, freq=25))
        == "Bessel3 HP 25 Hz (phase)"
    )
    mag = CrossoverDesign(kind="lp", family="bessel", order=4, freq=80, bessel_norm="mag")
    assert rc.describe_crossover(mag) == "Bessel4 LP 80 Hz (−3 dB)"
    assert rc.format_hz(12_345) == "12.3 kHz"


# --- whole documents ----------------------------------------------------------------------------------------------


def test_annotate_designs_on_adopted_doc():
    doc = default_config_doc(M2X4HD)
    out = doc.outputs[1]
    out.crossover[0].compiled = [Biquad(**dict(zip(["b0", "b1", "b2", "a1", "a2"], GOLDEN_LP_80, strict=True)))] * 2 + [
        PASSTHROUGH
    ] * 2
    out.crossover[1].compiled = [Biquad(**dict(zip(["b0", "b1", "b2", "a1", "a2"], GOLDEN_HP_20, strict=True)))] * 2 + [
        PASSTHROUGH
    ] * 2
    out.peq[0].compiled = d.peaking(42, 4, -3.5)
    out.peq[1].compiled = d.linkwitz_transform(40, 0.9, 20, 0.6)
    doc.inputs[0].peq[0].compiled = d.high_shelf(3000, 0.7, -2)
    before = doc.model_dump()

    got = rc.annotate_designs(doc)

    assert doc.model_dump() == before  # input untouched
    assert rc.describe_crossover(got.outputs[1].crossover[0].design) == "LR4 LP 80 Hz"
    assert rc.describe_crossover(got.outputs[1].crossover[1].design) == "LR4 HP 20 Hz"
    assert got.outputs[1].peq[0].design == FilterDesign(type="peaking", freq=42.0, q=4.0, gain_db=-3.5)
    assert got.outputs[1].peq[1].design.type == "raw"
    assert got.outputs[1].peq[2].design.type == "passthrough"
    assert got.inputs[0].peq[0].design.type == "highshelf"
    assert got.outputs[0].crossover[0].design is None
    for a, b in zip(got.outputs, doc.outputs, strict=True):
        assert [p.compiled for p in a.peq] == [p.compiled for p in b.peq]
        assert [g.compiled for g in a.crossover] == [g.compiled for g in b.crossover]
        assert [g.bypass for g in a.crossover] == [g.bypass for g in b.crossover]


def test_annotate_keeps_matching_designs():
    doc = default_config_doc(M2X4HD)
    lt = FilterDesign.model_validate({"type": "lt", "lt": {"f0": 40, "q0": 0.9, "fp": 20, "qp": 0.6}})
    doc.outputs[0].peq[0].design = lt
    doc.outputs[0].peq[0].compiled = d.design_filter(lt)
    raw = d.raw([0.3, 0.1, 0.05, 0.2, -0.1])
    doc.outputs[0].peq[1].design = FilterDesign(type="raw", raw=raw)
    doc.outputs[0].peq[1].compiled = raw
    xo = CrossoverDesign(kind="lp", family="lr", order=4, freq=70)
    doc.outputs[0].crossover[0].design = xo
    doc.outputs[0].crossover[0].compiled = list(reversed(d.design_crossover(xo)))
    # A stale design (compiled no longer matches) is replaced by recognition.
    doc.outputs[0].crossover[1].design = CrossoverDesign(kind="hp", family="lr", order=4, freq=30)
    doc.outputs[0].crossover[1].compiled = d.design_crossover(CrossoverDesign(kind="hp", family="bw", order=2, freq=30))
    # A design that can't even be built (too big for the group) is also re-recognized.
    doc.outputs[1].crossover[0].design = CrossoverDesign(kind="bp", family="lr", order=8, freq=20, freq_hi=80)

    got = rc.annotate_designs(doc)

    assert got.outputs[0].peq[0].design == lt
    assert got.outputs[0].peq[1].design.type == "raw"
    assert got.outputs[0].crossover[0].design == xo
    assert got.outputs[0].crossover[1].design == CrossoverDesign(kind="hp", family="bw", order=2, freq=30.0)
    assert got.outputs[1].crossover[0].design is None
