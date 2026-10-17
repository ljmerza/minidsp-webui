"""A real `DeviceSession` (temporary database, respx-mocked minidspd) behind a throwaway app that mounts the web
router, the Phase 2 UI router, the System page router and the System JSON API router."""

import asyncio

import httpx
import pytest
import respx
from fastapi import APIRouter

from app.api.v1 import system as system_api
from app.bus import Bus
from app.daemon.client import DaemonClient
from app.model.device_profile import M2X4HD
from app.model.master import MasterStatus
from app.state import Database, Repo
from app.sync.session import DeviceSession, SessionManager
from app.web import system as system_ui
from tests.web.params.conftest import DAEMON_URL, SERIAL, Ctx, FakeDaemon, make_app, make_settings

__all__ = ["SERIAL", "API", "UI"]

API = f"/api/v1/devices/{SERIAL}/presets"
UI = f"/ui/devices/{SERIAL}/presets"


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
async def ctx(tmp_path, settings):
    bus = Bus()
    db = await Database.open(tmp_path / "state" / "system.sqlite3")
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
        app.include_router(system_ui.router)
        api = APIRouter(prefix="/api/v1")
        api.include_router(system_api.router)
        app.include_router(api)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                yield Ctx(http, session, fake, repo, settings, app, bus)
        finally:
            await session.stop()
            await client.aclose()
            await db.close()
