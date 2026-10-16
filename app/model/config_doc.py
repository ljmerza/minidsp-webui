"""Canonical desired-state document, one per (device serial, preset).

Paths are dotted strings such as ``outputs.0.gain_db``, ``outputs.0.crossover.1.compiled.2`` or
``inputs.0.routing.3.enabled``. A path is the unit of sync status. Biquads and design objects are atomic
leaves: ``outputs.0.peq.3.compiled`` is one path, not five.
"""

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.model.device_profile import DeviceProfile
from app.model.filters import PASSTHROUGH, Biquad, CrossoverDesign, FilterDesign

SCHEMA_VERSION = 1


class _DocModel(BaseModel):
    """NaN/Infinity are valid JSON to Python's parser; they are never valid settings."""

    model_config = ConfigDict(allow_inf_nan=False)


class PeqBand(_DocModel):
    bypass: bool = True
    design: FilterDesign = Field(default_factory=FilterDesign)
    compiled: Biquad = PASSTHROUGH
    external: bool = False  # reserved: a band managed by another tool is never written by us


class CrossoverGroup(_DocModel):
    bypass: bool = True
    design: CrossoverDesign | None = None
    compiled: list[Biquad] = Field(default_factory=list)  # exactly profile.crossover_biquads


class Compressor(_DocModel):
    bypass: bool = True
    threshold_db: float = 0.0
    ratio: float = 1.0
    attack_ms: float = 10.0
    release_ms: float = 100.0


class Fir(_DocModel):
    bypass: bool = True
    blob_id: str | None = None


class RouteCell(_DocModel):
    enabled: bool = False
    gain_db: float = 0.0


class InputCh(_DocModel):
    gain_db: float = 0.0
    mute: bool = False
    peq: list[PeqBand] = Field(default_factory=list)
    routing: list[RouteCell] = Field(default_factory=list)


class OutputCh(_DocModel):
    gain_db: float = 0.0
    mute: bool = False
    invert: bool = False
    delay_ms: float = 0.0  # sent to the device rounded to whole samples
    peq: list[PeqBand] = Field(default_factory=list)
    crossover: list[CrossoverGroup] = Field(default_factory=list)
    compressor: Compressor = Field(default_factory=Compressor)
    fir: Fir = Field(default_factory=Fir)


class ConfigDoc(_DocModel):
    schema_version: int = SCHEMA_VERSION
    inputs: list[InputCh] = Field(default_factory=list)
    outputs: list[OutputCh] = Field(default_factory=list)


def default_config_doc(profile: DeviceProfile) -> ConfigDoc:
    """A flat config: unity gains, nothing muted, all filters pass-through and bypassed.

    Routing follows the usual factory layout: output ``o`` is fed by input ``o % n_inputs``
    (on a 2x4HD: In 1 -> Out 1/3, In 2 -> Out 2/4).
    """
    inputs = [
        InputCh(
            peq=[PeqBand() for _ in range(profile.peq_per_input)],
            routing=[RouteCell(enabled=(o % profile.n_inputs == i)) for o in range(profile.n_outputs)],
        )
        for i in range(profile.n_inputs)
    ]
    outputs = [
        OutputCh(
            peq=[PeqBand() for _ in range(profile.peq_per_output)],
            crossover=[
                CrossoverGroup(compiled=[PASSTHROUGH] * profile.crossover_biquads)
                for _ in range(profile.crossover_groups)
            ],
        )
        for _ in range(profile.n_outputs)
    ]
    return ConfigDoc(inputs=inputs, outputs=outputs)


# --- path helpers -----------------------------------------------------------------------------------------------


class PathError(KeyError):
    """The dotted path does not exist in the document."""


_BIQUAD_KEYS = frozenset(Biquad.model_fields)
_ATOMIC_KEYS = frozenset({"design"})
_NON_DEVICE_KEYS = frozenset({"design", "external", "schema_version"})
_MISSING = object()


def _split(path: str) -> list[str]:
    if not path:
        raise PathError(path)
    return path.split(".")


def _step(node: Any, part: str, path: str) -> Any:
    if isinstance(node, list):
        try:
            idx = int(part)
        except ValueError:
            raise PathError(path) from None
        if not 0 <= idx < len(node):
            raise PathError(path)
        return node[idx]
    if isinstance(node, BaseModel):
        if part not in type(node).model_fields:
            raise PathError(path)
        return getattr(node, part)
    if isinstance(node, dict):
        if part not in node:
            raise PathError(path)
        return node[part]
    raise PathError(path)


def get_path(doc: ConfigDoc, path: str) -> Any:
    """Value at a dotted path (a model, list or scalar)."""
    node: Any = doc
    for part in _split(path):
        node = _step(node, part, path)
    return node


def set_path(doc: ConfigDoc, path: str, value: Any) -> ConfigDoc:
    """Return a new validated document with ``path`` set to ``value``. The input document is not modified."""
    parts = _split(path)
    data = doc.model_dump()
    node: Any = data
    for part in parts[:-1]:
        node = _step(node, part, path)
    last = parts[-1]
    if isinstance(value, BaseModel):
        value = value.model_dump()
    elif isinstance(value, list):
        value = [v.model_dump() if isinstance(v, BaseModel) else v for v in value]
    if isinstance(node, list):
        _step(node, last, path)
        node[int(last)] = value
    elif isinstance(node, dict):
        if last not in node:
            raise PathError(path)
        node[last] = value
    else:
        raise PathError(path)
    return ConfigDoc.model_validate(data)


def _is_leaf(key: str, value: Any) -> bool:
    if key in _ATOMIC_KEYS:
        return True
    if isinstance(value, dict):
        return value.keys() == _BIQUAD_KEYS
    return not isinstance(value, list)


def flatten(doc: ConfigDoc) -> dict[str, Any]:
    """All leaf paths in document order, mapped to JSON-compatible values (biquads/designs as dicts)."""
    out: dict[str, Any] = {}

    def walk(prefix: str, key: str, value: Any) -> None:
        if _is_leaf(key, value):
            out[prefix] = value
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}.{k}" if prefix else k, k, v)
        else:
            for i, v in enumerate(value):
                walk(f"{prefix}.{i}", str(i), v)

    walk("", "", doc.model_dump(mode="json"))
    return out


def diff_paths(old: ConfigDoc, new: ConfigDoc) -> list[str]:
    """Leaf paths whose values differ, in document order. Both docs must have the same shape."""
    a, b = flatten(old), flatten(new)
    paths = [p for p, v in b.items() if a.get(p, _MISSING) != v]
    paths.extend(p for p in a if p not in b)
    return paths


def is_device_path(path: str) -> bool:
    """True when the path maps to something written to the device (design intent and metadata don't)."""
    return not any(part in _NON_DEVICE_KEYS for part in path.split("."))


def validate_doc(doc: ConfigDoc, profile: DeviceProfile) -> list[str]:
    """Shape and range problems for this profile; an empty list means the doc is usable."""
    problems: list[str] = []

    def check_len(path: str, items: list, expected: int) -> None:
        if len(items) != expected:
            problems.append(f"{path}: expected {expected} entries, got {len(items)}")

    def check_gain(path: str, value: float) -> None:
        if not (math.isfinite(value) and profile.gain_min_db <= value <= profile.gain_max_db):
            problems.append(f"{path}: {value} dB outside {profile.gain_min_db}..{profile.gain_max_db}")

    def check_biquads(path: str, biquads: list[Biquad]) -> None:
        for k, bq in enumerate(biquads):
            if not bq.is_stable:
                problems.append(f"{path}.{k}: unstable or non-finite biquad {list(bq.as_tuple())}")

    check_len("inputs", doc.inputs, profile.n_inputs)
    check_len("outputs", doc.outputs, profile.n_outputs)
    for i, ch in enumerate(doc.inputs):
        check_gain(f"inputs.{i}.gain_db", ch.gain_db)
        check_len(f"inputs.{i}.peq", ch.peq, profile.peq_per_input)
        for k, band in enumerate(ch.peq):
            check_biquads(f"inputs.{i}.peq.{k}.compiled", [band.compiled])
        check_len(f"inputs.{i}.routing", ch.routing, profile.n_outputs)
        for o, cell in enumerate(ch.routing):
            check_gain(f"inputs.{i}.routing.{o}.gain_db", cell.gain_db)
    for n, ch in enumerate(doc.outputs):
        check_gain(f"outputs.{n}.gain_db", ch.gain_db)
        if not (math.isfinite(ch.delay_ms) and 0 <= profile.delay_samples(ch.delay_ms) <= profile.delay_max_samples):
            problems.append(f"outputs.{n}.delay_ms: {ch.delay_ms} ms outside 0..{profile.delay_max_ms:.3f}")
        check_len(f"outputs.{n}.peq", ch.peq, profile.peq_per_output)
        for k, band in enumerate(ch.peq):
            check_biquads(f"outputs.{n}.peq.{k}.compiled", [band.compiled])
        check_len(f"outputs.{n}.crossover", ch.crossover, profile.crossover_groups)
        for g, group in enumerate(ch.crossover):
            check_len(f"outputs.{n}.crossover.{g}.compiled", group.compiled, profile.crossover_biquads)
            check_biquads(f"outputs.{n}.crossover.{g}.compiled", group.compiled)
    return problems
