# Smart-breaker integration — design note (Eaton AbleEdge)

**Status:** firmware skeleton merged, mock-tested. **Live smoke is blocked on
Eaton developer credentials** (see [What BUILD still needs](#what-build-still-needs)).

## What this is — and is not

WQM-1 **communicates with** a residential smart breaker the customer already
owns, so BlueSignal can ask for the site's AWG (atmospheric water generator) or
similar compressor circuit to be energised or de-energised, and read what that
circuit is drawing as the breaker meters it.

| It is | It is not |
|---|---|
| A thin API client on the Pi, one bound circuit | A smart breaker, a panel, or a load-centre product |
| A controller that owns interlock ordering + fail-safe | Panel electrical work of any kind |
| Config the installer fills in from the panel label | A UL-listed breaker SKU, or a claim of listing compatibility |
| The existing G5Q-14 relays kept as the local interlock | A replacement for those relays, or a pretence that the relays *are* the integration |

The firmware never derives litres/day, nameplate amps, or any figure the
vendor API does not report. Circuit ampacity is **installer input**
(`smart_breaker_circuit_amps`) and is carried through for display only.

Vendor scope: **Eaton AbleEdge only.** FranklinWH is a document-only appendix
(the owner plans an installation of both under a Master Electrician); nothing
else is in scope.

## Architecture

```
                    BlueSignal Cloud (Firebase / cloud.bluesignal.xyz)
                    ┌──────────────────────────────────────────────┐
   dashboard ─────▶│ deviceCommands queue  {type:"awg", state, …} │
                    │ device events         smart_breaker_failsafe │
                    │ (future) AbleEdge proxy  — holds Eaton secrets│
                    └───────────────┬──────────────────────────────┘
                                    │ HTTPS, X-API-Key (existing)
                                    ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ WQM-1 (Pi Zero 2W)                                                        │
│                                                                           │
│  CommandWorker ─▶ _apply_cloud_command(type="awg") ─┐                     │
│  Service Window ─▶ cmd.sock  {action:"awg_set"}  ───┤                     │
│                                                     ▼                     │
│                                  SmartBreakerController.request(on|off)   │
│                                     │  owns: interlock order, fail-safe,  │
│                                     │        queued OFF, link health      │
│                 ┌───────────────────┼─────────────────────┐               │
│                 ▼                                         ▼               │
│   RelayController.set(ch, …)                 AbleEdgeClient (urllib)      │
│   G5Q-14 ch N = interlock                      authenticate / get_status  │
│   (COM→NO in series with AWG                   set_circuit / get_power    │
│    enable or contactor coil)                              │               │
│                 │                SmartBreakerWorker.poll ─┘ every         │
│                 │                                        smart_breaker_poll_s │
└─────────────────┼────────────────────────────────────────┼────────────────┘
                  │ dry contact                            │ HTTPS + OAuth2 bearer
                  ▼                                        ▼   + Em-Api-Subscription-Key
   ┌──────────────────────┐                ┌──────────────────────────────────┐
   │ AWG enable input /   │                │ Eaton Smart Breaker API          │
   │ contactor coil       │                │ (Brightlayer / AbleEdge portal)  │
   └──────────┬───────────┘                └───────────────┬──────────────────┘
              │                                            │ Eaton cloud ↔ breaker
              ▼                                            ▼
   ┌──────────────────────┐   240 V branch   ┌──────────────────────────────────┐
   │ AWG (compressor)     │◀─────────────────│ Eaton AbleEdge smart breaker     │
   │                      │                  │ (customer's panel, deviceId UUID)│
   └──────────────────────┘                  └──────────────────────────────────┘
```

Two independent halves make up every OFF: the **interlock relay** (local,
instant, needs no network) and the **breaker** (remote, authoritative for the
branch circuit). ON requires both.

Source: `src/integrations/smart_breaker/` — `base.py` (contract),
`ableedge.py` (client), `controller.py` (policy), `worker.py`, `fake.py`
(test double), `__init__.py` (`build_smart_breaker()` factory).

## Vocabulary

The firmware speaks in the **load's** terms: `on` / `off`. Eaton speaks in the
**breaker's**: `close` (energised) / `open` (de-energised). The inversion is
confined to `ableedge.py`; nothing else in the tree sees `open`/`close`.

## Auth flow

Eaton's public reference describes an OAuth2 **client-credentials** grant plus
a per-application **subscription key** on every request:

```
POST {smart_breaker_token_url}                  (default https://api.em.eaton.com/oauth2/token)
  Authorization: Basic base64(client_id:client_secret)
  Content-Type: application/x-www-form-urlencoded
  Em-Api-Subscription-Key: <subscription key>
  grant_type=client_credentials
→ { "access_token": …, "token_type": "Bearer", "expires_in": 3600 }

GET/POST {smart_breaker_api_base}/devices/{deviceId}/…   (default https://api.em.eaton.com/api/v1)
  Authorization: Bearer <access_token>
  Em-Api-Subscription-Key: <subscription key>
```

Client behaviour (`AbleEdgeClient`):

- Token cached; refreshed 60 s before `expires_in`; a `401` on any call
  forgets the token and retries **once**.
- No retry loop otherwise. Eaton returns `429`; the worker's cadence and the
  supervisor's backoff are the retry policy.
- HTTP → typed errors: `401/403 → AuthError`, `404 → NotBound`,
  `418 → UnsupportedCommand` (EV charger bound where a breaker was expected),
  `429 → RateLimited`, `503 → DeviceUnavailable`, other `5xx`/transport →
  `Unreachable`. The controller treats every one of these as a link-health
  sample.

**Two portal generations.** Eaton's older "Smart Breaker API / EM API"
reference (v1.20) names the key header `Em-Api-Subscription-Key` and issues
tokens at `/oauth2/token`. The newer AbleEdge developer portal describes an
Apigee service-account token from `https://api.eaton.com/oauth/accesstoken`
plus an **Organization** token, with the key in an `api-key` header. The
client takes the token URL, API base, and header name as parameters so live
smoke can match whichever the issued credentials belong to without a code
change. Breaker endpoints require the **organisation** service-account token
in either generation: the credentials that go on the Pi (or in the proxy)
are the organisation's, not the application's.

### Where the secrets live

| Mode (`smart_breaker_auth_mode`) | Secrets | Status |
|---|---|---|
| `direct` | On the Pi in `/etc/bluesignal/config.yaml` (`smart_breaker_client_id`, `_client_secret`, `_subscription_key`). Same pattern as `api_key` / `app_key`: schema `remote=False`, provisioning-time only, never pushable from the cloud, never committed (`.gitleaks` already covers `config/*.example`). | **Implemented** |
| `cloud_proxy` | In Cloud Functions config. The Pi calls `{cloud_api_base}/v2/devices/{id}/awg/...` with its existing `X-API-Key`; the function holds the Eaton org credentials, exchanges the token, and forwards. | **Specified, not built** — `build_smart_breaker()` logs and disables the feature if selected. Preferred for production because a stolen SD card then yields nothing that can operate a breaker. Proposed contract: `POST …/awg/command {state, reason}` → `{ok, breaker}`; `GET …/awg/status` → `{isOn, connected, power}`. |

Eaton application secrets have a **one-year lifetime** and must be rotated
via their portal; note the date when issued.

## Circuit binding

One controller, one circuit. The binding is what the installer records from
the panel label and the Eaton installer app:

| Setting | Meaning | Source |
|---|---|---|
| `smart_breaker_vendor` | `none` (default) / `relay_only` / `ableedge` | installer |
| `smart_breaker_site_id` | Eaton `locationId` (UUID) the breaker was commissioned under | Eaton installer app / API |
| `smart_breaker_device_id` | Eaton device UUID (`hardwareType: emcb`) for the AWG circuit | Eaton installer app / API |
| `smart_breaker_circuit_label` | Free text from the panel directory, e.g. `Panel A / 14 — AWG-1` | installer |
| `smart_breaker_circuit_amps` | Breaker ampacity **as printed on the breaker**. `0` = not entered → boot warning. Never guessed. | installer |
| `smart_breaker_interlock_relay` | `0` = none, `1-4` = G5Q channel wired in series with the AWG enable / contactor coil | installer |

Binding keys are restart-required (the client and worker are built at boot).
They are remotely configurable so the dashboard can complete a binding the
installer started; **credentials, URLs and the auth mode are not**.

Policy keys — `smart_breaker_poll_s` (15–3600), `smart_breaker_fail_safe`,
`smart_breaker_unreachable_grace_s` — are hot: the worker re-reads them each
cycle.

### Ampacity and the API

The Eaton `deviceData` payload includes `breaker.metadata.ratedCurrent`. It is
**not** consumed. The installer's figure from the physical label is the
record; a later change may log a mismatch as a hint, never overwrite.

## Command paths (hook points)

| Ingress | Payload | Handler |
|---|---|---|
| Service Window / CLI over `/var/run/bluesignal/cmd.sock` | `{"action":"awg_set","state":true\|false,"reason":"…"}` | `WQM1App._handle_cmd` → `controller.request()` |
| same | `{"action":"awg_status"}` | `controller.status()` snapshot |
| Cloud `deviceCommands` poll | `{"id","type":"awg","state","reason","durationSeconds"}` | `_apply_cloud_command` → `_handle_cmd(awg_set)` → ack `done`/`error` (error text carries the typed vendor error) |
| Cloud `deviceCommands` poll | `{"type":"relay", …}` | **unchanged** — direct G5Q control stays as-is |
| LoRaWAN FPort 100 | relay downlink | **unchanged** |
| `RulesEngine` | threshold/adaptive rules | **untouched** — no automation drives the AWG in this PR; `manual.override` semantics are unaffected |

`durationSeconds` on an ON schedules a local `awg_set off` timer, mirroring the
relay path. Eaton's own `secondsUntilReset` only re-*closes* after an open
(the opposite direction), so it is not used.

Sensor sampling, the SQLite buffer, LoRa, and the cloud sync path are not
modified. The AWG controller is an additional worker with its own thread.

## Interlock ordering

```
OFF:  relay(ch) ← de-energise        (local, always succeeds or is reported)
      breaker   ← open               (remote; failure ⇒ queued, resent on recovery)

ON:   breaker   ← close              (remote; failure ⇒ nothing else happens)
      relay(ch) ← energise           (only after the breaker confirmed)
```

An ON that failed is **never retried behind the operator's back**. Only OFF is
queued. This mirrors `control/rules.py`: letting go of a load is not
discretionary; energising one is.

## Fail-safe matrix

Every vendor call — request or poll — is a link-health sample. The link counts
as **down since boot** until the first success, so a unit that never reaches
Eaton still fails safe. When the link has been down for
`smart_breaker_unreachable_grace_s` (default 300 s; `0` = immediately), the
configured mode is applied **once**, a `smart_breaker_failsafe` event is
queued for the cloud, and on recovery a `smart_breaker_restored` event
follows.

| Condition | `fail_safe: off` (default) | `fail_safe: last` | `fail_safe: on` |
|---|---|---|---|
| Vendor unreachable / 5xx / timeout | Interlock relay **drops now**; desired := OFF; breaker `open` **queued** and sent on recovery | Log + event only; nothing actuated | Interlock relay **energises**; breaker position cannot change until link returns |
| `401/403` (credentials rejected / expired secret) | same as above, logged at ERROR | same | same |
| `429` rate-limited | same (named in the log so cadence can be slowed) | same | same |
| `503` breaker offline from Eaton cloud | same | same | same |
| `418` device cannot take the command | Request fails immediately, counts as link loss | same | same |
| Explicit OFF request while unreachable | Relay drops, open queued, ack `error` (breaker unconfirmed) | same | same |
| Explicit ON request while unreachable | Fails, ack `error`, relay untouched, nothing queued | same | same |
| Link recovers | Queued open delivered first, then status/power sampled; `restored` event | `restored` event | `restored` event |
| Service restart / `config_reload` | **Nothing sent** — a restart must not open the customer's breaker; `main._shutdown` drops every relay coil as it always has | same | same |
| Host reboot request | Relays to fail-safe first (existing `app.reboot`); breaker untouched | same | same |
| Breaker moved by someone else (Eaton app, panel handle, utility override event) | Detected on next poll, logged as WARNING; WQM-1 does **not** fight it | same | same |

**What fail-OFF can and cannot guarantee.** Without a network path there is
no way to open the breaker. Fail-OFF therefore guarantees the *interlock*
opens; the branch circuit itself stays wherever Eaton last left it until the
queued open lands. That is precisely why the G5Q relay stays in the design:
if the site needs the compressor to stop when WQM-1 loses the cloud, the
installer wires the interlock. If no relay is wired, `fail_safe: off` is an
intent plus a queued command, and the status snapshot says so
(`interlockRelay: null`).

`fail_safe: on` exists because the config schema promised it. It is only
sensible for a load where *running* is the safe state; it is not recommended
for compressors and the docs say so.

## Telemetry

`get_power()` reads `GET /devices/{id}/data/telemetry/meter/reading` and
carries `currentA`, `voltageAN` and `energy.deliveredWH` through **as
reported**, plus the raw payload. Eaton's own samples show amps/volts on
`/meter/reading` and milli-units on `/deviceData`; **unit scaling is on the
live-smoke list** and no derived figure (kW, L/day, duty cycle) is produced
until it is settled. A failed power read is telemetry, not link loss: the
last good sample is kept.

Status snapshot (`awg_status`) fields: `vendor, deviceId, siteId,
circuitLabel, circuitAmps, interlockRelay, failSafe, failSafeApplied, linkOk,
unreachableForS, lastError, desired, pendingCommand, breaker{isOn, connected,
position, observedAt}, power{currentA, voltageV, energyDeliveredWh,
observedAt}`.

## Rate limits

Eaton documents `429` on the position endpoint and 5-minute telemetry
cadence. `smart_breaker_poll_s` is floored at 15 s and defaults to 60 s; each
poll is two or three calls (position, isConnected, meter reading). Slow it
down if `RateLimited` shows up in `lastError`.

## Warranty / terms caveats (internal)

Not customer copy. For the installer and BUILD:

- Eaton's developer terms govern API use; secret rotation is annual and the
  developer, not Eaton, stores the secret.
- The AbleEdge breaker's own protective function is unaffected by the remote
  handle; the remote handle is a *control* feature. No claim about listing,
  compatibility, or protective behaviour is made or implied by this firmware.
- Panel work — installing the smart breaker, wiring the interlock relay
  contact into the AWG enable/contactor circuit — is the licensed
  electrician's, under local code. The relay contact is a dry contact
  (G5Q-14 ratings in `hardware-overview.md`); it must not switch the branch
  circuit itself.
- Remote de-energising a compressor mid-cycle is the load manufacturer's
  concern (short-cycle protection, restart delay). The controller offers
  `durationSeconds` and the fail-safe grace as the only timing tools; it does
  not model the AWG.

## Testing without credentials

`integrations.smart_breaker.fake.FakeSmartBreaker` implements the same four
calls in memory with failure injection (`unreachable`, `reject_auth`,
`rate_limited`, `fail_next(err)`). Tests:

- `tests/test_smart_breaker_ableedge.py` — client against a scripted
  `urlopen`: headers, token exchange/caching/refresh, on→close, status map.
- `tests/test_smart_breaker_controller.py` — interlock ordering, the full
  fail-safe matrix, queued OFF, relay_only, factory refusals.
- `tests/test_smart_breaker_wiring.py` — config schema, `awg_set` /
  `awg_status` / cloud `awg` through a booted `WQM1App`, worker cadence.

## What BUILD still needs

1. **Eaton developer credentials** (blocking live smoke): a developer
   account on the AbleEdge / Smart Breaker portal, an Application (client id
   + secrets + subscription key), an Organisation with a service account, and
   at least one commissioned `emcb` device in a Location. Confirm which
   portal generation the credentials are for and set `smart_breaker_api_base`,
   `smart_breaker_token_url` (and the header name, if `api-key`) accordingly.
   **Do not commit any of it.** For bench work use the Eaton sandbox host
   named in their docs (`api.em-dev1.eaton.com`) once access is granted.
2. **Live-smoke checklist** (first run with credentials):
   - token exchange succeeds; `expires_in` observed;
   - `GET …/breaker/remoteHandle/position` vocabulary (`open`/`close`) as
     assumed;
   - `POST …/position {command: close}` returns 204 and the handle moves;
   - `GET …/meter/reading` units (A vs mA, V vs mV) — then decide scaling;
   - `429` threshold at 60 s polling.
3. **Cloud side** (out of tree, `functions/`): accept and enqueue command
   `type: "awg"`; add `smart_breaker_failsafe` / `smart_breaker_restored` to
   the device-event allow-list; optionally build the `cloud_proxy` endpoints
   above.
4. **Service Window page** for the binding form (currently config.yaml +
   `awg_set`/`awg_status` over the socket). Not required for the skeleton.

## Appendix — FranklinWH (future, document only)

The owner intends to install a FranklinWH system alongside the Eaton breaker.
FranklinWH publishes a **partner API** (`https://api.franklinwh.com/`,
namespace `/api-common/`) for approved partners: sites, device inventory,
running status, power/energy telemetry, aPower switch control, and **Smart
Circuits** (`POST /api-common/setSmartCircuits`). Authentication is a
partner-issued `cp`/`ck` credential pair exchanged at
`/api-common/tokenizer` for a token sent as the raw `Authorization` header
value. Access is by **partner approval** (installer / service-provider
onboarding via FleetView), not self-service.

Position: **not implemented, not scheduled.** Prerequisites before any code:

- BlueSignal is approved as a FranklinWH partner and receives `cp`/`ck` for
  the site;
- the aGate's Smart Circuits module is fitted and the AWG is on one of its
  circuits (a Smart Circuit is a controllable branch on the aGate, which is a
  different thing from a smart breaker in the main panel);
- the partner OpenAPI is in hand so the client is written against Eaton's
  peer, not against the mobile-app endpoints.

The unofficial community library that drives FranklinWH's consumer/installer
app API is **out of bounds** under the same rule that excludes scrapers for
any vendor. If both systems are present on one site, the AWG has one
authoritative switch; the binding must name it, and the other system is
telemetry only.
