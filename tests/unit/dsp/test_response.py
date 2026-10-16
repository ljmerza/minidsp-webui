import json
import math

import numpy as np
import pytest

from app.dsp import design as d
from app.dsp import response as r
from app.model.config_doc import default_config_doc
from app.model.device_profile import M2X4HD
from app.model.filters import CrossoverDesign
from app.model.payload import delay_duration
from tests.unit.dsp.vectors_gen import GOLDEN_HP_20, GOLDEN_LP_80


def xo(kind: str, family: str, order: int, freq: float):
    return d.design_crossover(CrossoverDesign(kind=kind, family=family, order=order, freq=freq))


def test_grid_defaults():
    f = r.freq_grid()
    assert len(f) == 512
    assert f[0] == pytest.approx(5.0)
    assert f[-1] == pytest.approx(24_000.0)
    assert np.allclose(np.diff(np.log(f)), np.log(f[1] / f[0]))
    sub = r.view_grid("sub")
    assert (sub[0], sub[-1]) == pytest.approx((5.0, 300.0))
    full = r.view_grid("full")
    assert (full[0], full[-1]) == pytest.approx((10.0, 20_000.0))
    with pytest.raises(ValueError):
        r.freq_grid(100, 10)


def test_passthrough_is_flat_zero_phase():
    resp = r.filter_response([d.passthrough()])
    assert np.all(resp.magnitude_db == 0.0)
    assert np.all(resp.phase_deg == 0.0)
    assert np.all(resp.group_delay_ms == 0.0)
    assert resp.warnings == []
    assert resp.float32_deviation_db == 0.0


def test_empty_cascade_is_unity():
    assert np.all(r.filter_response([]).magnitude_db == 0.0)


def test_peq_6db_at_1k():
    resp = r.filter_response([d.peaking(1000, 1, 6)], [1000.0])
    assert resp.magnitude_db[0] == pytest.approx(6.0, abs=0.01)


@pytest.mark.parametrize("fc", [80.0, 1000.0])
def test_lr4_lp_plus_hp_sums_flat(fc):
    f = r.freq_grid()
    lp = r.cascade_tf(xo("lp", "lr", 4, fc), f, float32=False)
    hp = r.cascade_tf(xo("hp", "lr", 4, fc), f, float32=False)
    total = r.Response.from_tf(lp + hp, f)
    assert np.max(np.abs(total.magnitude_db)) < 0.01


@pytest.mark.parametrize("order", [2, 6])
def test_lr2_lr6_sum_flat_with_hp_inverted(order):
    f = r.freq_grid()
    lp = r.cascade_tf(xo("lp", "lr", order, 500), f, float32=False)
    hp = r.cascade_tf(xo("hp", "lr", order, 500), f, float32=False)
    assert np.max(np.abs(r.Response.from_tf(lp + hp * -1.0, f).magnitude_db)) < 0.01


def test_lr8_sum_flat():
    f = r.freq_grid()
    lp = r.cascade_tf(xo("lp", "lr", 8, 500), f, float32=False)
    hp = r.cascade_tf(xo("hp", "lr", 8, 500), f, float32=False)
    assert np.max(np.abs(r.Response.from_tf(lp + hp, f).magnitude_db)) < 0.01


def test_golden_combined_response_matches_doc_table():
    f = [10.0, 20.0, 40.0, 60.0, 80.0, 120.0, 200.0]
    doc_db = [-24.6, -6.1, -1.1, -2.5, -6.1, -15.7, -32.1]
    resp = r.filter_response([GOLDEN_LP_80, GOLDEN_LP_80, GOLDEN_HP_20, GOLDEN_HP_20], f, float32=False)
    assert resp.magnitude_db.tolist() == pytest.approx(doc_db, abs=0.06)
    assert [round(x, 1) for x in resp.magnitude_db] == doc_db


def test_float32_rounding_warning_on_shaker_crossover():
    # The 20 Hz high-pass's denominator nearly cancels at low frequency; float32 rounding moves it ~0.15 dB.
    resp = r.filter_response([GOLDEN_LP_80, GOLDEN_LP_80, GOLDEN_HP_20, GOLDEN_HP_20])
    assert 0.1 < resp.float32_deviation_db < 0.3
    assert len(resp.warnings) == 1
    assert "float32" in resp.warnings[0]


def test_float32_deviation_small_for_ordinary_peq():
    resp = r.filter_response([d.peaking(1000, 1, 6)])
    assert resp.float32_deviation_db < 1e-4
    assert resp.warnings == []
    assert r.filter_response([d.peaking(1000, 1, 6)], float32=False).float32_deviation_db < 1e-4


def test_float32_deviation_ignores_deep_stopband():
    exact = r.Tf(np.array([1e-8, 1.0]), np.zeros(2))
    rounded = r.Tf(np.array([2e-8, 1.0]), np.zeros(2))
    assert r.float32_deviation_db(exact, rounded) == 0.0
    silent = r.Tf(np.zeros(2, dtype=complex), np.zeros(2))
    assert r.float32_deviation_db(silent, silent) == 0.0


def test_to_float32():
    assert r.to_float32([0.1, 0, 0, 0, 0])[0] == float(np.float32(0.1))
    assert r.to_float32({"b0": 1, "b1": 0, "b2": 0, "a1": 0, "a2": 0}) == (1.0, 0.0, 0.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        r.to_float32([1, 2])


def test_group_delay_matches_numerical_gradient():
    bq = d.lowpass2(100, 2.0)
    f = np.geomspace(20, 500, 4000)
    resp = r.filter_response([bq], f, float32=False)
    w = 2 * np.pi * f
    numeric = -np.gradient(np.unwrap(np.angle(resp.h)), w) * 1000
    assert np.allclose(resp.group_delay_ms[5:-5], numeric[5:-5], rtol=1e-3, atol=1e-5)


def test_delay_9ms():
    assert r.delay_samples(9.0) == 864
    assert M2X4HD.delay_samples(9.0) == 864
    assert delay_duration(9.0, M2X4HD) == {"secs": 0, "nanos": 9_000_000}
    f = r.freq_grid()
    resp = r.Response.from_tf(r.delay_tf(864, f), f)
    assert np.allclose(resp.magnitude_db, 0.0)
    assert np.allclose(resp.group_delay_ms, 9.0)
    # The 512-point grid steps ~22 rad at 24 kHz for 9 ms: plain np.unwrap would alias, ours stays linear.
    assert np.allclose(resp.phase_unwrapped_deg, -360 * f * 0.009, atol=1e-6)


def test_phase_unwrapped_matches_np_unwrap_on_dense_grid():
    f = np.geomspace(5, 24_000, 20_000)
    bqs = xo("lp", "lr", 8, 300)
    resp = r.filter_response(bqs, f, float32=False)
    assert np.allclose(resp.phase_unwrapped_deg, np.degrees(np.unwrap(np.angle(resp.h))), atol=1e-6)
    assert resp.phase_unwrapped_deg[-1] == pytest.approx(-720, abs=5)


def test_fir_response():
    f = np.array([10.0, 1000.0, 20_000.0])
    unit = r.fir_tf([1.0], f)
    assert np.allclose(unit.h, 1.0)
    taps = np.zeros(5000)
    taps[100] = 0.5
    resp = r.Response.from_tf(r.fir_tf(taps, np.geomspace(10, 20_000, 200)), np.geomspace(10, 20_000, 200))
    assert np.allclose(resp.magnitude_db, 20 * math.log10(0.5))
    assert np.allclose(resp.group_delay_ms, 100 / 96)


def test_compressor_curve():
    out = r.compressor_curve(-20, 4, [-40, -20, 0])
    assert out.tolist() == [-40, -20, -15]
    with pytest.raises(ValueError):
        r.compressor_curve(-20, 0.5, [0])


def test_to_dict_is_json_safe():
    doc = default_config_doc(M2X4HD)
    doc.outputs[0].mute = True
    resp = r.output_response(doc, 0, r.freq_grid(n=16))
    data = resp.to_dict()
    text = json.dumps(data, allow_nan=False)
    assert "NaN" not in text
    assert data["magnitude_db"] == [r.DB_FLOOR] * 16
    assert data["group_delay_ms"] == [0.0] * 16
    assert data["phase_unwrapped_deg"] == [0.0] * 16
    assert set(data) == {
        "freqs",
        "magnitude_db",
        "phase_deg",
        "phase_unwrapped_deg",
        "group_delay_ms",
        "float32_deviation_db",
        "warnings",
    }


def test_at_picks_nearest_point():
    resp = r.filter_response([d.gain(6)], [10.0, 100.0])
    assert abs(resp.at(95)) == pytest.approx(10 ** (6 / 20))


# --- channel chains -----------------------------------------------------------------------------------------------


def shaker_doc():
    """Out 2 fed by In 1 with the shaker crossover, 9 ms delay and +3 dB; Out 1 a plain sub."""
    doc = default_config_doc(M2X4HD)
    out = doc.outputs[1]
    out.crossover[0].compiled = xo("lp", "lr", 4, 80)
    out.crossover[0].bypass = False
    out.crossover[1].compiled = xo("hp", "lr", 4, 20)
    out.crossover[1].bypass = False
    out.delay_ms = 9.0
    out.gain_db = 3.0
    doc.inputs[0].routing[1].enabled = True
    return doc


def test_default_doc_output_follows_routing():
    doc = default_config_doc(M2X4HD)
    f = r.freq_grid(n=8)
    # Out 1 is fed by In 1 (the default view); Out 2 by In 2 only, so nothing from In 1.
    assert np.allclose(r.output_response(doc, 0, f).magnitude_db, 0.0)
    assert np.all(r.output_response(doc, 1, f).magnitude_db == r.DB_FLOOR)
    assert np.allclose(r.output_response(doc, 1, f, inputs=None).magnitude_db, 0.0)
    assert np.allclose(r.output_response(doc, 1, f, inputs=[1]).magnitude_db, 0.0)


def test_output_chain_matches_hand_built_product():
    doc = shaker_doc()
    f = r.freq_grid()
    resp = r.output_response(doc, 1, f, float32=False)
    expected = r.filter_response(
        [*doc.outputs[1].crossover[0].compiled, *doc.outputs[1].crossover[1].compiled], f, float32=False
    )
    audible = expected.magnitude_db > -150
    assert np.allclose(resp.magnitude_db[audible], expected.magnitude_db[audible] + 3.0)
    assert np.allclose(resp.group_delay_ms, expected.group_delay_ms + 9.0)
    assert [w.split(":")[0] for w in resp.warnings] == ["output 2"]  # float32 (the 20 Hz HP), no FIR warning


def test_output_chain_float32_warns_with_label():
    doc = default_config_doc(M2X4HD)
    doc.outputs[0].crossover[1].compiled = [d.highpass2(20, 1 / math.sqrt(2))] * 2 + [d.passthrough()] * 2
    doc.outputs[0].crossover[1].bypass = False
    resp = r.output_response(doc, 0)
    assert resp.warnings and resp.warnings[0].startswith("output 1:")


def test_bypassed_blocks_contribute_unity():
    doc = shaker_doc()
    doc.outputs[1].crossover[0].bypass = True
    doc.outputs[1].crossover[1].bypass = True
    doc.outputs[1].peq[0].compiled = d.peaking(1000, 1, 6)  # still bypassed
    resp = r.output_response(doc, 1, [1000.0])
    assert resp.magnitude_db[0] == pytest.approx(3.0, abs=1e-6)
    doc.outputs[1].peq[0].bypass = False
    assert r.output_response(doc, 1, [1000.0]).magnitude_db[0] == pytest.approx(9.0, abs=0.01)


def test_input_side_gain_mute_peq_and_route_gain():
    doc = default_config_doc(M2X4HD)
    doc.inputs[0].gain_db = -6.0
    doc.inputs[0].routing[0].gain_db = -3.0
    doc.inputs[0].peq[2].compiled = d.peaking(100, 1, 4)
    doc.inputs[0].peq[2].bypass = False
    f = [100.0]
    assert r.output_response(doc, 0, f).magnitude_db[0] == pytest.approx(-5.0, abs=0.01)
    assert r.input_response(doc, 0, f).magnitude_db[0] == pytest.approx(4.0, abs=0.01)
    doc.inputs[0].mute = True
    assert r.output_response(doc, 0, f).magnitude_db[0] == r.DB_FLOOR


def test_inputs_sum_coherently():
    doc = default_config_doc(M2X4HD)
    doc.inputs[1].routing[0].enabled = True
    resp = r.output_response(doc, 0, [100.0], inputs=None)
    assert resp.magnitude_db[0] == pytest.approx(20 * math.log10(2), abs=1e-9)


def test_invert_mute_and_master():
    doc = default_config_doc(M2X4HD)
    doc.outputs[0].invert = True
    f = [100.0]
    stages = r.output_stages(doc, 0, np.array(f))
    assert stages["total"].h[0] == pytest.approx(-1.0)
    assert r.output_response(doc, 0, f, master_db=-20).magnitude_db[0] == pytest.approx(-20.0)
    doc.outputs[0].mute = True
    assert r.output_response(doc, 0, f).magnitude_db[0] == r.DB_FLOOR


def test_output_stages_order_and_product():
    doc = shaker_doc()
    f = r.freq_grid(n=32)
    stages = r.output_stages(doc, 1, f)
    assert list(stages) == ["input", "peq", "crossover", "fir", "polarity", "delay", "gain", "total"]
    product = np.prod([stages[k].h for k in list(stages)[:-1]], axis=0)
    assert np.allclose(stages["total"].h, product)


def test_fir_in_chain_and_unknown_taps_warning():
    doc = default_config_doc(M2X4HD)
    doc.outputs[0].fir.bypass = False
    f = r.freq_grid(n=16)
    resp = r.output_response(doc, 0, f)
    assert any("FIR" in w for w in resp.warnings)
    assert np.allclose(resp.magnitude_db, 0.0)
    resp = r.output_response(doc, 0, f, fir_taps={0: [0.5]})
    assert np.allclose(resp.magnitude_db, 20 * math.log10(0.5), atol=1e-6)
    assert not any("FIR" in w for w in resp.warnings)
    doc.outputs[0].fir.bypass = True
    assert np.allclose(r.output_response(doc, 0, f, fir_taps={0: [0.5]}).magnitude_db, 0.0)


def test_system_sum_is_complex():
    doc = default_config_doc(M2X4HD)
    doc.inputs[0].routing[1].enabled = True  # Out 1 and Out 2 both from In 1
    f = [100.0, 1000.0]
    both = r.system_response(doc, [0, 1], f)
    assert both.magnitude_db.tolist() == pytest.approx([6.0206] * 2, abs=1e-4)
    doc.outputs[1].invert = True
    cancelled = r.system_response(doc, [0, 1], f)
    assert np.all(cancelled.magnitude_db == r.DB_FLOOR)
    doc.outputs[1].invert = False
    doc.outputs[1].delay_ms = 5.0  # half a period at 100 Hz → cancels there, adds at 1 kHz (10 periods... 5 ms = 5)
    delayed = r.system_response(doc, [0, 1], f)
    assert delayed.magnitude_db[0] < -100
    assert delayed.magnitude_db[1] == pytest.approx(6.0206, abs=1e-4)


def test_system_sum_of_nothing():
    doc = default_config_doc(M2X4HD)
    resp = r.system_response(doc, [], [100.0])
    assert resp.magnitude_db[0] == r.DB_FLOOR


def test_delay_rounds_to_whole_samples():
    doc = default_config_doc(M2X4HD)
    doc.outputs[0].delay_ms = 0.0104  # 0.9984 samples → 1
    resp = r.output_response(doc, 0, [1000.0])
    assert resp.group_delay_ms[0] == pytest.approx(1000 / 96_000)
