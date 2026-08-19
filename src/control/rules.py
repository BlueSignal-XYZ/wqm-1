"""
Threshold-Based Relay Automation Rules Engine

Evaluates sensor readings against configurable thresholds and
triggers relay actions. Also handles relay commands from LoRaWAN downlinks.

Safety features (loaded from policies.yaml):
- Schedule window: rules only fire during configured hours
- Cooldown: prevents rapid relay cycling after turn-off
- Max on-time per hour: limits cumulative relay activation
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as dt_time
from typing import Any

logger = logging.getLogger("wqm1.rules")


def _utc_now() -> datetime:
    """Default wall clock: timezone-aware UTC."""
    return datetime.now(UTC)


@dataclass
class Rule:
    """A threshold rule for relay automation.

    Exactly one of ``threshold`` / ``adaptive_k`` must be set. A fixed rule
    compares against ``threshold``. An adaptive rule compares against the
    site's own learned normal: median ± k·σ̂ for the current UTC hour, taken
    from the water profile installed via :meth:`RulesEngine.set_baselines`.
    An adaptive rule with no established baseline does not fire at all —
    never actuate a load off a normal we have not established.
    """

    sensor: str  # "ph", "tds_ppm", "turbidity_ntu", "orp_mv", "temp_c"
    operator: str  # ">", "<", ">=", "<=", "==" ("==" is fixed-only)
    relay: int  # 1-4
    action: str  # "on" or "off"
    threshold: float | None = None
    adaptive_k: float | None = None  # band width in σ̂ units, 0.5–10
    duration_s: int = 0  # 0 = indefinite until condition clears


_OPERATORS = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: abs(a - b) < 0.01,
}

# MAD → σ̂ under normality; must match MAD_TO_SIGMA in the cloud engine
# (functions/v2/baselines.js) so edge and cloud agree on what "unusual" means.
_MAD_TO_SIGMA = 1.4826


class RulesEngine:
    """Evaluates rules against sensor readings and controls relays."""

    def __init__(
        self,
        relay_controller: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Args:
            relay_controller: Object exposing ``set(channel, state)``.
            clock: Callable returning the current timezone-aware datetime.
                Defaults to UTC wall time; inject a fixed clock to make
                schedule-window and hourly-budget checks deterministic.
        """
        self._rules: list[Rule] = []
        self._relay = relay_controller
        self._clock = clock or _utc_now
        # Track auto-shutoff timers: {relay_channel: shutoff_time}
        self._timers: dict[int, float] = {}

        # --- Policy settings ---
        self._schedule_enabled = False
        self._schedule_start: dt_time | None = None
        self._schedule_end: dt_time | None = None
        self._cooldown_s = 0
        self._max_on_s_per_hour = 0  # 0 = unlimited
        self._manual_override = False

        # --- Per-relay cooldown tracking: relay → monotonic time of last OFF ---
        self._last_off: dict[int, float] = {}

        # --- Per-relay on-time tracking: relay → (hour, cumulative_seconds) ---
        self._on_time: dict[int, tuple[int, float]] = {}
        # relay → monotonic time when it was last turned ON (for accumulation)
        self._on_since: dict[int, float] = {}

        # --- Sensor-health suspension: reading columns whose rules are paused
        # because the sensor monitor flagged the probe as stuck/faulted. A
        # flatlined probe must never keep (or start) actuating a relay on
        # frozen data.
        self._suspended_columns: set[str] = set()
        # Channels awaiting a one-shot drop to fail-safe because the sensor
        # driving them was just suspended. See _revert_suspended_to_failsafe.
        self._pending_failsafe: set[int] = set()

        # --- Learned water profile (adaptive rules) ---
        # Canonical param -> {buckets: [{m, mad, n}|None ×24], overall: {m, mad},
        # learning?: bool}, as computed by the cloud baseline engine and synced
        # down through the device config channel. Empty until set_baselines.
        self._baselines: dict[str, Any] = {}

    # Canonical monitor sensor names -> reading/rule column names.
    _SENSOR_TO_COLUMN = {
        "ph": "ph",
        "tds": "tds_ppm",
        "turbidity": "turbidity_ntu",
        "temperature": "temp_c",
        "orp": "orp_mv",
        "chlorine": "chlorine_mgl",
        "conductivity": "conductivity_uscm",
        "salinity": "salinity_ppt",
    }
    # Reading column -> canonical name, for water-profile lookups (the cloud
    # baseline engine keys its params canonically: "tds", not "tds_ppm").
    _COLUMN_TO_PARAM = {v: k for k, v in _SENSOR_TO_COLUMN.items()}

    # Per-column σ̂ floors, mirroring PROFILE_PARAMS madFloor in
    # functions/v2/baselines.js. A flat-lined history has MAD 0; without a
    # floor an adaptive band collapses to zero width and any wiggle actuates.
    _MAD_FLOORS = {
        "ph": 0.05,
        "temp_c": 0.2,
        "tds_ppm": 5.0,
        "turbidity_ntu": 0.5,
        "orp_mv": 5.0,
        "chlorine_mgl": 0.05,
        "conductivity_uscm": 10.0,
    }

    def set_suspended_sensors(self, sensors: set[str]) -> None:
        """
        Pause rules for the given canonical sensor names (from SensorMonitor),
        and queue every channel they drive to drop to its fail-safe state.

        Only *newly* suspended columns queue a reversion. A sensor that stays
        suspended across cycles must not re-issue OFF every 60 s — the operator
        may deliberately be holding a channel on by hand while swapping a probe.
        """
        columns = {self._SENSOR_TO_COLUMN.get(s, s) for s in sensors}
        newly = columns - self._suspended_columns
        if columns != self._suspended_columns:
            logger.warning(
                "Rule suspension changed: %s",
                ", ".join(sorted(columns)) if columns else "none",
            )
        if newly:
            self._pending_failsafe |= {r.relay for r in self._rules if r.sensor in newly}
        self._suspended_columns = columns

    def load_policies(self, policies: dict) -> None:
        """Load safety policies from a policies dict (policies.yaml format)."""
        schedule = policies.get("schedule", {})
        if schedule.get("enabled", False):
            self._schedule_enabled = True
            start_str = schedule.get("start", "00:00")
            end_str = schedule.get("end", "23:59")
            sh, sm = (int(x) for x in start_str.split(":"))
            eh, em = (int(x) for x in end_str.split(":"))
            self._schedule_start = dt_time(sh, sm)
            self._schedule_end = dt_time(eh, em)
            logger.info("Schedule window: %s – %s", start_str, end_str)

        limits = policies.get("limits", {})
        self._cooldown_s = limits.get("cooldown_seconds", 0)
        max_min = limits.get("max_on_minutes_per_hour", 0)
        self._max_on_s_per_hour = max_min * 60 if max_min else 0

        manual = policies.get("manual", {})
        self._manual_override = manual.get("override", False)

        logger.info(
            "Policies loaded: cooldown=%ds, max_on=%dmin/hr, manual_override=%s",
            self._cooldown_s,
            max_min,
            self._manual_override,
        )

    def add_rule(self, rule: Rule) -> None:
        """Add a threshold rule (fixed or adaptive). Invalid rules are
        rejected with a warning rather than raising — one bad rule must not
        take down the rest of a loaded rule set."""
        if rule.adaptive_k is not None:
            if rule.operator == "==":
                logger.warning(
                    "Adaptive rule rejected: '==' has no band direction (%s)", rule.sensor
                )
                return
            if not isinstance(rule.adaptive_k, int | float) or not 0.5 <= rule.adaptive_k <= 10:
                logger.warning(
                    "Adaptive rule rejected: adaptive_k %r outside 0.5–10 (%s)",
                    rule.adaptive_k,
                    rule.sensor,
                )
                return
        elif rule.threshold is None:
            logger.warning("Rule rejected: neither threshold nor adaptive_k (%s)", rule.sensor)
            return
        self._rules.append(rule)
        logger.info(
            "Rule added: %s %s %s → relay %d %s",
            rule.sensor,
            rule.operator,
            f"baseline±{rule.adaptive_k}σ̂" if rule.adaptive_k is not None else rule.threshold,
            rule.relay,
            rule.action,
        )

    def load_rules(self, rules_list: list[dict]) -> None:
        """Load rules from config dict list."""
        self._rules.clear()
        for r in rules_list:
            try:
                r = dict(r)
                # The cloud stores the adaptive width camelCase; accept both.
                if "adaptiveK" in r:
                    r.setdefault("adaptive_k", r.pop("adaptiveK"))
                self.add_rule(Rule(**r))
            except (TypeError, KeyError) as e:
                logger.warning("Invalid rule %s: %s", r, e)

    def set_baselines(self, profile: dict | None) -> None:
        """
        Install the learned water profile (the cloud baseline engine's output,
        synced down via the device config channel or loaded from local cache).

        Only the ``params`` map is kept. Passing None or a profile without
        params clears the baselines, which silences every adaptive rule —
        the safe direction: no established normal, no actuation.
        """
        params = (profile or {}).get("params")
        self._baselines = params if isinstance(params, dict) else {}
        if self._baselines:
            established = [k for k, v in self._baselines.items() if not v.get("learning")]
            logger.info(
                "Water profile installed: %s established, %d learning",
                ", ".join(sorted(established)) or "none",
                len(self._baselines) - len(established),
            )
        else:
            logger.info("Water profile cleared — adaptive rules idle")

    def _resolve_threshold(self, rule: Rule) -> float | None:
        """
        The value this rule compares against right now.

        Fixed rules return their threshold. Adaptive rules resolve
        median ± k·σ̂ for the current UTC hour from the learned baseline —
        ``+`` for ">"/">=" (fire above the site's normal band), ``-`` for
        "<"/"<=" (fire below it). Returns None — rule does not fire — when
        the baseline is absent or still learning.
        """
        if rule.adaptive_k is None:
            return rule.threshold
        param = self._COLUMN_TO_PARAM.get(rule.sensor, rule.sensor)
        baseline = self._baselines.get(param)
        if not isinstance(baseline, dict) or baseline.get("learning"):
            return None
        hour = self._clock().astimezone(UTC).hour
        buckets = baseline.get("buckets") or []
        stats = buckets[hour] if hour < len(buckets) and buckets[hour] else None
        stats = stats or baseline.get("overall")
        if not isinstance(stats, dict) or not isinstance(stats.get("m"), int | float):
            return None
        floor = self._MAD_FLOORS.get(rule.sensor, 0.5)
        sigma = max((stats.get("mad") or 0) * _MAD_TO_SIGMA, floor)
        if rule.operator in (">", ">="):
            return stats["m"] + rule.adaptive_k * sigma
        return stats["m"] - rule.adaptive_k * sigma

    def _is_in_schedule(self) -> bool:
        """Check if the current time is within the schedule window."""
        if not self._schedule_enabled:
            return True
        if self._schedule_start is None or self._schedule_end is None:
            return True
        now = self._clock().time()
        if self._schedule_start <= self._schedule_end:
            return self._schedule_start <= now <= self._schedule_end
        # Overnight window (e.g. 22:00 – 06:00)
        return now >= self._schedule_start or now <= self._schedule_end

    def _is_in_cooldown(self, relay: int) -> bool:
        """Check if a relay is still in its cooldown period."""
        if self._cooldown_s <= 0:
            return False
        last = self._last_off.get(relay)
        if last is None:
            return False
        return (time.monotonic() - last) < self._cooldown_s

    def _check_on_time_budget(self, relay: int) -> bool:
        """Return True if the relay still has on-time budget this hour."""
        if self._max_on_s_per_hour <= 0:
            return True
        current_hour = self._clock().hour
        entry = self._on_time.get(relay)
        if entry is None or entry[0] != current_hour:
            # New hour — reset accumulator
            self._on_time[relay] = (current_hour, 0.0)
            return True
        return entry[1] < self._max_on_s_per_hour

    def _accumulate_on_time(self, relay: int, seconds: float) -> None:
        """Add seconds to a relay's hourly on-time counter."""
        if self._max_on_s_per_hour <= 0:
            return
        current_hour = self._clock().hour
        entry = self._on_time.get(relay)
        if entry is None or entry[0] != current_hour:
            self._on_time[relay] = (current_hour, seconds)
        else:
            self._on_time[relay] = (current_hour, entry[1] + seconds)

    def evaluate(self, reading: dict) -> list[tuple[int, bool]]:
        """
        Evaluate all rules against a sensor reading.

        Args:
            reading: Dict with sensor keys (ph, tds_ppm, etc.)

        Returns:
            List of (relay_channel, state) actions to take.
        """
        actions: list[tuple[int, bool]] = []
        now_mono = time.monotonic()

        # --- De-energizing runs BEFORE the guards, always ---
        #
        # Both of the calls below can only ever turn a channel OFF, so neither
        # the schedule window nor manual override may skip them. Turning things
        # on is discretionary; letting go of a load is not.
        #
        # Without this, a relay switched on at 20:59 with a 30 s duration and a
        # window closing at 21:00 stayed on until the window reopened, because
        # the timer sweep sat below an early `return`.
        actions.extend(self._revert_suspended_to_failsafe())
        actions.extend(self._expire_timers(now_mono))

        # --- Guard: schedule window ---
        if not self._is_in_schedule():
            logger.debug("Outside schedule window, skipping rules")
            return self._apply(actions)

        # --- Guard: manual override ---
        if self._manual_override:
            logger.debug("Manual override active, skipping rules")
            return self._apply(actions)

        # Accumulate on-time for relays that are currently on
        for relay, since in list(self._on_since.items()):
            elapsed = now_mono - since
            self._accumulate_on_time(relay, elapsed)
            self._on_since[relay] = now_mono

        for rule in self._rules:
            if rule.sensor in self._suspended_columns:
                logger.debug("Rule for %s suspended (sensor health)", rule.sensor)
                continue
            value = reading.get(rule.sensor)
            if value is None:
                continue

            op_fn = _OPERATORS.get(rule.operator)
            if op_fn is None:
                continue

            threshold = self._resolve_threshold(rule)
            if threshold is None:
                # Adaptive rule with no established baseline yet — stays quiet.
                continue

            if op_fn(value, threshold):
                state = rule.action == "on"

                if state:
                    # --- Guard: cooldown ---
                    if self._is_in_cooldown(rule.relay):
                        logger.debug("Relay %d in cooldown, skipping ON", rule.relay)
                        continue

                    # --- Guard: max on-time budget ---
                    if not self._check_on_time_budget(rule.relay):
                        logger.debug(
                            "Relay %d exceeded on-time budget, skipping",
                            rule.relay,
                        )
                        continue

                    # Track on-start for accumulation
                    if rule.relay not in self._on_since:
                        self._on_since[rule.relay] = now_mono

                actions.append((rule.relay, state))

                # Set auto-shutoff timer if duration specified
                if state and rule.duration_s > 0:
                    self._timers[rule.relay] = now_mono + rule.duration_s

                # Track OFF for cooldown
                if not state:
                    self._last_off[rule.relay] = now_mono
                    self._on_since.pop(rule.relay, None)

        return self._apply(actions)

    def _expire_timers(self, now_mono: float) -> list[tuple[int, bool]]:
        """Auto-shutoff sweep: channels whose ``duration_s`` has elapsed."""
        actions: list[tuple[int, bool]] = []
        for ch in [ch for ch, t in self._timers.items() if now_mono >= t]:
            actions.append((ch, False))
            self._last_off[ch] = now_mono
            self._on_since.pop(ch, None)
            del self._timers[ch]
        return actions

    def _revert_suspended_to_failsafe(self) -> list[tuple[int, bool]]:
        """
        De-energize every channel driven by a sensor that has just been
        suspended, and forget its auto-shutoff timer.

        **De-energizing IS the fail-safe state, and that is the whole design.**
        Fail-safe direction is set by the wiring, not by firmware: a load on
        COM→NO stops when the coil drops, a load on COM→NC runs. Section 05 of
        the installer manual requires life-critical loads (aeration,
        circulation) on NC precisely so that a dead controller leaves them
        running. Dropping the coil therefore puts every channel in exactly the
        state its installer chose for "the controller is not to be trusted right
        now" — without firmware needing to know, or be told correctly, which way
        each channel was wired. A firmware-side fail-safe table would be a
        second copy of a fact that already exists in the field wiring, and the
        copy would be the one that goes stale.

        Suspension alone used to just stop evaluating the rule, which left the
        relay wherever it happened to be. For a rule with ``duration_s: 0``
        ("hold until the condition clears" — what the shipped dosing examples
        use), a channel energized at the moment its probe froze stayed energized
        indefinitely, because the rule that would have released it no longer
        ran. On a dosing pump that is a chemical overfeed driven by a reading
        that stopped being true.

        Fires once per suspension transition, not every cycle: re-issuing OFF
        every 60 s would bury the log and defeat the operator's ability to
        override a channel by hand while a probe is being replaced.
        """
        if not self._pending_failsafe:
            return []
        actions: list[tuple[int, bool]] = []
        for ch in sorted(self._pending_failsafe):
            actions.append((ch, False))
            self._last_off[ch] = time.monotonic()
            self._on_since.pop(ch, None)
            self._timers.pop(ch, None)
        logger.warning(
            "Sensor health: reverting relay(s) %s to fail-safe (de-energized) — "
            "driving sensor(s) %s suspended",
            ", ".join(str(c) for c in sorted(self._pending_failsafe)),
            ", ".join(sorted(self._suspended_columns)) or "none",
        )
        self._pending_failsafe.clear()
        return actions

    def _apply(self, actions: list[tuple[int, bool]]) -> list[tuple[int, bool]]:
        """Push actions to the relay controller and return them."""
        if self._relay and actions:
            for channel, state in actions:
                try:
                    self._relay.set(channel, state)
                except Exception as e:
                    logger.error("Relay %d action failed: %s", channel, e)
        return actions

    def process_downlink_command(self, fport: int, payload: bytes) -> bool:
        """
        Process relay command from LoRaWAN downlink.

        FPort 100: Relay control
            Byte 0: relay channel (1-4)
            Byte 1: state (0=off, 1=on)
            Bytes 2-3: duration in seconds (big-endian, 0=indefinite)

        Returns:
            True if command was valid and executed.
        """
        if fport != 100 or len(payload) < 2:
            return False

        channel = payload[0]
        state = bool(payload[1])
        duration = int.from_bytes(payload[2:4], "big") if len(payload) >= 4 else 0

        if not 1 <= channel <= 4:
            logger.warning("Invalid relay channel in downlink: %d", channel)
            return False

        logger.info(
            "Downlink relay command: ch=%d %s duration=%ds",
            channel,
            "ON" if state else "OFF",
            duration,
        )

        if self._relay:
            self._relay.set(channel, state)
            if state and duration > 0:
                self._timers[channel] = time.monotonic() + duration

        return True
