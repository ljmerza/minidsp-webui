"""The embedded mock's level pattern (MINIDSPD_MOCK_LEVELS, app/daemon/mock_levels.py)."""

import pytest

from app.config import Settings
from app.daemon.mock_levels import NO_SIGNAL_DBFS, input_levels, output_levels, pattern_levels
from app.model.config_doc import ConfigDoc
from tests.unit.sync.test_summary import ZERO, peq, walkthrough


def doc(data: dict | None = None) -> ConfigDoc:
    return ConfigDoc.model_validate(data or walkthrough())


def test_off_keeps_the_devices_readings():
    assert pattern_levels("off", 0.0, doc(), n_inputs=2, n_outputs=4) is None


@pytest.mark.parametrize(("pattern", "low", "high"), [("signal", -24.0, -16.0), ("hot", -10.0, -2.0)])
def test_in_1_carries_a_swinging_programme_and_in_2_none(pattern, low, high):
    readings = [input_levels(pattern, t / 10, 2) for t in range(40)]
    assert all(low <= r[0] <= high for r in readings)
    assert {r[1] for r in readings} == {NO_SIGNAL_DBFS}
    assert max(r[0] for r in readings) - min(r[0] for r in readings) > 6  # it moves


def test_silent_has_no_signal_anywhere():
    inputs, outputs = pattern_levels("silent", 1.0, doc(), n_inputs=2, n_outputs=4)
    assert inputs == [NO_SIGNAL_DBFS] * 2 and outputs == [NO_SIGNAL_DBFS] * 4


def test_outputs_follow_the_routing_levels_and_mutes():
    # Walkthrough: In 1 -> Shakers (+4 route, +12 output), Sub unrouted, Out 3 / Out 4 muted.
    out = output_levels(doc(), [-20.0, NO_SIGNAL_DBFS])
    assert out[0] == -4.0  # -20 + 4 + 12
    assert out[1] == NO_SIGNAL_DBFS  # nothing routed
    assert out[2] == out[3] == NO_SIGNAL_DBFS  # muted
    assert output_levels(doc(), [-20.0, NO_SIGNAL_DBFS], master_mute=True) == [NO_SIGNAL_DBFS] * 4


def test_hot_input_clips_a_loud_output_at_0_dbfs():
    assert output_levels(doc(), [-6.0, NO_SIGNAL_DBFS])[0] == 0.0  # +16 total would pass 0 dBFS


def test_an_empty_filter_silences_the_output():
    data = walkthrough()
    data["outputs"][0]["peq"][2] = {**peq(), "compiled": ZERO}
    assert output_levels(doc(data), [-20.0, NO_SIGNAL_DBFS])[0] == NO_SIGNAL_DBFS


def test_the_setting_accepts_the_patterns_only():
    assert Settings(_env_file=None, minidspd_mock_levels="signal").minidspd_mock_levels == "signal"
    with pytest.raises(ValueError):
        Settings(_env_file=None, minidspd_mock_levels="loud")


def test_the_monitor_hook_rewrites_level_frames():
    import json

    from app.bus import Bus
    from app.daemon.monitor import DeviceMonitor

    monitor = DeviceMonitor(
        serial=1,
        bus=Bus(),
        client=None,  # type: ignore[arg-type]
        resolve_index=None,  # type: ignore[arg-type]
        levels_hook=lambda i, o: ([-20.0] * len(i), [-5.0] * len(o)),
    )
    monitor.levels_enabled = True
    monitor.handle_message(
        json.dumps({"master": {"preset": 0}, "input_levels": [-60, -60], "output_levels": [-60] * 4})
    )
    assert monitor.status.input_levels == [-20.0, -20.0] and monitor.status.output_levels == [-5.0] * 4


def test_only_the_mock_device_gets_the_pattern():
    from types import SimpleNamespace

    from app.sync.session import DeviceSession

    fake = SimpleNamespace(device=SimpleNamespace(url="usb:0"))
    assert DeviceSession._mock_levels(fake, [-60.0], [-60.0]) == ([-60.0], [-60.0])  # type: ignore[arg-type]
