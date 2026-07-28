"""
Safety-aware relay channel controller (Commercial tier).

Every actuation goes through this module. Nothing else calls
``RelayController.set`` once this is wired in.

THE MODEL
---------
A dead Pi cannot energise a coil, so the only state a crashed, hung, or
unpowered device can hold is DE-ENERGISED. Fail-safe therefore always means
de-energised, and the contact type decides what that does physically:

    contact=NC  ->  de-energised = load RUNNING
    contact=NO  ->  de-energised = load STOPPED

Callers speak in LOAD state ("run the aerator"), never in coil state. This
module is the only place that translates load state to a coil level, using the
channel's declared contact:

    coil_energised = run XOR (contact == NC)

That is why a rule saying ``action: "on"`` does the intuitive thing on both
wiring styles: on an NC aeration channel "on" means de-energise, because a
de-energised NC contact is closed and the aerator runs.

WHAT THIS GUARANTEES
--------------------
* A channel that has not been commissioned never actuates.
* Dwell limits (min on/off, min interval) protect contactor coils and pump
  motors — and are ALWAYS bypassed for a fail-safe reversion. Anti-chatter must
  never delay a safety action.
* Reverting to fail-safe is a single operation — de-energise — identical in
  software fault, power loss, and crash. No path to safety requires energising.
* Every transition is logged with its cause and bumps a wear counter.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from utils.config import (
    CAUSE_OTA,
    CAUSE_STALENESS,
    CONTACT_NC,
    FAIL_SAFE_RUN,
    ChannelConfig,
)

logger = logging.getLogger("wqm1.channel")


class ChannelController:
    """Translates load-state requests into safe coil transitions."""

    def __init__(
        self,
        relay_controller: Any,
        configs: dict[int, ChannelConfig] | None = None,
        db: Any = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._relay = relay_controller
        self._configs: dict[int, ChannelConfig] = dict(configs or {})
        self._db = db
        # Injectable so dwell tests are deterministic instead of sleeping.
        self._now = clock or time.monotonic

        # Desired LOAD state per channel (True = running).
        self._run: dict[int, bool] = {}
        # Monotonic timestamps of the last transition into the current state.
        self._since: dict[int, float] = {}
        self._last_transition: dict[int, float] = {}
        # Consecutive cycles without a valid driving reading.
        self._misses: dict[int, int] = {}
        self._cycles: dict[int, int] = {}
        self._last_cause: dict[int, str] = {}

        self._load_cycle_counters()

        # Boot inert: every channel starts at its fail-safe, which is always
        # de-energised. RelayController already drives the pins LOW at init;
        # this seeds our own view of load state to match the wiring.
        for ch, cfg in self._configs.items():
            self._run[ch] = cfg.fail_safe_state == FAIL_SAFE_RUN
            self._since[ch] = self._now()

    # -- config ------------------------------------------------------------

    def set_configs(self, configs: dict[int, ChannelConfig]) -> None:
        """
        Replace channel configs (config reload).

        Any channel that disappears or becomes uncommissioned is driven to
        fail-safe first — we never leave a coil energised under a config we no
        longer hold.
        """
        for ch in list(self._configs):
            new = configs.get(ch)
            if new is None or not new.commissioned:
                self.revert_to_fail_safe(ch, cause="setpoint", reason="config reload")
        self._configs = dict(configs)
        for ch, cfg in self._configs.items():
            self._run.setdefault(ch, cfg.fail_safe_state == FAIL_SAFE_RUN)
            self._since.setdefault(ch, self._now())

    def config(self, channel: int) -> ChannelConfig | None:
        return self._configs.get(channel)

    @property
    def channels(self) -> list[int]:
        return sorted(self._configs)

    # -- coil translation --------------------------------------------------

    @staticmethod
    def _coil_for(cfg: ChannelConfig, run: bool) -> bool:
        """True = energise the coil. See the module docstring for the truth table."""
        return run != (cfg.contact == CONTACT_NC)

    def _drive(self, cfg: ChannelConfig, run: bool) -> None:
        self._relay.set(cfg.channel, self._coil_for(cfg, run))

    # -- queries -----------------------------------------------------------

    def is_running(self, channel: int) -> bool:
        """Current LOAD state (not coil state)."""
        return bool(self._run.get(channel, False))

    def cycles(self, channel: int) -> int:
        return self._cycles.get(channel, 0)

    def last_cause(self, channel: int) -> str | None:
        return self._last_cause.get(channel)

    def is_commissioned(self, channel: int) -> bool:
        cfg = self._configs.get(channel)
        return bool(cfg and cfg.commissioned)

    # -- anti-chatter ------------------------------------------------------

    def passes_deadband(self, channel: int, value: float, threshold: float, operator: str) -> bool:
        """
        True if `value` clears `threshold` by at least the channel's deadband.

        Requiring the reading to travel past the threshold by a margin is what
        stops a probe hovering on the setpoint from cycling a contactor.
        """
        cfg = self._configs.get(channel)
        if cfg is None or cfg.deadband <= 0:
            return True
        band = cfg.deadband
        if operator in (">", ">="):
            return value >= threshold + band
        if operator in ("<", "<="):
            return value <= threshold - band
        return True

    def _dwell_block_reason(self, channel: int, run: bool) -> str | None:
        """Why this transition must wait, or None if it may proceed."""
        cfg = self._configs.get(channel)
        if cfg is None:
            return None
        now = self._now()

        last = self._last_transition.get(channel)
        if last is not None and cfg.min_interval_s > 0 and (now - last) < cfg.min_interval_s:
            return f"min_interval_s {cfg.min_interval_s}s not elapsed"

        since = self._since.get(channel)
        if since is None:
            return None
        held = now - since
        currently_running = self.is_running(channel)
        if currently_running and not run and cfg.min_on_s > 0 and held < cfg.min_on_s:
            return f"min_on_s {cfg.min_on_s}s not elapsed"
        if not currently_running and run and cfg.min_off_s > 0 and held < cfg.min_off_s:
            return f"min_off_s {cfg.min_off_s}s not elapsed"
        return None

    # -- actuation ---------------------------------------------------------

    def request(self, channel: int, run: bool, cause: str, reason: str = "") -> bool:
        """
        Ask for a LOAD state. Returns True if the channel actually transitioned.

        Refused when the channel is unknown, not commissioned, already in the
        requested state, or still inside a dwell window.
        """
        cfg = self._configs.get(channel)
        if cfg is None:
            logger.warning("CH%d: no config, refusing to actuate", channel)
            return False

        if not cfg.commissioned:
            logger.warning("CH%d: not commissioned, refusing to actuate (cause=%s)", channel, cause)
            return False

        if self.is_running(channel) == run:
            return False

        blocked = self._dwell_block_reason(channel, run)
        if blocked is not None:
            logger.info(
                "CH%d: %s -> %s held off (%s)",
                channel,
                "run" if self.is_running(channel) else "stop",
                "run" if run else "stop",
                blocked,
            )
            return False

        self._apply(cfg, run, cause, reason)
        return True

    def revert_to_fail_safe(self, channel: int, cause: str, reason: str = "") -> bool:
        """
        Drive a channel to its fail-safe. Bypasses every dwell limit.

        Fail-safe is always de-energised, so this is the same physical action
        the hardware takes on power loss — which is exactly why it is safe to
        run unconditionally.
        """
        cfg = self._configs.get(channel)
        if cfg is None:
            # No config: we cannot know the contact, but de-energising is
            # always the safe direction, so do that much.
            try:
                self._relay.set(channel, False)
            except Exception as e:  # pragma: no cover - defensive
                logger.error("CH%d: fail-safe de-energise failed: %s", channel, e)
            return False

        target_run = cfg.fail_safe_state == FAIL_SAFE_RUN
        changed = self.is_running(channel) != target_run

        # Drive the pin unconditionally, even when our bookkeeping already says
        # we are there — the point of a fail-safe is not to trust bookkeeping.
        self._apply(cfg, target_run, cause, reason or "fail-safe", force=True)
        return changed

    def force_test_fire(self, channel: int, cause: str) -> bool:
        """
        Drive a channel AWAY from its fail-safe for a commissioning test-fire.

        This is the ONLY path that actuates an uncommissioned channel, and it
        exists so an installer can watch the load move before declaring the
        wiring good. Callers must have already collected an explicit operator
        confirmation — nothing here can verify that, so it is deliberately not
        reachable from the rules engine, a cloud command, or a downlink.

        Dwell limits are skipped: a test-fire is an operator standing at the
        panel, not the control loop cycling a contactor.
        """
        cfg = self._configs.get(channel)
        if cfg is None:
            logger.warning("CH%d: cannot test-fire without a validated config", channel)
            return False

        target_run = cfg.fail_safe_state != FAIL_SAFE_RUN
        logger.warning(
            "CH%d: TEST-FIRE driving load %s (coil %s)",
            channel,
            "RUN" if target_run else "STOP",
            "energised" if self._coil_for(cfg, target_run) else "de-energised",
        )
        self._apply(cfg, target_run, cause, "commissioning test-fire", force=True)
        return True

    def all_fail_safe(self, cause: str, reason: str = "") -> None:
        """Revert every known channel. Used on OTA, rollback, and shutdown."""
        for channel in sorted(self._configs):
            self.revert_to_fail_safe(channel, cause=cause, reason=reason)
        logger.warning("All channels reverted to fail-safe (cause=%s)", cause)

    def _apply(
        self, cfg: ChannelConfig, run: bool, cause: str, reason: str, force: bool = False
    ) -> None:
        changed = self._run.get(cfg.channel) != run
        self._drive(cfg, run)

        now = self._now()
        self._run[cfg.channel] = run
        self._last_cause[cfg.channel] = cause
        if changed or force:
            self._since[cfg.channel] = now
            self._last_transition[cfg.channel] = now
        if changed:
            self._cycles[cfg.channel] = self._cycles.get(cfg.channel, 0) + 1

        logger.info(
            "CH%d %s -> load %s (coil %s, cause=%s%s)",
            cfg.channel,
            cfg.role,
            "RUN" if run else "STOP",
            "energised" if self._coil_for(cfg, run) else "de-energised",
            cause,
            f", {reason}" if reason else "",
        )

        if changed or force:
            self._record(cfg.channel, run, cause, reason)

    # -- staleness ---------------------------------------------------------

    def note_reading(self, channel: int, valid: bool) -> bool:
        """
        Feed one control cycle's sensor validity for a channel.

        Returns True if this call tripped a staleness reversion. The counter
        resets only on a valid reading, so intermittent probes still trip.
        """
        cfg = self._configs.get(channel)
        if cfg is None:
            return False

        if valid:
            if self._misses.get(channel):
                logger.info("CH%d: sensor recovered, staleness counter reset", channel)
            self._misses[channel] = 0
            return False

        misses = self._misses.get(channel, 0) + 1
        self._misses[channel] = misses
        if misses < cfg.stale_cycles:
            logger.debug("CH%d: stale reading %d/%d", channel, misses, cfg.stale_cycles)
            return False

        if misses == cfg.stale_cycles:
            logger.error(
                "CH%d: no valid reading for %d consecutive cycles — reverting to fail-safe (%s)",
                channel,
                misses,
                cfg.fail_safe_state,
            )
        self.revert_to_fail_safe(
            channel,
            cause=CAUSE_STALENESS,
            reason=f"{misses} cycles without a valid reading",
        )
        return True

    def stale_count(self, channel: int) -> int:
        return self._misses.get(channel, 0)

    # -- OTA ---------------------------------------------------------------

    def prepare_for_ota(self, reason: str = "ota apply") -> None:
        """Revert everything before an OTA swap or a rollback."""
        self.all_fail_safe(cause=CAUSE_OTA, reason=reason)

    # -- telemetry / persistence -------------------------------------------

    def state_bitmask(self) -> int:
        """4-bit mask of LOAD state (bit 0 = channel 1, 1 = running)."""
        mask = 0
        for ch in range(1, 5):
            if self.is_running(ch):
                mask |= 1 << (ch - 1)
        return mask

    def snapshot(self) -> dict[str, Any]:
        """Compact summary for the uplink and the status page."""
        return {
            "state_bitmask": self.state_bitmask(),
            "cycles": {ch: self.cycles(ch) for ch in sorted(self._configs)},
            "last_cause": {ch: self._last_cause.get(ch) for ch in sorted(self._configs)},
            "commissioned": {ch: self.is_commissioned(ch) for ch in sorted(self._configs)},
            "stale": {ch: self.stale_count(ch) for ch in sorted(self._configs)},
        }

    def _load_cycle_counters(self) -> None:
        if self._db is None:
            return
        try:
            self._cycles = dict(self._db.get_relay_cycles())
        except Exception as e:
            logger.warning("Could not load relay cycle counters: %s", e)

    def _record(self, channel: int, run: bool, cause: str, reason: str) -> None:
        if self._db is None:
            return
        try:
            self._db.log_relay_transition(
                channel=channel,
                new_state=bool(run),
                cause=cause,
                reason=reason,
                cycles=self._cycles.get(channel, 0),
            )
        except Exception as e:
            # Never let an audit-write failure block or unwind an actuation —
            # the relay has already moved, and losing the log is strictly less
            # bad than raising through the control loop.
            logger.error("CH%d: failed to log transition: %s", channel, e)
