Never merge, deploy, push to main, delete data, change pricing, or send email or messages. Open a PR and stop.

# OTA hold and rollback image

**Rule (launch upgrade v2, Leo §10):** no over-the-air release reaches a field unit until a board passes the bench test (`docs/ota-runbook.md` §10) and the Captain approves it. As of 2026-10-12 no field unit has ever received an update.

These are the Captain's levers. An agent documents them and stops.

## The hold, from narrowest to widest

There is **no single fleet-wide switch today.** The hold is made of these levers, each already in the code:

1. **Do not target.** A unit polls for a target; with no target it gets 204 and does nothing (`ota-runbook.md` §3). Holding means: no `POST /v2/devices/:id/firmware/target`.
2. **Clear a target** that was set by mistake (§5.1). That unit does nothing on its next poll.
3. **Yank a release** fleet-wide: `yanked: true` on `firmware_releases/{X_Y_Z}` (§5.2). A yanked release is never served, even to units still targeted at it.
4. **Turn the agent off on a unit:** `ota_enabled: false` in `/etc/bluesignal/config.yaml` (hot-reloaded; §9). The agent stops polling. The cloud remote-config schema does not carry this key today, so it is set on the unit, over SSH or at the bench.

**TODO(JACQUES):** decide whether to add a fleet-wide hold, for example a single `firmware_hold` flag the `/v2/devices/:id/firmware` endpoint checks before answering with any target. It would be a small server change behind the Captain's review. It is not built in this PR.

## Rollback image

- **On the unit:** every update keeps the previous release in `/opt/bluesignal/releases/`, and a failed self-test switches `/opt/bluesignal/current` back automatically (§5.3). Manual switch-back over SSH: §5.4.
- **Golden card image:** the bench image captured from unit #1 (`npm run bench -- golden …` in the marketplace repo, `docs/operations/wqm1-bench-and-site-commissioning.md`). Reflashing a card from it restores a unit that will not boot. **TODO(LEO):** record where the golden image is stored, its sha256 and its firmware version once captured.
- A unit that is offline cannot be rolled back remotely. Plan a site visit.

## Before any first release to the field (checklist)

- [ ] Bench test passed on a production board, including one deliberately corrupted bundle to watch the rollback fire (§10.2).
- [ ] The Captain approved this exact version in writing.
- [ ] One canary unit targeted, watched through one full sample interval.
- [ ] The golden image and its sha256 recorded above.
