"""App health (docs/03 §10 failure modes) and the daily housekeeping job.

`health_report` is what ``GET /api/v1/health`` returns: the overall ``status`` is ``ok``, ``degraded`` (with a
``problems`` list a person can read) or ``starting`` (the first daemon check hasn't finished). It never contacts the
daemon itself; it reads what discovery, the monitors, the writers and the persisters already know. Degraded never
means "restart me": the app keeps retrying on its own, which is why Docker's liveness probe (``/healthz``) stays
separate.

`run_retention` / `maintenance_loop` keep the database bounded on an SD card (docs/06 retention): the event log is
trimmed to its newest rows, revision history to the newest rows per preset, and the WAL is checkpointed.
"""

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from app import __version__
from app.state import Repo
from app.state.repo import EVENT_RETENTION, REVISION_RETENTION
from app.sync.session import DeviceSession, SessionManager, SessionState

log = logging.getLogger(__name__)

OK = "ok"
DEGRADED = "degraded"
STARTING = "starting"

MAINTENANCE_INTERVAL_S = 24 * 3600
MAINTENANCE_FIRST_DELAY_S = 60.0

STORAGE_MEMORY = (
    "The state database could not be opened: preset state and history are kept in memory only and are lost on restart."
)
STORAGE_FAILING = (
    "Saving state to the database is failing (disk full or database locked?). Changes are kept in memory and retried."
)

_STATE_TEXT = {
    SessionState.ABSENT: "not found on minidspd (unplugged?)",
    SessionState.DAEMON_DOWN: "unreachable because minidspd is down",
    SessionState.STARTING: "starting",
}


def persist_failing(session: DeviceSession) -> bool:
    """Whether the session's debounced state writes are currently failing (values stay pending and are retried)."""
    persister = getattr(session, "_persister", None)  # owned by the session; read-only use here
    if persister is None:
        return False
    return any(persister.failures(key) > 0 for key in persister.pending_keys)


def storage_problem(manager: SessionManager, sessions: Iterable[DeviceSession] | None = None) -> str | None:
    """A banner-ready sentence when state isn't being saved, else None."""
    if manager.repo is None:
        return STORAGE_MEMORY
    sessions = list(manager) if sessions is None else sessions
    if any(persist_failing(s) for s in sessions):
        return STORAGE_FAILING
    return None


def _active_preset(session: DeviceSession) -> int | None:
    master = session.master
    preset = master.preset if master is not None else None
    if preset is None or not 0 <= preset < session.profile.n_presets:
        return None
    return preset


def device_health(session: DeviceSession) -> tuple[dict[str, Any], list[str]]:
    """Report for one device and the problems it contributes."""
    serial = session.serial
    state = SessionState(session.state)
    report: dict[str, Any] = {
        "serial": serial,
        "state": str(state),
        "monitor_connected": session.monitor.connected,
        "preset_switching": session.preset_switching,
        "state_loaded": session.loaded,
        "persist_failing": persist_failing(session),
        "sync": None,
    }
    problems: list[str] = []
    if state != SessionState.CONNECTED:
        problems.append(f"device {serial}: {_STATE_TEXT.get(state, str(state))}")
    elif not session.monitor.connected:
        problems.append(f"device {serial}: live status stream is reconnecting")
    if session.load_error:
        problems.append(f"device {serial}: stored preset state could not be loaded ({session.load_error})")
    preset = _active_preset(session)
    if session.loaded and preset is not None:
        summary = session.sync_summary(preset)
        report["sync"] = {"preset": preset, **summary["counts"], "error": summary["error"]}
        failed = summary["counts"].get("failed", 0)
        if failed:
            problems.append(
                f"device {serial}: {failed} setting(s) of preset {preset + 1} could not be written "
                f"({summary['error'] or 'write failed'})"
            )
    return report, problems


def health_report(manager: SessionManager) -> dict[str, Any]:
    """Overall health. Keeps the keys of the Phase 1 summary (app, version, daemon, daemon_error, device, state,
    devices) and adds ``status``, ``problems`` and ``storage``."""
    sessions = list(manager)
    problems: list[str] = []
    if manager.daemon_up is False:
        problems.append(f"minidspd unreachable: {manager.last_error or 'no answer'}")
    elif manager.daemon_up and not sessions:
        unsupported = [d.product_name or "unknown" for d in manager.unsupported]
        problems.append(
            "no supported device on minidspd" + (f" (unsupported: {', '.join(unsupported)})" if unsupported else "")
        )
    storage = storage_problem(manager, sessions)
    if storage:
        problems.append(storage)
    devices = []
    for session in sessions:
        report, device_problems = device_health(session)
        devices.append(report)
        if manager.daemon_up is not False:  # with the daemon down every device is down too: one problem is enough
            problems.extend(device_problems)

    if problems:
        status = DEGRADED
    elif manager.daemon_up is None:
        status = STARTING
    else:
        status = OK
    return {
        "status": status,
        "problems": problems,
        "app": "ok",
        "version": __version__,
        "daemon": {None: "unknown", True: "up", False: "down"}[manager.daemon_up],
        "daemon_error": manager.last_error,
        "device": "connected" if any(s.state == SessionState.CONNECTED for s in sessions) else "absent",
        # "memory": the state DB could not be opened, so preset state and history are lost on restart
        "state": "persistent" if manager.repo is not None else "memory",
        "storage": "memory" if manager.repo is None else ("failing" if storage else "ok"),
        "devices": devices,
    }


# --- housekeeping -----------------------------------------------------------------------------------------------------


async def trim_revisions(repo: Repo, keep: int = REVISION_RETENTION) -> int:
    """Delete all but the newest ``keep`` revisions of every preset. Commits already trim their own preset; this
    catches presets that haven't been committed to since the limit was lowered or a backup was imported."""
    if keep < 1:
        raise ValueError("keep must be >= 1")
    res = await repo.db.execute(
        "DELETE FROM revision WHERE id IN ("
        " SELECT id FROM ("
        "  SELECT id, ROW_NUMBER() OVER (PARTITION BY serial, preset ORDER BY revision DESC) AS rn FROM revision"
        " ) WHERE rn > ?)",
        (keep,),
    )
    return res.rowcount


async def run_retention(
    repo: Repo, *, events: int = EVENT_RETENTION, revisions: int = REVISION_RETENTION
) -> dict[str, int]:
    """One retention pass. Each step is independent, so one failing doesn't skip the others. Returns rows deleted."""
    deleted = {"events": 0, "revisions": 0}
    try:
        deleted["events"] = await repo.trim_events(events)
    except Exception:
        log.exception("event log trim failed")
    try:
        deleted["revisions"] = await trim_revisions(repo, revisions)
    except Exception:
        log.exception("revision history trim failed")
    try:
        # Fold the WAL back into the database file and truncate it, so it doesn't sit at its high-water mark.
        await repo.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        log.exception("WAL checkpoint failed")
    if any(deleted.values()):
        log.info("retention: deleted %d event(s) and %d revision(s)", deleted["events"], deleted["revisions"])
    return deleted


async def maintenance_loop(
    repo: Repo,
    *,
    interval_s: float = MAINTENANCE_INTERVAL_S,
    first_delay_s: float = MAINTENANCE_FIRST_DELAY_S,
) -> None:
    """Run `run_retention` shortly after startup, then every ``interval_s``. Runs until cancelled."""
    await asyncio.sleep(first_delay_s)
    while True:
        try:
            await run_retention(repo)
        except Exception:
            log.exception("maintenance failed")
        await asyncio.sleep(interval_s)
