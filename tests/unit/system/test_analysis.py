"""The pure integration analysis and multi-output helpers of app/api/v1/system.py."""

import math

import numpy as np
import pytest

from app.api.v1 import system as s
from app.dsp import design as d
from app.dsp import response as resp
from app.model.config_doc import ConfigDoc, CrossoverGroup, default_config_doc
from app.model.device_profile import M2X4HD
from app.model.filters import CrossoverDesign

LP80 = CrossoverDesign(kind="lp", family="lr", order=4, freq=80)
HP20 = CrossoverDesign(kind="hp", family="lr", order=4, freq=20)


def flat_pair() -> ConfigDoc:
    """Input 1 routed to Out 1 and Out 2, everything else flat."""
    doc = default_config_doc(M2X4HD)
    doc.inputs[0].routing[1].enabled = True
    return doc


def group(design: CrossoverDesign) -> CrossoverGroup:
    return CrossoverGroup(bypass=False, design=design, compiled=d.design_crossover(design, M2X4HD.crossover_biquads))


def shaker_setup(delay_ms: float = 9.0) -> ConfigDoc:
    """The live config of docs 01: Shakers LR4 80/20 + 12 dB + 9 ms on Out 1, a flat Sub on Out 2."""
    doc = flat_pair()
    shakers = doc.outputs[0]
    shakers.gain_db = 12.0
    shakers.delay_ms = delay_ms
    shakers.crossover = [group(LP80), group(HP20)]
    return doc


# --- default pair / parsing ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["Shakers", "Sub", "Out 3", "Out 4"], (0, 1)),
        (["Subwoofer", "Bass shakers", "Out 3"], (1, 0)),
        (["Out 1", "Tactile", "Out 3", "LFE sub"], (1, 3)),
        (["Out 1", "Out 2", "Out 3", "Out 4"], (0, 1)),
        (["Shakers", "Out 2", "Out 3"], (0, 1)),
        (["Only"], None),
    ],
)
def test_default_pair(names, expected):
    assert s.default_pair(names) == expected


def test_parse_pair():
    names = ["Shakers", "Sub", "Out 3", "Out 4"]
    assert s.parse_pair(None, 4, names) == (0, 1)
    assert s.parse_pair("  ", 4, names) == (0, 1)
    assert s.parse_pair("2,3", 4, names) == (2, 3)
    assert s.parse_pair("none", 4, names) is None
    for bad in ("1", "a,b", "0,1,2", "0,4", "-1,0", "2,2"):
        with pytest.raises(ValueError):
            s.parse_pair(bad, 4, names)


# --- element-wise helpers -------------------------------------------------------------------------------------------


def test_phase_difference_wraps_and_marks_silence():
    h_a = np.exp(1j * np.radians([0.0, 90.0, 170.0, -170.0, 10.0]))
    h_b = np.exp(1j * np.radians([0.0, -90.0, -170.0, 170.0, 0.0]))
    h_b[4] = 0
    dphi = s.phase_difference_deg(h_a, h_b)
    assert dphi[:4] == pytest.approx([0.0, 180.0, -20.0, 20.0])
    assert math.isnan(dphi[4])
    assert np.all((dphi[:4] > -180) & (dphi[:4] <= 180))


def test_level_and_deficit():
    h_a = np.array([1.0, 1.0, 2.0, 1.0, 0.0], dtype=complex)
    h_b = np.array([1.0, -1.0, 1.0j, 0.5, 1.0], dtype=complex)
    level = s.level_difference_db(h_a, h_b)
    deficit = s.summation_deficit_db(h_a, h_b)
    assert level[:4] == pytest.approx([0.0, 0.0, 20 * math.log10(2), 20 * math.log10(2)])
    assert deficit[0] == pytest.approx(0.0)
    assert deficit[1] == resp.DB_FLOOR  # exact cancellation, floored
    assert deficit[2] == pytest.approx(20 * math.log10(math.sqrt(5) / 3))
    assert deficit[3] == pytest.approx(0.0)
    assert math.isnan(level[4]) and math.isnan(deficit[4])


def test_regions_interpolate_edges_and_split_on_silence():
    freqs = np.array([10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0])
    dphi = np.array([0.0, 100.0, 140.0, 179.0, -170.0, np.nan, 150.0])
    regions = s.cancellation_regions(freqs, dphi, 120.0)
    assert len(regions) == 2
    first, second = regions
    # |Δφ| crosses 120 halfway between 20 Hz (100) and 30 Hz (140): log-interpolated
    assert first.f_lo == pytest.approx(math.exp((math.log(20) + math.log(30)) / 2))
    assert first.f_hi == pytest.approx(50.0)  # the next point is silent: the edge stays on the grid
    assert first.peak_freq == 40.0 and first.peak_dphi_deg == pytest.approx(179.0)
    assert first.worst_deficit_db is None and first.level_diff_db is None
    assert (second.f_lo, second.f_hi) == (70.0, 70.0)


def test_regions_cover_the_wrap_and_whole_range():
    freqs = np.geomspace(20, 100, 5)
    dphi = np.array([170.0, 179.0, -179.0, -175.0, 170.0])
    (region,) = s.cancellation_regions(freqs, dphi, 120.0, deficit_db=np.array([-5, -20, -30, -10, -1.0]))
    assert (region.f_lo, region.f_hi) == (20.0, 100.0)
    assert region.worst_deficit_db == -30.0
    assert s.cancellation_regions(freqs, np.zeros(5)) == []
    assert s.cancellation_regions(freqs, np.full(5, np.nan)) == []


# --- integration ----------------------------------------------------------------------------------------------------


def test_delay_moves_the_cancellation_band():
    """Two flat outputs, one delayed 9 ms (864 samples): Δφ = −360°·f·9 ms, so |Δφ| > 120° from 37.04 to 74.07 Hz."""
    doc = flat_pair()
    doc.outputs[0].delay_ms = 9.0
    result = s.integration(doc, 0, 1)
    assert result.freqs[0] == pytest.approx(20) and result.freqs[-1] == pytest.approx(100)
    assert len(result.freqs) == s.INTEGRATION_POINTS
    expected = -360 * result.freqs * 864 / 96_000
    wrapped = (expected + 180) % 360 - 180
    wrapped = np.where(wrapped <= -180, wrapped + 360, wrapped)
    np.testing.assert_allclose(result.phase_diff_deg, wrapped, atol=1e-6)
    (region,) = result.regions
    assert region.f_lo == pytest.approx(120 / 360 / 0.009, rel=1e-3)
    assert region.f_hi == pytest.approx(240 / 360 / 0.009, rel=1e-3)
    assert region.peak_freq == pytest.approx(1 / 0.018, rel=0.01)  # 180° at 55.6 Hz
    assert region.worst_deficit_db < -40
    assert region.level_diff_db == pytest.approx(0, abs=1e-9)
    assert result.warnings == []


def test_polarity_and_aligned():
    doc = flat_pair()
    assert s.integration(doc, 0, 1).regions == []
    doc.outputs[1].invert = True
    result = s.integration(doc, 0, 1)
    (region,) = result.regions
    assert (region.f_lo, region.f_hi) == (pytest.approx(20), pytest.approx(100))
    assert abs(region.peak_dphi_deg) == pytest.approx(180)


def test_shaker_setup_delay_changes_the_phase():
    """Phase 4 acceptance: the 9 ms shaker delay visibly changes the integration."""
    without = s.integration(shaker_setup(0.0), 0, 1)
    with_delay = s.integration(shaker_setup(9.0), 0, 1)
    assert not np.allclose(without.phase_diff_deg, with_delay.phase_diff_deg)
    # the LR4 band-pass alone already leads/lags the flat sub, the delay adds −3.24°/Hz on top
    shift = (with_delay.phase_diff_deg - without.phase_diff_deg + 180) % 360 - 180
    expected = (-360 * with_delay.freqs * 0.009 + 180) % 360 - 180
    np.testing.assert_allclose(shift, expected, atol=1e-6)
    level = with_delay.level_diff_db[np.argmin(np.abs(with_delay.freqs - 50))]
    assert 9 < level < 12  # +12 dB shakers, less the skirts of the 20-80 Hz LR4 band-pass
    assert with_delay.regions, "the 9 ms delay puts part of 20-100 Hz in cancellation"


def test_muted_and_silent_outputs_warn():
    doc = flat_pair()
    doc.outputs[1].mute = True
    doc.outputs[1].invert = True
    result = s.integration(doc, 0, 1, names=["Shakers", "Sub", "Out 3", "Out 4"])
    assert result.regions  # analysed as un-muted
    assert result.warnings == ["Sub is muted; the integration panel treats it as un-muted"]
    assert doc.outputs[1].mute  # the caller's document is untouched

    doc = default_config_doc(M2X4HD)  # factory routing: Input 1 doesn't feed Out 2
    result = s.integration(doc, 0, 1)
    assert np.all(np.isnan(result.phase_diff_deg))
    assert result.regions == []
    assert any("Out 2 is silent" in w for w in result.warnings)
    data = result.to_dict(digits=2)
    assert data["phase_diff_deg"][0] is None and data["deficit_db"][0] is None


def test_integration_to_dict_rounds():
    doc = flat_pair()
    doc.outputs[0].delay_ms = 9.0
    data = s.integration(doc, 0, 1, points=16).to_dict(digits=2)
    assert data["a"] == 0 and data["b"] == 1 and data["threshold_deg"] == 120
    assert data["range"] == [pytest.approx(20), pytest.approx(100)]
    assert len(data["freqs"]) == len(data["phase_diff_deg"]) == len(data["level_diff_db"]) == 16
    assert all(v == round(v, 2) for v in data["phase_diff_deg"])
    assert data["regions"][0]["f_lo"] == pytest.approx(37.04, abs=0.5)


# --- multi-output response ------------------------------------------------------------------------------------------


def test_system_view_sum_and_notes():
    doc = flat_pair()
    doc.outputs[1].invert = True
    freqs = resp.view_grid("sub", 64)
    result = s.system_view(doc, [0, 1], freqs)
    assert set(result.channels) == {0, 1}
    assert np.all(result.channels[0].magnitude_db == pytest.approx(0, abs=1e-9))
    assert np.all(result.total.magnitude_db == resp.DB_FLOOR)  # equal and opposite: the electrical sum cancels
    assert result.warnings == []

    doc.outputs[1].invert = False
    doc.outputs[2].mute = True
    names = ["Shakers", "Sub", "Out 3", "Out 4"]
    result = s.system_view(doc, [0, 1, 2, 3], freqs, names=names)
    assert result.total.magnitude_db[0] == pytest.approx(20 * math.log10(2), abs=1e-6)  # Out 4 is fed by Input 2
    assert "Out 3 is muted, so it adds nothing to the sum" in result.warnings
    assert "no selected input is routed to Out 4, so it is silent" in result.warnings
    assert s.system_view(doc, [0, 1, 2, 3], freqs, inputs=None).total.magnitude_db[0] == pytest.approx(
        20 * math.log10(4),
        abs=1e-6,  # In 1 + In 2 both reach Out 2
    )

    empty = s.system_view(doc, [], freqs)
    assert empty.total is None and empty.channels == {}
