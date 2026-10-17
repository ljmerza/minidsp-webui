from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.api.deps import get_manager
from app.sync.health import OK, health_report
from app.sync.session import SessionManager

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(
    manager: Annotated[SessionManager, Depends(get_manager)],
    strict: Annotated[bool, Query(description="Answer 503 unless the overall status is ok")] = False,
) -> JSONResponse:
    """App, daemon, device and storage state. ``status`` is ok, degraded (see ``problems``) or starting."""
    report = health_report(manager)
    return JSONResponse(report, status_code=503 if strict and report["status"] != OK else 200)
