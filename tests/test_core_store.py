"""Tests for core/store.py (SQLite store)."""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import threading
import time
import types

import pytest

from cloud_compute_core import store as store_mod
from cloud_compute_core.interfaces import ApiError, Services
from cloud_compute_core.models import HostState, utc_now_iso
from cloud_compute_core.store import Store, provide
from cloud_compute_core.testing import (
    sample_host, sample_host_state, sample_instance, sample_job, sample_local_host,
    sample_slurm_host, sample_template, sample_tunnel,
)


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).replace(
        microsecond=0, tzinfo=None).isoformat() + "Z"


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------- schema / paths


def test_schema_version_and_tables(store, tmp_path):
    conn = sqlite3.connect(str(tmp_path / "cloud-compute.db"))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store_mod.SCHEMA_VERSION == 1
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"hosts", "host_state", "templates", "instances", "jobs", "tunnels", "records",
            "events", "leases", "kv"} <= names


def test_reopen_is_idempotent_and_persists(tmp_path):
    path = tmp_path / "sub" / "dir" / "db.sqlite"
    s1 = Store(path)
    s1.upsert_host(sample_host())
    s1.kv_set("k", "v")
    s1.close()
    s2 = Store(path)
    try:
        assert s2.get_host("x1carbon") == sample_host()
        assert s2.kv_get("k") == "v"
    finally:
        s2.close()


def test_default_path_uses_plugin_data_dir(tmp_path, monkeypatch):
    calls = []

    def plugin_data_dir(name):
        calls.append(name)
        d = tmp_path / "plugin-data" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    fake = types.ModuleType("plugins.plugin_storage")
    fake.plugin_data_dir = plugin_data_dir
    pkg = types.ModuleType("plugins")
    pkg.plugin_storage = fake
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", fake)
    s = Store()
    try:
        assert calls == ["cloud-compute"]
        assert s.path == tmp_path / "plugin-data" / "cloud-compute" / "cloud-compute.db"
        assert s.path.exists()
    finally:
        s.close()


def _fake_hermes_constants(monkeypatch, home):
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)


def test_default_path_falls_back_to_public_get_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)
    _fake_hermes_constants(monkeypatch, tmp_path / "hh")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other-profile"))   # must be ignored
    s = Store()
    try:
        assert s.path == tmp_path / "hh" / "plugin-data" / "cloud-compute" / "cloud-compute.db"
        assert s.path.exists()
        assert not (tmp_path / "other-profile").exists()
    finally:
        s.close()


def test_default_path_never_reads_hermes_home_env(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    with pytest.raises(ApiError) as ei:
        Store()
    assert ei.value.status == 503 and ei.value.code == "unavailable"
    assert not (tmp_path / "hh").exists()


def test_default_path_unavailable_raises_503(monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    with pytest.raises(ApiError) as ei:
        Store()
    assert ei.value.status == 503 and ei.value.code == "unavailable"
    with pytest.raises(ApiError):
        provide(Services())


def test_provide_returns_store(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)
    _fake_hermes_constants(monkeypatch, tmp_path)
    s = provide(Services())
    try:
        assert isinstance(s, Store)
    finally:
        s.close()


def test_store_source_has_no_environ_read():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "core" / "store.py").read_text(encoding="utf-8")
    assert "os.environ" not in src and "getenv" not in src


# ---------------------------------------------------------------- hosts


def test_host_crud_and_kind_filter(store):
    assert store.get_host("nope") is None
    assert store.list_hosts() == []
    a = store.upsert_host(sample_host(name="b-host"))
    b = store.upsert_host(sample_slurm_host(name="a-host"))
    c = store.upsert_host(sample_local_host(name="a-host"))
    assert a == store.get_host("x1carbon")
    # sorted by name then id
    assert [h.id for h in store.list_hosts()] == ["local", "slurm-x1", "x1carbon"]
    assert [h.id for h in store.list_hosts(kind="slurm")] == ["slurm-x1"]
    assert [h.id for h in store.list_hosts(kind="ec2")] == []
    assert store.get_host("slurm-x1").slurm.workdir == "/data/cloud-compute"
    store.upsert_host(sample_host(name="renamed"))
    assert store.get_host("x1carbon").name == "renamed"
    assert len(store.list_hosts()) == 3
    assert store.delete_host("x1carbon") is True
    assert store.delete_host("x1carbon") is False
    assert store.get_host("x1carbon") is None
    assert b.id and c.id


def test_upsert_host_fills_created_at(store):
    spec = sample_host(created_at="")
    stored = store.upsert_host(spec)
    assert stored.created_at and stored.created_at.endswith("Z")
    assert store.get_host("x1carbon").created_at == stored.created_at
    kept = store.upsert_host(sample_host(created_at="2026-01-01T00:00:00Z"))
    assert kept.created_at == "2026-01-01T00:00:00Z"


def test_host_state_roundtrip_and_prev_status(store):
    store.upsert_host(sample_host())
    assert store.get_host_state("x1carbon") is None
    st = sample_host_state()
    assert store.set_host_state(st) is None
    assert store.get_host_state("x1carbon") == st
    assert store.set_host_state(sample_host_state(status="offline", last_error="boom")) == "online"
    assert store.set_host_state(sample_host_state(status="online")) == "offline"
    assert store.get_host_state("x1carbon").status == "online"
    # delete_host removes state too
    store.delete_host("x1carbon")
    assert store.get_host_state("x1carbon") is None


def test_delete_host_does_not_cascade(store):
    store.upsert_host(sample_host())
    store.upsert_instance(sample_instance())
    store.delete_host("x1carbon")
    assert store.get_instance("i-ab12cd34") is not None


# ---------------------------------------------------------------- templates / instances / jobs / tunnels


def test_template_crud(store):
    assert store.get_template("python-dev") is None
    t = store.upsert_template(sample_template())
    store.upsert_template(sample_template(id="a-tmpl", name="A"))
    assert store.get_template("python-dev") == t
    assert [x.id for x in store.list_templates()] == ["a-tmpl", "python-dev"]
    assert store.delete_template("a-tmpl") is True
    assert store.delete_template("a-tmpl") is False
    assert [x.id for x in store.list_templates()] == ["python-dev"]


def test_instance_crud_and_filters(store):
    i1 = sample_instance(id="i-00000002", created_at="2026-11-08T10:00:00Z")
    i2 = sample_instance(id="i-00000001", created_at="2026-11-08T11:00:00Z", status="stopped")
    i3 = sample_instance(id="i-00000003", created_at="2026-11-08T09:00:00Z", host_id="local",
                         status="destroyed")
    for i in (i1, i2, i3):
        assert store.upsert_instance(i) == i
    assert store.get_instance("i-00000002") == i1
    assert store.get_instance("i-missing") is None
    assert [i.id for i in store.list_instances()] == ["i-00000003", "i-00000002", "i-00000001"]
    assert [i.id for i in store.list_instances(host_id="x1carbon")] == ["i-00000002", "i-00000001"]
    assert [i.id for i in store.list_instances(statuses={"running", "stopped"})] == [
        "i-00000002", "i-00000001"]
    assert [i.id for i in store.list_instances(host_id="local", statuses={"destroyed"})] == [
        "i-00000003"]
    assert store.list_instances(statuses=set()) == []
    store.upsert_instance(sample_instance(id="i-00000002", created_at="2026-11-08T10:00:00Z",
                                          status="error", error="x"))
    assert store.get_instance("i-00000002").status == "error"
    assert store.delete_instance("i-00000002") is True
    assert store.delete_instance("i-00000002") is False


def test_job_crud_and_filters(store):
    j1 = sample_job(id="j-1", submitted_at="2026-11-08T10:00:00Z", state="PENDING")
    j2 = sample_job(id="j-2", submitted_at="2026-11-08T09:00:00Z", state="COMPLETED")
    j3 = sample_job(id="j-3", submitted_at="2026-11-08T11:00:00Z", host_id="other",
                    state="RUNNING")
    for j in (j1, j2, j3):
        store.upsert_job(j)
    assert store.get_job("j-1") == j1
    assert store.get_job("j-none") is None
    assert [j.id for j in store.list_jobs()] == ["j-2", "j-1", "j-3"]
    assert [j.id for j in store.list_jobs(active_only=True)] == ["j-1", "j-3"]
    assert [j.id for j in store.list_jobs(host_id="slurm-x1")] == ["j-2", "j-1"]
    assert [j.id for j in store.list_jobs(host_id="slurm-x1", active_only=True)] == ["j-1"]
    store.upsert_job(sample_job(id="j-1", submitted_at="2026-11-08T10:00:00Z", state="FAILED",
                                exit_code="1:0", notified=True))
    got = store.get_job("j-1")
    assert got.state == "FAILED" and got.notified is True
    assert [j.id for j in store.list_jobs(active_only=True)] == ["j-3"]
    assert store.delete_job("j-1") is True
    assert store.delete_job("j-1") is False


def test_tunnel_crud_and_open_only(store):
    t1 = sample_tunnel(id="t-1", status="open", created_at="2026-11-08T10:00:00Z")
    t2 = sample_tunnel(id="t-2", status="starting", created_at="2026-11-08T09:00:00Z")
    t3 = sample_tunnel(id="t-3", status="closed", created_at="2026-11-08T08:00:00Z")
    t4 = sample_tunnel(id="t-4", status="error", created_at="2026-11-08T07:00:00Z", error="x")
    for t in (t1, t2, t3, t4):
        store.upsert_tunnel(t)
    assert [t.id for t in store.list_tunnels()] == ["t-2", "t-1"]
    assert [t.id for t in store.list_tunnels(open_only=False)] == ["t-4", "t-3", "t-2", "t-1"]
    assert store.get_tunnel("t-4") == t4
    assert store.get_tunnel("t-9") is None
    assert store.delete_tunnel("t-1") is True
    assert store.delete_tunnel("t-1") is False


def test_corrupt_rows_are_skipped(store, tmp_path, caplog):
    store.upsert_instance(sample_instance(id="i-good"))
    store.upsert_host(sample_host())
    conn = sqlite3.connect(str(tmp_path / "cloud-compute.db"))
    try:
        with conn:
            conn.execute("INSERT INTO instances (id, host_id, status, data, updated_at) "
                         "VALUES ('i-bad', 'x1carbon', 'running', '{not json', '')")
            conn.execute("INSERT INTO instances (id, host_id, status, data, updated_at) "
                         "VALUES ('i-bad2', 'x1carbon', 'running', '{\"id\": \"i-bad2\"}', '')")
            conn.execute("INSERT INTO hosts (id, host_id, status, data, updated_at) "
                         "VALUES ('h-bad', 'h-bad', 'ssh', '[1,2', '')")
            conn.execute("INSERT INTO records (kind, id, data) VALUES ('x', 'r', 'nope')")
    finally:
        conn.close()
    with caplog.at_level("WARNING"):
        assert [i.id for i in store.list_instances()] == ["i-good"]
        assert [h.id for h in store.list_hosts()] == ["x1carbon"]
        assert store.get_instance("i-bad") is None
        assert store.list_records("x") == []
    assert "i-bad" in caplog.text
    snap = store.snapshot()
    assert [i["id"] for i in snap["instances"]] == ["i-good"]


# ---------------------------------------------------------------- generic records


def test_generic_records(store):
    assert store.get_record("agent_run", "a-1") is None
    store.put_record("agent_run", "a-2", {"id": "a-2", "n": 2}, host_id="h1", status="running")
    store.put_record("agent_run", "a-1", {"id": "a-1", "n": 1}, host_id="h2", status="done")
    store.put_record("other", "a-1", {"other": True})
    assert store.get_record("agent_run", "a-1") == {"id": "a-1", "n": 1}
    assert store.get_record("other", "a-1") == {"other": True}
    assert [r["id"] for r in store.list_records("agent_run")] == ["a-1", "a-2"]
    assert [r["id"] for r in store.list_records("agent_run", host_id="h1")] == ["a-2"]
    assert [r["id"] for r in store.list_records("agent_run", statuses={"done"})] == ["a-1"]
    assert store.list_records("agent_run", host_id="h1", statuses={"done"}) == []
    store.put_record("agent_run", "a-1", {"id": "a-1", "n": 9}, host_id="h2", status="running")
    assert store.get_record("agent_run", "a-1")["n"] == 9
    assert len(store.list_records("agent_run", statuses=["running"])) == 2
    assert store.delete_record("agent_run", "a-1") is True
    assert store.delete_record("agent_run", "a-1") is False
    assert store.get_record("other", "a-1") == {"other": True}


# ---------------------------------------------------------------- events


def test_events_order_since_and_limit(store):
    seqs = [store.append_event("notify", {"i": i}) for i in range(10)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 10
    all_ev = store.events_since(0)
    assert [e["payload"]["i"] for e in all_ev] == list(range(10))
    assert set(all_ev[0]) == {"seq", "ts", "name", "payload"}
    assert all_ev[0]["name"] == "notify" and all_ev[0]["ts"].endswith("Z")
    after = store.events_since(seqs[6])
    assert [e["seq"] for e in after] == seqs[7:]
    assert [e["seq"] for e in store.events_since(seqs[2], limit=3)] == seqs[3:6]
    assert store.events_since(seqs[-1]) == []


def test_event_pruning_bound(store, monkeypatch):
    monkeypatch.setattr(store_mod, "MAX_EVENTS", 5)
    monkeypatch.setattr(store_mod, "PRUNE_EVERY", 1)
    seqs = [store.append_event("e", {"i": i}) for i in range(20)]
    remaining = store.events_since(0, limit=1000)
    assert len(remaining) == 5
    assert [e["seq"] for e in remaining] == seqs[-5:]


def test_default_event_cap_is_5000():
    assert store_mod.MAX_EVENTS == 5000


# ---------------------------------------------------------------- leases


def test_lease_exclusive_across_two_stores(tmp_path):
    clock = Clock()
    path = tmp_path / "shared.db"
    a, b = Store(path, clock=clock), Store(path, clock=clock)
    try:
        assert a.acquire_lease("bg", "owner-a", 30) is True
        assert b.acquire_lease("bg", "owner-b", 30) is False
        # same owner renews and extends
        clock.t += 20
        assert a.acquire_lease("bg", "owner-a", 30) is True
        clock.t += 20  # 40 s after first acquire, but only 20 s after renewal
        assert b.acquire_lease("bg", "owner-b", 30) is False
        # expiry
        clock.t += 11
        assert b.acquire_lease("bg", "owner-b", 30) is True
        assert a.acquire_lease("bg", "owner-a", 30) is False
        # release only by owner
        a.release_lease("bg", "owner-a")
        assert a.acquire_lease("bg", "owner-a", 30) is False
        b.release_lease("bg", "owner-b")
        assert a.acquire_lease("bg", "owner-a", 30) is True
        # independent lease names
        assert b.acquire_lease("other", "owner-b", 30) is True
    finally:
        a.close()
        b.close()


def test_lease_uses_wall_clock_by_default(store):
    assert store.acquire_lease("x", "o1", 60) is True
    assert store.acquire_lease("x", "o2", 60) is False
    assert store.acquire_lease("y", "o1", -1) is True
    assert store.acquire_lease("y", "o2", 60) is True


# ---------------------------------------------------------------- kv


def test_kv(store):
    assert store.kv_get("a") is None
    store.kv_set("a", "1")
    assert store.kv_get("a") == "1"
    store.kv_set("a", "2")
    assert store.kv_get("a") == "2"
    assert store.kv_delete("a") is True
    assert store.kv_delete("a") is False
    assert store.kv_get("a") is None


# ---------------------------------------------------------------- snapshot


def test_snapshot_shape_and_windows(tmp_path):
    now = time.time()
    s = Store(tmp_path / "snap.db", clock=lambda: now)
    try:
        s.upsert_host(sample_host())
        s.upsert_host(sample_slurm_host())
        s.set_host_state(sample_host_state())
        s.upsert_instance(sample_instance(id="i-live"))
        s.upsert_instance(sample_instance(id="i-gone", status="destroyed"))
        s.upsert_instance(sample_instance(id="i-stopped", status="stopped"))
        s.upsert_job(sample_job(id="j-run", state="RUNNING"))
        s.upsert_job(sample_job(id="j-pend", state="PENDING"))
        s.upsert_job(sample_job(id="j-recent", state="COMPLETED", ended_at=_iso(now - 3600)))
        s.upsert_job(sample_job(id="j-old", state="FAILED", ended_at=_iso(now - 25 * 3600)))
        # no ended_at: falls back to updated_at (just now)
        s.upsert_job(sample_job(id="j-noend", state="CANCELLED", ended_at=None))
        s.upsert_job(sample_job(id="j-unknown", state="UNKNOWN"))
        snap = s.snapshot()
    finally:
        s.close()
    import json
    json.dumps(snap)  # JSON-safe
    assert set(snap) == {"hosts", "instances", "jobs", "generated_at"}
    hosts = {h["spec"]["id"]: h for h in snap["hosts"]}
    assert set(hosts) == {"x1carbon", "slurm-x1"}
    assert hosts["x1carbon"]["state"]["status"] == "online"
    assert hosts["x1carbon"]["state"] == sample_host_state().to_dict()
    assert hosts["slurm-x1"]["state"] is None
    assert hosts["slurm-x1"]["spec"] == sample_slurm_host().to_dict()
    assert sorted(i["id"] for i in snap["instances"]) == ["i-live", "i-stopped"]
    assert sorted(j["id"] for j in snap["jobs"]) == ["j-noend", "j-pend", "j-recent", "j-run"]
    assert snap["generated_at"].endswith("Z")


def test_snapshot_empty(store):
    snap = store.snapshot()
    assert snap["hosts"] == [] and snap["instances"] == [] and snap["jobs"] == []


# ---------------------------------------------------------------- concurrency


def test_concurrent_writes_from_threads(store):
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(25):
                store.upsert_instance(sample_instance(id=f"i-{n:02d}{i:04d}"))
                store.append_event("e", {"n": n, "i": i})
                store.kv_set(f"k{n}", str(i))
                store.set_host_state(sample_host_state(host_id=f"h{n}"))
                store.acquire_lease("bg", f"o{n}", 30)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.list_instances()) == 150
    assert len(store.events_since(0, limit=1000)) == 150
    assert all(store.kv_get(f"k{n}") == "24" for n in range(6))


def test_concurrent_writes_two_stores(tmp_path):
    path = tmp_path / "two.db"
    a, b = Store(path), Store(path)
    errors: list[BaseException] = []

    def worker(s: Store, tag: str) -> None:
        try:
            for i in range(30):
                s.put_record("r", f"{tag}{i}", {"i": i})
                s.append_event("e", {"tag": tag})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        ts = [threading.Thread(target=worker, args=(a, "a")),
              threading.Thread(target=worker, args=(b, "b"))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == []
        assert len(a.list_records("r")) == 60
        assert len(b.events_since(0, limit=1000)) == 60
    finally:
        a.close()
        b.close()


def test_close_then_use_raises(tmp_path):
    s = Store(tmp_path / "c.db")
    s.close()
    with pytest.raises(Exception):
        s.kv_get("x")
    s.close()  # idempotent


def test_host_state_model_roundtrip_types(store):
    st = sample_host_state()
    store.set_host_state(st)
    got = store.get_host_state(st.host_id)
    assert isinstance(got, HostState)
    assert got.capabilities.runtime == "podman"
    assert got.resources is not None and got.resources.mem_total_mb == 15974
    assert utc_now_iso()


# ---------------------------------------------------------------- connection lifetime (bug-hunt #3)


def test_connections_do_not_grow_with_finished_threads(store):
    def worker():
        store.get_host("x")              # what each liveness/probe/stats/reaper/poll thread does

    for _ in range(200):
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    alive = len(store._conns)
    assert alive < 20, f"{alive} SQLite connections still open after 200 finished threads"


def test_thread_connection_is_closed_when_the_thread_ends(store):
    seen = {}

    def worker():
        seen["conn"] = store._conn()
        store.upsert_host(sample_host(id="from-thread"))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    import gc
    gc.collect()
    with pytest.raises(sqlite3.ProgrammingError):     # "Cannot operate on a closed database"
        seen["conn"].execute("SELECT 1")
    assert store.get_host("from-thread") is not None  # the write was committed


def test_dead_thread_connections_are_swept_even_if_the_holder_lingers(store):
    keep = []

    def worker():
        store.get_host("x")
        keep.append(store._local.holder)              # keep it alive past the thread's end

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert keep[0].conn is not None                   # the finalizer did not run
    store._conn()                                     # main thread: no new connection needed...
    t2 = threading.Thread(target=lambda: store.get_host("y"))
    t2.start()
    t2.join()                                         # ...but a new thread's first use sweeps
    assert keep[0].conn is None


def test_concurrent_threads_keep_working_connections(store):
    errors = []
    barrier = threading.Barrier(8)

    def worker(n):
        try:
            barrier.wait(5)
            for i in range(20):
                store.upsert_host(sample_host(id=f"h{n}-{i}"))
                assert store.get_host(f"h{n}-{i}") is not None
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.list_hosts()) == 160


def test_close_closes_every_thread_connection_and_blocks_new_ones(tmp_path):
    s = Store(tmp_path / "c.db")
    ready, release = threading.Event(), threading.Event()

    def worker():
        s.get_host("x")
        ready.set()
        release.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    ready.wait(5)
    assert len(s._conns) == 2
    s.close()
    release.set()
    t.join()
    assert s._conns == []
    with pytest.raises(RuntimeError):
        s.get_host("x")


# ---------------------------------------------------------------- file permissions


posix_only = pytest.mark.skipif(sys.platform == "win32" or not hasattr(__import__("os"), "getuid"),
                                reason="POSIX permission bits; Windows relies on the user profile ACLs")


def _mode(path) -> int:
    import os
    return os.stat(path).st_mode & 0o777


@posix_only
def test_new_data_folder_and_database_are_owner_only(tmp_path):
    import os
    old = os.umask(0o022)
    try:
        s = Store(tmp_path / "data" / "cloud-compute.db")
        try:
            s.kv_set("k", "v")
            assert _mode(tmp_path / "data") == 0o700
            assert _mode(s.path) == 0o600
            for suffix in ("-wal", "-shm"):
                side = s.path.with_name(s.path.name + suffix)
                if side.exists():
                    assert _mode(side) == 0o600, suffix
        finally:
            s.close()
    finally:
        os.umask(old)


@posix_only
def test_existing_database_is_tightened_but_a_foreign_folder_is_left_alone(tmp_path):
    import os
    folder = tmp_path / "shared"
    folder.mkdir()
    os.chmod(folder, 0o755)
    db = folder / "cloud-compute.db"
    Store(db).close()
    os.chmod(db, 0o644)
    s = Store(db)
    try:
        assert _mode(db) == 0o600
        assert _mode(folder) == 0o755          # a caller-chosen folder that already existed
    finally:
        s.close()


@posix_only
def test_default_data_folder_is_tightened(tmp_path, monkeypatch):
    import os
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)
    _fake_hermes_constants(monkeypatch, tmp_path / "hh")
    folder = tmp_path / "hh" / "plugin-data" / "cloud-compute"
    folder.mkdir(parents=True)
    os.chmod(folder, 0o755)                     # as Hermes' plugin_data_dir() leaves it
    s = Store()
    try:
        assert _mode(folder) == 0o700 and _mode(s.path) == 0o600
    finally:
        s.close()


def test_windows_leaves_permissions_to_the_profile(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(store_mod, "_os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(store_mod.Path, "chmod", lambda self, mode: calls.append(mode))
    s = Store(tmp_path / "w" / "cloud-compute.db")
    try:
        assert calls == []
    finally:
        s.close()
