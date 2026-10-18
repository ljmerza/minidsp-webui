from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import get_manager
from app.daemon.embedded import health_fields, health_problems
from app.sync.health import DEGRADED, OK, STARTING, health_report
from app.sync.session import SessionManager

router = APIRouter(tags=["health"])


def full_health_report(app: FastAPI, manager: SessionManager) -> dict[str, Any]:
    """`health_report` plus the daemon-process and USB fields (docs/11): ``daemon_mode``, ``daemon_process``
    (running, restarting, failed, starting, stopped or external), ``daemon_restarts``, ``usb_device_visible``, ..."""
    report = health_report(manager)
    supervisor = getattr(app.state, "daemon_supervisor", None)
    settings = getattr(app.state, "settings", None)
    fields = health_fields(
        supervisor, getattr(app.state, "usb_probe", None), mock=bool(settings and settings.minidspd_mock)
    )
    report.update(fields)
    extra = health_problems(fields, device_connected=report["device"] == "connected")
    if extra:
        report["problems"] = [*extra, *report["problems"]]
        if report["status"] in (OK, STARTING):
            report["status"] = DEGRADED
    return report


@router.get("/health")
async def health(
    request: Request,
    manager: Annotated[SessionManager, Depends(get_manager)],
    strict: Annotated[bool, Query(description="Answer 503 unless the overall status is ok")] = False,
) -> JSONResponse:
    """App, daemon, device and storage state. ``status`` is ok, degraded (see ``problems``) or starting. In embedded
    mode it also reports the bundled minidspd process and whether the miniDSP is visible on USB."""
    report = full_health_report(request.app, manager)
    return JSONResponse(report, status_code=503 if strict and report["status"] != OK else 200)
