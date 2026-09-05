# Firmware Overview

The WQM-1 firmware (v2.x) is a supervised, multi-worker Python application. It
samples water-quality sensors, buffers readings locally, controls relays
against cloud-configured rules, and reports to the BlueSignal Cloud over both
Wi-Fi/HTTP and LoRaWAN. It updates itself over the air and reports its own
health.

## Architecture

`main.py` is thin wiring: it detects the host board, builds the hardware
drivers and workers, and hands control to the supervisor. The real work runs in
independent worker threads so a slow GPS fix or a cloud retry can never stall
sensor reads, relay rules, or LoRa receive windows.

| Worker | Responsibility |
|---|---|
| `SamplingWorker` | Read every sensor, run relay rules, write readings to SQLite |
| `RadioWorker` | LoRaWAN join + uplink on the SX1262 (its own thread — timing-critical) |
| `CloudSyncWorker` | Batch-sync buffered readings over HTTP; per-row result handling |
| `CommandWorker` | Poll the cloud command queue; apply remote config; nudge OTA |
| `HeartbeatWorker` | Periodic self-report (health + diagnostics) |
| `GpsWorker` | Blocking UART GPS fixes, isolated from everything else |
| `SmartBreakerWorker` | Optional: poll the bound Eaton AbleEdge breaker, track link health, apply fail-safe |
| `Supervisor` (main thread) | Fan, LEDs, liveness, systemd watchdog pets, shutdown |

The supervisor pets the systemd watchdog **only** while every worker's liveness
timestamp is fresh; a hung worker therefore triggers a clean restart.

Source: `src/app/` (supervisor, state, workers), `src/main.py`.

## Sensing

- **Analog (Raspberry Pi hosts):** pH, TDS, and turbidity via the ADS1115 ADC;
  temperature via a DS18B20 1-Wire probe. Analog TDS excitation is hardware
  (CD4060); firmware 2.1.1 is sampling-only and does not drive that
  oscillator. Analog ORP (AIN3) is non-functional on PCBA rev Fin_3 — ORP
  comes from the digital RS485 probe instead. BUILD **ESTIMATE** notes for
  Q4 / VIN0 / AMS1117 current are in
  [hardware-overview.md](hardware-overview.md#build-provisional-engineering-estimates-2026-09-04)
  — they are not measured bench values.
- **RS485 digital probes (Honde Tech):** residual chlorine, digital ORP, and a
  5-in-1 (pH/EC/TDS/salinity/temperature) over a shared Modbus-RTU bus through
  a USB adapter. When the 5-in-1 is present its pH/TDS/temperature supersede the
  analog equivalents; the digital ORP supersedes analog ORP.
- **Smarter sensing (`src/sensing/`):** stuck/flatline detection, robust
  z-score spike flags, and calibration-drift tracking. A stuck probe is
  reported as a fault (never fake data) and its relay rules are suspended.
  Adaptive sampling can speed up while a value is changing fast.

Source: `src/sensors/`, `src/sensing/`, `src/app/workers.py`,
`src/diagnostics/explain.py` (plain-language health strings).

## Storage & cloud sync

Readings are written to a local SQLite database (write-ahead logging) first, so
a network outage never loses data. `CloudSyncWorker` batches them to the ingest
endpoint and marks each row synced only when the server confirms it stored that
row; rows older than 24 h are sent with a backfill flag. Heartbeats carry
uptime, buffer depth, disk/memory, CPU temperature, link quality, error
counters, firmware/config versions, and per-sensor health.

Source: `src/storage/database.py`, `src/cloud/client.py`, `src/utils/health.py`.

## Remote management

- **OTA updates** — a separate `bluesignal-ota` service polls for signed
  (Ed25519) firmware bundles, verifies + hashes them, swaps the `current`
  symlink atomically, self-tests, and auto-rolls-back on failure. See
  [ota-runbook.md](ota-runbook.md).
- **Remote config** — the cloud can push a validated desired-config; hot keys
  apply live, restart keys apply on the next restart, and a bad config can
  never brick the device (last-known-good is kept). Credentials and URLs can
  never be pushed remotely.
- **Commands** — `relay`, `awg`, `restart`, `config_reload`, `ota_check`,
  `diagnostics`.

Source: `src/ota/`, `src/utils/config.py` (ConfigManager), `src/main.py`.

## Smart breaker / AWG load control

Optional (`smart_breaker_vendor`, default `none`). WQM-1 talks **to** a
residential smart breaker the customer already owns — Eaton AbleEdge — so the
cloud or the installer can ask for the site's AWG circuit on/off and read what
it draws. The G5Q-14 relays stay as the local interlock/fallback; the unit is
not a breaker and does no panel work. Fail-safe on API loss is configurable
(`off` shipped for compressor loads). Command paths: Service Window socket
`awg_set` / `awg_status`, cloud command `type: "awg"`. The Service Window's
**AWG circuit** page (`/awg/`) binds the breaker, switches the load, and shows
link / position / power; the dashboard carries a matching traffic light. See
[smart-breaker-integration.md](smart-breaker-integration.md) and
[smart-breaker-installer-guide.md](smart-breaker-installer-guide.md).

Source: `src/integrations/smart_breaker/`.

## LoRaWAN

Class-A LoRaWAN 1.0.3 on the SX1262 (915 MHz US / 868 MHz EU), Cayenne LPP
payloads, AES-128. LoRa is a real-time snapshot + downlink channel; HTTP is the
durable one. Source: `src/radio/`.

## Host boards

The firmware runs on the Raspberry Pi Zero 2W (full analog + LoRa + relay I/O)
and, digital-first, on the Arduino UNO Q / VENTUNO Q (RS485 probes + USB GPS +
Wi-Fi sync; analog/LoRa/relay require the Pi). See [platforms.md](platforms.md).

## Related docs

- [getting-started.md](getting-started.md) — install & commissioning
- [platforms.md](platforms.md) — supported host boards
- [ota-runbook.md](ota-runbook.md) — over-the-air update operations
- [sensor-calibration.md](sensor-calibration.md) — per-probe calibration
- [smart-breaker-integration.md](smart-breaker-integration.md) — AWG circuit control via Eaton AbleEdge (design, auth, fail-safe)
- [smart-breaker-installer-guide.md](smart-breaker-installer-guide.md) — binding a site's AWG to a breaker id
- [hardware-overview.md](hardware-overview.md) — board, BOM, pinout; BUILD provisional TDS / 3.3 V estimates
