"""Plain-language summaries and health rules (app/sync/summary.py; phase 9 design sections 6 and 7)."""

import json
from typing import Any

import pytest

from app.config import Settings
from app.model.config_doc import ConfigDoc, default_config_doc
from app.model.device_profile import M2X4HD
from app.model.filters import Biquad, CrossoverDesign, FilterDesign
from app.state.repo import ChannelMeta
from app.sync.summary import Levels, Thresholds, summarize

SERIAL = 1
PEAK = {"b0": 0.99, "b1": -1.9, "b2": 0.9, "a1": 1.9, "a2": -0.9}  # any non-flat, non-empty biquad
ZERO = {"b0": 0.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}
FLAT = Biquad().model_dump()


def meta(kind: str, idx: int, name: str | None = None, **flags: bool) -> ChannelMeta:
    return ChannelMeta(SERIAL, kind, idx, name, None, None, **flags)


NAMES = {("output", 0): meta("output", 0, "Shakers"), ("output", 1): meta("output", 1, "Sub")}


def xo(kind: str, freq: float, order: int = 4, family: str = "lr", freq_hi: float | None = None) -> dict:
    design = CrossoverDesign(kind=kind, family=family, order=order, freq=freq, freq_hi=freq_hi)
    return {"bypass": False, "design": design.model_dump(), "compiled": [PEAK] * 4}


def peq(type_: str = "peaking", freq: float = 40.0, gain_db: float | None = -4.0, q: float | None = 4.0) -> dict:
    kwargs: dict[str, Any] = {"type": type_, "freq": freq}
    if gain_db is not None:
        kwargs["gain_db"] = gain_db
    if q is not None and type_ not in ("lp1", "hp1", "ap1"):
        kwargs["q"] = q
    return {"bypass": False, "design": FilterDesign(**kwargs).model_dump(), "compiled": PEAK, "external": False}


def base(*, fill: bool = True) -> dict:
    """The factory layout with In 1 routed nowhere; ``fill`` routes In 2 to Out 2-4 so that only Out 1 (and what a
    test routes itself) is in question. Tests route what they need."""
    data = default_config_doc(M2X4HD).model_dump()
    for i, ch in enumerate(data["inputs"]):
        for o, cell in enumerate(ch["routing"]):
            cell["enabled"] = fill and i == 1 and o > 0
    return data


def route(data: dict, i: int, o: int, gain_db: float = 0.0) -> dict:
    data["inputs"][i]["routing"][o] = {"enabled": True, "gain_db": gain_db}
    return data


def walkthrough() -> dict:
    """The mock state of the phase 9 walkthroughs (design 4.1 / 6.2): In 1 feeds Shakers (+4 dB route, +12 dB output,
    20-80 Hz LR4, a 4 dB cut at 40 Hz, 8.75 ms), Out 3 and Out 4 (both muted); the Sub's route was turned off; In 2
    feeds nothing."""
    data = base(fill=False)
    route(data, 0, 0, 4.0)
    route(data, 0, 2)
    route(data, 0, 3)
    shakers = data["outputs"][0]
    shakers.update(gain_db=12.0, delay_ms=8.75)
    shakers["crossover"] = [xo("hp", 20), xo("lp", 80)]
    shakers["peq"][0] = peq()
    data["outputs"][1]["peq"][0] = peq()
    data["outputs"][2]["mute"] = True
    data["outputs"][3]["mute"] = True
    return data


def run(data: dict, channel_meta: dict | None = None, **kwargs: Any):
    doc = ConfigDoc.model_validate(data)
    return summarize(
        doc,
        M2X4HD,
        NAMES if channel_meta is None else channel_meta,
        kwargs.pop("settings", None),
        kwargs.pop("statuses", {}),
        kwargs.pop("last_verify", None),
        kwargs.pop("levels", None),
        **kwargs,
    )


def codes(summary) -> list[str]:
    return [p.code for p in summary.problems]


# --- the worked examples (acceptance: the mock state renders exactly these) -------------------------------------------


def test_walkthrough_state_renders_the_worked_examples():
    s = run(walkthrough(), preset=0, master={"preset": 0, "volume": -30.0, "mute": False})
    shakers, sub, out3, out4 = s.outputs
    in1, in2 = s.inputs
    assert shakers.sentence == "Plays 20–80 Hz (steep), fed by In 1. 1 EQ cut at 40 Hz. Waits 8.75 ms."
    assert sub.sentence == "Plays full range, fed by nothing. 1 EQ cut at 40 Hz."
    assert (sub.badge, sub.badge_glyph, sub.badge_word) == ("silent", "⛔", "Silent")
    assert out3.sentence == "Muted. Would play full range, fed by In 1."
    assert out3.badge == out4.badge == "muted"
    assert in1.sentence == "Level 0 dB, no EQ. Sent to Shakers (+4 dB), Out 3 and Out 4."
    assert in2.sentence == "Not sent to any output."

    assert (shakers.total.text, shakers.total.breakdown) == ("+16.0 dB", "route +4 + output +12")
    assert shakers.total.at_limit and not shakers.total.route_at_limit
    assert shakers.headroom.text == "Peaks above about −16 dBFS on In 1 will overload Shakers."
    assert shakers.headroom.level == "caution" and shakers.headroom.peak_dbfs == -16.0
    assert (sub.total.text, sub.total.breakdown) == ("0.0 dB", "output 0")

    assert codes(s) == ["no_route", "high_total", "muted_routed", "muted_routed", "input_unused"]
    first = s.problems[0]
    assert first.text == "Sub is silent: no input is routed to it."
    assert [a.label for a in first.actions] == ["Route In 1 → Sub", "Open Routing"]
    assert first.actions[0].params == {"input": 0, "output": 1}
    assert s.problems[1].text == "Shakers: +16 dB total. Peaks above about −16 dBFS on In 1 will overload it."
    assert s.counts == {"problem": 1, "caution": 1, "info": 3}


def test_not_in_use_outputs_fold_to_one_line_and_hide_their_notes():
    channel_meta = {**NAMES, ("output", 2): meta("output", 2, unused=True), ("input", 1): meta("input", 1, unused=True)}
    s = run(walkthrough(), channel_meta)
    out3, out4 = s.outputs[2], s.outputs[3]
    assert out3.sentence == "Not in use: muted."
    assert (out3.badge, out3.badge_glyph, out3.badge_word) == ("not_in_use", "◌", "Not in use")
    assert out4.badge == "muted"  # only the flagged one
    assert codes(s) == ["no_route", "high_total", "muted_routed"]  # Out 3's and In 2's info notes are gone
    assert s.problems[-1].index == 3


def test_unused_output_without_a_route_is_not_reported_silent():
    channel_meta = {**NAMES, ("output", 1): meta("output", 1, "Sub", unused=True)}
    s = run(walkthrough(), channel_meta)
    assert "no_route" not in codes(s)
    assert s.outputs[1].badge == "ready"


def test_tactile_flag_is_passed_through_without_changing_the_words():
    channel_meta = {**NAMES, ("output", 0): meta("output", 0, "Shakers", tactile=True)}
    tactile = run(walkthrough(), channel_meta).outputs[0]
    plain = run(walkthrough()).outputs[0]
    assert tactile.tactile and not plain.tactile
    assert tactile.sentence == plain.sentence and tactile.badge == plain.badge


# --- sentence templates (6.2) -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crossover", "clause"),
    [
        ([xo("lp", 80)], "Plays bass below 80 Hz (steep)"),
        ([xo("hp", 20), xo("lp", 80)], "Plays 20–80 Hz (steep)"),
        ([xo("hp", 30, order=2, family="bw")], "Plays everything above 30 Hz (gentle)"),
        ([xo("hp", 20, order=2, family="bw"), xo("lp", 80)], "Plays 20–80 Hz (gentle below, steep above)"),
        ([xo("bp", 25, order=8, freq_hi=90)], "Plays 25–90 Hz (very steep)"),
        ([xo("lp", 2000)], "Plays everything below 2 kHz (steep)"),
        ([xo("hp", 500), xo("lp", 2500)], "Plays 500 Hz–2.5 kHz (steep)"),
        ([{**xo("lp", 80), "bypass": True}], "Plays full range, unprocessed"),
        ([{"bypass": False, "design": None, "compiled": [PEAK, FLAT, FLAT, FLAT]}], "Plays through a custom filter"),
        ([{"bypass": False, "design": None, "compiled": [FLAT] * 4}], "Plays full range, unprocessed"),
    ],
)
def test_band_clause(crossover, clause):
    data = route(base(), 0, 0)
    groups = crossover + [{"bypass": True, "design": None, "compiled": [FLAT] * 4}] * (2 - len(crossover))
    data["outputs"][0]["crossover"] = groups
    assert run(data).outputs[0].sentence == f"{clause}, fed by In 1."


def test_high_pass_above_low_pass_plays_nothing():
    data = route(base(), 0, 0)
    data["outputs"][0]["crossover"] = [xo("hp", 100), xo("lp", 80)]
    s = run(data)
    out = s.outputs[0]
    assert out.sentence == "Plays nothing: its high-pass (100 Hz) is above its low-pass (80 Hz). Fed by In 1."
    assert out.badge == "silent"
    assert s.problems[0].code == "band_empty"
    assert s.problems[0].text == "Shakers plays nothing: its high-pass (100 Hz) is above its low-pass (80 Hz)."
    assert s.problems[0].actions[0].href == "/outputs/1?tab=crossover"
    assert out.parts["band"].state == "problem"


@pytest.mark.parametrize(
    ("routes", "sources"),
    [
        ([], "nothing"),
        ([(0, 4.0)], "In 1"),
        ([(0, 0.0), (1, 0.0)], "In 1 and In 2 (mono)"),
        ([(0, 4.0), (1, -6.0)], "In 1 (+4 dB) and In 2 (−6 dB)"),
    ],
)
def test_sources_clause(routes, sources):
    data = base(fill=False)
    for i, gain in routes:
        route(data, i, 0, gain)
    assert (
        run(data)
        .outputs[0]
        .sentence.startswith(f"Plays full range, {'unprocessed, ' if routes else ''}fed by {sources}.")
    )


def test_muted_source_is_named():
    data = route(base(), 0, 0)
    data["inputs"][0]["mute"] = True
    assert "fed by In 1 (muted)." in run(data).outputs[0].sentence


@pytest.mark.parametrize(
    ("bands", "clause"),
    [
        ([peq()], "1 EQ cut at 40 Hz."),
        ([peq(freq=63), peq(freq=40)], "2 EQ cuts (40, 63 Hz)."),
        ([peq(freq=25, gain_db=3)], "1 EQ boost at 25 Hz."),
        ([peq("lowshelf", freq=30, gain_db=3, q=0.7)], "Bass shelf +3 dB below 30 Hz."),
        ([peq(), peq(freq=25, gain_db=2)], "1 EQ cut at 40 Hz and 1 EQ boost at 25 Hz."),
        ([peq("notch", freq=60, gain_db=None)], "1 EQ notch at 60 Hz."),
        ([peq(freq=f) for f in (30, 40, 50, 60, 70)], "5 EQ bands on."),
        ([peq(gain_db=0)], None),  # a 0 dB peak changes nothing
        ([{**peq(), "bypass": True}], None),
        (
            [{"bypass": False, "design": FilterDesign(type="raw", raw=Biquad(**PEAK)).model_dump(), "compiled": PEAK}],
            "1 custom EQ filter.",
        ),
    ],
)
def test_eq_clause(bands, clause):
    data = route(base(), 0, 0)
    for k, band in enumerate(bands):
        data["outputs"][0]["peq"][k] = band
    sentence = run(data).outputs[0].sentence
    if clause is None:
        assert sentence == "Plays full range, unprocessed, fed by In 1."
    else:
        assert sentence == f"Plays full range, fed by In 1. {clause}"


def test_delay_polarity_and_compressor_clauses():
    data = route(base(), 0, 0)
    out = data["outputs"][0]
    out.update(delay_ms=1.5, invert=True)
    out["compressor"] = {"bypass": False, "threshold_db": -36.0, "ratio": 4.0, "attack_ms": 10.0, "release_ms": 100.0}
    s = run(data).outputs[0]
    assert s.sentence == (
        "Plays full range, fed by In 1. Waits 1.5 ms. Polarity flipped (180°). Compressor on above −36 dBFS (4:1)."
    )
    assert (s.parts["delay"].label, s.parts["polarity"].label) == ("Delay 1.5", "Polarity 180°")
    assert s.parts["compressor"].label == "Comp. −36 dBFS 4:1"


def test_default_blocks_are_quiet():
    parts = run(route(base(), 0, 0)).outputs[0].parts
    assert {
        k: (p.label, p.state) for k, p in parts.items() if k in ("band", "eq", "delay", "polarity", "compressor")
    } == {
        "band": ("Full range", "default"),
        "eq": ("EQ none", "default"),
        "delay": ("Delay 0", "default"),
        "polarity": ("Polarity 0°", "default"),
        "compressor": ("Comp. off", "default"),
    }


def test_input_sentences():
    data = route(base(fill=False), 0, 0, 4.0)
    route(data, 0, 1)
    data["inputs"][0]["gain_db"] = -2.0
    data["inputs"][0]["peq"][0] = peq()
    data["inputs"][1]["mute"] = True
    in1, in2 = run(data).inputs
    assert in1.sentence == "Level −2 dB, 1 EQ band. Sent to Shakers (+4 dB) and Sub."
    assert [(t.output, t.name, t.route_db) for t in in1.sends_to] == [(0, "Shakers", 4.0), (1, "Sub", 0.0)]
    assert in2.sentence == "Muted. Not sent to any output."


# --- totals (6.3) -----------------------------------------------------------------------------------------------------


def test_two_routes_show_a_range_and_each_breakdown():
    data = route(base(), 0, 0, 4.0)
    route(data, 1, 0, -2.0)
    data["outputs"][0]["gain_db"] = 12.0
    total = run(data).outputs[0].total
    assert total.text == "+10 to +16 dB"
    assert total.breakdown == "In 1: route +4 + output +12 · In 2: route −2 + output +12"
    assert (total.min, total.max, total.input) == (10.0, 16.0, 0)


def test_input_level_is_part_of_the_total():
    data = route(base(), 0, 0, 4.0)
    data["inputs"][0]["gain_db"] = -2.0
    data["outputs"][0]["gain_db"] = 12.0
    total = run(data).outputs[0].total
    assert (total.text, total.breakdown) == ("+14.0 dB", "In 1 level −2 + route +4 + output +12")


def test_limits_follow_the_settings_ceiling():
    data = route(base(), 0, 0, 6.0)
    data["outputs"][0]["gain_db"] = 6.0
    channel_meta = {("output", 0): ChannelMeta(SERIAL, "output", 0, "Shakers", None, 6.0)}
    total = run(data, channel_meta).outputs[0].total
    assert (total.ceiling, total.at_limit, total.route_at_limit) == (6.0, True, True)
    assert run(data, {}).outputs[0].total.at_limit is False  # the profile's +12 dB


def test_high_total_threshold_comes_from_settings():
    data = route(base(), 0, 0, 0.0)
    data["outputs"][0]["gain_db"] = 11.5
    s = run(data)
    assert "high_total" not in codes(s) and s.outputs[0].headroom.level == "info"
    assert s.outputs[0].badge == "ready"
    s = run(data, settings=Settings(_env_file=None, guidance_high_total_db=10.0))
    assert "high_total" in codes(s) and s.outputs[0].badge == "check"
    assert s.outputs[0].parts["level"].state == "caution"
    assert run(route(base(), 0, 0, -3.0)).outputs[0].headroom.level == "none"


def test_muted_output_gets_no_headroom_caution():
    data = walkthrough()
    data["outputs"][0]["mute"] = True
    assert "high_total" not in codes(run(data))


# --- health rules (section 7) -----------------------------------------------------------------------------------------


def test_only_source_muted():
    data = route(base(), 0, 0)
    data["inputs"][0]["mute"] = True
    s = run(data)
    assert s.problems[0].code == "only_source_muted"
    assert s.problems[0].text == "Shakers only gets In 1, which is muted."
    assert s.problems[0].actions[0].label == "Unmute In 1" and s.problems[0].actions[0].params == {"input": 0}
    assert s.outputs[0].badge == "silent" and s.outputs[0].badge_reason == s.problems[0].text


@pytest.mark.parametrize(
    ("where", "sentence", "text"),
    [
        (
            "crossover",
            "Silent: its band filter is empty. Fed by In 1.",
            "Shakers is silent: its band filter 1 is empty (all zeros).",
        ),
        ("peq", "Silent: EQ band 3 is empty. Fed by In 1.", "Shakers is silent: its EQ band 3 is empty (all zeros)."),
    ],
)
def test_filter_empty(where, sentence, text):
    data = route(base(), 0, 0)
    if where == "crossover":
        data["outputs"][0]["crossover"][0] = {"bypass": False, "design": None, "compiled": [ZERO, FLAT, FLAT, FLAT]}
    else:
        data["outputs"][0]["peq"][2] = {**peq(), "compiled": ZERO}
    s = run(data)
    out = s.outputs[0]
    assert out.sentence == sentence and out.badge == "silent"
    assert s.problems[0].code == "filter_empty" and s.problems[0].text == text
    assert s.problems[0].actions[0].label == "Reset this filter to flat"


def test_empty_filter_that_is_bypassed_is_harmless():
    data = route(base(), 0, 0)
    data["outputs"][0]["peq"][2] = {**peq(), "bypass": True, "compiled": ZERO}
    assert run(data).outputs[0].badge == "ready"


def test_master_mute_silences_every_output_once():
    s = run(walkthrough(), master={"preset": 0, "volume": -30.0, "mute": True})
    assert codes(s).count("master_muted") == 1
    assert s.problems[0].code in ("no_route", "master_muted")
    assert s.outputs[0].badge == "silent" and s.outputs[0].badge_reason == "Everything is muted (Mute all)."
    assert "high_total" not in codes(s)  # nothing can overload while everything is muted


def test_clip_and_overload_badge():
    s = run(walkthrough(), levels=Levels(clipped=frozenset({("output", 0)})))
    shakers = s.outputs[0]
    assert shakers.badge == "overload" and shakers.headroom.level == "problem"
    assert shakers.headroom.text == "Shakers clipped: lower it or the source."
    assert "clip" in codes(s) and shakers.parts["level"].state == "problem"


def test_send_failed_and_changed_on_device():
    statuses = {"outputs.0.gain_db": "failed", "outputs.0.delay_ms": "failed", "outputs.0.mute": "synced"}
    verify = {"preset": 0, "paths": ["outputs.0.peq.0.compiled", "inputs.0.routing.0.gain_db"]}
    s = run(walkthrough(), statuses=statuses, last_verify=verify, preset=0)
    texts = [p.text for p in s.problems]
    assert "2 settings on Shakers didn't reach the device." in texts
    assert "1 setting on Shakers changed on the device since the app sent them." in texts
    assert "1 setting on In 1 changed on the device since the app sent them." in texts
    shakers = s.outputs[0]
    assert shakers.sync.by_block["level"] == "failed" and shakers.sync.by_block["eq"] == "changed"
    assert shakers.sync.by_block["sources"] == "changed"  # the route into it
    assert shakers.sync.headline == "Failed"
    # a check of another preset doesn't count, nor does a value since used or forgotten
    assert "changed_on_device" not in codes(run(walkthrough(), last_verify=verify, preset=1))
    used = {**verify, "accepted": ["outputs.0.peq.0.compiled"], "marked_unknown": ["inputs.0.routing.0"]}
    assert "changed_on_device" not in codes(run(walkthrough(), last_verify=used, preset=0))


def test_failed_settings_make_the_badge_check_not_silent():
    data = route(base(), 0, 0)
    s = run(data, statuses={"outputs.0.gain_db": "failed"})
    assert s.outputs[0].badge == "check"
    assert s.outputs[0].badge_reason == "1 setting on Shakers didn't reach the device."


def test_unknown_settings_are_one_info_note_and_keep_ready_away():
    data = route(base(), 0, 0)
    s = run(data, statuses={"outputs.0.gain_db": "unknown", "outputs.1.gain_db": "unknown"})
    assert codes(s)[-1] == "unknown_settings"
    assert s.problems[-1].text == "2 settings are unknown: the app hasn't sent or read them yet."
    assert s.outputs[0].badge == "check" and s.outputs[0].sync.headline == "Unknown"


def test_pending_waits_for_its_preset():
    data = route(base(), 0, 0)
    s = run(data, statuses={"outputs.0.gain_db": "pending"}, staged=True, preset=1)
    assert s.outputs[0].sync.headline == "Waiting for preset 2" and s.outputs[0].badge == "ready"
    assert run(data, statuses={"outputs.0.gain_db": "pending"}).outputs[0].sync.headline == "Sending"


def test_no_signal_out_only_with_live_levels():
    data = route(base(), 0, 0)
    assert "no_signal_out" not in codes(run(data))
    levels = Levels(inputs=(-20.0, -120.0), outputs=(-120.0, -120.0, -120.0, -120.0))
    s = run(data, levels=levels)
    assert "no_signal_out" in codes(s)
    assert next(p for p in s.problems if p.code == "no_signal_out").text == "In 1 has signal but Shakers shows none."
    borderline = Levels(inputs=(-80.0, -120.0), outputs=(-120.0,) * 4)  # can't tell: no caution
    assert "no_signal_out" not in codes(run(data, levels=borderline))


def test_input_unused_turns_caution_when_it_has_signal():
    s = run(walkthrough(), levels=Levels(inputs=(-20.0, -30.0), outputs=(-20.0,) * 4))
    note = next(p for p in s.problems if p.code == "input_unused")
    assert note.level == "caution" and note.text == "In 2 isn't sent to any output."
    assert next(p for p in run(walkthrough()).problems if p.code == "input_unused").level == "info"


def test_limiter_crushing():
    data = route(base(), 0, 0)
    data["outputs"][0]["compressor"] = {
        "bypass": False,
        "threshold_db": -70.0,
        "ratio": 20.0,
        "attack_ms": 10.0,
        "release_ms": 100.0,
    }
    s = run(data)
    assert s.problems[0].code == "limiter_crushing"
    assert s.problems[0].text == (
        "Shakers's compressor starts at −70 dBFS with a 20:1 ratio, so it may hold it almost silent."
    )
    assert s.outputs[0].badge == "check" and s.outputs[0].parts["compressor"].state == "caution"


def test_volume_and_editing_notes():
    s = run(route(base(), 0, 0), preset=1, master={"preset": 0, "volume": -90.0, "mute": False})
    assert codes(s) == ["volume_very_low", "editing_inactive"]
    assert s.problems[0].text == "Volume is at −90 dB, which is very quiet."
    assert s.problems[1].text == "You're editing preset 2, which isn't playing."
    assert [a.label for a in s.problems[1].actions] == ["Switch to preset 2…", "Edit preset 1 instead"]
    assert codes(run(route(base(), 0, 0), preset=0, master={"preset": 0, "volume": -30.0})) == []


def test_ordering_is_problem_then_caution_then_info():
    s = run(walkthrough(), statuses={"inputs.1.gain_db": "unknown"}, master={"preset": 0, "volume": -90.0})
    levels = [p.level for p in s.problems]
    assert levels == sorted(levels, key=["problem", "caution", "info"].index)


def test_thresholds_from_settings():
    assert Thresholds.from_settings(None) == Thresholds()
    th = Thresholds.from_settings(Settings(_env_file=None, guidance_no_signal_dbfs=-80.0, guidance_comp_ratio=8.0))
    assert (th.no_signal_dbfs, th.comp_ratio, th.high_total_db) == (-80.0, 8.0, 12.0)


def test_json_shape():
    data = run(walkthrough(), preset=0).as_json()
    json.dumps(data)
    assert data["counts"] == {"problem": 1, "caution": 1, "info": 3}
    out = data["outputs"][1]
    assert set(out) >= {"name", "badge", "sentence", "parts", "total", "headroom", "problems", "sync"}
    assert out["problems"][0]["silences"] is True and out["parts"]["band"]["label"] == "Full range"
    assert data["inputs"][1]["sentence"] == "Not sent to any output."


def test_first_run_badges_are_unknown_not_a_guess():
    # Design 4.4 (review M2): before set-up the app hasn't sent or read anything, so no output is "⚠ Check".
    unused = {**NAMES, ("output", 3): meta("output", 3, unused=True)}
    s = run(walkthrough(), unused, first_run=True)
    assert [(o.badge, o.badge_glyph, o.badge_word) for o in s.outputs[:3]] == [("unknown", "?", "Unknown")] * 3
    assert s.outputs[3].badge == "not_in_use"  # muted and marked Not in use in Settings: the user said so
    assert "set up this device" in s.outputs[0].badge_reason
    assert run(walkthrough(), unused).outputs[1].badge == "silent"
