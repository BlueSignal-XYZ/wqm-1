"""Tests for the config editor utility."""

import pytest
import yaml

from service_window.config_editor import read_config, update_config


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"app_key": "AABBCCDD" * 4, "sensor_read_s": 60}))
    return str(path)


class TestReadConfig:
    def test_read_existing(self, config_path):
        cfg = read_config(config_path)
        assert cfg["sensor_read_s"] == 60

    def test_read_missing_returns_empty(self, tmp_path):
        cfg = read_config(str(tmp_path / "nonexistent.yaml"))
        assert cfg == {}


class TestUpdateConfig:
    def test_merge_update(self, config_path):
        update_config(config_path, {"sensor_read_s": 30})
        cfg = read_config(config_path)
        assert cfg["sensor_read_s"] == 30
        # Original key preserved
        assert cfg["app_key"] == "AABBCCDD" * 4

    def test_add_new_key(self, config_path):
        update_config(config_path, {"new_field": "hello"})
        cfg = read_config(config_path)
        assert cfg["new_field"] == "hello"

    def test_atomic_write_creates_parents(self, tmp_path):
        path = str(tmp_path / "sub" / "dir" / "config.yaml")
        update_config(path, {"key": "val"})
        cfg = read_config(path)
        assert cfg["key"] == "val"
