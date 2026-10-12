Never merge, deploy, push to main, delete data, change pricing, or send email or messages. Open a PR and stop.

# AGENTS.md — WQM-1 firmware

Read this before you change anything. It is written for an agent with no memory of earlier work. A unit in the field cannot be fixed by a website rollback: if it goes offline, someone drives to it.

## What this repo is

Firmware for the BlueSignal WQM-1 water-quality monitor: a Raspberry Pi (or a supported host board, `docs/platforms.md`) on the WQM-1 HAT. It reads pH, TDS, turbidity and temperature (optional RS485 probes add ORP, chlorine and conductivity), stores readings in SQLite, uploads them to BlueSignal Cloud, and runs the on-device Service Window and the OTA agent. Hardware design files are in `hardware/`.

The current release is in `VERSION` (2.4.0). The marketing site reads the same number from `src/shared/site/specs.json` in the marketplace repo; keep them in step through a PR there.

## How to run and test

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest tests/ -q            # no hardware needed; hardware-marked tests skip
```

A simulated unit: `docs/getting-started.md` and `docs/site-flow-v2.md` (no hardware needed).

CI (`.github/workflows/ci.yml`) runs ruff, the tests, and the signing checks. `release.yml` builds, signs and publishes a release from a tag. Pushing a tag is the Captain's.

## Dangerous files (CODEOWNERS routes these to the Captain)

- `src/ota/`, `config/ota_public_key.pem`, `.github/workflows/release.yml`, `docs/ota-runbook.md`: the update path to field units.
- `src/radio/`: LoRaWAN keys and radio.
- `src/utils/config.py` `SETTINGS_SCHEMA`: what the cloud may set remotely.
- `setup.sh`, `systemd/`: what a fresh unit installs and runs.
- `VERSION`.

## Rules

- **No over-the-air release reaches a field unit until a board passes the bench test and the Captain approves it.** An agent never targets, publishes or yanks a release; the hold switch and the rollback image are in `docs/ota-hold.md`.
- Never write to a live device, its config or its record in Cloud. The live unit `BS-WQM1-0000a92e4e7d` is grandfathered; nothing may change its path.
- Never commit a key. The OTA private key exists only as the `OTA_SIGNING_KEY` Actions secret.
- Wording in docs and the README follows the marketplace `AGENTS.md`: the WQM-1 is "designed to connect with" water-from-air and rainwater systems, never "controls" or "integrates"; no safe/potable claims; no UL/ETL/NSF/FCC claim for our own unit; never the factory supplier's name.
- Verify before claiming: call something "done" only with proof attached (test output, a log, a photo of the bench). Anything not checked is "unverified".

## How to finish

Run ruff and pytest, open a PR, and stop. The Captain merges, tags and releases.
