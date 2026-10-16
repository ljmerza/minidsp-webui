from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_manager, get_session
from app.sync.session import DeviceSession, SessionManager

router = APIRouter(tags=["devices"])


@router.get("/devices")
async def list_devices(manager: Annotated[SessionManager, Depends(get_manager)]) -> list[dict]:
    """Known devices with live info. Devices the app doesn't support are listed with ``supported: false``."""
    devices = [{**s.info(), "supported": True} for s in manager]
    devices.extend(
        {
            "serial": d.serial,
            "product_name": d.product_name,
            "url": d.url,
            "supported": False,
        }
        for d in manager.unsupported
    )
    return devices


@router.get("/devices/{serial}")
async def get_device(session: Annotated[DeviceSession, Depends(get_session)]) -> dict:
    return {**session.info(), "supported": True}
