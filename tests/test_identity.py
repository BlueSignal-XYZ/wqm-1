"""Tests for firmware/src/utils/identity.py — device identity generation."""

from unittest.mock import mock_open, patch


class TestPiSerial:
    def test_parse_serial_from_cpuinfo(self, mock_hardware):
        cpuinfo = "processor\t: 0\nmodel name\t: ARMv7\nSerial\t\t: 10000000abcdef01\n"
        with patch("builtins.open", mock_open(read_data=cpuinfo)):
            from utils.identity import get_pi_serial

            serial = get_pi_serial()
            assert serial == "10000000abcdef01"

    def test_fallback_on_missing_file(self, mock_hardware):
        with patch("builtins.open", side_effect=FileNotFoundError):
            from utils.identity import get_pi_serial

            serial = get_pi_serial()
            assert serial == "0000000000000000"


class TestDeviceId:
    def test_format(self, mock_hardware):
        from utils.identity import get_device_id

        device_id = get_device_id("10000000abcdef01")
        assert device_id == "BS-WQM1-0000abcdef01"

    def test_uses_last_12_chars(self, mock_hardware):
        from utils.identity import get_device_id

        device_id = get_device_id("1234567890abcdef")
        assert device_id == "BS-WQM1-567890abcdef"


class TestDevEUI:
    def test_format(self, mock_hardware):
        from utils.identity import get_dev_eui

        eui = get_dev_eui("10000000abcdef01")
        assert eui == bytes.fromhex("0018B200abcdef01")
        assert len(eui) == 8

    def test_oui_prefix(self, mock_hardware):
        from utils.identity import get_dev_eui

        eui = get_dev_eui("0000000012345678")
        assert eui[:4] == bytes.fromhex("0018B200")


class TestAppEUI:
    def test_constant(self, mock_hardware):
        from utils.identity import APP_EUI

        assert bytes.fromhex("0000000000000000") == APP_EUI
        assert len(APP_EUI) == 8


class TestBLEName:
    def test_format(self, mock_hardware):
        from utils.identity import get_ble_name

        name = get_ble_name("BS-WQM1-0000abcdef01")
        assert name == "BlueSignal-ef01"
        assert name.startswith("BlueSignal-")


# ---------------------------------------------------------------------------
# Provisioned identity (commissioning plan PR 1): the printed label is the
# identity, the DevEUI follows the board, and no file means no change.
# ---------------------------------------------------------------------------

import json  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def cpuinfo_serial(monkeypatch):
    """A Pi with a known serial, independent of the host running the tests."""
    from utils import identity

    monkeypatch.setattr(identity, "get_pi_serial", lambda: "10000000abcdef01")
    return "10000000abcdef01"


class TestProvisionedIdentity:
    def _write(self, tmp_path, monkeypatch, body):
        from utils.identity import IDENTITY_FILE_ENV

        f = tmp_path / "bluesignal-identity.json"
        f.write_text(body if isinstance(body, str) else json.dumps(body))
        monkeypatch.setenv(IDENTITY_FILE_ENV, str(f))
        return f

    def test_provisioned_file_wins_for_id_and_dev_eui(self, tmp_path, monkeypatch, cpuinfo_serial):
        from utils.identity import get_dev_eui, get_device_id, identity_source

        self._write(tmp_path, monkeypatch, {"serial": "WQM-10001", "dev_eui": "feffff0000000001"})
        assert get_device_id() == "WQM-10001"
        assert get_dev_eui().hex().upper() == "FEFFFF0000000001"
        assert identity_source() == "provisioned"

    def test_absent_file_falls_back_to_pi_derived_unchanged(
        self, tmp_path, monkeypatch, cpuinfo_serial
    ):
        from utils.identity import IDENTITY_FILE_ENV, get_dev_eui, get_device_id, identity_source

        monkeypatch.setenv(IDENTITY_FILE_ENV, str(tmp_path / "nope.json"))
        assert get_device_id() == "BS-WQM1-0000abcdef01"
        assert get_dev_eui() == bytes.fromhex("0018B200abcdef01")
        assert identity_source() == "derived"

    def test_serial_without_dev_eui_keeps_the_derived_dev_eui(
        self, tmp_path, monkeypatch, cpuinfo_serial
    ):
        from utils.identity import get_dev_eui, get_device_id

        self._write(tmp_path, monkeypatch, {"serial": "WQM-10002"})
        assert get_device_id() == "WQM-10002"
        assert get_dev_eui() == bytes.fromhex("0018B200abcdef01")

    @pytest.mark.parametrize(
        "body",
        [
            "{not json",
            "[]",
            {"dev_eui": "feffff0000000001"},  # no serial
            {"serial": "BS-WQM1-0000abcdef01"},  # a derived id is not a label
            {"serial": "WQM-1"},  # wrong digit count
            {"serial": "WQM-10001", "dev_eui": "zz"},  # bad DevEUI poisons the whole file
        ],
    )
    def test_malformed_file_falls_back_and_logs(
        self, tmp_path, monkeypatch, cpuinfo_serial, body, caplog
    ):
        import logging

        from utils.identity import get_dev_eui, get_device_id, read_provisioned_identity

        self._write(tmp_path, monkeypatch, body)
        with caplog.at_level(logging.WARNING, logger="wqm1.identity"):
            assert read_provisioned_identity() is None
        assert any("ignoring" in r.getMessage().lower() for r in caplog.records)
        assert get_device_id() == "BS-WQM1-0000abcdef01"
        assert get_dev_eui() == bytes.fromhex("0018B200abcdef01")

    def test_label_is_uppercased_and_the_file_is_passed_through(self, tmp_path, monkeypatch):
        from utils.identity import read_provisioned_identity

        self._write(
            tmp_path,
            monkeypatch,
            {
                "serial": " wqm-10009 ",
                "dev_eui": "feffff0000000009",
                "ap_passphrase": "rain-tank-42",
            },
        )
        prov = read_provisioned_identity()
        assert prov["serial"] == "WQM-10009"
        assert prov["dev_eui"] == "FEFFFF0000000009"
        assert prov["ap_passphrase"] == "rain-tank-42"

    def test_explicit_serial_argument_still_means_derived(self, tmp_path, monkeypatch):
        """Callers that pass a serial are computing a derived id on purpose."""
        from utils.identity import get_dev_eui, get_device_id

        self._write(tmp_path, monkeypatch, {"serial": "WQM-10001", "dev_eui": "feffff0000000001"})
        assert get_device_id("1234567890abcdef") == "BS-WQM1-567890abcdef"
        assert get_dev_eui("1234567890abcdef") == bytes.fromhex("0018B20090abcdef")

    def test_default_paths_are_the_boot_partition_both_layouts(self):
        from utils.identity import PROVISIONED_IDENTITY_PATHS

        assert PROVISIONED_IDENTITY_PATHS == (
            "/boot/firmware/bluesignal-identity.json",
            "/boot/bluesignal-identity.json",
        )


class TestHardwareIdentity:
    def test_reports_pi_serial_and_derived_id(self, tmp_path, monkeypatch, cpuinfo_serial):
        from utils.identity import IDENTITY_FILE_ENV, hardware_identity

        f = tmp_path / "id.json"
        f.write_text(json.dumps({"serial": "WQM-10001"}))
        monkeypatch.setenv(IDENTITY_FILE_ENV, str(f))
        hw = hardware_identity()
        assert hw == {
            "identitySource": "provisioned",
            "piSerial": "10000000abcdef01",
            "derivedId": "BS-WQM1-0000abcdef01",
        }

    def test_fallback_zeros_are_not_reported_as_a_serial(self, monkeypatch, tmp_path):
        from utils import identity

        monkeypatch.setattr(identity, "get_pi_serial", lambda: identity.FALLBACK_PI_SERIAL)
        monkeypatch.setenv(identity.IDENTITY_FILE_ENV, str(tmp_path / "none.json"))
        hw = identity.hardware_identity()
        assert "piSerial" not in hw
        assert hw["identitySource"] == "derived"

    def test_heartbeat_carries_it(self, monkeypatch, tmp_path, cpuinfo_serial, mock_hardware):
        from utils import identity
        from utils.health import HealthReporter

        monkeypatch.setenv(identity.IDENTITY_FILE_ENV, str(tmp_path / "none.json"))
        hb = HealthReporter("2.3.0").build_heartbeat()
        assert hb["piSerial"] == "10000000abcdef01"
        assert hb["derivedId"] == "BS-WQM1-0000abcdef01"
        assert hb["identitySource"] == "derived"


class TestApName:
    def test_label_gives_last_four_digits(self):
        from utils.identity import ap_name

        assert ap_name("WQM-10001") == "WQM1-0001"
        assert ap_name("BS-WQM1-0000a92e4e7d") == "WQM1-4E7D"
        assert ap_name("SIM-WQM1-00007") == "WQM1-0007"


class TestLabelRegexParity:
    def test_firmware_and_marketplace_agree_on_the_label_shape(self):
        """LABEL_RE here and WQM1_LABEL_RE in marketplace functions/v2/deviceSerial.js
        must be the same pattern. The marketplace file is read when the two repos
        sit side by side (the way every session checks them out); otherwise the
        firmware's own pattern is asserted alone."""
        from pathlib import Path

        from utils.identity import LABEL_RE

        assert LABEL_RE.pattern == r"^WQM-\d{5}$"
        mp = (
            Path(__file__).resolve().parents[2]
            / "marketplace"
            / "functions"
            / "v2"
            / "deviceSerial.js"
        )
        if mp.exists():
            src = mp.read_text()
            assert "WQM1_LABEL_RE = /^WQM-\\d{5}$/" in src
