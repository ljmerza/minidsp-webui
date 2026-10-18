"""JSON export, schema validation, import planning and applying (offline devices and live sessions)."""

import base64
import copy
import hashlib
import json

import numpy as np
import pytest

from app.model.config_doc import ConfigDoc, default_config_doc, set_path
from app.model.device_profile import M2X4HD
from app.model.payload import device_values
from app.state import Database, Repo
from app.sync import backup as svc
from app.sync import profiles as profiles_svc
from app.sync.fir import FIR_SETTING_KEY
from app.sync.session import SessionManager
from app.web.settings_ui import RUNTIME_KEY

from ..conftest import SERIAL
from ..session.conftest import settle

OTHER = 922222


def doc_with(**values) -> ConfigDoc:
    doc = default_config_doc(M2X4HD)
    for path, value in values.items():
        doc = set_path(doc, path.replace("__", "."), value)
    return doc


def shadow(doc: ConfigDoc) -> dict:
    return dict(device_values(doc, M2X4HD))


async def seed(repo: Repo, serial: int = SERIAL) -> dict:
    """A populated database; returns what was stored."""
    doc = doc_with(outputs__1__gain_db=-3.0, outputs__1__delay_ms=9.0)
    await repo.upsert_device(serial, product_name="2x4HD", hw_id=10, dsp_version=100, fw="1.75")
    await repo.commit_revision(serial, 0, doc, summary="seed", source="ui", applied=shadow(doc), unknown_paths=[])
    await repo.commit_revision(serial, 2, doc_with(outputs__0__mute=True), source="ui")
    await repo.set_channel_meta(serial, "output", 1, name="Shakers", color="#ff0000", max_gain_db=6.0)
    blob = await repo.put_fir_blob([1.0, 0.5, 0.25], name="test.wav")
    await repo.create_profile("Movies", doc, description="evening", product_name="2x4HD")
    await repo.set_setting(RUNTIME_KEY, {"gain_step_confirm_db": 8.0})
    await repo.set_setting(f"adopted.{serial}", {"mode": "trust", "preset": 0})
    return {"doc": doc, "blob": blob}


@pytest.fixture
async def other_repo(tmp_path):
    db = await Database.open(tmp_path / "other" / "state.sqlite3")
    yield Repo(db)
    await db.close()


# --- export / parse ---------------------------------------------------------------------------------------------------


async def test_export_round_trips_through_parse(repo):
    stored = await seed(repo)
    data = await svc.build_export(repo)
    text = json.dumps(data)  # JSON-serializable as is
    backup = svc.parse_backup(text)
    assert backup.format == svc.FORMAT and backup.version == svc.EXPORT_VERSION and backup.db_schema_version == 1
    (device,) = backup.devices
    assert device.serial == SERIAL and device.product_name == "2x4HD" and device.fw == "1.75"
    assert [p.preset for p in device.presets] == [0, 2]
    assert device.presets[0].desired == stored["doc"].model_dump(mode="json")
    assert device.presets[0].applied == shadow(stored["doc"]) and device.presets[0].revision == 1
    assert device.presets[1].applied is None
    assert [(c.kind, c.idx, c.name, c.max_gain_db) for c in device.channels] == [("output", 1, "Shakers", 6.0)]
    assert [p.name for p in backup.profiles] == ["Movies"]
    (blob,) = backup.fir_blobs
    assert blob.id == stored["blob"].id and list(blob.decode()) == [1.0, 0.5, 0.25]
    assert backup.settings[RUNTIME_KEY] == {"gain_step_confirm_db": 8.0}


async def test_export_includes_state_rows_without_a_device_row(repo):
    await repo.save_preset_state(OTHER, 1, desired=default_config_doc(M2X4HD))
    data = await svc.build_export(repo)
    assert [d["serial"] for d in data["devices"]] == [OTHER]
    assert data["devices"][0]["product_name"] is None and data["devices"][0]["presets"][0]["preset"] == 1


def _export(**changes) -> dict:
    data = {
        "format": svc.FORMAT,
        "version": 1,
        "devices": [
            {
                "serial": SERIAL,
                "product_name": "2x4HD",
                "presets": [{"preset": 0, "desired": default_config_doc(M2X4HD).model_dump(mode="json")}],
            }
        ],
    }
    data.update(changes)
    return data


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{not json", "not a JSON file"),
        (b"[]", "expected a JSON object"),
        (json.dumps({"format": "other"}), "not a minidsp-webui backup"),
        (json.dumps(_export(version=99)), "malformed"),
        (json.dumps(_export(extra=1)), "malformed"),
        (b"[" * 100_000 + b"]" * 100_000, "not a JSON file"),  # nested too deeply for the parser
    ],
)
def test_parse_rejects_bad_files(raw, message):
    with pytest.raises(svc.BackupError, match=message):
        svc.parse_backup(raw)


def test_parse_lists_every_problem():
    data = _export()
    preset = data["devices"][0]["presets"][0]
    bad = copy.deepcopy(preset)
    bad["desired"]["outputs"][0]["gain_db"] = "loud"
    data["devices"][0]["presets"].append(bad)
    data["devices"].append(copy.deepcopy(data["devices"][0]))
    data["profiles"] = [{"name": "A", "config": {}}, {"name": " A ", "config": {}}]
    taps = np.array([1.0, 2.0]).astype("<f8").tobytes()
    sha = hashlib.sha256(taps).hexdigest()
    data["fir_blobs"] = [
        {"id": sha[:16], "n_taps": 3, "sha256": sha, "taps": base64.b64encode(taps).decode()},
        {"id": "0" * 16, "n_taps": 2, "sha256": "0" * 64, "taps": base64.b64encode(taps).decode()},
        {"id": "1" * 16, "n_taps": 2, "sha256": "1" * 64, "taps": "!!"},
    ]
    with pytest.raises(svc.BackupError) as info:
        svc.parse_backup(data)
    problems = "\n".join(info.value.problems)
    assert "preset 1 appears twice" in problems
    assert "devices.0.presets.1.desired.outputs.0.gain_db" in problems
    assert f"device {SERIAL} appears twice" in problems
    assert "profile 'A' appears twice" in problems
    assert "taps hold 2 values, n_taps says 3" in problems
    assert "don't match the sha256/id" in problems
    assert "not valid base64" in problems


def test_parse_rejects_bad_schema_fields():
    data = _export()
    data["devices"][0]["channels"] = [{"kind": "output", "idx": 0, "color": "red"}]
    with pytest.raises(svc.BackupError) as info:
        svc.parse_backup(data)
    assert any("devices.0.channels.0.color" in p for p in info.value.problems)


def test_fir_entry_rejects_non_finite_taps():
    taps = np.array([1.0, np.nan]).astype("<f8").tobytes()
    sha = hashlib.sha256(taps).hexdigest()
    entry = svc.FirEntry(id=sha[:16], n_taps=2, sha256=sha, taps=base64.b64encode(taps).decode())
    with pytest.raises(ValueError, match="finite"):
        entry.decode()


def test_import_options():
    assert svc.ImportOptions.build().include == frozenset(svc.PARTS)
    assert svc.ImportOptions.build(include=["presets", " "]).include == {"presets"}
    with pytest.raises(svc.BackupError, match="unknown part"):
        svc.ImportOptions.build(include=["presets", "history"])


# --- import into a database with no live session ----------------------------------------------------------------------


async def test_import_into_an_empty_app(repo, other_repo, settings):
    stored = await seed(repo)
    backup = svc.parse_backup(await svc.build_export(repo))
    plan = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings)
    assert plan.ok, plan.problems
    summary = plan.as_json()
    assert summary["devices"][0]["live"] is False and summary["devices"][0]["channel_changes"] == 1
    assert [p["stored"] for p in summary["devices"][0]["presets"]] == [False, False]
    assert summary["profiles"]["new"] == ["Movies"] and summary["fir_blobs"] == {"new": 1, "total": 1}
    assert summary["settings"] == sorted([RUNTIME_KEY, f"adopted.{SERIAL}"])

    report = await svc.apply_import(plan, repo=other_repo, manager=None, settings=settings)
    assert report.ok, report.errors
    assert [p["mode"] for p in report.presets] == ["stored", "stored"]
    state = await other_repo.get_preset_state(SERIAL, 0)
    assert state.desired == stored["doc"].model_dump(mode="json")
    assert state.applied is None, "the backup's shadow is not trusted by default"
    (rev,) = await other_repo.list_revisions(SERIAL, 0)
    assert rev.source == "import" and rev.summary == svc.IMPORT_SUMMARY
    assert (await other_repo.get_device(SERIAL)).fw == "1.75"
    assert (await other_repo.get_channel_meta(SERIAL, "output", 1)).name == "Shakers"
    assert (await other_repo.get_profile_by_name("Movies")).description == "evening"
    assert (await other_repo.get_fir_blob(stored["blob"].id)).n_taps == 3
    assert await other_repo.get_setting(RUNTIME_KEY) == {"gain_step_confirm_db": 8.0}
    assert settings.gain_step_confirm_db == 8.0, "runtime settings take effect at once"
    assert report.as_json()["profiles"]["created"] == ["Movies"]
    (event,) = await other_repo.list_events(kind="backup")
    assert "imported a JSON backup" in event.message

    # Importing the same backup again changes nothing.
    again = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings)
    assert again.as_json()["profiles"] == {"new": [], "overwrite": [], "skip": []}
    assert again.settings == {} and again.fir_new == [] and again.devices[0].channels == []
    assert [p.changes for p in again.devices[0].presets] == [0, 0]


async def test_restore_shadow_only_for_presets_without_state(repo, other_repo, settings):
    stored = await seed(repo)
    backup = svc.parse_backup(await svc.build_export(repo))
    await other_repo.commit_revision(SERIAL, 2, default_config_doc(M2X4HD), source="ui", applied={"x": 1})
    options = svc.ImportOptions(restore_shadow=True)
    plan = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings, options=options)
    report = await svc.apply_import(plan, repo=other_repo, manager=None, settings=settings)
    assert report.ok
    assert (await other_repo.get_preset_state(SERIAL, 0)).applied == shadow(stored["doc"])
    kept = await other_repo.get_preset_state(SERIAL, 2)
    assert kept.applied == {"x": 1} and kept.desired == doc_with(outputs__0__mute=True).model_dump(mode="json")


async def test_include_limits_the_parts(repo, other_repo, settings):
    await seed(repo)
    backup = svc.parse_backup(await svc.build_export(repo))
    options = svc.ImportOptions.build(include=["profiles"])
    plan = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings, options=options)
    assert plan.devices == [] and plan.settings == {}
    await svc.apply_import(plan, repo=other_repo, manager=None, settings=settings)
    assert await other_repo.list_preset_states(SERIAL) == []
    assert [p.name for p in await other_repo.list_profiles()] == ["Movies"]
    assert (await other_repo.list_fir_blobs())[0].n_taps == 3, "FIR blobs come with whatever references them"


async def test_settings_only_import_brings_no_fir_blobs(repo, other_repo, settings):
    await seed(repo)
    backup = svc.parse_backup(await svc.build_export(repo))
    options = svc.ImportOptions.build(include=["settings"])
    plan = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings, options=options)
    assert plan.fir_new == [] and plan.profiles_new == []
    await svc.apply_import(plan, repo=other_repo, manager=None, settings=settings)
    assert await other_repo.list_fir_blobs() == []


async def test_profile_name_clash(repo, other_repo, settings):
    await seed(repo)
    backup = svc.parse_backup(await svc.build_export(repo))
    await other_repo.create_profile("Movies", default_config_doc(M2X4HD), product_name="2x4HD")
    plan = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings)
    assert plan.as_json()["profiles"]["skip"] == ["Movies"]
    report = await svc.apply_import(plan, repo=other_repo, manager=None, settings=settings)
    assert report.profiles_skipped == ["Movies"]
    assert (await other_repo.get_profile_by_name("Movies")).description is None

    options = svc.ImportOptions(overwrite_profiles=True)
    plan = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings, options=options)
    assert plan.as_json()["profiles"]["overwrite"] == ["Movies"]
    report = await svc.apply_import(plan, repo=other_repo, manager=None, settings=settings)
    assert report.profiles_updated == ["Movies"]
    assert (await other_repo.get_profile_by_name("Movies")).description == "evening"


async def test_target_serial_retargets_presets_and_adoption(repo, other_repo, settings):
    await seed(repo)
    backup = svc.parse_backup(await svc.build_export(repo))
    options = svc.ImportOptions(target_serial=OTHER)
    plan = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings, options=options)
    assert plan.ok and plan.devices[0].serial == OTHER and plan.devices[0].source_serial == SERIAL
    assert f"adopted.{OTHER}" in plan.settings and f"adopted.{SERIAL}" not in plan.settings
    await svc.apply_import(plan, repo=other_repo, manager=None, settings=settings)
    assert len(await other_repo.list_preset_states(OTHER)) == 2
    assert await other_repo.list_preset_states(SERIAL) == []


async def test_plan_problems(repo, settings):
    data = _export()
    device = data["devices"][0]
    desired = device["presets"][0]["desired"]
    desired["outputs"][0]["gain_db"] = 30.0
    desired["outputs"][1]["fir"] = {"bypass": False, "blob_id": "abcdef0123456789"}
    device["presets"].append({"preset": 5, "desired": default_config_doc(M2X4HD).model_dump(mode="json")})
    device["channels"] = [
        {"kind": "input", "idx": 2, "name": "x"},
        {"kind": "output", "idx": 0, "max_gain_db": 40.0},
    ]
    data["profiles"] = [{"name": "Loud", "product_name": "2x4HD", "config": desired}]
    data["settings"] = {RUNTIME_KEY: {"gain_step_confirm_db": 100}, FIR_SETTING_KEY: "yes"}
    plan = await svc.plan_import(svc.parse_backup(data), repo=repo, manager=None, settings=settings)
    problems = "\n".join(plan.problems)
    assert not plan.ok
    assert f"device {SERIAL} preset 1: outputs.0.gain_db: 30.0 dB outside" in problems
    assert "FIR blob abcdef0123456789 is neither in the backup nor stored" in problems
    assert "preset 6: the 2x4HD has 4 presets" in problems
    assert "input 3: the 2x4HD has 2 inputs" in problems
    assert "gain ceiling 40 dB outside" in problems
    assert "profile 'Loud': outputs.0.gain_db" in problems
    assert f"settings.{RUNTIME_KEY}" in problems and f"settings.{FIR_SETTING_KEY}" in problems
    with pytest.raises(svc.BackupError):
        await svc.apply_import(plan, repo=repo, manager=None, settings=settings)


async def test_plan_refuses_unknown_models_and_ambiguous_targets(repo, settings):
    data = _export()
    data["devices"][0]["product_name"] = "10x10HD"
    second = copy.deepcopy(data["devices"][0])
    second["serial"] = OTHER
    data["devices"].append(second)
    backup = svc.parse_backup(data)
    plan = await svc.plan_import(backup, repo=repo, manager=None, settings=settings)
    assert any("unsupported model '10x10HD'" in p for p in plan.problems)
    options = svc.ImportOptions(target_serial=1)
    plan = await svc.plan_import(backup, repo=repo, manager=None, settings=settings, options=options)
    assert any("a target device needs a backup of exactly one" in p for p in plan.problems)


async def test_restore_shadow_is_not_used_for_another_device(repo, other_repo, settings):
    await seed(repo)
    backup = svc.parse_backup(await svc.build_export(repo))
    options = svc.ImportOptions(target_serial=OTHER, restore_shadow=True)
    plan = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings, options=options)
    assert plan.ok and any(f"belongs to device {SERIAL}" in w for w in plan.warnings)
    await svc.apply_import(plan, repo=other_repo, manager=None, settings=settings)
    state = await other_repo.get_preset_state(OTHER, 0)
    assert state.applied is None, "another device's shadow never describes this hardware"


async def test_offline_restore_shadow_needs_a_fully_synced_preset(repo, other_repo, settings):
    doc = doc_with(outputs__0__gain_db=-1.0)
    partial = shadow(doc)
    partial.pop("outputs.0.gain_db")
    backup = backup_of({0: doc, 1: doc}, applied={0: partial, 1: shadow(doc)})
    options = svc.ImportOptions(restore_shadow=True)
    plan = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings, options=options)
    assert [w for w in plan.warnings if "preset 1:" in w and "nothing assumed" in w]
    report = await svc.apply_import(plan, repo=other_repo, manager=None, settings=settings)
    assert report.ok
    assert (await other_repo.get_preset_state(SERIAL, 0)).applied is None
    assert (await other_repo.get_preset_state(SERIAL, 1)).applied == shadow(doc)


async def test_loaded_profiles_record_is_translated(repo, other_repo, settings, tmp_path):
    filler = await repo.create_profile("Filler", default_config_doc(M2X4HD), product_name="2x4HD")
    movies = await repo.create_profile("Movies", doc_with(outputs__0__gain_db=-5.0), product_name="2x4HD")
    await repo.set_setting(
        profiles_svc.LOADED_KEY,
        {f"{SERIAL}:1": {"profile": movies.id, "at": "2026-01-01T00:00:00Z"}, "77:0": {"profile": filler.id}},
    )
    await other_repo.create_profile("Games", doc_with(outputs__1__gain_db=-2.0), product_name="2x4HD")
    backup = svc.parse_backup(await svc.build_export(repo))
    plan = await svc.plan_import(backup, repo=other_repo, manager=None, settings=settings)
    assert profiles_svc.LOADED_KEY not in plan.settings and profiles_svc.LOADED_KEY in plan.as_json()["settings"]
    report = await svc.apply_import(plan, repo=other_repo, manager=None, settings=settings)
    assert profiles_svc.LOADED_KEY in report.settings
    new_movies = await other_repo.get_profile_by_name("Movies")
    new_filler = await other_repo.get_profile_by_name("Filler")
    assert new_movies.id != movies.id
    assert await profiles_svc.loaded_slots(other_repo) == {
        (SERIAL, 1): (new_movies.id, "2026-01-01T00:00:00Z"),
        (77, 0): (new_filler.id, None),
    }

    # Re-targeted: only the backup's device, under the new serial; a profile kept with other settings is dropped.
    third = await Database.open(tmp_path / "third" / "state.sqlite3")
    try:
        target = Repo(third)
        await target.create_profile("Movies", default_config_doc(M2X4HD), product_name="2x4HD")
        options = svc.ImportOptions(target_serial=OTHER)
        backup = backup_of({0: default_config_doc(M2X4HD)})
        backup.profiles = [svc.ProfileEntry(**p) for p in (await svc.build_export(repo))["profiles"]]
        backup.settings = {
            profiles_svc.LOADED_KEY: {f"{SERIAL}:1": {"profile": movies.id}, "77:0": {"profile": filler.id}}
        }
        backup.devices[0].presets[0].preset = 1
        plan = await svc.plan_import(backup, repo=target, manager=None, settings=settings, options=options)
        await svc.apply_import(plan, repo=target, manager=None, settings=settings)
        assert await profiles_svc.loaded_slots(target) == {}, "Movies here holds other settings"
        options = svc.ImportOptions(target_serial=OTHER, overwrite_profiles=True)
        plan = await svc.plan_import(backup, repo=target, manager=None, settings=settings, options=options)
        await svc.apply_import(plan, repo=target, manager=None, settings=settings)
        replaced = await target.get_profile_by_name("Movies")
        assert await profiles_svc.loaded_slots(target) == {(OTHER, 1): (replaced.id, None)}
    finally:
        await third.close()


# --- import into a live session ---------------------------------------------------------------------------------------


@pytest.fixture
def live_manager(client, bus, settings, repo):
    def build(session) -> SessionManager:
        manager = SessionManager(client, bus, settings, repo=repo)
        manager.sessions[session.serial] = session
        return manager

    return build


def backup_of(presets: dict[int, ConfigDoc], *, applied: dict[int, dict] | None = None, **device) -> svc.BackupDoc:
    applied = applied or {}
    return svc.parse_backup(
        {
            "format": svc.FORMAT,
            "version": 1,
            "devices": [
                {
                    "serial": SERIAL,
                    "product_name": "2x4HD",
                    "presets": [
                        {"preset": p, "desired": doc.model_dump(mode="json"), "applied": applied.get(p)}
                        for p, doc in presets.items()
                    ],
                    **device,
                }
            ],
        }
    )


async def test_live_import_applies_the_active_preset_and_stages_the_rest(
    make_session, live_manager, repo, settings, fake
):
    session = await make_session()
    manager = live_manager(session)
    active = doc_with(outputs__1__gain_db=-4.0)
    staged = doc_with(outputs__2__invert=True)
    backup = backup_of({0: active, 1: staged})
    plan = await svc.plan_import(backup, repo=repo, manager=manager, settings=settings)
    assert plan.ok and [(p.active, p.changes) for p in plan.devices[0].presets] == [(True, 1), (False, 1)]
    report = await svc.apply_import(plan, repo=repo, manager=manager, settings=settings, timeout=3)
    assert report.ok, report.errors
    modes = {p["preset"]: p["mode"] for p in report.presets}
    assert modes == {0: "applied", 1: "staged"}
    await settle(session)
    assert session.desired(0) == active and session.desired(1) == staged
    assert any(o["index"] == 1 and o.get("gain") == -4.0 for o in fake.outputs_written())
    # Nothing was confirmed on the device before, so every setting of the active preset is written, not only the
    # ones that differ from the defaults.
    assert session.sync_summary(0)["counts"].get("unknown", 0) == 0
    assert not any(o.get("invert") for o in fake.outputs_written()), "an inactive preset is only staged"
    assert session.sync_status(1)["outputs.2.invert"] == "pending"
    (rev,) = await repo.list_revisions(SERIAL, 0, limit=1)
    assert rev.source == "import"


async def test_live_import_never_trusts_the_backup_shadow_by_default(make_session, live_manager, repo, settings, fake):
    session = await make_session()
    manager = live_manager(session)
    doc = doc_with(outputs__0__gain_db=-1.0)
    backup = backup_of({0: doc}, applied={0: shadow(doc)})
    plan = await svc.plan_import(backup, repo=repo, manager=manager, settings=settings)
    report = await svc.apply_import(plan, repo=repo, manager=manager, settings=settings, timeout=3)
    assert report.presets[0]["mode"] == "applied"
    await settle(session)
    assert fake.configs, "the preset was pushed, not assumed to be on the device"


async def test_live_restore_shadow_trusts_a_fully_synced_preset(make_session, live_manager, repo, settings, fake):
    session = await make_session()
    manager = live_manager(session)
    doc = doc_with(outputs__0__gain_db=-1.0)
    backup = backup_of({0: doc}, applied={0: shadow(doc)})
    options = svc.ImportOptions(restore_shadow=True)
    plan = await svc.plan_import(backup, repo=repo, manager=manager, settings=settings, options=options)
    assert plan.warnings == []
    report = await svc.apply_import(plan, repo=repo, manager=manager, settings=settings, timeout=3)
    assert report.presets[0]["mode"] == "trusted" and report.presets[0]["writes"] == 0
    assert fake.configs == []
    assert set(session.sync_status(0).values()) == {"synced"}
    assert session.adoption is not None


async def test_live_restore_shadow_pushes_a_preset_that_was_not_synced(
    make_session, live_manager, repo, settings, fake
):
    session = await make_session()
    manager = live_manager(session)
    doc = doc_with(outputs__0__gain_db=-1.0)
    partial = shadow(doc)
    partial.pop("outputs.0.gain_db")
    backup = backup_of({0: doc}, applied={0: partial})
    options = svc.ImportOptions(restore_shadow=True)
    plan = await svc.plan_import(backup, repo=repo, manager=manager, settings=settings, options=options)
    assert any("pushed instead of trusted" in w for w in plan.warnings)
    report = await svc.apply_import(plan, repo=repo, manager=manager, settings=settings, timeout=3)
    assert report.presets[0]["mode"] == "applied" and report.warnings == plan.warnings
    await settle(session)
    assert any(o["index"] == 0 and o.get("gain") == -1.0 for o in fake.outputs_written())


async def test_live_import_lowers_a_ceiling_after_the_gains(make_session, live_manager, repo, settings):
    session = await make_session()
    await session.set_value(0, "outputs.1.gain_db", 5.0, confirm=True)
    manager = live_manager(session)
    backup = backup_of(
        {0: doc_with(outputs__1__gain_db=-2.0)}, channels=[{"kind": "output", "idx": 1, "max_gain_db": 0.0}]
    )
    plan = await svc.plan_import(backup, repo=repo, manager=manager, settings=settings)
    report = await svc.apply_import(plan, repo=repo, manager=manager, settings=settings, timeout=3)
    assert report.ok, report.errors
    assert report.channels == 1
    assert session.channel_ceiling("output", 1) == 0.0


async def test_live_import_reports_preset_errors_and_continues(make_session, live_manager, repo, settings):
    session = await make_session()
    await session.set_channel_meta("output", 0, max_gain_db=0.0)
    manager = live_manager(session)
    backup = backup_of({0: doc_with(outputs__0__gain_db=3.0), 1: doc_with(outputs__1__gain_db=-1.0)})
    plan = await svc.plan_import(backup, repo=repo, manager=manager, settings=settings)
    report = await svc.apply_import(plan, repo=repo, manager=manager, settings=settings, timeout=3)
    assert not report.ok and "preset 1" in report.errors[0] and "ceiling" in report.errors[0]
    assert [p["preset"] for p in report.presets] == [1]
    (event,) = await repo.list_events(kind="backup")
    assert event.level == "warning"


async def test_live_import_restores_the_fir_toggle_and_adoption(make_session, live_manager, repo, settings):
    session = await make_session()
    assert session.adoption is None and not session.profile.fir_supported
    manager = live_manager(session)
    backup = svc.parse_backup(
        {
            "format": svc.FORMAT,
            "version": 1,
            "settings": {FIR_SETTING_KEY: True, f"adopted.{SERIAL}": {"mode": "push", "preset": 0}, "x.y": [1]},
        }
    )
    plan = await svc.plan_import(backup, repo=repo, manager=manager, settings=settings)
    report = await svc.apply_import(plan, repo=repo, manager=manager, settings=settings)
    assert sorted(report.settings) == sorted([FIR_SETTING_KEY, f"adopted.{SERIAL}", "x.y"])
    assert session.profile.fir_supported and session.adoption == {"mode": "push", "preset": 0}
    assert await repo.get_setting("x.y") == [1]


async def test_product_mismatch_with_a_live_session(make_session, live_manager, repo, settings):
    session = await make_session()
    manager = live_manager(session)
    backup = backup_of({0: default_config_doc(M2X4HD)})
    backup.devices[0].product_name = "Flex"
    plan = await svc.plan_import(backup, repo=repo, manager=manager, settings=settings)
    assert any("the backup is for a Flex, the device is a 2x4HD" in p for p in plan.problems)


def test_fully_synced():
    doc = doc_with(outputs__0__delay_ms=1.0)
    values = shadow(doc)
    entry = svc.PresetEntry(preset=0, desired={}, applied=values)
    assert svc.fully_synced(entry, doc, M2X4HD)
    nearly = {**values, "outputs.0.delay_ms": 1.004}  # the same whole number of samples
    assert svc.fully_synced(svc.PresetEntry(preset=0, desired={}, applied=nearly), doc, M2X4HD)
    staged_push = ({**values, "#force": ["outputs.0.mute"]}, []), ({**values, "#wrap_requested": True}, [])
    for applied, unknown in (
        ({**values, "#pending": ["x"]}, []),
        (values, ["outputs.0.mute"]),
        (None, []),
        *staged_push,
    ):
        entry = svc.PresetEntry(preset=0, desired={}, applied=applied, unknown_paths=unknown)
        assert not svc.fully_synced(entry, doc, M2X4HD)


async def test_live_restore_shadow_is_not_trusted_for_another_device(make_session, live_manager, repo, settings, fake):
    session = await make_session()
    manager = live_manager(session)
    doc = doc_with(outputs__0__gain_db=-1.0)
    backup = backup_of({0: doc}, applied={0: shadow(doc)}, serial=OTHER)
    options = svc.ImportOptions(target_serial=SERIAL, restore_shadow=True)
    plan = await svc.plan_import(backup, repo=repo, manager=manager, settings=settings, options=options)
    assert plan.ok and any(f"belongs to device {OTHER}" in w for w in plan.warnings)
    report = await svc.apply_import(plan, repo=repo, manager=manager, settings=settings, timeout=3)
    assert report.presets[0]["mode"] == "applied"
    await settle(session)
    assert fake.configs, "the preset was written, not assumed to be on the device"
    assert set(session.sync_status(0).values()) == {"synced"}
