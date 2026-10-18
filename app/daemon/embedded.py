"""Embedded minidspd (docs/11): the all-in-one image runs the daemon as a child process of the app.

`render_config` builds the daemon's TOML config from the ``MINIDSPD_*`` settings, `DaemonSupervisor` writes it to a
tmpfs (``/run/minidsp``, never the SD card) and keeps ``minidspd --config …`` running: restart on exit with a 1 s → 30 s
backoff, the daemon's stderr forwarded to our log at warning and above only, SIGTERM then SIGKILL on shutdown.
`usb_status` answers "can this container see and open the miniDSP?" for ``/api/v1/health``, and `UsbWatcher` restarts
the daemon when the miniDSP re-enumerates, which minidspd can't notice by itself inside a container.

Nothing above the daemon client knows about any of this: the app talks to the embedded daemon over HTTP exactly as it
talks to an external one.
"""

import asyncio
import contextlib
import errno
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
import tomllib
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
daemon_log = logging.getLogger("minidspd")

DEFAULT_HTTP_BIND = "127.0.0.1:5380"
DEFAULT_CONFIG_DIR = "/run/minidsp"
CONFIG_NAME = "config.toml"
# minidsp-rs only accepts mock serials between 900000 and 965535; the dev mock uses the same one.
MOCK_SERIAL = 911111
MINIDSP_USB_VENDOR = 0x2752

# daemon_process values in /api/v1/health
STARTING = "starting"
RUNNING = "running"
RESTARTING = "restarting"
FAILED = "failed"
STOPPED = "stopped"
EXTERNAL = "external"

_ANY_HOST = {"0.0.0.0", "[::]"}


class EmbeddedConfigError(ValueError):
    """The daemon config can't be built from the settings (bad bind address or MINIDSPD_EXTRA_CONFIG)."""


# --- config -----------------------------------------------------------------------------------------------------------


def split_bind(bind: str) -> tuple[str, int]:
    """``ip:port`` (``[v6]:port`` for IPv6) → (host, port). Raises EmbeddedConfigError.

    The host must be an IP literal: minidspd parses the address with Rust's ``SocketAddr::from_str``, and a hostname
    there only makes its HTTP listener fail while the process (and USB discovery) keeps running."""
    host, sep, port = bind.strip().rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise EmbeddedConfigError(f"MINIDSPD_HTTP_BIND must be ip:port, e.g. {DEFAULT_HTTP_BIND}; got {bind!r}")
    bracketed = host.startswith("[") and host.endswith("]")
    if ":" in host and not bracketed:
        raise EmbeddedConfigError(f"MINIDSPD_HTTP_BIND: put an IPv6 address in brackets, e.g. [::1]:5380; got {bind!r}")
    try:
        address = ipaddress.ip_address(host[1:-1] if bracketed else host)
    except ValueError:
        address = None
    if address is None or (address.version == 6) != bracketed:
        raise EmbeddedConfigError(
            f"MINIDSPD_HTTP_BIND needs an IP address, e.g. {DEFAULT_HTTP_BIND} or [::1]:5380; got {bind!r}"
        )
    return host, int(port)


def daemon_url_for_bind(bind: str) -> str:
    """The URL the app uses to reach a daemon bound to ``bind`` from inside the same container."""
    host, port = split_bind(bind)
    if host in _ANY_HOST:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise EmbeddedConfigError(f"unsupported value in the daemon config: {value!r}")


def _toml_string(text: str) -> str:
    """A TOML basic string. JSON's escapes are a subset of TOML's, but not with ``ensure_ascii``: it writes characters
    above U+FFFF as surrogate-pair escapes, which TOML rejects. Unescaped, the only character JSON leaves that a TOML
    basic string forbids is DEL."""
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml_string(key)


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def dump_toml(doc: Mapping[str, Any]) -> str:
    """Serialize the small TOML subset minidspd's config uses: top-level scalars, tables and arrays of tables (whose
    nested tables are written inline)."""
    lines = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in doc.items() if not isinstance(v, dict | list)]
    lines += [
        f"{_toml_key(k)} = {_toml_value(v)}" for k, v in doc.items() if isinstance(v, list) and not _is_table_array(v)
    ]
    for key, value in doc.items():
        if isinstance(value, dict):
            lines += ["", f"[{_toml_key(key)}]"] + [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()]
        elif _is_table_array(value):
            for item in value:
                lines += ["", f"[[{_toml_key(key)}]]"] + [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in item.items()]
    return "\n".join(lines).lstrip("\n") + "\n"


def _merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge ``extra`` into ``base``: tables merge, arrays of tables are appended, other values replace."""
    for key, value in extra.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            base[key] = _merge(dict(current), value)
        elif isinstance(current, list) and isinstance(value, list):
            base[key] = current + value
        else:
            base[key] = value
    return base


def build_config(
    *, http_bind: str = DEFAULT_HTTP_BIND, discovery: bool = False, mock: bool = False, extra: str = ""
) -> dict[str, Any]:
    """The daemon config as a dict. No ``[[tcp_server]]``: the official apps aren't used, so it isn't exposed."""
    split_bind(http_bind)
    doc: dict[str, Any] = {
        "ignore_advertisements": not discovery,
        "http_server": {"bind_address": http_bind.strip()},
    }
    if mock:
        doc["static_device"] = [{"url": f"mock:?serial={MOCK_SERIAL}"}]
    if extra.strip():
        try:
            parsed = tomllib.loads(extra)
        except tomllib.TOMLDecodeError as exc:
            raise EmbeddedConfigError(f"MINIDSPD_EXTRA_CONFIG is not valid TOML: {exc}") from None
        http_server = parsed.get("http_server", {})
        if not isinstance(http_server, dict):
            raise EmbeddedConfigError("MINIDSPD_EXTRA_CONFIG: http_server must be a table ([http_server])")
        if "bind_address" in http_server:
            raise EmbeddedConfigError("set the daemon's HTTP address with MINIDSPD_HTTP_BIND, not in the extra config")
        doc = _merge(doc, parsed)
    return doc


def render_config(**kwargs: Any) -> str:
    """`build_config` as TOML text. Raises EmbeddedConfigError."""
    text = "# Generated by minidsp-webui at startup (MINIDSPD_* settings); edits are overwritten.\n" + dump_toml(
        build_config(**kwargs)
    )
    tomllib.loads(text)  # never hand the daemon something it can't parse
    return text


def prepare_config_dir(preferred: str = DEFAULT_CONFIG_DIR, shm: str = "/dev/shm") -> tuple[Path, bool]:
    """A writable directory for the config: ``preferred`` (mount a tmpfs there), else a fresh directory in ``shm``
    (a tmpfs in every container), else in the system temp dir. Returns (path, created_a_temp_dir)."""
    path = Path(preferred)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = tempfile.NamedTemporaryFile(dir=path, prefix=".probe-")  # noqa: SIM115 - closed right away
        probe.close()
        return path, False
    except OSError as exc:
        reason = exc.strerror or str(exc)
    for parent in (shm, None):
        try:
            fallback = Path(tempfile.mkdtemp(prefix="minidsp-", dir=parent))
        except OSError:
            continue
        log.log(
            logging.INFO if parent else logging.WARNING,
            "%s is not writable (%s); writing the minidspd config to %s",
            preferred,
            reason,
            fallback,
        )
        return fallback, True
    raise OSError(f"no writable directory for the minidspd config ({preferred}: {reason})")


def write_config(text: str, directory: Path) -> Path:
    """Write the config atomically (0600: MINIDSPD_EXTRA_CONFIG may hold device URLs or addresses)."""
    target = directory / CONFIG_NAME
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target


# --- log forwarding ---------------------------------------------------------------------------------------------------

# env_logger's default format: "[2026-10-17T12:00:00Z WARN  minidspd::device_manager] message"
_ENV_LOGGER = re.compile(r"^\[\S+\s+(ERROR|WARN|INFO|DEBUG|TRACE)\s+([^\]]*)\]\s?(.*)$")


_LEVELS = {"ERROR": logging.ERROR, "WARN": logging.WARNING}


def parse_stderr_line(line: str) -> tuple[int, str] | None:
    """(our log level, text) for a daemon stderr line, None for a blank one. Warnings and errors keep their level;
    the daemon's info/debug chatter maps to debug. Lines that aren't env_logger records (panics, ``eprintln!``
    listener errors) are treated as warnings."""
    if not line.strip():
        return None
    match = _ENV_LOGGER.match(line)
    if match is None:
        return logging.WARNING, line
    level, target, message = match.groups()
    return _LEVELS.get(level, logging.DEBUG), f"{target}: {message}"


def forward_stderr_line(line: str, logger: logging.Logger = daemon_log) -> None:
    """Forward one stderr line at the level `parse_stderr_line` gives it, without collapsing repeats."""
    record = parse_stderr_line(line)
    if record is not None:
        logger.log(record[0], "%s", record[1])


@dataclass
class _Repeats:
    since: float
    count: int = 0


class StderrForwarder:
    """`forward_stderr_line` that collapses repeats. minidspd retries a device it can't open every second with the
    same warning, forever; forwarded as-is that is a log line per second on the SD card. A warning or error is logged
    the first time, its repeats go to debug, and at most once per ``repeat_window_s`` it is logged again with the
    number of repeats since."""

    def __init__(
        self,
        logger: logging.Logger = daemon_log,
        *,
        repeat_window_s: float = 300.0,
        max_tracked: int = 64,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.logger = logger
        self.repeat_window_s = repeat_window_s
        self.max_tracked = max_tracked
        self._clock = clock
        self._seen: OrderedDict[tuple[int, str], _Repeats] = OrderedDict()

    def __call__(self, line: str) -> None:
        record = parse_stderr_line(line)
        if record is None:
            return
        level, text = record
        if level < logging.WARNING:
            self.logger.debug("%s", text)
            return
        now = self._clock()
        seen = self._seen.get(record)
        if seen is None:
            self.logger.log(level, "%s", text)
            self._seen[record] = _Repeats(now)
            if len(self._seen) > self.max_tracked:
                self._seen.popitem(last=False)
            return
        self._seen.move_to_end(record)
        seen.count += 1
        if now - seen.since < self.repeat_window_s:
            self.logger.debug("%s", text)
            return
        self.logger.log(level, "%s (repeated %d times in the last %.0f s)", text, seen.count, now - seen.since)
        seen.since, seen.count = now, 0


async def _pump(stream: asyncio.StreamReader | None, handle: Callable[[str], None]) -> None:
    if stream is None:
        return
    while True:
        try:
            line = await stream.readline()
        except ValueError:  # a line longer than the reader's limit: the buffer was dropped, keep reading
            continue
        if not line:
            return
        handle(line.decode("utf-8", "replace").rstrip())


def describe_exit(code: int) -> str:
    if code < 0:
        try:
            return f"signal {signal.Signals(-code).name}"
        except ValueError:
            return f"signal {-code}"
    return f"exit code {code}"


# --- supervisor -------------------------------------------------------------------------------------------------------


@dataclass
class EmbeddedOptions:
    http_bind: str = DEFAULT_HTTP_BIND
    discovery: bool = False
    mock: bool = False
    extra_config: str = ""
    binary: str = "minidspd"
    config_dir: str = DEFAULT_CONFIG_DIR

    @classmethod
    def from_settings(cls, settings: Any) -> "EmbeddedOptions":
        return cls(
            http_bind=settings.minidspd_http_bind,
            discovery=settings.minidspd_discovery,
            mock=settings.minidspd_mock,
            extra_config=settings.minidspd_extra_config,
            binary=settings.minidspd_binary,
            config_dir=settings.minidspd_config_dir,
        )


class DaemonSupervisor:
    """Runs and restarts minidspd. `start` returns once the first spawn was attempted; `stop` is idempotent.

    ``state`` is ``starting``, ``running``, ``restarting`` (waiting out the backoff), ``failed`` (the config can't be
    written, the binary can't be run, or it is crash-looping: ``crash_limit`` exits within ``crash_window_s`` and the
    current run hasn't lasted ``stable_after_s`` yet) or ``stopped``. It keeps retrying in every case but a bad config.
    """

    def __init__(
        self,
        options: EmbeddedOptions | None = None,
        *,
        backoff_min_s: float = 1.0,
        backoff_max_s: float = 30.0,
        stable_after_s: float = 30.0,
        stop_timeout_s: float = 5.0,
        crash_window_s: float = 300.0,
        crash_limit: int = 5,
        env: Mapping[str, str] | None = None,
        usb_identity: Callable[[], tuple[tuple[int, int], ...] | None] | None = None,
    ) -> None:
        self.options = options or EmbeddedOptions()
        self.backoff_min_s = backoff_min_s
        self.backoff_max_s = backoff_max_s
        self.stable_after_s = stable_after_s  # a run at least this long resets the backoff and ends "failed"
        self.stop_timeout_s = stop_timeout_s
        self.crash_window_s = crash_window_s
        self.crash_limit = crash_limit
        self.env = env
        self.usb_identity = usb_identity

        self.restarts = 0
        self.pid: int | None = None
        self.last_exit: int | None = None
        self.last_error: str | None = None  # why the last run ended (or couldn't start)
        self.listener_error: str | None = None  # the current run's HTTP listener failed; the process keeps running
        self.usb_at_spawn: tuple[tuple[int, int], ...] | None = None  # `usb_identity` when the current run started
        self.config_path: Path | None = None
        self.started_at: float | None = None  # monotonic start of the current process

        self._phase = STARTING
        self._config_failed = False
        self._spawn_failed = False
        self._task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False
        self._restart_requested = False
        self._wake = asyncio.Event()
        self._spawned = asyncio.Event()
        self._crashes: deque[float] = deque()
        self._temp_dir: Path | None = None
        self._loop_logged = False
        self._stderr = StderrForwarder()  # shared by every run, so a crash loop doesn't repeat the same lines either

    # public -----------------------------------------------------------------------------------------------------------

    @property
    def state(self) -> str:
        if self._phase in (STARTING, STOPPED):
            return self._phase
        if self._config_failed:
            return FAILED
        looping = self._recent_crashes() >= self.crash_limit
        if self._phase == RUNNING:
            uptime = self.uptime_s or 0.0
            return FAILED if looping and uptime < self.stable_after_s else RUNNING
        return FAILED if self._spawn_failed or looping else RESTARTING

    @property
    def uptime_s(self) -> float | None:
        if self._phase != RUNNING or self.started_at is None:
            return None
        return time.monotonic() - self.started_at

    async def start(self) -> None:
        if self._task is not None or self._config_failed:
            return
        try:
            text = render_config(
                http_bind=self.options.http_bind,
                discovery=self.options.discovery,
                mock=self.options.mock,
                extra=self.options.extra_config,
            )
            directory, created = await asyncio.to_thread(prepare_config_dir, self.options.config_dir)
            self._temp_dir = directory if created else None
            self.config_path = await asyncio.to_thread(write_config, text, directory)
        except Exception as exc:  # whatever the config problem, report it in health instead of failing app startup
            self._config_failed = True
            self._phase = RESTARTING
            self.last_error = f"cannot write the minidspd config: {exc}"
            log.error("embedded minidspd not started: %s", self.last_error)
            return
        log.info(
            "starting embedded minidspd (%s, HTTP on %s%s)",
            self.options.binary,
            self.options.http_bind,
            ", with a mock device" if self.options.mock else "",
        )
        self._task = asyncio.create_task(self._run(), name="minidspd-supervisor")
        await self._spawned.wait()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._proc is not None:
            await self._terminate(self._proc)
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), self.stop_timeout_s + 2)
            except TimeoutError:
                self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._phase = STOPPED
        self.pid = None
        self.started_at = None
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        elif self.config_path is not None:
            with contextlib.suppress(OSError):
                self.config_path.unlink()

    async def restart(self, reason: str) -> bool:
        """SIGTERM the running daemon and respawn it right away: no backoff, and it doesn't count as a crash. False if
        no daemon is running."""
        proc = self._proc
        if self._stopping or proc is None or self._phase != RUNNING:
            return False
        log.warning("restarting embedded minidspd: %s", reason)
        self._restart_requested = True
        await self._terminate(proc)
        return True

    def snapshot(self) -> dict[str, Any]:
        uptime = self.uptime_s
        state = self.state
        # A running daemon only has an error while its HTTP listener is down; why an earlier run ended is history
        # (daemon_last_exit), not a current error.
        error = self.listener_error if state == RUNNING else self.listener_error or self.last_error
        return {
            "daemon_process": state,
            "daemon_restarts": self.restarts,
            "daemon_pid": self.pid,
            "daemon_uptime_s": round(uptime, 1) if uptime is not None else None,
            "daemon_last_exit": describe_exit(self.last_exit) if self.last_exit is not None else None,
            "daemon_process_error": error,
        }

    # internals --------------------------------------------------------------------------------------------------------

    def _recent_crashes(self) -> int:
        now = time.monotonic()
        while self._crashes and now - self._crashes[0] > self.crash_window_s:
            self._crashes.popleft()
        return len(self._crashes)

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ if self.env is None else self.env)
        env.setdefault("RUST_LOG", "warn")  # the daemon's info logging stays off the SD card
        return env

    def _on_stderr(self, line: str) -> None:
        self._stderr(line)
        # The HTTP listener runs as its own task in minidspd: when it fails (address in use, …) the process keeps
        # running, so record why the app can't reach it.
        if line.startswith("HTTP/TCP listener error"):
            self.listener_error = f"minidspd: {line.strip()}"

    async def _run(self) -> None:
        backoff = self.backoff_min_s
        while not self._stopping:
            started = time.monotonic()
            await self._run_once()
            if self._stopping:
                break
            if self._restart_requested:  # `restart`: respawn now
                self._restart_requested = False
                self.restarts += 1
                continue
            if time.monotonic() - started >= self.stable_after_s:
                backoff = self.backoff_min_s
                self._loop_logged = False
            self._crashes.append(time.monotonic())
            self._phase = RESTARTING
            if self._recent_crashes() >= self.crash_limit and not self._loop_logged:
                self._loop_logged = True
                log.error(
                    "embedded minidspd failed %d times in %.0f s (%s); it keeps being restarted, at most every %.0f s",
                    self._recent_crashes(),
                    self.crash_window_s,
                    self.last_error,
                    self.backoff_max_s,
                )
            log.warning("embedded minidspd: %s; restarting in %.1f s", self.last_error, backoff)
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), backoff)
            backoff = min(backoff * 2, self.backoff_max_s)
            if not self._stopping:
                self.restarts += 1

    def _read_usb_identity(self) -> tuple[tuple[int, int], ...] | None:
        if self.usb_identity is None:
            return None
        try:
            return self.usb_identity()
        except Exception:
            log.exception("reading the USB identity failed")
            return None

    async def _run_once(self) -> None:
        self.usb_at_spawn = self._read_usb_identity()  # before the spawn: what this run's libusb can know about
        try:
            proc = await asyncio.create_subprocess_exec(
                self.options.binary,
                "--config",
                str(self.config_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(),
            )
        except OSError as exc:
            self._spawn_failed = True
            self._phase = RESTARTING
            self.last_exit = None
            self.last_error = f"cannot run {self.options.binary}: {exc.strerror or exc}"
            self._spawned.set()
            return
        self._spawn_failed = False
        self.listener_error = None
        self._proc = proc
        self.pid = proc.pid
        self.started_at = time.monotonic()
        self._phase = RUNNING
        self._spawned.set()
        if self._stopping:  # stop() raced the spawn
            await self._terminate(proc)
        pumps = asyncio.gather(
            _pump(proc.stderr, self._on_stderr),
            _pump(proc.stdout, lambda line: daemon_log.debug("%s", line)),
        )
        code = await proc.wait()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(pumps, 2)
        self._proc = None
        self.pid = None
        self.started_at = None
        self.last_exit = code
        if not self._stopping and not self._restart_requested:
            self.last_error = f"minidspd exited ({describe_exit(code)})"

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM, wait ``stop_timeout_s``, then SIGKILL."""
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), self.stop_timeout_s)
        except TimeoutError:
            log.warning("embedded minidspd ignored SIGTERM for %.0f s; killing it", self.stop_timeout_s)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


# --- USB --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UsbStatus:
    """What the container sees of the miniDSP on USB.

    ``visible``: a vendor-2752 device node is present in the container. ``access``: ``ok``, ``denied`` (present but
    open() fails: EACCES = file permissions/udev rule, EPERM = rootful Docker's device cgroup), ``not_mounted``
    (``/dev/bus/usb`` isn't in the container), ``absent`` (no miniDSP plugged in) or ``unknown``."""

    visible: bool
    access: str
    detail: str
    devices: tuple[str, ...] = ()


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _try_open(node: Path) -> int | None:
    """Open the node read/write the way libusb does, without any ioctl (claims nothing; harmless while minidspd holds
    the device). Returns None on success, else the errno."""
    try:
        fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
    except OSError as exc:
        return exc.errno or errno.EIO
    os.close(fd)
    return None


def _denied_detail(node: Path, err: int) -> str:
    if err == errno.EPERM:
        return f"{node} is visible but not openable (EPERM): rootful Docker needs device_cgroup_rules ['c 189:* rmw']"
    if err == errno.EACCES:
        try:
            st = node.stat()
            owner = f" (owner {st.st_uid}:{st.st_gid}, mode {st.st_mode & 0o777:o}; running as {os.getuid()}:"
            owner += f"{os.getgid()}, groups {','.join(map(str, os.getgroups())) or '-'})"
        except OSError:
            owner = ""
        return (
            f"{node} is visible but not openable (EACCES){owner}: add the container user to the device's group "
            "(group_add) or give the device to the Docker user with a udev OWNER rule (rootless)"
        )
    return f"{node} is visible but not openable ({errno.errorcode.get(err, err)})"


def _sysfs_devices(sys_root: Path, vendor: int) -> list[tuple[int, int, str]]:
    found = []
    if not sys_root.is_dir():
        return found
    for dev in sorted(sys_root.iterdir()):
        vid = _read(dev / "idVendor")
        if vid is None or vid.lower() != f"{vendor:04x}":
            continue
        bus, num = _read(dev / "busnum"), _read(dev / "devnum")
        if bus and num and bus.isdigit() and num.isdigit():
            found.append((int(bus), int(num), _read(dev / "product") or "miniDSP"))
    return found


def _descriptor_vendor(node: Path) -> int | None:
    try:
        with node.open("rb") as fh:
            desc = fh.read(18)
    except OSError:
        return None
    return int.from_bytes(desc[8:10], "little") if len(desc) >= 10 else None


def usb_status(
    dev_root: str | Path = "/dev/bus/usb",
    sys_root: str | Path = "/sys/bus/usb/devices",
    vendor: int = MINIDSP_USB_VENDOR,
) -> UsbStatus:
    """Look for the miniDSP: sysfs (read-only in containers, shows what is plugged into the host) names its bus and
    device number, then the matching ``/dev/bus/usb`` node is opened. Without sysfs, readable nodes are scanned for
    the vendor id in their cached device descriptor."""
    dev_root, sys_root = Path(dev_root), Path(sys_root)
    mounted = dev_root.is_dir()
    sysfs = _sysfs_devices(sys_root, vendor)
    if sysfs:
        denied: list[str] = []
        visible: list[str] = []
        for bus, num, product in sysfs:
            node = dev_root / f"{bus:03d}" / f"{num:03d}"
            if not node.exists():
                continue
            visible.append(str(node))
            err = _try_open(node)
            if err is None:
                return UsbStatus(True, "ok", f"{product} at {node}", tuple(visible))
            denied.append(_denied_detail(node, err))
        if denied:
            return UsbStatus(True, "denied", denied[0], tuple(visible))
        if not mounted:
            return UsbStatus(
                False,
                "not_mounted",
                "a miniDSP is plugged into the host, but /dev/bus/usb isn't mounted into the container",
            )
        return UsbStatus(False, "not_mounted", "a miniDSP is plugged into the host, but its device node isn't here")
    if not mounted:
        return UsbStatus(False, "not_mounted", "/dev/bus/usb isn't mounted into the container")
    if sys_root.is_dir():
        return UsbStatus(False, "absent", f"no miniDSP (USB vendor {vendor:04x}) is plugged in")
    found = []
    for node in sorted(dev_root.glob("*/*")):
        if _descriptor_vendor(node) == vendor:
            found.append(str(node))
            err = _try_open(node)
            if err is None:
                return UsbStatus(True, "ok", f"miniDSP at {node}", tuple(found))
            return UsbStatus(True, "denied", _denied_detail(node, err), tuple(found))
    return UsbStatus(False, "unknown", "no readable miniDSP device node found (sysfs is not available to check)")


def usb_identity(
    sys_root: str | Path = "/sys/bus/usb/devices", vendor: int = MINIDSP_USB_VENDOR
) -> tuple[tuple[int, int], ...] | None:
    """``(bus, device number)`` of every plugged-in miniDSP, or None without sysfs. The device number changes when
    the device is re-enumerated (unplugged or power cycled), not when only minidspd restarts."""
    root = Path(sys_root)
    if not root.is_dir():
        return None
    return tuple(sorted((bus, num) for bus, num, _ in _sysfs_devices(root, vendor)))


class UsbProbe:
    """`usb_status` cached for a few seconds, since health is polled."""

    def __init__(self, ttl_s: float = 5.0, probe: Callable[[], UsbStatus] = usb_status) -> None:
        self.ttl_s = ttl_s
        self._probe = probe
        self._at = -float("inf")
        self._value: UsbStatus | None = None

    def get(self) -> UsbStatus:
        now = time.monotonic()
        if self._value is None or now - self._at >= self.ttl_s:
            try:
                self._value = self._probe()
            except OSError as exc:
                self._value = UsbStatus(False, "unknown", f"USB scan failed: {exc}")
            self._at = now
        return self._value


class UsbWatcher:
    """Restarts the embedded minidspd when a miniDSP re-enumerates (re-plug, power cycle).

    minidspd's hidapi enumerates through one long-lived libusb context. That libusb supports hotplug, so it keeps its
    own device list and only updates it on udev events, which never arrive inside a container (no udevd; the daemon's
    netlink socket joins no groups). A miniDSP that comes back under a new device number, or is plugged in after the
    daemon started, is never seen until the daemon restarts.

    Every ``interval_s`` this compares sysfs with the devices present when the current run started. When a new one
    is still there after ``grace_s`` and the daemon lists fewer ready USB devices than sysfs shows, the daemon is
    restarted once. The next run starts with the new device number, so the session's reconnect sees it changed and
    treats it as a possible power cycle."""

    def __init__(
        self,
        supervisor: DaemonSupervisor,
        listed: Callable[[], Awaitable[int | None]],
        *,
        identity: Callable[[], tuple[tuple[int, int], ...] | None] = usb_identity,
        interval_s: float = 5.0,
        grace_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.supervisor = supervisor
        self._listed = listed  # ready USB devices (with a serial) on the daemon, None if it can't be asked
        self._identity = identity
        self.interval_s = interval_s
        self.grace_s = grace_s
        self._clock = clock
        self._since: float | None = None
        self._served: set[tuple[int, int]] = set()  # new devices this run turned out to see after all
        self._pid: int | None = None
        self._task: asyncio.Task[None] | None = None

    async def check(self) -> bool:
        """One step. True if it restarted the daemon."""
        sup = self.supervisor
        if sup.pid != self._pid:
            self._pid, self._served, self._since = sup.pid, set(), None
        try:
            current = self._identity()
        except Exception:
            log.exception("reading the USB identity failed")
            current = None
        spawned = sup.usb_at_spawn
        if sup.state != RUNNING or spawned is None or not current:
            self._since = None
            return False
        new = set(current) - set(spawned) - self._served
        if not new:
            self._since = None
            return False
        now = self._clock()
        if self._since is None:
            self._since = now
            return False
        if now - self._since < self.grace_s:
            return False
        self._since = None
        listed = await self._listed()
        if listed is None:
            return False  # the daemon isn't answering: the supervisor and health deal with that
        if listed >= len(current):
            self._served |= new
            return False
        where = ", ".join(f"{bus:03d}/{num:03d}" for bus, num in sorted(new))
        return await sup.restart(
            f"a miniDSP appeared on USB at {where} after it started, and it can't see it (no hotplug events inside a "
            "container)"
        )

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="minidspd-usb-watcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.check()
            except Exception:
                log.exception("USB watcher step failed")


# --- health -----------------------------------------------------------------------------------------------------------


def health_fields(supervisor: DaemonSupervisor | None, usb: UsbProbe | None, *, mock: bool = False) -> dict[str, Any]:
    """The daemon-process and USB fields of ``/api/v1/health``. External mode (no supervisor) reports
    ``daemon_process: external`` and no USB state: the host daemon owns the device."""
    if supervisor is None:
        return {
            "daemon_mode": "external",
            "daemon_mock": False,
            "daemon_process": EXTERNAL,
            "daemon_restarts": 0,
            "usb_device_visible": None,
            "usb_access": None,
            "usb_detail": None,
        }
    status = usb.get() if usb is not None else None
    return {
        "daemon_mode": "embedded",
        "daemon_mock": mock,
        **supervisor.snapshot(),
        "usb_device_visible": status.visible if status else None,
        "usb_access": status.access if status else None,
        "usb_detail": status.detail if status else None,
    }


def health_problems(fields: Mapping[str, Any], *, device_connected: bool) -> list[str]:
    """Readable problems for the embedded fields (none in external mode). A missing USB device only counts while no
    device is connected, and never in mock mode without hardware."""
    if fields.get("daemon_mode") != "embedded":
        return []
    problems = []
    if fields.get("daemon_process") == FAILED:
        problems.append(f"embedded minidspd is failing: {fields.get('daemon_process_error') or 'unknown error'}")
    elif fields.get("daemon_process") == RUNNING and fields.get("daemon_process_error"):
        problems.append(f"embedded minidspd is running but unreachable: {fields['daemon_process_error']}")
    if not device_connected and not fields.get("daemon_mock") and fields.get("usb_access") not in (None, "ok"):
        problems.append(f"USB: {fields.get('usb_detail')}")
    return problems
