"""Make one louder's options (design §14.4) and the wizards' field-level Undo (§14.1)."""

import time

from app.web.task_louder import options, preselect, result_lines
from app.web.tasks import UNDO_WINDOW_S, UndoRecord, possessive, same_value, undo_plan
from tests.unit.sync.test_summary import route, run, walkthrough


def louder(data=None, n=0, direction="up", amount=3.0, volume=-30.0):
    s = run(data or walkthrough(), preset=0, master={"preset": 0, "volume": volume, "mute": False})
    found = options(s, n, direction, amount, gain_min=-72.0, volume=volume, volume_max=0.0, high_total=12.0)
    return s, {o.id: o for o in found}


def test_at_the_limit_the_output_level_is_out_and_the_route_is_preselected():
    s, opts = louder()
    assert not opts["output"].available
    assert opts["output"].reason == "Shakers' output level is at its +12 dB limit."
    route_opt = opts["routes"]
    assert route_opt.available and route_opt.values == {"inputs.0.routing.0.gain_db": 7.0}
    assert route_opt.label == "Raise the route In 1 → Shakers from +4 to +7 dB (output level stays at its +12 limit)"
    assert preselect(s.outputs[0], list(opts.values()), "up") == "routes"
    assert result_lines(s, 0, route_opt, -30.0) == [
        "Shakers: +16 → +19 dB total.",
        "Peaks above about −19 dBFS on In 1 will overload it.",
    ]


def test_below_the_limit_the_output_level_is_preselected():
    data = walkthrough()
    data["outputs"][0]["gain_db"] = 8.0
    s, opts = louder(data)
    assert opts["output"].label == "Raise Shakers' output level from +8 to +11 dB"
    assert opts["output"].values == {"outputs.0.gain_db": 11.0}
    assert preselect(s.outputs[0], list(opts.values()), "up") == "output"


def test_lowering_the_others_and_raising_volume():
    s, opts = louder()
    others = opts["others"]
    # Out 3 and Out 4 aren't flagged Not in use here, so they are lowered too.
    assert others.available and others.volume == -27.0
    assert others.values == {"outputs.1.gain_db": -3.0, "outputs.2.gain_db": -3.0, "outputs.3.gain_db": -3.0}
    assert others.recommended  # +19 dB would be at or above the +12 dB caution
    assert result_lines(s, 0, others, -30.0)[0] == "Shakers: +16 dB total (unchanged)."
    # No room for Volume.
    assert not louder(volume=-1.0)[1]["others"].available
    assert "no room" in louder(volume=-1.0)[1]["others"].reason


def test_quieter_and_unrouted():
    _, opts = louder(direction="down")
    assert opts["output"].values == {"outputs.0.gain_db": 9.0} and "others" not in opts
    _, opts = louder(n=1)  # the Sub has no route
    assert not opts["routes"].available and opts["routes"].reason == "No input is routed to Sub."
    _, opts = louder(route(walkthrough(), 0, 1, gain_db=11.0), n=1)
    assert not opts["routes"].available  # 11 + 3 passes the +12 limit


def test_a_route_change_elsewhere_is_left_alone_by_undo():
    changes = {"inputs.0.routing.1.enabled": (False, True), "inputs.0.routing.1.gain_db": (-6.0, 0.0)}
    restore, skipped = undo_plan(changes, {"inputs.0.routing.1.enabled": True, "inputs.0.routing.1.gain_db": -3.0})
    assert restore == {"inputs.0.routing.1.enabled": False}
    assert skipped == ["inputs.0.routing.1.gain_db"]
    # Nothing to do when it is already back; a path that vanished is skipped.
    assert undo_plan({"a": (1.0, 2.0)}, {"a": 1.0}) == ({}, ["a"])
    assert undo_plan({"a": (1.0, 2.0)}, {}) == ({}, ["a"])
    assert undo_plan({"a": (1.0, 2.0)}, {"a": 2.004}) == ({"a": 1.0}, [])


def test_same_value_compares_numbers_and_filter_objects():
    assert same_value({"b0": 1.0, "bypass": True}, {"b0": 1.000001, "bypass": True})
    assert not same_value({"b0": 1.0}, {"b0": 1.5})
    assert same_value(True, True)
    assert not same_value(False, True)


def test_undo_record_round_trips_and_merges():
    a = UndoRecord(1, {"x": (0.0, 1.0)}, {"volume": (-30.0, -27.0)}, at=100.0)
    b = UndoRecord(1, {"x": (1.0, 2.0), "y": (False, True)}, at=110.0)
    merged = a.merged(b)
    assert merged.edits == {"x": (0.0, 2.0), "y": (False, True)} and merged.at == 110.0
    back = UndoRecord.from_json(merged.to_json())
    assert back is not None and back.edits == merged.edits and back.master == {"volume": (-30.0, -27.0)}
    assert UndoRecord.from_json("not json") is None and UndoRecord.from_json('{"p": -1}') is None
    assert UndoRecord.from_json('{"p": 0, "m": {"preset": [0, 1]}}').master == {}  # only volume / mute
    fresh = UndoRecord(0, {"x": (0, 1)})
    assert UNDO_WINDOW_S - 1 < fresh.seconds_left() <= UNDO_WINDOW_S
    assert UndoRecord(0, at=time.time() - 60).seconds_left() == 0.0


def test_possessive():
    assert possessive("Sub") == "Sub's" and possessive("Shakers") == "Shakers'"
