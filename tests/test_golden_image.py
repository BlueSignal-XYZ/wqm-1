"""The golden-image tree names no board and carries no Wi-Fi secret.

The image build itself needs the Raspberry Pi OS tarball, root and qemu.
These checks are what CI can hold without that.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tools" / "golden"

# A credential would be one of these. "PasswordAuthentication" and the word
# "password-locked" in a comment are not secrets; the patterns are the
# forms a NetworkManager or wpa_supplicant key would take.
_SECRET_MARKERS = ("psk=", "wifi-sec", "wpa-psk", "wifi-password")


def test_tree_is_present():
    assert (GOLDEN / "build-golden.sh").is_file()
    assert (GOLDEN / "MANIFEST.md").is_file()
    assert (GOLDEN / "overlay/usr/local/sbin/wqm1-firstboot").is_file()
    assert (GOLDEN / "overlay/etc/systemd/system/wqm1-firstboot.service").is_file()
    assert (GOLDEN / "bootfs/network-config").is_file()
    assert (GOLDEN / "bootfs/user-data").is_file()


def test_build_does_not_hardcode_a_board_name():
    script = (GOLDEN / "build-golden.sh").read_text()
    assert 'BOARD_MODEL="Raspberry Pi Zero W' not in script
    assert "sed 's|^BOARD_MODEL=" not in script
    # The chroot has no device tree. This string only selects the Pi package
    # path; it is not a model and setup.sh must not be patched to embed one.
    assert 'WQM1_BOARD_MODEL="Raspberry Pi"' in script
    assert "setup-offline.sh" not in script


def test_firstboot_reads_the_device_tree_model():
    text = (GOLDEN / "overlay/usr/local/sbin/wqm1-firstboot").read_text()
    assert "/proc/device-tree/model" in text
    assert "Raspberry Pi Zero W" not in text
    assert "Raspberry Pi Zero 2 W" not in text
    assert "64-bit card on a Pi Zero W" in text
    assert "32-bit card on a Pi 5" in text


def test_build_selects_arch_without_a_second_script():
    script = (GOLDEN / "build-golden.sh").read_text()
    assert "--arch" in script
    assert "qemu-aarch64" in script
    assert "QEMU_CPU=arm1176" in script
    # arm64 must not be forced onto the ARMv6 CPU the armhf build uses.
    assert 'if [ "$ARCH" = "armhf" ]' in script
    assert "wqm1-golden-arm64.img" in script
    assert "wqm1-golden.img" in script


def test_hostname_follows_the_board_every_boot(tmp_path):
    """A card imaged on one Pi and moved to another used to keep the first
    hostname. The SSID is derived at runtime; the hostname has to be too."""
    script = GOLDEN / "overlay/usr/local/sbin/wqm1-firstboot"
    text = script.read_text()
    code = "\n".join(
        line for line in text.splitlines() if line and not line.lstrip().startswith("#")
    )
    assert "identity-done" not in code
    assert "ap_name().lower()" in text

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "hostnamectl.log"

    def _fake(name: str, body: str) -> None:
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)

    _fake("python3", "#!/bin/sh\necho wqm1-4fd8\n")
    _fake("hostname", "#!/bin/sh\necho wqm1-e794\n")
    _fake("hostnamectl", f'#!/bin/sh\necho "$@" >> {log}\n')
    _fake("systemctl", "#!/bin/sh\nexit 0\n")
    for cmd in ("rfkill", "iw", "nmcli"):
        _fake(cmd, "#!/bin/sh\nexit 0\n")

    src = tmp_path / "src"
    src.mkdir()
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n127.0.1.1\twqm1-e794\n")
    hostname_file = tmp_path / "hostname"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "WQM1_FIRMWARE_SRC": str(src),
        "WQM1_HOSTS_FILE": str(hosts),
        "WQM1_HOSTNAME_FILE": str(hostname_file),
        "WQM1_CPUINFO": str(tmp_path / "missing-cpuinfo"),
    }
    import subprocess

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "set-hostname wqm1-4fd8" in log.read_text()
    assert "wqm1-4fd8" in hosts.read_text()
    assert "wqm1-e794" not in hosts.read_text().split("127.0.1.1", 1)[-1]
    assert not hostname_file.exists()
    assert "hostname set to wqm1-4fd8" in result.stdout


def test_hostname_fallback_uses_the_last_four_of_the_cpu_serial(tmp_path):
    script = GOLDEN / "overlay/usr/local/sbin/wqm1-firstboot"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "hostnamectl.log"

    def _fake(name: str, body: str) -> None:
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)

    _fake("python3", "#!/bin/sh\nexit 1\n")
    _fake("hostname", "#!/bin/sh\necho raspberrypi\n")
    _fake("hostnamectl", f'#!/bin/sh\necho "$@" >> {log}\n')
    _fake("systemctl", "#!/bin/sh\nexit 0\n")
    for cmd in ("rfkill", "iw", "nmcli"):
        _fake(cmd, "#!/bin/sh\nexit 0\n")
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("Serial\t\t: 1000000086544fd8\n")
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.1.1\traspberrypi\n")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "WQM1_FIRMWARE_SRC": str(tmp_path / "missing-src"),
        "WQM1_HOSTS_FILE": str(hosts),
        "WQM1_HOSTNAME_FILE": str(tmp_path / "hostname"),
        "WQM1_CPUINFO": str(cpuinfo),
    }
    import subprocess

    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "set-hostname wqm1-4fd8" in log.read_text()


def test_hostname_is_left_alone_when_it_already_matches(tmp_path):
    script = GOLDEN / "overlay/usr/local/sbin/wqm1-firstboot"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "hostnamectl.log"

    def _fake(name: str, body: str) -> None:
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)

    _fake("python3", "#!/bin/sh\necho wqm1-4fd8\n")
    _fake("hostname", "#!/bin/sh\necho wqm1-4fd8\n")
    _fake("hostnamectl", f'#!/bin/sh\necho "$@" >> {log}\n')
    _fake("systemctl", "#!/bin/sh\nexit 0\n")
    for cmd in ("rfkill", "iw", "nmcli"):
        _fake(cmd, "#!/bin/sh\nexit 0\n")
    src = tmp_path / "src"
    src.mkdir()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "WQM1_FIRMWARE_SRC": str(src),
        "WQM1_HOSTS_FILE": str(tmp_path / "hosts"),
        "WQM1_HOSTNAME_FILE": str(tmp_path / "hostname"),
    }
    import subprocess

    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert not log.exists()
    assert "hostname already wqm1-4fd8" in result.stdout


def test_no_wifi_credentials_anywhere_in_the_tree():
    for path in GOLDEN.rglob("*"):
        if not path.is_file():
            continue
        lowered = path.read_text(errors="replace").lower()
        for marker in _SECRET_MARKERS:
            assert marker not in lowered, f"{path} contains {marker}"
