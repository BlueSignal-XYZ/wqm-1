"""Tests for src/utils/config.py — settings loader and atomic JSON writer."""

import json

import pytest


class TestLoadSettings:
    def test_returns_defaults_when_file_missing(self, tmp_path, mock_hardware):
        from utils.config import _load_settings

        s = _load_settings(str(tmp_path / "nonexistent.yaml"))
        # defaults
        assert s.sensor_read_s == 60
        assert s.lora_tx_s == 300
        assert s.orp_enabled is False

    def test_overrides_from_yaml(self, tmp_path, mock_hardware):
        from utils.config import _load_settings

        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            "sensor_read_s: 120\n"
            "lora_tx_s: 600\n"
            "orp_enabled: true\n"
            "app_key: ffffffffffffffffffffffffffffffff\n"
        )
        s = _load_settings(str(yaml_path))
        assert s.sensor_read_s == 120
        assert s.lora_tx_s == 600
        assert s.orp_enabled is True

    def test_ignores_unknown_keys(self, tmp_path, mock_hardware):
        from utils.config import _load_settings

        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("sensor_read_s: 42\nnot_a_real_field: hello\n")
        s = _load_settings(str(yaml_path))
        assert s.sensor_read_s == 42
        assert not hasattr(s, "not_a_real_field")

    def test_handles_invalid_yaml(self, tmp_path, mock_hardware):
        from utils.config import _load_settings

        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("not: valid: yaml: here: [\n")
        # should return defaults rather than raise
        s = _load_settings(str(yaml_path))
        assert s.sensor_read_s == 60

    def test_handles_empty_yaml(self, tmp_path, mock_hardware):
        from utils.config import _load_settings

        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("")
        s = _load_settings(str(yaml_path))
        assert s.sensor_read_s == 60


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
        monkeypatch.setattr(cfg, "_settings", None)

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
