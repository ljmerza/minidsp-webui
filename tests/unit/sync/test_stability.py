import math

import numpy as np
import pytest

from app.dsp.stability import (
    UnstableBiquadError,
    biquad_problem,
    check_biquad,
    is_stable,
    pole_radius,
    poles_stable,
)
from app.model.filters import PASSTHROUGH, Biquad

# Shaker crossover on the device: LR4 80 Hz low-pass, first section (float32 readback, docs/spike-results.md 0.8).
LR4_LP_80 = (6.828594e-06, 1.3657188e-05, 6.828594e-06, 1.9925952, -0.99262255)


def test_known_device_biquads_are_stable():
    assert is_stable(LR4_LP_80)
    assert is_stable(PASSTHROUGH)
    assert is_stable(dict(zip(("b0", "b1", "b2", "a1", "a2"), LR4_LP_80, strict=True)))
    assert is_stable(Biquad(b0=0.5, b1=0.1, b2=0.2, a1=0.3, a2=-0.4))


@pytest.mark.parametrize(
    ("a1", "a2"),
    [
        (0.0, 1.0),  # poles on the unit circle
        (0.0, -1.0),
        (1.0, 0.0),  # pole at z = 1
        (-1.0, 0.0),
        (1.5, -0.5),  # |a1| == 1 - a2
        (2.0, -0.99),
        (0.0, 1.5),
    ],
)
def test_unstable_or_marginal_poles_are_rejected(a1, a2):
    assert not poles_stable(a1, a2)
    assert not is_stable((1.0, 0.0, 0.0, a1, a2))
    with pytest.raises(UnstableBiquadError, match="unstable"):
        check_biquad((1.0, 0.0, 0.0, a1, a2))


def test_triangle_matches_pole_radius():
    rng = np.random.default_rng(1234)
    checked = 0
    for a1, a2 in rng.uniform(-2.2, 2.2, size=(5000, 2)):
        radius = pole_radius(a1, a2)
        if abs(radius - 1.0) < 1e-6:
            continue  # too close to call in floating point
        assert poles_stable(a1, a2) == (radius < 1.0), (a1, a2, radius)
        checked += 1
    assert checked > 4900


def test_device_convention_signs():
    # Device form negates a1/a2: y[n] = ... + a1*y[n-1] + a2*y[n-2]. a1 = 1.9 / a2 = -0.95 is a stable resonator,
    # while the RBJ-signed version of it (a1 = -1.9, a2 = 0.95) is not stable in the device convention.
    assert poles_stable(1.9, -0.95)
    assert not poles_stable(-1.9, 0.95)


def test_float32_rounding_onto_the_unit_circle_is_unstable():
    a2 = -(1 - 1e-9)  # stable in float64, rounds to -1.0 in float32
    assert abs(a2) < 1
    assert float(np.float32(a2)) == -1.0
    assert not poles_stable(0.0, a2)


@pytest.mark.parametrize(
    ("coeffs", "message"),
    [
        ((math.nan, 0, 0, 0, 0), "non-finite"),
        ((1, 0, 0, math.inf, 0), "non-finite"),
        ((1e39, 0, 0, 0, 0), "float32"),
        ((1, 0, 0, 0), "5 coefficients"),
        (("x", 0, 0, 0, 0), "numbers"),
    ],
)
def test_malformed_biquads(coeffs, message):
    problem = biquad_problem(coeffs)
    assert problem is not None and message in problem
    assert not is_stable(coeffs)
    with pytest.raises(UnstableBiquadError):
        check_biquad(coeffs)


def test_check_biquad_passes_stable_input():
    check_biquad(LR4_LP_80)


def test_agrees_with_model_property_for_ordinary_values():
    rng = np.random.default_rng(99)
    for a1, a2 in rng.uniform(-2, 2, size=(500, 2)):
        if abs(pole_radius(a1, a2) - 1) < 1e-6:
            continue
        assert Biquad(a1=a1, a2=a2).is_stable == is_stable((1, 0, 0, a1, a2))
