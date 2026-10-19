"""The dB-weighted Volume slider (phase 9 design §10.3): app/web/volume_scale.py, the server's mirror of
``mdsp.volumeScale`` in static/js/master.js (tests/js/master.test.mjs pins the browser side to the same numbers)."""

import math

import pytest

from app.web.volume_scale import POS_MAX, TICKS_DB, from_pos, percent, slider_view, to_pos

TICK_POSITIONS = {-60.0: 150.0, -40.0: 433.333, -30.0: 575.0, -20.0: 716.667, -10.0: 858.333, 0.0: 1000.0}


def test_the_bottom_15_percent_holds_minus_127_to_minus_60():
    assert to_pos(-127) == 0
    assert to_pos(-60) == pytest.approx(150)
    assert to_pos(0) == POS_MAX
    assert to_pos(-30) == pytest.approx(575)


@pytest.mark.parametrize("tick", TICKS_DB)
def test_exact_at_the_ticks_both_ways(tick):
    pos = to_pos(tick)
    assert pos == pytest.approx(TICK_POSITIONS[tick], abs=1e-3)
    assert from_pos(pos) == tick
    assert from_pos(round(pos)) == tick


def test_monotonic_and_every_half_db_round_trips():
    positions = [to_pos(-127 + k * 0.5) for k in range(255)]
    assert all(b > a for a, b in zip(positions, positions[1:], strict=False))
    for k in range(255):
        db = -127 + k * 0.5
        assert from_pos(to_pos(db)) == db
    values = [from_pos(p / 2) for p in range(2001)]
    assert all(b >= a for a, b in zip(values, values[1:], strict=False))


def test_clamps_rounds_and_has_no_negative_zero():
    assert to_pos(-200) == 0 and to_pos(12) == POS_MAX and to_pos(math.nan) == 0
    assert from_pos(-5) == -127 and from_pos(5000) == 0
    assert math.copysign(1, from_pos(999.99)) == 1
    assert from_pos(to_pos(-40.2)) == -40
    assert from_pos(to_pos(-40.25)) == -40  # half steps round up, as Math.round in the browser


def test_a_range_without_the_knee_is_linear():
    assert to_pos(-25, -50, 0) == 500
    assert from_pos(500, -50, 0) == -25


def test_slider_view_ticks_band_and_position():
    view = slider_view(-20.0, -127.0, 0.0, (-40.0, -10.0))
    assert view["pos"] == 716.667 and view["max"] == POS_MAX
    assert [t["label"] for t in view["ticks"]] == ["−60", "−40", "−30", "−20", "−10", "0"]
    assert [t["at"] for t in view["ticks"]] == [15.0, 43.33, 57.5, 71.67, 85.83, 100.0]
    assert view["band"] == {"left": 43.33, "width": 42.5, "low": -40.0, "high": -10.0}
    assert percent(-40, -127, 0) == 43.33


def test_slider_view_without_a_volume_or_a_usable_band():
    view = slider_view(None, -127.0, 0.0, (-10.0, -40.0))
    assert view["pos"] == 0 and view["band"] is None
    assert slider_view(-20.0, -127.0, 0.0, None)["band"] is None
    # the band is clipped to the range
    assert slider_view(-20.0, -127.0, 0.0, (-200.0, 10.0))["band"] == {
        "left": 0.0,
        "width": 100.0,
        "low": -127.0,
        "high": 0.0,
    }
