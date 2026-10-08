"""
Calibration you can actually perform, on arithmetic that cannot drift.

Two defects are pinned here, and they are different in kind.

**The wizard asked for a number the product never displayed.** Every page said
"read the voltage" and nothing — not the status page, not the sensors page, not
the wizard itself — showed a voltage. So the only way to calibrate a unit was to
SSH in and poke the ADC by hand, which is not a thing a field installer does.
The channels therefore stayed on their factory-nominal constants, and a nominal
pH reading is indistinguishable on the dashboard from a calibrated one.

**The wizard and the sensor disagreed by exactly the divider ratio.** The wizard
solved `k = ppm / v` while the sensor computed `ppm = (v_adc / 0.3125) * k`, so
a *correct* calibration entry produced readings 3.2x high, forever, with no
error anywhere — both halves were individually self-consistent. The fix is not a
corrected formula in two places; it is ONE function, inverted, which is what
`test_the_wizard_inverts_exactly_what_the_sensor_applies` exists to hold.

The third test class covers the reason the live reading is a *command* rather
than a local ADC read: two processes on one conversion register cannot be made
safe from either side.
"""

import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from sensors.tds import compensated_voltage, k_from_reference, ppm_from_adc
from service_window.app import create_app
from utils.config import TDS_DIVIDER_RATIO

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(path: str, temp_c: float | None) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            ph REAL, tds_ppm REAL, turbidity_ntu REAL, orp_mv REAL,
            temp_c REAL, lat REAL, lon REAL, alt_m REAL,
            battery_v REAL, relay_state INTEGER DEFAULT 0, synced INTEGER DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT INTO readings (timestamp, ph, tds_ppm, temp_c) VALUES (?, ?, ?, ?)",
        ("2026-08-21T18:00:00Z", 5.93, 15.4, temp_c),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def make_client(tmp_path):
    """Build an authenticated Service Window client with a chosen water temp."""

    def _build(temp_c: float | None = 25.0):
        db_path = str(tmp_path / f"t{temp_c}.db")
        _make_db(db_path, temp_c)
        app = create_app(
            {
                "db_path": db_path,
                "pin": "9999",
                "config_path": str(tmp_path / "config.yaml"),
                "cal_path": str(tmp_path / "calibration.yaml"),
                "cmd_sock": str(tmp_path / "cmd.sock"),
            }
        )
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["pin_verified"] = True
        return app, client

    return _build


def _fake_daemon(**channels):
    """Stand in for the firmware's adc_voltages command."""
    return MagicMock(return_value={"ok": True, "channels": channels})


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


class TestOneDefinitionOfTheConversion:
    def test_the_wizard_inverts_exactly_what_the_sensor_applies(self):
        """Solve k from a reference, feed it forward, get the reference back.

        This is the whole guarantee. If someone reintroduces a second copy of
        the divider or the temperature term on either side, this fails — which
        is the only way to catch it, because both halves stay individually
        plausible and nothing throws.
        """
        for known_ppm in (100.0, 707.0, 1413.0):
            for adc_v in (0.05, 0.31, 1.2):
                for temp_c in (15.0, 25.0, 31.5, None):
                    k = k_from_reference(known_ppm, adc_v, temp_c)
                    assert ppm_from_adc(adc_v, temp_c, k) == pytest.approx(known_ppm, rel=1e-9)

    def test_solving_k_from_the_raw_adc_voltage_would_be_3_2x_wrong(self):
        """Documents the bug by size, so the magnitude is on the record.

        `k = ppm / v_adc` is the formula the wizard used and the number an
        installer could observe. It is wrong by 1/0.3125 = 3.2, which is large
        enough to matter and small enough to look like a plausible reading.
        """
        known_ppm, adc_v = 707.0, 0.4
        naive_k = known_ppm / adc_v
        assert ppm_from_adc(adc_v, 25.0, naive_k) == pytest.approx(known_ppm / TDS_DIVIDER_RATIO)
        assert ppm_from_adc(adc_v, 25.0, naive_k) / known_ppm == pytest.approx(3.2, abs=0.01)

    def test_a_non_conducting_probe_refuses_to_produce_a_calibration(self):
        """An enormous k is the failure that never announces itself.

        Solve against a dead probe and every later reading is wrong while
        looking entirely normal — which is worse than refusing.
        """
        for adc_v in (0.0, -0.01):
            with pytest.raises(ValueError, match="conducting"):
                k_from_reference(707.0, adc_v, 25.0)

    def test_a_nonsense_temperature_cannot_invert_a_reading(self):
        """A compensation coefficient <= 0 flips the sign of every ppm."""
        assert compensated_voltage(0.3, -9000.0) > 0


# ---------------------------------------------------------------------------
# The live readout
# ---------------------------------------------------------------------------


class TestLiveVoltageEndpoint:
    def test_it_reports_the_voltage_the_wizards_ask_for(self, make_client):
        app, client = make_client(temp_c=25.0)
        with patch(
            "service_window.routes.calibration.send_command",
            _fake_daemon(
                ph={"channel": 2, "adcVolts": 1.336},
                tds={"channel": 0, "adcVolts": 0.0096},
            ),
        ):
            body = client.get("/calibrate/live.json").get_json()
        assert body["ok"] is True
        assert body["channels"]["ph"]["adcVolts"] == 1.336
        assert body["channels"]["tds"]["adcVolts"] == 0.0096

    def test_the_tds_channel_also_carries_the_voltage_k_is_defined_against(self, make_client):
        """Both numbers, named — so "which voltage?" is never a question again."""
        app, client = make_client(temp_c=25.0)
        with patch(
            "service_window.routes.calibration.send_command",
            _fake_daemon(tds={"channel": 0, "adcVolts": 0.31250}),
        ):
            tds = client.get("/calibrate/live.json").get_json()["channels"]["tds"]
        assert tds["adcVolts"] == 0.3125
        assert tds["calibrationVolts"] == pytest.approx(1.0, abs=1e-4)

    def test_an_assumed_temperature_is_declared_not_hidden(self, make_client):
        """25 C is the right fallback and the wrong thing to assume silently.

        A beaker at 31 C calibrated as though it were 25 C is ~10% out, with
        nothing on screen to suggest it.
        """
        app, client = make_client(temp_c=None)
        with patch(
            "service_window.routes.calibration.send_command",
            _fake_daemon(tds={"channel": 0, "adcVolts": 0.3125}),
        ):
            body = client.get("/calibrate/live.json").get_json()
        assert body["tempAssumed"] is True
        assert body["tempC"] is None
        assert body["tempUsedC"] == 25.0

    def test_a_measured_temperature_is_used_and_named(self, make_client):
        app, client = make_client(temp_c=31.5)
        with patch(
            "service_window.routes.calibration.send_command",
            _fake_daemon(tds={"channel": 0, "adcVolts": 0.3125}),
        ):
            body = client.get("/calibrate/live.json").get_json()
        assert body["tempAssumed"] is False
        assert body["tempUsedC"] == pytest.approx(31.5)

    def test_a_dead_daemon_says_so_rather_than_rendering_an_empty_box(self, make_client):
        app, client = make_client()
        with patch(
            "service_window.routes.calibration.send_command",
            MagicMock(return_value={"ok": False, "error": "connection refused"}),
        ):
            resp = client.get("/calibrate/live.json")
        assert resp.status_code == 502
        assert "connection refused" in resp.get_json()["error"]

    def test_one_failed_channel_does_not_hide_the_others(self, make_client):
        app, client = make_client()
        with patch(
            "service_window.routes.calibration.send_command",
            _fake_daemon(
                ph={"channel": 2, "error": "I2C read failed"},
                tds={"channel": 0, "adcVolts": 0.0096},
            ),
        ):
            channels = client.get("/calibrate/live.json").get_json()["channels"]
        assert channels["ph"]["error"] == "I2C read failed"
        assert channels["tds"]["adcVolts"] == 0.0096

    def test_it_is_behind_the_pin(self, make_client, tmp_path):
        """The readout drives the bus. It is not a public page."""
        app, _ = make_client()
        anon = app.test_client()
        assert anon.get("/calibrate/live.json").status_code == 302


# ---------------------------------------------------------------------------
# The TDS wizard end to end
# ---------------------------------------------------------------------------


class TestTdsWizardSavesACorrectK:
    def _saved_k(self, app):
        from service_window.config_editor import read_config

        return read_config(app.config["CAL_PATH"]).get("tds_k")

    def test_a_reference_solution_produces_the_k_that_reads_it_back(self, make_client):
        app, client = make_client(temp_c=25.0)
        client.post("/calibrate/tds", data={"known_ppm": "707", "adc_volts": "0.4"})
        k = self._saved_k(app)
        assert ppm_from_adc(0.4, 25.0, k) == pytest.approx(707.0, abs=0.5)

    def test_the_old_field_name_is_rejected_rather_than_reinterpreted(self, make_client):
        """`measured_v` meant something else and solved to a different k.

        A rename means a stale form post fails loudly instead of being silently
        re-read under the new meaning — the failure mode this whole change is
        about.
        """
        app, client = make_client()
        client.post("/calibrate/tds", data={"known_ppm": "707", "measured_v": "0.4"})
        assert self._saved_k(app) is None

    def test_a_dead_probe_is_refused_at_the_wizard_too(self, make_client):
        app, client = make_client()
        client.post("/calibrate/tds", data={"known_ppm": "707", "adc_volts": "0"})
        assert self._saved_k(app) is None

    def test_zero_ppm_reference_is_refused(self, make_client):
        """k solved against 0 ppm is 0, which makes every reading 0 ppm."""
        app, client = make_client()
        client.post("/calibrate/tds", data={"known_ppm": "0", "adc_volts": "0.4"})
        assert self._saved_k(app) is None


# ---------------------------------------------------------------------------
# Why the readout is a command, not a local read
# ---------------------------------------------------------------------------


class TestAdcTransactionIsIndivisible:
    """One conversion register, four channels, two readers.

    `read_raw` is write-MUX -> poll -> read-conversion. Interleave two of those
    and the second caller's channel select lands between the first caller's
    start and its read, so pH comes back labelled TDS. No exception, no log
    line. The lock is what makes a second reader possible at all — and the
    reason the Service Window asks the daemon instead of opening the bus
    itself, since a lock in one process cannot serialise two.
    """

    def _adc(self):
        from sensors.ads1115 import ADS1115

        return ADS1115()

    def test_concurrent_reads_never_interleave(self):
        """The fake bus SLEEPS mid-transaction, on purpose.

        Without it this test passes whether or not the lock exists: MagicMock
        side-effects return too fast to be preempted, so no interleave ever
        occurs and the assertion proves nothing. A real I2C transaction is
        milliseconds of blocking I/O with the GIL released — the sleep is what
        makes the mock resemble the hardware in the one respect that matters.
        """
        adc = self._adc()
        in_flight: list[int] = []
        overlaps: list[tuple[int, int]] = []
        bookkeeping = threading.Lock()

        def write(_addr, _reg, data):
            channel = ((data[0] >> 4) & 0x07) - 0x04
            with bookkeeping:
                if in_flight:
                    overlaps.append((in_flight[-1], channel))
                in_flight.append(channel)
            time.sleep(0.001)  # conversion time: the window an interleave uses

        def read(_addr, reg, _n):
            if reg == 0x01:
                return [0x85, 0x83]  # OS bit set: conversion complete
            time.sleep(0.001)
            with bookkeeping:
                if in_flight:
                    in_flight.pop()
            return [0x10, 0x00]

        adc._bus = MagicMock()
        adc._bus.write_i2c_block_data.side_effect = write
        adc._bus.read_i2c_block_data.side_effect = read

        threads = [
            threading.Thread(target=lambda c=c: [adc.read_voltage(c) for _ in range(15)])
            for c in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert overlaps == [], f"channel select interleaved with a conversion read: {overlaps[:3]}"

    def test_closing_the_bus_waits_for_an_in_flight_read(self):
        """Shutdown must not turn into a traceback mid-transaction."""
        adc = self._adc()
        adc._bus = MagicMock()
        adc._bus.read_i2c_block_data.return_value = [0x85, 0x83]
        with adc._lock:
            closer = threading.Thread(target=adc.close)
            closer.start()
            closer.join(timeout=0.2)
            assert closer.is_alive(), "close() did not take the read lock"
        closer.join(timeout=1.0)
        assert adc._bus is None
