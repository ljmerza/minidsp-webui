"""Every body-carrying route of the real app is capped before Starlette spools or parses the body."""

import tempfile

import pytest
from starlette import formparsers

from app.api.limits import JSON_BODY_LIMIT, BodyLimitMiddleware

from .conftest import SERIAL
from .test_app_wiring import running

HX = {"HX-Request": "true"}
ADOPT_UI = f"/ui/devices/{SERIAL}/adopt"


@pytest.fixture
def rollovers(monkeypatch):
    """Records every multipart file part Starlette moved from memory to a temp file on disk (the SD card)."""
    seen: list[bool] = []

    class Spy(tempfile.SpooledTemporaryFile):
        def rollover(self):
            seen.append(True)
            super().rollover()

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Spy)
    return seen


@pytest.mark.parametrize("path", [f"{ADOPT_UI}/preview", ADOPT_UI])
async def test_adopt_upload_over_the_cap_is_refused_before_it_is_spooled(settings, client, daemon, rollovers, path):
    big = b"output 0 gain 0\n" * (4 << 16)  # 4 MiB
    async with running(settings, client, daemon) as (_, http):
        response = await http.post(path, files={"script_file": ("big.txt", big, "text/plain")}, headers=HX)
    assert response.status_code == 413 and response.json()["error"] == "too_large"
    assert rollovers == []


async def test_adopt_upload_within_the_cap_still_previews(settings, client, daemon, rollovers):
    async with running(settings, client, daemon) as (_, http):
        files = {"script_file": ("ok.txt", b"output 0 gain -3\n", "text/plain")}
        response = await http.post(f"{ADOPT_UI}/preview", files=files, data={"source": "script"}, headers=HX)
    assert response.status_code == 200, response.text
    assert rollovers == []


def test_multipart_caps_fit_in_starlettes_memory_spool():
    """A capped body larger than the spool would still reach a temp file on disk before the cap is hit."""
    middleware = BodyLimitMiddleware(app=None)
    spool = formparsers.MultiPartParser.spool_max_size
    for path in (
        f"/api/v1/devices/{SERIAL}/presets/0/outputs/2/fir",
        "/ui/profiles/import",
        f"{ADOPT_UI}/preview",
        ADOPT_UI,
    ):
        limit = middleware.limit_for({"type": "http", "method": "POST", "path": path})
        assert limit is not None and limit <= spool, path


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/v1/import/cli-script", "text"),
        (f"/api/v1/devices/{SERIAL}/presets/0/import/cli-script", "text"),
        ("/api/v1/profiles/import/cli-script", "text"),
        (f"/api/v1/devices/{SERIAL}/adopt", "script"),
    ],
)
async def test_oversized_json_scripts_are_refused_unread(settings, client, daemon, path, body):
    """Unbounded, these were read whole, validated, then echoed back in full in the 422."""
    payload = {body: "x" * (4 << 20), "mode": "trust", "name": "big"}
    async with running(settings, client, daemon) as (_, http):
        response = await http.post(path, json=payload)
    assert response.status_code == 413 and response.json()["error"] == "too_large"
    assert len(response.content) < 1000


async def test_a_full_length_script_still_fits(settings, client, daemon):
    from app.api.v1.adopt import MAX_SCRIPT_CHARS

    text = ("# " + "é" * 98 + "\n") * (MAX_SCRIPT_CHARS // 101)  # é escapes: about 6 bytes per character
    async with running(settings, client, daemon) as (_, http):
        response = await http.post("/api/v1/import/cli-script", json={"text": text})
    assert response.status_code == 200, response.text[:200]


async def test_unlisted_routes_get_a_default_cap(settings, client, daemon):
    async with running(settings, client, daemon) as (_, http):
        big = {"name": "x" * (JSON_BODY_LIMIT + 1)}
        response = await http.patch(f"/api/v1/devices/{SERIAL}/presets/0", json=big)
        assert response.status_code == 413
        # The backup routes stream and cap their own (larger) bodies
        response = await http.post("/api/v1/backup/import/preview", content=b"{" + b" " * (JSON_BODY_LIMIT + 1))
        assert response.status_code != 413
