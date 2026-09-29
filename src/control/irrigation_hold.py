"""
Irrigation hold — one relay as a normally-closed contact in an irrigation
controller's rain-sensor (SEN) loop.

The irrigator wires relay N's COM and NC terminals to the controller's
rain-sensor input, in place of the rain sensor or in series with it. While
the coil is de-energised COM–NC is closed and the controller runs its own
schedule, exactly as it does with a dry rain sensor. When a water condition
trips, this engine energises the coil, COM–NC opens, the controller reads
"wet" and pauses every zone. When every condition has been clear for the
release period the coil drops and the controller resumes.

**The fail direction is the design.** Power loss, a reboot, a crashed
process or a dead unit all leave the coil de-energised, the contact closed,
and the controller on its normal schedule. Nothing here may invert that: the
engine only ever energises to hold and de-energises to release.

Why this is not a :class:`~control.rules.Rule`: rules cannot arrive from the
cloud (``rules`` is not in ``SETTINGS_SCHEMA``), and every rules-engine guard
— the schedule window, the cooldown, the hourly on-budget, the continuous
on-time ceiling, manual override — would cut an energised hold short and let
the controller water through the very condition it was paused for. So the
hold is its own engine with its own settings, its channel is exempt from the
relay controller's ``max_on_s`` ceiling, and while it owns a channel no rule,
manual command or LoRa downlink may drive that channel.

``evaluate`` is pure logic plus relay writes on transitions only, so it is
unit-testable the way ``RulesEngine`` is: inject a relay double and a clock.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("wqm1.irrigation_hold")

ON_FAULT_MODES = ("release", "hold")

# (reading column, settings key, comparison). The column names are the ones
# rules.py `_SENSOR_TO_COLUMN` uses. A threshold of 0 switches a condition
# off. Tank level is deliberately absent: no tank column exists in firmware
# readings, and a condition on a value the unit does not measure would be a
# condition that can never trip.
CONDITIONS: tuple[tuple[str, str, str], ...] = (
    ("turbidity_ntu", "irrigation_hold_turbidity_ntu", ">="),
    ("tds_ppm", "irrigation_hold_tds_ppm", ">="),
    ("ph", "irrigation_hold_ph_min", "<"),
    ("ph", "irrigation_hold_ph_max", ">"),
    ("flow_rate_gpm", "irrigation_hold_flow_gpm_max", ">="),
)

# Reading column -> the canonical name SensorMonitor.suspended_sensors() uses.
_COLUMN_TO_SENSOR = {
    "turbidity_ntu": "turbidity",
    "tds_ppm": "tds",
    "ph": "ph",
    "flow_rate_gpm": "flow",
}

_COMPARE: dict[str, Callable[[float, float], bool]] = {
    ">=": lambda v, t: v >= t,
    ">": lambda v, t: v > t,
    "<": lambda v, t: v < t,
}

# Words and units for the one-line status the Service Window prints.
_LABELS = {
    "turbidity_ntu": ("turbidity", "NTU"),
    "tds_ppm": ("TDS", "ppm"),
    "ph": ("pH", ""),
    "flow_rate_gpm": ("flow", "gpm"),
}


@dataclass(frozen=True)
class HoldConfig:
    """The settings the engine acts on, snapshotted by :meth:`configure`."""

    enabled: bool = False
    relay: int = 0
    thresholds: tuple[tuple[str, str, str, float], ...] = ()  # (column, key, op, threshold)
    trip_samples: int = 2
    release_s: float = 600.0
    on_fault: str = "release"
    interlock_relay: int = 0

    @classmethod
    def from_settings(cls, settings: Any) -> HoldConfig:
        def num(key: str, default: float = 0.0) -> float:
            try:
                v = float(getattr(settings, key, default) or 0.0)
            except (TypeError, ValueError):
                return default
            return v if math.isfinite(v) else default

        thresholds = tuple((col, key, op, num(key)) for col, key, op in CONDITIONS if num(key) > 0)
        on_fault = str(getattr(settings, "irrigation_hold_on_fault", "release") or "release")
        return cls(
            enabled=bool(getattr(settings, "irrigation_hold_enabled", False)),
            relay=int(getattr(settings, "irrigation_hold_relay", 0) or 0),
            thresholds=thresholds,
            trip_samples=max(1, int(getattr(settings, "irrigation_hold_trip_samples", 2) or 1)),
            release_s=max(0.0, num("irrigation_hold_release_min", 10.0) * 60.0),
            on_fault=on_fault if on_fault in ON_FAULT_MODES else "release",
            interlock_relay=int(getattr(settings, "smart_breaker_interlock_relay", 0) or 0),
        )

    @property
    def conflict(self) -> bool:
        """The hold's channel is the smart-breaker interlock's channel."""
        return self.enabled and 1 <= self.relay <= 4 and self.relay == self.interlock_relay

    @property
    def armed(self) -> bool:
        """Enabled, pointed at a real channel, and not fighting the interlock."""
        return self.enabled and 1 <= self.relay <= 4 and not self.conflict


@dataclass
class HoldState:
    """What the hold is doing right now — the payload's ``irrigationHold``."""

    enabled: bool = False
    relay: int = 0
    active: bool = False
    since: str | None = None
    reasons: list[dict[str, Any]] = field(default_factory=list)
    fault: str | None = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "relay": self.relay,
            "active": self.active,
            "since": self.since,
            "reasons": [dict(r) for r in self.reasons],
            "fault": self.fault,
            "error": self.error,
        }


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class IrrigationHold:
    """The irrigation-hold engine. One per unit.

    Args:
        relays: object exposing ``set(channel, state, unbounded=False)``.
            ``None`` on a unit with no relay controller — the hold then never
            energises anything (the contact stays closed) and says so once.
        clock: seconds, monotonic in production; a virtual unit passes its
            compressed clock so the release delay passes in the emulator.
        wall_clock: timezone-aware now, for the ``since`` stamp.
    """

    def __init__(
        self,
        relays: Any = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._relays = relays
        self._clock = clock
        self._wall = wall_clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._cfg = HoldConfig()
        self._configured = False
        # Logical state: what the conditions say the contact should be.
        self._holding = False
        # Physical state: what the coil was last successfully driven to. A
        # failed relay write leaves these different, and the next evaluate()
        # retries until they agree.
        self._driven = False
        self._driven_channel = 0
        self._trip_count = 0
        self._clear_since: float | None = None
        self._since: str | None = None
        self._reasons: list[dict[str, Any]] = []
        self._fault: str | None = None
        self._last_suspended: set[str] = set()
        self._warned: set[str] = set()

    # -- configuration ---------------------------------------------------------

    def configure(self, settings: Any) -> None:
        """Snapshot the ``irrigation_hold_*`` settings. Idempotent and cheap —
        the sampling worker calls it every cycle so a hot config change
        applies on the next sample even if nobody called it on reload."""
        cfg = HoldConfig.from_settings(settings)
        with self._lock:
            if self._configured and cfg == self._cfg:
                return
            old = self._cfg
            self._cfg = cfg
            self._configured = True

            if self._driven and (not cfg.armed or cfg.relay != self._driven_channel):
                # Disabled, re-pointed or now conflicting: let go of the channel
                # we were holding. A different relay starts from zero.
                self._drive(False)
                self._reset()
            elif not cfg.armed or cfg.relay != old.relay:
                self._reset()

            if cfg.armed and (not old.armed or cfg.relay != old.relay):
                # Taking a channel over: start it released. A coil left on by a
                # rule or a manual command before the hold armed would otherwise
                # keep the controller paused with nothing ever releasing it.
                self._drive(False)

            if cfg.conflict:
                logger.error(
                    "Irrigation hold NOT armed: relay %d is the smart-breaker interlock "
                    "relay. Choose a different relay for the hold.",
                    cfg.relay,
                )
            elif cfg.enabled and not 1 <= cfg.relay <= 4:
                logger.warning(
                    "Irrigation hold is enabled but no relay is chosen "
                    "(irrigation_hold_relay: %d) — nothing is held",
                    cfg.relay,
                )
            elif cfg.armed:
                logger.info(
                    "Irrigation hold armed on relay %d: %s; trip after %d sample(s), "
                    "release after %g min clear, on probe fault: %s",
                    cfg.relay,
                    ", ".join(f"{col} {op} {thr:g}" for col, _, op, thr in cfg.thresholds)
                    or "no conditions set",
                    cfg.trip_samples,
                    cfg.release_s / 60.0,
                    cfg.on_fault,
                )
                if self._relays is None:
                    logger.warning(
                        "Irrigation hold is enabled but this unit has no relay controller — "
                        "the contact cannot be opened"
                    )
            elif old.enabled and not cfg.enabled:
                logger.info("Irrigation hold disabled")

    def _reset(self) -> None:
        self._holding = False
        self._trip_count = 0
        self._clear_since = None
        self._since = None
        self._reasons = []
        self._fault = None

    @property
    def config(self) -> HoldConfig:
        return self._cfg

    def owns(self, channel: int) -> bool:
        """True while the hold is armed on ``channel`` — manual commands, rules
        and downlinks on that channel are refused / set aside."""
        with self._lock:
            return self._cfg.armed and channel == self._cfg.relay

    def reserved_channels(self) -> set[int]:
        with self._lock:
            return {self._cfg.relay} if self._cfg.armed else set()

    # -- evaluation ------------------------------------------------------------

    def evaluate(
        self,
        reading: dict[str, Any],
        suspended: set[str] | None = None,
        now: float | None = None,
    ) -> HoldState:
        """Evaluate one sampling cycle and drive the relay on transitions.

        Args:
            reading: the cycle's reading dict (columns as in rules.py).
            suspended: canonical sensor names the monitor has suspended. None
                means "no information this cycle" (the monitor threw) and
                reuses the last known set — a monitor error must not look like
                a probe recovering.
            now: seconds on the engine's clock; defaults to ``clock()``.
        """
        with self._lock:
            if suspended is not None:
                self._last_suspended = set(suspended)
            now = self._clock() if now is None else now
            cfg = self._cfg
            if self._driven and (not cfg.armed or self._driven_channel != cfg.relay):
                # A release that failed during configure() is retried here.
                self._drive(False)
            if not cfg.armed:
                return self.state()

            tripping: list[dict[str, Any]] = []
            unknown = False
            for col, _key, op, threshold in cfg.thresholds:
                value = reading.get(col)
                if (
                    col in self._last_suspended
                    or _COLUMN_TO_SENSOR.get(col) in self._last_suspended
                    or not isinstance(value, int | float)
                    or isinstance(value, bool)
                    or not math.isfinite(float(value))
                ):
                    unknown = True
                    continue
                if _COMPARE[op](float(value), threshold):
                    tripping.append({"sensor": col, "value": value, "threshold": threshold})

            if unknown and cfg.on_fault == "release":
                # A probe the hold depends on cannot be read. The hold lets go
                # and the controller runs its schedule — even if another
                # condition trips, because a hold we cannot explain is worse
                # than the controller's own schedule.
                if self._holding:
                    logger.warning(
                        "Irrigation hold released on relay %d: a probe it depends on "
                        "is faulted (on_fault: release)",
                        cfg.relay,
                    )
                self._reset()
                self._fault = "released"
            elif tripping or unknown:
                self._trip_count += 1
                self._clear_since = None
                if tripping:
                    self._reasons = tripping
                if not self._holding and self._trip_count >= cfg.trip_samples:
                    self._holding = True
                    self._since = _iso(self._wall())
                    logger.warning(
                        "Irrigation hold ENGAGED on relay %d: %s",
                        cfg.relay,
                        describe_reasons(self._reasons) or "probe fault (on_fault: hold)",
                    )
                self._fault = "held" if unknown and self._holding else None
            else:
                self._trip_count = 0
                self._fault = None
                if self._holding:
                    if self._clear_since is None:
                        self._clear_since = now
                    if now - self._clear_since >= cfg.release_s:
                        logger.info(
                            "Irrigation hold released on relay %d: every condition clear "
                            "for %g min",
                            cfg.relay,
                            cfg.release_s / 60.0,
                        )
                        self._reset()

            if self._holding != self._driven:
                self._drive(self._holding)
            return self.state()

    def _drive(self, on: bool) -> None:
        """Energise (hold) or de-energise (release) the coil. A failure is
        logged and retried on the next sample, never raised."""
        channel = self._cfg.relay if on else (self._driven_channel or self._cfg.relay)
        if self._relays is None:
            if on and "no_relays" not in self._warned:
                self._warned.add("no_relays")
                logger.warning("Irrigation hold wants relay %d open but no relays exist", channel)
            return
        if not 1 <= channel <= 4:
            return
        try:
            if on:
                # Exempt from the continuous on-time ceiling: a hold lasts as
                # long as the condition does, not as long as a dosing pump may.
                self._relays.set(channel, True, unbounded=True)
            else:
                self._relays.set(channel, False)
        except Exception as e:  # noqa: BLE001 — retried next sample
            logger.error(
                "Irrigation hold could not switch relay %d %s: %s (retrying next sample)",
                channel,
                "ON" if on else "OFF",
                e,
            )
            return
        self._driven = on
        self._driven_channel = channel if on else 0

    # -- reporting ---------------------------------------------------------------

    def state(self) -> HoldState:
        with self._lock:
            cfg = self._cfg
            return HoldState(
                enabled=cfg.enabled,
                relay=cfg.relay,
                active=self._driven,
                since=self._since if self._driven else None,
                reasons=[dict(r) for r in self._reasons] if self._driven else [],
                fault=self._fault if cfg.armed else None,
                error="relay_conflict" if cfg.conflict else None,
            )

    def payload(self) -> dict[str, Any] | None:
        """``metadata.irrigationHold`` for the upload — None (key omitted, so a
        unit without the feature sends a byte-identical payload) when the hold
        is disabled."""
        with self._lock:
            if not self._cfg.enabled:
                return None
            return self.state().to_payload()

    def status(self) -> dict[str, Any]:
        """The ``irrigation_hold_status`` command's answer: the payload shape,
        always present, plus the release delay for display."""
        with self._lock:
            out = self.state().to_payload()
            out["releaseMin"] = self._cfg.release_s / 60.0
            return out


def describe_reasons(reasons: list[dict[str, Any]]) -> str:
    """``turbidity 14.2 NTU ≥ 8, pH 5.9 < 6`` — for logs and the Service Window."""
    parts = []
    ops = {
        "turbidity_ntu": "≥",
        "tds_ppm": "≥",
        "flow_rate_gpm": "≥",
    }
    for r in reasons:
        col = str(r.get("sensor", ""))
        label, unit = _LABELS.get(col, (col, ""))
        value, threshold = r.get("value"), r.get("threshold")
        if col == "ph" and isinstance(value, int | float) and isinstance(threshold, int | float):
            op = "<" if value < threshold else ">"
        else:
            op = ops.get(col, "≥")
        v = f"{value:g}" if isinstance(value, int | float) else "?"
        t = f"{threshold:g}" if isinstance(threshold, int | float) else "?"
        parts.append(f"{label} {v}{(' ' + unit) if unit else ''} {op} {t}")
    return ", ".join(parts)


def status_line(status: dict[str, Any] | None) -> str | None:
    """One line for the Service Window status page, or None to omit the row
    (the firmware could not be asked). Describes the contact, never the
    water: "holding" means the contact is open, "clear" means it is closed."""
    if not status or not status.get("ok", True):
        return None
    if not status.get("enabled"):
        return "off"
    if status.get("error") == "relay_conflict":
        return f"not armed — relay {status.get('relay')} is the breaker interlock relay"
    if not 1 <= int(status.get("relay") or 0) <= 4:
        return "enabled, no relay chosen"
    if status.get("active"):
        why = describe_reasons(list(status.get("reasons") or []))
        if status.get("fault") == "held":
            why = (why + ", " if why else "") + "probe fault"
        since = status.get("since") or "—"
        return f"holding since {since}" + (f" ({why})" if why else "")
    if status.get("fault") == "released":
        return "clear — probe fault, released"
    return "clear"
