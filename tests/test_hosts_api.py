"""Hosts routes, compute_status tool and host background tasks."""

from __future__ import annotations

import threading
import time

import pytest

from cloud_compute_core import hosts_api, registry
from cloud_compute_core.config import Config
from cloud_compute_core.interfaces import ApiError, ApiRequest, Services
from cloud_compute_core.models import HostState
from cloud_compute_core.store import Store
from cloud_compute_core.testing import (
    FakeEvents, make_services, sample_capabilities, sample_host, sample_host_state,
    sample_instance, sample_job, sample_local_host, sample_slurm_host,
)


class FakeProber:
    def __init__(self, store=None):
        self.store = store
        self.probed: list[str] = []
        self.live: list[str] = []
        self.tested: list = []
        self.fail = False
        self.gate: threading.Event | None = None
        self.lock = threading.Lock()

    def probe(self, host):
        if self.gate is not None:
            self.gate.wait(10)
        with self.lock:
            self.probed.append(host.id)
        if self.fail:
            raise RuntimeError("boom")
        state = sample_host_state(host_id=host.id)
        if self.store is not None:
            self.store.set_host_state(state)
        return state

    def liveness(self, host):
        with self.lock:
            self.live.append(host.id)
        return "online"

    def test_connection(self, spec):
        self.tested.append(spec)

        class R:
            def to_dict(self_inner):
                return {"ok": True, "status": "online", "steps": [], "capabilities": None}
        return R()

    def refresh_all(self, deep):
        return []


@pytest.fixture(autouse=True)
def _reset_state():
    hosts_api._probing.clear()
    hosts_api._checking.clear()
    hosts_api._last_probe_start.clear()
    registry.clear_errors()
    yield
    hosts_api._probing.clear()
    hosts_api._checking.clear()
    hosts_api._last_probe_start.clear()


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "hosts.db")
    prober = FakeProber(store)
    events = FakeEvents()
    services = make_services(store=store, prober=prober, events=events, config=Config())
    yield services, store, prober, events
    store.close()


def call(handler, services, body=None, query=None, **params):
    return handler(ApiRequest(path_params=params, query=query or {}, body=body, services=services))


def body(**kw):
    return {"name": "Dev Box", "kind": "ssh", "address": "devbox.local", "user": "dev", **kw}


# --------------------------------------------------------------------------- create / list / get


def test_create_slugifies_id_saves_emits_and_probes(env):
    services, store, prober, events = env
    out = call(hosts_api.create_host, services, body())
    assert out["spec"]["id"] == "dev-box"
    assert out["spec"]["created_at"]
    assert out["state"]["host_id"] == "dev-box" and out["state"]["status"] == "online"
    assert store.get_host("dev-box") is not None
    assert events.payloads("hosts.changed") == [{"ids": ["dev-box"], "reason": "created"}]
    assert prober.probed == ["dev-box"]


def test_create_with_explicit_id_and_defaults(env):
    services, store, *_ = env
    out = call(hosts_api.create_host, services, body(id="box1", limits={"max_instances": 2}))
    assert out["spec"]["id"] == "box1" and out["spec"]["limits"]["max_instances"] == 2
    assert out["spec"]["port"] == 22 and out["spec"]["runtime_pref"] == "auto"


def test_create_slurm_gets_default_settings(env):
    services, *_ = env
    out = call(hosts_api.create_host, services, body(kind="slurm", name="hpc"))
    assert out["spec"]["slurm"]["workdir"] == "~/cloud-compute"


def test_create_local_defaults_address(env):
    services, *_ = env
    out = call(hosts_api.create_host, services, {"name": "This computer", "kind": "local"})
    assert out["spec"]["address"] == "localhost"


def test_create_conflict(env):
    services, *_ = env
    call(hosts_api.create_host, services, body())
    with pytest.raises(ApiError) as e:
        call(hosts_api.create_host, services, body())
    assert e.value.status == 409 and e.value.code == "conflict"


@pytest.mark.parametrize("bad", [
    {"kind": "nope"},
    {"name": ""},
    {"address": "bad host;rm"},
    {"address": ""},
    {"user": "a b"},
    {"jump_host": "x\ny"},
    {"port": 0},
    {"port": "22"},
    {"port": True},
    {"id": "Bad_ID"},
    {"runtime_pref": "lxc"},
    {"exec_prefix": "podman"},
    {"exec_prefix": ["ok", ""]},
    {"interactive_prefix": ["a\nb"]},
    {"limits": {"max_instances": -1}},
    {"limits": "x"},
    {"slurm": {"poll_seconds": 5}},
])
def test_create_invalid(env, bad):
    services, store, prober, events = env
    with pytest.raises(ApiError) as e:
        call(hosts_api.create_host, services, body(**bad))
    assert e.value.status == 400 and e.value.code == "invalid"
    assert store.list_hosts() == [] and events.events == []


def test_create_non_object_body(env):
    services, *_ = env
    with pytest.raises(ApiError) as e:
        call(hosts_api.create_host, services, None)
    assert e.value.status == 400


def test_create_survives_probe_failure(env):
    services, store, prober, events = env
    prober.fail = True
    out = call(hosts_api.create_host, services, body())
    assert out["state"] is None and store.get_host("dev-box") is not None


def test_create_without_prober_module(tmp_path):
    store = Store(tmp_path / "x.db")
    services = Services(store=store, events=FakeEvents())   # no prober -> 503
    try:
        out = call(hosts_api.create_host, services, body())
        assert out["state"] is None and store.get_host("dev-box") is not None
        with pytest.raises(ApiError) as e:
            call(hosts_api.probe_host, services, host_id="dev-box")
        assert e.value.status == 503
        with pytest.raises(ApiError) as e:
            call(hosts_api.test_host, services, body())
        assert e.value.status == 503
    finally:
        store.close()


def test_list_hosts_with_state_and_kind_filter(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    store.upsert_host(sample_slurm_host())
    store.set_host_state(sample_host_state())
    out = call(hosts_api.list_hosts, services)
    by_id = {e["spec"]["id"]: e for e in out}
    assert set(by_id) == {"x1carbon", "slurm-x1"}
    assert by_id["x1carbon"]["state"]["status"] == "online"
    assert by_id["slurm-x1"]["state"] is None
    only = call(hosts_api.list_hosts, services, query={"kind": "slurm"})
    assert [e["spec"]["id"] for e in only] == ["slurm-x1"]
    with pytest.raises(ApiError) as e:
        call(hosts_api.list_hosts, services, query={"kind": "zzz"})
    assert e.value.status == 400


def test_get_host(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    out = call(hosts_api.get_host, services, host_id="x1carbon")
    assert out["spec"]["id"] == "x1carbon" and out["state"] is None
    with pytest.raises(ApiError) as e:
        call(hosts_api.get_host, services, host_id="nope")
    assert e.value.status == 404 and e.value.code == "not_found"


# --------------------------------------------------------------------------- test


def test_test_route_does_not_write_store(env):
    services, store, prober, events = env
    out = call(hosts_api.test_host, services, body())
    assert out["ok"] is True and out["status"] == "online"
    assert store.list_hosts() == [] and events.events == []
    assert prober.tested and prober.tested[0].address == "devbox.local"
    assert prober.probed == []


def test_test_route_validates(env):
    services, *_ = env
    with pytest.raises(ApiError) as e:
        call(hosts_api.test_host, services, body(address="a b"))
    assert e.value.status == 400


# --------------------------------------------------------------------------- patch


def test_patch_merges_partial_and_emits(env):
    services, store, prober, events = env
    store.upsert_host(sample_slurm_host())
    out = call(hosts_api.patch_host, services,
               {"name": "Renamed", "limits": {"max_instances": 9}, "slurm": {"account": "lab"}},
               host_id="slurm-x1")
    spec = out["spec"]
    assert spec["name"] == "Renamed" and spec["limits"]["max_instances"] == 9
    assert spec["limits"]["allow_agents"] is False
    assert spec["slurm"]["account"] == "lab" and spec["slurm"]["workdir"] == "/data/cloud-compute"
    assert spec["exec_prefix"][0] == "podman" and spec["address"] == "x1carbon"
    assert spec["created_at"] == "2026-11-08T12:00:00Z"
    assert store.get_host("slurm-x1").name == "Renamed"
    assert events.payloads("hosts.changed") == [{"ids": ["slurm-x1"], "reason": "updated"}]


def test_patch_same_id_and_kind_allowed_changed_rejected(env):
    services, store, _, events = env
    store.upsert_host(sample_host())
    call(hosts_api.patch_host, services, {"id": "x1carbon", "kind": "ssh", "port": 2222},
         host_id="x1carbon")
    assert store.get_host("x1carbon").port == 2222
    for bad in ({"id": "other"}, {"kind": "local"}):
        with pytest.raises(ApiError) as e:
            call(hosts_api.patch_host, services, bad, host_id="x1carbon")
        assert e.value.status == 400
    assert len(events.payloads("hosts.changed")) == 1


def test_patch_invalid_and_unknown(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    with pytest.raises(ApiError) as e:
        call(hosts_api.patch_host, services, {"address": "bad addr"}, host_id="x1carbon")
    assert e.value.status == 400
    assert store.get_host("x1carbon").address == "x1carbon"
    with pytest.raises(ApiError) as e:
        call(hosts_api.patch_host, services, {"name": "x"}, host_id="ghost")
    assert e.value.status == 404
    with pytest.raises(ApiError) as e:
        call(hosts_api.patch_host, services, None, host_id="x1carbon")
    assert e.value.status == 400


# --------------------------------------------------------------------------- delete


def test_delete_removes_host_state_and_emits(env):
    services, store, _, events = env
    store.upsert_host(sample_host())
    store.set_host_state(sample_host_state())
    store.upsert_instance(sample_instance(status="destroyed"))
    store.upsert_job(sample_job(id="j-done0000", host_id="x1carbon", state="COMPLETED"))
    store.upsert_job(sample_job(id="j-lost0000", host_id="x1carbon", state="UNKNOWN",
                                ended_at="2026-11-08T12:00:00Z"))
    assert call(hosts_api.delete_host, services, host_id="x1carbon") == {"deleted": True}
    assert store.get_host("x1carbon") is None and store.get_host_state("x1carbon") is None
    assert events.payloads("hosts.changed") == [{"ids": ["x1carbon"], "reason": "deleted"}]


@pytest.mark.parametrize("status", ["running", "creating"])
def test_delete_conflict_with_live_instances(env, status):
    services, store, _, events = env
    store.upsert_host(sample_host())
    store.upsert_instance(sample_instance(status=status))
    with pytest.raises(ApiError) as e:
        call(hosts_api.delete_host, services, host_id="x1carbon")
    assert e.value.status == 409 and e.value.code == "conflict"
    assert store.get_host("x1carbon") is not None and events.events == []


def test_delete_unknown(env):
    services, *_ = env
    with pytest.raises(ApiError) as e:
        call(hosts_api.delete_host, services, host_id="ghost")
    assert e.value.status == 404


# --------------------------------------------------------------------------- probe


def test_probe_route_returns_state_dict(env):
    services, store, prober, _ = env
    store.upsert_host(sample_host())
    out = call(hosts_api.probe_host, services, host_id="x1carbon")
    assert out["host_id"] == "x1carbon" and out["status"] == "online"
    assert "capabilities" in out and prober.probed == ["x1carbon"]
    with pytest.raises(ApiError) as e:
        call(hosts_api.probe_host, services, host_id="ghost")
    assert e.value.status == 404


# --------------------------------------------------------------------------- routes / registry


def test_routes_declared_and_registered():
    got = {(r.method, r.path) for r in hosts_api.routes()}
    assert got == {("GET", "/hosts"), ("POST", "/hosts"), ("POST", "/hosts/test"),
                   ("GET", "/hosts/{host_id}"), ("PATCH", "/hosts/{host_id}"),
                   ("DELETE", "/hosts/{host_id}"), ("POST", "/hosts/{host_id}/probe")}
    collected = [(r.method, r.path) for r in registry.collect_routes()]
    assert got <= set(collected)
    assert len(collected) == len(set(collected))
    assert registry.import_errors().get("hosts_api") is None


def test_registry_collects_tool_and_tasks():
    assert "compute_status" in [t.name for t in registry.collect_tools()]
    names = [b.name for b in registry.collect_background()]
    assert "hosts.liveness" in names and "hosts.probe" in names


# --------------------------------------------------------------------------- compute_status


def test_tool_spec():
    spec, host_spec = hosts_api.tools()
    assert host_spec.name == "compute_host" and host_spec.toolset == "cloud_compute"
    assert spec.name == "compute_status" and spec.toolset == "cloud_compute"
    assert spec.approval is None and spec.handler is hosts_api.compute_status
    props = spec.schema["parameters"]["properties"]
    assert props["host_id"]["type"] == "string"
    assert props["refresh"]["type"] == "boolean" and props["refresh"]["default"] is False
    assert "host ids" in spec.schema["description"]


def test_compute_host_tool_schema_and_approval_hook():
    spec = hosts_api.tools()[1]
    params = spec.schema["parameters"]
    assert params["required"] == ["action"]
    assert params["properties"]["action"]["enum"] == ["types", "list", "test", "add", "update", "remove",
                                                      "start", "stop", "terminate", "extend"]
    assert set(params["properties"]) == {
        "action", "host_id", "kind", "name", "address", "user", "port", "identity_file", "jump_host",
        "workdir", "partition", "account", "qos", "allow_agents", "max_instances",
        "minutes", "delete_environments", "save", "region", "instance_type", "disk_gb",
        "max_lifetime_minutes", "idle_stop_minutes", "auto_start", "ssh_cidr",
        "access_key_secret", "secret_key_secret"}
    assert spec.approval is hosts_api.describe_compute_host
    assert spec.handler({"action": "types"}, None, {})["types"]      # real, not the old stub
    assert spec.handler(None, None, {})["error"]["code"] == "invalid"


def test_compute_status_shape(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    store.upsert_host(sample_slurm_host())
    store.set_host_state(sample_host_state())
    store.upsert_instance(sample_instance())
    store.upsert_job(sample_job())
    store.upsert_job(sample_job(id="j-done0000", state="COMPLETED"))
    out = hosts_api.compute_status({}, services, {})
    hosts = {h["id"]: h for h in out["hosts"]}
    assert set(hosts) == {"x1carbon", "slurm-x1"}
    h = hosts["x1carbon"]
    assert set(h) == {"id", "name", "kind", "status", "allow_agents", "last_seen", "capabilities",
                      "resources", "card"}
    assert h["card"] == '::cc-host{id="x1carbon"}' and h["allow_agents"] is False
    assert h["status"] == "online" and h["capabilities"]["runtime"] == "podman"
    assert h["resources"]["cpu_total"] == 8.0
    assert hosts["slurm-x1"]["status"] == "unknown" and hosts["slurm-x1"]["resources"] == {}
    assert [i["id"] for i in out["instances"]] == ["i-ab12cd34"]
    assert [j["id"] for j in out["jobs"]] == ["j-1a2b3c4d"]
    assert "warning" not in out


def test_compute_status_host_filter_and_unknown(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    store.upsert_host(sample_slurm_host())
    out = hosts_api.compute_status({"host_id": "slurm-x1"}, services, {})
    assert [h["id"] for h in out["hosts"]] == ["slurm-x1"]
    err = hosts_api.compute_status({"host_id": "ghost"}, services, {})
    assert err["error"]["code"] == "not_found"
    assert hosts_api.compute_status({"host_id": 5}, services, {})["error"]["code"] == "invalid"


def test_compute_status_refresh(env):
    services, store, prober, _ = env
    store.upsert_host(sample_host())
    store.upsert_host(sample_local_host())
    out = hosts_api.compute_status({"refresh": True}, services, {})
    assert sorted(prober.probed) == ["local", "x1carbon"]
    assert all(h["status"] == "online" for h in out["hosts"])
    prober.probed.clear()
    hosts_api.compute_status({"host_id": "local", "refresh": True}, services, {})
    assert prober.probed == ["local"]
    prober.probed.clear()
    hosts_api.compute_status({"refresh": False}, services, {})
    assert prober.probed == []


def test_compute_status_refresh_failure_and_missing_prober(env, tmp_path):
    services, store, prober, _ = env
    store.upsert_host(sample_host())
    prober.fail = True
    out = hosts_api.compute_status({"refresh": True}, services, {})
    assert out["hosts"][0]["status"] == "unknown" and "warning" in out
    bare = Services(store=store, events=FakeEvents())
    out = hosts_api.compute_status({"refresh": True}, bare, {})
    assert out["hosts"] and "warning" in out


def test_compute_status_never_raises():
    class Broken:
        @property
        def store(self):
            raise RuntimeError("db gone")
    out = hosts_api.compute_status({}, Broken(), {})
    assert out["error"]["code"] == "internal" and "db gone" in out["error"]["message"]
    out = hosts_api.compute_status(None, Broken(), None)
    assert "error" in out
    assert "error" in hosts_api.compute_status({}, Services(), {})    # no store service


# --------------------------------------------------------------------------- background tasks


def test_background_task_definitions(env):
    services, *_ = env
    services.config.liveness_seconds = 20
    services.config.poll_seconds = 45
    tasks = {t.name: t for t in hosts_api.background_tasks()}
    assert tasks["hosts.liveness"].interval_s(services) == 20
    assert tasks["hosts.probe"].interval_s(services) == 45


def test_liveness_checks_every_host_concurrently_and_quickly(env):
    services, store, prober, _ = env
    for i in range(5):
        store.upsert_host(sample_host(id=f"h{i}", name=f"h{i}"))
    start = time.monotonic()
    hosts_api.run_liveness(services)
    assert time.monotonic() - start < 3
    assert sorted(prober.live) == [f"h{i}" for i in range(5)]
    assert hosts_api._checking == set()


def test_liveness_returns_within_budget_when_prober_hangs(env, monkeypatch):
    services, store, prober, _ = env
    monkeypatch.setattr(hosts_api, "LIVENESS_BUDGET_S", 0.3)
    store.upsert_host(sample_host())
    gate = threading.Event()
    prober.liveness = lambda host: gate.wait(10)
    start = time.monotonic()
    hosts_api.run_liveness(services)
    assert time.monotonic() - start < 2
    hosts_api.run_liveness(services)          # still in flight: not dispatched again
    gate.set()


def test_liveness_without_hosts_or_prober(tmp_path):
    store = Store(tmp_path / "e.db")
    try:
        hosts_api.run_liveness(Services(store=store))        # no hosts: no prober needed
        store.upsert_host(sample_host())
        with pytest.raises(ApiError):                         # the loop records this
            hosts_api.run_liveness(Services(store=store))
    finally:
        store.close()


def _wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_probe_task_dispatches_in_background_and_returns_fast(env):
    services, store, prober, _ = env
    store.upsert_host(sample_host())
    store.upsert_host(sample_local_host())
    prober.gate = threading.Event()
    start = time.monotonic()
    hosts_api.run_probe(services)
    assert time.monotonic() - start < 1            # probes are blocked, run() still returned
    assert prober.probed == []
    prober.gate.set()
    assert _wait_for(lambda: len(prober.probed) == 2 and not hosts_api._probing)
    assert sorted(prober.probed) == ["local", "x1carbon"]


def test_probe_task_does_not_double_dispatch(env):
    services, store, prober, _ = env
    store.upsert_host(sample_host())
    prober.gate = threading.Event()
    hosts_api.run_probe(services)
    hosts_api.run_probe(services)
    hosts_api.run_probe(services)
    prober.gate.set()
    assert _wait_for(lambda: not hosts_api._probing)
    assert prober.probed == ["x1carbon"]
    hosts_api.run_probe(services)                  # finished: probes again
    assert _wait_for(lambda: len(prober.probed) == 2 and not hosts_api._probing)


def test_probe_task_survives_probe_errors(env):
    services, store, prober, _ = env
    store.upsert_host(sample_host())
    prober.fail = True
    hosts_api.run_probe(services)
    assert _wait_for(lambda: prober.probed == ["x1carbon"] and not hosts_api._probing)


def test_probe_task_rate_limits_slurm_hosts(env):
    services, store, prober, _ = env
    store.upsert_host(sample_slurm_host())
    store.upsert_host(sample_host())
    hosts_api.run_probe(services)
    assert _wait_for(lambda: len(prober.probed) == 2 and not hosts_api._probing)
    prober.probed.clear()
    hosts_api.run_probe(services)                  # immediately again: slurm skipped, ssh probed
    assert _wait_for(lambda: prober.probed == ["x1carbon"] and not hosts_api._probing)
    time.sleep(0.05)
    assert prober.probed == ["x1carbon"]
    hosts_api._last_probe_start["slurm-x1"] -= 1000
    hosts_api.run_probe(services)
    assert _wait_for(lambda: "slurm-x1" in prober.probed and not hosts_api._probing)


def test_state_roundtrip_type():
    assert isinstance(sample_host_state(), HostState)
    assert sample_capabilities().runtime == "podman"


@pytest.mark.parametrize("status", ["stopped", "exited", "error"])
def test_delete_conflict_with_any_non_destroyed_instance(env, status):
    services, store, _, events = env
    store.upsert_host(sample_host())
    store.upsert_instance(sample_instance(status=status))
    with pytest.raises(ApiError) as e:
        call(hosts_api.delete_host, services, host_id="x1carbon")
    assert e.value.status == 409 and e.value.code == "conflict"
    assert f"1 {status}" in e.value.message and "destroy" in e.value.message
    assert store.get_host("x1carbon") is not None and events.events == []


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "COMPLETING", "UNKNOWN"])
def test_delete_conflict_with_open_slurm_jobs(env, state):
    services, store, _, events = env
    store.upsert_host(sample_slurm_host())
    store.upsert_job(sample_job(state=state, ended_at=None))
    store.upsert_job(sample_job(id="j-done0000", state="COMPLETED"))
    with pytest.raises(ApiError) as e:
        call(hosts_api.delete_host, services, host_id="slurm-x1")
    assert e.value.status == 409 and "1 open Slurm job(s)" in e.value.message
    assert "cancel" in e.value.message
    assert store.get_host("slurm-x1") is not None and events.events == []


def test_delete_conflict_message_lists_both_counts(env):
    services, store, _, _ = env
    store.upsert_host(sample_host())
    store.upsert_instance(sample_instance(status="stopped"))
    store.upsert_instance(sample_instance(id="i-00000002", status="error"))
    store.upsert_job(sample_job(host_id="x1carbon"))
    with pytest.raises(ApiError) as e:
        call(hosts_api.delete_host, services, host_id="x1carbon")
    msg = e.value.message
    assert "2 environment(s)" in msg and "1 error" in msg and "1 stopped" in msg
    assert "1 open Slurm job(s)" in msg
