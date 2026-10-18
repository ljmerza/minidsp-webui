"""422 replies say where and what was wrong (loc, msg, type) but never repeat what was sent: no pydantic ``input``,
no copy of the request body, no script text."""

import pytest

from app.api.v1.adopt import MAX_SCRIPT_CHARS

from .conftest import SERIAL
from .test_app_wiring import running

MARKER = "ECHO-MARKER-"


def _assert_not_echoed(response, *, limit: int = 4000) -> None:
    assert response.status_code == 422, response.text[:300]
    assert MARKER not in response.text
    assert len(response.content) < limit, len(response.content)


async def test_request_validation_errors_keep_loc_msg_type_only(settings, client, daemon):
    big = MARKER * ((MAX_SCRIPT_CHARS // len(MARKER)) + 10)  # over max_length, still under the body cap
    async with running(settings, client, daemon) as (_, http):
        response = await http.post("/api/v1/import/cli-script", json={"text": big, "extra": MARKER})
    _assert_not_echoed(response)
    errors = response.json()["detail"]
    assert {tuple(e["loc"]) for e in errors} == {("body", "text"), ("body", "extra")}
    for err in errors:
        assert set(err) == {"loc", "msg", "type"}
    assert {e["type"] for e in errors} == {"string_too_long", "extra_forbidden"}


async def test_a_malformed_json_body_is_not_reflected(settings, client, daemon):
    body = b'{"text": "' + MARKER.encode() * 20_000 + b'"'  # truncated JSON
    async with running(settings, client, daemon) as (_, http):
        response = await http.post(
            "/api/v1/import/cli-script", content=body, headers={"Content-Type": "application/json"}
        )
    _assert_not_echoed(response)
    assert response.json()["detail"][0]["type"] == "json_invalid"


async def test_wrong_types_in_a_master_write_are_not_reflected(settings, client, daemon):
    async with running(settings, client, daemon) as (_, http):
        response = await http.put(f"/api/v1/devices/{SERIAL}/master", json={"volume": MARKER * 5000})
    _assert_not_echoed(response)


@pytest.mark.parametrize(
    ("path", "field", "extra"),
    [
        (f"/api/v1/devices/{SERIAL}/presets/0/import/cli-script", "text", {}),
        (f"/api/v1/devices/{SERIAL}/adopt", "script", {"mode": "trust"}),
        ("/api/v1/profiles/import/cli-script", "text", {"name": "bad script"}),
    ],
)
async def test_script_errors_name_the_line_but_not_its_text(settings, client, daemon, path, field, extra):
    bad = "\n".join(
        [
            "output 0 gain -3",
            f"output 0 gain {MARKER}{'x' * 20_000}",
            f"{MARKER}{'y' * 20_000} on",
            f"output 0 mute {MARKER}",
        ]
    )
    payload = {field: bad, **extra}
    async with running(settings, client, daemon) as (_, http):
        response = await http.post(path, json=payload)
    _assert_not_echoed(response)
    data = response.json()
    assert data["error"] == "invalid"
    assert [p.split(":")[0] for p in data["problems"]] == ["line 2", "line 3", "line 4"]
    assert "not a finite number" in data["problems"][0] and "unknown command" in data["problems"][1]


async def test_preset_patch_type_errors_are_not_reflected(settings, client, daemon):
    async with running(settings, client, daemon) as (_, http):
        response = await http.patch(
            f"/api/v1/devices/{SERIAL}/presets/0", json={"outputs": {"0": {"gain_db": MARKER * 5000}}}
        )
    _assert_not_echoed(response)
    assert any(p.startswith("outputs.0.gain_db:") for p in response.json()["problems"])
