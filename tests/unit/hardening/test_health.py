"""docs/03 §10: the degraded health report and the storage banner text."""

import asyncio
from types import SimpleNamespace

import httpx

from app.bus import Bus
from app.daemon.client import DaemonClient
from app.model.master import DaemonDevice
from app.sync import health
from app.sync.session import SessionManager, SessionState

from ..conftest import DAEMON_URL, SERIAL


class Manager(SimpleNamespace):
    def __iter__(self):
        return iter(self.sessions)


def make_manager(sessions=(), **kwargs) -> Manager:
    defaults = {"daemon_up": True, "last_error": None, "repo": object(), "unsupported": []}
    return Manager(sessions=list(sessions), **{**defaults, **kwargs})


async def connected(session) -> None:
    """Pretend the live stream is up (the test daemon has no websocket, so stop the real monitor first)."""
    await session.monitor.stop()
    session.set_state(SessionState.CONNECTED)
    session.monitor.connected = True


async def test_healthy(session):
    await connected(session)
    report = health.health_report(make_manager([session]))
    assert report["status"] == "ok" and report["problems"] == []
    assert report["daemon"] == "up" and report["device"] == "connected" and report["storage"] == "ok"
    device = report["devices"][0]
    assert device["serial"] == SERIAL and device["state_loaded"] is True
    assert device["sync"]["preset"] == 0 and device["sync"]["failed"] == 0


async def test_starting_before_the_first_daemon_check():
    report = health.health_report(make_manager(daemon_up=None))
    assert report["status"] == "starting" and report["daemon"] == "unknown"


async def test_daemon_down_is_one_problem(session):
    session.set_state(SessionState.DAEMON_DOWN)
    report = health.health_report(make_manager([session], daemon_up=False, last_error="connection refused"))
    assert report["status"] == "degraded"
    assert report["problems"] == ["minidspd unreachable: connection refused"]
    assert report["devices"][0]["state"] == "daemon_down"


async def test_no_supported_device():
    device = DaemonDevice(url="usb:1", product_name="SHD")
    report = health.health_report(make_manager(unsupported=[device]))
    assert report["status"] == "degraded"
    assert report["problems"] == ["no supported device on minidspd (unsupported: SHD)"]


async def test_device_absent_and_stream_reconnecting(session):
    session.set_state(SessionState.ABSENT)
    report = health.health_report(make_manager([session]))
    assert report["problems"] == [f"device {SERIAL}: not found on minidspd (unplugged?)"]
    await connected(session)
    session.monitor.connected = False
    report = health.health_report(make_manager([session]))
    assert report["problems"] == [f"device {SERIAL}: live status stream is reconnecting"]


async def test_memory_state_is_degraded():
    report = health.health_report(make_manager(repo=None))
    assert report["status"] == "degraded" and report["state"] == "memory" and report["storage"] == "memory"
    assert health.STORAGE_MEMORY in report["problems"]
    assert health.storage_problem(make_manager(repo=None)) == health.STORAGE_MEMORY


async def test_failing_persist_is_degraded(session, repo, monkeypatch):
    await connected(session)

    async def disk_full(*args, **kwargs):
        raise OSError("database or disk is full")

    monkeypatch.setattr(repo, "save_preset_state", disk_full)
    await session.edit(1, {"outputs": {"0": {"gain_db": -3.0}}})  # inactive preset: staged, persisted
    assert await session._persister.flush() is False
    assert health.persist_failing(session)
    report = health.health_report(make_manager([session], repo=repo))
    assert report["status"] == "degraded" and report["storage"] == "failing"
    assert health.STORAGE_FAILING in report["problems"]
    assert report["devices"][0]["persist_failing"] is True
    assert session.desired(1).outputs[0].gain_db == -3.0  # the in-memory state carries on

    monkeypatch.undo()
    assert await session._persister.flush() is True
    assert health.health_report(make_manager([session], repo=repo))["status"] == "ok"


async def test_persist_still_failing_while_a_retry_is_in_flight(session, repo, monkeypatch):
    await connected(session)
    retry_started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def locked(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:  # the retry waits on the lock (busy_timeout) before failing again
            retry_started.set()
            await release.wait()
        raise OSError("database is locked")

    monkeypatch.setattr(repo, "save_preset_state", locked)
    await session.edit(1, {"outputs": {"0": {"gain_db": -3.0}}})
    assert await session._persister.flush() is False
    retry = asyncio.create_task(session._persister.flush())
    await asyncio.wait_for(retry_started.wait(), 1)
    assert not session._persister.pending_keys  # the value is out for writing
    assert health.persist_failing(session)
    assert health.storage_problem(make_manager([session], repo=repo)) == health.STORAGE_FAILING
    release.set()
    assert await retry is False


async def test_writes_given_up_are_reported(session, fake):
    await connected(session)
    session.writer.backoff_min_s = session.writer.backoff_max_s = 0.01
    fake.fail_when = lambda body: 500
    await session.edit(0, {"outputs": {"2": {"gain_db": -6.0}}})
    for _ in range(200):
        if session.sync_summary(0)["counts"]["failed"]:
            break
        await asyncio.sleep(0.01)
    report = health.health_report(make_manager([session]))
    assert report["status"] == "degraded"
    assert report["devices"][0]["sync"]["failed"] == 1
    assert report["problems"] == [
        f"device {SERIAL}: 1 setting(s) of preset 1 could not be written (Specified channel or peq is out of range)"
    ]


async def test_report_with_the_real_manager_when_the_daemon_is_down(settings, daemon):
    daemon.get("/devices").mock(side_effect=httpx.ConnectError("refused"))
    client = DaemonClient(DAEMON_URL, timeout=0.5)
    try:
        manager = SessionManager(client, Bus(), settings)
        await manager.discover()
        report = health.health_report(manager)
    finally:
        await client.aclose()
    assert report["status"] == "degraded" and report["daemon"] == "down"
    assert report["problems"][0].startswith("minidspd unreachable")
    assert report["storage"] == "memory"  # no repo given
