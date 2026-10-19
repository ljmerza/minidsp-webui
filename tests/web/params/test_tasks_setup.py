"""Set levels, Save or load a setup and Set up this device on a real session (phase 9 design §14.6-14.8)."""

import html
import json
import re

from app.sync import profiles as svc
from app.web import task_adopt

from .conftest import HX, SERIAL

ORIGIN = {**HX, "Origin": "http://test"}


def text(response) -> str:
    return html.unescape(response.text)


async def _post(ctx, task: str, files=None, **form):
    return await ctx.http.post(f"/ui/tasks/{task}", data={"mode": "dialog", **form}, files=files, headers=ORIGIN)


async def _get(ctx, task: str, **params):
    return await ctx.http.get(f"/ui/tasks/{task}", params={"mode": "dialog", **params}, headers=HX)


async def _silent_sub(ctx) -> None:
    values = {f"inputs.{i}.routing.{o}.enabled": (i == 0 and o == 0) for i in range(2) for o in range(4)}
    response = await ctx.edit(0, values, commit=True, confirm=True)
    assert response.status_code == 200, response.text
    await ctx.settle()


async def _name(ctx, kind: str, index: int, name: str) -> None:
    response = await ctx.http.post(
        f"/ui/devices/{SERIAL}/channels/{kind}/{index}/name", data={"name": name}, headers=ORIGIN
    )
    assert response.status_code == 200, response.text


# --- Save or load a setup (§14.8) -------------------------------------------------------------------------------------


async def test_save_checks_for_problems_first_then_saves_with_an_empty_name_field(ctx):
    await _silent_sub(ctx)
    check = text(await _get(ctx, "profiles", step="2", do="save", preset="1"))
    assert "Out 2 is silent: no input is routed to it." in check
    assert "Save anyway" in check and "Fix first" in check
    name = text(await _get(ctx, "profiles", step="3", do="save", preset="1"))
    assert "Name (for example Movies, Music, Night)" in name
    field = re.search(r'<input[^>]*name="name"[^>]*>', name).group(0)
    assert 'value=""' in field and "placeholder" not in field
    saved = text(await _post(ctx, "profiles", step="3", do="save", preset="1", name="Movies", op="save"))
    assert "Saved “Movies” from preset 1. Same as preset 1 right now." in saved
    assert [p.profile.name for p in await svc.list_profiles(ctx.repo)] == ["Movies"]
    again = text(await _post(ctx, "profiles", step="3", do="save", preset="1", name="Movies", op="save"))
    assert "There is already a profile named “Movies”" in again
    empty = text(await _post(ctx, "profiles", step="3", do="save", preset="1", name="  ", op="save"))
    assert "Type a name first." in empty


async def test_a_wizard_write_lets_the_mqtt_bridge_refresh_at_once(ctx):
    class Bridge:
        refreshes = 0

        def refresh(self) -> None:
            self.refreshes += 1

    bridge = ctx.app.state.mqtt = Bridge()
    await _get(ctx, "profiles", step="3", do="save", preset="1")
    assert bridge.refreshes == 0  # reading a step changes nothing
    await _post(ctx, "profiles", step="3", do="save", preset="1", name="Movies", op="save")
    assert bridge.refreshes == 1  # Home Assistant's profile list updates now, as on the Profiles page


async def test_save_with_nothing_wrong_says_so(ctx):
    check = text(await _get(ctx, "profiles", step="2", do="save", preset="1"))
    assert "No problems found in preset 1." in check


async def _loud_profile(ctx) -> int:
    """A profile whose Out 1 output level is +12 dB (preset 2 holds 0 dB, so loading it there raises 12 dB)."""
    response = await ctx.edit(0, {"outputs.0.gain_db": 12.0}, commit=True, confirm=True)
    assert response.status_code == 200
    profile = await svc.save_preset(ctx.session, 0, "Movies")
    return profile.id


async def test_load_into_preset_2_needs_one_confirm_with_the_raise_checkbox_and_keeps_the_target(ctx):
    pid = await _loud_profile(ctx)
    which = text(await _get(ctx, "profiles", step="2", profile=str(pid)))
    assert re.search(rf'name="profile" value="{pid}" checked', which) and "What it does" in which
    target = text(await _get(ctx, "profiles", step="3", profile=str(pid)))
    assert "1 · ▶ playing · already holds this profile" in target
    base = {"do": "load", "profile": str(pid), "target": "2", "when": "store"}
    check = text(await _get(ctx, "profiles", step="5", **base))
    assert "I confirm raising Out 1 output level by 12 dB." in check
    box = re.search(r'<input type="checkbox" name="(ok_0)" value="([^"]+)" required>', check)
    assert box, check
    assert "Out 1 output level" in check and "Nothing is sent until you press Load." in check
    # Without the tick nothing is loaded.
    refused = text(await _post(ctx, "profiles", step="5", op="apply", **base))
    assert "Tick the box first" in refused
    assert ctx.session.desired(1).outputs[0].gain_db == 0.0
    loaded = text(await _post(ctx, "profiles", step="5", op="apply", **base, **{box.group(1): box.group(2)}))
    assert "“Movies” is in preset 2 (waiting until you switch to it)." in loaded
    assert "Waiting until preset 2 plays" not in loaded  # said once, not twice (PR 17 persona re-run)
    assert ctx.session.desired(1).outputs[0].gain_db == 12.0
    # The chosen preset is kept for the next load (P11), and Switch now is offered.
    assert 'name="target" value="2"' in loaded and "Load another profile into preset 2" in loaded
    assert "Switch to preset 2 now" in loaded
    rev, prev, at = (re.search(rf'name="{k}" value="([^"]+)"', loaded).group(1) for k in ("rev", "prev", "at"))
    undone = text(await _post(ctx, "profiles", step="6", op="undo", **base, rev=rev, prev=prev, at=at))
    assert "Undone: preset 2 holds what it had before." in undone
    assert ctx.session.desired(1).outputs[0].gain_db == 0.0


async def test_the_check_re_renders_with_the_new_list_when_the_state_changed(ctx):
    pid = await _loud_profile(ctx)
    base = {"do": "load", "profile": str(pid), "target": "2", "when": "store"}
    check = text(await _get(ctx, "profiles", step="5", **base))
    box = re.search(r'name="(ok_0)" value="([^"]+)"', check)
    # Meanwhile someone lowers preset 2's Out 1, so the load would now raise it by 18 dB, not 12.
    response = await ctx.edit(1, {"outputs.0.gain_db": -6.0}, commit=True, confirm=True)
    assert response.status_code == 200
    again = text(await _post(ctx, "profiles", step="5", op="apply", **base, **{box.group(1): box.group(2)}))
    assert "The settings changed since this list was made" in again
    assert "I confirm raising Out 1 output level by 18 dB." in again
    assert "outputs." not in re.sub(r'value="[^"]*"', "", again)  # never a raw key on screen
    assert ctx.session.desired(1).outputs[0].gain_db == -6.0


async def test_undo_of_a_load_leaves_a_preset_changed_since_alone(ctx):
    pid = await _loud_profile(ctx)
    base = {"do": "load", "profile": str(pid), "target": "2", "when": "store"}
    check = text(await _get(ctx, "profiles", step="5", **base))
    box = re.search(r'name="(ok_0)" value="([^"]+)"', check)
    loaded = text(await _post(ctx, "profiles", step="5", op="apply", **base, **{box.group(1): box.group(2)}))
    rev, prev, at = (re.search(rf'name="{k}" value="([^"]+)"', loaded).group(1) for k in ("rev", "prev", "at"))
    await ctx.edit(1, {"outputs.1.gain_db": -3.0}, commit=True)
    refused = text(await _post(ctx, "profiles", step="6", op="undo", **base, rev=rev, prev=prev, at=at))
    assert "Preset 2 was changed since, so Undo left it as is" in refused
    assert ctx.session.desired(1).outputs[0].gain_db == 12.0


async def test_the_profiles_list_links_each_profile_to_the_load_wizard(ctx):
    from app.web import profiles as profiles_ui

    pid = await _loud_profile(ctx)
    request = type("R", (), {"scope": {"root_path": ""}, "app": ctx.app})()
    listing = await profiles_ui.list_context(request, ctx.session)
    assert listing["profiles"][0]["load_href"] == f"/tasks/profiles?do=load&profile={pid}"
    assert (await ctx.http.get(f"/tasks/profiles?profile={pid}&step=2")).status_code == 200


# --- Set up this device (§14.7) ---------------------------------------------------------------------------------------


async def test_the_factory_defaults_path_warns_about_a_custom_name_and_needs_the_tick(ctx):
    await _name(ctx, "output", 0, "Shakers")
    how = text(await _get(ctx, "adopt", step="2", how="factory"))
    assert "You named Out 1 “Shakers”, so it was probably set up before." in how
    assert "My device really is at factory defaults." in how
    # Without the tick it doesn't go on, and the final step refuses too (the session's guard stays as well).
    refused = text(await _post(ctx, "adopt", step="2", how="factory", op="source"))
    assert "Tick “My device really is at factory defaults” first" in refused
    apply = {"how": "factory", "presets": "1", "choice": "trust", "step": "5", "op": "apply"}
    refused = text(await _post(ctx, "adopt", **apply))
    assert "Tick “My device really is at factory defaults” first" in refused
    assert ctx.session.needs_adoption
    done = text(await _post(ctx, "adopt", **apply, factory_ok="1"))
    assert "Preset 1:" in done and "known" in done and "0 written" in done
    assert not ctx.session.needs_adoption and ctx.session.adoption["mode"] == "trust"


async def test_adopt_from_a_command_script_previews_then_remembers(ctx):
    taken = text(await _post(ctx, "adopt", step="2", how="script", op="source", script="output 0 gain 3\n"))
    assert "Which presets?" in taken
    token = re.search(r'name="token" value="([^"]+)"', taken).group(1)
    preview = text(await _get(ctx, "adopt", step="4", how="script", token=token, presets="1"))
    assert "Does this match how it sounds today?" in preview and "Out 1:" in preview
    assert "The script sets 1 of the preset's" in preview
    done = text(await _post(ctx, "adopt", step="5", op="apply", how="script", token=token, presets="1", choice="trust"))
    assert "Preset 1:" in done and "0 written" in done
    assert ctx.session.desired(0).outputs[0].gain_db == 3.0
    bad = text(await _post(ctx, "adopt", step="2", how="script", op="source", script="output 0 gain loud\n"))
    assert "Some lines can't be read" in bad and "line 1" in bad


async def test_adopt_from_a_profile_file(ctx):
    pid = await _loud_profile(ctx)
    export = svc.export_profile((await svc.get_profile(ctx.repo, pid)).profile)
    files = {"backup_file": ("movies.json", json.dumps(export).encode(), "application/json")}
    taken = text(await _post(ctx, "adopt", files=files, step="2", how="backup", op="source"))
    assert "Which presets?" in taken, taken
    token = re.search(r'name="token" value="([^"]+)"', taken).group(1)
    assert task_adopt.unstash(token).label.startswith("movies.json")
    junk = {"backup_file": ("x.json", b"not json", "application/json")}
    refused = text(await _post(ctx, "adopt", files=junk, step="2", how="backup", op="source"))
    assert "That file can't be used: it isn't valid JSON." in refused


async def test_dont_know_yet_writes_nothing_and_says_how_to_go_on(ctx):
    done = text(await _post(ctx, "adopt", step="5", op="apply", how="software", presets="1"))
    assert re.search(r"Preset 1: 0 settings known, \d+ unknown, 0 written\.", done)
    assert ctx.session.adoption["mode"] == "unknown"
    assert "Open Home" in done and "Check device" in done


# --- Set levels (§14.6) -----------------------------------------------------------------------------------------------


async def test_set_levels_starts_quiet(ctx):
    quiet = text(await _get(ctx, "set-levels", step="1"))
    assert "Turn the volume down from −20 dB to −40 dB" in quiet and "Applies now" in quiet
    done = text(await _post(ctx, "set-levels", step="1", op="quiet"))
    assert ctx.session.master.volume == -40.0
    assert "Changes so far" in done and "Volume −20 dB → −40 dB" in done
    assert "quiet enough to start" in done


async def test_set_levels_start_quiet_confirms_a_big_drop_with_a_checkbox(ctx):
    await ctx.session.set_volume(-5.0, confirm=True)
    response = await _get(ctx, "set-levels", step="1")
    raw, quiet = response.text, text(response)
    assert "I confirm turning the Volume down by 35 dB (from −5 dB to −40 dB)." in quiet
    assert re.search(r'<input type="checkbox" name="confirm" value="1" required>', raw)
    unticked = text(await _post(ctx, "set-levels", step="1", op="quiet"))
    assert ctx.session.master.volume == -5.0
    assert "Tick the box to confirm turning the Volume down this far." in unticked
    assert "needs confirmation" not in unticked and "I confirm turning the Volume down by 35 dB" in unticked
    done = text(await _post(ctx, "set-levels", step="1", op="quiet", confirm="1"))
    assert ctx.session.master.volume == -40.0
    assert "Volume −5 dB → −40 dB" in done and "quiet enough to start" in done


async def test_set_levels_start_quiet_needs_no_checkbox_for_a_small_drop(ctx):
    quiet = text(await _get(ctx, "set-levels", step="1"))
    assert "I confirm" not in quiet


async def test_set_levels_input_feedback_and_its_fix_and_undo_all(ctx):
    feedback = text(await _get(ctx, "set-levels", step="3", pk_i="-0.4,-120", cl_i="2,0"))
    assert "In 1 hits CLIP: the signal arriving is too hot." in feedback
    assert "the miniDSP can't undo clipping that happens before it" in feedback
    assert "In 2: no signal." in feedback
    raw = (await _post(ctx, "set-levels", step="3", op="lower_in", input="0", pk_i="-0.4,-120", cl_i="2,0")).text
    fixed = html.unescape(raw)
    assert ctx.session.desired(0).inputs[0].gain_db == -3.0
    assert "In 1 input level 0 dB → −3 dB" in fixed and "Undo all" in fixed
    record, changes = (
        html.unescape(re.search(rf'name="{k}" value="([^"]+)"', raw).group(1)) for k in ("undo", "changes")
    )
    undone = text(await _post(ctx, "set-levels", step="7", op="undo", undo=record, changes=changes))
    assert "Undone" in undone and ctx.session.desired(0).inputs[0].gain_db == 0.0


async def test_set_levels_output_results_offer_three_fixes_that_apply_now(ctx):
    result = text(await _get(ctx, "set-levels", step="4", phase="result", pk_o="-0.2,-30,-120,-120", cl_o="2,0,0,0"))
    assert "Out 1 clipped twice at 0.0 dB total." in result
    assert "Lower Out 1 by 3 dB" in result and "Lower the route instead" in result
    assert "Add a compressor at −3 dBFS…" in result
    assert "Out 2 peaks at −30 dBFS, no CLIP" in result
    comp = text(await _post(ctx, "set-levels", step="4", phase="result", op="fix", fix="comp", output="0"))
    c = ctx.session.desired(0).outputs[0].compressor
    assert not c.bypass and c.threshold_db == -3.0 and c.ratio == 10.0
    assert "Watching again" in comp and "Out 1's compressor on at −3 dBFS" in comp


async def test_set_levels_everyday_volume_steps_and_usual_level(ctx):
    vol = text(await _get(ctx, "set-levels", step="6"))
    assert "Most listening sits between −40 dB and −10 dB" in vol
    up = text(await _post(ctx, "set-levels", step="6", op="vol", delta="3"))
    assert ctx.session.master.volume == -17.0 and "Volume −20 dB → −17 dB" in up
    remembered = text(await _post(ctx, "set-levels", step="6", op="usual"))
    assert "Remembered −17 dB as your usual level." in remembered
    assert await ctx.repo.get_setting(f"ui.usual_volume.{SERIAL}") == -17.0
    done = text(await _get(ctx, "set-levels", step="7", pk_i="-8,-120"))
    assert "Inputs peak around −8 dBFS." in done and "Usual volume −17 dB." in done
    assert 'href="/tasks/profiles?do=save"' in done


async def test_every_step_renders_as_a_page(ctx):
    for path in (
        "/tasks/profiles",
        "/tasks/profiles?do=save",
        "/tasks/set-levels",
        "/tasks/set-levels?step=2",
        "/tasks/set-levels?step=4",
        "/tasks/set-levels?step=6",
        "/tasks/adopt",
        "/tasks/adopt?step=2",
        "/tasks/adopt?step=3&how=software",
        "/tasks/adopt?step=4&how=software&presets=1",
        "/tasks/adopt?step=5&how=software&presets=1",
    ):
        response = await ctx.http.get(path)
        assert response.status_code == 200, (path, response.text[-500:])
        assert 'class="task-heading"' in response.text, path


async def test_the_load_check_renders_as_a_page_under_the_shell_header(ctx):
    pid = await _loud_profile(ctx)
    page = await ctx.http.get(f"/tasks/profiles?do=load&profile={pid}&target=2&when=store&step=5")
    assert page.status_code == 200 and "I confirm raising Out 1 output level by 12 dB." in html.unescape(page.text)
