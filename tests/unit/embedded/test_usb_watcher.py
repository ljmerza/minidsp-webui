"""Re-plug / power cycle recovery: minidspd's libusb never gets a hotplug event inside a container, so it can't see a
miniDSP that re-enumerated under a new device number. `UsbWatcher` notices and restarts the daemon (`restart`)."""

import time
from pathlib import Path

from app.daemon.embedded import RUNNING, UsbWatcher, usb_identity

from .conftest import starts, wait_for


def _plug(sys_root: Path, dev: int, name: str = "1-1.3") -> None:
    d = sys_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "idVendor").write_text("2752\n")
    (d / "busnum").write_text("1\n")
    (d / "devnum").write_text(f"{dev}\n")


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _listed(count: int | None):
    calls = []

    async def listed() -> int | None:
        calls.append(count)
        return count

    listed.calls = calls
    return listed


async def test_restart_respawns_right_away_without_counting_a_crash(make_supervisor, record):
    sup = make_supervisor(backoff_min_s=5.0, backoff_max_s=5.0)  # a crash restart would wait 5 s
    await sup.start()
    try:
        await wait_for(lambda: len(starts(record)) == 1)
        first = sup.pid
        began = time.monotonic()
        assert await sup.restart("test") is True
        await wait_for(lambda: len(starts(record)) == 2 and sup.state == RUNNING, timeout=3)
        assert time.monotonic() - began < 2.0
        assert sup.pid != first and sup.restarts == 1
        assert sup._recent_crashes() == 0
        assert sup.snapshot()["daemon_process_error"] is None
    finally:
        await sup.stop()


async def test_restart_while_not_running_does_nothing(make_supervisor, record):
    sup = make_supervisor()
    assert await sup.restart("test") is False  # never started
    assert starts(record) == []


async def test_a_re_enumerated_minidsp_restarts_the_daemon_once(make_supervisor, record, tmp_path):
    sys_root = tmp_path / "sys"
    _plug(sys_root, 4)
    sup = make_supervisor(backoff_min_s=5.0, backoff_max_s=5.0, usb_identity=lambda: usb_identity(sys_root))
    clock = _Clock()
    listed = _listed(0)  # the daemon's cached libusb list doesn't have the new device number
    watcher = UsbWatcher(sup, listed, identity=lambda: usb_identity(sys_root), grace_s=10.0, clock=clock)
    await sup.start()
    try:
        await wait_for(lambda: len(starts(record)) == 1)
        assert sup.usb_at_spawn == ((1, 4),)
        assert await watcher.check() is False  # nothing changed

        _plug(sys_root, 5)  # power cycled: the kernel hands out a new device number
        assert await watcher.check() is False  # give the daemon a chance first
        clock.t += 5
        assert await watcher.check() is False
        assert listed.calls == []
        clock.t += 6
        assert await watcher.check() is True
        await wait_for(lambda: len(starts(record)) == 2 and sup.state == RUNNING, timeout=3)
        assert sup.usb_at_spawn == ((1, 5),) and sup.restarts == 1

        clock.t += 60  # the new daemon has the device in its list now; no second restart
        assert await watcher.check() is False
        clock.t += 60
        assert await watcher.check() is False
        assert len(starts(record)) == 2
    finally:
        await sup.stop()


async def test_a_daemon_that_lists_the_device_is_left_alone(make_supervisor, record, tmp_path):
    sys_root = tmp_path / "sys"
    sup = make_supervisor(usb_identity=lambda: usb_identity(sys_root))
    clock = _Clock()
    listed = _listed(1)
    watcher = UsbWatcher(sup, listed, identity=lambda: usb_identity(sys_root), grace_s=10.0, clock=clock)
    sys_root.mkdir()
    await sup.start()
    try:
        await wait_for(lambda: len(starts(record)) == 1)
        assert sup.usb_at_spawn == ()
        _plug(sys_root, 7)  # plugged in after the daemon started, and the daemon picked it up anyway
        await watcher.check()
        clock.t += 11
        assert await watcher.check() is False
        clock.t += 11
        assert await watcher.check() is False
        assert listed.calls == [1]  # asked once, then trusted
        assert len(starts(record)) == 1
    finally:
        await sup.stop()


async def test_without_sysfs_nothing_is_restarted(make_supervisor, record, tmp_path):
    sup = make_supervisor(usb_identity=lambda: None)
    clock = _Clock()
    listed = _listed(0)
    watcher = UsbWatcher(sup, listed, identity=lambda: None, grace_s=0.0, clock=clock)
    await sup.start()
    try:
        await wait_for(lambda: len(starts(record)) == 1)
        clock.t += 100
        assert await watcher.check() is False
        assert listed.calls == [] and len(starts(record)) == 1
    finally:
        await sup.stop()
