"""The compressor/FIR tab partials render with the output page's context, and the JS transfer curve matches Python."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1.compressor_fir import transfer_curve
from app.model.device_profile import M2X4HD
from app.web.templating import templates

from ..conftest import SERIAL

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which("node")


def render(name: str, **extra) -> str:
    ctx = {
        "root": "/base",
        "asset_version": "t",
        "serial": SERIAL,
        "preset": 1,
        "channel": {"index": 2, "name": "Out 3"},
        "session": SimpleNamespace(profile=M2X4HD),
        **extra,
    }
    return templates.env.get_template(name).render(ctx)


def test_compressor_partial():
    html = render("output/_compressor.html")
    assert f"/base/api/v1/devices/{SERIAL}/presets/1/outputs/2/compressor" in html
    assert "compressorEditor(" in html and 'hx-trigger="sse:sync, sse:desired"' in html
    assert "/base/static/js/compressor.js?v=t" in html and "uPlot.iife.min.js" in html
    assert "approximate, hard knee" in html


def test_fir_partial():
    html = render("output/_fir.html")
    assert f"/base/api/v1/devices/{SERIAL}/presets/1/outputs/2/fir" in html
    assert "firEditor(" in html and "/base/static/js/fir.js?v=t" in html
    assert "96 kHz mono" in html and "max 4096 taps" in html
    assert "audio drops out briefly" in html


def test_static_files_exist():
    for name in ("compressor.js", "fir.js"):
        assert (ROOT / "app" / "static" / "js" / name).is_file()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize(("threshold", "ratio"), [(-36, 50), (0, 50), (-20.5, 4), (-90, 2), (-40, 0.5)])
def test_js_transfer_curve_matches_python(threshold, ratio):
    script = (
        f"const c = require({json.dumps(str(ROOT / 'app' / 'static' / 'js' / 'compressor.js'))});"
        f"process.stdout.write(JSON.stringify(c.transferCurve({threshold}, {ratio})));"
    )
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True, timeout=30).stdout
    assert json.loads(out) == transfer_curve(threshold, ratio)
