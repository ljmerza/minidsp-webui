import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__, web
from app.api import v1
from app.api.errors import register_exception_handlers
from app.bus import Bus
from app.config import Settings, get_settings
from app.daemon.client import DaemonClient
from app.sync.session import SessionManager

STATIC_DIR = Path(__file__).parent / "static"


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
        manager = SessionManager(daemon, bus, settings)
        app.state.bus = bus
        app.state.client = daemon
        app.state.manager = manager
        manager.start()  # discovery retries in the background, so the app serves even when the daemon is down
        try:
            yield
        finally:
            await manager.stop()
            if client is None:
                await daemon.aclose()

    app = FastAPI(title="minidsp-webui", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    register_exception_handlers(app)
    app.include_router(v1.build_router())
    app.include_router(web.router)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness probe for Docker: the app process is up (daemon state is in /api/v1/health)."""
        return {"status": "ok", "version": __version__}

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
