"""The presets / adopt / import-export routers, mounted on a throwaway app around a real session."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import adopt, import_export, presets
from app.sync.session import SessionManager

from ..conftest import SERIAL
from .conftest import settle, switch_externally

PI_NOTES = (Path(__file__).parents[2] / "fixtures" / "pi_notes_commands.txt").read_text()
BASE = f"/api/v1/devices/{SERIAL}"


@pytest.fixture
async def api(session, client, bus, settings):
    app = FastAPI()
    manager = SessionManager(client, bus, settings)
    manager.sessions[SERIAL] = session
    app.state.manager = manager
    for module in (presets, adopt, import_export):
        app.include_router(module.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def test_get_preset(api):
    response = await api.get(f"{BASE}/presets/0")
    assert response.status_code == 200 and response.headers["etag"] == '"0"'
    data = response.json()
    assert data["preset"] == 0 and data["active"] is True and data["revision"] == 0
    assert len(data["desired"]["outputs"]) == 4
    assert data["sync"]["outputs.0.gain_db"] == "unknown"
    assert data["counts"]["unknown"] == len(data["sync"])
    lean = (await api.get(f"{BASE}/presets/1", params={"sync": "false"})).json()
    assert "sync" not in lean and lean["active"] is False
    listing = (await api.get(f"{BASE}/presets")).json()
    assert [p["preset"] for p in listing] == [0, 1, 2, 3]


async def test_unknown_device_and_preset(api):
    assert (await api.get("/api/v1/devices/5/presets/0")).status_code == 404
    response = await api.get(f"{BASE}/presets/4")
    assert response.status_code == 404 and response.json()["error"] == "unknown_preset"


async def test_patch_preset(api, session, fake):
    response = await api.patch(
        f"{BASE}/presets/0",
        params={"commit": "true"},
        content=b'{"outputs": {"0": {"gain_db": -2.5}}}',
        headers={"content-type": "application/merge-patch+json"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["revision"] == 1 and data["changed"] == ["outputs.0.gain_db"]
    # "pending" until the writer flushes (50 ms gather window); a slow (e.g. parallel, loaded) run can already see the
    # flush done by the time the response is built. Either is correct; the write itself is checked below.
    assert data["sync"]["outputs.0.gain_db"] in ("pending", "synced")
    await settle(session)
    assert fake.configs == [{"outputs": [{"index": 0, "gain": -2.5}]}]
    assert (await api.get(f"{BASE}/presets/0")).json()["sync"]["outputs.0.gain_db"] == "synced"


async def test_patch_validation_and_guards(api, fake):
    response = await api.patch(f"{BASE}/presets/0", json={"outputs": {"0": {"gain_db": 40}}})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid" and "outputs.0.gain_db" in response.json()["problems"][0]

    response = await api.patch(f"{BASE}/presets/0", json={"outputs": {"0": {"gian_db": 1}}})
    assert response.status_code == 422 and response.json()["problems"] == ["outputs.0.gian_db: unknown field"]

    response = await api.patch(f"{BASE}/presets/0", json={"outputs": {"0": {"gain_db": 10}}})
    assert response.status_code == 409 and response.json()["error"] == "confirmation_required"
    response = await api.patch(
        f"{BASE}/presets/0", params={"confirm": "true"}, json={"outputs": {"0": {"gain_db": 10}}}
    )
    assert response.status_code == 200

    assert (await api.patch(f"{BASE}/presets/0", json=[1])).status_code == 422
    assert fake.configs == [] or fake.configs[-1] == {"outputs": [{"index": 0, "gain": 10.0}]}


async def test_if_match(api):
    patch = {"outputs": {"1": {"mute": True}}}
    commit = {"commit": "true"}
    response = await api.patch(f"{BASE}/presets/0", params=commit, json=patch, headers={"If-Match": '"0"'})
    assert response.status_code == 200 and response.json()["revision"] == 1
    unmute = {"outputs": {"1": {"mute": False}}}
    response = await api.patch(f"{BASE}/presets/0", params=commit, json=unmute, headers={"If-Match": "0"})
    assert response.status_code == 412
    assert response.json() | {"detail": None} == {
        "detail": None,
        "error": "revision_conflict",
        "expected": 0,
        "revision": 1,
    }
    response = await api.patch(f"{BASE}/presets/0", params=commit, json=unmute, headers={"If-Match": 'W/"1"'})
    assert response.status_code == 200 and response.json()["revision"] == 2
    # Without commit the edit stays pending (no revision row yet); it counts once someone checks a revision.
    response = await api.patch(f"{BASE}/presets/0", json=patch, headers={"If-Match": "*"})
    assert response.status_code == 200 and response.json()["revision"] == 2
    response = await api.patch(f"{BASE}/presets/0", json=unmute, headers={"If-Match": "2"})
    assert response.status_code == 412 and response.json()["revision"] == 3
    assert (await api.patch(f"{BASE}/presets/0", json=patch, headers={"If-Match": "abc"})).status_code == 400


async def test_channel_patches_and_explicit_commit(api, session, fake):
    response = await api.patch(
        f"{BASE}/presets/0/outputs/2", params={"commit": "false"}, json={"gain_db": -6.0, "invert": True}
    )
    assert response.status_code == 200
    assert response.json()["revision"] == 0 and response.json()["summary"]["uncommitted"] is True
    response = await api.patch(
        f"{BASE}/presets/0/inputs/1", params={"commit": "false"}, json={"routing": {"2": {"enabled": True}}}
    )
    assert response.json()["device_paths"] == ["inputs.1.routing.2.enabled"]
    assert (await api.patch(f"{BASE}/presets/0/outputs/9", json={"mute": True})).status_code == 422

    response = await api.post(f"{BASE}/presets/0/commit")
    assert response.json() == {"serial": SERIAL, "preset": 0, "revision": 1}
    history = (await api.get(f"{BASE}/presets/0/history")).json()
    assert history["revision"] == 1
    assert [r["summary"] for r in history["revisions"]] == [
        "3 changes: inputs.1.routing.2.enabled, outputs.2.gain_db, outputs.2.invert"
    ]
    await settle(session)
    assert len(fake.configs) >= 1


async def test_push_and_activate(api, session, fake):
    response = await api.post(f"{BASE}/presets/0/push", params={"mute_wrap": "true"})
    assert response.status_code == 200
    assert response.json()["flush"]["ok"] is True
    assert fake.configs[0] == {"outputs": [{"index": n, "mute": True} for n in range(4)]}

    await api.patch(f"{BASE}/presets/2/outputs/0", json={"mute": True})
    await settle(session, 2)  # the writer decides "staged" on its next cycle
    staged = (await api.get(f"{BASE}/presets/2", params={"sync": "false"})).json()
    assert staged["staged"] is True and staged["counts"]["pending"] == 1
    response = await api.post(f"{BASE}/presets/2/activate")
    assert response.status_code == 200
    data = response.json()
    assert data["master"]["preset"] == 2 and data["summary"]["active"] is True
    assert data["summary"]["counts"]["pending"] == 0
    assert fake.masters == [{"preset": 2}]
    await switch_externally(session, fake, 0)


async def test_adopt_routes(api, fake):
    status = (await api.get(f"{BASE}/adopt")).json()
    assert status["adopted"] is False and status["modes"] == ["trust", "push", "unknown"]

    response = await api.post(f"{BASE}/adopt", json={"mode": "push"})
    assert response.status_code == 409
    response = await api.post(f"{BASE}/adopt", json={"mode": "trust"})
    assert response.status_code == 409 and response.json()["error"] == "confirmation_required"
    assert "confirm_defaults" in response.json()["detail"]
    assert (await api.get(f"{BASE}/adopt")).json()["adopted"] is False

    response = await api.post(f"{BASE}/adopt", json={"mode": "trust", "script": PI_NOTES})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["writes"] == 0 and data["known"] > 0 and data["revision"] == 1 and data["mode"] == "trust"
    assert fake.configs == []
    assert (await api.get(f"{BASE}/adopt")).json()["adoption"]["mode"] == "trust"

    doc = (await api.get(f"{BASE}/presets/0")).json()["desired"]
    doc["outputs"][3]["gain_db"] = "loud"
    response = await api.post(f"{BASE}/adopt", json={"mode": "unknown", "doc": doc})
    assert response.status_code == 422 and response.json()["problems"][0].startswith("outputs.3.gain_db")
    assert (await api.post(f"{BASE}/adopt", json={"mode": "trust", "bogus": 1})).status_code == 422
    response = await api.post(f"{BASE}/adopt", json={"mode": "trust", "preset": 1, "confirm_defaults": True})
    assert response.status_code == 200 and response.json()["unknown"] == 0 and fake.configs == []


async def test_script_preview(api):
    response = await api.post("/api/v1/import/cli-script", json={"text": PI_NOTES + "\noutput 9 gain 0\ngain -- -3\n"})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False and len(data["errors"]) == 1 and len(data["warnings"]) == 1
    assert data["doc"]["outputs"][0]["gain_db"] == 12.0 and data["problems"] == []
    assert {"line": 12, "path": "outputs.0.gain_db", "value": 12.0} in data["ops"]
    assert "outputs.0.crossover.0.bypass" in data["device_paths"]

    response = await api.post(
        "/api/v1/import/cli-script",
        json={"text": "output 1 gain -- -1", "serial": SERIAL, "preset": 0, "base": "current"},
    )
    assert response.json()["doc"]["outputs"][1]["gain_db"] == -1.0
    bad = await api.post("/api/v1/import/cli-script", json={"text": "output 0 gain 30"})
    assert bad.json()["problems"] == ["outputs.0.gain_db: 30.0 dB outside -72.0..12.0"]
    assert (await api.post("/api/v1/import/cli-script", json={"text": "", "base": "current"})).status_code == 422
    assert (await api.post("/api/v1/import/cli-script", json={"text": "", "serial": 1})).status_code == 404
    missing_preset = await api.post("/api/v1/import/cli-script", json={"text": "", "serial": SERIAL, "base": "current"})
    assert missing_preset.status_code == 422


async def test_import_and_export_routes(api, session, fake):
    url = f"{BASE}/presets/0/import/cli-script"
    response = await api.post(url, json={"text": "output 3 gain -- -2\noutput 3 mute on\nsource toslink"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["device_paths"] == ["outputs.3.gain_db", "outputs.3.mute"]
    assert data["flush"]["ok"] is True and data["revision"] == 1
    assert data["warnings"][0]["line"] == 3

    response = await api.post(url, json={"text": "output 0 gain 12"})
    assert response.status_code == 409
    response = await api.post(url, json={"text": "output 0 frobnicate"})
    assert response.status_code == 422 and response.json()["problems"][0].startswith("line 1:")
    response = await api.post(url, json={"text": "output 0 gain 1"}, headers={"If-Match": "0"})
    assert response.status_code == 412

    export = await api.get(f"{BASE}/presets/0/export/cli-script", params={"download": "true"})
    assert export.status_code == 200 and export.headers["content-type"].startswith("text/plain")
    assert export.headers["content-disposition"] == 'attachment; filename="minidsp-preset-1.txt"'
    assert "output 3 gain -- -2" in export.text
    plain = await api.get(f"{BASE}/presets/0/export/cli-script", params={"mute_wrap": "false"})
    assert "content-disposition" not in plain.headers and "output 3 mute on" in plain.text


async def test_state_unavailable_is_503(api, session, repo):
    session.loaded = False
    await repo.db.close()
    response = await api.get(f"{BASE}/presets/0")
    assert response.status_code == 503 and response.json()["error"] == "state_unavailable"
