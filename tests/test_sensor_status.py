"""
Clean water is a result. A faulted probe says why.

The defect this fixes, in one line of arithmetic: `tds.py` refused any ADC
sample at or below ADC_RAIL_MARGIN_V (0.05 V) as an open input, but the TDS
chain has a 0.3125 divider in front of it, so 0.05 V at the converter is 0.16 V
at the probe — **80 ppm at the default 500 ppm/V calibration**. Every sample
below 80 ppm was discarded and logged as "probe disconnected or dry". A
customer with clean water lost the channel entirely, and because the cloud
mirror was rebuilt from each payload, the tile did not go blank — it ceased to
exist.

The rail margin was written for pH, whose AFE is biased near mid-supply and
genuinely cannot approach a rail in real water. TDS's valid range STARTS at
zero. One constant, two channels, opposite physics.

Two behaviours are pinned here, and they are the whole point:

  · a low reading is a READING — clean water reports a number;
  · a refusal is still a REPORT — the channel travels carrying its reason, so
    the cloud can say "check the probe" instead of showing nothing.
"""

import json
from unittest.mock import MagicMock

import pytest

from sensors.status import NO_CONDUCTION, OK, OUT_OF_RANGE, UNCALIBRATED
from utils.config import (
    ADC_OPEN_INPUT_V,
    ADC_RAIL_MARGIN_V,
    TDS_DIVIDER_RATIO,
    TURB_CLEAR_TOLERANCE_V,
    TURB_V_CLEAR,
)


@pytest.fixture
def adc():
    return MagicMock()


class TestTDSCleanWater:
    """The regression that cost a customer their TDS channel."""

    @pytest.mark.parametrize(
        "voltage,expected_ppm",
        [
            (0.010, 16.0),
            (0.030, 48.0),
            (0.050, 80.0),  # exactly the old cutoff — was silently dropped
            (0.100, 160.0),
        ],
    )
    def test_low_tds_reports_a_number(self, mock_hardware, adc, voltage, expected_ppm):
        from sensors.tds import TDSSensor

        adc.read_voltage = MagicMock(return_value=voltage)
        result = TDSSensor(adc).read_detailed(temp_c=25.0)
        assert result.status == OK
        assert result.value == pytest.approx(expected_ppm, abs=0.5)

    def test_the_old_threshold_was_inside_the_measuring_range(self):
        """Documents the arithmetic, so nobody reinstates the 50 mV floor.

        If this ever fails, the divider or the default calibration moved and the
        story in this file needs rewriting — not the guard loosening again.
        """
        default_k = 500.0
        discarded_below_ppm = (ADC_RAIL_MARGIN_V / TDS_DIVIDER_RATIO) * default_k
        assert discarded_below_ppm == pytest.approx(80.0, abs=0.1)
        # And the replacement is far below anything a conducting sample makes.
        assert (ADC_OPEN_INPUT_V / TDS_DIVIDER_RATIO) * default_k < 5.0

    @pytest.mark.parametrize("voltage", [0.0, 0.001, ADC_OPEN_INPUT_V])
    def test_an_open_or_dry_probe_is_still_refused(self, mock_hardware, adc, voltage):
        """Loosening the floor must not resurrect the fabricated-ppm bug."""
        from sensors.tds import TDSSensor

        adc.read_voltage = MagicMock(return_value=voltage)
        result = TDSSensor(adc).read_detailed()
        assert result.value is None
        assert result.status == NO_CONDUCTION

    def test_the_refusal_carries_a_reason_an_installer_can_act_on(self, mock_hardware, adc):
        from sensors.tds import TDSSensor

        adc.read_voltage = MagicMock(return_value=0.0)
        result = TDSSensor(adc).read_detailed()
        assert result.needs_attention
        assert "V" in (result.detail or "")

    def test_railed_high_is_measured_against_this_channels_chain(self, mock_hardware, adc):
        """2.3 V is the TDS chain's top; 4.096 V is the CONVERTER's range.

        Comparing against the converter's range — as the code used to — meant a
        railed TDS signal could never be detected at all, because the chain
        cannot reach 4 V in the first place.
        """
        from sensors.tds import TDSSensor

        adc.read_voltage = MagicMock(return_value=2.29)
        result = TDSSensor(adc).read_detailed()
        assert result.value is None
        assert result.status == OUT_OF_RANGE


class TestTurbidityClearWater:
    """Water clearer than the reference is the best case, not an error."""

    @pytest.mark.parametrize("above", [0.0, 0.05, TURB_CLEAR_TOLERANCE_V])
    def test_clearer_than_the_reference_reads_zero_ntu(self, mock_hardware, adc, above):
        from sensors.turbidity import TurbiditySensor

        adc.read_voltage = MagicMock(return_value=TURB_V_CLEAR + above)
        result = TurbiditySensor(adc).read_detailed()
        assert result.status == OK
        assert result.value == 0.0

    def test_far_above_the_reference_is_a_stale_calibration_not_pristine_water(
        self, mock_hardware, adc
    ):
        from sensors.turbidity import TurbiditySensor

        adc.read_voltage = MagicMock(return_value=TURB_V_CLEAR + TURB_CLEAR_TOLERANCE_V + 0.05)
        result = TurbiditySensor(adc).read_detailed()
        assert result.value is None
        assert result.status == UNCALIBRATED

    def test_disconnected_is_distinguished_from_maximum_turbidity(self, mock_hardware, adc):
        """Both sit below the full-scale point, and they are not the same news.

        0.4 V is genuinely dirtier than the sensor can measure; 0.0005 V is an
        input with nothing on it. Reporting both as "no reading" told the owner
        the same thing about a filthy tank and an unplugged probe.
        """
        from sensors.turbidity import TurbiditySensor

        adc.read_voltage = MagicMock(return_value=0.4)
        assert TurbiditySensor(adc).read_detailed().status == OUT_OF_RANGE

        adc.read_voltage = MagicMock(return_value=0.0005)
        assert TurbiditySensor(adc).read_detailed().status == NO_CONDUCTION


class TestBackwardCompatibleRead:
    """`read()` keeps its old contract for every existing caller."""

    def test_read_returns_the_value_or_none(self, mock_hardware, adc):
        from sensors.tds import TDSSensor

        adc.read_voltage = MagicMock(return_value=0.3125)
        assert TDSSensor(adc).read(temp_c=25.0) == pytest.approx(500.0, abs=5.0)

        adc.read_voltage = MagicMock(return_value=0.0)
        assert TDSSensor(adc).read() is None


class TestStatusReachesTheCloudPayload:
    """A fault has to leave the device, or none of the above matters."""

    def _client(self):
        from cloud.client import CloudClient

        return CloudClient(
            device_id="BS-WQM1-TEST",
            ingest_url="https://example.invalid/ingest",
            command_url="https://example.invalid/cmd",
            api_key="k",
            fw_version="2.1.0",
        )

    def test_a_faulted_channel_is_sent_rather_than_omitted(self):
        """The line this replaces was `if val is not None` — and nothing else.

        A NULL column meant the key never appeared in the payload, so the cloud
        could not tell a probe out of the water from a probe that was never
        fitted. Omission is not a message.
        """
        row = {
            "timestamp": "2026-08-21T18:00:00Z",
            "ph": 5.89,
            "tds_ppm": None,
            "sensor_status": json.dumps({"tds": NO_CONDUCTION}),
        }
        payload = self._client().reading_to_json(row)
        assert payload["sensors"]["tds"] == {"value": None, "status": NO_CONDUCTION}
        assert payload["sensors"]["ph"]["value"] == 5.89

    def test_a_healthy_channel_is_unchanged(self):
        row = {"timestamp": "2026-08-21T18:00:00Z", "tds_ppm": 238.0, "sensor_status": None}
        sensors = self._client().reading_to_json(row)["sensors"]
        assert sensors["tds"] == {"value": 238.0}

    def test_a_value_wins_over_a_stale_status(self):
        row = {
            "timestamp": "2026-08-21T18:00:00Z",
            "tds_ppm": 238.0,
            "sensor_status": json.dumps({"tds": NO_CONDUCTION}),
        }
        sensors = self._client().reading_to_json(row)["sensors"]
        assert sensors["tds"] == {"value": 238.0}

    @pytest.mark.parametrize("raw", ["not json", "[]", "", None, 42, json.dumps({"tds": 7})])
    def test_a_bad_status_column_never_costs_us_the_reading(self, raw):
        """The values in the row are still worth syncing.

        A status is an annotation on a reading, not a precondition for it —
        letting a malformed column drop the whole row would trade a cosmetic
        problem for data loss.
        """
        row = {"timestamp": "2026-08-21T18:00:00Z", "ph": 7.1, "sensor_status": raw}
        sensors = self._client().reading_to_json(row)["sensors"]
        assert sensors["ph"] == {"value": 7.1}
        assert "tds" not in sensors
