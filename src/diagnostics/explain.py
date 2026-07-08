"""
Plain-Language Diagnostics

Single source of truth for every human-readable health message the WQM-1
emits — heartbeat ``sensorHealth`` maps, Service Window panels, and monitor
events all come through :func:`explain`.

The copy is written for a NON-TECHNICAL field installer: no jargon, always a
concrete next step. ``explain`` never raises — unknown subjects or states
return a sane generic entry so a firmware/cloud version skew can't crash the
diagnostics path.
"""

from typing import Any

# Sensors (short canonical names used in events, heartbeats, and monitoring).
SENSOR_SUBJECTS: tuple[str, ...] = ("ph", "tds", "turbidity", "temperature", "orp")

# System subsystems (used by the Service Window system-health view).
SYSTEM_SUBJECTS: tuple[str, ...] = ("cloud", "lora", "gps", "storage")

SENSOR_STATES: tuple[str, ...] = (
    "ok",
    "stuck",
    "stuck_no_data",
    "spike",
    "recovered",
    "calibration_overdue",
    "calibration_drift",
)

SYSTEM_STATES: tuple[str, ...] = ("ok", "degraded", "down")

# Display names installers actually use.
_DISPLAY_NAMES: dict[str, str] = {
    "ph": "pH probe",
    "tds": "TDS probe",
    "turbidity": "turbidity sensor",
    "temperature": "temperature probe",
    "orp": "ORP probe",
    "cloud": "cloud connection",
    "lora": "LoRa radio",
    "gps": "GPS receiver",
    "storage": "local storage",
}

# state -> status. Anything unknown maps to "attention" (safe middle ground).
_STATE_STATUS: dict[str, str] = {
    "ok": "ok",
    "recovered": "ok",
    "spike": "attention",
    "calibration_overdue": "attention",
    "calibration_drift": "attention",
    "degraded": "attention",
    "stuck": "fault",
    "stuck_no_data": "fault",
    "down": "fault",
}

# Generic sensor copy per state: (message, likelyCause, action).
# Templates may reference {name}, {minutes}, {age_days} — filled from context
# with safe fallbacks so a missing key can never raise.
_SENSOR_COPY: dict[str, tuple[str, str | None, str | None]] = {
    "ok": ("{name} is reading normally.", None, None),
    "stuck": (
        "{name} reading has been flat for {minutes} minutes.",
        "Probe may be dry, disconnected, or coated in buildup.",
        "Check the probe cable, then rinse the probe tip and retest.",
    ),
    "stuck_no_data": (
        "{name} has returned no data for {minutes} minutes.",
        "The probe is likely unplugged, or its wiring is damaged.",
        "Check the cable and connector. If it stays blank, power-cycle the unit.",
    ),
    "spike": (
        "{name} briefly jumped to an unusual value.",
        "Could be a real water event, an air bubble on the probe, or electrical interference.",
        "No action needed if readings settle. If spikes keep happening, "
        "route the probe cable away from pumps and motors.",
    ),
    "recovered": ("{name} is reading normally again.", None, None),
    "calibration_overdue": (
        "{name} was last calibrated {age_days} days ago.",
        "Probes slowly lose accuracy between calibrations.",
        "Recalibrate the {name} at the next service visit.",
    ),
    "calibration_drift": (
        "{name} readings have drifted from their post-calibration baseline.",
        "Probe aging or fouling can slowly shift readings over weeks.",
        "Recalibrate the {name} soon.",
    ),
}

# Per-sensor refinements where the generic cause/action isn't the best advice.
_SENSOR_OVERRIDES: dict[tuple[str, str], dict[str, str]] = {
    ("ph", "stuck"): {
        "likelyCause": "Probe may be dry, disconnected, or coated in biofilm.",
        "action": "Check the probe cable, then rinse the probe tip and retest.",
    },
    ("tds", "stuck"): {
        "likelyCause": "Probe may be out of the water, disconnected, or scaled over.",
        "action": "Make sure the probe is submerged and the cable is seated, "
        "then wipe the electrodes clean.",
    },
    ("turbidity", "stuck"): {
        "likelyCause": "The lens may be blocked, or the sensor may be "
        "out of the water or disconnected.",
        "action": "Make sure the sensor is submerged, then wipe the lens with a soft cloth.",
    },
    ("temperature", "stuck"): {
        "likelyCause": "The sensor may be disconnected or failing.",
        "action": "Check the temperature sensor cable connection at the enclosure.",
    },
}

# System copy: (subject, state) -> (message, likelyCause, action).
_SYSTEM_COPY: dict[tuple[str, str], tuple[str, str | None, str | None]] = {
    ("cloud", "ok"): ("Cloud sync is working normally.", None, None),
    ("cloud", "degraded"): (
        "Cloud sync is slow or dropping out.",
        "Weak internet connection at the site, or a temporary service issue.",
        "No action needed yet — readings are saved on the device and will catch up.",
    ),
    ("cloud", "down"): (
        "Cloud sync is down. Readings are being saved on the device.",
        "No internet connection, or the cloud service is unreachable.",
        "Check the site's internet connection. Data uploads automatically once it's back.",
    ),
    ("lora", "ok"): ("LoRa radio is transmitting normally.", None, None),
    ("lora", "degraded"): (
        "Some LoRa transmissions are failing.",
        "Weak signal to the gateway, or radio interference.",
        "If this persists, check that the antenna is upright and undamaged.",
    ),
    ("lora", "down"): (
        "LoRa radio is not transmitting.",
        "Antenna problem, radio fault, or the gateway is offline.",
        "Check the antenna connection. Contact support if it doesn't recover.",
    ),
    ("gps", "ok"): ("GPS has a good position fix.", None, None),
    ("gps", "degraded"): (
        "GPS fix is weak or comes and goes.",
        "Poor view of the sky, or nearby interference.",
        "If this persists, move the unit to a spot with a clearer view of the sky.",
    ),
    ("gps", "down"): (
        "GPS has no position fix.",
        "The antenna may be blocked, damaged, or disconnected.",
        "Check the GPS antenna and make sure the unit can see the sky.",
    ),
    ("storage", "ok"): ("Local storage is healthy.", None, None),
    ("storage", "degraded"): (
        "Local storage is filling up.",
        "Readings are piling up faster than they are being synced or cleaned up.",
        "Check the device's connection so it can sync. Contact support if it keeps growing.",
    ),
    ("storage", "down"): (
        "Local storage is failing.",
        "The memory card may be worn out or corrupted.",
        "Contact support to arrange a memory card replacement.",
    ),
}

# Fallback values for template keys so formatting never raises and still
# reads like a sentence ("flat for several minutes").
_CONTEXT_DEFAULTS: dict[str, Any] = {"minutes": "several", "age_days": "many"}


class _SafeDict(dict):
    """format_map helper — unknown placeholders render as themselves."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fmt(template: str | None, ctx: dict[str, Any]) -> str | None:
    if template is None:
        return None
    return template.format_map(_SafeDict(ctx))


def display_name(subject: str) -> str:
    """Installer-facing name for a sensor or system subject."""
    return _DISPLAY_NAMES.get(subject, str(subject) if subject else "device")


def explain(sensor: str, state: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Translate a (subject, state) pair into installer-facing copy.

    Args:
        sensor: Sensor (ph/tds/turbidity/temperature/orp) or system subject
            (cloud/lora/gps/storage). Unknown subjects get generic copy.
        state: Condition name (see SENSOR_STATES / SYSTEM_STATES). Unknown
            states get a generic "unrecognized condition" entry.
        context: Optional template values (e.g. {"minutes": 20,
            "age_days": 104}).

    Returns:
        {"status": "ok"|"attention"|"fault", "message": str,
         "likelyCause": str|None, "action": str|None} — never raises.
    """
    ctx: dict[str, Any] = dict(_CONTEXT_DEFAULTS)
    if context:
        ctx.update({k: v for k, v in context.items() if v is not None})
    ctx["name"] = display_name(sensor)
    ctx["state"] = state

    entry: tuple[str, str | None, str | None] | None = None
    if sensor in SYSTEM_SUBJECTS:
        entry = _SYSTEM_COPY.get((sensor, state))
    elif sensor in SENSOR_SUBJECTS:
        entry = _SENSOR_COPY.get(state)

    if entry is None:
        # Unknown subject and/or state — sane generic, never raise.
        return {
            "status": _STATE_STATUS.get(state, "attention"),
            "message": _fmt("{name} reported a condition we couldn't identify ({state}).", ctx),
            "likelyCause": None,
            "action": "If this keeps appearing, contact BlueSignal support.",
        }

    message, cause, action = entry
    override = _SENSOR_OVERRIDES.get((sensor, state), {})
    cause = override.get("likelyCause", cause)
    action = override.get("action", action)
    return {
        "status": _STATE_STATUS.get(state, "attention"),
        "message": _fmt(message, ctx),
        "likelyCause": _fmt(cause, ctx),
        "action": _fmt(action, ctx),
    }


def all_states() -> list[tuple[str, str]]:
    """Every supported (subject, state) pair — the golden test iterates this."""
    pairs = [(subject, state) for subject in SENSOR_SUBJECTS for state in SENSOR_STATES]
    pairs += [(subject, state) for subject in SYSTEM_SUBJECTS for state in SYSTEM_STATES]
    return pairs
