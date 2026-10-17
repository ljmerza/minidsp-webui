"""Compressor/FIR routers on a throwaway app, over a real session, temporary database and the stateful fake daemon."""

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import compressor_fir, presets
from app.sync.session import SessionManager

from ..conftest import SERIAL
from ..session.conftest import fake, make_session, repo, session  # noqa: F401


@pytest.fixture
def manager(session, client, bus, settings):  # noqa: F811
    m = SessionManager(client, bus, settings)
    m.sessions[SERIAL] = session
    return m


@pytest.fixture
async def api(manager, settings, repo):  # noqa: F811
    app = FastAPI()
    app.state.manager = manager
    app.state.settings = settings
    app.state.repo = repo
    for module in (presets, compressor_fir):
        app.include_router(module.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
