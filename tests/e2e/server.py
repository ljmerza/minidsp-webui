"""The app under test for the browser tests: ``python -m app.main`` in embedded mode with minidspd's mock 2x4HD.

Each `AppServer` is a fresh app: its own SQLite file, its own daemon (so a fresh mock device) and ephemeral loopback
ports for both. It never reads the developer's environment or ``.env`` for app settings (it runs from a temp dir with
every setting variable removed), so it can't reach a real device or an MQTT broker.

The minidspd binary comes from ``MINIDSPD_BINARY``, then ``PATH``, then the cache ``scripts/fetch-minidspd.sh`` fills.
"""

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config import Settings
from app.daemon.embedded import MOCK_SERIAL

ROOT = Path(__file__).resolve().parents[2]
START_TIMEOUT_S = 45.0
FETCH_HINT = "run scripts/fetch-minidspd.sh (or set MINIDSPD_BINARY to a minidspd 0.1.12 binary)"


def minidspd_version() -> str:
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if line.startswith("ARG MINIDSP_VERSION="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("no ARG MINIDSP_VERSION in the Dockerfile")


def cached_minidspd() -> Path:
    """Where scripts/fetch-minidspd.sh puts the binary."""
    explicit = os.environ.get("MINIDSPD_CACHE_DIR")
    cache = (
        Path(explicit)
        if explicit
        else Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "minidsp-webui"
    )
    return cache / f"minidspd-{minidspd_version()}" / "minidspd"


def find_minidspd() -> Path | None:
    explicit = os.environ.get("MINIDSPD_BINARY")
    if explicit:
        path = Path(explicit)
        return path if path.is_file() and os.access(path, os.X_OK) else None
    on_path = shutil.which("minidspd")
    if on_path:
        return Path(on_path)
    cached = cached_minidspd()
    return cached if cached.is_file() and os.access(cached, os.X_OK) else None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _clean_env() -> dict[str, str]:
    """The parent environment without any app setting (MINIDSPD_*, MQTT_*, DB_PATH, ...)."""
    names = {name.upper() for name in Settings.model_fields}
    return {k: v for k, v in os.environ.items() if k.upper() not in names}


@dataclass
class AppServer:
    """``with AppServer(binary) as app: app.url`` – started, connected to the mock device, and stopped afterwards."""

    minidspd: Path
    settings: dict[str, str] = field(default_factory=dict)  # extra app settings, e.g. {"GAIN_STEP_CONFIRM_DB": "3"}
    serial: int = MOCK_SERIAL
    port: int = 0
    url: str = ""
    daemon_url: str = ""  # the bundled minidspd's own API (loopback)
    log_path: Path | None = None
    _proc: subprocess.Popen | None = None
    _tmp: tempfile.TemporaryDirectory | None = None

    def start(self) -> "AppServer":
        self._tmp = tempfile.TemporaryDirectory(prefix="mdw-e2e-")
        tmp = Path(self._tmp.name)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.daemon_url = f"http://127.0.0.1:{free_port()}"
        env = _clean_env() | {
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
            "MINIDSPD_MODE": "embedded",
            "MINIDSPD_MOCK": "true",
            "MINIDSPD_BINARY": str(self.minidspd),
            "MINIDSPD_HTTP_BIND": self.daemon_url.removeprefix("http://"),
            "MINIDSPD_CONFIG_DIR": str(tmp),
            "DB_PATH": str(tmp / "e2e.sqlite3"),
            "WEBUI_BIND": "127.0.0.1",
            "WEBUI_PORT": str(self.port),
            "MQTT_ENABLED": "false",
            "VERIFY_NIGHTLY": "false",
            "LOG_LEVEL": "info",
        }
        env |= self.settings
        self.log_path = tmp / "app.log"
        log = self.log_path.open("wb")
        # cwd is the temp dir, so a developer's .env (pydantic-settings reads ./.env) is never picked up.
        # Its own process group, so a kill also reaches minidspd if the app can't stop it itself.
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "app.main"],
            cwd=tmp,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        try:
            self._wait_connected()
        except BaseException:
            self.stop()
            raise
        return self

    def _wait_connected(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT_S
        last = "no answer"
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(f"the app exited with {self._proc.returncode}:\n{self.log_tail()}")
            try:
                health = httpx.get(f"{self.url}/api/v1/health", timeout=2).json()
                devices = health.get("devices") or []
                if any(d["serial"] == self.serial and d["state"] == "connected" for d in devices):
                    self._only_the_mock(devices)
                    return
                last = str(health.get("devices"))
            except (httpx.HTTPError, ValueError) as exc:
                last = repr(exc)
            time.sleep(0.2)
        raise RuntimeError(f"the mock device never connected ({last}):\n{self.log_tail()}")

    def _only_the_mock(self, devices: list[dict]) -> None:
        # minidspd also probes USB. A real miniDSP plugged into this machine would show up next to the mock, and the
        # tests click things: refuse to run rather than risk writing to it.
        others = [d["serial"] for d in devices if d["serial"] != self.serial]
        if others:
            raise RuntimeError(f"minidspd sees a device other than the mock ({others}); refusing to run browser tests")

    def write_behind_the_apps_back(self, config: dict) -> None:
        """A config write straight to the mock (as the official app or the remote would make), e.g.
        ``{"outputs": [{"index": 1, "gain": -6.0}]}``; the app only finds out when it checks the device."""
        response = httpx.post(f"{self.daemon_url}/devices/0/config", json=config, timeout=5)
        response.raise_for_status()

    def log_tail(self, lines: int = 40) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self._proc.pid, signal.SIGKILL)
                self._proc.wait(timeout=5)
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._proc.pid, signal.SIGKILL)  # a daemon left behind by an unclean exit
        self._proc = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def __enter__(self) -> "AppServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
