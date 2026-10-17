"""Verify JSON API and the Device page partial, on a throwaway app over a real session and the fake frame bridge."""

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import verify as verify_api
from app.daemon.client import DaemonRejected, DeviceNotFound
from app.daemon.rawframes import M2X4HD_MAP, RawFrameReader, RawFrameTimeout
from app.sync import verify as svc
from app.sync.session import SessionManager, StateUnavailable

from ..conftest import SERIAL
from .conftest import f32
from .test_verify import shaker  # noqa: F401


@pytest.fixture
def manager(shaker, client, bus, settings):  # noqa: F811
    m = SessionManager(client, bus, settings)
    m.sessions[SERIAL] = shaker
    return m


@pytest.fixture(autouse=True)
def _fake_bridge(monkeypatch, bridge):
    monkeypatch.setattr(svc, "RawFrameReader", lambda url: RawFrameReader(url, timeout=0.2, connect=bridge.connect))


@pytest.fixture
async def http(manager, settings):
    app = FastAPI()
    app.state.manager = manager
    app.state.settings = settings
    app.include_router(verify_api.router, prefix="/api/v1")
    app.include_router(verify_api.ui_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


# --- JSON ---------------------------------------------------------------------------------------------------------


async def test_get_before_any_run(http):
    r = await http.get(f"/api/v1/devices/{SERIAL}/verify")
    assert r.status_code == 404
    assert r.json()["error"] == "not_verified"


async def test_run_and_fetch(http, bridge):
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "drift"
    assert body["paths"] == ["outputs.0.gain_db"]
    assert body["source"] == "api"
    assert body["marked_unknown"] == []
    item = next(i for i in body["items"] if i["path"] == "outputs.0.gain_db")
    assert item == {
        "path": "outputs.0.gain_db",
        "status": "drift",
        "expected": 12.0,
        "actual": pytest.approx(-6.0206),
        "raw": pytest.approx(-6.0206),
        "note": None,
        "matches_desired": False,
    }
    again = await http.get(f"/api/v1/devices/{SERIAL}/verify")
    assert again.json()["checked_at"] == body["checked_at"]


async def test_mark_unknown_query(http, bridge, shaker):  # noqa: F811
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify", params={"mark_unknown": "true"})
    assert r.json()["marked_unknown"] == ["outputs.0.gain_db"]
    assert shaker.sync_status(0)["outputs.0.gain_db"] == "unknown"


async def test_preset_must_be_active(http, bridge):
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify", params={"preset": 2})
    assert r.status_code == 409
    assert r.json()["error"] == "preset_not_active"
    assert bridge.requests == []
    assert (await http.post(f"/api/v1/devices/{SERIAL}/verify", params={"preset": 0})).status_code == 200


async def test_unknown_device(http):
    assert (await http.post("/api/v1/devices/1/verify")).status_code == 404


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (svc.VerifyUnsupported("nope"), 501, "unsupported"),
        (svc.VerifyError("changed"), 409, "busy"),
        (StateUnavailable("db"), 503, "state_unavailable"),
        (RawFrameTimeout("no answer"), 502, "readback_failed"),
        (DeviceNotFound("gone"), 503, "device_absent"),
        (DaemonRejected(500, "boom"), 502, "daemon_rejected"),
    ],
)
async def test_errors_map_to_http(http, monkeypatch, exc, status, code):
    async def fail(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(svc, "verify_device", fail)
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify")
    assert r.status_code == status
    assert r.json()["error"] == code


async def test_bridge_unreachable_is_503(http, bridge):
    bridge.fail = OSError("refused")
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify")
    assert r.status_code == 503
    assert r.json()["error"] == "daemon_unavailable"


def test_unexpected_errors_are_not_swallowed():
    with pytest.raises(KeyError):
        verify_api.error_response(KeyError("bug"))


# --- HTML partial -------------------------------------------------------------------------------------------------


async def test_partial_before_any_run(http):
    r = await http.get(f"/ui/devices/{SERIAL}/verify")
    assert r.status_code == 200
    assert 'id="verify-panel"' in r.text
    assert "Verify device" in r.text
    assert "Not verified since the app started" in r.text


async def test_partial_run_shows_drift_first(http, bridge):
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    r = await http.post(f"/ui/devices/{SERIAL}/verify")
    assert r.status_code == 200
    html = r.text
    assert "1 setting(s) differ" in html
    assert html.index("outputs.0.gain_db") < html.index("outputs.1.gain_db")
    assert 'class="row-failed"' in html
    assert "−6.02 dB" in html
    assert "Mark drifted settings unknown" in html
    assert "can&#39;t report" in html or "can't report" in html


async def test_partial_clean_run(http):
    r = await http.post(f"/ui/devices/{SERIAL}/verify")
    assert "no drift found" in r.text
    assert "Mark drifted" not in r.text


async def test_partial_shows_errors(http, bridge):
    bridge.fail = OSError("refused")
    r = await http.post(f"/ui/devices/{SERIAL}/verify")
    assert r.status_code == 200
    assert "Verify failed" in r.text


async def test_partial_mark_unknown(http, bridge, shaker):  # noqa: F811
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    await http.post(f"/ui/devices/{SERIAL}/verify")
    r = await http.post(
        f"/ui/devices/{SERIAL}/verify/mark-unknown",
        data={"preset": "0", "path": ["outputs.0.gain_db", "outputs.1.gain_db"]},  # only drifted paths count
    )
    assert r.status_code == 200
    assert "Marked 1 setting(s) unknown" in r.text
    assert "mdsp-sync-changed" in r.headers["HX-Trigger"]
    status = shaker.sync_status(0)
    assert status["outputs.0.gain_db"] == "unknown"
    assert status["outputs.1.gain_db"] == "synced"
    assert svc.last_report(SERIAL).marked_unknown == ["outputs.0.gain_db"]


async def test_partial_mark_unknown_needs_a_matching_report(http):
    r = await http.post(f"/ui/devices/{SERIAL}/verify/mark-unknown", data={"preset": "0", "path": "x"})
    assert "verify again" in r.text
    assert "HX-Trigger" not in r.headers


async def test_partial_unknown_device(http):
    assert (await http.get("/ui/devices/1/verify")).status_code == 404
    assert (await http.post("/ui/devices/1/verify")).status_code == 404
    assert (await http.post("/ui/devices/1/verify/mark-unknown")).status_code == 404


async def test_partial_unsupported_model(http, monkeypatch):
    monkeypatch.setattr(svc, "memory_map_for", lambda _name: None)
    r = await http.get(f"/ui/devices/{SERIAL}/verify")
    assert "isn&#39;t available" in r.text or "isn't available" in r.text
    assert "hx-post" not in r.text


async def test_cross_site_ui_posts_are_refused(http):
    r = await http.post(f"/ui/devices/{SERIAL}/verify", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
