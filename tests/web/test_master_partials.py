from urllib.parse import unquote

import pytest

from app.daemon.client import DaemonRejected, DaemonUnavailable, DeviceNotFound
from app.sync.session import ConfirmationRequired, MasterValidationError, PresetSwitchInProgress, PresetSwitchTimeout

from .conftest import HX, SERIAL

BASE = f"/ui/devices/{SERIAL}/master"


@pytest.mark.parametrize(
    ("path", "form", "call"),
    [
        ("/volume", {"volume": "-20.5"}, ("set_volume", -20.5)),
        ("/volume/up", {}, ("volume_step", 1)),
        ("/volume/down", {}, ("volume_step", -1)),
        ("/mute", {"mute": "true"}, ("set_mute", True)),
        ("/mute", {"mute": "false"}, ("set_mute", False)),
        ("/mute", {}, ("toggle_mute", None)),
        ("/source", {"source": "Toslink"}, ("set_source", "Toslink")),
        ("/preset", {"preset": "2"}, ("set_preset", 2)),
    ],
)
async def test_master_actions_call_the_session_and_return_the_header(session, http, path, form, call):
    response = await http.post(BASE + path, data=form, headers=HX)
    assert response.status_code == 200, response.text
    assert session.calls == [call]
    assert "X-Mdsp-Error" not in response.headers
    assert 'class="hdr"' in response.text and "<html" not in response.text


async def test_header_reflects_the_new_state(http):
    html = (await http.post(BASE + "/preset", data={"preset": "3"}, headers=HX)).text
    assert 'id="preset-4" class="preset is-active"' in html
    html = (await http.post(BASE + "/volume", data={"volume": "-7.5"}, headers=HX)).text
    assert 'value="-7.5"' in html


async def test_requires_htmx_header(session, http):
    response = await http.post(BASE + "/volume", data={"volume": "-10"})
    assert response.status_code == 400
    assert session.calls == []


@pytest.mark.parametrize(
    ("path", "form", "fragment"),
    [
        ("/volume", {"volume": "loud"}, "volume: expected a number"),
        ("/volume", {"volume": "nan"}, "finite"),
        ("/volume", {"volume": "inf"}, "finite"),
        ("/volume", {}, "volume: expected a number"),
        ("/mute", {"mute": "maybe"}, "mute: expected true or false"),
        ("/source", {}, "source: missing"),
        ("/preset", {"preset": "two"}, "preset: expected an integer"),
    ],
)
async def test_bad_form_values_are_422_with_message(session, http, path, form, fragment):
    response = await http.post(BASE + path, data=form, headers=HX)
    assert response.status_code == 422
    assert fragment in unquote(response.headers["X-Mdsp-Error"])
    assert 'class="hdr"' in response.text  # the current state is still rendered
    assert session.calls == []


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (MasterValidationError("volume 5 dB outside -127..0"), 422),
        (ConfirmationRequired("raising the master volume by 20 dB (to 0 dB) needs confirmation"), 409),
        (PresetSwitchInProgress("preset switch to 2 in progress"), 409),
        (PresetSwitchTimeout("device did not report preset 2 within 15 s"), 504),
        (DaemonUnavailable("connection refused"), 503),
        (DeviceNotFound("device gone"), 503),
        (DaemonRejected(500, '{"error":{"type":"X"}}', error_type="X", message="bad payload"), 502),
    ],
)
async def test_service_errors_map_to_status_and_message(session, http, exc, status):
    session.fail_with = exc
    response = await http.post(BASE + "/volume", data={"volume": "-10"}, headers=HX)
    assert response.status_code == status
    assert unquote(response.headers["X-Mdsp-Error"]) == str(exc)
    assert 'class="hdr"' in response.text
    logged = [message for _, message in session.events]
    assert bool(logged) is not isinstance(exc, MasterValidationError)


async def test_error_header_is_ascii_safe(session, http):
    session.fail_with = DaemonUnavailable("minidspd → unreachable: café")
    response = await http.post(BASE + "/volume/up", headers=HX)
    assert response.status_code == 503
    assert response.headers["X-Mdsp-Error"].isascii()
    assert unquote(response.headers["X-Mdsp-Error"]) == "minidspd → unreachable: café"


async def test_unknown_device_is_404_fragment(http):
    response = await http.post("/ui/devices/1/master/volume/up", headers=HX)
    assert response.status_code == 404
    assert unquote(response.headers["X-Mdsp-Error"]) == "Unknown device 1"
    assert 'class="hdr"' in response.text


@pytest.mark.parametrize(
    ("form", "confirmed"), [({}, False), ({"confirm": "true"}, True), ({"confirm": "false"}, False)]
)
async def test_volume_passes_the_confirm_flag(session, http, form, confirmed):
    response = await http.post(BASE + "/volume", data={"volume": "-3", **form}, headers=HX)
    assert response.status_code == 200
    assert session.confirmed is confirmed
