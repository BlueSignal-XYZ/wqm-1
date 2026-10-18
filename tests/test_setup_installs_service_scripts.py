"""setup.sh must copy every script a systemd unit executes.

Found 2026-10-12 while reviewing the commissioning plan: bluesignal-card.service
and bluesignal-ap-fallback.service were enabled by setup.sh but the scripts they
run were never copied into the release tree, so no carded unit could consume its
cloud key or raise its setup hotspot. The unit tests import the modules directly
and stayed green. This test reads the units and the installer as text so the
next missing copy fails the build, not the bench.
"""

from __future__ import annotations

import re
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
        for f in ("wqm1.db", "wqm1.db-wal", "wqm1.db-shm", "water-profile.json", ".provisioned"):
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
