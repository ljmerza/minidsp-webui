"""Request body limits, enforced while the body streams in.

A size check inside the route comes too late: Starlette spools multipart file parts to a temporary file with no size
cap, and FastAPI reads a JSON body whole before validating it, so a multi-GB POST would fill the disk (or memory)
before the route could refuse it. This middleware answers 413 as soon as the declared or received size passes the
route's limit. Routes not listed get ``DEFAULT_BODY_LIMIT``; the backup routes stream and cap their own bodies.

Multipart limits stay within Starlette's in-memory spool (1 MiB), so an accepted upload never touches the SD card.
"""

import re
from collections.abc import Sequence

from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartParser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.v1.adopt import MAX_SCRIPT_CHARS
from app.sync.fir import MAX_UPLOAD_BYTES
from app.web.sync_ui import MAX_SCRIPT_BYTES

MULTIPART_SLACK = 64 * 1024  # boundaries, part headers and the file name around the WAV
# Larger file parts roll over to a temp file on disk; a body within this can't hold one.
MULTIPART_MEMORY_LIMIT = MultiPartParser.spool_max_size
JSON_BODY_LIMIT = 1 << 20  # a 100k-character REW text is at most ~600 kB as JSON (\uXXXX escapes)
# A full-length script even if every character is sent as a \uXXXX escape, plus the request's other fields
SCRIPT_JSON_LIMIT = 6 * MAX_SCRIPT_CHARS + MULTIPART_SLACK
DEFAULT_BODY_LIMIT = JSON_BODY_LIMIT
SELF_LIMITED = None  # the route streams the body and enforces its own limit

BODY_LIMITS: tuple[tuple[re.Pattern[str], int | None], ...] = (
    (
        re.compile(r"/devices/[^/]+/presets/[^/]+/outputs/[^/]+/fir$"),
        min(MAX_UPLOAD_BYTES + MULTIPART_SLACK, MULTIPART_MEMORY_LIMIT),
    ),
    (re.compile(r"/import/rew$"), JSON_BODY_LIMIT),
    # The first match wins, so each multipart UI upload comes before the JSON API route its pattern also matches.
    (re.compile(r"/ui/profiles/import$"), min((1 << 20) + MULTIPART_SLACK, MULTIPART_MEMORY_LIMIT)),
    (re.compile(r"/profiles/import$"), JSON_BODY_LIMIT),
    # The adopt wizard's form: an uploaded script file and the text area, each up to MAX_SCRIPT_BYTES
    (re.compile(r"/ui/devices/[^/]+/adopt(/preview)?$"), 2 * MAX_SCRIPT_BYTES + MULTIPART_SLACK),
    (re.compile(r"/import/cli-script$"), SCRIPT_JSON_LIMIT),
    (re.compile(r"/devices/[^/]+/adopt$"), SCRIPT_JSON_LIMIT),
    (re.compile(r"/api/v1/backup/"), SELF_LIMITED),
)


class BodyTooLarge(HTTPException):
    """Raised from ``receive``; FastAPI passes HTTP exceptions from body parsing through unchanged."""

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"request body too large (at most {limit} bytes)")


def _too_large(limit: int) -> JSONResponse:
    exc = BodyTooLarge(limit)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "error": "too_large"})


class BodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        limits: Sequence[tuple[re.Pattern[str], int | None]] = BODY_LIMITS,
        default: int | None = DEFAULT_BODY_LIMIT,
    ) -> None:
        self.app = app
        self.limits = limits
        self.default = default

    def limit_for(self, scope: Scope) -> int | None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return None
        return next((limit for pattern, limit in self.limits if pattern.search(scope["path"])), self.default)

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
