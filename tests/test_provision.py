"""Tests for the provisioning tool."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _setup_path():
    """Ensure scripts/ is importable."""
    scripts_dir = str(Path(__file__).parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


class TestGetIdentity:
    def test_identity_has_required_fields(self):
        from provision import _get_identity

        identity = _get_identity()
        assert "device_id" in identity
        assert "dev_eui" in identity
        assert "ble_name" in identity

    def test_device_id_format(self):
        from provision import _get_identity

        identity = _get_identity()
        assert identity["device_id"].startswith("BS-WQM1-")


class TestAppKeyValidation:
    def test_valid_hex32(self):
        from provision import _HEX32_RE

        assert _HEX32_RE.match("A" * 32)
        assert _HEX32_RE.match("0123456789abcdef" * 2)

    def test_invalid_keys(self):
        from provision import _HEX32_RE

        assert not _HEX32_RE.match("short")
        assert not _HEX32_RE.match("G" * 32)  # not hex
        assert not _HEX32_RE.match("")


class TestQRGeneration:
    def test_generate_qr(self, tmp_path):
        from provision import _generate_qr

        output = str(tmp_path / "test.svg")
        identity = {"device_id": "BS-WQM1-test", "dev_eui": "0018B200AABBCCDD"}
        result = _generate_qr(identity, output)
        assert result is True
        assert Path(output).exists()
        assert Path(output).stat().st_size > 0


class TestConfigReadWrite:
    def test_write_and_read_config(self, tmp_path):
        import yaml
        from provision import _read_config, _write_config

        # Patch the config path
        config_path = str(tmp_path / "config.yaml")
        with patch("provision._CONFIG_PATH", config_path):
            # Write initial config
            Path(config_path).write_text(yaml.safe_dump({"app_key": "0" * 32}))
            # Update
            _write_config({"app_key": "A" * 32})
            cfg = _read_config()
            assert cfg["app_key"] == "A" * 32
