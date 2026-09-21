"""Tests for src/sensors/flow.py — the flow-meter driver family (2.3.0).

Rules under test, in the order they matter:

1. The lifetime totalizer is the register. A reboot restores it; a lost count
   is a counter reset the CLOUD records from the register going backwards —
   there is no ``counter_reset`` status, because in the payload contract a
   status displaces the value and the value must travel.
2. A meter that is not fitted publishes nothing. A first sample publishes the
   total and NOT a rate (no interval yet) rather than a 0.0 nobody measured.
3. A healthy unit's payload stays byte-identical (absent channels are absent).
"""

import json
import sqlite3
import struct
from types import SimpleNamespace

import pytest

from sensors.flow import (
    FLOW_CHANNELS,
    FLOW_RATE_KEY,
    FLOW_TOTAL_KEY,
    ModbusFlowMeter,
    PulseFlowMeter,
)
from sensors.modbus import ModbusBus, append_crc
from sensors.status import OK, OUT_OF_RANGE, READ_FAILED


class FakeLgpio:
    """Just enough of lgpio: records the claim and hands back the callback."""

    RISING_EDGE = 1

    def __init__(self) -> None:
        self.claims: list[tuple[int, int, int]] = []
        self.debounce: list[tuple[int, int, int]] = []
        self.closed: list[int] = []
        self.fire = None

    def gpiochip_open(self, chip):
        return 100 + chip

    def gpio_claim_alert(self, handle, gpio, edge):
        self.claims.append((handle, gpio, edge))

    def gpio_set_debounce_micros(self, handle, gpio, us):
        self.debounce.append((handle, gpio, us))

    def callback(self, handle, gpio, edge, func):
        self.fire = func
        return SimpleNamespace(cancel=lambda: None)

    def gpiochip_close(self, handle):
        self.closed.append(handle)


def make_pulse(**kw):
    lg = FakeLgpio()
    clock = {"t": 1000.0}
    meter = PulseFlowMeter(gpio=26, k_ppg=100.0, lgpio_module=lg, clock=lambda: clock["t"], **kw)
    return meter, lg, clock


class TestPulseFlowMeter:
    def test_claims_the_pin_with_debounce_on_the_rising_edge(self):
        meter, lg, _ = make_pulse(debounce_us=800)
        assert lg.claims == [(100, 26, 1)]
        assert lg.debounce == [(100, 26, 800)]
        meter.close()
        assert lg.closed == [100]

    def test_first_sample_publishes_the_total_and_no_rate(self):
        meter, lg, _ = make_pulse()
        for _ in range(250):
            lg.fire(0, 26, 1, 0)
        out = meter.read_detailed()
        assert out[FLOW_TOTAL_KEY].value == pytest.approx(2.5)  # 250 pulses / 100 ppg
        assert out[FLOW_TOTAL_KEY].status == OK
        # No interval yet → the rate is ABSENT, never 0.0.
        assert FLOW_RATE_KEY not in out

    def test_rate_is_pulses_over_the_interval_and_total_never_decreases(self):
        meter, lg, clock = make_pulse()
        meter.read_detailed()  # t=1000, count 0
        for _ in range(300):
            lg.fire(0, 26, 1, 0)
        clock["t"] = 1060.0  # one minute later
        out = meter.read_detailed()
        assert out[FLOW_TOTAL_KEY].value == pytest.approx(3.0)
        assert out[FLOW_RATE_KEY].value == pytest.approx(3.0)  # 3 gal in 1 min
        clock["t"] = 1120.0  # idle minute
        out = meter.read_detailed()
        assert out[FLOW_TOTAL_KEY].value == pytest.approx(3.0)  # frozen, not lower
        assert out[FLOW_RATE_KEY].value == 0.0

    def test_lifetime_count_is_restored_and_persisted(self):
        persisted: list[int] = []
        meter, lg, _ = make_pulse(initial_count=5000, persist=persisted.append)
        for _ in range(10):
            lg.fire(0, 26, 1, 0)
        out = meter.read_detailed()
        assert out[FLOW_TOTAL_KEY].value == pytest.approx(50.1)
        assert persisted == [5010]
        assert meter.lifetime_pulses == 5010

    def test_a_failing_persist_never_stops_the_sample(self):
        def boom(_n):
            raise OSError("disk full")

        meter, lg, _ = make_pulse(persist=boom)
        lg.fire(0, 26, 1, 0)
        assert meter.read_detailed()[FLOW_TOTAL_KEY].value == pytest.approx(0.01)

    def test_implausible_rate_is_out_of_range_but_the_total_still_counts(self):
        meter, lg, clock = make_pulse()
        meter.read_detailed()
        for _ in range(100_000):
            lg.fire(0, 26, 1, 0)
        clock["t"] = 1001.0  # 1000 gal in one second
        out = meter.read_detailed()
        assert out[FLOW_TOTAL_KEY].status == OK
        assert out[FLOW_RATE_KEY].status == OUT_OF_RANGE
        assert out[FLOW_RATE_KEY].value is None

    def test_k_factor_from_a_bucket_test_changes_the_gallons(self):
        meter, lg, _ = make_pulse()
        for _ in range(200):
            lg.fire(0, 26, 1, 0)
        meter.set_calibration(200.0)
        assert meter.read_detailed()[FLOW_TOTAL_KEY].value == pytest.approx(1.0)
        with pytest.raises(ValueError):
            meter.set_calibration(0)

    def test_rejects_a_pin_off_the_header(self):
        with pytest.raises(ValueError):
            PulseFlowMeter(gpio=40, lgpio_module=FakeLgpio())


# ---------------------------------------------------------------------------
# Modbus (clamp-on) meter
# ---------------------------------------------------------------------------


class FakeSerial:
    def __init__(self) -> None:
        self.responses: list[bytes] = []
        self.writes: list[bytes] = []

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def read(self, n: int) -> bytes:
        if not self.responses:
            return b""
        return self.responses.pop(0)[:n]

    def close(self) -> None:
        pass


def make_bus(fake: FakeSerial) -> ModbusBus:
    return ModbusBus("/dev/ttyTEST", retries=1, serial_factory=lambda: fake, sleep=lambda _s: None)


def fc03(address: int, regs: list[int]) -> bytes:
    body = struct.pack(">BBB", address, 0x03, len(regs) * 2)
    for r in regs:
        body += struct.pack(">H", r)
    return append_crc(body)


def float_abcd(v: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", v)))


def int32_abcd(v: int) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">i", v)))


class TestModbusFlowMeter:
    def test_reads_unit_and_multiplier_once_then_total_and_rate_in_gallons(self):
        fake = FakeSerial()
        # unit index 0 = m³, multiplier index 3 = x1, total 12 m³, rate 0.24 m³/h
        fake.responses += [
            fc03(4, [0]),
            fc03(4, [3]),
            fc03(4, int32_abcd(12)),
            fc03(4, float_abcd(0.24)),
        ]
        meter = ModbusFlowMeter(make_bus(fake), address=4)
        out = meter.read_detailed()
        assert out[FLOW_TOTAL_KEY].value == pytest.approx(12 * 264.172, rel=1e-4)
        assert out[FLOW_RATE_KEY].value == pytest.approx(0.24 * 264.172 / 60, rel=1e-3)
        # Unit + multiplier are read once; the next cycle is two reads.
        fake.responses += [fc03(4, int32_abcd(13)), fc03(4, float_abcd(0.0))]
        out = meter.read_detailed()
        assert out[FLOW_TOTAL_KEY].value == pytest.approx(13 * 264.172, rel=1e-4)
        assert out[FLOW_RATE_KEY].value == 0.0
        # Register addresses are the manual's numbers minus one: 1437→0x059C, 9→0x0008, 1→0x0000.
        assert fake.writes[0][:4] == bytes.fromhex("0403059C")
        assert fake.writes[2][:4] == bytes.fromhex("04030008")
        assert fake.writes[3][:4] == bytes.fromhex("04030000")

    def test_gallon_unit_and_multiplier_are_honoured(self):
        fake = FakeSerial()
        # unit index 2 = gal, multiplier index 1 = x0.01, accumulator 123456 → 1234.56 gal
        fake.responses += [
            fc03(4, [2]),
            fc03(4, [1]),
            fc03(4, int32_abcd(123456)),
            fc03(4, float_abcd(6.0)),
        ]
        out = ModbusFlowMeter(make_bus(fake), address=4).read_detailed()
        assert out[FLOW_TOTAL_KEY].value == pytest.approx(1234.56)
        assert out[FLOW_RATE_KEY].value == pytest.approx(0.1)  # 6 gal/h

    def test_silence_is_read_failed_on_both_channels(self):
        out = ModbusFlowMeter(make_bus(FakeSerial()), address=4).read_detailed()
        assert out[FLOW_TOTAL_KEY].status == READ_FAILED
        assert out[FLOW_RATE_KEY].status == READ_FAILED
        assert out[FLOW_TOTAL_KEY].value is None

    def test_an_unknown_unit_refuses_rather_than_guesses(self):
        fake = FakeSerial()
        fake.responses += [fc03(4, [9]), fc03(4, [3])]
        out = ModbusFlowMeter(make_bus(fake), address=4).read_detailed()
        assert out[FLOW_TOTAL_KEY].status == READ_FAILED

    def test_unknown_model_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            ModbusFlowMeter(make_bus(FakeSerial()), address=4, model="nope")


# ---------------------------------------------------------------------------
# Plumbing: worker, cloud payload, LoRa, database, calibration, monitor
# ---------------------------------------------------------------------------


class FakeFlow:
    name = "flow"

    def __init__(self, total, rate, rate_status=OK):
        from sensors.status import SensorResult

        self._out = {FLOW_TOTAL_KEY: SensorResult(total, OK)}
        if rate_status == OK:
            self._out[FLOW_RATE_KEY] = SensorResult(rate, OK)
        else:
            self._out[FLOW_RATE_KEY] = SensorResult(None, rate_status, "test")

    def read_detailed(self):
        return dict(self._out)


class FakeDB:
    def __init__(self):
        self.readings = []

    def insert_reading(self, r):
        self.readings.append(r)
        return len(self.readings)


def make_worker(sensors):
    from app.state import StateStore
    from app.workers import SamplingWorker

    settings = SimpleNamespace(sensor_read_s=60)
    return SamplingWorker(
        lambda: settings,
        sensors=sensors,
        db=(db := FakeDB()),
        rules=None,
        relays=None,
        leds=None,
        health=SimpleNamespace(update_last_seen=lambda: None),
        state=StateStore(),
    ), db


class TestPlumbing:
    def test_worker_stores_both_columns_and_a_rate_fault(self, mock_hardware):
        worker, db = make_worker({"flow": FakeFlow(1234.5, None, rate_status=OUT_OF_RANGE)})
        worker.step()
        row = db.readings[-1]
        assert row["flow_total_gal"] == 1234.5
        assert row["flow_rate_gpm"] is None
        assert json.loads(row["sensor_status"]) == {"flow_rate_gpm": OUT_OF_RANGE}

    def test_a_totalizer_alone_is_a_reading(self, mock_hardware):
        # An idle AWG's frozen register is a measurement of zero production.
        worker, db = make_worker({"flow": FakeFlow(1234.5, 0.0), "ph": None})
        worker.step()
        assert len(db.readings) == 1

    def test_no_meter_means_no_flow_keys_anywhere(self, mock_hardware):
        worker, db = make_worker({"temperature": SimpleNamespace(read_temp_c=lambda: 21.0)})
        worker.step()
        row = db.readings[-1]
        assert row["flow_total_gal"] is None and row["flow_rate_gpm"] is None

    def test_cloud_payload_carries_flow_only_when_present(self, mock_hardware):
        from cloud.client import CloudClient

        client = CloudClient("BS-WQM1-TEST", "http://i", "http://c", "k", "2.3.0")
        with_flow = client.reading_to_json(
            {
                "timestamp": "2026-09-17T12:00:00Z",
                "ph": 7.0,
                "flow_total_gal": 1234.5,
                "flow_rate_gpm": 0.04,
            }
        )["sensors"]
        assert with_flow["flow_total_gal"] == {"value": 1234.5}
        assert with_flow["flow_rate_gpm"] == {"value": 0.04}
        without = client.reading_to_json({"timestamp": "2026-09-17T12:00:00Z", "ph": 7.0})[
            "sensors"
        ]
        assert "flow_total_gal" not in without and "flow_rate_gpm" not in without

    def test_a_flow_fault_travels_with_its_status(self, mock_hardware):
        from cloud.client import CloudClient

        client = CloudClient("BS-WQM1-TEST", "http://i", "http://c", "k", "2.3.0")
        sensors = client.reading_to_json(
            {
                "timestamp": "2026-09-17T12:00:00Z",
                "flow_total_gal": 10.0,
                "sensor_status": json.dumps({"flow_rate_gpm": READ_FAILED}),
            }
        )["sensors"]
        assert sensors["flow_rate_gpm"] == {"value": None, "status": READ_FAILED}

    def test_lora_channels_13_and_14_round_trip_in_kilogallons(self, mock_hardware):
        from radio.cayenne import CH_FLOW_RATE, CH_FLOW_TOTAL, decode, encode

        assert (CH_FLOW_RATE, CH_FLOW_TOTAL) == (13, 14)
        payload = encode({"flow_rate_gpm": 0.04, "flow_total_gal": 12340.0})
        out = decode(payload)
        assert out["flow_rate_gpm"] == pytest.approx(0.04)
        assert out["flow_total_gal"] == pytest.approx(12340.0)  # 10-gal steps on air
        # Headroom: 300,000 gal fits; gallons on air would have wrapped at 327.
        assert decode(encode({"flow_total_gal": 300000.0}))["flow_total_gal"] == pytest.approx(
            300000.0
        )

    def test_database_v5_to_v6_migration_adds_the_columns_and_meta_survives(
        self, tmp_path, mock_hardware
    ):
        from storage.database import SCHEMA_VERSION, WQM1Database

        path = tmp_path / "v5.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                ph REAL, tds_ppm REAL, turbidity_ntu REAL, orp_mv REAL, temp_c REAL,
                chlorine_mgl REAL, conductivity_uscm REAL, salinity_ppt REAL,
                lat REAL, lon REAL, alt_m REAL, battery_v REAL, relay_state INTEGER DEFAULT 0,
                synced INTEGER DEFAULT 0, sync_state TEXT NOT NULL DEFAULT 'pending',
                sync_attempts INTEGER NOT NULL DEFAULT 0, sensor_status TEXT
            );
            CREATE TABLE lorawan_session (
                id INTEGER PRIMARY KEY CHECK (id = 1), dev_addr BLOB, nwk_skey BLOB, app_skey BLOB,
                fcnt_up INTEGER DEFAULT 0, fcnt_down INTEGER DEFAULT 0, joined INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP, mac_params TEXT
            );
            INSERT OR IGNORE INTO lorawan_session (id) VALUES (1);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('schema_version', '5');
            INSERT INTO readings (timestamp, ph) VALUES ('2026-09-01T00:00:00Z', 7.0);
            """
        )
        conn.commit()
        conn.close()

        db = WQM1Database(path=str(path))
        # v7 (clock confidence) followed v6; a v5 buffer must land on the
        # CURRENT version, having applied both — the v6 block writes '6'
        # literally so that a v5 device cannot record itself as v7 and skip
        # the v7 column (the v3 trap, again).
        assert SCHEMA_VERSION == 7
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(readings)")}
        assert {"flow_total_gal", "flow_rate_gpm", "clock_source"} <= cols
        assert db.get_meta("schema_version") == "7"
        # The old row survives with NULL flow — never a zero.
        assert db.get_latest()["flow_total_gal"] is None
        # Lifetime pulse count round-trips through meta.
        assert db.get_meta("flow_pulse_count") is None
        db.set_meta("flow_pulse_count", "5010")
        assert db.get_meta("flow_pulse_count") == "5010"
        row_id = db.insert_reading(
            {"timestamp": "2026-09-17T12:00:00Z", "flow_total_gal": 3.0, "flow_rate_gpm": 0.1}
        )
        assert row_id > 0
        assert db.get_latest()["flow_total_gal"] == 3.0
        db.close()

    def test_bucket_test_sets_the_k_factor(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        assert cm.data.flow_k_ppg == pytest.approx(1703.4)
        k = cm.calibrate_flow(known_gal=5.0, pulses=8600)
        assert k == pytest.approx(1720.0)
        assert cm.data.flow_k_ppg == pytest.approx(1720.0)
        assert cm.data.calibrated_at.get("flow")
        with pytest.raises(ValueError):
            cm.calibrate_flow(known_gal=0, pulses=10)

    def test_monitor_never_calls_an_idle_meter_stuck_but_still_notices_silence(self, mock_hardware):
        from sensing.monitor import NO_FLATLINE, SensorMonitor

        assert "flow" in NO_FLATLINE
        settings = SimpleNamespace(
            flatline_window_min=20,
            spike_z_threshold=6.0,
            drift_check_enabled=False,
        )
        t = {"now": 0.0}
        mon = SensorMonitor(lambda: settings, clock=lambda: t["now"])
        events = []
        for i in range(30):
            t["now"] = i * 60.0
            events += mon.observe({"flow_rate_gpm": 0.0})
        assert not [e for e in events if e["type"] == "sensor_stuck"]
        for i in range(30, 60):
            t["now"] = i * 60.0
            events += mon.observe({"flow_rate_gpm": None})
        kinds = [e["details"]["kind"] for e in events if e["type"] == "sensor_stuck"]
        assert kinds == ["no_data"]

    def test_explain_knows_the_flow_meter(self, mock_hardware):
        from diagnostics.explain import SENSOR_SUBJECTS, explain

        assert "flow" in SENSOR_SUBJECTS
        assert "flow meter" in explain("flow", "stuck_no_data", {"minutes": 3})["message"].lower()

    def test_settings_defaults_are_off_and_in_the_schema(self, mock_hardware):
        from utils.config import SETTINGS_SCHEMA, Settings

        s = Settings()
        assert s.flow_pulse_enabled is False and s.rs485_flow_enabled is False
        assert s.flow_pulse_gpio == 26 and s.rs485_baud == 9600
        for key in (
            "flow_pulse_enabled",
            "flow_pulse_gpio",
            "rs485_flow_enabled",
            "rs485_flow_addr",
            "rs485_flow_model",
            "rs485_baud",
        ):
            assert key in SETTINGS_SCHEMA, key

    def test_channel_pair_is_exactly_two(self):
        assert FLOW_CHANNELS == ("flow_total_gal", "flow_rate_gpm")

    def test_service_window_reader_tolerates_an_unmigrated_buffer(self, tmp_path, mock_hardware):
        """The reader runs while the main service (which migrates) restarts."""
        from service_window.db_reader import DBReader

        old = tmp_path / "old.db"
        conn = sqlite3.connect(str(old))
        conn.executescript(
            """
            CREATE TABLE readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                ph REAL, tds_ppm REAL, turbidity_ntu REAL, orp_mv REAL, temp_c REAL,
                lat REAL, lon REAL, alt_m REAL, battery_v REAL,
                relay_state INTEGER DEFAULT 0, synced INTEGER DEFAULT 0
            );
            INSERT INTO readings (timestamp, ph) VALUES ('2026-09-17T12:00:00Z', 7.1);
            """
        )
        conn.commit()
        conn.close()
        reader = DBReader(str(old))
        assert reader.get_latest_reading()["ph"] == 7.1
        assert "flow_total_gal" not in reader.get_readings(1)[0]

        new = tmp_path / "new.db"
        from storage.database import WQM1Database

        db = WQM1Database(path=str(new))
        db.insert_reading({"timestamp": "2026-09-17T12:00:00Z", "flow_total_gal": 2.0})
        db.close()
        assert DBReader(str(new)).get_latest_reading()["flow_total_gal"] == 2.0
