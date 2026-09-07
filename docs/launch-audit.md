# WQM-1 pre-launch audit — firmware 2.1.1 and PCBA rev Fin_3

Audit of the shipping firmware and the board against what the product claims
to do, done 2026-09-07 before the public firmware release. Every claim in the
README, the hardware overview and the installer runbook was checked against
the schematic (`hardware/fab/PCBA_Fin_3.pdf`), the BOM, and the code path
that implements it.

**Bottom line.** The sampling, storage, cloud sync, supervisor, OTA and
Service Window paths are in good shape and match their claims. Two claims did
not hold, and both are headline features:

1. **LoRaWAN could not work on any network.** Four independent defects, each
   sufficient alone: the antenna switch was never enabled, the uplink sat on
   a fixed 915.000 MHz carrier that is not a US915 channel, the receiver
   never used the inverted IQ that every LoRaWAN downlink is sent with, and
   the transmitter was never retuned after the RX2 window. No JoinAccept could
   ever have been received. **Fixed in this branch; must be proven on a bench
   against a gateway before launch.**
2. **The README told a new user to power the unit from USB-C.** On this board
   the 24 V input is the only source for the analog rails and the relay
   coils. On USB-C the ADS1115, the pH front-end and all four relays are
   unpowered, and the very next step (`diagnostics.sh`) fails. **Fixed in the
   docs.**

Everything else is graded below. Items marked **FIXED** are changed in this
branch with tests; items marked **DECISION** need Jacques to choose; items
marked **BENCH** cannot be settled from the repository and are on the
validation checklist at the end.

---

## 1. Hardware facts established from the schematic and BOM

These drove the firmware review. Pin assignments in `config/pinmap.yaml` and
`src/utils/config.py` all match the schematic (I²C on GPIO2/3, ADS1115 at 0x48
with ADDR to GND, ALERT/RDY on GPIO5, 1-Wire GPIO4, relays GPIO17/27/22/23,
LEDs GPIO24/25/12/13, SX1262 NSS/RST/BUSY/DIO1 on GPIO8/18/20/16, GPS on
UART0 with EXTINT GPIO19, fan GPIO21).

| Block | What the board actually does | Consequence for firmware / docs |
|---|---|---|
| Power tree | 24 V → LMR51450 → **+5 V** (Pi header pins 2/4, AMS1117 3.3 V for LoRa/GPS/LEDs, turbidity probe). 24 V → TPS560430 → **+6.5 V** → LP2985 → **+5VA** (ADS1115, LMP91200, REF3020, LMV321) → ME6206 **+3.0 V** and TPS60400 **−3.0 V** (CD4060, LM324). 24 V → D17 → **VRLY** (relay coils). | USB-C powers the Pi, radio and GPS only. Analog and relays need 24 V. Two supplies at once fight on the Pi's 5 V rail. |
| ADS1115 | Single ADS1115BQ, VDD = +5VA, I²C via PCA9306. AIN0 = TDS chain (0–2.3 V), AIN1 = turbidity via LMV321 (0–4.5 V), AIN2 = LMP91200 VOUT, AIN3 = LMP91200 VOCM. | Four channels, not six; one converter, not two. AIN1 needs the ±6.144 V PGA. AIN2/AIN3 are a differential pair. |
| pH | LMP91200 with REF3020 2.048 V. No SPI to the Pi: the AFE runs on power-on defaults. VOUT and VOCM go to the ADC through 1 kΩ. BNC centre → INP, shell → VCM. | The correct measurement is AIN2 − AIN3 (or AIN2 minus a measured VOCM). Firmware reads AIN2 single-ended and relies on two-point calibration to absorb VOCM. |
| ORP | No analog ORP circuit. | `orp_enabled` (analog) must stay false; ORP is RS485 only. Docs already say so. |
| TDS | CD4060 free-running square wave (R10 10 k / C60 1 nF) → LM324 precision rectifier → R47/R48 LPF → D20 clamp → AIN0. | Firmware is sampling-only, as documented. Absolute ppm/V unknown until the cell constant is measured. |
| Turbidity | J1: probe on +5 V (digital rail), signal through R56/LMV321 → AIN1. | Works on USB power electrically, but the ADC it feeds does not. |
| Relays | 4 × G5Q-14 (10 A / 250 VAC, 24 V coil on VRLY), S8050 + LTV-354T, active-high, flyback diodes. COM/NO/NC on 5.08 mm terminals. | GPIO17/27/22/23 default pull-down on BCM2837, so no click at power-on. Coils cannot close without 24 V. |
| LoRa | NiceRF **LORA1262-915TCXO** module. Pins 7, 12, 14 NC; pin 11 `RXE` unconnected. Antenna through R30 0 Ω to an **SMA** (not U.FL). The module's antenna switch is "integrated and controlled by the chip", i.e. by SX1262 DIO2. | The host must enable DIO2-as-RF-switch (it did not). Docs said U.FL. |
| GPS | u-blox MAX-M10S, SMA antenna, V_BCKP tied to 3.3 V (no backup cell). TXD → GPIO15, RXD → GPIO14. | Every power cycle is a cold start; the README's "warm start in ~25 s" only applies while the unit stays powered. 38400 baud is field-verified. |
| DS18B20 | JST J3, 4.7 kΩ pull-up to 3.3 V on GPIO4. | Kernel w1 driver; correct. |

---

## 2. Findings

### BLOCKERS (the product does not do what it says)

**B1 — LoRa antenna switch never enabled.** `src/radio/sx1262.py` never
issued `SetDio2AsRfSwitchCtrl (0x9D)`. On the LORA1262 the switch is driven
by DIO2; with it disabled the PA transmits into the isolated port and nothing
reaches the SMA. This alone explains "No JoinAccept received" on every unit.
**FIXED** — `init()` now enables it (and runs image calibration for
902–928 MHz). *BENCH: measure conducted power at the SMA.*

**B2 — Uplink on a fixed 915.000 MHz carrier.** `LORA_FREQUENCY =
915_000_000` is not a US915 channel (125 kHz uplinks are 902.3 + 0.2·n MHz).
The `tx_frequency` argument existed but nothing set it; there was no channel
table and no hopping. **FIXED** — the MAC hops across the configured sub-band
(`lora_sub_band`, default 2 = TTN FSB2: channels 8–15 + 500 kHz ch 65) and
applies the network's CFList / LinkADRReq channel mask. Single-channel
operation at +22 dBm was also outside FCC §15.247's hopping requirement.

**B3 — Receiver never demodulates a downlink.** `receive()` inherited the
uplink packet parameters: standard IQ, CRC on, and the uplink frequency at
125 kHz for RX1; RX2 was 923.3 MHz at **125 kHz** with LDRO forced on for
SF12. LoRaWAN downlinks are inverted-IQ, no CRC, 500 kHz, and SF12/500k is
sent without LDRO. **FIXED** — RX sets its own packet params (inverted IQ,
CRC off, datasheet §15.4 register workaround), RX1 is on downlink channel
(uplink ch mod 8) at the RX1DROffset data rate, RX2 is SF12/500 kHz, and LDRO
follows symbol time.

**B4 — TX parameters not restored after RX2.** `set_rx_config()` retuned the
chip and `send()` never retuned it back, so every uplink after the first RX2
window went out at 923.3 MHz SF12. **FIXED** — the driver re-applies the
stored TX configuration before every transmission.

**B5 — README power instructions.** "Connect the USB-C power supply" followed
by "diagnostics should show ADS1115 found at 0x48" cannot both be true (see
§1). **FIXED** — README and hardware overview now state that 24 V is required
for analog and relays, USB-C is Pi-only bench power, and the two must never be
connected together.

### HIGH

**H1 — Turbidity clipped at the clean end.** The ADS1115 driver used ±4.096 V
on every channel while the turbidity chain is 0–4.5 V with a 4.1 V clear-water
reference. Anything above 4.096 V read as 4.096 V, so the cleanest ~0.4 V
(roughly the first 300 NTU above "clear") collapsed to one value, and a
clear-water calibration was taken at the saturation point. **FIXED** —
per-channel PGA, ±6.144 V on AIN1. *BENCH: compare VIN1 on a DMM with the
firmware voltage in clear water.*

**H2 — JoinAccept accepted without a MIC check.** Any 0x20 frame (another
network's JoinAccept, or noise) became a "successful" join with garbage keys
that were then persisted; the device never rejoins once `joined=1`. **FIXED**
— MIC verified; DLSettings (RX1DROffset, RX2 DR), RxDelay and CFList applied.

**H3 — Downlinks accepted without MIC or frame-counter checks.** FPort 100
drives relays. A captured "relay ON" frame could be replayed at any RX window
and any spoofed frame decrypted to random bytes. **FIXED** — MIC verified
with NwkSKey, 32-bit FCntDown reconstructed and enforced monotonic, confirmed
downlinks acknowledged.

**H4 — RX window timing.** Hardcoded 1 s / 2 s; the network's RxDelay (TTN v3
sends 5 s) was ignored, and the join RX2 opened at ~7 s instead of 6 s.
**FIXED** — windows are scheduled from the TX-done timestamp; RxDelay,
RX1DROffset, RX2 and the channel mask persist with the session (DB schema v5,
`lorawan_session.mac_params`) so a reboot does not lose them.

**H5 — Relay schedule window evaluated in UTC.** `policies.yaml`'s
`07:00–21:00` was compared against `datetime.now(UTC)`. In Texas that is
01:00–15:00 local. **FIXED** — the default clock is the unit's local time
(the timezone set at imaging); adaptive baselines still convert to UTC.

**H6 — Customer relay rules lost on every upgrade.** The firmware read
`policies.yaml` from the release tree, and both `setup.sh` and the OTA agent
install a fresh tree with the stock file. A site's rules and its
`manual.override: false` reverted silently on each update. **FIXED** —
`/etc/bluesignal/policies.yaml` is read first; `setup.sh` seeds it once and
never overwrites it.

**H7 — Service Window had no CSRF protection.** Every state-changing route
(relays, AWG circuit, reboot, PIN, keys) is a plain POST and Flask's session
cookie defaulted to SameSite=None, so any page on the installer's phone could
POST `/relays/set`. **FIXED** — `SESSION_COOKIE_SAMESITE=Lax`, HttpOnly.
Proper per-form CSRF tokens remain a follow-up.

**H8 — Duration timers only run at the sampling cadence.** `duration_s` is
swept inside `RulesEngine.evaluate()`, which the sampling worker calls once
per `sensor_read_s` (60 s default, up to 3600 s). A "30 s" acid dose runs until
the next sample — 60 s, twice the configured volume — and if sampling stalls
(no probe declared, worker in backoff) the relay stays on. **FIXED** —
`RelayController` owns a per-channel timer thread: rules, LoRa downlinks and
cloud `durationSeconds` all arm it, a later command replaces it, and
`limits.max_continuous_on_minutes` in `policies.yaml` (0 = off) is a hard
ceiling on any on-period from any source.

**H9 — Flatline detector will trip on genuinely stable water.** A DS18B20
quantises to 0.0625 °C and pH is rounded to 0.01 through a 5-sample median;
in a large, still body of water twenty identical readings in a 20-minute
window are normal. Standard deviation 0 < noise floor → `sensor_stuck` →
every relay driven by that sensor is dropped to fail-safe (including the
dosing pumps, via the temp > 45 °C shutoff rules) and its rules stay
suspended until the value moves. **FIXED** — "flat" is now advisory (event
+ health "attention", no rule suspension, no relay reversion); only
"no_data" suspends. A dead analog input still arrives as `no_data` via the
rail / open-input checks. *BENCH: two hours in a bucket of still water should
now produce an advisory event and nothing else.*

**H10 — pH default calibration does not match the front-end.** Defaults are
V@pH4 = 1.04 V, V@pH7 = 1.50 V (a positive 153 mV/pH slope). Through the
LMP91200 the electrode voltage rides on VOCM (≈ VREF/2 ≈ 1.02 V unless the
AFE's default VCM selection differs) with the Nernst sign the other way
(pH 4 above pH 7). An uncalibrated unit therefore publishes pH that is both
offset and inverted, and nothing marked pH `uncalibrated`. **FIXED** — pH
now publishes nothing, with status `uncalibrated`, until a real two-point
calibration is stored (`CalibrationManager.is_calibrated("ph")`); the
calibration page says a service restart applies it. *BENCH*: measure VOUT
and VOCM in pH 4/7/10 buffers and record them. Longer term, read AIN2−AIN3
differentially so VOCM drift cancels.

**H11 — OTA self-test needs a fresh reading.** `_self_test()` passes only when
the main service is active **and** a readings row is newer than the apply
time. A unit with no probes declared, a probe outage, or an AWG-control-only
deployment can never update: every release rolls back after
`ota_self_test_timeout_s`. **FIXED** — the supervisor touches
`/var/lib/bluesignal/alive` every 30 s while every worker is alive, and the
self-test passes on "service active AND (fresh reading OR fresh liveness
beat)".

**H12 — Cayenne LPP clips TDS, turbidity and ORP at 327.67.** They are packed
as value×100 into int16 (LPP type 0x02). Real ranges are 0–2000 ppm, 0–3000
NTU, −500…+900 mV. Over LoRa the cloud receives 327.67 for anything above,
with no error flag; `tests/test_lora_tx.py` codifies it. **DECISION** —
change the scaling (e.g. ppm/10, NTU/10, mV/10) together with the platform
decoder; not changed here because it is a cloud contract.

### MEDIUM

- **M1 Relay commands from the cloud, the Service Window socket and LoRa
  downlinks bypass the rule-level guards** (manual override, cooldown,
  hourly budget) — by design, manual control must work in override. **Partly
  FIXED** with H8: every source now shares the controller's timer, a cloud
  duration is enforced there (and replaced, not stacked, by a later
  command), and `max_continuous_on_minutes` caps a duration-less ON from any
  source. The hourly budget still counts only rule-driven on-time.
- **M2 `max_on_minutes_per_hour` is a start gate, not a cap.** A relay
  already on (duration 0) is never turned off by the budget; the budget only
  refuses a new ON. Outside the schedule window an ON rule with duration 0 is
  not released by its OFF rule until the window reopens (documented in
  `policies.yaml` now).
- **M3 Smart breaker: no short-cycle protection.** **FIXED** —
  `smart_breaker_min_off_s` (default 180, hot, remote-tunable): an ON within
  that many seconds of the last OFF (any source, fail-safe included) is
  refused with `retryAfterS`.
- **M4 DevNonce is random and joins retry every 300 s without backoff.**
  **FIXED** — monotonic DevNonce counter persisted in `mac_params` before
  each JoinRequest; join attempts back off after the third (doubling to an
  hour).
- **M5 No rejoin or session-recovery path.** **FIXED** — a LinkCheckReq
  rides every 24th uplink that has seen no downlink; after three unanswered
  the session is forgotten and rejoined. The `/lora/` page has a "Forget
  session and rejoin" button (socket action `lora_rejoin`).
- **M6 Rotation never drops pending rows.** **FIXED** — with
  `cloud_enabled: false` the oldest pending rows beyond `db_max_rows` are
  rotated too (logged as a warning); with the cloud on, pending rows remain
  untouchable.
- **M7 Service Window PIN.** Four digits, plaintext in `config.yaml`, rate
  limited to 5 tries/minute per IP: exhaustive search in ~33 hours from the
  LAN. **Mitigated** — 15 wrong PINs within an hour lock the address out for
  15 minutes (constant-time compare added); the wizard already accepts 4-8
  digits, so use more than four on a shared network.
- **M8 Sync duplicates.** If the ingest POST succeeds but the response is
  lost, the rows stay pending and are re-sent; the payload has no client
  id, so de-duplication depends on the server keying on deviceId+timestamp.
- **M9 ADS1115 conversion timeout returned a stale value** (the previous
  channel's result under this channel's name). **FIXED** — raises, the sensor
  reports `read_failed`.
- **M10 SX1262 BUSY timeout was non-fatal**, so an unpowered or absent radio
  "initialised" and the README's "LoRa init failed" path never fired.
  **FIXED** — `init()` raises when BUSY stays high after reset.
- **M11 `bluesignal-provision.service` is copied but never enabled** by
  `setup.sh`. **FIXED** — enabled.
- **M12 DevEUI prefix `0018B2`** is registered to Adeunis RF unless BlueSignal
  holds it; TTN enforces DevEUI uniqueness. Verify the OUI.
- **M13 GPS backup.** V_BCKP is tied to 3.3 V, so every power cycle is a cold
  start (30–60 s outdoors). The README's warm/hot-start figures only apply
  while the unit stays powered.
- **M14 `pip3 install --ignore-installed`** in `setup.sh` reinstalls
  Debian-managed packages (pyyaml, flask) over apt's copies.

### LOW / INFO

- LoRa1262 pin 11 (`RXE`) is unconnected. On this module the switch is
  chip-controlled, so this is expected; *BENCH-confirm against the module
  datasheet that RXEN needs no host drive.*
- RSSI was read with `GetRssiInst` after the packet ended (noise floor).
  **FIXED** — `GetPacketStatus` packet RSSI and SNR (the SNR feeds
  DevStatusAns).
- DS18B20 driver did not filter the 85.0 °C power-on value (**FIXED**); the
  GPS `power_cycle()` comment said "low" while the code pulses high
  (comment fixed; harmless on an M10 in continuous mode).
- Service Window `SECRET_KEY` is regenerated per process start unless set, so
  sessions log out on every restart (annoyance, not a hole). Non-constant-time
  PIN compare (LAN-only, low value).
- ADS1115 ALERT/RDY is pulled up to 3.3 V while the converter runs at 5 V —
  fine for an open-drain output, and the firmware polls the OS bit anyway.
- Relay state bitmask is updated without a lock from three threads; only the
  reported bitmask can be momentarily wrong, never the coil.
- Marketing copy elsewhere describes a "six-channel, dual-ADS1115,
  65 × 56.5 mm" board. Fin_3 is one ADS1115, four analog channels, 120 × 105
  mm. The README is right; keep other collateral consistent with it. "Up to
  15 km" is a line-of-sight LoRa figure, not something this firmware at SF9
  has demonstrated.

---

## 3. Verified correct (no action)

- **Sampling and fitment.** An undeclared probe is never sampled; a None
  reading is never turned into a number; all-null cycles are not stored;
  TDS/turbidity carry a status instead of vanishing. RS485 5-in-1 values
  supersede analog. Per-driver median filters, rail and open-input checks
  match the chains they guard (after H1).
- **Storage.** WAL, per-thread connections, busy_timeout, commit per insert,
  pending rows never rotated, three-state sync with per-row server acks,
  backfill flag, migrations stamped per step.
- **Cloud client.** urllib with default certificate verification, timeouts,
  bounded retries, 4xx not retried, HTTP(S)-only URL guard, secrets never
  logged, credentials and URLs not remotely configurable.
- **Supervisor / watchdog.** `Type=notify`, `WatchdogSec=180`, pets only
  while every worker's beat is fresh; worker crashes are contained with
  backoff; radio worst case (join ≈ 14 s) is far inside the liveness
  deadline; GPS blocks only its own thread.
- **Relays.** `GPIO.setup(..., initial=LOW)` on every pin, `all_off()` at
  start, on SIGTERM/atexit, before a requested reboot, and when a driving
  sensor is suspended; rules never act on None or a suspended sensor;
  `manual.override: true` ships as the safe default.
- **OTA.** Ed25519 over the canonical manifest with the tarball hash pinned
  inside it, product/version pinned, size and free-disk guards, tar-slip
  validation plus Python's `data` filter, staging + atomic symlink flip,
  self-test with rollback, boot-time recovery of an interrupted apply,
  pruning that protects current and previous, `requiresDeps` refused.
- **Setup.** Correct `/boot/firmware` handling on Bookworm/Trixie, idempotent
  overlay edits, serial console removed and getty masked, service user in
  the right groups, tmpfiles entry for the socket directory, versioned
  release layout.
- **LoRaWAN crypto.** JoinRequest MIC, JoinAccept "decrypt-by-encrypt", key
  derivation, uplink MIC/B0, CTR encryption with the right direction byte,
  DevAddr byte order, session persistence with FCntUp never going backwards
  (now persisted before TX). Cayenne encoding sizes and types (except H12).
- **Identity.** DevEUI derived deterministically from the Pi serial.

---

## 4. Bench validation before launch

None of the RF, analog or relay behaviour can be proven from the repository.
Run these on a Fin_3 unit with the branch flashed, and record the numbers in
`docs/hardware-overview.md` (replacing the BUILD estimates).

1. **Power.** 24 V applied: confirm +5 V, +6.5 V, +5VA = 5.0 V, ±3.0 V and
   VRLY = 24 V. USB-C only: confirm ADS1115 absent from `i2cdetect` (this is
   the expected result now documented).
2. **Turbidity range.** Probe in clear water: DMM on VIN1 vs. the firmware
   voltage — they must agree above 4.1 V; then add turbidity and confirm the
   reading moves immediately rather than after a ~0.4 V dead zone.
3. **pH.** Electrode in pH 4 / 7 / 10 buffers: record VOUT (AIN2) and VOCM
   (AIN3). Set `CalibrationData` defaults from these; confirm the Nernst sign;
   decide on H10 (refuse pH until calibrated, and/or differential read).
4. **TDS.** 1413 µS/cm standard: record VIN0 and derive the ppm/V constant.
5. **LoRaWAN.** With the TTN console open: JoinRequest visible on FSB2
   frequencies, JoinAccept received (log line "OTAA join successful"),
   `rx1_delay=5s` in the log, uplinks with incrementing FCnt and live
   data, a scheduled FPort-100 downlink actuating a relay once and its replay
   refused, LinkADRAns visible in the uplink FOpts. Measure conducted power at
   the SMA at +22 dBm.
6. **Relays.** 24 V on, service start/stop/`kill -9`, reboot request: no coil
   energised at any transition. Dosing rule with `duration_s: 30` at
   `sensor_read_s: 60`: time the actual on-period (expect ~60 s until H8 is
   done).
7. **Flatline.** Two hours with the probes in still water at room
   temperature: does `sensor_stuck` fire and drop relays (H9)?
8. **OTA.** One signed round-trip on a unit with probes, then one with no
   probes declared (expect rollback until H11 is decided).

---

## 5. Changes in this branch

| Area | Change | Tests |
|---|---|---|
| `src/radio/sx1262.py` | DIO2 RF switch, image calibration, inverted-IQ RX packet params + 0x0736 workaround, `set_tx_config()` re-applied before every TX, LDRO by symbol time, BUSY-stuck raises | `tests/test_launch_audit_regressions.py::TestSx1262RfSwitchAndRx` |
| `src/radio/lorawan.py` | US915 channel plan and hopping, RX1/RX2 parameters, absolute RX timing with RxDelay, JoinAccept MIC + DLSettings/RxDelay/CFList, downlink MIC + FCntDown, ACK, MAC commands (LinkADR, RXParamSetup, RXTimingSetup, DevStatus, DutyCycle), payload-per-DR limit, FCnt persisted before TX, `mac_params` | `…::TestUs915ChannelPlan`, `TestJoinAcceptVerification`, `TestDownlinkVerification` + existing `test_lorawan*.py` |
| `src/storage/database.py` | Schema v5: `lorawan_session.mac_params` | `…::TestSessionMacParams` |
| `src/main.py` | MAC wiring (sub-band, persist hook, params restore); policies read from `/etc/bluesignal` first | `…::TestRulesAndPolicies` |
| `src/utils/config.py` | `lora_sub_band` setting | schema validation tests |
| `src/sensors/ads1115.py` | Per-channel PGA (±6.144 V on AIN1); conversion timeout raises | `…::TestAds1115TurbidityRange` |
| `src/control/rules.py` | Schedule window in local time | `…::TestRulesAndPolicies` |
| `src/service_window/app.py` | SameSite=Lax, HttpOnly session cookie | `…::TestServiceWindowCookies` |
| `setup.sh`, `config/policies.yaml` | Seed `/etc/bluesignal/policies.yaml`; document local time and the ON/OFF window semantics | — |
| `README.md`, `docs/hardware-overview.md`, `docs/firmware-overview.md` | Power requirements, SMA antenna, real log lines, sub-band, EU868 claim removed | — |
| `src/control/relay.py`, `src/control/rules.py` | Per-channel auto-off timer thread, `max_continuous_on_minutes` ceiling, rules/downlink/cloud durations routed through it | `tests/test_launch_audit_followups.py::TestRelayAutoOff` |
| `src/sensing/monitor.py` | "flat" advisory, only "no_data" suspends rules | `…::TestFlatlineAdvisory` |
| `src/sensors/ph.py`, `src/calibration/calibrate.py`, `src/app/workers.py` | pH `uncalibrated` gate and status | `…::TestPhCalibrationGate` |
| `src/app/supervisor.py`, `src/ota/agent.py` | Liveness beat file; self-test accepts it | `…::TestOtaLiveness` |
| `src/integrations/smart_breaker/controller.py`, `src/utils/config.py` | `smart_breaker_min_off_s` short-cycle guard | `…::TestShortCycleGuard` |
| `src/radio/lorawan.py`, `src/main.py` | DevNonce counter, join backoff, LinkCheck rejoin, `lora_rejoin`; packet RSSI/SNR | `…::TestJoinHygiene` |
| `src/storage/database.py` | Pending-row cap when cloud sync is off | `…::TestPendingRotation` |
| `src/service_window/auth.py`, `routes/lora.py`, `routes/calibration.py` | PIN lockout escalation, forget-session button, restart hint after calibration | `…::TestPinLockout` |
| `src/sensors/temperature.py`, `src/sensors/gps.py`, `setup.sh` | 85.0 °C filter, EXTINT comment, provision service enabled | `…::TestDriverFixes` |
