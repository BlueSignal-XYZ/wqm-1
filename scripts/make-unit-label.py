#!/usr/bin/env python3
"""Print a WQM-1 setup-hotspot label from a Pi serial.

Bench machine only. The label carries the setup SSID, the derived
passphrase, and a Wi-Fi QR code. It writes SVG and PDF files and prints
those paths. It does not log the passphrase, and the firmware never runs it.

    python3 scripts/make-unit-label.py 1000000086544fd8 --out-dir ./labels
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import qrcode

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from utils.identity import _normalise, ap_name, get_derived_id  # noqa: E402
from utils.netctl import derived_passphrase  # noqa: E402
from utils.wifi_setup import wifi_qr_payload  # noqa: E402


def build_label(pi_serial: str) -> dict[str, str]:
    """SSID, passphrase, and QR payload for one Pi serial.

    The serial is normalised the same way the unit reads ``/proc/cpuinfo``,
    so the passphrase matches the hotspot that unit will raise.
    """
    serial = _normalise(pi_serial)
    if not serial or set(serial) == {"0"}:
        raise ValueError("Pass the Pi serial from /proc/cpuinfo (16 hex digits).")
    ssid = ap_name(get_derived_id(serial))
    passphrase = derived_passphrase(serial)
    return {
        "serial": serial,
        "ssid": ssid,
        "passphrase": passphrase,
        "qr": wifi_qr_payload(ssid, passphrase),
    }


def _qr_matrix(payload: str) -> list[list[bool]]:
    code = qrcode.QRCode(border=2, box_size=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(payload)
    code.make(fit=True)
    return [[bool(cell) for cell in row] for row in code.get_matrix()]


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_svg(label: dict[str, str]) -> str:
    """A printable label: name, passphrase, and the Wi-Fi QR."""
    matrix = _qr_matrix(label["qr"])
    module = 4
    origin = 248
    rects: list[str] = []
    for y, row in enumerate(matrix):
        for x, on in enumerate(row):
            if not on:
                continue
            rects.append(
                f'<rect x="{origin + x * module}" y="{28 + y * module}" '
                f'width="{module}" height="{module}"/>'
            )
    qr = "\n  ".join(rects)
    ssid = _xml(label["ssid"])
    passphrase = _xml(label["passphrase"])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="420" height="200" '
        'viewBox="0 0 420 200">\n'
        '<rect width="420" height="200" fill="white"/>\n'
        '<text x="16" y="36" font-family="sans-serif" font-size="14">WQM-1 setup Wi-Fi</text>\n'
        f'<text x="16" y="78" font-family="sans-serif" font-size="22">{ssid}</text>\n'
        '<text x="16" y="112" font-family="sans-serif" font-size="12">Password</text>\n'
        f'<text x="16" y="140" font-family="monospace" font-size="18">{passphrase}</text>\n'
        '<g fill="black">\n'
        f"  {qr}\n"
        "</g>\n"
        "</svg>\n"
    )


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_pdf(label: dict[str, str]) -> bytes:
    """A one-page label. Text is the SSID and passphrase; the QR is drawn."""
    matrix = _qr_matrix(label["qr"])
    module = 3
    origin_x = 300
    origin_y = 36
    height = len(matrix) * module
    parts = [
        "BT /F1 12 Tf 28 200 Td (WQM-1 setup Wi-Fi) Tj ET",
        f"BT /F1 18 Tf 28 160 Td ({_pdf_escape(label['ssid'])}) Tj ET",
        "BT /F1 11 Tf 28 128 Td (Password) Tj ET",
        f"BT /F1 16 Tf 28 104 Td ({_pdf_escape(label['passphrase'])}) Tj ET",
    ]
    for y, row in enumerate(matrix):
        for x, on in enumerate(row):
            if not on:
                continue
            px = origin_x + x * module
            py = origin_y + height - (y + 1) * module
            parts.append(f"{px} {py} {module} {module} re f")
    stream = ("\n".join(parts) + "\n").encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 432 240] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"endstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode(
            "ascii"
        )
    )
    return bytes(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a printable WQM-1 hotspot label (SVG and PDF) from a Pi serial."
    )
    parser.add_argument("serial", help="Pi serial from /proc/cpuinfo")
    parser.add_argument(
        "--out-dir",
        default=".",
        help="Directory for the SVG and PDF (created if needed)",
    )
    args = parser.parse_args(argv)
    try:
        label = build_label(args.serial)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    svg_path = out / f"{label['ssid']}-hotspot-label.svg"
    pdf_path = out / f"{label['ssid']}-hotspot-label.pdf"
    svg_path.write_text(render_svg(label), encoding="utf-8")
    pdf_path.write_bytes(render_pdf(label))
    # Paths only. The passphrase stays in the label files, not in this output.
    print(svg_path)
    print(pdf_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
