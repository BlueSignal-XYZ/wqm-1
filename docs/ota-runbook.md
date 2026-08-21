# OTA Firmware Updates — Operator Runbook

How WQM-1 firmware releases are built, signed, published, targeted, watched,
rolled back, and how the signing key rotates. Design rationale lives in the
marketplace repo at `docs/architecture/ota-firmware-updates.md`; this document
is the hands-on procedure.

Placeholders used throughout:

- `$API_BASE` — `https://us-central1-waterquality-trading.cloudfunctions.net/app`
- `$ADMIN_ID_TOKEN` — Firebase ID token of a user with `role == admin`
- `$DEVICE_ID` — e.g. `BS-WQM1-abc123def456`
- `<bucket>` — the project's private Cloud Storage bucket

## 1. How a release is built and signed

Pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`, which:

1. **Refuses to build** unless `tag == VERSION file == pyproject.toml` version.
2. Builds `wqm1-firmware-X.Y.Z.tar.gz` — repo files at the **top level** of
   the archive (no wrapping directory): `src/ config/ scripts/ systemd/
   requirements.txt VERSION setup.sh`. The device extracts it straight into
   `/opt/bluesignal/releases/X.Y.Z/`.
3. Generates `manifest.json`:
   `{product, version, channel, sha256, sizeBytes, minFromVersion, keyId,
   createdAt, requiresDeps, notes}` — `minFromVersion` comes from an optional
   repo-root `MIN_FROM_VERSION` file (default `2.0.0`); `notes` comes from the
   annotated tag message.
4. Signs the **exact manifest bytes** with Ed25519 using the `OTA_SIGNING_KEY`
   repo secret (`keyId: ota-2026`) and self-verifies the signature.
5. Attaches `wqm1-firmware-X.Y.Z.tar.gz`, `manifest.json`, `manifest.sig`
   (base64), and `checksums.sha256` to the GitHub release, and uploads a
   `publish-payload.json` workflow artifact — the ready-made body for the
   publish call below.

The signature is over the verbatim manifest string; the manifest pins the
tarball's sha256, so signing the manifest transitively authenticates the
bundle. Devices verify signature → manifest → hash, in that order.

## 2. Publishing a release to the fleet

The artifact is signed but not visible to devices until uploaded + registered.

**2.1 Upload to the private Storage bucket** (path must match
`publish-payload.json`'s `storagePath`):

```bash
gsutil cp wqm1-firmware-X.Y.Z.tar.gz \
  "gs://<bucket>/firmware/wqm1/X.Y.Z/wqm1-firmware-X.Y.Z.tar.gz"
```

The bucket stays private — devices receive short-lived signed URLs from the
poll endpoint, never a public object.

**2.2 Register the release** (server re-verifies the signature):

```bash
curl -X POST "$API_BASE/v2/firmware/releases" \
  -H "Authorization: Bearer $ADMIN_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d @publish-payload.json
```

`publish-payload.json` is the `ota-publish-payload` artifact from the release
workflow run: `{"manifest": "<exact signed string>", "signature": "<base64>",
"storagePath": "firmware/wqm1/X.Y.Z/wqm1-firmware-X.Y.Z.tar.gz"}`.

## 3. Targeting a device

Master first, always — the site master is the canary. Assign one device,
watch it soak, then do the rest.

```bash
# Assign
curl -X POST "$API_BASE/v2/devices/$DEVICE_ID/firmware/target" \
  -H "Authorization: Bearer $ADMIN_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"version": "X.Y.Z"}'

# Clear (un-assign)
curl -X POST "$API_BASE/v2/devices/$DEVICE_ID/firmware/target" \
  -H "Authorization: Bearer $ADMIN_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"version": null}'
```

The device polls every `ota_poll_s` (15 min default, ±20% jitter). To make it
poll immediately, touch the flag file on the device (or send the `ota_check`
device command — the firmware touches the same file):

```bash
sudo touch /var/lib/bluesignal/ota-check-now
```

## 4. Watching an update

- **RTDB `devices/{id}/otaStatus`** — `{phase, version, error?, updatedAt}`.
  Phase order on success: `downloading → verifying → applying → self_test →
  success`. Failures land as `verify_failed`, `failed`, or `rolled_back` with
  a reason in `error`.
- **RTDB `device_events`** — append-only `ota_*` audit trail of every
  transition (mirrors the creditAuditLog pattern).
- **RTDB `devices/{id}/otaLastCheckAt`** — the OTA agent's poll heartbeat; if
  it goes stale the agent itself is down (check `journalctl -u bluesignal-ota`).
- On success the server promotes `devices/{id}/firmwareVersion` and clears
  `otaTarget` automatically.
- On device: `journalctl -u bluesignal-ota -f` and
  `/var/log/bluesignal/ota.log`; last outcome in
  `/var/lib/bluesignal/ota-state.json`.

The self-test requires `bluesignal-wqm.service` active **and** a fresh sensor
reading in SQLite within `ota_self_test_timeout_s` (600 s default). If either
fails, the device rolls itself back — no cloud involvement needed.

## 5. Rollback procedures

**5.1 Stop a rollout (device hasn't applied yet):** clear the target (see
§3). A yanked/cleared target returns 204 on the next poll and nothing happens.

**5.2 Yank a bad release (fleet-wide):** set `yanked: true` on
`firmware_releases/{X_Y_Z}` (RTDB, version key uses `_` for `.`). Yanked
releases are never served, even to devices still targeted at them. Then
re-target affected devices at the last good version — `minFromVersion` on the
old release must allow it (this is why `minFromVersion` should only be raised
when an actual security floor demands it).

**5.3 Automatic on-device rollback:** failed verification never touches the
running install. A failed self-test flips `/opt/bluesignal/current` back to
the previous release, restarts services, and reports `rolled_back` (the bad
release directory is kept for post-mortem). If the agent dies mid-apply, the
boot-time safety net in the agent detects the interrupted state file and rolls
back on next start.

**5.4 Manual on-device rollback (break-glass, SSH):**

```bash
ls -l /opt/bluesignal/releases/          # see what's installed
sudo ln -s /opt/bluesignal/releases/<good-version> /opt/bluesignal/current.tmp
sudo mv -T /opt/bluesignal/current.tmp /opt/bluesignal/current
sudo systemctl restart bluesignal-wqm.service bluesignal-service-window.service
```

## 6. Key management and rotation

- The **private key** exists only as the `OTA_SIGNING_KEY` GitHub Actions
  secret. It is never in the repo, never on a device, never in an artifact.
- The **public key** is committed as `config/ota_public_key.pem` and ships
  inside every bundle; devices read it from
  `/opt/bluesignal/current/config/ota_public_key.pem`.
- Generate a pair: `python3 scripts/ota-generate-keys.py` (add `--out-private`
  to write the private PEM to a file instead of stdout).

**Rotation (`keyId` bump):**

1. Generate the new pair. Do NOT replace the CI secret yet.
2. Commit the new public key as `config/ota_public_key.pem` and bump the
   manifest `keyId` in `.github/workflows/release.yml` (e.g. `ota-2027`).
3. Ship a release **signed by the OLD key** that carries the NEW public key.
   Devices verify it with the old key and, once applied, trust the new one.
4. After the fleet is on that release, replace `OTA_SIGNING_KEY` with the new
   private key. Subsequent releases verify against the new public key.
5. If the old key is suspected compromised, skip the overlap: re-provision the
   public key over SSH (break-glass) and rotate the secret immediately.

## 7. v2.0.0 field bootstrap (one-time, per device)

Devices below 2.0.0 predate the OTA agent and the `releases/current` layout,
so the first 2.0.0 install is manual:

1. SSH to the device, clone/copy the v2.0.0 tree, and run `bash setup.sh`.
   The script migrates the legacy flat `/opt/bluesignal/src` install to
   `/opt/bluesignal/releases/legacy-1.1.0/` + `current` symlink, installs
   2.0.0 as `releases/2.0.0`, rewrites the systemd units to run from
   `current/src`, and installs + enables `bluesignal-ota.service`.
2. Confirm `config/ota_public_key.pem` landed in the release (setup.sh warns
   if missing) and that the device has its API key provisioned
   (`api_key` in `/etc/bluesignal/config.yaml`).
3. Verify the agent is polling: `journalctl -u bluesignal-ota -f` and
   `devices/{id}/otaLastCheckAt` in RTDB.

**After this bootstrap, SSH is break-glass only.** All routine firmware
changes go through the signed OTA path — an unsigned change made over SSH is
invisible to the audit trail and gets clobbered by the next release anyway.

## 8. Remote host reboot (recovery)

The OTA agent has a second job: it is the only root-resident process on a unit,
so it is what performs a **host reboot** requested from the Service Window
(Settings → Reboot the unit, typed confirmation required).

This exists because SSH is not always the break-glass path it is assumed to be.
A unit seen in the field on 2026-08-02 had an sshd that reset every connection
(`kex_exchange_identification: Connection reset by peer`, a dirty filesystem
after a hot SD-card pull) while the Service Window on :8080 kept serving fine —
it was already resident in memory. With SSH dead there was no remote recovery
at all: only a site visit or a re-image.

**Privilege path — no new sudo rights.** The firmware and the Service Window
both run as the install user and stay that way:

1. Service Window POSTs `reboot` to the firmware's command socket.
2. The firmware drives the relays to fail-safe (`RelayController.all_off()`)
   and touches `/var/lib/bluesignal/reboot-request`.
3. The OTA agent (root) sees the flag between poll cycles — never mid-apply —
   removes it, and runs `systemctl reboot`.
4. systemd SIGTERMs the firmware on the way down, which drops the coils a
   second time via `_shutdown()` and the `atexit` handler.

Requests older than 5 minutes are ignored, so a flag that outlived a power cut
cannot reboot a unit out of nowhere; the flag is removed *before* the reboot is
ordered, so a reboot that fails cannot loop. When OTA polling is off (disabled,
or the unit is not commissioned) the agent no longer exits outright — it watches
for reboot requests for 30 s, then exits so `Restart=always` re-reads the
settings, which keeps the reboot button working with a worst-case latency of
about half a minute.

Operating notes:

```bash
journalctl -u bluesignal-ota -f          # "Rebooting host on request..."
sudo touch /var/lib/bluesignal/reboot-request   # same thing, by hand
```

If the button reports success and nothing happens, the agent is down — check
`systemctl status bluesignal-ota`. A reboot is not a substitute for a rollback:
if a unit is unhealthy after an update, use §5.

## 9. Device-side settings reference

All in `/etc/bluesignal/config.yaml` (schema-validated; hot-reloadable):

| Setting | Default | Meaning |
| --- | --- | --- |
| `ota_enabled` | `true` | Master switch; agent stops polling when false (still serves reboot requests, §8) |
| `ota_poll_s` | `900` | Poll interval, ±20% jitter |
| `ota_max_bundle_bytes` | `67108864` | Hard bundle size cap (64 MB) |
| `ota_self_test_timeout_s` | `600` | Self-test window before rollback |
| `ota_keep_releases` | `2` | Release dirs retained (current + previous never pruned) |

## 10. First release: what "successful" actually means

Written 2026-08-21, before the pipeline's maiden run. Every step above had been
built and none of it had ever executed against a real device — there were zero
tags in this repo, so `OTA_SIGNING_KEY` had never signed anything and the
rollback path had never rolled anything back. That is the context this section
exists for.

### 10.1 The distinction that matters

The design fails safe:

- **Verification precedes any change.** Signature → manifest → tarball hash, in
  that order. A bundle that fails any of them never touches the running install.
- **The self-test is the real gate.** After the symlink flips, the device
  requires `bluesignal-wqm.service` active AND a fresh sensor reading in SQLite
  within `ota_self_test_timeout_s`. Either failure flips `current` back,
  restarts, and reports `rolled_back` — no cloud involvement.

So a bad *release* is well handled. What is untested is the *agent* — the thing
that performs those steps. Do not read "the design is safe" as "the first run is
safe": those are different claims, and only the first one has evidence.

### 10.2 Gates before publishing

1. **Bench-soak on a lab Pi.** One successful OTA, and one deliberately
   corrupted bundle to watch the rollback fire. This tests the safety net rather
   than assuming it, and it is the only item here that cannot be done after the
   fact.
2. **Confirm the agent is alive on the target.** `devices/{id}/otaLastCheckAt`
   should be newer than roughly one `ota_poll_s`. Absent or stale means the OTA
   agent is not running — publishing will succeed and reach nothing. A unit that
   has never been through `setup.sh` has no `releases/`+`current` layout and
   cannot apply an update at all.
3. **Check the upgrade floor.** `minFromVersion` on the new release must allow
   the version the device is on. With no `MIN_FROM_VERSION` file it defaults to
   2.0.0.

### 10.3 Acceptance is about the CHANGE, not the mechanism

"The OTA succeeded" is not the test. `otaStatus: success` only says the bundle
applied and the service came back — it says nothing about whether the release
did what it was cut to do, and a green mechanism reporting on itself is exactly
the failure mode this codebase keeps rediscovering.

Write the acceptance criterion from the release notes before you publish. For
v2.1.1 it was:

> Within one sample interval of `firmwareVersion` promoting to 2.1.1, the target
> device sends `tds` as either a real number or
> `{value: null, status: "no_conduction"}`.

Either outcome ends the ambiguity the release was cut to end — clean water under
the old 80 ppm floor, or a probe that genuinely reads nothing. **If neither
appears, the firmware did not take, whatever `otaStatus` says.**

### 10.4 If it goes wrong

In increasing order of reach: clear the target (§3) stops one device; yank the
release (§5.2) stops the fleet; the device's own rollback (§5.3) covers the
apply step; SSH is the backstop. None of these depend on the others working.
