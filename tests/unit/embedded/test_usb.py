"""USB visibility (usb_status, UsbProbe) and the embedded health fields and problems."""

import errno
import os
from pathlib import Path

import pytest

from app.daemon.embedded import (
    EXTERNAL,
    DaemonSupervisor,
    UsbProbe,
    UsbStatus,
    _denied_detail,
    health_fields,
    health_problems,
    usb_identity,
    usb_status,
)

DESCRIPTOR = bytes.fromhex("12010002ef020140522711007501010b0002")  # a 2x4HD: VID 2752, PID 0011


def add_sysfs_device(sys_root: Path, name: str, vendor: str, bus: int, dev: int, product: str = "2x4HD") -> None:
    d = sys_root / name
    d.mkdir(parents=True)
    (d / "idVendor").write_text(f"{vendor}\n")
    (d / "busnum").write_text(f"{bus}\n")
    (d / "devnum").write_text(f"{dev}\n")
    (d / "product").write_text(f"{product}\n")


def add_node(dev_root: Path, bus: int, dev: int, data: bytes = DESCRIPTOR) -> Path:
    node = dev_root / f"{bus:03d}" / f"{dev:03d}"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_bytes(data)
    return node


@pytest.fixture
def roots(tmp_path):
    return tmp_path / "dev", tmp_path / "sys"


def test_visible_and_openable(roots):
    dev_root, sys_root = roots
    add_sysfs_device(sys_root, "usb1", "1d6b", 1, 1, "root hub")
    add_sysfs_device(sys_root, "1-1.3", "2752", 1, 4)
    add_node(dev_root, 1, 1)
    node = add_node(dev_root, 1, 4)
    status = usb_status(dev_root, sys_root)
    assert status == UsbStatus(True, "ok", f"2x4HD at {node}", (str(node),))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_visible_but_not_openable(roots):
    dev_root, sys_root = roots
    add_sysfs_device(sys_root, "1-1.3", "2752", 1, 4)
    node = add_node(dev_root, 1, 4)
    node.chmod(0o000)
    status = usb_status(dev_root, sys_root)
    assert status.visible and status.access == "denied"
    assert status.detail.startswith(f"{node} is visible but not openable (EACCES)")
    assert "udev OWNER rule" in status.detail


def test_plugged_in_but_usb_not_mounted(roots):
    dev_root, sys_root = roots
    add_sysfs_device(sys_root, "1-1.3", "2752", 1, 4)
    status = usb_status(dev_root, sys_root)
    assert not status.visible and status.access == "not_mounted"
    assert "isn't mounted into the container" in status.detail


def test_plugged_in_but_node_missing(roots):
    dev_root, sys_root = roots
    add_sysfs_device(sys_root, "1-1.3", "2752", 1, 4)
    add_node(dev_root, 1, 1)
    status = usb_status(dev_root, sys_root)
    assert (status.visible, status.access) == (False, "not_mounted")


def test_nothing_mounted(roots):
    dev_root, sys_root = roots
    status = usb_status(dev_root, sys_root)
    assert (status.visible, status.access) == (False, "not_mounted")


def test_no_minidsp_plugged_in(roots):
    dev_root, sys_root = roots
    add_sysfs_device(sys_root, "usb1", "1d6b", 1, 1)
    add_node(dev_root, 1, 1)
    status = usb_status(dev_root, sys_root)
    assert (status.visible, status.access) == (False, "absent")
    assert "2752" in status.detail


def test_without_sysfs_the_descriptors_are_scanned(roots):
    dev_root, sys_root = roots
    add_node(dev_root, 1, 1, bytes(18))
    node = add_node(dev_root, 1, 7)
    status = usb_status(dev_root, sys_root)
    assert status == UsbStatus(True, "ok", f"miniDSP at {node}", (str(node),))


def test_without_sysfs_and_no_readable_minidsp(roots):
    dev_root, sys_root = roots
    add_node(dev_root, 1, 1, bytes(18))
    assert usb_status(dev_root, sys_root).access == "unknown"


def test_eperm_points_at_the_device_cgroup():
    detail = _denied_detail(Path("/dev/bus/usb/001/004"), errno.EPERM)
    assert "(EPERM)" in detail and "c 189:* rmw" in detail


def test_usb_probe_caches_for_its_ttl():
    calls = []

    def probe():
        calls.append(1)
        return UsbStatus(False, "absent", "x")

    cached = UsbProbe(ttl_s=60, probe=probe)
    assert cached.get() is cached.get()
    assert len(calls) == 1
    fresh = UsbProbe(ttl_s=0, probe=probe)
    fresh.get()
    fresh.get()
    assert len(calls) == 3


def test_usb_probe_reports_scan_errors():
    def probe():
        raise PermissionError(13, "denied")

    assert UsbProbe(probe=probe).get().access == "unknown"


# --- health fields ----------------------------------------------------------------------------------------------------


def test_external_mode_fields():
    fields = health_fields(None, None)
    assert fields["daemon_process"] == EXTERNAL and fields["daemon_mode"] == "external"
    assert fields["daemon_restarts"] == 0 and fields["usb_device_visible"] is None
    assert health_problems(fields, device_connected=False) == []


def test_embedded_fields_include_the_supervisor_and_usb():
    sup = DaemonSupervisor()
    usb = UsbProbe(probe=lambda: UsbStatus(True, "ok", "2x4HD at /dev/bus/usb/001/004"))
    fields = health_fields(sup, usb, mock=True)
    assert fields["daemon_mode"] == "embedded" and fields["daemon_mock"] is True
    assert fields["daemon_process"] == "starting" and fields["daemon_restarts"] == 0
    assert (fields["usb_device_visible"], fields["usb_access"]) == (True, "ok")


def _fields(**overrides):
    return {
        "daemon_mode": "embedded",
        "daemon_mock": False,
        "daemon_process": "running",
        "daemon_process_error": None,
        "usb_access": "ok",
        "usb_detail": "ok",
        **overrides,
    }


def test_failed_daemon_is_a_problem():
    problems = health_problems(
        _fields(daemon_process="failed", daemon_process_error="minidspd exited (exit code 1)"), device_connected=True
    )
    assert problems == ["embedded minidspd is failing: minidspd exited (exit code 1)"]


def test_listener_error_of_a_running_daemon_is_a_problem():
    error = "minidspd: HTTP/TCP listener error: Cannot assign requested address (os error 99)"
    problems = health_problems(_fields(daemon_process_error=error), device_connected=False)
    assert problems == [f"embedded minidspd is running but unreachable: {error}"]


def test_usb_problem_only_while_no_device_is_connected():
    fields = _fields(usb_access="denied", usb_detail="/dev/bus/usb/001/004 is visible but not openable (EACCES)")
    assert health_problems(fields, device_connected=False) == [
        "USB: /dev/bus/usb/001/004 is visible but not openable (EACCES)"
    ]
    assert health_problems(fields, device_connected=True) == []


def test_mock_mode_without_usb_is_fine():
    fields = _fields(daemon_mock=True, usb_access="not_mounted", usb_detail="/dev/bus/usb isn't mounted")
    assert health_problems(fields, device_connected=False) == []


def test_usb_identity_changes_only_on_re_enumeration(roots):
    _dev_root, sys_root = roots
    assert usb_identity(sys_root) is None  # no sysfs: can't tell
    add_sysfs_device(sys_root, "usb1", "1d6b", 1, 1, "root hub")
    assert usb_identity(sys_root) == ()  # no miniDSP plugged in
    add_sysfs_device(sys_root, "1-1.3", "2752", 1, 4)
    assert usb_identity(sys_root) == ((1, 4),)
    (sys_root / "1-1.3" / "devnum").write_text("5\n")  # power cycled: the kernel hands out a new number
    assert usb_identity(sys_root) == ((1, 5),)
