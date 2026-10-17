"""MQTT / Home Assistant discovery bridge (docs/10). Disabled unless ``MQTT_ENABLED``."""

from app.mqtt.bridge import MqttBridge, RemovalResult, create_bridge, remove_from_home_assistant
from app.mqtt.payloads import CommandError, Topics
from app.mqtt.profiles import ProfileCatalog

__all__ = [
    "CommandError",
    "MqttBridge",
    "ProfileCatalog",
    "RemovalResult",
    "Topics",
    "create_bridge",
    "remove_from_home_assistant",
]
