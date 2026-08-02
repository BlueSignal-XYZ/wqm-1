"""Tests for src/diagnostics/explain.py — plain-language diagnostics copy."""

from diagnostics.explain import (
    SENSOR_STATES,
    SENSOR_SUBJECTS,
    SYSTEM_STATES,
    SYSTEM_SUBJECTS,
    all_states,
    display_name,
    explain,
)

VALID_STATUSES = {"ok", "attention", "fault", "disabled"}
EXPECTED_KEYS = {"status", "message", "likelyCause", "action"}


class TestGolden:
    def test_every_subject_state_pair_yields_copy(self):
        """The golden test: every combination gives a non-empty message and
        a valid status — nothing in the matrix can ship a blank panel."""
        pairs = all_states()
        assert pairs  # sanity
        for subject, state in pairs:
            info = explain(subject, state)
            assert set(info) == EXPECTED_KEYS, (subject, state)
            assert info["status"] in VALID_STATUSES, (subject, state)
            assert isinstance(info["message"], str) and info["message"].strip(), (subject, state)

    def test_all_states_covers_full_matrix(self):
        pairs = set(all_states())
        for subject in SENSOR_SUBJECTS:
            for state in SENSOR_STATES:
                assert (subject, state) in pairs
        for subject in SYSTEM_SUBJECTS:
            for state in SYSTEM_STATES:
                assert (subject, state) in pairs
        assert len(pairs) == len(SENSOR_SUBJECTS) * len(SENSOR_STATES) + len(SYSTEM_SUBJECTS) * len(
            SYSTEM_STATES
        )

    def test_non_ok_states_always_have_an_action(self):
        """A non-technical installer must always get a next step for any
        attention/fault condition."""
        for subject, state in all_states():
            info = explain(subject, state)
            # ok needs no action; disabled means "not installed" — also no action.
            if info["status"] not in ("ok", "disabled"):
                assert info["action"], (subject, state)


class TestSensorCopy:
    def test_stuck_ph_reads_like_the_spec(self):
        info = explain("ph", "stuck", {"minutes": 20})
        assert info["status"] == "fault"
        assert "pH probe" in info["message"]
        assert "flat for 20 minutes" in info["message"]
        assert "biofilm" in info["likelyCause"]
        assert "rinse" in info["action"]

    def test_stuck_no_data_mentions_no_data(self):
        info = explain("tds", "stuck_no_data", {"minutes": 20})
        assert info["status"] == "fault"
        assert "no data" in info["message"]
        assert "TDS probe" in info["message"]

    def test_missing_context_falls_back_gracefully(self):
        info = explain("ph", "stuck")  # no context at all
        assert "several minutes" in info["message"]

    def test_status_mapping(self):
        assert explain("ph", "ok")["status"] == "ok"
        assert explain("ph", "recovered")["status"] == "ok"
        assert explain("ph", "spike")["status"] == "attention"
        assert explain("ph", "calibration_overdue")["status"] == "attention"
        assert explain("ph", "calibration_drift")["status"] == "attention"
        assert explain("ph", "stuck")["status"] == "fault"
        assert explain("ph", "stuck_no_data")["status"] == "fault"

    def test_calibration_overdue_includes_age(self):
        info = explain("orp", "calibration_overdue", {"age_days": 104})
        assert "104 days" in info["message"]
        assert "ORP probe" in info["message"]

    def test_drift_action_says_recalibrate(self):
        info = explain("turbidity", "calibration_drift")
        assert "Recalibrate" in info["action"]
        assert "turbidity sensor" in info["message"]

    def test_display_names(self):
        assert display_name("ph") == "pH probe"
        assert display_name("temperature") == "temperature probe"
        assert display_name("orp") == "ORP probe"


class TestSystemCopy:
    def test_cloud_down_is_fault_with_reassurance(self):
        info = explain("cloud", "down")
        assert info["status"] == "fault"
        assert "saved on the device" in info["message"]
        assert info["action"]

    def test_degraded_is_attention(self):
        for subject in SYSTEM_SUBJECTS:
            assert explain(subject, "degraded")["status"] == "attention"

    def test_system_ok(self):
        assert explain("gps", "ok")["status"] == "ok"


class TestUnknowns:
    def test_unknown_sensor_never_raises(self):
        info = explain("flux_capacitor", "stuck")
        assert info["status"] in VALID_STATUSES
        assert info["message"]

    def test_unknown_state_never_raises(self):
        info = explain("ph", "quantum_flutter")
        assert info["status"] in VALID_STATUSES
        assert "quantum_flutter" in info["message"]

    def test_unknown_everything_never_raises(self):
        info = explain("", "")
        assert info["status"] in VALID_STATUSES
        assert info["message"]

    def test_unknown_state_keeps_known_status_semantics(self):
        # unknown subject with a known-fault state still maps to fault
        assert explain("mystery", "down")["status"] == "fault"
