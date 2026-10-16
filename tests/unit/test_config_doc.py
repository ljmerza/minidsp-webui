import pytest
from pydantic import ValidationError

from app.model.config_doc import (
    ConfigDoc,
    PathError,
    default_config_doc,
    diff_paths,
    flatten,
    get_path,
    is_device_path,
    set_path,
    validate_doc,
)
from app.model.device_profile import M2X4HD, profile_for
from app.model.filters import PASSTHROUGH, Biquad, CrossoverDesign, FilterDesign


@pytest.fixture
def doc() -> ConfigDoc:
    return default_config_doc(M2X4HD)


def test_profile_2x4hd():
    p = M2X4HD
    assert (p.n_inputs, p.n_outputs, p.peq_per_input, p.peq_per_output) == (2, 4, 10, 10)
    assert (p.crossover_groups, p.crossover_biquads, p.sample_rate) == (2, 4, 96_000)
    assert p.sources == ("Analog", "Toslink", "Usb")
    assert not p.fir_supported and not p.dirac_supported
    assert (p.gain_min_db, p.gain_max_db) == (-72.0, 12.0)
    assert p.delay_samples(9.0) == 864
    assert p.delay_max_samples == 7664
    assert p.delay_max_ms == pytest.approx(79.833, abs=1e-3)
    assert p.canonical_source("USB") == "Usb"
    assert p.canonical_source("hdmi") is None


def test_profile_for_overrides():
    assert profile_for("2x4HD") is M2X4HD
    assert profile_for("Flex") is None
    assert profile_for(None) is None
    custom = profile_for("2x4HD", gain_max_db=6.0, fir_supported=True)
    assert custom.gain_max_db == 6.0 and custom.fir_supported
    assert M2X4HD.gain_max_db == 12.0


def test_defaults_shape(doc):
    assert validate_doc(doc, M2X4HD) == []
    assert len(doc.inputs) == 2 and len(doc.outputs) == 4
    assert all(len(ch.peq) == 10 for ch in doc.inputs + doc.outputs)
    out = doc.outputs[0]
    assert len(out.crossover) == 2 and all(len(g.compiled) == 4 for g in out.crossover)
    assert all(g.bypass for g in out.crossover)
    assert all(b.bypass and b.compiled == PASSTHROUGH and b.design.type == "passthrough" for b in out.peq)
    assert out.compressor.bypass and out.fir.bypass and out.fir.blob_id is None
    routing = [[cell.enabled for cell in ch.routing] for ch in doc.inputs]
    assert routing == [[True, False, True, False], [False, True, False, True]]


def test_defaults_roundtrip_json(doc):
    assert ConfigDoc.model_validate_json(doc.model_dump_json()) == doc


def test_get_path(doc):
    assert get_path(doc, "outputs.0.gain_db") == 0.0
    assert get_path(doc, "inputs.0.routing.2.enabled") is True
    assert get_path(doc, "outputs.1.crossover.1.compiled.3") == PASSTHROUGH
    for bad in ("outputs.9.gain_db", "outputs.0.nope", "outputs.x", "", "outputs.0.gain_db.more"):
        with pytest.raises(PathError):
            get_path(doc, bad)


def test_set_path_returns_new_doc(doc):
    new = set_path(doc, "outputs.0.gain_db", 12.0)
    assert new.outputs[0].gain_db == 12.0
    assert doc.outputs[0].gain_db == 0.0
    coeffs = Biquad(b0=0.5, b1=0.1, b2=0.0, a1=0.2, a2=0.0)
    new = set_path(new, "outputs.0.crossover.0.compiled.1", coeffs)
    assert new.outputs[0].crossover[0].compiled[1] == coeffs
    new = set_path(new, "outputs.0.peq.2.design", {"type": "peaking", "freq": 50, "q": 2, "gain_db": -3})
    assert new.outputs[0].peq[2].design.freq == 50
    with pytest.raises(PathError):
        set_path(doc, "outputs.4.gain_db", 1.0)
    with pytest.raises(ValidationError):
        set_path(doc, "outputs.0.peq.2.design", {"type": "peaking"})


def test_flatten_leaves(doc):
    flat = flatten(doc)
    assert flat["outputs.0.crossover.1.compiled.2"] == {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}
    assert flat["inputs.0.peq.3.compiled"]["b0"] == 1.0
    assert flat["inputs.1.routing.3.enabled"] is True
    assert flat["outputs.2.fir.blob_id"] is None
    assert isinstance(flat["outputs.0.peq.0.design"], dict)
    assert flat["outputs.0.crossover.0.design"] is None
    assert not any(p.startswith("outputs.0.crossover.1.compiled.2.") for p in flat)


def test_diff_paths(doc):
    assert diff_paths(doc, doc) == []
    new = set_path(doc, "outputs.0.delay_ms", 9.0)
    new = set_path(new, "inputs.0.routing.1.enabled", True)
    new = set_path(new, "outputs.0.crossover.0.compiled.0", Biquad(b0=0.9))
    assert diff_paths(doc, new) == [
        "inputs.0.routing.1.enabled",
        "outputs.0.delay_ms",
        "outputs.0.crossover.0.compiled.0",
    ]


def test_is_device_path():
    assert is_device_path("outputs.0.gain_db")
    assert is_device_path("outputs.0.peq.3.compiled")
    assert not is_device_path("outputs.0.peq.3.design")
    assert not is_device_path("outputs.0.crossover.1.design")
    assert not is_device_path("schema_version")


def test_validate_doc_reports_problems(doc):
    bad = set_path(doc, "outputs.0.gain_db", 20.0)
    bad = set_path(bad, "outputs.1.delay_ms", 90.0)
    bad = set_path(bad, "inputs.0.routing.0.gain_db", -80.0)
    problems = validate_doc(bad, M2X4HD)
    assert any(p.startswith("outputs.0.gain_db") for p in problems)
    assert any(p.startswith("outputs.1.delay_ms") for p in problems)
    assert any(p.startswith("inputs.0.routing.0.gain_db") for p in problems)
    short = ConfigDoc(inputs=doc.inputs, outputs=doc.outputs[:3])
    assert any(p.startswith("outputs:") for p in validate_doc(short, M2X4HD))


def test_filter_design_validation():
    FilterDesign(type="lp2", freq=80, q=0.707)
    FilterDesign(type="raw", raw=Biquad())
    with pytest.raises(ValidationError):
        FilterDesign(type="peaking", freq=100, q=1)
    with pytest.raises(ValidationError):
        FilterDesign(type="lp1", freq=-5)
    assert Biquad().is_passthrough and not Biquad(b0=0.5).is_passthrough


def test_crossover_design_validation():
    CrossoverDesign(kind="lp", family="lr", order=4, freq=80)
    CrossoverDesign(kind="bp", family="bw", order=2, freq=20, freq_hi=80)
    with pytest.raises(ValidationError):
        CrossoverDesign(kind="lp", family="lr", order=3, freq=80)
    with pytest.raises(ValidationError):
        CrossoverDesign(kind="bp", family="bw", order=2, freq=80)
    with pytest.raises(ValidationError):
        CrossoverDesign(kind="hp", family="bw", order=9, freq=80)


def test_delay_limit_is_the_measured_device_maximum(doc):
    at_max = set_path(doc, "outputs.0.delay_ms", M2X4HD.delay_ms_from_samples(7664))
    assert validate_doc(at_max, M2X4HD) == []
    over = set_path(doc, "outputs.0.delay_ms", M2X4HD.delay_ms_from_samples(7665))
    assert any(p.startswith("outputs.0.delay_ms") for p in validate_doc(over, M2X4HD))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_are_rejected(doc, value):
    for path in ("outputs.0.delay_ms", "outputs.1.gain_db", "inputs.0.routing.2.gain_db"):
        with pytest.raises(ValidationError):
            set_path(doc, path, value)
    with pytest.raises(ValidationError):
        Biquad(b0=value)
    # Values that bypassed validation are reported, not crashed on.
    raw = doc.model_copy(deep=True)
    raw.outputs[0].delay_ms = value
    raw.outputs[1].gain_db = value
    problems = validate_doc(raw, M2X4HD)
    assert any(p.startswith("outputs.0.delay_ms") for p in problems)
    assert any(p.startswith("outputs.1.gain_db") for p in problems)


def test_unstable_biquads_are_rejected(doc):
    assert PASSTHROUGH.is_stable
    unstable = Biquad(a1=0.0, a2=1.5)
    assert not unstable.is_stable
    assert not Biquad(a1=1.2, a2=-0.1).is_stable
    bad = set_path(doc, "outputs.0.peq.3.compiled", unstable)
    bad = set_path(bad, "outputs.1.crossover.0.compiled.2", unstable)
    bad = set_path(bad, "inputs.1.peq.0.compiled", unstable)
    problems = validate_doc(bad, M2X4HD)
    assert [p.split(":")[0] for p in problems] == [
        "inputs.1.peq.0.compiled.0",
        "outputs.0.peq.3.compiled.0",
        "outputs.1.crossover.0.compiled.2",
    ]
