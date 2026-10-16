"""Server-sent events: the live feed for browsers (and anything else that speaks SSE).

Event names are bus topics: ``levels``, ``master``, ``device``, ``sync``, ``desired``, ``log``.
On connect the stream starts with a snapshot (``device`` + ``master`` per device) so clients can render immediately.
"""

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sse_starlette import EventSourceResponse

from app.api.deps import get_bus, get_manager
from app.bus import DEVICE, MASTER, Bus
from app.sync.session import SessionManager

router = APIRouter(tags=["events"])


def _sse(topic: str, data: dict[str, Any]) -> dict[str, str]:
    return {"event": topic, "data": json.dumps(data, separators=(",", ":"))}


async def event_stream(
    bus: Bus, manager: SessionManager, topics: set[str] | None = None, serial: int | None = None
) -> AsyncIterator[dict[str, str]]:
    sub = bus.subscribe(topics)
    try:
        for session in manager:
            if serial is not None and session.serial != serial:
                continue
            if topics is None or DEVICE in topics:
                yield _sse(DEVICE, session.device_event())
            if (topics is None or MASTER in topics) and session.master is not None:
                yield _sse(MASTER, {"serial": session.serial, "master": session.master.model_dump()})
        async for event in sub:
            if serial is not None and event.serial is not None and event.serial != serial:
                continue
            yield _sse(event.topic, event.data)
    finally:
        sub.close()


@router.get("/events")
async def events(
    bus: Annotated[Bus, Depends(get_bus)],
    manager: Annotated[SessionManager, Depends(get_manager)],
    topics: Annotated[str | None, Query(description="Comma-separated topics; default all")] = None,
    serial: Annotated[int | None, Query(description="Only events for this device")] = None,
) -> EventSourceResponse:
    wanted = {t.strip() for t in topics.split(",") if t.strip()} if topics else None
    return EventSourceResponse(
        event_stream(bus, manager, wanted, serial),
        ping=15,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
