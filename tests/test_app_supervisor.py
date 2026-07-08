"""Tests for src/app/supervisor.py + src/app/state.py — liveness-gated
watchdog petting, fault handling, restart requests."""

import threading


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeNotifier:
    def __init__(self):
        self.ready_calls = 0
        self.watchdog_calls = 0
        self.stopping_calls = 0

    def ready(self):
        self.ready_calls += 1

    def watchdog(self):
        self.watchdog_calls += 1

    def stopping(self):
        self.stopping_calls += 1


class IdleWorker:
    """Minimal Worker-compatible stub the supervisor can supervise."""

    def __init__(self, name, interval=10.0):
        self.name = name
        self._interval = interval
        self.consecutive_errors = 0

    def interval_s(self):
        return self._interval

    def step(self):
        pass

    def run_forever(self, stop_event, state, on_fault=None):
        while not stop_event.is_set():
            state.beat(self.name)
            stop_event.wait(0.01)


def make_supervisor(workers, clock, **kw):
    from app.state import StateStore
    from app.supervisor import Supervisor

    state = StateStore(clock=clock)
    sup = Supervisor(workers=workers, state=state, clock=clock, **kw)
    return sup, state


class TestLiveness:
    def test_pets_watchdog_when_all_workers_fresh(self, mock_hardware):
        clock = FakeClock()
        notifier = FakeNotifier()
        w = IdleWorker("sampling", interval=10.0)
        sup, state = make_supervisor([w], clock, notifier=notifier)
        state.beat("sampling")
        sup.tick()
        assert notifier.watchdog_calls == 1

    def test_starved_worker_stops_the_petting(self, mock_hardware):
        clock = FakeClock()
        notifier = FakeNotifier()
        w = IdleWorker("sampling", interval=10.0)
        sup, state = make_supervisor([w], clock, notifier=notifier)
        state.beat("sampling")
        # Deadline is max(3×interval, 60s) = 60s — advance past it.
        clock.advance(61.0)
        sup.tick()
        assert notifier.watchdog_calls == 0

    def test_slow_worker_gets_interval_scaled_deadline(self, mock_hardware):
        clock = FakeClock()
        notifier = FakeNotifier()
        w = IdleWorker("gps", interval=600.0)
        sup, state = make_supervisor([w], clock, notifier=notifier)
        state.beat("gps")
        clock.advance(1500.0)  # < 3×600 — still fine
        sup.tick()
        assert notifier.watchdog_calls == 1
        clock.advance(400.0)  # now 1900 > 1800 — overdue
        sup.tick()
        assert notifier.watchdog_calls == 1

    def test_hardware_watchdog_petted_alongside(self, mock_hardware):
        clock = FakeClock()

        class HW:
            pets = 0

            def pet(self):
                HW.pets += 1

            def close(self):
                pass

        w = IdleWorker("sampling")
        sup, state = make_supervisor([w], clock, hw_watchdog=HW())
        state.beat("sampling")
        sup.tick()
        assert HW.pets == 1


class TestRunLoop:
    def test_start_seeds_liveness_and_sends_ready(self, mock_hardware):
        clock = FakeClock()
        notifier = FakeNotifier()
        w = IdleWorker("sampling")
        sup, state = make_supervisor([w], clock, notifier=notifier)
        sup.start()
        try:
            assert notifier.ready_calls == 1
            assert "sampling" in state.beats()
        finally:
            sup.stop(join_timeout_s=1.0)

    def test_restart_request_exits_run_loop(self, mock_hardware):
        clock = FakeClock()
        w = IdleWorker("sampling")
        sup, state = make_supervisor([w], clock, tick_s=0.01)
        sup.start()
        state.request_restart()
        t = threading.Thread(target=sup.run)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert sup.exit_reason == "restart"
        sup.stop(join_timeout_s=1.0)

    def test_db_rotate_called_on_schedule(self, mock_hardware):
        clock = FakeClock()

        class DB:
            rotations = 0

            def rotate(self):
                DB.rotations += 1

        sup, state = make_supervisor([], clock, db=DB())
        sup.tick()
        assert DB.rotations == 0  # not yet due
        clock.advance(601.0)
        sup.tick()
        assert DB.rotations == 1


class TestWorkerRunner:
    def test_step_exception_bumps_error_and_thread_survives(self, mock_hardware):
        from app.state import StateStore
        from app.workers import Worker

        clock = FakeClock()

        class Flaky(Worker):
            name = "flaky"
            error_bucket = "cloud"

            def __init__(self):
                super().__init__(clock)
                self.calls = 0

            def interval_s(self):
                return 0.0

            def step(self):
                self.calls += 1
                if self.calls <= 2:
                    raise RuntimeError("boom")

        state = StateStore(clock=clock)
        stop = threading.Event()
        w = Flaky()

        # Run enough iterations then stop from a timer.
        def stopper():
            while w.calls < 4:
                pass
            stop.set()

        t = threading.Thread(target=stopper, daemon=True)
        t.start()
        # Neutralize backoff waits: stop_event.wait is real but backoff uses
        # max(interval, backoff) — patch the backoff table via error reset.
        import app.workers as workers_mod

        orig = workers_mod._BACKOFF_S
        workers_mod._BACKOFF_S = (0, 0, 0, 0)
        try:
            w.run_forever(stop, state)
        finally:
            workers_mod._BACKOFF_S = orig

        assert w.calls >= 4
        assert state.error_counts()["cloud"] == 2
        assert "flaky" in state.beats()

    def test_fault_callback_after_streak(self, mock_hardware):
        from app.state import StateStore
        from app.workers import FAULT_STREAK, Worker

        clock = FakeClock()

        class Dead(Worker):
            name = "dead"
            error_bucket = "gps"

            def __init__(self):
                super().__init__(clock)
                self.calls = 0

            def interval_s(self):
                return 0.0

            def step(self):
                self.calls += 1
                raise RuntimeError("always")

        state = StateStore(clock=clock)
        stop = threading.Event()
        faults = []
        w = Dead()

        def stopper():
            while w.calls < FAULT_STREAK + 1:
                pass
            stop.set()

        threading.Thread(target=stopper, daemon=True).start()
        import app.workers as workers_mod

        orig = workers_mod._BACKOFF_S
        workers_mod._BACKOFF_S = (0, 0, 0, 0)
        try:
            w.run_forever(stop, state, on_fault=faults.append)
        finally:
            workers_mod._BACKOFF_S = orig
        assert "dead" in faults


class TestStateStore:
    def test_event_queue_bounded_and_non_blocking(self, mock_hardware):
        from app.state import StateStore

        state = StateStore()
        for i in range(150):
            state.emit_event({"type": "anomaly_spike", "n": i})
        # Queue caps at 100; extras are dropped, not blocking.
        drained = []
        while True:
            batch = state.drain_events(limit=50)
            if not batch:
                break
            drained.extend(batch)
        assert len(drained) == 100

    def test_gps_snapshot_roundtrip(self, mock_hardware):
        from app.state import StateStore

        state = StateStore()
        state.set_gps(37.1, -78.2, 120.0, 9)
        fix = state.gps()
        assert (fix.lat, fix.lon, fix.alt_m, fix.sats) == (37.1, -78.2, 120.0, 9)
