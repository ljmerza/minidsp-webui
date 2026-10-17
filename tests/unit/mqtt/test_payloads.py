import json
import math
from dataclasses import replace

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


PHASE2_TOPICS = {
    "homeassistant/number/minidsp_911111/volume/config",
    "homeassistant/switch/minidsp_911111/mute/config",
    "homeassistant/select/minidsp_911111/source/config",
    "homeassistant/select/minidsp_911111/preset/config",
    "homeassistant/binary_sensor/minidsp_911111/connected/config",
    "homeassistant/sensor/minidsp_911111/sync/config",
}
OUTPUT_TOPICS = {
    f"homeassistant/{component}/minidsp_911111/output_{n}_{kind}/config"
    for n in range(1, 5)
    for component, kind in (("number", "gain"), ("switch", "mute"), ("number", "delay"))
}
LEVEL_TOPICS = {f"homeassistant/sensor/minidsp_911111/input_{n}_level/config" for n in (1, 2)} | {
    f"homeassistant/sensor/minidsp_911111/output_{n}_level/config" for n in range(1, 5)
}
PROFILE_TOPIC = "homeassistant/select/minidsp_911111/profile/config"
PUSH_TOPIC = "homeassistant/button/minidsp_911111/push_all/config"


def test_discovery_topics_and_components():
    assert set(configs()) == PHASE2_TOPICS | OUTPUT_TOPICS | LEVEL_TOPICS | {PUSH_TOPIC}
    # No saved profiles -> no Profile select; removal covers it anyway.
    assert sorted(p.removal_topics(SERIAL, TOPICS, M2X4HD)) == sorted([*configs(), PROFILE_TOPIC])
    with_profiles = configs(info=replace(INFO, profiles=("Movies",)))
    assert sorted(p.removal_topics(SERIAL, TOPICS, M2X4HD)) == sorted(with_profiles)


def test_discovery_shared_device_block():
    unique_ids = set()
    for topic, config in configs().items():
        assert config["device"] == {
            "identifiers": ["minidsp_911111"],
            "name": "miniDSP 2x4HD",
            "manufacturer": "miniDSP",
            "model": "2x4HD",
            "serial_number": "911111",
            "sw_version": "fw 1.75",
            "configuration_url": "http://ui",
        }
        if "/button/" not in topic and "_level/" not in topic:
            assert config["state_topic"] == "minidsp-webui/911111/state"
        assert config["availability_mode"] == "all"
        assert config["origin"]["name"] == "minidsp-webui"
        unique_ids.add(config["unique_id"])
    assert len(unique_ids) == len(configs())


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
        if topic.endswith(("/connected/config", "/sync/config")):
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
        "outputs": [],
        "profile": None,
    }
    empty = p.build_state(connected=False, state="daemon_down", volume=None, mute=None, source=None, preset=None)
    assert empty["preset"] is None and empty["preset_switching"] is None and empty["sync"] == "unknown"
    assert empty["outputs"] == [] and empty["profile"] is None


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


# --- Phase 5: per-output controls, profile, push-all, levels ------------------------------------------------------


def outputs_state(**overrides):
    outputs = [p.output_state(-3.0, False, 1.5), p.output_state(-10.0, True, 0.0)]
    outputs += [p.output_state(None, None, None)] * 2
    return state(outputs=outputs, **overrides)


def test_output_entity_names():
    assert p.output_entity(0, "gain") == "output_1_gain"
    assert p.parse_output_entity("output_4_delay") == (3, "delay")
    assert p.parse_output_entity("output_2_mute") == (1, "mute")
    for bad in ("output_0_gain", "output_1_invert", "output_x_gain", "output_1_gain_", "volume", "output_01_gain"):
        assert p.parse_output_entity(bad) is None


def test_output_gain_number_uses_channel_names_and_ceilings():
    info = replace(INFO, outputs=(p.OutputInfo("Shakers", 6.0), *[p.OutputInfo(f"Out {n}", 12.0) for n in (2, 3, 4)]))
    cs = configs(info=info)
    gain = cs["homeassistant/number/minidsp_911111/output_1_gain/config"]
    assert gain["name"] == "Shakers gain" and gain["unique_id"] == "minidsp_911111_output_1_gain"
    assert gain["command_topic"] == "minidsp-webui/911111/cmd/output_1_gain"
    assert (gain["min"], gain["max"], gain["step"], gain["unit_of_measurement"]) == (-72.0, 6.0, 0.1, "dB")
    assert render(gain["value_template"], outputs_state()) == "-3.0"
    assert render(gain["value_template"], state()) == "None"  # no outputs in the state yet
    second = cs["homeassistant/number/minidsp_911111/output_2_gain/config"]
    assert second["max"] == 12.0 and render(second["value_template"], outputs_state()) == "-10.0"
    fourth = cs["homeassistant/number/minidsp_911111/output_4_gain/config"]
    assert render(fourth["value_template"], outputs_state()) == "None"


def test_default_output_names_and_ranges():
    cs = configs()
    assert cs["homeassistant/number/minidsp_911111/output_3_gain/config"]["name"] == "Out 3 gain"
    assert cs["homeassistant/number/minidsp_911111/output_3_gain/config"]["max"] == M2X4HD.gain_max_db
    assert cs["homeassistant/sensor/minidsp_911111/input_2_level/config"]["name"] == "In 2 level"


def test_gain_range_never_inverted():
    assert p.gain_range(M2X4HD, 30.0) == (-72.0, 12.0)
    assert p.gain_range(M2X4HD, -100.0) == (-72.0, -72.0)


def test_output_mute_switch():
    config = configs()["homeassistant/switch/minidsp_911111/output_2_mute/config"]
    assert config["name"] == "Out 2 mute" and config["command_topic"].endswith("/cmd/output_2_mute")
    assert render(config["value_template"], outputs_state()) == "ON"
    first = configs()["homeassistant/switch/minidsp_911111/output_1_mute/config"]
    assert render(first["value_template"], outputs_state()) == "OFF"
    assert render(first["value_template"], state()) == "None"
    fourth = configs()["homeassistant/switch/minidsp_911111/output_4_mute/config"]
    assert render(fourth["value_template"], outputs_state()) == "None"


def test_output_delay_number_is_config():
    config = configs()["homeassistant/number/minidsp_911111/output_1_delay/config"]
    assert config["entity_category"] == "config" and config["unit_of_measurement"] == "ms"
    assert (config["min"], config["step"]) == (0.0, 0.01)
    assert config["max"] == 79.83 and config["max"] <= M2X4HD.delay_max_ms
    assert render(config["value_template"], outputs_state()) == "1.5"


def test_profile_select_only_with_profiles():
    assert PROFILE_TOPIC not in configs()
    config = configs(info=replace(INFO, profiles=("Games", "Movies")))[PROFILE_TOPIC]
    assert config["options"] == ["Games", "Movies"]
    assert config["command_topic"] == "minidsp-webui/911111/cmd/profile"
    assert render(config["value_template"], state(profile="Games")) == "Games"
    assert render(config["value_template"], state()) == "None"


def test_push_all_button():
    config = configs()[PUSH_TOPIC]
    assert "state_topic" not in config and "value_template" not in config
    assert config["command_topic"] == "minidsp-webui/911111/cmd/push_all"
    assert config["payload_press"] == "PRESS" and config["entity_category"] == "config"


def test_level_sensors_disabled_by_default():
    levels = {"i": [-20.04, -30.0], "o": [-1.0, -2.0, -3.0, -4.25]}
    for topic in LEVEL_TOPICS:
        config = configs()[topic]
        assert config["enabled_by_default"] is False
        assert config["state_class"] == "measurement" and config["unit_of_measurement"] == "dBFS"
        assert config["state_topic"] == "minidsp-webui/911111/levels"
        assert config["expire_after"] == p.LEVELS_EXPIRE_S
    cs = configs()
    assert render(cs["homeassistant/sensor/minidsp_911111/input_1_level/config"]["value_template"], levels) == "-20.04"
    assert render(cs["homeassistant/sensor/minidsp_911111/output_4_level/config"]["value_template"], levels) == "-4.25"
    short = {"i": [], "o": []}
    assert render(cs["homeassistant/sensor/minidsp_911111/output_4_level/config"]["value_template"], short) == "None"


def test_build_levels():
    assert p.build_levels({"i": [-20.04, -30.0], "o": [-1.06, math.nan, None, True]}) == {
        "i": [-20.0, -30.0],
        "o": [-1.1, None, None, None],
    }
    assert p.build_levels({}) == {"i": [], "o": []}


def test_state_carries_outputs_and_profile():
    doc = outputs_state(profile="Movies")
    assert doc["outputs"][0] == {"gain": -3.0, "mute": False, "delay": 1.5}
    assert doc["outputs"][3] == {"gain": None, "mute": None, "delay": None}
    assert doc["profile"] == "Movies"
    assert p.output_state(0.0, False, 1.0416666666)["delay"] == 1.042


@pytest.mark.parametrize(("payload", "expected"), [("-3", -3.0), ("6", 6.0), ("-72", -72.0), (" 0.5 ", 0.5)])
def test_parse_gain(payload, expected):
    assert p.parse_gain(payload, M2X4HD, 6.0) == expected


@pytest.mark.parametrize("payload", ["6.1", "12", "-72.5", "nan", "", "loud", "inf"])
def test_parse_gain_rejects_not_clamps(payload):
    with pytest.raises(p.CommandError):
        p.parse_gain(payload, M2X4HD, 6.0)


def test_parse_delay():
    assert p.parse_delay("1.5", M2X4HD) == 1.5
    assert p.parse_delay("0", M2X4HD) == 0.0
    assert p.parse_delay(str(M2X4HD.delay_max_ms), M2X4HD) == M2X4HD.delay_max_ms
    for bad in ("-0.01", "80", "nan", "x"):
        with pytest.raises(p.CommandError):
            p.parse_delay(bad, M2X4HD)


def test_parse_press_and_profile():
    p.parse_press("PRESS")
    p.parse_press(" press ")
    with pytest.raises(p.CommandError):
        p.parse_press("ON")
    names = ["Games", "Movies", "movies"]
    assert p.parse_profile(" Games ", names) == "Games"
    assert p.parse_profile("games", names) == "Games"
    assert p.parse_profile("movies", names) == "movies"  # exact match wins over the ambiguous casefold one
    with pytest.raises(p.CommandError):
        p.parse_profile("MOVIES", names)  # ambiguous
    with pytest.raises(p.CommandError):
        p.parse_profile("Music", names)
