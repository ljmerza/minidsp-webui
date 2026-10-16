"""Smoke test of the webui container in docker-compose.dev.yml (skipped when it isn't running)."""

import os
import time

import httpx
import pytest

from .conftest import MOCK_SERIAL

pytestmark = pytest.mark.integration

WEBUI_URL = os.environ.get("WEBUI_URL", "http://127.0.0.1:8480").rstrip("/")


@pytest.fixture(scope="module")
def webui() -> str:
    try:
        response = httpx.get(f"{WEBUI_URL}/healthz", timeout=3)
    except httpx.HTTPError:
        pytest.skip(f"dev webui not reachable at {WEBUI_URL}")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    return WEBUI_URL


def test_dev_webui_sees_mock_device(webui: str):
    for _ in range(50):
        devices = httpx.get(f"{webui}/api/v1/devices", timeout=3).json()
        if devices and devices[0].get("state") == "connected":
            break
        time.sleep(0.2)
    assert [(d["serial"], d["product_name"]) for d in devices] == [(MOCK_SERIAL, "2x4HD")]
    assert devices[0]["state"] == "connected"
    health = httpx.get(f"{webui}/api/v1/health", timeout=3).json()
    assert (health["daemon"], health["device"]) == ("up", "connected")
