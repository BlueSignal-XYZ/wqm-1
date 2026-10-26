# Install v3: firmware build plan

**Stack:** this stacks on #155 (base `claude/loving-lamport-hq4t3a`). The
Captain merges #155 first or retargets. Not checked against master.

**Status:** plan, 2026-10-24. Written by Claude for Grok under the `AGENTS.md`
handoff. Revised the same day after the review on PR #155 to match the
Captain's ruling: setup is Wi-Fi only with the LEDs as the guide; there is
no PIN anywhere (not on the hotspot, not later); owners use BlueSignal
Cloud only; the unit's local page stays off; the old wizard, PIN, sensors,
QR and claim-token steps are obsolete. Revised again the same day after
the Captain answered Decisions 2 and 3: the Service Window stays running
but binds loopback only; no mutating route answers from the home
network, so §11 is skipped. Nothing here is built yet.

**What it builds:** the firmware half of the install spec,
`marketplace/docs/operations/wqm1-install-spec.md` (marketplace PR #1457).
Read the spec first; build item IDs (W1–W9) and situation IDs (A1–H4) refer
to it. The Cloud half is `marketplace/docs/operations/wqm1-install-build-plan.md`.

**The install, in one line:** plug in, join the open network "BlueSignal
Setup", type the home Wi-Fi password, then finish in Cloud. The unit's
setup network does Wi-Fi and nothing else. Lights tell the installer what
to do. The unit does not serve a local page on the home network.

**Each section below is one PR**, in the order given. Open each PR with
`Plan: docs/install-build-plan.md §<n>` in its body and the label
`needs-review`. Do not combine sections (AGENTS rule 6).

---

## Rules for every PR in this plan

- **The live unit `BS-WQM1-0000a92e4e7d` is not touched.** It runs old
  firmware, receives no OTA, and has no identity file. Nothing here may
  change the derived-identity path (`identity.get_derived_id`) or what a
  card with no identity file does at boot.
- **No release, tag or OTA.** `VERSION` and tags are the Captain's. The ten
  bench units get this firmware on their cards through the golden image
  (`tools/golden/`), after the bench pass in the spec's section 5.
- **CODEOWNERS files** (`setup.sh`, `systemd/`, `src/utils/config.py`
  `SETTINGS_SCHEMA`) are touched by §2 (the existing clock sudoers line in
  `setup.sh`, only if that PR has to change it), §6 and §7 (`setup.sh` and
  `systemd/`). Say so in the PR body; the Captain reviews those lines.
  `SETTINGS_SCHEMA` is not named by any section below. If a later decision
  (see the end of this plan) requires it, that PR says so. §10 may touch
  `src/service_window/__main__.py` and
  `systemd/bluesignal-service-window.service` for the loopback bind
  (Decision 2, 24 Oct 2026), and `setup.sh` for the printed LAN URLs.
- **Prove it the way CI does:** `ruff check . && ruff format --check . &&
  pytest tests/ -q`. Paste the command and the summary line in the PR.
- **Say what you did not verify.** Most of this needs a Pi and a phone; the
  tests use fakes. Each section lists its bench check.

## What must always be true

Every PR keeps all of these, and the tests named below pin them.

| # | Invariant | Pinned by |
|---|---|---|
| I1 | A client on the setup network (192.168.4.0/24) can reach one thing: the Wi-Fi page. Nothing that changes the unit, other than its Wi-Fi, answers there. | §4 tests; §5 tests; §10 tests |
| I2 | The home Wi-Fi password is written only into the NetworkManager profile (root-only). Never into a log, a state file, the boot partition, a heartbeat or Cloud. | §2, §4 tests (capture logs and files, search for the password) |
| I3 | A network that has reached BlueSignal is never deleted by the unit until another network has reached BlueSignal. | §2, §6 tests |
| I4 | At most one join runs at a time. | §2 test |
| I5 | No page the unit serves reloads itself, navigates on a timer, or replaces a form. A status change edits text in place. | §4 tests (the port-80 page); §12 tests (any leftover Service Window template) |
| I6 | Every failure ends with the setup network up and the lights at "ready", unless a proven network exists and is being retried. | §2, §6 tests |
| I7 | The setup network and its page work without the firmware (`bluesignal-wqm`), the Service Window or Cloud running. | §4 test (portal answers with both stopped) |

---

## §1. Device key race (fix first)

**Why:** `utils/device_keys.py` `ensure_keypair()` writes every new key
through one fixed temp name (`<key>.tmp`, line 133). Two processes creating
the key at once (the firmware and the Service Window both call it at first
boot) clobber each other's temp file. A repro in this session ran 300
concurrent trials: 298 raised `FileNotFoundError` and 1 returned a public
key that was not the one on disk. Registration signs with that key, so a
unit can register a key it does not hold.

**Change:** take an exclusive `fcntl.flock` on `<key>.lock` (same directory,
mode 0600) around the whole read-or-create in `ensure_keypair()`, and
re-read the key file after the lock is held. Keep the fixed `.tmp` name and
`_promote_tmp()` crash recovery unchanged; under the lock only one writer
exists.

**Files:** `src/utils/device_keys.py`, a new `tests/test_device_keys.py` (today the key is only exercised inside `tests/test_device_onboarding_v2.py`).

**Tests:**
- 8 processes (multiprocessing) call `ensure_keypair()` on an empty
  directory at once: every call returns the same public key, it equals the
  key on disk, and no call raises. Run it 50 times in the test.
- An existing valid key file is returned unchanged without being rewritten
  (mtime unchanged).
- A leftover valid `.tmp` is still promoted (existing test keeps passing).

**Done:** the concurrent test passes 50 of 50 runs; it fails on master.

**Trap:** the lock file must not be deleted after use. Deleting it lets a
third process lock a different inode.

---

## §2. The join: one at a time, proven by reaching BlueSignal (W3, W7)

Build this before the page (§4), so the page has a join to call.

The old wizard's network step can call the new join until §9 deletes the
wizard. That is not the same as the wizard remaining usable from the setup
network. From the 8080 block (§5) onward the old wizard is intentionally
unreachable from `192.168.4.0/24`. Do not read this section as "the current
wizard keeps working in the meantime" on BlueSignal Setup: it does not,
once §5 has merged.

**New module `src/utils/join.py`**, pure orchestration over `netctl`:

- `start_join(ssid, password, *, hidden=False) -> dict`: takes the lock,
  writes the join state, starts a worker thread, and returns at once with
  `{"state": "joining", "ssid": ...}`. A second call while one runs returns
  `{"state": "busy", "ssid": <the running one>}` and starts nothing (B7).
- The worker waits 1.5 s before touching the radio, so the HTTP response
  that started it reaches the phone before the setup network goes down.
- `read_state() -> dict`: the last join, from
  `/var/lib/bluesignal/join-state.json` (mode 0600, atomic write). Fields:
  `state` (idle | joining | joined | failed), `ssid`, `reason` (the existing
  `_STATION_FAIL` kinds plus `no_bluesignal`), `message` (the existing
  sentence), `at`. No password, ever (I2). This replaces
  `current_app.config["LAST_JOIN"]` in `routes/setup.py`, which lives in
  process memory: a restart, or a second phone talking to another process,
  loses it (D3).

**Changes in `src/utils/netctl.py`:**

1. **Try under a new profile name.** A join creates the NetworkManager
   profile as `bs-try-<8 hex>`, never as the SSID. On success the profile
   is renamed to the SSID and any older profile for the same SSID is
   deleted. On failure only the `bs-try-` profile is deleted.
   Today `_fail_join()` runs `nmcli connection delete <ssid>` (line 1115),
   which deletes the proven profile when a re-typed password for the same
   network is wrong. That breaks I3.
2. **Success means BlueSignal answered.** After association and a usable
   IPv4 address, `reach_bluesignal()` makes an HTTPS request to the host of
   `cloud_api_base` with certificate verification on. Any HTTP status
   counts; it proves DNS, routing and TLS through this network. Keep the
   gateway/DNS probe as the first step, so `no_internet` stays a distinct
   reason.
3. **Three-minute hold (D5).** If the unit has an address but BlueSignal does
   not answer, keep trying every 10 s for 180 s before failing with
   `no_bluesignal`. A router that asks the owner to approve new devices
   (Eero, Firewalla) is approved in that window without anyone redoing the
   join.
4. **Clock fix (D6).** If TLS fails with a certificate-time error and
   timesyncd has not synced, fetch `http://<api host>/` over plain HTTP,
   read the `Date` header, and set the clock forward only, through the
   existing `utils/clock.py` path (`sudo -n date -u -s`, one sudoers line
   from `setup.sh`). Then retry the HTTPS request once. Never move the
   clock backwards.
5. **Forget older networks only after success** (rule 7 of the spec). The
   existing `forget_saved_wifi(keep=ssid)` moves from `_finish_setup` in
   `routes/setup.py` into the success path of the worker, after
   `reach_bluesignal()` returns.
6. **Last five attempts on the card (W7).** Append each attempt to the boot
   status file (`diagnostics/boot_status.py`) as one line: time, SSID,
   reason. Keep five. The existing publisher already refuses a file that
   contains a passphrase; add the home password to its refuse list for the
   duration of the write.

**Files:** `src/utils/join.py` (new), `src/utils/netctl.py`,
`src/diagnostics/boot_status.py`, `src/service_window/routes/setup.py` (call
`join.start_join` / `join.read_state` instead of `join_network` and
`LAST_JOIN`), `tests/test_join.py` (new), existing netctl tests.

**Tests (fakes for nmcli, clock and HTTPS):**
- Wrong password: state is `failed`, reason `wrong_password`, and a new
  `join.read_state()` in a fresh import (simulated restart) still says so.
- Same SSID already proven, wrong password typed: the proven profile still
  exists afterwards (I3). This test fails on master.
- Address but BlueSignal blocked for 120 s, then reachable: joined, no
  failure written. Blocked for 200 s: `no_bluesignal` after 180 s.
- Clock 2 years behind, TLS time error: clock set forward from the Date
  header, retry succeeds. Clock ahead of the header: clock not moved.
- Two `start_join` calls 100 ms apart: the second returns `busy` with the
  first SSID (I4).
- The password string appears in no log record (caplog), no file under the
  state and boot directories, and no `read_state()` field (I2).
- Older saved networks are deleted only after `reach_bluesignal()` succeeds.

**Done:** all of the above pass. Until §9 deletes it, the old wizard's
network step still joins through the new path when a test client (or a
browser not on the setup network) reaches it. After §5, a phone on
BlueSignal Setup cannot open that wizard; that is intended.

**Bench check:** wrong password, then right; a router with client approval
(or a firewall rule blocking the API host for 2 minutes).

**Traps:**
- The worker thread must not die with the HTTP server. In §4 the port-80
  listener closes the moment the setup network goes down; the worker runs
  in the same process and must keep running after the socket closes.
- `nmcli connection modify <id> connection.id <ssid>` renames; do not
  re-create the profile (that drops the saved password).
- Hidden networks (C3): the `bs-try-` profile needs `wifi.hidden yes`.

---

## §3. Open "BlueSignal Setup" plus identity and firstboot (W1 name)

One PR. Do not fold the port-80 page or the 8080 block into this change
(AGENTS rule 6).

- `utils/identity.py`: `SETUP_SSID = "BlueSignal Setup"`. `ap_name()`
  returns it, for every unit and every argument. Add `host_name(device_id=None)`
  returning today's rule (`wqm1-` + last four, lowercase), for the hostname
  only.
- `tools/golden/overlay/usr/local/sbin/wqm1-firstboot` line 57: call
  `host_name()`. **If it keeps calling `ap_name()`, the regex check fails
  and the script silently falls back to the Pi serial**, so every unit gets
  a hostname from its Pi instead of its label. Add a test that runs the
  script (it already has `WQM1_*` overrides for tests) against a label
  identity file and expects `wqm1-0001`.
- `netctl.hostapd_config(ssid, passphrase=None)`: with no passphrase, emit
  no `wpa`, `wpa_key_mgmt`, `wpa_passphrase` or `rsn_pairwise` lines (an
  open network). `start_ap()` accepts `passphrase=None`; it still refuses a
  non-empty passphrase under 8 characters. Rewrite the docstrings that say
  "an open AP into a PIN-gated page is still an open AP": the premise is
  gone because the page sets only Wi-Fi (spec decision 1) and there is no
  PIN anywhere.
- `ap_credentials()` returns `(SETUP_SSID, None)`. Remove the passphrase
  from `join_network`'s `ap_passphrase`, `_restore_ap`,
  `scripts/wqm1-ap-fallback.py`, `diagnostics/ap_fail.py` (keep its
  generic `psk=` redaction), `scripts/make-unit-label.py`,
  `scripts/demo-unit.py`, `scripts/simulate-fleet.py`, `utils/netsim.py`.
  `derived_passphrase()` is deleted with its callers
  (`scripts/wqm1-ap-fallback.py`, `scripts/make-unit-label.py`) and tests
  (`tests/test_netctl.py`, `tests/test_unit_label.py`).
- OWE (Enhanced Open) is **not** in this PR. Whether the BCM43430 offers it
  in AP mode is a bench question; add it later as a separate PR if it does.

**Files:** `src/utils/identity.py`, `src/utils/netctl.py`,
`tools/golden/overlay/usr/local/sbin/wqm1-firstboot`,
the scripts and diagnostics named above, `tests/test_netctl.py`,
`tests/test_ap_unit_layout.py`, `tests/test_simulate.py`,
`tests/test_netinfo.py`, `tests/test_unit_label.py`.

**Tests:**
- `ap_name()` is "BlueSignal Setup" for a label, a derived id and a
  simulated id; `host_name()` is `wqm1-0001` for `WQM-10001`.
- `hostapd_config(ssid, None)` contains no `wpa` line; with an 8-character
  passphrase it is unchanged from today.
- The firstboot script test named above.

**Done:** a simulated unit advertises "BlueSignal Setup" as an open
network; `host_name()` is what firstboot writes.

**Bench check:** the phone's Wi-Fi list shows an open network named
"BlueSignal Setup". Opening the page is §4.

**Trap:** firstboot must call `host_name()`, not `ap_name()`. See the
silent serial fallback above.

---

## §4. The Wi-Fi page on port 80, without the proxy (W2)

One PR. Depends on §2 (a join to call) and §3 (the open setup network).

**Decision: the port-80 listener renders the page itself.** Today
`utils/captive_portal.py` proxies every request to the Service Window
(Flask, port 8080), which takes minutes to start on a Pi Zero; that is why a
"starting up" page with a 3-second refresh exists, and why the wizard page
reloaded every 5 s (`setup_base.html:55`). The proxy also hides the phone's
address (the Service Window sees 127.0.0.1), which broke the hotspot checks.
Serving the page from the listener removes all three and gives I7.

- `utils/captive_portal.py` serves, while the setup network is up:
  - `GET /` and `GET /setup/`: the page (one HTML document, inline CSS and
    JS, no external requests). English or Spanish from `Accept-Language`
    (C9).
  - `GET /api/networks`: the cached scan, annotated with the existing
    `utils/wifi_setup.py` helpers (`annotate_network`, `block_reason`,
    band and weak-signal sentences).
  - `POST /api/rescan`: refresh the cache (see the trap below).
  - `POST /api/join` `{ssid, password, hidden}`: validation through the
    existing `wifi_setup` helpers (`normalize_password`, `password_problem`,
    `block_reason`), then `join.start_join`. Returns `joining` or `busy`.
  - `GET /api/state`: `join.read_state()` plus, when the unit lost a proven
    network (§6), `{lost: ssid, reason}`.
  - Captive probes keep their current redirect to `/setup/`.
  - Everything else: 404 with a one-line page linking `/setup/`. **No proxy
    to 8080.** Delete `_proxy`, `rewrite_location`, `unavailable_html` and
    its refresh.
- **No reload, ever (I5).** The page polls `/api/state` with `fetch` every
  3 s and changes one status line. It never calls `location.reload`, never
  sets `location` on a timer, has no `<meta http-equiv="refresh">`, and
  never re-renders the form. The picked network and anything typed survive
  a status change (C1).
- **Copy.** The sentences come from the spec (B7, C3, C4, C6, C7, D3–D7, the
  reconnection lead "Lost SmithHome: the router rejected the password",
  "Already connecting to SmithHome — watch the lights"). Put every string in
  one module, `src/utils/setup_copy.py`, with `en` and `es` dicts of the same
  keys. MARKET and AIR read the strings before the Captain merges (AGENTS:
  customer-facing words go past AIR).
- The page does not name the unit. Cloud finds the unit (spec section 2).

**Files:** `src/utils/captive_portal.py`, `src/utils/setup_copy.py` (new),
`tests/test_captive_portal.py`, a new `tests/test_setup_page.py`.

**Tests:**
- With the Service Window and the firmware not running, `GET /setup/`
  returns the page and `POST /api/join` starts a join (I7).
- `GET /settings`, `GET /relays`, `POST /relays/1` and any 8080 path on the
  listener return 404 (I1).
- The page source (the rendered HTML and its inline JS) contains none of
  `location.reload`, `http-equiv="refresh"`, `location.href =`,
  `location.replace(` (I5). Parse the rendered page; do not grep source
  files.
- `setup_copy.en` and `setup_copy.es` have the same keys.

**Done:** a simulated unit (`scripts/demo-unit.py`) shows the page at
`http://127.0.0.1:<port>/setup/`, lists networks, and a join through the
page reaches `joined` against the virtual radio.

**Bench check (spec section 5):** iPhone, Pixel and Samsung each open the
page by themselves (B3, B4, B6); phone call and app switch mid-typing keep
the form (C1).

**Traps:**
- **Scanning while the setup network is up.** The spec lists this as
  unverified: the BCM43430 may not scan in AP mode. Scan before raising the
  setup network and cache the result to `/run/bluesignal/scan.json`.
  `POST /api/rescan` tries `iw dev wlan0 scan ap-force`; if that fails,
  keep the cached list and say "Couldn't refresh the list. Type the network
  name if yours isn't here." Never drop the setup network to rescan: it
  drops the phone.
- The listener is single-threaded (`run_loop` handles one request at a
  time). Keep handlers short; the join runs in §2's worker thread.
- `captive.py` and the PIN-on-hotspot checks in `auth.py` become dead for
  port 80; leave their removal to §9 so this PR stays reviewable.

---

## §5. Close 8080 to the setup network (I1)

One PR. The Service Window binds `0.0.0.0:8080`, so a phone on the setup
network can open `http://192.168.4.1:8080` directly. Add a `before_request`
in `service_window/app.py`: if `request.remote_addr` is in `AP_NETWORK`,
return 404 with "This page isn't available on the setup network." Direct
connections carry the phone's real address, so `captive.from_hotspot()`
works for this.

**The old wizard is intentionally unreachable from the setup network from
this PR onward.** Identity, sensors, PIN, QR and cloud steps still exist in
the tree until §9 deletes them, and a test client (or a browser on the home
network) can still hit them until then. A phone on BlueSignal Setup cannot.
That is the intended gap, not a bug. Setup from this point is the port-80
Wi-Fi page plus the lights, then Cloud. Do not add a bypass, a proxy, or a
"keep the wizard on 8080 for the setup network" exception.

**Files:** `src/service_window/app.py`, `tests/test_service_window_setup.py`.

**Tests:**
- A request to the Service Window from 192.168.4.23 returns 404; from
  10.0.0.5 it does not (until §10 binds loopback, when a client using
  the unit's LAN address gets connection refused).
- The 404 body is the sentence above.

**Done:** `http://192.168.4.1:8080` does not answer the wizard, or any
other Service Window page, from a client whose address is in
`192.168.4.0/24`.

**Bench check:** `http://192.168.4.1:8080` does not answer from the phone
on BlueSignal Setup.

**Trap:** do not delete the wizard or the PIN in this PR. That is §9.

---

## §6. Retry forever, and the setup network comes back for reconnection (W4)

**Today:** `ensure_reachable()` runs once at boot from
`bluesignal-ap-fallback.service` (a oneshot). A unit that boots before its
router (G1) raises the setup network and never tries the router again until
someone power-cycles it.

**Change:** turn the boot oneshot into a small long-running service,
`bluesignal-netwatch.service` (root, like the fallback; `Restart=always`),
running a state machine in `src/utils/netwatch.py` (pure logic, injected
clock and netctl so it is testable):

| State | Enters when | Does | Leaves when |
|---|---|---|---|
| `online` | station associated with a usable IPv4 | nothing | association lost or no usable IPv4 for 3 min → `lost` |
| `lost` | 3 min without the saved network | raises the setup network in reconnection mode (the page leads with the lost SSID and reason) | 5 min passed and no phone is associated → `retry` |
| `retry` | from `lost` | drops the setup network, lets NetworkManager try every saved network for 60 s | station up → `online`; otherwise → `lost` |
| `first` | no proven network has ever existed | setup network up, no cycling | a join succeeds (§2) → `online` |

- **A phone on the setup network stops the cycle.** Check
  `hostapd_cli -p /run/wqm1/hostapd all_sta`; while any station is listed,
  stay in `lost`. Only §2's join may take the setup network down then.
- **No setup network for an internet outage (G4).** If the station is
  associated with a usable IPv4 address, stay `online` even when BlueSignal
  does not answer. The firmware buffers.
- **Unproven network at boot (D2):** a saved profile still named `bs-try-`
  gets 3 minutes at boot, then is deleted and the unit goes to `first` or
  `lost`.
- **Planned Wi-Fi change (G3).** A request file
  `/run/bluesignal/wifi-window.json {until}` (written by §13's cloud
  command) raises the setup network until `until` (at most 15 min) even
  though the station is up. The chip cannot run both, so the unit is offline
  for that window; Cloud's copy says so.
- State is published to `/run/bluesignal/netwatch.json` for the lights (§7)
  and the page (§4 `/api/state`).

**Files:** `src/utils/netwatch.py` (new), `scripts/wqm1-ap-fallback.py`
(becomes the entry point of the daemon, or is replaced by
`scripts/wqm1-netwatch.py`), `systemd/bluesignal-ap-fallback.service` →
`systemd/bluesignal-netwatch.service`, `setup.sh` (install and enable the
new unit, disable the old), `tests/test_netwatch.py` (new),
`tests/test_ap_unit_layout.py`.

**Tests (fake clock and fake netctl):**
- Router off at boot, on after 5 min: `lost` → `retry` → `online` within
  6 min of the router coming back, with no input (G1).
- A phone associated during `lost`: the setup network is never dropped,
  across 30 simulated minutes.
- Associated, usable IPv4, BlueSignal unreachable for an hour: no setup
  network raised (G4).
- No state in the machine deletes a proven profile (I3).
- A `wifi-window` request past its `until`, or longer than 15 min, is
  ignored or clamped.
- The boot oneshot's current behaviour for a card with no saved network
  (setup network up within the radio wait) is unchanged.

**Done:** tests pass; `setup.sh` installs one service and the old oneshot is
gone.

**Bench check:** router off at power-up and on 5 minutes later (spec 5.6);
change the router's password after install (5.10b). Watch for the radio
wedging after many AP/station switches (spec section 7); run the cycle for
an hour.

**Traps:**
- `setup.sh` and `systemd/` are CODEOWNERS files. Keep the diff there small.
- Two writers to the radio: the daemon and §2's join. The daemon must not
  act while `join.read_state()` says `joining`.
- `RADIO_WAIT_S` and `prepare_wifi_radio()` still apply on every raise; the
  Pi Zero radio is slow to come back.

---

## §7. Lights that say what to do (W5)

**Today:** `control/led.py` is driven by the firmware only, so nothing
lights until the firmware is up, and there is no "ready" or "joining"
pattern.

**Change:**
- `src/control/patterns.py` (pure): one table from a state to a pattern, as
  in the spec's lights table — starting, ready (chase), joining (all fast
  together), online (LED 1 slow), couldn't join (chase plus LED 4 double),
  on Wi-Fi but BlueSignal unreachable (LED 1 slow plus LED 4 slow),
  undervoltage (LED 4 double, A4), and "is this the one?" (LEDs 1+4 then
  2+3, back and forth, §13). Patterns are timings, never colours (H4).
- `bluesignal-lights.service`, started early (before the network), reads
  `/run/bluesignal/netwatch.json`, `join-state.json` and the portal mode
  file once a second and drives the LEDs through `platform_support.gpio`
  (so it works on the Orange Pi host too). "Ready" is shown only when the
  port-80 page answers (B1).
- **One owner of the pins at a time.** When the firmware starts, it writes
  `/run/bluesignal/leds.owner` with its pid and the lights service stops
  driving; when that pid is gone, the lights service takes the pins back.
  The firmware renders setup-related states from the same `patterns.py`
  table, so the meaning never depends on which process is drawing.

**Files:** `src/control/patterns.py` (new), `src/control/led.py`,
`scripts/wqm1-lights.py` (new), `systemd/bluesignal-lights.service` (new),
`setup.sh`, `tests/test_patterns.py`, `tests/test_led.py`.

**Tests:** every state maps to exactly one pattern, and no two states share
one; ready requires the page to answer; the ownership hand-off (firmware pid
present → lights service writes nothing; pid gone → it resumes) with a fake
GPIO.

**Done:** from power-on, LED 1 is steady within 30 s on a bench Pi; the
chase starts when the page answers.

**Bench check:** LED visibility with the lid open (spec section 7).

**Trap:** both processes opening the same GPIO lines raises on the Orange Pi
(gpiochip lines are exclusive). The hand-off must close the lines, not just
stop writing.

---

## Precondition (run before §8)

Not a PR. A short check that the pinned crypto library can do what §8
needs, recorded here so the sealed-password PR does not discover a missing
primitive after the page is already in review.

**X25519 key agreement in `pycryptodome==3.24.0`:** this VM did not have
the package until `python3 -m pip install --user pycryptodome==3.24.0`.
Then:

```
python3 --version
python3 -c 'import Crypto; print("pycryptodome", Crypto.__version__)'
python3 -c '
from Crypto.Protocol.DH import key_agreement
from Crypto.PublicKey import ECC
from Crypto.Protocol.KDF import HKDF
from Crypto.Hash import SHA256
from Crypto.Cipher import ChaCha20_Poly1305
import Crypto

def kdf(x):
    return HKDF(x, 32, b"", SHA256, 1)

print("pycryptodome", Crypto.__version__)
a = ECC.generate(curve="curve25519")
b = ECC.generate(curve="curve25519")
ab = key_agreement(static_priv=a, static_pub=b.public_key(), kdf=kdf)
ba = key_agreement(static_priv=b, static_pub=a.public_key(), kdf=kdf)
print("shared_len_ab", len(ab))
print("shared_len_ba", len(ba))
print("both_directions_equal", ab == ba)
nonce = b"\x00" * 24
plain = b"wqm1-precondition"
enc = ChaCha20_Poly1305.new(key=ab, nonce=nonce)
ct, tag = enc.encrypt_and_digest(plain)
dec = ChaCha20_Poly1305.new(key=ba, nonce=nonce)
out = dec.decrypt_and_verify(ct, tag)
print("roundtrip_ok", out == plain)
print("ct_len", len(ct), "tag_len", len(tag))
'
```

Real output (Python 3.12.3 on this VM, 2026-10-24, rerun after the #156
review):

```
Python 3.12.3
pycryptodome 3.24.0
pycryptodome 3.24.0
shared_len_ab 32
shared_len_ba 32
both_directions_equal True
roundtrip_ok True
ct_len 17 tag_len 16
```

Both directions of `key_agreement` on `curve25519` returned the same 32
bytes. One XChaCha20-Poly1305 encrypt/decrypt with a 24-byte nonce
round-tripped the plaintext. Rerun independently on 2026-10-24 under
Python 3.11.17 (CI's version) and 3.13.17, each in a fresh venv with
`pycryptodome==3.24.0`: both directions equal (32 bytes), round-trip ok,
and a ciphertext with one flipped bit is rejected (`ValueError`). Not
verified: this check is not under `pytest`; the §8 PR adds it as a test.

**How HKDF-SHA256 + XChaCha20-Poly1305 will be checked across JS and
Python** (do this in the §8 PR; it is unverified today — there is no
vendored `@noble` tree in this repo and this VM did not run Node against
one):

1. Vendor `@noble/curves` (x25519) and `@noble/ciphers` (xchacha20poly1305)
   under `src/utils/setup_static/`, with licences and versions recorded.
2. Pin the shared parameters in one place both sides import or copy:
   HKDF hash SHA-256, output length 32, salt and info strings, 24-byte
   nonce, and any AAD.
3. Under Node, using those exact vendored files, emit one committed
   fixture: X25519 shared secret → HKDF-SHA256 → XChaCha20-Poly1305
   ciphertext of a known `{password, nonce}` payload.
4. A Python test decrypts that fixture with pycryptodome 3.24.0 and asserts
   the plaintext.
5. A second committed fixture, produced by Python, is decrypted under Node
   with the same vendored files.
6. Do not claim interop until both fixtures pass. If they fail, stop and
   say so; do not add a second crypto library to paper over a mismatch.

---

## §8. Sealed password (W1b)

**Why:** on an open network, anyone in range can capture the password as it
is typed (spec risk 1). WebCrypto is not available on a plain-HTTP page, so
the page needs its own crypto library.

**Change:**
- At each boot the listener creates an X25519 key pair in memory (never on
  disk) and puts the public key in the page.
- **The nonce is per attempt, not per page.** Each `/api/state` response
  carries a fresh random 16-byte nonce, and the page uses the latest one it
  has seen. The listener keeps the last few unspent nonces for 10 minutes
  and spends one on any decrypted join, right or wrong. So a wrong
  password is retyped and sent again from the same page with no reload
  (I5, C1), and a captured request still cannot be replayed.
- The page encrypts `{password, nonce}` with X25519 + HKDF-SHA256 +
  XChaCha20-Poly1305 and posts the ciphertext to `/api/join`.
- JS: vendor `@noble/curves` (x25519) and `@noble/ciphers`
  (xchacha20poly1305) minified under `src/utils/setup_static/`, with their
  licences and versions recorded. No CDN.
- Python: `pycryptodome` 3.24.0 (already in `requirements.txt`): X25519 key
  agreement (`Crypto.Protocol.DH.key_agreement` on `curve25519`), HKDF,
  `ChaCha20_Poly1305` with a 24-byte nonce. The precondition above already
  agreed in both directions and round-tripped one ciphertext on this VM.
- The server rejects a plaintext password field, a wrong or reused page
  nonce, and a decryption failure (with "Reload the page and try again").

**Files:** `src/utils/captive_portal.py`, `src/utils/setup_seal.py` (new),
`src/utils/setup_static/` (new), `tests/test_setup_seal.py`.

**Tests:** a JS-produced ciphertext (fixture generated once with the vendored
library under Node and committed) decrypts in Python; a Python-produced
ciphertext decrypts under Node with the same vendored files; a replayed
nonce is refused; a captured `/api/join` request body does not contain the
password; the key changes across a simulated restart; a wrong-password
join followed by a second submit from the same page (new nonce from the
next `/api/state`, no reload) reaches the join; a nonce older than 10
minutes is refused.

**Done:** spec bench step 10d: a second phone capturing traffic during a
join does not see the password. The two fixtures in the precondition pass.

**Traps:**
- The precondition showed both-direction `key_agreement` on `curve25519`,
  one XChaCha20-Poly1305 round-trip and a rejected tampered ciphertext in
  3.24.0, on Python 3.11 and 3.12/3.13. If CI disagrees, raise it in the
  PR rather than adding a second crypto library.
- This does not stop a fake "BlueSignal Setup" (spec risk 3). Do not claim
  it does in comments or docs.

---

## §9. Delete the wizard and the PIN (W6)

Decision 1 is answered (24 Oct 2026) and does not hold this PR. This PR
deletes the setup wizard and the PIN only. It does not turn the local
page off and it does not remove pH calibration or RS485 setup; that is
§10, which still waits (see that header).

Captain's ruling, 2026-10-24: setup is Wi-Fi only with the LEDs as the
guide. There is no PIN anywhere. Owners use BlueSignal Cloud only. The
unit's local page stays off. The old wizard, PIN, sensors, QR and
claim-token steps are obsolete. Same day: with the local page off, pH
calibration and RS485 sensor setup move into BlueSignal Cloud, as Cloud
commands with no unit page.

**Order: §9 merges after §10.** Decision 3 is answered (24 Oct 2026): no
mutating route answers from the home network, so §11 is skipped. Today
the 8080 login is the only thing between a phone on the home network and
the relays, calibration, RS485 and settings routes on `0.0.0.0:8080`.
Deleting the PIN before §10 binds loopback leaves them open with no
login. Until §10 has merged, §9 may be built and reviewed but not
merged.

One PR. Deleting the wizard, deleting the PIN and login, and moving the
bench key paste to `scripts/provision.py` are one change: removing the old
setup path. They land together; splitting them leaves a half-removed path
in the tree (AGENTS rule 6 is about unrelated changes). Do not fold
local-page-off, CSRF, or template cleanup into this change.

This section used to say the Service Window would drop its login and become
a read-only local page, unlocked from Cloud for 30 minutes (`local_unlock`).
That LAN-read decision is withdrawn.

**Change:**
- Delete the setup wizard: `routes/setup.py` pages `welcome`, `pin`,
  `identity`, `sensors`, `network`, `cloud`, `done`, their templates under
  `templates/setup/` and `setup_base.html`, `needs_setup` and the redirect
  in `app.py`, the factory-PIN prompt, `_probe_block` (the probe check that
  blocked Connect), `captive.py`'s hotspot-session logic and the hotspot
  branch of `auth.py`.
- **No PIN anywhere.** Delete the login route and every PIN check. There is
  no factory PIN on the hotspot and no PIN later.
- The bench key paste from the old cloud step moves to `scripts/provision.py`
  only.

**Files:** `src/service_window/` (app, auth, captive, routes/setup,
templates/setup, setup_base.html), `scripts/provision.py`,
`docs/site-flow-v2.md`, `docs/install-day-runbook.md`,
`docs/getting-started.md` (PIN 1234 and `WQM1-xxxx` mentions), tests for
the deleted routes.

**Tests:**
- No route answers `/setup/...` any more.
- No PIN prompt, PIN field or factory PIN remains.

**Done:** the Service Window has no wizard and no PIN anywhere.

**Traps:**
- `scripts/provision.py`, `scripts/diagnostics.sh` and the burn-in may log
  in with the PIN today. Find every caller of the login route before
  deleting it.
- Docs in the marketplace repo that mention PIN 1234 or `WQM1-xxxx` are
  updated through a marketplace PR (the Cloud plan's §7), not here.
- Do not invent Cloud UI or command fields here. Decision 1 is answered;
  the unit-side path is §13 and the marketplace list at the end of this
  plan.

---

## §10. The unit's local page stays off

**Held:** Decision 1 is answered and does not hold this PR. The unit side
needs a Cloud command path for pH calibration and RS485 sensor setup
(§13; fields in marketplace #1457,
`marketplace/docs/operations/wqm1-install-build-plan.md` §5) before the
local page can go off, so this PR still waits on that Cloud work.
Decisions 2 and 3 are answered (24 Oct 2026) and do not hold this PR.

One PR. With the local page off, **no GET page is readable from the home
network.** This is not "read-only without a PIN." It is off. Consulted:
this plan, `SECURITY.md` (reporting policy only; it does not define LAN
exposure), `docs/site-flow-v2.md` (the flow this plan replaces).

**No `local_unlock` and no `local-unlock.json`.** With the page off there
is nothing to unlock. Do not add `unlock_required`.

**Decision 2, answered 24 Oct 2026:** the Service Window (port 8080)
stays running but binds loopback (`127.0.0.1`) only, so no client using
the unit's LAN address gets a page (connection refused). Bench access
is by SSH tunnel (`ssh -L 8080:127.0.0.1:8080 pi@<unit>`).

**Test (loopback only):**
- The Service Window listens on loopback only. A client using the unit's
  LAN address does not get a GET page (connection refused).

Not chosen: 404 on `0.0.0.0:8080` — "off" would be a route filter that
can miss a new route, not a property of the socket. Not chosen: process
not running — that drops the bench path on the unit that loopback
keeps (calibration through the SSH tunnel above).

**Files:** `src/service_window/__main__.py` (today `--host` defaults to
`0.0.0.0`), the systemd unit if it must pass `--host 127.0.0.1`,
`setup.sh` for the printed LAN URLs, tests for the loopback bind.

**Done:** a client using the unit's LAN address on port 8080 gets
connection refused; the process still answers on `127.0.0.1`.

**Callers of :8080 to fix or confirm before §10**

Grep 2026-10-24 for `8080`, `:8080`, and LAN Service Window URLs
(`$(hostname).local`, `<unit-ip>`, `<pi-ip>`, `wqmbench.local`). Lines
below were read. None of these files are changed in this plan PR.

Will break under loopback-only (LAN address or another machine):

- `scripts/diagnostics.sh:122` — prints
  `http://$(hostname).local:8080`. Connection refused.
- `scripts/first-boot-check.sh:34` — prints
  `http://$(hostname).local:8080/provision`. Connection refused.
- `setup.sh:619` and `:624` — prints
  `http://$(hostname).local:8080` and `/provision`. Connection refused.
- `README.md:266` — phone path `http://<hostname>.local:8080`.
  Connection refused. `:557–570` — "from another machine" via
  `http://<pi-ip>:8080/…`. Connection refused.
- `docs/getting-started.md:355–357` —
  `http://192.168.4.1:8080/setup/` and `http://<unit-ip>:8080`.
  Connection refused.
- `docs/install-day-runbook.md:106` — `http://wqmbench.local:8080`.
  `:215` — "SSH dead but :8080 alive" recovery. `:340–343` —
  `http://192.168.4.1:8080/setup/` and `http://<unit-ip>:8080`.
  All three break.
- `docs/sensor-calibration.md:12` — `http://<unit-ip>:8080`.
  Connection refused. Bench then uses the SSH tunnel.
- `docs/smart-breaker-installer-guide.md:47` —
  `http://<pi-ip>:8080/settings/`. Connection refused.
- `docs/raspberry-pi.md:106–107` — "that port still answers on the
  site LAN after the join." The claim becomes false.
- `docs/ota-runbook.md:194` — field note that `:8080` kept serving
  when sshd was dead. That LAN recovery path breaks. The reboot
  action itself is the command socket (`src/main.py:1074–1076`), not
  an HTTP call to `:8080`.
- Docs that send a person to the Service Window from the LAN without
  a `:8080` in the line, still wrong as written:
  `docs/apple-home.md:52` ("LAN only"),
  `docs/home-assistant.md:95`,
  `docs/platforms.md:203` and `:258`.

Checked; do not call `:8080` on the unit's LAN address:

- `scripts/provision.py` — CLI only. No `:8080` and no Service Window
  HTTP.
- `scripts/burn-in.sh`, `src/diagnostics/burnin.py:27`,
  `docs/burn-in.md:40` — no `:8080`. Burn-in watches
  `bluesignal-service-window.service`; it does not HTTP to the port.
- `.github/workflows` — no `:8080`. `ci.yml:128` only counts
  `service_window` `.pyc` files in the OTA bundle.
- `src/ota/agent.py:182` and `:568` — comments that compile must not
  block `:8080`. No HTTP to the port.
- `scripts/demo-unit.py:6`, `:8`, `:63–64` — `localhost` / default
  `--host 127.0.0.1`. Unaffected.
- `docs/site-flow-v2.md:75–76` — same localhost demo. Unaffected.
- `scripts/simulate-fleet.py:359–364` starts
  `python -m service_window` without `--host` (inherits today's
  `0.0.0.0` default); `:377` talks to `http://127.0.0.1:{port}`.
  After the default host becomes loopback the client still works.
- Tests (`tests/test_captive_portal.py`,
  `tests/test_site_flow_v2.py:84`,
  `tests/test_service_window_reboot.py:4`,
  `tests/test_setup_firmware_starting.py:3`) — Flask test client or
  string checks. None open a unit LAN socket.
- No `curl …:8080` in scripts, tests, or CI.

`src/service_window/__main__.py:23` defaults `--host` to `0.0.0.0`.
That is the bind §10 changes, not a caller.

**Traps:**
- Do not start this PR until §13 can accept the calibration and RS485
  Cloud commands (fields in marketplace `wqm1-install-build-plan.md` §5,
  marketplace #1457). Do not invent Cloud fields or endpoints here.
  Until those ship, BlueSignal staff calibrate on the bench from the
  Service Window (`docs/sensor-calibration.md`). With loopback, they
  still can, through the SSH tunnel above, so loopback does not put
  bench calibration on hold.
- Checked 2026-10-24 for the loopback option: the current captive portal
  already reaches the Service Window over `127.0.0.1`
  (`utils/captive_portal.py`, the `HTTPConnection("127.0.0.1", …)` proxy),
  so loopback does not break it, and §4 removes that proxy anyway. The
  `iptables` line in `systemd/bluesignal-captive-portal.service` that
  mentions 8080 deletes a legacy redirect; it adds none. The printed
  LAN URLs and the other callers are in the list above; change them
  in this PR or a follow-up that lands with it, not before.

---

## §11. CSRF — skipped

**Skipped.** Decision 3, answered 24 Oct 2026: no mutating route answers
from the home network, so this PR is not opened. Loopback (Decision 2)
takes POST, PUT, PATCH and DELETE off the LAN the same way it takes
GET pages off: connection refused. Relays, calibration, RS485 and
settings move to Cloud commands (§13); nothing on the home network
needs them.

The CSRF rule that would have applied (403 if both `Origin` and
`Referer` are missing; 403 if either header is a foreign host) is
kept here only so a later change that puts a mutating route back on
the LAN knows what to build. Do not implement it while those routes
stay off.

---

## §12. I5 template cleanup

One PR. Remove auto-refresh from leftover Service Window templates
(`sensors.html` 15 s, `status.html` 30 s, `static/app.js` 30 s on `/`).
They must not reload themselves, navigate on a timer, or replace a form
(I5). The port-80 setup page is already I5 under §4.

Decision 2 is loopback, so §10 does not delete those templates; this
PR still opens. If a later change deletes them, skip this PR.

**Files:** the leftover templates and `static/app.js`, plus a render test.

**Tests:** every remaining template renders without the strings named in
I5. Parse the rendered page; do not grep source files.

**Done:** no leftover Service Window page reloads itself.

---

## §13. Cloud commands and heartbeat fields (W9, and the hooks for M2, M5)

**Today:** the firmware polls `deviceCommands` every `command_poll_s` (5 s
by default) and handles `relay`, `awg`, `restart`, `config_reload`,
`ota_check`, `diagnostics` in `main.py` `_apply_cloud_command`.

**Add these command types:**

| Type | Does | Bounds |
|---|---|---|
| `identify` | the "is this the one?" pattern (§7) for `seconds` | 10–120 s, default 60 |
| `wifi_window` | writes `/run/bluesignal/wifi-window.json` (§6) | at most 900 s |
| `adc_voltages`, `calibrate_ph`, `calibrate_tds`, `calibrate_turbidity`, `calibrate_orp` | the calibration the Service Window does today, from Cloud | fields, ranges and results: marketplace `docs/operations/wqm1-install-build-plan.md` §5 |
| `rs485_scan`, `rs485_assign`, `rs485_calibrate` | RS485 probe setup from Cloud | the same table |

The calibration and RS485 types are Decision 1 (24 Oct 2026). They must
exist on the unit before §10 turns the local page off. **Move the math, do
not copy it:** the slope, `k`, offset and RS485 calls the Service Window
routes do today (`routes/calibration.py`, `routes/rs485.py`) move into one
module both the routes and the command handler call, so the two cannot
drift while both exist. The daemon applies a new calibration to the running
sensors; it does not need a restart (the Service Window's "restart to
apply" hint goes away with it).

**Results on the ack.** `ack_command` gains an optional `result` dict,
posted beside `status`: the slope, `k`, offset, the voltages read, or the
probes found. A refused calibration acks `error` with the reason
(`voltages_too_close`, `no_conduction`, `no_orp_circuit`). Cloud
allowlists the keys (Cloud plan §5).

There is no `local_unlock`. The unit's local page stays off; Cloud does not
turn it on.

Each acks `done` or `error` through the existing `ack_command`. Unknown
fields are ignored; out-of-range values are clamped, not trusted.

**Heartbeat and register:** in `utils/health.py` `build_heartbeat` (and the
register payload), add:
- `wifiSsid` — the associated station SSID; omitted when not on Wi-Fi.
- `ipv6Prefix` — the /64 of the station interface's global IPv6 address
  (`2000::/3`), as `xxxx:xxxx:xxxx:xxxx::/64`; omitted when there is none.
  Read from the interface, never from an outside service. Cloud uses it to
  match a phone on the same Wi-Fi when the unit itself reports over IPv4.
- `gps` — `{lat, lon, fixAt}` from the last fix; omitted when there has
  never been one. **Try for a fix right after first boot** instead of
  waiting for `gps_fix_s`, so a new unit can be found by location while the
  installer is still there (spec W9). **Send one heartbeat
within 30 s of a successful join** instead of waiting for `heartbeat_s`
(600 s by default), so Cloud shows "online on SmithHome" while the
installer is still standing there (E1).

**Files:** `src/main.py`, `src/utils/health.py`, `src/app/workers.py` (the
early heartbeat), `docs/cloud-payload.md`, tests.

**Tests:** each command's dispatch, clamp and ack; each calibration
command gives the same slope, `k` or offset as the Service Window route on
the same inputs (one shared fixture), and acks the refusal reasons above;
`wifiSsid` present on a station and absent on LTE and on the setup
network; `ipv6Prefix` is the /64 of a global address and is omitted for a
link-local or ULA address; `gps` is omitted with no fix; the first-boot fix
attempt runs once; the early heartbeat fires once after a join and not on
every reconnect; an incoming `local_unlock` is ignored (unknown type) and
does not write a file.

**Done:** with the Cloud half's endpoint (Cloud plan §2), `identify` rocks
the lights on a bench Pi within 10 s of the tap.

**Traps:**
- The poll only runs once the unit has a device API key
  (`_cloud_http_ready`). The ten bench units get theirs from the card. A
  unit with no card has none until registration succeeds, so it cannot be
  asked to identify itself. Say so in the PR; do not invent a second
  channel.
- An SSID, a prefix and a location are the owner's data. The marketplace
  side stores them under its personal-data inventory (Cloud plan §1), keeps
  only salted tags of the prefix, and deletes the location once the unit
  has an owner. The firmware sends them nowhere else.
- This section is big enough to split, if the reviewer asks: (a)
  `identify`, `wifi_window`, the heartbeat fields; (b) the calibration and
  RS485 commands with the shared math module. (b) must land before §10.

---

## Later, not now

- **W8, Wi-Fi from one unit to the next.** After the first ten, only if a
  site has more than one unit.
- **OWE** on the setup network, if the bench shows the chip supports it.

## Order and dependencies

§1 → §2 → §3 → §4 → §5 → §6 → §7 → §8 → §13 → §10 (held on §13
Cloud commands) → §9 → §12. §11 is skipped.

**§9 never merges before §10.** §9 deletes the PIN, and the PIN is the
only gate on the mutating 8080 routes until §10 binds loopback.

§4 needs §2 (a join to call) and §3 (the open network). §5 can merge as
soon as the Service Window still exists; after it merges, the old wizard is
intentionally unreachable from the setup network. §8 can start once §4 is
merged (run the precondition first). Decision 1 is answered (24 Oct 2026)
and no longer holds §9 or §10. Decisions 2 and 3 are answered (24 Oct
2026) and no longer hold §10. §9 (wizard and PIN only) can be built
now but merges only after §10 (see above). **The unit side needs a Cloud
command path for calibration and RS485 (§13; marketplace #1457) before
the local page can go off, so the §10 PR still waits on that Cloud
work.** §11 is skipped: Decision 3 answered no mutating route from the
home network. §12 still opens: loopback does not delete leftover
templates. `identify` / `wifi_window` / heartbeat in
§13 can start after §7. The calibration and RS485 command handlers in
§13 must land before §10. The Cloud half's §2 (Add unit) needs this
plan's §13.

## Not verified by this plan

- Every bench item named above; the spec's section 7 list.
- That `iw dev wlan0 scan ap-force` works on the BCM43430 in AP mode.
- HKDF-SHA256 + XChaCha20-Poly1305 interop between the vendored JS libraries
  and pycryptodome (the method is in the precondition; no fixture has been
  generated).
- The repro numbers in §1 came from this session's scratch test, not CI.

## Decisions from the Captain

1. **Answered, 24 Oct 2026.** With the local page off, pH calibration and
   RS485 sensor setup move into BlueSignal Cloud, as Cloud commands with
   no unit page. The unit-side path is §13; the Cloud side, with field
   names and results, is marketplace `wqm1-install-build-plan.md` §5.
2. **Answered, 24 Oct 2026.** The Service Window (port 8080) stays
   running but binds loopback (`127.0.0.1`) only, so no client using
   the unit's LAN address gets a page (connection refused). Bench
   access is by SSH tunnel. §10 builds that bind; it still waits on
   §13 Cloud commands (marketplace #1457), not on this decision.
   Not chosen: 404 on `0.0.0.0:8080` (a route filter can miss a new
   route). Not chosen: process not running (drops the bench path
   loopback keeps).
3. **Answered, 24 Oct 2026.** No mutating route answers from the home
   network, so §11 (CSRF) is skipped. Relays, calibration, RS485 and
   settings move to Cloud commands (§13). With loopback (Decision 2)
   this answer comes for free.

## Marketplace Cloud work (defined in marketplace #1457)

Defined in marketplace `docs/operations/wqm1-install-build-plan.md` §5: the
command types, their fields and ranges, the `result` each ack carries, and
the Calibration panel in Cloud. That plan's §1 accepts `wifiSsid`,
`ipv6Prefix` and `gps`; its §2 uses them to find the unit.
