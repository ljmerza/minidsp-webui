"""Fixtures for the tests that run against the dev stack (``make dev-up``).

``MINIDSPD_MOCK_URL`` points at the mock minidspd (default http://127.0.0.1:15380). It is deliberately not the app's
``MINIDSPD_URL``: these tests write big gains and volume jumps, so they must never pick up a URL meant for real
hardware. The ``daemon_url`` fixture also refuses any daemon that isn't listing exactly the mock device.
``MINIDSPD_MOCK_CONTAINER`` names the mock's container, used to read its DSP memory back with
``minidsp debug dump-float``.
"""

import asyncio
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest
import uvicorn

from app.config import Settings
from app.daemon.client import DaemonClient
from app.main import create_app

MOCK_URL = os.environ.get("MINIDSPD_MOCK_URL", "http://127.0.0.1:15380").rstrip("/")
MOCK_CONTAINER = os.environ.get("MINIDSPD_MOCK_CONTAINER", "minidsp-webui-dev-minidspd-mock-1")
MOCK_SERIAL = 911111


def _device_serials(devices: object) -> list[int | None] | None:
    """Serials of a ``/devices`` listing (None for a device still being probed), or None if it isn't a list."""
    if not isinstance(devices, list):
        return None
    serials: list[int | None] = []
    for device in devices:
        version = device.get("version") if isinstance(device, dict) else None
        serials.append(version.get("serial") if isinstance(version, dict) else None)
    return serials


@pytest.fixture(scope="session")
def daemon_url() -> str:
    """Wait (up to 30 s) for the mock daemon to list its device; fail with a hint if it never does.

    Fails at once if the daemon lists anything other than the single mock device, so a URL that points at real
    hardware can never receive the writes these tests make.
    """
    last_error = ""
    for _ in range(60):
        try:
            response = httpx.get(f"{MOCK_URL}/devices", timeout=2)
            serials = _device_serials(response.json()) if response.status_code == 200 else None
            if serials == [MOCK_SERIAL]:
                return MOCK_URL
            if serials and None not in serials:
                pytest.fail(
                    f"{MOCK_URL} lists devices {serials}, not just the mock ({MOCK_SERIAL}); refusing to run tests "
                    "that write to it. Point MINIDSPD_MOCK_URL at the dev stack's mock minidspd."
                )
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except (httpx.HTTPError, ValueError) as exc:
            last_error = repr(exc)
        time.sleep(0.5)
    pytest.fail(f"mock minidspd not ready at {MOCK_URL} ({last_error}); run `make dev-up` first")


@pytest.fixture
async def raw(daemon_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """Plain HTTP client for asserting the daemon's wire behaviour."""
    async with httpx.AsyncClient(base_url=daemon_url, timeout=10) as client:
        yield client


@pytest.fixture
async def daemon(daemon_url: str) -> AsyncIterator[DaemonClient]:
    """The app's own daemon client."""
    client = DaemonClient(daemon_url, timeout=10, preset_timeout=15)
    yield client
    await client.aclose()


@pytest.fixture
async def restore_master(raw: httpx.AsyncClient) -> AsyncIterator[None]:
    """Put the master status back after a test that changes it."""
    before = (await raw.get("/devices/0")).json()["master"]
    yield
    body = {k: before[k] for k in ("preset", "source", "volume", "mute") if before.get(k) is not None}
    await raw.post("/devices/0", json=body)


class MockMemory:
    """Reads the mock's DSP settings memory (scalar writes only: gains, mutes, delays, polarity, compressor).

    The mock ignores biquad and bypass commands (it just acks them), so those can't be read back.
    """

    def __init__(self, container: str) -> None:
        self.container = container

    async def dump(self, start: int, end: int) -> dict[int, float]:
        """Non-zero words in [start, end) as floats. ``minidsp debug dump-float`` takes bare hex addresses."""
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            self.container,
            "minidsp",
            "debug",
            "dump-float",
            f"{start:x}",
            f"{end:x}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"dump-float failed: {err.decode().strip()}")
        words: dict[int, float] = {}
        for line in out.decode().splitlines():
            addr, _, value = line.partition(":")
            if value:
                words[int(addr, 16)] = float(value)
        return words

    @staticmethod
    def as_int(value: float) -> int:
        """Integer writes (mute/enable/delay samples) are stored raw, so dump-float shows them as subnormal floats."""
        return struct.unpack("<I", struct.pack("<f", value))[0]

    async def word(self, addr: int) -> float:
        return (await self.dump(addr, addr + 1)).get(addr, 0.0)

    async def int_word(self, addr: int) -> int:
        return self.as_int(await self.word(addr))


@pytest.fixture(scope="session")
def mock_memory() -> MockMemory:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available; can't read the mock's memory")
    probe = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", MOCK_CONTAINER], capture_output=True, text=True, check=False
    )
    if probe.stdout.strip() != "true":
        pytest.skip(f"mock container {MOCK_CONTAINER!r} not running; set MINIDSPD_MOCK_CONTAINER")
    return MockMemory(MOCK_CONTAINER)


@pytest.fixture
def post_config(raw: httpx.AsyncClient) -> Callable:
    async def post(body: dict) -> httpx.Response:
        return await raw.post("/devices/0/config", json=body)

    return post


@pytest.fixture(scope="module")
def app_url(daemon_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The app, pointed at the mock, served by uvicorn in a background thread on an ephemeral loopback port."""
    settings = Settings(
        _env_file=None,
        minidspd_url=daemon_url,
        db_path=str(tmp_path_factory.mktemp("app") / "state.sqlite3"),
        preset_switch_timeout_s=5.0,
        levels_idle_grace_s=0.2,
        monitor_backoff_min_s=0.1,
        monitor_backoff_max_s=0.5,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        _wait_connected(url)
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()


def _wait_connected(url: str) -> None:
    devices: object = None
    for _ in range(150):
        try:
            devices = httpx.get(f"{url}/api/v1/devices", timeout=2).json()
            if any(d.get("state") == "connected" and d.get("monitor_connected") for d in devices):
                return
        except httpx.HTTPError as exc:
            devices = exc
        time.sleep(0.1)
    pytest.fail(f"app never connected to the mock: {devices!r}")


@pytest.fixture
async def api(app_url: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=app_url, timeout=20) as client:
        yield client
