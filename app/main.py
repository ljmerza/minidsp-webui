import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__, web
from app.api import v1
from app.api.errors import register_exception_handlers
from app.api.limits import BodyLimitMiddleware
from app.api.v1 import verify as verify_api
from app.bus import Bus
from app.config import Settings, get_settings
from app.daemon.client import DaemonClient
from app.mqtt import create_bridge
from app.state import Database, Repo
from app.sync.backup import apply_staged_restore, start_nightly_backups
from app.sync.fir import restore_fir_capability
from app.sync.health import health_report, maintenance_loop
from app.sync.session import SessionManager
from app.sync.verify import parse_at, start_nightly_verify
from app.web import backup as backup_ui
from app.web import devices as devices_ui
from app.web import filters as filters_ui
from app.web import profiles as profiles_ui
from app.web import state_ui
from app.web import system as system_ui
from app.web.settings_ui import apply_stored_runtime_settings

STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger(__name__)


async def _open_db(path: str) -> Database | None:
    """Open the state DB. Without it the app still serves master controls; preset routes answer 503."""
    try:
        return await Database.open(path)
    except Exception:
        log.exception("cannot open the state database at %s; preset state will not be kept", path)
        return None


def create_app(settings: Settings | None = None, *, client: DaemonClient | None = None) -> FastAPI:
    """Build the app. ``client`` lets tests inject a daemon client (it is not closed on shutdown)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        bus = Bus()
        daemon = client or DaemonClient(
            settings.minidspd_url,
            timeout=settings.daemon_timeout_s,
            preset_timeout=settings.preset_switch_timeout_s,
        )
        try:
            await asyncio.to_thread(apply_staged_restore, settings.db_path)  # a restore staged on the Settings page
        except Exception:
            log.exception("staged database restore failed")
        db = await _open_db(settings.db_path)
        repo = Repo(db) if db else None
        if repo:
            await apply_stored_runtime_settings(repo, settings)  # values saved on the Settings page override the env
        manager = SessionManager(daemon, bus, settings, repo=repo)
        await restore_fir_capability(repo, settings, manager)  # the persisted experimental-FIR toggle
        mqtt = create_bridge(manager, bus, settings)
        app.state.bus = bus
        app.state.client = daemon
        app.state.db = db
        app.state.repo = repo
        app.state.manager = manager
        app.state.mqtt = mqtt
        manager.start()  # discovery retries in the background, so the app serves even when the daemon is down
        if mqtt:
            mqtt.start()
        maintenance = asyncio.create_task(maintenance_loop(repo), name="maintenance") if repo else None
        backups = start_nightly_backups(settings.db_path) if db else None
        verify_at = parse_at(settings.verify_at)
        verify_task = start_nightly_verify(manager, at=verify_at) if settings.verify_nightly else None
        try:
            yield
        finally:
            for task in (maintenance, backups, verify_task):
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            if mqtt:
                await mqtt.stop()  # publishes "offline" while the sessions still exist
            await manager.stop()  # commits pending edits and flushes each session's persister
            if db:
                await db.close()
            if client is None:
                await daemon.aclose()

    app = FastAPI(title="minidsp-webui", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    register_exception_handlers(app)
    app.add_middleware(BodyLimitMiddleware)  # caps upload/import bodies before they are spooled or parsed
    app.include_router(v1.build_router())
    app.include_router(web.router)
    app.include_router(state_ui.router)
    app.include_router(filters_ui.router)
    app.include_router(system_ui.router)
    app.include_router(profiles_ui.router)
    app.include_router(backup_ui.router)
    app.include_router(devices_ui.router)
    app.include_router(verify_api.ui_router)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness probe for Docker: always 200 while the process serves. ``health`` is the overall status (ok,
        degraded, starting); the details are in /api/v1/health. Degraded never means "restart me"."""
        manager = getattr(app.state, "manager", None)
        health = health_report(manager)["status"] if manager is not None else "starting"
        return {"status": "ok", "version": __version__, "health": health}

    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        "app.main:app",
        host=settings.webui_bind,
        port=settings.webui_port,
        log_level=settings.log_level,
        proxy_headers=True,
    )


if __name__ == "__main__":
    run()
