"""
Installer-facing Wi-Fi facts for the setup page.

The Pi Zero W radio is 2.4 GHz only, and the setup page is what a hurried
installer sees. This module turns scan text and a typed password into the
sentences on that page. It does not run commands and it does not log secrets.
"""

from __future__ import annotations

import http.client
import re
from typing import Any

# Below this, a unit left in a closet will drop the link after the installer
# leaves. -70 dBm is the line the setup page warns on.
WEAK_RSSI_DBM = -70

BAND_MESSAGE = (
    "This unit only sees 2.4 GHz WiFi. Turn on 2.4 GHz on the router or use its 2.4 GHz network."
)
HIDDEN_NOTE = (
    "A hidden network is in range. Type its name. "
    "This unit only sees 2.4 GHz WiFi. Turn on 2.4 GHz on the router "
    "or use its 2.4 GHz network."
)
WEAK_SIGNAL_MESSAGE = (
    "Weak signal here. Mount closer to the router or add an extender before leaving."
)
ENTERPRISE_MESSAGE = (
    "This network is WPA2-Enterprise. It wants a username and a company login, "
    "not just a Wi-Fi password. This unit cannot join it. Ask the site for a "
    "normal 2.4 GHz network with a password."
)
SAE_MESSAGE = (
    "This network is WPA3 only. This unit joins WPA2 networks that use a password. "
    "On the router, turn on WPA2 and WPA3 together, or use the 2.4 GHz WPA2 network."
)
WEP_MESSAGE = (
    "This network uses old WEP security. This unit cannot join it. "
    "Use a WPA2 network with a password."
)
OPEN_NOTE = (
    "This network is open. If it has a sign-in page, this unit cannot tap through it. "
    "A network with a normal password is the one that works."
)
CAPTIVE_JOIN_MESSAGE = (
    "The unit got an address, but a sign-in page is blocking the internet. "
    "This unit cannot tap through that page. Use a network with a normal password, "
    "or ask the site to allow this unit without a sign-in page."
)
TRAILING_SPACE_WARNING = (
    "Removed a space at the end of the password. "
    "If the password really ends with a space, tick that box and join again."
)
CELL_DATA_NOTE = "If this page doesn't load, turn off mobile data and stay connected to {ssid}."

WPA_PSK_MIN = 8
WPA_PSK_MAX = 63

_SECURITY_NOTES = {
    "enterprise": ENTERPRISE_MESSAGE,
    "sae_only": SAE_MESSAGE,
    "wep": WEP_MESSAGE,
    "open": OPEN_NOTE,
}
_UNJOINABLE = frozenset({"enterprise", "sae_only", "wep"})
_HIGH_BANDS = frozenset({"5", "6"})

_FREQ_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ghz|mhz)?", re.IGNORECASE)
_SUITE_RE = re.compile(r"authentication suites:\s*(.+)$", re.IGNORECASE)
_DBM_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*dBm", re.IGNORECASE)


def cell_data_note(ssid: str | None) -> str:
    """The landing-page sentence. The setup network's name when none is given."""
    from utils.identity import SETUP_SSID

    name = (ssid or "").strip() or SETUP_SSID
    return CELL_DATA_NOTE.format(ssid=name)


def band_of_mhz(freq: int | None) -> str:
    """``2.4``, ``5``, ``6``, or ``unknown``."""
    if freq is None:
        return "unknown"
    if 2400 <= freq <= 2500:
        return "2.4"
    if 4900 <= freq < 5925:
        return "5"
    if freq >= 5925:
        return "6"
    return "unknown"


def parse_freq_token(text: str) -> int | None:
    """MHz from ``2412``, ``2412 MHz``, or ``5.18 GHz``."""
    raw = (text or "").strip()
    if not raw or raw == "--":
        return None
    match = _FREQ_RE.search(raw)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit == "ghz" or value < 20:
        return int(round(value * 1000))
    return int(round(value))


def percent_to_dbm(percent: int) -> int:
    """Inverse of the 0–100 scale the setup page already stores.

    ``percent = clamp((dBm + 100) * 2)``. Values that were clamped at 0 or
    100 cannot be recovered; the middle of the scale is exact.
    """
    return int(round(int(percent) / 2 - 100))


def dbm_to_percent(dbm: float) -> int:
    return max(0, min(100, int(round((dbm + 100.0) * 2))))


def bars_for_dbm(dbm: int | None) -> int:
    """0–4 bars. Two bars is -70 dBm, the last strength that is not a warning."""
    if dbm is None:
        return 0
    if dbm >= -50:
        return 4
    if dbm >= -60:
        return 3
    if dbm >= -70:
        return 2
    if dbm >= -80:
        return 1
    return 0


def is_weak_rssi(dbm: int | None) -> bool:
    return dbm is not None and int(dbm) < WEAK_RSSI_DBM


def bar_glyphs(bars: int) -> str:
    """Four marks an installer can read inside a phone's network picker."""
    filled = max(0, min(4, int(bars)))
    return ("▮" * filled) + ("▯" * (4 - filled))


def format_station_rssi(dbm: int | None) -> str:
    """The ``station_rssi`` line. ``unknown`` when the link did not say."""
    if dbm is None:
        return "unknown"
    return f"{int(dbm)} dBm"


def parse_iw_link(text: str) -> int | None:
    """dBm from ``iw dev wlan0 link`` (``signal: -54 dBm``)."""
    match = _DBM_RE.search(text or "")
    if not match:
        return None
    return int(round(float(match.group(1))))


def _suite_name(text: str) -> str | None:
    low = text.lower()
    if "802.1x" in low or "00-0f-ac:1" in low:
        return "802.1x"
    if "sae" in low or "00-0f-ac:8" in low:
        return "sae"
    if "psk" in low or "00-0f-ac:2" in low or "00-0f-ac:6" in low:
        return "psk"
    return None


def classify_auth_suites(suites: set[str]) -> str:
    """``wpa_psk``, ``enterprise``, ``sae_only``, or ``open``.

    WPA2/WPA3 mixed mode (PSK and SAE together) is a normal password
    network. SAE without PSK is WPA3 only. 802.1X without PSK is enterprise.
    """
    has_1x = "802.1x" in suites
    has_psk = "psk" in suites
    has_sae = "sae" in suites
    if has_1x and not has_psk:
        return "enterprise"
    if has_sae and not has_psk:
        return "sae_only"
    if has_psk or has_sae:
        return "wpa_psk"
    if has_1x:
        return "enterprise"
    return "open"


def classify_nmcli_security(text: str) -> str:
    """NetworkManager's ``SECURITY`` column.

    ``WPA2`` is a password. ``WPA2 802.1X`` is enterprise. ``WPA3`` alone is
    SAE-only. ``WPA2 WPA3`` is mixed mode and can be joined with a password.
    """
    raw = (text or "").strip()
    if not raw or raw == "--":
        return "open"
    up = raw.upper()
    if "802.1X" in up or "ENTERPRISE" in up:
        return "enterprise"
    if "WEP" in up:
        return "wep"
    if "WPA3" in up and "WPA2" not in up and "WPA1" not in up:
        return "sae_only"
    if "WPA" in up:
        return "wpa_psk"
    return "unknown"


def security_message(security: str) -> str | None:
    return _SECURITY_NOTES.get(security)


def _hidden_ssid(ssid: str) -> bool:
    text = ssid.strip()
    if not text:
        return True
    # iw prints an empty hidden SSID as a run of ``\x00``.
    return text.replace("\\x00", "").replace("\x00", "") == ""


def annotate_network(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill band, bars, and security on one scan row. Idempotent."""
    ssid = str(raw.get("ssid") or "")
    hidden = bool(raw.get("hidden")) or _hidden_ssid(ssid)
    if hidden:
        ssid = ""
    signal = int(raw.get("signal") or 0)
    dbm = raw.get("rssi_dbm")
    if dbm is None and raw.get("signal") is not None:
        dbm = percent_to_dbm(signal)
    elif dbm is not None:
        dbm = int(round(float(dbm)))
    freq = raw.get("freq_mhz")
    if freq is None and raw.get("freq"):
        freq = parse_freq_token(str(raw.get("freq")))
    elif freq is not None:
        freq = int(freq)
    band = str(raw.get("band") or "") or band_of_mhz(freq if isinstance(freq, int) else None)
    if raw.get("security"):
        security = str(raw["security"])
    elif raw.get("secured") is False:
        security = "open"
    elif raw.get("secured") is True:
        security = "wpa_psk"
    else:
        security = classify_nmcli_security(str(raw.get("security_text") or ""))
    secured = bool(raw.get("secured")) if security == "unknown" else security not in ("open", "")
    note = BAND_MESSAGE if band in _HIGH_BANDS else security_message(security)
    joinable = security not in _UNJOINABLE and band not in _HIGH_BANDS and not hidden
    return {
        "ssid": ssid,
        "signal": signal,
        "secured": bool(secured) if security != "open" else False,
        "security": security,
        "security_note": note,
        "rssi_dbm": dbm,
        "bars": bars_for_dbm(dbm if isinstance(dbm, int) else None),
        "bar_text": bar_glyphs(bars_for_dbm(dbm if isinstance(dbm, int) else None)),
        "weak": is_weak_rssi(dbm if isinstance(dbm, int) else None),
        "freq_mhz": freq if isinstance(freq, int) else None,
        "band": band,
        "hidden": hidden,
        "joinable": joinable,
    }


def split_nmcli_terse(line: str) -> list[str]:
    """Split one ``nmcli -t`` line. A colon inside a field is escaped as ``\\:``."""
    parts: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in line:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def parse_nmcli_wifi(text: str) -> list[dict[str, Any]]:
    """Rows from ``nmcli -t -f SSID,SIGNAL,SECURITY,FREQ device wifi list``."""
    found: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = split_nmcli_terse(line)
        if len(parts) < 2:
            continue
        ssid = parts[0]
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        security_text = parts[2] if len(parts) > 2 else ""
        freq = parts[3] if len(parts) > 3 else ""
        found.append(
            annotate_network(
                {
                    "ssid": ssid,
                    "signal": signal,
                    "security": classify_nmcli_security(security_text),
                    "security_text": security_text,
                    "freq": freq,
                    "hidden": _hidden_ssid(ssid),
                }
            )
        )
    return _strongest(found)


def parse_iw_scan(text: str) -> list[dict[str, Any]]:
    """BSS list from ``iw dev wlan0 scan``, strongest first.

    A BSS with an empty SSID is a hidden network and is kept. Frequency
    decides the band. Authentication suites decide enterprise and WPA3-only.
    """
    found: list[dict[str, Any]] = []
    ssid = ""
    saw_ssid = False
    signal_pct = 0
    dbm: int | None = None
    freq: int | None = None
    suites: set[str] = set()
    saw_rsn = False
    privacy = False

    def flush() -> None:
        nonlocal ssid, saw_ssid, signal_pct, dbm, freq, suites, saw_rsn, privacy
        if saw_ssid or saw_rsn or freq is not None or dbm is not None:
            if suites:
                security = classify_auth_suites(suites)
            elif saw_rsn:
                security = "wpa_psk"
            elif privacy:
                security = "wep"
            else:
                security = "open"
            hidden = _hidden_ssid(ssid) if saw_ssid else True
            found.append(
                annotate_network(
                    {
                        "ssid": "" if hidden else ssid,
                        "signal": signal_pct,
                        "rssi_dbm": dbm,
                        "freq_mhz": freq,
                        "security": security,
                        "hidden": hidden or not saw_ssid,
                    }
                )
            )
        ssid = ""
        saw_ssid = False
        signal_pct = 0
        dbm = None
        freq = None
        suites = set()
        saw_rsn = False
        privacy = False

    for raw in (text or "").splitlines():
        if raw.startswith("BSS "):
            flush()
            continue
        line = raw.strip()
        if line.startswith("SSID:"):
            saw_ssid = True
            ssid = line.split(":", 1)[1].strip()
        elif line.startswith("freq:"):
            freq = parse_freq_token(line.split(":", 1)[1])
        elif line.startswith("signal:"):
            parsed = parse_iw_link(line)
            if parsed is not None:
                dbm = parsed
                signal_pct = dbm_to_percent(parsed)
        elif line.startswith("RSN:") or line.startswith("WPA:"):
            saw_rsn = True
        elif "Privacy" in line and line.lower().startswith("capability"):
            privacy = True
        else:
            match = _SUITE_RE.search(line)
            if match:
                name = _suite_name(match.group(1))
                if name:
                    suites.add(name)
    flush()
    return _strongest(found)


def _strongest(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per visible SSID (the strongest). Hidden rows collapse to one."""
    best: dict[str, dict[str, Any]] = {}
    hidden: dict[str, Any] | None = None
    for row in rows:
        if row.get("hidden"):
            if hidden is None or int(hidden.get("signal") or 0) < int(row.get("signal") or 0):
                hidden = row
            continue
        ssid = str(row.get("ssid") or "")
        if not ssid:
            continue
        prev = best.get(ssid)
        if prev is None or int(prev.get("signal") or 0) < int(row.get("signal") or 0):
            best[ssid] = row
    out = list(best.values())
    if hidden is not None:
        out.append(hidden)
    return sorted(out, key=lambda item: -int(item.get("signal") or 0))


def band_notice(networks: list[dict[str, Any]]) -> str | None:
    """The 2.4 GHz sentence when the scan saw no 2.4 GHz network.

    A scan that did not report frequency stays quiet: unknown is not 5 GHz.
    An empty scan says the sentence, because a 2.4-only radio that sees
    nothing is usually a router that is only beaconing on 5 GHz.
    """
    if not networks:
        return BAND_MESSAGE
    bands = [str(n.get("band") or "unknown") for n in networks]
    known = [band for band in bands if band != "unknown"]
    if not known:
        return None
    if "2.4" not in known:
        return BAND_MESSAGE
    return None


def unseen_ssid_notice(networks: list[dict[str, Any]], ssid: str) -> str | None:
    """The 2.4 GHz sentence when the typed name is not a visible network.

    A hidden SSID is not in the list on purpose. The sentence still applies:
    the name they typed may be the router's 5 GHz network. The join is still
    attempted so a real hidden 2.4 GHz network can be used.
    """
    name = (ssid or "").strip()
    if not name:
        return None
    for net in networks:
        if net.get("hidden"):
            continue
        if net.get("ssid") != name:
            continue
        if net.get("band") in _HIGH_BANDS:
            return BAND_MESSAGE
        return None
    return BAND_MESSAGE


def find_network(networks: list[dict[str, Any]], ssid: str) -> dict[str, Any] | None:
    name = (ssid or "").strip()
    for net in networks:
        if net.get("hidden"):
            continue
        if net.get("ssid") == name:
            return net
    return None


def has_hidden(networks: list[dict[str, Any]]) -> bool:
    return any(bool(n.get("hidden")) for n in networks)


def joinable_networks(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [n for n in networks if n.get("joinable") and n.get("ssid")]


def blocked_networks(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Networks the list shows but the unit must not try to join."""
    return [n for n in networks if n.get("ssid") and not n.get("joinable")]


def block_code(net: dict[str, Any] | None) -> str | None:
    """Why a scanned network cannot be joined, as a short code, or None.

    ``refused_band`` (5 or 6 GHz), or ``refused_<security>`` for enterprise,
    WPA3-only and WEP. The port-80 page maps the code to a sentence in the
    phone's language (``utils.setup_copy``).
    """
    if net is None:
        return None
    if net.get("band") in _HIGH_BANDS:
        return "refused_band"
    security = str(net.get("security") or "")
    if security in _UNJOINABLE:
        return f"refused_{security}"
    return None


def block_reason(net: dict[str, Any] | None) -> str | None:
    """Why a scanned network cannot be joined, or None when the join may start."""
    code = block_code(net)
    if code is None:
        return None
    if code == "refused_band":
        return BAND_MESSAGE
    return security_message(code.removeprefix("refused_"))


def normalize_password(raw: str, *, keep_trailing_space: bool) -> tuple[str, str | None]:
    """Drop an accidental trailing space, and say so.

    Leading spaces and spaces in the middle are part of the password and
    stay. The warning is returned so the page can show it before the join.
    """
    text = "" if raw is None else str(raw)
    if keep_trailing_space:
        return text, None
    trimmed = text.rstrip(" ")
    if trimmed != text:
        return trimmed, TRAILING_SPACE_WARNING
    return text, None


_PASSWORD_MESSAGES = {
    "password_needed": "This network needs a password of 8 to 63 characters.",  # nosec B105 - a sentence
    "password_char": "The password has a character this unit cannot type. Re-type it on this page.",  # nosec B105 - a sentence
    "password_short": "A Wi-Fi password must be 8 to 63 characters. This one is too short.",  # nosec B105 - a sentence
    "password_long": "A Wi-Fi password must be 8 to 63 characters. This one is too long.",  # nosec B105 - a sentence
}


def password_problem_code(password: str, *, secured: bool) -> str | None:
    """The rule ``password_problem`` states, as a short code, or None.

    ``password_needed``, ``password_char``, ``password_short`` or
    ``password_long``. One rule, two wordings: the Service Window prints the
    English sentence, the port-80 page the phone's language.
    """
    if not password:
        return "password_needed" if secured else None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in password):
        return "password_char"
    length = len(password)
    if length < WPA_PSK_MIN:
        return "password_short"
    if length > WPA_PSK_MAX:
        return "password_long"
    return None


def password_problem(password: str, *, secured: bool) -> str | None:
    """Length 8–63 for a WPA-PSK. A blank password is only valid on open Wi-Fi.

    Spaces and other visible characters count. A control character is refused
    so it cannot be mistaken for a line break in the form.
    """
    code = password_problem_code(password, secured=secured)
    return None if code is None else _PASSWORD_MESSAGES[code]


# Default SSIDs a phone's hotspot advertises. A unit left on one goes offline
# when the installer drives away (spec C6). A name match is a warning, never
# a refusal: a home router can be named anything.
_PHONE_HOTSPOT_RE = re.compile(
    r"(?<![a-z])(iphone|ipad|android|androidap|galaxy|pixel|oneplus|moto|motorola|"
    r"redmi|xiaomi|huawei|oppo|vivo|hotspot)(?![a-z])",
    re.IGNORECASE,
)


def looks_like_phone_hotspot(ssid: str) -> bool:
    """True when ``ssid`` reads like a phone's own hotspot ("Jo's iPhone")."""
    return bool(_PHONE_HOTSPOT_RE.search(ssid or ""))


def wifi_connect_argv(
    ssid: str, password: str | None, iface: str, name: str | None = None
) -> list[str]:
    """nmcli argv. The password is one argument, never a shell string.

    Spaces, quotes, and ``$`` stay in that one argument. Nothing here logs it.
    ``name`` sets the NetworkManager profile name; a join tries under a
    ``bs-try-`` name so a wrong password never deletes a proven profile.
    """
    argv = ["device", "wifi", "connect", ssid, "ifname", iface]
    if name:
        argv.extend(["name", name])
    if password:
        argv.extend(["password", password])
    return argv


def interpret_sign_in_response(status: int, location: str, body: bytes) -> str:
    """``clear``, ``captive``, or ``unknown`` from one HTTP check.

    ``generate_204`` answers 204 with an empty body when the internet is
    open. A redirect, status 511, or an HTML page is a sign-in page.
    """
    loc = (location or "").strip()
    if status == 204 and not loc:
        return "clear"
    if status in (301, 302, 303, 307, 308, 511):
        return "captive"
    if status == 200:
        text = body[:4000].decode("utf-8", "replace").lower()
        if "<html" in text or "sign in" in text or "signin" in text or "captive" in text:
            return "captive"
        if body.strip():
            return "captive"
        return "unknown"
    return "unknown"


def probe_sign_in_page(timeout: float = 1.5) -> str:
    """Ask the well-known Android check whether a sign-in page is in the way.

    Any error is ``unknown``. A dead router stays "no internet"; it is not
    reported as a sign-in page. This function does not log the response body.
    """
    conn = http.client.HTTPConnection("connectivitycheck.gstatic.com", 80, timeout=timeout)
    try:
        conn.request(
            "GET",
            "/generate_204",
            headers={"Host": "connectivitycheck.gstatic.com", "Connection": "close"},
        )
        resp = conn.getresponse()
        body = resp.read(4096)
        status = resp.status
        location = resp.getheader("Location") or ""
    except OSError:
        return "unknown"
    finally:
        conn.close()
    return interpret_sign_in_response(status, location, body)


def classify_probe(state: str) -> str:
    """``detected``, ``missing``, or ``unchecked`` from a status-file word."""
    text = (state or "").strip().lower()
    if text in {"ok", "detected", "present"} or text.startswith("ok "):
        return "detected"
    if text in {"faulted", "absent", "no probe", "not fitted", "missing"}:
        return "missing"
    return "unchecked"


def probe_rows(adc: str, onewire: str) -> list[dict[str, str]]:
    """What the finish step shows for the analog board and the 1-Wire probe."""
    states = {"adc": adc, "onewire": onewire}
    names = {"adc": "ADC", "onewire": "1-Wire"}
    rows: list[dict[str, str]] = []
    for key in ("adc", "onewire"):
        kind = classify_probe(states[key])
        word = {"detected": "detected", "missing": "not detected"}.get(kind, "not checked")
        rows.append(
            {
                "id": key,
                "name": names[key],
                "state": (states[key] or "").strip() or "unknown",
                "kind": kind,
                "word": word,
                "confirm": f"Finish without probe {names[key]}",
            }
        )
    return rows


def unconfirmed_missing(rows: list[dict[str, str]], form: Any) -> list[dict[str, str]]:
    """Missing probes the installer has not explicitly accepted."""
    pending: list[dict[str, str]] = []
    for row in rows:
        if row.get("kind") != "missing":
            continue
        if str(form.get(f"confirm_{row['id']}", "")) != "yes":
            pending.append(row)
    return pending


def missing_probe_message(pending: list[dict[str, str]]) -> str:
    labels = [str(row["confirm"]) for row in pending]
    if len(labels) == 1:
        return f"Tick {labels[0]} to finish. That probe was not detected."
    joined = " and ".join(labels)
    return f"Tick {joined} to finish. Those probes were not detected."


def wifi_qr_payload(ssid: str, password: str | None) -> str:
    """The WIFI: QR text a phone camera joins with.

    Backslash, semicolon, comma, colon, and quote are escaped. The payload
    ends with ``;;`` as the Wi-Fi QR spec requires. No password is an open
    network (``T:nopass``).
    """

    def esc(value: str) -> str:
        out: list[str] = []
        for ch in value:
            if ch in '\\;,:"':
                out.append("\\" + ch)
            else:
                out.append(ch)
        return "".join(out)

    if not password:
        return f"WIFI:T:nopass;S:{esc(ssid)};;"
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"
