# Installer mistakes

Written for the person holding the phone at the tank, not for the shop.
The setup page says the same things. This page is the longer version, for
the night before and for the drive back when something did not join.

The unit's radio is the one on a Pi Zero W: **2.4 GHz only**.

## The phone will not open the page

The phone joined **BlueSignal Setup** and the browser is blank, or it keeps
using the cell network.

Turn **mobile data off** and stay on **BlueSignal Setup**.

- **iPhone:** if it asks, choose **Use Without Internet**. The setup page
  has no internet. That is the hotspot, not a failure.
- **Android:** if it asks, choose **Stay connected**. Do not let the phone
  drop **BlueSignal Setup** and go back to the cell network.

The page says this at the top: "If your phone asks, tap Keep or Stay
connected."

## The network is not in the list

The unit only sees **2.4 GHz** Wi-Fi.

If the scan is empty, or the name you typed is not in the list, the page
says: "This unit only sees 2.4 GHz WiFi. Turn on 2.4 GHz on the router or
use its 2.4 GHz network."

On the router, turn 2.4 GHz on, or use the network whose name is the 2.4 GHz
one (often the name without `-5G`).

A **hidden** network does not show its name. If the scan says a hidden
network is in range, type the name in "Hidden network name". The unit still
cannot see a hidden network that is 5 GHz only.

## The bars are short

Each network shows four bars. Below about -70 dBm the page says: "Weak
signal here. Mount closer to the router or add an extender before leaving."

Check this with the lid closed, where the unit will actually sit. A join
that worked on the tailgate can be too weak once the enclosure is shut.
After a join the unit writes the signal to `station_rssi` in
`wqm1-status.txt` on the card.

## The network will not take a normal password

The unit joins a normal Wi-Fi password (WPA2). It cannot join these, and
the page says which one it is instead of a generic failure:

- **WPA2-Enterprise** — the router wants a username and a company login.
  Ask for a guest network with a normal password.
- **WPA3 only** — on the router, turn on WPA2 and WPA3 together, or use the
  WPA2 network.
- **Open, with a sign-in page** — the unit can get an address and still be
  stuck on a page that says Agree or asks for an email. It cannot tap that
  page. Use a network with a password, or ask the site to allow this unit
  without the sign-in page. If a join gets an address and then hits that
  page, the setup page says so when the hotspot comes back.

## The password

Type it as the owner says it, including spaces and symbols in the middle.
Use **Show password** to check it.

A space at the **end** is almost always a slip of the thumb. The page
removes that space and tells you before it joins. If the password really
does end with a space, tick "The password ends with a space".

A WPA password is **8 to 63 characters**. Shorter and longer are refused
before the unit drops the hotspot.

## Probes, before you tap Finish

The finish step shows what the unit actually detected:

- **ADC** — the analog board (pH, TDS, turbidity).
- **1-Wire** — the temperature probe.

If one of those was not detected, Finish stays put until you tick
**Finish without probe ADC** or **Finish without probe 1-Wire**. That tick
is the record that you meant to leave it off. Do not tick it to get out of
the driveway and plan to come back.

## The label on the box

The hotspot name and its password are printed from the Pi serial, on the
bench, before the unit leaves:

```bash
python3 scripts/make-unit-label.py <pi-serial> --out-dir ./labels
```

That writes an SVG and a PDF named after the unit's hostname
(`wqm1-4fd8-setup-label.svg`): the network name **BlueSignal Setup**,
"No password", and a Wi-Fi QR code (`WIFI:T:nopass;S:BlueSignal Setup;;`).
The phone camera joins the setup network from the QR. The network is the
same on every unit and has no password (install plan §5).

The script runs on the bench computer and prints the file paths.
See also [burn-in.md](burn-in.md).

## Hardware the page cannot see

These are not in the wizard. They are how units come back in the truck.

- Seat the Pi on the HAT **CPU up**. A Pi pressed on upside down does not
  boot, and the header pins bend.
- Do not pull power during the first boot. Wait until the hotspot
  **BlueSignal Setup** is up. Killing power in that minute corrupts the card.
- Use the supplied power supply and a **short** cable. A long thin cable
  sags the voltage and the unit reboots in a loop. The status file calls
  that undervoltage.
- Do not put the unit in a metal box with the antenna inside. Use a
  plastic enclosure, or mount it so the antenna side faces out of the metal.
- Check the probe ports before you leave. A probe in the wrong port reads
  as a plausible number that is not the water. The ADC and 1-Wire lines on
  the finish step are the check that the board and the temperature probe
  answered.
