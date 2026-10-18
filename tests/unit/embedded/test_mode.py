"""MINIDSPD_MODE: settings resolution, app wiring in both modes, /api/v1/health, /healthz and the Device page."""

import os
import signal
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.config import Settings
from app.daemon.client import DaemonClient
from app.daemon.embedded import UsbProbe, UsbStatus, usb_identity
from app.main import create_app

from .conftest import pid_alive, starts, wait_for

EXTERNAL_URL = "http://127.0.0.1:1"
DEVICE_JSON = {
    "url": "mock:?serial=911111",
    "product_name": "2x4HD",
    "version": {"hw_id": 10, "dsp_version": 100, "serial": 911111, "fw_major": 1, "fw_minor": 75},
}
MASTER_JSON = {"preset": 0, "source": "Analog", "volume": -20.0, "mute": False, "dirac": False}


# --- settings ---------------------------------------------------------------------------------------------------------


def test_default_is_embedded_on_loopback():
    s = Settings(_env_file=None)
    assert s.minidspd_mode == "embedded" and s.embedded_daemon
    assert s.minidspd_url == "http://127.0.0.1:5380"
    assert (s.minidspd_mock, s.minidspd_discovery, s.minidspd_extra_config) == (False, False, "")
    assert s.minidspd_http_bind == "127.0.0.1:5380"
    assert s.minidspd_config_dir == "/run/minidsp"


def test_a_minidspd_url_alone_keeps_external_mode():
    s = Settings(_env_file=None, minidspd_url="http://daemon.example:5380")
    assert s.minidspd_mode == "external" and not s.embedded_daemon
    assert s.minidspd_url == "http://daemon.example:5380"


def test_minidspd_url_from_env_keeps_external_mode(monkeypatch):
    monkeypatch.setenv("MINIDSPD_URL", "http://127.0.0.1:5380")
    assert Settings(_env_file=None).minidspd_mode == "external"


def test_explicit_embedded_mode_ignores_minidspd_url(monkeypatch):
    monkeypatch.setenv("MINIDSPD_MODE", "embedded")
    monkeypatch.setenv("MINIDSPD_URL", "http://elsewhere:5380")
    monkeypatch.setenv("MINIDSPD_HTTP_BIND", "0.0.0.0:6380")
    monkeypatch.setenv("MINIDSPD_MOCK", "true")
    s = Settings(_env_file=None)
    assert s.minidspd_mode == "embedded" and s.minidspd_mock
    assert s.minidspd_url == "http://127.0.0.1:6380"


def test_explicit_external_mode(monkeypatch):
    monkeypatch.setenv("MINIDSPD_MODE", "external")
    s = Settings(_env_file=None)
    assert s.minidspd_mode == "external" and s.minidspd_url == "http://127.0.0.1:5380"


@pytest.mark.parametrize("overrides", [{"minidspd_mode": "host"}, {"minidspd_http_bind": "5380"}])
def test_bad_daemon_settings_are_refused(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_a_bad_bind_is_fine_in_external_mode():
    s = Settings(_env_file=None, minidspd_mode="external", minidspd_http_bind="nonsense")
    assert s.minidspd_url == "http://127.0.0.1:5380"


# --- app wiring -------------------------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def running(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


@pytest.fixture
def embedded_settings(tmp_path, fake_daemon, record, monkeypatch):
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_MODE", "run")
    return Settings(
        _env_file=None,
        minidspd_mode="embedded",
        minidspd_mock=True,
        minidspd_binary=str(fake_daemon),
        minidspd_config_dir=str(tmp_path / "run"),
        minidspd_http_bind=f"127.0.0.1:{_free_port()}",  # nothing answers there: the fake daemon serves no HTTP
        db_path=str(tmp_path / "state.sqlite3"),
        daemon_timeout_s=0.5,
    )


async def test_external_mode_starts_no_daemon(tmp_path):
    settings = Settings(_env_file=None, minidspd_url=EXTERNAL_URL, db_path=str(tmp_path / "state.sqlite3"))
    with respx.mock(base_url=EXTERNAL_URL, assert_all_called=False) as daemon:
        daemon.get("/devices").respond(json=[DEVICE_JSON])
        daemon.get("/devices/0").respond(json={"master": MASTER_JSON, "input_levels": [], "output_levels": []})
        client = DaemonClient(EXTERNAL_URL, timeout=1.0, preset_timeout=2.0)
        app = create_app(settings, client=client)
        async with running(app) as http:
            assert app.state.daemon_supervisor is None and app.state.usb_probe is None
            assert app.state.manager.usb_identity is None  # an external daemon: a reconnect can't be told apart
            await wait_for(lambda: app.state.manager.get(911111) is not None)
            body = (await http.get("/api/v1/health")).json()
        await client.aclose()
    assert body["daemon_mode"] == "external" and body["daemon_process"] == "external"
    assert body["daemon_restarts"] == 0 and body["usb_device_visible"] is None
    assert body["daemon"] == "up"


async def test_embedded_mode_supervises_the_daemon(embedded_settings, record):
    app = create_app(embedded_settings)
    async with running(app) as http:
        sup = app.state.daemon_supervisor
        app.state.usb_probe = UsbProbe(probe=lambda: UsbStatus(False, "not_mounted", "/dev/bus/usb isn't mounted"))
        assert app.state.manager.usb_identity is usb_identity
        await wait_for(lambda: len(starts(record)) == 1)
        pid = sup.pid
        assert app.state.client.base_url == embedded_settings.minidspd_url

        body = (await http.get("/api/v1/health")).json()
        assert body["daemon_mode"] == "embedded" and body["daemon_mock"] is True
        assert body["daemon_process"] == "running" and body["daemon_restarts"] == 0
        assert body["daemon_pid"] == pid
        assert body["usb_device_visible"] is False and body["usb_access"] == "not_mounted"
        assert not any(p.startswith("USB:") for p in body["problems"])  # mock mode needs no USB device

        os.kill(pid, 9)
        await wait_for(lambda: sup.restarts == 1 and sup.state == "running")
        body = (await http.get("/api/v1/health")).json()
        assert body["daemon_restarts"] == 1 and body["daemon_last_exit"] == "signal SIGKILL"

        page = await http.get("/device")
        assert page.status_code == 200 and "minidspd process" in page.text
        assert 'x-if="h.daemon_process_error"' in page.text  # shown while running too (listener errors)
        pid = sup.pid
    assert sup.state == "stopped"
    assert not pid_alive(pid)


async def test_embedded_failure_degrades_health(embedded_settings, tmp_path):
    embedded_settings.minidspd_binary = str(tmp_path / "missing-minidspd")
    embedded_settings.minidspd_mock = False
    app = create_app(embedded_settings)
    async with running(app) as http:
        app.state.usb_probe = UsbProbe(probe=lambda: UsbStatus(True, "denied", "/dev/bus/usb/001/004 ... (EACCES)"))
        body = (await http.get("/api/v1/health")).json()
        strict = await http.get("/api/v1/health?strict=true")
        healthz = (await http.get("/healthz")).json()
    assert body["daemon_process"] == "failed"
    assert body["status"] == "degraded"
    assert body["problems"][0].startswith("embedded minidspd is failing: cannot run ")
    assert "USB: /dev/bus/usb/001/004 ... (EACCES)" in body["problems"]
    assert strict.status_code == 503
    assert healthz == {"status": "ok", "version": healthz["version"], "health": "degraded"}


async def test_startup_failure_still_stops_the_daemon(embedded_settings, record, monkeypatch):
    async def boom(*args, **kwargs):
        await wait_for(lambda: starts(record))  # the daemon is up when startup fails
        raise RuntimeError("startup failed")

    monkeypatch.setattr("app.main._open_db", boom)
    app = create_app(embedded_settings)
    with pytest.raises(RuntimeError, match="startup failed"):
        async with app.router.lifespan_context(app):
            pass
    sup = app.state.daemon_supervisor
    assert sup.state == "stopped" and sup.last_exit == -signal.SIGTERM
    (start,) = starts(record)
    assert not pid_alive(start["pid"])
