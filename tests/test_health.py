"""Tests for src/utils/health.py — health telemetry + heartbeat builder."""


class TestHealthReporter:
    def test_initial_report(self, mock_hardware):
        from utils.health import HealthReporter

        hr = HealthReporter("2.0.0")
        report = hr.get_report()
        assert report["firmwareVersion"] == "2.0.0"
        # Honest nulls: no RSSI yet must not fake a value. Battery was removed
        # 2026-08-20 — nothing measured it and the percentage was invented.
        assert "batteryLevel" not in report
        assert report["signalStrength"] is None
        assert report["lastSeen"] == 0

    def test_rssi_update(self, mock_hardware):
        from utils.health import HealthReporter

        hr = HealthReporter("2.0.0")
        hr.update_rssi(-67)
        assert hr.get_signal_strength() == -67

    def test_last_seen_update(self, mock_hardware):
        from utils.health import HealthReporter

        hr = HealthReporter("2.0.0")
        hr.update_last_seen()
        report = hr.get_report()
        assert report["lastSeen"] > 0


class TestHeartbeatBuilder:
    def test_uptime_uses_injected_clock(self, mock_hardware):
        from utils.health import HealthReporter

        t = [1000.0]
        hr = HealthReporter("2.0.0", clock=lambda: t[0])
        t[0] = 1360.0
        hb = hr.build_heartbeat()
        assert hb["uptimeS"] == 360
        assert hb["firmwareVersion"] == "2.0.0"

    def test_optional_fields_omitted_when_unknown(self, mock_hardware):
        from utils.health import HealthReporter

        hr = HealthReporter("2.0.0")
        hb = hr.build_heartbeat()
        assert "batteryLevel" not in hb
        assert "loraRssi" not in hb
        assert "errorCounts" not in hb
        assert "sensorHealth" not in hb

    def test_buffer_depth_from_db(self, mock_hardware):
        from utils.health import HealthReporter

        class FakeDB:
            def get_state_count(self, state):
                return {"pending": 12, "failed_permanent": 3}[state]

        hr = HealthReporter("2.0.0")
        hb = hr.build_heartbeat(db=FakeDB())
        assert hb["bufferDepth"] == 12
        assert hb["bufferFailedRows"] == 3

    def test_db_errors_do_not_break_heartbeat(self, mock_hardware):
        from utils.health import HealthReporter

        class BrokenDB:
            def get_state_count(self, state):
                raise RuntimeError("db locked")

        hr = HealthReporter("2.0.0")
        hb = hr.build_heartbeat(db=BrokenDB())
        assert "bufferDepth" not in hb
        assert hb["firmwareVersion"] == "2.0.0"

    def test_context_fields_pass_through(self, mock_hardware):
        from utils.health import HealthReporter

        hr = HealthReporter("2.0.0")
        hr.update_rssi(-72)
        hb = hr.build_heartbeat(
            error_counts={"cloud": 2},
            sensor_health={"ph": {"status": "ok"}},
            config_version=4,
            ota_phase="idle",
        )
        assert hb["loraRssi"] == -72
        assert hb["errorCounts"] == {"cloud": 2}
        assert hb["sensorHealth"] == {"ph": {"status": "ok"}}
        assert hb["configVersion"] == 4
        assert hb["otaPhase"] == "idle"
