"""The backup JSON API and the Settings → Backup partial, on a throwaway app."""

import json

import httpx

from app.model.config_doc import default_config_doc
from app.model.device_profile import M2X4HD
from app.state import Database, Repo
from app.sync import backup as svc
from app.sync.session import SessionManager

from ..conftest import SERIAL
from ..session.conftest import settle
from .conftest import make_app

API = "/api/v1/backup"


async def seeded_export(repo: Repo) -> bytes:
    doc = default_config_doc(M2X4HD).model_copy(deep=True)
    doc.outputs[1].gain_db = -6.0
    await repo.upsert_device(SERIAL, product_name="2x4HD")
    await repo.commit_revision(SERIAL, 0, doc, source="ui")
    await repo.create_profile("Games", doc, product_name="2x4HD")
    return json.dumps(await svc.build_export(repo)).encode()


async def test_export(http, repo):
    await repo.create_profile("Movies", default_config_doc(M2X4HD))
    response = await http.get(f"{API}/export")
    assert response.status_code == 200 and "content-disposition" not in response.headers
    assert response.json()["profiles"][0]["name"] == "Movies"
    response = await http.get(f"{API}/export", params={"download": "true"})
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="minidsp-webui-backup-') and disposition.endswith('.json"')


async def test_import_preview_and_confirm(http, repo, tmp_path, settings):
    body = await seeded_export(repo)
    other = await Database.open(tmp_path / "other.sqlite3")
    http.app.state.repo = Repo(other)
    try:
        response = await http.post(f"{API}/import/preview", content=body)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["ok"] and plan["profiles"]["new"] == ["Games"]
        assert plan["devices"][0]["presets"] == [
            {"preset": 0, "changes": 1, "active": False, "stored": False, "unknown": 0}
        ]

        response = await http.post(f"{API}/import", content=body)
        assert response.status_code == 409 and response.json()["error"] == "confirmation_required"
        assert response.json()["plan"]["ok"] is True
        assert await http.app.state.repo.list_profiles() == [], "nothing written without confirm"

        response = await http.post(
            f"{API}/import", params={"confirm": "true", "include": "presets,profiles"}, content=body
        )
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["ok"] and report["profiles"]["created"] == ["Games"] and report["presets"][0]["mode"] == "stored"
    finally:
        await other.close()


async def test_import_errors(http, repo, monkeypatch):
    response = await http.post(f"{API}/import/preview", content=b"nope")
    assert response.status_code == 422 and "not a JSON file" in response.json()["detail"]
    response = await http.post(f"{API}/import/preview", params={"include": "everything"}, content=b"{}")
    assert response.status_code == 422 and "unknown part" in response.json()["detail"]

    bad = json.loads(await seeded_export(repo))
    bad["devices"][0]["presets"][0]["desired"]["outputs"][0]["gain_db"] = 50.0
    response = await http.post(f"{API}/import", params={"confirm": "true"}, content=json.dumps(bad))
    assert response.status_code == 422
    assert any("outputs.0.gain_db" in p for p in response.json()["problems"])

    monkeypatch.setattr(svc, "MAX_JSON_BYTES", 10)
    response = await http.post(f"{API}/import/preview", content=b"x" * 11)
    assert response.status_code == 413

    async def chunks():
        yield b"x" * 6
        yield b"x" * 6

    response = await http.post(f"{API}/import/preview", content=chunks())
    assert response.status_code == 413, "a streamed body without a length is cut off too"


async def test_import_into_a_live_session(http, repo, make_session, fake):
    session = await make_session()
    http.app.state.manager.sessions[SERIAL] = session
    doc = default_config_doc(M2X4HD).model_copy(deep=True)
    doc.outputs[3].gain_db = -12.0
    backup = {
        "format": svc.FORMAT,
        "version": 1,
        "devices": [
            {"serial": SERIAL, "product_name": "2x4HD", "presets": [{"preset": 0, "desired": doc.model_dump()}]}
        ],
    }
    response = await http.post(f"{API}/import", params={"confirm": "true"}, content=json.dumps(backup))
    assert response.status_code == 200, response.text
    assert response.json()["presets"][0]["mode"] == "applied"
    await settle(session)
    assert any(o["index"] == 3 and o.get("gain") == -12.0 for o in fake.outputs_written())


async def test_without_a_database(settings, client, bus):
    app = make_app(settings, None, SessionManager(client, bus, settings))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get(f"{API}/export")).status_code == 503
        assert (await http.get(f"{API}/db")).status_code == 503
        assert (await http.post(f"{API}/import/preview", content=b"{}")).status_code == 503
        page = await http.get("/ui/settings/backup")
        assert page.status_code == 200 and "The state database isn" in page.text


async def test_db_download_and_files(http, repo, settings):
    await repo.upsert_device(SERIAL, product_name="2x4HD")
    response = await http.get(f"{API}/db")
    assert response.status_code == 200 and response.headers["content-type"] == "application/vnd.sqlite3"
    assert response.headers["content-disposition"].startswith('attachment; filename="minidsp-webui-')
    assert svc.validate_db_bytes(response.content).serials == [SERIAL]

    listing = (await http.get(f"{API}/db/files")).json()
    assert listing == {"files": [], "keep": svc.KEEP_BACKUPS, "pending_restore": None}
    result = svc.run_backup(settings.db_path, svc.default_backup_dir(settings.db_path))
    listing = (await http.get(f"{API}/db/files")).json()
    assert [f["name"] for f in listing["files"]] == [result.path.name]
    response = await http.get(f"{API}/db/files/{result.path.name}")
    assert response.status_code == 200 and response.content == result.path.read_bytes()
    assert (await http.get(f"{API}/db/files/state.sqlite3")).status_code == 404
    assert (await http.get(f"{API}/db/files/..%2Fstate.sqlite3")).status_code == 404


async def test_db_download_of_a_memory_database(http, settings):
    settings.db_path = ":memory:"
    response = await http.get(f"{API}/db")
    assert response.status_code == 503 and "not a plain file" in response.json()["detail"]


async def test_db_restore_flow(http, repo, settings):
    await repo.upsert_device(SERIAL, product_name="2x4HD")
    data = svc.snapshot(settings.db_path)

    response = await http.post(f"{API}/db/restore", params={"dry_run": "true"}, content=data)
    assert response.status_code == 200 and response.json()["staged"] is False
    assert svc.pending_restore(settings.db_path) is None

    response = await http.post(f"{API}/db/restore", content=b"not a database")
    assert response.status_code == 422 and response.json()["detail"] == "not an SQLite database"

    response = await http.post(f"{API}/db/restore", content=data)
    assert response.status_code == 200 and response.json()["restart_required"] is True
    assert response.json()["serials"] == [SERIAL]
    assert (await http.get(f"{API}/db/files")).json()["pending_restore"]["size"] == len(data)
    (event,) = await repo.list_events(kind="backup")
    assert "restart" in event.message

    page = await http.get("/ui/settings/backup")
    assert "A database restore is staged" in page.text

    assert (await http.delete(f"{API}/db/restore")).json() == {"cancelled": True}
    assert (await http.delete(f"{API}/db/restore")).json() == {"cancelled": False}


async def test_restore_works_without_a_database(settings, manager, tmp_path):
    source = await Database.open(tmp_path / "src.sqlite3")
    await source.close()
    data = svc.snapshot(tmp_path / "src.sqlite3")
    app = make_app(settings, None, manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(f"{API}/db/restore", content=data)
    assert response.status_code == 200 and svc.pending_restore(settings.db_path) is not None


async def test_settings_partial(http, repo, make_session, settings):
    page = await http.get("/ui/settings/backup")
    assert page.status_code == 200
    assert 'id="settings-backup"' in page.text and "Download JSON backup" in page.text
    assert "/api/v1/backup/export?download=true" in page.text
    assert "export/cli-script" not in page.text, "no device, no CLI script links"
    assert "No nightly backups yet" in page.text

    session = await make_session(start=False)
    http.app.state.manager.sessions[SERIAL] = session
    svc.run_backup(settings.db_path, svc.default_backup_dir(settings.db_path))
    page = await http.get("/ui/settings/backup")
    for p in range(4):
        assert f"/api/v1/devices/{SERIAL}/presets/{p}/export/cli-script?download=true" in page.text
    assert "/api/v1/backup/db/files/minidsp-webui-" in page.text
    assert f'"serial": {SERIAL}' in page.text


async def test_cancel_from_the_partial(http, settings):
    svc.restore_path(settings.db_path).write_bytes(b"x")
    page = await http.post("/ui/settings/backup/restore/cancel")
    assert page.status_code == 200 and "Restore cancelled." in page.text
    assert svc.pending_restore(settings.db_path) is None
    page = await http.post("/ui/settings/backup/restore/cancel")
    assert "No restore was staged." in page.text


async def test_partial_refuses_cross_site_posts(http, settings):
    response = await http.post("/ui/settings/backup/restore/cancel", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403
