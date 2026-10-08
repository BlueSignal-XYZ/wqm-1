"""A healthy virtual unit must look healthy on its own status page.

Found 2026-10-03 while scripting the University videos: a freshly commissioned
demo unit showed every base probe as "reading has been flat for 13 minutes"
two minutes after boot. Two causes, both pinned here:

1. The simulator's bounded walk moved less per sample than NOISE_FLOOR, so a
   healthy virtual probe read as stuck. It now adds per-sample noise of three
   times the floor; the flatline fault still freezes the reading exactly.
2. The card printed the number of readings as "minutes". It now states the
   time the readings actually span.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

from sensing.monitor import NOISE_FLOOR
from sensors.sim import build_simulated_sensors
from service_window.health import sensor_cards
from utils.config import Settings


def _rows(n: int, step_s: int, value: float = 7.0):
    t0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    return [
        {
            "timestamp": (t0 - timedelta(seconds=i * step_s)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ph": value,
            "tds": 300.0,
            "turbidity": 4.0,
            "temperature": 21.0,
        }
        for i in range(n)
    ]


class TestTheCardStatesRealMinutes:
    def test_thirty_readings_ten_seconds_apart_are_five_minutes_not_thirty(self):
        rows = _rows(30, 10)
        cards = sensor_cards(rows, now=datetime(2026, 10, 3, 12, 0, 5, tzinfo=UTC))
        assert "flat for 5 minutes" in cards["ph"]["message"]
        assert "flat for 30 minutes" not in cards["ph"]["message"]

    def test_a_sixty_second_cadence_reads_the_span_not_the_count(self):
        rows = _rows(30, 60)
        cards = sensor_cards(rows, now=datetime(2026, 10, 3, 12, 0, 5, tzinfo=UTC))
        assert "flat for 29 minutes" in cards["ph"]["message"]


class TestAHealthySimulatedProbeIsNotFlat:
    """The status page judges the last 30 readings (five minutes on a demo
    unit). Every 30-reading window, on every seed tried, must wander more than
    the noise floor."""

    def test_every_base_channel_wanders_more_than_its_noise_floor(self):
        for seed in range(20):
            sensors = build_simulated_sensors(Settings(simulate_seed=seed), faults=[])
            for ch in ("ph", "tds", "turbidity", "temperature"):
                walk = getattr(sensors, ch)._walk
                values = [walk.tick() for _ in range(300)]
                for i in range(0, len(values) - 30, 10):
                    window = values[i : i + 30]
                    assert statistics.pstdev(window) > NOISE_FLOOR[ch], (seed, ch, i)

    def test_the_flatline_fault_still_reads_exactly_flat(self):
        settings = Settings()
        sensors = build_simulated_sensors(settings, faults=[])
        walk = sensors.ph._walk
        walk.tick()
        walk.frozen = True
        values = {walk.tick() for _ in range(10)}
        assert len(values) == 1
