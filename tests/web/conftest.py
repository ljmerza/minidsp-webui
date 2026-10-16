"""Fakes for the session layer, and a throwaway app that mounts only the web router and the static files."""

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import web
from app.config import Settings
from app.main import STATIC_DIR
from app.model.device_profile import M2X4HD, DeviceProfile
from app.model.master import DaemonDevice, MasterStatus, StatusSummary
from app.sync.session import SessionState

SERIAL = 911111
HX = {"HX-Request": "true"}


class FakeMonitor:
    connected = True

    def status_age(self) -> float | None:
        return 0.5


@dataclass
class FakeSession:
    serial: int = SERIAL
    profile: DeviceProfile = M2X4HD
    state: SessionState = SessionState.CONNECTED
    master: MasterStatus | None = field(
        default_factory=lambda: MasterStatus(preset=1, source="Analog", volume=-20.0, mute=False, dirac=False)
    )
    status: StatusSummary | None = None
    preset_switching: int | None = None
    monitor_connected: bool = True
    fail_with: Exception | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)
    events: list[tuple[str, str]] = field(default_factory=list)
    confirmed: bool | None = None  # ``confirm`` of the last set_volume call

    def __post_init__(self) -> None:
        self.monitor = FakeMonitor()

    def device_event(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "state": str(self.state),
            "monitor_connected": self.monitor_connected,
            "preset_switching": self.preset_switching,
        }

    def info(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "product_name": self.profile.product_name,
            "url": "mock:?serial=911111",
            "daemon_index": 0,
            "hw_id": 10,
            "dsp_version": 100,
            "fw": "1.75",
            "state": str(self.state),
            "monitor_connected": self.monitor_connected,
            "last_error": None,
            "profile": self.profile.model_dump(),
        }

    def log_event(self, level: str, message: str, **data: Any) -> None:
        self.events.append((level, message))

    async def _record(self, name: str, arg: Any, **changes: Any) -> MasterStatus:
        self.calls.append((name, arg))
        if self.fail_with is not None:
            raise self.fail_with
        self.master = (self.master or MasterStatus()).model_copy(update=changes)
        return self.master

    async def set_volume(self, volume_db: float, *, confirm: bool = False) -> MasterStatus:
        self.confirmed = confirm
        return await self._record("set_volume", volume_db, volume=volume_db)

    async def volume_step(self, direction: int) -> MasterStatus:
        current = self.master.volume if self.master and self.master.volume is not None else 0.0
        return await self._record("volume_step", direction, volume=current + 0.5 * direction)

    async def set_mute(self, mute: bool) -> MasterStatus:
        return await self._record("set_mute", mute, mute=mute)

    async def toggle_mute(self) -> MasterStatus:
        muted = bool(self.master and self.master.mute)
        return await self._record("toggle_mute", None, mute=not muted)

    async def set_source(self, source: str) -> MasterStatus:
        return await self._record("set_source", source, source=source)

    async def set_preset(self, preset: int) -> MasterStatus:
        return await self._record("set_preset", preset, preset=preset)


@dataclass
class FakeManager:
    sessions: dict[int, FakeSession] = field(default_factory=dict)
    daemon_up: bool | None = True
    last_error: str | None = None
    unsupported: list[DaemonDevice] = field(default_factory=list)

    def get(self, serial: int) -> FakeSession | None:
        return self.sessions.get(serial)

    def default(self) -> FakeSession | None:
        return next(iter(self.sessions.values()), None)

    def __iter__(self):
        return iter(list(self.sessions.values()))


def make_app(manager: FakeManager, settings: Settings | None = None) -> FastAPI:
    app = FastAPI()
    app.state.manager = manager
    app.state.settings = settings or Settings(_env_file=None, minidspd_url="http://daemon.invalid:5380")
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def manager(session: FakeSession) -> FakeManager:
    return FakeManager(sessions={session.serial: session})


@pytest.fixture
async def http(manager: FakeManager):
    transport = httpx.ASGITransport(app=make_app(manager))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
