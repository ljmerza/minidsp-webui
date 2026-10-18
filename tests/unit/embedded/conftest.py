"""A fake ``minidspd`` for the supervisor tests: a Python script that records each start and then behaves as the
``FAKE_MODE`` environment variable says."""

import asyncio
import contextlib
import json
import os
import stat
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.daemon.embedded import DaemonSupervisor, EmbeddedOptions

FAKE_DAEMON = """\
import json, os, signal, sys, time

mode = os.environ.get("FAKE_MODE", "run")
config = sys.argv[sys.argv.index("--config") + 1]
with open(os.environ["FAKE_RECORD"], "a") as fh:
    fh.write(json.dumps({
        "pid": os.getpid(),
        "argv": sys.argv[1:],
        "t": time.monotonic(),
        "rust_log": os.environ.get("RUST_LOG"),
        "config": open(config).read(),
    }) + "\\n")
for line in os.environ.get("FAKE_STDERR", "").split("|"):
    if line:
        print(line, file=sys.stderr, flush=True)
print("App is running on: 127.0.0.1:5380", flush=True)
if mode.startswith("exit:"):
    sys.exit(int(mode.split(":")[1]))
if mode.startswith("sleep-exit:"):
    time.sleep(float(mode.split(":")[1]))
    sys.exit(1)
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.05)
"""


@pytest.fixture
def fake_daemon(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "minidspd"
    path.parent.mkdir()
    path.write_text(f"#!{sys.executable}\n{FAKE_DAEMON}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def record(tmp_path: Path) -> Path:
    return tmp_path / "starts.jsonl"


def starts(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line.strip()]


@pytest.fixture
def make_supervisor(tmp_path: Path, fake_daemon: Path, record: Path) -> Iterator[Callable[..., DaemonSupervisor]]:
    created: list[DaemonSupervisor] = []

    def make(mode: str = "run", *, stderr: str = "", options: dict | None = None, **kwargs) -> DaemonSupervisor:
        env = {**os.environ, "FAKE_MODE": mode, "FAKE_RECORD": str(record), "FAKE_STDERR": stderr}
        env.pop("RUST_LOG", None)
        opts = EmbeddedOptions(**{"binary": str(fake_daemon), "config_dir": str(tmp_path / "run"), **(options or {})})
        kwargs = {"backoff_min_s": 0.05, "backoff_max_s": 0.2, "stop_timeout_s": 1.0, **kwargs}
        supervisor = DaemonSupervisor(opts, env=env, **kwargs)
        created.append(supervisor)
        return supervisor

    yield make
    # The tests stop their supervisors; this only catches a failing test's leftovers.
    for supervisor in created:
        if supervisor.pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(supervisor.pid, 9)


async def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still "exists"; check its state.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False
