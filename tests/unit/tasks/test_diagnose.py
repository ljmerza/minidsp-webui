"""Something wrong? (phase 9 design §14.3): each check against fixture documents (app/web/task_diagnose.py)."""

from typing import Any

from app.model.config_doc import ConfigDoc
from app.model.device_profile import M2X4HD
from app.sync.summary import Levels
from app.web.task_diagnose import FAIL, OK, PENDING, UNKNOWN, Facts, find_fix, run_checks, signal
from tests.unit.sync.test_summary import ZERO, base, peq, route, run, walkthrough, xo

NO = -120.0


def facts(data: dict, **kwargs: Any) -> Facts:
    master = {"preset": 0, "volume": kwargs.pop("volume", -30.0), "mute": kwargs.pop("master_mute", False)}
    levels = kwargs.pop("levels", None)
    statuses = kwargs.pop("statuses", {})
    summary = run(data, preset=0, master=master, levels=levels, statuses=statuses)
    return Facts(
        summary=summary,
        doc=ConfigDoc.model_validate(data),
        profile=M2X4HD,
        master_mute=master["mute"],
        volume=master["volume"],
        source="Analog",
        levels=levels,
        statuses=statuses,
        **kwargs,
    )


def checks(f: Facts, what: str = "one", output: int | None = 1) -> dict[str, Any]:
    return {c.id: c for g in run_checks(f, what, output) for c in g.checks}


def test_the_silent_sub_fails_on_no_route_with_a_route_fix_in_words():
    c = checks(facts(walkthrough()))
    assert [c[k].result for k in ("dev-conn", "dev-mute", "dev-volume")] == [OK, OK, OK]
    assert c["dev-signal"].result == UNKNOWN  # no meters
    assert c["o1-mute"].result == OK
    route_check = c["o1-route"]
    assert route_check.result == FAIL and route_check.text == "No input is routed to Sub"
    fix = route_check.fixes[0]
    assert fix.label == "Route In 1 → Sub at 0 dB"
    assert fix.preview == "Route In 1 → Sub at 0 dB. Sub will then play full range from In 1."
    assert fix.values == {"inputs.0.routing.1.enabled": True}
    assert fix.confirm is None
    assert route_check.cause == "no input was routed"
    # What depends on a route waits for the fix.
    assert c["o1-input"].result == PENDING and c["o1-meter"].result == PENDING
    assert c["o1-band"].result == OK and c["o1-comp"].result == OK and c["o1-sent"].result == OK


def test_route_fix_prefers_the_input_with_signal_and_confirms_a_big_raise():
    data = walkthrough()
    data["inputs"][1]["routing"][1] = {"enabled": False, "gain_db": -20.0}
    levels = Levels(inputs=(NO, -20.0), outputs=(NO,) * 4)
    fix = checks(facts(data, levels=levels))["o1-route"].fixes[0]
    assert fix.label == "Route In 2 → Sub at 0 dB"
    assert fix.values == {"inputs.1.routing.1.enabled": True, "inputs.1.routing.1.gain_db": 0.0}
    assert fix.big_raise and "by 20 dB" in fix.confirm


def test_device_checks_mute_all_and_a_very_low_volume():
    c = checks(facts(walkthrough(), master_mute=True, volume=-90.0))
    assert c["dev-mute"].result == FAIL and c["dev-mute"].fixes[0].master == {"mute": False}
    vol = c["dev-volume"]
    assert vol.result == FAIL and vol.fixes[0].master == {"volume": -30.0}
    assert vol.fixes[0].big_raise and "by 60 dB (to −30 dB)" in vol.fixes[0].confirm
    # A device-wide problem holds back the output's meter check.
    data = route(walkthrough(), 0, 1)
    assert checks(facts(data, master_mute=True))["o1-meter"].result == PENDING


def test_disconnected_device_stops_after_the_device_checks():
    groups = run_checks(facts(walkthrough(), connected=False, conn_label="minidspd unreachable"), "one", 1)
    assert len(groups) == 1
    assert groups[0].checks[0].result == FAIL and "minidspd unreachable" in groups[0].checks[0].text


def test_input_signal_yes_no_and_cant_tell():
    def sig(levels):
        return checks(facts(walkthrough(), levels=Levels(inputs=levels, outputs=(NO,) * 4)))["dev-signal"]

    assert sig((-18.0, NO)).result == OK and "In 1 shows signal (−18 dBFS)" in sig((-18.0, NO)).text
    assert sig((-60.0, -60.0)).result == UNKNOWN and "In 1 reads −60 dBFS" in sig((-60.0, -60.0)).text  # the mock
    none = sig((NO, NO))
    assert none.result == FAIL and none.fixes[0].kind == "source" and none.fixes[0].choices == M2X4HD.sources
    assert signal(-60.0, facts(walkthrough()).th) == "unsure"


def test_muted_output_and_muted_only_source():
    data = route(walkthrough(), 0, 1)
    data["outputs"][1]["mute"] = True
    c = checks(facts(data, statuses={"outputs.1.mute": "unknown"}))
    assert c["o1-mute"].result == FAIL and c["o1-mute"].fixes[0].values == {"outputs.1.mute": False}
    assert c["o1-mute"].fixes[0].confirm and "never been sent" in c["o1-mute"].fixes[0].confirm  # the never-sent ask
    data = route(walkthrough(), 0, 1)
    data["inputs"][0]["mute"] = True
    c = checks(facts(data))
    assert c["o1-input"].result == FAIL and c["o1-input"].fixes[0].values == {"inputs.0.mute": False}


def test_very_low_levels_raise_the_lowest_part_to_0_db():
    data = route(walkthrough(), 0, 1, gain_db=-50.0)
    c = checks(facts(data))["o1-input"]
    assert c.result == FAIL and "very low" in c.text
    assert c.fixes[0].values == {"inputs.0.routing.1.gain_db": 0.0} and c.fixes[0].big_raise


def test_band_and_empty_filters():
    data = route(walkthrough(), 0, 1)
    data["outputs"][1]["crossover"] = [xo("hp", 100), xo("lp", 80)]
    c = checks(facts(data))["o1-band"]
    assert c.result == FAIL and "plays nothing" in c.text and c.fixes[0].kind == "link"
    data = route(walkthrough(), 0, 1)
    data["outputs"][1]["peq"][3] = {**peq(), "compiled": ZERO}
    c = checks(facts(data))["o1-band"]
    assert c.result == FAIL and c.fixes[0].label == "Reset EQ band 4 to flat"
    assert list(c.fixes[0].values) == ["outputs.1.peq.3"]


def test_a_crushing_compressor_is_turned_off():
    data = route(walkthrough(), 0, 1)
    data["outputs"][1]["compressor"] = {
        "bypass": False,
        "threshold_db": -70.0,
        "ratio": 20.0,
        "attack_ms": 10.0,
        "release_ms": 100.0,
    }
    c = checks(facts(data))["o1-comp"]
    assert c.result == FAIL and c.fixes[0].values == {"outputs.1.compressor.bypass": True}


def test_sync_failed_unknown_and_sent():
    data = route(walkthrough(), 0, 1)
    assert checks(facts(data, statuses={"outputs.1.gain_db": "failed"}))["o1-sent"].fixes[0].kind == "retry"
    unknown = checks(facts(data, statuses={"outputs.1.mute": "unknown"}))["o1-sent"]
    assert unknown.result == UNKNOWN and "can't tell" in unknown.text
    assert checks(facts(data, statuses={"outputs.1.mute": "synced"}))["o1-sent"].result == OK


def test_the_output_meter_is_the_last_check():
    data = route(walkthrough(), 0, 1)
    on = checks(facts(data, levels=Levels(inputs=(-20.0, NO), outputs=(NO, -20.0, NO, NO))))["o1-meter"]
    assert on.result == OK and on.text.startswith("The miniDSP is sending sound on Sub")
    assert "amplifier" in on.detail
    off = checks(facts(data, levels=Levels(inputs=(-20.0, NO), outputs=(NO, NO, NO, NO))))["o1-meter"]
    assert off.result == FAIL and "In 1 has signal but Sub shows none" in off.detail
    assert checks(facts(data, levels=Levels(inputs=(-60.0, -60.0), outputs=(-60.0,) * 4)))["o1-meter"].result == UNKNOWN


def test_everything_silent_checks_every_output_in_use():
    groups = run_checks(facts(walkthrough()), "all")
    assert [g.title for g in groups] == ["", "Shakers", "Sub", "Out 3", "Out 4"]


def test_distorted_reads_clip_lights_inputs_and_totals():
    f = facts(walkthrough(), levels=Levels(inputs=(-0.5, NO), outputs=(0.0,) * 4), clipped=frozenset({"o0"}))
    c = {x.id: x for x in run_checks(f, "distorted")[0].checks}
    assert c["dist-clip"].result == FAIL and "Shakers" in c["dist-clip"].text
    assert c["dist-inputs"].result == FAIL and "too hot" in c["dist-inputs"].text
    assert c["dist-o0"].result == FAIL and "−16 dBFS" in c["dist-o0"].text
    assert {fx.task for fx in c["dist-o0"].fixes} == {"louder", "set-levels"}
    unknown = {x.id: x for x in run_checks(facts(walkthrough()), "distorted")[0].checks}
    assert unknown["dist-clip"].result == UNKNOWN  # no browser report


def test_find_fix_only_returns_fixes_of_failed_checks():
    groups = run_checks(facts(walkthrough()), "one", 1)
    assert find_fix(groups, "o1-route", 0)[1].label == "Route In 1 → Sub at 0 dB"
    assert find_fix(groups, "o1-mute", 0) is None
    assert find_fix(groups, "o1-route", 5) is None
    groups = run_checks(facts(route(base(fill=False), 0, 1)), "one", 1)
    assert find_fix(groups, "o1-route", 0) is None  # already fixed elsewhere
