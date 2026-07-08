"""
Adaptive Sampling

Event-driven sensor sampling: when any watched sensor is changing fast
(rate-of-change in %-of-sensor-range per minute above threshold), the
sampling worker should tighten its loop to ``sensor_read_fast_s``; once
readings have been calm for ``adaptive_hold_s``, it decays back to the
normal ``sensor_read_s`` cadence.

Pure logic — no hardware, no threads, no sleeps. The clock is injected
(monotonic seconds) and settings come from a provider callable so remote
config changes apply live.
"""

import time
from collections.abc import Callable
from typing import Any

from utils.config import Settings

# Full-scale span per reading field, used to normalise rate-of-change to
# %-of-range per minute so one threshold works across dissimilar sensors.
SENSOR_RANGES: dict[str, float] = {
    "ph": 14.0,  # 0 to 14
    "tds_ppm": 5000.0,  # 0 to 5000 ppm
    "turbidity_ntu": 4000.0,  # 0 to 4000 NTU
    "temp_c": 60.0,  # -10 to 50 C
    "orp_mv": 4000.0,  # -2000 to +2000 mV
}

# Reading-dict field -> canonical sensor name (matches sensing.monitor and
# diagnostics.explain).
FIELD_TO_SENSOR: dict[str, str] = {
    "ph": "ph",
    "tds_ppm": "tds",
    "turbidity_ntu": "turbidity",
    "temp_c": "temperature",
    "orp_mv": "orp",
}


class AdaptiveSampler:
    """Decides the current sampling interval from recent rate-of-change."""

    def __init__(
        self,
        settings_provider: Callable[[], Settings],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings_provider
        self._clock = clock
        # field -> (monotonic seconds, value) of the last non-None sample
        self._last: dict[str, tuple[float, float]] = {}
        self._fast_since: float | None = None  # when fast mode was entered
        self._last_trigger_t: float | None = None  # most recent (re-)trigger
        self._trigger_sensor: str | None = None

    def observe(self, reading: dict[str, Any]) -> None:
        """Feed each stored reading; may enter or extend fast mode."""
        settings = self._settings()
        now = self._clock()
        for field, sensor_range in SENSOR_RANGES.items():
            if field not in reading:
                continue
            value = reading.get(field)
            if value is None:
                continue
            value = float(value)
            prev = self._last.get(field)
            self._last[field] = (now, value)
            if prev is None:
                continue
            prev_t, prev_v = prev
            dt_min = (now - prev_t) / 60.0
            if dt_min <= 0:
                continue
            rate = abs(value - prev_v) / sensor_range * 100.0 / dt_min
            if settings.adaptive_sampling_enabled and rate >= settings.adaptive_delta_threshold:
                if self._fast_since is None:
                    self._fast_since = now
                self._last_trigger_t = now
                self._trigger_sensor = FIELD_TO_SENSOR[field]
        self._maybe_decay(settings, now)

    def current_interval_s(self) -> int:
        """Sampling interval to use right now (seconds)."""
        settings = self._settings()
        self._maybe_decay(settings, self._clock())
        if not settings.adaptive_sampling_enabled or self._fast_since is None:
            return settings.sensor_read_s
        return settings.sensor_read_fast_s

    def status(self) -> dict[str, Any]:
        """{"mode": "fast"|"normal", "trigger_sensor": str|None, "since": float|None}."""
        settings = self._settings()
        self._maybe_decay(settings, self._clock())
        if not settings.adaptive_sampling_enabled or self._fast_since is None:
            return {"mode": "normal", "trigger_sensor": None, "since": None}
        return {
            "mode": "fast",
            "trigger_sensor": self._trigger_sensor,
            "since": self._fast_since,
        }

    def _maybe_decay(self, settings: Settings, now: float) -> None:
        """Drop back to normal after adaptive_hold_s with no re-trigger, or
        immediately if adaptive sampling was disabled (e.g. via remote config)."""
        if self._fast_since is None:
            return
        disabled = not settings.adaptive_sampling_enabled
        held_long_enough = (
            self._last_trigger_t is not None
            and now - self._last_trigger_t >= settings.adaptive_hold_s
        )
        if disabled or held_long_enough:
            self._fast_since = None
            self._last_trigger_t = None
            self._trigger_sensor = None
