# Irrigation hold

One WQM-1 relay becomes a normally-closed contact in an irrigation
controller's rain-sensor loop. When a water condition you set trips, the unit
opens the contact, the controller reads "wet", and it pauses every zone. When
the water has been back under your settings for the release period, the
contact closes and the controller resumes its own schedule.

Source: `src/control/irrigation_hold.py`. Settings arrive from the cloud
device page (`PUT /v2/devices/:id/config`); the Service Window shows the
state but does not edit it.

## What it does and does not do

- It pauses and resumes the controller. It does not run zones, change the
  controller's schedule, or know which zones exist.
- It acts on readings the unit already takes: turbidity, TDS, pH and flow
  rate. **Tank level is not a condition in this version** — the firmware has
  no tank reading to act on.
- "Holding" and "clear" describe the relay contact, never the water. The
  unit reports what it measured against the numbers you entered; it does not
  judge whether the water is fit for any use.

## Wiring

Wired by your licensed irrigator. (Texas licenses landscape irrigators under
TCEQ rules, 30 TAC chapter 344.) BlueSignal supplies the unit and the
settings screen; the connection to the controller is the irrigator's work.

1. Pick a free relay (1–4). It must not be the relay already used as the
   smart-breaker interlock — the unit refuses to arm on that one and reports
   `relay_conflict`.
2. Wire that relay's **COM** and **NC** terminals to the controller's
   rain-sensor input (often marked SEN or RS), either in place of the rain
   sensor or in series with it. In series, either device can pause the
   controller: a wet rain sensor or a tripped hold.
3. Leave **NO** unconnected. Never use COM–NO for this: it inverts the fail
   direction (below).
4. The unit needs its **24 V supply on VRLY**. The relay coil is powered from
   it; a unit running from USB alone cannot open the contact, and the hold
   stays clear.

The NC contact is rated 3 A; a controller's sensor loop carries milliamps at
24 VAC, well inside that. The contacts are dry — no voltage from the WQM-1
reaches the controller. Use it with controllers that accept a
normally-closed rain-sensor contact; check the controller's own manual for
where its sensor terminals are and whether its sensor input must be switched
on.

## Fail direction, and why it is this way round

De-energised, COM–NC is **closed**: the controller sees a dry sensor and runs
its schedule. So power loss, a reboot, a crashed firmware process or a dead
unit all leave the controller doing exactly what it did before the WQM-1 was
fitted. The unit can pause watering; it can never be the reason watering
stops for good.

At boot every relay is switched off before anything else runs, so the
contact is closed until the first sample decides. If the condition is still
present, the hold re-engages after the trip count, as it would on any other
sample.

## Settings

All ten are cloud-editable and apply without a restart (the engine re-reads
them every sampling cycle). The same names exist in the cloud's allowlist.

| Setting | Range | Default | Meaning |
|---|---|---|---|
| `irrigation_hold_enabled` | on/off | off | Master switch. Off: the key is absent from the payload and the relay is an ordinary relay. |
| `irrigation_hold_relay` | 0–4 | 0 | Relay channel; 0 = none (enabled with 0 does nothing and logs once). Must not be `smart_breaker_interlock_relay`. |
| `irrigation_hold_turbidity_ntu` | 0–4000 | 0 | Hold when turbidity ≥ this. 0 = off. |
| `irrigation_hold_tds_ppm` | 0–20000 | 0 | Hold when TDS ≥ this. 0 = off. |
| `irrigation_hold_ph_min` | 0–14 | 0 | Hold when pH < this. 0 = off. |
| `irrigation_hold_ph_max` | 0–14 | 0 | Hold when pH > this. 0 = off. |
| `irrigation_hold_flow_gpm_max` | 0–1000 | 0 | Hold when flow rate ≥ this. 0 = off. |
| `irrigation_hold_trip_samples` | 1–10 | 2 | Consecutive samples with at least one condition tripping before the hold engages. |
| `irrigation_hold_release_min` | 0–1440 | 10 | Minutes every condition must stay clear before the hold releases. 0 = release on the first clear sample. |
| `irrigation_hold_on_fault` | `release` / `hold` | `release` | What to do when a probe a condition depends on cannot be read. |

A condition with threshold 0 is ignored entirely — its probe can be missing
or faulted without affecting the hold.

## How it decides

- **Trip:** a condition trips when its reading is present and crosses its
  threshold. The hold engages after `irrigation_hold_trip_samples`
  consecutive samples with any condition tripping. One clear sample resets
  the count.
- **Release:** once every condition is clear, the release clock starts; the
  hold releases when it reaches `irrigation_hold_release_min`. A re-trip
  during the wait restarts the clock. That delay is the only hysteresis —
  there is no second threshold.
- **Probe fault:** a condition is *unknown* when its reading is missing (the
  driver reported a status instead of a value) or the sensor monitor has
  suspended the probe for no data.
  - `release` (default): any unknown condition releases the hold at once,
    even if another condition is tripping, and the state reports
    `fault: "released"`. A pause nobody can explain is worse than the
    controller's own schedule.
  - `hold`: an unknown condition counts as tripping (the trip count still
    applies) and a hold it causes or extends reports `fault: "held"`.
  - When the probe recovers, the monitor's empty suspension set reaches the
    hold on the next sample and normal evaluation resumes.
- **Relay changes:** changing the relay while holding releases the old
  channel and restarts the trip count on the new one. Disabling the hold
  releases it. A relay write that fails is logged and retried on the next
  sample; the state reports the contact as it was actually driven.

## What else is kept off the hold's channel

While the hold is armed on relay N:

- **Manual commands are refused** — cloud relay commands, the Service Window
  buttons (shown disabled) and LoRa FPort 100 downlinks all get
  `irrigation hold owns relay N`. A manual OFF would silently release a hold.
- **Automation rules on relay N are set aside**, with a WARNING in the log.
  They are not deleted: they resume if the hold is disabled, without a
  restart.
- **The continuous on-time ceiling** (`limits.max_continuous_on_minutes`) does
  not apply to the hold's channel — a hold lasts as long as the condition.
- **The rules engine's probe-fault reversion** does not touch relay N; the
  hold has its own fault policy above.

## Status

In the cloud payload: `metadata.irrigationHold` — shape in
`docs/cloud-payload.md` §6. On the unit, the Service Window dashboard shows
one line, read live from the firmware (the row is left out if the firmware
cannot be reached):

| Line | Contact | Controller |
|---|---|---|
| `off` | ordinary relay | runs its schedule |
| `clear` | closed | runs its schedule |
| `holding since … (turbidity 14 NTU ≥ 8)` | open | paused |
| `clear — probe fault, released` | closed | runs its schedule |
| `not armed — relay N is the breaker interlock relay` | untouched | runs its schedule |

The Relays page labels the channel "Irrigation hold" with the same line.

## Bench test

1. Short the controller's sensor terminals through the relay: COM and NC to
   the SEN terminals, with the controller's rain-sensor input switched on.
   Confirm the controller shows no rain delay with the unit powered off.
2. Power the unit from 24 V. Enable the hold on the cloud device page with a
   turbidity threshold (for the simulator below, 80 NTU).
3. On a virtual unit, drive a spike:
   `scripts/simulate-fleet.py --count 1 --cycles 30 --faults "turbidity:spike@3" --hold-relay 3 --hold-turbidity-ntu 80 --hold-release-min 5 …`
   The spike holds 120 NTU for ten cycles; the hold engages on the second
   spike sample and releases five simulated minutes after it ends. On real
   hardware, lift the turbidity probe into a cloudy sample instead.
4. Watch the controller's rain-delay or sensor indicator come on when the
   dashboard line reads `holding`, and go off when it returns to `clear`.
5. Pull the unit's power while it is holding: the indicator must go off and
   the controller resume. That is the fail direction working.
