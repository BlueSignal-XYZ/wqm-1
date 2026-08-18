# Install-day runbook — one WQM-1, bench to commissioned

Written for a single-operator install: bench prep the evening before, drive out,
commission on site, leave with the unit reporting. Reconciled against the code in
this repo as of 2026-08-18 — where a step differs from the printed installer
manual, the code is right and the manual defect is named in §6.

Companions: [`getting-started.md`](getting-started.md) (first install from
scratch), [`ota-runbook.md`](ota-runbook.md) §7 (field bootstrap),
`marketplace/docs/operations/device-reflash-runbook.md` (taking a unit back to
zero).

**Fill in before you go:**

| | |
|---|---|
| Site / customer | |
| Device id | `BS-WQM1-` + last 12 hex of the Pi serial |
| Uplink | Wi-Fi + cloud ☐ · LoRaWAN ☐ · both ☐ |
| Probes actually fitted | pH ☐ TDS ☐ Turbidity ☐ Temp ☐ RS485 ☐ |
| Relays wired to anything | no ☐ yes ☐ → what: |
| Site Wi-Fi SSID / password | (needed at flash time — see T6) |

---

## 0. Decide which software stack goes on the unit — first, not on site

Two different stacks exist in the tree and they are not compatible:

| Stack | Service(s) | Config | Where it's documented |
|---|---|---|---|
| **v2 firmware (this repo)** | `bluesignal-wqm`, `bluesignal-ota`, `bluesignal-service-window` | `/etc/bluesignal/config.yaml` | this repo |
| Legacy standalone scripts | `wqm1-sensor-read`, `wqm1-relay` | `/etc/bluesignal/wqm1.env` | `marketplace/docs/operations/wqm1-pi-setup.md` |

Units #1 and #2 were commissioned on the legacy stack. **Use the v2 firmware for
this install** — it is the only one with the guided Service Window wizard, signed
OTA, remote reboot, drift detection and per-probe fitment. Everything below
assumes it. `setup.sh` migrates a legacy flat install automatically
(`/opt/bluesignal/src` → `releases/legacy-1.1.0/` + `current` symlink), so a
re-used Pi is fine.

---

## 1. Bench prep (do this tonight, not in a field)

### 1.1 Flash the card

Raspberry Pi Imager → Pi Zero 2W → Raspberry Pi OS Lite (64-bit). In **Edit
Settings**:

| Setting | Value | Why it matters |
|---|---|---|
| Hostname | `wqm1` | every command here addresses `wqm1.local` |
| Username | `pi` | `setup.sh` adapts to any user, but the commands below assume `pi@` |
| Wireless LAN | **the site's SSID + password + country** | see T6 — this is the only chance to set it |
| SSH | enabled, password auth | break-glass access |

Re-using a card? Clear the stale host key or SSH shows a mismatch instead of a
prompt: `ssh-keygen -R wqm1.local`

### 1.2 Install the firmware

```bash
ssh pi@wqm1.local
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/bluesignal-xyz/wqm-1.git
cd wqm-1 && sudo bash setup.sh
sudo reboot                     # the boot-config overlays need it
```

### 1.3 Prove the hardware on the bench

```bash
sudo bash /opt/bluesignal/scripts/diagnostics.sh
```

Every line **PASS** except the LoRaWAN AppKey warning (expected until §1.4) and
GPS, which may warn indoors — take the unit near a window or accept it and
re-check on site. Any **FAIL** is a bench problem: fix it here, where you have
tools, not at the well.

### 1.4 Claim the device in the cloud — the one irreversible step

`POST /v2/devices/claim` mints **both** secrets and **returns them exactly once.
They are not retrievable afterwards.** Losing them means unclaiming and
re-claiming.

Easiest path: `cloud.bluesignal.xyz/cloud/commissioning/new` → the Scan step.
The camera is a fast path, never a gate — units without a QR sticker (the first
production run) drop through to manual device-id entry, and an unknown device is
claimed right there. It reveals the ingest key once, with copy-to-clipboard.
Command-line alternative: `marketplace/scripts/commission-device.cjs
--pi-serial <16hex> …` (dry-run first, then `--commit`).

Capture and store all three before closing the page:

- `api_key` — the HTTP ingest key, bound to this device id
- `app_key` + `join_eui` — LoRaWAN OTAA credentials
- `ttn_configured` / `ttn_registered` — **read these two.** See T2.

### 1.5 Walk the Service Window wizard as far as the bench allows

Power the unit, join its network, open `http://wqm1.local:8080`. Until setup
completes the unit serves only the wizard:

`welcome → pin → identity → network → cloud → sensors → done`

On the bench do **pin**, **identity** (screenshot the QR for your install
records) and **cloud** (paste `api_key`; add `app_key`/`join_eui` if this site
uses LoRa — the wizard verifies the key against the cloud there and then rather
than letting a typo surface as a unit that just never appears). Leave
**network**, **sensors** and **done** for site, where the readings and the signal
are real.

### 1.6 Prove one reading lands before you drive

Confirm the device appears on its cloud device page and its first reading
arrives. A unit shows `offline` until the first reading lands, then flips itself
online — that is by design, not a fault. **If nothing lands on the bench, nothing
will land at the site.**

### 1.7 Pack

Unit + Pi + HAT (seated), 24 V DC supply and the screw-terminal leads, USB-C 5 V
supply as the bench/debug fallback, LoRa (U.FL) and GPS (SMA) antennas, spare
flashed SD card, laptop with SSH, phone for the Service Window, pH 7.00 buffer +
pH 4.00 or 10.00, distilled water for rinsing, RS485→USB adapter and probe leads
if RS485 is fitted, and the claim secrets somewhere you can read them offline.

---

## 2. On site

1. **Mount and wire first, power second.** 24 V DC to the screw terminal is the
   production source; the USB-C supply is for debug only.
2. **Wizard → network**, at the final mounting position with the enclosure as it
   will actually sit. This step exists because a unit that associates fine with
   the lid open can drop when it closes, and that failure is invisible until the
   next day.
3. **Wizard → cloud** if not done on the bench.
4. **Wizard → sensors.** Tick only the probes physically fitted. **An unticked
   box is a real declaration of "not fitted"** and is what stops a bare board
   publishing a plausible-looking pH. Leave *ORP (analog)* unticked — see T3.
5. **Wizard → done.** Work the go/no-go checklist; finishing restarts monitoring
   with everything applied.
6. **Calibrate pH** if fitted: rinse in distilled water, stabilize in 7.00, rinse,
   stabilize in 4.00 or 10.00, verify 6.95–7.05 back in 7.00.
7. **RS485 probes**, if any: connect **one at a time** — Service Window → RS485
   Sensors → Add a new probe. Red = +12 V rail, black = ground, yellow = A,
   green/white = B. USB carries data only. A/B must never touch the power wires;
   that kills the probe's transceiver permanently.
8. **Relays**: leave automation inert unless this site is actually dosing. See T5.

---

## 3. Go / no-go before you leave the site

- [ ] `diagnostics.sh` — zero FAIL
- [ ] Heartbeat LED (LED 1) blinking at 1 Hz
- [ ] Every declared probe reading a plausible value; nothing declared that isn't fitted
- [ ] `journalctl -u bluesignal-wqm` shows `Stored id=` lines — **not** `No probes declared fitted`. See T11.
- [ ] A reading visible on the device page in the cloud, timestamped in the last few minutes
- [ ] Wi-Fi signal checked **with the enclosure closed**
- [ ] GPS fix acquired (LED 3), or knowingly accepted as unavailable
- [ ] LoRa joined — or knowingly accepted as not joining, per T2
- [ ] Factory PIN replaced (the wizard refuses 1234)
- [ ] Relays confirmed inert, or confirmed clicking on the correct channel
- [ ] Device id, DevEUI and the identity QR recorded for the install record
- [ ] `journalctl -u bluesignal-ota -f` shows the agent polling, and
      `devices/{id}/otaLastCheckAt` is moving — this is what makes SSH
      break-glass instead of load-bearing

---

## 4. If it goes wrong

| Symptom | First move |
|---|---|
| `i2cdetect` finds nothing | HAT seating. Then `sudo modprobe i2c-dev`, then check `dtparam=i2c_arm=on` and reboot |
| No DS18B20 | `dtoverlay=w1-gpio,gpiopin=4` in config.txt, wiring to GPIO 4, reboot |
| GPS silent | `enable_uart=1` + `dtoverlay=disable-bt`; cold start outdoors takes minutes; if bytes are garbled try `gps_baud: 38400` |
| GPS permission denied | `sudo usermod -aG dialout $USER` + reboot |
| Service won't start | `journalctl -u bluesignal-wqm -e` — usually YAML syntax in `/etc/bluesignal/config.yaml` |
| Cloud key rejected | Re-check on the wizard's Finish step; the probe is read-only and says which of key/network failed |
| SSH dead but :8080 alive | Known: use Service Window → Settings → Reboot the unit (typed confirmation). Don't drive back for it |
| Nothing works and light is fading | Swap in the spare SD card, re-run §1.2, re-use the **same** claim secrets — the device id derives from the Pi's CPU serial and does not change on re-flash |

---

## 5. After the install

SSH is break-glass from here. All routine firmware change goes over signed OTA
(`ota-runbook.md`) — an unsigned SSH change is invisible to the audit trail and
gets clobbered by the next release anyway.

---

## 6. Known traps

**T1 — the claim secrets are shown once.** `api_key`, `app_key` and `join_eui`
come back from a single `POST /v2/devices/claim` response and are never
retrievable. Capture them before you close the page (§1.4).

**T2 — a claim does not register the unit on TTN, so LoRa will not join.**
`functions/v2/devices.js` registers the device on The Things Network only when
`TTN_APP_ID`, `TTN_API_KEY` and `BLUESIGNAL_APP_EUI` exist in the Cloud Functions
runtime. None of the three is written to `functions/.env` by the deploy
workflow's "Write functions runtime env" step, and none is declared as a function
secret — the same shape of gap that left `STRIPE_SECRET_KEY` unset for months.
The claim response tells you the truth: `ttn_configured: false` means the server
is missing those variables, **not** that the unit is faulty. If this site needs
LoRa, register the DevEUI + AppKey by hand in the TTN console before you leave,
or plan on Wi-Fi + cloud for now.

**T3 — there is no analog ORP on Fin_3.** `config/pinmap.yaml` has AIN3 as spare
(`PH_INN`); the BNC front end is pH only. ORP comes from the optional RS485
digital probe. Leave "ORP (analog)" unticked in the wizard. **The installer
manual on the public site (Rev G) still shows ORP as a selectable mode on the pH
BNC** — a known defect awaiting Rev H. The correction is already written up as
item A.5 of [`manual-errata-revD9.md`](manual-errata-revD9.md); the Rev H brief
itself is not in this branch. If the customer reads that page, correct it in
person.

**T4 — the unit will report firmware `2.0.0`.** `VERSION` and `pyproject.toml`
both say 2.0.0, while the tree carries the first-boot wizard and RS485 support
that `getting-started.md` labels "v2.2+" / "v2.3+". The features are present; the
version label just hasn't been cut. Nothing to fix on the unit.

**T5 — relay automation ships inert and should stay that way.** `manual.override:
true` in `config/policies.yaml` holds all four relays off regardless of rules. The
rules in `config.yaml.example` are editable examples, not fixed functions. Enable
only after the driving probes are seated, reading in range, and calibrated.

**T6 — Wi-Fi can only be set when the card is flashed.** The Service Window is
served over that network, so there is no way to join a new network from the
device's own UI. If the site's SSID isn't known at flash time, bring a phone
hotspot with the same SSID/password you flashed, or bring a laptop and a spare
card.

**T7 — GPS defaults to 9600 baud.** Covers NEO-6/7/8/MAX-M10. A NEO-M9N ships at
38400 and will init cleanly while never producing a fix.

**T8 — every RS485 probe ships at Modbus address 1.** Connect and add them one at
a time or they collide.

**T9 — the cloud always acks a relay command as `done`.** It cannot tell whether
the relay physically moved. The only confirmation is listening for the click at
the unit. Our boards are active-low.

**T11 — a unit that records nothing looks exactly like a healthy one.** The
fitment declaration is what decides whether a probe is sampled at all, and an
undeclared probe is silently not read. Get it wrong and the unit heartbeats,
passes diagnostics, reports uptime, disk, CPU and link quality, and stores no
water data at all — while the cloud device page keeps showing whatever it last
received, with green sensor cards, for as long as you leave it. A field unit ran
that way for fifteen days: 249 readings stored, 13,938 rejected by the cloud and
marked permanently failed. Since 2.1.0 the firmware refuses to manufacture the
empty rows and says `No probes declared fitted` in the journal instead, but the
declaration is still yours to get right. **Before you leave, confirm the journal
shows `Stored id=` lines carrying real values** — a green dashboard is not
evidence.

**T10 — a re-flash does not give you a new device.** The device id derives from
the Pi's CPU serial, so a re-flashed unit re-attaches to the old record, readings
and alerts. To genuinely start over, purge first —
`marketplace/scripts/purge-device.cjs`, dry-run then `--apply`.
