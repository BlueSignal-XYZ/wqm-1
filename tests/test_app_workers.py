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


class TestGpsWorker:
    def test_fix_published_to_state(self, mock_hardware):
        from app.state import StateStore
        from app.workers import GpsWorker

        state = StateStore()
        fix = SimpleNamespace(latitude=37.5, longitude=-78.5, altitude=90.0, satellites=7)
        gps = SimpleNamespace(get_fix=lambda timeout_s: fix, power_cycle=lambda: None)
        worker = GpsWorker(make_settings(), gps, None, state)
        worker.step()
        assert state.gps().lat == 37.5
        assert state.gps().sats == 7

    def test_no_fix_and_no_previous_triggers_power_cycle(self, mock_hardware):
        from app.state import StateStore
        from app.workers import GpsWorker

        state = StateStore()
        cycles = []
        gps = SimpleNamespace(get_fix=lambda timeout_s: None, power_cycle=lambda: cycles.append(1))
        worker = GpsWorker(make_settings(), gps, None, state)
        worker.step()
        assert cycles == [1]
