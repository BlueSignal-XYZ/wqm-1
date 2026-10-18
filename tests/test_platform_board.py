"""
Board-profile detection tests (src/platform_support/board.py).

Detection resolves which host the firmware runs on and whether Linux can
reach the headers directly. The load-bearing invariant: anything ambiguous
falls back to the Raspberry Pi profile, because an OTA update must never
demote a working analog field unit to digital-only.
"""

import pytest

from platform_support import PROFILES, BoardProfile, detect_board


def _model_file(tmp_path, text: str, raw: bytes | None = None) -> str:
    p = tmp_path / "model"
    if raw is not None:
        p.write_bytes(raw)
    else:
        p.write_text(text)
    return str(p)


class TestProfiles:
    def test_reference_profile_has_direct_headers(self):
        assert PROFILES["rpi-zero-2w"].has_direct_headers is True

    def test_arduino_q_family_is_headerless(self):
        assert PROFILES["arduino-uno-q"].has_direct_headers is False
        assert PROFILES["arduino-ventuno-q"].has_direct_headers is False
        assert PROFILES["generic-linux"].has_direct_headers is False

    def test_profiles_are_frozen(self):
        with pytest.raises(AttributeError):
            PROFILES["rpi-zero-2w"].has_direct_headers = False  # type: ignore[misc]

    def test_ids_are_self_consistent(self):
        for key, profile in PROFILES.items():
            assert profile.id == key
            assert isinstance(profile, BoardProfile)


class TestModelDetection:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("Raspberry Pi Zero 2 W Rev 1.0", "rpi-zero-2w"),
            ("Raspberry Pi Zero W Rev 1.1", "rpi-zero-w"),
            ("Raspberry Pi Zero WH Rev 1.1", "rpi-zero-w"),
            ("Raspberry Pi 3 Model B Rev 1.2", "rpi-3"),
            ("Raspberry Pi 3 Model B Plus Rev 1.3", "rpi-3"),
            ("Raspberry Pi 3 Model A Plus Rev 1.0", "rpi-3"),
            ("Raspberry Pi 4 Model B Rev 1.5", "rpi-4"),
            ("Raspberry Pi 400 Rev 1.0", "rpi-4"),
            ("Raspberry Pi 5 Model B Rev 1.0", "rpi-5"),
            ("Raspberry Pi 500 Rev 1.0", "rpi-5"),
            # A Pi we do not name keeps the Zero 2 W profile, on purpose.
            ("Raspberry Pi 2 Model B Rev 1.1", "rpi-zero-2w"),
            ("Raspberry Pi", "rpi-zero-2w"),
            ("Arduino UNO Q", "arduino-uno-q"),
            ("Qualcomm Technologies, Inc. QRB2210 RB1", "arduino-uno-q"),
            ("Arduino VENTUNO Q", "arduino-ventuno-q"),
            ("Qualcomm Dragonwing IQ-8275 EVK", "arduino-ventuno-q"),
            # Unknown Qualcomm boards land on the UNO Q profile (headerless).
            ("Qualcomm Technologies, Inc. Mystery Board", "arduino-uno-q"),
        ],
    )
    def test_model_string_matrix(self, tmp_path, model, expected):
        path = _model_file(tmp_path, model)
        assert detect_board(model_path=path).id == expected

    def test_device_tree_nul_terminator_stripped(self, tmp_path):
        # /proc/device-tree/model is NUL-terminated on real hardware.
        path = _model_file(tmp_path, "", raw=b"Arduino UNO Q\x00")
        assert detect_board(model_path=path).id == "arduino-uno-q"

    def test_ventuno_wins_over_generic_qualcomm_match(self, tmp_path):
        path = _model_file(tmp_path, "Qualcomm SoC — Arduino VENTUNO Q rev A")
        assert detect_board(model_path=path).id == "arduino-ventuno-q"

    def test_case_insensitive(self, tmp_path):
        path = _model_file(tmp_path, "RASPBERRY PI ZERO 2 W")
        assert detect_board(model_path=path).id == "rpi-zero-2w"


class TestFallbackSafety:
    """Ambiguity must resolve to the Pi profile — never demote a field unit."""

    def test_missing_model_file_falls_back_to_rpi(self, tmp_path):
        missing = str(tmp_path / "does-not-exist")
        cpu = str(tmp_path / "no-cpuinfo")
        assert detect_board(model_path=missing, cpuinfo_path=cpu).id == "rpi-zero-2w"

    def test_unrecognized_model_falls_back_to_rpi(self, tmp_path):
        path = _model_file(tmp_path, "Frobnicator 9000")
        assert detect_board(model_path=path).id == "rpi-zero-2w"

    def test_empty_model_falls_back_to_rpi(self, tmp_path):
        path = _model_file(tmp_path, "")
        assert detect_board(model_path=path).id == "rpi-zero-2w"


class TestOverride:
    def test_explicit_override_pins_profile(self, tmp_path):
        # Even on a "Pi" model string, an explicit id wins.
        path = _model_file(tmp_path, "Raspberry Pi Zero 2 W")
        assert detect_board(override="arduino-uno-q", model_path=path).id == "arduino-uno-q"

    def test_unknown_override_falls_back_to_auto(self, tmp_path):
        path = _model_file(tmp_path, "Arduino UNO Q")
        assert detect_board(override="nonsense-board", model_path=path).id == "arduino-uno-q"

    def test_auto_is_not_treated_as_profile_id(self, tmp_path):
        path = _model_file(tmp_path, "Arduino VENTUNO Q")
        assert detect_board(override="auto", model_path=path).id == "arduino-ventuno-q"


def _cpuinfo(tmp_path, text: str) -> str:
    p = tmp_path / "cpuinfo"
    p.write_text(text)
    return str(p)


_ZERO_W_CPUINFO = """\
processor\t: 0
model name\t: ARMv6-compatible processor rev 7 (v6l)
model\t: 7
Hardware\t: BCM2835
Revision\t: 9000c1
Serial\t\t: 00000000aabbccdd
Model\t\t: Raspberry Pi Zero W Rev 1.1
"""


class TestCpuinfo:
    def test_model_line_when_device_tree_is_missing(self, tmp_path):
        missing = str(tmp_path / "no-model")
        cpu = _cpuinfo(tmp_path, _ZERO_W_CPUINFO)
        assert detect_board(model_path=missing, cpuinfo_path=cpu).id == "rpi-zero-w"

    def test_revision_when_the_model_line_is_absent(self, tmp_path):
        missing = str(tmp_path / "no-model")
        # New-style codes: type field is bits 4..11. 902120 → Zero 2 W,
        # a02082 → 3B, a020d3 → 3B+, c03114 → 4B, c04170 → 5.
        cases = {
            "902120": "rpi-zero-2w",
            "a02082": "rpi-3",
            "a020d3": "rpi-3",
            "c03114": "rpi-4",
            "0xc04170": "rpi-5",
        }
        for revision, expected in cases.items():
            cpu = _cpuinfo(
                tmp_path,
                f"processor\t: 0\nmodel\t: 7\nHardware\t: BCM2835\nRevision\t: {revision}\n",
            )
            assert detect_board(model_path=missing, cpuinfo_path=cpu).id == expected

    def test_garbage_revision_is_ignored(self, tmp_path):
        missing = str(tmp_path / "no-model")
        cpu = _cpuinfo(tmp_path, "Revision\t: not-hex\nmodel\t: 173\n")
        assert detect_board(model_path=missing, cpuinfo_path=cpu).id == "rpi-zero-2w"

    def test_old_style_revision_is_not_a_model(self, tmp_path):
        missing = str(tmp_path / "no-model")
        cpu = _cpuinfo(tmp_path, "Revision\t: 000e\nmodel\t: 7\n")
        assert detect_board(model_path=missing, cpuinfo_path=cpu).id == "rpi-zero-2w"

    def test_cpuid_model_number_is_not_a_board(self, tmp_path):
        missing = str(tmp_path / "no-model")
        cpu = _cpuinfo(tmp_path, "model\t\t: 173\nmodel name\t: Intel(R) Xeon(R) Processor\n")
        assert detect_board(model_path=missing, cpuinfo_path=cpu).id == "rpi-zero-2w"

    def test_device_tree_wins_over_cpuinfo(self, tmp_path):
        path = _model_file(tmp_path, "Raspberry Pi 4 Model B Rev 1.5")
        cpu = _cpuinfo(tmp_path, _ZERO_W_CPUINFO)
        assert detect_board(model_path=path, cpuinfo_path=cpu).id == "rpi-4"

    def test_model_line_wins_over_a_disagreeing_revision(self, tmp_path):
        missing = str(tmp_path / "no-model")
        cpu = _cpuinfo(
            tmp_path,
            "Revision\t: 9000c1\nModel\t\t: Raspberry Pi Zero 2 W Rev 1.0\n",
        )
        assert detect_board(model_path=missing, cpuinfo_path=cpu).id == "rpi-zero-2w"

    def test_empty_device_tree_does_not_consult_cpuinfo(self, tmp_path):
        path = _model_file(tmp_path, "")
        cpu = _cpuinfo(tmp_path, _ZERO_W_CPUINFO)
        assert detect_board(model_path=path, cpuinfo_path=cpu).id == "rpi-zero-2w"

    def test_unrecognized_device_tree_does_not_consult_cpuinfo(self, tmp_path):
        path = _model_file(tmp_path, "Frobnicator 9000")
        cpu = _cpuinfo(tmp_path, _ZERO_W_CPUINFO)
        assert detect_board(model_path=path, cpuinfo_path=cpu).id == "rpi-zero-2w"


class TestPiHeaderPath:
    """Supported Pis share the Zero 2 W header behaviour. Pi 5 does not."""

    def test_zero_2w_profile_is_unchanged(self):
        profile = PROFILES["rpi-zero-2w"]
        assert profile.name == "Raspberry Pi Zero 2 W"
        assert profile.has_direct_headers is True
        assert profile.gpio_backend == "rpi"
        assert profile.supported is True
        assert profile.family == "raspberry-pi"

    @pytest.mark.parametrize("profile_id", ["rpi-zero-w", "rpi-zero-2w", "rpi-3", "rpi-4"])
    def test_supported_pis_share_the_header_map(self, profile_id):
        from platform_support.hostpins import pins_for_board

        profile = PROFILES[profile_id]
        assert profile.supported is True
        assert profile.has_direct_headers is True
        assert profile.gpio_backend == "rpi"
        pins = pins_for_board(profile_id)
        ref = pins_for_board("rpi-zero-2w")
        assert pins.backend == "rpi"
        assert pins.i2c_bus == ref.i2c_bus == 1
        assert pins.spi_bus == ref.spi_bus == 0
        assert pins.spi_device == ref.spi_device == 0
        assert pins.gps_port == ref.gps_port == "/dev/serial0"
        assert pins.w1_pin == ref.w1_pin == "GPIO4"
        assert pins.lines == ref.lines
        assert pins.board_id == profile_id

    def test_zero_2w_model_is_not_classified_as_zero_w(self, tmp_path):
        path = _model_file(tmp_path, "Raspberry Pi Zero 2 W Rev 1.0")
        assert detect_board(model_path=path).id == "rpi-zero-2w"

    def test_pi_5_is_named_and_not_given_the_header(self, tmp_path):
        from platform_support.hostpins import pins_for_board

        path = _model_file(tmp_path, "Raspberry Pi 5 Model B Rev 1.1")
        profile = detect_board(model_path=path)
        assert profile.id == "rpi-5"
        assert profile.supported is False
        assert profile.has_direct_headers is False
        assert profile.gpio_backend == "none"
        pins = pins_for_board("rpi-5")
        assert pins.backend == "none"
        assert pins.lines == {}

    def test_nul_terminated_pi_4_model(self, tmp_path):
        path = _model_file(tmp_path, "", raw=b"Raspberry Pi 4 Model B Rev 1.2\x00")
        assert detect_board(model_path=path).id == "rpi-4"
