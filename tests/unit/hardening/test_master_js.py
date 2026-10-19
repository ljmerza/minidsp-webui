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
    assert case["dialog"] == (
        "Drop the volume from -20 to -120 dB? That's 100 dB quieter in one step. The app asks before any drop of "
        "more than 20 dB, so a slip of the slider can't silence everything without you noticing."
    )
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
    assert cases["big raise"]["dialog"] == (
        "Raise the volume from -20 to -10 dB? That's 10 dB louder in one step. The app asks before any raise of more "
        "than 6 dB."
    )


def test_the_db_weighted_slider_sends_the_db_of_its_position(cases):
    """Design §10.3: the slider's value is a track position; the request carries the dB and the same confirm rule."""
    case = cases["slider drop accepted"]
    assert case["prevented"] and case["issued"] and case["confirm"] == "true"
    assert case["dialog"].startswith("Drop the volume from -10 to -40 dB?")
    assert case["volume"] == "-40"
    cancelled = cases["slider drop cancelled"]
    assert not cancelled["issued"] and float(cancelled["value"]) == pytest.approx(858.333, abs=1e-3)  # back at -10 dB
    small = cases["slider small step"]
    assert small["dialog"] is None and small["volume"] == "-10.5"


def test_typed_minus_10(cases):
    """P12's "typed −10 and got no confirm": from −3.5 that's a 6.5 dB drop (under 20), from −30 a 20 dB raise."""
    assert cases["typed -10 from -3.5"]["dialog"] is None
    assert cases["typed -10 from -30"]["dialog"].startswith("Raise the volume from -30 to -10 dB?")
