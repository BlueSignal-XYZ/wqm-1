"""Tests for src/utils/config.py — layered ConfigManager, schema validation,
remote overlay, atomic JSON writer."""

import json

import pytest


def make_manager(tmp_path, base_yaml=None, remote_yaml=None):
    from utils.config import ConfigManager

    base = tmp_path / "config.yaml"
    if base_yaml is not None:
        base.write_text(base_yaml)
    remote = tmp_path / "config.d" / "remote.yaml"
    if remote_yaml is not None:
        remote.parent.mkdir(parents=True, exist_ok=True)
        remote.write_text(remote_yaml)
    return ConfigManager(str(base), str(remote))


class TestConfigManagerBase:
    def test_returns_defaults_when_file_missing(self, tmp_path, mock_hardware):
        mgr = make_manager(tmp_path)
        assert mgr.settings.sensor_read_s == 60
        assert mgr.settings.lora_tx_s == 300
        assert mgr.settings.orp_enabled is False
        assert mgr.remote_version is None

    def test_overrides_from_yaml(self, tmp_path, mock_hardware):
        mgr = make_manager(
            tmp_path,
            "sensor_read_s: 120\n"
            "lora_tx_s: 600\n"
            "orp_enabled: true\n"
            "app_key: ffffffffffffffffffffffffffffffff\n",
        )
        s = mgr.settings
        assert s.sensor_read_s == 120
        assert s.lora_tx_s == 600
        assert s.orp_enabled is True

    def test_ignores_unknown_keys(self, tmp_path, mock_hardware):
        mgr = make_manager(tmp_path, "sensor_read_s: 42\nnot_a_real_field: hello\n")
        assert mgr.settings.sensor_read_s == 42
        assert not hasattr(mgr.settings, "not_a_real_field")

    def test_rejects_out_of_range_local_values(self, tmp_path, mock_hardware):
        # A locally mis-edited value must not take the loop to a broken cadence.
        mgr = make_manager(tmp_path, "sensor_read_s: 1\n")
        assert mgr.settings.sensor_read_s == 60

    def test_rejects_wrong_types(self, tmp_path, mock_hardware):
        mgr = make_manager(tmp_path, "sensor_read_s: 'sixty'\norp_enabled: 3\n")
        assert mgr.settings.sensor_read_s == 60
        assert mgr.settings.orp_enabled is False

    def test_handles_invalid_yaml(self, tmp_path, mock_hardware):
        mgr = make_manager(tmp_path, "not: valid: yaml: here: [\n")
        assert mgr.settings.sensor_read_s == 60

    def test_handles_empty_yaml(self, tmp_path, mock_hardware):
        mgr = make_manager(tmp_path, "")
        assert mgr.settings.sensor_read_s == 60

    def test_rules_pass_through(self, tmp_path, mock_hardware):
        mgr = make_manager(
            tmp_path,
            "rules:\n  - sensor: ph\n    below: 6.5\n    relay: 1\n    action: 'on'\n",
        )
        assert mgr.settings.rules == [{"sensor": "ph", "below": 6.5, "relay": 1, "action": "on"}]


class TestRemoteOverlay:
    def test_remote_overlay_wins_over_base(self, tmp_path, mock_hardware):
        mgr = make_manager(
            tmp_path,
            base_yaml="sensor_read_s: 120\n",
            remote_yaml="version: 3\nvalues:\n  sensor_read_s: 30\n",
        )
        assert mgr.settings.sensor_read_s == 30
        assert mgr.remote_version == 3

    def test_remote_overlay_cannot_set_credentials(self, tmp_path, mock_hardware):
        mgr = make_manager(
            tmp_path,
            remote_yaml=(
                "version: 1\nvalues:\n"
                "  api_key: attacker\n"
                "  cloud_ingest_url: https://evil.example/ingest\n"
                "  sensor_read_s: 30\n"
            ),
        )
        # Valid keys apply, credential/URL keys are refused.
        assert mgr.settings.sensor_read_s == 30
        assert mgr.settings.api_key == ""
        assert "evil.example" not in mgr.settings.cloud_ingest_url

    def test_apply_remote_config_persists_and_reloads(self, tmp_path, mock_hardware):
        mgr = make_manager(tmp_path)
        ok, errors = mgr.apply_remote_config(5, {"heartbeat_s": 1200, "batch_size": 25})
        assert ok and not errors
        assert mgr.settings.heartbeat_s == 1200
        assert mgr.settings.batch_size == 25
        assert mgr.remote_version == 5
        # Survives a full reload (i.e. a reboot).
        mgr.reload()
        assert mgr.settings.heartbeat_s == 1200
        assert mgr.remote_version == 5

    def test_apply_remote_config_rejects_bad_values_keeping_last_known_good(
        self, tmp_path, mock_hardware
    ):
        mgr = make_manager(tmp_path)
        assert mgr.apply_remote_config(1, {"sensor_read_s": 45})[0]
        ok, errors = mgr.apply_remote_config(2, {"sensor_read_s": 999999})
        assert not ok
        assert errors
        # Previous overlay still in force — bad config can't brick the device.
        assert mgr.settings.sensor_read_s == 45
        assert mgr.remote_version == 1

    def test_apply_remote_config_rejects_unknown_keys(self, tmp_path, mock_hardware):
        mgr = make_manager(tmp_path)
        ok, errors = mgr.apply_remote_config(1, {"rm_rf_slash": True})
        assert not ok
        assert any("unknown key" in e for e in errors)


class TestValidateValues:
    def test_hot_and_restart_classification(self, mock_hardware):
        from utils.config import hot_keys, restart_keys

        values = {"sensor_read_s": 30, "gps_baud": 38400, "heartbeat_s": 900}
        assert hot_keys(values) == {"sensor_read_s", "heartbeat_s"}
        assert restart_keys(values) == {"gps_baud"}

    def test_bool_not_accepted_as_int(self, mock_hardware):
        from utils.config import validate_values

        accepted, errors = validate_values({"sensor_read_s": True})
        assert not accepted
        assert errors

    def test_int_widens_to_float(self, mock_hardware):
        from utils.config import validate_values

        accepted, errors = validate_values({"fan_on_temp_c": 65})
        assert accepted == {"fan_on_temp_c": 65.0}
        assert not errors


class TestFirmwareVersion:
    def test_version_read_from_version_file(self, mock_hardware):
        from pathlib import Path

        from utils.config import FIRMWARE_VERSION

        on_disk = (Path(__file__).parent.parent / "VERSION").read_text().strip()
        assert on_disk == FIRMWARE_VERSION


class TestAtomicJsonWrite:
    def test_writes_json_file(self, tmp_path, mock_hardware):
        from utils.config import atomic_json_write

        target = tmp_path / "out.json"
        atomic_json_write(str(target), {"key": "value", "num": 42})
        assert target.exists()
        data = json.loads(target.read_text())
        assert data == {"key": "value", "num": 42}

    def test_creates_parent_directory(self, tmp_path, mock_hardware):
        from utils.config import atomic_json_write

        target = tmp_path / "nested" / "deep" / "out.json"
        atomic_json_write(str(target), {"x": 1})
        assert target.exists()

    def test_overwrites_existing(self, tmp_path, mock_hardware):
        from utils.config import atomic_json_write

        target = tmp_path / "out.json"
        target.write_text('{"old": "data"}')
        atomic_json_write(str(target), {"new": "data"})
        assert json.loads(target.read_text()) == {"new": "data"}

    def test_no_tmp_left_on_success(self, tmp_path, mock_hardware):
        from utils.config import atomic_json_write

        target = tmp_path / "out.json"
        atomic_json_write(str(target), {"a": 1})
        tmp = target.with_suffix(".tmp")
        assert not tmp.exists()

    def test_handles_datetime_via_default_str(self, tmp_path, mock_hardware):
        """atomic_json_write uses default=str so non-JSON types serialise via __str__."""
        from datetime import datetime

        from utils.config import atomic_json_write

        target = tmp_path / "out.json"
        atomic_json_write(str(target), {"ts": datetime(2026, 4, 14, 12, 0, 0)})
        data = json.loads(target.read_text())
        assert "2026-04-14" in data["ts"]

    def test_raises_on_write_failure(self, tmp_path, mock_hardware, monkeypatch):
        from utils.config import atomic_json_write

        def broken_open(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr("builtins.open", broken_open)
        with pytest.raises(OSError):
            atomic_json_write(str(tmp_path / "out.json"), {"x": 1})


class TestGetSettingsCaching:
    def test_get_settings_returns_cached_instance(self, tmp_path, mock_hardware, monkeypatch):
        import utils.config as cfg

        # Reset cache
        monkeypatch.setattr(cfg, "_manager", None)

        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("sensor_read_s: 999\n")

        first = cfg.get_settings(str(yaml_path))
        assert first.sensor_read_s == 999

        # Second call (without path) returns the cached instance
        second = cfg.get_settings()
        assert second is first


class TestHardwareConstants:
    def test_adc_channel_assignments_match_schematic(self, mock_hardware):
        """Ensure ADC channels match the BST.ADC.SchDoc schematic assignments."""
        from utils import config

        assert config.ADC_CH_TDS == 0
        assert config.ADC_CH_TURBIDITY == 1
        assert config.ADC_CH_PH == 2
        assert config.ADC_CH_ORP == 3

    def test_lora_frequency_is_us915(self, mock_hardware):
        from utils import config

        assert config.LORA_FREQUENCY == 915_000_000

    def test_tds_divider_ratio_matches_resistors(self, mock_hardware):
        from utils import config

        # R57=2.2kΩ, R58=1kΩ: ratio = 1k/(1k+2.2k)
        expected = 1000.0 / (2200.0 + 1000.0)
        assert abs(config.TDS_DIVIDER_RATIO - expected) < 1e-9
