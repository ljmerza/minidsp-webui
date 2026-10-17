"""JSON API router registry. To add a router: import its module here and append its ``router`` to ``ROUTERS``."""

from fastapi import APIRouter, Depends

from app.api.deps import require_same_origin
from app.api.v1 import (
    adopt,
    compressor_fir,
    devices,
    dsp,
    events,
    filters,
    health,
    import_export,
    master,
    presets,
    rew,
    status,
    system,
)

PREFIX = "/api/v1"

ROUTERS: list[APIRouter] = [
    health.router,
    devices.router,
    status.router,
    master.router,
    events.router,
    presets.router,
    adopt.router,
    import_export.router,
    filters.router,
    dsp.router,
    compressor_fir.router,
    rew.router,
    system.router,
]


def build_router() -> APIRouter:
    # Every state-changing route refuses cross-site browser requests (see require_same_origin).
    api = APIRouter(prefix=PREFIX, dependencies=[Depends(require_same_origin)])
    for router in ROUTERS:
        api.include_router(router)
    return api
