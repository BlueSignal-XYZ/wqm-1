"""Bench burn-in and the boot self-check lines it depends on.

The ten-minute loop is driven with a fake clock. Nothing here opens a
radio, an I2C bus, or the boot partition of this machine.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from diagnostics.boot_status import (
    commission_verdict,
    parse_brcmfmac_firmware,
    parse_throttled,
)
from diagnostics.burnin import (
    RestartTracker,
    Sample,
    evaluate,
    parse_systemctl_show,
    run_burnin,
    service_is_restarting,
)

ROOT = Path(__file__).resolve().parent.parent


def test_brcmfmac_firmware_line_from_the_failing_card():
    text = (
        "brcmfmac: brcmf_c_preinit_dcmds: Firmware: BCM43430/1 wl0: "
        "Jul 19 2021 03:24:18 version 7.45.98 (TOB) (56df937 CY) FWID 01-8e14b897"
    )
    assert parse_brcmfmac_firmware(text) == "BCM43430/1 7.45.98 (TOB)"
    other = (
        "Firmware: BCM43430/1 wl0: Jun 14 2023 07:27:45 version 7.45.96.s1 "
        "(gf031a129) FWID 01-70bd2af7"
    )
    assert parse_brcmfmac_firmware(other) == "BCM43430/1 7.45.96.s1"


def test_throttled_bits():
    assert parse_throttled("throttled=0x0") == "ok"
    assert parse_throttled("throttled=0x50005") == (
        "now undervoltage,throttled since-boot undervoltage,throttled throttled=0x50005"
    )
    assert parse_throttled("nope") == "unknown"


def test_verdict_is_ready_only_when_the_installer_can_reach_a_healthy_unit():
    assert commission_verdict(
        {"hotspot": "up", "portal": "up (80)", "adc": "ok", "power": "ok", "fatal": "none"}
    ) == ("READY")
    assert (
        commission_verdict(
            {
                "hotspot": "station",
                "portal": "down: station",
                "adc": "ok",
                "power": "ok",
                "fatal": "none",
            }
        )
        == "READY"
    )
    assert (
        commission_verdict(
            {
                "hotspot": "up",
                "portal": "down: connection refused",
                "adc": "ok",
                "power": "ok",
                "fatal": "none",
            }
        )
        == "FAIL: portal down: connection refused"
    )
    failed = commission_verdict(
        {
            "hotspot": "failed: Error: Connection activation failed",
            "adc": "ok",
            "power": "ok",
            "fatal": "none",
        }
    )
    assert failed == "FAIL: Error: Connection activation failed"
    assert (
        commission_verdict(
            {
                "hotspot": "up",
                "portal": "up (80)",
                "adc": "faulted",
                "power": "now undervoltage throttled=0x1",
                "fatal": "none",
            }
        )
        == "FAIL: weak power supply or cable; adc faulted"
    )
    # The hotspot is up, so a failed site join is not a board failure.
    assert (
        commission_verdict(
            {
                "hotspot": "up",
                "portal": "up (80)",
                "station": "fail: no address",
                "adc": "ok",
                "power": "ok",
                "fatal": "none",
            }
        )
        == "READY"
    )
    assert (
        commission_verdict(
            {
                "hotspot": "station",
                "portal": "down: station",
                "station": "fail: no address",
                "adc": "ok",
                "power": "ok",
                "fatal": "none",
            }
        )
        == "FAIL: no address"
    )
    # A GPS with no fix and a missing 1-Wire probe are not commissioning failures.
    # Undervoltage since this boot is: a weak supply already happened.
    assert (
        commission_verdict(
            {
                "hotspot": "up",
                "portal": "up (80)",
                "adc": "ok",
                "power": "since-boot undervoltage throttled=0x10000",
                "gps": "no fix",
                "onewire": "no probe",
                "fatal": "none",
            }
        )
        == "FAIL: weak power supply or cable"
    )


def test_burnin_passes_when_every_check_holds():
    sample = Sample(hotspot_active=True, adc_ok=True, undervoltage=False, power="ok")
    status, detail = evaluate([sample, sample])
    assert status == "PASS"
    assert "2 checks" in detail


def test_burnin_fails_with_the_check_that_broke():
    good = Sample(True, True, False, "ok")
    bad = Sample(False, False, True, "now undervoltage throttled=0x1", ["bluesignal-wqm.service"])
    status, detail = evaluate([good, bad])
    assert status == "FAIL"
    assert "1/2 checks failed" in detail
    assert "check 2: hotspot down" in detail
    assert "adc not answering" in detail
    assert "undervoltage" in detail
    assert "bluesignal-wqm.service" in detail


def test_run_burnin_samples_every_interval_for_the_duration(tmp_path):
    t = [0.0]

    def clock():
        return t[0]

    def sleep(seconds):
        t[0] += seconds

    n = {"n": 0}

    def probe():
        n["n"] += 1
        return Sample(True, True, False, "ok")

    dest = tmp_path / "wqm1-burnin.txt"
    text = run_burnin(
        duration_s=600, interval_s=30, probe=probe, dest=dest, sleep=sleep, clock=clock
    )
    assert n["n"] == 21  # t = 0, 30, …, 600
    assert text.startswith("BURN-IN: PASS\n")
    assert dest.read_text(encoding="utf-8") == text
    assert "passphrase" not in text


def test_a_restart_during_the_window_fails_and_the_first_count_does_not():
    assert service_is_restarting(None, 3, "active") is False
    assert service_is_restarting(3, 4, "active") is True
    assert service_is_restarting(1, 1, "failed") is True
    names = (
        "bluesignal-wqm.service",
        "bluesignal-service-window.service",
        "bluesignal-ap-fallback.service",
        "NetworkManager.service",
    )
    tracker = RestartTracker()
    assert tracker.update({name: (2, "active") for name in names}) == []
    climbed = {name: (2, "active") for name in names}
    climbed["bluesignal-wqm.service"] = (3, "active")
    assert tracker.update(climbed) == ["bluesignal-wqm.service"]


def test_systemctl_show_parser():
    assert parse_systemctl_show("NRestarts=4\nActiveState=active\n") == (4, "active")


def test_shell_entry_skips_without_the_flag_and_runs_when_it_is_present(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    script = ROOT / "scripts" / "burn-in.sh"
    env = {
        "PATH": "/usr/bin:/bin",
        "WQM1_BURNIN_BOOT_DIRS": str(boot),
        "PYTHONPATH": str(ROOT / "src"),
        "WQM1_BURNIN_DURATION": "0",
        "WQM1_BURNIN_INTERVAL": "30",
    }
    skipped = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert skipped.returncode == 0, skipped.stderr
    assert "skipping" in skipped.stdout
    assert not (boot / "wqm1-burnin.txt").exists()

    (boot / "burnin").write_text("")
    monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "unused-status.txt"))
    ran = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert not (boot / "burnin").exists()
    text = (boot / "wqm1-burnin.txt").read_text(encoding="utf-8")
    assert text.startswith("BURN-IN: ")
    assert "PASS" in text or "FAIL" in text
    assert ran.returncode in (0, 1)
    assert "passphrase" not in text
