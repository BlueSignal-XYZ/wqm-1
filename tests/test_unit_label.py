"""Bench label: the open setup network's name and a QR that joins it (§5)."""

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


def test_label_names_the_open_network_and_carries_no_password(tmp_path, capsys):
    serial = "1000000086544fd8"
    mod = _load()
    label = mod.build_label(serial)
    assert label["ssid"] == "BlueSignal Setup"
    assert label["host"] == "wqm1-4fd8"
    assert label["qr"] == "WIFI:T:nopass;S:BlueSignal Setup;;"
    assert "passphrase" not in label

    assert mod.main([serial, "--out-dir", str(tmp_path)]) == 0
    printed = capsys.readouterr().out.split()
    svg_path = tmp_path / "wqm1-4fd8-setup-label.svg"
    pdf_path = tmp_path / "wqm1-4fd8-setup-label.pdf"
    assert printed == [str(svg_path), str(pdf_path)]
    svg = svg_path.read_text(encoding="utf-8")
    pdf = pdf_path.read_bytes()
    assert "BlueSignal Setup" in svg and "No password" in svg
    assert "Password</text>" not in svg
    assert b"WIFI:T:" not in pdf  # the payload is the QR drawing, not a caption
    assert b"BlueSignal Setup" in pdf and b"No password" in pdf
    assert pdf.startswith(b"%PDF-1.4")


def test_an_open_network_qr_says_nopass():
    from utils.wifi_setup import wifi_qr_payload

    assert wifi_qr_payload("BlueSignal Setup", None) == "WIFI:T:nopass;S:BlueSignal Setup;;"
    assert wifi_qr_payload("BlueSignal Setup", "") == "WIFI:T:nopass;S:BlueSignal Setup;;"


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
