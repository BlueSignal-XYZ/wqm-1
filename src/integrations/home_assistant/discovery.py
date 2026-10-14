"""
Pure mapping: one WQM-1 reading → the MQTT messages Home Assistant discovers.

No paho here. :func:`discovery_messages` builds the retained ``config``
documents (Home Assistant's MQTT discovery format, one per entity) and
:func:`state_payload` builds the one JSON document every entity reads its
value from. Both are unit-tested without a broker; the publisher only ever
hands these to MQTT.

Every value is the reading's own number or absent. A channel the monitor has
suspended (a faulted probe) is left out of the state document — Home
Assistant then shows the entity as *unknown*, which is the truth, rather than
the last number it saw. No threshold, no verdict, no derived scale.
"""

from __future__ import annotations

import json
import math
from typing import Any

MANUFACTURER = "BlueSignal XYZ, Inc."
MODEL = "WQM-1"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
TOPIC_ROOT = "bluesignal/wqm1"

# Words that must never appear in anything the integration names. An entity
# name is customer-facing copy on a dashboard; the same C8 rule as the
# website applies (no verdict about the water).
BANNED_NAME_WORDS = ("safe", "clean", "pure", "drinkable", "potable", "leak", "alarm", "quality")

# One entry per reading column. ``device_class`` / ``state_class`` are Home
# Assistant's own vocabulary and are set only where HA documents the unit as
# valid for that class; a channel HA has no class for ships with its unit and
# an icon and nothing else. ``sensor`` is the monitor's name for the probe
# (what SensorMonitor.suspended_sensors() yields), so a fault can be matched.
ENTITIES: tuple[dict[str, Any], ...] = (
    {
        "key": "temperature",
        "column": "temp_c",
        "sensor": "temperature",
        "name": "Water temperature",
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
        "min": -270.0,
        "max": 100.0,
    },
    {
        "key": "ph",
        "column": "ph",
        "sensor": "ph",
        "name": "pH",
        "unit": "pH",
        "device_class": "ph",
        "state_class": "measurement",
        "min": 0.0,
        "max": 14.0,
    },
    {
        "key": "tds",
        "column": "tds_ppm",
        "sensor": "tds",
        "name": "TDS",
        "unit": "ppm",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:water-opacity",
        "min": 0.0,
        "max": 20000.0,
    },
    {
        "key": "turbidity",
        "column": "turbidity_ntu",
        "sensor": "turbidity",
        "name": "Turbidity",
        "unit": "NTU",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:blur",
        "min": 0.0,
        "max": 4000.0,
    },
    {
        "key": "orp",
        "column": "orp_mv",
        "sensor": "orp",
        "name": "ORP",
        "unit": "mV",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:flash-outline",
        "min": -2000.0,
        "max": 2000.0,
    },
    {
        "key": "chlorine",
        "column": "chlorine_mgl",
        "sensor": "chlorine",
        "name": "Residual chlorine",
        "unit": "mg/L",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:test-tube",
        "min": 0.0,
        "max": 100.0,
    },
    {
        "key": "conductivity",
        "column": "conductivity_uscm",
        "sensor": "conductivity",
        "name": "Conductivity",
        "unit": "µS/cm",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:omega",
        "min": 0.0,
        "max": 200000.0,
    },
    {
        "key": "salinity",
        "column": "salinity_ppt",
        "sensor": "salinity",
        "name": "Salinity",
        "unit": "ppt",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:shaker-outline",
        "min": 0.0,
        "max": 100.0,
    },
    {
        "key": "flow_rate",
        "column": "flow_rate_gpm",
        "sensor": "flow",
        "name": "Flow rate",
        "unit": "gal/min",
        "device_class": "volume_flow_rate",
        "state_class": "measurement",
        "min": 0.0,
        "max": 1000.0,
    },
    {
        # The lifetime totalizer — the evidence register. total_increasing lets
        # Home Assistant's energy-style dashboards chart gallons per day.
        "key": "flow_total",
        "column": "flow_total_gal",
        "sensor": "flow",
        "name": "Water produced",
        "unit": "gal",
        "device_class": "water",
        "state_class": "total_increasing",
        "min": 0.0,
        "max": 1.0e12,
    },
)

# The irrigation hold's relay contact: ON = holding (contact open), OFF =
# clear. Describes the contact, never the water.
HOLD_ENTITY: dict[str, Any] = {
    "key": "irrigation_hold",
    "name": "Irrigation hold",
    "icon": "mdi:sprinkler-variant",
}


def _tail(device_id: str) -> str:
    return str(device_id or "").strip()[-4:].lower() or "unit"


def _node_id(device_id: str) -> str:
    """A topic-safe id for the unit: the label with everything but
    ``[a-z0-9]`` turned into underscores."""
    raw = str(device_id or "").strip().lower() or "unit"
    return "wqm1_" + "".join(c if c.isalnum() else "_" for c in raw)


def device_name(device_id: str) -> str:
    """``WQM-1 a92e`` — the label's last four, same as the Apple Home name."""
    return f"WQM-1 {_tail(device_id)}"


def topics_for(device_id: str, prefix: str = DEFAULT_DISCOVERY_PREFIX) -> dict[str, str]:
    node = _node_id(device_id)
    return {
        "node": node,
        "state": f"{TOPIC_ROOT}/{node}/state",
        "availability": f"{TOPIC_ROOT}/{node}/availability",
        "discovery_prefix": str(prefix or DEFAULT_DISCOVERY_PREFIX).strip("/"),
    }


def _device_block(device_id: str, firmware_version: str) -> dict[str, Any]:
    return {
        "identifiers": [_node_id(device_id)],
        "name": device_name(device_id),
        "manufacturer": MANUFACTURER,
        "model": MODEL,
        "sw_version": str(firmware_version),
    }


def discovery_messages(
    device_id: str,
    firmware_version: str,
    prefix: str = DEFAULT_DISCOVERY_PREFIX,
) -> list[tuple[str, str]]:
    """``[(topic, json), ...]`` — one retained config per entity. Every
    config names the same state topic and availability topic, so the
    publisher sends one state document per cycle and Home Assistant fans it
    out. Publishing an empty payload on a config topic removes the entity;
    the publisher never does that on its own (an owner removes the device in
    Home Assistant)."""
    t = topics_for(device_id, prefix)
    device = _device_block(device_id, firmware_version)
    out: list[tuple[str, str]] = []
    for ent in ENTITIES:
        cfg: dict[str, Any] = {
            "name": ent["name"],
            "unique_id": f"{t['node']}_{ent['key']}",
            "object_id": f"{t['node']}_{ent['key']}",
            "state_topic": t["state"],
            # A missing key renders '' and Home Assistant shows *unknown* —
            # the truth for a channel that was not read this cycle.
            "value_template": "{{ value_json." + ent["key"] + " | default('') }}",
            "unit_of_measurement": ent["unit"],
            "availability_topic": t["availability"],
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        if ent.get("device_class"):
            cfg["device_class"] = ent["device_class"]
        if ent.get("state_class"):
            cfg["state_class"] = ent["state_class"]
        if ent.get("icon"):
            cfg["icon"] = ent["icon"]
        topic = f"{t['discovery_prefix']}/sensor/{t['node']}/{ent['key']}/config"
        out.append((topic, json.dumps(cfg, ensure_ascii=False, separators=(",", ":"))))

    hold_cfg = {
        "name": HOLD_ENTITY["name"],
        "unique_id": f"{t['node']}_{HOLD_ENTITY['key']}",
        "object_id": f"{t['node']}_{HOLD_ENTITY['key']}",
        "state_topic": t["state"],
        "value_template": "{{ value_json.irrigation_hold | default('') }}",
        "payload_on": "holding",
        "payload_off": "clear",
        "icon": HOLD_ENTITY["icon"],
        "availability_topic": t["availability"],
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
    }
    topic = f"{t['discovery_prefix']}/binary_sensor/{t['node']}/{HOLD_ENTITY['key']}/config"
    out.append((topic, json.dumps(hold_cfg, ensure_ascii=False, separators=(",", ":"))))
    return out


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def state_payload(
    reading: dict[str, Any] | None,
    hold: dict[str, Any] | None = None,
    suspended: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """The one state document. ``reading`` is the row the sampling worker
    stored; ``hold`` is ``IrrigationHold.payload()`` (``None`` when the hold
    is disabled); ``suspended`` is the monitor's faulted-sensor set.

    A channel that is missing, non-finite, out of its physical range or
    suspended is left out — never written as 0 and never carried over."""
    r = reading or {}
    susp = set(suspended or ())
    out: dict[str, Any] = {}
    faults: list[str] = []
    for ent in ENTITIES:
        if ent["sensor"] in susp:
            faults.append(ent["key"])
            continue
        v = _num(r.get(ent["column"]))
        if v is None or not ent["min"] <= v <= ent["max"]:
            continue
        out[ent["key"]] = v
    h = hold or {}
    if h.get("enabled"):
        out["irrigation_hold"] = "holding" if h.get("active") else "clear"
    if faults:
        out["faults"] = faults
    return out
