"""Tests for src/sensing/monitor.py — flatline, spike, and drift detection."""

from sensing.monitor import DriftTracker, SensorMonitor
from utils.config import Settings


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_monitor(clock: FakeClock, **overrides) -> tuple[SensorMonitor, Settings]:
    defaults = {"drift_check_enabled": False}  # most tests exercise other paths
    defaults.update(overrides)
    settings = Settings(**defaults)
    return SensorMonitor(lambda: settings, clock=clock), settings


def reading(**fields) -> dict:
    return {"timestamp": "2026-07-08T00:00:00Z", **fields}


class TestFlatline:
    def test_stuck_detected_at_exactly_window_span(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        events = []
        for _ in range(20):  # t = 0 .. 1140, span 19 min < 20
            events.extend(monitor.observe(reading(ph=7.0)))
            clock.advance(60)
        assert events == []
        events = monitor.observe(reading(ph=7.0))  # t=1200, span exactly 20 min
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "sensor_stuck"
        assert ev["sensor"] == "ph"
        assert "flat for 20 minutes" in ev["message"]
        assert ev["details"]["kind"] == "flat"

    def test_stuck_not_re_emitted_until_recovered(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        for _ in range(21):
            monitor.observe(reading(ph=7.0))
            clock.advance(60)
        # Still flat — no second event
        for _ in range(5):
            assert monitor.observe(reading(ph=7.0)) == []
            clock.advance(60)
        assert monitor.suspended_sensors() == {"ph"}

    def test_recovery_emits_event_and_clears_suspension(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        for _ in range(21):
            monitor.observe(reading(ph=7.0))
            clock.advance(60)
        assert monitor.suspended_sensors() == {"ph"}
        events = monitor.observe(reading(ph=7.4))  # variation above noise floor
        assert [e["type"] for e in events] == ["sensor_recovered"]
        assert events[0]["sensor"] == "ph"
        assert monitor.suspended_sensors() == set()

    def test_all_none_window_is_stuck_no_data(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        events = []
        for _ in range(21):
            events.extend(monitor.observe(reading(turbidity_ntu=None)))
            clock.advance(60)
        stuck = [e for e in events if e["type"] == "sensor_stuck"]
        assert len(stuck) == 1
        assert stuck[0]["sensor"] == "turbidity"
        assert stuck[0]["details"]["kind"] == "no_data"
        assert "no data" in stuck[0]["message"]
        # Data resumes -> recovered
        events = monitor.observe(reading(turbidity_ntu=12.5))
        assert [e["type"] for e in events] == ["sensor_recovered"]

    def test_normal_variation_never_flags(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        events = []
        for i in range(40):
            events.extend(monitor.observe(reading(ph=7.0 + 0.1 * (i % 2))))
            clock.advance(60)
        assert events == []
        assert monitor.suspended_sensors() == set()

    def test_absent_field_is_not_monitored(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        for i in range(25):
            events = monitor.observe(reading(ph=7.0 + 0.1 * (i % 2)))
            assert events == []
            clock.advance(60)
        assert "orp" not in monitor.health()


class TestSpike:
    def _feed_baseline(self, monitor, clock, n=15):
        for i in range(n):
            monitor.observe(reading(ph=7.0 + 0.1 * (i % 2)))
            clock.advance(60)

    def test_spike_flagged_once_then_rate_limited(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        self._feed_baseline(monitor, clock)
        events = monitor.observe(reading(ph=12.0))  # t=900
        assert [e["type"] for e in events] == ["anomaly_spike"]
        assert events[0]["sensor"] == "ph"
        assert abs(events[0]["details"]["z"]) >= 6.0
        clock.advance(60)
        # keep a healthy baseline flowing, spike again 10 min later
        for i in range(9):
            monitor.observe(reading(ph=7.0 + 0.1 * (i % 2)))
            clock.advance(60)
        events = monitor.observe(reading(ph=12.0))  # t=1500 — within 30 min
        assert events == []
        # ... and again after the 30-min rate limit has passed
        clock.advance(60)
        i = 0
        while clock.t < 2760:  # next spike at t=2760 > 900 + 1800
            monitor.observe(reading(ph=7.0 + 0.1 * (i % 2)))
            clock.advance(60)
            i += 1
        events = monitor.observe(reading(ph=12.0))
        assert [e["type"] for e in events] == ["anomaly_spike"]

    def test_no_spike_with_too_few_samples(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        for i in range(5):
            monitor.observe(reading(ph=7.0 + 0.1 * (i % 2)))
            clock.advance(60)
        assert monitor.observe(reading(ph=12.0)) == []

    def test_normal_wiggle_is_not_a_spike(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        self._feed_baseline(monitor, clock)
        assert monitor.observe(reading(ph=7.2)) == []


class TestHealth:
    def test_health_map_shape(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        monitor.observe(reading(ph=7.0, tds_ppm=350.0, temp_c=21.5))
        health = monitor.health()
        assert set(health) == {"ph", "tds", "temperature"}
        for entry in health.values():
            assert set(entry) == {"status", "message", "likelyCause", "action"}
            assert entry["status"] == "ok"
            assert entry["message"]

    def test_stuck_sensor_is_fault(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        for _ in range(21):
            monitor.observe(reading(ph=7.0))
            clock.advance(60)
        health = monitor.health()
        assert health["ph"]["status"] == "fault"
        assert health["ph"]["action"]  # installers always get a next step

    def test_recent_spike_is_attention_then_clears(self):
        clock = FakeClock()
        monitor, _ = make_monitor(clock)
        for i in range(15):
            monitor.observe(reading(ph=7.0 + 0.1 * (i % 2)))
            clock.advance(60)
        monitor.observe(reading(ph=12.0))
        assert monitor.health()["ph"]["status"] == "attention"
        clock.advance(31 * 60)  # spike ages out of the attention window
        assert monitor.health()["ph"]["status"] == "ok"


class TestDriftTracker:
    def _feed_hours(self, tracker, clock, sensor, value, hours):
        results = []
        for _ in range(hours):
            result = tracker.observe(sensor, value)
            if result:
                results.append(result)
            clock.advance(3600)
        return results

    def test_drift_fires_after_migration_not_before(self):
        clock = FakeClock(10.0)
        tracker = DriftTracker(clock=clock)
        # 25 hourly samples at 7.0 -> 24 snapshots -> baseline frozen at 7.0
        assert self._feed_hours(tracker, clock, "ph", 7.0, 25) == []
        # Migrate to 7.2 (delta 0.2 < 0.3 threshold): never fires
        assert self._feed_hours(tracker, clock, "ph", 7.2, 30) == []
        assert tracker.drifting_sensors() == set()
        # Migrate to 7.5 (delta 0.5 > 0.3): fires exactly once (7-day limit)
        results = self._feed_hours(tracker, clock, "ph", 7.5, 40)
        assert len(results) == 1
        assert results[0]["baseline"] == 7.0
        assert results[0]["delta"] > 0.3
        assert tracker.drifting_sensors() == {"ph"}

    def test_relative_threshold_for_tds(self):
        clock = FakeClock(10.0)
        tracker = DriftTracker(clock=clock)
        self._feed_hours(tracker, clock, "tds", 1000.0, 25)
        # 5% shift — under the 10% relative threshold
        assert self._feed_hours(tracker, clock, "tds", 1050.0, 30) == []
        # 15% shift — over threshold
        results = self._feed_hours(tracker, clock, "tds", 1150.0, 40)
        assert len(results) == 1

    def test_temperature_excluded(self):
        clock = FakeClock(10.0)
        tracker = DriftTracker(clock=clock)
        self._feed_hours(tracker, clock, "temperature", 20.0, 25)
        assert self._feed_hours(tracker, clock, "temperature", 45.0, 40) == []

    def test_reset_baseline_clears_drift(self):
        clock = FakeClock(10.0)
        tracker = DriftTracker(clock=clock)
        self._feed_hours(tracker, clock, "ph", 7.0, 25)
        self._feed_hours(tracker, clock, "ph", 7.6, 40)
        assert tracker.drifting_sensors() == {"ph"}
        tracker.reset_baseline("ph")  # recalibrated — start over
        assert tracker.drifting_sensors() == set()


class TestDriftViaMonitor:
    def test_drift_event_folded_into_observe_and_health(self):
        clock = FakeClock(10.0)
        monitor, _ = make_monitor(clock, drift_check_enabled=True)
        events = []
        for _ in range(25):  # hourly readings: baseline period at pH 7.0
            events.extend(monitor.observe(reading(ph=7.0)))
            clock.advance(3600)
        for _ in range(40):  # migrated period at pH 7.6
            events.extend(monitor.observe(reading(ph=7.6)))
            clock.advance(3600)
        drift_events = [e for e in events if e["type"] == "calibration_drift"]
        assert len(drift_events) == 1
        assert drift_events[0]["sensor"] == "ph"
        assert "drifted" in drift_events[0]["message"]
        health = monitor.health()
        assert health["ph"]["status"] == "attention"
        assert "Recalibrate" in health["ph"]["action"]

    def test_drift_disabled_emits_nothing(self):
        clock = FakeClock(10.0)
        monitor, _ = make_monitor(clock, drift_check_enabled=False)
        events = []
        for _ in range(25):
            events.extend(monitor.observe(reading(ph=7.0)))
            clock.advance(3600)
        for _ in range(40):
            events.extend(monitor.observe(reading(ph=7.6)))
            clock.advance(3600)
        assert [e for e in events if e["type"] == "calibration_drift"] == []
        assert monitor.health()["ph"]["status"] == "ok"
