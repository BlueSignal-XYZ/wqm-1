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


def test_no_wifi_credentials_anywhere_in_the_tree():
    for path in GOLDEN.rglob("*"):
        if not path.is_file():
            continue
        lowered = path.read_text(errors="replace").lower()
        for marker in _SECRET_MARKERS:
            assert marker not in lowered, f"{path} contains {marker}"
