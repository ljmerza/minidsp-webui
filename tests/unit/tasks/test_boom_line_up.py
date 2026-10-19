"""Fix a boom (design §14.2) and Line up shakers (§14.5): band choice, width ranges, the preview, the delay maths."""

import pytest

from app.model.config_doc import ConfigDoc, PeqBand, default_config_doc
from app.model.device_profile import M2X4HD
from app.model.filters import Biquad, FilterDesign
from app.sync.filters import compile_band
from app.web.task_eq_cut import (
    WIDTHS,
    about_hz,
    done_text,
    free_band,
    free_bands,
    half_depth_range,
    new_band,
    plan_text,
    preview,
    reading,
    width_text,
)
from app.web.task_eq_cut import Answers as BoomAnswers
from app.web.task_line_up import guess_pair, line_up, result_lines, to_metres
from tests.unit.sync.test_summary import NAMES, base, meta, run, walkthrough, xo


def band(freq: float = 40.0, gain_db: float = -4.0, *, bypass: bool = False, external: bool = False) -> PeqBand:
    design = FilterDesign(type="peaking", freq=freq, q=4.0, gain_db=gain_db)
    return compile_band(PeqBand(), design=design, bypass=bypass).model_copy(update={"external": external})


# --- Fix a boom ---------------------------------------------------------------------------------------------------


def test_free_band_is_the_first_unused_one():
    bands = [band(), PeqBand(), PeqBand()]
    assert free_band(bands).index == 1 and free_band(bands).replaces is None
    assert free_bands(bands) == [1, 2]


def test_a_factory_reset_band_counts_as_unused():
    # A factory-reset 2x4HD reports switched-off bands with all-zero coefficients.
    zero = Biquad(b0=0.0, b1=0.0, b2=0.0, a1=0.0, a2=0.0)
    factory = PeqBand(bypass=True, design=FilterDesign(type="raw", raw=zero), compiled=zero)
    found = free_band([band(), factory])
    assert found.index == 1 and found.replaces is None
    # Switched on, an all-zero band silences the output: it is in use (the health rules point it out), not free.
    assert free_band([band(), factory.model_copy(update={"bypass": False})]) is None


def test_a_switched_off_band_is_used_only_when_no_band_is_unused():
    bands = [band(), band(63.0, bypass=True)] + [band(30.0 + k) for k in range(8)]
    found = free_band(bands)
    assert found.index == 1 and found.replaces == "EQ cut at 63 Hz"
    # An unused band later on wins over a switched-off one earlier.
    bands[5] = PeqBand()
    assert free_band(bands).index == 5


def test_all_ten_in_use_leaves_nothing_to_pick():
    bands = [band(30.0 + k) for k in range(10)]
    assert free_band(bands) is None and free_bands(bands) == []
    # A band managed by another tool is never used, even when it is unused or switched off.
    bands[3] = PeqBand(external=True)
    bands[4] = band(bypass=True, external=True)
    assert free_band(bands) is None


@pytest.mark.parametrize(
    ("q", "low", "high"),
    [(8.0, 37.6, 42.5), (4.0, 35.4, 45.3), (1.4, 28.2, 56.7), (0.71, 20.8, 77.0)],
)
def test_half_depth_ranges_match_the_design_copy(q, low, high):
    # Design §13.3 computed these for a 4 dB cut at 40 Hz.
    lo, hi = half_depth_range(40.0, q, -4.0)
    assert lo == pytest.approx(low, abs=0.15) and hi == pytest.approx(high, abs=0.15)


def test_width_and_reading_words():
    texts = [width_text(w, 40.0, -4.0) for w in WIDTHS]
    assert texts == [
        "Narrow (Q 8): about 38–43 Hz",
        "Medium (Q 4): about 35–45 Hz, good for booms",
        "Wide (Q 1.4): about 28–57 Hz",
    ]
    assert reading(40.0, 4.0, -4.0) == "Lowers about 35–45 Hz, deepest −4 dB at 40 Hz."
    assert reading(1000.0, 4.0, 3.0).startswith("Raises about ") and reading(1000.0, 4.0, 3.0).endswith(
        "most +3 dB at 1 kHz."
    )
    assert [about_hz(f) for f in (37.6, 42.5, 133.0, 1130.0)] == [38.0, 43.0, 130.0, 1100.0]


def test_plan_and_done_words():
    a = BoomAnswers(1, 40.0, "cut", 4.0, "medium")
    free = free_band([band(), PeqBand()])
    assert plan_text("Sub", a, free) == [
        "Sub: cut 4 dB at 40 Hz, medium width (Q 4).",
        "Uses EQ band 2 (free), switched on.",
        "Nothing else changes.",
    ]
    replaced = free_band([band(), band(63.0, bypass=True)] + [band() for _ in range(8)])
    assert "its old setting (EQ cut at 63 Hz) is replaced" in plan_text("Sub", a, replaced)[1]
    assert (
        done_text("Sub", [band(), band(40.0)], 1) == "Sub now has 2 EQ cuts: −4 dB at 40 Hz (new) and −4 dB at 40 Hz."
    )
    assert done_text("Sub", [band(25.0, 3.0), band(40.0)], 1) == (
        "Sub now has 2 EQ bands: −4 dB at 40 Hz (new) and +3 dB at 25 Hz."
    )


def test_preview_draws_now_and_with_the_cut_on_the_sub_range():
    out = default_config_doc(M2X4HD).outputs[1]
    out.peq[0] = band()  # a cut at 40 Hz already
    graph = preview(out, 1, new_band(out.peq[1], 40.0, 4.0, -4.0), 40.0)
    assert graph["range"] == "5–300 Hz"
    assert graph["now"] != graph["new"]
    assert [t["label"] for t in graph["x_ticks"]] == ["10", "20", "50", "100", "200"]
    assert [t["label"] for t in graph["y_ticks"]] == ["−12", "−6", "0", "6"]
    # A high note gets the full range.
    assert preview(out, 1, new_band(out.peq[1], 2000.0, 4.0, -4.0), 2000.0)["range"] == "20 Hz–20 kHz"


# --- Line up --------------------------------------------------------------------------------------------------------


def test_three_metres_is_8_75_ms():
    result = line_up(0.0, 0.0, 0.0, 3.0, M2X4HD)
    assert result.target == "felt" and result.samples == 840 and result.delay_ms == 8.75 and result.error is None
    lines = result_lines("Shakers", "Sub", result, 0.0, 0.0)
    assert lines == [
        "Sound from Sub takes 8.75 ms to reach you.",
        "Shakers will wait 8.75 ms (840 samples) so both arrive together. Sub keeps its 0 ms.",
    ]


def test_the_heard_ones_own_delay_adds_up():
    # delay(felt) = delay(heard) + (d_heard - d_felt) / 343 m/s
    result = line_up(0.0, 2.0, 0.5, 3.0, M2X4HD)
    assert result.exact_ms == pytest.approx(2.0 + 2.5 / 343 * 1000)
    assert result.delay_ms == pytest.approx(round(result.exact_ms * 96) / 96)


def test_a_negative_result_delays_the_heard_one_instead():
    result = line_up(0.0, 0.0, 4.0, 1.0, M2X4HD)
    assert result.target == "heard" and result.samples == 840
    assert "Shakers is farther from you than Sub, so Sub waits instead." in result_lines("Shakers", "Sub", result, 0, 0)


def test_beyond_79_8_ms_is_refused_with_the_reason():
    result = line_up(0.0, 0.0, 0.0, 30.0, M2X4HD)
    assert result.error is not None and "at most 79.8 ms" in result.error and "about 27 m" in result.error
    assert result_lines("Shakers", "Sub", result, 0, 0)[-1] == result.error
    # Just inside the limit is fine.
    assert line_up(0.0, 0.0, 0.0, 27.0, M2X4HD).error is None


def test_feet():
    assert to_metres(10.0, "ft") == pytest.approx(3.048)
    assert to_metres(3.0, "m") == 3.0


def _pair(data, names):
    summary = run(data, names, preset=0, master={"preset": 0, "volume": -30.0, "mute": False})
    docs = list(ConfigDoc.model_validate(data).outputs)
    return guess_pair(summary.outputs, docs)


def test_defaults_come_from_the_tactile_flag_then_names_then_bands():
    data = walkthrough()
    assert _pair(data, NAMES) == (0, 1)  # "Shakers" and "Sub"
    flagged = {**NAMES, ("output", 2): meta("output", 2, "Rear", tactile=True)}
    assert _pair(data, flagged)[0] == 2
    # No names: Out 1 plays 20-80 Hz (a band-pass, so felt), Out 2 has only a low-pass (so heard).
    plain = base()
    plain["outputs"][0]["crossover"] = [xo("hp", 20), xo("lp", 80)]
    plain["outputs"][1]["crossover"] = [xo("lp", 80), {**xo("lp", 80), "bypass": True}]
    assert _pair(plain, {}) == (0, 1)
    # Nothing to go on: no felt guess, the heard one is the first output.
    assert _pair(base(), {}) == (None, 0)
