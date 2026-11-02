# Saturday 7 Nov 2026 — flash, boot, join, claim

A step-by-step bench script for one WQM-1. At the end of the day the
owner has a card that boots, a phone that joined it, and a Cloud
dashboard that shows the unit claimed with a first reading.

This page is the script. Expected result and recovery sit under each
step. The firmware half is this repo. The Cloud dashboard is
`https://cloud.bluesignal.xyz` (assets Last-Modified 27 Oct 2026).

Never target, write to, or change the path of the live field unit
`BS-WQM1-0000a92e4e7d`. OTA stays on hold
([ota-hold.md](ota-hold.md)): no `POST /v2/devices/:id/firmware/target`.

The WQM-1 is designed to connect with water-from-air and rainwater
systems. It reports readings. It does not control those systems. No
safe/potable claim. No UL/ETL/NSF/FCC claim for this unit.

Companions: [flash-a-card.md](flash-a-card.md) (write the image),
[flash-and-verify.md](flash-and-verify.md) (the shorter installer
checklist), [installer-mistakes.md](installer-mistakes.md) (2.4 GHz,
weak signal, sign-in Wi-Fi).

## What "go" means

All of these, on this bench board, with this card, this phone, and this
Cloud account:

1. Raspberry Pi Imager writes the card and says **Write Successful**.
2. The open network **BlueSignal Setup** appears within 10 minutes of
   first power.
3. The phone opens `http://192.168.4.1/setup/` (captive portal or typed).
4. Join of a 2.4 GHz WPA2 network succeeds: setup network gone, phone
   back on the site Wi-Fi, Cloud reachable from the unit.
5. Cloud claim from the signed-in dashboard succeeds for this serial.
6. Cloud shows at least one reading (pH / TDS / turbidity / temperature
   as fitted). Journal has `Stored id=` and `All subsystems initialised`.

Anything short of that is **no-go** for that step. Recover as written;
do not reflash unless the table says to.

## Materials

- One microSD card, 16 GB, that you can erase.
- Raspberry Pi Imager on the laptop.
- The newest green **Actions → Card image** artifact for **master**:
  `wqm1-pi-zero-<commit>.img.xz` for a Zero W / Zero 2 W, or
  `wqm1-pi-5-<commit>.img.xz` for a Pi 5. Do not mix them.
- The matching HAT on the Pi. 24 V on the screw terminal for analog
  readings; USB-C on the Pi is enough for the join and claim. **Never
  both at once.**
- A phone with mobile data that you can turn off.
- A 2.4 GHz WPA2 network whose password you know. Not 5 GHz-only, not
  WPA3-only, not enterprise, not a sign-in page.
- A BlueSignal Cloud account that can open Devices and Claim.

Firmware on the card is `VERSION` 2.4.0 plus whatever master has
landed. Confirm the card label after the write.

## 1. Write the card

1. Download the `.img.xz` from the green Card image run. Do not unzip
   it; Imager reads xz.
2. Imager → **Use custom** → that file → the SD card.
3. When Imager offers OS customisation (hostname, user, Wi-Fi), choose
   **No**. Customisation writes a site network over the setup network.
4. Wait for **Write Successful**. Do not pull the card while it says
   *Verifying*.

**Expected:** Imager ejects the card. Optional: remount and
`cat /Volumes/bootfs/wqm1-card.txt` shows firmware version, commit, and
**BlueSignal Setup**.

**Failed write / Verifying interrupted:** throw the card away for this
bench. Write a second card. Do not boot a half-written image.

**Wrong file for the board:** a Zero W given the Pi 5 image does not
boot. Use the table in [flash-a-card.md](flash-a-card.md) §1.

## 2. First boot

1. Card into the Pi on the HAT. Power from 24 V or from the Pi's own
   port, not both.
2. Wait up to 10 minutes on a Zero W (grow, keys, firmware on one core).
3. On the phone, join the open network **BlueSignal Setup**. No password.
   Turn **mobile data off**. iPhone: **Use Without Internet**. Android:
   **Stay connected**.

**Expected:** the network is in the phone's list. `wqm1-status.txt` on
the boot partition (after a clean power-off) has `hotspot: up` and
`portal: up (80)` and `COMMISSION: READY` once first-boot checks pass.

**No network after 10 minutes:** power off, read
`/Volumes/bootfs/wqm1-status.txt`, use the table in
[flash-a-card.md](flash-a-card.md) §6. Typical recoveries:

| Status line | Recover without reflashing? |
|---|---|
| missing file | No. Rewrite the card. |
| `BOOT: stage=boot` and little else | No. Paste the file. New card if the first write was interrupted. |
| `hotspot: failed:` | Power cycle once. If it fails again, paste the file. |
| `station: ok <ip>` | Imager customisation. Rewrite, choose **No**. |
| `power: undervoltage seen` | Shorter cable, known-good supply. Same card. |
| `card: moved from …` | Setup must join again on this Pi. Same card. |

**Power pulled mid-boot:** firstboot records the step as started and
resumes it (`tools/golden/overlay/usr/local/sbin/wqm1-firstboot`). Power
on again. Do not reflash.

**Read-only or corrupted card:** `COMMISSION: FAIL` or no status file.
Rewrite on a second card. A directory-fsync failure on config writes is
tolerated in software; a dead card is not.

## 3. Phone on the setup page

1. The captive portal should open the Wi-Fi page. If it does not, type
   `http://192.168.4.1/setup/` (no `:8080`).
2. There is no PIN. The page sets only this unit's Wi-Fi.
3. Confirm the probes that are actually fitted. USB-C alone will not
   power the analog rails; 24 V will.

**Expected:** a list of 2.4 GHz networks, four bars, English (or
Spanish) sentences. A 5 GHz-only router is named and refused before
Join. A signal below −70 dBm is warned.

**Blank page / phone bounced to cell:** mobile data still on, or the
phone dropped **BlueSignal Setup**. Turn data off, rejoin, type the
address. See [installer-mistakes.md](installer-mistakes.md).

**Phone leaves mid-join:** the join keeps running. Wait. If it failed,
the setup network comes back with the reason. Do not tap Join again
until the page says failed or the setup network is back.

**Only 5 GHz in range:** turn 2.4 GHz on at the router, or pick the
2.4 GHz name (often without `-5G`). Same card.

**Enterprise / WPA3-only / WEP:** the page refuses that row. Use a
normal WPA2 password network. Same card.

## 4. Join the site Wi-Fi

1. Pick the 2.4 GHz WPA2 network. Type the password. Use **Show
   password**. A trailing space is stripped unless you tick the box.
2. Tap Join. The setup network **will drop**. That is expected.
3. Wait up to about a minute.

A join is success only when all four are true (`utils/netctl.py`
`join_network`):

1. `wlan0` is on that SSID.
2. It has a real IPv4 lease (not `169.254.x`).
3. The gateway or DNS answers.
4. BlueSignal answers over HTTPS.

**Expected:** the phone lands on the site Wi-Fi. The setup network is
gone. `wqm1-status.txt` has `station: ok <ip>` and
`portal: down: station`.

**Wrong password:** setup network comes back within about a minute.
Page says the password was not accepted. `station: fail: wrong password`.
Type it again. Do not reflash.

**Network not found:** page says so. Move closer, confirm 2.4 GHz, or
type a hidden name. Same card.

**No DHCP / only 169.254:** page says the router did not give an
address. Turn DHCP on, or try another network. Same card.

**Address, no gateway/DNS (`no_internet`):** page says the unit cannot
reach the router or DNS. Fix the router. Same card.

**Address, gateway, but BlueSignal not reached (`no_bluesignal`):**
page says BlueSignal was not reached. Check outbound HTTPS, captive
sign-in, DNS, and the clock (a Pi with no RTC and no NTP can fail TLS).
Same card. Power cycle after the network is fixed.

**Captive sign-in page:** the unit cannot tap Agree. Use a password
network, or ask the site to allow this unit without a sign-in page.
Setup network comes back. Same card.

**Power pulled mid-join:** on reboot, `ensure_reachable` raises
**BlueSignal Setup** if the station is not up. The last join reads as
`interrupted`. Join again. Same card.

## 5. Claim on Cloud

1. Stay signed in at `https://cloud.bluesignal.xyz` on a network that
   has internet (the phone, once it has left the setup network, or the
   laptop).
2. Scan the box-label QR, or open Claim and type the serial
   `BS-WQM1-` + the last 12 hex of the Pi serial (from the card label
   or the setup page).
3. If Cloud asks for a claim code, type the code printed under the QR
   on **this** unit's box. The firmware identity QR (`v`, `device_id`,
   `pk`) does not carry a `claim_token`.
4. Wait. The unit POSTs `/v2/devices/register` itself (`device_id`,
   `pk`, `fw`, `ts`, `sig`). You never paste an HTTP key.

**Expected:** Cloud shows the unit on this account. Firmware register
state is `registered` (or `pending` / `pending_identity` until the
dashboard assigns it). Heartbeat is POST
`/v2/devices/<id>/heartbeat` with `X-API-Key`. Pending commands are
GET `/v2/devices/<id>/commands/pending` with the same key.

**Cloud 5xx or unreachable:** register stays `pending` and retries
(5 s → 1 h). Heartbeat and ingest idle until a key exists. Do not
reflash. Wait, or move the unit onto a network that can reach
`us-central1-waterquality-trading.cloudfunctions.net`.

**403 `claim_token_required`:** the firmware QR is not a claim code.
Use the box-label URL (`/claim/<serial>#t=…`) or type the printed
code. Same card. If this account's Cloud still requires a token the
box does not have, that is a Cloud/label mismatch — stop and do not
guess a code.

**409 already on another account:** ask that account to release the
unit from the device page (`unclaimDevice`). Same card, same identity
key. Do not reflash; a reflash mints a new Ed25519 key and Cloud
returns `pk_conflict`, which the firmware **does not** retry.

**Duplicate / reused claim code:** Cloud rejects it. Use this unit's
own code. Same card.

**New owner re-claim:** the previous account unclaims. Heartbeat 401
clears the HTTP key and the unit re-registers the **same** `pk`. If
someone reflashed first, you are in `pk_conflict` — restore the old
identity seed or unclaim/re-register on Cloud. Firmware has no
production factory-reset route.

**Reflash of an already-claimed unit:** new key, `pk_conflict`, stop.
Recovery is on Cloud (release the old key / accept the new `pk`), not
on the card. Do not keep rewriting the card hoping it will self-heal.

**Clock not set (no RTC, no NTP):** readings are stored with
`clock_source=unsynced` and still upload. Register `stale_ts` waits
for NTP or uses `serverTime`. A TLS "certificate not yet valid"
failure is `no_bluesignal` until the clock is stepped. Same card;
give the unit a network that serves NTP, or wait for Cloud's Date
header path after a successful HTTPS answer.

## 6. First reading

1. Power from 24 V if analog probes are fitted.
2. Wait one sample interval (default 60 s).
3. Cloud tiles: pH, TDS, turbidity, temperature as fitted. Optional
   RS485 (chlorine / ORP / multi) only if that probe is on the bus.

**Expected:** Cloud has a row. On a bench with SSH (optional):
`journalctl -u bluesignal-wqm -n 80` shows `All subsystems initialised`
and `Stored id=`. A pending-command poll that finds nothing, or a
Cloud that is unreachable, must not stop that line.

**No probes declared:** journal `No probes declared fitted`. Nothing
is stored. Fit and declare them in the Service Window (Sensors). Same
card.

**ADC / RS485 missing:** journal names the hardware note (for example
`ADC missing at 0x48`), not "no probes declared". Check 24 V, HAT
seating, and the RS485 jack. Same card.

**OTA:** leave it. `ota_enabled: false` on a unit, and no firmware
target, is the hold. A download failure keeps the current release. A
power loss mid-apply rolls back to the previous release on the next
agent start.

## 7. Before you leave the bench

- [ ] Setup network gone; unit on the site Wi-Fi.
- [ ] Cloud shows this serial claimed on **this** account.
- [ ] At least one reading (or a journal reason if a probe is absent).
- [ ] No `power: undervoltage seen`.
- [ ] Owner has the box QR and knows **BlueSignal Setup** is first
      install, not daily use.
- [ ] No OTA target was set.

SSID or password change **after** a successful join: the 5-minute
Wi-Fi retry only runs while the setup hotspot is still up. A
commissioned unit on a dead station stays there until a **power
cycle**, which raises **BlueSignal Setup** if the saved network will
not come up. Then join the new network from the phone. No reflash.

## What only this bench can prove

Software tests do not replace these:

- The real radio raises **BlueSignal Setup** on this HAT and this Pi.
- This phone's captive portal opens `http://192.168.4.1/setup/`.
- This site's 2.4 GHz AP accepts the unit and hands a lease.
- BlueSignal HTTPS from this network, with this clock.
- Cloud claim for this serial on this account, including whether a
  claim token is required.
- Analog readings on 24 V, and RS485 if a probe is fitted.
- A deliberate power pull mid-boot, mid-join, and (if ever allowed)
  mid-OTA.

Those stay **unverified** until someone does them on the hardware and
writes the result next to the step.
