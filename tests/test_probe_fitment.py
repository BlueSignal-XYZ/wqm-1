"""
A probe that is not fitted must not be read.

This is the root fix for a class of bug that ate a whole bring-up. The
firmware assumed the four core analog probes were always physically present —
`health.py` said so in a comment, "core analog probes are always fitted" — so
it read them regardless of what was attached. An unfitted channel is an open
input; the conversion turns that into a number and everything downstream
believes it.

The consequences, all observed on one bench unit with NO probes attached:

  * pH published continuously for nine hours (0.25, 5.80, 9.06, 10.19, 11.32,
    and both clamp rails 0.00 / 14.00);
  * 38 threshold alerts raised from that noise;
  * the commissioning wizard reported "pH probe is reading normally", so the
    unit passed its own go/no-go check.

Two earlier attempts fixed symptoms rather than the cause: rejecting railed
inputs (works for TDS, whose open input floats to a rail; useless for pH,
whose front-end biases an open input to mid-scale) and rejecting volatile
windows (catches a wild wander, misses a settled one — a 1.17 pH spread was
observed passing a 2.0 threshold).

Declaring fitment is deterministic and needs no thresholds at all: an
undeclared probe is never constructed, and SamplingWorker skips a None sensor.
"""

import pytest

from service_window.health import sensor_cards
from utils.config import Settings


class TestDefaultsProtectExistingUnits:
    """An upgrade must never silence a unit that IS fitted."""

    def test_core_probes_default_to_fitted(self):
        s = Settings()
        assert s.ph_enabled is True
        assert s.tds_enabled is True
        assert s.turbidity_enabled is True
        assert s.temperature_enabled is True

    def test_a_config_written_before_these_keys_existed_still_reports(self):
        # Absent from config must mean fitted, or every deployed unit goes
        # quiet the moment it takes this firmware.
        cards = sensor_cards([], orp_enabled=False, config={})
        assert cards["ph"]["status"] != "disabled"


class TestAnUndeclaredProbeIsReportedAsNotInstalled:
    """The wizard must say 'not installed', not 'reading normally'."""

    @pytest.mark.parametrize("probe", ["ph", "tds", "turbidity", "temperature"])
    def test_disabling_a_probe_marks_it_disabled(self, probe):
        cards = sensor_cards([], orp_enabled=False, config={f"{probe}_enabled": False})
        assert cards[probe]["status"] == "disabled"

    @pytest.mark.parametrize("probe", ["ph", "tds", "turbidity", "temperature"])
    def test_an_unfitted_probe_never_reads_as_healthy(self, probe):
        """The specific lie: a green card for a channel with nothing attached."""
        cards = sensor_cards([], orp_enabled=False, config={f"{probe}_enabled": False})
        assert cards[probe]["status"] != "ok"

    def test_disabling_one_probe_does_not_disable_the_others(self):
        cards = sensor_cards([], orp_enabled=False, config={"ph_enabled": False})
        assert cards["ph"]["status"] == "disabled"
        assert cards["tds"]["status"] != "disabled"
        assert cards["turbidity"]["status"] != "disabled"


class TestTheAssumptionIsGone:
    def test_health_no_longer_claims_core_probes_are_always_fitted(self):
        import inspect

        import service_window.health as health

        src = inspect.getsource(health)
        assert "core analog probes are always fitted" not in src
        assert "_enabled" in src

    def test_fitment_is_declared_per_probe_in_the_settings_schema(self):
        from utils.config import SETTINGS_SCHEMA

        for key in ("ph_enabled", "tds_enabled", "turbidity_enabled", "temperature_enabled"):
            assert key in SETTINGS_SCHEMA, f"{key} cannot be set without a schema entry"
            # Sensors are constructed at start-up, so these need a restart —
            # exactly like orp_enabled.
            assert SETTINGS_SCHEMA[key].hot is False
