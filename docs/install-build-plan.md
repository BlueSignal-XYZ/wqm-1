# Install v3: firmware build plan

**Status:** plan, 2026-10-24. Written by Claude for Grok under the `AGENTS.md`
handoff. Nothing here is built yet.

**What it builds:** the firmware half of the install spec,
`marketplace/docs/operations/wqm1-install-spec.md` (marketplace PR #1457).
Read the spec first; build item IDs (W1–W9) and situation IDs (A1–H4) refer
to it. The Cloud half is `marketplace/docs/operations/wqm1-install-build-plan.md`.

**The install, in one line:** plug in, join the open network "BlueSignal
Setup", type the home Wi-Fi password, then finish in Cloud. The unit's
setup network does Wi-Fi and nothing else.

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
  `SETTINGS_SCHEMA`) are touched by §2, §4, §5 and §7. Say so in the PR body;
  the Captain reviews those lines.
- **Prove it the way CI does:** `ruff check . && ruff format --check . &&
  pytest tests/ -q`. Paste the command and the summary line in the PR.
- **Say what you did not verify.** Most of this needs a Pi and a phone; the
  tests use fakes. Each section lists its bench check.

## What must always be true

Every PR keeps all of these, and the tests named below pin them.

| # | Invariant | Pinned by |
|---|---|---|
| I1 | A client on the setup network (192.168.4.0/24) can reach one thing: the Wi-Fi page. Nothing that changes the unit, other than its Wi-Fi, answers there. | §3 tests; §7 tests |
| I2 | The home Wi-Fi password is written only into the NetworkManager profile (root-only). Never into a log, a state file, the boot partition, a heartbeat or Cloud. | §2, §3 tests (capture logs and files, search for the password) |
| I3 | A network that has reached BlueSignal is never deleted by the unit until another network has reached BlueSignal. | §2, §4 tests |
| I4 | At most one join runs at a time. | §2 test |
| I5 | No page the unit serves reloads itself, navigates on a timer, or replaces a form. A status change edits text in place. | §3 and §7 tests |
| I6 | Every failure ends with the setup network up and the lights at "ready", unless a proven network exists and is being retried. | §2, §4 tests |
| I7 | The setup network and its page work without the firmware (`bluesignal-wqm`), the Service Window or Cloud running. | §3 test (portal answers with both stopped) |

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

Build this before the page (§3), so the page has a join to call and the
current wizard keeps working in the meantime.

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

**Done:** all of the above pass; the current wizard's network step still
joins through the new path.

**Bench check:** wrong password, then right; a router with client approval
(or a firewall rule blocking the API host for 2 minutes).

**Traps:**
- The worker thread must not die with the HTTP server. In §3 the port-80
  listener closes the moment the setup network goes down; the worker runs
  in the same process and must keep running after the socket closes.
- `nmcli connection modify <id> connection.id <ssid>` renames; do not
  re-create the profile (that drops the saved password).
- Hidden networks (C3): the `bs-try-` profile needs `wifi.hidden yes`.

---

## §3. The Wi-Fi page on port 80 (W1 name, W2)

**Decision: the port-80 listener renders the page itself.** Today
`utils/captive_portal.py` proxies every request to the Service Window
(Flask, port 8080), which takes minutes to start on a Pi Zero; that is why a
"starting up" page with a 3-second refresh exists, and why the wizard page
reloaded every 5 s (`setup_base.html:55`). The proxy also hides the phone's
address (the Service Window sees 127.0.0.1), which broke the hotspot checks.
Serving the page from the listener removes all three and gives I7.

### 3a. The name: "BlueSignal Setup", open

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
  gone because the page sets only Wi-Fi (spec decision 1).
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

### 3b. The page

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
    network (§4), `{lost: ssid, reason}`.
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

### 3c. Close 8080 to the setup network (I1)

The Service Window binds `0.0.0.0:8080`, so a phone on the setup network can
open `http://192.168.4.1:8080` directly. Add a `before_request` in
`service_window/app.py`: if `request.remote_addr` is in `AP_NETWORK`,
return 404 with "This page isn't available on the setup network." Direct
connections carry the phone's real address, so `captive.from_hotspot()`
works for this. Keep it independent of the PIN code that §7 deletes.

**Files:** `src/utils/identity.py`, `src/utils/netctl.py`,
`src/utils/captive_portal.py`, `src/utils/setup_copy.py` (new),
`src/service_window/app.py`, `tools/golden/overlay/usr/local/sbin/wqm1-firstboot`,
the scripts and diagnostics named above, `tests/test_captive_portal.py`,
`tests/test_ap_unit_layout.py`, `tests/test_service_window_setup.py`,
`tests/test_simulate.py`, `tests/test_netinfo.py`, `tests/test_unit_label.py`, a new
`tests/test_setup_page.py`.

**Tests:**
- `ap_name()` is "BlueSignal Setup" for a label, a derived id and a
  simulated id; `host_name()` is `wqm1-0001` for `WQM-10001`.
- `hostapd_config(ssid, None)` contains no `wpa` line; with an 8-character
  passphrase it is unchanged from today.
- With the Service Window and the firmware not running, `GET /setup/`
  returns the page and `POST /api/join` starts a join (I7).
- `GET /settings`, `GET /relays`, `POST /relays/1` and any 8080 path on the
  listener return 404 (I1). A request to the Service Window from
  192.168.4.23 returns 404; from 10.0.0.5 it does not.
- The page source (the rendered HTML and its inline JS) contains none of
  `location.reload`, `http-equiv="refresh"`, `location.href =`,
  `location.replace(` (I5). Parse the rendered page; do not grep source
  files.
- `setup_copy.en` and `setup_copy.es` have the same keys.
- The firstboot script test named in 3a.

**Done:** a simulated unit (`scripts/demo-unit.py`) shows the page at
`http://127.0.0.1:<port>/setup/`, lists networks, and a join through the
page reaches `joined` against the virtual radio.

**Bench check (spec section 5):** iPhone, Pixel and Samsung each open the
page by themselves (B3, B4, B6); phone call and app switch mid-typing keep
the form (C1); `http://192.168.4.1:8080` does not answer from the phone.

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
  port 80; leave their removal to §7 so this PR stays reviewable.

---

## §4. Retry forever, and the setup network comes back for reconnection (W4)

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
  `/run/bluesignal/wifi-window.json {until}` (written by §8's cloud
  command) raises the setup network until `until` (at most 15 min) even
  though the station is up. The chip cannot run both, so the unit is offline
  for that window; Cloud's copy says so.
- State is published to `/run/bluesignal/netwatch.json` for the lights (§5)
  and the page (§3 `/api/state`).

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

## §5. Lights that say what to do (W5)

**Today:** `control/led.py` is driven by the firmware only, so nothing
lights until the firmware is up, and there is no "ready" or "joining"
pattern.

**Change:**
- `src/control/patterns.py` (pure): one table from a state to a pattern, as
  in the spec's lights table — starting, ready (chase), joining (all fast
  together), online (LED 1 slow), couldn't join (chase plus LED 4 double),
  on Wi-Fi but BlueSignal unreachable (LED 1 slow plus LED 4 slow),
  undervoltage (LED 4 double, A4), and "is this the one?" (LEDs 1+4 then
  2+3, back and forth, §8). Patterns are timings, never colours (H4).
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

## §6. Sealed password (W1b)

**Why:** on an open network, anyone in range can capture the password as it
is typed (spec risk 1). WebCrypto is not available on a plain-HTTP page, so
the page needs its own crypto library.

**Change:**
- At each boot the listener creates an X25519 key pair in memory (never on
  disk) and puts the public key and a random 16-byte page nonce in the page.
- The page encrypts `{password, nonce}` with X25519 + HKDF-SHA256 +
  XChaCha20-Poly1305 and posts the ciphertext to `/api/join`.
- JS: vendor `@noble/curves` (x25519) and `@noble/ciphers`
  (xchacha20poly1305) minified under `src/utils/setup_static/`, with their
  licences and versions recorded. No CDN.
- Python: `pycryptodome` (already in `requirements.txt`): X25519 key
  agreement, HKDF, `ChaCha20_Poly1305` with a 24-byte nonce.
- The server rejects a plaintext password field, a wrong or reused page
  nonce, and a decryption failure (with "Reload the page and try again").

**Files:** `src/utils/captive_portal.py`, `src/utils/setup_seal.py` (new),
`src/utils/setup_static/` (new), `tests/test_setup_seal.py`.

**Tests:** a JS-produced ciphertext (fixture generated once with the vendored
library under Node and committed) decrypts in Python; a replayed nonce is
refused; a captured `/api/join` request body does not contain the password;
the key changes across a simulated restart.

**Done:** spec bench step 10d: a second phone capturing traffic during a
join does not see the password.

**Traps:**
- Confirm `pycryptodome==3.24.0` has X25519 key agreement
  (`Crypto.Protocol.DH.key_agreement`); if it does not, raise it in the PR
  rather than adding a second crypto library.
- This does not stop a fake "BlueSignal Setup" (spec risk 3). Do not claim
  it does in comments or docs.

---

## §7. Delete the wizard; the unit's page goes read-only (W6)

**Change:**
- Delete the setup wizard: `routes/setup.py` pages `welcome`, `pin`,
  `identity`, `sensors`, `network`, `cloud`, `done`, their templates under
  `templates/setup/` and `setup_base.html`, `needs_setup` and the redirect
  in `app.py`, the factory-PIN prompt, `_probe_block` (the probe check that
  blocked Connect), `captive.py`'s hotspot-session logic and the hotspot
  branch of `auth.py`.
- **No login on the Service Window.** All GET pages work without a PIN:
  status, readings, Wi-Fi, firmware.
- **Every route that changes the unit is locked** (relays, calibration,
  RS485, settings, AWG, LoRa, provision, Apple Home and Home Assistant
  settings) behind one decorator, `unlock_required`, which checks
  `/var/lib/bluesignal/local-unlock.json {until}`. That file is written only
  by the `local_unlock` cloud command (§8), for at most 30 minutes. Locked
  pages say "Changes are locked. Unlock the unit's page in BlueSignal Cloud."
- **CSRF.** With no login, any page on the home network could POST to the
  unit while it is unlocked. Mutating routes require an `Origin` (or
  `Referer`) whose host is the unit's own address or hostname.
- **No auto-refresh on any page** (I5): `sensors.html` (15 s), `status.html`
  (30 s) and `static/app.js` (30 s on `/`) change to `fetch` and update text
  in place.
- The bench key paste from the old cloud step moves to `scripts/provision.py`
  only.

**Files:** `src/service_window/` (app, auth, captive, routes, templates,
static), `scripts/provision.py`, `docs/site-flow-v2.md`,
`docs/install-day-runbook.md`, `docs/getting-started.md` (PIN 1234 and
`WQM1-xxxx` mentions), tests for each route.

**Tests:**
- Walk the Flask `url_map`: every rule whose methods include POST, PUT,
  PATCH or DELETE returns 403 when locked, and works when unlocked within
  `until`, and returns 403 after `until`. Do not keep a hand-written list of
  routes; read the map (AGENTS rule 3).
- A POST with a foreign `Origin` returns 403 even when unlocked.
- Every template renders without the strings named in I5.
- No route answers `/setup/...` any more.

**Done:** the Service Window has no PIN anywhere; pH calibration and the
RS485 wizard work end to end while unlocked (existing tests, unlocked).

**Traps:**
- `scripts/provision.py`, `scripts/diagnostics.sh` and the burn-in may log
  in with the PIN today. Find every caller of the login route before
  deleting it.
- Docs in the marketplace repo that mention PIN 1234 or `WQM1-xxxx` are
  updated through a marketplace PR (the Cloud plan's §6), not here.

---

## §8. Cloud commands and heartbeat fields (W9, and the hooks for M2, M5, W6)

**Today:** the firmware polls `deviceCommands` every `command_poll_s` (5 s
by default) and handles `relay`, `awg`, `restart`, `config_reload`,
`ota_check`, `diagnostics` in `main.py` `_apply_cloud_command`.

**Add three command types:**

| Type | Does | Bounds |
|---|---|---|
| `identify` | the "is this the one?" pattern (§5) for `seconds` | 10–120 s, default 60 |
| `wifi_window` | writes `/run/bluesignal/wifi-window.json` (§4) | at most 900 s |
| `local_unlock` | writes `/var/lib/bluesignal/local-unlock.json` (§7) | at most 1800 s |

Each acks `done` or `error` through the existing `ack_command`. Unknown
fields are ignored; out-of-range values are clamped, not trusted.

**Heartbeat:** add `wifiSsid` (the associated station SSID; omitted when not
on Wi-Fi) in `utils/health.py` `build_heartbeat`. **Send one heartbeat
within 30 s of a successful join** instead of waiting for `heartbeat_s`
(600 s by default), so Cloud shows "online on SmithHome" while the
installer is still standing there (E1).

**Files:** `src/main.py`, `src/utils/health.py`, `src/app/workers.py` (the
early heartbeat), `docs/cloud-payload.md`, tests.

**Tests:** each command's dispatch, clamp and ack; `wifiSsid` present on a
station and absent on LTE and on the setup network; the early heartbeat
fires once after a join and not on every reconnect.

**Done:** with the Cloud half's endpoint (Cloud plan §2), `identify` rocks
the lights on a bench Pi within 10 s of the tap.

**Traps:**
- The poll only runs once the unit has a device API key
  (`_cloud_http_ready`). The ten bench units get theirs from the card. A
  unit with no card has none until registration succeeds, so it cannot be
  asked to identify itself. Say so in the PR; do not invent a second
  channel.
- An SSID is the owner's data. The marketplace side stores it under its
  personal-data inventory (Cloud plan §1); the firmware sends it nowhere
  else.

---

## Later, not now

- **W8, Wi-Fi from one unit to the next.** After the first ten, only if a
  site has more than one unit.
- **OWE** on the setup network, if the bench shows the chip supports it.

## Order and dependencies

§1 → §2 → §3 → §4 → §5 → §8 → §7 → §6. §7 comes after §8 because the lock
needs the unlock command. §6 can move earlier once §3 is merged. The Cloud
half's §2 (Add unit) needs this plan's §8.

## Not verified by this plan

- Every bench item named above; the spec's section 7 list.
- That `iw dev wlan0 scan ap-force` works on the BCM43430 in AP mode.
- That `pycryptodome==3.24.0` exposes X25519 key agreement.
- The repro numbers in §1 came from this session's scratch test, not CI.
