"""The dB-weighted Volume slider (phase 9 design §10.3, friction P12).

The master volume runs from −127 to 0 dB, but everyday listening sits around −40…−10 dB, so a linear slider makes the
usable part a thin strip at the top (−30 dB looks like 75%). The slider instead gives −127…−60 dB the first 15% of the
track and −60…0 dB the rest, with ticks at −60, −40, −30, −20, −10 and 0, and shades the "typical" band from Settings.

The ``<input type=range>`` stays native: its value is a track position from 0 to ``POS_MAX`` and ``aria-valuetext``
says the dB. ``static/js/master.js`` (``mdsp.volumeScale``) does the same mapping in the browser; this mirror renders
the first position, the ticks and the band. tests/js/master.test.mjs and tests/unit/test_volume_scale.py pin both to
the same numbers.
"""

import math
from collections.abc import Sequence
from typing import Any

POS_MAX = 1000.0  # the range input's max (min is 0)
KNEE_DB = -60.0  # the bottom 15% of the track ends here
KNEE_AT = 0.15
TICKS_DB = (-60.0, -40.0, -30.0, -20.0, -10.0, 0.0)


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _kneed(low: float, high: float) -> bool:
    return low < KNEE_DB < high


def to_pos(db: float, low: float = -127.0, high: float = 0.0) -> float:
    """Where ``db`` sits on the track (0…POS_MAX, unrounded). A range that doesn't cross the knee is linear."""
    if not high > low or not math.isfinite(db):
        return 0.0
    v = _clamp(db, low, high)
    if not _kneed(low, high):
        return POS_MAX * (v - low) / (high - low)
    if v <= KNEE_DB:
        return POS_MAX * KNEE_AT * ((v - low) / (KNEE_DB - low))
    return POS_MAX * (KNEE_AT + (1 - KNEE_AT) * ((v - KNEE_DB) / (high - KNEE_DB)))


def from_pos(pos: float, low: float = -127.0, high: float = 0.0, step: float = 0.5) -> float:
    """The dB of a track position, on the device's step grid. Half steps round up, like ``Math.round`` in JS."""
    if not high > low:
        return low
    p = _clamp(pos if math.isfinite(pos) else 0.0, 0.0, POS_MAX) / POS_MAX
    if not _kneed(low, high):
        db = low + p * (high - low)
    elif p <= KNEE_AT:
        db = low + (p / KNEE_AT) * (KNEE_DB - low)
    else:
        db = KNEE_DB + ((p - KNEE_AT) / (1 - KNEE_AT)) * (high - KNEE_DB)
    if step > 0:
        db = math.floor(db / step + 0.5) * step
    return _clamp(db, low, high) + 0.0  # + 0.0: no −0


def percent(db: float, low: float, high: float) -> float:
    """``db``'s place on the track in percent (2 decimals), for CSS."""
    return round(to_pos(db, low, high) / POS_MAX * 100, 2)


def _tick_label(db: float) -> str:
    return "0" if db == 0 else f"−{abs(db):g}"


def slider_view(
    volume: float | None, low: float, high: float, typical: Sequence[float] | None = None
) -> dict[str, Any]:
    """What the header's slider needs: the first position, the ticks (``label``, ``at`` in %) and the typical band
    (``left`` / ``width`` in %, or None when it's empty or outside the range)."""
    ticks = [{"db": t, "label": _tick_label(t), "at": percent(t, low, high)} for t in TICKS_DB if low <= t <= high]
    band = None
    if typical is not None and len(typical) == 2:
        lo, hi = (_clamp(float(v), low, high) for v in typical)
        if hi > lo:
            left = percent(lo, low, high)
            band = {"left": left, "width": round(percent(hi, low, high) - left, 2), "low": lo, "high": hi}
    pos = to_pos(volume if volume is not None else low, low, high)
    return {"pos": round(pos, 3), "max": POS_MAX, "ticks": ticks, "band": band}


__all__ = ["KNEE_AT", "KNEE_DB", "POS_MAX", "TICKS_DB", "from_pos", "percent", "slider_view", "to_pos"]
