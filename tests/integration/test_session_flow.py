"""DeviceSession end to end against the mock minidspd: adopt, edits, staging, activation, restart, readback.

Only output 3 (the fourth, unused output) is written, and it is put back afterwards.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.bus import Bus
from app.config import Settings
from app.daemon.client import DaemonClient
from app.model.device_profile import profile_for
from app.state import Database, Repo
from app.sync.session import DeviceSession

from .conftest import MOCK_SERIAL, MockMemory

pytestmark = pytest.mark.integration

PI_NOTES = (Path(__file__).parents[1] / "fixtures" / "pi_notes_commands.txt").read_text()
OUT3_MUTE, OUT3_GAIN, OUT3_INVERT = 5, 31, 83  # mock DSP memory words of output 3 (see test_daemon_contract)
MUTED, UNMUTED = 1, 2


class Flow:
    def __init__(self, daemon_url: str, db_path: Path) -> None:
        self.daemon_url = daemon_url
        self.db_path = db_path
        self.settings = Settings(
            _env_file=None, minidspd_url=daemon_url, write_coalesce_ms=20, preset_switch_timeout_s=5.0
        )
        self.client = DaemonClient(daemon_url, timeout=10, preset_timeout=15)
        self.db: Database | None = None
        self.sessions: list[DeviceSession] = []

    async def session(self) -> DeviceSession:
        if self.db is None:
            self.db = await Database.open(self.db_path)
        index, device = await self.client.resolve_index(MOCK_SERIAL)
        profile = profile_for(device.product_name)
        assert profile is not None
        session = DeviceSession(
            MOCK_SERIAL,
            profile,
            client=self.client,
            bus=Bus(),
            settings=self.settings,
            device=device,
            daemon_index=index,
            repo=Repo(self.db),
            commit_idle_s=0.2,
        )
        await session.refresh_status()
        await session.ensure_loaded()
        session.start()
        self.sessions.append(session)
        return session

    async def close(self) -> None:
        for session in self.sessions:
            await session.stop()
        if self.db is not None:
            await self.db.close()
        await self.client.aclose()


@pytest.fixture
async def flow(daemon_url: str, tmp_path: Path, restore_master) -> AsyncIterator[Flow]:
    f = Flow(daemon_url, tmp_path / "flow.sqlite3")
    yield f
    await f.close()


@pytest.fixture
async def restore_out3(raw: httpx.AsyncClient, mock_memory: MockMemory) -> AsyncIterator[None]:
    mute = await mock_memory.int_word(OUT3_MUTE)
    gain = await mock_memory.word(OUT3_GAIN)
    yield
    body = {"outputs": [{"index": 3, "mute": mute == MUTED, "gain": gain, "invert": False}]}
    assert (await raw.post("/devices/0/config", json=body)).status_code == 200


async def test_adopt_edit_stage_activate_restart(flow: Flow):
    session = await flow.session()
    active = await session.active_preset()

    result = await session.adopt("trust", script=PI_NOTES)
    assert result.writes == 0 and session.writer.writes_sent == 0
    assert session.sync_status(active)["outputs.0.gain_db"] == "synced"

    edit = await session.edit(active, {"outputs": {"3": {"gain_db": -3.5}}}, commit=True)
    assert edit.revision == 2
    assert (await session.writer.wait_idle(active, timeout=10)).ok
    assert session.writer.writes_sent == 1
    assert session.sync_status(active)["outputs.3.gain_db"] == "synced"

    other = (active + 1) % session.profile.n_presets
    await session.edit(other, {"outputs": {"3": {"gain_db": -2.0}}})
    assert (await session.writer.wait_idle(other, timeout=10)).staged
    assert session.writer.writes_sent == 1
    await session.activate_preset(other)
    assert session.sync_summary(other)["counts"]["pending"] == 0
    assert session.writer.writes_sent == 2
    await session.edit(other, {"outputs": {"3": {"gain_db": 0.0}}}, commit=True)
    await session.activate_preset(active)
    await session.edit(active, {"outputs": {"3": {"gain_db": 0.0}}}, commit=True)
    assert (await session.writer.wait_idle(active, timeout=10)).ok

    history = await session.history(active)
    assert [h.source for h in history] == ["api", "api", "adopt"]

    await session.stop()
    again = await flow.session()
    assert again.desired(other).outputs[3].gain_db == 0.0
    assert again.sync_status(active)["outputs.0.gain_db"] == "synced"
    assert not again.needs_adoption
    assert (await again.writer.wait_idle(active, timeout=2)).ok
    assert again.writer.writes_sent == 0  # a restart trusts the stored shadow and writes nothing


async def test_writes_land_in_device_memory(flow: Flow, mock_memory: MockMemory, restore_out3):
    session = await flow.session()
    active = await session.active_preset()
    await session.adopt("unknown")

    await session.edit_channel(active, "output", 3, {"gain_db": -4.5, "invert": True})
    assert (await session.writer.wait_idle(active, timeout=10)).ok
    assert await mock_memory.word(OUT3_GAIN) == -4.5

    imported = await session.import_cli_script(active, "output 3 gain -- -6\noutput 3 invert off\n")
    assert imported.edit.flush is not None and imported.edit.flush.ok  # mute-wrapped on output 3 only
    assert await mock_memory.int_word(OUT3_MUTE) == UNMUTED  # restored after the wrap
    assert await mock_memory.word(OUT3_GAIN) == -6.0

    await session.edit_channel(active, "output", 3, {"mute": True, "gain_db": 0.0, "invert": False})
    assert (await session.writer.wait_idle(active, timeout=10)).ok
    assert await mock_memory.int_word(OUT3_MUTE) == MUTED
    assert await mock_memory.word(OUT3_GAIN) == 0.0
