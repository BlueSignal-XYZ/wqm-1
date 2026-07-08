"""
Sensor Health Monitoring

Detects per-sensor conditions from the stream of stored readings and turns
them into events + a plain-language health map:

- **Flatline/stuck** — a sensor whose readings stay within its noise floor
  (or return only None) for the whole flatline window. Extends the dry-probe
  philosophy of commit ce9aef6 (turbidity rail -> None): a dead probe must
  surface as a fault, never as plausible-looking data.
- **Spike** — robust z-score (median/MAD) outlier. The spiking value is
  still stored/synced upstream; we only flag it.
- **Calibration drift** — a slow migration of the 24h rolling median away
  from the post-calibration baseline (DriftTracker).

Pure logic: injected clock, settings provider, no hardware imports. All
message copy comes from :mod:`diagnostics.explain` (single source of truth).
"""

import statistics
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from diagnostics.explain import explain
from utils.config import Settings

# Reading-dict field -> canonical sensor name (matches diagnostics.explain).
FIELD_TO_SENSOR: dict[str, str] = {
    "ph": "ph",
    "tds_ppm": "tds",
    "turbidity_ntu": "turbidity",
    "temp_c": "temperature",
    "orp_mv": "orp",
}

# Per-sensor noise floor: population stddev below this over a full flatline
# window means the reading is suspiciously flat (real water always wanders).
NOISE_FLOOR: dict[str, float] = {
    "ph": 0.02,
    "tds": 5.0,
    "turbidity": 1.0,
    "temperature": 0.05,
    "orp": 2.0,
}

# Spike events are rate-limited to one per sensor per this many seconds; a
# spike also keeps the sensor at "attention" in health() for the same span.
SPIKE_RATE_LIMIT_S = 30 * 60

# Minimum non-None samples in the window before spike detection engages.
SPIKE_MIN_SAMPLES = 10

# Consistency constant for a MAD-based robust z-score (normal distribution).
MAD_Z_SCALE = 0.6745

# Drift thresholds per sensor: ("abs", units) or ("rel", fraction of
# baseline). Temperature is deliberately excluded — it has no calibration.
DRIFT_THRESHOLDS: dict[str, tuple[str, float]] = {
    "ph": ("abs", 0.3),
    "tds": ("rel", 0.10),
    "turbidity": ("rel", 0.15),
    "orp": ("abs", 30.0),
}


class DriftTracker:
    """
    Calibration-drift proxy.

    Keeps one median snapshot per hour per sensor (deque, bounded to
    ``history_h`` hours), captures a baseline from the first
    ``baseline_capture_h`` hourly snapshots after ``reset_baseline()`` (the
    coordinator wires the calibration manager's ``set_calibrated`` to it;
    the first sample ever seen for a sensor implicitly starts a baseline
    too), then compares the rolling ``compare_window_h`` median against
    that baseline. Emits at most one drift result per sensor per
    ``event_interval_s`` (default 7 days).
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        baseline_capture_h: int = 24,
        compare_window_h: int = 24,
        history_h: int = 7 * 24,
        event_interval_s: float = 7 * 24 * 3600.0,
    ) -> None:
        self._clock = clock
        self._baseline_capture_h = baseline_capture_h
        self._compare_window_h = compare_window_h
        self._history_h = history_h
        self._event_interval_s = event_interval_s
        self._state: dict[str, dict[str, Any]] = {}

    def _new_state(self) -> dict[str, Any]:
        return {
            "bucket_hour": None,  # int hour index of the accumulating bucket
            "bucket": deque(maxlen=720),  # raw samples this hour (bounded)
            "snapshots": deque(maxlen=self._history_h),  # (end_time_s, median)
            "capture": [],  # first hourly medians after reset -> baseline
            "baseline": None,
            "last_event_t": None,
            "drifting": False,
        }

    def reset_baseline(self, sensor: str) -> None:
        """Restart baseline capture — call right after a calibration."""
        self._state[sensor] = self._new_state()

    def drifting_sensors(self) -> set[str]:
        """Sensors currently beyond their drift threshold."""
        return {s for s, st in self._state.items() if st["drifting"]}

    def observe(self, sensor: str, value: float) -> dict[str, Any] | None:
        """
        Feed one sample. Returns drift details {"baseline", "current",
        "delta", "threshold"} when the sensor newly reports drift (subject
        to the per-sensor event interval), else None.
        """
        now = self._clock()
        st = self._state.get(sensor)
        if st is None:
            st = self._new_state()
            self._state[sensor] = st

        hour = int(now // 3600)
        if st["bucket_hour"] is None:
            st["bucket_hour"] = hour
        elif hour != st["bucket_hour"]:
            self._finalize_bucket(st)
            st["bucket_hour"] = hour
        st["bucket"].append(float(value))

        return self._check(sensor, st, now)

    def _finalize_bucket(self, st: dict[str, Any]) -> None:
        if not st["bucket"]:
            return
        med = statistics.median(st["bucket"])
        end_t = (st["bucket_hour"] + 1) * 3600.0
        st["snapshots"].append((end_t, med))
        st["bucket"].clear()
        if st["baseline"] is None:
            st["capture"].append(med)
            if len(st["capture"]) >= self._baseline_capture_h:
                st["baseline"] = statistics.median(st["capture"])
                st["capture"] = []

    def _check(self, sensor: str, st: dict[str, Any], now: float) -> dict[str, Any] | None:
        threshold_spec = DRIFT_THRESHOLDS.get(sensor)
        baseline = st["baseline"]
        if threshold_spec is None or baseline is None:
            return None
        window = [m for t, m in st["snapshots"] if t > now - self._compare_window_h * 3600.0]
        if not window:
            return None
        current = statistics.median(window)
        kind, threshold = threshold_spec
        delta = abs(current - baseline)
        if kind == "rel":
            if baseline == 0:
                return None
            exceeded = delta > threshold * abs(baseline)
            threshold_value = threshold * abs(baseline)
        else:
            exceeded = delta > threshold
            threshold_value = threshold
        st["drifting"] = exceeded
        if not exceeded:
            return None
        last = st["last_event_t"]
        if last is not None and now - last < self._event_interval_s:
            return None
        st["last_event_t"] = now
        return {
            "baseline": baseline,
            "current": current,
            "delta": delta,
            "threshold": threshold_value,
        }


class SensorMonitor:
    """Per-sensor flatline/spike/drift detection and health reporting."""

    def __init__(
        self,
        settings_provider: Callable[[], Settings],
        clock: Callable[[], float] = time.monotonic,
        drift_tracker: DriftTracker | None = None,
    ) -> None:
        self._settings = settings_provider
        self._clock = clock
        # sensor -> deque[(t, value|None)] covering the flatline window
        self._windows: dict[str, deque[tuple[float, float | None]]] = {}
        # sensor -> None (healthy) | "flat" | "no_data"
        self._stuck: dict[str, str | None] = {}
        self._last_spike_t: dict[str, float] = {}  # last spike detected (health)
        self._last_spike_emit_t: dict[str, float] = {}  # last spike event emitted
        self._drift = drift_tracker or DriftTracker(clock=clock)

    def observe(self, reading: dict[str, Any]) -> list[dict[str, Any]]:
        """Feed one stored reading; returns newly raised events."""
        settings = self._settings()
        now = self._clock()
        window_s = settings.flatline_window_min * 60.0
        events: list[dict[str, Any]] = []
        for field, sensor in FIELD_TO_SENSOR.items():
            if field not in reading:
                continue
            raw = reading.get(field)
            value = None if raw is None else float(raw)
            window = self._windows.setdefault(sensor, deque(maxlen=4096))
            window.append((now, value))
            while window and window[0][0] < now - window_s:
                window.popleft()

            # Spike detection is skipped on any cycle where the sensor was
            # stuck coming in — the value that breaks a flatline IS the
            # recovery, and z-scoring it against the flat window would
            # always misread it as an anomaly.
            was_stuck = bool(self._stuck.get(sensor))
            events.extend(self._check_stuck(sensor, window, now, window_s, settings))
            if value is not None and not was_stuck:
                spike = self._check_spike(sensor, window, value, now, settings)
                if spike:
                    events.append(spike)
                if settings.drift_check_enabled:
                    drift = self._drift.observe(sensor, value)
                    if drift:
                        events.append(self._drift_event(sensor, drift))
        return events

    def health(self) -> dict[str, dict[str, Any]]:
        """
        Per-sensor {status, message, likelyCause, action} — shipped verbatim
        in heartbeats (``sensorHealth``) and shown in the Service Window.
        """
        settings = self._settings()
        now = self._clock()
        drifting = self._drift.drifting_sensors() if settings.drift_check_enabled else set()
        result: dict[str, dict[str, Any]] = {}
        for sensor in self._windows:
            stuck = self._stuck.get(sensor)
            last_spike = self._last_spike_t.get(sensor)
            if stuck:
                state = "stuck_no_data" if stuck == "no_data" else "stuck"
                info = explain(sensor, state, {"minutes": settings.flatline_window_min})
            elif sensor in drifting:
                info = explain(sensor, "calibration_drift")
            elif last_spike is not None and now - last_spike < SPIKE_RATE_LIMIT_S:
                info = explain(sensor, "spike")
            else:
                info = explain(sensor, "ok")
            result[sensor] = info
        return result

    def suspended_sensors(self) -> set[str]:
        """Sensors currently stuck — the sampling worker suspends relay
        rules tied to these (we only expose the set)."""
        return {sensor for sensor, kind in self._stuck.items() if kind}

    def reset_baseline(self, sensor: str) -> None:
        """Restart the drift baseline for a sensor (wired to calibration)."""
        self._drift.reset_baseline(sensor)

    # ------------------------------------------------------------------
    # Detection internals
    # ------------------------------------------------------------------

    def _check_stuck(
        self,
        sensor: str,
        window: deque[tuple[float, float | None]],
        now: float,
        window_s: float,
        settings: Settings,
    ) -> list[dict[str, Any]]:
        floor = NOISE_FLOOR[sensor]
        values = [v for _, v in window if v is not None]
        stuck = self._stuck.get(sensor)

        if stuck:
            recovered = False
            if stuck == "no_data":
                # Any data resuming counts as recovery.
                recovered = window[-1][1] is not None
            elif len(values) >= 2 and statistics.pstdev(values) >= floor:
                recovered = True
            if not recovered:
                return []
            self._stuck[sensor] = None
            info = explain(sensor, "recovered")
            return [
                {
                    "type": "sensor_recovered",
                    "sensor": sensor,
                    "message": info["message"],
                    "details": {"previous": stuck},
                }
            ]

        span = window[-1][0] - window[0][0]
        if span < window_s:
            return []
        if not values:
            kind, stddev = "no_data", None
        else:
            stddev = statistics.pstdev(values) if len(values) >= 2 else 0.0
            if stddev >= floor:
                return []
            kind = "flat"
        self._stuck[sensor] = kind
        minutes = settings.flatline_window_min
        info = explain(
            sensor,
            "stuck_no_data" if kind == "no_data" else "stuck",
            {"minutes": minutes},
        )
        return [
            {
                "type": "sensor_stuck",
                "sensor": sensor,
                "message": info["message"],
                "details": {
                    "kind": kind,
                    "window_min": minutes,
                    "stddev": stddev,
                    "noise_floor": floor,
                    "likelyCause": info["likelyCause"],
                    "action": info["action"],
                },
            }
        ]

    def _check_spike(
        self,
        sensor: str,
        window: deque[tuple[float, float | None]],
        value: float,
        now: float,
        settings: Settings,
    ) -> dict[str, Any] | None:
        if self._stuck.get(sensor):
            return None
        values = [v for _, v in window if v is not None]
        if len(values) < SPIKE_MIN_SAMPLES:
            return None
        median = statistics.median(values)
        mad = statistics.median([abs(v - median) for v in values])
        # A near-zero MAD would make any deviation look infinite; floor it
        # at half the sensor's noise floor so quantized-but-healthy streams
        # don't false-positive.
        mad_effective = max(mad, NOISE_FLOOR[sensor] * 0.5)
        z = MAD_Z_SCALE * (value - median) / mad_effective
        if abs(z) < settings.spike_z_threshold:
            return None
        self._last_spike_t[sensor] = now
        last_emit = self._last_spike_emit_t.get(sensor)
        if last_emit is not None and now - last_emit < SPIKE_RATE_LIMIT_S:
            return None  # rate-limited: at most one event per sensor per 30 min
        self._last_spike_emit_t[sensor] = now
        info = explain(sensor, "spike", {"value": value})
        return {
            "type": "anomaly_spike",
            "sensor": sensor,
            "message": info["message"],
            "details": {
                "value": value,
                "z": round(z, 2),
                "median": median,
                "mad": mad,
                "likelyCause": info["likelyCause"],
                "action": info["action"],
            },
        }

    def _drift_event(self, sensor: str, drift: dict[str, Any]) -> dict[str, Any]:
        info = explain(sensor, "calibration_drift", drift)
        return {
            "type": "calibration_drift",
            "sensor": sensor,
            "message": info["message"],
            "details": {
                **drift,
                "likelyCause": info["likelyCause"],
                "action": info["action"],
            },
        }
