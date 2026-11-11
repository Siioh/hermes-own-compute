"""Instance routes, env_* tools, approval messages and background tasks."""

from __future__ import annotations

import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from cloud_compute_core import instances_api, registry
from cloud_compute_core.interfaces import ApiError, ApiRequest
from cloud_compute_core.models import HostLimits, TemplateSpec, VariableSpec
from cloud_compute_core.reaper import Reaper
from cloud_compute_core.sandbox import SandboxManager
from cloud_compute_core.templates import TemplateStore
from cloud_compute_core.testing import (
    exec_result, sample_host, sample_instance, sample_local_host,
)

SECRET = "s3cr3t-value-XYZ"


# --------------------------------------------------------------------------- fixtures


def _is_write(argv, stdin=None):
    return argv[:2] == ["sh", "-c"] and "cat >" in argv[2]


def _is_rm_env(argv, stdin=None):
    return argv[:2] == ["sh", "-c"] and argv[2].startswith("rm -f")


class FakeFiles:
    def __init__(self, fail: BaseException | None = None):
        self.fail = fail
        self.exports: list[tuple[str, str]] = []

    def export(self, instance_id, path=None, *, reason="manual"):
        if self.fail is not None:
            raise self.fail
        self.exports.append((instance_id, reason))
        return {"id": "x-00000001", "local_path": "C:/exports/work-i-1/x.tar.gz", "bytes": 2048,
                "files": 3}


@pytest.fixture
def env(services, fake_transports, fake_secrets):
    fake_secrets.set("HF_TOKEN", SECRET)
    host = sample_host(name="x1carbon", runtime_pref="podman", limits=HostLimits(max_instances=3))
    services.store.upsert_host(host)
    services.set("templates", TemplateStore(services.store))
    python_dev = services.templates.get("python-dev")      # built-ins ship no secrets: add one
    python_dev.variables = [VariableSpec(name="HF_TOKEN", source="secret:HF_TOKEN", scope="runtime")]
    services.templates.save(python_dev)
    services.set("sandboxes", SandboxManager(services))
    services.set("reaper", Reaper(services))
    services.set("files", FakeFiles())
    t = fake_transports.get(host.id)
    t.script(_is_write, lambda argv, stdin: exec_result(
        stdout=f"/home/u/.cache/cloud-compute/{argv[4]}.env\n"))
    t.script(_is_rm_env, exec_result())
    t.script(("podman", "run"), exec_result(stdout="3f2a9c1d0b7e\n"))
    t.script(("podman", "inspect"), exec_result(stdout=json.dumps(
        [{"Id": "f0" * 32, "State": {"Status": "running"}, "NetworkSettings": {"Ports": {}}}])))
    t.script(("podman", "exec"), exec_result(stdout=f"out {SECRET}\n", stderr="err\n"))
    t.script(("podman", "logs"), exec_result(stdout=f"log line {SECRET}\n"))
    for verb in ("stop", "start", "rm", "stats", "ps", "build"):
        t.script(("podman", verb), exec_result())
    return SimpleNamespace(services=services, host=host, t=t)


class Router:
    """Dispatch like the dashboard layer: static segments are tried before ``{param}`` ones."""

    def __init__(self, services):
        self.services = services

        def key(route):
            return tuple(1 if s.startswith("{") else 0 for s in route.path.split("/") if s)

        self.routes = sorted(instances_api.routes(), key=key)

    def call(self, method, path, query=None, body=None):
        segs = [s for s in path.split("/") if s]
        for route in self.routes:
            rsegs = [s for s in route.path.split("/") if s]
            if route.method != method or len(rsegs) != len(segs):
                continue
            params = {}
            for r, s in zip(rsegs, segs):
                if r.startswith("{"):
                    params[r[1:-1]] = s
                elif r != s:
                    break
            else:
                req = ApiRequest(path_params=params, query=dict(query or {}), body=body,
                                 services=self.services)
                return route.handler(req)
        raise AssertionError(f"no route for {method} {path}")


@pytest.fixture
def api(env):
    return Router(env.services)


def _spawn(api, **body):
    body.setdefault("template_id", "python-dev")
    body.setdefault("host_id", "x1carbon")
    return api.call("POST", "/instances", body=body)


# --------------------------------------------------------------------------- template routes


def test_template_routes_crud(api):
    listed = api.call("GET", "/templates")
    assert [t["id"] for t in listed[:4]] == ["ubuntu-dev", "python-dev", "desktop-novnc", "slurm-python"]
    assert api.call("GET", "/templates/python-dev")["image"].startswith("docker.io/library/python")

    new = TemplateSpec(id="mine", name="Mine", description="d").to_dict()
    created = api.call("POST", "/templates", body=new)
    assert created["id"] == "mine" and created["builtin"] is False
    with pytest.raises(ApiError) as exc:
        api.call("POST", "/templates", body=new)
    assert exc.value.status == 409

    patched = api.call("PATCH", "/templates/mine", body={"cpus": 1, "id": "other", "name": "Renamed"})
    assert patched["id"] == "mine" and patched["cpus"] == 1 and patched["name"] == "Renamed"
    assert patched["memory_gb"] == 4  # untouched fields are merged from the current spec

    assert api.call("DELETE", "/templates/mine") == {"deleted": True}
    assert api.call("DELETE", "/templates/mine") == {"deleted": False}
    json.dumps(listed)


def test_template_routes_errors(api):
    for method, path in (("GET", "/templates/nope"), ("PATCH", "/templates/nope")):
        with pytest.raises(ApiError) as exc:
            api.call(method, path, body={"cpus": 1})
        assert exc.value.status == 404
    with pytest.raises(ApiError) as exc:
        api.call("POST", "/templates", body={"id": "x"})  # missing required fields
    assert exc.value.status == 400
    with pytest.raises(ApiError) as exc:
        api.call("POST", "/templates", body=TemplateSpec(id="Bad Id", name="n", description="").to_dict())
    assert exc.value.status == 400
    with pytest.raises(ApiError) as exc:
        api.call("PATCH", "/templates/python-dev", body={"cpus": -1})
    assert exc.value.status == 400
    with pytest.raises(ApiError) as exc:
        api.call("POST", "/templates", body=None)
    assert exc.value.status == 400


def test_get_template_default_returns_the_shipped_version(api):
    api.call("PATCH", "/templates/python-dev", body={"cpus": 3})
    assert api.call("GET", "/templates/python-dev")["cpus"] == 3
    shipped = api.call("GET", "/templates/python-dev", query={"default": "1"})
    assert shipped["id"] == "python-dev" and shipped["cpus"] != 3 and shipped["builtin"] is True
    assert shipped["variables"] == []
    assert api.call("GET", "/templates/python-dev", query={"default": "0"})["cpus"] == 3
    json.dumps(shipped)


def test_get_template_default_is_404_for_a_user_template(api):
    api.call("POST", "/templates", body={"name": "Mine", "image": "docker.io/library/alpine:3"})
    assert api.call("GET", "/templates/mine")["id"] == "mine"
    for tid in ("mine", "nope"):
        with pytest.raises(ApiError) as exc:
            api.call("GET", f"/templates/{tid}", query={"default": "1"})
        assert exc.value.status == 404 and exc.value.code == "not_found"


def test_create_template_needs_only_a_name_and_an_image(api):
    created = api.call("POST", "/templates", body={"name": "My Tools", "image": "docker.io/library/alpine:3"})
    assert created["id"] == "my-tools" and created["name"] == "My Tools"
    assert created["description"] == "" and created["builtin"] is False
    assert created["ttl_minutes"] is None and created["idle_minutes"] is None
    assert api.call("GET", "/templates/my-tools") == created
    again = api.call("POST", "/templates", body={"name": "My Tools", "image": "docker.io/library/alpine:3"})
    assert again["id"] != "my-tools"                         # a generated id never collides
    with pytest.raises(ApiError) as exc:                     # an explicit id that is taken
        api.call("POST", "/templates", body={"id": "my-tools", "name": "X", "image": "alpine:3"})
    assert exc.value.status == 409
    with pytest.raises(ApiError) as exc:                     # no image
        api.call("POST", "/templates", body={"name": "No Image"})
    assert exc.value.status == 400


def test_template_preview_route(api):
    prev = api.call("GET", "/templates/python-dev/preview", query={"host": "x1carbon"})
    assert prev["template_id"] == "python-dev" and prev["host_id"] == "x1carbon"
    assert SECRET not in json.dumps(prev)
    assert [v["name"] for v in prev["variables"]] == ["HF_TOKEN"]
    with pytest.raises(ApiError) as exc:
        api.call("GET", "/templates/python-dev/preview")
    assert exc.value.status == 400
    with pytest.raises(ApiError) as exc:
        api.call("GET", "/templates/python-dev/preview", query={"host": "ghost"})
    assert exc.value.status == 404
    with pytest.raises(ApiError) as exc:
        api.call("GET", "/templates/nope/preview", query={"host": "x1carbon"})
    assert exc.value.status == 404


# --------------------------------------------------------------------------- instance routes


def test_instance_lifecycle_routes(api, env):
    rec = _spawn(api, name="mine", ttl_minutes=30)
    assert rec["status"] == "running" and rec["name"] == "mine" and rec["host_id"] == "x1carbon"
    iid = rec["id"]
    assert SECRET not in json.dumps(rec)

    assert [r["id"] for r in api.call("GET", "/instances")] == [iid]
    assert [r["id"] for r in api.call("GET", "/instances", query={"host": "x1carbon"})] == [iid]
    assert api.call("GET", "/instances", query={"host": "other"}) == []
    assert api.call("GET", f"/instances/{iid}")["id"] == iid

    assert api.call("POST", f"/instances/{iid}/stop")["status"] == "stopped"
    assert api.call("POST", f"/instances/{iid}/start")["status"] == "running"
    assert api.call("POST", f"/instances/{iid}/destroy")["status"] == "destroyed"
    assert api.call("GET", "/instances") == []
    assert [r["id"] for r in api.call("GET", "/instances", query={"destroyed": "1"})] == [iid]


def test_instance_route_errors(api):
    for method, path, body in (
        ("GET", "/instances/i-00000000", None),
        ("POST", "/instances/i-00000000/stop", None),
        ("GET", "/instances/i-00000000/logs", None),
        ("POST", "/instances", {"template_id": "nope", "host_id": "x1carbon"}),
        ("POST", "/instances", {"template_id": "python-dev", "host_id": "ghost"}),
    ):
        with pytest.raises(ApiError) as exc:
            api.call(method, path, body=body)
        assert exc.value.status == 404, (method, path)
    rec = _spawn(api)
    with pytest.raises(ApiError) as exc:
        api.call("POST", f"/instances/{rec['id']}/explode")
    assert exc.value.status == 400 and exc.value.code == "invalid"
    for body in (None, {}, {"template_id": "python-dev"}, {"template_id": "python-dev",
                 "host_id": "x1carbon", "ttl_minutes": "5"}, {"template_id": "python-dev",
                 "host_id": "x1carbon", "idle_minutes": True}, {"template_id": "python-dev",
                 "host_id": "x1carbon", "name": ["x"]}):
        with pytest.raises(ApiError) as exc:
            api.call("POST", "/instances", body=body)
        assert exc.value.status == 400


def test_spawn_limit_reached_propagates(api, env):
    env.services.store.upsert_host(sample_host(limits=HostLimits(max_instances=1)))
    _spawn(api)
    with pytest.raises(ApiError) as exc:
        _spawn(api)
    assert exc.value.status == 409 and exc.value.code == "limit_reached"


def test_stop_all_is_not_shadowed(api, env):
    a, b = _spawn(api), _spawn(api)
    result = api.call("POST", "/instances/stop-all", body={})
    assert sorted(result["stopped"]) == sorted([a["id"], b["id"]])
    assert all(r["status"] == "stopped" for r in result["instances"])
    assert api.call("POST", "/instances/stop-all")["stopped"] == []  # no body at all
    # filtered by host: nothing on another host
    _spawn(api)
    assert api.call("POST", "/instances/stop-all", body={"host_id": "other"})["stopped"] == []
    assert len(api.call("POST", "/instances/stop-all", body={"host_id": "x1carbon"})["stopped"]) == 1
    with pytest.raises(ApiError) as exc:
        api.call("POST", "/instances/stop-all", body={"host_id": 5})
    assert exc.value.status == 400
    # the literal route precedes the parameterised ones in the real ordering
    paths = [r.path for r in Router(env.services).routes]
    assert paths.index("/instances/stop-all") < paths.index("/instances/{instance_id}/{action}")
    # and the action handler refuses the literal id even if matched
    with pytest.raises(ApiError) as exc:
        instances_api.instance_action(ApiRequest(
            path_params={"instance_id": "stop-all", "action": "destroy"}, query={}, body=None,
            services=env.services))
    assert exc.value.status == 400


def test_logs_route(api):
    rec = _spawn(api)
    out = api.call("GET", f"/instances/{rec['id']}/logs", query={"tail": "50"})
    assert "log line" in out["text"] and SECRET not in out["text"]
    argv = [c.argv for c in api.services.transports.get("x1carbon").calls if c.argv and "logs" in c.argv]
    assert argv[-1][argv[-1].index("--tail") + 1] == "50"
    api.call("GET", f"/instances/{rec['id']}/logs")  # default tail
    for bad in ("abc", "0", "10001", "-3"):
        with pytest.raises(ApiError) as exc:
            api.call("GET", f"/instances/{rec['id']}/logs", query={"tail": bad})
        assert exc.value.status == 400


def test_display_image_route(api, env, monkeypatch, tmp_path):
    from cloud_compute_core import sandbox
    recipe = tmp_path / "Containerfile"
    recipe.write_text("FROM scratch\n")
    monkeypatch.setattr(sandbox, "DISPLAY_CONTAINERFILE", recipe)
    out = api.call("POST", "/hosts/x1carbon/display-image")
    assert out == {"ok": True, "output_tail": ""}
    env.t.script(("podman", "build"), exec_result(rc=1, stderr="boom"))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    out = api.call("POST", "/hosts/x1carbon/display-image")
    assert out["ok"] is False and "boom" in out["output_tail"]
    with pytest.raises(ApiError) as exc:
        api.call("POST", "/hosts/ghost/display-image")
    assert exc.value.status == 404


# --------------------------------------------------------------------------- tools


def _tools():
    return {t.name: t for t in instances_api.tools()}


def test_tool_schemas():
    tools = _tools()
    assert set(tools) == {"env_spawn", "env_exec", "env_control"}
    spawn, ex, ctl = (tools[n].schema for n in ("env_spawn", "env_exec", "env_control"))
    assert spawn["name"] == "env_spawn" and spawn["parameters"]["required"] == ["template", "host_id"]
    p = spawn["parameters"]["properties"]
    assert (p["ttl_minutes"]["minimum"], p["ttl_minutes"]["maximum"]) == (5, 1440)
    assert (p["idle_minutes"]["minimum"], p["idle_minutes"]["maximum"]) == (5, 1440)
    assert "compute_status" in spawn["description"]
    assert ex["parameters"]["required"] == ["instance_id", "command"]
    t = ex["parameters"]["properties"]["timeout_s"]
    assert (t["minimum"], t["maximum"], t["default"]) == (1, 600, 120)
    assert ctl["parameters"]["required"] == ["instance_id", "action"]
    assert ctl["parameters"]["properties"]["action"]["enum"] == [
        "stop", "start", "destroy", "extend", "logs"]
    assert ctl["parameters"]["properties"]["tail"]["default"] == 200
    m = ctl["parameters"]["properties"]["minutes"]
    assert (m["type"], m["minimum"], m["maximum"]) == ("integer", 1, 10080)
    sv = ctl["parameters"]["properties"]["save"]
    assert (sv["type"], sv["default"]) == ("boolean", True)
    assert "Destroy" not in p["ttl_minutes"]["description"]
    assert "kept" in p["ttl_minutes"]["description"]
    for spec in tools.values():
        json.dumps(spec.schema)
        assert spec.toolset == "cloud_compute"
    assert tools["env_exec"].approval is None
    assert tools["env_spawn"].approval and tools["env_control"].approval


def test_tool_flow(env):
    tools = _tools()
    out = tools["env_spawn"].handler({"template": "python-dev", "host_id": "x1carbon"}, env.services,
                                     {"session_id": "sess-9"})
    assert out["instance"]["status"] == "running" and out["instance"]["session_id"] == "sess-9"
    assert out["preview"]["template_id"] == "python-dev"
    assert SECRET not in json.dumps(out)
    iid = out["instance"]["id"]

    res = tools["env_exec"].handler({"instance_id": iid, "command": "python -V"}, env.services, {})
    assert set(res) == {"rc", "stdout", "stderr", "timed_out", "card"}
    assert res["card"] == f'::cc-env{{id="{iid}"}}'
    assert res["rc"] == 0 and res["timed_out"] is False and SECRET not in json.dumps(res)

    logs = tools["env_control"].handler({"instance_id": iid, "action": "logs", "tail": 10},
                                        env.services, {})
    assert "log line" in logs["logs"] and SECRET not in logs["logs"]
    for action, status in (("stop", "stopped"), ("start", "running"), ("destroy", "destroyed")):
        r = tools["env_control"].handler({"instance_id": iid, "action": action}, env.services, {})
        assert r["instance"]["status"] == status


def test_exec_output_capped_to_tail(env):
    big = "a" * (50 * 1024) + "END"
    env.t.script(("podman", "exec"), exec_result(stdout=big, stderr=big))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    iid = _spawn(Router(env.services))["id"]
    res = instances_api.env_exec({"instance_id": iid, "command": "x"}, env.services, {})
    for key in ("stdout", "stderr"):
        assert len(res[key].encode()) <= 20 * 1024 and res[key].endswith("END")
    # even if the sandbox returned more, the tool trims again
    class Big:
        def exec(self, *a, **k):
            return exec_result(stdout=big, stderr=big)
    env.services.set("sandboxes", Big())
    res = instances_api.env_exec({"instance_id": iid, "command": "x"}, env.services, {})
    assert len(res["stdout"].encode()) <= 20 * 1024 and res["stdout"].endswith("END")


def test_tool_handlers_return_error_dicts(env):
    tools = _tools()
    cases = [
        ("env_spawn", {}),
        ("env_spawn", {"template": "nope", "host_id": "x1carbon"}),
        ("env_spawn", {"template": "python-dev", "host_id": "ghost"}),
        ("env_spawn", {"template": "python-dev", "host_id": "x1carbon", "ttl_minutes": 2}),
        ("env_spawn", {"template": "python-dev", "host_id": "x1carbon", "idle_minutes": 5000}),
        ("env_exec", {"instance_id": "i-00000000", "command": "ls"}),
        ("env_exec", {"instance_id": "i-00000000"}),
        ("env_exec", {"instance_id": "i-00000000", "command": "ls", "timeout_s": 601}),
        ("env_exec", {"instance_id": "i-00000000", "command": "ls", "timeout_s": 0}),
        ("env_control", {"instance_id": "i-00000000", "action": "stop"}),
        ("env_control", {"instance_id": "i-00000000", "action": "logs"}),
        ("env_control", {"instance_id": "i-00000000", "action": "reboot"}),
        ("env_control", {"instance_id": "i-00000000", "action": "logs", "tail": 0}),
    ]
    for name, args in cases:
        out = tools[name].handler(args, env.services, {})
        assert set(out) == {"error"} and out["error"]["code"] in ("invalid", "not_found"), (name, args, out)
    # unexpected exceptions become internal errors, never raised
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("kaput")
    env.services.set("sandboxes", Boom())
    for name, args in (("env_spawn", {"template": "python-dev", "host_id": "x1carbon"}),
                       ("env_exec", {"instance_id": "i-1", "command": "ls"}),
                       ("env_control", {"instance_id": "i-1", "action": "stop"})):
        out = tools[name].handler(args, env.services, None)
        assert out["error"]["code"] == "internal" and "kaput" in out["error"]["message"]
    assert tools["env_exec"].handler(None, env.services, {})["error"]["code"] == "invalid"


# --------------------------------------------------------------------------- approval


def test_spawn_approval_message(env):
    msg = _tools()["env_spawn"].approval(
        {"template": "python-dev", "host_id": "x1carbon"}, env.services)
    assert msg == "Spawn python-dev on x1carbon (2 CPU, 4 GB, TTL 4 h, sends HF_TOKEN [redacted])?"
    assert SECRET not in msg
    # arg overrides and persistent templates
    msg = instances_api.approval_env_spawn(
        {"template": "python-dev", "host_id": "x1carbon", "ttl_minutes": 90}, env.services)
    assert "TTL 90 min" in msg
    env.services.templates.save(TemplateSpec(
        id="keep", name="Keep", description="", persistent=True, cpus=1.5, memory_gb=8, ttl_minutes=None,
        variables=[VariableSpec(name="A_KEY", source="secret:HF_TOKEN"),
                   VariableSpec(name="UNSET", source="secret:NOT_SET"),
                   VariableSpec(name="LIT", source="literal:visible-literal")]))
    msg = instances_api.approval_env_spawn({"template": "keep", "host_id": "x1carbon"}, env.services)
    assert msg == "Spawn keep on x1carbon (1.5 CPU, 8 GB, no TTL, sends A_KEY [redacted])?"
    assert "UNSET" not in msg and "visible-literal" not in msg


def test_spawn_approval_makes_no_network_calls(env):
    instances_api.approval_env_spawn({"template": "python-dev", "host_id": "x1carbon"}, env.services)
    assert env.t.calls == []
    assert env.services.transports.requests == []


def test_spawn_approval_fails_closed(env, services):
    for args in ({}, {"template": "nope", "host_id": "x1carbon"}, None):
        msg = instances_api.approval_env_spawn(args, env.services)
        assert isinstance(msg, str) and msg
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("down")
    env.services.set("templates", Broken())
    assert instances_api.approval_env_spawn(
        {"template": "python-dev", "host_id": "x1carbon"}, env.services) == instances_api.GENERIC_SPAWN_MESSAGE


def test_spawn_approval_falls_back_when_preview_fails(env):
    class NoPreview:
        def preview(self, *a):
            raise RuntimeError("no")
    env.services.set("sandboxes", NoPreview())
    msg = instances_api.approval_env_spawn({"template": "python-dev", "host_id": "x1carbon"},
                                           env.services)
    assert "sends HF_TOKEN [redacted]" in msg


def test_control_approval_only_for_destroy(env):
    rec = _spawn(Router(env.services))
    approval = _tools()["env_control"].approval
    for action in ("stop", "start", "logs"):
        assert approval({"instance_id": rec["id"], "action": action}, env.services) is None
    msg = approval({"instance_id": rec["id"], "action": "destroy"}, env.services)
    assert msg == (f"Deletes {rec['name']} on x1carbon. The files in /work are saved to this "
                   "computer first, and it is deleted only if that copy is complete. Files "
                   "outside /work are not saved.")
    msg = approval({"instance_id": rec["id"], "action": "destroy", "save": False}, env.services)
    assert msg == (f"Deletes {rec['name']} on x1carbon and every file in it. Nothing is saved, "
                   "not even the files in /work.")
    msg = approval({"instance_id": rec["id"], "action": "extend", "minutes": 30}, env.services)
    assert msg.startswith(f"Extends {rec['name']} by 30 minutes (until ") and msg.endswith(").")
    # unknown instance and broken store still ask
    assert "i-zzz" in approval({"instance_id": "i-zzz", "action": "destroy"}, env.services)
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("down")
    env.services.set("store", Broken())
    assert approval({"instance_id": "i-1", "action": "destroy"}, env.services).startswith(
        "Deletes i-1 on ")


# --------------------------------------------------------------------------- background


def _task(name):
    return {t.name: t for t in instances_api.background_tasks()}[name]


def test_background_task_definitions(env):
    names = [t.name for t in instances_api.background_tasks()]
    assert names == ["instances.stats", "instances.reaper"]
    for t in instances_api.background_tasks():
        assert t.interval_s(env.services) == 60.0


def test_stats_task_refreshes_hosts_with_running_instances(env):
    api = Router(env.services)
    _spawn(api)
    env.services.store.upsert_host(sample_local_host())
    calls = []
    real = env.services.sandboxes.refresh_stats
    env.services.sandboxes.refresh_stats = lambda hid: (calls.append(hid), real(hid))[1]
    _task("instances.stats").run(env.services)
    assert calls == ["x1carbon"]  # the local host has nothing running


def test_stats_task_swallows_errors_and_is_bounded(env, monkeypatch):
    api = Router(env.services)
    _spawn(api)
    instances_api._stats_inflight.clear()

    def boom(hid):
        raise RuntimeError("down")
    env.services.sandboxes.refresh_stats = boom
    _task("instances.stats").run(env.services)  # no raise

    monkeypatch.setattr(instances_api, "STATS_HOST_TIMEOUT_S", 0.2)
    env.services.sandboxes.refresh_stats = lambda hid: time.sleep(1.5)
    started = time.monotonic()
    _task("instances.stats").run(env.services)
    assert time.monotonic() - started < 1.0  # gave up waiting on the slow host
    # while that call is still in flight, the host is skipped rather than stacked
    started = time.monotonic()
    _task("instances.stats").run(env.services)
    assert time.monotonic() - started < 0.5
    time.sleep(1.6)
    assert not instances_api._stats_inflight


def test_stats_task_rotates_hosts_when_budget_is_spent(env, monkeypatch):
    store = env.services.store
    for hid in ("h-a", "h-b", "h-c"):
        store.upsert_host(sample_host(id=hid, name=hid))
        store.upsert_instance(sample_instance(id=f"i-{hid[-1]*8}", host_id=hid, status="running"))
    instances_api._stats_inflight.clear()
    seen = []
    env.services.sandboxes = SimpleNamespace(refresh_stats=lambda hid: (seen.append(hid), time.sleep(0.1)))
    monkeypatch.setattr(instances_api, "STATS_BUDGET_S", 0.25)
    monkeypatch.setattr(instances_api, "_stats_cursor", 0)
    _task("instances.stats").run(env.services)
    first = list(seen)
    assert 1 <= len(first) < 3
    _task("instances.stats").run(env.services)
    assert seen[len(first)] != first[0]  # next tick starts on a different host


def _fake_reaper(hosts, sweep_host):
    return SimpleNamespace(host_ids=lambda: list(hosts), sweep_host=sweep_host)


def test_reaper_task_sweeps_each_host_and_swallows_errors(env):
    instances_api._reap_inflight.clear()
    swept = []

    def sweep_host(hid):
        swept.append(hid)
        if hid == "b":
            raise RuntimeError("down")
        return SimpleNamespace(errors=["x"])

    env.services.reaper = _fake_reaper(["a", "b", "c"], sweep_host)
    _task("instances.reaper").run(env.services)  # no raise
    assert sorted(swept) == ["a", "b", "c"]
    assert not instances_api._reap_inflight

    def boom():
        raise RuntimeError("down")
    env.services.reaper = SimpleNamespace(host_ids=boom)
    _task("instances.reaper").run(env.services)  # no raise


def test_reaper_task_is_bounded_and_skips_stuck_host(env, monkeypatch):
    instances_api._reap_inflight.clear()
    monkeypatch.setattr(instances_api, "REAPER_BUDGET_S", 0.3)
    gate = threading.Event()
    seen = []

    def sweep_host(hid):
        seen.append(hid)
        if hid == "stuck":
            gate.wait(5)
        return SimpleNamespace(errors=[])

    env.services.reaper = _fake_reaper(["stuck", "ok"], sweep_host)
    started = time.monotonic()
    _task("instances.reaper").run(env.services)
    assert time.monotonic() - started < 1.0
    assert sorted(seen) == ["ok", "stuck"]  # the healthy host was not blocked by the stuck one
    assert instances_api._reap_inflight == {"stuck"}
    seen.clear()
    _task("instances.reaper").run(env.services)
    assert seen == ["ok"]  # stuck host skipped while still in flight, not stacked
    gate.set()
    deadline = time.monotonic() + 3
    while instances_api._reap_inflight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not instances_api._reap_inflight
    assert not [t for t in threading.enumerate() if t.name.startswith("cc-reap-")]


def test_reaper_task_with_real_reaper_stops_idle(env):
    api = Router(env.services)
    rec = _spawn(api)
    stored = env.services.store.get_instance(rec["id"])
    stored.last_activity_at = "2020-01-01T00:00:00Z"
    stored.expires_at = None
    env.services.store.upsert_instance(stored)
    instances_api._reap_inflight.clear()
    _task("instances.reaper").run(env.services)
    assert env.services.store.get_instance(rec["id"]).status == "stopped"



def test_patch_template_persists_agent_base_url_and_model(api):
    body = TemplateSpec(id="agent-x", name="A", description="d",
                        agent={"enabled": True, "mode": "hermes"}).to_dict()
    api.call("POST", "/templates", body=body)
    out = api.call("PATCH", "/templates/agent-x", body={
        "agent": {"enabled": True, "mode": "hermes", "base_url": "https://llm.example/v1",
                  "model": "m-1"}})
    assert out["agent"]["base_url"] == "https://llm.example/v1" and out["agent"]["model"] == "m-1"
    assert api.call("GET", "/templates/agent-x")["agent"]["model"] == "m-1"


def test_patch_template_rejects_bad_base_url_with_400(api):
    body = TemplateSpec(id="agent-y", name="A", description="d", agent={"enabled": True}).to_dict()
    api.call("POST", "/templates", body=body)
    with pytest.raises(ApiError) as exc:
        api.call("PATCH", "/templates/agent-y", body={"agent": {"base_url": "ftp://x/v1"}})
    assert exc.value.status == 400 and "base_url" in exc.value.message
    assert api.call("GET", "/templates/agent-y")["agent"] == {"enabled": True}


def test_patch_builtin_agent_template_is_copy_on_write(api, env):
    from cloud_compute_core.agents import AGENT_TEMPLATES
    env.services.templates.save(copy.deepcopy(next(t for t in AGENT_TEMPLATES
                                                   if t.id == "agent-script")))
    cur = api.call("GET", "/templates/agent-script")["agent"]
    out = api.call("PATCH", "/templates/agent-script", body={
        "agent": dict(cur, base_url="http://localhost:8000/v1", model="m")})
    assert out["id"] == "agent-script" and out["agent"]["base_url"] == "http://localhost:8000/v1"


def _provide_seeds_templates(services) -> bool:
    """True when building ``services.agents`` alone saves the built-in agent templates."""
    try:
        services.agents
        return "hermes-agent" in {x.id for x in services.store.list_templates()}
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def fresh(services):
    """A real services container whose agents provider seeds on build (skips otherwise)."""
    from cloud_compute_core import testing

    probe = testing.make_services(store=services.store, config=services.config,
                                  transports=services.transports, secrets=services.secrets,
                                  events=services.events)
    if not _provide_seeds_templates(probe):
        pytest.skip("agents.provide() does not seed agent templates yet (agent templates are not seeded)")
    for tid in ("hermes-agent", "agent-script"):
        services.store.delete_template(tid)
    return services


def test_first_list_templates_includes_agent_templates(fresh):
    assert not {"hermes-agent", "agent-script"} & {t.id for t in fresh.store.list_templates()}
    ids = [t["id"] for t in Router(fresh).call("GET", "/templates")]
    assert "hermes-agent" in ids and "agent-script" in ids
    again = [t["id"] for t in Router(fresh).call("GET", "/templates")]
    assert again == ids


def test_get_template_hermes_agent_on_fresh_store(fresh):
    out = Router(fresh).call("GET", "/templates/hermes-agent")
    assert out["id"] == "hermes-agent" and out["agent"]["mode"] == "hermes"
    assert "model" in out["agent"] and "base_url" in out["agent"]


def test_list_templates_survives_broken_agents_provider(services):
    from cloud_compute_core import testing

    def boom(_services):
        raise RuntimeError("agents provider exploded")

    broken = testing.make_services(store=services.store, config=services.config,
                                   transports=services.transports, secrets=services.secrets,
                                   events=services.events)
    object.__getattribute__(broken, "_providers")["agents"] = boom
    router = Router(broken)
    ids = [t["id"] for t in router.call("GET", "/templates")]
    assert "python-dev" in ids
    assert router.call("GET", "/templates/python-dev")["id"] == "python-dev"


def test_list_templates_survives_unavailable_agents(services):
    from cloud_compute_core import testing

    gone = testing.make_services(store=services.store, config=services.config,
                                 transports=services.transports, secrets=services.secrets,
                                 events=services.events)
    object.__getattribute__(gone, "_providers").pop("agents")
    assert Router(gone).call("GET", "/templates")


class _Access:
    def __init__(self):
        self.closed = []

    def close_instance_tunnels(self, instance_id):
        self.closed.append(instance_id)


def test_stop_routes_and_reaper_close_tunnels(api, env):
    access = _Access()
    env.services.set("access", access)
    a, b, c = _spawn(api), _spawn(api), _spawn(api)
    api.call("POST", f"/instances/{a['id']}/stop")
    assert access.closed == [a["id"]]
    api.call("POST", "/instances/stop-all", body={})
    assert sorted(access.closed) == sorted([a["id"], b["id"], c["id"]])


def test_reaper_idle_stop_closes_tunnels(env):
    access = _Access()
    env.services.set("access", access)
    api = Router(env.services)
    rec = _spawn(api)
    stored = env.services.store.get_instance(rec["id"])
    stored.last_activity_at = "2020-01-01T00:00:00Z"
    stored.expires_at = None
    env.services.store.upsert_instance(stored)
    instances_api._reap_inflight.clear()
    _task("instances.reaper").run(env.services)
    assert env.services.store.get_instance(rec["id"]).status == "stopped"
    assert access.closed == [rec["id"]]


def test_api_default_names_are_readable(api):
    assert [_spawn(api)["name"] for _ in range(2)] == ["python-dev-1", "python-dev-2"]


# --------------------------------------------------------------------------- registry


def test_registry_collects_module():
    registry.clear_errors()
    route_keys = [(r.method, r.path) for r in registry.collect_routes()]
    for key in (("GET", "/templates"), ("POST", "/instances"), ("POST", "/instances/stop-all"),
                ("POST", "/hosts/{host_id}/display-image"),
                ("GET", "/instances/{instance_id}/logs")):
        assert route_keys.count(key) == 1, key
    names = [t.name for t in registry.collect_tools()]
    for n in ("env_spawn", "env_exec", "env_control"):
        assert names.count(n) == 1
    bg = [b.name for b in registry.collect_background()]
    assert bg.count("instances.stats") == 1 and bg.count("instances.reaper") == 1
    errors = registry.import_errors()
    assert not [k for k in errors if "instances_api" in k], errors


# --------------------------------------------------------------------------- data safety


def _files(env):
    return env.services.files


def _rm_count(env):
    return sum(1 for c in env.t.calls if c.kind == "run" and c.argv[:2] == ["podman", "rm"])


def test_destroy_route_saves_by_default_and_keeps_the_shape(api, env):
    rec = _spawn(api)
    out = api.call("POST", f"/instances/{rec['id']}/destroy")
    assert out["status"] == "destroyed" and set(out) == set(rec) | {"saved", "message"}
    assert _files(env).exports == [(rec["id"], "before-delete")]


def test_destroy_route_save_false_skips_the_export(api, env):
    rec = _spawn(api)
    _files(env).fail = RuntimeError("must not be called")
    out = api.call("POST", f"/instances/{rec['id']}/destroy", body={"save": False})
    assert out["status"] == "destroyed" and set(out) == set(rec) | {"saved", "message"}
    assert out["saved"] is None and out["message"].endswith("without saving its files.")


def test_destroy_route_not_saved_is_a_409_and_keeps_everything(api, env):
    rec = _spawn(api)
    _files(env).fail = ApiError(501, "not_implemented", "File transfer is not available yet.")
    before = _rm_count(env)
    with pytest.raises(ApiError) as exc:
        api.call("POST", f"/instances/{rec['id']}/destroy", body={"save": True})
    assert (exc.value.status, exc.value.code) == (409, "not_saved")
    assert "Delete without saving" in exc.value.message
    assert _rm_count(env) == before
    assert api.call("GET", f"/instances/{rec['id']}")["status"] == "running"


@pytest.mark.parametrize("body", [{"save": "false"}, {"save": 0}, {"save": None}, ["x"]])
def test_destroy_route_rejects_a_bad_save_flag(api, env, body):
    rec = _spawn(api)
    with pytest.raises(ApiError) as exc:
        api.call("POST", f"/instances/{rec['id']}/destroy", body=body)
    assert exc.value.status == 400


def test_extend_route_returns_the_record(api, env):
    rec = _spawn(api, ttl_minutes=30)
    out = api.call("POST", f"/instances/{rec['id']}/extend", body={"minutes": 60})
    assert set(out) == set(rec) and out["id"] == rec["id"]
    assert out["expires_at"] > rec["expires_at"] and out["expiry_warned_at"] is None
    for body in (None, {}, {"minutes": 0}, {"minutes": 10081}, {"minutes": True}):
        with pytest.raises(ApiError) as exc:
            api.call("POST", f"/instances/{rec['id']}/extend", body=body)
        assert exc.value.status == 400, body


def test_env_control_extend_and_destroy_without_saving(env):
    rec = _spawn(Router(env.services), ttl_minutes=30)
    ctl = _tools()["env_control"].handler
    out = ctl({"instance_id": rec["id"], "action": "extend", "minutes": 15}, env.services, {})
    assert out["instance"]["expires_at"] > rec["expires_at"]
    assert ctl({"instance_id": rec["id"], "action": "extend"}, env.services, {})["error"]["code"] \
        == "invalid"
    _files(env).fail = RuntimeError("nope")
    err = ctl({"instance_id": rec["id"], "action": "destroy"}, env.services, {})
    assert err["error"]["code"] == "not_saved"
    assert ctl({"instance_id": rec["id"], "action": "destroy", "save": "x"},
               env.services, {})["error"]["code"] == "invalid"
    out = ctl({"instance_id": rec["id"], "action": "destroy", "save": False}, env.services, {})
    assert out["instance"]["status"] == "destroyed"


def test_control_approval_restart_after_the_time_limit(env):
    api = Router(env.services)
    rec = _spawn(api, ttl_minutes=30)
    approval = _tools()["env_control"].approval
    assert approval({"instance_id": rec["id"], "action": "start"}, env.services) is None
    env.services.sandboxes._update(rec["id"], lambda r: setattr(
        r, "expires_at", "2020-01-01T00:00:00Z"))
    api.call("POST", f"/instances/{rec['id']}/stop")
    assert approval({"instance_id": rec["id"], "action": "start"}, env.services) == \
        f"Restarts {rec['name']} on x1carbon for another 30 minutes."
