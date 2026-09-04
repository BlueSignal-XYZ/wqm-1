# Installer Manual — Rev D → Rev E regeneration brief

The printed **WQM-1 Product Installer Manual** (doc `WQM1-IM-100`, currently
Rev D / "RevD8" PDF) is maintained **outside these repos**. This file is the
authoritative change-list for regenerating it as **Rev E**, reconciled against
the shipping firmware (`v2.0.0`) and the BlueSignal website.

Hand the fenced prompt below to whoever (or whatever tool) regenerates the
manual. It is self-contained. Decisions baked in (founder-approved 2026-07-16):
board rev **Fin_3** is canonical; sampling copy **aligns to firmware** (60 s);
new water-quality parameters are framed as **optional Honde Tech RS485
add-on products**, not built-in channels.

**Warranty update (Jacques, 2026-09-01):** the WQM-1 Limited Warranty is
**90-day PCBA/board, 30-day probes**. This supersedes the 2026-07-16 figure
of "1 yr hardware + 90-day probes" referenced below and in item 9 of section
C — every instance in this file has been updated to match.

---

```
ROLE: Update the BlueSignal WQM-1 Product Installer Manual from Rev D
(WQM1-IM-100) to Rev E. Preserve structure, voice, dark visual system,
section order, and the "Install it once. Prove it forever." tone. Change
ONLY what's below; keep everything else. The shipping firmware (v2.0.0) and
the BlueSignal website are ground truth — where the manual disagrees, it's
wrong.

=== A. FACTUAL CORRECTIONS ===
1. STATUS LEDs (Section 00) — the board has FOUR function LEDs, not PWR+STA1-4.
   Replace with: LED1 Heartbeat (1 Hz = alive/sampling) - LED2 LoRa (on during
   uplink) - LED3 GPS (on while seeking a fix) - LED4 Fault (blinks on a
   sensor/system fault; details on the dashboard). No power LED, no relay LED.
   Remove the "confirm STA assignments" caution.
2. BOARD REV — change every "PCB WQM-1 REV A"/"REV A" to "PCBA rev Fin_3"
   (cover caption, Section 04 figure + footer, Appendix).
3. SAMPLING (Section 03; Section 01 if repeated) — replace "every 6 minutes"
   with "sampled every 60 s by default (cloud-configurable 5-3600 s); LoRa
   uplink every 5 min; cloud sync every 5 min." Remove all other "6 minute".
4. RELAY MAP (Section 09) — automation ships INERT until the installer enables
   it (state this). Present CH1 dosing / CH2 second dose or valve / CH3
   aeration-circulation / CH4 circulation-flush (or alarm/contactor if wired)
   as EDITABLE examples set in cloud rules — not fixed functions. Keep the
   SLD-002/003 wiring patterns.
5. ANALOG ORP — on Fin_3 the BNC front end has no working analog ORP (AIN3 is
   spare). Stop showing "pH or ORP" on the BNC as either/or. ORP now comes from
   the digital RS485 ORP probe (Section B). Keep pH on the BNC.

=== B. NEW CAPABILITIES (frame as OPTIONAL Honde Tech RS485 add-on PRODUCTS on
a shared Modbus bus; RS485->USB adapter supplied with the probes; 12 V to
probes from the unit rail, USB is data-only — NOT built-in channels) ===
6. RS485 digital-probe expansion (Section 01/03 + install block in Section 08):
   - Residual chlorine 0-20 mg/L, flow cell 15-30 L/h, guided zero/slope cal,
     recal 30-60 days, activate in 3M KCL.
   - Digital ORP (RD-ORP-WE-01) +/-1999 mV; supersedes analog ORP.
   - 5-in-1 (RD-PETSTS-01): pH, EC 0-10,000 uS/cm, TDS, salinity 0-8 ppt, temp
     in one probe; pH buffers 4.01/6.86/9.18 via the Service Window wizard.
   - Bus discipline: probes ship at address 1 — add one at a time via Service
     Window -> RS485 Sensors -> Scan/Add. Wiring: A=yellow, B=green, 12 V=red
     (unit rail), GND=black.
7. Remote management & self-monitoring panel (Section 01 or near Section 11):
   - OTA updates — signed, automatic, self-testing, auto-rollback (SSH is
     break-glass only; point to the OTA runbook).
   - Remote config from the dashboard (cadence/thresholds/rules; credentials &
     URLs can never be pushed).
   - Heartbeat diagnostics — uptime, buffer depth, disk, CPU temp, link
     quality, per-sensor plain-language health on the device page.
   - Drift/stuck/spike detection — flags a dead or drifting probe, suspends
     that channel's relay rules, nags "recalibrate soon" instead of acting on
     bad data.
   - Guided Service Window (phone browser) — first-boot wizard (set a real PIN,
     claim QR, sensor auto-detect, live first-reading check, go/no-go) + guided
     calibration wizards.
8. HOST BOARDS (Section 03 Compute; Appendix) — "Runs on Raspberry Pi Zero 2W
   (full analog + LoRa + relay I/O) or, digital-first, on Arduino UNO Q /
   VENTUNO Q (RS485 probes + USB GPS + Wi-Fi; analog/LoRa/relay need the Pi)."

=== C. CONFIRM UNCHANGED ===
9. Warranty is 90-day PCBA/board, 30-day consumable probes; keep
   the "DRAFT — attorney review, do not distribute" banner until legal signs.
10. Enclosure is IP65 (not IP67) wherever shown.
11. 90 days cloud monitoring included; paid tiers thereafter (Residential $5/mo,
    Commercial $10/mo per device) — match the website.

Add a Rev E revision-history line: LED table corrected; board rev Fin_3;
cadence 60 s; relay map clarified (editable, inert by default); analog ORP
superseded by digital; RS485 Honde add-ons added; OTA/remote-config/
diagnostics/UNO-Q added.
```

---

## Firmware/source references (for the manual author to verify against)

| Correction | Ground truth in repo |
|---|---|
| LED scheme | `src/utils/config.py` (`LED_HEARTBEAT/LORA_TX/GPS_FIX/ERROR`), `src/control/led.py`, `src/app/workers.py` |
| Board = Fin_3 | `src/main.py:12`, `config/pinmap.yaml`, `README.md`, `hardware/fab/PCBA_Fin_3.pdf` |
| Cadence 60 s | `src/utils/config.py` `sensor_read_s=60`, `lora_tx_s=300`, `sync_interval_s=300` |
| Relay defaults inert | `config/policies.yaml` (`manual.override: true`, illustrative map) |
| Analog ORP dead on Fin_3 | `src/sensors/orp.py`, `src/utils/config.py` |
| RS485 params | `src/sensors/honde.py`, `src/app/workers.py`, `src/diagnostics/explain.py` |
| OTA / remote config / heartbeat / drift / Service Window / UNO Q | `docs/ota-runbook.md`, `docs/platforms.md`, `src/ota/`, `src/sensing/`, `src/service_window/`, `src/platform_support/board.py` |
