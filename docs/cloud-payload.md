# Cloud reading payload — reference

What one reading looks like when `CloudSyncWorker` POSTs it to the ingest
endpoint, and the rules a consumer may rely on. Source of truth is
`src/cloud/client.py` (`CloudClient.reading_to_json`); this page exists so the
cloud side (`marketplace/functions/readings.js`) and the firmware agree on the
contract in writing, not by reading each other's code.

Ships in **2.2.0**, the first release after v2.1.1. Units still on 2.1.1 send
the same document *without* any `status` entries — see §4.

## 1. Shape

```json
{
  "deviceId": "BS-WQM1-0000a92e4e7d",
  "timestamp": 1757300000000,
  "sensors": {
    "ph":          { "value": 7.42 },
    "temperature": { "value": 24.1 },
    "turbidity":   { "value": 3.8 },
    "tds":         { "value": null, "status": "no_conduction" }
  },
  "metadata": {
    "firmware": "2.2.0",
    "signalStrength": -61,
    "relayState": 0,
    "backfill": true,
    "gps": { "latitude": 30.42, "longitude": -97.94, "altitude": 251.0 },
    "radios": { "lora": { "present": true, "chip": "SX1262", "mode": "us915" },
                "gps":  { "fix": true, "sats": 9, "lat": 30.42, "lon": -97.94 } }
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `deviceId` | string | Canonical serial, `BS-WQM1-` + lowercase hex. The cloud canonicalises casing on every intake anyway. |
| `timestamp` | integer ms | From the row's ISO `timestamp`; now() if unparseable. |
| `sensors.<channel>` | object | One entry per channel that has something to say this cycle — see §2. |
| `sensors.<channel>.value` | number or `null` | The measurement in the channel's unit, or `null` when `status` explains why there is none. **Never a fabricated number.** |
| `sensors.<channel>.status` | string, optional | Present only when `value` is `null`. Vocabulary in §3. |
| `metadata.firmware` | string | `VERSION` at build time. |
| `metadata.signalStrength` | integer or `null` | From the health reporter; best-effort. |
| `metadata.relayState` | integer | Relay bitmask; 0 when no relays. |
| `metadata.backfill` | boolean, optional | `true` on rows older than ~23 h so the server's 30-day window accepts post-outage history. Absent otherwise. |
| `metadata.gps` | object, optional | Only when the row carries a fix. |
| `metadata.radios` | object, optional | LoRa presence and the GPS snapshot, for the Cloud Radios card. Only emitted when something is present. |

Channel names are the cloud's keys (`functions/v2/sensorChannels.js`), mapped
from DB columns by `_SENSOR_MAP`: `ph`, `tds`, `turbidity`, `orp`,
`temperature`, `chlorine`, `conductivity`, `salinity`. Battery is not sent
(removed 2026-08-20; see `utils/health.py`).

## 2. Which channels appear

- A channel with a **number** appears as `{ "value": <n> }` — nothing else.
- A channel the analog drivers **refused this cycle** appears as
  `{ "value": null, "status": "<code>" }`. That is the whole point of the
  field: before 2.2.0 a refused channel was simply omitted, and the cloud
  could not tell a probe out of the water from a probe that was never fitted.
  On 2026-08-21 that erased a customer's TDS tile. Omission is not a message.
- A channel that is **not fitted** on this unit does not appear at all. The
  cloud's provisioned sensor inventory (`devices/{id}/sensorInventory`) is
  what says a unit *should* have a channel; this payload only ever says what
  the unit *observed*.
- If a value and a stale status both exist for a channel, **the value wins**
  and the status is dropped. A number is the better answer.

Only the analog channels (`ph`, `tds`, `turbidity`) currently produce a
status; the RS485 probes and temperature report a value or nothing.

## 3. Status vocabulary

Stable strings from `src/sensors/status.py`. They are stored in SQLite, sent
here, and rendered on a customer's dashboard, so **add codes, never rename or
repurpose one.**

| Code | Meaning | Who sets it |
|---|---|---|
| `no_conduction` | The electrode sees effectively nothing — a disconnected probe **or** a probe in air. On this hardware the two are electrically identical, so one code covers both and the customer-facing text names both. | TDS rail check (input at or below `ADC_OPEN_INPUT_V`), turbidity floor |
| `out_of_range` | Signal outside the band the channel can represent: railed high against the channel's own chain, or past documented full scale. Real, but not a measurement. | TDS top-of-chain check, turbidity past full scale, pH outside 0–14 |
| `uncalibrated` | The maths produced something impossible from a plausible voltage (negative ppm, NTU above the clear-water tolerance), or pH has no two-point calibration stored. A calibration or signal-path fault, not a property of the water. | TDS, turbidity, pH |
| `read_failed` | The I²C read itself threw. Says nothing about the probe. | Any analog driver |
| `ok` | Defined for completeness; **never transmitted**, because a channel that is ok carries a value instead. | — |

Why `no_conduction` and not `disconnected`: the firmware cannot distinguish an
unplugged BNC from a dry electrode, and claiming a distinction the electronics
cannot make would send the installer to the wrong fault. The cloud copy says
"disconnected or out of the water".

Why the status rides **inside** `sensors.<channel>` rather than in a sibling
`sensor_status` object: the cloud already keys everything about a channel on
`sensors.<channel>` — range validation, the `latestMetrics` merge, the fitted
inventory, the per-channel `channelStatus` flag — so one object per channel
keeps one lookup per channel on both sides. It was also the shape the cloud
consumer was written against first (`functions/v2/latestMetrics.js`,
`statusWritePaths`), so the firmware matched it rather than the other way
round.

## 4. Compatibility

- **A healthy unit's payload is byte-identical to 2.1.1's.** Statuses are
  only recorded for non-ok channels (`workers.py` leaves `sensor_status`
  NULL on a clean cycle), so nothing is added until something is wrong.
  Pinned by `tests/test_sensor_status.py`.
- **An old ingest that ignores `status`** still receives a valid reading:
  `value: null` is treated as an invalid sample for that channel and the
  other channels store normally. Nothing in the reading depends on the field.
- **The current ingest** (`functions/readings.js`) reads `status` to (a) mark
  the channel as *fitted* in the sensor inventory — a probe that reports a
  fault is proving it exists — and (b) write `devices/{id}/channelStatus/<ch>`
  with the observation time, cleared only when the same device later sends a
  real number for that channel. Unknown codes are stored as `unknown`, so a
  new firmware code never breaks an old cloud.
- **A malformed `sensor_status` column never costs the reading.** The decoder
  tolerates non-JSON, non-object and non-string entries and simply sends the
  values.

## 5. Where it is tested

- `tests/test_sensor_status.py` — driver refusals carry a reason; a faulted
  channel is sent rather than omitted; a healthy channel is unchanged; a value
  wins over a stale status; a bad column never drops a row; a healthy unit's
  whole payload is byte-identical with and without the column.
- `tests/test_cloud.py`, `tests/test_cloud_v2.py` — the rest of the envelope
  and the per-row sync contract.
- Cloud side: `marketplace/functions/v2/latestMetrics.test.js`,
  `functions/v2/sensorInventory.test.js`.
