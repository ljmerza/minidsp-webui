"""The whole app (lifespan, session manager, monitor, JSON API, SSE) against the mock minidspd."""

import asyncio
import json
import time

import httpx
import pytest

from .conftest import MOCK_SERIAL

pytestmark = pytest.mark.integration

DEVICE = f"/api/v1/devices/{MOCK_SERIAL}"


async def _external_volume(api: httpx.AsyncClient, raw: httpx.AsyncClient, volume: float) -> bool:
    """Set the volume behind the app's back; True once the app's cached status shows it (poll=true, ~2 s)."""
    await raw.post("/devices/0", json={"volume": volume})
    for _ in range(40):
        master = (await api.get(f"{DEVICE}/status")).json()["master"]
        if master and master["volume"] == volume:
            return True
        await asyncio.sleep(0.1)
    return False


async def _sse_levels(api: httpx.AsyncClient, first_timeout: float, count: int = 3) -> tuple[float, list[dict]]:
    """Open the SSE feed for levels; return seconds until the first levels event and ``count`` payloads."""
    started = time.monotonic()
    elapsed = None
    events: list[dict] = []
    async with asyncio.timeout(first_timeout + 3):
        async with api.stream("GET", "/api/v1/events", params={"topics": "levels", "serial": MOCK_SERIAL}) as stream:
            event = None
            async for line in stream.aiter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event == "levels":
                    if elapsed is None:
                        elapsed = time.monotonic() - started
                    events.append(json.loads(line.split(":", 1)[1]))
                    if len(events) == count:
                        break
    assert elapsed is not None
    return elapsed, events


async def test_app_discovers_mock(api: httpx.AsyncClient):
    devices = (await api.get("/api/v1/devices")).json()
    assert len(devices) == 1
    device = devices[0]
    assert device["serial"] == MOCK_SERIAL
    assert device["product_name"] == "2x4HD"
    assert device["supported"] is True
    assert device["daemon_index"] == 0
    assert (await api.get("/api/v1/health")).status_code == 200


async def test_app_status(api: httpx.AsyncClient):
    status = (await api.get(f"{DEVICE}/status")).json()
    assert status["state"] == "connected"
    assert status["master"]["source"] in ("Analog", "Toslink", "Usb")


@pytest.mark.usefixtures("restore_master")
async def test_app_master_volume_mute_source(api: httpx.AsyncClient, raw: httpx.AsyncClient):
    # confirm: the mock's volume before this test is arbitrary, so this may be a raise of more than 6 dB.
    response = await api.put(f"{DEVICE}/master", json={"volume": -33.5, "mute": True, "confirm": True})
    assert response.status_code == 200, response.text
    assert (response.json()["volume"], response.json()["mute"]) == (-33.5, True)

    response = await api.put(f"{DEVICE}/master", json={"source": "toslink"})
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "Toslink"

    device = (await raw.get("/devices/0")).json()["master"]
    assert (device["volume"], device["mute"], device["source"]) == (-33.5, True, "Toslink")


@pytest.mark.usefixtures("restore_master")
async def test_app_big_volume_raise_needs_confirm(api: httpx.AsyncClient, raw: httpx.AsyncClient):
    assert await _external_volume(api, raw, -60.0)
    response = await api.put(f"{DEVICE}/master", json={"volume": -20.0})
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "confirmation_required"
    assert (await raw.get("/devices/0")).json()["master"]["volume"] == -60.0
    response = await api.put(f"{DEVICE}/master", json={"volume": -20.0, "confirm": True})
    assert response.status_code == 200, response.text
    assert response.json()["volume"] == -20.0


async def test_app_rejects_out_of_range_master(api: httpx.AsyncClient):
    assert (await api.put(f"{DEVICE}/master", json={"volume": 5.0})).status_code == 422
    assert (await api.put(f"{DEVICE}/master", json={"preset": 7})).status_code == 422
    assert (await api.put(f"{DEVICE}/master", json={"source": "Hdmi"})).status_code == 422


@pytest.mark.usefixtures("restore_master")
async def test_app_volume_step_clamps_at_zero(api: httpx.AsyncClient, raw: httpx.AsyncClient):
    # At the top of the range the session sends an absolute volume, which 0.1.12 supports.
    assert await _external_volume(api, raw, 0.0)
    response = await api.post(f"{DEVICE}/master/volume/up")
    assert response.status_code == 200, response.text
    assert response.json()["volume"] == 0.0


@pytest.mark.usefixtures("restore_master")
async def test_app_sees_external_volume_change(api: httpx.AsyncClient, raw: httpx.AsyncClient):
    """Phase 1 acceptance: a change made outside the app (e.g. `minidsp gain`) shows up within ~2 s via poll=true."""
    assert await _external_volume(api, raw, -55.0)


@pytest.mark.usefixtures("restore_master")
async def test_app_volume_step(api: httpx.AsyncClient, raw: httpx.AsyncClient):
    assert await _external_volume(api, raw, -20.0)
    response = await api.post(f"{DEVICE}/master/volume/down")
    assert response.status_code == 200, response.text
    assert response.json()["volume"] == -20.5


@pytest.mark.usefixtures("restore_master")
async def test_app_toggle_mute(api: httpx.AsyncClient):
    response = await api.post(f"{DEVICE}/master/mute/toggle")
    assert response.status_code == 200, response.text


@pytest.mark.usefixtures("restore_master")
async def test_app_preset_switch(api: httpx.AsyncClient):
    response = await api.put(f"{DEVICE}/master", json={"preset": 1})
    assert response.status_code == 200, response.text
    assert response.json()["preset"] == 1


async def test_app_streams_levels_over_sse(api: httpx.AsyncClient):
    _, events = await _sse_levels(api, first_timeout=15)
    assert all(len(e["i"]) == 2 and len(e["o"]) == 4 for e in events)


async def test_app_levels_start_promptly(api: httpx.AsyncClient):
    # Start from a poll-only monitor (cached levels are cleared then). Leaving the levels socket also takes ~10 s today.
    for _ in range(300):
        if (await api.get(f"{DEVICE}/status")).json()["input_levels"] == []:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("monitor never went back to a poll-only socket")
    elapsed, _ = await _sse_levels(api, first_timeout=3, count=1)
    assert elapsed < 3
