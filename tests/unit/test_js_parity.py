"""Runs the node tests of the browser DSP mirror (tests/js/*.test.mjs) and skips when node isn't installed.

Besides the shared tests/vectors, this hands node a file of whole-document responses computed here by
app/dsp/response.py (MDSP_EXTRA_VECTORS), so the JS channel-chain composition (routing, gains, mute, polarity,
delay, electrical sum, unwrapped phase, group delay) is checked against the Python authority too.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.dsp import design as d
from app.dsp import response as r
from app.model.config_doc import ConfigDoc, CrossoverGroup, PeqBand, default_config_doc
from app.model.device_profile import M2X4HD
from app.model.filters import CrossoverDesign, FilterDesign

ROOT = Path(__file__).resolve().parents[2]
JS_TESTS = sorted((ROOT / "tests" / "js").glob("*.test.mjs"))
NODE = shutil.which("node")
MIN_NODE_MAJOR = 18  # node:test


def _node_major() -> int:
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True, check=True).stdout
    match = re.match(r"v(\d+)", out.strip())
    return int(match.group(1)) if match else 0


def _band(design: dict, bypass: bool = False) -> PeqBand:
    fd = FilterDesign(**design)
    return PeqBand(bypass=bypass, design=fd, compiled=d.design_filter(fd))


def _group(design: dict, bypass: bool = False) -> CrossoverGroup:
    cd = CrossoverDesign(**design)
    return CrossoverGroup(bypass=bypass, design=cd, compiled=d.design_crossover(cd, M2X4HD.crossover_biquads))


def sample_doc() -> ConfigDoc:
    doc = default_config_doc(M2X4HD)
    lp80 = {"kind": "lp", "family": "lr", "order": 4, "freq": 80.0}
    hp20 = {"kind": "hp", "family": "lr", "order": 4, "freq": 20.0}

    in0 = doc.inputs[0]
    in0.gain_db = -2.0
    in0.peq[0] = _band({"type": "peaking", "freq": 42.0, "q": 4.0, "gain_db": -3.5})
    in0.peq[1] = _band({"type": "lowshelf", "freq": 30.0, "q": 0.7, "gain_db": 6.0}, bypass=True)
    doc.inputs[1].routing[0].enabled = True
    doc.inputs[1].routing[0].gain_db = -3.0
    doc.inputs[1].peq[0] = _band({"type": "hp2", "freq": 15.0, "q": 0.6})

    out0 = doc.outputs[0]
    out0.gain_db = -4.0
    out0.peq[0] = _band({"type": "lowshelf", "freq": 35.0, "q": 0.9, "gain_db": 4.0})
    out0.crossover[0] = _group(lp80)

    out1 = doc.outputs[1]
    doc.inputs[0].routing[1].enabled = True
    out1.gain_db = 10.0
    out1.invert = True
    out1.delay_ms = 9.0
    out1.crossover[0] = _group(lp80)
    out1.crossover[1] = _group(hp20)
    out1.peq[3] = _band({"type": "notch", "freq": 60.0, "q": 10.0})

    doc.outputs[2].mute = True
    doc.outputs[3].fir.bypass = False
    doc.outputs[3].delay_ms = 1.234
    return ConfigDoc.model_validate(doc.model_dump())


def _expected(resp: r.Response) -> dict:
    out = resp.to_dict()
    out.pop("freqs")
    return out


def extra_vectors() -> list[dict]:
    doc = sample_doc()
    doc_json = doc.model_dump(mode="json")
    grid = r.freq_grid()
    sub = r.view_grid("sub", 256)
    vectors = []
    cases = [
        ("output 1, Input 1", [0], (0,), None, grid),
        ("output 1, all inputs", [0], None, None, grid),
        ("output 2 (LR4 80/20, 9 ms, inverted, notch)", [1], (0,), None, sub),
        ("sum of outputs 1+2", [0, 1], (0,), None, sub),
        ("all outputs, all inputs, master -10 dB", [0, 1, 2, 3], None, -10.0, grid),
    ]
    for name, outputs, inputs, master_db, freqs in cases:
        resp = r.system_response(doc, outputs, freqs, inputs=inputs, master_db=master_db)
        vectors.append(
            {
                "name": name,
                "kind": "system",
                "doc": doc_json,
                "outputs": outputs,
                "inputs": None if inputs is None else list(inputs),
                "master_db": master_db,
                "freqs": freqs.tolist(),
                "expected": _expected(resp),
            }
        )
    vectors.append(
        {
            "name": "input 1 PEQ",
            "kind": "input",
            "doc": doc_json,
            "input": 0,
            "freqs": grid.tolist(),
            "expected": _expected(r.input_response(doc, 0, grid)),
        }
    )
    shaker = [bq.as_tuple() for g in doc.outputs[1].crossover for bq in g.compiled]
    vectors.append(
        {
            "name": "shaker crossover on the full grid",
            "kind": "filter",
            "coeffs": [list(c) for c in shaker],
            "freqs": grid.tolist(),
            "expected": _expected(r.filter_response(shaker, grid)),
        }
    )
    return vectors


def test_sample_doc_exercises_the_chain():
    resp = r.system_response(sample_doc(), [0, 1, 2, 3], inputs=None)
    assert any("FIR is active" in w for w in resp.warnings)
    assert any("float32" in w for w in resp.warnings)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_js_dsp_mirror_matches_python(tmp_path):
    if _node_major() < MIN_NODE_MAJOR:
        pytest.skip(f"node >= {MIN_NODE_MAJOR} is needed for node:test")
    assert JS_TESTS, "tests/js/*.test.mjs not found"
    extra = tmp_path / "extra_vectors.json"
    extra.write_text(json.dumps(extra_vectors()))
    env = {**os.environ, "MDSP_EXTRA_VECTORS": str(extra)}
    proc = subprocess.run(
        [NODE, "--test", *[str(p) for p in JS_TESTS]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    failures = [line for line in output.splitlines() if line.lstrip().startswith("not ok")]
    assert proc.returncode == 0, "node tests failed:\n" + "\n".join(failures) + "\n\n" + output[-6000:]
    passed = re.search(r"^# pass (\d+)", output, re.MULTILINE)
    assert passed and int(passed.group(1)) > 100, output[-2000:]
    assert "whole-document responses" in output
