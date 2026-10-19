"""Profiles page: rendering, list actions (HTMX), diff partial, load / revert JSON actions and history timeline."""

import json
from urllib.parse import unquote

from app.dsp.design import design_crossover
from app.model.config_doc import default_config_doc
from app.model.device_profile import M2X4HD
from app.model.filters import CrossoverDesign
from app.state import Profile
from app.sync import profiles as svc
from app.web.profiles import MAX_IMPORT_BYTES, format_unit, pretty_summary

from .conftest import API, HX, SERIAL

UI = "/ui/profiles"


async def _create(ctx, name: str, **outputs0) -> int:
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][0].update(outputs0)
    response = await ctx.http.post(f"{API}/profiles", json={"name": name, "config": doc})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _error(response) -> str:
    return unquote(response.headers.get("X-Mdsp-Error", ""))


async def test_page_renders_empty(ctx):
    response = await ctx.http.get("/profiles")
    assert response.status_code == 200
    html = response.text
    assert "No profiles yet" in html
    assert "Save current preset as a profile" in html
    assert "/static/js/profiles.js" in html
    assert 'aria-current="page">Profiles</a>' in html


async def test_page_lists_profiles_with_slots(ctx):
    pid = await _create(ctx, "Movies", gain_db=-2.0)
    await ctx.http.post(f"{API}/profiles/{pid}/apply", params={"preset": 1})
    await _create(ctx, "Games <b>")
    html = (await ctx.http.get("/profiles")).text
    assert "Movies" in html
    assert "Games &lt;b&gt;" in html  # escaped
    assert "Preset 2" in html and "loaded" in html
    assert f"/api/v1/profiles/{pid}/export" in html
    assert f"/api/v1/profiles/{pid}/export/cli-script" in html
    assert 'value="profile:' in html  # diff sources


async def test_page_unknown_device_and_bad_preset(ctx):
    assert (await ctx.http.get("/profiles", params={"serial": 1})).status_code == 404
    assert (await ctx.http.get("/profiles", params={"tab": "history", "preset": 9})).status_code == 404


async def test_page_without_database(ctx):
    ctx.app.state.repo = None
    ctx.app.state.manager.repo = None
    ctx.session.repo = None
    response = await ctx.http.get("/profiles")
    assert response.status_code == 200
    assert "Profiles unavailable" in response.text


async def test_save_requires_htmx(ctx):
    response = await ctx.http.post(f"{UI}/save", data={"preset": "0", "name": "A"})
    assert response.status_code == 400
    assert await ctx.repo.list_profiles() == []


async def test_save_rename_duplicate_delete(ctx):
    form = {"preset": "0", "name": " Movies ", "description": "x"}
    response = await ctx.http.post(f"{UI}/save", data=form, headers=HX)
    assert response.status_code == 200
    assert "Saved preset 1 as “Movies”" in response.text
    [profile] = await ctx.repo.list_profiles()
    assert profile.name == "Movies" and profile.description == "x"

    response = await ctx.http.post(f"{UI}/save", data={"preset": "0", "name": "Movies"}, headers=HX)
    assert response.status_code == 409
    assert "already a profile named" in _error(response)
    assert "Movies" in response.text  # the list is still shown

    response = await ctx.http.post(f"{UI}/save", data={"preset": "", "name": "B"}, headers=HX)
    assert response.status_code == 422

    response = await ctx.http.post(f"{UI}/{profile.id}/rename", data={"name": "Cinema", "description": ""}, headers=HX)
    assert response.status_code == 200
    renamed = await ctx.repo.get_profile(profile.id)
    assert renamed.name == "Cinema" and renamed.description is None

    response = await ctx.http.post(f"{UI}/{profile.id}/duplicate", headers=HX)
    assert "Cinema (copy)" in response.text

    response = await ctx.http.post(f"{UI}/{profile.id}/delete", headers=HX)
    assert response.status_code == 200
    assert "Deleted “Cinema”" in response.text
    assert [p.name for p in await ctx.repo.list_profiles()] == ["Cinema (copy)"]
    response = await ctx.http.post(f"{UI}/{profile.id}/delete", headers=HX)
    assert response.status_code == 404


async def test_import_upload(ctx):
    pid = await _create(ctx, "Movies")
    exported = (await ctx.http.get(f"{API}/profiles/{pid}/export")).content
    files = {"file": ("movies.json", exported, "application/json")}
    response = await ctx.http.post(f"{UI}/import", files=files, headers=HX)
    assert response.status_code == 200
    assert "Imported “Movies (copy)”" in response.text

    response = await ctx.http.post(f"{UI}/import", files={"file": ("x.json", b"{", "application/json")}, headers=HX)
    assert response.status_code == 422
    response = await ctx.http.post(f"{UI}/import", data={"nothing": "1"}, headers=HX)
    assert response.status_code == 422
    big = b" " * (MAX_IMPORT_BYTES + 10)
    response = await ctx.http.post(f"{UI}/import", files={"file": ("x.json", big, "application/json")}, headers=HX)
    assert response.status_code == 413


async def test_import_upload_of_a_cli_script(ctx):
    pid = await _create(ctx, "Movies", gain_db=-6.0, delay_ms=1.23456)
    script = (await ctx.http.get(f"{API}/profiles/{pid}/export/cli-script")).content
    files = {"file": ("Living room.txt", script, "text/plain")}
    response = await ctx.http.post(f"{UI}/import", files=files, headers=HX)
    assert response.status_code == 200, response.text
    assert "Imported “Living room”" in response.text
    imported = await ctx.repo.get_profile_by_name("Living room")
    original = await ctx.repo.get_profile(pid)
    assert imported.config["outputs"] == original.config["outputs"]

    for raw in (b"output 9 gain -- 1\n", b"\x00\x01", b"[" * 200_000):
        response = await ctx.http.post(f"{UI}/import", files={"file": ("x.txt", raw, "text/plain")}, headers=HX)
        assert response.status_code == 422, raw[:20]


async def test_list_has_no_phantom_edit_for_unaligned_delays(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][1]["delay_ms"] = 1.234
    raw = await ctx.repo.create_profile("Raw", doc)  # stored as is, like an old profile without a device model
    response = await ctx.http.post(f"{API}/profiles/{raw.id}/apply", params={"preset": 0, "confirm": "true"})
    assert response.status_code == 200, response.text
    html = (await ctx.http.get(f"{UI}/list", headers=HX)).text
    assert "is-same" in html and "edited since" not in html


async def test_diff_partial(ctx):
    a = await _create(ctx, "A", gain_db=-3.0, delay_ms=9.0, invert=True)
    response = await ctx.http.get(f"{UI}/diff", params={"a": f"profile:{a}", "b": "preset:0"})
    assert response.status_code == 200
    html = response.text
    assert "3 differences" in html
    assert "Out 1 output level" in html
    assert "−3.00 dB" in html
    assert "9.000 ms (864 samples)" in html
    assert "180°" in html

    same = await ctx.http.get(f"{UI}/diff", params={"a": f"profile:{a}", "b": f"profile:{a}"})
    assert "pick two different" in same.text
    response = await ctx.http.get(f"{UI}/diff", params={"a": "bogus", "b": "preset:0"})
    assert response.status_code == 422
    response = await ctx.http.get(f"{UI}/diff", params={"a": "", "b": ""})
    assert "Pick two" in response.text


async def test_diff_partial_formats_filter_units(ctx):
    flat = await _create(ctx, "Flat")
    xover = {"kind": "lp", "family": "lr", "order": 4, "freq": 80.0}
    group = {
        "bypass": False,
        "design": xover,
        "compiled": [bq.model_dump() for bq in design_crossover(CrossoverDesign(**xover))],
    }
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][1]["crossover"][0] = group
    sub = await _create_doc(ctx, "Sub", doc)
    response = await ctx.http.get(f"{UI}/diff", params={"a": f"profile:{flat}", "b": f"profile:{sub}"})
    html = response.text
    assert "1 difference from" in html
    assert "Out 2 · band filter 1" in html
    assert "LR4 LP 80 Hz" in html
    assert '<td class="val is-a">off</td>' in html


async def _create_doc(ctx, name: str, doc: dict) -> int:
    response = await ctx.http.post(f"{API}/profiles", json={"name": name, "config": doc})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_load_preview_and_apply(ctx):
    pid = await _create(ctx, "Games", gain_db=-2.0)
    url = f"{UI}/{pid}/load"
    response = await ctx.http.post(url, json={"preset": 1, "preview": True}, headers=HX)
    assert response.status_code == 200
    data = response.json()
    assert data["changes"] == 1
    assert "Store “Games” in preset 2?" in data["question"]
    assert "1 setting change:\n• Out 1 output level: +0.00 dB → −2.00 dB" in data["question"]
    assert "Preset 2 isn't playing: it waits and is sent when you switch to preset 2." in data["question"]
    assert "outputs." not in data["question"]
    assert data["raises"] == [] and data["needs_confirm"] is False and data["problems"] == []

    response = await ctx.http.post(url, json={"preset": 1, "activate": True, "preview": True}, headers=HX)
    assert "switches to preset 2" in response.json()["question"]

    response = await ctx.http.post(url, json={"preset": 1}, headers=HX)
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "Waiting until preset 2 plays" in data["message"]
    assert ctx.session.desired(1).outputs[0].gain_db == -2.0

    response = await ctx.http.post(url, json={"preset": 1, "preview": True}, headers=HX)
    assert "already matches" in response.json()["question"]


async def test_load_confirmation_flow(ctx):
    pid = await _create(ctx, "Loud", gain_db=11.0)
    url = f"{UI}/{pid}/load"
    response = await ctx.http.post(url, json={"preset": 0}, headers=HX)
    assert response.status_code == 409
    assert response.json()["error"] == "confirmation_required"
    response = await ctx.http.post(url, json={"preset": 0, "confirm": True}, headers=HX)
    assert response.status_code == 200
    assert response.json()["level"] == "info"
    await ctx.settle()
    assert ctx.session.sync_summary(0)["counts"]["pending"] == 0


async def test_load_and_switch_from_ui(ctx):
    pid = await _create(ctx, "Games", delay_ms=4.0)
    response = await ctx.http.post(f"{UI}/{pid}/load", json={"preset": 2, "activate": True}, headers=HX)
    assert response.status_code == 200, response.text
    assert "Switched to preset 3" in response.json()["message"]
    assert ctx.fake.master["preset"] == 2


async def test_load_errors(ctx):
    pid = await _create(ctx, "X")
    assert (await ctx.http.post(f"{UI}/{pid}/load", json={"preset": 0})).status_code == 400  # no HX header
    response = await ctx.http.post(f"{UI}/{pid}/load", content=b"nope", headers=HX)
    assert response.status_code == 400
    response = await ctx.http.post(f"{UI}/{pid}/load", json={"preset": 0, "bogus": 1}, headers=HX)
    assert response.status_code == 400
    response = await ctx.http.post(f"{UI}/999/load", json={"preset": 0}, headers=HX)
    assert response.status_code == 404
    response = await ctx.http.post(f"{UI}/{pid}/load", json={"preset": 8}, headers=HX)
    assert response.status_code == 404
    response = await ctx.http.post(f"{UI}/{pid}/load", json={"preset": 0, "serial": 5}, headers=HX)
    assert response.status_code == 404
    big = await _create(ctx, "Big", gain_db=11.5)
    await ctx.session.set_channel_meta("output", 0, max_gain_db=6.0)  # the Shakers ceiling is now below the profile
    response = await ctx.http.post(f"{UI}/{big}/load", json={"preset": 0, "confirm": True}, headers=HX)
    assert response.status_code == 422
    assert response.json()["problems"]


async def test_load_failed_write_warns(ctx):
    pid = await _create(ctx, "P", gain_db=-1.0)
    ctx.fake.fail = 500
    ctx.session.writer.max_attempts = 1
    response = await ctx.http.post(f"{UI}/{pid}/load", json={"preset": 0}, headers=HX)
    assert response.status_code == 200
    data = response.json()
    assert data["level"] == "warning"
    assert "Not finished" in data["message"]


async def test_load_with_a_big_raise_asks_once_by_name(ctx):
    await ctx.session.set_channel_meta("output", 0, name="Shakers")
    pid = await _create(ctx, "Movies", gain_db=12.0)
    url = f"{UI}/{pid}/load"
    data = (await ctx.http.post(url, json={"preset": 1, "preview": True}, headers=HX)).json()
    assert data["needs_confirm"] is True
    assert [r["label"] for r in data["raises"]] == ["Shakers output level"]
    question = data["question"]
    assert "This raises Shakers output level by 12 dB at once (more than 6 dB in one step). Pressing OK" in question
    assert "• Shakers output level: +0.00 dB → +12.00 dB" in question
    assert "outputs." not in question
    raise_12 = {"path": "outputs.0.gain_db", "delta_db": 12.0, "kind": "gain"}
    assert data["confirms"] == {"raises": [raise_12], "fir_loads": []}
    # profiles.js sends back what the question named: one question, no second 409
    response = await ctx.http.post(url, json={"preset": 1, "confirmed": data["confirms"]}, headers=HX)
    assert response.status_code == 200 and response.json()["ok"] is True
    assert ctx.session.desired(1).outputs[0].gain_db == 12.0


async def test_load_asks_again_when_a_raise_grew_after_the_preview(ctx):
    """The answer covers only the raise the preview named: a level lowered meanwhile (another tab, MQTT) makes the
    load a bigger raise, which must be asked about again rather than slip through on the old yes."""
    pid = await _create(ctx, "Loud", gain_db=8.0)
    url = f"{UI}/{pid}/load"
    preview = (await ctx.http.post(url, json={"preset": 0, "preview": True}, headers=HX)).json()
    assert preview["needs_confirm"] is True
    assert preview["confirms"]["raises"] == [{"path": "outputs.0.gain_db", "delta_db": 8.0, "kind": "gain"}]

    assert (await ctx.edit(0, {"outputs": {"0": {"gain_db": -30.0}}})).status_code == 200  # a second client

    response = await ctx.http.post(url, json={"preset": 0, "confirmed": preview["confirms"]}, headers=HX)
    assert response.status_code == 409
    data = response.json()
    assert data["error"] == "confirmation_required"
    assert "by 38 dB" in data["detail"]
    assert data["confirms"]["raises"] == [{"path": "outputs.0.gain_db", "delta_db": 38.0, "kind": "gain"}]
    assert ctx.session.desired(0).outputs[0].gain_db == -30.0  # nothing loaded

    # answering the new question loads it
    response = await ctx.http.post(url, json={"preset": 0, "confirmed": data["confirms"]}, headers=HX)
    assert response.status_code == 200, response.text
    assert ctx.session.desired(0).outputs[0].gain_db == 8.0


async def test_load_confirmed_still_passes_a_smaller_raise(ctx):
    pid = await _create(ctx, "Loud", gain_db=8.0)
    url = f"{UI}/{pid}/load"
    preview = (await ctx.http.post(url, json={"preset": 0, "preview": True}, headers=HX)).json()
    assert (await ctx.edit(0, {"outputs": {"0": {"gain_db": 1.0}}})).status_code == 200  # now a 7 dB raise
    response = await ctx.http.post(url, json={"preset": 0, "confirmed": preview["confirms"]}, headers=HX)
    assert response.status_code == 200, response.text
    assert ctx.session.desired(0).outputs[0].gain_db == 8.0


async def test_load_confirmed_does_not_cover_a_raise_on_another_output(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][0]["gain_db"] = 8.0
    doc["outputs"][1]["gain_db"] = -20.0
    pid = await _create_doc(ctx, "Two", doc)
    url = f"{UI}/{pid}/load"
    preview = (await ctx.http.post(url, json={"preset": 0, "preview": True}, headers=HX)).json()
    assert [r["path"] for r in preview["confirms"]["raises"]] == ["outputs.0.gain_db"]
    assert (await ctx.edit(0, {"outputs": {"1": {"gain_db": -40.0}}})).status_code == 200  # Out 2 now rises 20 dB
    response = await ctx.http.post(url, json={"preset": 0, "confirmed": preview["confirms"]}, headers=HX)
    assert response.status_code == 409
    assert {r["path"] for r in response.json()["confirms"]["raises"]} == {"outputs.0.gain_db", "outputs.1.gain_db"}


async def test_load_409_names_settings(ctx):
    await ctx.session.set_channel_meta("output", 0, name="Shakers")
    pid = await _create(ctx, "Movies", gain_db=12.0)
    response = await ctx.http.post(f"{UI}/{pid}/load", json={"preset": 1}, headers=HX)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail == "This raises Shakers output level by 12 dB at once (more than 6 dB in one step)"


async def test_history_tab_and_revert(ctx):
    await ctx.edit(0, {"outputs": {"0": {"gain_db": -10.0}}})
    first = ctx.session.revisions[0]
    await ctx.edit(0, {"outputs": {"0": {"gain_db": -8.0}}})
    html = (await ctx.http.get("/profiles", params={"tab": "history", "preset": 1})).text
    assert "Out 1 output level −10 → −8 dB" in html
    assert f"r{first}" in html
    assert "Revert to here" in html
    assert f"revision:0:{first}" in html

    url = f"/ui/devices/{SERIAL}/presets/0/history/{first}/revert"
    preview = await ctx.http.post(url, json={"preview": True}, headers=HX)
    assert "Revert preset 1 to revision" in preview.json()["question"]
    response = await ctx.http.post(url, json={}, headers=HX)
    assert response.status_code == 200
    assert f"reverted to revision {first}" in response.json()["message"]
    assert ctx.session.desired(0).outputs[0].gain_db == -10.0

    partial = await ctx.http.get(f"/ui/devices/{SERIAL}/presets/0/history")
    assert "revert" in partial.text
    assert (await ctx.http.post(url, json={}, headers=HX)).json()["changed"] == 0

    bad = await ctx.http.post(url, json={"confirmed": {"raises": [], "bogus": 1}}, headers=HX)
    assert bad.status_code == 400
    bad = await ctx.http.post(f"/ui/devices/{SERIAL}/presets/0/history/999/revert", json={}, headers=HX)
    assert bad.status_code == 404
    assert (await ctx.http.post("/ui/devices/1/presets/0/history/1/revert", json={}, headers=HX)).status_code == 404


async def test_revert_answers_the_409_with_what_it_named(ctx):
    await ctx.edit(0, {"outputs": {"0": {"gain_db": 10.0}}}, confirm="true")
    loud = ctx.session.revisions[0]
    await ctx.edit(0, {"outputs": {"0": {"gain_db": 0.0}}})
    url = f"/ui/devices/{SERIAL}/presets/0/history/{loud}/revert"
    response = await ctx.http.post(url, json={}, headers=HX)
    assert response.status_code == 409
    confirms = response.json()["confirms"]
    assert confirms["raises"] == [{"path": "outputs.0.gain_db", "delta_db": 10.0, "kind": "gain"}]
    await ctx.edit(0, {"outputs": {"0": {"gain_db": -20.0}}})  # meanwhile: the revert is now a 30 dB raise
    response = await ctx.http.post(url, json={"confirmed": confirms}, headers=HX)
    assert response.status_code == 409 and "by 30 dB" in response.json()["detail"]
    response = await ctx.http.post(url, json={"confirmed": response.json()["confirms"]}, headers=HX)
    assert response.status_code == 200, response.text
    assert ctx.session.desired(0).outputs[0].gain_db == 10.0


async def test_history_paging(ctx):
    for gain in range(30):
        await ctx.edit(0, {"outputs": {"2": {"gain_db": -float(gain)}}})
    page = await ctx.http.get(f"/ui/devices/{SERIAL}/presets/0/history")
    assert page.status_code == 200
    assert "Load older revisions" in page.text
    more = page.text.split('hx-get="')[-1].split('"')[0].replace("&amp;", "&")
    older = await ctx.http.get(more)
    assert older.status_code == 200
    assert "<ul" not in older.text  # rows only, appended to the list
    assert (await ctx.http.get(f"/ui/devices/{SERIAL}/presets/9/history")).status_code == 404
    assert (await ctx.http.get("/ui/devices/1/presets/0/history")).status_code == 404


async def test_list_partial(ctx):
    await _create(ctx, "A")
    response = await ctx.http.get(f"{UI}/list")
    assert response.status_code == 200
    assert 'data-profile-name="A"' in response.text


def test_pretty_summary():
    assert pretty_summary(None, "outputs.0.gain_db: 10 → 12") == "Out 1 output level 10 → 12 dB"
    assert pretty_summary(None, "outputs.0.gain_db: -2.5 → 0") == "Out 1 output level −2.5 → 0 dB"
    assert pretty_summary(None, "outputs.0.invert: off → on") == "Out 1 polarity off → on"
    assert pretty_summary(None, "outputs.1.peq.2 changed") == "Out 2 · EQ band 3 changed"
    text = pretty_summary(None, "4 changes: outputs.0.gain_db, inputs.1.mute, outputs.3.delay_ms, …")
    assert text == "4 changes: Out 1 output level, In 2 mute, Out 4 delay, …"
    assert pretty_summary(None, "Loaded profile “Games”") == "Loaded profile “Games”"
    assert pretty_summary(None, None) == "—"


def test_unique_name():
    assert svc.unique_name("A", []) == "A"
    assert svc.unique_name("A", ["a"]) == "A (copy)"
    assert svc.unique_name("A", ["A", "A (copy)"]) == "A (copy 2)"
    assert svc.unique_name("A (copy)", ["A (copy)"]) == "A (copy 2)"
    long = "x" * 80
    assert len(svc.unique_name(long, [long])) <= svc.MAX_NAME


def test_format_unit():
    band = {"bypass": False, "design": {"type": "peaking", "freq": 40.0, "q": 1.0, "gain_db": -3.0}, "compiled": {}}
    assert format_unit("outputs.0.peq.1", band, None).startswith("PEQ 40 Hz")
    off = {**band, "bypass": True}
    assert format_unit("outputs.0.peq.1", off, band).startswith("off (PEQ 40 Hz")
    unity = {"b0": 1, "b1": 0, "b2": 0, "a1": 0, "a2": 0}
    flat = {"bypass": True, "design": {"type": "passthrough"}, "compiled": unity}
    assert format_unit("outputs.0.peq.1", flat, band) == "off"
    raw_a = {"bypass": False, "design": {"type": "raw", "raw": {"b0": 0.5}}, "compiled": {**unity, "b0": 0.5}}
    raw_b = {**raw_a, "compiled": {**unity, "b0": 0.25}}
    assert "[0.5 0 0 0 0]" in format_unit("outputs.0.peq.1", raw_a, raw_b)
    group = {"bypass": False, "design": None, "compiled": [{"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}]}
    assert format_unit("outputs.0.crossover.0", group, None) == "Flat"
    assert format_unit("outputs.0.crossover.0", None, group) == "—"


def test_export_json_is_stable():
    doc = default_config_doc(M2X4HD)
    profile = Profile(1, "A", None, "2x4HD", doc.model_dump(mode="json"), "t", "t")
    data = svc.export_profile(profile)
    assert json.loads(json.dumps(data))["config"] == doc.model_dump(mode="json")


async def test_page_without_device_still_manages_profiles(ctx):
    pid = await _create(ctx, "Movies")
    ctx.app.state.manager.sessions.clear()
    response = await ctx.http.get("/profiles")
    assert response.status_code == 200
    html = response.text
    assert "Movies" in html
    assert "Save current preset as a profile" not in html  # nothing to save from
    assert "Store and play now</button>" not in html
    history = await ctx.http.get("/profiles", params={"tab": "history"})
    assert "No device" in history.text
    diff = await ctx.http.get(f"{UI}/diff", params={"a": f"profile:{pid}", "b": "preset:0"})
    assert diff.status_code == 422
    assert "no device" in _error(diff)
