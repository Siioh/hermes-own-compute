"""Slurm routes, tools, approval and the poll task (core/slurm_api.py)."""

from __future__ import annotations

import threading

import pytest

from cloud_compute_core import registry, slurm_api
from cloud_compute_core.approval import approval_for
from cloud_compute_core.interfaces import ApiError, ApiRequest, BackgroundTask, Route, ToolSpec
from cloud_compute_core.models import SlurmSettings
from cloud_compute_core.store import Store
from cloud_compute_core.testing import (
    make_services, sample_host, sample_job, sample_slurm_host,
)


class FakeSlurm:
    def __init__(self):
        self.calls = []
        self.jobs = {"j-1a2b3c4d": sample_job()}
        self.poll_errors = {}
        self.polled = []

    def partitions(self, host_id):
        self.calls.append(("partitions", host_id))
        if host_id == "nope":
            raise ApiError(404, "not_found", "no host nope")
        from cloud_compute_core.models import Partition
        return [Partition("cpu", "up", 2, 0, 4, 4, 4096, 2048, None, "1-00:00:00", True)]

    def submit(self, req, *, session_id=None):
        self.calls.append(("submit", req, session_id))
        if req.host_id == "offline":
            raise ApiError(503, "host_offline", "host offline")
        return sample_job(id="j-22222222", name=req.name, host_id=req.host_id,
                          session_id=session_id)

    def list(self, host_id=None, active_only=False):
        self.calls.append(("list", host_id, active_only))
        return list(self.jobs.values())

    def get(self, job_id):
        self.calls.append(("get", job_id))
        if job_id not in self.jobs:
            raise ApiError(404, "not_found", f"no job {job_id}")
        return self.jobs[job_id]

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return sample_job(id=job_id, state="CANCELLED")

    def output(self, job_id, *, tail=200, stream="out"):
        self.calls.append(("output", job_id, tail, stream))
        return "line1\nline2\n"

    def delete(self, job_id):
        self.calls.append(("delete", job_id))
        return {"deleted": job_id, "host_id": "slurm-x1", "job_dir": "/data/cc/" + job_id,
                "note": "Removed"}

    def clear_finished(self, host_id=None):
        self.calls.append(("clear_finished", host_id))
        return {"deleted": [], "count": 0, "job_dirs": [], "note": "No finished jobs to remove."}

    def poll(self, host_id):
        self.polled.append(host_id)
        err = self.poll_errors.get(host_id)
        if err:
            raise err
        return []

    # the real manager answers these from the store: open = not terminal
    def hosts_with_active_jobs(self):
        from cloud_compute_core.models import TERMINAL_JOB_STATES
        return sorted({j.host_id for j in self.store.list_jobs(None)
                       if j.state not in TERMINAL_JOB_STATES})

    def poll_interval(self, host_id):
        host = self.store.get_host(host_id)
        seconds = host.slurm.poll_seconds if host and host.slurm else 60
        return max(30.0, float(seconds))


@pytest.fixture
def fake(store):
    f = FakeSlurm()
    f.store = store
    return f


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "slurm.db")


@pytest.fixture
def services(fake, store):
    return make_services(slurm=fake, store=store)


@pytest.fixture
def clock():
    class Clock:
        t = 1000.0

        def __call__(self):
            return self.t

    c = Clock()
    slurm_api._state = slurm_api._PollState(c)
    yield c
    slurm_api._state = slurm_api._PollState()


def req(services, path_params=None, query=None, body=None):
    return ApiRequest(path_params=path_params or {}, query=query or {}, body=body,
                      services=services)


def route(method, path):
    for r in slurm_api.routes():
        if (r.method, r.path) == (method, path):
            return r.handler
    raise AssertionError(f"no route {method} {path}")


def tool(name):
    return next(t for t in slurm_api.tools() if t.name == name)


SUBMIT = {"host_id": "slurm-x1", "name": "hello", "command": "hostname"}


# --------------------------------------------------------------------------- routes


def test_route_table():
    got = {(r.method, r.path) for r in slurm_api.routes()}
    assert got == {
        ("GET", "/jobs"), ("POST", "/jobs"), ("GET", "/jobs/{job_id}"),
        ("POST", "/jobs/{job_id}/cancel"), ("GET", "/jobs/{job_id}/output"),
        ("GET", "/hosts/{host_id}/partitions"), ("DELETE", "/jobs/{job_id}"),
        ("POST", "/jobs/clear-finished"),
    }
    assert all(isinstance(r, Route) for r in slurm_api.routes())


def test_list_jobs_query(services, fake):
    out = route("GET", "/jobs")(req(services))
    assert out[0]["id"] == "j-1a2b3c4d" and fake.calls[-1] == ("list", None, False)
    route("GET", "/jobs")(req(services, query={"host": "slurm-x1", "active": "1"}))
    assert fake.calls[-1] == ("list", "slurm-x1", True)
    route("GET", "/jobs")(req(services, query={"active": "0"}))
    assert fake.calls[-1] == ("list", None, False)


def test_submit_route(services, fake):
    out = route("POST", "/jobs")(req(services, body={**SUBMIT, "cpus": 4, "mem_gb": 2.5,
                                                     "session_id": "s1"}))
    assert out["id"] == "j-22222222" and out["session_id"] == "s1"
    _, jr, sid = fake.calls[-1]
    assert (jr.host_id, jr.name, jr.command, jr.cpus, jr.mem_gb, jr.gpus, jr.time_limit) == \
        ("slurm-x1", "hello", "hostname", 4, 2.5, 0, "00:30:00")
    assert sid == "s1"


@pytest.mark.parametrize("body", [
    None, [], {"name": "x", "command": "c"}, {"host_id": "h", "command": "c"},
    {"host_id": "h", "name": "x"},
    {"host_id": "h", "name": "x", "command": "c", "script": "s"},
    {**SUBMIT, "cpus": "4"}, {**SUBMIT, "cpus": 0}, {**SUBMIT, "cpus": True},
    {**SUBMIT, "mem_gb": "1"}, {**SUBMIT, "mem_gb": 0}, {**SUBMIT, "gpus": -1},
    {**SUBMIT, "image": 5}, {**SUBMIT, "name": ""},
])
def test_submit_route_rejects(services, fake, body):
    with pytest.raises(ApiError) as e:
        route("POST", "/jobs")(req(services, body=body))
    assert (e.value.status, e.value.code) == (400, "invalid")
    assert not [c for c in fake.calls if c[0] == "submit"]


def test_script_accepted_and_preserved(services, fake):
    script = "#!/bin/bash\necho hi\n"
    route("POST", "/jobs")(req(services, body={"host_id": "h", "name": "n", "script": script}))
    assert fake.calls[-1][1].script == script and fake.calls[-1][1].command is None


def test_get_and_cancel_route(services, fake):
    assert route("GET", "/jobs/{job_id}")(req(services, {"job_id": "j-1a2b3c4d"}))["id"] == "j-1a2b3c4d"
    with pytest.raises(ApiError) as e:
        route("GET", "/jobs/{job_id}")(req(services, {"job_id": "zzz"}))
    assert e.value.status == 404
    out = route("POST", "/jobs/{job_id}/cancel")(req(services, {"job_id": "j-1a2b3c4d"}))
    assert out["state"] == "CANCELLED" and fake.calls[-1] == ("cancel", "j-1a2b3c4d")
    with pytest.raises(ApiError) as e:
        route("GET", "/jobs/{job_id}")(req(services, {}))
    assert e.value.code == "invalid"


def test_output_route(services, fake):
    h = route("GET", "/jobs/{job_id}/output")
    assert h(req(services, {"job_id": "j"})) == {"text": "line1\nline2\n", "status": "ok",
                                               "note": None}
    assert fake.calls[-1] == ("output", "j", 200, "out")
    h(req(services, {"job_id": "j"}, query={"tail": "50", "stream": "err"}))
    assert fake.calls[-1] == ("output", "j", 50, "err")
    h(req(services, {"job_id": "j"}, query={"tail": "99999999"}))
    assert fake.calls[-1][2] == slurm_api.MAX_TAIL
    for q in ({"tail": "x"}, {"tail": "0"}, {"stream": "both"}):
        with pytest.raises(ApiError) as e:
            h(req(services, {"job_id": "j"}, query=q))
        assert e.value.code == "invalid"


def test_partitions_route(services):
    out = route("GET", "/hosts/{host_id}/partitions")(req(services, {"host_id": "slurm-x1"}))
    assert out[0]["name"] == "cpu" and out[0]["default"] is True
    with pytest.raises(ApiError) as e:
        route("GET", "/hosts/{host_id}/partitions")(req(services, {"host_id": "nope"}))
    assert e.value.status == 404


def test_routes_unavailable_without_manager():
    from cloud_compute_core.interfaces import Services
    bare = Services()
    with pytest.raises(ApiError) as e:
        route("GET", "/jobs")(req(bare))
    assert (e.value.status, e.value.code) == (503, "unavailable")


# --------------------------------------------------------------------------- tools


def test_schemas_are_valid_function_schemas():
    specs = slurm_api.tools()
    assert [t.name for t in specs] == ["slurm_submit", "slurm_jobs"]
    for t in specs:
        assert isinstance(t, ToolSpec) and t.toolset == "cloud_compute"
        s = t.schema
        assert s["name"] == t.name and s["description"]
        assert s["parameters"]["type"] == "object"
        props = s["parameters"]["properties"]
        assert set(s["parameters"]["required"]) <= set(props)
        assert "compute_status" in s["description"]
    submit = tool("slurm_submit").schema["parameters"]
    assert submit["required"] == ["host_id", "name"]
    assert set(submit["properties"]) == {"host_id", "name", "command", "script", "image",
                                         "partition", "cpus", "mem_gb", "gpus", "time_limit",
                                         "array"}
    jobs = tool("slurm_jobs").schema["parameters"]
    assert jobs["required"] == []
    assert jobs["properties"]["action"]["enum"] == ["list", "status", "output", "wait",
                                                    "cancel", "delete"]
    assert jobs["properties"]["stream"]["enum"] == ["out", "err"]
    assert "tight loop" in tool("slurm_jobs").schema["description"]


def test_submit_tool_records_session(services, fake):
    out = tool("slurm_submit").handler({**SUBMIT, "image": "docker://x"}, services,
                                       {"session_id": "sess-9"})
    assert out["job"]["id"] == "j-22222222" and out["job"]["session_id"] == "sess-9"
    assert fake.calls[-1][1].image == "docker://x" and fake.calls[-1][2] == "sess-9"
    tool("slurm_submit").handler(SUBMIT, services, {})
    assert fake.calls[-1][2] is None


@pytest.mark.parametrize("args", [
    {}, {"host_id": "h"}, {"host_id": "h", "name": "n"}, {**SUBMIT, "cpus": "many"},
    {**SUBMIT, "script": "x"}, "nonsense", None,
])
def test_submit_tool_invalid_never_raises(services, args):
    out = tool("slurm_submit").handler(args, services, {})
    assert out["error"]["code"] == "invalid" and isinstance(out["error"]["message"], str)


def test_tool_error_shapes(services, fake):
    out = tool("slurm_submit").handler({**SUBMIT, "host_id": "offline"}, services, {})
    assert out == {"error": {"code": "host_offline", "message": "host offline"}}
    fake.list = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    out = tool("slurm_jobs").handler({}, services, {})
    assert out["error"]["code"] == "internal" and "boom" in out["error"]["message"]
    out = tool("slurm_jobs").handler({"action": "status", "job_id": "zzz"}, services, {})
    assert out["error"]["code"] == "not_found"


def test_tool_error_when_manager_missing():
    from cloud_compute_core.interfaces import Services
    out = tool("slurm_jobs").handler({}, Services(), {})
    assert out["error"]["code"] == "unavailable"


def test_jobs_tool_actions(services, fake):
    h = tool("slurm_jobs").handler
    assert h({}, services, {})["jobs"][0]["id"] == "j-1a2b3c4d"
    assert fake.calls[-1] == ("list", None, False)
    h({"host_id": "slurm-x1", "action": "list"}, services, {})
    assert fake.calls[-1] == ("list", "slurm-x1", False)
    assert h({"action": "status", "job_id": "j-1a2b3c4d"}, services, {})["job"]["name"] == "hello"
    assert h({"action": "cancel", "job_id": "j-1a2b3c4d"}, services, {})["job"]["state"] == "CANCELLED"
    assert h({"action": "output", "job_id": "j", "tail": 5, "stream": "err"}, services, {}) == \
        {"text": "line1\nline2\n", "status": "ok", "note": None, "card": '::cc-job{id="j"}'}
    assert fake.calls[-1] == ("output", "j", 5, "err")
    for bad in ({"action": "status"}, {"action": "explode"}, {"action": "output", "job_id": "j",
                                                              "stream": "x"}):
        assert h(bad, services, {})["error"]["code"] == "invalid"


# --------------------------------------------------------------------------- approval


def test_submit_approval_message(services, store):
    store.upsert_host(sample_slurm_host(name="Cluster One"))
    msg = tool("slurm_submit").approval(
        {**SUBMIT, "name": "train", "cpus": 4, "mem_gb": 8.0, "time_limit": "01:00:00",
         "image": "docker://python:3.12", "array": "1-3"}, services)
    assert msg == ("Submit Slurm job train on Cluster One (4 CPU, 8 GB, 01:00:00, "
                   "image docker://python:3.12, array 1-3)? It runs this command as you on the "
                   "cluster:\n" + SUBMIT["command"])


def test_submit_approval_defaults_and_unknown_host(services):
    msg = tool("slurm_submit").approval({"host_id": "ghost", "name": "n"}, services)
    assert msg == "Submit Slurm job n on ghost (1 CPU, 1 GB, 00:30:00)?"
    assert tool("slurm_submit").approval({}, services)  # never None


def test_jobs_approval_only_for_cancel(services, store):
    store.upsert_host(sample_slurm_host())
    store.upsert_job(sample_job())
    a = tool("slurm_jobs").approval
    assert a({"action": "cancel", "job_id": "j-1a2b3c4d"}, services) == \
        "Cancel Slurm job j-1a2b3c4d (hello) on slurm-x1?"
    assert a({"action": "cancel", "job_id": "j-unknown"}, services) == \
        "Cancel Slurm job j-unknown on the selected host?"
    for args in ({}, {"action": "list"}, {"action": "status", "job_id": "j"},
                 {"action": "output", "job_id": "j"}):
        assert a(args, services) is None


def test_approval_gate_integration(services, store):
    store.upsert_host(sample_slurm_host())
    d = approval_for("slurm_submit", SUBMIT, tool("slurm_submit"), services)
    assert d["action"] == "approve" and d["rule_key"].startswith(
        "cloud-compute:slurm_submit:slurm-x1:")
    assert "Submit Slurm job hello on slurm-x1" in d["message"]
    assert approval_for("slurm_jobs", {"action": "list"}, tool("slurm_jobs"), services) is None
    d = approval_for("slurm_jobs", {"action": "cancel", "job_id": "j-1"}, tool("slurm_jobs"),
                     services)
    assert d["action"] == "approve" and d["rule_key"].startswith("cloud-compute:slurm_jobs:")


# --------------------------------------------------------------------------- background task


def _setup_hosts(store, *specs):
    for host_id, poll_seconds in specs:
        store.upsert_host(sample_slurm_host(
            id=host_id, name=host_id, slurm=SlurmSettings(poll_seconds=poll_seconds)))


def _job(store, host_id, jid, state="RUNNING"):
    store.upsert_job(sample_job(id=jid, host_id=host_id, state=state))


def task():
    return slurm_api.background_tasks()[0]


def test_task_declared():
    t = task()
    assert isinstance(t, BackgroundTask) and t.name == "slurm.poll"


def test_polls_only_hosts_with_active_jobs(services, store, fake, clock):
    _setup_hosts(store, ("a", 60), ("b", 60), ("c", 60))
    store.upsert_host(sample_host())                 # ssh host with an active-looking job
    _job(store, "a", "j-aaaaaaa1")
    _job(store, "b", "j-bbbbbbb1", state="COMPLETED")
    task().run(services)
    assert fake.polled == ["a"]


def test_no_active_jobs_means_no_poll(services, store, fake, clock):
    _setup_hosts(store, ("a", 60))
    _job(store, "a", "j-aaaaaaa1", state="COMPLETED")
    task().run(services)
    assert fake.polled == []


def test_host_with_only_unknown_state_job_is_polled(services, store, fake, clock):
    # SUSPENDED etc. map to UNKNOWN: not terminal, so the manager keeps tracking the job
    _setup_hosts(store, ("a", 60), ("b", 60))
    _job(store, "a", "j-aaaaaaa1", state="UNKNOWN")
    _job(store, "b", "j-bbbbbbb1", state="FAILED")
    assert store.list_jobs(None, True) == []         # the store's active filter misses it
    task().run(services)
    assert fake.polled == ["a"]


def test_poll_interval_error_for_one_host_does_not_hide_others(services, store, fake, clock):
    _setup_hosts(store, ("a", 60), ("b", 60))
    _job(store, "a", "j-aaaaaaa1")
    _job(store, "b", "j-bbbbbbb1")
    real = fake.poll_interval
    fake.poll_interval = lambda h: (_ for _ in ()).throw(RuntimeError("x")) if h == "a" else real(h)
    task().run(services)
    assert sorted(fake.polled) == ["a", "b"]


def test_open_jobs_are_polled_every_10_seconds_whatever_poll_seconds_says(
        services, store, fake, clock):
    _setup_hosts(store, ("a", 60), ("fast", 5))
    _job(store, "a", "j-aaaaaaa1")
    _job(store, "fast", "j-ffffffff")
    task().run(services)
    assert sorted(fake.polled) == ["a", "fast"]
    clock.t += 5
    task().run(services)
    assert len(fake.polled) == 2                     # nothing due yet
    clock.t += 5                                     # 10 s: both due
    task().run(services)
    assert sorted(fake.polled) == ["a", "a", "fast", "fast"]
    clock.t += 10
    task().run(services)
    assert len(fake.polled) == 6


def test_polling_stops_once_nothing_is_open(services, store, fake, clock):
    _setup_hosts(store, ("a", 60))
    _job(store, "a", "j-aaaaaaa1")
    task().run(services)
    store.upsert_job(sample_job(id="j-aaaaaaa1", host_id="a", state="FAILED"))
    clock.t += 30
    task().run(services)
    assert fake.polled == ["a"]


def test_one_poll_per_host_per_cycle(services, store, fake, clock):
    _setup_hosts(store, ("a", 60))
    _job(store, "a", "j-aaaaaaa1")
    _job(store, "a", "j-aaaaaaa2")
    task().run(services)
    task().run(services)
    assert fake.polled == ["a"]


def test_idle_interval_is_min_over_hosts_never_below_30(services, store, clock):
    t = task()
    assert t.interval_s(services) == 60.0            # no hosts
    _setup_hosts(store, ("a", 120), ("b", 45))
    assert t.interval_s(services) == 45.0
    _setup_hosts(store, ("c", 5))
    assert t.interval_s(services) == 30.0
    _setup_hosts(store, ("d", 0))
    assert t.interval_s(services) == 30.0


def test_tick_is_10_seconds_or_less_while_a_job_is_open(services, store, clock):
    t = task()
    _setup_hosts(store, ("a", 120))
    assert t.interval_s(services) == 120.0
    _job(store, "a", "j-aaaaaaa1")
    assert t.interval_s(services) <= 10.0
    store.upsert_job(sample_job(id="j-aaaaaaa1", host_id="a", state="COMPLETED"))
    assert t.interval_s(services) == 120.0


def test_failure_in_one_host_does_not_stop_others(services, store, fake, clock):
    _setup_hosts(store, ("a", 60), ("b", 60), ("c", 60))
    for h in "abc":
        _job(store, h, f"j-{h * 8}")
    fake.poll_errors = {"a": RuntimeError("ssh died"), "b": ApiError(503, "host_offline", "down")}
    task().run(services)                             # must not raise
    assert sorted(fake.polled) == ["a", "b", "c"]
    assert slurm_api._state.inflight == {}


def test_overlapping_poll_for_same_host_is_skipped(services, store, fake, clock):
    _setup_hosts(store, ("a", 60))
    _job(store, "a", "j-aaaaaaa1")
    slurm_api._state.inflight["a"] = threading.Event()   # a previous poll is still running
    task().run(services)
    assert fake.polled == []


def test_slow_poll_does_not_block_the_tick(services, store, fake, clock, monkeypatch):
    monkeypatch.setattr(slurm_api, "RUN_BUDGET_S", 0.2)
    gate = threading.Event()
    fake.poll = lambda host_id: gate.wait(5) and []
    _setup_hosts(store, ("a", 60))
    _job(store, "a", "j-aaaaaaa1")
    task().run(services)                             # returns after the budget
    assert "a" in slurm_api._state.inflight
    clock.t += 100
    task().run(services)                             # still in flight: skipped, not stacked
    gate.set()


def test_manager_unavailable_is_quiet(store, clock):
    from cloud_compute_core.interfaces import Services
    _setup_hosts(store, ("a", 60))
    _job(store, "a", "j-aaaaaaa1")
    task().run(Services(store=store))                # slurm service missing -> no raise


# --------------------------------------------------------------------------- registry


def test_registry_picks_up_module():
    names = {(r.method, r.path) for r in registry.collect_routes()}
    assert ("POST", "/jobs") in names and ("GET", "/hosts/{host_id}/partitions") in names
    assert {"slurm_submit", "slurm_jobs"} <= {t.name for t in registry.collect_tools()}
    assert "slurm.poll" in {b.name for b in registry.collect_background()}
    assert "slurm_api" not in registry.import_errors()


# --------------------------------------------------------------------------- on-demand refresh


def _open(store, fake, host="a", jid="j-aaaaaaa1", state="RUNNING"):
    _setup_hosts(store, (host, 60))
    _job(store, host, jid, state)
    fake.jobs = {j.id: j for j in store.list_jobs(None)}
    return jid


def test_list_jobs_refreshes_and_returns_post_poll_state(services, store, fake, clock):
    jid = _open(store, fake)

    def poll(host_id):
        fake.polled.append(host_id)
        fake.jobs[jid] = sample_job(id=jid, host_id="a", state="COMPLETED")
        return []
    fake.poll = poll
    out = route("GET", "/jobs")(req(services))
    assert fake.polled == ["a"]
    assert [j["state"] for j in out] == ["COMPLETED"]
    assert all("stale" not in j for j in out)


def test_get_job_refreshes_and_returns_post_poll_state(services, store, fake, clock):
    jid = _open(store, fake)

    def poll(host_id):
        fake.polled.append(host_id)
        fake.jobs[jid] = sample_job(id=jid, host_id="a", state="COMPLETED")
        return []
    fake.poll = poll
    out = route("GET", "/jobs/{job_id}")(req(services, path_params={"job_id": jid}))
    assert fake.polled == ["a"] and out["state"] == "COMPLETED" and "stale" not in out


def test_fresh_host_is_not_refreshed_again_within_5_seconds(services, store, fake, clock):
    jid = _open(store, fake)
    route("GET", "/jobs")(req(services))
    clock.t += 4
    route("GET", "/jobs")(req(services))
    route("GET", "/jobs/{job_id}")(req(services, path_params={"job_id": jid}))
    assert fake.polled == ["a"]                      # rate limited
    clock.t += 2                                     # 6 s since the last poll started
    route("GET", "/jobs")(req(services))
    assert fake.polled == ["a", "a"]


def test_background_poll_counts_as_fresh_for_requests(services, store, fake, clock):
    _open(store, fake)
    task().run(services)
    clock.t += 3
    route("GET", "/jobs")(req(services))
    assert fake.polled == ["a"]


def test_terminal_jobs_and_no_jobs_never_trigger_a_refresh(services, store, fake, clock):
    jid = _open(store, fake, state="COMPLETED")
    route("GET", "/jobs")(req(services))
    route("GET", "/jobs/{job_id}")(req(services, path_params={"job_id": jid}))
    fake.jobs = {}
    route("GET", "/jobs")(req(services))
    assert fake.polled == []


def test_slow_refresh_returns_cached_data_marked_stale(services, store, fake, clock, monkeypatch):
    monkeypatch.setattr(slurm_api, "ON_DEMAND_BUDGET_S", 0.2)
    jid = _open(store, fake)
    gate = threading.Event()
    fake.poll = lambda host_id: gate.wait(5) and []
    out = route("GET", "/jobs")(req(services))
    assert [j["state"] for j in out] == ["RUNNING"] and out[0]["stale"] is True
    one = route("GET", "/jobs/{job_id}")(req(services, path_params={"job_id": jid}))
    assert one["stale"] is True                      # joins the poll in flight, times out too
    gate.set()


def test_failed_refresh_is_stale_and_does_not_raise(services, store, fake, clock):
    _open(store, fake)
    fake.poll_errors = {"a": ApiError(503, "host_offline", "down")}
    out = route("GET", "/jobs")(req(services))
    assert out[0]["stale"] is True and out[0]["state"] == "RUNNING"
    fake.poll_errors = {}
    clock.t += 10
    out = route("GET", "/jobs")(req(services))
    assert "stale" not in out[0]


def test_stale_flag_only_on_open_jobs_of_the_stale_host(services, store, fake, clock, monkeypatch):
    monkeypatch.setattr(slurm_api, "ON_DEMAND_BUDGET_S", 0.2)
    _setup_hosts(store, ("a", 60), ("b", 60))
    _job(store, "a", "j-aaaaaaa1")
    _job(store, "b", "j-bbbbbbb1")
    _job(store, "a", "j-aaaaaaa2", state="COMPLETED")
    fake.jobs = {j.id: j for j in store.list_jobs(None)}
    gate = threading.Event()
    fake.poll = lambda host_id: (gate.wait(5) and []) if host_id == "a" else []
    out = {j["id"]: j for j in route("GET", "/jobs")(req(services))}
    assert out["j-aaaaaaa1"].get("stale") is True
    assert "stale" not in out["j-bbbbbbb1"] and "stale" not in out["j-aaaaaaa2"]
    gate.set()


def test_request_never_blocks_longer_than_the_budget(services, store, fake, clock, monkeypatch):
    import time as _time
    monkeypatch.setattr(slurm_api, "ON_DEMAND_BUDGET_S", 0.3)
    _setup_hosts(store, ("a", 60), ("b", 60), ("c", 60))
    for h in "abc":
        _job(store, h, f"j-{h * 8}")
    fake.jobs = {j.id: j for j in store.list_jobs(None)}
    gate = threading.Event()
    fake.poll = lambda host_id: gate.wait(5) and []
    began = _time.monotonic()
    out = route("GET", "/jobs")(req(services))
    assert _time.monotonic() - began < 1.5           # one shared deadline, not 3 x budget
    assert all(j["stale"] for j in out)
    gate.set()


def test_budget_default_is_under_8_seconds():
    assert slurm_api.ON_DEMAND_BUDGET_S < 8 and slurm_api.RUN_BUDGET_S <= 8
    assert slurm_api.OPEN_POLL_S <= 10 and slurm_api.STALE_S <= 5


def test_list_and_status_tools_refresh_too(services, store, fake, clock):
    jid = _open(store, fake)
    out = tool("slurm_jobs").handler({"action": "list"}, services, {})
    assert fake.polled == ["a"] and "jobs" in out
    clock.t += 6
    out = tool("slurm_jobs").handler({"action": "status", "job_id": jid}, services, {})
    assert fake.polled == ["a", "a"] and out["job"]["id"] == jid


def test_status_tool_marks_stale(services, store, fake, clock):
    jid = _open(store, fake)
    fake.poll_errors = {"a": RuntimeError("ssh died")}
    out = tool("slurm_jobs").handler({"action": "status", "job_id": jid}, services, {})
    assert out["job"]["stale"] is True


def test_poll_in_flight_is_shared_not_duplicated(services, store, fake, clock):
    _open(store, fake)
    gate = threading.Event()
    started = threading.Event()

    def poll(host_id):
        fake.polled.append(host_id)
        started.set()
        gate.wait(5)
        return []
    fake.poll = poll
    results = []
    t1 = threading.Thread(target=lambda: results.append(route("GET", "/jobs")(req(services))))
    t1.start()
    assert started.wait(2)
    t2 = threading.Thread(target=lambda: results.append(route("GET", "/jobs")(req(services))))
    t2.start()
    gate.set()
    t1.join(5)
    t2.join(5)
    assert fake.polled == ["a"] and len(results) == 2


# --------------------------------------------------------------------------- real manager, fake transport


def _e2e(tmp_path):
    """The real SlurmManager behind the API, fake transport/events/clock."""
    from cloud_compute_core.testing import exec_result
    from cloud_compute_core.slurm import TIME_ENV
    services = make_services(store=Store(tmp_path / "e2e.db"))
    services.store.upsert_host(sample_slurm_host())
    services.store.upsert_job(sample_job(id="j-00000001", slurm_job_id="5", state="RUNNING"))
    t = services.transports.get("slurm-x1")
    t.script(lambda argv, stdin: argv[:3] == ["env", TIME_ENV, "squeue"], exec_result(stdout=""))
    return services, t, exec_result, TIME_ENV


FINISHED = "5|COMPLETED|0:0|1762586987|1762586988|c1\n"
RUNNING = "5|RUNNING|0:0|1762586987|Unknown|c1\n"


def test_finished_job_is_reported_by_the_next_read_not_the_next_tick(tmp_path, clock):
    services, t, exec_result, env = _e2e(tmp_path)
    sacct = lambda argv, stdin: argv[:3] == ["env", env, "sacct"]
    t.script(sacct, exec_result(stdout=RUNNING))
    (job,) = route("GET", "/jobs")(req(services))
    assert job["state"] == "RUNNING"
    t._run_rules.clear()                                     # the job ends: new answers
    t.script(lambda argv, stdin: argv[:3] == ["env", env, "squeue"], exec_result(stdout=""))
    t.script(sacct, exec_result(stdout=FINISHED))
    clock.t += 36                                            # the 36 s that showed PENDING
    (job,) = route("GET", "/jobs")(req(services))
    assert job["state"] == "COMPLETED" and "stale" not in job
    assert route("GET", "/jobs/{job_id}")(
        req(services, path_params={"job_id": "j-00000001"}))["state"] == "COMPLETED"


def test_job_finished_is_emitted_exactly_once_across_task_and_requests(tmp_path, clock):
    services, t, exec_result, env = _e2e(tmp_path)
    t.script(lambda argv, stdin: argv[:3] == ["env", env, "sacct"], exec_result(stdout=FINISHED))
    clock.t += 100
    route("GET", "/jobs")(req(services))
    for _ in range(3):
        clock.t += 10
        task().run(services)
        route("GET", "/jobs")(req(services))
        route("GET", "/jobs/{job_id}")(req(services, path_params={"job_id": "j-00000001"}))
    assert len(services.events.payloads("job.finished")) == 1
    assert len(services.events.payloads("notify")) == 1


def test_background_task_alone_finishes_the_job_within_one_tick(tmp_path, clock):
    services, t, exec_result, env = _e2e(tmp_path)
    t.script(lambda argv, stdin: argv[:3] == ["env", env, "sacct"], exec_result(stdout=FINISHED))
    task().run(services)
    assert services.store.get_job("j-00000001").state == "COMPLETED"
    assert len(services.events.payloads("job.finished")) == 1
    task().run(services)
    assert len(services.events.payloads("job.finished")) == 1


def test_unreachable_host_serves_cache_flagged_stale(tmp_path, clock):
    services, t, exec_result, env = _e2e(tmp_path)
    t._run_rules.clear()
    t.script(lambda argv, stdin: argv[:3] == ["env", env, "squeue"],
             exec_result(rc=255, stderr="ssh: connect failed"))
    (job,) = route("GET", "/jobs")(req(services))
    assert job["state"] == "RUNNING" and job["stale"] is True


# --------------------------------------------------------------------------- delete, wait, strict params


def test_submit_route_unknown_field_is_unknown_parameter(services, fake):
    with pytest.raises(ApiError) as e:
        route("POST", "/jobs")(req(services, body={**SUBMIT, "bogus": 1}))
    assert (e.value.status, e.value.code) == (400, "unknown_parameter")
    assert "bogus" in e.value.message and "time_limit" in e.value.message
    assert not [c for c in fake.calls if c[0] == "submit"]


def test_submit_tool_rejects_unknown_parameter_naming_it_and_the_valid_ones(services, fake):
    out = tool("slurm_submit").handler({**SUBMIT, "nodes": 2, "walltime": "1:00"}, services, {})
    err = out["error"]
    assert err["code"] == "unknown_parameter"
    assert "nodes" in err["message"] and "walltime" in err["message"]
    for name in ("host_id", "name", "command", "script", "image", "array"):
        assert name in err["message"]
    assert not [c for c in fake.calls if c[0] == "submit"]


def test_jobs_tool_rejects_unknown_parameter(services, fake):
    out = tool("slurm_jobs").handler({"action": "status", "job_id": "j-1a2b3c4d",
                                      "name": "hello"}, services, {})
    assert out["error"]["code"] == "unknown_parameter"
    assert "name" in out["error"]["message"] and "wait_s" in out["error"]["message"]
    assert not [c for c in fake.calls if c[0] == "get"]


def test_jobs_tool_list_is_not_returned_for_a_misspelled_job_id(services, fake):
    out = tool("slurm_jobs").handler({"action": "status", "jobid": "j-1a2b3c4d"}, services, {})
    assert out["error"]["code"] == "unknown_parameter" and "jobs" not in out


def test_delete_route_and_clear_finished_route(services, fake):
    out = route("DELETE", "/jobs/{job_id}")(req(services, {"job_id": "j-1a2b3c4d"}))
    assert out["deleted"] == "j-1a2b3c4d" and out["job_dir"]
    assert fake.calls[-1] == ("delete", "j-1a2b3c4d")
    out = route("POST", "/jobs/clear-finished")(req(services, body={"host_id": "slurm-x1"}))
    assert out["count"] == 0 and fake.calls[-1] == ("clear_finished", "slurm-x1")
    route("POST", "/jobs/clear-finished")(req(services))
    assert fake.calls[-1] == ("clear_finished", None)


def test_delete_tool_needs_no_approval(services, store, fake):
    out = tool("slurm_jobs").handler({"action": "delete", "job_id": "j-1a2b3c4d"}, services, {})
    assert out["deleted"] == "j-1a2b3c4d"
    assert tool("slurm_jobs").approval({"action": "delete", "job_id": "j-1a2b3c4d"},
                                       services) is None
    assert approval_for("slurm_jobs", {"action": "delete", "job_id": "j-1"}, tool("slurm_jobs"),
                        services) is None
    assert approval_for("slurm_jobs", {"action": "wait", "job_id": "j-1"}, tool("slurm_jobs"),
                        services) is None


def test_hermes_tool_call_limit_is_respected():
    assert slurm_api.WAIT_HARD_CAP_S < slurm_api.HERMES_TOOL_CALL_LIMIT_S - 30


@pytest.fixture
def fake_time(monkeypatch, clock):
    """Drive wait() with the poll clock: sleeping advances time instantly."""
    monkeypatch.setattr(slurm_api, "_monotonic", lambda: clock.t)

    def sleep(seconds):
        clock.t += max(seconds, 0.01)

    monkeypatch.setattr(slurm_api, "_sleep", sleep)
    return clock


OUT_OK = "@@cc dir=1 files=1 nonempty=1\nc1\n"


def _wait_env(tmp_path, sacct_results):
    services, t, exec_result, env = _e2e(tmp_path)
    t.script(lambda argv, stdin: argv[:3] == ["env", env, "sacct"],
             [exec_result(stdout=x) for x in sacct_results])
    t.script(lambda argv, stdin: argv[:2] == ["sh", "-c"] and "@@cc" in argv[2],
             exec_result(stdout=OUT_OK))
    return services, t, env


def test_wait_returns_when_the_job_finishes_with_its_output(tmp_path, fake_time):
    services, t, env = _wait_env(tmp_path, [RUNNING] * 3 + [FINISHED])
    out = tool("slurm_jobs").handler({"action": "wait", "job_id": "j-00000001", "wait_s": 120},
                                     services, {})
    assert out["finished"] is True and out["job"]["state"] == "COMPLETED"
    assert out["output"] == {"text": "c1\n", "status": "ok", "note": None}
    assert out["wait_s"] == 120 and out["waited_s"] < 120 and "note" not in out
    polls = sum(1 for a in t.argvs() if a[:3] == ["env", env, "squeue"])
    assert polls == 4
    # polite: polls are at least OPEN_POLL_S - 1 apart in poll-clock time
    assert out["waited_s"] >= 3 * (slurm_api.OPEN_POLL_S - 1)


def test_wait_times_out_with_a_note(tmp_path, fake_time):
    services, t, env = _wait_env(tmp_path, [RUNNING])
    out = tool("slurm_jobs").handler({"action": "wait", "job_id": "j-00000001", "wait_s": 30},
                                     services, {})
    assert out["finished"] is False and out["job"]["state"] == "RUNNING"
    assert "still RUNNING" in out["note"] and out["waited_s"] >= 30
    polls = sum(1 for a in t.argvs() if a[:3] == ["env", env, "squeue"])
    assert polls <= 4                                   # 30 s at one poll per ~10 s


class _Cfg:
    def __init__(self, wait_max_seconds):
        self.wait_max_seconds = wait_max_seconds


def test_wait_cap_comes_from_wait_max_seconds_and_the_hard_cap(tmp_path, fake_time, monkeypatch):
    services, t, env = _wait_env(tmp_path, [FINISHED])
    cfg = _Cfg(45)
    monkeypatch.setattr(slurm_api, "_wait_cap", lambda s, real=slurm_api._wait_cap:
                        real(type("S", (), {"config": cfg})()))
    out = tool("slurm_jobs").handler({"action": "wait", "job_id": "j-00000001", "wait_s": 9999},
                                     services, {})
    assert out["wait_s"] == 45 and "capped at 45 s" in out["note"]
    cfg.wait_max_seconds = 100000
    assert slurm_api._wait_cap(services) == slurm_api.WAIT_HARD_CAP_S


def test_wait_cap_defaults_to_300_without_the_setting():
    class Cfg:
        pass
    from cloud_compute_core.interfaces import Services
    assert slurm_api._wait_cap(Services(config=Cfg())) == 300


def test_wait_rejects_bad_wait_s(services, fake):
    for bad in (0, -5, "soon", True):
        out = tool("slurm_jobs").handler({"action": "wait", "job_id": "j-1a2b3c4d",
                                          "wait_s": bad}, services, {})
        assert out["error"]["code"] == "invalid", bad
    out = tool("slurm_jobs").handler({"action": "wait"}, services, {})
    assert out["error"]["code"] == "invalid"


def test_wait_on_a_finished_job_returns_at_once(tmp_path, fake_time):
    services, t, env = _wait_env(tmp_path, [FINISHED])
    rec = services.store.get_job("j-00000001")
    rec.state, rec.ended_at = "FAILED", "2026-11-08T12:05:00Z"
    services.store.upsert_job(rec)
    out = tool("slurm_jobs").handler({"action": "wait", "job_id": "j-00000001"}, services, {})
    assert out["finished"] is True and out["waited_s"] == 0
    assert "error_output" in out                         # not COMPLETED: stderr comes too
    assert not [a for a in t.argvs() if a[:3] == ["env", env, "squeue"]]


def test_every_single_job_tool_result_carries_a_card(services, fake):
    card = '::cc-job{id="j-1a2b3c4d"}'
    out = tool("slurm_submit").handler(SUBMIT, services, {})
    assert out["card"] == '::cc-job{id="j-22222222"}'
    h = tool("slurm_jobs").handler
    for action in ("status", "output", "cancel", "delete"):
        assert h({"action": action, "job_id": "j-1a2b3c4d"}, services, {})["card"] == card, action
    assert "card" not in h({}, services, {})                       # list: many jobs
    assert "card" not in h({"action": "status", "job_id": "zzz"}, services, {})  # error


def test_wait_result_carries_a_card(tmp_path, fake_time):
    services, t, env = _wait_env(tmp_path, [FINISHED])
    out = tool("slurm_jobs").handler({"action": "wait", "job_id": "j-00000001", "wait_s": 30},
                                     services, {})
    assert out["card"] == '::cc-job{id="j-00000001"}'
