# AbleEdge load control (WQM-1 ↔ Eaton)

WQM-1 remains a water-quality monitor. This integration lets a unit
**communicate with an existing Eaton AbleEdge smart breaker** that already
feeds an atmospheric water generator (AWG) or similar compressor load. It
does not turn WQM-1 into a breaker, does not do panel electrical work, and
does not ship a UL-listed breaker SKU.

Vendor lock (Jacques, 2026-09-05): **Eaton AbleEdge only.** Span, Lumin, and
Savant clients are not implemented and are refused if named in config.

**Live smoke is blocked** until the Eaton developer application credentials
for `jacques@bluesignal.xyz` are installed on the unit (or later in Cloud
Functions). This tree ships a mock-tested skeleton plus the command-path
hook. Do not commit Client ID, secrets, or API keys.

## Architecture

```
cloud.bluesignal.xyz  --awg/circuit command-->  WQM-1 CommandWorker
                                                    |
                                                    v
                                           LoadController
                                          /      |       \
                                   vendor=    vendor=    vendor=
                                   ableedge   relay_only  none
                                      |          |          |
                                      v          v          v
                              AbleEdge HTTP   G5Q-14     refuse
                              (Eaton API)    fallback    (no-op)
                                      |
                                      v
                         Eaton AbleEdge breaker
                         (site AWG circuit)
```

- **Water path is unchanged.** Sampling, SQLite WAL, LoRaWAN, and cloud
  ingest do not call AbleEdge. AWG on/off arrives on the existing command
  socket / cloud command poll (`awg` / `circuit` / `circuit_set`).
- **G5Q-14 relays stay.** `relay_set` and `manual.override` in
  `policies.yaml` still own the four dry-contact channels. Relays are
  **fallback / interlock**, not the AbleEdge integration. Setting
  `vendor: relay_only` is an explicit installer choice to actuate a
  designated relay instead of Eaton — it is labelled as such in logs.
- **BlueSignal owns water quality + optional AWG load control** via the
  customer's AbleEdge breaker. Circuit ampacity is installer input only.
  Firmware never invents L/day, nameplate amps, or a default breaker size.

### Circuit binding

| Field | Who sets it | Meaning |
|---|---|---|
| `load_control.site_id` | Installer / cloud site record | BlueSignal site id (documentation / logs) |
| `load_control.device_id` | Installer | AbleEdge **device UUID** from the Eaton portal |
| `load_control.circuit_id` | Installer, optional | Circuit UUID when distinct; defaults to `device_id` |
| `load_control.circuit_ampacity_a` | Installer only | Nameplate / observed ampacity. Omitted if unknown. Never guessed. |

Eaton's Smart Breaker API addresses the breaker as `deviceId`. Commands
go to that UUID. `circuit_id` is stored so a later panel model that
exposes a separate circuit object can bind without renaming config.

### Auth flow

Official API: [Smart Breaker API](https://api.em.eaton.com/docs)
(Brightlayer / formerly EM API). Developer getting started:
<https://ableedge-portal.eaton.com/gettingStarted>

1. Create an Application on the AbleEdge / Smart Breaker developer portal
   (`jacques@bluesignal.xyz`). That yields a **Client ID**, **two
   application secrets** (one-year lifetime; Eaton does not store them),
   and a **subscription / API key**.
2. Every request carries `Em-Api-Subscription-Key`.
3. `POST /api/v1/serviceAccount/authToken` with `{clientId, clientSecret}`
   returns `{data: {token, expiresAt}}` (~1 hour).
4. Subsequent calls use `Authorization: Bearer {token}`.
5. Device on/off: `POST /api/v1/devices/{deviceId}/breaker/remoteHandle/position`
   with `command: close` (circuit **ON**, load energized) or `open`
   (circuit **OFF**). Mapping is Eaton's remote-handle language — do not
   invert it.
6. Energy: `GET /api/v1/devices/{deviceId}/data/telemetry/meter/reading`.
   Watts are `V * I` only when both are present in the meter payload.

**Production tokens may later live only in Cloud Functions** (the Pi
would then send AWG intent to `cloud.bluesignal.xyz`, and the function
would hold Eaton secrets). On-device, secrets use the existing pattern:
environment variables or files under `/etc/bluesignal/secrets/ableedge/`.
Config.yaml holds **names of those refs**, never values.

## Config schema

```yaml
load_control:
  vendor: none            # ableedge | none | relay_only
  site_id: ""
  device_id: ""           # AbleEdge device UUID
  circuit_id: ""          # optional
  circuit_ampacity_a:     # installer number or omit
  poll_s: 30              # 5–3600
  fail_safe: off          # off | last | on
  fallback_relay:         # 1–4 interlock, or omit
  api_base: "https://api.em.eaton.com"
  backend: http           # http | mock (bench/tests)
  credentials:
    client_id_env: ABLEEDGE_CLIENT_ID
    client_secret_env: ABLEEDGE_CLIENT_SECRET
    subscription_key_env: ABLEEDGE_SUBSCRIPTION_KEY
    secrets_dir: /etc/bluesignal/secrets/ableedge
```

The block is **local-only** (not in the remote-config schema), so a
compromised cloud account cannot re-bind a breaker or push Eaton secrets.

`backend: mock` or `ABLEEDGE_BACKEND=mock` uses the in-process fake for
bench tests without credentials. Do not enable mock on a field unit.

## Fail-safe matrix

Documented here only — not customer marketing copy. Prefer **fail-OFF**
for compressor / AWG loads.

| Condition | `fail_safe=off` (default) | `fail_safe=last` | `fail_safe=on` |
|---|---|---|---|
| AbleEdge API / auth unreachable on `set_circuit` | Local OFF; optional fallback relay OFF | Keep last applied (OFF if never set) | Local ON; optional fallback relay ON |
| Same on status / power / poll | Latch the same policy once (no flap) | Same | Same |
| API returns | Latch clears; circuit is **not** auto-toggled | Same | Same |
| `vendor=none` | Command refused | — | — |
| `vendor=relay_only` | Actuate `fallback_relay` only (not AbleEdge) | — | — |
| Process shutdown | Local OFF (and interlock OFF) unless policy is `on` | Local OFF | Honour `on` |

When the API is down the firmware does **not** claim the remote breaker
changed. `CircuitCommandResult.ok` is false, `via=fail_safe`, and the
cloud command is acked as `error` with the reason.

## Command path (hook points)

Cloud poll (`deviceCommands`) and the service-window Unix socket share
`WQM1App._set_awg_circuit` → `LoadController.set_circuit`.

| Source | Shape |
|---|---|
| Cloud | `{"id": "…", "type": "awg", "state": true}` or `"type": "circuit"` |
| Socket | `{"action": "circuit_set", "state": true}` or `"action": "awg_set"` |

Existing `relay` / `relay_set` commands are unchanged. `manual.override`
in `policies.yaml` still gates **automation rules** on the G5Q-14s; it
does not mute an explicit cloud AWG command (same as a manual relay
command today).

`AbleEdgePollWorker` (`name=ableedge`) refreshes reachability at
`poll_s` when `vendor=ableedge`. It does not write readings.

## Installer: bind site AWG → AbleEdge device

1. Confirm the site already has an Eaton AbleEdge breaker on the AWG /
   compressor circuit. WQM-1 does not install or list that breaker.
2. In the Eaton installer / AbleEdge app, copy the breaker's **device
   UUID**.
3. On the Pi, edit `/etc/bluesignal/config.yaml`:

   ```yaml
   load_control:
     vendor: ableedge
     site_id: "<BlueSignal site id>"
     device_id: "<AbleEdge device UUID>"
     circuit_id: ""          # or the circuit UUID if Eaton shows a distinct one
     circuit_ampacity_a: 20  # only the number the installer actually read
     poll_s: 30
     fail_safe: off
     fallback_relay: 4       # optional G5Q-14 interlock; omit if unused
   ```

4. Install credentials (see checklist). Restart `bluesignal-wqm`.
5. From cloud or the command socket, send an `awg` off, then on, and
   confirm the breaker handle in the Eaton app. If credentials are
   missing the unit logs that live smoke is blocked and applies
   fail-safe.

## Developer-app checklist (`jacques@bluesignal.xyz`)

Do this on the AbleEdge / Brightlayer portal — **do not paste values into
git**.

1. Sign in at <https://ableedge-portal.eaton.com/gettingStarted> (account
   already created: `jacques@bluesignal.xyz`).
2. Create a Team if prompted, then an **Application**.
3. Record, offline:
   - **Client ID**
   - **Application secret 1** and **secret 2** (Eaton will not show them
     again; rotate yearly, independently)
   - **API / subscription key** (`Em-Api-Subscription-Key`)
4. On the unit (or later, in Cloud Functions only):

   ```bash
   # Option A — systemd EnvironmentFile / env
   export ABLEEDGE_CLIENT_ID="…"
   export ABLEEDGE_CLIENT_SECRET="…"
   export ABLEEDGE_SUBSCRIPTION_KEY="…"

   # Option B — files (mode 0600, not in the repo)
   sudo mkdir -p /etc/bluesignal/secrets/ableedge
   printf '%s' "$ABLEEDGE_CLIENT_ID" | sudo tee /etc/bluesignal/secrets/ableedge/client_id >/dev/null
   printf '%s' "$ABLEEDGE_CLIENT_SECRET" | sudo tee /etc/bluesignal/secrets/ableedge/client_secret >/dev/null
   printf '%s' "$ABLEEDGE_SUBSCRIPTION_KEY" | sudo tee /etc/bluesignal/secrets/ableedge/subscription_key >/dev/null
   sudo chmod 600 /etc/bluesignal/secrets/ableedge/*
   ```

5. Bind `device_id` as above. Restart the service.
6. Live smoke (still blocked in this PR until step 4 is done): one
   `awg` off, one `awg` on, one status/power read against the real
   breaker.

## What this PR does not do

- No Span / Lumin / Savant code.
- No TDS / ORP hardware claim changes.
- No new PCBA / KiCad.
- No production secrets in the repo.
- No UL / listing claims.
- No customer-facing marketing copy about fail-safe behaviour.

## Source

- `src/integrations/ableedge/` — client interface, HTTP skeleton, mock,
  fail-safe controller, schema, secret refs
- `src/app/workers.py` — `AbleEdgePollWorker`
- `src/main.py` — command-path hook (`awg` / `circuit` / `circuit_set`)
- `config/config.yaml.example` — commented schema
- `tests/test_ableedge.py` — mock client + fail-safe + schema
