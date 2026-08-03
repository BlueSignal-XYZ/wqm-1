"""
The pH channel needs a guard the rail check cannot give it.

Field evidence, 2026-08-02, a unit with NO electrode attached, after the rail
and out-of-range guards had already shipped:

    02:23  pH 11.32   (quality: good)
    02:25  pH  5.80   (quality: good)
    02:26  pH  9.06   (quality: good)
    02:28  pH 10.19   (quality: good)

Every one passed both existing guards and reached the cloud, and five threshold
alerts fired off them. The reason is physical: the TDS chain floats to a supply
RAIL when its probe is open, which the rail check catches — but the LMP91200
biases an open pH electrode input to MID-SCALE, producing voltages that are
electrically valid and convert to entirely in-range pH.

What gives it away is the spread. Chemistry does not move 5.5 pH in five
minutes; a floating input wanders across the whole scale.
"""

from unittest.mock import MagicMock

import pytest

from sensors.ph import PHSensor
from utils.config import PH_MAX_WINDOW_SPAN


def _feed(sensor, ph_values):
    """Drive the sensor with the voltages that produce these pH values."""
    out = []
    for ph in ph_values:
        v = sensor._v_ph7 + (ph - 7.0) / sensor._slope
        sensor._adc.read_voltage = MagicMock(return_value=v)
        out.append(sensor.read())
    return out


@pytest.fixture
def sensor():
    return PHSensor(MagicMock())


class TestTheFieldFailure:
    """The exact sequence that reached production, replayed."""

    FIELD = [11.32, 5.80, 9.06, 10.19, 7.50]

    def test_the_real_disconnected_probe_data_is_rejected(self, sensor):
        assert _feed(sensor, self.FIELD)[-1] is None

    def test_at_most_two_samples_escape_before_the_gate_engages(self, sensor):
        """The known, accepted limit of this guard.

        The gate needs three samples to distinguish a wander from a step, so
        the first two readings of a disconnected probe still publish. That is
        two minutes of bad data at the default interval instead of an
        indefinite stream, and it is the price of not blinding the device for
        five minutes at every startup. Anything more than two is a regression."""
        out = _feed(sensor, self.FIELD)
        escaped = [v for v in out if v is not None]
        assert len(escaped) <= 2, f"{len(escaped)} readings escaped: {escaped}"
        # Everything from the third sample on is suppressed, permanently, for
        # as long as the input keeps wandering.
        assert all(v is None for v in out[2:]), out

    def test_it_keeps_rejecting_while_the_input_keeps_wandering(self, sensor):
        _feed(sensor, self.FIELD)
        assert _feed(sensor, [3.2, 12.8, 6.1])[-1] is None


class TestARealProbeStillWorks:
    """A guard that suppresses good data is worse than the bug."""

    def test_a_stable_electrode_reads_normally(self, sensor):
        out = _feed(sensor, [7.01, 7.00, 7.02, 6.99, 7.01])
        assert out[-1] == pytest.approx(7.01, abs=0.05)

    def test_a_genuine_gradual_drift_is_not_suppressed(self, sensor):
        # 0.2 pH over five samples — a real, slow excursion worth alerting on.
        out = _feed(sensor, [7.00, 7.05, 7.10, 7.15, 7.20])
        assert out[-1] is not None

    def test_a_swing_just_inside_the_limit_is_allowed(self, sensor):
        half = PH_MAX_WINDOW_SPAN / 2 - 0.05
        out = _feed(sensor, [7 - half, 7, 7, 7, 7 + half])
        assert out[-1] is not None

    def test_a_swing_just_outside_the_limit_is_rejected(self, sensor):
        half = PH_MAX_WINDOW_SPAN / 2 + 0.05
        assert _feed(sensor, [7 - half, 7, 7, 7, 7 + half])[-1] is None

    def test_it_recovers_once_the_signal_settles(self, sensor):
        """A probe reconnected mid-deployment must start reading again."""
        _feed(sensor, [11.32, 5.80, 9.06, 10.19, 7.50])
        out = _feed(sensor, [7.00, 7.01, 7.00, 6.99, 7.01])
        assert out[-1] == pytest.approx(7.0, abs=0.05)


class TestTheGateNeedsEnoughHistory:
    """Three samples is the smallest window that can tell a wander from a step.

    Deliberately NOT gated on a full window: that would blind the device for
    five minutes at startup and break the read-once contract that the
    calibration and temperature-compensation tests rely on."""

    def test_a_single_read_still_returns_a_value(self, sensor):
        assert _feed(sensor, [7.0])[0] == pytest.approx(7.0, abs=0.05)

    def test_two_reads_still_return_values(self, sensor):
        assert all(v is not None for v in _feed(sensor, [7.0, 7.0]))

    def test_the_gate_engages_from_the_third_sample(self, sensor):
        out = _feed(sensor, [11.32, 5.80, 9.06])
        assert out[0] is not None and out[1] is not None
        assert out[2] is None, "the wander should be caught by sample three"


class TestThresholdIsDocumented:
    def test_the_limit_is_a_named_constant_carrying_its_field_evidence(self):
        import inspect

        import utils.config as cfg

        src = inspect.getsource(cfg)
        assert "PH_MAX_WINDOW_SPAN" in src
        # The number must arrive with the reason it was chosen and a warning
        # that it is not yet bench-confirmed.
        assert "BENCH-VALIDATE" in src
        assert "11.32" in src or "5.5 pH" in src
