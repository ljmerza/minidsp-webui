"""Compressor GET/PUT on a real session and the stateful fake daemon: presets, ranges, curve and write ordering."""

import json

import pytest

from app.api.v1.compressor_fir import COMPRESSOR_PRESETS, compressor_problems, transfer_curve

from ..conftest import SERIAL
from ..session.conftest import settle

BASE = f"/api/v1/devices/{SERIAL}/presets"
URL = f"{BASE}/0/outputs/2/compressor"


def compressor_writes(fake) -> list[dict]:
    return [o["compressor"] for o in fake.outputs_written() if "compressor" in o]


def test_transfer_curve_is_hard_knee():
    curve = dict((x, y) for x, y in transfer_curve(-36, 50, step=6))
    assert curve[-90] == -90 and curve[-36] == -36
    assert curve[0] == pytest.approx(-36 + 36 / 50, abs=1e-3)
    # The threshold is a point of the curve even off the step grid.
    assert [-20.5, -20.5] in transfer_curve(-20.5, 4)
    # A ratio below 1 (expander) is drawn as 1:1, never steeper than the input.
    assert all(x == y for x, y in transfer_curve(-40, 0.5))


def test_ranges():
    assert compressor_problems({"threshold_db": -36, "ratio": 50, "attack_ms": 15, "release_ms": 30}) == []
    problems = compressor_problems({"threshold_db": 5, "ratio": 0.5, "attack_ms": 0, "release_ms": 9000})
    assert len(problems) == 4 and problems[0].startswith("compressor.threshold_db: 5 outside -90..0")


async def test_get_defaults_with_curve_and_notes(api):
    response = await api.get(URL)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["path"] == "outputs.2.compressor"
    assert data["compressor"] == {
        "bypass": True,
        "threshold_db": 0.0,
        "ratio": 1.0,
        "attack_ms": 10.0,
        "release_ms": 100.0,
    }
    assert set(data["sync_status"]) == {f"outputs.2.compressor.{k}" for k in data["compressor"]}
    assert data["curve"][0] == [-90.0, -90.0] and data["curve"][-1] == [0.0, 0.0]
    assert data["readable"] == ["threshold_db"]
    assert any("approximate" in n for n in data["notes"])
    assert set(data["presets"]) == {"tactile_limiter", "beq_safety"}


async def test_presets_endpoint(api):
    data = (await api.get("/api/v1/compressor/presets")).json()
    assert data["presets"]["tactile_limiter"]["values"] == {
        "threshold_db": -36.0,
        "ratio": 50.0,
        "attack_ms": 15.0,
        "release_ms": 30.0,
    }
    assert data["presets"]["beq_safety"]["values"]["threshold_db"] == 0.0
    assert data["ranges"]["threshold_db"] == [-90.0, 0.0]


async def test_tactile_limiter_preset_writes_parameters_before_enabling(api, session, fake):
    response = await api.put(URL, json={"preset": "tactile_limiter", "bypass": False})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["view"]["compressor"] == {"bypass": False, **COMPRESSOR_PRESETS["tactile_limiter"]["values"]}
    assert "outputs.2.compressor.bypass" in data["device_paths"]
    await settle(session)
    # minidspd applies a compressor's bypass before its parameters, so the un-bypass waits for a later call.
    assert compressor_writes(fake) == [
        {"threshold": -36.0, "ratio": 50.0, "attack": 15.0, "release": 30.0},
        {"bypass": False},
    ]
    status = (await api.get(URL)).json()["sync_status"]
    assert set(status.values()) == {"synced"}


async def test_explicit_fields_override_a_preset_and_only_changes_are_written(api, session, fake):
    await api.put(URL, json={"preset": "beq_safety"})
    await settle(session)
    response = await api.put(URL, json={"preset": "beq_safety", "threshold_db": -3})
    assert response.status_code == 200
    assert response.json()["device_paths"] == ["outputs.2.compressor.threshold_db"]
    await settle(session)
    assert compressor_writes(fake)[-1] == {"threshold": -3.0}
    unchanged = await api.put(URL, json={"threshold_db": -3})
    assert unchanged.json()["changed"] == []


async def test_bypass_only_toggle(api, session, fake):
    response = await api.put(URL, json={"bypass": True})
    assert response.status_code == 200
    assert response.json()["changed"] == []  # already bypassed in the desired state
    await settle(session)
    assert compressor_writes(fake) == []


@pytest.mark.parametrize(
    ("body", "status", "needle"),
    [
        ({"threshold_db": 5}, 422, "threshold_db: 5 outside -90..0"),
        ({"ratio": 0.5}, 422, "ratio: 0.5 outside 1..100"),
        ({"attack_ms": 0}, 422, "attack_ms"),
        ({"release_ms": 6000}, 422, "release_ms"),
        ({}, 422, "nothing to change"),
        ({"knee": 3}, 422, "knee"),
        ({"preset": "loud"}, 422, "preset"),
    ],
)
async def test_refused(api, fake, body, status, needle):
    response = await api.put(URL, json=body)
    assert response.status_code == status
    assert needle in response.text
    assert compressor_writes(fake) == []


async def test_bad_output_and_preset(api):
    assert (await api.get(f"{BASE}/0/outputs/4/compressor")).status_code == 422
    assert (await api.put(f"{BASE}/0/outputs/9/compressor", json={"bypass": False})).status_code == 422
    assert (await api.get(f"{BASE}/7/outputs/0/compressor")).status_code == 404


async def test_inactive_preset_is_staged(api, session, fake):
    response = await api.put(f"{BASE}/1/outputs/2/compressor", json={"preset": "tactile_limiter"})
    assert response.status_code == 200
    result = await settle(session, preset=1, timeout=1.0)
    assert result.staged
    assert compressor_writes(fake) == []


async def test_if_match_and_commit(api, session):
    stale = await api.put(URL, json={"threshold_db": -10}, headers={"If-Match": "99"})
    assert stale.status_code == 412
    ok = await api.put(URL + "?commit=true", json={"threshold_db": -10})
    assert ok.status_code == 200 and ok.json()["revision"] >= 1


async def test_failed_parameters_are_never_enabled(api, session, fake):
    session.writer.max_attempts = 2
    session.writer.backoff_min_s = session.writer.backoff_max_s = 0.01
    fake.fail_when = lambda body: 500 if "threshold" in json.dumps(body) else None
    response = await api.put(URL, json={"preset": "tactile_limiter", "bypass": False})
    assert response.status_code == 200
    await settle(session)
    assert fake.rejected
    sent = [o["compressor"] for body in fake.configs + fake.rejected for o in body["outputs"] if "compressor" in o]
    assert {"bypass": False} not in sent
    status = (await api.get(URL)).json()["sync_status"]
    assert status["outputs.2.compressor.threshold_db"] in ("failed", "unknown")
    assert status["outputs.2.compressor.bypass"] != "synced"


@pytest.mark.parametrize("fields", [{"ratio": 0}, {"ratio": 0.5}, {"attack_ms": 0}, {"release_ms": 0}])
async def test_generic_patch_shares_the_ranges(api, fake, fields):
    patch = {"outputs": {"2": {"compressor": {**fields, "bypass": False}}}}
    response = await api.patch(f"{BASE}/0", json=patch)
    assert response.status_code == 422, response.text
    assert compressor_writes(fake) == []
