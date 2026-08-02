"""
An unconnected analog probe must report NOTHING, never a plausible number.

Regression for a first-field-unit incident (2026-08-02): a WQM-1 with no
probes attached reported pH 0.25, 10.86, 11.29, 0.00 and 14.00 to the cloud
over a few minutes — the last two being the clamp rails exactly. The cloud
marked every one `quality: "good"` (they sit inside its 0-14 sanity band) and
raised four CRITICAL threshold alerts from what was electrical noise on a
floating pin.

The bug was the clamp itself: `max(0.0, min(14.0, ph))` converts an impossible
value into a confident, in-range lie. Turbidity already did this correctly and
its comment describes the same failure ("a fabricated 3000 NTU, which can then
drive relay automation indefinitely") — the guard was simply never applied to
the other two analog channels.
"""

from unittest.mock import MagicMock

import pytest

from sensors.ph import PHSensor
from sensors.tds import TDSSensor
from utils.config import ADC_FULL_SCALE_V, ADC_RAIL_MARGIN_V


def _adc(voltage):
    adc = MagicMock()
    adc.read_voltage.return_value = voltage
    return adc


class TestPHOpenInput:
    def test_floating_low_rail_reports_nothing(self):
        assert PHSensor(_adc(0.0)).read() is None

    def test_floating_high_rail_reports_nothing(self):
        assert PHSensor(_adc(ADC_FULL_SCALE_V)).read() is None

    @pytest.mark.parametrize("v", [0.0, 0.01, ADC_RAIL_MARGIN_V])
    def test_anything_pinned_at_a_rail_reports_nothing(self, v):
        assert PHSensor(_adc(v)).read() is None

    def test_no_reading_is_ever_the_clamp_rail(self):
        """The two values the field unit actually published, 0.00 and 14.00,
        must now be unreachable: reaching a rail means the input was outside
        anything an electrode can produce."""
        for v in [x / 100 for x in range(0, 410, 7)]:
            result = PHSensor(_adc(v)).read()
            assert result not in (0.0, 14.0), f"{v} V still yields a clamp rail"

    def test_a_valid_mid_scale_reading_still_works(self):
        """The guard must not silence a working probe. The pH 7 bias point is
        the sensor's own calibrated v_ph7, so it must read ~7."""
        sensor = PHSensor(_adc(0.0))
        v7 = sensor._v_ph7
        sensor._adc = _adc(v7)
        assert sensor.read() == pytest.approx(7.0, abs=0.05)

    def test_adc_failure_still_reports_nothing(self):
        adc = MagicMock()
        adc.read_voltage.side_effect = OSError("i2c fail")
        assert PHSensor(adc).read() is None


class TestTDSOpenInput:
    def test_floating_low_rail_reports_nothing(self):
        """The field unit published a steady 1.2 ppm from a disconnected
        probe — small, plausible, and entirely fictional."""
        assert TDSSensor(_adc(0.0)).read() is None

    def test_floating_high_rail_reports_nothing(self):
        assert TDSSensor(_adc(ADC_FULL_SCALE_V)).read() is None

    def test_a_valid_reading_still_works(self):
        value = TDSSensor(_adc(0.5)).read()
        assert value is not None and value > 0

    def test_negative_ppm_reports_nothing_rather_than_zero(self):
        sensor = TDSSensor(_adc(0.5))
        sensor.set_calibration(-100.0)  # bad constant -> negative ppm
        assert sensor.read() is None


class TestNoChannelStillClamps:
    """Structural guard: the clamp pattern must not come back to any analog
    channel. Reporting a boundary value is how this shipped in the first
    place."""

    @pytest.mark.parametrize("module", ["ph", "tds", "turbidity"])
    def test_no_clamping_to_a_boundary(self, module):
        import inspect
        import importlib

        src = inspect.getsource(importlib.import_module(f"sensors.{module}"))
        assert "max(0.0, min(" not in src, f"{module} clamps into range again"
