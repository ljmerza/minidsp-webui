"""DaemonSupervisor against a fake minidspd script: start, restart with backoff, crash loops, shutdown."""

import asyncio
import logging
import os
import signal
import time
import tomllib

from app.daemon.embedded import FAILED, RESTARTING, RUNNING, STOPPED, DaemonSupervisor, EmbeddedOptions

from .conftest import pid_alive, starts, wait_for


async def test_starts_the_daemon_with_the_generated_config(make_supervisor, record, tmp_path):
    sup = make_supervisor(options={"mock": True})
    await sup.start()
    try:
        await wait_for(lambda: len(starts(record)) == 1)
        (start,) = starts(record)
        assert sup.state == RUNNING and sup.pid == start["pid"]
        assert start["argv"] == ["--config", str(tmp_path / "run" / "config.toml")]
        assert tomllib.loads(start["config"])["static_device"] == [{"url": "mock:?serial=911111"}]
        assert start["rust_log"] == "warn"
        snap = sup.snapshot()
        assert snap["daemon_process"] == RUNNING and snap["daemon_restarts"] == 0
        assert snap["daemon_pid"] == start["pid"] and snap["daemon_uptime_s"] >= 0
    finally:
        await sup.stop()
    assert sup.state == STOPPED and sup.pid is None
    assert not pid_alive(start["pid"])
    assert not (tmp_path / "run" / "config.toml").exists()


async def test_an_explicit_rust_log_is_kept(make_supervisor, record):
    sup = make_supervisor()
    sup.env = {**sup.env, "RUST_LOG": "debug"}
    await sup.start()
    try:
        await wait_for(lambda: starts(record))
        assert starts(record)[0]["rust_log"] == "debug"
    finally:
        await sup.stop()


async def test_restarts_with_exponential_backoff_up_to_the_cap(make_supervisor, record, caplog):
    sup = make_supervisor("exit:3", backoff_min_s=0.1, backoff_max_s=0.4, crash_limit=100)
    with caplog.at_level(logging.WARNING, "app.daemon.embedded"):
        await sup.start()
        try:
            await wait_for(lambda: len(starts(record)) >= 6, timeout=10)
        finally:
            await sup.stop()
    times = [s["t"] for s in starts(record)]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    # 0.1, 0.2, 0.4, 0.4, 0.4 plus process start-up time
    for gap, backoff in zip(gaps, [0.1, 0.2, 0.4, 0.4, 0.4], strict=False):
        assert backoff <= gap < backoff + 1.0
    assert sup.restarts >= 5
    # stop() may have SIGTERMed the last run (last_exit is then -15); last_error keeps the crash reason
    assert sup.last_error == "minidspd exited (exit code 3)"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "minidspd exited (exit code 3); restarting in 0.1 s" in warnings[0]


async def test_a_killed_daemon_is_restarted_quickly(make_supervisor, record):
    sup = make_supervisor(backoff_min_s=0.2)
    await sup.start()
    try:
        await wait_for(lambda: len(starts(record)) == 1)
        first = sup.pid
        killed_at = time.monotonic()
        os.kill(first, signal.SIGKILL)
        await wait_for(lambda: sup.state == RESTARTING)
        assert sup.snapshot()["daemon_last_exit"] == "signal SIGKILL"
        await wait_for(lambda: len(starts(record)) == 2 and sup.state == RUNNING)
        assert time.monotonic() - killed_at < 2.0
        assert sup.restarts == 1 and sup.pid != first
        assert sup.last_error == "minidspd exited (signal SIGKILL)"
    finally:
        await sup.stop()


async def test_crash_loop_reports_failed_and_logs_one_error(make_supervisor, record, caplog):
    sup = make_supervisor("exit:1", backoff_min_s=0.02, backoff_max_s=0.02, crash_limit=3, crash_window_s=60)
    with caplog.at_level(logging.WARNING, "app.daemon.embedded"):
        await sup.start()
        try:
            await wait_for(lambda: len(starts(record)) >= 6, timeout=10)
            assert sup.state == FAILED
            assert sup.snapshot()["daemon_process_error"] == "minidspd exited (exit code 1)"
        finally:
            await sup.stop()
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "failed 3 times in 60 s" in errors[0]


async def test_crash_loop_state_clears_once_a_run_lasts(make_supervisor):
    sup = make_supervisor(crash_limit=2, crash_window_s=60, stable_after_s=0.2)
    await sup.start()
    try:
        sup._crashes.extend([time.monotonic()] * 2)  # as if it had just crash-looped
        assert sup.state == FAILED
        await wait_for(lambda: sup.state == RUNNING, timeout=2)
    finally:
        await sup.stop()


async def test_crashes_outside_the_window_are_forgotten(make_supervisor):
    sup = make_supervisor(crash_limit=2, crash_window_s=0.1, stable_after_s=60)
    await sup.start()
    try:
        sup._crashes.extend([time.monotonic()] * 2)
        assert sup.state == FAILED
        await asyncio.sleep(0.15)
        assert sup.state == RUNNING
    finally:
        await sup.stop()


async def test_a_stable_run_resets_the_backoff(make_supervisor, record):
    # Each run lasts 0.3 s, longer than stable_after_s, so every restart waits the minimum again.
    sup = make_supervisor("sleep-exit:0.3", backoff_min_s=0.05, backoff_max_s=2.0, stable_after_s=0.2, crash_limit=100)
    await sup.start()
    try:
        await wait_for(lambda: len(starts(record)) >= 6, timeout=10)
    finally:
        await sup.stop()
    times = [s["t"] for s in starts(record)][:6]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    # Without the reset the backoffs would be 0.05, 0.1, 0.2, 0.4, 0.8: a spread of 0.75 s.
    assert max(gaps) - min(gaps) < 0.25


async def test_stop_sends_sigterm_then_sigkill(make_supervisor, record, caplog):
    sup = make_supervisor("ignore-term", stop_timeout_s=0.3)
    await sup.start()
    await wait_for(lambda: len(starts(record)) == 1)
    await asyncio.sleep(0.2)  # let the script install its SIGTERM handler
    pid = sup.pid
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, "app.daemon.embedded"):
        await sup.stop()
    assert 0.3 <= time.monotonic() - began < 2.0
    assert not pid_alive(pid)
    assert sup.state == STOPPED
    assert any("ignored SIGTERM" in r.getMessage() for r in caplog.records)
    assert len(starts(record)) == 1  # no restart after a requested stop


async def test_stop_is_graceful_and_idempotent(make_supervisor, record):
    sup = make_supervisor()
    await sup.start()
    await wait_for(lambda: len(starts(record)) == 1)
    began = time.monotonic()
    await sup.stop()
    await sup.stop()
    assert time.monotonic() - began < 1.0
    assert sup.state == STOPPED and sup.last_exit == -signal.SIGTERM


async def test_stop_during_backoff_does_not_respawn(make_supervisor, record):
    sup = make_supervisor("exit:2", backoff_min_s=5.0, backoff_max_s=5.0)
    await sup.start()
    await wait_for(lambda: sup.state == RESTARTING)
    began = time.monotonic()
    await sup.stop()
    assert time.monotonic() - began < 1.0
    assert len(starts(record)) == 1 and sup.restarts == 0


async def test_missing_binary_is_failed_and_retried(tmp_path):
    sup = DaemonSupervisor(
        EmbeddedOptions(binary=str(tmp_path / "nope"), config_dir=str(tmp_path / "run")),
        backoff_min_s=0.02,
        backoff_max_s=0.05,
    )
    await sup.start()
    try:
        assert sup.state == FAILED
        assert sup.last_error.startswith(f"cannot run {tmp_path / 'nope'}: ")
        await wait_for(lambda: sup.restarts >= 2)
        assert sup.state == FAILED
    finally:
        await sup.stop()


async def test_bad_extra_config_fails_without_spawning(make_supervisor, record, caplog):
    sup = make_supervisor(options={"extra_config": "[broken"})
    with caplog.at_level(logging.ERROR, "app.daemon.embedded"):
        await sup.start()
    assert sup.state == FAILED and "not valid TOML" in sup.last_error
    assert starts(record) == []
    assert any("not started" in r.getMessage() for r in caplog.records)
    await sup.stop()
    assert sup.state == STOPPED


async def test_non_table_http_server_in_extra_config_fails_without_raising(make_supervisor, record):
    sup = make_supervisor(options={"extra_config": "http_server = 1"})
    await sup.start()  # must not take the app's startup down with it
    assert sup.state == FAILED and "must be a table" in sup.last_error
    assert starts(record) == []
    await sup.stop()


async def test_listener_errors_are_reported_while_the_process_runs(make_supervisor, record):
    sup = make_supervisor(stderr="HTTP/TCP listener error: Address already in use (os error 98)")
    await sup.start()
    try:
        error = "minidspd: HTTP/TCP listener error: Address already in use (os error 98)"
        await wait_for(lambda: sup.snapshot()["daemon_process_error"] is not None)
        assert sup.state == RUNNING
        assert sup.snapshot()["daemon_process_error"] == error
    finally:
        await sup.stop()


async def test_a_healthy_respawn_reports_no_process_error(make_supervisor, record):
    sup = make_supervisor(backoff_min_s=0.05)
    await sup.start()
    try:
        await wait_for(lambda: len(starts(record)) == 1)
        os.kill(sup.pid, signal.SIGKILL)
        await wait_for(lambda: len(starts(record)) == 2 and sup.state == RUNNING)
        snap = sup.snapshot()
        assert snap["daemon_last_exit"] == "signal SIGKILL"  # the history stays visible
        assert snap["daemon_process_error"] is None  # but the running daemon has no error
    finally:
        await sup.stop()


async def test_daemon_stderr_is_forwarded_at_warning_and_up_only(make_supervisor, record, caplog):
    stderr = (
        "[2026-10-17T12:00:00Z WARN  minidspd::device_manager] device went away"
        "|[2026-10-17T12:00:00Z INFO  minidspd] chatty"
        "|[2026-10-17T12:00:00Z ERROR minidspd::http] broken"
    )
    sup = make_supervisor(stderr=stderr)
    with caplog.at_level(logging.INFO):
        await sup.start()
        try:
            await wait_for(lambda: len([r for r in caplog.records if r.name == "minidspd"]) >= 2)
            await asyncio.sleep(0.1)
        finally:
            await sup.stop()
    forwarded = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "minidspd"]
    assert forwarded == [
        (logging.WARNING, "minidspd::device_manager: device went away"),
        (logging.ERROR, "minidspd::http: broken"),
    ]  # the INFO line and stdout's "App is running on" stay at debug


async def test_falls_back_to_a_temp_dir_and_removes_it(make_supervisor, record, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    sup = make_supervisor(options={"config_dir": str(blocker / "minidsp")})
    await sup.start()
    try:
        await wait_for(lambda: starts(record))
        config = sup.config_path
        assert config.exists() and config.parent != blocker
    finally:
        await sup.stop()
    assert not config.parent.exists()
