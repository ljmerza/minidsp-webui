"""Every payload the builder can produce validates against minidspd 0.1.12's own Config schema."""

import jsonschema
import pytest

from app.model.config_doc import default_config_doc
from app.model.payload import build_plan, device_values, mute_payload
from tests.unit.sync.conftest import (
    FIR_PROFILE,
    LR4_LP_80,
    PEAK,
    PROFILE,
    config_validator,
    doc_with,
    run_rounds,
    trusted,
)


def busy_doc(profile):
    """A document where every kind of field differs from the defaults."""
    changes = {}
    for i in range(profile.n_inputs):
        changes |= {f"inputs.{i}.gain_db": -1.5, f"inputs.{i}.mute": True}
        for k in range(profile.peq_per_input):
            changes |= {f"inputs.{i}.peq.{k}.compiled": PEAK, f"inputs.{i}.peq.{k}.bypass": k % 2 == 0}
        for m in range(profile.n_outputs):
            changes |= {f"inputs.{i}.routing.{m}.enabled": (i + m) % 2 == 1, f"inputs.{i}.routing.{m}.gain_db": -3.0}
    for n in range(profile.n_outputs):
        changes |= {
            f"outputs.{n}.gain_db": 6.0,
            f"outputs.{n}.mute": n == 3,
            f"outputs.{n}.invert": True,
            f"outputs.{n}.delay_ms": 9.0,
            f"outputs.{n}.compressor.bypass": False,
            f"outputs.{n}.compressor.threshold_db": -20.0,
            f"outputs.{n}.compressor.ratio": 4.0,
            f"outputs.{n}.compressor.attack_ms": 15.0,
            f"outputs.{n}.compressor.release_ms": 30.0,
        }
        for k in range(profile.peq_per_output):
            changes |= {f"outputs.{n}.peq.{k}.compiled": PEAK, f"outputs.{n}.peq.{k}.bypass": False}
        for g in range(profile.crossover_groups):
            changes[f"outputs.{n}.crossover.{g}.bypass"] = g == 1
            for k in range(profile.crossover_biquads):
                changes[f"outputs.{n}.crossover.{g}.compiled.{k}"] = LR4_LP_80
        if profile.fir_supported:
            changes |= {f"outputs.{n}.fir.blob_id": "taps" if n % 2 else None, f"outputs.{n}.fir.bypass": n % 2 == 0}
    return doc_with(changes, default_config_doc(profile), profile)


@pytest.mark.parametrize("split_channels", [True, False])
@pytest.mark.parametrize("profile", [PROFILE, FIR_PROFILE], ids=["2x4hd", "2x4hd+fir"])
def test_push_all_payloads_validate(validator, profile, split_channels):
    desired = busy_doc(profile)
    rounds = run_rounds(
        desired,
        {},
        device_values(desired, profile),
        profile=profile,
        fir_taps={"taps": [1.0, 0.5, -0.25]},
        split_channels=split_channels,
    )
    assert len(rounds) == 2  # main writes, then the deferred un-bypasses / coefficients after re-bypass
    sent = [body for round_ in rounds for body in round_]
    for body in sent:
        validator.validate(body)
    for body in sent:
        for output in body.get("outputs", []):
            for group in output.get("crossover", []):
                assert all("index" in bq for bq in group.get("coeff", []))


def test_incremental_payloads_validate(validator):
    base = default_config_doc(PROFILE)
    for path, value in device_values(busy_doc(PROFILE), PROFILE).items():
        desired = doc_with({path: value}, base)
        for round_ in run_rounds(desired, trusted(base), [path]):
            for body in round_:
                validator.validate(body)


def test_mute_payload_validates(validator):
    validator.validate(mute_payload({0: True, 1: True, 2: False, 3: False}).body)


def test_schema_is_strict_enough_to_catch_mistakes(validator):
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"outputs": [{"index": 0, "gian": 1.0}]})  # typo
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"outputs": [{"index": 0, "delay": 1.5}]})  # bare number: HTTP 500 on the device
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"outputs": [{"index": 0, "delay": {"secs": 0}}]})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"outputs": [{"index": -1}]})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"inputs": [{"index": 0, "routing": [{"index": 0, "enabled": True}]}]})


def test_loose_schema_also_accepts_payloads():
    loose = config_validator(strict_keys=False)
    desired = busy_doc(PROFILE)
    plan = build_plan(desired, {}, device_values(desired, PROFILE), profile=PROFILE)
    for payload in plan.payloads:
        loose.validate(payload.body)
