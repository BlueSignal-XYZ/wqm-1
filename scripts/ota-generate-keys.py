#!/usr/bin/env python3
"""
Generate an Ed25519 keypair for OTA firmware release signing.

Usage:
    python3 scripts/ota-generate-keys.py
    python3 scripts/ota-generate-keys.py --out-private /secure/path/ota-signing-key.pem

The PRIVATE key becomes the GitHub Actions secret ``OTA_SIGNING_KEY`` (the
release workflow signs the manifest with it on tag push). It is printed to
stdout by default and only written to disk when --out-private is given.
NEVER commit it.

The PUBLIC key is committed as ``config/ota_public_key.pem`` — it ships inside
every firmware bundle and is what devices verify manifests against.

Key rotation: generate a new pair, commit the new public key alongside a new
``keyId`` (e.g. ota-2027), ship a release signed by the OLD key that carries
the NEW public key, then switch the CI secret. See docs/ota-runbook.md.
"""

import argparse
import os
import sys
from pathlib import Path

from Crypto.PublicKey import ECC


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an Ed25519 OTA signing keypair")
    parser.add_argument(
        "--out-private",
        metavar="PATH",
        help="write the private key PEM to PATH (mode 600) instead of printing it",
    )
    args = parser.parse_args()

    key = ECC.generate(curve="Ed25519")
    private_pem = key.export_key(format="PEM")
    public_pem = key.public_key().export_key(format="PEM")

    if args.out_private:
        out = Path(args.out_private)
        out.write_text(private_pem + "\n")
        os.chmod(out, 0o600)
        print(f"Private key written to {out} (mode 600). NEVER commit this file.")
    else:
        print("=== PRIVATE KEY — set as the OTA_SIGNING_KEY CI secret; NEVER commit ===")
        print(private_pem)
        print()

    print("=== PUBLIC KEY — commit as config/ota_public_key.pem ===")
    print(public_pem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
