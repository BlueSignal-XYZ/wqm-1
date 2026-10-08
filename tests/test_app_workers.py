"""Tests for src/app/workers.py — worker step logic with fake collaborators."""

from types import SimpleNamespace


def make_settings(**overrides):
    base = dict(
        sensor_read_s=60,
        gps_fix_s=600,
        gps_fix_timeout_s=30,
        lora_tx_s=300,
        sync_interval_s=300,
        command_poll_s=5,
        heartbeat_s=600,
        db_path="/tmp/x.db",
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    return lambda: ns


class FakeDB:
    def __init__(self):
        self.readings = []

    def insert_reading(self, r):
        self.readings.append(r)
        return len(self.readings)

    def get_latest(self):
        return self.readings[-1] if self.readings else None

    def get_state_count(self, state):
        return {"pending": len(self.readings), "failed_permanent": 0}[state]


class FakeSensor:
    def __init__(self, value=7.0, fail=False):
        self.value = value
        self.fail = fail

    def read(self, temp_c=None):
        if self.fail:
            raise OSError("i2c error")
        return self.value

    def read_temp_c(self):
        if self.fail:
            raise OSError("1-wire error")
        return self.value


class TestSamplingWorker:
    def make(self, state=None, monitor=None, adaptive=None, rules=None, sensors=None):
        from app.state import StateStore
        from app.workers import SamplingWorker

        state = state or StateStore()
        sensors = sensors or {
            "temperature": FakeSensor(21.5),
            "ph": FakeSensor(7.2),
            "tds": FakeSensor(340.0),
            "turbidity": FakeSensor(3.0),
            "orp": None,
        }
        db = FakeDB()
        health = SimpleNamespace(update_last_seen=lambda: None)
        relays = SimpleNamespace(get_state_bitmask=lambda: 5)
        worker = SamplingWorker(
            make_settings(),
            sensors=sensors,
            db=db,
            rules=rules,
            relays=relays,
            leds=None,
            health=health,
            state=state,
            monitor=monitor,
            adaptive=adaptive,
        )
        return worker, db, state

    def test_step_stores_reading_with_gps_and_relay_state(self, mock_hardware):
        worker, db, state = self.make()
        state.set_gps(37.0, -78.0, 100.0, 8)
        worker.step()
        assert len(db.readings) == 1
        r = db.readings[0]
        assert r["ph"] == 7.2
        assert r["tds_ppm"] == 340.0
        assert r["lat"] == 37.0
        assert r["relay_state"] == 5

    def test_failed_sensor_stores_none_and_counts_error(self, mock_hardware):
        sensors = {
            "temperature": FakeSensor(21.5),
            "ph": FakeSensor(fail=True),
            "tds": FakeSensor(340.0),
            "turbidity": FakeSensor(3.0),
            "orp": None,
        }
        worker, db, state = self.make(sensors=sensors)
        worker.step()
        assert db.readings[0]["ph"] is None
        assert db.readings[0]["tds_ppm"] == 340.0  # others unaffected
        assert state.error_counts()["sensor"] == 1

    def test_monitor_events_are_queued_and_rules_suspended(self, mock_hardware):
        events = [{"type": "sensor_stuck", "sensor": "ph", "message": "flat"}]

        class FakeMonitor:
            def observe(self, reading):
                return list(events)

            def suspended_sensors(self):
                return {"ph"}

        class FakeRules:
            def __init__(self):
                self.suspended = None
                self.evaluated = []

            def set_suspended_sensors(self, s):
                self.suspended = s

            def evaluate(self, reading):
                self.evaluated.append(reading)

        rules = FakeRules()
        worker, db, state = self.make(monitor=FakeMonitor(), rules=rules)
        worker.step()
        assert state.drain_events() == events
        assert rules.suspended == {"ph"}
        assert len(rules.evaluated) == 1

    def test_adaptive_interval_wins_when_present(self, mock_hardware):
        class FakeAdaptive:
            def observe(self, reading):
                pass

            def current_interval_s(self):
                return 15

        worker, _, _ = self.make(adaptive=FakeAdaptive())
        assert worker.interval_s() == 15.0

    def test_interval_falls_back_to_settings(self, mock_hardware):
        worker, _, _ = self.make()
        assert worker.interval_s() == 60.0


class FakeMultiSensor:
    """RS485 5-in-1 stand-in: read_all() returns all five params at once."""

    def __init__(self, values=None, fail=False):
        self.values = values
        self.fail = fail

    def read_all(self):
        if self.fail:
            raise OSError("modbus timeout")
        return self.values


class TestSamplingWorkerRs485:
    make = TestSamplingWorker.make

    MULTI = {
        "ph": 6.86,
        "conductivity_uscm": 100.0,
        "temp_c": 25.0,
        "tds_ppm": 50.0,
        "salinity_ppt": 0.5,
    }

    def sensors(self, **overrides):
        base = {
            "temperature": FakeSensor(21.5),
            "ph": FakeSensor(7.2),
            "tds": FakeSensor(340.0),
            "turbidity": FakeSensor(3.0),
            "orp": None,
            "chlorine": None,
            "multi485": None,
        }
        base.update(overrides)
        return base

    def test_multi485_supersedes_analog_ph_tds_temp(self, mock_hardware):
        worker, db, _ = self.make(sensors=self.sensors(multi485=FakeMultiSensor(self.MULTI)))
        worker.step()
        r = db.readings[0]
        assert r["ph"] == 6.86  # digital, not the analog 7.2
        assert r["tds_ppm"] == 50.0  # digital, not the analog 340.0
        assert r["temp_c"] == 25.0  # digital, not the DS18B20 21.5
        assert r["conductivity_uscm"] == 100.0
        assert r["salinity_ppt"] == 0.5
        assert r["turbidity_ntu"] == 3.0  # analog params unaffected

    def test_multi485_failure_falls_back_to_analog(self, mock_hardware):
        worker, db, state = self.make(sensors=self.sensors(multi485=FakeMultiSensor(fail=True)))
        worker.step()
        r = db.readings[0]
        assert r["ph"] == 7.2  # analog fallback
        assert r["tds_ppm"] == 340.0
        assert r["temp_c"] == 21.5
        assert r["conductivity_uscm"] is None
        assert r["salinity_ppt"] is None
        assert state.error_counts()["sensor"] == 1

    def test_multi485_returning_none_falls_back(self, mock_hardware):
        worker, db, _ = self.make(sensors=self.sensors(multi485=FakeMultiSensor(values=None)))
        worker.step()
        assert db.readings[0]["ph"] == 7.2

    def test_chlorine_sensor_stored(self, mock_hardware):
        worker, db, _ = self.make(sensors=self.sensors(chlorine=FakeSensor(0.35)))
        worker.step()
        assert db.readings[0]["chlorine_mgl"] == 0.35

    def test_no_rs485_sensors_stores_nulls(self, mock_hardware):
        worker, db, _ = self.make(sensors=self.sensors())
        worker.step()
        r = db.readings[0]
        assert r["chlorine_mgl"] is None
        assert r["conductivity_uscm"] is None
        assert r["salinity_ppt"] is None


class TestCommandWorker:
    def make(self, poll_result, state=None):
        from app.state import StateStore
        from app.workers import CommandWorker

        state = state or StateStore()
        applied = []
        config_versions = []
        ota_nudges = []
        sent_events = []

        cloud = SimpleNamespace(
            poll=lambda: poll_result,
            send_event=lambda t, message=None, sensor=None, details=None: sent_events.append(
                {"type": t, "message": message}
            ),
        )
        worker = CommandWorker(
            make_settings(),
            cloud,
            state,
            apply_command=applied.append,
            on_config_version=config_versions.append,
            on_ota_pending=lambda: ota_nudges.append(True),
        )
        return worker, state, applied, config_versions, ota_nudges, sent_events

    def test_applies_commands_and_hints(self, mock_hardware):
        poll = {
            "commands": [{"id": "c1", "type": "relay"}],
            "configVersion": 7,
            "otaPending": True,
        }
        worker, state, applied, cfg, ota, _ = self.make(poll)
        worker.step()
        assert applied == [{"id": "c1", "type": "relay"}]
        assert cfg == [7]
        assert ota == [True]

    def test_one_bad_command_does_not_stop_the_rest(self, mock_hardware):
        from app.state import StateStore
        from app.workers import CommandWorker

        state = StateStore()
        seen = []

        def apply(cmd):
            seen.append(cmd["id"])
            if cmd["id"] == "bad":
                raise ValueError("nope")

        cloud = SimpleNamespace(
            poll=lambda: {"commands": [{"id": "bad"}, {"id": "good"}]},
            send_event=lambda *a, **k: None,
        )
        worker = CommandWorker(make_settings(), cloud, state, apply_command=apply)
        worker.step()
        assert seen == ["bad", "good"]

    def test_drains_event_queue_to_cloud(self, mock_hardware):
        poll = {"commands": [], "configVersion": None, "otaPending": False}
        worker, state, _, _, _, sent = self.make(poll)
        state.emit_event({"type": "sensor_stuck", "message": "flat", "sensor": "ph"})
        worker.step()
        assert sent == [{"type": "sensor_stuck", "message": "flat"}]


class TestHeartbeatWorker:
    def test_sends_full_payload(self, mock_hardware):
        from app.state import StateStore
        from app.workers import HeartbeatWorker
        from utils.health import HealthReporter

        state = StateStore()
        state.incr_error("cloud")
        sent = []
        cloud = SimpleNamespace(send_heartbeat=lambda p: (sent.append(p), True)[1])
        hr = HealthReporter("2.0.0", clock=lambda: 0.0)
        worker = HeartbeatWorker(
            make_settings(),
            cloud,
            FakeDB(),
            hr,
            state,
            config_version_provider=lambda: 4,
            sensor_health_provider=lambda: {"ph": {"status": "ok"}},
        )
        worker.step()
        assert len(sent) == 1
        hb = sent[0]
        assert hb["firmwareVersion"] == "2.0.0"
        assert hb["configVersion"] == 4
        assert hb["errorCounts"]["cloud"] == 1
        assert hb["sensorHealth"] == {"ph": {"status": "ok"}}
        assert hb["bufferDepth"] == 0


class FakeGps:
    """A GPS that can express power save, because the worker now uses it.

    The previous fake was a SimpleNamespace with `get_fix` and `power_cycle`
    only. A fake that cannot represent sleeping cannot catch a unit that never
    sleeps — the same shape of hole as an RTDB double that could not express
    merge-vs-replace.

    `fixes` is consumed one per get_fix() call, so a test can say "nothing on
    the first attempt, a fix on the retry" and check the loop actually closes.
    """

    def __init__(self, fixes):
        self._fixes = list(fixes)
        self.calls = []
        self.awake = True

    def get_fix(self, timeout_s):
        self.calls.append("get_fix")
        return self._fixes.pop(0) if self._fixes else None

    def wake(self):
        self.calls.append("wake")
        self.awake = True

    def sleep(self):
        self.calls.append("sleep")
        self.awake = False

    def resync(self):
        self.calls.append("resync")

    def power_cycle(self):
        self.calls.append("power_cycle")


FIX = SimpleNamespace(latitude=37.5, longitude=-78.5, altitude=90.0, satellites=7)


class TestGpsWorker:
    def _run(self, fixes, state=None):
        from app.state import StateStore
        from app.workers import GpsWorker

        state = state or StateStore()
        gps = FakeGps(fixes)
        GpsWorker(make_settings(), gps, None, state).step()
        return gps, state

    def test_fix_published_to_state(self, mock_hardware):
        gps, state = self._run([FIX])
        assert state.gps().lat == 37.5
        assert state.gps().sats == 7

    def test_the_module_is_left_asleep_after_a_good_fix(self, mock_hardware):
        """The whole point of a daily cadence on a solar unit.

        Acquiring once a day and then tracking continuously for the other 23
        hours 59 minutes spends the power the cadence change was meant to save.
        """
        gps, _ = self._run([FIX])
        assert gps.calls == ["wake", "get_fix", "sleep"]
        assert gps.awake is False

    def test_a_desynced_toggle_self_corrects_within_one_cycle(self, mock_hardware):
        """EXTINT has no readback, so `wake()` can do the opposite of its name.

        Nothing arrives, we pulse again, and the retry succeeds. Without this
        one desync would cost every fix from then on and never say so.
        """
        gps, state = self._run([None, FIX])
        assert gps.calls == ["wake", "get_fix", "resync", "get_fix", "sleep"]
        assert state.gps().lat == 37.5

    def test_no_fix_and_no_previous_triggers_power_cycle(self, mock_hardware):
        # A module that has NEVER produced a coordinate is a different fault
        # from a drifted toggle, and gets the bigger hammer. It is deliberately
        # left awake so the next attempt starts warm.
        gps, _ = self._run([None, None])
        assert gps.calls.count("power_cycle") == 1
        assert "sleep" not in gps.calls

    def test_a_failed_attempt_with_a_known_coordinate_goes_back_to_sleep(self, mock_hardware):
        """We already know where the unit is; it has not moved.

        Burning the interval searching for a coordinate we hold would trade
        real power for nothing.
        """
        from app.state import StateStore

        state = StateStore()
        state.set_gps(30.38, -97.99, 209.5, 12)
        gps, _ = self._run([None, None], state=state)
        assert gps.calls == ["wake", "get_fix", "resync", "get_fix", "sleep"]
        assert "power_cycle" not in gps.calls


class TestGpsCadenceBounds:
    """At most once a day, at least once every fifteen days (founder, 2026-08-21).

    A WQM-1 is bolted to a structure. Its coordinate changes only if somebody
    physically moves the unit, so the old ten-minute default spent solar budget
    re-deriving a constant.
    """

    def test_the_default_is_daily(self):
        from utils.config import Settings

        assert Settings().gps_fix_s == 86_400

    def test_the_bounds_are_one_day_to_fifteen_days(self):
        from utils.config import GPS_FIX_MAX_S, GPS_FIX_MIN_S, SETTINGS_SCHEMA

        spec = SETTINGS_SCHEMA["gps_fix_s"]
        assert spec.min == GPS_FIX_MIN_S == 86_400
        assert spec.max == GPS_FIX_MAX_S == 1_296_000

    def test_a_deployed_unit_carrying_the_old_600_is_rejected_not_honoured(self):
        """It must land on the default and SAY so, not keep the old rate.

        The validator rejects out-of-range values and falls back, so an
        existing config.yaml with the ten-minute cadence produces a logged
        error rather than a unit that quietly keeps draining its pack.
        """
        from utils.config import validate_values

        accepted, errors = validate_values({"gps_fix_s": 600})
        assert "gps_fix_s" not in accepted
        assert any("gps_fix_s" in e for e in errors)


class TestUnequippedUnitRecordsNothing:
    """A unit with no probe fitted must not manufacture empty reading rows.

    The cloud rejects a sensor-less row and the firmware marks it
    failed_permanent, so every such row is a permanent dead entry that is
    never retried.
    """

    def _worker(self, sensors):
        from app.workers import SamplingWorker

        db = FakeDB()
        worker = SamplingWorker(
            make_settings(),
            sensors=sensors,
            db=db,
            rules=None,
            relays=None,
            leds=None,
            health=SimpleNamespace(update_last_seen=lambda: None),
            state=SimpleNamespace(
                gps=lambda: SimpleNamespace(lat=None, lon=None, alt_m=None),
                incr_error=lambda _b: None,
                emit_event=lambda _e: True,
            ),
        )
        return worker, db

    def test_no_probe_fitted_writes_no_row(self):
        worker, db = self._worker(
            {
                "temperature": None,
                "ph": None,
                "tds": None,
                "turbidity": None,
                "orp": None,
                "chlorine": None,
                "multi485": None,
            }
        )
        for _ in range(5):
            worker.step()
        assert db.readings == []

    def test_a_fitted_probe_that_fails_records_no_row_either(self):
        """Corrected 2026-08-19, after the field disproved the original rule.

        This used to assert that a fitted-but-failing probe should still be
        stored so the fault stayed visible. It cannot be: the cloud rejects a
        sensor-less row as "Missing sensors data" and the firmware marks it
        failed_permanent, so the row records nothing anyone reads — it only
        grows the graveyard. The fault belongs on the sensor error counter and
        the heartbeat, which _safe_read already feeds.
        """
        worker, db = self._worker(
            {
                "temperature": None,
                "ph": FakeSensor(fail=True),
                "tds": None,
                "turbidity": None,
                "orp": None,
                "chlorine": None,
                "multi485": None,
            }
        )
        worker.step()
        assert db.readings == []

    def test_recording_resumes_once_a_probe_is_declared(self):
        sensors = {
            "temperature": None,
            "ph": None,
            "tds": None,
            "turbidity": None,
            "orp": None,
            "chlorine": None,
            "multi485": None,
        }
        worker, db = self._worker(sensors)
        worker.step()
        assert db.readings == []

        sensors["ph"] = FakeSensor(value=7.2)
        worker.step()
        assert len(db.readings) == 1
        assert db.readings[0]["ph"] == 7.2


class TestDeclaredButAbsentProbeRecordsNothing:
    """The failure the first version of this guard missed.

    A driver may return a live object for hardware that is not there —
    DS18B20.__init__ catches NoSensorFoundError and still constructs. So
    "is any sensor object wired" is not the same question as "did anything
    measure", and only the second one keeps the graveyard empty.
    """

    def _worker(self, sensors):
        from app.workers import SamplingWorker

        db = FakeDB()
        errors = []
        worker = SamplingWorker(
            make_settings(),
            sensors=sensors,
            db=db,
            rules=None,
            relays=None,
            leds=None,
            health=SimpleNamespace(update_last_seen=lambda: None),
            state=SimpleNamespace(
                gps=lambda: SimpleNamespace(lat=None, lon=None, alt_m=None),
                incr_error=lambda b: errors.append(b),
                emit_event=lambda _e: True,
            ),
        )
        return worker, db, errors

    def test_object_present_but_reading_nothing_writes_no_row(self):
        """A probe declared and constructed, but returning None every cycle."""
        dead = FakeSensor(fail=True)
        worker, db, errors = self._worker(
            {
                "temperature": None,
                "ph": dead,
                "tds": None,
                "turbidity": None,
                "orp": None,
                "chlorine": None,
                "multi485": None,
            }
        )
        for _ in range(5):
            worker.step()
        assert db.readings == []
        # The fault still surfaces — through the error counter, which is what
        # the heartbeat carries.
        assert errors.count("sensor") == 5

    def test_a_single_working_probe_is_enough_to_record(self):
        worker, db, _ = self._worker(
            {
                "temperature": None,
                "ph": FakeSensor(value=7.1),
                "tds": None,
                "turbidity": None,
                "orp": None,
                "chlorine": None,
                "multi485": None,
            }
        )
        worker.step()
        assert len(db.readings) == 1
        assert db.readings[0]["ph"] == 7.1

    def test_recording_resumes_when_the_probe_starts_reading(self):
        dead = FakeSensor(fail=True)
        worker, db, _ = self._worker(
            {
                "temperature": None,
                "ph": dead,
                "tds": None,
                "turbidity": None,
                "orp": None,
                "chlorine": None,
                "multi485": None,
            }
        )
        worker.step()
        assert db.readings == []

        dead.fail = False
        dead.value = 6.8
        worker.step()
        assert len(db.readings) == 1
        assert db.readings[0]["ph"] == 6.8
