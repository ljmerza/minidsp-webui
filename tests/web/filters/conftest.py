"""The params fixtures (a real `DeviceSession` with a temporary database and a respx-mocked minidspd), with the
filter UI router mounted next to the web and Phase 2 routers."""

import asyncio

import httpx
import pytest
import respx

from app.bus import Bus
from app.daemon.client import DaemonClient
from app.model.device_profile import M2X4HD
from app.model.master import MasterStatus
from app.state import Database, Repo
from app.sync.session import DeviceSession, SessionManager
from app.web import filters as filters_ui
from tests.web.params.conftest import DAEMON_URL, HX, SERIAL, Ctx, FakeDaemon, make_app, make_settings

__all__ = ["HX", "SERIAL", "UI", "post"]

UI = f"/ui/devices/{SERIAL}"
SAME_ORIGIN = {**HX, "Origin": "http://test"}


async def post(ctx: Ctx, path: str, body: dict) -> httpx.Response:
    return await ctx.http.post(path, json=body, headers=SAME_ORIGIN)


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
async def ctx(tmp_path, settings):
    bus = Bus()
    db = await Database.open(tmp_path / "state" / "filters.sqlite3")
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
        app.include_router(filters_ui.router)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                yield Ctx(http, session, fake, repo, settings, app, bus)
        finally:
            await session.stop()
            await client.aclose()
            await db.close()
