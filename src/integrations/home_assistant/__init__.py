"""
Home Assistant — the WQM-1 as MQTT-discovered entities on the owner's own
broker.

Rules this package keeps:

* Opt-in. ``mqtt_enabled`` is off by default; nothing connects until it is on.
* Readings out, nothing in. The unit publishes state and discovery configs and
  subscribes to no topic. No relay, setting or hold can be driven over MQTT.
* Relay, never grade. Every entity is a measurement with its unit; there is
  no "water quality" entity, no verdict and no threshold.
* The broker is the owner's (Home Assistant's built-in Mosquitto, usually).
  Its address and credentials are local-only keys: a remote config can turn
  the feature on or off but can never point the unit at a broker.
* Home Assistant bridges the entities to Google Home, Alexa and SmartThings
  itself (Nabu Casa cloud or a self-hosted bridge). BlueSignal claims no
  "Works with" badge on any of those.
"""

from integrations.home_assistant.discovery import (
    ENTITIES,
    HOLD_ENTITY,
    discovery_messages,
    state_payload,
    topics_for,
)
from integrations.home_assistant.publisher import (
    HomeAssistantPublisher,
    MqttUnavailable,
)

__all__ = [
    "ENTITIES",
    "HOLD_ENTITY",
    "HomeAssistantPublisher",
    "MqttUnavailable",
    "discovery_messages",
    "state_payload",
    "topics_for",
]
