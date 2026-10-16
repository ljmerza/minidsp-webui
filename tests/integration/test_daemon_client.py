"""The app's daemon client against the mock minidspd."""

import pytest

from app.daemon.client import DaemonClient, DaemonRejected, DaemonUnavailable, DeviceNotFound
from app.model.master import MasterStatus

from .conftest import MOCK_SERIAL

pytestmark = pytest.mark.integration


async def test_client_lists_and_resolves_mock(daemon: DaemonClient):
    devices = await daemon.list_devices()
    assert [d.product_name for d in devices] == ["2x4HD"]
    index, device = await daemon.resolve_index(MOCK_SERIAL)
    assert index == 0
    assert device.version is not None and device.version.serial == MOCK_SERIAL


async def test_client_status(daemon: DaemonClient):
    status = await daemon.get_status(0)
    assert status.master.source in ("Analog", "Toslink", "Usb")
    assert len(status.input_levels) == 2
    assert len(status.output_levels) == 4


@pytest.mark.usefixtures("restore_master")
async def test_client_post_master(daemon: DaemonClient):
    master = await daemon.post_master(0, MasterStatus(volume=-12.5, mute=False, source="Usb"))
    assert (master.volume, master.mute, master.source) == (-12.5, False, "Usb")


async def test_client_post_config_accepts_doc_payload(daemon: DaemonClient):
    await daemon.post_config(
        0,
        {
            "inputs": [{"index": 0, "routing": [{"index": 1, "mute": False, "gain": 0.0}]}],
            "outputs": [{"index": 1, "gain": -1.0, "delay": {"secs": 0, "nanos": 1_000_000}}],
        },
    )
    await daemon.post_config(0, {"outputs": [{"index": 1, "gain": 0.0, "delay": {"secs": 0, "nanos": 0}}]})


async def test_client_maps_out_of_range_index(daemon: DaemonClient):
    with pytest.raises(DeviceNotFound):
        await daemon.get_status(3)


async def test_client_maps_plain_text_internal_error(daemon: DaemonClient):
    with pytest.raises(DaemonRejected) as info:
        await daemon.post_config(0, {"outputs": [{"index": 9, "gain": -1.0}]})
    assert info.value.status_code == 500
    assert info.value.error_type is None
    assert "out of range" in info.value.body


async def test_client_maps_handler_panic_to_unavailable(daemon: DaemonClient):
    # Empty reply from the 0.1.12 routing-index panic: outcome unknown, same as a timeout.
    with pytest.raises(DaemonUnavailable):
        await daemon.post_config(0, {"inputs": [{"index": 0, "routing": [{"index": 9, "mute": True}]}]})


# minidspd 0.1.12 has no /volume, /source or /preset routes; these helpers go through POST /devices/{i}.
@pytest.mark.usefixtures("restore_master")
async def test_client_volume_up(daemon: DaemonClient):
    await daemon.post_master(0, {"volume": -20.0})
    assert (await daemon.volume_up(0)).volume == -19.5
    assert (await daemon.volume_down(0)).volume == -20.0


@pytest.mark.usefixtures("restore_master")
async def test_client_toggle_mute_and_source(daemon: DaemonClient):
    before = (await daemon.get_status(0)).master.mute
    assert (await daemon.toggle_mute(0)).mute is (not before)
    assert (await daemon.set_source(0, "toslink")).source == "Toslink"


@pytest.mark.usefixtures("restore_master")
async def test_client_set_preset(daemon: DaemonClient):
    assert (await daemon.set_preset(0, 1)).preset == 1
