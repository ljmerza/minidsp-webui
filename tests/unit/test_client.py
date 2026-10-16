import json

import httpx
import pytest

from app.daemon.client import DaemonClient, DaemonRejected, DaemonUnavailable, DeviceNotFound
from app.model.master import MasterStatus

from .conftest import DEVICE_JSON, MASTER_JSON, SERIAL


async def test_list_devices(client, daemon):
    probing = {"url": "usb:2", "product_name": None, "version": None}
    daemon.get("/devices").respond(json=[probing, DEVICE_JSON])
    devices = await client.list_devices()
    assert devices[0].serial is None
    assert devices[1].serial == SERIAL
    assert devices[1].product_name == "2x4HD"
    assert devices[1].version.fw == "1.75"


async def test_get_status_full_and_levels(client, daemon):
    daemon.get("/devices/0").respond(
        json={"master": MASTER_JSON, "input_levels": [-40.0, -120.0], "output_levels": [-50.0, -60.0, -120.0, -120.0]}
    )
    status = await client.get_status(0)
    assert status.master.volume == -20.0
    assert status.master.source == "Analog"
    assert status.input_levels == [-40.0, -120.0]
    assert len(status.output_levels) == 4


async def test_post_master_sends_only_set_fields(client, daemon):
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -10.0})
    result = await client.post_master(0, MasterStatus(volume=-10.0))
    assert json.loads(route.calls.last.request.content) == {"volume": -10.0}
    assert result.volume == -10.0


async def test_post_config_body_and_empty_response(client, daemon):
    route = daemon.post("/devices/1/config").respond(200, content=b"")
    payload = {"outputs": [{"index": 0, "gain": 3.0}]}
    assert await client.post_config(1, payload) is None
    assert json.loads(route.calls.last.request.content) == payload


@pytest.mark.parametrize(
    ("method", "current", "sent"),
    [
        ("volume_up", -20.0, {"volume": -19.5}),
        ("volume_down", -20.0, {"volume": -20.5}),
        ("volume_up", -0.25, {"volume": 0.0}),
        ("volume_down", -127.0, {"volume": -127.0}),
    ],
)
async def test_volume_steps_are_clamped_absolute_writes(client, daemon, method, current, sent):
    # minidspd 0.1.12 has no /volume/up|down routes, so steps are absolute POST /devices/{i} writes.
    route = daemon.post("/devices/0").respond(json=MASTER_JSON)
    await getattr(client, method)(0, current=current)
    assert json.loads(route.calls.last.request.content) == sent


async def test_volume_step_reads_current_when_not_given(client, daemon):
    daemon.get("/devices/0").respond(json={"master": {**MASTER_JSON, "volume": -30.0}})
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "volume": -29.5})
    assert (await client.volume_up(0)).volume == -29.5
    assert json.loads(route.calls.last.request.content) == {"volume": -29.5}


async def test_toggle_mute_inverts_current(client, daemon):
    daemon.get("/devices/0").respond(json={"master": {**MASTER_JSON, "mute": False}})
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "mute": True})
    assert (await client.toggle_mute(0)).mute is True
    assert json.loads(route.calls.last.request.content) == {"mute": True}
    await client.toggle_mute(0, current=True)
    assert json.loads(route.calls.last.request.content) == {"mute": False}


async def test_set_source_sends_variant_name(client, daemon):
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "source": "Toslink"})
    assert (await client.set_source(0, "toslink")).source == "Toslink"
    assert json.loads(route.calls.last.request.content) == {"source": "Toslink"}


async def test_set_preset(client, daemon):
    route = daemon.post("/devices/0").respond(json={**MASTER_JSON, "preset": 2})
    assert (await client.set_preset(0, 2)).preset == 2
    assert json.loads(route.calls.last.request.content) == {"preset": 2}


async def test_resolve_index_by_serial(client, daemon):
    other = {**DEVICE_JSON, "version": {**DEVICE_JSON["version"], "serial": 1}}
    daemon.get("/devices").respond(json=[other, DEVICE_JSON])
    index, device = await client.resolve_index(SERIAL)
    assert index == 1
    assert device.serial == SERIAL
    with pytest.raises(DeviceNotFound):
        await client.resolve_index(42)


async def test_device_not_ready_maps_to_device_not_found(client, daemon):
    body = {"message": "the specified device is not ready", "error": {"type": "DeviceNotReady"}}
    daemon.get("/devices/3").respond(500, json=body)
    with pytest.raises(DeviceNotFound) as exc:
        await client.get_status(3)
    assert "DeviceNotReady" in exc.value.body


async def test_internal_error_maps_to_rejected_with_body(client, daemon):
    body = {"message": "Delay should be within [0, 80] ms", "error": {"type": "InternalError"}}
    daemon.post("/devices/0/config").respond(500, json=body)
    with pytest.raises(DaemonRejected) as exc:
        await client.post_config(0, {"outputs": [{"index": 0, "delay": {"secs": 1, "nanos": 0}}]})
    assert exc.value.status_code == 500
    assert exc.value.error_type == "InternalError"
    assert exc.value.message == "Delay should be within [0, 80] ms"
    assert "Delay" in exc.value.body


async def test_non_json_error_body(client, daemon):
    daemon.post("/devices/0/config").respond(500, text="Something went wrong: boom")
    with pytest.raises(DaemonRejected) as exc:
        await client.post_config(0, {})
    assert exc.value.body == "Something went wrong: boom"
    assert exc.value.error_type is None


async def test_still_initializing_is_unavailable(client, daemon):
    body = {"message": "init", "error": {"type": "ApplicationStillInitializing"}}
    daemon.get("/devices").respond(500, json=body)
    with pytest.raises(DaemonUnavailable):
        await client.list_devices()


async def test_connection_errors_are_unavailable(client, daemon):
    daemon.get("/devices").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(DaemonUnavailable):
        await client.list_devices()
    daemon.get("/devices/0").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(DaemonUnavailable):
        await client.get_status(0)


def test_ws_url():
    client = DaemonClient("http://127.0.0.1:5380/")
    assert client.ws_url(0, levels=True, poll=True) == "ws://127.0.0.1:5380/devices/0?levels=true&poll=true"
    assert client.ws_url(1, levels=False, poll=True) == "ws://127.0.0.1:5380/devices/1?poll=true"
    assert DaemonClient("https://h/x").ws_url(0, levels=False, poll=False) == "wss://h/x/devices/0"


@pytest.mark.parametrize(
    "payload", [[{"product_name": "2x4HD"}], [{"url": "x", "version": {"serial": 1}}], {"devices": []}, "nope"]
)
async def test_malformed_device_list_is_rejected(client, daemon, payload):
    daemon.get("/devices").respond(json=payload)
    with pytest.raises(DaemonRejected):
        await client.list_devices()


async def test_malformed_status_is_rejected(client, daemon):
    daemon.get("/devices/0").respond(json={"master": {"volume": "loud"}})
    with pytest.raises(DaemonRejected):
        await client.get_status(0)
