"""Bench label: SSID, derived passphrase, and the Wi-Fi QR. No log line."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make-unit-label.py"


def _load():
    spec = importlib.util.spec_from_file_location("make_unit_label", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_label_qr_payload_and_files_do_not_print_the_passphrase(tmp_path, capsys):
    import hashlib

    from utils.identity import ap_name, get_derived_id
    from utils.netctl import derived_passphrase
    from utils.wifi_setup import wifi_qr_payload

    serial = "1000000086544fd8"
    mod = _load()
    label = mod.build_label(serial)
    expect_pass = hashlib.sha256(f"wqm1-ap:{serial}".encode()).hexdigest()[:12]
    ssid = ap_name(get_derived_id(serial))
    assert label["ssid"] == ssid == "WQM1-4FD8"
    assert label["passphrase"] == expect_pass == derived_passphrase(serial)
    assert label["qr"] == f"WIFI:T:WPA;S:{ssid};P:{expect_pass};;"
    assert label["qr"] == wifi_qr_payload(ssid, expect_pass)

    assert mod.main([serial, "--out-dir", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert expect_pass not in printed
    svg = (tmp_path / f"{ssid}-hotspot-label.svg").read_text(encoding="utf-8")
    pdf = (tmp_path / f"{ssid}-hotspot-label.pdf").read_bytes()
    assert ssid in svg and expect_pass in svg
    assert b"WIFI:T:WPA" not in pdf  # the payload is the QR drawing, not a caption
    assert ssid.encode("ascii") in pdf
    assert expect_pass.encode("ascii") in pdf
    assert pdf.startswith(b"%PDF-1.4")


def test_qr_escapes_reserved_characters():
    from utils.wifi_setup import wifi_qr_payload

    assert wifi_qr_payload('a;b"c', "p:q,s") == 'WIFI:T:WPA;S:a\\;b\\"c;P:p\\:q\\,s;;'


def test_bad_serial_exits_without_a_file(tmp_path, capsys):
    mod = _load()
    assert mod.main(["0000000000000000", "--out-dir", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []
    err = capsys.readouterr().err
    assert "serial" in err.lower()


def test_firmware_does_not_invoke_the_label_script():
    """The passphrase is a bench print. Firmware and boot units must not run it."""
    root = ROOT
    banned = []
    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "make-unit-label" in text:
            banned.append(str(path))
    for path in (root / "systemd").glob("*.service"):
        if "make-unit-label" in path.read_text(encoding="utf-8", errors="replace"):
            banned.append(str(path))
    assert banned == []
