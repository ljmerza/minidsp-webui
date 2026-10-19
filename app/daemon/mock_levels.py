"""A synthetic meter pattern for the embedded mock device (``MINIDSPD_MOCK_LEVELS``; phase 9 design §17, PR 11).

minidspd's mock 2x4HD reports a flat −60 dBFS on every meter, so nothing that watches the meters (the "Something
wrong?" signal checks, Set levels, the CLIP lights) can be exercised on it. With a pattern set, the app replaces the
mock's readings in each level frame:

- ``signal``: In 1 carries a programme around −20 dBFS that swings ±4 dB every 4 s; the other inputs read no signal.
- ``hot``: the same around −6 dBFS, so an output with a total level of about +1 dB or more reaches CLIP.
- ``silent``: every input reads no signal (nothing playing).
- ``off`` (the default): the mock's own readings.

Each output follows the app's desired settings of the playing preset: the loudest enabled route from an unmuted input
with signal, plus input, route and output level; no signal when the output or Mute all is muted, or when its band or an
EQ band passes nothing. Volume, EQ shapes and the compressor are ignored (as the headroom sentence ignores them).

It only ever applies to the mock device (a ``mock:`` daemon URL); a real miniDSP's meters are never touched.
"""

import math
from collections.abc import Sequence
from typing import Any

PATTERNS = ("off", "signal", "hot", "silent")
NO_SIGNAL_DBFS = -120.0  # the meters' floor (static/js/meters.js)
_BASE_DBFS = {"signal": -20.0, "hot": -6.0}
SWING_DB = 4.0
PERIOD_S = 4.0


def input_levels(pattern: str, t: float, n_inputs: int) -> list[float]:
    """The inputs' readings at time ``t`` (seconds)."""
    base = _BASE_DBFS.get(pattern)
    levels = [NO_SIGNAL_DBFS] * n_inputs
    if base is not None and n_inputs:
        levels[0] = round(min(0.0, base + SWING_DB * math.sin(2 * math.pi * t / PERIOD_S)), 1)
    return levels


def _passes(out: Any) -> bool:
    from app.sync.summary import analyse_band, analyse_eq  # local: summary imports the model package

    band = analyse_band(out)
    return not (band.nothing_passes or band.empty or analyse_eq(out.peq).empty)


def output_levels(doc: Any, inputs: Sequence[float], *, master_mute: bool = False) -> list[float]:
    """The outputs' readings for the inputs' ``inputs`` and the desired document ``doc`` (a `ConfigDoc`)."""
    if doc is None:
        return []
    levels = []
    for n, out in enumerate(doc.outputs):
        if master_mute or out.mute or not _passes(out):
            levels.append(NO_SIGNAL_DBFS)
            continue
        best = NO_SIGNAL_DBFS
        for i, ch in enumerate(doc.inputs):
            cell = ch.routing[n] if n < len(ch.routing) else None
            if cell is None or not cell.enabled or ch.mute or i >= len(inputs) or inputs[i] <= NO_SIGNAL_DBFS:
                continue
            best = max(best, inputs[i] + ch.gain_db + cell.gain_db + out.gain_db)
        levels.append(round(min(0.0, max(NO_SIGNAL_DBFS, best)), 1))
    return levels


def pattern_levels(
    pattern: str, t: float, doc: Any, *, n_inputs: int, n_outputs: int, master_mute: bool = False
) -> tuple[list[float], list[float]] | None:
    """(inputs, outputs) for ``pattern`` at time ``t``; None for ``off`` (keep the device's own readings)."""
    if pattern not in PATTERNS or pattern == "off":
        return None
    inputs = input_levels(pattern, t, n_inputs)
    outputs = output_levels(doc, inputs, master_mute=master_mute) if doc is not None else []
    if len(outputs) != n_outputs:
        outputs = (outputs + [NO_SIGNAL_DBFS] * n_outputs)[:n_outputs]
    return inputs, outputs


__all__ = ["NO_SIGNAL_DBFS", "PATTERNS", "input_levels", "output_levels", "pattern_levels"]
