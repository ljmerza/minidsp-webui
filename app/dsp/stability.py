"""Biquad stability checks (docs 03 §5).

Device convention: ``H(z) = (b0 + b1·z⁻¹ + b2·z⁻²) / (1 − a1·z⁻¹ − a2·z⁻²)``. The poles are the roots of
``z² − a1·z − a2``; they lie strictly inside the unit circle exactly when ``|a2| < 1`` and ``|a1| < 1 − a2``.

The device stores coefficients as float32, so a biquad sitting right on the edge can be stable in float64 and
unstable once rounded. Both representations are checked.
"""

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

_KEYS = ("b0", "b1", "b2", "a1", "a2")

type BiquadLike = Sequence[float] | Mapping[str, float] | Any


class UnstableBiquadError(ValueError):
    """The biquad is unstable, non-finite or malformed."""


def _coefficients(biquad: BiquadLike) -> tuple[float, float, float, float, float]:
    if isinstance(biquad, Mapping):
        values = [biquad[k] for k in _KEYS]
    elif hasattr(biquad, "as_tuple"):
        values = list(biquad.as_tuple())
    else:
        values = list(biquad)
    if len(values) != 5:
        raise UnstableBiquadError(f"a biquad has 5 coefficients (b0 b1 b2 a1 a2), got {len(values)}")
    try:
        return tuple(float(v) for v in values)  # type: ignore[return-value]
    except (TypeError, ValueError) as exc:
        raise UnstableBiquadError(f"biquad coefficients must be numbers: {exc}") from None


def _float32(values: Sequence[float]) -> list[float]:
    with np.errstate(over="ignore"):
        return [float(x) for x in np.array(values, dtype=np.float32)]


def _triangle(a1: float, a2: float) -> bool:
    return abs(a2) < 1.0 and abs(a1) < 1.0 - a2


def poles_stable(a1: float, a2: float) -> bool:
    """True when the poles of ``1 − a1·z⁻¹ − a2·z⁻²`` are strictly inside the unit circle, in float64 and float32."""
    if not (math.isfinite(a1) and math.isfinite(a2)):
        return False
    a1_32, a2_32 = _float32([a1, a2])
    return _triangle(a1, a2) and _triangle(a1_32, a2_32)


def pole_radius(a1: float, a2: float) -> float:
    """Largest pole magnitude (< 1 means stable)."""
    return float(np.max(np.abs(np.roots([1.0, -a1, -a2]))))


def biquad_problem(biquad: BiquadLike) -> str | None:
    """Why the biquad can't be sent to the device, or None when it's fine."""
    try:
        coeffs = _coefficients(biquad)
    except UnstableBiquadError as exc:
        return str(exc)
    if not all(math.isfinite(c) for c in coeffs):
        return "biquad has a non-finite coefficient"
    if not all(math.isfinite(c) for c in _float32(coeffs)):
        return "biquad coefficient overflows float32"
    _, _, _, a1, a2 = coeffs
    if not poles_stable(a1, a2):
        return f"unstable biquad: pole radius {pole_radius(a1, a2):.6g} (needs |a2| < 1 and |a1| < 1 - a2)"
    return None


def is_stable(biquad: BiquadLike) -> bool:
    return biquad_problem(biquad) is None


def check_biquad(biquad: BiquadLike) -> None:
    """Raise `UnstableBiquadError` unless the biquad is finite and stable."""
    problem = biquad_problem(biquad)
    if problem is not None:
        raise UnstableBiquadError(problem)
