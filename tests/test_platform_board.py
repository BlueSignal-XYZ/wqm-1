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
            ("Raspberry Pi 4 Model B Rev 1.5", "rpi-zero-2w"),
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
        assert detect_board(model_path=missing).id == "rpi-zero-2w"

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
