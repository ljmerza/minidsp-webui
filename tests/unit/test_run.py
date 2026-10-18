"""`python -m app.main`: logging defaults that respect the SD-card budget, and a single app build."""

import logging

import pytest

from app import main
from app.config import Settings


@pytest.fixture
def library_loggers():
    """``configure_logging`` sets the httpx/httpcore levels; put them back afterwards."""
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore", "minidspd")]
    levels = [lg.level for lg in loggers]
    yield
    for lg, level in zip(loggers, levels, strict=True):
        lg.setLevel(level)


def test_default_log_level_is_warning(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert Settings(_env_file=None).log_level == "warning"


@pytest.mark.parametrize("level", ["warning", "info"])
def test_no_per_request_lines_below_debug(level, library_loggers):
    assert main.configure_logging(level) is False  # uvicorn's access log stays off
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)  # one line per daemon request otherwise
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)


def test_debug_keeps_per_request_lines(library_loggers):
    assert main.configure_logging("debug") is True
    assert logging.getLogger("httpx").isEnabledFor(logging.INFO)


@pytest.mark.parametrize("rust_log", [None, "warn", "error"])
def test_daemon_output_stays_off_by_default(rust_log, monkeypatch, library_loggers):
    if rust_log is None:
        monkeypatch.delenv("RUST_LOG", raising=False)
    else:
        monkeypatch.setenv("RUST_LOG", rust_log)  # repeats of a warning go to debug: keep them off the SD card
    logging.getLogger("minidspd").setLevel(logging.NOTSET)
    main.configure_logging("warning")
    assert not logging.getLogger("minidspd").isEnabledFor(logging.INFO)


@pytest.mark.parametrize("rust_log", ["info", "debug", "minidspd=trace"])
def test_an_explicit_rust_log_shows_the_daemon_output(rust_log, monkeypatch, library_loggers):
    """The daemon's info/debug lines are forwarded at debug; RUST_LOG=info alone must be enough to see them."""
    monkeypatch.setenv("RUST_LOG", rust_log)
    logging.getLogger("minidspd").setLevel(logging.NOTSET)
    main.configure_logging("warning")
    assert logging.getLogger("minidspd").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("app").isEnabledFor(logging.INFO)  # the app's own logging stays at LOG_LEVEL


def test_run_serves_the_built_app(monkeypatch, library_loggers):
    import uvicorn

    calls = []
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda target, **kwargs: calls.append((target, kwargs)))
    monkeypatch.setattr(main, "get_settings", lambda: Settings(_env_file=None))
    main.run()
    [(target, kwargs)] = calls
    assert target is main.app  # not "app.main:app": that imports the module again and builds a second app
    assert kwargs["access_log"] is False and kwargs["log_level"] == "warning"
