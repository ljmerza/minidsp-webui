from typing import Annotated

from fastapi import APIRouter, Depends

from app import __version__
from app.api.deps import get_manager
from app.sync.session import SessionManager, SessionState

router = APIRouter(tags=["health"])


def health_summary(manager: SessionManager) -> dict:
    daemon = {None: "unknown", True: "up", False: "down"}[manager.daemon_up]
    sessions = list(manager)
    connected = any(s.state == SessionState.CONNECTED for s in sessions)
    return {
        "app": "ok",
        "version": __version__,
        "daemon": daemon,
        "daemon_error": manager.last_error,
        "device": "connected" if connected else "absent",
        # "memory": the state DB could not be opened, so preset state and history are lost on restart
        "state": "persistent" if manager.repo is not None else "memory",
        "devices": [
            {"serial": s.serial, "state": str(s.state), "monitor_connected": s.monitor.connected} for s in sessions
        ],
    }


@router.get("/health")
async def health(manager: Annotated[SessionManager, Depends(get_manager)]) -> dict:
    """App, daemon and device state."""
    return health_summary(manager)
