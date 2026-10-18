"""The UI's big-raise / big-drop confirmation in app/static/js/master.js, run in node with a stub DOM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DRIVER = Path(__file__).with_name("js") / "master_confirm.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture(scope="module")
def cases() -> dict[str, dict]:
    out = subprocess.run(
        [NODE, str(DRIVER), str(ROOT / "app" / "static" / "js" / "master.js")],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    return {case["name"]: case for case in map(json.loads, out.splitlines())}


def test_big_drop_asks_and_sends_confirm(cases):
    case = cases["big drop accepted"]
    assert case["prevented"] and case["issued"]
    assert case["dialog"] == "Drop the volume from -20.0 to -120.0 dB? That's 100.0 dB quieter."
    assert case["confirm"] == "true"  # the server's UI guard accepts it


def test_cancelled_drop_leaves_the_volume(cases):
    case = cases["big drop cancelled"]
    assert case["prevented"] and not case["issued"]
    assert case["value"] == -20  # the slider goes back
    assert case["confirm"] is None


def test_drop_at_the_threshold_and_small_steps_pass(cases):
    for name in ("drop at the threshold", "small step"):
        assert cases[name]["dialog"] is None and not cases[name]["prevented"]


def test_big_raise_still_asks(cases):
    assert cases["big raise"]["dialog"] == "Raise the volume from -20.0 to -10.0 dB? That's 10.0 dB louder."
