import math
import random

import pytest

from app.model.config_doc import default_config_doc, set_path
from app.model.payload import (
    build_plan,
    delay_duration,
    device_values,
    diff,
    fir_problem,
    mute_payload,
    same_value,
    value_problem,
)
from tests.unit.sync.conftest import (
    FIR_PROFILE,
    LR4_LP_80,
    PEAK,
    PROFILE,
    UNSTABLE,
    doc_with,
    run_rounds,
    trusted,
)

DEFAULT = default_config_doc(PROFILE)


def plan_for(changes, *, applied=None, pending=None, profile=PROFILE, **kwargs):
    desired = doc_with(changes, profile=profile)
    applied = trusted(default_config_doc(profile), profile) if applied is None else applied
    pending = list(changes) if pending is None else pending
    return build_plan(desired, applied, pending, profile=profile, **kwargs)


def bodies(plan):
    return [p.body for p in plan.payloads]


# --- delay ------------------------------------------------------------------------------------------------------


def daemon_samples(duration: dict[str, int]) -> int:
    """minidspd: round(as_micros(d) * 96000 / 1e6), with nanos truncated to whole microseconds."""
    micros = duration["secs"] * 1_000_000 + duration["nanos"] // 1000
    return round(micros * 96000 / 1e6)


def test_delay_round_trips_every_sample_count():
    for samples in range(PROFILE.delay_max_samples + 1):
        ms = samples / 96.0
        duration = delay_duration(ms, PROFILE)
        assert duration["secs"] == 0 and 0 <= duration["nanos"] < 1_000_000_000
        assert daemon_samples(duration) == samples, samples


def test_delay_known_values():
    assert delay_duration(1.5, PROFILE) == {"secs": 0, "nanos": 1_500_000}  # 144 samples, verified on hardware
    assert delay_duration(9.0, PROFILE) == {"secs": 0, "nanos": 9_000_000}
    assert delay_duration(0, PROFILE) == {"secs": 0, "nanos": 0}
    # 10.41 ms is 999.36 samples: rounded to 999 before converting.
    assert daemon_samples(delay_duration(10.41, PROFILE)) == 999
    assert delay_duration(10.41, PROFILE)["nanos"] == round(999 / 96000 * 1e9)


def test_delay_compares_as_samples():
    assert same_value("outputs.0.delay_ms", 1.5, 1.501, PROFILE)
    assert not same_value("outputs.0.delay_ms", 1.5, 1.52, PROFILE)
    assert not same_value("outputs.0.delay_ms", None, 1.5, PROFILE)
    assert same_value("outputs.0.gain_db", 1.5, 1.5, PROFILE)
    assert not same_value("outputs.0.gain_db", 1.5, 1.501, PROFILE)


def test_delay_payload():
    plan = plan_for({"outputs.1.delay_ms": 9.0})
    assert bodies(plan) == [{"outputs": [{"index": 1, "delay": {"secs": 0, "nanos": 9_000_000}}]}]
    assert plan.payloads[0].values == {"outputs.1.delay_ms": 9.0}


# --- diff -------------------------------------------------------------------------------------------------------


def test_diff_known_and_unknown():
    desired = doc_with({"outputs.0.gain_db": -3.0, "outputs.1.delay_ms": 1.5})
    applied = trusted(DEFAULT)
    assert diff(desired, applied, PROFILE) == ["outputs.0.gain_db", "outputs.1.delay_ms"]
    del applied["inputs.0.mute"]
    assert diff(desired, applied, PROFILE) == ["inputs.0.mute", "outputs.0.gain_db", "outputs.1.delay_ms"]
    assert diff(desired, applied, PROFILE, include_unknown=False) == ["outputs.0.gain_db", "outputs.1.delay_ms"]
    assert diff(desired, trusted(desired), PROFILE) == []


def test_diff_ignores_sub_sample_delay_changes_and_design_intent():
    applied = trusted(doc_with({"outputs.0.delay_ms": 1.5}))
    assert diff(doc_with({"outputs.0.delay_ms": 1.503}), applied, PROFILE) == []
    designed = doc_with({"outputs.0.peq.0.design": {"type": "peaking", "freq": 50, "q": 2, "gain_db": -3}})
    assert diff(designed, trusted(DEFAULT), PROFILE) == []


def test_device_values_skip_metadata_external_and_unsupported_fir():
    values = device_values(doc_with({"outputs.2.peq.4.external": True}), PROFILE)
    assert not any(".design" in p or ".external" in p or "schema_version" in p for p in values)
    assert not any(p.startswith("outputs.2.peq.4.") for p in values)
    assert "outputs.2.peq.5.compiled" in values
    assert not any(".fir." in p for p in values)
    assert "outputs.0.fir.blob_id" in device_values(default_config_doc(FIR_PROFILE), FIR_PROFILE)
    # 2 inputs x (2 + 10 PEQ x 2 + 4 routes x 2) + 4 outputs x (4 + 10 PEQ x 2 + 2 groups x 5 + 5 compressor)
    assert len(values) == 2 * (2 + 20 + 8) + 4 * (4 + 20 + 10 + 5) - 2


# --- mapping ----------------------------------------------------------------------------------------------------


def test_scalars_and_routing_mapping():
    plan = plan_for(
        {
            "inputs.0.gain_db": -2.5,
            "inputs.0.mute": True,
            "inputs.1.routing.2.enabled": True,
            "inputs.1.routing.3.enabled": False,
            "inputs.1.routing.3.gain_db": -6.0,
            "outputs.3.gain_db": 4.0,
            "outputs.3.mute": True,
            "outputs.3.invert": True,
        },
        pending=[
            "outputs.3.invert",
            "inputs.1.routing.3.gain_db",
            "outputs.3.mute",
            "inputs.0.gain_db",
            "inputs.1.routing.2.enabled",
            "outputs.3.gain_db",
            "inputs.1.routing.3.enabled",
            "inputs.0.mute",
        ],
    )
    assert bodies(plan) == [
        {"inputs": [{"index": 0, "mute": True, "gain": -2.5}]},
        {
            "inputs": [
                {
                    "index": 1,
                    "routing": [{"index": 2, "mute": False}, {"index": 3, "mute": True, "gain": -6.0}],
                }
            ]
        },
        {"outputs": [{"index": 3, "mute": True, "gain": 4.0, "invert": True}]},
    ]
    assert plan.payloads[1].values == {
        "inputs.1.routing.2.enabled": True,
        "inputs.1.routing.3.enabled": False,
        "inputs.1.routing.3.gain_db": -6.0,
    }


def test_routing_mute_is_inverted_enable():
    for enabled in (True, False):
        plan = plan_for({"inputs.0.routing.1.enabled": enabled}, applied={})
        assert bodies(plan) == [{"inputs": [{"index": 0, "routing": [{"index": 1, "mute": not enabled}]}]}]


def test_crossover_biquads_carry_their_index():
    plan = plan_for({"outputs.0.crossover.0.compiled.1": LR4_LP_80, "outputs.0.crossover.0.compiled.0": LR4_LP_80})
    group = bodies(plan)[0]["outputs"][0]["crossover"][0]
    assert group == {"index": 0, "coeff": [{"index": 0, **LR4_LP_80}, {"index": 1, **LR4_LP_80}]}


def test_compressor_mapping():
    plan = plan_for(
        {
            "outputs.2.compressor.threshold_db": -20.0,
            "outputs.2.compressor.ratio": 4.0,
            "outputs.2.compressor.attack_ms": 15.0,
            "outputs.2.compressor.release_ms": 30.0,
        }
    )
    assert bodies(plan) == [
        {"outputs": [{"index": 2, "compressor": {"threshold": -20.0, "ratio": 4.0, "attack": 15.0, "release": 30.0}}]}
    ]


def test_merged_payload_when_not_split():
    plan = plan_for(
        {"outputs.1.gain_db": -1.0, "inputs.0.gain_db": 1.0, "outputs.0.gain_db": -2.0}, split_channels=False
    )
    assert bodies(plan) == [
        {
            "inputs": [{"index": 0, "gain": 1.0}],
            "outputs": [{"index": 0, "gain": -2.0}, {"index": 1, "gain": -1.0}],
        }
    ]


def test_plan_is_deterministic():
    changes = {
        f"outputs.{n}.peq.{k}.compiled": PEAK
        for n in range(PROFILE.n_outputs)
        for k in range(PROFILE.peq_per_output)
        if (n + k) % 3 == 0
    }
    changes.update({"inputs.1.routing.0.enabled": True, "outputs.2.delay_ms": 3.0, "outputs.0.gain_db": -1.0})
    reference = bodies(plan_for(changes))
    paths = list(changes)
    rng = random.Random(7)
    for _ in range(10):
        rng.shuffle(paths)
        assert bodies(plan_for(changes, pending=list(paths))) == reference
    peq = reference[1]["outputs"][0]["peq"]
    assert [b["index"] for b in peq] == sorted(b["index"] for b in peq)


def test_float_values_are_plain_floats():
    plan = plan_for({"outputs.0.gain_db": -3})
    gain = bodies(plan)[0]["outputs"][0]["gain"]
    assert type(gain) is float


# --- glitch-safety ordering -------------------------------------------------------------------------------------


def test_peq_unbypass_is_sent_after_its_coefficients():
    changes = {"outputs.1.peq.3.compiled": PEAK, "outputs.1.peq.3.bypass": False}
    rounds = run_rounds(doc_with(changes), trusted(DEFAULT), changes)
    assert rounds == [
        [{"outputs": [{"index": 1, "peq": [{"index": 3, "coeff": PEAK}]}]}],
        [{"outputs": [{"index": 1, "peq": [{"index": 3, "bypass": False}]}]}],
    ]
    plan = plan_for(changes)
    assert plan.deferred == ["outputs.1.peq.3.bypass"]


def test_peq_unbypass_with_confirmed_coefficients_is_one_call():
    desired = doc_with({"outputs.1.peq.3.compiled": PEAK, "outputs.1.peq.3.bypass": False})
    applied = trusted(doc_with({"outputs.1.peq.3.compiled": PEAK}))
    plan = build_plan(desired, applied, ["outputs.1.peq.3.bypass"], profile=PROFILE)
    assert bodies(plan) == [{"outputs": [{"index": 1, "peq": [{"index": 3, "bypass": False}]}]}]
    assert plan.deferred == []


def test_peq_unbypass_pulls_in_unknown_coefficients():
    desired = doc_with({"inputs.0.peq.0.compiled": PEAK, "inputs.0.peq.0.bypass": False})
    applied = trusted(desired)
    del applied["inputs.0.peq.0.compiled"]
    del applied["inputs.0.peq.0.bypass"]
    rounds = run_rounds(desired, applied, ["inputs.0.peq.0.bypass"])
    assert rounds == [
        [{"inputs": [{"index": 0, "peq": [{"index": 0, "coeff": PEAK}]}]}],
        [{"inputs": [{"index": 0, "peq": [{"index": 0, "bypass": False}]}]}],
    ]


def test_peq_unbypass_with_stale_coefficients_rewrites_them_first():
    desired = doc_with({"outputs.0.peq.0.compiled": PEAK, "outputs.0.peq.0.bypass": False})
    applied = trusted(DEFAULT)  # shadow knows the old (passthrough) coefficients, bypassed
    plan = build_plan(desired, applied, ["outputs.0.peq.0.bypass"], profile=PROFILE)
    assert bodies(plan) == [{"outputs": [{"index": 0, "peq": [{"index": 0, "coeff": PEAK}]}]}]
    assert plan.deferred == ["outputs.0.peq.0.bypass"]


def test_forced_coefficients_defer_the_unbypass_even_when_confirmed():
    desired = doc_with({"outputs.0.peq.0.compiled": PEAK, "outputs.0.peq.0.bypass": False})
    plan = build_plan(
        desired, trusted(desired), ["outputs.0.peq.0.compiled", "outputs.0.peq.0.bypass"], profile=PROFILE
    )
    assert plan.deferred == ["outputs.0.peq.0.bypass"]


def test_peq_bypass_and_coefficients_share_a_call_when_bypassing():
    desired = doc_with({"outputs.0.peq.0.compiled": PEAK, "outputs.0.peq.0.bypass": False})
    applied = trusted(desired)
    rebypass = doc_with({"outputs.0.peq.0.compiled": LR4_LP_80, "outputs.0.peq.0.bypass": True})
    plan = build_plan(rebypass, applied, ["outputs.0.peq.0.compiled", "outputs.0.peq.0.bypass"], profile=PROFILE)
    assert bodies(plan) == [{"outputs": [{"index": 0, "peq": [{"index": 0, "coeff": LR4_LP_80, "bypass": True}]}]}]


def test_never_unbypass_when_coefficients_cannot_be_written():
    plan = plan_for({"outputs.0.peq.0.compiled": UNSTABLE, "outputs.0.peq.0.bypass": False})
    assert plan.payloads == []
    assert "unstable" in plan.blocked["outputs.0.peq.0.compiled"]
    assert "outputs.0.peq.0.compiled" in plan.blocked["outputs.0.peq.0.bypass"]


def test_never_unbypass_when_coefficients_gave_up():
    desired = doc_with({"outputs.0.peq.0.compiled": PEAK, "outputs.0.peq.0.bypass": False})
    applied = trusted(DEFAULT)
    plan = build_plan(desired, applied, ["outputs.0.peq.0.bypass"], profile=PROFILE, skip={"outputs.0.peq.0.compiled"})
    assert plan.payloads == []
    assert "given up" in plan.blocked["outputs.0.peq.0.bypass"]


def test_crossover_unbypass_shares_the_call_with_missing_biquads():
    desired = doc_with({"outputs.0.crossover.0.compiled.0": LR4_LP_80, "outputs.0.crossover.0.bypass": False})
    applied = trusted(desired)
    del applied["outputs.0.crossover.0.compiled.0"]
    del applied["outputs.0.crossover.0.compiled.3"]
    plan = build_plan(desired, applied, ["outputs.0.crossover.0.bypass"], profile=PROFILE)
    assert plan.deferred == []
    assert bodies(plan) == [
        {
            "outputs": [
                {
                    "index": 0,
                    "crossover": [
                        {
                            "index": 0,
                            "coeff": [
                                {"index": 0, **LR4_LP_80},
                                {"index": 3, "b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0},
                            ],
                            "bypass": False,
                        }
                    ],
                }
            ]
        }
    ]


def test_crossover_unbypass_blocked_by_unstable_biquad():
    plan = plan_for({"outputs.0.crossover.1.compiled.2": UNSTABLE, "outputs.0.crossover.1.bypass": False})
    assert plan.payloads == []
    assert set(plan.blocked) == {"outputs.0.crossover.1.compiled.2", "outputs.0.crossover.1.bypass"}


def test_crossover_rebypass_goes_before_new_coefficients():
    active = doc_with({"outputs.0.crossover.0.compiled.0": LR4_LP_80, "outputs.0.crossover.0.bypass": False})
    desired = doc_with({"outputs.0.crossover.0.compiled.0": PEAK, "outputs.0.crossover.0.bypass": True}, active)
    rounds = run_rounds(desired, trusted(active), ["outputs.0.crossover.0.compiled.0", "outputs.0.crossover.0.bypass"])
    assert rounds == [
        [{"outputs": [{"index": 0, "crossover": [{"index": 0, "bypass": True}]}]}],
        [{"outputs": [{"index": 0, "crossover": [{"index": 0, "coeff": [{"index": 0, **PEAK}]}]}]}],
    ]


def test_compressor_unbypass_waits_for_parameters():
    changes = {"outputs.1.compressor.threshold_db": -20.0, "outputs.1.compressor.bypass": False}
    rounds = run_rounds(doc_with(changes), trusted(DEFAULT), changes)
    assert rounds == [
        [{"outputs": [{"index": 1, "compressor": {"threshold": -20.0}}]}],
        [{"outputs": [{"index": 1, "compressor": {"bypass": False}}]}],
    ]


# --- FIR --------------------------------------------------------------------------------------------------------


def test_fir_coefficients_travel_alone():
    taps = {"blob-a": [0.5, 0.25, 0.125]}
    changes = {"outputs.2.fir.blob_id": "blob-a", "outputs.2.gain_db": -1.0, "outputs.2.fir.bypass": False}
    rounds = run_rounds(
        doc_with(changes, profile=FIR_PROFILE),
        trusted(default_config_doc(FIR_PROFILE), FIR_PROFILE),
        changes,
        profile=FIR_PROFILE,
        fir_taps=taps,
    )
    assert rounds == [
        [
            {"outputs": [{"index": 2, "gain": -1.0}]},
            {"outputs": [{"index": 2, "fir": {"coefficients": [0.5, 0.25, 0.125]}}]},
        ],
        [{"outputs": [{"index": 2, "fir": {"bypass": False}}]}],
    ]


def test_fir_clear_and_missing_taps():
    applied = trusted(doc_with({"outputs.0.fir.blob_id": "old"}, profile=FIR_PROFILE), FIR_PROFILE)
    plan = build_plan(default_config_doc(FIR_PROFILE), applied, ["outputs.0.fir.blob_id"], profile=FIR_PROFILE)
    assert bodies(plan) == [{"outputs": [{"index": 0, "fir": {"coefficients": []}}]}]
    assert plan.payloads[0].kind == "fir"

    plan = plan_for({"outputs.0.fir.blob_id": "nope"}, profile=FIR_PROFILE)
    assert plan.payloads == [] and "not available" in plan.blocked["outputs.0.fir.blob_id"]


def test_fir_problems():
    assert fir_problem(None, FIR_PROFILE)
    assert fir_problem([0.0] * (FIR_PROFILE.fir_max_taps + 1), FIR_PROFILE)
    assert fir_problem([math.nan], FIR_PROFILE)
    assert fir_problem([1.0] * FIR_PROFILE.fir_max_taps, FIR_PROFILE) is None


def test_fir_paths_rejected_without_fir_support():
    assert value_problem("outputs.0.fir.bypass", True, PROFILE) == "this device has no FIR support"


# --- validation -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value", "fragment"),
    [
        ("outputs.0.gain_db", 13.0, "outside"),
        ("outputs.0.gain_db", -73.0, "outside"),
        ("outputs.0.gain_db", math.nan, "finite"),
        ("outputs.0.gain_db", True, "finite"),
        ("inputs.0.routing.1.gain_db", 30.0, "outside"),
        ("outputs.0.delay_ms", 79.85, "maximum"),
        ("outputs.0.delay_ms", -1.0, "non-negative"),
        ("outputs.4.gain_db", 0.0, "output index 4"),
        ("inputs.2.mute", False, "input index 2"),
        ("outputs.0.peq.10.bypass", True, "PEQ index 10"),
        ("inputs.0.routing.4.enabled", True, "routing output index 4"),
        ("outputs.0.crossover.2.bypass", True, "crossover group index 2"),
        ("outputs.0.crossover.0.compiled.4", LR4_LP_80, "crossover biquad index 4"),
        ("outputs.0.mute", 1, "true/false"),
        ("outputs.0.peq.0.compiled", UNSTABLE, "unstable"),
        ("outputs.0.peq.0.compiled", {"b0": 1}, "expected a biquad"),
        ("outputs.0.compressor.ratio", -1.0, "ratio -1 outside 1..100"),
        ("outputs.0.compressor.ratio", 0.5, "ratio 0.5 outside 1..100"),
        ("outputs.0.compressor.attack_ms", 0.0, "attack 0 ms outside 0.1..2000"),
        ("outputs.0.compressor.threshold_db", 3.0, "threshold 3 dB outside -90..0"),
        ("outputs.0.compressor.attack_ms", math.inf, "finite"),
        ("inputs.0.invert", True, "unknown device path"),
        ("outputs.0.peq.0.design", {}, "unknown device path"),
        ("presets.0", 1, "not a device path"),
    ],
)
def test_value_problems(path, value, fragment):
    problem = value_problem(path, value, PROFILE)
    assert problem is not None and fragment in problem


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("outputs.0.gain_db", 12.0),
        ("outputs.0.gain_db", -72.0),
        ("outputs.0.delay_ms", 79.833),
        ("outputs.3.compressor.threshold_db", -90.0),
        ("inputs.1.routing.3.enabled", True),
        ("outputs.0.crossover.1.compiled.3", LR4_LP_80),
    ],
)
def test_valid_values(path, value):
    assert value_problem(path, value, PROFILE) is None


def test_invalid_pending_value_is_blocked_not_sent():
    desired = DEFAULT.model_copy(deep=True)
    desired.outputs[0].gain_db = 40.0
    plan = build_plan(desired, trusted(DEFAULT), ["outputs.0.gain_db", "outputs.1.mute"], profile=PROFILE)
    assert plan.blocked == {"outputs.0.gain_db": "gain 40.0 dB outside -72..12"}
    assert bodies(plan) == [{"outputs": [{"index": 1, "mute": False}]}]


def test_unknown_pending_path_is_blocked():
    plan = build_plan(DEFAULT, {}, ["outputs.0.fir.bypass"], profile=PROFILE)
    assert plan.payloads == [] and "outputs.0.fir.bypass" in plan.blocked


def test_mute_payload():
    payload = mute_payload({3: True, 0: False})
    assert payload.body == {"outputs": [{"index": 0, "mute": False}, {"index": 3, "mute": True}]}
    assert payload.values == {"outputs.0.mute": False, "outputs.3.mute": True}
    assert payload.kind == "mute" and payload.paths == ("outputs.0.mute", "outputs.3.mute")


def test_set_path_round_trip_feeds_builder():
    doc = set_path(DEFAULT, "outputs.1.gain_db", -4.0)
    assert bodies(build_plan(doc, trusted(DEFAULT), diff(doc, trusted(DEFAULT), PROFILE), profile=PROFILE)) == [
        {"outputs": [{"index": 1, "gain": -4.0}]}
    ]
