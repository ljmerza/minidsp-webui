"""Request body limits for the upload and import routes, enforced while the body streams in.

A size check inside the route comes too late: Starlette spools multipart file parts to a temporary file with no size
cap, and FastAPI reads a JSON body whole before validating it, so a multi-GB POST would fill the disk (or memory)
before the route could refuse it. This middleware answers 413 as soon as the declared or received size passes the
route's limit.
"""

import re
from collections.abc import Sequence

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.sync.fir import MAX_UPLOAD_BYTES

MULTIPART_SLACK = 64 * 1024  # boundaries, part headers and the file name around the WAV
JSON_BODY_LIMIT = 1 << 20  # a 100k-character REW text is at most ~600 kB as JSON (\uXXXX escapes)

BODY_LIMITS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"/devices/[^/]+/presets/[^/]+/outputs/[^/]+/fir$"), MAX_UPLOAD_BYTES + MULTIPART_SLACK),
    (re.compile(r"/import/rew$"), JSON_BODY_LIMIT),
)


class BodyTooLarge(HTTPException):
    """Raised from ``receive``; FastAPI passes HTTP exceptions from body parsing through unchanged."""

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"request body too large (at most {limit} bytes)")


def _too_large(limit: int) -> JSONResponse:
    exc = BodyTooLarge(limit)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "error": "too_large"})


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, limits: Sequence[tuple[re.Pattern[str], int]] = BODY_LIMITS) -> None:
        self.app = app
        self.limits = limits

    def limit_for(self, scope: Scope) -> int | None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return None
        return next((limit for pattern, limit in self.limits if pattern.search(scope["path"])), None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self.limit_for(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length", b"")
        if length.isdigit() and int(length) > limit:
            await _too_large(limit)(scope, receive, send)
            return

        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await _too_large(limit)(scope, receive, send)
