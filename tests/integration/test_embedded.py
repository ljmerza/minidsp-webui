"""The all-in-one image in embedded + mock mode (docs/11): the app runs the bundled minidspd itself, finds the mock
2x4HD, survives a daemon kill and shuts down cleanly. No USB, no host daemon.

Uses ``EMBEDDED_IMAGE`` if set (``make test-embedded`` builds it), else builds ``minidsp-webui:embedded-test`` from
this checkout (cached after the first run). The container gets a random loopback port and is removed afterwards.
"""

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MOCK_SERIAL = 911111


def _docker(*args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


@pytest.fixture(scope="module")
def image() -> str:
    if shutil.which("docker") is None or _docker("info", check=False).returncode != 0:
        pytest.skip("docker is not available")
    tag = os.environ.get("EMBEDDED_IMAGE")
    if tag:
        return tag
    tag = "minidsp-webui:embedded-test"
    _docker("build", "-q", "-t", tag, str(ROOT), timeout=1200)
    return tag


@pytest.fixture(scope="module")
def container(image: str) -> Iterator[str]:
    name = f"mdspw-embedded-test-{uuid.uuid4().hex[:8]}"
    _docker(
        "run", "-d", "--name", name,
        "-p", "127.0.0.1::8480",
        "--tmpfs", "/run/minidsp:mode=1777,size=1m",
        "-e", "MINIDSPD_MOCK=true",
        "-e", "LOG_LEVEL=info",
        image,
    )  # fmt: skip
    try:
        yield name
    finally:
        _docker("rm", "-f", name, check=False)


@pytest.fixture(scope="module")
def base_url(container: str) -> str:
    port = _docker("port", container, "8480").stdout.split()[0].rsplit(":", 1)[1]
    return f"http://127.0.0.1:{port}"


def health(base_url: str) -> dict:
    return httpx.get(f"{base_url}/api/v1/health", timeout=5).json()


def wait_health(base_url: str, predicate, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    last: dict | str = "no answer"
    while time.monotonic() < deadline:
        try:
            last = health(base_url)
            if predicate(last):
                return last
        except (httpx.HTTPError, ValueError) as exc:
            last = repr(exc)
        time.sleep(0.25)
    raise AssertionError(f"health never matched: {json.dumps(last, indent=1)}")


def connected_ok(h: dict) -> bool:
    devices = h.get("devices") or []
    return (
        h.get("status") == "ok"
        and h.get("daemon_process") == "running"
        and any(d["serial"] == MOCK_SERIAL and d["state"] == "connected" and d["monitor_connected"] for d in devices)
    )


def test_finds_the_mock_device_through_the_bundled_daemon(base_url: str):
    h = wait_health(base_url, connected_ok)
    assert h["daemon_mode"] == "embedded" and h["daemon_mock"] is True
    assert h["daemon_restarts"] == 0 and h["daemon"] == "up"
    assert h["usb_device_visible"] is False and h["usb_access"] == "not_mounted"  # no USB in this container
    assert h["problems"] == []
    devices = httpx.get(f"{base_url}/api/v1/devices", timeout=5).json()
    assert [d["serial"] for d in devices] == [MOCK_SERIAL]
    page = httpx.get(f"{base_url}/device", timeout=5)
    assert page.status_code == 200 and "minidspd process" in page.text


def test_writes_reach_the_bundled_daemon(base_url: str, container: str):
    wait_health(base_url, connected_ok)
    r = httpx.put(f"{base_url}/api/v1/devices/{MOCK_SERIAL}/master", json={"volume": -31.5}, timeout=10)
    assert r.status_code == 200 and r.json()["volume"] == -31.5
    # Read it back from the daemon itself, inside the container (its API is not published).
    out = _docker(
        "exec", container, "python", "-c",
        "import json, urllib.request as u; print(json.load(u.urlopen('http://127.0.0.1:5380/devices/0'))['master'])",
    ).stdout  # fmt: skip
    assert "'volume': -31.5" in out


def test_daemon_runs_unprivileged_with_its_config_on_tmpfs(base_url: str, container: str):
    h = wait_health(base_url, connected_ok)
    status = _docker("exec", container, "cat", f"/proc/{h['daemon_pid']}/status").stdout
    uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
    assert uid_line.split()[1:] == ["1000"] * 4
    config = _docker("exec", container, "cat", "/run/minidsp/config.toml").stdout
    assert 'bind_address = "127.0.0.1:5380"' in config and "mock:?serial=911111" in config
    assert "tcp_server" not in config
    mounts = _docker("exec", container, "cat", "/proc/mounts").stdout
    assert any(line.split()[1:3] == ["/run/minidsp", "tmpfs"] for line in mounts.splitlines())
    published = _docker("port", container).stdout
    assert "5380" not in published and "8480" in published
    binaries = _docker("exec", container, "minidsp", "--version").stdout
    assert "0.1.12" in binaries


def test_a_killed_daemon_is_restarted_and_the_device_comes_back(base_url: str, container: str):
    before = wait_health(base_url, connected_ok)
    _docker("exec", container, "sh", "-c", f"kill -9 {before['daemon_pid']}")
    killed = time.monotonic()
    restarted = wait_health(base_url, lambda h: h["daemon_restarts"] == 1 and h["daemon_process"] == "running", 10)
    assert time.monotonic() - killed < 3.0
    assert restarted["daemon_pid"] != before["daemon_pid"]
    after = wait_health(base_url, connected_ok, 20)
    assert after["daemon_last_exit"] == "signal SIGKILL"


def test_docker_stop_is_clean_and_quick(base_url: str, container: str):
    wait_health(base_url, connected_ok)
    began = time.monotonic()
    _docker("stop", "-t", "10", container)
    assert time.monotonic() - began < 8.0
    assert _docker("inspect", "-f", "{{.State.ExitCode}}", container).stdout.strip() == "0"
    logs = _docker("logs", container).stdout + _docker("logs", container).stderr
    assert "Application shutdown complete" in logs
    assert " ERROR " not in logs and "Traceback" not in logs
