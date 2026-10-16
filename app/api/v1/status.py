from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_session
from app.sync.session import DeviceSession

router = APIRouter(tags=["status"])


def status_payload(session: DeviceSession) -> dict:
    status = session.status
    age = session.monitor.status_age()
    return {
        "serial": session.serial,
        "state": str(session.state),
        "monitor_connected": session.monitor.connected,
        "preset_switching": session.preset_switching,
        "master": status.master.model_dump() if status else None,
        "input_levels": status.input_levels if status else [],
        "output_levels": status.output_levels if status else [],
        "age_s": round(age, 3) if age is not None else None,
    }


@router.get("/devices/{serial}/status")
async def get_status(session: Annotated[DeviceSession, Depends(get_session)]) -> dict:
    """Master status and levels, cached from the monitor (no device round-trip)."""
    return status_payload(session)
