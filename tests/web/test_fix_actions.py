"""Every fix action of the health rules (phase 9 design §7) reaches the UI as a button or a link (review M2: actions
without an href or task, such as [Unmute all], used to be dropped)."""

import re
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from app.sync.summary import Levels
from app.web import shell
from app.web.flow import fixes
from app.web.templating import templates
from tests.unit.sync.test_summary import FLAT, ZERO, base, peq, route, run, walkthrough, xo

SUMMARY_SRC = Path(__file__).resolve().parents[2] / "app" / "sync" / "summary.py"


def _states():
    """Summaries that between them raise every health rule."""
    a = walkthrough()  # no_route (Sub), high_total, muted_routed (Out 3, 4), input_unused (In 2)
    a["outputs"][0]["peq"][2] = {**peq(), "compiled": ZERO}  # filter_empty (EQ band)
    a["outputs"][1]["crossover"][0] = {"bypass": False, "design": None, "compiled": [ZERO, FLAT, FLAT, FLAT]}
    a["inputs"][0]["peq"][1] = {**peq(), "compiled": ZERO}  # filter_empty (input EQ band)
    yield run(
        a,
        preset=0,
        master={"preset": 0, "volume": -90.0, "mute": True},  # master_muted, volume_very_low
        statuses={"outputs.0.gain_db": "failed", "inputs.1.gain_db": "unknown", "outputs.0.mute": "synced"},
        last_verify={"preset": 0, "paths": ["outputs.0.peq.0.compiled"]},  # changed_on_device
        levels=Levels(clipped=frozenset({("output", 0)})),  # clip
    )
    b = route(base(), 0, 0)
    b["inputs"][0]["mute"] = True  # only_source_muted
    b["outputs"][1]["crossover"] = [xo("hp", 100), xo("lp", 80)]  # band_empty
    b["outputs"][2]["compressor"] = {
        "bypass": False,
        "threshold_db": -70.0,
        "ratio": 20.0,
        "attack_ms": 10.0,
        "release_ms": 100.0,
    }  # limiter_crushing
    yield run(b, preset=1, master={"preset": 0, "volume": -30.0, "mute": False})  # editing_inactive
    yield run(route(base(), 0, 0), levels=Levels(inputs=(-20.0, -120.0), outputs=(-120.0,) * 4))  # no_signal_out


def _problems():
    return [p for s in _states() for p in s.problems]


def _request():
    return SimpleNamespace(app=FastAPI(), scope={"root_path": ""})


def test_the_states_cover_every_action_the_rules_define():
    defined = set(re.findall(r'Action\(\s*"(\w+)"', SUMMARY_SRC.read_text()))
    seen = {a.id for p in _problems() for a in p.actions}
    assert defined and defined <= seen, defined - seen


def test_every_action_becomes_a_button_or_a_link():
    names = ["In 1", "In 2"], ["Shakers", "Sub", "Out 3", "Out 4"]
    for problem in _problems():
        item = shell._item(problem, _request(), "", *names)
        offered = {f.id for f in item.fixes}
        assert offered == {a.id for a in problem.actions}, (problem.code, offered)
        for fix in item.fixes:
            assert fix.href or fix.values or fix.action, (problem.code, fix)


def test_unmute_all_volume_reset_filter_retry_and_clip_are_inline():
    by_id = {}
    for problem in _problems():
        for fix in fixes(problem, "", ["In 1", "In 2"], ["Shakers", "Sub", "Out 3", "Out 4"]):
            by_id.setdefault(fix.id, fix)
    unmute = by_id["unmute_all"]
    assert (unmute.action, unmute.endpoint, unmute.values, unmute.undo) == (
        "master",
        "mute",
        {"mute": "false"},
        {"mute": "true"},
    )
    volume = by_id["set_volume"]
    assert (volume.action, volume.endpoint, volume.values, volume.undo) == (
        "master",
        "volume",
        {"volume": -30.0},
        {"volume": -90.0},
    )
    reset = by_id["reset_filter"]
    ((path, flat),) = reset.values.items()
    assert reset.action is None and path in reset.undo
    if ".peq." in path:
        assert flat["compiled"] == FLAT and flat["bypass"] is True and flat["design"]["type"] == "passthrough"
    else:
        assert flat["compiled"] == [FLAT] * 4 and flat["design"] is None
    assert reset.undo[path]["compiled"] != flat["compiled"]
    assert by_id["retry"].action == "post" and by_id["retry"].endpoint == "retry"
    assert by_id["reset_clip"].action == "clip"
    assert by_id["send_app_values"].href == "/device#sync"
    assert by_id["switch_preset"].href == "/device?preset=2#sync"
    assert by_id["edit_preset"].href == "/routing?preset=1"


def test_master_and_retry_fixes_render_as_shell_actions():
    by_id = {f.id: f for p in _problems() for f in fixes(p, "", ["In 1", "In 2"], ["S", "Sub", "O3", "O4"])}
    macro = templates.get_template("partials/fix_macros.html").module.action_fix
    html = str(macro(by_id["unmute_all"], "btn-small", "/ui/devices/1/presets/0/edit", 0))
    assert 'data-shell-action="master"' in html and 'data-endpoint="mute"' in html
    assert 'data-values=\'{"mute": "false"}\'' in html and ">Unmute all</button>" in html
    html = str(macro(by_id["retry"], "btn-small", "/ui/devices/1/presets/0/edit", 0))
    assert 'data-shell-action="post"' in html and 'data-url="/ui/devices/1/presets/0/retry"' in html
