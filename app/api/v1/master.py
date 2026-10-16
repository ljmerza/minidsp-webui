from typing import Annotated, Literal

from fastapi import APIRouter, Depends

from app.api.deps import get_session
from app.model.master import MasterStatus, MasterUpdate
from app.sync.session import DeviceSession

router = APIRouter(tags=["master"])

Session = Annotated[DeviceSession, Depends(get_session)]


@router.put("/devices/{serial}/master")
async def put_master(update: MasterUpdate, session: Session) -> MasterStatus:
    """Change volume/mute/source/preset. A preset change waits until the device reports it (up to 15 s).

    Raising the volume by more than the confirm step (``GAIN_STEP_CONFIRM_DB``) in one request answers 409
    ``confirmation_required`` unless ``confirm`` is true.
    """
    return await session.set_master(update)


@router.post("/devices/{serial}/master/volume/{direction}")
async def volume_step(direction: Literal["up", "down"], session: Session) -> MasterStatus:
    """±0.5 dB, clamped to the master range."""
    return await session.volume_step(1 if direction == "up" else -1)


@router.post("/devices/{serial}/master/mute/toggle")
async def toggle_mute(session: Session) -> MasterStatus:
    return await session.toggle_mute()
