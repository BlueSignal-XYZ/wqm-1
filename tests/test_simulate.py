"""
The virtual WQM-1 (sensors/sim) — PR 7 of the commissioning plan.

What these pin, in order of how much it would cost to lose:

1. A virtual unit is STRUCTURALLY unable to reach production: any cloud URL
   whose host is not loopback is refused at construction, and there is no
   override flag anywhere in the package.
2. Every simulated serial is on the reserved SIM-WQM1- prefix, which is
   neither a label nor a Pi-derived id.
3. The simulator carries no payload of its own: the rows it stores come out
   of the real SamplingWorker and the JSON out of the real CloudClient.
4. Faults are scripted per channel and arrive at the cloud as the status the
   hardware would send (no_conduction, a totalizer reset, a clock jump).
5. N units on one host have N distinct identities.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _settings(**over):
    from utils.config import Settings

    s = Settings()
    s.simulate_enabled = True
    s.flow_pulse_enabled = True
    s.sensor_read_s = 60
    for k, v in over.items():
        setattr(s, k, v)
    return s


class TestEndpointGuard:
    def test_loopback_hosts_accepted(self):
        from sensors.sim.unit import assert_emulator_endpoint

        for url in (
            "http://localhost:5001/waterquality-trading/us-central1/app",
            "http://127.0.0.1:9000/x",
            "http://[::1]:5001/x",
        ):
            assert assert_emulator_endpoint(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "https://us-central1-waterquality-trading.cloudfunctions.net/app",
            "https://cloud.bluesignal.xyz/api",
            "http://10.0.0.5:5001/app",
            "http://localhost.evil.example/app",
            "",
        ],
    )
    def test_anything_else_is_refused(self, url):
        from sensors.sim.unit import NotAnEmulator, assert_emulator_endpoint

        with pytest.raises(NotAnEmulator):
            assert_emulator_endpoint(url)

    def test_virtual_unit_refuses_a_production_endpoint_at_construction(self, tmp_path):
        from sensors.sim.unit import NotAnEmulator, VirtualUnit

        with pytest.raises(NotAnEmulator):
            VirtualUnit(
                1,
                db_path=str(tmp_path / "u.db"),
                cloud_api_base="https://us-central1-waterquality-trading.cloudfunctions.net/app",
                ingest_url="http://localhost:5001/ingest",
                api_key="k" * 32,
                settings=_settings(),
            )
        with pytest.raises(NotAnEmulator):
            VirtualUnit(
                1,
                db_path=str(tmp_path / "u.db"),
                cloud_api_base="http://localhost:5001/app",
                ingest_url="https://us-central1-waterquality-trading.cloudfunctions.net/ingestReading",
                api_key="k" * 32,
                settings=_settings(),
            )

    def test_no_override_exists(self):
        """No keyword, env var or flag relaxes the guard — grep the package."""
        import sensors.sim.unit as unit

        src = Path(unit.__file__).read_text()
        for banned in ("allow_production", "ALLOW_PRODUCTION", "--force", "unsafe", "skip_guard"):
            assert banned not in src
        script = Path(__file__).parent.parent / "scripts" / "simulate-fleet.py"
        text = script.read_text()
        for banned in ("allow_production", "ALLOW_PRODUCTION", "--force", "--unsafe", "--i-know"):
            assert banned not in text


class TestSerials:
    def test_reserved_prefix_is_neither_label_nor_derived(self):
        from sensors.sim.unit import sim_serial
        from utils.identity import LABEL_RE, is_simulated_serial

        s = sim_serial(7)
        assert s == "SIM-WQM1-00007"
        assert is_simulated_serial(s)
        assert not LABEL_RE.match(s)
        assert not s.startswith("BS-WQM1-")
        assert not s.startswith("WQM-")

    def test_dev_eui_is_in_the_ieee_test_range_and_unique(self):
        from sensors.sim.unit import sim_dev_eui

        euis = {sim_dev_eui(i) for i in range(1, 101)}
        assert len(euis) == 100
        assert all(e.startswith("FEFFFF") and len(e) == 16 for e in euis)

    def test_identity_file_boots_a_unit_under_its_sim_serial(self, tmp_path, monkeypatch):
        from sensors.sim.unit import sim_identity
        from utils.identity import IDENTITY_FILE_ENV, get_dev_eui, get_device_id

        f = tmp_path / "bluesignal-identity.json"
        f.write_text(json.dumps(sim_identity(3)))
        monkeypatch.setenv(IDENTITY_FILE_ENV, str(f))
        assert get_device_id() == "SIM-WQM1-00003"
        assert get_dev_eui().hex().upper() == "FEFFFF0000000003"


class TestFaults:
    def test_parse(self):
        from sensors.sim import parse_faults

        faults = parse_faults("tds:no_conduction@40, ph:flatline ,flow:reset@100,clock:jump@7")
        assert [(f.channel, f.kind, f.at_cycle) for f in faults] == [
            ("tds", "no_conduction", 40),
            ("ph", "flatline", 0),
            ("flow", "reset", 100),
            ("clock", "jump", 7),
        ]

    def test_typo_fails_loudly(self):
        from sensors.sim import parse_faults

        with pytest.raises(ValueError):
            parse_faults("tsd:no_conduction")
        with pytest.raises(ValueError):
            parse_faults("tds:noconduction")

    def test_no_conduction_arrives_as_status_not_omission(self, tmp_path):
        """Cycle 3 onward the TDS probe is out of the water: the ROW stores the
        status and the PAYLOAD carries {value: null, status: no_conduction} —
        the exact contract the cloud consumes (docs/cloud-payload.md)."""
        from sensors.sim.unit import VirtualUnit

        u = VirtualUnit(
            1,
            db_path=str(tmp_path / "u.db"),
            cloud_api_base="http://localhost:5001/app",
            ingest_url="http://localhost:5001/ingest",
            api_key="k" * 32,
            settings=_settings(),
            faults="tds:no_conduction@3",
        )
        u.sample(4)
        rows = u.db.get_unsynced(10)
        assert len(rows) == 4
        assert rows[1]["tds_ppm"] is not None
        assert rows[2]["tds_ppm"] is None
        assert json.loads(rows[2]["sensor_status"]) == {"tds": "no_conduction"}
        payload = u.cloud.reading_to_json(rows[3])
        assert payload["deviceId"] == "SIM-WQM1-00001"
        assert payload["sensors"]["tds"] == {"value": None, "status": "no_conduction"}
        assert payload["sensors"]["ph"]["value"] is not None
        assert "flow_total_gal" in payload["sensors"]
        u.close()

    def test_totalizer_resets_and_clock_jumps_are_visible_in_the_rows(self, tmp_path):
        from sensors.sim.unit import VirtualUnit

        start = datetime(2026, 9, 1, tzinfo=UTC)
        u = VirtualUnit(
            2,
            db_path=str(tmp_path / "u.db"),
            cloud_api_base="http://localhost:5001/app",
            ingest_url="http://localhost:5001/ingest",
            api_key="k" * 32,
            settings=_settings(),
            faults="flow:reset@5,clock:jump@8",
            start=start,
        )
        u.sample(10)
        rows = u.db.get_unsynced(20)
        totals = [r["flow_total_gal"] for r in rows]
        # Monotonic up to the reset, then it restarts from (near) zero.
        assert totals[3] > totals[0]
        assert totals[4] < totals[3]
        assert all(b >= a for a, b in zip(totals[4:], totals[5:], strict=False))
        ts = [r["timestamp"] for r in rows]
        assert ts[0] == "2026-09-01T00:01:00Z"
        assert ts[6] == "2026-09-01T00:07:00Z"
        assert ts[7] == "2026-09-02T00:08:00Z"  # the jump: one day forward at cycle 8
        assert u.clock.jumped_at == [8]
        u.close()

    def test_flatline_freezes_the_walk(self, tmp_path):
        from sensors.sim.unit import VirtualUnit

        u = VirtualUnit(
            1,
            db_path=str(tmp_path / "u.db"),
            cloud_api_base="http://localhost:5001/app",
            ingest_url="http://localhost:5001/ingest",
            api_key="k" * 32,
            settings=_settings(),
            faults="ph:flatline@2",
        )
        u.sample(6)
        rows = u.db.get_unsynced(10)
        assert len({r["ph"] for r in rows[1:]}) == 1
        u.close()


class TestNoOwnPayload:
    def test_simulator_builds_no_reading_or_json(self):
        """The rule of Track B: the package may not carry its own copy of the
        reading schema or the payload shape."""
        import sensors.sim as pkg
        import sensors.sim.unit as unit

        for mod in (pkg, unit):
            src = Path(mod.__file__).read_text()
            assert '"deviceId"' not in src
            assert "insert_reading(" not in src
            assert '"tds_ppm"' not in src
            assert "json.dumps" not in src
        # And it drives the real ones:
        src = Path(unit.__file__).read_text()
        assert "from app.workers import SamplingWorker" in src
        assert "from cloud import CloudClient" in src
        assert "from storage.database import WQM1Database" in src

    def test_sampling_uses_the_real_worker(self, tmp_path):
        from app.workers import SamplingWorker
        from sensors.sim.unit import VirtualUnit

        u = VirtualUnit(
            1,
            db_path=str(tmp_path / "u.db"),
            cloud_api_base="http://localhost:5001/app",
            ingest_url="http://localhost:5001/ingest",
            api_key="k" * 32,
            settings=_settings(),
        )
        assert isinstance(u.sampler, SamplingWorker)
        u.close()

    def test_sync_goes_through_the_real_client_and_buffers_on_failure(self, tmp_path, monkeypatch):
        """No emulator here, so the POST fails: nothing is marked synced and
        the pending depth is the sample count — store-and-forward, unchanged."""
        from sensors.sim.unit import VirtualUnit

        u = VirtualUnit(
            1,
            db_path=str(tmp_path / "u.db"),
            cloud_api_base="http://localhost:1/app",
            ingest_url="http://localhost:1/ingest",
            api_key="k" * 32,
            settings=_settings(),
        )
        u.sample(5)
        assert u.sync() == 0
        assert u.pending() == 5
        hb = u.health.build_heartbeat(db=u.db)
        assert hb["bufferDepth"] == 5
        u.close()


class TestFitment:
    def test_unfitted_channels_are_not_simulated(self, tmp_path):
        from sensors.sim import build_simulated_sensors

        sim = build_simulated_sensors(_settings(tds_enabled=False, flow_pulse_enabled=False))
        assert sim.tds is None
        assert sim.flow is None
        assert sim.ph is not None

    def test_gps_lost_fault(self):
        from sensors.sim import build_simulated_sensors, parse_faults

        sim = build_simulated_sensors(_settings(), parse_faults("gps:lost@2"))
        assert sim.gps.get_fix() is not None
        sim.cycle.tick()
        sim.cycle.tick()
        assert sim.gps.get_fix() is None


class TestManyUnits:
    def test_ten_units_have_ten_distinct_identities(self, tmp_path):
        from sensors.sim.unit import VirtualUnit

        units = [
            VirtualUnit(
                i,
                db_path=str(tmp_path / f"u{i}.db"),
                cloud_api_base="http://localhost:5001/app",
                ingest_url="http://localhost:5001/ingest",
                api_key="k" * 32,
                settings=_settings(simulate_seed=i),
            )
            for i in range(1, 11)
        ]
        assert len({u.serial for u in units}) == 10
        assert len({u.dev_eui for u in units}) == 10
        for u in units:
            u.sample(2)
            assert u.db.get_unsynced(5)[0]["ph"] is not None
            u.close()

    def test_full_tier_unit_files_are_self_contained(self, tmp_path):
        """The full tier writes one identity file + one config per unit, every
        path inside the unit's own directory, cmd_sock included."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "simulate_fleet", Path(__file__).parent.parent / "scripts" / "simulate-fleet.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import yaml

        args = mod._args(
            [
                "--count",
                "2",
                "--cloud-api-base",
                "http://localhost:5001/app",
                "--ingest-url",
                "http://localhost:5001/ingest",
            ]
        )
        files = mod._unit_files(tmp_path, 2, args, "k" * 32, 8102)
        ident = json.loads(files["identity"].read_text())
        assert ident["serial"] == "SIM-WQM1-00002"
        cfg = yaml.safe_load(files["config"].read_text())
        assert cfg["simulate_enabled"] is True
        assert cfg["board"] == "generic-linux"
        assert cfg["cmd_sock"] == cfg["service_window"]["cmd_sock"]
        assert cfg["cmd_sock"].startswith(str(files["dir"]))
        assert cfg["service_window"]["port"] == 8102

    def test_script_refuses_production_before_doing_anything(self, tmp_path, capsys):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "simulate_fleet2", Path(__file__).parent.parent / "scripts" / "simulate-fleet.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rc = mod.main(
            [
                "--count",
                "1",
                "--cloud-api-base",
                "https://us-central1-waterquality-trading.cloudfunctions.net/app",
                "--ingest-url",
                "http://localhost:5001/ingest",
                "--workdir",
                str(tmp_path),
            ]
        )
        assert rc == 2
        assert "REFUSED" in capsys.readouterr().err
        assert not any(tmp_path.iterdir())
