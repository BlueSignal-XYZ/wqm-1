# Installer Manual Rev G — audit against shipping firmware

Audit of the **WQM-1 Product & Installer Manual**, doc `WQM1-IM-100` **Rev G**
(July 2026, 35 pp), checked line by line against firmware `v2.0.0` in this repo,
the published pages on bluesignal.xyz, and Texas/NEC/FCC/MMWA requirements.

The manual is maintained **outside these repos**. This file is the change-list
for regenerating it as **Rev H**, and follows `manual-errata-revD9.md`.

**Rev G must not be published as-is.** One blocker (§1) reverses a
founder-approved correction and states a capability the hardware does not have.
Two more findings (§2, §3) put signed customer attestations and the firmware
license on the wrong side of the facts.

Most of the document is accurate — see "Verified correct" at the end, which is
long on purpose. The findings below are the exceptions.

---

## BLOCKERS — must be fixed before the manual is published anywhere public

### 1. ORP on the BNC does not exist on Fin_3. Rev G re-added it after Rev E removed it.

Rev G's own changelog leads with:

> "ORP restored as a selectable mode on the pH BNC (one BNC: pH or ORP; RS485
> digital ORP for both at once) · ORP calibration added"

`manual-errata-revD9.md` line 41, founder-approved 2026-07-16, says the
opposite:

> "5. ANALOG ORP — on Fin_3 the BNC front end has no working analog ORP (AIN3 is
> spare). **Stop showing "pH or ORP" on the BNC as either/or.** ORP now comes
> from the digital RS485 ORP probe (Section B). Keep pH on the BNC."

Rev G undid that. Three independent places in the shipping firmware confirm the
correction was right:

| Source | Says |
|---|---|
| `config/pinmap.yaml:14` | `spare: 3  # AIN3, PH_INN (no ORP on Fin_3)` |
| `src/sensors/orp.py:4-6` | "On PCBA Fin_3, AIN3 is wired to PH_INN (LMP91200 reference via R13) — no ORP conditioning circuit or connector is present. **ORP readings are non-functional on Fin_3 hardware.**" |
| `src/main.py:213` | `logger.info("ORP disabled (orp_enabled=false); AIN3 is spare on PCBA Fin_3")` |

Two things are wrong, and the second is worse than the first:

1. There is **no pH/ORP mode switch on the BNC.** In firmware pH is **AIN2**
   (`PH_INP` from the LMP91200). The analog `ORPSensor` reads **AIN3**, a
   different ADC channel — it is not the same signal path re-tasked. The manual
   describes a selector that does not exist in hardware or firmware.
2. **AIN3 is the pH front end's own reference input.** Enabling analog ORP does
   not produce a bad ORP reading; it produces `(PH_INN − 2.048 V) × 1000`, a
   number that will look plausible and sit stable. Section 12 tells the
   installer to calibrate that number against a Zobell standard, which will
   apply an offset and make it look calibrated.

The failure mode is a disinfection dosing rule (Section 10 lists ORP as the
trigger for UV/ozone and chlorine dosing) driven by a voltage that has nothing
to do with the water.

**Rev H:** restore the Rev E language everywhere. The BNC is **pH only**. ORP
comes from the RS485 digital probe (RD-ORP-WE-01, ±1999 mV), which works and is
already documented correctly in Section 08. Affected: the Rev G changelog line
(p2), Section 01 channel table (p6), Section 03 spec incl. "usable ORP span
approximately ±1 V" (p7), Section 04 board map item 11 and the FIG 4.1 callout
(pp8–9), Section 06 (p11), Section 08 (p15), Section 12 ORP calibration row
(p22), Appendix A (p27), Appendix C right-edge callout (p30), Appendix B.5 row
label (p29).

If a board revision after Fin_3 adds an ORP conditioning circuit, this reverses
— but then the manual must say which revision, because Rev G names Fin_3 on
pages 8, 9 and 30.

### 2. Firmware licence clause contradicts GPL-3.0 and is ineffective as written

Section 15.4:

> "BlueSignal grants the purchaser a non-exclusive, **non-transferable** license
> to use the embedded software **solely as installed on the unit**."

The firmware is **GPL-3.0** (`LICENSE-FIRMWARE`, README licence table, and
bluesignal.xyz/terms, which tells customers the firmware "may be used, studied,
modified, and redistributed on those terms"). GPL-3.0 §10 forbids imposing
further restrictions on the rights it grants, so this sentence is both wrong
about the product and unenforceable as to the GPL'd portion.

**Rev H:** state that firmware is GPL-3.0 with source at the public repo, keep
"licensed, not sold" only for any genuinely proprietary component, and keep the
existing (correct) sentence that open-source components stay governed by their
own licences. The p35 footer already points the right way — §15.4 is the
outlier. Distributing GPL software on a device also carries a source-offer
obligation; the manual is the natural place to state where source lives.

---

## HIGH

### 3. "Staleness reversion" is not what the firmware does — and the customer signs for it

Section 13 check 18, Appendix F check 18, and Appendix E (the **customer-signed**
handover record) all attest:

> "Staleness reversion demonstrated — probe pulled, channel **reverted**, cause
> logged"

What the firmware actually does is *suspend the rule*. `src/control/rules.py:239`:

```python
if rule.sensor in self._suspended_columns:
    logger.debug("Rule for %s suspended (sensor health)", rule.sensor)
    continue
```

`continue` — no action is appended, so the relay **holds its last state**.
Nothing drives it to a fail-safe position. For a rule with `duration_s: 0`
("stay until condition clears", the shipped default for the dosing examples in
`policies.yaml`), a channel that was energised when its probe went stuck stays
energised, and the rule that would have cleared it no longer evaluates. The
auto-shutoff timer loop runs outside the suspension check, so only rules with
`duration_s > 0` self-cancel.

Section 11's wording is accurate — "that channel's relay rules are suspended".
The checklist and the handover record overstate it.

**RESOLVED — the firmware was fixed rather than the manual reworded.** The two
options were (a) reword checks 18 down to what the code did, or (b) implement
reversion and keep the wording. (b) shipped, because the manual was describing
the right product: a newly suspended sensor now queues every channel its rules
drive for a one-shot de-energize, and **de-energizing is the fail-safe state by
construction** — fail-safe direction lives in the field wiring (COM→NO stops,
COM→NC runs), so dropping the coil puts each channel exactly where its installer
decided a dead controller should leave it, with no firmware-side table of
wiring choices to go stale.

Fixed in the same change: the auto-shutoff sweep sat below the schedule guard's
early return, so a relay switched on at 20:59 with a 30 s duration stayed on
until the window reopened. Both de-energizing paths now run ahead of the
schedule and manual-override guards.

Six tests added, five of which fail against the previous `rules.py`.

**Rev H:** no wording change needed to checks 18 or Appendix E — they are now
true. §15.3(09)'s exclusion for defeating staleness reversion is likewise now
an exclusion for defeating something real.

### 4. The one-year suit limitation is void under the manual's own governing law

Section 15, p26:

> "Governing law: State of Texas ... Any action for breach of this warranty must
> be commenced within **one (1) year** after the cause of action accrues."

**Tex. Civ. Prac. & Rem. Code §16.070(a)** voids a contractual stipulation that
shortens the limitations period to less than **two years**. The clause is
unenforceable under the law the same sentence selects.

**`/warranty` is fixed** — changed to two years, the shortest period Texas will
actually enforce, with the statute cited in a comment so it does not get
shortened back. **Rev H must make the same change**, or the printed warranty and
the published one will disagree about the deadline to sue, which is a worse
position than either number alone.

Not legal advice — still on the list for the Texas attorney review the AWG brief
mandates, but a void clause should not sit in a public document while waiting
for a lawyer to say it is void.

---

## MEDIUM

### 5. ~~Input voltage conflicts with the published warranty page~~ — WITHDRAWN

I raised this as a conflict between the manual's "24 V DC nominal" and
`/warranty`'s "9–24 V DC". It is not one, and the answer was already written
down: the header comment in `WarrantyPage.jsx` records the founder's decision of
2026-08-01 — the manual specifies what to **install** (a listed 24 V 2 A Class-2
supply), the page states what the board **tolerates**, and the range reaches
below 24 V because the compute module's USB input is used for bench work. Both
are true of the same hardware.

That comment also says, in terms, not to reconcile the two numbers by narrowing
the range, because a published voltage range is an express warranty by
description and narrowing it would strand legitimately bench-powered units
outside coverage. Correct, and left alone.

**Rev H should say this out loud** so the next reader does not re-derive it as a
bug: one sentence in Section 07 noting that the tolerated input range is wider
than the specified install voltage, and that the warranty page states the former.
An audit finding that keeps getting re-raised and re-dismissed costs more over
time than the sentence does.

### 6. Three different response commitments, two for the same channel

| Where | Promise |
|---|---|
| §14 escalation step 02 | Support ticket from the site record — "response within **one business day**" |
| §15 p26 | Warranty claim, incl. "a ticket from the site record" — "within **two (2) business days**" |
| §15.2 | RMA processing — "within **ten (10) business days** of receipt" |
| `/warranty` page | "respond within **2 business days**" |

§15.2 is a distinct commitment and fine. The first two describe the *same
channel* with different numbers. Pick one.

### 7. SLD-001 Note 6 contradicts its own drawing

Note 6 reads "SUPPRESSION ON ALL INDUCTIVE LOADS AND COILS. **12 AWG CU MIN.**"
The same sheet shows CB1 feeding the PSU with **14 AWG**, and Section 07 calls
for **18 AWG minimum** on the DC feed. As printed the note contradicts both.
State what the 12 AWG applies to, or drop it — Appendix B already sizes
conductors properly.

### 8. Warranty conditioned on a paid subscription — Magnuson-Moss risk

§15.3(08) voids coverage for "relay-based control operated without an active
control-tier subscription." Conditioning a hardware warranty on the purchase of
a service is close to the tie-in prohibition in **15 U.S.C. §2302(c)** for
consumer products. The safety-practice exclusions (suppression, contactors,
NC on life-critical) are ordinary and defensible; this one is different in kind.

**`/warranty` is fixed**, and the replacement is better targeted rather than
merely safer: the exclusion now names *the commissioning procedure* — declared
contact type, fail-safe direction, load current, dwell times, and the logged
test-fire from Section 11. What wears out a relay is an uncommissioned channel,
not an unpaid invoice. The subscription can still gate the *feature* in
software; it just should not gate the *warranty*.

**Rev H:** replace §15.3(08) with the same commissioning-based wording.

### 9. Appendix A calls AIN3 "spare" — it is PH_INN

Appendix A: "AIN0 = TDS ... AIN1 = turbidity ... **AIN3 spare**." Per
`pinmap.yaml`, AIN3 is `PH_INN`, the LMP91200 reference. An integrator told a
pin is spare may drive it. Say "AIN3 — reserved, carries PH_INN; do not drive."
(This is the same root cause as §1: "spare" is how the pin map describes the
absence of ORP.)

---

## LOW

10. **Appendix B.1, 18 AWG row.** The header states stranded uncoated copper at
    75 °C per NEC Ch.9 Table 8 (18 AWG = 8.45 Ω/kft), which gives **43 ft** at
    1 A, not the **45 ft** printed; 45 matches the *solid* figure (8.08). Every
    other cell in B.1 and B.2 recomputes exactly (16 AWG 71, 14 AWG 115, 12 AWG
    182, 10 AWG @5 A 290). Change 45→43, or the callout "past 45 ft" to 43.
11. **FCC ID `2AD66-1262` must be verified against the actual grant** before this
    goes public, and Part 15 Subpart B normally wants the Class A/B statement
    alongside the Part 15 conditions. Compliance reviewer, not me.
12. **Appendix A is split across pp27 and 35**, and the contents page renders it
    "27 · 35", which reads as a typo. Either move the regulatory block into its
    own appendix or label it "27, 35".
13. **Appendix B.4** — "never fit a DC-blocking inline attenuator or surge
    arrestor on the GPS run" is correct but parses two ways; §7.1 *requires* a
    DC-passing GPS arrestor. Rephrase to "must be DC-passing" in both places.
14. **Appendix F (Spanish)** rule 04 drops the English §05.07 detail about
    switching a contactor coil through NC where load current exceeds the 3 A NC
    rating. Safety-relevant; add it.
15. **§15.1 vs `/warranty` 90-day list** — the manual covers "antennas, terminal
    blocks, and cables"; the page covers "antennas and accessories, including the
    power cable and enclosure". Neither is wrong, they just differ. Align.

---

## Verified correct — checked, no change needed

Recorded so Rev H does not "fix" something that is already right.

**Pin map — every assignment matches `config/pinmap.yaml` exactly:** relays
GPIO17/27/22/23 · status LEDs GPIO24/25/12/13 · 1-Wire GPIO4 · fan GPIO21 ·
LoRa SPI0 CS GPIO8 / RST GPIO18 / BUSY GPIO20 / DIO1 GPIO16 · GPS UART with
EXTINT GPIO19 · I²C1 GPIO2/GPIO3 with ADC ready GPIO5 · ADS1115 AIN0 TDS
(0–2.3 V) and AIN1 turbidity (0–4.5 V).

**Timing** — `sensor_read_s=60` default and the 5–3600 s configurable range,
`lora_tx_s=300` (5 min uplink), `sync_interval_s=300` (5 min cloud sync) all
match `src/utils/config.py`.

**Relay behaviour** — active-high through LTV-354T optocouplers
(`src/control/relay.py`); "ships inert" matches `policies.yaml`
`manual.override: true`; the illustrative CH1–CH4 map is correctly presented as
editable rather than fixed.

**Sensor-health suspension exists** (`SensorMonitor.suspended_sensors()` →
`RulesEngine.set_suspended_sensors()`), so Section 11's drift/stuck/spike
paragraph is accurate as written — it is only checks 18 that overstate it (§3).

**Appendix B arithmetic** — B.1 and B.2 recompute from Vd = 2·L·I·R/1000 at 3 %
of 24 V and 120 V using NEC Ch.9 Table 8 (one row off, §10 above). B.3/B.4 coax
figures match published specs at 915 MHz (RG-174 27 dB/100 ft, RG-58 16,
LMR-195 11.1, LMR-240 7.6, LMR-400 3.9).

**Code references** — NEC 250.50/250.53 (rod spacing 6 ft), 430.102 (disconnect
within sight), 430.72 (coil conductors), Class 2 usage. Texas trades: TSBPE
plumbing, TCEQ irrigator licensing incl. the advertising prohibition.

**GPS bias** — the DC-passing arrestor requirement and the active-antenna LNA
gain budget are right, and are the sort of thing that is usually wrong.

**Cloud tiers** match the published `CLOUD_TIERS` (Hobbyist free · Residential
$4.99/mo · Commercial $10/mo).

**Counts** — 19 checklist items in Section 13 and 19 in Appendix F, matching the
"19 checks" callouts and Appendix E.

**No channel-confidential data.** No installer cost, margin, MOQ, or supplier
part numbers anywhere in the document — the only prices are public cloud tiers
and third-party retail ranges. Safe to publish on that count once the technical
findings are resolved.

---

## Rev H regeneration brief

Hand the fenced block below to whoever (or whatever) regenerates the manual. It
is self-contained and assumes no access to this repo.

```
ROLE: Update the BlueSignal WQM-1 Product & Installer Manual from Rev G to
Rev H (doc WQM1-IM-100). Preserve structure, voice, visual system, section
order, drawing set, and page count as closely as possible. Rev H is a
correctness pass, not a redesign.

=== A. REMOVE ANALOG ORP FROM THE BNC (the reason this revision exists) ===
The BNC accepts a pH electrode ONLY. There is no pH/ORP mode switch, in
firmware or hardware, on PCBA Fin_3 — the revision this manual documents.
Rev G reintroduced a claim that Rev E had deliberately removed.

ORP is available exactly one way: the optional RS485 digital ORP probe
(RD-ORP-WE-01, +/-1999 mV) on the Modbus bus, which Section 08 already
documents correctly and which should now be the ONLY place ORP appears as an
available measurement.

Edit every one of these:
 - p2 changelog: delete the "ORP restored as a selectable mode" line. Rev H's
   changelog should say ORP on the BNC was removed in error-correction, and
   that ORP is RS485-only.
 - p6 Section 01 channel table: the row becomes "pH" alone. Keep the separate
   RS485 row that lists chlorine / EC / ORP as optional digital probes.
 - p7 Section 03 spec: the front-end row describes a pH AFE. DELETE the
   sentence "accepts a pH or an ORP electrode on the same BNC; usable ORP span
   approximately +/-1 V, which covers the full practical range for water
   treatment."
 - p8 FIG 4.1 callout 11 and p9 board map row 11: "pH BNC" / "pH electrode".
 - p11 Section 06 and p15 Section 08: "pH BNC", not "pH or ORP BNC".
 - p22 Section 12: DELETE the "ORP (BNC)" calibration row. Keep the RS485
   chlorine and 5-in-1 rows. In the recalibrate table, "pH or ORP (BNC)"
   becomes "pH (BNC)".
 - p27 Appendix A: delete "and is selectable between pH and ORP in firmware".
   Change "AIN3 spare" to "AIN3 — reserved (carries PH_INN, the pH front end's
   reference leg); do not drive." Keep the existing sentence pointing
   integrators at the RS485 probe for ORP.
 - p29 Appendix B.5: the cable row is "pH (BNC coax)".
 - p30 Appendix C right-edge callout: "pH BNC".

=== B. WARRANTY AND LEGAL (Section 15) ===
1. LIMITATIONS PERIOD: change "within one (1) year after the cause of action
   accrues" to "within TWO (2) YEARS". Tex. Civ. Prac. & Rem. Code 16.070
   voids any contractual period under two years, and Texas is the governing
   law this same clause selects. bluesignal.xyz/warranty has already been
   changed to two years; the two documents must agree.
2. FIRMWARE LICENCE (15.4): the WQM-1 firmware is GPL-3.0. Replace "grants the
   purchaser a non-exclusive, non-transferable license to use the embedded
   software solely as installed on the unit" with a statement that the
   firmware is licensed under GPL-3.0, that source is available at the public
   BlueSignal repository, and that the licence grants the right to use, study,
   modify and redistribute on its terms. Keep the existing sentence about
   open-source components retaining their own licences. Keep "licensed, not
   sold" ONLY for genuinely proprietary components, if any remain. GPL-3.0
   section 10 forbids the further restrictions the current wording imposes,
   so the paragraph is not merely inaccurate, it is ineffective.
3. SUBSCRIPTION TIE-IN (15.3 item 08): replace "Relay-based control operated
   without an active control-tier subscription" with "Relay channels actuated
   without completing the commissioning steps in Section 11 — declared contact
   type, fail-safe direction, load current, dwell times, and the logged
   test-fire." Conditioning a hardware warranty on buying a service is the
   tie-in 15 U.S.C. 2302(c) prohibits for consumer products; conditioning it
   on correct commissioning is both lawful and better aimed. The subscription
   may still gate the feature in software.
4. SUPPORT RESPONSE TIME: Section 14 escalation step 02 promises "response
   within one business day"; Section 15 promises two business days for the
   same channel (a ticket opened from the site record). Make both TWO business
   days, matching the published warranty page. The 10-business-day RMA
   processing commitment in 15.2 is a different thing and stays.

=== C. THINGS THAT ARE TRUE NOW AND WERE NOT WHEN REV G WAS WRITTEN ===
Commissioning check 18, Appendix F check 18, and the Appendix E handover line
about staleness reversion are CORRECT as printed and must be kept. The firmware
now de-energizes every channel driven by a sensor that has just been suspended,
which puts the channel in whatever state its wiring defines as safe. Optionally
add one clarifying sentence to Section 11: "Reversion de-energizes the channel.
Because fail-safe direction is set by your wiring, a load on NO stops and a load
on NC runs — the same behaviour as a dead controller, which is what Section 05's
NC rule is for."

=== D. SMALLER CORRECTIONS ===
5. SLD-001 note 6 reads "SUPPRESSION ON ALL INDUCTIVE LOADS AND COILS. 12 AWG
   CU MIN." The same sheet shows 14 AWG on CB1 and Section 07 specifies 18 AWG
   minimum for the DC feed. Delete "12 AWG CU MIN" — Appendix B sizes
   conductors and the note contradicts both the drawing and Section 07.
6. Appendix B.1, 18 AWG row: at the stated basis (stranded uncoated copper,
   75 C, NEC Ch.9 Table 8, 8.45 ohm/kft) 1 A gives 43 ft, not 45. Change the
   cell to 43 and the "past 45 ft on a 1 A DC load" callout to 43 ft. Every
   other cell in B.1 and B.2 is exactly right; do not touch them.
7. Section 07: add one sentence noting that the board TOLERATES a wider input
   range than the 24 V DC install specification, and that the published
   warranty page states the tolerated range. This is not a contradiction and
   should stop being re-raised as one.
8. Appendix B.4: "never fit a DC-blocking inline attenuator or surge arrestor"
   parses two ways. Rephrase to "any inline device on the GPS run must be
   DC-passing — a DC-blocking arrestor or attenuator will kill the active
   antenna", matching Section 7.1 which REQUIRES a DC-passing GPS arrestor.
9. Appendix F (Spanish) rule 04 omits the English Section 05.07 detail about
   switching a contactor coil through NC where load current exceeds the 3 A NC
   rating. Add it — it is safety-relevant.
10. Appendix A is split across pp27 and 35 and the contents page renders this
    as "27 · 35", which reads as a typo. Either give the regulatory block its
    own appendix letter or label it "27, 35".
11. VERIFY BEFORE PUBLISHING: the FCC ID printed on p35 (2AD66-1262) must match
    the actual grant for the LoRa module as shipped, and Part 15 Subpart B
    normally requires the Class A/B statement alongside the two Part 15
    conditions. This is a compliance check, not a copy edit.

=== E. DO NOT CHANGE ===
Everything in the "verified correct" list of docs/manual-errata-revG.md,
which includes every GPIO in Appendix A, the 60 s / 300 s / 300 s timing
constants, the relay ratings and active-high optoisolated drive, "ships inert",
the NEC and Texas licensing citations, the cloud tier prices, and all of
Appendix B other than the single 18 AWG cell above.
```

## What is published today — and the founder's decision on the erratum

**2026-08-18.** The manual now on bluesignal.xyz is the authored Rev G, byte for
byte, at `/docs/WQM-1-Installer-Manual-RevG-20260818.pdf`. Founder instruction,
verbatim: *"I dont want the erratum shit, use my latest version."*

That reverses the 2026-08-17 arrangement, where the site served `Rev G-1` — the
same PDF with a correction page bound in after the cover and a red bar on the
four pages that instruct action (`marketplace/scripts/build-manual-erratum.py`,
which still exists and still works but is **out of the publish path**). Do not
reintroduce it without the same person asking for it.

So the three findings below are, for now, live on a public PDF with nothing
guarding them:

| Finding | Status on the published PDF | Where it is actually fixed |
|---|---|---|
| §1 ORP selectable on the pH BNC (pp. 2, 6, 7, 8, 9, 11, 15, 22, 27, 29, 30) | stated as authored | nowhere yet — **Rev H** |
| One-year limitations period (p. 26) — void under Tex. Civ. Prac. & Rem. Code §16.070 | stated as authored | `bluesignal.xyz/warranty` (two years), which p. 26 itself names as governing |
| Exclusion 15.3(08) conditions the relay warranty on a control-tier subscription (p. 25) — 15 U.S.C. §2302(c) tie-in | stated as authored | `bluesignal.xyz/warranty` (keyed to the Section 11 commissioning steps) |

The two warranty rows are half-covered: the manual's own order-of-precedence
clause (p. 26) points at the published warranty page for hardware terms, and
that page is correct. §1 has no such backstop — the manual is the only place an
installer reads how to wire the BNC. **Rev H is now the only fix for it**, which
makes the regeneration brief above the priority item, not a backlog entry.

Three exports of Rev G exist and they are not interchangeable: 17 Aug, 18 Aug
(reworded change list p. 2, redrawn FIG 4.1 callouts p. 8), and the published
one (new p. 2, original p. 8). Each publish takes a **new** filename — Cloudflare
serves `public/` immutable, so changed bytes at an old path stay invisible for up
to a year — and the retired paths are 301'd in `marketplace/firebase.json`, with
`src/pages/landing/manualLink.test.js` failing the build if a redirect ever
points at a file that does not exist.

## Publishing it

The manual was staged for the Doc Center's **published** shelf, verified to
build, and then **backed out** — `ops-docs/` is world-readable by URL, and §1
would put a capability claim the hardware cannot meet in front of installers
who would then commission ORP-driven disinfection dosing on it. That is the one
thing the audit existed to prevent, so it is not a conveyor decision.

Publishing is three steps in `BlueSignal-XYZ/marketplace` once Rev H exists (or
once §1 is confirmed a non-issue on a newer board):

```bash
mkdir -p ops-docs/manuals
cp <manual>.pdf ops-docs/manuals/BlueSignal_WQM1_Installer_Manual_RevH.pdf
```

then add to `ops-docs/titles.json`:

```json
"manuals/BlueSignal_WQM1_Installer_Manual_RevH.pdf": "WQM-1 Installer Manual — Rev H (doc WQM1-IM-100)"
```

and one line to `CATEGORY_LABELS` in `src/ops/panels/DocCenterPanel.tsx`:

```ts
manuals: 'Product manuals',
```

`npm run build:ops` then lists it in the manifest and serves it at
`ops.bluesignal.xyz/docs/manuals/…`. Verified working on the staged copy: the
filename clears the `BLOCKED_NAME_PATTERNS` guard in `build-ops-docs.mjs`, and
the manifest renders the title above.

Filename convention follows the shelf's existing entries
(`BlueSignal_Site_Survey_Access_Agreement.pdf`,
`BlueSignal_Petition_WQP02_RevK_Duplex.pdf`) — the uploaded working title,
`Manual_design_system_upgrade.pdf`, describes the design pass rather than the
document and should not survive into a customer-facing URL.
