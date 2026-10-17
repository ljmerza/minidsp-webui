"""Runs the node tests of static/js/system.js's pure helpers; skipped when node isn't installed."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_system_js_helpers():
    major = re.match(r"v(\d+)", subprocess.run([NODE, "--version"], capture_output=True, text=True).stdout)
    if not major or int(major.group(1)) < 18:
        pytest.skip("node >= 18 is needed for node:test")
    proc = subprocess.run(
        [NODE, "--test", str(Path(__file__).with_name("system.test.mjs"))],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output[-4000:]
    passed = re.search(r"^# pass (\d+)", output, re.MULTILINE)
    assert passed and int(passed.group(1)) >= 5, output[-2000:]
