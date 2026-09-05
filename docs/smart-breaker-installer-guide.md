# Binding this site's AWG to an Eaton AbleEdge breaker — installer guide

Read [smart-breaker-integration.md](smart-breaker-integration.md) first for
what the integration does and does not do. This page is the hands-on order of
operations.

> **Panel work is the licensed electrician's.** Installing the AbleEdge
> breaker, landing the AWG branch circuit on it, and wiring the WQM-1 relay
> contact into the AWG's enable / contactor circuit are all their job, under
> local code. WQM-1 is a dry-contact + API device. Nothing below asks you to
> open a panel.

## 0. What you need on site

- The AWG (or compressor load) on its **own** branch circuit, on an Eaton
  AbleEdge smart breaker (`hardwareType: emcb` — not the EV-charger variant).
- The breaker **commissioned** in the Eaton installer app under a Location
  (site) that belongs to BlueSignal's Eaton **Organisation**.
- From BlueSignal BUILD: the organisation service-account **client id**,
  **client secret**, and **subscription key** — or, once it ships, the word
  that the cloud proxy is live so nothing goes on the Pi.
- The **ampacity printed on the breaker** and its panel-directory label.
- Optional but strongly recommended: one free G5Q-14 relay channel on the
  WQM-1 wired by the electrician in series with the AWG enable input or
  contactor coil (COM→NO). This is the local interlock that makes
  `fail_safe: off` mean something when the cloud is gone.

## 1. Collect the Eaton identifiers

From the Eaton installer app / portal after commissioning, or via the API:

```
GET {api_base}/devices?locationId=<site UUID>
→ data[].id            ← smart_breaker_device_id  (UUID)
  data[].locationId    ← smart_breaker_site_id
  data[].hardwareType  ← must be "emcb"
  data[].serialNumber  ← write it on the commissioning sheet
```

Cross-check the serial against the label on the breaker so the UUID you bind
is the AWG circuit and not the neighbour.

## 2. Enter the binding on the unit

### 2a. From the Service Window (preferred)

Open `http://<pi-ip>:8080/settings/` → **AWG load control → Open the AWG
circuit page** (once a vendor is saved the page also gets its own "AWG
circuit" entry in the sidebar). Under **Breaker binding**:

| Field | Enter |
|---|---|
| Vendor | *Eaton AbleEdge smart breaker* |
| Site UUID | Eaton `locationId` (optional, must be a UUID if given) |
| Device UUID | the breaker's Eaton device UUID — **not** the serial number |
| Panel label | how the circuit is labelled in the panel, e.g. `Panel A / 14 — AWG-1` |
| Circuit ampacity | as printed on the breaker handle — required, never guessed |
| Interlock relay channel | the G5Q channel wired in series with the AWG enable |
| Fail-safe | *OFF* for compressor loads |
| Grace period / poll interval | defaults are fine unless the site says otherwise |
| Eaton client ID / client secret / subscription key | from BUILD; typed once, never shown again |

**Save binding**, then **Restart monitoring service** on the same page. The
page validates everything against the firmware's own schema (UUID shape,
ampacity range, enum values) before writing, so a rejected save leaves the
config untouched and tells you which field to fix. Leaving a credential
field blank keeps the value already on the unit.

### 2b. From the config file (SSH fallback)

Edit `/etc/bluesignal/config.yaml` with `sudo nano` over SSH. Quote the enum
values — bare `off`/`on` are YAML booleans and will be rejected.

```yaml
smart_breaker_vendor: "ableedge"
smart_breaker_site_id: "90965b9a-7dba-455a-a280-30d8a86d9b5e"     # Eaton locationId
smart_breaker_device_id: "f4628c73-0c62-491a-9454-a4f1b08e98ef"   # Eaton device UUID
smart_breaker_circuit_label: "Panel A / 14 — AWG-1"
smart_breaker_circuit_amps: 20          # from the breaker face — never guessed
smart_breaker_interlock_relay: 3        # G5Q channel in series with AWG enable; 0 = none
smart_breaker_poll_s: 60
smart_breaker_fail_safe: "off"          # off | last | on  (off for compressors)
smart_breaker_unreachable_grace_s: 300

smart_breaker_auth_mode: "direct"
smart_breaker_api_base: "https://api.em.eaton.com/api/v1"     # BUILD confirms per credentials
smart_breaker_token_url: "https://api.em.eaton.com/oauth2/token"
smart_breaker_client_id: "<from BUILD>"
smart_breaker_client_secret: "<from BUILD>"
smart_breaker_subscription_key: "<from BUILD>"
```

Then restart the service (binding and credentials are restart-required):

```bash
sudo systemctl restart bluesignal-wqm
journalctl -u bluesignal-wqm -n 50 | grep -i "AWG control\|smart breaker\|AbleEdge"
```

Expected: `AWG control: Eaton AbleEdge device <uuid> (site <uuid>, interlock
relay 3, fail-safe off)`. If instead you see `AbleEdge client not started:
… credentials incomplete` or `… no smart_breaker_device_id`, the feature is
**off for this boot** and the monitor runs as before — fix the config and
restart.

If `smart_breaker_circuit_amps` is still `0` you will see a warning at every
boot until the installer enters it.

## 3. Bench-check the path (no panel involved)

### 3a. From the Service Window

After the restart, reload the **AWG circuit** page. The **Live** row shows
the breaker position (ON/OFF), **Breaker link** (Up/Down with the last
error), the fail-safe mode and whether it has been applied, the last power
sample (A / V / Wh) and the interlock channel; it refreshes every 10 s. The
dashboard gets a matching **AWG breaker** traffic light.

Under **Switch**, type a reason (it is recorded by Eaton and in the audit
log), press **AWG ON**, confirm, then **AWG OFF**. The result line under the
buttons reads the same `ok` / `breaker` / `interlockOk` fields described in
the table below in plain words — "breaker confirmed", or "Interlock relay is
off, but the breaker did not confirm … queued".

### 3b. From the command socket (SSH)

Over the firmware command socket, using the same client the Service Window
uses (run from the install directory, e.g. `/opt/bluesignal/current`):

```bash
cd /opt/bluesignal/current
# Snapshot: link health, breaker position, last power sample
PYTHONPATH=src python3 -c 'import json; from service_window.cmd_client import send_command as c; \
  print(json.dumps(c("/var/run/bluesignal/cmd.sock", "awg_status"), indent=2))'

# Ask for the circuit ON, then OFF (reason is recorded by Eaton)
PYTHONPATH=src python3 -c 'from service_window.cmd_client import send_command as c; \
  print(c("/var/run/bluesignal/cmd.sock", "awg_set", state=True, reason="installer bench test"))'
PYTHONPATH=src python3 -c 'from service_window.cmd_client import send_command as c; \
  print(c("/var/run/bluesignal/cmd.sock", "awg_set", state=False, reason="installer bench test"))'
```

Read the result:

| Field | Good | Not good |
|---|---|---|
| `ok` | `true` | `false` — read `error` |
| `breaker` | `confirmed` | `unconfirmed` (vendor did not accept; for OFF the interlock still dropped and the open is queued) |
| `interlockOk` | `true` | `false` — relay coil did not respond; check the channel |
| `linkOk` (status) | `true` | `false` — see `lastError`, `unreachableForS` |

Watch the breaker's handle indicator / Eaton app while you do it; the
electrician confirms the AWG actually stops and starts.

## 4. Prove the fail-safe once

With the AWG running and the electrician present:

1. Pull the unit's network (Wi-Fi off, or unplug the router uplink). Stay
   on the unit's own Wi-Fi / LAN so the Service Window still answers.
2. Wait `smart_breaker_unreachable_grace_s` (default 5 min). The AWG page
   shows **Breaker link: Down** with the seconds counting up; the dashboard
   card goes amber ("has not answered for N minutes").
3. Expect: interlock relay drops → AWG enable opens → load stops. The card
   goes red ("fail-safe (OFF) has been applied"). Log line:
   `FAIL-SAFE OFF applied (interlock dropped, breaker open queued)`.
4. Restore the network. Expect: `Queued breaker open delivered`, then
   `Smart breaker link restored after …s`. The breaker is now open too and
   the card returns to green showing **OFF**.
5. Ask for ON again (**AWG ON** on the page, or `awg_set true`) — fail-safe
   never re-energises by itself.

If no interlock relay is wired, step 3 will only log and queue; the load keeps
running until the link returns. Decide with the customer whether that is
acceptable; if not, wire the relay.

## 5. Hand-off record

Write on the commissioning sheet: site UUID, device UUID, breaker serial,
ampacity as printed, interlock channel, fail-safe mode, the date the Eaton
secret was issued (**one-year rotation**), and who at BlueSignal holds the
credentials.

## Alternatives

- **No smart breaker on site** — Vendor *Relay only* on the AWG page (or
  `smart_breaker_vendor: "relay_only"` with `smart_breaker_interlock_relay: N`)
  gives the same `awg_set` command surface driving just the relay. No API, no
  fail-safe timer (nothing to lose contact with), no link card.
- **Cloud proxy** (`smart_breaker_auth_mode: "cloud_proxy"`) — keeps Eaton
  secrets off the Pi. Not yet available; the unit logs and disables the
  feature if you select it today.

## Still blocked on BUILD's side

- Eaton developer credentials and a commissioned test breaker for live smoke
  (see the design note's checklist). Until then everything above — firmware,
  socket commands and the Service Window page — has been exercised only
  against the in-memory fake and a stubbed command socket in `tests/`.
- Cloud Functions (out of this repo): `type: "awg"` command enqueue from the
  dashboard; event allow-list for `smart_breaker_failsafe` /
  `smart_breaker_restored`.
