"""MQTT / Home Assistant discovery bridge (docs/10). Disabled unless ``MQTT_ENABLED``."""

from app.mqtt.bridge import MqttBridge, create_bridge
from app.mqtt.payloads import CommandError, Topics

__all__ = ["CommandError", "MqttBridge", "Topics", "create_bridge"]
