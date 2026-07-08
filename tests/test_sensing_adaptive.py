"""Tests for src/sensing/adaptive.py — adaptive sampling logic (fake clock, no sleeps)."""

from sensing.adaptive import AdaptiveSampler
from utils.config import Settings


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_sampler(clock: FakeClock, **overrides) -> tuple[AdaptiveSampler, Settings]:
    defaults = {"adaptive_sampling_enabled": True}
    defaults.update(overrides)
    settings = Settings(**defaults)
    return AdaptiveSampler(lambda: settings, clock=clock), settings


def reading(**fields) -> dict:
    return {"timestamp": "2026-07-08T00:00:00Z", **fields}


class TestAdaptiveSampler:
    def test_starts_in_normal_mode(self):
        clock = FakeClock()
        sampler, settings = make_sampler(clock)
        assert sampler.current_interval_s() == settings.sensor_read_s
        assert sampler.status() == {"mode": "normal", "trigger_sensor": None, "since": None}

    def test_fast_ph_swing_triggers_fast_mode(self):
        clock = FakeClock()
        sampler, settings = make_sampler(clock)
        sampler.observe(reading(ph=7.0))
        clock.advance(60)
        # 1.0 pH in 1 min = 7.14 %-of-range/min >= 5.0 threshold
        sampler.observe(reading(ph=8.0))
        assert sampler.current_interval_s() == settings.sensor_read_fast_s
        status = sampler.status()
        assert status["mode"] == "fast"
        assert status["trigger_sensor"] == "ph"
        assert status["since"] == 60

    def test_slow_drift_stays_normal(self):
        clock = FakeClock()
        sampler, settings = make_sampler(clock)
        # 0.1 pH per minute = 0.71 %/min, well under the 5 %/min threshold
        for i in range(10):
            sampler.observe(reading(ph=7.0 + 0.1 * i))
            clock.advance(60)
        assert sampler.current_interval_s() == settings.sensor_read_s
        assert sampler.status()["mode"] == "normal"

    def test_disabled_flag_forces_normal(self):
        clock = FakeClock()
        sampler, settings = make_sampler(clock, adaptive_sampling_enabled=False)
        sampler.observe(reading(ph=7.0))
        clock.advance(60)
        sampler.observe(reading(ph=10.0))  # huge swing, but adaptive is off
        assert sampler.current_interval_s() == settings.sensor_read_s
        assert sampler.status()["mode"] == "normal"

    def test_decays_back_to_normal_after_hold(self):
        clock = FakeClock()
        sampler, settings = make_sampler(clock)
        sampler.observe(reading(ph=7.0))
        clock.advance(60)
        sampler.observe(reading(ph=8.0))  # trigger at t=60
        assert sampler.current_interval_s() == settings.sensor_read_fast_s
        # Stable readings while the hold elapses
        for _ in range(9):
            clock.advance(60)
            sampler.observe(reading(ph=8.0))
        assert clock.t == 600  # 540s since trigger, still inside hold
        assert sampler.current_interval_s() == settings.sensor_read_fast_s
        clock.advance(settings.adaptive_hold_s)  # now well past trigger + hold
        assert sampler.current_interval_s() == settings.sensor_read_s
        assert sampler.status()["mode"] == "normal"

    def test_retrigger_extends_fast_mode(self):
        clock = FakeClock()
        sampler, settings = make_sampler(clock)
        sampler.observe(reading(ph=7.0))
        clock.advance(60)
        sampler.observe(reading(ph=8.0))  # trigger at t=60
        clock.advance(240)
        # 4 pH over 4 min = 7.14 %-of-range/min >= 5.0 -> re-trigger at t=300
        sampler.observe(reading(ph=12.0))
        # At t=700 the first hold (60+600=660) has lapsed, but the
        # re-trigger holds until 300+600=900.
        clock.advance(400)
        assert sampler.current_interval_s() == settings.sensor_read_fast_s
        clock.advance(200)  # t=900
        assert sampler.current_interval_s() == settings.sensor_read_s

    def test_since_reports_fast_mode_entry_not_retrigger(self):
        clock = FakeClock()
        sampler, _ = make_sampler(clock)
        sampler.observe(reading(ph=7.0))
        clock.advance(60)
        sampler.observe(reading(ph=8.0))
        clock.advance(60)
        sampler.observe(reading(ph=9.0))  # re-trigger
        assert sampler.status()["since"] == 60

    def test_other_sensors_trigger_too(self):
        clock = FakeClock()
        sampler, settings = make_sampler(clock)
        sampler.observe(reading(tds_ppm=100.0))
        clock.advance(60)
        # 500 ppm/min over a 5000 range = 10 %/min
        sampler.observe(reading(tds_ppm=600.0))
        assert sampler.current_interval_s() == settings.sensor_read_fast_s
        assert sampler.status()["trigger_sensor"] == "tds"

    def test_none_values_are_ignored(self):
        clock = FakeClock()
        sampler, settings = make_sampler(clock)
        sampler.observe(reading(ph=7.0, tds_ppm=None))
        clock.advance(60)
        sampler.observe(reading(ph=None, tds_ppm=250.0))
        clock.advance(60)
        # The None gaps must not fabricate a rate-of-change
        sampler.observe(reading(ph=7.05, tds_ppm=255.0))
        assert sampler.current_interval_s() == settings.sensor_read_s

    def test_live_settings_change_applies_immediately(self):
        clock = FakeClock()
        settings = Settings(adaptive_sampling_enabled=True)
        sampler = AdaptiveSampler(lambda: settings, clock=clock)
        sampler.observe(reading(ph=7.0))
        clock.advance(60)
        sampler.observe(reading(ph=8.0))
        assert sampler.current_interval_s() == settings.sensor_read_fast_s
        # Remote config disables adaptive sampling — takes effect right away
        settings.adaptive_sampling_enabled = False
        assert sampler.current_interval_s() == settings.sensor_read_s
        assert sampler.status()["mode"] == "normal"

    def test_orp_watched_when_present(self):
        clock = FakeClock()
        sampler, settings = make_sampler(clock)
        sampler.observe(reading(orp_mv=200.0))
        clock.advance(60)
        # 300 mV/min over a 4000 mV range = 7.5 %/min
        sampler.observe(reading(orp_mv=500.0))
        assert sampler.current_interval_s() == settings.sensor_read_fast_s
        assert sampler.status()["trigger_sensor"] == "orp"
