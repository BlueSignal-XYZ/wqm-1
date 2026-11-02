# Flash and verify — installer checklist

For a customer install from a card already built from this repo's image
(GitHub **Actions → Card image**). One card, one phone, no SSH.

The older "flash Raspberry Pi OS and run `setup.sh`" path is
[install-day-runbook.md](install-day-runbook.md) §1. Do not mix the two:
Imager customisation on a card image writes Wi-Fi over the setup network.

Writing the card is [flash-a-card.md](flash-a-card.md). This page is what
to prove after that, before you leave the site.

The WQM-1 is designed to connect with water-from-air and rainwater
systems. It reports readings. It does not control those systems.

Never target or change the live field unit `BS-WQM1-0000a92e4e7d`.

## 1. Write the card

1. Download the `wqm1-pi-zero-<commit>.img.xz` (Zero W / Zero 2 W) or
   `wqm1-pi-5-<commit>.img.xz` (Pi 5) from the newest green **Card image**
   run on the branch you were told to use. Master is the shipping branch.
2. Raspberry Pi Imager → **Use custom** → that `.img.xz`.
3. When Imager offers OS customisation (hostname, user, Wi-Fi), choose
   **No**. Customisation is the usual reason the setup network never
   appears.
4. Wait for **Write Successful**. Do not pull the card while it says
   *Verifying*.

Optional: put the card back, `cat /Volumes/bootfs/wqm1-card.txt`, and
eject. That file is the label: firmware version, commit, setup network
name.

## 2. First boot

1. Card into the Pi on the HAT. Power from the 24 V screw terminal, or
   from the Pi's own port on the bench. **Never both at once.**
2. A Pi Zero W can take up to 10 minutes the first time (file system
   grow, keys, firmware on one slow core).
3. The green light on the Pi stops blinking once Linux starts.
   `config.txt` turns it off. A light that is not blinking does not mean
   the card is unread.
4. On the phone, join the open network named on the card label
   (**BlueSignal Setup** on current images). No password.

If the network is missing after 10 minutes, power off, read
`/Volumes/bootfs/wqm1-status.txt`, and use the table in
[flash-a-card.md](flash-a-card.md) §6.

## 3. Commission on the phone

1. The Wi-Fi page should open by itself (captive portal). If it does not,
   open `http://192.168.4.1/setup/`.
2. There is no PIN. The page sets only this unit's Wi-Fi.
3. Confirm the probes that are actually fitted.
4. Pick the site Wi-Fi, type the password, tap Join. The setup network
   will drop. That is expected.
5. Success: the phone lands on the site Wi-Fi, and the setup network is
   gone. Failure: the setup network comes back within about a minute and
   the page says which check failed (password, not found, no address, no
   internet, or BlueSignal not reached).

A join is success only when `wlan0` is on that SSID, has a real IPv4
lease (not `169.254.x`), and the unit reached BlueSignal over HTTPS.

## 4. Claim and first reading

1. Scan the box-label QR and claim the unit in BlueSignal Cloud.
2. On Cloud, wait one sample interval (default 60 s) for the first
   reading. pH, TDS, turbidity and temperature appear once those probes
   are fitted and the HAT is on 24 V. USB-C on the Pi alone will not
   power the analog rails.
3. If Cloud is empty, pull the card and read `wqm1-status.txt`:

| Line | Meaning |
|---|---|
| `station: ok <ip>` | The unit joined the site Wi-Fi. Cloud claim or the ingest URL is the next check. |
| `station: fail: …` | The join did not hold. Repeat §3. |
| `hotspot: up` | Still on the setup network. Join did not finish. |
| `COMMISSION: READY` | First-boot checks passed. Look at Cloud and the journal. |

On a bench with SSH (optional): `journalctl -u bluesignal-wqm -n 80`
should show `All subsystems initialised` and a `Stored id=` line. A
pending-command poll that finds nothing, or a Cloud that is unreachable,
must not stop that line.

## 5. Before you leave

- [ ] Setup network is gone; the unit is on the site Wi-Fi.
- [ ] Cloud shows the unit claimed and at least one reading.
- [ ] No `power: undervoltage seen` on `wqm1-status.txt`.
- [ ] The owner has the claim QR and knows the setup network is only for
      first install, not daily use.

OTA stays on hold. Do not target this unit with a firmware release
until a board has passed the bench test and the Captain has approved it
([ota-hold.md](ota-hold.md)).
