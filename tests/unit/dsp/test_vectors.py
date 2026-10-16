"""Shared design/response vectors (tests/vectors/*.json): the Python authority side of the JS parity test."""

import json
from pathlib import Path

import numpy as np
import pytest

from app.dsp import response as r
from app.dsp.stability import is_stable
from tests.unit.dsp import vectors_gen as gen

COEFF_TOL = 1e-12
DB_TOL = 0.05
FILES = sorted(gen.VECTORS_DIR.glob("*.json"))


def load(path: Path) -> list[dict]:
    return json.loads(path.read_text())


VECTORS = [(p.name, v) for p in FILES for v in load(p)]


def test_vector_files_exist():
    assert {p.name for p in FILES} == set(gen.build())


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_files_are_up_to_date(path):
    built = gen.build()[path.name]
    stored = load(path)
    assert [v["name"] for v in stored] == [v["name"] for v in built]
    for s, b in zip(stored, built, strict=True):
        assert s["kind"] == b["kind"] and s["params"] == b["params"] and s["freqs"] == b["freqs"]
        assert np.allclose(s["coeffs"], b["coeffs"], rtol=0, atol=COEFF_TOL) if s["coeffs"] else not b["coeffs"]
        assert np.allclose(s["expected_db"], b["expected_db"], atol=1e-4)


@pytest.mark.parametrize(("file", "vector"), VECTORS, ids=lambda x: x if isinstance(x, str) else x["name"])
def test_vector_shape(file, vector):
    assert {"name", "kind", "params", "coeffs", "freqs", "expected_db"} <= set(vector)
    assert len(vector["expected_db"]) == len(vector["freqs"])
    assert all(len(row) == 5 for row in vector["coeffs"])


@pytest.mark.parametrize(
    ("file", "vector"),
    [(f, v) for f, v in VECTORS if v["kind"] not in ("raw", "delay")],
    ids=lambda x: x if isinstance(x, str) else x["name"],
)
def test_design_reproduces_coefficients(file, vector):
    designed = gen.coeffs_for(vector["kind"], vector["params"])
    assert np.allclose(designed, vector["coeffs"], rtol=0, atol=COEFF_TOL)


@pytest.mark.parametrize(("file", "vector"), VECTORS, ids=lambda x: x if isinstance(x, str) else x["name"])
def test_response_matches(file, vector):
    coeffs = [tuple(c) for c in vector["coeffs"]]
    exact = r.filter_response(coeffs, vector["freqs"], float32=False).magnitude_db
    assert np.allclose(exact, vector["expected_db"], atol=DB_TOL)
    if "expected_db_float32" in vector:
        rounded = r.filter_response(coeffs, vector["freqs"]).magnitude_db
        assert np.allclose(rounded, vector["expected_db_float32"], atol=DB_TOL)


@pytest.mark.parametrize(("file", "vector"), VECTORS, ids=lambda x: x if isinstance(x, str) else x["name"])
def test_coefficients_are_stable(file, vector):
    assert all(is_stable(c) for c in vector["coeffs"])


def test_golden_coefficients_are_the_device_values():
    by_name = {v["name"]: v for v in load(gen.VECTORS_DIR / "golden.json")}
    assert by_name["BW2 LP 80 Hz (live device)"]["coeffs"] == [gen.GOLDEN_LP_80]
    assert by_name["BW2 HP 20 Hz (live device)"]["coeffs"] == [gen.GOLDEN_HP_20]
    # Exact equality, not just 1e-12: re-designing with identical params must produce an empty diff.
    assert gen.coeffs_for("lp2", by_name["BW2 LP 80 Hz (live device)"]["params"]) == [gen.GOLDEN_LP_80]
    assert gen.coeffs_for("hp2", by_name["BW2 HP 20 Hz (live device)"]["params"]) == [gen.GOLDEN_HP_20]


def test_golden_combined_rounds_to_doc_table():
    [combined] = [v for v in load(gen.VECTORS_DIR / "golden.json") if v["kind"] == "chain"]
    assert combined["freqs"] == gen.GOLDEN_FREQS
    assert [round(x, 1) for x in combined["expected_db"]] == combined["doc_db"] == gen.GOLDEN_DOC_DB


def test_section_8_2_cases():
    filters = {v["name"]: v for v in load(gen.VECTORS_DIR / "filters.json")}
    through = filters["Pass-through"]
    assert through["coeffs"] == [[1.0, 0.0, 0.0, 0.0, 0.0]]
    assert set(through["expected_db"]) == {0.0}
    assert set(through["expected_phase_deg"]) == {0.0}
    peq = filters["PEQ +6 dB @ 1 kHz Q 1"]
    assert peq["expected_db"][peq["freqs"].index(1000.0)] == pytest.approx(6.0, abs=0.01)
    [delay] = load(gen.VECTORS_DIR / "delay.json")
    assert delay["params"] == {"delay_ms": 9.0, "samples": 864, "payload": {"secs": 0, "nanos": 9_000_000}}
    assert np.allclose(delay["expected_group_delay_ms"], 9.0)
    rew = load(gen.VECTORS_DIR / "rew.json")
    assert [v["coeffs"][0] for v in rew] == gen.REW_FILTERS
