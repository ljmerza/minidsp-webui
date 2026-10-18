"""Verify JSON API and the Device page partial, on a throwaway app over a real session and the fake frame bridge."""

import json
import re

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
    assert "Check device" in r.text
    assert "Not checked since the app started" in r.text


async def test_partial_run_shows_drift_first(http, bridge):
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    r = await http.post(f"/ui/devices/{SERIAL}/verify")
    assert r.status_code == 200
    html = r.text
    assert "1 changed on device" in html
    assert html.index("outputs.0.gain_db") < html.index("outputs.1.gain_db")
    assert 'class="row-failed"' in html
    assert "−6.02 dB" in html
    assert "Forget these values" in html
    assert "Use the device&#39;s values" in html or "Use the device's values" in html
    assert "can&#39;t report" in html or "can't report" in html


async def test_partial_clean_run(http):
    r = await http.post(f"/ui/devices/{SERIAL}/verify")
    assert "nothing changed on device" in r.text
    assert "Forget these values" not in r.text


async def test_partial_shows_errors(http, bridge):
    bridge.fail = OSError("refused")
    r = await http.post(f"/ui/devices/{SERIAL}/verify")
    assert r.status_code == 200
    assert "Check device failed" in r.text


async def test_partial_mark_unknown(http, bridge, shaker):  # noqa: F811
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    await http.post(f"/ui/devices/{SERIAL}/verify")
    r = await http.post(
        f"/ui/devices/{SERIAL}/verify/mark-unknown",
        data={"preset": "0", "path": ["outputs.0.gain_db", "outputs.1.gain_db"]},  # only drifted paths count
    )
    assert r.status_code == 200
    assert "Forgot 1 setting: the app now treats them as Unknown" in r.text
    assert "mdsp-sync-changed" in r.headers["HX-Trigger"]
    status = shaker.sync_status(0)
    assert status["outputs.0.gain_db"] == "unknown"
    assert status["outputs.1.gain_db"] == "synced"
    assert svc.last_report(SERIAL).marked_unknown == ["outputs.0.gain_db"]


async def test_partial_mark_unknown_needs_a_matching_report(http):
    r = await http.post(f"/ui/devices/{SERIAL}/verify/mark-unknown", data={"preset": "0", "path": "x"})
    assert "check the device again" in r.text
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


# --- accept device values -----------------------------------------------------------------------------------------


async def test_accept_device_values_json(http, bridge, shaker, fake):  # noqa: F811
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify/accept", json={"preset": 0})
    assert r.status_code == 404 and r.json()["error"] == "not_verified"
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    await http.post(f"/api/v1/devices/{SERIAL}/verify")
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify/accept", json={"preset": 1})
    assert r.status_code == 409 and r.json()["error"] == "report_changed"

    r = await http.post(f"/api/v1/devices/{SERIAL}/verify/accept", json={"preset": 0, "paths": ["outputs.0.gain_db"]})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == ["outputs.0.gain_db"]
    assert body["report"]["status"] == "ok" and body["report"]["accepted"] == ["outputs.0.gain_db"]
    assert shaker.desired(0).outputs[0].gain_db == -6.02
    assert shaker.sync_status(0)["outputs.0.gain_db"] == "synced"
    assert fake.configs == []


async def test_accept_refuses_a_gain_above_the_ceiling(http, bridge, shaker):  # noqa: F811
    await shaker.set_channel_meta("output", 1, name="Sub", max_gain_db=0.0)
    bridge.memory[M2X4HD_MAP.output_gain[1]] = f32(5.7448344)
    await http.post(f"/api/v1/devices/{SERIAL}/verify")
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify/accept", json={"preset": 0})
    assert r.status_code == 422 and r.json()["problems"]


async def test_partial_offers_and_accepts_device_values(http, bridge, shaker):  # noqa: F811
    shaker.writer.invalidate(0, ["outputs.0.delay_ms"])
    bridge.memory[M2X4HD_MAP.output_delay[0]] = f32(0.0)
    r = await http.post(f"/ui/devices/{SERIAL}/verify")
    assert "nothing changed on device; 1 unknown setting(s)" in r.text
    assert 'id="verify-use"' in r.text
    r = await http.post(f"/ui/devices/{SERIAL}/verify/accept", data={"preset": "0", "path": ["outputs.0.delay_ms"]})
    assert r.status_code == 200
    assert "Using the device&#39;s values for 1 setting." in r.text
    trigger = json.loads(r.headers["HX-Trigger"])
    assert "mdsp-sync-changed" in trigger
    assert trigger["mdsp-toast"]["action"]["label"] == "Undo"
    assert shaker.sync_status(0)["outputs.0.delay_ms"] == "synced"
    assert shaker.desired(0).outputs[0].delay_ms == 0.0


async def test_partial_accept_needs_a_matching_report(http):
    r = await http.post(f"/ui/devices/{SERIAL}/verify/accept", data={"preset": "0", "path": "x"})
    assert "check the device again" in r.text
    assert "HX-Trigger" not in r.headers


# --- safe "Use the device's values" (phase9 design §11.2) ---------------------------------------------------------

ZERO_BIQUAD = dict.fromkeys(("b0", "b1", "b2", "a1", "a2"), 0.0)
XOVER = "outputs.0.crossover.0.compiled.0"


def zero_crossover(bridge) -> None:
    for k in range(5):
        bridge.memory[M2X4HD_MAP.crossover_biquad(0, 0, 0) + k] = f32(0.0)


def visible(html: str) -> str:
    """The text a user can see: hidden form fields carry internal paths on purpose."""
    return re.sub(r"<input[^>]*>", "", html)


async def test_accept_json_refuses_zero_filters_and_offers_undo(http, bridge, shaker, fake):  # noqa: F811
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    zero_crossover(bridge)
    await http.post(f"/api/v1/devices/{SERIAL}/verify")
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify/accept", json={"preset": 0})
    body = r.json()
    assert body["accepted"] == ["outputs.0.gain_db"]
    assert body["refused"] == [XOVER]
    assert body["undo"]["seconds"] == 30
    token = body["undo"]["token"]

    r = await http.post(f"/api/v1/devices/{SERIAL}/verify/accept/undo", json={"preset": 0, "token": "nope"})
    assert r.status_code == 409 and r.json()["error"] == "undo_nothing"
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify/accept/undo", json={"preset": 0, "token": token})
    assert r.status_code == 200
    assert "outputs.0.gain_db" in r.json()["restored"] and r.json()["skipped"] == []
    assert shaker.desired(0).outputs[0].gain_db == 12.0
    assert shaker.writer.pending_paths(0) == []
    assert fake.configs == []
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify/accept/undo", json={"preset": 0})
    assert r.status_code == 409


async def test_undo_json_after_the_window_is_410(http, bridge, shaker, monkeypatch):  # noqa: F811
    from app.sync import session as session_mod

    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    await http.post(f"/api/v1/devices/{SERIAL}/verify")
    await http.post(f"/api/v1/devices/{SERIAL}/verify/accept", json={"preset": 0})
    monkeypatch.setattr(session_mod, "ACCEPT_UNDO_S", -1.0)
    r = await http.post(f"/api/v1/devices/{SERIAL}/verify/accept/undo", json={"preset": 0})
    assert r.status_code == 410 and r.json()["error"] == "undo_expired"


async def test_partial_confirm_lists_names_effects_and_refusals(http, bridge, shaker):  # noqa: F811
    await shaker.set_channel_meta("output", 0, name="Shakers")
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    zero_crossover(bridge)
    r = await http.post(f"/ui/devices/{SERIAL}/verify")
    assert "Forget these values" in r.text and 'id="verify-use"' in r.text
    assert r.text.index("Forget these values") < r.text.index("verify-divider") < r.text.index('id="verify-use"')
    assert "Shakers · band filter 1 is all zeros, which would make Shakers silent." in r.text

    r = await http.get(
        f"/ui/devices/{SERIAL}/verify/accept/confirm", params={"preset": "0", "path": ["outputs.0.gain_db"]}
    )
    assert r.status_code == 200
    html = r.text
    assert "<dialog" in html and "data-modal" in html
    assert "Use the device's values for 1 setting?" in html
    assert "Nothing is sent to the device" in html
    assert "Shakers output level:</span> +12.00 dB" in html and "−6.02 dB" in html
    assert 'Shakers<span class="confirm-effect">: 18.0 dB quieter</span>' in html
    assert "Not used (looks like a read error)" in html
    assert "would make Shakers silent" in html
    assert 'name="path" value="outputs.0.gain_db"' in html
    assert f'name="path" value="{XOVER}"' not in html  # the refused filter is never posted
    assert "outputs." not in visible(html)
    assert shaker.desired(0).outputs[0].gain_db == 12.0  # the confirm changes nothing


async def test_partial_confirm_needs_a_matching_report(http):
    r = await http.get(f"/ui/devices/{SERIAL}/verify/accept/confirm", params={"preset": "0"})
    assert r.status_code == 409


async def test_partial_accept_refuses_a_stale_confirm(http, bridge, shaker):  # noqa: F811
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    await http.post(f"/ui/devices/{SERIAL}/verify")
    r = await http.post(
        f"/ui/devices/{SERIAL}/verify/accept",
        data={"preset": "0", "checked_at": "2000-01-01T00:00:00", "path": ["outputs.0.gain_db"]},
    )
    assert "check the device again" in r.text
    assert shaker.desired(0).outputs[0].gain_db == 12.0


async def test_partial_accept_then_undo(http, bridge, shaker, fake):  # noqa: F811
    bridge.memory[M2X4HD_MAP.output_gain[0]] = f32(-6.0206)
    await http.post(f"/ui/devices/{SERIAL}/verify")
    report = svc.last_report(SERIAL)
    r = await http.post(
        f"/ui/devices/{SERIAL}/verify/accept",
        data={"preset": "0", "checked_at": report.checked_at, "path": ["outputs.0.gain_db"]},
    )
    toast = json.loads(r.headers["HX-Trigger"])["mdsp-toast"]
    assert toast["timeout_ms"] == 30000
    assert toast["action"]["url"].endswith(f"/ui/devices/{SERIAL}/verify/accept/undo")
    assert shaker.desired(0).outputs[0].gain_db == -6.02

    r = await http.post(toast["action"]["url"], data=toast["action"]["values"])
    assert r.status_code == 200
    assert "Undone: the app&#39;s earlier values are back for 1 setting." in r.text
    trigger = json.loads(r.headers["HX-Trigger"])
    assert "mdsp-sync-changed" in trigger and "Undone" in trigger["mdsp-toast"]["message"]
    assert shaker.desired(0).outputs[0].gain_db == 12.0
    assert fake.configs == []

    r = await http.post(toast["action"]["url"], data=toast["action"]["values"])
    assert r.status_code == 409 and "Nothing to undo" in r.text
