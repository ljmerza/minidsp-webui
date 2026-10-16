"""Map service-layer exceptions to HTTP responses."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.daemon.client import DaemonRejected, DaemonUnavailable, DeviceNotFound
from app.sync.session import ConfirmationRequired, MasterValidationError, PresetSwitchInProgress, PresetSwitchTimeout


def _json(status: int, detail: str, **extra: object) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


async def _validation(_: Request, exc: Exception) -> JSONResponse:
    return _json(422, str(exc))


async def _confirmation(_: Request, exc: Exception) -> JSONResponse:
    return _json(409, str(exc), error="confirmation_required")


async def _switching(_: Request, exc: Exception) -> JSONResponse:
    return _json(409, str(exc))


async def _switch_timeout(_: Request, exc: Exception) -> JSONResponse:
    return _json(504, str(exc))


async def _unavailable(_: Request, exc: Exception) -> JSONResponse:
    return _json(503, str(exc), error="daemon_unavailable")


async def _not_found(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DeviceNotFound)
    return _json(503, str(exc), error="device_absent", daemon_body=exc.body)


async def _rejected(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DaemonRejected)
    return _json(
        502,
        str(exc),
        error="daemon_rejected",
        daemon_status=exc.status_code,
        daemon_error=exc.error_type,
        daemon_body=exc.body,
    )


async def _not_implemented(_: Request, exc: Exception) -> JSONResponse:
    return _json(501, str(exc) or "not implemented yet")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(MasterValidationError, _validation)
    app.add_exception_handler(ConfirmationRequired, _confirmation)
    app.add_exception_handler(PresetSwitchInProgress, _switching)
    app.add_exception_handler(PresetSwitchTimeout, _switch_timeout)
    app.add_exception_handler(DaemonUnavailable, _unavailable)
    app.add_exception_handler(DeviceNotFound, _not_found)
    app.add_exception_handler(DaemonRejected, _rejected)
    app.add_exception_handler(NotImplementedError, _not_implemented)
