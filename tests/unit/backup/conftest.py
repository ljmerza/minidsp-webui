"""A database at ``settings.db_path``, the stateful fake minidspd from the session tests, and session/app factories."""

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import backup as backup_api
from app.model.device_profile import M2X4HD
from app.model.master import MasterStatus
from app.state import Database, Repo
from app.sync.session import DeviceSession, SessionManager
from app.web import backup as backup_web

from ..conftest import SERIAL
from ..session.conftest import FakeDaemon


@pytest.fixture
def fake(daemon) -> FakeDaemon:
    return FakeDaemon(daemon)


@pytest.fixture
async def repo(settings):
    db = await Database.open(settings.db_path)
    yield Repo(db)
    await db.close()


@pytest.fixture
async def make_session(client, bus, settings, repo, fake):
    created: list[DeviceSession] = []

    async def make(*, start: bool = True, **overrides: Any) -> DeviceSession:
        s = DeviceSession(
            SERIAL,
            M2X4HD,
            client=client,
            bus=bus,
            settings=settings.model_copy(update={"write_coalesce_ms": 5, **overrides}),
            daemon_index=0,
            repo=repo,
            commit_idle_s=0.2,
        )
        s.monitor.update_master(MasterStatus.model_validate(fake.master))
        await s.ensure_loaded()
        if start:
            s.start()
            await asyncio.sleep(0)
        created.append(s)
        return s

    yield make
    for s in created:
        if not s._stopped:
            await s.stop()


@pytest.fixture
def manager(client, bus, settings, repo) -> SessionManager:
    return SessionManager(client, bus, settings, repo=repo)


def make_app(settings, repo, manager) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.repo = repo
    app.state.manager = manager
    app.include_router(backup_api.router, prefix="/api/v1")
    app.include_router(backup_web.router)
    return app


@pytest.fixture
async def http(settings, repo, manager):
    app = make_app(settings, repo, manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.app = app  # type: ignore[attr-defined]
        yield client
