"""Profiles JSON API: CRUD, save a preset, load (staged / switch & apply), diff, export/import, history revert."""

import asyncio

from app.dsp.design import design_filter
from app.model.config_doc import default_config_doc
from app.model.device_profile import M2X4HD
from app.model.filters import FilterDesign
from app.sync import profiles as svc
from app.sync.session import ConfirmationRequired

from .conftest import API, SERIAL

_DESIGN = {"type": "peaking", "freq": 40.0, "q": 1.0, "gain_db": -3.0}
PEQ_ON = {
    "bypass": False,
    "design": _DESIGN,
    "compiled": design_filter(FilterDesign(**_DESIGN)).model_dump(),
}


async def test_nothing_is_seeded(ctx):
    response = await ctx.http.get(f"{API}/profiles")
    assert response.status_code == 200
    assert response.json() == []
    assert await ctx.repo.list_profiles() == []


async def test_save_current_preset_as_profile(ctx):
    await ctx.edit(0, {"outputs": {"0": {"gain_db": -4.5, "delay_ms": 9.0}}})
    data = await ctx.save("Movies", description="evening")
    assert data["name"] == "Movies"
    assert data["description"] == "evening"
    assert data["product_name"] == "2x4HD"
    assert data["config"]["outputs"][0]["gain_db"] == -4.5
    assert data["config"] == ctx.session.desired(0).model_dump(mode="json")
    listing = (await ctx.http.get(f"{API}/profiles")).json()
    assert [p["name"] for p in listing] == ["Movies"]
    assert "config" not in listing[0]
    one = (await ctx.http.get(f"{API}/profiles/{data['id']}")).json()
    assert one["matches"] == [0]  # preset 1 holds exactly this


async def test_create_from_document_and_validation(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][1]["gain_db"] = 3.0
    response = await ctx.http.post(f"{API}/profiles", json={"name": "Doc", "config": doc})
    assert response.status_code == 201
    assert response.json()["config"]["outputs"][1]["gain_db"] == 3.0

    doc["outputs"][1]["gain_db"] = 40.0
    response = await ctx.http.post(f"{API}/profiles", json={"name": "Too loud", "config": doc})
    assert response.status_code == 422
    assert any("outputs.1.gain_db" in p for p in response.json()["problems"])

    response = await ctx.http.post(f"{API}/profiles", json={"name": "Neither"})
    assert response.status_code == 422
    response = await ctx.http.post(
        f"{API}/profiles", json={"name": "Both", "config": doc, "serial": SERIAL, "preset": 0}
    )
    assert response.status_code == 422
    response = await ctx.http.post(f"{API}/profiles", json={"name": "  ", "serial": SERIAL, "preset": 0})
    assert response.status_code == 422
    response = await ctx.http.post(f"{API}/profiles", json={"name": "X", "serial": SERIAL, "preset": 7})
    assert response.status_code == 404


async def test_names_are_unique_and_duplicates_get_copy_suffix(ctx):
    first = await ctx.save("Games")
    response = await ctx.http.post(f"{API}/profiles", json={"name": "Games", "serial": SERIAL, "preset": 0})
    assert response.status_code == 409
    assert response.json()["error"] == "name_taken"

    dup = await ctx.http.post(f"{API}/profiles/{first['id']}/duplicate")
    assert dup.status_code == 201
    assert dup.json()["name"] == "Games (copy)"
    dup2 = await ctx.http.post(f"{API}/profiles/{first['id']}/duplicate")
    assert dup2.json()["name"] == "Games (copy 2)"
    named = await ctx.http.post(f"{API}/profiles/{first['id']}/duplicate", json={"name": "Games night"})
    assert named.json()["name"] == "Games night"
    assert named.json()["config"] == first["config"]


async def test_rename_describe_and_delete(ctx):
    profile = await ctx.save("Movies")
    other = await ctx.save("Music")
    response = await ctx.http.patch(f"{API}/profiles/{profile['id']}", json={"name": "Cinema", "description": "x"})
    assert response.status_code == 200
    assert response.json()["name"] == "Cinema"
    assert response.json()["description"] == "x"
    response = await ctx.http.patch(f"{API}/profiles/{profile['id']}", json={"name": "Music"})
    assert response.status_code == 409
    response = await ctx.http.patch(f"{API}/profiles/{profile['id']}", json={"name": None})
    assert response.status_code == 422
    response = await ctx.http.patch(f"{API}/profiles/{profile['id']}", json={"description": None})
    assert response.json()["description"] is None

    assert (await ctx.http.delete(f"{API}/profiles/{profile['id']}")).status_code == 204
    assert (await ctx.http.get(f"{API}/profiles/{profile['id']}")).status_code == 404
    assert (await ctx.http.delete(f"{API}/profiles/{profile['id']}")).status_code == 404
    assert [p["id"] for p in (await ctx.http.get(f"{API}/profiles")).json()] == [other["id"]]


async def test_load_into_inactive_preset_is_staged(ctx):
    await ctx.edit(0, {"outputs": {"0": {"gain_db": -2.0}}})
    profile = await ctx.save("Games")
    await ctx.settle()
    ctx.fake.configs.clear()

    response = await ctx.http.post(f"{API}/profiles/{profile['id']}/apply", params={"preset": 1})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["preset"] == 1
    assert data["flush"]["staged"] is True
    assert data["activated"] is False
    assert ctx.fake.configs == []  # preset 2 isn't active: nothing written yet
    assert ctx.session.desired(1) == ctx.session.desired(0)

    history = (await ctx.http.get(f"{API}/devices/{SERIAL}/presets/1/history")).json()
    assert history["revisions"][0]["source"] == "profile"
    assert "Games" in history["revisions"][0]["summary"]

    listing = (await ctx.http.get(f"{API}/profiles")).json()
    assert [(s["serial"], s["preset"]) for s in listing[0]["loaded_into"]] == [(SERIAL, 1)]


async def test_load_into_active_preset_is_mute_wrapped_and_splits_unbypass(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][0]["gain_db"] = -6.0
    doc["outputs"][0]["peq"][2] = {**doc["outputs"][0]["peq"][2], **PEQ_ON}
    created = await ctx.http.post(f"{API}/profiles", json={"name": "Movies", "config": doc})
    assert created.status_code == 201, created.text
    ctx.fake.configs.clear()

    response = await ctx.http.post(f"{API}/profiles/{created.json()['id']}/apply", params={"preset": 0})
    assert response.status_code == 200, response.text
    assert response.json()["flush"]["ok"] is True
    calls = ctx.fake.configs
    # The first call mutes the affected outputs, the last restores them (docs 03 §4 mute-wrap).
    first_outputs = {o["index"]: o for o in calls[0]["outputs"]}
    assert first_outputs[0]["mute"] is True
    assert all(set(o) == {"index", "mute"} for o in calls[0]["outputs"])
    assert any(o.get("index") == 0 and o.get("mute") is False for o in calls[-1].get("outputs", []))

    # PEQ un-bypass goes in a later call than its coefficients.
    def peq_calls(pred):
        return [
            i
            for i, body in enumerate(calls)
            for o in body.get("outputs", [])
            if o["index"] == 0
            for band in o.get("peq", [])
            if band["index"] == 2 and pred(band)
        ]

    coeff = peq_calls(lambda band: "coeff" in band)
    unbypass = peq_calls(lambda band: band.get("bypass") is False)
    assert coeff and unbypass
    assert max(coeff) < min(unbypass)
    assert ctx.session.sync_summary(0)["counts"]["failed"] == 0


async def test_load_needs_confirmation_for_big_raise(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][0]["gain_db"] = 10.0
    created = (await ctx.http.post(f"{API}/profiles", json={"name": "Loud", "config": doc})).json()
    response = await ctx.http.post(f"{API}/profiles/{created['id']}/apply", params={"preset": 0})
    assert response.status_code == 409
    assert response.json()["error"] == "confirmation_required"
    assert ctx.session.desired(0).outputs[0].gain_db == 0.0

    response = await ctx.http.post(f"{API}/profiles/{created['id']}/apply", params={"preset": 0, "confirm": "true"})
    assert response.status_code == 200
    assert ctx.session.desired(0).outputs[0].gain_db == 10.0


async def test_load_default_preset_is_the_active_one(ctx):
    profile = await ctx.save("Base")
    response = await ctx.http.post(f"{API}/profiles/{profile['id']}/apply")
    assert response.status_code == 200
    assert response.json()["preset"] == 0


async def test_load_and_switch(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][1]["delay_ms"] = 9.0
    created = (await ctx.http.post(f"{API}/profiles", json={"name": "Games", "config": doc})).json()
    ctx.fake.configs.clear()
    response = await ctx.http.post(f"{API}/profiles/{created['id']}/apply", params={"preset": 1, "activate": "true"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["activated"] is True
    assert ctx.fake.master["preset"] == 1
    assert data["master"]["preset"] == 1
    delays = [o["delay"] for o in ctx.fake.outputs_written() if o["index"] == 1 and "delay" in o]
    assert delays == [{"secs": 0, "nanos": 9_000_000}]  # 864 samples at 96 kHz
    assert ctx.session.sync_summary(1)["counts"]["pending"] == 0


async def test_load_preview_changes_nothing(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][0]["gain_db"] = -1.0
    doc["outputs"][0]["peq"][0] = {**doc["outputs"][0]["peq"][0], **PEQ_ON}
    created = (await ctx.http.post(f"{API}/profiles", json={"name": "P", "config": doc})).json()
    ctx.fake.configs.clear()
    response = await ctx.http.post(f"{API}/profiles/{created['id']}/apply", params={"preset": 0, "preview": "true"})
    assert response.status_code == 200
    data = response.json()
    assert data["changes"] == 2  # the gain and one PEQ band (design, coefficients and bypass count once)
    assert data["device_changes"] >= 3
    assert data["active"] is True
    assert ctx.fake.configs == []
    assert ctx.session.desired(0).outputs[0].gain_db == 0.0


async def _big_raise_profile(ctx, name: str = "Loud") -> int:
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][0]["gain_db"] = 12.0  # +12 dB at once: the big-step guard asks
    doc["inputs"][0]["routing"][1]["gain_db"] = 7.0  # a route level counts too
    doc["outputs"][2]["gain_db"] = 3.0  # below the 6 dB step: not asked about
    created = (await ctx.http.post(f"{API}/profiles", json={"name": name, "config": doc})).json()
    return created["id"]


async def test_load_preview_returns_the_raises_load_profile_asks_about(ctx):
    pid = await _big_raise_profile(ctx)
    preview = await svc.load_preview(ctx.session, pid, 1)
    assert preview["needs_confirm"] is True and preview["problems"] == []
    assert preview["raise_limit_db"] == 6.0
    raised = {r["path"]: r["delta_db"] for r in preview["raises"]}
    assert raised == {"outputs.0.gain_db": 12.0, "inputs.0.routing.1.gain_db": 7.0}
    assert all(r["kind"] == "gain" for r in preview["raises"])
    assert ctx.session.desired(1).outputs[0].gain_db == 0.0  # a preview changes nothing

    try:
        await svc.load_profile(ctx.session, pid, 1)
    except ConfirmationRequired as exc:
        assert {(r.path, r.delta_db) for r in exc.raises} == set(raised.items())
        assert str(exc) == preview["confirmation"]
    else:
        raise AssertionError("load_profile should have asked first")
    result = await svc.load_profile(ctx.session, pid, 1, confirm=True)
    assert result.edit.doc.outputs[0].gain_db == 12.0


async def test_load_preview_without_raises_needs_no_confirm(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][0]["gain_db"] = -3.0
    pid = (await ctx.http.post(f"{API}/profiles", json={"name": "Quiet", "config": doc})).json()["id"]
    preview = await svc.load_preview(ctx.session, pid, 1)
    assert preview["needs_confirm"] is False and preview["raises"] == [] and preview["confirmation"] is None
    await svc.load_profile(ctx.session, pid, 1)  # no ConfirmationRequired


async def test_api_preview_names_the_raises(ctx):
    await ctx.session.set_channel_meta("output", 0, name="Shakers")
    pid = await _big_raise_profile(ctx)
    data = (await ctx.http.post(f"{API}/profiles/{pid}/apply", params={"preset": 1, "preview": "true"})).json()
    labels = {r["path"]: r["label"] for r in data["raises"]}
    assert labels == {
        "outputs.0.gain_db": "Shakers output level",
        "inputs.0.routing.1.gain_db": "In 1 → Out 2 route level",
    }


async def test_load_refuses_other_device_models(ctx):
    profile = await ctx.repo.create_profile("Other", default_config_doc(M2X4HD), product_name="4x10HD")
    response = await ctx.http.post(f"{API}/profiles/{profile.id}/apply", params={"preset": 0})
    assert response.status_code == 422
    assert "4x10HD" in response.json()["detail"]


async def test_load_with_failed_write_reports_flush(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][2]["gain_db"] = -3.0
    created = (await ctx.http.post(f"{API}/profiles", json={"name": "P", "config": doc})).json()
    ctx.fake.fail = 500
    ctx.session.writer.max_attempts = 1
    response = await ctx.http.post(f"{API}/profiles/{created['id']}/apply", params={"preset": 0})
    assert response.status_code == 200
    assert response.json()["flush"]["ok"] is False
    # the desired state holds the profile even though the device didn't take it
    assert ctx.session.desired(0).outputs[2].gain_db == -3.0
    assert ctx.session.sync_status(0)["outputs.2.gain_db"] != "synced"


async def test_unknown_profile_and_device(ctx):
    assert (await ctx.http.post(f"{API}/profiles/99/apply", params={"preset": 0})).status_code == 404
    assert (await ctx.http.get(f"{API}/profiles/99")).status_code == 404
    profile = await ctx.save("A")
    response = await ctx.http.post(f"{API}/profiles/{profile['id']}/apply", params={"preset": 0, "serial": 1})
    assert response.status_code == 404
    response = await ctx.http.post(f"{API}/profiles/{profile['id']}/apply", params={"preset": 9})
    assert response.status_code == 404


async def test_diff_profile_profile_and_preset(ctx):
    a = await ctx.save("A")
    await ctx.edit(0, {"outputs": {"0": {"gain_db": -5.0, "peq": {"1": PEQ_ON}}}})
    b = await ctx.save("B")
    response = await ctx.http.get(f"{API}/profiles/diff", params={"a": f"profile:{a['id']}", "b": f"profile:{b['id']}"})
    assert response.status_code == 200
    data = response.json()
    assert data["a"]["label"] == "Profile “A”"
    paths = [r["path"] for r in data["rows"]]
    assert paths == ["outputs.0.gain_db", "outputs.0.peq.1"]
    peq = data["rows"][1]
    assert peq["a"]["bypass"] is True and peq["b"]["bypass"] is False
    assert "outputs.0.peq.1.bypass" in peq["fields"]
    assert data["count"] == 2

    same = await ctx.http.get(f"{API}/profiles/diff", params={"a": f"profile:{b['id']}", "b": "preset:0"})
    assert same.json()["count"] == 0
    other = await ctx.http.get(f"{API}/profiles/diff", params={"a": "preset:1", "b": "preset:0"})
    assert other.json()["count"] == 2


async def test_diff_bad_sources(ctx):
    for a in ("nope", "profile:x", "preset", "revision:1", "profile:1:2"):
        response = await ctx.http.get(f"{API}/profiles/diff", params={"a": a, "b": "preset:0"})
        assert response.status_code == 422, a
    response = await ctx.http.get(f"{API}/profiles/diff", params={"a": "profile:5", "b": "preset:0"})
    assert response.status_code == 404
    response = await ctx.http.get(f"{API}/profiles/diff", params={"a": "revision:0:77", "b": "preset:0"})
    assert response.status_code == 404
    response = await ctx.http.get(f"{API}/profiles/diff", params={"a": "preset:9", "b": "preset:0"})
    assert response.status_code == 404


async def test_export_import_round_trip(ctx):
    await ctx.edit(0, {"outputs": {"3": {"invert": True}}})
    profile = await ctx.save("Movies", description="d")
    response = await ctx.http.get(f"{API}/profiles/{profile['id']}/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert 'filename="minidsp-profile-Movies.json"' in response.headers["content-disposition"]
    exported = response.json()
    assert exported["format"] == svc.EXPORT_FORMAT
    assert exported["config"] == profile["config"]

    imported = await ctx.http.post(f"{API}/profiles/import", json=exported)
    assert imported.status_code == 201, imported.text
    data = imported.json()
    assert data["name"] == "Movies (copy)"
    assert data["description"] == "d"
    assert data["config"] == profile["config"]
    assert data["warnings"] == []

    renamed = await ctx.http.post(f"{API}/profiles/import", params={"name": "Fresh"}, json=exported)
    assert renamed.json()["name"] == "Fresh"


async def test_import_rejects_bad_files(ctx):
    response = await ctx.http.post(f"{API}/profiles/import", content=b"not json")
    assert response.status_code == 422
    response = await ctx.http.post(f"{API}/profiles/import", json={"format": "other"})
    assert response.status_code == 422
    response = await ctx.http.post(f"{API}/profiles/import", json={"format": svc.EXPORT_FORMAT, "version": 9})
    assert response.status_code == 422
    bad = {"format": svc.EXPORT_FORMAT, "version": 1, "name": "x", "config": {"outputs": "no"}}
    response = await ctx.http.post(f"{API}/profiles/import", json=bad)
    assert response.status_code == 422
    response = await ctx.http.post(f"{API}/profiles/import", content=b"[" * (2 << 20))
    assert response.status_code == 413
    assert await ctx.repo.list_profiles() == []


async def test_import_warns_about_missing_fir_taps(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][0]["fir"] = {"bypass": True, "blob_id": "deadbeef"}
    data = {"format": svc.EXPORT_FORMAT, "version": 1, "name": "FIR", "product_name": "2x4HD", "config": doc}
    response = await ctx.http.post(f"{API}/profiles/import", json=data)
    assert response.status_code == 201
    assert any("FIR" in w for w in response.json()["warnings"])


async def test_history_revision_and_revert(ctx):
    await ctx.edit(0, {"outputs": {"0": {"gain_db": -10.0}}})
    await ctx.edit(0, {"outputs": {"0": {"gain_db": -8.0}}})
    history = (await ctx.http.get(f"{API}/devices/{SERIAL}/presets/0/history")).json()
    revs = [r["revision"] for r in history["revisions"]]
    assert len(revs) >= 2
    old = revs[1]
    row = (await ctx.http.get(f"{API}/devices/{SERIAL}/presets/0/history/{old}")).json()
    assert row["desired"]["outputs"][0]["gain_db"] == -10.0

    preview = await ctx.http.post(f"{API}/devices/{SERIAL}/presets/0/history/{old}/revert", params={"preview": "true"})
    assert preview.json()["changes"] == 1
    assert ctx.session.desired(0).outputs[0].gain_db == -8.0

    response = await ctx.http.post(f"{API}/devices/{SERIAL}/presets/0/history/{old}/revert")
    assert response.status_code == 200, response.text
    assert response.json()["reverted_to"] == old
    assert ctx.session.desired(0).outputs[0].gain_db == -10.0
    await ctx.settle()
    assert [o["gain"] for o in ctx.fake.outputs_written() if o["index"] == 0 and "gain" in o][-1] == -10.0
    history = (await ctx.http.get(f"{API}/devices/{SERIAL}/presets/0/history")).json()
    assert history["revisions"][0]["source"] == "revert"
    assert history["revisions"][0]["revision"] == revs[0] + 1  # history is appended to, never rewritten

    diff = await ctx.http.get(f"{API}/profiles/diff", params={"a": f"revision:0:{revs[0]}", "b": "preset:0"})
    assert [r["path"] for r in diff.json()["rows"]] == ["outputs.0.gain_db"]


async def test_revert_errors(ctx):
    response = await ctx.http.post(f"{API}/devices/{SERIAL}/presets/0/history/42/revert")
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_revision"
    response = await ctx.http.get(f"{API}/devices/{SERIAL}/presets/0/history/42")
    assert response.status_code == 404
    response = await ctx.http.post(f"{API}/devices/{SERIAL}/presets/7/history/1/revert")
    assert response.status_code == 404
    response = await ctx.http.post(f"{API}/devices/1/presets/0/history/1/revert")
    assert response.status_code == 404


async def test_revert_keeps_uncommitted_edits_as_their_own_revision(ctx):
    await ctx.edit(0, {"outputs": {"1": {"gain_db": -3.0}}})
    base = ctx.session.revisions[0]
    await ctx.http.patch(f"{API}/devices/{SERIAL}/presets/0", json={"outputs": {"1": {"gain_db": -4.0}}})
    response = await ctx.http.post(f"{API}/devices/{SERIAL}/presets/0/history/{base}/revert")
    assert response.status_code == 200
    history = (await ctx.http.get(f"{API}/devices/{SERIAL}/presets/0/history")).json()["revisions"]
    assert [r["source"] for r in history[:2]] == ["revert", "api"]
    assert ctx.session.desired(0).outputs[1].gain_db == -3.0


async def test_delete_forgets_loaded_slots(ctx):
    profile = await ctx.save("A")
    await ctx.http.post(f"{API}/profiles/{profile['id']}/apply", params={"preset": 2})
    slots = await svc.loaded_slots(ctx.repo)
    assert slots[(SERIAL, 2)][0] == profile["id"]
    await ctx.http.delete(f"{API}/profiles/{profile['id']}")
    assert await svc.loaded_slots(ctx.repo) == {}


async def test_later_load_takes_over_the_slot(ctx):
    a = await ctx.save("A")
    b = await ctx.save("B")
    await ctx.http.post(f"{API}/profiles/{a['id']}/apply", params={"preset": 3})
    await asyncio.sleep(0.002)
    await ctx.http.post(f"{API}/profiles/{b['id']}/apply", params={"preset": 3})
    listing = {p["name"]: p for p in (await ctx.http.get(f"{API}/profiles")).json()}
    assert listing["A"]["loaded_into"] == []
    assert [s["preset"] for s in listing["B"]["loaded_into"]] == [3]


async def test_without_database(ctx):
    ctx.app.state.repo = None
    ctx.app.state.manager.repo = None
    ctx.session.repo = None
    assert (await ctx.http.get(f"{API}/profiles")).status_code == 503
    response = await ctx.http.post(f"{API}/profiles", json={"name": "A", "serial": SERIAL, "preset": 0})
    assert response.status_code == 503


async def test_load_writes_settings_the_device_never_confirmed(ctx):
    # The profile equals the preset's desired state, but nothing of it was ever confirmed on the device: a load must
    # still write it all, so the preset really holds the profile.
    created = await ctx.http.post(
        f"{API}/profiles", json={"name": "Flat", "config": ctx.session.desired(0).model_dump(mode="json")}
    )
    pid = created.json()["id"]
    assert ctx.session.sync_summary(0)["counts"]["unknown"] > 0
    preview = (await ctx.http.post(f"{API}/profiles/{pid}/apply", params={"preset": 0, "preview": "true"})).json()
    assert preview["changes"] == 0 and preview["unknown"] == ctx.session.sync_summary(0)["counts"]["unknown"]
    ctx.fake.configs.clear()

    response = await ctx.http.post(f"{API}/profiles/{pid}/apply", params={"preset": 0})
    assert response.status_code == 200, response.text
    assert ctx.fake.configs
    counts = ctx.session.sync_summary(0)["counts"]
    assert counts["unknown"] == 0 and counts["pending"] == 0 and counts["failed"] == 0

    ctx.fake.configs.clear()
    response = await ctx.http.post(f"{API}/profiles/{pid}/apply", params={"preset": 0})
    assert response.status_code == 200 and ctx.fake.configs == []  # synced now: nothing left to write


async def test_export_names_outside_latin_1(ctx):
    for name in ("Ωmega", "Кино", "映画"):
        created = await ctx.http.post(
            f"{API}/profiles", json={"name": name, "config": default_config_doc(M2X4HD).model_dump(mode="json")}
        )
        pid = created.json()["id"]
        for path in ("export", "export/cli-script"):
            response = await ctx.http.get(f"{API}/profiles/{pid}/{path}")
            assert response.status_code == 200, response.text
            disposition = response.headers["content-disposition"]
            assert disposition.isascii() and "filename*=UTF-8''minidsp-profile-" in disposition
    response = await ctx.http.get(f"{API}/profiles/{pid}/export")
    assert 'filename="minidsp-profile-profile.json"' in response.headers["content-disposition"]


async def test_import_rejects_deeply_nested_json(ctx):
    response = await ctx.http.post(f"{API}/profiles/import", content=b"[" * 200_000 + b"]" * 200_000)
    assert response.status_code == 422
    assert await ctx.repo.list_profiles() == []


async def test_cli_script_export_and_import(ctx):
    await ctx.edit(0, {"outputs": {"1": {"gain_db": -4.5, "delay_ms": 2.0, "invert": True}}})
    profile = await ctx.save("Movies")
    response = await ctx.http.get(f"{API}/profiles/{profile['id']}/export/cli-script")
    assert response.status_code == 200
    assert 'filename="minidsp-profile-Movies.txt"' in response.headers["content-disposition"]
    script = response.text
    assert '# minidsp-webui profile "Movies".' in script and "output 1 gain -- -4.5" in script

    imported = await ctx.http.post(
        f"{API}/profiles/import/cli-script", json={"text": script + "config 2\n", "name": "Movies"}
    )
    assert imported.status_code == 201, imported.text
    data = imported.json()
    assert data["name"] == "Movies (copy)" and data["product_name"] == "2x4HD"
    assert data["config"]["outputs"][1] == profile["config"]["outputs"][1]
    assert data["warnings"] and data["warnings"][0].startswith("line ")  # the ignored "config 2"

    bad = await ctx.http.post(f"{API}/profiles/import/cli-script", json={"text": "output 9 gain -- 1", "name": "x"})
    assert bad.status_code == 422
    empty = await ctx.http.post(f"{API}/profiles/import/cli-script", json={"text": "# nothing\n", "name": "x"})
    assert empty.status_code == 422
    model = await ctx.http.post(
        f"{API}/profiles/import/cli-script", json={"text": script, "name": "x", "product_name": "Flex"}
    )
    assert model.status_code == 422
    assert [p.name for p in await ctx.repo.list_profiles()] == ["Movies", "Movies (copy)"]


async def test_profile_delays_are_whole_samples(ctx):
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    doc["outputs"][0]["delay_ms"] = 1.23456  # 118.52 samples
    created = await ctx.http.post(f"{API}/profiles", json={"name": "Odd", "config": doc})
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    rounded = M2X4HD.delay_ms_from_samples(M2X4HD.delay_samples(1.23456))
    assert created.json()["config"]["outputs"][0]["delay_ms"] == rounded

    # A profile stored before delays were rounded (or without a device model) still matches once loaded.
    raw = await ctx.repo.create_profile("Raw", doc)
    response = await ctx.http.post(f"{API}/profiles/{raw.id}/apply", params={"preset": 0, "confirm": "true"})
    assert response.status_code == 200, response.text
    await ctx.settle()
    preview = (await ctx.http.post(f"{API}/profiles/{raw.id}/apply", params={"preset": 0, "preview": "true"})).json()
    assert preview["changes"] == 0 and preview["device_changes"] == 0
    assert (await ctx.http.get(f"{API}/profiles/{raw.id}")).json()["matches"] == [0]
    assert (await ctx.http.get(f"{API}/profiles/{pid}")).json()["matches"] == [0]

    patched = await ctx.http.patch(f"{API}/profiles/{pid}", json={"config": doc})
    assert patched.json()["config"]["outputs"][0]["delay_ms"] == rounded
