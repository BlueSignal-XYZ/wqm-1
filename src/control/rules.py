# mypy: ignore-errors
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
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dt_time

logger = logging.getLogger("wqm1.rules")


@dataclass
class Rule:
    """A threshold rule for relay automation."""

    sensor: str  # "ph", "tds_ppm", "turbidity_ntu", "orp_mv", "temp_c"
    operator: str  # ">", "<", ">=", "<=", "=="
    threshold: float
    relay: int  # 1-4
    action: str  # "on" or "off"
    duration_s: int = 0  # 0 = indefinite until condition clears


_OPERATORS = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: abs(a - b) < 0.01,
}


class RulesEngine:
    """Evaluates rules against sensor readings and controls relays."""

    def __init__(self, relay_controller=None):
        self._rules: list[Rule] = []
        self._relay = relay_controller
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
        """Add a threshold rule."""
        self._rules.append(rule)
        logger.info(
            "Rule added: %s %s %.2f → relay %d %s",
            rule.sensor,
            rule.operator,
            rule.threshold,
            rule.relay,
            rule.action,
        )

    def load_rules(self, rules_list: list[dict]) -> None:
        """Load rules from config dict list."""
        self._rules.clear()
        for r in rules_list:
            try:
                self.add_rule(Rule(**r))
            except (TypeError, KeyError) as e:
                logger.warning("Invalid rule %s: %s", r, e)

    def _is_in_schedule(self) -> bool:
        """Check if the current time is within the schedule window."""
        if not self._schedule_enabled:
            return True
        now = datetime.now().time()
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
        current_hour = datetime.now().hour
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
        current_hour = datetime.now().hour
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
        actions = []

        # --- Guard: schedule window ---
        if not self._is_in_schedule():
            logger.debug("Outside schedule window, skipping rules")
            return actions

        # --- Guard: manual override ---
        if self._manual_override:
            logger.debug("Manual override active, skipping rules")
            return actions

        now_mono = time.monotonic()

        # Accumulate on-time for relays that are currently on
        for relay, since in list(self._on_since.items()):
            elapsed = now_mono - since
            self._accumulate_on_time(relay, elapsed)
            self._on_since[relay] = now_mono

        for rule in self._rules:
            value = reading.get(rule.sensor)
            if value is None:
                continue

            op_fn = _OPERATORS.get(rule.operator)
            if op_fn is None:
                continue

            if op_fn(value, rule.threshold):
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

        # Check auto-shutoff timers
        expired = [ch for ch, t in self._timers.items() if now_mono >= t]
        for ch in expired:
            actions.append((ch, False))
            self._last_off[ch] = now_mono
            self._on_since.pop(ch, None)
            del self._timers[ch]

        # Apply actions to relay controller
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
