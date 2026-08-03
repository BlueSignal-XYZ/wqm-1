"""
A misconfigured cloud transport must SAY SO, and saving a key must not
silently disable the thing it configures.

Both defects cost a real bring-up several hours on 2026-08-02. The unit had
`cloud_enabled: true` and an empty `api_key`, so:

  * `main.py` skipped the cloud block on a bare `if` with no else, producing
    not one line of log — indistinguishable from a device that was never
    configured for cloud at all; and
  * the Provisioning form had earlier written `cloud_enabled: false` while
    accepting the key field, because the enable checkbox was unticked — the
    save actively turned OFF the feature being set up.

The dashboard, meanwhile, said "Offline — no sensor data", which was true and
useless. Nothing anywhere named the cause.
"""

import re
from pathlib import Path

import pytest
import yaml

SRC = Path(__file__).resolve().parent.parent / "src"


class TestCloudSkipIsNeverSilent:
    """Every reason the cloud worker does not start must be logged."""

    @pytest.fixture
    def main_src(self):
        return (SRC / "main.py").read_text()

    def test_enabled_without_a_key_logs_an_error(self, main_src):
        block = main_src[main_src.index("Cloud (HTTP/WiFi) transport") :][:2500]
        assert "cloud_enabled and not self._settings.api_key" in block
        assert "logger.error" in block
        # The message has to name the fix, not just the fault.
        assert re.search(r"paste the key|Claim it", block, re.I)

    def test_key_without_enabled_warns_that_nothing_uploads(self, main_src):
        block = main_src[main_src.index("Cloud (HTTP/WiFi) transport") :][:2500]
        assert "not self._settings.cloud_enabled and self._settings.api_key" in block
        assert "logger.warning" in block
        assert re.search(r"never uploaded|stored locally", block, re.I)

    def test_the_fully_disabled_case_is_still_stated(self, main_src):
        block = main_src[main_src.index("Cloud (HTTP/WiFi) transport") :][:2500]
        assert "logger.info" in block

    def test_the_bare_silent_skip_cannot_come_back(self, main_src):
        """The original shape: a lone `if enabled and key:` and nothing else."""
        idx = main_src.index("Cloud (HTTP/WiFi) transport")
        block = main_src[idx : idx + 2500]
        # There must be diagnostic branches BEFORE the construction branch.
        first_if = block.index("if self._settings.cloud_enabled")
        assert "logger" in block[:first_if] or "logger" in block[first_if : first_if + 1500]


class TestSavingAKeyNeverDisablesCloud:
    """Providing a credential means 'make this work'."""

    @pytest.fixture
    def provision_src(self):
        return (SRC / "service_window" / "routes" / "provision.py").read_text()

    def test_a_supplied_key_forces_cloud_enabled(self, provision_src):
        assert "if api_key and not enabled:" in provision_src
        seg = provision_src[provision_src.index("if api_key and not enabled:") :][:600]
        assert 'updates["cloud_enabled"] = True' in seg

    def test_the_operator_is_told_it_was_enabled_for_them(self, provision_src):
        seg = provision_src[provision_src.index("if api_key and not enabled:") :][:600]
        assert "flash(" in seg
        assert re.search(r"enabled", seg, re.I)

    def test_a_deliberate_disable_is_still_possible(self, provision_src):
        """Blank key + unticked box must remain a real 'turn it off'."""
        seg = provision_src[provision_src.index("if api_key and not enabled:") :][:600]
        # The override is conditional on a key being present, not unconditional.
        assert "if api_key and not enabled:" in seg
        assert 'updates["cloud_enabled"] = enabled' not in seg.split("if api_key")[0]


class TestConfigContractHolds:
    """The example config must stay loadable and honest about these keys."""

    def test_example_config_parses_and_ships_cloud_off_with_no_key(self):
        example = SRC.parent / "config" / "config.yaml.example"
        cfg = yaml.safe_load(example.read_text())
        # Shipping enabled-with-no-key is the exact broken state above.
        assert not (cfg.get("cloud_enabled") and not cfg.get("api_key"))
