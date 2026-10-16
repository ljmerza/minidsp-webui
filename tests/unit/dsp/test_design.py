import math

import numpy as np
import pytest

from app.dsp import design as d
from app.dsp.response import filter_response
from app.dsp.stability import is_stable
from app.model.filters import PASSTHROUGH, Biquad, CrossoverDesign, FilterDesign
from tests.unit.dsp.vectors_gen import GOLDEN_HP_20, GOLDEN_LP_80

FS = 96_000
SQRT_HALF = 1 / math.sqrt(2)


def mag_db(bqs, f: float) -> float:
    return float(filter_response(bqs if isinstance(bqs, list) else [bqs], [f], float32=False).magnitude_db[0])


def phase_deg(bqs, f: float) -> float:
    return float(filter_response(bqs if isinstance(bqs, list) else [bqs], [f], float32=False).phase_deg[0])


# --- golden vectors (docs 04 §8.1) --------------------------------------------------------------------------------


def test_golden_shaker_coefficients_are_bit_exact():
    assert list(d.lowpass2(80, SQRT_HALF).as_tuple()) == GOLDEN_LP_80
    assert list(d.highpass2(20, SQRT_HALF).as_tuple()) == GOLDEN_HP_20


def test_butterworth_q_formula_reproduces_golden_bits():
    q = d.butterworth_qs(2)[0]
    assert list(d.lowpass2(80, q).as_tuple()) == GOLDEN_LP_80


def test_lr4_group_is_two_golden_sections_then_passthrough():
    group = d.design_crossover(CrossoverDesign(kind="lp", family="lr", order=4, freq=80))
    assert [list(b.as_tuple()) for b in group[:2]] == [GOLDEN_LP_80, GOLDEN_LP_80]
    assert group[2:] == [PASSTHROUGH, PASSTHROUGH]


def test_from_rbj_negates_a1_a2():
    assert d.from_rbj(2, 4, 6, 2, 1, -0.5).as_tuple() == (1.0, 2.0, 3.0, -0.5, 0.25)


# --- RBJ recipes ---------------------------------------------------------------------------------------------------


def test_peaking_hits_gain_at_center():
    assert mag_db(d.peaking(1000, 1, 6), 1000) == pytest.approx(6.0, abs=1e-9)
    assert mag_db(d.peaking(42, 4, -3.5), 42) == pytest.approx(-3.5, abs=1e-9)
    assert mag_db(d.peaking(1000, 1, 6), 10) == pytest.approx(0.0, abs=0.01)


@pytest.mark.parametrize("gain", [-12.0, -3.0, 4.5, 12.0])
def test_low_shelf_ends_and_midpoint(gain):
    bq = d.low_shelf(200, SQRT_HALF, gain)
    assert mag_db(bq, 1) == pytest.approx(gain, abs=0.01)
    assert mag_db(bq, 40_000) == pytest.approx(0.0, abs=0.01)
    assert mag_db(bq, 200) == pytest.approx(gain / 2, abs=1e-9)


@pytest.mark.parametrize("gain", [-12.0, -3.0, 4.5, 12.0])
def test_high_shelf_ends_and_midpoint(gain):
    bq = d.high_shelf(2000, SQRT_HALF, gain)
    assert mag_db(bq, 1) == pytest.approx(0.0, abs=0.01)
    assert mag_db(bq, 47_999) == pytest.approx(gain, abs=0.01)
    assert mag_db(bq, 2000) == pytest.approx(gain / 2, abs=1e-9)


def test_shelf_slope_to_q():
    # S = 1 is the steepest monotonic shelf and gives Q = 1/sqrt(2) for any gain.
    assert d.shelf_q_from_slope(1, 6) == pytest.approx(SQRT_HALF)
    assert d.shelf_q_from_slope(1, -15) == pytest.approx(SQRT_HALF)
    assert d.shelf_q_from_slope(0.5, 6) < SQRT_HALF
    with pytest.raises(d.DesignError):
        d.shelf_q_from_slope(0, 6)
    with pytest.raises(d.DesignError):
        d.shelf_q_from_slope(10, 12)
    with pytest.raises(d.DesignError):
        d.shelf_q_from_slope(1, math.inf)


@pytest.mark.parametrize("q", [0.5, SQRT_HALF, 2.0])
def test_lp2_hp2_gain_at_corner_is_q(q):
    assert mag_db(d.lowpass2(300, q), 300) == pytest.approx(20 * math.log10(q), abs=1e-9)
    assert mag_db(d.highpass2(300, q), 300) == pytest.approx(20 * math.log10(q), abs=1e-9)
    assert mag_db(d.lowpass2(300, q), 1) == pytest.approx(0, abs=1e-3)
    assert mag_db(d.highpass2(300, q), 47_000) == pytest.approx(0, abs=0.01)


def test_bandpass_notch_allpass():
    assert mag_db(d.bandpass(500, 2), 500) == pytest.approx(0.0, abs=1e-9)
    assert mag_db(d.bandpass(500, 2), 5) < -40
    assert mag_db(d.notch(60, 10), 60) < -150
    assert mag_db(d.notch(60, 10), 1000) == pytest.approx(0.0, abs=0.01)
    for f in (10, 100, 1000, 20_000):
        assert mag_db(d.allpass2(100, 0.7), f) == pytest.approx(0.0, abs=1e-9)
    assert abs(phase_deg(d.allpass2(100, 0.7), 100)) == pytest.approx(180.0, abs=1e-6)


def test_first_order_sections():
    for bq in (d.lowpass1(120), d.highpass1(120), d.allpass1(120)):
        assert bq.b2 == 0 and bq.a2 == 0
    assert mag_db(d.lowpass1(120), 120) == pytest.approx(-3.0103, abs=1e-4)
    assert mag_db(d.highpass1(120), 120) == pytest.approx(-3.0103, abs=1e-4)
    assert mag_db(d.lowpass1(120), 0.01) == pytest.approx(0, abs=1e-6)
    for f in (10, 120, 10_000):
        assert mag_db(d.allpass1(120), f) == pytest.approx(0.0, abs=1e-9)
    assert phase_deg(d.allpass1(120), 120) == pytest.approx(-90.0, abs=1e-6)
    assert phase_deg(d.allpass1(120), 0.01) == pytest.approx(0.0, abs=0.01)


def test_linkwitz_transform():
    f0, q0, fp, qp = 40.0, 0.9, 20.0, 0.6
    bq = d.linkwitz_transform(f0, q0, fp, qp)
    assert mag_db(bq, 0.001) == pytest.approx(40 * math.log10(f0 / fp), abs=1e-6)
    assert mag_db(bq, 47_999) == pytest.approx(0.0, abs=1e-3)
    # Pre-warped at sqrt(f0·fp): the digital response equals the analog prototype exactly there.
    fc = math.sqrt(f0 * fp)
    s = 2j * math.pi * fc
    w0, wp = 2 * math.pi * f0, 2 * math.pi * fp
    analog = (s * s + w0 / q0 * s + w0 * w0) / (s * s + wp / qp * s + wp * wp)
    assert mag_db(bq, fc) == pytest.approx(20 * math.log10(abs(analog)), abs=1e-9)
    # The transformed driver (sealed box at f0/q0) ends up with the target's high-pass shape.
    for f in (10.0, 20.0, 50.0):
        s = 2j * math.pi * f
        driver = s * s / (s * s + w0 / q0 * s + w0 * w0)
        target = s * s / (s * s + wp / qp * s + wp * wp)
        got = mag_db(bq, f) + 20 * math.log10(abs(driver))
        assert got == pytest.approx(20 * math.log10(abs(target)), abs=0.01)


def test_bilinear_matches_rbj_lowpass():
    w = 2 * math.pi * 1000
    q = 0.9
    got = d.bilinear((0.0, 0.0, w * w), (1.0, w / q, w * w), 1000)
    assert np.allclose(got.as_tuple(), d.lowpass2(1000, q).as_tuple(), atol=1e-12)


def test_gain_and_passthrough():
    assert d.gain(-6).as_tuple() == pytest.approx((10 ** (-6 / 20), 0, 0, 0, 0))
    assert d.passthrough().as_tuple() == (1.0, 0.0, 0.0, 0.0, 0.0)


@pytest.mark.parametrize("gain_db", [121.0, -121.0, 1e6, -1e6])
def test_absurd_gains_are_design_errors(gain_db):
    for spec in (
        {"type": "gain", "gain_db": gain_db},
        {"type": "peaking", "freq": 1000, "q": 1, "gain_db": gain_db},
        {"type": "lowshelf", "freq": 100, "q": 0.7, "gain_db": gain_db},
        {"type": "highshelf", "freq": 100, "q": 0.7, "gain_db": gain_db},
    ):
        with pytest.raises(d.DesignError, match="outside"):
            d.design_filter(spec)


def test_arithmetic_errors_become_design_errors(monkeypatch):
    def overflow(*_args):
        raise OverflowError("Numerical result out of range")

    monkeypatch.setattr(d, "peaking", overflow)
    with pytest.raises(d.DesignError, match="out of range"):
        d.design_filter({"type": "peaking", "freq": 1000, "q": 1, "gain_db": 3})
    monkeypatch.setattr(d, "lowpass2", overflow)
    with pytest.raises(d.DesignError, match="out of range"):
        d.design_crossover({"kind": "lp", "family": "lr", "order": 4, "freq": 80})


def test_raw_accepts_stable_and_rejects_unstable():
    assert d.raw([1, 0, 0, 0.5, -0.25]).as_tuple() == (1, 0, 0, 0.5, -0.25)
    assert d.raw({"b0": 1, "b1": 0, "b2": 0, "a1": 0, "a2": 0}) == PASSTHROUGH
    with pytest.raises(d.DesignError, match="unstable"):
        d.raw([1, 0, 0, 1.5, -0.4])
    with pytest.raises(d.DesignError, match="5 coefficients"):
        d.raw([1, 0, 0])


@pytest.mark.parametrize(
    "call",
    [
        lambda: d.peaking(0, 1, 3),
        lambda: d.peaking(48_000, 1, 3),
        lambda: d.peaking(1000, 0, 3),
        lambda: d.peaking(1000, 1, math.nan),
        lambda: d.lowpass1(-5),
        lambda: d.linkwitz_transform(40, 0.7, 20, -1),
        lambda: d.gain(math.inf),
    ],
)
def test_bad_parameters_raise(call):
    with pytest.raises(d.DesignError):
        call()


ALL_DESIGNS = [
    {"type": "peaking", "freq": 1000, "q": 1, "gain_db": 6},
    {"type": "lowshelf", "freq": 100, "q": 0.7, "gain_db": 3},
    {"type": "highshelf", "freq": 5000, "q": 0.7, "gain_db": -3},
    {"type": "lp1", "freq": 100},
    {"type": "hp1", "freq": 100},
    {"type": "lp2", "freq": 100, "q": 0.7},
    {"type": "hp2", "freq": 100, "q": 0.7},
    {"type": "bandpass", "freq": 100, "q": 0.7},
    {"type": "notch", "freq": 100, "q": 0.7},
    {"type": "ap1", "freq": 100},
    {"type": "ap2", "freq": 100, "q": 0.7},
    {"type": "lt", "lt": {"f0": 40, "q0": 0.9, "fp": 20, "qp": 0.6}},
    {"type": "gain", "gain_db": -3},
    {"type": "passthrough"},
    {"type": "raw", "raw": {"b0": 0.5, "b1": 0, "b2": 0, "a1": 0, "a2": 0}},
]


@pytest.mark.parametrize("spec", ALL_DESIGNS, ids=lambda s: s["type"])
def test_design_filter_dispatches_every_type(spec):
    from_dict = d.design_filter(spec)
    from_model = d.design_filter(FilterDesign.model_validate(spec))
    assert from_dict == from_model
    assert is_stable(from_dict)


def test_design_filter_errors():
    with pytest.raises(d.DesignError, match="requires"):
        d.design_filter({"type": "peaking", "freq": 100})
    with pytest.raises(d.DesignError, match="Nyquist"):
        d.design_filter({"type": "lp2", "freq": 60_000, "q": 0.7})
    with pytest.raises(d.DesignError, match="unstable"):
        d.design_filter({"type": "raw", "raw": {"b0": 1, "b1": 0, "b2": 0, "a1": 0, "a2": 1.0}})


def test_design_filter_rejects_designs_unstable_in_float32():
    # A very low, very sharp notch: fine in float64, its poles land on the unit circle once rounded.
    with pytest.raises(d.DesignError, match="unstable"):
        d.design_filter({"type": "notch", "freq": 0.01, "q": 100})


# --- crossovers ---------------------------------------------------------------------------------------------------

BW_TABLE = {
    2: [0.7071],
    3: [1.0],
    4: [0.5412, 1.3066],
    5: [0.6180, 1.6180],
    6: [0.5176, 0.7071, 1.9319],
    7: [0.5550, 0.8019, 2.2470],
    8: [0.5098, 0.6013, 0.9000, 2.5629],
}


@pytest.mark.parametrize("order", range(2, 9))
def test_butterworth_q_table(order):
    assert [round(q, 4) for q in d.butterworth_qs(order)] == BW_TABLE[order]


def test_butterworth_orders_out_of_range():
    assert d.butterworth_qs(1) == []
    with pytest.raises(d.DesignError):
        d.butterworth_qs(9)


def test_linkwitz_riley_sections():
    assert d.linkwitz_riley_qs(2) == [0.5]
    assert d.linkwitz_riley_qs(4) == pytest.approx([SQRT_HALF, SQRT_HALF])
    assert d.linkwitz_riley_qs(6) == pytest.approx([0.5, 1.0, 1.0])
    assert d.linkwitz_riley_qs(8) == pytest.approx([0.5412, 0.5412, 1.3066, 1.3066], abs=1e-4)
    with pytest.raises(d.DesignError):
        d.linkwitz_riley_qs(3)


@pytest.mark.parametrize(
    ("family", "order", "used"),
    [("bw", n, math.ceil(n / 2)) for n in range(1, 9)]
    + [("lr", 2, 1), ("lr", 4, 2), ("lr", 6, 3), ("lr", 8, 4)]
    + [("bessel", n, math.ceil(n / 2)) for n in range(2, 9)],
)
def test_biquads_used(family, order, used):
    for kind in ("lp", "hp"):
        xo = CrossoverDesign(kind=kind, family=family, order=order, freq=100)
        assert d.biquads_needed(xo) == used
        group = d.design_crossover(xo)
        assert len(group) == 4
        assert group[used:] == [PASSTHROUGH] * (4 - used)
        assert all(not b.is_passthrough for b in group[:used])


@pytest.mark.parametrize("order", range(1, 9))
def test_butterworth_is_3db_down_at_fc(order):
    for kind in ("lp", "hp"):
        group = d.design_crossover(CrossoverDesign(kind=kind, family="bw", order=order, freq=250))
        assert mag_db(group, 250) == pytest.approx(-3.0103, abs=1e-4)


@pytest.mark.parametrize("order", [2, 4, 6, 8])
def test_linkwitz_riley_is_6db_down_at_fc(order):
    for kind in ("lp", "hp"):
        group = d.design_crossover(CrossoverDesign(kind=kind, family="lr", order=order, freq=250))
        assert mag_db(group, 250) == pytest.approx(-6.0206, abs=1e-4)


@pytest.mark.parametrize("order", range(1, 9))
def test_crossover_slope_is_6db_per_octave_per_order(order):
    lp = d.design_crossover(CrossoverDesign(kind="lp", family="bw", order=order, freq=100))
    assert mag_db(lp, 400) - mag_db(lp, 800) == pytest.approx(6.02 * order, abs=0.3)


def test_bandpass_group():
    xo = CrossoverDesign(kind="bp", family="lr", order=4, freq=20, freq_hi=80)
    group = d.design_crossover(xo)
    assert [list(b.as_tuple()) for b in group] == [GOLDEN_HP_20, GOLDEN_HP_20, GOLDEN_LP_80, GOLDEN_LP_80]


def test_crossover_too_big_for_group():
    with pytest.raises(d.DesignError, match="needs 8 biquads"):
        d.design_crossover({"kind": "bp", "family": "lr", "order": 8, "freq": 20, "freq_hi": 80})
    with pytest.raises(d.DesignError, match="needs 2 biquads"):
        d.design_crossover({"kind": "lp", "family": "bw", "order": 4, "freq": 80}, n_biquads=1)


def test_crossover_bad_input():
    with pytest.raises(d.DesignError):
        d.design_crossover({"kind": "lp", "family": "lr", "order": 3, "freq": 80})
    with pytest.raises(d.DesignError, match="Nyquist"):
        d.design_crossover({"kind": "lp", "family": "bw", "order": 2, "freq": 50_000})
    with pytest.raises(d.DesignError, match="unknown crossover family"):
        d.prototype_sections("lp", "cheby", 2, 100, "phase")
    with pytest.raises(d.DesignError):
        d.prototype_sections("lp", "bessel", 1, 100, "phase")


def test_bessel_gate(monkeypatch):
    xo = CrossoverDesign(kind="lp", family="bessel", order=4, freq=100)
    monkeypatch.setattr(d, "BESSEL_VERIFIED", False)
    with pytest.raises(d.DesignError, match="Bessel"):
        d.design_crossover(xo)
    assert len(d.design_crossover(xo, allow_unverified=True)) == 4
    monkeypatch.setattr(d, "BESSEL_VERIFIED", True)
    assert len(d.design_crossover(xo)) == 4


def test_section_biquad_kinds():
    assert d.Section("lp1", 100).biquad() == d.lowpass1(100)
    assert d.Section("hp1", 100).biquad() == d.highpass1(100)
    assert d.Section("lp2", 100, 0.6).biquad() == d.lowpass2(100, 0.6)
    assert d.Section("hp2", 100, 0.6).biquad() == d.highpass2(100, 0.6)


def test_designs_are_biquad_models():
    assert isinstance(d.peaking(1000, 1, 3), Biquad)
