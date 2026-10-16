"""The minidspd 0.1.12 HTTP contract, as observed against the mock. See MOCK_NOTES.md for the findings."""

import asyncio
import json

import httpx
import pytest
import websockets

from .conftest import MOCK_SERIAL, MockMemory

pytestmark = pytest.mark.integration

# 2x4HD DSP addresses (minidsp-rs protocol/src/device/m2x4hd.rs). Mute/enable words: 1 = muted/off, 2 = on.
IN_MUTE = (0, 1)
OUT_MUTE = (2, 3, 4, 5)
IN_GAIN = (26, 27)
OUT_GAIN = (28, 29, 30, 31)
OUT_DELAY = (64, 65, 66, 67)
OUT_INVERT = (80, 81, 82, 83)
COMP0_THRESHOLD, COMP0_RATIO, COMP0_ATTACK, COMP0_RELEASE = 40, 42, 44, 45
MUTED, UNMUTED = 1, 2


def routing_enable_addr(inp: int, out: int) -> int:
    return 6 + 4 * inp + out


def routing_gain_addr(inp: int, out: int) -> int:
    return 32 + 4 * inp + out


def delay_payload(samples: int) -> dict:
    """docs/06: whole samples at 96 kHz, sent as a serde Duration."""
    return {"secs": 0, "nanos": round(samples / 96000 * 1e9)}


def error_type(response: httpx.Response) -> str | None:
    try:
        return response.json()["error"]["type"]
    except (ValueError, KeyError, TypeError):
        return None


# --- discovery / status ---------------------------------------------------------------------------------------------


async def test_device_list_shows_mock_2x4hd(raw: httpx.AsyncClient):
    devices = (await raw.get("/devices")).json()
    assert len(devices) == 1
    device = devices[0]
    assert device["product_name"] == "2x4HD"
    assert device["url"] == f"mock:?serial={MOCK_SERIAL}"
    assert device["version"]["serial"] == MOCK_SERIAL
    assert (device["version"]["hw_id"], device["version"]["dsp_version"]) == (10, 100)


async def test_get_status_shape(raw: httpx.AsyncClient):
    response = await raw.get("/devices/0")
    assert response.status_code == 200
    status = response.json()
    assert set(status["master"]) >= {"preset", "source", "volume", "mute"}
    assert "dirac" not in status["master"]  # omitted (None) on the 2x4HD
    assert len(status["input_levels"]) == 2
    assert len(status["output_levels"]) == 4
    # The mock's meters are fixed at -60 dBFS.
    assert set(status["input_levels"] + status["output_levels"]) == {-60.0}


async def test_device_path_also_accepts_serial(raw: httpx.AsyncClient):
    # get_device() matches the path segment against serials before treating it as an index.
    assert (await raw.get(f"/devices/{MOCK_SERIAL}")).status_code == 200


async def test_out_of_range_index_is_500_with_typed_error(raw: httpx.AsyncClient):
    response = await raw.get("/devices/5")
    assert response.status_code == 500
    assert error_type(response) == "DeviceIndexOutOfRange"


# --- master status --------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("restore_master")
async def test_master_volume_mute_source_echo(raw: httpx.AsyncClient):
    response = await raw.post("/devices/0", json={"volume": -20.0, "mute": True, "source": "Toslink"})
    assert response.status_code == 200
    master = response.json()
    assert (master["volume"], master["mute"], master["source"]) == (-20.0, True, "Toslink")
    assert master["dirac"] is None  # the POST echo includes dirac: null, GET omits it

    status = (await raw.get("/devices/0")).json()["master"]
    assert (status["volume"], status["mute"], status["source"]) == (-20.0, True, "Toslink")


@pytest.mark.usefixtures("restore_master")
async def test_master_volume_is_truncated_to_half_db(raw: httpx.AsyncClient):
    assert (await raw.post("/devices/0", json={"volume": -20.3})).json()["volume"] == -20.0
    assert (await raw.post("/devices/0", json={"volume": -20.7})).json()["volume"] == -20.5


@pytest.mark.usefixtures("restore_master")
async def test_master_volume_is_not_range_checked(raw: httpx.AsyncClient):
    # The daemon accepts out-of-range volumes and silently mangles them, so the app must clamp before sending.
    assert (await raw.post("/devices/0", json={"volume": 5.0})).json()["volume"] == -5.0
    assert (await raw.post("/devices/0", json={"volume": -200.0})).json()["volume"] == -127.5


async def test_master_source_names_are_case_sensitive(raw: httpx.AsyncClient):
    response = await raw.post("/devices/0", json={"source": "toslink"})
    assert response.status_code == 500
    assert error_type(response) == "ParseError"


@pytest.mark.usefixtures("restore_master")
async def test_master_preset_switch_is_instant_and_unvalidated(raw: httpx.AsyncClient):
    assert (await raw.post("/devices/0", json={"preset": 1})).json()["preset"] == 1
    # Preset 7 doesn't exist on a 2x4HD, but the mock stores it anyway.
    assert (await raw.post("/devices/0", json={"preset": 7})).json()["preset"] == 7
    assert (await raw.post("/devices/0", json={"preset": 0})).json()["preset"] == 0


@pytest.mark.usefixtures("restore_master")
async def test_master_via_config_master_status(raw: httpx.AsyncClient):
    response = await raw.post("/devices/0/config", json={"master_status": {"volume": -30.0}})
    assert response.status_code == 200
    assert response.text == ""
    assert (await raw.get("/devices/0")).json()["master"]["volume"] == -30.0


@pytest.mark.parametrize("path", ["volume/up", "volume/down", "volume/toggle_mute", "source/analog", "preset/1"])
async def test_dedicated_master_endpoints_absent_in_0_1_12(raw: httpx.AsyncClient, path: str):
    # Added upstream after the 0.1.12 release (minidsp-rs d548db7). The Pi runs 0.1.12 too.
    response = await raw.post(f"/devices/0/{path}")
    assert response.status_code == 404
    assert response.text == "Not Found"


# --- config writes --------------------------------------------------------------------------------------------------


async def test_output_gain_mute_invert(post_config, mock_memory: MockMemory):
    response = await post_config({"outputs": [{"index": 1, "gain": -3.5, "mute": True, "invert": True}]})
    assert response.status_code == 200
    assert response.text == ""
    assert await mock_memory.word(OUT_GAIN[1]) == -3.5
    assert await mock_memory.int_word(OUT_MUTE[1]) == MUTED
    invert_on = await mock_memory.int_word(OUT_INVERT[1])

    await post_config({"outputs": [{"index": 1, "gain": 0.0, "mute": False, "invert": False}]})
    assert await mock_memory.word(OUT_GAIN[1]) == 0.0
    assert await mock_memory.int_word(OUT_MUTE[1]) == UNMUTED
    assert await mock_memory.int_word(OUT_INVERT[1]) != invert_on


async def test_output_gain_is_not_range_checked(post_config, mock_memory: MockMemory):
    # +50 dB is accepted as-is: gain limits are entirely the app's job.
    assert (await post_config({"outputs": [{"index": 2, "gain": 50.0}]})).status_code == 200
    assert await mock_memory.word(OUT_GAIN[2]) == 50.0
    await post_config({"outputs": [{"index": 2, "gain": 0.0}]})


async def test_input_gain_and_mute(post_config, mock_memory: MockMemory):
    assert (await post_config({"inputs": [{"index": 1, "gain": -6.0, "mute": True}]})).status_code == 200
    assert await mock_memory.word(IN_GAIN[1]) == -6.0
    assert await mock_memory.int_word(IN_MUTE[1]) == MUTED
    await post_config({"inputs": [{"index": 1, "gain": 0.0, "mute": False}]})
    assert await mock_memory.int_word(IN_MUTE[1]) == UNMUTED


@pytest.mark.parametrize("samples", [1, 7, 96, 4801, 7999, 8000])
async def test_delay_duration_round_trips_whole_samples(post_config, mock_memory: MockMemory, samples: int):
    response = await post_config({"outputs": [{"index": 3, "delay": delay_payload(samples)}]})
    assert response.status_code == 200
    # The daemon truncates the Duration to whole microseconds before rounding to samples; the sample count survives.
    assert await mock_memory.int_word(OUT_DELAY[3]) == samples


async def test_delay_limit_is_8000_samples(post_config):
    response = await post_config({"outputs": [{"index": 3, "delay": delay_payload(8001)}]})
    assert response.status_code == 500
    assert "Delay should be within [0, 80] ms was 8001" in response.text
    await post_config({"outputs": [{"index": 3, "delay": delay_payload(0)}]})


async def test_delay_must_be_a_duration_object(post_config):
    response = await post_config({"outputs": [{"index": 3, "delay": 1.5}]})
    assert response.status_code == 500
    assert error_type(response) == "ParseError"


async def test_routing_mute_is_inverted_enable(post_config, mock_memory: MockMemory):
    response = await post_config({"inputs": [{"index": 1, "routing": [{"index": 2, "mute": True, "gain": -1.5}]}]})
    assert response.status_code == 200
    assert await mock_memory.int_word(routing_enable_addr(1, 2)) == MUTED
    assert await mock_memory.word(routing_gain_addr(1, 2)) == -1.5

    await post_config({"inputs": [{"index": 1, "routing": [{"index": 2, "mute": False, "gain": 0.0}]}]})
    assert await mock_memory.int_word(routing_enable_addr(1, 2)) == UNMUTED
    assert await mock_memory.word(routing_gain_addr(1, 2)) == 0.0


async def test_peq_coeff_then_unbypass_in_separate_calls(post_config):
    coeff = {"b0": 1.0012, "b1": -1.9876, "b2": 0.9871, "a1": 1.9876, "a2": -0.9883}
    assert (await post_config({"outputs": [{"index": 0, "peq": [{"index": 3, "coeff": coeff}]}]})).status_code == 200
    assert (await post_config({"outputs": [{"index": 0, "peq": [{"index": 3, "bypass": False}]}]})).status_code == 200
    assert (await post_config({"inputs": [{"index": 0, "peq": [{"index": 9, "bypass": True}]}]})).status_code == 200


async def test_peq_index_out_of_range_is_plain_text_500(post_config):
    response = await post_config({"outputs": [{"index": 0, "peq": [{"index": 10, "bypass": True}]}]})
    assert response.status_code == 500
    # InternalError can't be serialized by the daemon, so the body isn't JSON.
    assert error_type(response) is None
    assert "Specified channel or peq is out of range" in response.text


async def test_crossover_biquads_with_index(post_config):
    lr4_lp_80 = {"b0": 6.7e-06, "b1": 1.34e-05, "b2": 6.7e-06, "a1": 1.99259, "a2": -0.99262}
    body = {
        "outputs": [
            {
                "index": 3,
                "crossover": [{"index": 0, "coeff": [{"index": k, **lr4_lp_80} for k in range(4)], "bypass": False}],
            }
        ]
    }
    assert (await post_config(body)).status_code == 200


@pytest.mark.parametrize(
    ("crossover", "message"),
    [
        ({"index": 0, "coeff": [{"b0": 1, "b1": 0, "b2": 0, "a1": 0, "a2": 0}]}, "biquad index not specified"),
        ({"index": 0, "coeff": [{"index": 4, "b0": 1, "b1": 0, "b2": 0, "a1": 0, "a2": 0}]}, "out of range"),
        ({"index": 2, "coeff": [{"index": 0, "b0": 1, "b1": 0, "b2": 0, "a1": 0, "a2": 0}]}, "out of range"),
    ],
)
async def test_crossover_rejections(post_config, crossover: dict, message: str):
    response = await post_config({"outputs": [{"index": 3, "crossover": [crossover]}]})
    assert response.status_code == 500
    assert message in response.text


async def test_compressor_fields(post_config, mock_memory: MockMemory):
    body = {
        "index": 0,
        "compressor": {"bypass": True, "threshold": -10.0, "ratio": 4.0, "attack": 10.0, "release": 100},
    }
    assert (await post_config({"outputs": [body]})).status_code == 200
    words = await mock_memory.dump(COMP0_THRESHOLD, COMP0_RELEASE + 1)
    assert words[COMP0_THRESHOLD] == -10.0
    assert words[COMP0_RATIO] == 4.0
    assert words[COMP0_ATTACK] == 10.0
    assert words[COMP0_RELEASE] == 100.0


@pytest.mark.parametrize(
    "body",
    [
        {"inputs": [{"index": 5, "gain": -6.0}]},
        {"outputs": [{"index": 9, "gain": -6.0}]},
        {"outputs": [{"gain": -6.0}]},
    ],
)
async def test_bad_channel_rejected(post_config, body: dict):
    response = await post_config(body)
    assert response.status_code == 500
    assert error_type(response) is None


async def test_unknown_fields_are_ignored(post_config):
    assert (await post_config({"outputs": [{"index": 0, "bogus": 1}], "extra": True})).status_code == 200


async def test_failed_call_leaves_earlier_parts_applied(post_config, mock_memory: MockMemory):
    """Why a failed write marks every path in it unknown: the daemon applies in order and stops at the first error."""
    await post_config({"outputs": [{"index": 0, "gain": 0.0}]})
    response = await post_config({"outputs": [{"index": 0, "gain": -2.5}, {"index": 9, "gain": -1.0}]})
    assert response.status_code == 500
    assert await mock_memory.word(OUT_GAIN[0]) == -2.5
    await post_config({"outputs": [{"index": 0, "gain": 0.0}]})


async def test_routing_output_out_of_range_drops_connection(post_config, raw: httpx.AsyncClient):
    # 0.1.12 panics in the request handler (fixed upstream later); the connection closes without a response.
    with pytest.raises(httpx.RemoteProtocolError):
        await post_config({"inputs": [{"index": 0, "routing": [{"index": 9, "mute": True}]}]})
    assert (await raw.get("/devices/0")).status_code == 200


# --- websocket ------------------------------------------------------------------------------------------------------


async def _collect(url: str, seconds: float, during=None) -> list[dict]:
    messages: list[dict] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    async with websockets.connect(url) as ws:
        task = asyncio.create_task(during()) if during else None
        try:
            while (remaining := deadline - loop.time()) > 0:
                try:
                    messages.append(json.loads(await asyncio.wait_for(ws.recv(), remaining)))
                except TimeoutError:
                    break
        finally:
            if task:
                await task
    return messages


def _ws_url(daemon_url: str, query: str) -> str:
    return daemon_url.replace("http://", "ws://", 1) + f"/devices/0{query}"


@pytest.mark.usefixtures("restore_master")
async def test_ws_without_poll_does_not_push_master_changes(daemon_url: str, raw: httpx.AsyncClient):
    await raw.post("/devices/0", json={"volume": -20.0})

    async def change():
        await asyncio.sleep(0.5)
        await raw.post("/devices/0", json={"volume": -41.5})

    messages = await _collect(_ws_url(daemon_url, ""), 3.0, change)
    assert len(messages) == 1  # just the initial full status
    assert messages[0]["master"]["volume"] == -20.0
    assert messages[0]["input_levels"]


@pytest.mark.usefixtures("restore_master")
async def test_ws_levels_and_poll_on_one_socket(daemon_url: str, raw: httpx.AsyncClient):
    await raw.post("/devices/0", json={"volume": -20.0})

    async def change():
        await asyncio.sleep(0.5)
        await raw.post("/devices/0", json={"volume": -41.5})

    messages = await _collect(_ws_url(daemon_url, "?levels=true&poll=true"), 4.5, change)
    first, rest = messages[0], messages[1:]
    assert first["master"]["volume"] == -20.0 and first["input_levels"]

    level_frames = [m for m in rest if "input_levels" in m]
    master_frames = [m for m in rest if m.get("master")]
    # Levels every 250 ms with an empty master object; polled master changes (2 s) without levels.
    assert len(level_frames) >= 12
    assert all(m["master"] == {} and len(m["output_levels"]) == 4 for m in level_frames)
    assert [m["master"]["volume"] for m in master_frames] == [-41.5]
    assert all("input_levels" not in m for m in master_frames)
