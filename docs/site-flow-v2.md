# Site flow v2: the phone joins the unit, one pass, no typing

Added 2026-10-03 on the 2.4.0 commissioning branch; on master since #126
(2026-10-07). No real unit has booted it yet — see the checklist below. The
design and the reasoning live in the marketplace repo,
`docs/operations/wqm1-bench-and-site-commissioning.md`. This page covers
the firmware half and what still has to be proved on a real Pi.

## What the installer does

1. Scan the **Wi-Fi QR on the setup card** in the box with the iPhone camera,
   then tap "Join WQM1-xxxx".
2. The setup page **opens by itself** (captive portal). No address, and no
   PIN. The hotspot passphrase on the card is the proof of presence.
3. Watch the probes if you need to. A carded unit skips the identity and
   cloud-key steps because the bench card supplied both.
4. **Network, last:** the owner picks their Wi-Fi and types the password.
   *Join and finish* completes setup on the unit only after the checks
   below. Other saved Wi-Fi, including the shop network, is forgotten,
   setup is recorded, and the firmware restarts.
5. Scan the **box label QR**, which opens the claim page in BlueSignal Cloud.

The page tells the installer, before Join is tapped, that the setup network
will drop. It comes back within about a minute if the join does not succeed,
and it says which check failed. The sentence is stored before the hotspot
is raised again, so it is already there when the phone rejoins.

## When the join counts as success

`join_network` polls for about 55 seconds. Success is all three:

1. `wlan0` is associated to the SSID the installer chose (not the setup hotspot).
2. `wlan0` has a usable IPv4 address from the router. A link-local address
   (`169.254.x`) is not one.
3. The unit can reach that network's gateway (TCP/443 or TCP/80, including
   a refusal, which means the host answered) or a DNS server (a UDP query).

Otherwise the setup page says, in plain words, which of these failed:

| What happened | Status file | What the page says |
|---|---|---|
| Password rejected | `station: fail: wrong password` | The password was not accepted |
| Network not in range | `station: fail: network not found` | That network was not found |
| Associated, no DHCP lease, or only `169.254.x` | `station: fail: no address` | The router did not give the unit an address |
| Address, but no gateway and no DNS | `station: fail: no internet` | The unit cannot reach the router or DNS |

On every one of those, the hotspot and the port-80 portal come back (the
same re-arm as a wrong password, including the three AP attempts). Setup is
not marked complete, and other saved networks are not forgotten. A success
writes `station: ok <ip>`, closes port 80, and does not leave the portal
listening on the site LAN.

At boot, `scripts/wqm1-ap-fallback.py` keeps a station only when `wlan0`
already has a usable IPv4 address. It does not require the gateway: an
outage while a commissioned unit reboots must not turn the site Wi-Fi off
and raise the setup hotspot instead. The setup join is stricter.

## Where it lives

| Piece | File |
|---|---|
| Captive DNS (`address=/#/192.168.4.1`, DHCP option 114) and DHCP `192.168.4.10-254` | `utils/captive_portal.dnsmasq_ap_config`, written at AP start to `/run/wqm1/dnsmasq-ap.conf` and served by `wqm1-setup-dnsmasq.service` bound to `wlan0`. Stopped before a station join. |
| Port 80 Wi-Fi page, hotspot only | `utils/captive_portal` serves the page and `/api/*` itself; every sentence is in `utils/setup_copy` (English, Spanish). `systemd/bluesignal-captive-portal.service` (install user + `CAP_NET_BIND_SERVICE`). Nothing is proxied to 8080 (install plan §4). Closes when the station joins. |
| The page's join and rescan, as root | `utils/join_service`, `systemd/bluesignal-wifi-join.service`: a local socket at `/run/bluesignal/wifi-join.sock` (0600, install user). The join is `utils/join` (one at a time, result in `join-state.json`). |
| Scan list for the page | `utils/netctl.write_scan_cache`, taken before the setup network comes up, at `/run/bluesignal/scan.json`; a rescan is `iw dev wlan0 scan ap-force` and keeps the old list if that fails |
| Service Window, still on 8080 | `service_window` — closed to the setup network since install plan §3 |
| Probe paths + foreign-host redirect | `utils/captive_portal` on port 80, and `service_window/captive.py` on 8080 (same target: `http://192.168.4.1/setup/`) |
| Setup pages open on the hotspot; no PIN | `service_window/auth.login_required` + `captive.from_hotspot` |
| Network-last wizard; join finishes setup | `service_window/routes/setup.py` |
| Forget every other saved Wi-Fi | `utils/netctl.forget_saved_wifi` |
| Virtual radio for simulated units | `utils/netsim.py` (`WQM1_VIRTUAL_NET`) |
| One factory-fresh demo unit | `scripts/demo-unit.py` |

## Run it virtually

```bash
python3 scripts/demo-unit.py --reset          # http://localhost:8080/setup/
cloudflared tunnel --url http://localhost:8080   # public URL for a browser bot
```

The demo unit runs the real firmware and the real Service Window on the
virtual radio. Its owner network is `Smith-Home`, password `riverstone42`.
The laptop's own Wi-Fi is never touched. Cloud uploads go to a closed
loopback port and buffer.

`scripts/simulate-fleet.py --tier full --commission` walks the same flow on
up to ten units and reports each unit's joined network and remaining saved
networks.

## Not yet proved on hardware: the bench checklist for unit #1

- [ ] **iPhone opens the page by itself** after joining `WQM1-xxxx`, and one
      Android phone does the same. The sheet should load
      `http://192.168.4.1/setup/` with no `:8080`. If it does not, confirm
      `wqm1-status.txt` says `portal: up (80)`, that
      `hostapd_cli status` says `state=ENABLED` and `/run/wqm1/dnsmasq-ap.conf`
      contains `address=/#/192.168.4.1` and DHCP option 114, and
      that `ss -ltnp | grep ':80'` shows the captive-portal process. The
      legacy NAT rule should be absent (`sudo iptables -t nat -S PREROUTING`).
      After the unit joins the site Wi-Fi, port 80 on the unit must not
      answer. Unverified on hardware.
- [ ] **The `pi` user can delete NetworkManager profiles.** The Service
      Window runs as `pi`, and `forget_saved_wifi` calls
      `nmcli connection delete`. The join already needs the same polkit
      permission. If a delete is refused, the log line `Could not forget
      saved Wi-Fi` says so, and the shop network stays saved.
- [ ] **Wrong password, then right password.** The hotspot returns within a
      minute, iOS rejoins it, and the reopened page says the password was
      not accepted. `wqm1-status.txt` shows `station: fail: wrong password`
      after the failure and `station: ok <ip>` after the success.
      `portal: up (80)` while the hotspot is back; `portal: down: station`
      after the success. Unverified on hardware.
- [ ] **Right password, no DHCP.** Turn the router's DHCP off, or join a
      network that does not hand out addresses. The page says the unit did
      not get an address. The hotspot and `http://192.168.4.1/setup/` (no
      `:8080`) come back. `station: fail: no address`. Unverified on hardware.
- [ ] **Link-local only.** A lease of `169.254.x` is the same failure. The
      page names that address. Unverified on hardware.
- [ ] **Slow DHCP.** A lease that arrives within about a minute still
      finishes setup. `station: ok <that ip>`. Unverified on hardware.
- [ ] **Address, no route.** DHCP works and the gateway does not answer.
      The page says the unit cannot reach the router or DNS. The hotspot
      comes back. `station: fail: no internet`. Unverified on hardware.
- [ ] **After the join,** `nmcli connection show` lists only the owner's
      network. `wqm1-setup-ap` is the old NetworkManager hotspot profile
      and is deleted so it cannot come back. `hostapd_cli status` does
      not report `ENABLED`. `ip -4 addr show dev wlan0` shows the
      same address as `station: ok`, and it is not `169.254`.
