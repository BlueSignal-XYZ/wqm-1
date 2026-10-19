"""setup.sh must copy every script a systemd unit executes.

Found 2026-10-12 while reviewing the commissioning plan: bluesignal-card.service
and bluesignal-ap-fallback.service were enabled by setup.sh but the scripts they
run were never copied into the release tree, so no carded unit could consume its
cloud key or raise its setup hotspot. The unit tests import the modules directly
and stayed green. This test reads the units and the installer as text so the
next missing copy fails the build, not the bench.
"""

from __future__ import annotations

import os
import pwd
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SETUP = (ROOT / "setup.sh").read_text()
UNITS = sorted((ROOT / "systemd").glob("*.service"))

CURRENT_SCRIPTS = "/opt/bluesignal/current/scripts/"


def _exec_scripts() -> list[tuple[str, str]]:
    out = []
    for unit in UNITS:
        for line in unit.read_text().splitlines():
            if not line.startswith("ExecStart="):
                continue
            for token in line[len("ExecStart=") :].split():
                if token.startswith(CURRENT_SCRIPTS):
                    out.append((unit.name, token[len(CURRENT_SCRIPTS) :]))
    return out


def test_every_unit_script_exists_in_the_repo():
    for unit, script in _exec_scripts():
        assert (ROOT / "scripts" / script).is_file(), (
            f"{unit} runs scripts/{script}, which does not exist"
        )


@pytest.mark.parametrize("unit,script", _exec_scripts())
def test_setup_copies_every_script_a_unit_executes(unit, script):
    direct = r'cp\s+"\$SCRIPT_DIR/scripts/' + re.escape(script) + r'"'
    looped = (
        r"for\s+\w+\s+in[^\n]*\b" + re.escape(script) + r"\b[^\n]*;\s*do\s*\n"
        r"\s*sudo cp \"\$SCRIPT_DIR/scripts/\$\w+\""
    )
    copied = re.search(direct, SETUP) or re.search(looped, SETUP)
    assert copied, (
        f"{unit} executes {CURRENT_SCRIPTS}{script} but setup.sh never copies scripts/{script}"
    )


def test_no_unit_points_outside_the_current_release():
    """/opt/bluesignal/scripts/ was the pre-OTA layout; nothing lives there now."""
    for unit in UNITS:
        assert "/opt/bluesignal/scripts/" not in unit.read_text(), unit.name


def test_provision_check_runs_after_the_card_is_consumed():
    text = (ROOT / "systemd" / "bluesignal-provision.service").read_text()
    after = next(line for line in text.splitlines() if line.startswith("After="))
    assert "bluesignal-card.service" in after


class TestGoldenMode:
    """`setup.sh --golden` strips unit 1's state before the card is imaged."""

    def _block(self) -> str:
        start = SETUP.index('if [ "${1:-}" = "--golden" ]')
        return SETUP[start : SETUP.index("\nfi\n", start)]

    def test_removes_the_readings_database_and_wal(self):
        block = self._block()
        for f in (
            "wqm1.db",
            "wqm1.db-wal",
            "wqm1.db-shm",
            "water-profile.json",
            ".provisioned",
            "device-ed25519.key",
            "device-http.key",
            "device-register.json",
        ):
            assert f in block, f

    def test_refuses_a_card_carrying_bench_files_before_deleting_anything(self):
        block = self._block()
        assert block.index("bluesignal-identity.json") < block.index("rm -f")
        assert block.index("setup_completed") < block.index("rm -f")

    def test_installs_nothing_and_exits(self):
        block = self._block()
        assert "exit 0" in block
        assert "apt-get" not in block and "pip3" not in block

    def test_keeps_config_and_wifi(self):
        block = self._block()
        assert "config.yaml" not in block.split("rm -f", 1)[1].split("journalctl")[0]
        assert "nmcli" not in block

    def test_reasserts_state_dir_owner_and_mode_without_deleting_it(self):
        block = self._block()
        assert "wqm1_ensure_state_dir" in block
        assert "rmdir" not in block
        assert "rm -rf" not in block
        assert "0700" in SETUP
        assert "d /var/lib/bluesignal 0700 $INSTALL_USER $INSTALL_USER -" in SETUP


def _bash_function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    depth = 0
    for offset, char in enumerate(text[start:]):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : start + offset + 1]
    raise AssertionError(f"unclosed {name}")


class TestInstalledIdentityOwnership:
    """Identity files are 0600 in a 0700 dir owned by the firmware User=.

    bluesignal-wqm and bluesignal-service-window both run as pi (rewritten
    to $INSTALL_USER). Root units (OTA, card) share the directory but do
    not read the seed. setup.sh --golden must leave the dir, not unit 1's key.
    """

    IDENTITY_UNITS = (
        "bluesignal-wqm.service",
        "bluesignal-service-window.service",
    )

    def test_repo_units_name_pi_for_identity_readers_and_root_for_ota(self):
        for name in self.IDENTITY_UNITS:
            text = (ROOT / "systemd" / name).read_text()
            user = next(
                line.split("=", 1)[1].strip()
                for line in text.splitlines()
                if line.startswith("User=")
            )
            assert user == "pi", name
        ota = (ROOT / "systemd" / "bluesignal-ota.service").read_text()
        assert "User=root" in ota
        card = (ROOT / "systemd" / "bluesignal-card.service").read_text()
        assert "User=root" in card

    def test_setup_rewrites_user_pi_to_install_user(self):
        assert "s/^User=pi$/User=$INSTALL_USER/" in SETUP
        assert SETUP.index("wqm1_ensure_state_dir") < SETUP.index("Installing systemd services")

    def test_installed_units_and_state_dir_on_a_fake_root(self, tmp_path):
        root = tmp_path / "root"
        etc = root / "etc" / "systemd" / "system"
        etc.mkdir(parents=True)
        install_user = pwd.getpwuid(os.getuid()).pw_name
        for name in (*self.IDENTITY_UNITS, "bluesignal-captive-portal.service"):
            src = (ROOT / "systemd" / name).read_text()
            (etc / name).write_text(src.replace("User=pi", f"User={install_user}", 1))
            installed = (etc / name).read_text()
            user = next(
                line.split("=", 1)[1].strip()
                for line in installed.splitlines()
                if line.startswith("User=")
            )
            assert user == install_user, name

        state = root / "var" / "lib" / "bluesignal"
        state.mkdir(parents=True)
        state.chmod(0o755)
        (state / "device-ed25519.key").write_bytes(b"\x00" * 32)
        (state / "device-http.key").write_text("oldkey")
        (state / "device-register.json").write_text("{}")
        (state / "wqm1.db").write_text("bench-rows")

        script = "\n".join(
            [
                "set -euo pipefail",
                'sudo() { "$@"; }',
                f"INSTALL_USER={shlex.quote(install_user)}",
                _bash_function(SETUP, "wqm1_ensure_state_dir"),
                _bash_function(SETUP, "wqm1_runtime_user"),
                f"export WQM1_STATE_DIR={shlex.quote(str(state))}",
                f"export WQM1_WQM_UNIT={shlex.quote(str(etc / 'bluesignal-wqm.service'))}",
                'STATE_DIR="${WQM1_STATE_DIR:-/var/lib/bluesignal}"',
                'sudo rm -f "$STATE_DIR/wqm1.db" "$STATE_DIR/wqm1.db-wal" \\',
                '           "$STATE_DIR/wqm1.db-shm"',
                'sudo rm -f "$STATE_DIR/water-profile.json" \\',
                '           "$STATE_DIR/ota-check-now" "$STATE_DIR/reboot-request"',
                'sudo rm -f "$STATE_DIR/device-ed25519.key" \\',
                '           "$STATE_DIR/device-ed25519.key.tmp" \\',
                '           "$STATE_DIR/device-http.key" "$STATE_DIR/device-http.key.tmp" \\',
                '           "$STATE_DIR/device-register.json" \\',
                '           "$STATE_DIR/device-register.json.tmp"',
                'wqm1_ensure_state_dir "$(wqm1_runtime_user)"',
            ]
        )
        result = subprocess.run(
            ["bash", "-s"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr + script
        assert state.is_dir()
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert state.stat().st_uid == os.getuid()
        assert not (state / "device-ed25519.key").exists()
        assert not (state / "device-http.key").exists()
        assert not (state / "device-register.json").exists()
        assert not (state / "wqm1.db").exists()


class TestArmhfInstallFixes:
    """ARMv6 must not build from source when a wheel exists, and the captive
    portal's iptables redirect has to be installed — Trixie Lite has neither."""

    def test_every_pip_install_prefers_a_binary_wheel(self):
        installs = [line for line in SETUP.splitlines() if "pip3 install" in line]
        assert len(installs) >= 4
        assert all("--prefer-binary" in line for line in installs)

    def test_iptables_is_installed_on_every_host_path(self):
        # Pi, Orange Pi and the digital-first apt lists each name the package.
        assert "swig build-essential \\\n        iptables" in SETUP
        assert "systemd-timesyncd \\\n        iptables" in SETUP
        assert "python3-dev iptables" in SETUP
