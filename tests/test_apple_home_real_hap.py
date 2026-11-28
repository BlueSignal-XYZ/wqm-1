"""
Apple Home against the real HAP-python library.

tests/test_apple_home.py runs every path against a fake adapter, so it never
builds a real accessory. This file builds one with the library pinned in
requirements-apple-home.txt: no network, no mDNS, the driver is never started.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pyhap", reason="HAP-python not installed (requirements-apple-home.txt)")

from integrations.apple_home.bridge import HapAdapter  # noqa: E402


def _adapter(tmp_path: Path) -> HapAdapter:
    return HapAdapter(
        name="WQM-1 test",
        device_id="BS-WQM1-test",
        firmware_version="2.4.0",
        persist_file=tmp_path / "hap" / "accessory.state",
        port=51826,
    )


def test_the_real_library_builds_the_accessory(tmp_path):
    adapter = _adapter(tmp_path)
    assert adapter.setup_code  # the driver made a pairing code
    assert not adapter.paired


def test_every_named_service_carries_its_name(tmp_path):
    adapter = _adapter(tmp_path)
    acc = adapter._accessory
    for type_name in ("TemperatureSensor", "ContactSensor"):
        svc = acc.get_service(type_name)
        assert svc is not None, type_name
        assert svc.get_characteristic("Name").value, type_name


def test_temperature_and_contact_values_can_be_written(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.set("temperature", 24.1)
    adapter.set("contact", 0)
    assert adapter._chars["temperature"].value == 24.1
    assert adapter._chars["contact"].value == 0
