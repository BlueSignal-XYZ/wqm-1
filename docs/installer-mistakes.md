# Installer mistakes

Written for the person holding the phone at the tank, not for the shop.
The setup page says the same things. This page is the longer version, for
the night before and for the drive back when something did not join.

The phone on **BlueSignal Setup** sees one page: pick the Wi-Fi, type its
password, tap **Connect**. That page sets only the unit's Wi-Fi. The probes
are confirmed afterwards in BlueSignal Cloud, on the unit's page.

The production unit (Pi Zero 2 W) hears **2.4 GHz only**. A Pi 4 or Pi 5
bench unit hears 5 GHz too.

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

Most home routers broadcast one name on both 2.4 and 5 GHz. The unit joins
the 2.4 GHz side of that name by itself.

If the scan heard no 2.4 GHz network at all, the page says: "This scan did
not hear a 2.4 GHz network. You can still pick a name from the list, or type
it." Next to a name heard only on 5 GHz it says: "This scan only heard 5 GHz
for that name. You can still join it. The unit uses 2.4 GHz when it can."

If the join then fails as "not found", the router's 2.4 GHz side is off or
out of reach. In the router app, turn 2.4 GHz on, or use the network whose
name is the 2.4 GHz one (often the name without `-5G`). A router that runs
5 GHz only cannot be joined by a Pi Zero 2 W.

A **hidden** network does not show its name. If the page says "A hidden
network is in range. Type its name.", pick **Other network (type its
name)** and type it.

## The bars are short

Each network shows four bars. Below about -70 dBm the page says: "Weak
here. Readings may drop. Move the router or add an extender."

Check this with the lid closed, where the unit will actually sit. A join
that worked on the tailgate can be too weak once the enclosure is shut.
After a join the unit writes the signal to `station_rssi` in
`wqm1-status.txt` on the card.

## The network will not take a normal password

The unit joins a normal Wi-Fi password (WPA2, or WPA2/WPA3 together). It
cannot join these, and the page greys the network out and says why:

- **WPA2-Enterprise** — "That network needs a work login, not just a
  password." Ask for a guest network with a normal password.
- **WPA3 only** — "That network is WPA3 only. Turn on WPA2/WPA3 in the router
  app, or use another network."
- **WEP** — "That network uses old WEP security." Use another network.
- **Open, with a sign-in page** — the unit can get an address and still be
  stuck on a page that says Agree or asks for an email. It cannot tap that
  page. Use a network with a password, or ask the site to allow this unit
  without the sign-in page. If a join gets an address and then hits that
  page, the setup page says so when the hotspot comes back.

## The password

Type it as the owner says it, including spaces and symbols in the middle.
Use **Show password** to check it.

A space at the **end** is almost always a slip of the thumb. The page
stops and says so before it joins. If the password really does end with a
space, tick "The password really ends with a space" and tap **Connect**
again.

A WPA password is **8 to 63 characters**. Shorter and longer are refused
before the unit drops the hotspot.

## After you tap Connect

The page changes to "Connecting to <your Wi-Fi>", with a spinner, and says
what happens next:

1. **BlueSignal Setup turns off** while the unit joins, and the phone
   leaves it. That is expected. The phone usually goes back to the cell
   network or the home Wi-Fi.
2. **If the unit can't join, BlueSignal Setup comes back within five
   minutes**, usually within one. Join it again and the page says why:

   | The page says | Do this |
   |---|---|
   | The password was not accepted. | Check it with **Show password** and try again. |
   | That network wasn't found. Move the unit closer to the router. | Closer, or turn the router's 2.4 GHz on. |
   | Your router didn't give the unit an address. | Restart the router, or check its device limit. |
   | The unit joined, but the router isn't connected to the internet. | Fix the router's internet. |
   | Your router may be blocking new devices. | Approve the unit in the router app (Eero, Firewalla, Spectrum and others ask), then **Connect** again. |
   | This network needs a sign-in page the unit can't do. | Use a network with a normal password. |
   | The unit couldn't join that network. | Check the password, move closer, and try again. |

3. **If BlueSignal Setup does not come back, the unit is on the Wi-Fi.**
   Finish in BlueSignal Cloud: claim the unit and confirm the probes that
   are fitted on its page.

Do not power the unit off while it joins. A unit that did not join keeps
BlueSignal Setup on, so you, or the next visit, can set the Wi-Fi later.

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
  as a plausible number that is not the water. A first reading for each
  fitted probe in BlueSignal Cloud is the check that the board and the
  temperature probe answered.
