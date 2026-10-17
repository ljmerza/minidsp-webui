"""A real `DeviceSession` (temporary database, respx-mocked minidspd) behind a throwaway app that mounts the profiles
API (plus the presets router, for the history listing) and the profiles page."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx
from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles

from app import web
from app.api.errors import register_exception_handlers
from app.api.v1 import presets
from app.api.v1 import profiles as profiles_api
from app.bus import Bus
from app.config import Settings
from app.daemon.client import DaemonClient
from app.main import STATIC_DIR
from app.model.device_profile import M2X4HD
from app.model.master import MasterStatus
from app.state import Database, Repo
from app.sync.session import DeviceSession, SessionManager
from app.web import profiles as profiles_ui

DAEMON_URL = "http://127.0.0.1:1"  # nothing listens here, so the monitor's websocket fails fast
SERIAL = 944444
DEVICE_JSON = {
    "url": "mock:?serial=944444",
    "product_name": "2x4HD",
    "version": {"hw_id": 10, "dsp_version": 100, "serial": SERIAL, "fw_major": 1, "fw_minor": 75},
}
API = "/api/v1"
HX = {"HX-Request": "true", "Origin": "http://test"}


class FakeDaemon:
    """minidspd 0.1.12 stand-in: master status, preset switches and config writes. ``fail`` = HTTP status for
    config writes."""

    def __init__(self, mock: respx.MockRouter) -> None:
        self.master: dict[str, Any] = {"preset": 0, "source": "Analog", "volume": -20.0, "mute": False}
        self.configs: list[dict] = []
        self.fail: int | None = None
        mock.get("/devices").mock(side_effect=lambda _: httpx.Response(200, json=[DEVICE_JSON]))
        mock.get("/devices/0").mock(side_effect=self._status)
        mock.post("/devices/0/config").mock(side_effect=self._config)
        mock.post("/devices/0").mock(side_effect=self._post_master)

    def _status(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"master": self.master, "input_levels": [], "output_levels": []})

    def _config(self, request: httpx.Request) -> httpx.Response:
        if self.fail is not None:
            return httpx.Response(self.fail, text="the error: 'FormattedError { message: \"Internal error\" }'")
        self.configs.append(json.loads(request.content))
        return httpx.Response(200, text="")

    def _post_master(self, request: httpx.Request) -> httpx.Response:
        self.master.update(json.loads(request.content))
        return httpx.Response(200, json={**self.master, "dirac": None})

    def outputs_written(self) -> list[dict]:
        return [o for body in self.configs for o in body.get("outputs", [])]


@dataclass
class Ctx:
    http: httpx.AsyncClient
    session: DeviceSession
    fake: FakeDaemon
    repo: Repo
    app: FastAPI

    async def settle(self, preset: int = 0, timeout: float = 3.0):
        return await self.session.writer.wait_idle(preset, timeout=timeout)

    async def edit(self, preset: int, patch: dict, **params: Any) -> httpx.Response:
        return await self.http.patch(
            f"{API}/devices/{SERIAL}/presets/{preset}", json=patch, params={"commit": "true", **params}
        )

    async def save(self, name: str, preset: int = 0, **extra: Any) -> dict:
        response = await self.http.post(
            f"{API}/profiles", json={"name": name, "serial": SERIAL, "preset": preset, **extra}
        )
        assert response.status_code == 201, response.text
        return response.json()


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        minidspd_url=DAEMON_URL,
        preset_switch_timeout_s=2.0,
        monitor_backoff_min_s=0.05,
        monitor_backoff_max_s=0.2,
        levels_idle_grace_s=0.05,
        write_coalesce_ms=5,
        **overrides,
    )


def make_app(manager: SessionManager, settings: Settings, *, repo: Repo | None, bus: Bus) -> FastAPI:
    app = FastAPI()
    app.state.manager = manager
    app.state.settings = settings
    app.state.repo = repo
    app.state.bus = bus
    register_exception_handlers(app)
    api = APIRouter(prefix=API)
    api.include_router(presets.router)
    api.include_router(profiles_api.router)
    app.include_router(api)
    app.include_router(web.router)
    app.include_router(profiles_ui.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def ctx(tmp_path, settings):
    bus = Bus()
    db = await Database.open(tmp_path / "state" / "profiles.sqlite3")
    repo = Repo(db)
    client = DaemonClient(DAEMON_URL, timeout=1.0, preset_timeout=2.0)
    with respx.mock(base_url=DAEMON_URL, assert_all_called=False) as mock:
        fake = FakeDaemon(mock)
        session = DeviceSession(
            SERIAL, M2X4HD, client=client, bus=bus, settings=settings, daemon_index=0, repo=repo, commit_idle_s=0.2
        )
        session.monitor.update_master(MasterStatus.model_validate(fake.master))
        await session.ensure_loaded()
        session.start()
        await asyncio.sleep(0)
        manager = SessionManager(client, bus, settings, repo=repo)
        manager.sessions[SERIAL] = session
        manager.daemon_up = True
        app = make_app(manager, settings, repo=repo, bus=bus)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                yield Ctx(http, session, fake, repo, app)
        finally:
            await session.stop()
            await client.aclose()
            await db.close()
