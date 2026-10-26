# Device commands (firmware)

Wire formats for Cloud commissioning commands. Server marketplace #1479
and this firmware must match this page. The server's names are canonical.

The unit pulls work on the existing `command_poll_s` tick:
`GET {cloud_api_base}/v2/devices/{id}/commands/pending` with the device
key, applies each command once, and `POST`s
`{...}/commands/{cmdId}/result`.

The live unit `BS-WQM1-0000a92e4e7d` runs older firmware. These routes
are additive: ingest, `GET .../config`, and the legacy `deviceCommands`
poll are unchanged. Unverified on hardware.

Owner-only on the Cloud side. The unit authenticates with its device key.

## Status: `queued` / `sent`

Cloud stores each command as **`queued`**, then **`sent`**.

`GET .../commands/pending` returns commands that are `sent` (it marks
`queued` ones `sent` as it returns them). The same `sent` command is
returned on every poll until it expires or a result is accepted.

Firmware **dedupes by `id`**. A string `id` is required. A command
without one is rejected with `missing id` and is not applied. Handled
ids are stored in a small file next to the readings database
(`handled-commands.json`, written with `utils.atomic`) so a reboot does
not re-run a calibration step. A second `POST .../result` for the same
id is HTTP **409**; the unit treats 409 as already recorded (same as
200) and does not retry.

```json
{
  "commands": [
    {
      "id": "cmd-1",
      "type": "ph_calibrate",
      "payload": {
        "step": "start",
        "probe": "analog"
      },
      "expiresAt": 1700000060000
    }
  ]
}
```

`expiresAt` is Unix time in milliseconds. The unit rejects an expired
command and still posts `result`.

Allowed `type` values: `ph_calibrate`, `rs485_config`. Anything else is
`rejected` with message `unknown type`. Extra fields, non-finite
numbers, and a missing string `id` are rejected with a plain message.

## `ph_calibrate`

```json
{
  "step": "point",
  "probe": "analog",
  "buffer": 4.0
}
```

| Field | Required | Values |
|---|---|---|
| `step` | always | `start`, `point`, `finish` |
| `probe` | always | `analog`, `rs485_multi` |
| `buffer` | `point` only | see below |

`buffer` is rejected on `start` and `finish`.

Analog (BNC / `calibrate_ph`): **4.0** and **7.0** only.

RD-PETSTS-01 (`calibrate_ph_point`): **4.01**, **6.86**, and **9.18** only.

`10.0` is rejected. A buffer from the other probe's set is rejected.

`start` opens the session and clears stored points. `point` and `finish`
require a prior `start`. Analog `point` takes the median of 5
converter-voltage samples. If the spread (max − min) is above 0.02 V the
command is rejected with `voltage not stable`. A successful analog
`finish` clears the session so a later `finish` cannot reuse the volts.
RS485 `finish` with no points is rejected with `missing calibration points`.

Only **`finish`** carries `slope` and `offset`. `start` and `point`
results are `applied` with `buffers` and `measuredAt` only.
`measuredAt` is an ISO-8601 UTC string (`YYYY-MM-DDTHH:MM:SSZ`).

Analog `finish`:

```json
{
  "status": "applied",
  "result": {
    "slope": 5.0,
    "offset": -1.0,
    "buffers": [4.0, 7.0],
    "measuredAt": "2026-10-26T14:00:00Z"
  },
  "message": "pH calibration applied"
}
```

Analog `offset` is the intercept so `pH = slope * V + offset`
(`7.0 - slope * V@7`).

RS485 multi-sensor `finish` sends the points and may have `slope` /
`offset` null. The server accepts that as applied:

```json
{
  "status": "applied",
  "result": {
    "slope": null,
    "offset": null,
    "buffers": [4.01, 6.86, 9.18],
    "measuredAt": "2026-10-26T14:00:00Z"
  },
  "message": "pH calibration applied"
}
```

## `rs485_config`

Wire `sensor` names (canonical). Firmware maps them to the existing
probe drivers and rejects anything else with `sensor not allowed`:

| Wire name | Driver |
|---|---|
| `residual_chlorine` | `HondeChlorineSensor` |
| `RD-ORP-WE-01` | `HondeOrpSensor` |
| `RD-PETSTS-01` | `HondeMultiSensor` |

```json
{
  "op": "add",
  "sensor": "residual_chlorine",
  "address": 5,
  "newAddress": 7,
  "enabled": true
}
```

| Field | Used by |
|---|---|
| `op` | `add`, `set_address`, `enable`, `disable` |
| `sensor` | always (wire name above) |
| `address` | `add` and `set_address` (current); optional on enable/disable |
| `newAddress` | `set_address` |
| `enabled` | `enable` (`true`), `disable` (`false`) |

Addresses are 1–247.

`set_address` acts on the live attached probe at `address`. If that
probe is missing, disabled (detached), or at a different address, the
command is rejected with `no probe at that address`. The unit does not
attach a driver for a disabled sensor, does not write the new address
to a different probe, and does not record a config change the command
did not make.

An RS485 result uses the same wire names:

```json
{
  "status": "applied",
  "result": {
    "sensors": [
      {"sensor": "residual_chlorine", "address": 5, "enabled": true}
    ]
  },
  "message": "RS485 sensor added"
}
```

## Result

`POST {cloud_api_base}/v2/devices/{id}/commands/{cmdId}/result`

Header: `X-API-Key` (device key). Accepted only for a `sent` command.

```json
{
  "status": "applied",
  "result": {
    "buffers": [4.0],
    "measuredAt": "2026-10-26T14:00:00Z"
  },
  "message": "pH 4.0 point stored"
}
```

| Field | Notes |
|---|---|
| `status` | `applied` or `rejected` |
| `result` | omitted on most rejections |
| `message` | at most 200 characters, plain words |

HTTP 200: Cloud stored the result. HTTP 409: already recorded; the unit
treats that as success.

The dispatcher has no path to a coil, a smart breaker, or a host pin.
Legacy `deviceCommands` types (`relay`, `awg`, …) stay on the existing
poll and are unknown types on this route.
