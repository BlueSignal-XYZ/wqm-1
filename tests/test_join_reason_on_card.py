"""The card says why a join failed, in NetworkManager's own words.

Jacques, 2026-11-17: the card read ``join_1: … SpectrumSetup-9206
no_association`` and nothing else, so nobody could tell a router that never
answered from one that refused the radio. A failed join now keeps one short
line of NetworkManager's text after the reason, with the typed password
removed and a line that looks like a secret dropped.
"""

from __future__ import annotations

from utils.netctl import _run as _REAL_RUN  # before conftest blocks the shell

TIMEOUT_UP = "Error: Connection activation failed: (7) Timeout was reached."


class TestTheCardSaysWhyAJoinFailed:
    def test_a_failure_keeps_networkmanagers_words(self, tmp_path, monkeypatch):
        from diagnostics.boot_status import record_join_attempt

        status = tmp_path / "boot" / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        record_join_attempt("SpectrumSetup-9206", "no_association", detail=TIMEOUT_UP)
        line = next(ln for ln in status.read_text().splitlines() if ln.startswith("join_1:"))
        assert line.endswith(f"SpectrumSetup-9206 no_association - {TIMEOUT_UP}")

    def test_the_words_are_cut_to_one_short_line(self, tmp_path, monkeypatch):
        from diagnostics.boot_status import JOIN_DETAIL_MAX, record_join_attempt

        status = tmp_path / "boot" / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        record_join_attempt("PondHouse", "no_association", detail="Error:\n" + "x" * 300)
        line = next(ln for ln in status.read_text().splitlines() if ln.startswith("join_1:"))
        said = line.split(" no_association - ", 1)[1]
        assert len(said) == JOIN_DETAIL_MAX
        assert said.endswith("...")
        assert "\n" not in said

    def test_the_typed_password_never_reaches_the_card(self, tmp_path, monkeypatch):
        from diagnostics.boot_status import record_join_attempt

        status = tmp_path / "boot" / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        record_join_attempt(
            "PondHouse", "wrong_password", secret="hunter22", detail="rejected hunter22 at step 2"
        )
        text = status.read_text()
        assert "hunter22" not in text

    def test_words_that_look_like_a_secret_are_dropped(self):
        from diagnostics.boot_status import join_detail

        assert join_detail("802-11-wireless-security.psk=abc required") == ""
        assert join_detail("wifi-sec.key-mgmt missing") == ""

    def test_a_join_that_worked_records_no_words(self, tmp_path, monkeypatch):
        from diagnostics.boot_status import record_join_attempt

        status = tmp_path / "boot" / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        record_join_attempt("PondHouse", "joined", detail="Connection successfully activated")
        line = next(ln for ln in status.read_text().splitlines() if ln.startswith("join_1:"))
        assert line.endswith("PondHouse joined")

    def test_the_setup_join_passes_the_words_through(self, monkeypatch):
        from utils import join, netctl

        recorded = []
        monkeypatch.setattr(
            join,
            "_record_attempt",
            lambda ssid, reason, password, detail=None: recorded.append((reason, detail)),
        )
        monkeypatch.setattr(
            netctl,
            "join_network",
            lambda *a, **k: {
                "ok": False,
                "connected": False,
                "ap_restored": True,
                "error": TIMEOUT_UP,
                "message": "x",
                "reason": "no_association",
            },
        )
        started = join.start_join("PondHouse", "hunter22", ap_ssid="BlueSignal Setup")
        assert started["state"] == "joining"
        assert join.wait_for_follow_up(timeout_s=5)
        assert recorded == [("no_association", TIMEOUT_UP)]

    def test_a_timed_out_nmcli_never_reports_its_argv(self, monkeypatch):
        """str(TimeoutExpired) carries the whole argv; for `connection add`
        that is the password."""
        import subprocess

        from utils import netctl

        def hang(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd=["nmcli", "wifi-sec.psk", "hunter22"], timeout=25)

        monkeypatch.setattr(netctl.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(netctl.subprocess, "run", hang)
        rc, out, err = _REAL_RUN(["nmcli", "connection", "add", "wifi-sec.psk", "hunter22"], 25)
        assert rc == 124
        assert err == "nmcli connection add timed out after 25 s"
        assert "hunter22" not in out + err
