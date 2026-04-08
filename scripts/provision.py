#!/usr/bin/env python3
"""
WQM-1 Provisioning Tool — Interactive CLI

Guides a technician through first-boot commissioning:
  1. Display device identity (DevEUI, device ID, BLE name)
  2. Enter LoRaWAN AppKey
  3. Run hardware diagnostics
  4. Generate device label (QR code + identity report)

Usage: sudo python3 /opt/bluesignal/scripts/provision.py
"""

import json
import re
import subprocess
import sys
from pathlib import Path

# Add src to path so we can import identity module
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))

_CONFIG_PATH = "/etc/bluesignal/config.yaml"
_CAL_PATH = "/etc/bluesignal/calibration.yaml"
_PROVISIONED_FLAG = "/etc/bluesignal/.provisioned"
_DIAG_PATHS = [
    "/opt/bluesignal/scripts/diagnostics.sh",
    str(Path(__file__).resolve().parent / "diagnostics.sh"),
]

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _get_identity() -> dict:
    """Get device identity from Pi serial."""
    try:
        from utils.identity import get_ble_name, get_dev_eui, get_device_id

        device_id = get_device_id()
        dev_eui = get_dev_eui().hex().upper()
        ble_name = get_ble_name(device_id)
    except Exception:
        device_id = "BS-WQM1-unknown"
        dev_eui = "0000000000000000"
        ble_name = "BlueSignal-0000"
    return {"device_id": device_id, "dev_eui": dev_eui, "ble_name": ble_name}


def _read_config() -> dict:
    """Read config YAML."""
    try:
        import yaml

        p = Path(_CONFIG_PATH)
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def _write_config(updates: dict) -> None:
    """Merge updates into config file."""
    import yaml

    config = _read_config()
    config.update(updates)
    p = Path(_CONFIG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
    tmp.replace(p)


def _run_diagnostics() -> bool:
    """Run diagnostics.sh and return True if all pass."""
    script = None
    for p in _DIAG_PATHS:
        if Path(p).exists():
            script = p
            break
    if not script:
        print("  diagnostics.sh not found — skipping")
        return True

    result = subprocess.run(["bash", script], capture_output=False, timeout=60)
    return result.returncode == 0


def _generate_qr(identity: dict, output_path: str) -> bool:
    """Generate QR code SVG."""
    try:
        import qrcode
        import qrcode.image.svg

        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(json.dumps(identity))
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgImage)
        with open(output_path, "wb") as f:
            img.save(f)
        return True
    except ImportError:
        print("  qrcode package not installed — skipping QR generation")
        return False
    except Exception as e:
        print(f"  QR generation error: {e}")
        return False


def main() -> int:
    print("=== BlueSignal WQM-1 Provisioning ===")
    print()

    # Step 0: Identity
    identity = _get_identity()
    print(f"  Device ID:  {identity['device_id']}")
    print(f"  DevEUI:     {identity['dev_eui']}")
    print(f"  BLE Name:   {identity['ble_name']}")
    print()

    # Step 1: AppKey
    print("Step 1/4: Enter LoRaWAN AppKey")
    config = _read_config()
    current_key = config.get("app_key", "")
    if current_key and current_key != "00000000000000000000000000000000":
        print(f"  Current AppKey: {current_key[:8]}...{current_key[-8:]}")
        resp = input("  Keep current key? [Y/n]: ").strip().lower()
        if resp not in ("n", "no"):
            print("  Keeping existing AppKey.")
        else:
            current_key = ""

    if not current_key or current_key == "00000000000000000000000000000000":
        while True:
            app_key = input("  AppKey (32 hex chars): ").strip()
            if _HEX32_RE.match(app_key):
                _write_config({"app_key": app_key})
                print("  AppKey saved.")
                break
            print("  Invalid — must be exactly 32 hexadecimal characters.")
    print()

    # Step 2: Hardware diagnostics
    print("Step 2/4: Hardware Verification")
    diag_ok = _run_diagnostics()
    if not diag_ok:
        print()
        resp = input("  Some checks failed. Continue anyway? [y/N]: ").strip().lower()
        if resp not in ("y", "yes"):
            print("  Aborting. Fix issues and re-run provisioning.")
            return 1
    print()

    # Step 3: Generate device label
    print("Step 3/4: Generate Device Label")
    qr_path = "/tmp/wqm1-label.svg"
    if _generate_qr(identity, qr_path):
        print(f"  QR code saved to {qr_path}")

    report_path = "/tmp/wqm1-identity.json"
    with open(report_path, "w") as f:
        json.dump(identity, f, indent=2)
    print(f"  Identity report saved to {report_path}")
    print()

    # Step 4: Mark as provisioned
    print("Step 4/4: Finalize")
    Path(_PROVISIONED_FLAG).parent.mkdir(parents=True, exist_ok=True)
    Path(_PROVISIONED_FLAG).touch()
    print(f"  Provisioned flag set: {_PROVISIONED_FLAG}")
    print()

    print("=== Provisioning Complete! ===")
    print()
    print("Next steps:")
    print("  sudo systemctl restart bluesignal-wqm")
    print("  journalctl -u bluesignal-wqm -f")
    return 0


if __name__ == "__main__":
    sys.exit(main())
