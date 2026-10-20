"""A respx fake minidspd with state, a temporary database, and a factory for loaded, running sessions."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.model.device_profile import M2X4HD
from app.model.master import MasterStatus
from app.state import Database, Repo
from app.sync.session import COMMIT_COALESCE_S, DeviceSession

from ..conftest import DEVICE_JSON, MASTER_JSON, SERIAL

DAEMON_ERROR_BODY = (
    'the error: \'FormattedError { message: "Specified channel or peq is out of range", error: '
    "InternalError(Specified channel or peq is out of range) }' couldn't be serialized as json"
)


class FakeDaemon:
    """Stateful stand-in for minidspd 0.1.12: master status, preset switches and config writes.

    ``fail`` makes config writes fail: an exception is raised (transport error) or an int is the HTTP status.
    """

    def __init__(self, mock) -> None:
        self.master: dict[str, Any] = {k: v for k, v in MASTER_JSON.items() if k != "dirac"}
        self.configs: list[dict] = []
        self.masters: list[dict] = []
        self.rejected: list[dict] = []
        self.fail: Exception | int | None = None
        self.fail_when = None  # optional predicate(body) -> HTTP status to fail that write with, or None
        mock.get("/devices").mock(side_effect=lambda _: httpx.Response(200, json=[DEVICE_JSON]))
        mock.get("/devices/0").mock(side_effect=self._status)
        mock.post("/devices/0/config").mock(side_effect=self._config)
        mock.post("/devices/0").mock(side_effect=self._post_master)

    def _status(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"master": self.master, "input_levels": [], "output_levels": []})

    def _config(self, request: httpx.Request) -> httpx.Response:
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.fail is not None:
            return httpx.Response(self.fail, text=DAEMON_ERROR_BODY)
        body = json.loads(request.content)
        if self.fail_when is not None and (status := self.fail_when(body)) is not None:
            self.rejected.append(body)
            return httpx.Response(status, text=DAEMON_ERROR_BODY)
        self.configs.append(body)
        return httpx.Response(200, text="")

    def _post_master(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.masters.append(body)
        self.master.update(body)
        return httpx.Response(200, json={**self.master, "dirac": None})

    def outputs_written(self) -> list[dict]:
        return [o for body in self.configs for o in body.get("outputs", [])]


@pytest.fixture
def fake(daemon) -> FakeDaemon:
    return FakeDaemon(daemon)


@pytest.fixture
async def repo(tmp_path):
    db = await Database.open(tmp_path / "state" / "test.sqlite3")
    yield Repo(db)
    await db.close()


@pytest.fixture
async def make_session(client, bus, settings, repo, fake):
    """``await make_session(**settings_overrides)``: a loaded session (by default started) on the fake daemon."""
    created: list[DeviceSession] = []

    async def make(
        *,
        start: bool = True,
        with_repo: bool = True,
        commit_idle_s: float = 0.2,
        commit_coalesce_s: float = COMMIT_COALESCE_S,
        **overrides: Any,
    ):
        s = DeviceSession(
            SERIAL,
            M2X4HD,
            client=client,
            bus=bus,
            settings=settings.model_copy(update={"write_coalesce_ms": 5, **overrides}),
            daemon_index=0,
            repo=repo if with_repo else None,
            commit_idle_s=commit_idle_s,
            commit_coalesce_s=commit_coalesce_s,
        )
        s.monitor.update_master(MasterStatus.model_validate(fake.master))
        await s.ensure_loaded()
        if start:
            s.start()
            await asyncio.sleep(0)  # let the master watcher subscribe
        created.append(s)
        return s

    yield make
    for s in created:
        if not s._stopped:
            await s.stop()


@pytest.fixture
async def session(make_session):
    return await make_session()


async def drain(session: DeviceSession) -> None:
    """Wait for the session's background tasks (event-log inserts, idle commits)."""
    for _ in range(3):
        tasks = [t for t in session._tasks if not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            continue
        await asyncio.gather(*tasks, return_exceptions=True)


async def settle(session: DeviceSession, preset: int = 0, timeout: float = 3.0):
    return await session.writer.wait_idle(preset, timeout=timeout)


async def switch_externally(session: DeviceSession, fake: FakeDaemon, preset: int) -> None:
    """The preset changes outside the app (IR remote, official app): the monitor reports it."""
    fake.master["preset"] = preset
    session.monitor.update_master(MasterStatus(preset=preset))
    await asyncio.sleep(0.01)
