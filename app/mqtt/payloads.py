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
from collections import Counter
from collections.abc import Iterable, Mapping
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

COMMAND_ENTITIES = (VOLUME, MUTE, SOURCE, PRESET)
SYNC_STATES = ("synced", "pending", "failed", "unknown")

ORIGIN = {
    "name": "minidsp-webui",
    "sw_version": __version__,
    "support_url": "https://github.com/ljmerza/minidsp-webui",
}


def _is_int(text: str) -> bool:
    return text.isascii() and text.isdigit()


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
class DeviceInfo:
    """What the bridge needs to describe one device to Home Assistant."""

    serial: int
    profile: DeviceProfile
    sources: tuple[str, ...]
    fw: str | None = None
    configuration_url: str | None = None


def preset_label(preset: int) -> str:
    """UI numbering: device preset 0 is "Preset 1"."""
    return f"Preset {preset + 1}"


def preset_options(profile: DeviceProfile) -> list[str]:
    return [preset_label(p) for p in range(profile.n_presets)]


def volume_range(profile: DeviceProfile, floor_db: float) -> tuple[float, float]:
    """Slider range: the profile's master range, raised to the configured floor so the HA slider is usable."""
    low = max(profile.master_volume_min_db, floor_db)
    return min(low, profile.master_volume_max_db), profile.master_volume_max_db


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


def discovery_configs(info: DeviceInfo, topics: Topics, *, volume_floor_db: float) -> dict[str, dict[str, Any]]:
    """Discovery topic -> retained config payload for the Phase 2 entities of one device."""
    serial = info.serial
    state = topics.state(serial)

    def entity(object_id: str, name: str, *, needs_device: bool, **extra: Any) -> dict[str, Any]:
        return {
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

    low, high = volume_range(info.profile, volume_floor_db)
    configs = {
        topics.discovery("number", serial, VOLUME): entity(
            VOLUME,
            "Master volume",
            needs_device=True,
            command_topic=topics.command(serial, VOLUME),
            value_template=_nullable(VOLUME),
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
    }
    return configs


def removal_topics(serial: int, topics: Topics) -> list[str]:
    """Discovery topics to clear (empty retained payload) to delete the device's entities from HA."""
    return [
        topics.discovery(component, serial, object_id)
        for component, object_id in (
            ("number", VOLUME),
            ("switch", MUTE),
            ("select", SOURCE),
            ("select", PRESET),
            ("binary_sensor", CONNECTED),
            ("sensor", SYNC),
        )
    ]


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
) -> dict[str, Any]:
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
    }


def dumps(doc: Mapping[str, Any]) -> str:
    """Compact, key-sorted JSON so identical documents compare equal as strings."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)


# --- commands -----------------------------------------------------------------------------------------------------


def parse_volume(payload: str, profile: DeviceProfile, floor_db: float) -> float:
    try:
        value = float(payload.strip())
    except ValueError:
        raise CommandError(f"volume {payload!r} is not a number") from None
    if not math.isfinite(value):
        raise CommandError(f"volume {payload!r} is not a finite number")
    low, high = volume_range(profile, floor_db)
    if not low <= value <= high:
        raise CommandError(f"volume {value:g} dB outside {low:g}..{high:g}")
    return value


_TRUE = {"on", "true", "1"}
_FALSE = {"off", "false", "0"}


def parse_switch(payload: str) -> bool:
    lowered = payload.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise CommandError(f"expected ON or OFF, got {payload!r}")


def parse_source(payload: str, profile: DeviceProfile) -> str:
    canonical = profile.canonical_source(payload)
    if canonical is None:
        raise CommandError(f"unknown source {payload.strip()!r}; expected one of {', '.join(profile.sources)}")
    return canonical


def parse_preset(payload: str, profile: DeviceProfile) -> int:
    """``"Preset N"`` (or a bare ``N``), 1-based like the UI and HA; returns the 0-based device preset."""
    text = payload.strip()
    lowered = text.lower()
    if lowered.startswith("preset"):
        lowered = lowered[len("preset") :].strip()
    if not _is_int(lowered):
        raise CommandError(f"expected one of {', '.join(preset_options(profile))}, got {text!r}")
    number = int(lowered)
    if not 1 <= number <= profile.n_presets:
        raise CommandError(f"preset {text!r} outside Preset 1..Preset {profile.n_presets}")
    return number - 1
