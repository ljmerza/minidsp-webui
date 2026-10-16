import json
import math

import jinja2
import pytest

from app.model.device_profile import M2X4HD
from app.mqtt import payloads as p

SERIAL = 911111
TOPICS = p.Topics("minidsp-webui", "homeassistant")
INFO = p.DeviceInfo(serial=SERIAL, profile=M2X4HD, sources=M2X4HD.sources, fw="1.75", configuration_url="http://ui")


def configs(**kwargs):
    return p.discovery_configs(kwargs.pop("info", INFO), kwargs.pop("topics", TOPICS), volume_floor_db=-80.0, **kwargs)


def render(template: str, state: dict) -> str:
    """Render an HA value_template the way HA does for these simple cases (Jinja2 + tojson)."""
    env = jinja2.Environment()
    env.filters["tojson"] = json.dumps
    return env.from_string(template).render(value_json=state)


def state(**overrides):
    base = dict(connected=True, state="connected", volume=-20.0, mute=False, source="Analog", preset=0)
    return p.build_state(**{**base, **overrides})


# --- topics -------------------------------------------------------------------------------------------------------


def test_topics():
    t = p.Topics("/minidsp-webui/", "homeassistant/")
    assert t.bridge_availability == "minidsp-webui/availability"
    assert t.availability(SERIAL) == "minidsp-webui/911111/availability"
    assert t.state(SERIAL) == "minidsp-webui/911111/state"
    assert t.command(SERIAL, "volume") == "minidsp-webui/911111/cmd/volume"
    assert t.command_filter == "minidsp-webui/+/cmd/+"
    assert t.discovery("number", SERIAL, "volume") == "homeassistant/number/minidsp_911111/volume/config"


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("minidsp-webui/911111/cmd/volume", (SERIAL, "volume")),
        ("minidsp-webui/911111/cmd/preset", (SERIAL, "preset")),
        ("minidsp-webui/911111/state", None),
        ("minidsp-webui/abc/cmd/volume", None),
        ("minidsp-webui/１２/cmd/volume", None),
        ("minidsp-webui/911111/cmd/volume/extra", None),
        ("other/911111/cmd/volume", None),
        ("minidsp-webui-x/911111/cmd/volume", None),
    ],
)
def test_parse_command_topic(topic, expected):
    assert TOPICS.parse_command(topic) == expected


def test_parse_command_topic_with_nested_base():
    t = p.Topics("home/dsp")
    assert t.parse_command(t.command(7, "mute")) == (7, "mute")


# --- discovery ----------------------------------------------------------------------------------------------------


def test_discovery_topics_and_components():
    assert set(configs()) == {
        "homeassistant/number/minidsp_911111/volume/config",
        "homeassistant/switch/minidsp_911111/mute/config",
        "homeassistant/select/minidsp_911111/source/config",
        "homeassistant/select/minidsp_911111/preset/config",
        "homeassistant/binary_sensor/minidsp_911111/connected/config",
        "homeassistant/sensor/minidsp_911111/sync/config",
    }
    assert sorted(p.removal_topics(SERIAL, TOPICS)) == sorted(configs())


def test_discovery_shared_device_block():
    unique_ids = set()
    for config in configs().values():
        assert config["device"] == {
            "identifiers": ["minidsp_911111"],
            "name": "miniDSP 2x4HD",
            "manufacturer": "miniDSP",
            "model": "2x4HD",
            "serial_number": "911111",
            "sw_version": "fw 1.75",
            "configuration_url": "http://ui",
        }
        assert config["state_topic"] == "minidsp-webui/911111/state"
        assert config["availability_mode"] == "all"
        assert config["origin"]["name"] == "minidsp-webui"
        unique_ids.add(config["unique_id"])
    assert len(unique_ids) == 6


def test_device_block_omits_unknowns():
    device = p.device_block(p.DeviceInfo(serial=1, profile=M2X4HD, sources=()))
    assert "sw_version" not in device and "configuration_url" not in device


def test_volume_number():
    config = configs()["homeassistant/number/minidsp_911111/volume/config"]
    assert config["command_topic"] == "minidsp-webui/911111/cmd/volume"
    assert (config["min"], config["max"], config["step"], config["mode"]) == (-80.0, 0.0, 0.5, "slider")
    assert config["unit_of_measurement"] == "dB"
    assert render(config["value_template"], state()) == "-20.0"
    assert render(config["value_template"], state(volume=None)) == "None"  # HA's payload_reset -> unknown


def test_volume_floor_never_below_device_range():
    assert p.volume_range(M2X4HD, -200.0) == (-127.0, 0.0)
    assert p.volume_range(M2X4HD, -60.0) == (-60.0, 0.0)
    assert p.volume_range(M2X4HD, 10.0) == (0.0, 0.0)


def test_mute_switch():
    config = configs()["homeassistant/switch/minidsp_911111/mute/config"]
    assert config["command_topic"].endswith("/cmd/mute")
    assert (config["payload_on"], config["payload_off"]) == ("ON", "OFF")
    assert render(config["value_template"], state(mute=True)) == "ON"
    assert render(config["value_template"], state(mute=False)) == "OFF"
    assert render(config["value_template"], state(mute=None)) == "None"


def test_source_and_preset_selects():
    cs = configs()
    source = cs["homeassistant/select/minidsp_911111/source/config"]
    preset = cs["homeassistant/select/minidsp_911111/preset/config"]
    assert source["options"] == ["Analog", "Toslink", "Usb"]
    assert preset["options"] == ["Preset 1", "Preset 2", "Preset 3", "Preset 4"]
    assert render(source["value_template"], state(source="Toslink")) == "Toslink"
    assert render(preset["value_template"], state(preset=2)) == "Preset 3"
    assert render(preset["value_template"], state(preset=None)) == "None"


def test_controls_unavailable_when_disconnected_but_health_sensors_stay():
    for topic, config in configs().items():
        templates = [a["value_template"] for a in config["availability"] if "value_template" in a]
        assert config["availability"][0] == {"topic": "minidsp-webui/availability"}
        assert config["availability"][1] == {"topic": "minidsp-webui/911111/availability"}
        if "/binary_sensor/" in topic or "/sensor/" in topic:
            assert templates == []
        else:
            assert len(templates) == 1
            assert render(templates[0], state()) == "online"
            assert render(templates[0], state(connected=False)) == "offline"


def test_connectivity_and_sync_sensors():
    cs = configs()
    connected = cs["homeassistant/binary_sensor/minidsp_911111/connected/config"]
    sync = cs["homeassistant/sensor/minidsp_911111/sync/config"]
    assert connected["device_class"] == "connectivity" and "command_topic" not in connected
    assert render(connected["value_template"], state()) == "ON"
    assert render(connected["value_template"], state(connected=False)) == "OFF"
    assert sync["device_class"] == "enum" and sync["options"] == ["synced", "pending", "failed", "unknown"]
    doc = state(sync_counts={"synced": 3, "pending": 1})
    assert render(sync["value_template"], doc) == "pending"
    assert json.loads(render(sync["json_attributes_template"], doc)) == {
        "synced": 3,
        "pending": 1,
        "failed": 0,
        "unknown": 0,
    }


def test_discovery_is_json_serializable():
    for config in configs().values():
        json.loads(p.dumps(config))


# --- state --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        (None, "unknown"),
        ({}, "unknown"),
        ({"synced": 0}, "unknown"),
        ({"synced": 5}, "synced"),
        ({"synced": 5, "unknown": 1}, "unknown"),
        ({"synced": 5, "unknown": 1, "pending": 2}, "pending"),
        ({"synced": 5, "pending": 2, "failed": 1}, "failed"),
    ],
)
def test_overall_sync(counts, expected):
    assert p.overall_sync(counts) == expected


def test_sync_counts_from_statuses():
    from app.sync.writer import SyncStatus

    statuses = [SyncStatus.SYNCED, SyncStatus.SYNCED, SyncStatus.FAILED, "pending"]
    assert p.sync_counts(statuses) == {"synced": 2, "pending": 1, "failed": 1, "unknown": 0}


def test_build_state():
    doc = state(preset_switching=1, sync_counts={"synced": 10})
    assert doc == {
        "connected": True,
        "state": "connected",
        "volume": -20.0,
        "mute": False,
        "source": "Analog",
        "preset": "Preset 1",
        "preset_index": 0,
        "preset_switching": "Preset 2",
        "sync": "synced",
        "sync_counts": {"synced": 10, "pending": 0, "failed": 0, "unknown": 0},
    }
    empty = p.build_state(connected=False, state="daemon_down", volume=None, mute=None, source=None, preset=None)
    assert empty["preset"] is None and empty["preset_switching"] is None and empty["sync"] == "unknown"


def test_dumps_is_canonical():
    assert p.dumps({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    with pytest.raises(ValueError):
        p.dumps({"x": math.nan})


# --- commands -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("payload", "expected"), [("-20", -20.0), (" -20.5 ", -20.5), ("0", 0.0), ("-80", -80.0)])
def test_parse_volume(payload, expected):
    assert p.parse_volume(payload, M2X4HD, -80.0) == expected


@pytest.mark.parametrize("payload", ["0.5", "12", "-80.5", "-127", "nan", "inf", "-inf", "", "loud", "1e400"])
def test_parse_volume_rejects_not_clamps(payload):
    with pytest.raises(p.CommandError):
        p.parse_volume(payload, M2X4HD, -80.0)


@pytest.mark.parametrize(
    ("payload", "expected"), [("ON", True), ("off", False), (" true ", True), ("0", False), ("1", True)]
)
def test_parse_switch(payload, expected):
    assert p.parse_switch(payload) is expected


@pytest.mark.parametrize("payload", ["", "toggle", "yes", "2"])
def test_parse_switch_rejects(payload):
    with pytest.raises(p.CommandError):
        p.parse_switch(payload)


def test_parse_source():
    assert p.parse_source("toslink", M2X4HD) == "Toslink"
    assert p.parse_source(" USB ", M2X4HD) == "Usb"
    with pytest.raises(p.CommandError, match="expected one of Analog, Toslink, Usb"):
        p.parse_source("Hdmi", M2X4HD)


@pytest.mark.parametrize(("payload", "expected"), [("Preset 1", 0), ("preset 4", 3), ("Preset3", 2), ("2", 1)])
def test_parse_preset(payload, expected):
    assert p.parse_preset(payload, M2X4HD) == expected


@pytest.mark.parametrize("payload", ["Preset 0", "Preset 5", "0", "5", "-1", "Preset", "", "Preset ²", "Movies"])
def test_parse_preset_rejects(payload):
    with pytest.raises(p.CommandError):
        p.parse_preset(payload, M2X4HD)
