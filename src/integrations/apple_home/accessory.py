"""
Pure mapping: one WQM-1 reading → what the HomeKit accessory says.

No HAP-python here. :func:`snapshot_from_reading` is a function of the
reading row the sampling worker just stored, the irrigation hold's payload and
the set of suspended (faulted) sensors, and :data:`SERVICES` is the fixed
plan of services the accessory carries. Both are unit-tested without the
library, and the bridge only ever pushes a :class:`HomeSnapshot` into HAP.

Every value is the reading's own number or ``None``. No threshold, no
verdict, no derived "quality" scale.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any

# HomeKit's own services (Apple's Home app renders these natively).
SERVICE_TEMPERATURE = "TemperatureSensor"
SERVICE_CONTACT = "ContactSensor"

# HomeKit ContactSensorState: 0 = contact detected (closed), 1 = not
# detected (open). The irrigation hold's contact is normally CLOSED (COM–NC)
# and OPENS to hold, so holding == 1. Describes the contact, never the water.
CONTACT_CLOSED = 0
CONTACT_OPEN = 1

# HomeKit CurrentTemperature is bounded [-270, 100] °C by the spec; a water
# reading outside that is a sensor fault, not a temperature.
TEMPERATURE_MIN_C = -270.0
TEMPERATURE_MAX_C = 100.0

# Custom characteristics, one per channel HomeKit has no native type for.
# UUIDs are v5 in a BlueSignal namespace so they are stable across builds and
# never collide with Apple's. Third-party HomeKit apps show these by name;
# Apple's Home app ignores unknown characteristics and shows the
# temperature and contact services only. That asymmetry is stated on the
# bluesignal.xyz page, not hidden.
_NS = uuid.UUID("3b1d4b5e-0f6e-5c8a-9b2f-7d2a6e4c1f10")  # BlueSignal Apple Home namespace

CUSTOM_CHARACTERISTICS: dict[str, dict[str, Any]] = {
    "ph": {
        "display_name": "pH",
        "type_id": uuid.uuid5(_NS, "wqm1.ph"),
        "column": "ph",
        "unit": "",
        "min": 0.0,
        "max": 14.0,
        "step": 0.01,
    },
    "tds": {
        "display_name": "TDS",
        "type_id": uuid.uuid5(_NS, "wqm1.tds_ppm"),
        "column": "tds_ppm",
        "unit": "ppm",
        "min": 0.0,
        "max": 20000.0,
        "step": 1.0,
    },
    "turbidity": {
        "display_name": "Turbidity",
        "type_id": uuid.uuid5(_NS, "wqm1.turbidity_ntu"),
        "column": "turbidity_ntu",
        "unit": "NTU",
        "min": 0.0,
        "max": 4000.0,
        "step": 0.1,
    },
    "flow": {
        "display_name": "Flow rate",
        "type_id": uuid.uuid5(_NS, "wqm1.flow_rate_gpm"),
        "column": "flow_rate_gpm",
        "unit": "gpm",
        "min": 0.0,
        "max": 1000.0,
        "step": 0.01,
    },
}

# Reading column → the name SensorMonitor.suspended_sensors() uses.
_COLUMN_TO_SENSOR = {
    "temp_c": "temperature",
    "ph": "ph",
    "tds_ppm": "tds",
    "turbidity_ntu": "turbidity",
    "flow_rate_gpm": "flow",
}

# The fixed plan. Order is the order the Home app lists them.
SERVICES: tuple[dict[str, str], ...] = (
    {"key": "water_temperature", "type": SERVICE_TEMPERATURE, "name": "Water temperature"},
    {"key": "irrigation_hold", "type": SERVICE_CONTACT, "name": "Irrigation hold"},
)

# Words that must never appear in anything the accessory names. A HomeKit
# name is customer-facing copy on an Apple screen; the same C8 rule as the
# website applies (no verdict about the water).
BANNED_NAME_WORDS = ("safe", "clean", "pure", "drinkable", "potable", "leak", "alarm")


def accessory_name(device_id: str) -> str:
    """``WQM-1 a92e`` — the label's last four hex characters, so two units in
    one house are told apart, and nothing that reads as a serial number in
    full on a shared screen."""
    tail = str(device_id or "").strip()[-4:].lower() or "unit"
    return f"WQM-1 {tail}"


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


@dataclass(frozen=True)
class HomeSnapshot:
    """What the accessory reports after one sampling cycle."""

    temperature_c: float | None = None
    temperature_fault: bool = False
    hold_enabled: bool = False
    hold_active: bool = False
    hold_fault: bool = False
    custom: dict[str, float | None] = field(default_factory=dict)
    custom_faults: frozenset[str] = frozenset()

    @property
    def contact_state(self) -> int:
        return CONTACT_OPEN if self.hold_active else CONTACT_CLOSED

    @property
    def temperature_active(self) -> bool:
        """HomeKit StatusActive: a number was read this cycle."""
        return self.temperature_c is not None


def snapshot_from_reading(
    reading: dict[str, Any] | None,
    hold: dict[str, Any] | None = None,
    suspended: set[str] | frozenset[str] | None = None,
) -> HomeSnapshot:
    """Build the snapshot. ``reading`` is the row the sampling worker stored
    (columns as in ``RulesEngine._SENSOR_TO_COLUMN``); ``hold`` is
    ``IrrigationHold.payload()`` (``None`` when the hold is disabled);
    ``suspended`` is the monitor's faulted-sensor set."""
    r = reading or {}
    susp = set(suspended or ())

    temp = _num(r.get("temp_c"))
    if temp is not None and not TEMPERATURE_MIN_C <= temp <= TEMPERATURE_MAX_C:
        temp = None
    temp_fault = "temperature" in susp

    custom: dict[str, float | None] = {}
    faults: set[str] = set()
    for key, spec in CUSTOM_CHARACTERISTICS.items():
        v = _num(r.get(spec["column"]))
        if v is not None and not spec["min"] <= v <= spec["max"]:
            v = None
        custom[key] = v
        if _COLUMN_TO_SENSOR[spec["column"]] in susp:
            faults.add(key)

    h = hold or {}
    return HomeSnapshot(
        temperature_c=temp,
        temperature_fault=temp_fault,
        hold_enabled=bool(h.get("enabled")),
        hold_active=bool(h.get("active")),
        hold_fault=h.get("fault") is not None,
        custom=custom,
        custom_faults=frozenset(faults),
    )
