"""
scripts/wqm1-consume-card.py — the first-boot card consumer (commissioning
plan, PR 3/5). The bench writes bluesignal-cloud.json to the FAT partition;
the first boot moves it into config.yaml and deletes it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location(
        "consume_card", Path(__file__).parent.parent / "scripts" / "wqm1-consume-card.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CARD = {
    "serial": "WQM-10001",
    "api_key": "a" * 64,
    "app_key": "B" * 32,
    "join_eui": "70b3d57ed0000001",
    "cloud_api_base": "https://example.test/app",
    "cloud_ingest_url": "https://example.test/ingest",
    "cloud_enabled": True,
}


class TestConsume:
    def test_moves_credentials_into_config_and_deletes_the_card(self, mod, tmp_path):
        card = tmp_path / "bluesignal-cloud.json"
        card.write_text(json.dumps(CARD))
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({"sensor_read_s": 60}))

        r = mod.consume(paths=(str(card),), config_path=str(cfg), device_id="WQM-10001")
        assert r["action"] == "consumed"
        assert not card.exists()
        written = yaml.safe_load(cfg.read_text())
        assert written["api_key"] == "a" * 64
        assert written["app_key"] == "b" * 32
        assert written["app_eui"] == "70B3D57ED0000001"
        assert written["cloud_enabled"] is True
        assert written["cloud_ingest_url"] == "https://example.test/ingest"
        assert written["sensor_read_s"] == 60  # nothing else touched

    def test_refuses_a_card_for_another_unit_and_leaves_it_on_the_card(self, mod, tmp_path):
        card = tmp_path / "bluesignal-cloud.json"
        card.write_text(json.dumps(CARD))
        cfg = tmp_path / "config.yaml"
        cfg.write_text("{}")
        r = mod.consume(paths=(str(card),), config_path=str(cfg), device_id="WQM-10002")
        assert r["action"] == "refused"
        assert "WQM-10001" in r["reason"]
        assert card.exists()
        assert "api_key" not in (yaml.safe_load(cfg.read_text()) or {})

    def test_no_card_is_a_quiet_no_op(self, mod, tmp_path):
        r = mod.consume(paths=(str(tmp_path / "nope.json"),), config_path=str(tmp_path / "c.yaml"))
        assert r == {"action": "none", "reason": "no card file"}

    @pytest.mark.parametrize(
        "bad",
        [
            {"api_key": "short"},
            {"app_key": "zz"},
            {"join_eui": "12"},
            {"cloud_ingest_url": "ftp://x"},
        ],
    )
    def test_malformed_fields_are_refused(self, mod, bad):
        with pytest.raises(ValueError):
            mod.validate({**CARD, **bad}, "WQM-10001")

    def test_unit_runs_before_the_firmware_as_root(self):
        unit = (Path(__file__).parent.parent / "systemd" / "bluesignal-card.service").read_text()
        assert "Before=bluesignal-wqm.service" in unit
        assert "User=root" in unit
        assert "wqm1-consume-card.py" in unit
