"""
Threshold-Based Relay Automation Rules Engine

Evaluates sensor readings against configurable thresholds and
triggers relay actions. Also handles relay commands from LoRaWAN downlinks.
"""

import logging
import time
from dataclasses import dataclass

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

    def evaluate(self, reading: dict) -> list[tuple[int, bool]]:
        """
        Evaluate all rules against a sensor reading.

        Args:
            reading: Dict with sensor keys (ph, tds_ppm, etc.)

        Returns:
            List of (relay_channel, state) actions to take.
        """
        actions = []

        for rule in self._rules:
            value = reading.get(rule.sensor)
            if value is None:
                continue

            op_fn = _OPERATORS.get(rule.operator)
            if op_fn is None:
                continue

            if op_fn(value, rule.threshold):
                state = rule.action == "on"
                actions.append((rule.relay, state))

                # Set auto-shutoff timer if duration specified
                if state and rule.duration_s > 0:
                    self._timers[rule.relay] = time.monotonic() + rule.duration_s

        # Check auto-shutoff timers
        now = time.monotonic()
        expired = [ch for ch, t in self._timers.items() if now >= t]
        for ch in expired:
            actions.append((ch, False))
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
