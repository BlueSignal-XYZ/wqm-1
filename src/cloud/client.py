"""
HTTP cloud client — readings uplink, command polling, heartbeat, events,
and remote-config fetch over WiFi.

Talks to the BlueSignal Cloud Functions, authenticated with the device API key
(``X-API-Key``), which is bound to this device's id:

* ``ingestReading``  — POST an array of readings (store-and-forward batch).
* ``deviceCommands`` — POST ``{deviceId, action: "poll"}`` / ``"ack"``.
* ``{api_base}/v2/devices/{id}/heartbeat`` — POST periodic diagnostics.
* ``{api_base}/v2/devices/{id}/events``    — POST structured device events.
* ``{api_base}/v2/devices/{id}/config``    — GET desired remote config.

Uses only the Python standard library (urllib) so the firmware gains no new
dependency on the Pi. Network/transport errors are swallowed and logged — the
store-and-forward buffer keeps unsynced readings until a later cycle succeeds.

v2 sync semantics (fixes the v1.1.0 data-loss bug): the server's per-row
results (each carrying the batch ``index``) decide each row's fate — only
``status: "stored"`` rows are marked synced; per-row errors are marked
``failed_permanent`` (counted, retained, never retried forever). Rows older
than 24h are sent with ``metadata.backfill: true`` so the widened server
window accepts post-outage history.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("wqm1.cloud")

# DB column -> cloud sensor name. Cloud SENSOR_RANGES keys (functions/readings.js).
_SENSOR_MAP = {
    "ph": "ph",
    "tds_ppm": "tds",
    "turbidity_ntu": "turbidity",
    "orp_mv": "orp",
    "temp_c": "temperature",
    "chlorine_mgl": "chlorine",
    "conductivity_uscm": "conductivity",
    "salinity_ppt": "salinity",
    "battery_v": "battery_voltage",
}

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Rows older than this are flagged metadata.backfill (the server's live window
# is 24h; the flagged window is 30 days). Slightly under 24h so clock skew
# between device and server can't strand a row in between.
_BACKFILL_AGE_MS = 23 * 3600 * 1000


def _iso_to_ms(ts: str | None) -> int:
    """Convert a stored ISO-8601 'Z' timestamp to epoch milliseconds."""
    if not ts:
        return int(time.time() * 1000)
    try:
        dt = datetime.strptime(ts, _TS_FMT).replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return int(time.time() * 1000)


class CloudClient:
    """Stateless HTTP client to the BlueSignal cloud (one per device)."""

    def __init__(
        self,
        device_id: str,
        ingest_url: str,
        command_url: str,
        api_key: str,
        fw_version: str,
        api_base: str = "",
        batch_size: int = 50,
        timeout_s: float = 15.0,
        max_retries: int = 3,
        retry_delays: list[int] | tuple[int, ...] = (5, 15, 30),
        radios_provider: Callable[[], dict[str, Any] | None] | None = None,
        health_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._device_id = device_id
        self._ingest_url = ingest_url
        self._command_url = command_url
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._fw = fw_version
        self._batch_size = batch_size
        self._timeout_s = timeout_s
        self._max_retries = max(1, max_retries)
        self._retry_delays = list(retry_delays)
        # Optional callable returning current radio status (LoRa presence + GPS
        # fix) for the dashboard's Radios card. Read at sync time; never required.
        self._radios_provider = radios_provider
        # Optional callable returning {"batteryLevel": int|None,
        # "signalStrength": int|None} for per-reading metadata (HealthReporter
        # get_report) — v1 hardcoded these to null.
        self._health_provider = health_provider
        self._sleep = time.sleep  # patchable in tests
        self._now_ms: Callable[[], int] = lambda: int(time.time() * 1000)  # patchable

    def _device_url(self, suffix: str) -> str:
        return f"{self._api_base}/v2/devices/{self._device_id}/{suffix}"

    # -- reading -> cloud JSON ------------------------------------------------

    def reading_to_json(
        self,
        row: dict[str, Any],
        radios: dict[str, Any] | None = None,
        backfill: bool = False,
    ) -> dict[str, Any]:
        """Map one DB reading row to the cloud ingest schema."""
        sensors: dict[str, dict[str, float]] = {}
        for col, name in _SENSOR_MAP.items():
            val = row.get(col)
            if val is not None:
                sensors[name] = {"value": val}

        battery_level: int | None = None
        signal_strength: int | None = None
        if self._health_provider is not None:
            try:
                health = self._health_provider() or {}
                battery_level = health.get("batteryLevel")
                signal_strength = health.get("signalStrength")
            except Exception as e:  # noqa: BLE001 — health is best-effort metadata
                logger.debug("health_provider failed: %s", e)

        metadata: dict[str, Any] = {
            "firmware": self._fw,
            "batteryLevel": battery_level,
            "signalStrength": signal_strength,
            "relayState": row.get("relay_state", 0),
        }
        if backfill:
            metadata["backfill"] = True
        lat, lon = row.get("lat"), row.get("lon")
        if lat is not None and lon is not None:
            metadata["gps"] = {"latitude": lat, "longitude": lon, "altitude": row.get("alt_m")}

        # metadata.radios — the shape the cloud Radios card + map consume:
        #   {lora:{present,chip,mode,cs}, gps:{fix,sats,lat,lon}}.
        # LoRa presence + sat count come from the live `radios` snapshot; the
        # per-reading GPS fix is the row's own lat/lon (more accurate for that
        # sample). Only emitted when something is actually present.
        radio_meta: dict[str, Any] = {}
        if radios and isinstance(radios.get("lora"), dict):
            radio_meta["lora"] = radios["lora"]
        gps_meta: dict[str, Any] = dict((radios or {}).get("gps") or {})
        if lat is not None and lon is not None:
            gps_meta["lat"] = lat
            gps_meta["lon"] = lon
            gps_meta["fix"] = True
        if gps_meta.get("lat") is not None and gps_meta.get("lon") is not None:
            radio_meta["gps"] = gps_meta
        if radio_meta:
            metadata["radios"] = radio_meta

        return {
            "deviceId": self._device_id,
            "timestamp": _iso_to_ms(row.get("timestamp")),
            "sensors": sensors,
            "metadata": metadata,
        }

    # -- HTTP -----------------------------------------------------------------

    def _request(
        self, url: str, body: Any = None, method: str = "POST"
    ) -> tuple[int, dict[str, Any] | None]:
        """HTTP JSON request with retry. Returns (status_code, parsed_json|None)."""
        # Cloud endpoints are HTTP(S) URLs from config, never user input. Refuse
        # any other scheme so a mis-set config can't open a file:/ or custom URL.
        if not url.lower().startswith(("http://", "https://")):
            logger.error("Refusing non-HTTP(S) cloud URL: %s", url)
            return 0, None
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"X-API-Key": self._api_key}
        if data is not None:
            headers["Content-Type"] = "application/json"
        last_status = 0
        for attempt in range(self._max_retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                # B310: scheme validated above (HTTP/S only) — trusted cloud endpoint.
                with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:  # nosec B310
                    if resp.status == 204:
                        return 204, None
                    raw = resp.read().decode("utf-8") or "{}"
                    return resp.status, json.loads(raw)
            except urllib.error.HTTPError as e:
                last_status = e.code
                # 4xx (bad key / bad request) won't fix on retry — stop early.
                if 400 <= e.code < 500:
                    logger.warning("Cloud %s %s -> HTTP %d (no retry)", method, url, e.code)
                    return e.code, None
                logger.warning(
                    "Cloud %s %s -> HTTP %d (attempt %d)", method, url, e.code, attempt + 1
                )
            except (urllib.error.URLError, OSError, ValueError) as e:
                logger.warning("Cloud %s %s failed (attempt %d): %s", method, url, attempt + 1, e)
            if attempt < self._max_retries - 1:
                self._sleep(self._retry_delays[min(attempt, len(self._retry_delays) - 1)])
        return last_status, None

    def _post(self, url: str, body: Any) -> tuple[int, dict[str, Any] | None]:
        return self._request(url, body, "POST")

    # -- public API -----------------------------------------------------------

    def sync_readings(self, db: Any) -> int:
        """
        Drain pending readings and upload them as one batch.

        Per-row outcome handling (v2 server contract): each result entry
        carries the batch ``index`` — ``status: "stored"`` rows are marked
        synced, per-row errors are marked failed_permanent (no silent loss,
        no infinite retry). Legacy servers without per-row indices fall back
        to whole-batch mark-on-200. On transport failure nothing is marked
        and the buffer retries next cycle. Returns readings confirmed stored.
        """
        rows = db.get_unsynced(self._batch_size)
        if not rows:
            return 0
        radios: dict[str, Any] | None = None
        if self._radios_provider is not None:
            try:
                radios = self._radios_provider()
            except Exception as e:  # noqa: BLE001 — radio status is best-effort
                logger.debug("radios_provider failed: %s", e)

        now_ms = self._now_ms()
        payload = [
            self.reading_to_json(
                r, radios, backfill=(now_ms - _iso_to_ms(r.get("timestamp"))) > _BACKFILL_AGE_MS
            )
            for r in rows
        ]
        status, resp = self._post(self._ingest_url, payload)
        if status != 200:
            db.bump_sync_attempts([r["id"] for r in rows])
            return 0

        results = (resp or {}).get("results")
        # Inline guard (not a named boolean) so the type-checker narrows
        # `results` to a list for everything below the early return.
        if not (
            isinstance(results, list)
            and len(results) > 0
            and all(isinstance(e, dict) and isinstance(e.get("index"), int) for e in results)
        ):
            # Legacy server: keep the old (coarse) behavior rather than lose data.
            db.mark_synced([r["id"] for r in rows])
            return len(rows)

        stored_ids: list[int] = []
        failed_ids: list[int] = []
        for entry in results:
            idx = entry["index"]
            if not (0 <= idx < len(rows)):
                continue
            row_id = rows[idx]["id"]
            if entry.get("status") == "stored":
                stored_ids.append(row_id)
            elif entry.get("error"):
                failed_ids.append(row_id)
        # Rows the server didn't mention (shouldn't happen) stay pending.
        db.mark_synced(stored_ids)
        db.mark_failed_permanent(failed_ids)
        if failed_ids:
            logger.warning(
                "Cloud sync: %d row(s) permanently rejected: %s",
                len(failed_ids),
                "; ".join(
                    str(e.get("error"))
                    for e in results
                    if e.get("error") and isinstance(e.get("index"), int)
                )[:300],
            )
        if stored_ids:
            logger.info("Cloud sync: %d stored, %d rejected", len(stored_ids), len(failed_ids))
        return len(stored_ids)

    def poll(self) -> dict[str, Any]:
        """Fetch queued commands + control-plane hints for this device.

        Returns {commands: [...], relaySchedule, configVersion: int|None,
        otaPending: bool} — empty/default on any failure.
        """
        status, resp = self._post(
            self._command_url, {"deviceId": self._device_id, "action": "poll"}
        )
        if status == 200 and resp:
            cmds = resp.get("commands", [])
            return {
                "commands": cmds if isinstance(cmds, list) else [],
                "relaySchedule": resp.get("relaySchedule"),
                "configVersion": resp.get("configVersion"),
                "otaPending": bool(resp.get("otaPending")),
            }
        return {"commands": [], "relaySchedule": None, "configVersion": None, "otaPending": False}

    def poll_commands(self) -> list[dict[str, Any]]:
        """Back-compat shim: just the queued commands."""
        return self.poll()["commands"]

    def ack_command(self, command_id: str, status: str = "done", error: str | None = None) -> None:
        """Report a command result back to the cloud."""
        if not command_id:
            return
        body: dict[str, Any] = {
            "deviceId": self._device_id,
            "action": "ack",
            "commandId": command_id,
            "status": status,
        }
        if error:
            body["error"] = str(error)[:200]
        self._post(self._command_url, body)

    # -- v2 telemetry -----------------------------------------------------------

    def send_heartbeat(self, payload: dict[str, Any]) -> bool:
        """POST a heartbeat payload (built by HealthReporter.build_heartbeat)."""
        if not self._api_base:
            return False
        status, _ = self._post(self._device_url("heartbeat"), payload)
        return status == 200

    def send_event(
        self,
        event_type: str,
        message: str | None = None,
        sensor: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """POST a structured device event (allow-listed server-side)."""
        if not self._api_base:
            return False
        body: dict[str, Any] = {"type": event_type}
        if message:
            body["message"] = str(message)[:300]
        if sensor:
            body["sensor"] = sensor
        if details:
            body["details"] = details
        status, _ = self._post(self._device_url("events"), body)
        return status == 200

    def fetch_config(self) -> tuple[int | None, dict[str, Any], dict[str, Any] | None] | None:
        """GET the desired remote config plus the learned water profile.

        Returns (version, values, water_profile) — version is None when the
        server holds no config document but a profile exists (the profile
        rides the same endpoint), water_profile is None when the site has no
        learned baseline yet. Returns None on 204/failure.
        """
        if not self._api_base:
            return None
        status, resp = self._request(self._device_url("config"), method="GET")
        if status == 200 and resp:
            data = resp.get("data") or {}
            version = data.get("version")
            values = data.get("values") or {}
            profile = resp.get("waterProfile")
            if not isinstance(version, int):
                version = None
            if not isinstance(values, dict):
                values = {}
            if not isinstance(profile, dict):
                profile = None
            if version is None and profile is None:
                return None
            return version, values, profile
        return None

    def stop(self) -> None:
        """Symmetry with other subsystems (no persistent resources to release)."""
