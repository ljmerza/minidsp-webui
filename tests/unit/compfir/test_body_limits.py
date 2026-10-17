"""Upload/import bodies are capped while they stream in, before Starlette spools or parses them."""

import httpx
import pytest
from fastapi import FastAPI

from app.api.limits import JSON_BODY_LIMIT, MULTIPART_SLACK, BodyLimitMiddleware
from app.api.v1 import compressor_fir, presets, rew
from app.formats.wav import write_wav
from app.sync.fir import MAX_UPLOAD_BYTES

from ..conftest import SERIAL
from .test_fir_api import BASE, TAPS


@pytest.fixture
async def limited(manager, settings, repo):
    app = FastAPI()
    app.state.manager = manager
    app.state.settings = settings
    app.state.repo = repo
    for module in (presets, compressor_fir, rew):
        app.include_router(module.router, prefix="/api/v1")
    app.add_middleware(BodyLimitMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        await http.put("/api/v1/fir/capability", json={"enabled": True})
        yield http


async def test_a_declared_oversize_upload_is_refused_unread(limited, repo):
    big = b"\0" * (MAX_UPLOAD_BYTES + MULTIPART_SLACK + 1)
    response = await limited.post(BASE, files={"file": ("big.wav", big, "audio/wav")})
    assert response.status_code == 413 and response.json()["error"] == "too_large"
    assert await repo.list_fir_blobs() == []


async def test_a_streamed_oversize_upload_is_cut_off(limited, repo):
    chunk = b"\0" * 65536
    sent = 0

    async def body():  # a valid multipart file part of 4 MiB, without a Content-Length
        nonlocal sent
        yield b'--xyz\r\nContent-Disposition: form-data; name="file"; filename="big.wav"\r\n\r\n'
        for _ in range(64):
            sent += 1
            yield chunk
        yield b"\r\n--xyz--\r\n"

    headers = {"Content-Type": "multipart/form-data; boundary=xyz"}
    response = await limited.post(BASE, content=body(), headers=headers)
    assert response.status_code == 413
    assert sent < 64  # stopped reading early
    assert await repo.list_fir_blobs() == []


async def test_normal_uploads_and_imports_pass(limited):
    ok = await limited.post(BASE, files={"file": ("ok.wav", write_wav(TAPS), "audio/wav")})
    assert ok.status_code == 200, ok.text
    assert (await limited.post("/api/v1/import/rew", json={"text": "biquad1,"})).status_code == 200


async def test_rew_json_is_capped(limited):
    text = "x" * (JSON_BODY_LIMIT + 1)
    url = f"/api/v1/devices/{SERIAL}/presets/0/import/rew"
    for target in ("/api/v1/import/rew", url):
        response = await limited.post(target, json={"text": text, "bank": "outputs.0.peq"})
        assert response.status_code == 413


def test_profile_import_limits_pick_the_right_route():
    middleware = BodyLimitMiddleware(app=None)

    def limit(path: str) -> int | None:
        return middleware.limit_for({"type": "http", "method": "POST", "path": path})

    assert limit("/api/v1/profiles/import") == JSON_BODY_LIMIT
    assert limit("/ui/profiles/import") == (1 << 20) + MULTIPART_SLACK  # the multipart form needs the slack
    assert limit("/api/v1/profiles") is None
