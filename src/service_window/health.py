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
from sensing.monitor import FIELD_TO_SENSOR as _FIELD_TO_SENSOR
from sensing.monitor import NOISE_FLOOR as _NOISE_FLOOR

# A reading is "recent" within 3x the default sample cadence.
_RECENT_S = 3 * 60


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _sensor_enabled(sensor: str, orp_enabled: bool, config: dict[str, Any] | None) -> bool:
    """Which sensors this unit actually has, from its config."""
    cfg = config or {}
    if sensor == "orp":
        return orp_enabled or bool(cfg.get("rs485_orp_enabled"))
    if sensor == "chlorine":
        return bool(cfg.get("rs485_chlorine_enabled"))
    if sensor in ("conductivity", "salinity"):
        return bool(cfg.get("rs485_multi_enabled"))
    # The core four are no longer assumed fitted. Treating them as always
    # present is what made a disconnected electrode look like a working one on
    # this very page: it reported "pH probe is reading normally" for a channel
    # with nothing attached. Absent from config = fitted, so units upgrading
    # from before these keys existed keep their current behaviour.
    if sensor in ("ph", "tds", "turbidity", "temperature"):
        return bool(cfg.get(f"{sensor}_enabled", True))
    return True


def sensor_cards(
    readings: list[dict[str, Any]],
    orp_enabled: bool = False,
    now: datetime | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Per-sensor plain-language card from the recent readings (newest first).
    A sensor is: fault when its recent values are all missing or flat;
    ok otherwise. Disabled sensors (ORP without hardware, RS485 probes not
    fitted) say so.
    """
    now = now or datetime.now(UTC)
    cards: dict[str, dict[str, Any]] = {}
    latest = readings[0] if readings else None
    latest_at = _parse_ts(latest.get("timestamp")) if latest else None
    stale = latest_at is None or (now - latest_at).total_seconds() > _RECENT_S

    for field, sensor in _FIELD_TO_SENSOR.items():
        if not _sensor_enabled(sensor, orp_enabled, config):
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

    # Storage: readings are landing at all. Parse ONCE and keep the result —
    # the previous double-parse both wasted work and, if the second parse ever
    # disagreed with the first (timestamp mutated, parse non-deterministic on
    # bad input), subtracted None from a datetime at runtime.
    latest_ts = _parse_ts(latest.get("timestamp")) if latest else None
    fresh = latest_ts is not None and (now - latest_ts).total_seconds() <= _RECENT_S
    cards["storage"] = explain("storage", "ok" if (reading_count and fresh) else "stale")

    # RS485 bus: only shown when Modbus probes are enabled. A dead bus is one
    # fault (adapter / 12V), so it gets its own card instead of three probe
    # faults. Judged from whether the enabled probes' fields carry data.
    rs485_fields = []
    if config.get("rs485_chlorine_enabled"):
        rs485_fields.append("chlorine_mgl")
    if config.get("rs485_orp_enabled"):
        rs485_fields.append("orp_mv")
    if config.get("rs485_multi_enabled"):
        rs485_fields.append("conductivity_uscm")
    if rs485_fields:
        recent = readings[:10]
        answering = [f for f in rs485_fields if any(r.get(f) is not None for r in recent)]
        if not recent or not answering:
            cards["rs485"] = explain("rs485", "down")
        elif len(answering) < len(rs485_fields):
            cards["rs485"] = explain("rs485", "degraded")
        else:
            cards["rs485"] = explain("rs485", "ok")

    return cards


def smart_breaker_card(config: dict[str, Any], awg: dict[str, Any] | None) -> dict[str, Any] | None:
    """Traffic-light card for the AWG smart breaker link, or None when no
    vendor is bound (so an unbound unit's dashboard is untouched).

    Unlike the other system cards this needs the LIVE controller snapshot
    (``awg_status`` over the command socket) — link health is not in SQLite.
    ``awg`` is that snapshot, or None / ``{"ok": False}`` when the firmware
    did not answer, which reads as *stale* rather than as a breaker fault.
    """
    vendor = str(config.get("smart_breaker_vendor") or "none")
    if vendor == "none":
        return None
    if vendor == "relay_only":
        # No link to judge — the relay is on the Relays page and the firmware
        # drives it locally, so there is nothing that can be "down" here.
        return None

    ctx: dict[str, Any] = {
        "circuit": config.get("smart_breaker_circuit_label") or None,
        "fail_safe": str(config.get("smart_breaker_fail_safe") or "off").upper(),
    }
    if not awg or not awg.get("ok") or not awg.get("configured"):
        return explain("smart_breaker", "stale", ctx)

    breaker = awg.get("breaker") or {}
    if isinstance(breaker, dict) and breaker.get("isOn") is not None:
        ctx["position"] = "ON" if breaker.get("isOn") else "OFF"
    ctx["fail_safe"] = str(awg.get("failSafe") or ctx["fail_safe"]).upper()
    ctx["minutes"] = max(1, int(awg.get("unreachableForS") or 0) // 60)

    if awg.get("linkOk"):
        return explain("smart_breaker", "ok", ctx)
    if awg.get("failSafeApplied"):
        return explain("smart_breaker", "down", ctx)
    return explain("smart_breaker", "degraded", ctx)


def worst_status(cards: dict[str, dict[str, Any]]) -> str:
    """Aggregate status across cards: fault > attention > ok."""
    statuses = {c.get("status") for c in cards.values()}
    if "fault" in statuses:
        return "fault"
    if "attention" in statuses:
        return "attention"
    return "ok"
