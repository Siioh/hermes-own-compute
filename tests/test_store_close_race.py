"""Closing the store while other threads use it (seen on macOS).

Closing a SQLite connection from one thread while another thread runs a statement on
it crashes CPython (segfault on 3.12 and 3.14). The store now locks each connection for
the length of every database operation and closes it only under that lock; the unload
path stops the plugin's own threads before it closes the store.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from cloud_compute_core import background
from cloud_compute_core.store import Store, StoreClosedError
from cloud_compute_core.testing import sample_host

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

_STRESS = textwrap.dedent("""
    import importlib, sys, tempfile, threading, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    store_mod = importlib.import_module("core.store")
    testing = importlib.import_module("core.testing")
    tmp = Path(sys.argv[2])
    seen = set()
    for r in range(int(sys.argv[3])):
        s = store_mod.Store(tmp / f"s{r}.db")
        stop = threading.Event()

        def work(n):
            i = 0
            while not stop.is_set():
                try:
                    s.upsert_host(testing.sample_host(id=f"h{n}-{i % 5}"))
                    s.list_hosts()
                    s.acquire_lease("bg", f"o{n}", 5)
                    s.snapshot()
                except Exception as exc:
                    seen.add(type(exc).__name__)
                    if s.closed:
                        break           # one late call per thread is enough
                i += 1

        ts = [threading.Thread(target=work, args=(n,), daemon=True) for n in range(6)]
        for t in ts:
            t.start()
        time.sleep(0.01)
        s.close()
        time.sleep(0.005)
        stop.set()
        for t in ts:
            t.join(5)
    print("ERRORS", sorted(seen))
""")


def test_closing_while_threads_hammer_the_store_never_crashes(tmp_path):
    """Run in a child process: on the old code this segfaults, and the suite must not die."""
    script = tmp_path / "stress.py"
    script.write_text(_STRESS, encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    proc = subprocess.run([sys.executable, "-I", str(script), str(PLUGIN_ROOT), str(data), "80"],
                          capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, (
        f"the stress child died (exit {proc.returncode}): {proc.stderr[-2000:]}")
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("ERRORS")][-1]
    # the only error a thread may see after close() is the store's own, typed one
    assert set(eval(line[len("ERRORS"):])) <= {"StoreClosedError"}, line


def test_closed_store_raises_a_typed_error_on_every_thread(tmp_path):
    s = Store(tmp_path / "c.db")
    ready, go, out = threading.Event(), threading.Event(), {}

    def worker():
        s.get_host("x")                       # this thread now has its own connection
        ready.set()
        go.wait(5)
        try:
            s.upsert_host(sample_host(id="late"))
        except Exception as exc:  # noqa: BLE001
            out["exc"] = exc

    t = threading.Thread(target=worker)
    t.start()
    assert ready.wait(5)
    assert s.close() is True
    go.set()
    t.join(5)
    assert isinstance(out.get("exc"), StoreClosedError)
    assert isinstance(out["exc"], RuntimeError)          # older callers catch RuntimeError
    with pytest.raises(StoreClosedError):
        s.list_hosts()
    with pytest.raises(StoreClosedError):
        s.kv_set("k", "v")
    assert s.closed and s._conns == []
    assert s.close() is True                              # idempotent


def test_close_waits_for_a_running_statement_and_never_closes_under_it(tmp_path):
    s = Store(tmp_path / "c.db")
    inside, release, done = threading.Event(), threading.Event(), {}

    def worker():
        with s._tx() as conn:                 # a write transaction that takes a while
            conn.execute("INSERT INTO kv (key, value) VALUES ('a', '1')")
            inside.set()
            release.wait(5)
            done["still_open"] = conn.execute("SELECT 1").fetchone()[0] == 1
            conn.execute("INSERT INTO kv (key, value) VALUES ('b', '2')")

    t = threading.Thread(target=worker)
    t.start()
    assert inside.wait(5)
    closer = threading.Thread(target=lambda: done.setdefault("closed", s.close(timeout=5)))
    closer.start()
    time.sleep(0.2)
    assert closer.is_alive(), "close() must wait while a statement runs"
    release.set()
    t.join(5)
    closer.join(5)
    assert done == {"still_open": True, "closed": True}
    reopened = Store(tmp_path / "c.db")
    try:
        assert reopened.kv_get("a") == "1" and reopened.kv_get("b") == "2"   # it committed
    finally:
        reopened.close()


def test_close_is_bounded_and_the_busy_thread_closes_its_own_connection(tmp_path):
    s = Store(tmp_path / "c.db")
    inside, release, seen = threading.Event(), threading.Event(), {}

    def worker():
        with s._use() as conn:
            seen["holder"] = s._local.holder
            inside.set()
            release.wait(5)
            seen["ok"] = conn.execute("SELECT 1").fetchone()[0]   # still a live handle
        try:
            s.get_host("x")
        except StoreClosedError:
            seen["after"] = "closed"

    t = threading.Thread(target=worker)
    t.start()
    assert inside.wait(5)
    started = time.monotonic()
    assert s.close(timeout=0.2) is False        # reported, not a hang
    assert time.monotonic() - started < 2.0
    assert seen["holder"].conn is not None      # not closed under the running statement
    release.set()
    t.join(5)
    assert seen["ok"] == 1 and seen["after"] == "closed"
    assert seen["holder"].conn is None          # closed by its own thread afterwards


def test_shutdown_waits_for_worker_threads_before_returning():
    finished = threading.Event()

    def work():
        time.sleep(0.3)
        finished.set()

    t = background.track_worker(threading.Thread(target=work, name="cc-probe-test", daemon=True))
    t.start()
    assert background.shutdown(timeout=5) == []
    assert finished.is_set() and not t.is_alive()


def test_shutdown_wait_is_bounded_and_names_what_is_still_running():
    release = threading.Event()
    t = background.track_worker(threading.Thread(target=lambda: release.wait(10),
                                                 name="cc-probe-stuck", daemon=True))
    t.start()
    try:
        started = time.monotonic()
        assert background.shutdown(timeout=0.3) == ["cc-probe-stuck"]
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        t.join(5)


def test_probe_threads_are_tracked(monkeypatch):
    from cloud_compute_core import hosts_api

    gate = threading.Event()

    class Prober:
        def probe(self, host):
            gate.wait(5)

    class Svc:
        prober = Prober()

        class store:
            @staticmethod
            def list_hosts():
                return [sample_host(id="h-track")]

        config = None

    monkeypatch.setattr(hosts_api, "_config_seconds", lambda s, k, d: d)
    hosts_api.run_probe(Svc())
    try:
        assert "cc-probe-h-track" in [t.name for t in background.workers()]
    finally:
        gate.set()
        assert background.join_workers(5) == []


def test_unload_joins_the_workers_before_it_closes_the_store(monkeypatch, tmp_path):
    """The plugin's unload callback: loop and workers first, then the store."""
    import importlib.util

    name = "cloud_compute_close_race_plugin"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(PLUGIN_ROOT)])
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[name] = plugin
    try:
        spec.loader.exec_module(plugin)
        _unload_order(plugin, monkeypatch, tmp_path)
    finally:
        for m in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[m]


def _unload_order(plugin, monkeypatch, tmp_path):
    from cloud_compute_core import testing

    order = []
    store = Store(tmp_path / "u.db")
    services = testing.make_services(store=store)
    real_close = store.close
    monkeypatch.setattr(store, "close", lambda *a, **k: (order.append("close"), real_close())[1])
    release = threading.Event()

    def work():
        release.wait(0.3)
        store.upsert_host(sample_host(id="from-worker"))     # must land before close
        order.append("worker done")

    t = background.track_worker(threading.Thread(target=work, name="cc-probe-u", daemon=True))
    t.start()
    monkeypatch.setattr(plugin, "_SERVICES", services)
    plugin._cleanup()
    assert order == ["worker done", "close"]
    assert store.closed
