"""Tests for core/background.py: leased single loop, intervals, thread lifecycle."""

from __future__ import annotations

import contextvars
import logging
import threading

import pytest

from cloud_compute_core import background
from cloud_compute_core.background import BackgroundLoop
from cloud_compute_core.interfaces import ApiError, BackgroundTask, Services
from cloud_compute_core.store import Store

WAIT_S = 3.0   # upper bound for waiting on a thread; passing tests return much sooner


class Recorder:
    """Builds BackgroundTasks that record their runs."""

    def __init__(self):
        self.runs: list[str] = []

    def task(self, name, interval=10, fail=False, event=None):
        def run(services):
            self.runs.append(name)
            if event is not None:
                event.set()
            if fail:
                raise RuntimeError(f"{name} boom")

        return BackgroundTask(name=name, interval_s=lambda s: interval, run=run)


def make_services(store):
    return Services(store=store)


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture(autouse=True)
def _reset_singleton():
    background.stop()
    background._reset_ensure_for_tests()
    yield
    background.stop()
    background._reset_ensure_for_tests()
    leaked = [t for t in threading.enumerate() if t.name == "cloud-compute-bg"]
    assert not leaked, f"leaked background threads: {leaked}"


def test_first_tick_runs_every_task_then_only_due_ones(services, rec):
    tasks = [rec.task("fast", 10), rec.task("slow", 60)]
    loop = BackgroundLoop(services, tasks, owner="a")
    assert loop.run_once(now=1000.0) == ["fast", "slow"]
    assert loop.run_once(now=1005.0) == []
    assert loop.run_once(now=1010.0) == ["fast"]
    assert loop.run_once(now=1059.9) == ["fast"]
    assert loop.run_once(now=1070.0) == ["fast", "slow"]
    assert rec.runs == ["fast", "slow", "fast", "fast", "fast", "slow"]
    assert loop.last_runs == {"fast": 1070.0, "slow": 1070.0}


def test_uses_injected_clock_when_now_omitted(services, rec):
    t = [500.0]
    loop = BackgroundLoop(services, [rec.task("x", 10)], owner="a", clock=lambda: t[0])
    assert loop.run_once() == ["x"]
    t[0] = 505.0
    assert loop.run_once() == []
    t[0] = 510.0
    assert loop.run_once() == ["x"]


def test_failing_task_is_logged_others_run_and_it_is_rescheduled(services, rec, caplog):
    tasks = [rec.task("bad", 10, fail=True), rec.task("good", 10)]
    loop = BackgroundLoop(services, tasks, owner="a")
    with caplog.at_level(logging.WARNING, logger=background.log.name):
        assert loop.run_once(now=0.0) == ["bad", "good"]
    assert any("bad" in r.getMessage() and "boom" in r.getMessage() for r in caplog.records)
    assert "RuntimeError: bad boom" in loop.last_errors["bad"]
    assert "good" not in loop.last_errors
    # Failure does not make it retry sooner than its interval.
    assert loop.run_once(now=5.0) == []
    assert loop.run_once(now=10.0) == ["bad", "good"]


def test_error_cleared_after_success(services):
    state = {"fail": True}

    def run(s):
        if state["fail"]:
            raise ValueError("nope")

    loop = BackgroundLoop(services, [BackgroundTask("t", lambda s: 1, run)], owner="a")
    loop.run_once(now=0.0)
    assert "t" in loop.last_errors
    state["fail"] = False
    loop.run_once(now=2.0)
    assert "t" not in loop.last_errors


def test_interval_exception_is_logged_and_does_not_escape(services, rec, caplog):
    def bad_interval(s):
        raise RuntimeError("interval broke")

    tasks = [BackgroundTask("broken", bad_interval, lambda s: rec.runs.append("broken")),
             rec.task("ok", 10)]
    loop = BackgroundLoop(services, tasks, owner="a")
    with caplog.at_level(logging.WARNING, logger=background.log.name):
        assert loop.run_once(now=0.0) == ["ok"]
        assert loop.run_once(now=1.0) == []     # same problem again: not logged twice
    msgs = [r.getMessage() for r in caplog.records if "broken" in r.getMessage()]
    assert len(msgs) == 1 and "interval broke" in msgs[0]
    assert "interval broke" in loop.last_errors["broken"]
    assert "broken" not in rec.runs


@pytest.mark.parametrize("bad", ["soon", None, float("nan"), True, [5]])
def test_non_numeric_interval_skips_task(services, rec, bad):
    tasks = [BackgroundTask("weird", lambda s: bad, lambda s: rec.runs.append("weird")),
             rec.task("ok", 10)]
    loop = BackgroundLoop(services, tasks, owner="a")
    assert loop.run_once(now=0.0) == ["ok"]
    assert "weird" in loop.last_errors


@pytest.mark.parametrize("small", [0, 0.01, -5])
def test_interval_clamped_to_at_least_one_second(services, rec, small):
    loop = BackgroundLoop(services, [rec.task("x", small)], owner="a")
    assert loop.run_once(now=100.0) == ["x"]
    assert loop.run_once(now=100.5) == []
    assert loop.run_once(now=101.0) == ["x"]


def test_interval_is_read_each_tick(services):
    interval = [100]
    runs = []
    task = BackgroundTask("dyn", lambda s: interval[0], lambda s: runs.append(1))
    loop = BackgroundLoop(services, [task], owner="a")
    assert loop.run_once(now=0.0) == ["dyn"]
    assert loop.run_once(now=50.0) == []
    interval[0] = 10
    assert loop.run_once(now=50.0) == ["dyn"]


def test_default_owner_and_ttl(services):
    loop = BackgroundLoop(services, [], tick_s=4.0)
    host_pid = loop.owner.split(":")
    assert len(host_pid) >= 3 and len(host_pid[-1]) == 8
    assert loop.lease_ttl_s == 12.0
    assert BackgroundLoop(services, [], owner="x").owner == "x"
    assert BackgroundLoop(services, []).owner != BackgroundLoop(services, []).owner


def test_default_tasks_come_from_registry(services, monkeypatch, rec):
    from cloud_compute_core import registry

    monkeypatch.setattr(registry, "collect_background", lambda: [rec.task("reg", 10)])
    loop = BackgroundLoop(services, owner="a")
    assert loop.run_once(now=0.0) == ["reg"]


def test_run_once_takes_lease_and_renews_before_each_task(services, rec):
    calls = []
    store = services.store
    real = store.acquire_lease

    def spy(name, owner, ttl):
        calls.append((name, owner, ttl))
        return real(name, owner, ttl)

    store.acquire_lease = spy
    loop = BackgroundLoop(services, [rec.task("a", 10), rec.task("b", 10)], owner="o", tick_s=2.0)
    loop.run_once(now=0.0)
    assert calls == [("bg", "o", 6.0)] * 3        # tick + once per task
    assert loop.holds_lease


def test_lease_lost_mid_tick_stops_remaining_tasks(services):
    ran = []

    def steal(s):
        ran.append("first")
        # Another owner takes over while the first task runs (lease forcibly replaced).
        with s.store._tx(immediate=True) as conn:
            conn.execute("UPDATE leases SET owner='other', expires_at=1e18 WHERE name='bg'")

    tasks = [BackgroundTask("first", lambda s: 1, steal),
             BackgroundTask("second", lambda s: 1, lambda s: ran.append("second"))]
    loop = BackgroundLoop(services, tasks, owner="me")
    assert loop.run_once(now=0.0) == ["first"]
    assert ran == ["first"]
    assert not loop.holds_lease


def test_two_loops_sharing_a_db_only_one_runs(tmp_path, rec):
    path = tmp_path / "shared.db"
    s1, s2 = Store(path), Store(path)
    try:
        a = BackgroundLoop(make_services(s1), [rec.task("t", 1)], owner="a")
        b = BackgroundLoop(make_services(s2), [rec.task("u", 1)], owner="b")
        assert a.run_once(now=0.0) == ["t"]
        assert b.run_once(now=0.0) == []
        assert a.holds_lease and not b.holds_lease
        assert a.run_once(now=5.0) == ["t"]
        assert b.run_once(now=5.0) == []
        assert rec.runs == ["t", "t"]
    finally:
        s1.close()
        s2.close()


def test_other_takes_over_after_holder_releases(tmp_path, rec):
    path = tmp_path / "shared.db"
    s1, s2 = Store(path), Store(path)
    try:
        a = BackgroundLoop(make_services(s1), [rec.task("t", 1)], owner="a")
        b = BackgroundLoop(make_services(s2), [rec.task("u", 1)], owner="b")
        assert a.run_once(now=0.0) == ["t"]
        assert b.run_once(now=0.0) == []
        a.stop()
        assert not a.holds_lease
        assert b.run_once(now=1.0) == ["u"]
        assert b.holds_lease
    finally:
        s1.close()
        s2.close()


def test_other_takes_over_after_lease_expiry(tmp_path, rec):
    path = tmp_path / "shared.db"
    wall = [1000.0]
    s1 = Store(path, clock=lambda: wall[0])
    s2 = Store(path, clock=lambda: wall[0])
    try:
        a = BackgroundLoop(make_services(s1), [rec.task("t", 1)], owner="a", tick_s=5.0)  # ttl 15
        b = BackgroundLoop(make_services(s2), [rec.task("u", 1)], owner="b", tick_s=5.0)
        assert a.run_once(now=0.0) == ["t"]
        wall[0] = 1010.0
        assert b.run_once(now=10.0) == []             # a's lease still valid
        assert a.run_once(now=10.0) == ["t"]          # renewed to 1025
        wall[0] = 1030.0                              # a stopped ticking; lease expired
        assert b.run_once(now=30.0) == ["u"]
        assert a.run_once(now=30.0) == []             # b holds it now
        assert b.holds_lease and not a.holds_lease
    finally:
        s1.close()
        s2.close()


def test_store_unavailable_does_nothing_and_logs_once(rec, caplog):
    services = Services()                              # no store provider -> ApiError(503)
    with pytest.raises(ApiError):
        services.store
    loop = BackgroundLoop(services, [rec.task("t", 1)], owner="a")
    with caplog.at_level(logging.WARNING, logger=background.log.name):
        assert loop.run_once(now=0.0) == []
        assert loop.run_once(now=5.0) == []
    assert rec.runs == []
    assert not loop.holds_lease
    assert len([r for r in caplog.records if "store unavailable" in r.getMessage()]) == 1


def test_store_that_raises_does_not_crash_tick(rec):
    class Broken:
        def acquire_lease(self, *a):
            raise RuntimeError("disk gone")

        def release_lease(self, *a):
            raise RuntimeError("disk gone")

    loop = BackgroundLoop(Services(store=Broken()), [rec.task("t", 1)], owner="a")
    assert loop.run_once(now=0.0) == []
    loop.stop()                                        # release failure is swallowed


def test_thread_lifecycle_runs_and_stops_promptly(services, rec):
    ran = threading.Event()
    loop = BackgroundLoop(services, [rec.task("t", 1, event=ran)], owner="a", tick_s=0.01)
    assert not loop.is_running
    assert loop.start() is loop
    try:
        assert ran.wait(WAIT_S)
        assert loop.is_running and loop.holds_lease
        thread = next(t for t in threading.enumerate() if t.name == "cloud-compute-bg")
        assert thread.daemon
        assert loop.start() is loop                    # idempotent: no second thread
        assert sum(t.name == "cloud-compute-bg" for t in threading.enumerate()) == 1
    finally:
        loop.stop(timeout=WAIT_S)
    assert not loop.is_running
    assert not loop.holds_lease
    assert not thread.is_alive()
    assert services.store.acquire_lease("bg", "someone-else", 10)   # lease was released


def test_stop_wakes_a_long_tick_immediately(services, rec):
    ran = threading.Event()
    loop = BackgroundLoop(services, [rec.task("t", 1, event=ran)], owner="a", tick_s=60.0)
    loop.start()
    assert ran.wait(WAIT_S)
    loop.stop(timeout=WAIT_S)                          # would hang ~60 s if it slept
    assert not loop.is_running


def test_stop_without_start_and_restart(services, rec):
    loop = BackgroundLoop(services, [rec.task("t", 1)], owner="a", tick_s=0.01)
    loop.stop()
    ran = threading.Event()
    loop2 = BackgroundLoop(services, [rec.task("t2", 1, event=ran)], owner="a", tick_s=0.01,
                           clock=iter_clock())
    loop2.start()
    assert ran.wait(WAIT_S)
    loop2.stop(timeout=WAIT_S)
    ran.clear()
    loop2.start()                                      # can be started again after stop
    assert ran.wait(WAIT_S)
    loop2.stop(timeout=WAIT_S)


def test_contextvar_set_before_start_is_visible_in_task(services):
    var = contextvars.ContextVar("profile_scope", default="unset")
    seen = []
    done = threading.Event()

    def run(s):
        seen.append(var.get())
        done.set()

    loop = BackgroundLoop(services, [BackgroundTask("ctx", lambda s: 1, run)], owner="a",
                          tick_s=0.01)
    token = var.set("profile-a")
    try:
        loop.start()
    finally:
        var.reset(token)
    try:
        assert done.wait(WAIT_S)
    finally:
        loop.stop(timeout=WAIT_S)
    assert seen[0] == "profile-a"


def test_thread_survives_task_failure(services, rec):
    calls = []
    two = threading.Event()

    def run(s):
        calls.append(1)
        if len(calls) >= 2:
            two.set()
        raise RuntimeError("always")

    loop = BackgroundLoop(services, [BackgroundTask("f", lambda s: 1, run)], owner="a",
                          tick_s=0.01, clock=iter_clock())
    loop.start()
    try:
        assert two.wait(WAIT_S)
        assert loop.is_running
    finally:
        loop.stop(timeout=WAIT_S)


def iter_clock():
    """A clock that advances 1 s per call so a 1 s interval is due on every tick."""
    state = {"t": 0.0}
    lock = threading.Lock()

    def clock():
        with lock:
            state["t"] += 1.0
            return state["t"]

    return clock


# ---------------------------------------------------------------- module-level helpers


def test_module_start_is_idempotent_and_stop_clears(services, rec):
    assert background.current() is None
    ran = threading.Event()
    first = background.start(services, tasks=[rec.task("t", 1, event=ran)], tick_s=0.01)
    try:
        second = background.start(services, tasks=[rec.task("other", 1)], tick_s=0.01)
        assert second is first
        assert background.current() is first
        assert ran.wait(WAIT_S)
        assert first.is_running
    finally:
        background.stop()
    assert background.current() is None
    assert not first.is_running
    background.stop()                                  # stopping twice is fine


def test_module_start_after_stop_creates_new_loop(services, rec):
    first = background.start(services, tasks=[rec.task("t", 1)], tick_s=0.01)
    background.stop()
    second = background.start(services, tasks=[rec.task("t", 1)], tick_s=0.01)
    try:
        assert second is not first and second.is_running
    finally:
        background.stop()


def test_module_start_is_thread_safe(services, rec):
    results = []

    def go():
        results.append(background.start(services, tasks=[rec.task("t", 1)], tick_s=0.01))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT_S)
    try:
        assert len(results) == 6 and len({id(r) for r in results}) == 1
    finally:
        background.stop()


def test_import_starts_no_thread():
    assert not [t for t in threading.enumerate() if t.name == "cloud-compute-bg"]


# ---------------------------------------------------------------- ensure_started


def test_ensure_started_runs_one_real_loop(services, rec, monkeypatch):
    ran = threading.Event()
    tasks = [rec.task("t", 1, event=ran)]
    original = background.BackgroundLoop

    class Loop(original):
        def __init__(self, svc, task_list=None, **kw):
            super().__init__(svc, tasks, **kw)

    monkeypatch.setattr(background, "BackgroundLoop", Loop)
    assert background.ensure_started(services, tick_s=0.05) is True
    loop = background.current()
    assert loop is not None and loop.is_running
    assert ran.wait(WAIT_S)
    assert background.ensure_started(services) is False          # already running
    assert background.current() is loop


def test_ensure_started_restarts_a_loop_whose_thread_died(services, rec, monkeypatch):
    tasks = [rec.task("t", 1)]
    original = background.BackgroundLoop

    class Loop(original):
        def __init__(self, svc, task_list=None, **kw):
            super().__init__(svc, tasks, **kw)

    monkeypatch.setattr(background, "BackgroundLoop", Loop)
    assert background.ensure_started(services, tick_s=0.05) is True
    first = background.current()
    first._stop_event.set()                       # the thread winds down without background.stop()
    first._thread.join(WAIT_S)
    assert not first.is_running
    assert background.ensure_started(services, tick_s=0.05) is True
    second = background.current()
    assert second is not first and second.is_running


def test_ensure_started_does_not_restart_after_an_explicit_stop(services, rec, monkeypatch):
    tasks = [rec.task("t", 1)]
    original = background.BackgroundLoop

    class Loop(original):
        def __init__(self, svc, task_list=None, **kw):
            super().__init__(svc, tasks, **kw)

    monkeypatch.setattr(background, "BackgroundLoop", Loop)
    assert background.ensure_started(services, tick_s=0.05) is True
    background.stop()
    assert background.ensure_started(services) is False
    assert background.current() is None


def test_shutdown_stops_the_loop_and_allows_a_later_start(services, rec):
    ran = threading.Event()
    background.start(services, tasks=[rec.task("t", 1, event=ran)], tick_s=0.01)
    assert ran.wait(WAIT_S)
    background.shutdown()
    assert background.current() is None
    assert not [t for t in threading.enumerate() if t.name == "cloud-compute-bg"]
    background.shutdown()                                              # idempotent
    # unlike stop(), shutdown() forgets the start state, so a reload may start the loop again
    assert background._ensured is False


def test_shutdown_never_raises(monkeypatch):
    monkeypatch.setattr(background, "stop", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    background.shutdown()


def test_ensure_started_replaces_a_loop_that_works_on_another_container(rec, tmp_path, monkeypatch):
    from cloud_compute_core.store import Store

    original = background.BackgroundLoop

    class Loop(original):
        def __init__(self, svc, task_list=None, **kw):
            super().__init__(svc, [rec.task("t", 1)], **kw)

    monkeypatch.setattr(background, "BackgroundLoop", Loop)
    old_store, new_store = Store(tmp_path / "a.db"), Store(tmp_path / "a.db")
    old, new = make_services(old_store), make_services(new_store)
    try:
        assert background.ensure_started(old, tick_s=0.05) is True
        first = background.current()
        old_store.close()                                    # the container went stale
        assert background.ensure_started(new, tick_s=0.05) is True
        second = background.current()
        assert second is not first and second.services is new and second.is_running
        assert not first.is_running
        assert background.ensure_started(new, tick_s=0.05) is False      # settled again
    finally:
        background.stop()
        new_store.close()
