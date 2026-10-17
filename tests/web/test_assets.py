import hashlib
import math
import re

import pytest

from app.main import STATIC_DIR
from app.web.templating import fmt_db, fmt_duration, fmt_num, meter_fraction

VENDOR = STATIC_DIR / "vendor"


def _versions_table() -> list[tuple[str, str]]:
    text = (VENDOR / "VERSIONS.md").read_text()
    return re.findall(r"\| `([^`]+)` \| `([0-9a-f]{64})` \|", text)


def test_versions_md_lists_every_vendored_script_and_stylesheet():
    listed = {path for path, _ in _versions_table()}
    on_disk = {
        p.relative_to(VENDOR).as_posix() for p in VENDOR.rglob("*") if p.is_file() and p.suffix in (".js", ".css")
    }
    assert listed == on_disk


@pytest.mark.parametrize(("path", "sha256"), _versions_table())
def test_vendored_file_matches_recorded_sha256(path, sha256):
    assert hashlib.sha256((VENDOR / path).read_bytes()).hexdigest() == sha256


def test_no_cdn_references_in_templates_or_app_js():
    roots = [STATIC_DIR / "js", STATIC_DIR / "css", STATIC_DIR.parent / "templates"]
    for root in roots:
        for f in root.rglob("*"):
            if f.is_file():
                assert not re.search(r"(src|href)=\"https?://", f.read_text()), f


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-12.0, "−12.0"), (-0.04, "0.0"), (0.0, "0.0"), (3.25, "3.2"), (None, "—"), (math.nan, "—")],
)
def test_fmt_num(value, expected):
    assert fmt_num(value) == expected


def test_fmt_db():
    assert fmt_db(-12) == "−12.0 dB"
    assert fmt_db(6, signed=True) == "+6.0 dB"
    assert fmt_db(-18.04, unit="dBFS") == "−18.0 dBFS"
    assert fmt_db(None) == "—"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 s"),
        (45.9, "45 s"),
        (60, "1 min"),
        (12 * 60 + 59, "12 min"),
        (3600, "1 h"),
        (3 * 3600 + 12 * 60 + 5, "3 h 12 min"),
        (86_400, "1 d"),
        (2 * 86_400 + 4 * 3600 + 30 * 60, "2 d 4 h"),
        (None, "—"),
        (-1, "—"),
        (math.inf, "—"),
    ],
)
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


def test_meter_fraction_scale():
    assert meter_fraction(0) == 1.0
    assert meter_fraction(3) == 1.0  # clamped
    assert meter_fraction(-40) == pytest.approx(0.25)
    assert meter_fraction(-20) == pytest.approx(0.625)
    assert meter_fraction(-80) == pytest.approx(0.125)
    assert meter_fraction(-120) == 0.0
    assert meter_fraction(-200) == 0.0
    assert meter_fraction(None) == 0.0
    assert meter_fraction(-math.inf) == 0.0


def test_meter_scale_constants_match_the_js_mirror():
    js = (STATIC_DIR / "js" / "meters.js").read_text()
    for name, value in (("FLOOR_DB", "-120"), ("KNEE_DB", "-40"), ("KNEE_FRACTION", "0.25"), ("PEAK_HOLD_MS", "1500")):
        assert re.search(rf"const {name} = {re.escape(value)};", js), name
    assert re.search(r"const CLIP_DBFS = -1;", js)
