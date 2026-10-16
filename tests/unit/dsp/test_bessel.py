"""Bessel prototype checks backing `BESSEL_VERIFIED` (docs 04 §3, docs 09 U7).

Reference: Analog Devices MT-204 "The Bessel Response" (Rev. 0, 2/12), Figure 7 "Bessel Design Table", which lists
per-section F0 and Q for a -3 dB normalized low-pass. Its Q column fixes the pole *shape* of each order and its F0
ratios fix the relative section frequencies; both are independent of the normalization and match to the 4 published
digits. MT-204's absolute F0 scale is less precise (it varies 0.1-0.8% by order; at order 2, where the closed form
is easy, it says 1.2754 but the exact value is 1.2720), so the absolute normalizations are checked from their
definitions instead. The computed poles were also cross-checked against scipy.signal.besselap (all three norms,
orders 1-8, max |Δp| < 1e-12) in a throwaway environment; scipy is not a dependency.
"""

import math

import numpy as np
import pytest

from app.dsp import design as d
from app.dsp.response import filter_response
from app.model.filters import CrossoverDesign

# MT-204 Figure 7: (F0, Q) per section; Q is None for the real pole. Order 8 section 2's imaginary part is printed
# as "20044" (a typo for 2.0044) but F0 and Q are intact.
MT204 = {
    2: [(1.2754, 0.5771)],
    3: [(1.4524, 0.6910), (1.3270, None)],
    4: [(1.4192, 0.5219), (1.5912, 0.8055)],
    5: [(1.5611, 0.5635), (1.7607, 0.9165), (1.5069, None)],
    6: [(1.6060, 0.5103), (1.6913, 0.6112), (1.9071, 1.0234)],
    7: [(1.7174, 0.5324), (1.8235, 0.6608), (2.0507, 1.1262), (1.6853, None)],
    8: [(1.7838, 0.5060), (2.1953, 1.2258), (1.9591, 0.7109), (1.8378, 0.5597)],
}


def _sorted(sections):
    return sorted(sections, key=lambda s: -1.0 if s[1] is None else s[1])


@pytest.mark.parametrize("order", range(3, 9))
def test_section_qs_match_mt204(order):
    ours = d.bessel_sections(order, "mag")
    ref = _sorted(MT204[order])
    assert [q is None for _, q in ours] == [q is None for _, q in ref]
    for (_, q), (_, q_ref) in zip(ours, ref, strict=True):
        if q is not None:
            assert q == pytest.approx(q_ref, abs=1.5e-4)


@pytest.mark.parametrize("order", range(3, 9))
def test_relative_section_frequencies_match_mt204(order):
    ours = d.bessel_sections(order, "mag")
    ref = _sorted(MT204[order])
    for (w, _), (w_ref, _) in zip(ours, ref, strict=True):
        assert w / ours[-1][0] == pytest.approx(w_ref / ref[-1][0], rel=2e-4)


def test_order2_closed_form():
    # θ2(s) = s² + 3s + 3. |H(jω)|² = 9 / ((3 − ω²)² + 9ω²) = 1/2 at ω² = (√45 − 3)/2.
    assert d.reverse_bessel_coefficients(2) == [1, 3, 3]
    w3 = math.sqrt((math.sqrt(45) - 3) / 2)
    [(w_mag, q)] = d.bessel_sections(2, "mag")
    assert q == pytest.approx(1 / math.sqrt(3), abs=1e-12)
    assert w_mag == pytest.approx(math.sqrt(3) / w3, abs=1e-12)
    [(w_phase, _)] = d.bessel_sections(2, "phase")
    assert w_phase == pytest.approx(1.0, abs=1e-12)
    [(w_delay, _)] = d.bessel_sections(2, "delay")
    assert w_delay == pytest.approx(math.sqrt(3), abs=1e-12)


def test_reverse_bessel_polynomials():
    assert d.reverse_bessel_coefficients(1) == [1, 1]
    assert d.reverse_bessel_coefficients(3) == [1, 6, 15, 15]
    assert d.reverse_bessel_coefficients(4) == [1, 10, 45, 105, 105]


def _analog_lp(poles, w):
    return np.prod(-poles / (1j * w - poles))


@pytest.mark.parametrize("order", range(1, 9))
def test_mag_norm_is_3db_down_at_corner(order):
    h = _analog_lp(d.bessel_poles(order, "mag"), 1.0)
    assert 20 * math.log10(abs(h)) == pytest.approx(-3.0103, abs=1e-6)


@pytest.mark.parametrize("order", range(1, 9))
def test_phase_norm_poles_have_unit_geometric_mean(order):
    poles = d.bessel_poles(order, "phase")
    assert float(np.prod(np.abs(poles))) == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize("order", range(2, 9))
def test_phase_norm_is_near_midpoint_at_corner(order):
    poles = d.bessel_poles(order, "phase")
    # Summing per-pole phases gives the continuous (unwrapped) phase.
    lp = sum(math.degrees(np.angle(-p / (1j - p))) for p in poles)
    hp = sum(math.degrees(np.angle(1j / (1j - 1 / p))) for p in poles)
    assert lp == pytest.approx(-45 * order, abs=10.0)
    assert hp == pytest.approx(-lp, abs=1e-9)


@pytest.mark.parametrize("order", range(1, 9))
def test_delay_norm_has_unit_group_delay_at_dc(order):
    poles = d.bessel_poles(order, "delay")
    # Group delay of Π 1/(s − p) at ω→0 is Σ −1/p (real part).
    assert float(np.sum(-1 / poles).real) == pytest.approx(1.0, abs=1e-9)


def test_unknown_norm_and_order():
    with pytest.raises(d.DesignError):
        d.bessel_poles(4, "nope")  # type: ignore[arg-type]
    with pytest.raises(d.DesignError):
        d.bessel_poles(9)


@pytest.mark.parametrize("order", range(2, 9))
def test_digital_mag_norm_lowpass_is_3db_down(order):
    group = d.design_crossover(
        CrossoverDesign(kind="lp", family="bessel", order=order, freq=100), bessel_norm="mag", allow_unverified=True
    )
    db = filter_response(group, [100.0], float32=False).magnitude_db[0]
    assert db == pytest.approx(-3.0103, abs=0.01)


@pytest.mark.parametrize("order", range(2, 9))
def test_highpass_mirrors_lowpass(order):
    lp = d.design_crossover(CrossoverDesign(kind="lp", family="bessel", order=order, freq=100), allow_unverified=True)
    hp = d.design_crossover(CrossoverDesign(kind="hp", family="bessel", order=order, freq=100), allow_unverified=True)
    for x in (0.5, 0.8, 1.0, 1.5, 2.0):
        lp_db = filter_response(lp, [100.0 / x], float32=False).magnitude_db[0]
        hp_db = filter_response(hp, [100.0 * x], float32=False).magnitude_db[0]
        assert hp_db == pytest.approx(lp_db, abs=0.01)


def test_bessel_is_enabled():
    assert d.BESSEL_VERIFIED is True
