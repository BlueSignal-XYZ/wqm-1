# Engineering rules for agents (set by the Captain, Oct 19, 2026)

## Before writing code
1. **Read what the work touches before changing it.** That means the repo's rules file, the security doc for the area, and any test that pins the behavior. Most breakage came from a refactor that ignored a caller somewhere else.
2. **Write down what must always be true before building a security or money flow.** For example: one owner per device, a claim token works once, nothing locks without a release path. Test those first, then build the handlers.
3. **Read values by importing them, never by searching file text.** A script that regex-searches a source file breaks silently when the value moves.

## Before opening a PR
4. **Any new required setting must already exist before merge.** If a change makes the deploy refuse to run without a variable, confirm the variable is set first, or every deploy behind it is blocked.
5. **Prove it the way CI will.** Run the same commands CI runs, every test group included, and say which ones ran. "Tests pass" with no command listed doesn't count.
6. **One change per PR.** A fix, a refactor and new tests in one PR means a failure in one holds back the other two.
7. **Bring the branch up to date with master before asking for review.**

## After merge
8. **Confirm it actually reached production.** A green check on the PR is not a deploy. Check that the deploy finished and the new code is live.
9. **Never let the website ship ahead of its backend.** If the backend deploy fails, the website deploy stops too.
10. **A failure that makes no noise is the worst kind.** Any check or job whose purpose is to warn must fail visibly when it can't run. A job that skips itself and shows green is not allowed.

## Ownership
11. **An agent finishes its own PRs or closes them.** Anything still a draft three days later gets folded into another PR or closed with a reason.
12. **Red CI never merges, whoever authored the PR,** Dependabot included. Enforced by branch protection.
13. **Agents never take hard-to-undo actions:** no merging, deploying, deleting data, changing prices or sending email. Only the Captain does those.
14. **When an agent can't verify something, it says so.** A list of what it didn't check is worth more than a confident summary.

## Enforcement
- CI or branch protection enforces rules 4, 5, 9, 10 and 12.
- Review enforces rules 1, 2, 6 and 14.

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
