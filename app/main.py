from fastapi import FastAPI

from app import __version__

app = FastAPI(title="minidsp-webui", version=__version__)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe for Docker: the app process is up."""
    return {"status": "ok", "version": __version__}


def run() -> None:
    import uvicorn

    from app.config import settings

    uvicorn.run(
        "app.main:app",
        host=settings.webui_bind,
        port=settings.webui_port,
        log_level=settings.log_level,
        proxy_headers=True,
    )


if __name__ == "__main__":
    run()
