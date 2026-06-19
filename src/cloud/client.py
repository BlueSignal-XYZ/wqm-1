"""
HTTP cloud client — readings uplink + command polling over WiFi.

Talks to two BlueSignal Cloud Functions, authenticated with the device API key
(``X-API-Key``), which is bound to this device's id:

* ``ingestReading``  — POST an array of readings (store-and-forward batch).
* ``deviceCommands`` — POST ``{deviceId, action: "poll"}`` to fetch queued relay
  commands, and ``{deviceId, action: "ack", commandId, status}`` to report results.

Uses only the Python standard library (urllib) so the firmware gains no new
dependency on the Pi. Network/transport errors are swallowed and logged — the
store-and-forward buffer keeps unsynced readings until a later cycle succeeds.
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
    "battery_v": "battery_voltage",
}

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


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
        fw_version: str = "1.0.0",
        batch_size: int = 50,
        timeout_s: float = 15.0,
        max_retries: int = 3,
        retry_delays: list[int] | tuple[int, ...] = (5, 15, 30),
        radios_provider: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self._device_id = device_id
        self._ingest_url = ingest_url
        self._command_url = command_url
        self._api_key = api_key
        self._fw = fw_version
        self._batch_size = batch_size
        self._timeout_s = timeout_s
        self._max_retries = max(1, max_retries)
        self._retry_delays = list(retry_delays)
        # Optional callable returning current radio status (LoRa presence + GPS
        # fix) for the dashboard's Radios card. Read at sync time; never required.
        self._radios_provider = radios_provider
        self._sleep = time.sleep  # patchable in tests

    # -- reading -> cloud JSON ------------------------------------------------

    def reading_to_json(
        self, row: dict[str, Any], radios: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Map one DB reading row to the cloud ingest schema."""
        sensors: dict[str, dict[str, float]] = {}
        for col, name in _SENSOR_MAP.items():
            val = row.get(col)
            if val is not None:
                sensors[name] = {"value": val}

        metadata: dict[str, Any] = {
            "firmware": self._fw,
            "batteryLevel": None,
            "signalStrength": None,
            "relayState": row.get("relay_state", 0),
        }
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

    def _post(self, url: str, body: Any) -> tuple[int, dict[str, Any] | None]:
        """POST JSON with retry. Returns (status_code, parsed_json|None)."""
        data = json.dumps(body).encode("utf-8")
        last_status = 0
        for attempt in range(self._max_retries):
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json", "X-API-Key": self._api_key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                    raw = resp.read().decode("utf-8") or "{}"
                    return resp.status, json.loads(raw)
            except urllib.error.HTTPError as e:
                last_status = e.code
                # 4xx (bad key / bad request) won't fix on retry — stop early.
                if 400 <= e.code < 500:
                    logger.warning("Cloud POST %s -> HTTP %d (no retry)", url, e.code)
                    return e.code, None
                logger.warning("Cloud POST %s -> HTTP %d (attempt %d)", url, e.code, attempt + 1)
            except (urllib.error.URLError, OSError, ValueError) as e:
                logger.warning("Cloud POST %s failed (attempt %d): %s", url, attempt + 1, e)
            if attempt < self._max_retries - 1:
                self._sleep(self._retry_delays[min(attempt, len(self._retry_delays) - 1)])
        return last_status, None

    # -- public API -----------------------------------------------------------

    def sync_readings(self, db: Any) -> int:
        """
        Drain unsynced readings and upload them as one batch.

        On a 200 response the whole batch is marked synced (the cloud dedups by
        timestamp and rejects only stale/invalid rows, which would never become
        valid on retry). On transport failure nothing is marked, so the buffer
        is retried next cycle. Returns the number of readings uploaded.
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
        payload = [self.reading_to_json(r, radios) for r in rows]
        status, resp = self._post(self._ingest_url, payload)
        if status == 200:
            db.mark_synced([r["id"] for r in rows])
            if resp:
                logger.info(
                    "Cloud sync: %s stored, %s failed",
                    resp.get("processed"),
                    resp.get("failed"),
                )
            return len(rows)
        return 0

    def poll_commands(self) -> list[dict[str, Any]]:
        """Fetch queued commands for this device (marks them delivered cloud-side)."""
        status, resp = self._post(
            self._command_url, {"deviceId": self._device_id, "action": "poll"}
        )
        if status == 200 and resp:
            cmds = resp.get("commands", [])
            return cmds if isinstance(cmds, list) else []
        return []

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

    def stop(self) -> None:
        """Symmetry with other subsystems (no persistent resources to release)."""
