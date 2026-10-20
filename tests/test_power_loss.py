"""Power-loss resilience: atomic writes, resumable first boot, card moves, undervoltage.

The golden scripts run under fakes. Nothing here mounts a card, talks to
vcgencmd, or writes /boot on this machine.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tools" / "golden"
FIRSTBOOT = GOLDEN / "overlay/usr/local/sbin/wqm1-firstboot"
BOOT_STAGE = GOLDEN / "overlay/usr/local/sbin/wqm1-boot-stage"
RESILIENCE = GOLDEN / "rootfs-resilience.sh"
JOURNAL = GOLDEN / "overlay/usr/local/sbin/wqm1-journal-preserve"

OLD = "00000000aabbccdd"
NEW = "1000000086544fd8"
STEPS = ("board", "radio", "card", "hostname", "done")


def _fake(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


def _run_firstboot(
    tmp_path: Path,
    *,
    cpu_serial: str = NEW,
    nm_show: str = "",
    nm_rc: int = 0,
    hostname: str = "raspberrypi",
    state_text: str | None = None,
    status_text: str | None = None,
    binding_text: str | None = None,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    nm_log = tmp_path / "nmcli.log"
    _fake(bin_dir, "python3", "#!/bin/sh\nprintf '%s\\n' 'wqm1-4fd8'\n")
    _fake(bin_dir, "hostname", f"#!/bin/sh\nprintf '%s\\n' '{hostname}'\n")
    _fake(bin_dir, "hostnamectl", "#!/bin/sh\nexit 0\n")
    _fake(bin_dir, "systemctl", "#!/bin/sh\nexit 0\n")
    _fake(bin_dir, "rfkill", "#!/bin/sh\nexit 0\n")
    _fake(bin_dir, "iw", "#!/bin/sh\nexit 0\n")
    _fake(
        bin_dir,
        "nmcli",
        "#!/bin/sh\n"
        f'echo "$@" >> {nm_log}\n'
        'if [ "$1" = "-t" ]; then\n'
        f"  printf '%s\\n' '{nm_show}'\n"
        f"  exit {nm_rc}\n"
        "fi\n"
        "exit 0\n",
    )
    cpu = tmp_path / "cpuinfo"
    cpu.write_text(f"Serial\t\t: {cpu_serial}\n")
    status = tmp_path / "wqm1-status.txt"
    if status_text is not None:
        status.write_text(status_text)
    state = tmp_path / "firstboot-state"
    if state_text is not None:
        state.write_text(state_text)
    binding = tmp_path / "card-binding"
    if binding_text is not None:
        binding.write_text(binding_text)
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.1.1\traspberrypi\n")
    (tmp_path / "src").mkdir()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "WQM1_FIRMWARE_SRC": str(tmp_path / "src"),
        "WQM1_HOSTS_FILE": str(hosts),
        "WQM1_HOSTNAME_FILE": str(tmp_path / "hostname-file"),
        "WQM1_CPUINFO": str(cpu),
        "WQM1_STATUS_FILE": str(status),
        "WQM1_FIRSTBOOT_STATE": str(state),
        "WQM1_CARD_STATE": str(binding),
    }
    result = subprocess.run(
        ["bash", str(FIRSTBOOT)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result, status, state, binding, nm_log


def test_atomic_write_fsyncs_then_renames(tmp_path, monkeypatch):
    from utils.atomic import atomic_write_text

    synced: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    replaced: list[tuple[str, str]] = []
    real_replace = Path.replace

    def spy_replace(self: Path, target: Path) -> Path:
        replaced.append((self.name, Path(target).name))
        return real_replace(self, target)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(Path, "replace", spy_replace)
    dest = tmp_path / "data.json"
    atomic_write_text(dest, '{"ok": true}\n', mode=0o640)
    assert dest.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert not (tmp_path / "data.tmp").exists()
    assert replaced == [("data.tmp", "data.json")]
    assert len(synced) >= 2
    assert dest.stat().st_mode & 0o777 == 0o640


def test_atomic_write_drops_the_temp_file_when_rename_fails(tmp_path, monkeypatch):
    from utils.atomic import atomic_write_text

    def boom(self: Path, target: Path) -> Path:
        raise OSError("cross-device")

    monkeypatch.setattr(Path, "replace", boom)
    dest = tmp_path / "identity.json"
    dest.write_text("old", encoding="utf-8")
    with pytest.raises(OSError, match="cross-device"):
        atomic_write_text(dest, "new")
    assert dest.read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "identity.tmp").exists()


def test_atomic_write_refuses_a_missing_parent_when_told_not_to_create(tmp_path):
    from utils.atomic import atomic_write_text

    dest = tmp_path / "missing" / "config.yaml"
    with pytest.raises(FileNotFoundError):
        atomic_write_text(dest, "x: 1\n", create_parent=False)
    assert not dest.exists()


@pytest.mark.parametrize("step", ["board", "radio", "card", "hostname"])
def test_firstboot_resumes_each_interrupted_step(tmp_path, step):
    prior = "".join(f"{name}=done\n" for name in STEPS if name != step and name != "done")
    prior += f"{step}=started\n"
    result, status, state, _binding, _log = _run_firstboot(tmp_path, state_text=prior)
    assert result.returncode == 0, result.stderr
    assert f"resuming {step} after power loss" in result.stdout
    text = state.read_text(encoding="utf-8")
    for name in STEPS:
        assert f"{name}=done" in text
    assert "BOOT: stage=done" in status.read_text(encoding="utf-8")


def test_a_finished_step_is_not_logged_as_a_resume(tmp_path):
    result, status, state, _binding, _log = _run_firstboot(
        tmp_path, state_text="board=done\nradio=done\n"
    )
    assert result.returncode == 0, result.stderr
    assert "resuming " not in result.stdout
    text = state.read_text(encoding="utf-8")
    for name in STEPS:
        assert f"{name}=done" in text
    assert "BOOT: stage=done" in status.read_text(encoding="utf-8")


def test_boot_stage_replaces_a_stale_ready_line_before_the_hotspot(tmp_path):
    status = tmp_path / "wqm1-status.txt"
    status.write_text("COMMISSION: READY\nWQM-1 boot status\nhotspot: up\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "WQM1_STATUS_FILE": str(status), "HOME": str(tmp_path)}
    result = subprocess.run(
        ["bash", str(BOOT_STAGE)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert status.read_text(encoding="utf-8") == "BOOT: stage=boot\n"
    assert "COMMISSION" not in status.read_text(encoding="utf-8")


def test_firstboot_ends_on_stage_done(tmp_path):
    result, status, _state, binding, _log = _run_firstboot(
        tmp_path, status_text="BOOT: stage=boot\n"
    )
    assert result.returncode == 0, result.stderr
    text = status.read_text(encoding="utf-8")
    assert text.startswith("BOOT: stage=done\n")
    assert "COMMISSION" not in text
    from utils.cardmove import load

    assert load(binding).commissioned_serial == NEW
    assert f"card: ok {NEW}" in text


def test_shell_and_python_agree_when_a_card_moves_with_site_wifi(tmp_path):
    from utils.cardmove import heartbeat_fields, load

    seeded = f"commissioned_serial={OLD}\nmoved_from=\nmoved_to=\nwifi_reconfirm=0\n"
    result, status, _state, binding, nm_log = _run_firstboot(
        tmp_path,
        nm_show="PondHouse:802-11-wireless\nwqm1-setup-ap:802-11-wireless",
        binding_text=seeded,
    )
    assert result.returncode == 0, result.stderr
    assert f"card moved from {OLD} to {NEW}" in result.stdout
    text = status.read_text(encoding="utf-8")
    assert f"card: moved from {OLD} to {NEW}" in text
    assert "reconfirm: required" in text
    found = load(binding)
    assert found.commissioned_serial == OLD
    assert found.moved_from == OLD
    assert found.moved_to == NEW
    assert found.wifi_reconfirm is True
    logged = nm_log.read_text(encoding="utf-8")
    assert "connection modify PondHouse connection.autoconnect no" in logged
    assert "device disconnect wlan0" in logged
    fields = heartbeat_fields(binding)
    assert fields["cardMoved"] is True
    assert fields["cardMovedFrom"] == OLD
    assert fields["cardMovedTo"] == NEW
    assert fields["wifiReconfirm"] is True

    again, status2, _state2, binding2, _log2 = _run_firstboot(
        tmp_path / "again",
        nm_show="PondHouse:802-11-wireless",
        binding_text=binding.read_text(encoding="utf-8"),
    )
    assert again.returncode == 0, again.stderr
    stayed = load(binding2)
    assert stayed.commissioned_serial == OLD
    assert stayed.wifi_reconfirm is True
    assert f"card: moved from {OLD} to {NEW}" in status2.read_text(encoding="utf-8")


def test_a_move_without_site_wifi_adopts_the_new_pi_and_clears_next_boot(tmp_path):
    from utils.cardmove import load

    seeded = f"commissioned_serial={OLD}\nmoved_from=\nmoved_to=\nwifi_reconfirm=0\n"
    result, status, _state, binding, nm_log = _run_firstboot(tmp_path, binding_text=seeded)
    assert result.returncode == 0, result.stderr
    found = load(binding)
    assert found.commissioned_serial == NEW
    assert found.moved_from == OLD
    assert found.moved_to == NEW
    assert found.wifi_reconfirm is False
    assert f"card: moved from {OLD} to {NEW}" in status.read_text(encoding="utf-8")
    assert "reconfirm: ok" in status.read_text(encoding="utf-8")
    assert "connection modify" not in nm_log.read_text(encoding="utf-8")

    again, status2, _s, binding2, _l = _run_firstboot(
        tmp_path / "next",
        binding_text=binding.read_text(encoding="utf-8"),
    )
    assert again.returncode == 0, again.stderr
    cleared = load(binding2)
    assert cleared.commissioned_serial == NEW
    assert cleared.moved_from == ""
    assert cleared.moved is False
    assert f"card: ok {NEW}" in status2.read_text(encoding="utf-8")
    assert "card moved" not in again.stdout


def test_an_unreadable_network_list_latches_reconfirm(tmp_path):
    from utils.cardmove import load

    seeded = f"commissioned_serial={OLD}\nmoved_from=\nmoved_to=\nwifi_reconfirm=0\n"
    result, status, _state, binding, _log = _run_firstboot(tmp_path, nm_rc=1, binding_text=seeded)
    assert result.returncode == 0, result.stderr
    found = load(binding)
    assert found.commissioned_serial == OLD
    assert found.wifi_reconfirm is True
    assert found.moved_to == NEW
    assert "reconfirm: required" in status.read_text(encoding="utf-8")


def test_python_observe_matches_the_shell_rules(tmp_path, monkeypatch):
    from utils.cardmove import (
        confirm_binding,
        heartbeat_fields,
        observe,
        wifi_reconfirm_required,
    )

    path = tmp_path / "card-binding"
    monkeypatch.setenv("WQM1_CARD_STATE", str(path))
    first = observe(NEW, site_wifi=False)
    assert first.commissioned_serial == NEW
    assert first.moved is False
    assert heartbeat_fields() == {}
    assert observe("0000000000000000", site_wifi=True).commissioned_serial == NEW
    assert observe("", site_wifi=True).commissioned_serial == NEW

    moved = observe(OLD, site_wifi=True)
    assert moved.commissioned_serial == NEW
    assert moved.moved_from == NEW
    assert moved.moved_to == OLD
    assert wifi_reconfirm_required() is True
    assert heartbeat_fields()["wifiReconfirm"] is True

    monkeypatch.setattr("utils.cardmove.get_pi_serial", lambda: OLD)
    assert confirm_binding() is True
    assert wifi_reconfirm_required() is False
    assert heartbeat_fields() == {}
    assert observe(OLD, site_wifi=False).moved is False


def test_confirm_does_not_create_a_binding_from_the_fallback_serial(tmp_path, monkeypatch):
    from utils.cardmove import confirm_binding

    path = tmp_path / "card-binding"
    monkeypatch.setenv("WQM1_CARD_STATE", str(path))
    assert confirm_binding("1000000086544fd8") is False
    assert not path.exists()


def test_throttled_bits_and_the_commission_fail_reason(tmp_path, monkeypatch):
    from diagnostics.boot_status import (
        WEAK_POWER_REASON,
        commission_verdict,
        format_power_line,
        read_power,
        read_sysfs_throttled,
        text_has_undervoltage,
        write_boot_status,
    )
    from diagnostics.burnin import Sample, evaluate, undervoltage_now

    assert format_power_line("throttled=0x0") == "ok"
    assert format_power_line("throttled=0x1") == "undervoltage seen (1)"
    assert format_power_line("throttled=0x10000") == "undervoltage seen (1)"
    assert format_power_line("throttled=0x50005", count=4) == "undervoltage seen (4)"
    assert format_power_line("nope") == "unknown"
    capped = format_power_line("throttled=0x20000")
    assert capped.startswith("since-boot freq-capped")
    assert text_has_undervoltage(capped) is False
    assert WEAK_POWER_REASON == "weak power supply or cable"
    ready = {
        "hotspot": "up",
        "portal": "up (80)",
        "adc": "ok",
        "fatal": "none",
    }
    assert commission_verdict({**ready, "power": capped}) == "READY"
    assert commission_verdict({**ready, "power": "undervoltage seen (2)"}) == (
        f"FAIL: {WEAK_POWER_REASON}"
    )
    assert undervoltage_now("since-boot freq-capped throttled=0x20000") == (
        False,
        "since-boot freq-capped throttled=0x20000",
    )
    assert undervoltage_now("undervoltage seen (3)")[0] is True
    status, detail = evaluate(
        [
            Sample(True, True, True, "undervoltage seen (1)"),
            Sample(True, True, True, "undervoltage seen (2)"),
        ]
    )
    assert status == "FAIL"
    assert WEAK_POWER_REASON in detail
    assert "undervoltage seen (3)" in detail

    root = tmp_path / "sys"
    node = root / "devices/platform/soc/soc:firmware/get_throttled"
    node.parent.mkdir(parents=True)
    node.write_text("0x10000\n", encoding="utf-8")
    assert read_sysfs_throttled(tmp_path) == "throttled=0x10000"
    alarm = root / "class/hwmon/hwmon0/in0_lcrit_alarm"
    alarm.parent.mkdir(parents=True)
    node.unlink()
    alarm.write_text("1\n", encoding="utf-8")
    assert read_sysfs_throttled(tmp_path) == "throttled=0x1"

    throttled = tmp_path / "throttled"
    throttled.write_text("throttled=0x10000\n", encoding="utf-8")
    status_file = tmp_path / "wqm1-status.txt"
    monkeypatch.setenv("WQM1_THROTTLED_FILE", str(throttled))
    monkeypatch.setenv("WQM1_STATUS_FILE", str(status_file))
    monkeypatch.delenv("WQM1_CARD_STATE", raising=False)
    assert read_power() == "undervoltage seen (1)"
    written = write_boot_status(
        hotspot="up",
        portal="up (80)",
        adc="ok",
        fatal="none",
        wlan0="up",
        station="",
        board="Raspberry Pi Zero W",
        revision="9000c1",
        kernel="test",
        wifi="BCM43430/1",
        i2c="ok",
        onewire="not fitted",
        gps="absent",
        uptime="10",
    )
    assert written.startswith(f"COMMISSION: FAIL: {WEAK_POWER_REASON}\n")
    assert "BOOT: stage=" in written
    assert "power: undervoltage seen (1)" in written
    assert "power: undervoltage seen (1)" in status_file.read_text(encoding="utf-8")


def test_journal_preserve_keeps_the_newest_previous_boot(tmp_path):
    src = tmp_path / "journal"
    src.mkdir()
    old = src / "system-old.journal"
    new = src / "system@boot.journal"
    old.write_text("old-boot", encoding="utf-8")
    new.write_text("new-boot", encoding="utf-8")
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))
    dest = tmp_path / "previous"
    env = {
        "PATH": "/usr/bin:/bin",
        "WQM1_JOURNAL_SRC": str(src),
        "WQM1_JOURNAL_PREVIOUS": str(dest),
    }
    result = subprocess.run(
        ["bash", str(JOURNAL)], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert (dest / "previous.journal").read_text(encoding="utf-8") == "new-boot"
    missing = subprocess.run(
        ["bash", str(JOURNAL)],
        capture_output=True,
        text=True,
        env={**env, "WQM1_JOURNAL_SRC": str(tmp_path / "absent")},
        check=False,
    )
    assert missing.returncode == 0


def test_rootfs_resilience_dry_run_and_the_golden_build_hooks(tmp_path):
    cmdline = tmp_path / "cmdline.txt"
    cmdline.write_text(
        "console=tty1 root=PARTUUID=abcd-02 rootfstype=ext4 rootwait\n", encoding="utf-8"
    )
    fstab = tmp_path / "fstab"
    fstab.write_text("PARTUUID=abcd-02  /  ext4  defaults  0  0\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(RESILIENCE), "--dry-run", "--cmdline", str(cmdline), "--fstab", str(fstab)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "fsck.repair=yes" in out
    assert "noatime" in out
    assert "errors=remount-ro" in out
    assert "tune2fs -c 1 -C 1 -i 0 -e remount-ro" in out
    assert "overlayfs=disabled" in out
    assert "journald=persistent SyncIntervalSec=30s SystemMaxUse=32M" in out
    root_line = next(line for line in out.splitlines() if line.startswith("fstab_root="))
    assert root_line.rstrip().endswith("1")
    assert cmdline.read_text(encoding="utf-8").count("fsck.repair=yes") == 1
    again = subprocess.run(
        ["bash", str(RESILIENCE), "--dry-run", "--cmdline", str(cmdline), "--fstab", str(fstab)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert again.returncode == 0
    assert cmdline.read_text(encoding="utf-8").count("fsck.repair=yes") == 1

    script = (GOLDEN / "build-golden.sh").read_text(encoding="utf-8")
    assert 'bash "$HERE/rootfs-resilience.sh" apply-cmdline' in script
    assert 'bash "$HERE/rootfs-resilience.sh" apply-fstab' in script
    assert "tune2fs-args" in script
    assert "overlayroot" not in script
    assert "boot=overlay" not in script
    assert script.rfind("tune2fs") > script.rfind('e2fsck -fy "$L2"')
    journal = (GOLDEN / "overlay/etc/systemd/journald.conf.d/wqm1.conf").read_text(encoding="utf-8")
    assert any(line.strip() == "Storage=persistent" for line in journal.splitlines())
    assert not any(line.strip() == "Storage=volatile" for line in journal.splitlines())
    assert "SyncIntervalSec=30s" in journal
    assert "SystemMaxUse=32M" in journal
    assert "fsck.repair=yes" in (GOLDEN / "bootfs/cmdline.txt").read_text(encoding="utf-8")
