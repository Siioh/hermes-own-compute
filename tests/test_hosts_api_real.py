"""hosts_api against the real Store and the real Prober.

Only the transport is scripted, with captured probe output from a real Fedora box
(Podman 5.8.4, rootless) and a Slurm login container (Slurm 26.05.5, partitions cpu/gpu).
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from cloud_compute_core import background as real_background
from cloud_compute_core import hosts_api, registry
from cloud_compute_core import prober as real_prober
from cloud_compute_core.config import Config
from cloud_compute_core.interfaces import ApiRequest
from cloud_compute_core.store import Store
from cloud_compute_core.testing import (
    FakeEvents, FakeTransportFactory, exec_result, make_services, sample_host,
    sample_host_state,
)

_FIX = Path(__file__).parent / "fixtures" / "hosts"
_SLURM_PREFIX = ["podman", "exec", "-i", "-w", "/data", "slurmctld"]
_X1 = {"name": "x1carbon", "kind": "ssh", "address": "x1carbon"}


def _fixture(name: str) -> str:
    return (_FIX / name).read_text(encoding="utf-8")


def call(handler, services, body=None, query=None, **params):
    return handler(ApiRequest(path_params=params, query=query or {}, body=body, services=services))


@pytest.fixture(autouse=True)
def _reset_state():
    for d in (hosts_api._probing, hosts_api._checking, hosts_api._last_probe_start):
        d.clear()
    yield
    for d in (hosts_api._probing, hosts_api._checking, hosts_api._last_probe_start):
        d.clear()


@pytest.fixture
def real_env(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 22))])
    store = Store(tmp_path / "real.db")
    factory = FakeTransportFactory()
    events = FakeEvents()
    services = make_services(store=store, transports=factory, events=events, config=Config())
    assert isinstance(services.prober, real_prober.Prober)
    for host_id, fixture in (("x1carbon", "probe_x1carbon.txt"),
                             ("slurm-test", "probe_slurmctld.txt")):
        t = factory.get(host_id)
        t.script_script(r"==os", exec_result(rc=0, stdout=_fixture(fixture)))
        t.script(("true",), exec_result(rc=0))
    yield services, store, factory, events
    store.close()


def _slurm_body():
    return {"name": "Slurm Test", "id": "slurm-test", "kind": "slurm", "address": "x1carbon",
            "exec_prefix": list(_SLURM_PREFIX)}


def _save(store, data):
    data = {"id": data.get("id", "x1carbon"), "created_at": "2026-01-01T00:00:00Z", **data}
    return store.upsert_host(hosts_api._validate_spec(data))


def test_create_ssh_host_probes_parsed_capabilities(real_env):
    services, store, factory, events = real_env
    out = call(hosts_api.create_host, services, dict(_X1))
    state = out["state"]
    assert state["status"] == "online"
    caps = state["capabilities"]
    assert caps["runtime"] == "podman" and caps["runtime_version"] == "5.8.4"
    assert caps["rootless"] is True and caps["containers"] is True
    assert caps["display"] == "buildable"
    assert state["resources"]["source"] == "host"
    assert store.get_host_state("x1carbon").status == "online"
    assert [p["reason"] for p in events.payloads("hosts.changed")] == ["created"]
    assert [p["status"] for p in events.payloads("host.status")] == ["online"]


def test_create_slurm_host_uses_exec_prefix_and_parses_partitions(real_env):
    services, store, factory, events = real_env
    out = call(hosts_api.create_host, services, _slurm_body())
    assert out["spec"]["exec_prefix"] == _SLURM_PREFIX
    state = out["state"]
    assert state["status"] == "online"
    caps = state["capabilities"]
    assert caps["scheduler"] == "slurm" and caps["scheduler_version"] == "26.05.5"
    assert [p["name"] for p in caps["partitions"]] == ["cpu", "gpu"]
    assert caps["display"] == "unsupported"
    assert state["resources"]["source"] == "slurm"
    assert ("slurm-test", True) in factory.requests       # probed through the prefixed transport


def test_probe_route_updates_state_and_emits_only_on_change(real_env):
    services, store, factory, events = real_env
    call(hosts_api.create_host, services, dict(_X1))
    events.clear()
    first = call(hosts_api.probe_host, services, host_id="x1carbon")
    assert first["status"] == "online" and first["capabilities"]["runtime_version"] == "5.8.4"
    assert events.payloads("host.status") == []           # unchanged status: no event
    factory.get("x1carbon").latency_ms = None
    down = call(hosts_api.probe_host, services, host_id="x1carbon")
    assert down["status"] == "offline"
    assert events.payloads("host.status")[-1]["prev"] == "online"
    assert down["capabilities"]["runtime_version"] == "5.8.4"   # kept while offline


def test_test_connection_has_seven_steps_and_leaves_store_untouched(real_env):
    services, store, factory, events = real_env
    out = call(hosts_api.test_host, services, dict(_X1))
    assert [s["name"] for s in out["steps"]] == [
        "dns", "tcp", "auth", "runtime", "scheduler", "display", "gpu"]
    assert out["ok"] is True and out["status"] == "online"
    assert out["steps"][3]["ok"] is True and "podman 5.8.4" in out["steps"][3]["detail"]
    assert out["steps"][4]["ok"] is None
    assert store.list_hosts() == [] and store.get_host_state("x1carbon") is None
    assert events.names() == []


def test_test_connection_slurm_scheduler_step(real_env):
    services, store, *_ = real_env
    out = call(hosts_api.test_host, services, _slurm_body())
    steps = {s["name"]: s for s in out["steps"]}
    assert len(out["steps"]) == 8 and out["ok"] is True
    assert steps["scheduler"]["ok"] is True and "26.05.5" in steps["scheduler"]["detail"]
    assert "cpu" in steps["scheduler"]["detail"] and "gpu" in steps["scheduler"]["detail"]
    assert store.list_hosts() == []


def test_list_hosts_returns_probed_state(real_env):
    services, *_ = real_env
    call(hosts_api.create_host, services, dict(_X1))
    call(hosts_api.create_host, services, _slurm_body())
    rows = call(hosts_api.list_hosts, services)
    assert {r["spec"]["id"]: r["state"]["status"] for r in rows} == {
        "x1carbon": "online", "slurm-test": "online"}
    only = call(hosts_api.list_hosts, services, query={"kind": "slurm"})
    assert [r["spec"]["id"] for r in only] == ["slurm-test"]


def test_compute_status_refresh_true(real_env):
    services, store, factory, events = real_env
    _save(store, dict(_X1))
    _save(store, _slurm_body())
    assert store.get_host_state("x1carbon") is None
    out = hosts_api.compute_status({"refresh": True}, services)
    assert "warning" not in out and "error" not in out
    by_id = {h["id"]: h for h in out["hosts"]}
    x1 = by_id["x1carbon"]
    assert x1["status"] == "online" and x1["last_seen"]
    assert x1["capabilities"]["runtime_version"] == "5.8.4"
    assert x1["capabilities"]["rootless"] is True and x1["capabilities"]["display"] == "buildable"
    sl = by_id["slurm-test"]
    assert sl["capabilities"]["scheduler_version"] == "26.05.5"
    assert [p["name"] for p in sl["capabilities"]["partitions"]] == ["cpu", "gpu"]
    json.dumps(out)
    one = hosts_api.compute_status({"host_id": "slurm-test", "refresh": False}, services)
    assert [h["id"] for h in one["hosts"]] == ["slurm-test"]


def test_deep_probe_task_never_blocks_on_a_slow_probe(real_env):
    services, store, factory, events = real_env
    _save(store, dict(_X1))
    release = threading.Event()
    out = _fixture("probe_x1carbon.txt")

    def slow(script):
        release.wait(5)
        return exec_result(rc=0, stdout=out)

    t = factory.get("x1carbon")
    t._script_rules.clear()
    t.script_script(r"==os", slow)
    started = time.perf_counter()
    hosts_api.run_probe(services)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"run_probe blocked for {elapsed:.2f}s"
    deadline = time.time() + 2
    while not services.prober.is_probing("x1carbon") and time.time() < deadline:
        time.sleep(0.01)
    assert services.prober.is_probing("x1carbon")
    # a second tick while the probe runs neither blocks nor stacks another thread
    before = threading.active_count()
    started = time.perf_counter()
    hosts_api.run_probe(services)
    assert time.perf_counter() - started < 1.0
    assert threading.active_count() <= before
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and (services.prober.is_probing("x1carbon")
                                      or "x1carbon" in hosts_api._probing):
        time.sleep(0.02)
    assert store.get_host_state("x1carbon").status == "online"


def test_probe_tick_skips_hosts_the_prober_is_already_probing(real_env):
    """A manual probe in progress must not get a duplicate thread from the tick."""
    services, store, factory, events = real_env
    host = _save(store, dict(_X1))
    lock = services.prober._lock_for(host.id)
    lock.acquire()
    try:
        before = threading.active_count()
        hosts_api.run_probe(services)
        assert threading.active_count() <= before
        assert "x1carbon" not in hosts_api._probing
    finally:
        lock.release()


# --------------------------------------------------------------------------- registration


def _load_plugin_package():
    plugin_dir = Path(__file__).resolve().parents[1]
    name = "cloud_compute_hosts_wrap_test"
    for m in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        del sys.modules[m]
    spec = importlib.util.spec_from_file_location(
        name, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, name


def test_registered_compute_status_wrapper_returns_spec_json(tmp_path):
    store = Store(tmp_path / "wrap.db")
    services = make_services(store=store, config=Config())
    store.upsert_host(sample_host(id="h1"))
    store.set_host_state(sample_host_state(host_id="h1"))
    module, name = _load_plugin_package()
    try:
        class Ctx:
            def __init__(self):
                self.tools = {}

            def register_tool(self, name, toolset, schema, handler, **kw):
                self.tools[name] = (toolset, schema, handler)

            def __getattr__(self, attr):                  # hooks, skills, commands: ignored
                return lambda *a, **k: None

        ctx = Ctx()
        module.register(ctx)
        assert "compute_status" in ctx.tools, sorted(ctx.tools)
        toolset, schema, handler = ctx.tools["compute_status"]
        assert toolset == "cloud_compute" and schema["name"] == "compute_status"
        module._SERVICES = services
        raw = handler({})
        assert isinstance(raw, str)
        data = json.loads(raw)
        assert set(data) >= {"hosts", "instances", "jobs"}
        assert [h["id"] for h in data["hosts"]] == ["h1"]
        assert set(data["hosts"][0]) == {"id", "name", "kind", "status", "allow_agents",
                                         "last_seen", "capabilities", "resources", "card"}
        missing = json.loads(handler({"host_id": "nope"}))
        assert missing["error"]["code"] == "not_found"
    finally:
        # register() + the tool call start the process-wide background loop (a thread
        # that outlives the test); stop it before the store goes away.
        real_background.shutdown()
        assert real_background.current() is None
        store.close()
        for m in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[m]


def test_hosts_routes_and_tasks_are_collected_by_the_registry():
    found = {(r.method, r.path) for r in registry.collect_routes()}
    for expected in [("GET", "/hosts"), ("POST", "/hosts"), ("POST", "/hosts/test"),
                     ("GET", "/hosts/{host_id}"), ("PATCH", "/hosts/{host_id}"),
                     ("DELETE", "/hosts/{host_id}"), ("POST", "/hosts/{host_id}/probe")]:
        assert expected in found
    assert "compute_status" in {t.name for t in registry.collect_tools()}
    assert {"hosts.liveness", "hosts.probe"} <= {t.name for t in registry.collect_background()}


def test_slurm_state_carries_allocated_basis_ssh_does_not(real_env):
    services, store, factory, events = real_env
    call(hosts_api.create_host, services, dict(_X1))
    slurm_state = call(hosts_api.create_host, services, _slurm_body())["state"]
    assert slurm_state["resources"]["basis"] == "allocated"
    assert slurm_state["resources"]["cpu_total"] == 16 and slurm_state["resources"]["mem_used_mb"] == 0
    x1 = call(hosts_api.get_host, services, host_id="x1carbon")["state"]
    assert "basis" not in x1["resources"]
    listed = {h["spec"]["id"]: h["state"] for h in call(hosts_api.list_hosts, services)}
    assert listed["slurm-test"]["resources"]["basis"] == "allocated"
    assert "basis" not in listed["x1carbon"]["resources"]
    status = hosts_api.compute_status({}, services, {})
    by_id = {h["id"]: h for h in status["hosts"]}
    assert by_id["slurm-test"]["resources"]["basis"] == "allocated"
    assert "basis" not in by_id["x1carbon"]["resources"]


def _failed_workdir_result():
    from cloud_compute_core.prober import TestResult, TestStep
    step = TestStep("workdir", False, "/root/cloud-compute is writable but not visible from c1",
                    "Use a folder on shared storage, for example /data/cloud-compute")
    return TestResult(ok=False, status="degraded", steps=[step], capabilities=None)


def test_failed_workdir_check_is_kept_on_the_saved_host_and_survives_a_probe(real_env):
    services, store, factory, events = real_env
    call(hosts_api.create_host, services, _slurm_body())
    spec = store.get_host("slurm-test")
    assert store.get_host_state("slurm-test").capabilities.workdir_shared is None
    assert services.prober.remember_workdir(spec, _failed_workdir_result()) is True
    row = call(hosts_api.get_host, services, host_id="slurm-test")
    caps = row["state"]["capabilities"]
    assert caps["workdir_shared"] is False
    assert "not visible from c1" in caps["workdir_detail"] and "Fix: Use a folder" in caps["workdir_detail"]
    again = call(hosts_api.probe_host, services, host_id="slurm-test")      # a probe keeps it
    assert again["capabilities"]["workdir_shared"] is False
    assert "Fix:" in again["capabilities"]["workdir_detail"]


def test_workdir_check_of_another_address_or_an_unsaved_host_is_not_stored(real_env):
    services, store, factory, events = real_env
    call(hosts_api.create_host, services, _slurm_body())
    other = hosts_api._validate_spec({**_slurm_body(), "address": "elsewhere"})
    assert services.prober.remember_workdir(other, _failed_workdir_result()) is False
    unsaved = hosts_api._validate_spec({**_slurm_body(), "id": "nope", "name": "Nope"})
    assert services.prober.remember_workdir(unsaved, _failed_workdir_result()) is False
    assert store.get_host_state("slurm-test").capabilities.workdir_shared is None
