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
   factory PIN on the hotspot during first setup.
3. Set a PIN, then watch the probes. A carded unit skips the identity and
   cloud-key steps because the bench card supplied both.
4. **Network, last:** the owner picks their Wi-Fi and types the password.
   *Join and finish* completes setup on the unit. Other saved Wi-Fi,
   including the shop network, is forgotten, setup is recorded, and the
   firmware restarts.
5. Scan the **box label QR**, which opens the claim page in BlueSignal Cloud.

A wrong password brings the hotspot back. The reason is kept on the unit, so
the page that reopens says what went wrong.

## Where it lives

| Piece | File |
|---|---|
| Captive DNS (`address=/#/192.168.4.1`, DHCP option 114) | `utils/captive_portal.dnsmasq_shared_config`, written by `setup.sh` and each boot by `scripts/wqm1-ap-fallback.py` into `/etc/NetworkManager/dnsmasq-shared.d/wqm1-captive.conf` (shared mode only) |
| Port 80 captive listener, hotspot only | `utils/captive_portal`, `systemd/bluesignal-captive-portal.service` (install user + `CAP_NET_BIND_SERVICE`). Closes when the station joins. |
| Service Window, including setup, still on 8080 | `service_window` — the port-80 process proxies the setup UI here so the phone never sees `:8080` |
| Probe paths + foreign-host redirect | `utils/captive_portal` on port 80, and `service_window/captive.py` on 8080 (same target: `http://192.168.4.1/setup/`) |
| No factory PIN on the hotspot during first setup | `service_window/auth._first_setup_on_hotspot` |
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
      `/etc/NetworkManager/dnsmasq-shared.d/wqm1-captive.conf` exists, and
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
      minute, iOS rejoins it, and the reopened page names the failure.
- [ ] **After the join,** `nmcli connection show` lists only the owner's
      network and `wqm1-setup-ap`.
