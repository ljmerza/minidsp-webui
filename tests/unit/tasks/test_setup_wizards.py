"""The pure parts of Set levels (design §14.6), Save or load a setup (§14.8) and Set up this device (§14.7)."""

import json

import pytest

from app.sync.session import BigRaise
from app.sync.summary import Thresholds
from app.web.task_adopt import custom_names, docs_from_json, named_warning, stash, unstash
from app.web.task_levels import (
    CLOSE,
    HOT,
    NONE,
    OK,
    QUIET,
    UNKNOWN,
    done_lines,
    input_feedback,
    output_results,
    parse_counts,
    parse_readings,
)
from app.web.task_profiles import answers, confirm_value, confirmed_from
from tests.unit.sync.test_summary import NAMES, meta, run, walkthrough

TH = Thresholds()

# --- Set levels -------------------------------------------------------------------------------------------------------


def test_readings_parse_with_gaps_and_typographic_minus():
    assert parse_readings("-8.2,,−120,junk", 5) == [-8.2, None, -120.0, None, None]
    assert parse_readings("3", 1) == [0.0]  # a meter never reads above 0 dBFS
    assert parse_counts("2,x,-1", 4) == [2, 0, 0, 0]


@pytest.mark.parametrize(
    ("peak", "clips", "status", "text"),
    [
        (-8.0, 0, OK, "In 1 peaks at −8 dBFS"),
        (-2.0, 0, CLOSE, "In 1 peaks at −2 dBFS: just below CLIP."),
        (-0.5, 0, HOT, "In 1 hits CLIP: the signal arriving is too hot."),
        (-6.0, 1, HOT, "In 1 hits CLIP: the signal arriving is too hot."),
        (-30.0, 0, QUIET, "In 1 peaks at −30 dBFS: on the quiet side, which is fine."),
        (-60.0, 0, NONE, "In 1: no signal."),  # the mock's flat −60 dBFS isn't signal
        (None, 0, NONE, "In 1: no signal."),
    ],
)
def test_input_feedback_against_the_target_zone(peak, clips, status, text):
    s = run(walkthrough())
    reading = input_feedback(s, [peak, None], [clips, 0], TH, gain_min=-72.0)[0]
    assert (reading.status, reading.text) == (status, text)
    if status == HOT:
        assert "the miniDSP can't undo clipping that happens before it" in reading.detail
        assert [f.values for f in reading.fixes] == [{"inputs.0.gain_db": -3.0}]


def test_without_readings_the_inputs_cant_be_told():
    s = run(walkthrough())
    assert {r.status for r in input_feedback(s, [], [], TH, gain_min=-72.0, measured=False)} == {UNKNOWN}


def test_an_output_that_clipped_gets_three_fixes():
    s = run(walkthrough())
    results = {r.index: r for r in output_results(s, [-0.3, -40.0, None, None], [2, 0, 0, 0], TH, gain_min=-72.0)}
    shakers = results[0]
    assert shakers.status == HOT and shakers.text == "Shakers clipped twice at +16.0 dB total."
    assert shakers.detail == s.outputs[0].headroom.text
    by_id = {f.id: f for f in shakers.fixes}
    assert by_id["out"].values == {"outputs.0.gain_db": 9.0}
    assert by_id["out"].label == "Lower Shakers by 3 dB"
    assert by_id["route"].values == {"inputs.0.routing.0.gain_db": 1.0}
    assert by_id["comp"].values == {
        "outputs.0.compressor.bypass": False,
        "outputs.0.compressor.threshold_db": -3.0,
        "outputs.0.compressor.ratio": 10.0,
    }
    assert by_id["comp"].label == "Add a compressor at −3 dBFS…"
    assert results[1].status == OK and results[1].text == "Sub peaks at −40 dBFS, no CLIP"
    assert (results[2].status, results[2].text) == (NONE, "Out 3: no signal (it is muted).")


def test_unused_outputs_are_left_out_of_the_results():
    flags = {
        **NAMES,
        ("output", 2): meta("output", 2, None, unused=True),
        ("output", 3): meta("output", 3, None, unused=True),
    }
    s = run(walkthrough(), flags)
    assert [r.index for r in output_results(s, [None] * 4, [0] * 4, TH, gain_min=-72.0)] == [0, 1]


def test_done_lines():
    s = run(walkthrough())
    results = output_results(s, [-10.0, -20.0, None, None], [0, 0, 0, 0], TH, gain_min=-72.0)
    assert done_lines([-8.0, None], results, -30.0, -22.0, TH) == [
        "Inputs peak around −8 dBFS.",
        "No output clipped.",
        "Usual volume −22 dB.",
    ]
    assert done_lines([], None, -30.0, None, TH) == [
        "The inputs weren't measured.",
        "The outputs weren't watched.",
        "Volume −30 dB.",
    ]


# --- Save or load a setup ---------------------------------------------------------------------------------------------


def test_profile_answers_default_to_save_and_to_load_with_a_profile():
    assert answers({}, 4, 0).do == "save"
    a = answers({"profile": "3", "target": "2", "when": "play"}, 4, 0)
    assert (a.do, a.profile, a.target, a.when) == ("load", 3, 1, "play")
    assert answers({"target": "9"}, 4, 0).target is None


def test_the_ticked_raises_come_back_as_what_was_confirmed():
    raise_ = {"path": "outputs.0.gain_db", "delta_db": 12.0, "kind": "gain"}
    params = {"ok_0": confirm_value(raise_), "ok_1": "f|2", "ok_2": "r|broken", "other": "x"}
    confirmed = confirmed_from(params)
    assert confirmed.raises == (BigRaise("outputs.0.gain_db", 12.0, "gain"),)
    assert confirmed.fir_loads == frozenset({2})
    # It covers that raise, not a bigger one (the state changed since the list was made).
    assert confirmed.covers([BigRaise("outputs.0.gain_db", 12.0, "gain")], [])
    assert not confirmed.covers([BigRaise("outputs.0.gain_db", 18.0, "gain")], [])


# --- Set up this device ---------------------------------------------------------------------------------------------


def test_a_custom_name_is_a_sign_the_device_was_set_up_before():
    assert named_warning({}) is None
    assert named_warning({("output", 0): meta("output", 0, "Out 1")}) is None  # the default label isn't custom
    assert named_warning(NAMES) == (
        "You named Out 1 “Shakers” and Out 2 “Sub”, so it was probably set up before. "
        "Choose another option unless you've reset it."
    )
    many = {("input", i): meta("input", i, f"Player {i}") for i in range(2)} | NAMES
    assert custom_names(many)[0] == ("Out 1", "Shakers")  # outputs first
    assert named_warning(many).startswith("You named Out 1 “Shakers”, Out 2 “Sub”, In 1 “Player 0” and 1 more,")


def test_files_for_set_up_a_profile_export_or_a_backup():
    config = {"inputs": [], "outputs": []}
    docs, label = docs_from_json({"format": "minidsp-webui/profile", "name": "Movies", "config": config}, 1, "2x4HD")
    assert docs == {None: config} and label == "profile “Movies”"
    with pytest.raises(ValueError, match="is for a 4x10HD"):
        docs_from_json({"format": "minidsp-webui/profile", "product_name": "4x10HD", "config": config}, 1, "2x4HD")
    with pytest.raises(ValueError, match="isn't a backup or profile"):
        docs_from_json([1, 2], 1, "2x4HD")
    with pytest.raises(ValueError, match="can't be used"):
        docs_from_json(json.loads('{"format": "something else"}'), 1, "2x4HD")


def test_the_stash_keeps_a_file_for_an_hour():
    from app.web.task_adopt import STASH_TTL_S, Source

    token = stash(Source("script", "x", script="output 0 gain 1"), now=1000.0)
    assert unstash(token, now=1000.0 + STASH_TTL_S - 1).script == "output 0 gain 1"
    assert unstash(token, now=1000.0 + STASH_TTL_S + 1) is None
    assert unstash("nope") is None


def test_load_change_lines_lead_with_the_raises_and_levels():
    from app.web.task_profiles import change_rank

    paths = ["outputs.0.peq.0", "inputs.0.routing.0.gain_db", "outputs.1.delay_ms", "outputs.0.gain_db"]
    raised = {"outputs.0.gain_db"}
    assert sorted(paths, key=lambda p: change_rank(p, raised)) == [
        "outputs.0.gain_db",
        "outputs.1.delay_ms",
        "inputs.0.routing.0.gain_db",
        "outputs.0.peq.0",
    ]
