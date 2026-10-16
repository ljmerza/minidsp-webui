import copy
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from jsonschema.protocols import Validator

from app.model.config_doc import ConfigDoc, default_config_doc, set_path
from app.model.device_profile import M2X4HD
from app.model.payload import build_plan, device_values

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "minidspd-0.1.12-openapi.json"

PROFILE = M2X4HD
FIR_PROFILE = M2X4HD.model_copy(update={"fir_supported": True})

LR4_LP_80 = {"b0": 6.828594e-06, "b1": 1.3657188e-05, "b2": 6.828594e-06, "a1": 1.9925952, "a2": -0.99262255}
PEAK = {"b0": 1.0012, "b1": -1.9971, "b2": 0.9960, "a1": 1.9971, "a2": -0.9972}
UNSTABLE = {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 1.5}


def strict(schema: Any) -> Any:
    """Copy of an OpenAPI schema where every object with properties rejects unknown keys (catches typos)."""
    schema = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node.setdefault("additionalProperties", False)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


def config_validator(*, strict_keys: bool = True) -> Validator:
    spec = json.loads(FIXTURE.read_text())
    schemas = spec["components"]["schemas"]
    if strict_keys:
        schemas = strict(schemas)
    root = {**schemas["Config"], "components": {"schemas": schemas}}
    cls = jsonschema.validators.validator_for(root, default=jsonschema.Draft202012Validator)
    return cls(root)


@pytest.fixture(scope="session")
def validator() -> Validator:
    return config_validator()


def doc_with(changes: dict[str, Any], doc: ConfigDoc | None = None, profile=PROFILE) -> ConfigDoc:
    doc = doc or default_config_doc(profile)
    for path, value in changes.items():
        doc = set_path(doc, path, value)
    return doc


def trusted(doc: ConfigDoc, profile=PROFILE) -> dict[str, Any]:
    return dict(device_values(doc, profile))


def run_rounds(
    desired: ConfigDoc,
    applied: dict[str, Any],
    pending: Iterable[str],
    *,
    profile=PROFILE,
    fir_taps=None,
    split_channels: bool = True,
    max_rounds: int = 5,
) -> list[list[dict[str, Any]]]:
    """Simulate a writer where every call succeeds; returns the bodies sent per round."""
    applied = dict(applied)
    pending = set(pending)
    rounds = []
    for _ in range(max_rounds):
        plan = build_plan(desired, applied, pending, profile=profile, fir_taps=fir_taps, split_channels=split_channels)
        assert not plan.blocked, plan.blocked
        if not plan.payloads:
            assert not plan.deferred
            break
        rounds.append([p.body for p in plan.payloads])
        for payload in plan.payloads:
            applied.update(payload.values)
            pending -= set(payload.values)
        pending |= set(plan.deferred)
    else:
        raise AssertionError("plan did not converge")
    return rounds
