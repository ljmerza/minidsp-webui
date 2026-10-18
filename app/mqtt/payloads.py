"""Pure builders for the MQTT / Home Assistant bridge: topics, discovery configs, the state document and command
parsing (docs/10). Nothing here does I/O, so it is all unit-testable.

Every entity reads the one retained ``state`` JSON through a ``value_template``. Availability is the combination of:

* ``<base>/availability``: the bridge itself, ``online`` while connected; the broker publishes ``offline`` (LWT) if the
  app dies without a clean disconnect.
* ``<base>/<serial>/availability``: the device's entities, ``online`` while the app serves that device.
* for the controls only: the ``connected`` flag in the state, so volume/mute/source/preset grey out while the daemon or
  the device is unreachable. The connectivity and sync sensors stay available so they can report exactly that.
"""

import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app import __version__
from app.model.device_profile import DeviceProfile

ONLINE = "online"
OFFLINE = "offline"
ON = "ON"
OFF = "OFF"

VOLUME = "volume"
MUTE = "mute"
SOURCE = "source"
PRESET = "preset"
CONNECTED = "connected"
SYNC = "sync"

PROFILE = "profile"
PUSH_ALL = "push_all"
PRESS = "PRESS"

# Per-output entities: ``output_<n>_<kind>`` (1-based like the UI).
GAIN = "gain"
DELAY = "delay"
OUTPUT_KINDS = (GAIN, MUTE, DELAY)

COMMAND_ENTITIES = (VOLUME, MUTE, SOURCE, PRESET, PROFILE, PUSH_ALL)
SYNC_STATES = ("synced", "pending", "failed", "unknown")

GAIN_STEP_DB = 0.1
DELAY_STEP_MS = 0.01
LEVELS_EXPIRE_S = 10  # level sensors go unavailable when the (1 Hz) level stream stops

ORIGIN = {
    "name": "minidsp-webui",
    "sw_version": __version__,
    "support_url": "https://github.com/ljmerza/minidsp-webui",
}


def _is_int(text: str) -> bool:
    """A short run of ASCII digits (``int()`` raises on more than 4300 digits, and nothing valid is that long)."""
    return 0 < len(text) <= 18 and text.isascii() and text.isdigit()


_OUTPUT_ENTITY = re.compile(r"output_([1-9][0-9]{0,2})_(gain|mute|delay)")


def output_entity(index: int, kind: str) -> str:
    """Object id / command entity of a per-output control: ``output_1_gain`` for output index 0."""
    return f"output_{index + 1}_{kind}"


def parse_output_entity(entity: str) -> tuple[int, str] | None:
    """``(0-based output index, kind)`` for ``output_<n>_<kind>``, None for anything else."""
    match = _OUTPUT_ENTITY.fullmatch(entity)
    if match is None:
        return None
    return int(match.group(1)) - 1, match.group(2)


def level_entity(kind: str, index: int) -> str:
    return f"{kind}_{index + 1}_level"


def default_label(kind: str, index: int) -> str:
    """The UI's fallback channel name: "In 1" / "Out 1"."""
    return f"{'In' if kind == 'input' else 'Out'} {index + 1}"


class CommandError(ValueError):
    """A command payload that is malformed or out of range. It is rejected, never clamped."""


@dataclass(frozen=True, slots=True)
class Topics:
    base: str = "minidsp-webui"
    discovery_prefix: str = "homeassistant"

    def __post_init__(self) -> None:
        object.__setattr__(self, "base", self.base.strip("/"))
        object.__setattr__(self, "discovery_prefix", self.discovery_prefix.strip("/"))

    @property
    def bridge_availability(self) -> str:
        return f"{self.base}/availability"

    def availability(self, serial: int) -> str:
        return f"{self.base}/{serial}/availability"

    def state(self, serial: int) -> str:
        return f"{self.base}/{serial}/state"

    def levels(self, serial: int) -> str:
        return f"{self.base}/{serial}/levels"

    def command(self, serial: int, entity: str) -> str:
        return f"{self.base}/{serial}/cmd/{entity}"

    @property
    def command_filter(self) -> str:
        return f"{self.base}/+/cmd/+"

    def discovery(self, component: str, serial: int, object_id: str) -> str:
        return f"{self.discovery_prefix}/{component}/minidsp_{serial}/{object_id}/config"

    def parse_command(self, topic: str) -> tuple[int, str] | None:
        """``(serial, entity)`` for a command topic, None for anything else."""
        prefix = f"{self.base}/"
        if not topic.startswith(prefix):
            return None
        parts = topic[len(prefix) :].split("/")
        if len(parts) != 3 or parts[1] != "cmd" or not _is_int(parts[0]):
            return None
        return int(parts[0]), parts[2]


@dataclass(frozen=True, slots=True)
class OutputInfo:
    """Name (from Settings) and gain ceiling of one output; the HA gain slider's maximum is the ceiling."""

    name: str
    gain_max_db: float


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """What the bridge needs to describe one device to Home Assistant.

    ``outputs`` / ``inputs`` default to the profile's channels with their default names and the profile's gain
    maximum. ``profiles`` are the saved profile names offered by the Profile select (no select without any)."""

    serial: int
    profile: DeviceProfile
    sources: tuple[str, ...]
    fw: str | None = None
    configuration_url: str | None = None
    outputs: tuple[OutputInfo, ...] = ()
    inputs: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()

    def output_channels(self) -> tuple[OutputInfo, ...]:
        if self.outputs:
            return self.outputs
        return tuple(
            OutputInfo(default_label("output", n), self.profile.gain_max_db) for n in range(self.profile.n_outputs)
        )

    def input_names(self) -> tuple[str, ...]:
        return self.inputs or tuple(default_label("input", n) for n in range(self.profile.n_inputs))


def preset_label(preset: int) -> str:
    """UI numbering: device preset 0 is "Preset 1"."""
    return f"Preset {preset + 1}"


def preset_options(profile: DeviceProfile) -> list[str]:
    return [preset_label(p) for p in range(profile.n_presets)]


def volume_range(profile: DeviceProfile, floor_db: float) -> tuple[float, float]:
    """Slider range: the profile's master range, raised to the configured floor so the HA slider is usable."""
    low = max(profile.master_volume_min_db, floor_db)
    return min(low, profile.master_volume_max_db), profile.master_volume_max_db


def gain_range(profile: DeviceProfile, ceiling_db: float) -> tuple[float, float]:
    """Per-output gain range: the profile minimum up to the channel's ceiling (Settings)."""
    return profile.gain_min_db, max(profile.gain_min_db, min(ceiling_db, profile.gain_max_db))


def delay_max_ms(profile: DeviceProfile) -> float:
    """Largest delay HA may send: the device maximum rounded to 3 decimals, exactly what `output_state` publishes for
    a delay at the maximum (HA drops a state above ``max``). `parse_delay` accepts it even when it rounds up."""
    return round(profile.delay_max_ms, 3)


# --- discovery ----------------------------------------------------------------------------------------------------


def device_block(info: DeviceInfo) -> dict[str, Any]:
    device: dict[str, Any] = {
        "identifiers": [f"minidsp_{info.serial}"],
        "name": f"miniDSP {info.profile.product_name}",
        "manufacturer": "miniDSP",
        "model": info.profile.product_name,
        "serial_number": str(info.serial),
    }
    if info.fw:
        device["sw_version"] = f"fw {info.fw}"
    if info.configuration_url:
        device["configuration_url"] = info.configuration_url
    return device


def _availability(topics: Topics, serial: int, *, needs_device: bool) -> list[dict[str, str]]:
    entries = [{"topic": topics.bridge_availability}, {"topic": topics.availability(serial)}]
    if needs_device:
        entries.append(
            {
                "topic": topics.state(serial),
                "value_template": "{{ 'online' if value_json.connected else 'offline' }}",
            }
        )
    return entries


def _nullable(field: str, *, true: str | None = None, false: str | None = None) -> str:
    """Template that renders ``None`` (which HA reads as "unknown") when ``field`` is null."""
    value = f"value_json.{field}"
    if true is None:
        return f"{{{{ {value} if {value} is not none else 'None' }}}}"
    return f"{{% if {value} is none %}}None{{% elif {value} %}}{true}{{% else %}}{false}{{% endif %}}"


def _clamped(field: str, low: float, high: float) -> str:
    """Like `_nullable` for a number entity, clamped to its ``low..high`` range: HA drops an out-of-range state and
    keeps showing the previous value (e.g. a master volume below the configured floor)."""
    value = f"value_json.{field}"
    return (
        f"{{% if {value} is none %}}None{{% elif {value} < {low!r} %}}{low!r}"
        f"{{% elif {value} > {high!r} %}}{high!r}{{% else %}}{{{{ {value} }}}}{{% endif %}}"
    )


def _item(
    array: str, index: int, field: str | None = None, *, true: str | None = None, false: str | None = None
) -> str:
    """Like `_nullable` for ``value_json.<array>[index](.field)``, also rendering ``None`` when the array is missing
    or too short (a state published before the preset state is loaded)."""
    item = f"value_json.{array}[{index}]"
    value = item if field is None else f"{item}.{field}"
    guard = f"value_json.{array} is defined and value_json.{array} | length > {index} and {value} is not none"
    if true is None:
        return f"{{% if {guard} %}}{{{{ {value} }}}}{{% else %}}None{{% endif %}}"
    return f"{{% if not ({guard}) %}}None{{% elif {value} %}}{true}{{% else %}}{false}{{% endif %}}"


def discovery_configs(info: DeviceInfo, topics: Topics, *, volume_floor_db: float) -> dict[str, dict[str, Any]]:
    """Discovery topic -> retained config payload for the Phase 2 entities of one device."""
    serial = info.serial
    state = topics.state(serial)

    def entity(object_id: str, name: str, *, needs_device: bool, stateless: bool = False, **extra: Any) -> dict:
        config = {
            "name": name,
            "unique_id": f"minidsp_{serial}_{object_id}",
            "device": device_block(info),
            "origin": ORIGIN,
            "availability": _availability(topics, serial, needs_device=needs_device),
            "availability_mode": "all",
            "state_topic": state,
            "qos": 1,
            **extra,
        }
        if stateless:
            del config["state_topic"]
        return config

    low, high = volume_range(info.profile, volume_floor_db)
    configs = {
        topics.discovery("number", serial, VOLUME): entity(
            VOLUME,
            "Master volume",
            needs_device=True,
            command_topic=topics.command(serial, VOLUME),
            value_template=_clamped(VOLUME, low, high),
            min=low,
            max=high,
            step=info.profile.master_volume_step_db,
            mode="slider",
            unit_of_measurement="dB",
            icon="mdi:volume-high",
        ),
        topics.discovery("switch", serial, MUTE): entity(
            MUTE,
            "Master mute",
            needs_device=True,
            command_topic=topics.command(serial, MUTE),
            value_template=_nullable(MUTE, true=ON, false=OFF),
            payload_on=ON,
            payload_off=OFF,
            state_on=ON,
            state_off=OFF,
            icon="mdi:volume-off",
        ),
        topics.discovery("select", serial, SOURCE): entity(
            SOURCE,
            "Source",
            needs_device=True,
            command_topic=topics.command(serial, SOURCE),
            value_template=_nullable(SOURCE),
            options=list(info.sources),
            icon="mdi:import",
        ),
        topics.discovery("select", serial, PRESET): entity(
            PRESET,
            "Preset",
            needs_device=True,
            command_topic=topics.command(serial, PRESET),
            value_template=_nullable(PRESET),
            options=preset_options(info.profile),
            icon="mdi:tune-vertical",
        ),
        topics.discovery("binary_sensor", serial, CONNECTED): entity(
            CONNECTED,
            "Device connected",
            needs_device=False,
            value_template=f"{{{{ '{ON}' if value_json.connected else '{OFF}' }}}}",
            payload_on=ON,
            payload_off=OFF,
            device_class="connectivity",
            entity_category="diagnostic",
        ),
        topics.discovery("sensor", serial, SYNC): entity(
            SYNC,
            "Sync status",
            needs_device=False,
            value_template="{{ value_json.sync }}",
            device_class="enum",
            options=list(SYNC_STATES),
            json_attributes_topic=state,
            json_attributes_template="{{ value_json.sync_counts | tojson }}",
            entity_category="diagnostic",
            icon="mdi:sync",
        ),
        topics.discovery("button", serial, PUSH_ALL): entity(
            PUSH_ALL,
            "Push all settings",
            needs_device=True,
            stateless=True,
            command_topic=topics.command(serial, PUSH_ALL),
            payload_press=PRESS,
            entity_category="config",
            icon="mdi:upload",
        ),
    }
    if info.profiles:
        configs[topics.discovery("select", serial, PROFILE)] = entity(
            PROFILE,
            "Profile",
            needs_device=True,
            command_topic=topics.command(serial, PROFILE),
            value_template=_nullable(PROFILE),
            options=list(info.profiles),
            icon="mdi:bookmark-music",
        )

    profile = info.profile
    for n, output in enumerate(info.output_channels()):
        gain_low, gain_high = gain_range(profile, output.gain_max_db)
        gain, mute, delay = (output_entity(n, kind) for kind in OUTPUT_KINDS)
        configs[topics.discovery("number", serial, gain)] = entity(
            gain,
            f"{output.name} gain",
            needs_device=True,
            command_topic=topics.command(serial, gain),
            value_template=_item("outputs", n, "gain"),
            min=gain_low,
            max=gain_high,
            step=GAIN_STEP_DB,
            mode="slider",
            unit_of_measurement="dB",
            icon="mdi:knob",
        )
        configs[topics.discovery("switch", serial, mute)] = entity(
            mute,
            f"{output.name} mute",
            needs_device=True,
            command_topic=topics.command(serial, mute),
            value_template=_item("outputs", n, "mute", true=ON, false=OFF),
            payload_on=ON,
            payload_off=OFF,
            state_on=ON,
            state_off=OFF,
            icon="mdi:volume-off",
        )
        configs[topics.discovery("number", serial, delay)] = entity(
            delay,
            f"{output.name} delay",
            needs_device=True,
            command_topic=topics.command(serial, delay),
            value_template=_item("outputs", n, "delay"),
            min=0.0,
            max=delay_max_ms(profile),
            step=DELAY_STEP_MS,
            mode="box",
            unit_of_measurement="ms",
            entity_category="config",
            icon="mdi:timer-outline",
        )

    levels = topics.levels(serial)
    channels = [("input", "i", n, name) for n, name in enumerate(info.input_names())]
    channels += [("output", "o", n, o.name) for n, o in enumerate(info.output_channels())]
    for kind, key, n, name in channels:
        object_id = level_entity(kind, n)
        configs[topics.discovery("sensor", serial, object_id)] = entity(
            object_id,
            f"{name} level",
            needs_device=True,
            state_topic=levels,
            value_template=_item(key, n),
            unit_of_measurement="dBFS",
            state_class="measurement",
            suggested_display_precision=1,
            expire_after=LEVELS_EXPIRE_S,
            enabled_by_default=False,
            icon="mdi:waveform",
        )
    return configs


def removal_topics(serial: int, topics: Topics, profile: DeviceProfile) -> list[str]:
    """Every discovery topic the device can have, to clear (empty retained payload) when deleting its entities from
    HA, or when an optional entity (the Profile select) goes away."""
    entries = [
        ("number", VOLUME),
        ("switch", MUTE),
        ("select", SOURCE),
        ("select", PRESET),
        ("binary_sensor", CONNECTED),
        ("sensor", SYNC),
        ("button", PUSH_ALL),
        ("select", PROFILE),
    ]
    for n in range(profile.n_outputs):
        entries += [
            ("number", output_entity(n, GAIN)),
            ("switch", output_entity(n, MUTE)),
            ("number", output_entity(n, DELAY)),
        ]
    entries += [("sensor", level_entity("input", n)) for n in range(profile.n_inputs)]
    entries += [("sensor", level_entity("output", n)) for n in range(profile.n_outputs)]
    return [topics.discovery(component, serial, object_id) for component, object_id in entries]


# --- state --------------------------------------------------------------------------------------------------------


def overall_sync(counts: Mapping[str, int] | None) -> str:
    """Collapse per-path counts into one status: any failed > any pending > any unknown > synced."""
    if not counts or not any(counts.values()):
        return "unknown"
    for status in ("failed", "pending", "unknown"):
        if counts.get(status, 0) > 0:
            return status
    return "synced"


def sync_counts(statuses: Iterable[Any]) -> dict[str, int]:
    """Per-status counts from an iterable of SyncStatus values (or their strings)."""
    counted = Counter(str(s) for s in statuses)
    return {s: counted.get(s, 0) for s in SYNC_STATES}


def build_state(
    *,
    connected: bool,
    state: str,
    volume: float | None,
    mute: bool | None,
    source: str | None,
    preset: int | None,
    preset_switching: int | None = None,
    sync_counts: Mapping[str, int] | None = None,
    outputs: Sequence[Mapping[str, Any]] = (),
    profile: str | None = None,
) -> dict[str, Any]:
    """The retained state document. ``outputs`` are the active preset's per-output ``gain`` / ``mute`` / ``delay``
    (None when unknown); ``profile`` is the saved profile the active preset currently matches, if any."""
    return {
        "connected": connected,
        "state": state,
        "volume": volume,
        "mute": mute,
        "source": source,
        "preset": preset_label(preset) if preset is not None else None,
        "preset_index": preset,
        "preset_switching": preset_label(preset_switching) if preset_switching is not None else None,
        "sync": overall_sync(sync_counts),
        "sync_counts": {s: int((sync_counts or {}).get(s, 0)) for s in SYNC_STATES},
        "outputs": [{k: o.get(k) for k in (GAIN, MUTE, DELAY)} for o in outputs],
        "profile": profile,
    }


def output_state(gain: float | None, mute: bool | None, delay: float | None) -> dict[str, Any]:
    return {GAIN: gain, MUTE: mute, DELAY: None if delay is None else round(delay, 3)}


def build_levels(levels: Mapping[str, Any]) -> dict[str, list[float | None]]:
    """``{"i": [...], "o": [...]}`` dBFS from a bus ``levels`` event, rounded to 0.1 dB (non-finite -> null)."""

    def clean(values: Any) -> list[float | None]:
        out: list[float | None] = []
        for v in values if isinstance(values, list | tuple) else ():
            ok = isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
            out.append(round(float(v), 1) if ok else None)
        return out

    return {"i": clean(levels.get("i")), "o": clean(levels.get("o"))}


def dumps(doc: Mapping[str, Any]) -> str:
    """Compact, key-sorted JSON so identical documents compare equal as strings."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)


# --- commands -----------------------------------------------------------------------------------------------------


QUOTE_MAX = 32  # characters of a rejected payload quoted in an error (it ends up in the persistent event log)


def _quote(text: str) -> str:
    return repr(text if len(text) <= QUOTE_MAX else f"{text[:QUOTE_MAX]}...")


def parse_number(payload: str, what: str, low: float, high: float, unit: str) -> float:
    """A finite number inside ``low..high``; anything else is rejected (never clamped)."""
    try:
        value = float(payload.strip())
    except ValueError:
        raise CommandError(f"{what} {_quote(payload)} is not a number") from None
    if not math.isfinite(value):
        raise CommandError(f"{what} {_quote(payload)} is not a finite number")
    if not low <= value <= high:
        raise CommandError(f"{what} {value:g} {unit} outside {low:g}..{high:g}")
    return value


def parse_volume(payload: str, profile: DeviceProfile, floor_db: float) -> float:
    return parse_number(payload, "volume", *volume_range(profile, floor_db), "dB")


def parse_gain(payload: str, profile: DeviceProfile, ceiling_db: float) -> float:
    return parse_number(payload, "gain", *gain_range(profile, ceiling_db), "dB")


def parse_delay(payload: str, profile: DeviceProfile) -> float:
    return parse_number(payload, "delay", 0.0, max(profile.delay_max_ms, delay_max_ms(profile)), "ms")


def parse_press(payload: str) -> None:
    if payload.strip().upper() != PRESS:
        raise CommandError(f"expected {PRESS}, got {_quote(payload)}")


def parse_profile(payload: str, names: Sequence[str]) -> str:
    """One of the saved profile names: exact, else a unique case-insensitive match."""
    text = payload.strip()
    if text in names:
        return text
    matches = [n for n in names if n.casefold() == text.casefold()]
    if len(matches) == 1:
        return matches[0]
    raise CommandError(f"unknown profile {_quote(text)}")


_TRUE = {"on", "true", "1"}
_FALSE = {"off", "false", "0"}


def parse_switch(payload: str) -> bool:
    lowered = payload.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise CommandError(f"expected ON or OFF, got {_quote(payload)}")


def parse_source(payload: str, profile: DeviceProfile) -> str:
    canonical = profile.canonical_source(payload)
    if canonical is None:
        raise CommandError(f"unknown source {_quote(payload.strip())}; expected one of {', '.join(profile.sources)}")
    return canonical


def parse_preset(payload: str, profile: DeviceProfile) -> int:
    """``"Preset N"`` (or a bare ``N``), 1-based like the UI and HA; returns the 0-based device preset."""
    text = payload.strip()
    lowered = text.lower()
    if lowered.startswith("preset"):
        lowered = lowered[len("preset") :].strip()
    if not _is_int(lowered):
        raise CommandError(f"expected one of {', '.join(preset_options(profile))}, got {_quote(text)}")
    number = int(lowered)
    if not 1 <= number <= profile.n_presets:
        raise CommandError(f"preset {_quote(text)} outside Preset 1..Preset {profile.n_presets}")
    return number - 1
