"""
Plain-language device health for the Service Window.

Builds the traffic-light card model (subject -> {status, message,
likelyCause, action}) an installer sees on the home page and in the setup
wizard's go/no-go checklist. All copy comes from diagnostics.explain — the
same single source of truth that feeds heartbeat ``sensorHealth`` and the
Cloud dashboard, so the device says the same thing everywhere.

Read-only: everything is derived from the SQLite buffer + config files, so
this works even while the main firmware service is restarting.
"""

import statistics
from datetime import UTC, datetime
from typing import Any

from diagnostics.explain import explain

# Reading column -> canonical sensor name (matches sensing.monitor).
_FIELD_TO_SENSOR = {
    "ph": "ph",
    "tds_ppm": "tds",
    "turbidity_ntu": "turbidity",
    "temp_c": "temperature",
    "orp_mv": "orp",
}

# Quick flatline screen over the recent window (mirrors sensing.monitor's
# noise floors; this is a coarse read-side check, not the live detector).
_NOISE_FLOOR = {
    "ph": 0.02,
    "tds": 5.0,
    "turbidity": 1.0,
    "temperature": 0.05,
    "orp": 2.0,
}

# A reading is "recent" within 3x the default sample cadence.
_RECENT_S = 3 * 60


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def sensor_cards(
    readings: list[dict[str, Any]],
    orp_enabled: bool = False,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Per-sensor plain-language card from the recent readings (newest first).
    A sensor is: fault when its recent values are all missing or flat;
    ok otherwise. Disabled sensors (ORP without hardware) say so.
    """
    now = now or datetime.now(UTC)
    cards: dict[str, dict[str, Any]] = {}
    latest = readings[0] if readings else None
    latest_at = _parse_ts(latest.get("timestamp")) if latest else None
    stale = latest_at is None or (now - latest_at).total_seconds() > _RECENT_S

    for field, sensor in _FIELD_TO_SENSOR.items():
        if sensor == "orp" and not orp_enabled:
            cards[sensor] = explain(sensor, "disabled")
            continue
        if not readings or stale:
            cards[sensor] = explain(sensor, "stuck_no_data", {"minutes": _RECENT_S // 60})
            continue
        values = [r.get(field) for r in readings[:30]]
        present = [float(v) for v in values if v is not None]
        if not present:
            cards[sensor] = explain(sensor, "stuck_no_data", {"minutes": _RECENT_S // 60})
        elif len(present) >= 10 and statistics.pstdev(present) < _NOISE_FLOOR[sensor]:
            cards[sensor] = explain(sensor, "stuck", {"minutes": len(present)})
        else:
            cards[sensor] = explain(sensor, "ok")
    return cards


def system_cards(
    readings: list[dict[str, Any]],
    config: dict[str, Any],
    lora_session: dict[str, Any] | None,
    reading_count: int,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Cloud / LoRa / GPS / storage cards."""
    now = now or datetime.now(UTC)
    cards: dict[str, dict[str, Any]] = {}

    # Cloud: configured + recent rows actually syncing (synced flag on latest).
    cloud_enabled = bool(config.get("cloud_enabled")) and bool(config.get("api_key"))
    if not cloud_enabled:
        cards["cloud"] = explain("cloud", "down")
    else:
        recent_synced = any(r.get("synced") for r in readings[:20])
        cards["cloud"] = explain("cloud", "ok" if recent_synced else "degraded")

    # LoRa: joined session.
    lora_joined = bool(lora_session and lora_session.get("joined"))
    cards["lora"] = explain("lora", "ok" if lora_joined else "degraded")

    # GPS: latest reading carries a fix.
    latest = readings[0] if readings else None
    has_fix = bool(latest and latest.get("lat") is not None and latest.get("lon") is not None)
    cards["gps"] = explain("gps", "ok" if has_fix else "degraded")

    # Storage: readings are landing at all.
    fresh = bool(latest and _parse_ts(latest.get("timestamp")))
    if fresh:
        age_s = (now - _parse_ts(latest["timestamp"])).total_seconds()
        fresh = age_s <= _RECENT_S
    cards["storage"] = explain("storage", "ok" if (reading_count and fresh) else "stale")

    return cards


def worst_status(cards: dict[str, dict[str, Any]]) -> str:
    """Aggregate status across cards: fault > attention > ok."""
    statuses = {c.get("status") for c in cards.values()}
    if "fault" in statuses:
        return "fault"
    if "attention" in statuses:
        return "attention"
    return "ok"
