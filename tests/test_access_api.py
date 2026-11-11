"""Access API module: routes, the env_open tool and the sweep task."""

from __future__ import annotations

import json

import pytest

from cloud_compute_core import access as A
from cloud_compute_core import access_api as api
from cloud_compute_core import registry, tunnels as T
from cloud_compute_core.config import Config
from cloud_compute_core.interfaces import ApiError, ApiRequest
from cloud_compute_core.store import Store
from cloud_compute_core.testing import (
    exec_result, sample_host, sample_instance, sample_job,
    sample_slurm_host, sample_tunnel,
)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_access_manager import Env, FakePopen, TOKEN, display_instance, script_inspect, _template  # noqa: E402,F401

SECRET_FREE = "<token>"


@pytest.fixture(autouse=True)
def _reset():
    FakePopen.instances.clear()
    T._REGISTRY.clear()
    registry.clear_errors()
    yield
    T._REGISTRY.clear()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def call(handler, env, body=None, query=None, **params):
    return handler(ApiRequest(path_params=params, query=query or {}, body=body,
                              services=env.services))


# --------------------------------------------------------------------------- routes


def test_routes_exact_set():
    got = {(r.method, r.path) for r in api.routes()}
    assert got == {
        ("POST", "/instances/{instance_id}/terminal"), ("POST", "/hosts/{host_id}/terminal"),
        ("POST", "/jobs/{job_id}/terminal"), ("POST", "/instances/{instance_id}/display"),
        ("POST", "/instances/{instance_id}/forward"), ("POST", "/hosts/{host_id}/forward"),
        ("GET", "/tunnels"), ("DELETE", "/tunnels/{tunnel_id}"),
    }
    assert all(r.summary for r in api.routes())


def test_registry_collects_access_routes_tool_and_task():
    paths = {(r.method, r.path) for r in registry.collect_routes()}
    assert ("POST", "/instances/{instance_id}/terminal") in paths
    assert "env_open" in [t.name for t in registry.collect_tools()]
    assert "access.tunnel_sweep" in [b.name for b in registry.collect_background()]
    assert "access_api" not in registry.import_errors()


def test_static_routes_sort_before_instance_action_route():
    """A planned ``/instances/{id}/{action}`` must not shadow ``/instances/{id}/terminal``."""
    from cloud_compute_core.interfaces import Route
    generic = Route("POST", "/instances/{id}/{action}", lambda r: {}, "")
    routes = [generic, *api.routes()]

    def key(route):
        return tuple(1 if s.startswith("{") else 0 for s in route.path.split("/") if s)
    ordered = [r.path for r in sorted(routes, key=key) if r.path.startswith("/instances/")]
    assert ordered.index("/instances/{instance_id}/terminal") < ordered.index("/instances/{id}/{action}")
    assert ordered.index("/instances/{instance_id}/display") < ordered.index("/instances/{id}/{action}")


def test_terminal_routes_return_terminal_launch(env):
    env.add(host=sample_slurm_host(), instance=None, job=sample_job())
    env.store.upsert_host(sample_host())
    env.store.upsert_instance(sample_instance())
    for handler, params in ((api.terminal_instance, {"instance_id": "i-ab12cd34"}),
                            (api.terminal_host, {"host_id": "slurm-x1"}),
                            (api.terminal_job, {"job_id": "j-1a2b3c4d"})):
        out = call(handler, env, **params)
        assert set(out) == {"launched", "method", "command_preview", "copy_command", "error"}
        assert out["launched"] is True


def test_terminal_route_errors(env):
    with pytest.raises(ApiError) as e:
        call(api.terminal_instance, env, instance_id="i-nope")
    assert e.value.status == 404


def test_display_route_returns_tunnel_with_token_url(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    out = call(api.display_instance, env, instance_id="i-ab12cd34")
    assert out["kind"] == "display" and out["status"] == "open" and TOKEN in out["url"]


def test_forward_routes(env):
    env.add(instance=sample_instance(ports={"8000": "127.0.0.1:38000"}))
    out = call(api.forward_instance, env, {"remote_port": 8000, "local_port": 45001},
               instance_id="i-ab12cd34")
    assert out["kind"] == "port" and out["local_port"] == 45001 and out["remote_port"] == 38000
    out = call(api.forward_host, env, {"remote_port": 3000}, host_id="x1carbon")
    assert out["remote_port"] == 3000 and out["instance_id"] is None


@pytest.mark.parametrize("body", [None, {}, {"remote_port": "80"}, {"remote_port": 0},
                                  {"remote_port": 80, "local_port": 99999}, {"remote_port": True},
                                  [1]])
def test_forward_route_validates_body(env, body):
    env.add()
    with pytest.raises(ApiError) as e:
        call(api.forward_host, env, body, host_id="x1carbon")
    assert e.value.status == 400


def test_tunnels_list_and_close(env):
    env.add()
    out = call(api.forward_host, env, {"remote_port": 3000}, host_id="x1carbon")
    assert [t["id"] for t in call(api.list_tunnels, env)] == [out["id"]]
    closed = call(api.close_tunnel, env, tunnel_id=out["id"])
    assert closed["status"] == "closed"
    assert call(api.list_tunnels, env) == []
    assert [t["id"] for t in call(api.list_tunnels, env, query={"all": "1"})] == [out["id"]]
    with pytest.raises(ApiError) as e:
        call(api.close_tunnel, env, tunnel_id="t-nope")
    assert e.value.status == 404


# --------------------------------------------------------------------------- env_open


def run_tool(env, **args):
    return api.env_open(args, env.services, {"session_id": "s1"})


def test_tool_spec():
    (spec,) = api.tools()
    assert spec.name == "env_open" and spec.approval is None and spec.toolset == "cloud_compute"
    params = spec.schema["parameters"]
    assert params["required"] == ["kind"]
    assert params["properties"]["kind"]["enum"] == ["terminal", "display", "port"]
    assert set(params["properties"]) == {"instance_id", "host_id", "job_id", "kind", "remote_port"}
    assert params["properties"]["remote_port"]["type"] == "integer"
    assert params["properties"]["remote_port"]["minimum"] == 1
    assert params["properties"]["remote_port"]["maximum"] == 65535
    assert "compute_status" in spec.schema["description"]
    json.dumps(spec.schema)


def test_tool_terminal(env):
    env.add(instance=sample_instance())
    out = run_tool(env, kind="terminal", instance_id="i-ab12cd34")
    assert out["launched"] is True and out["method"] == "wt"


def test_tool_display_opens_browser_and_hides_token(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    out = run_tool(env, kind="display", instance_id="i-ab12cd34")
    assert out["opened"] is True and out["embeddable"] is True
    assert out["tunnel_id"].startswith("t-") and out["expires_at"]
    assert out["url"] == ("http://127.0.0.1:45678/<token>/vnc_lite.html"
                          "?path=<token>/websockify&scale=true")
    assert env.opened == [f"http://127.0.0.1:45678/{TOKEN}/vnc_lite.html"
                          f"?path={TOKEN}/websockify&scale=true"]
    assert TOKEN not in json.dumps(out)
    assert TOKEN not in json.dumps(env.events.events)


def test_tool_display_browser_failure_reports_not_opened(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.access._opener = lambda url: False
    out = run_tool(env, kind="display", instance_id="i-ab12cd34")
    assert out["opened"] is False and TOKEN not in json.dumps(out)


def test_tool_display_inspect_token_never_in_output(env):
    env.add(instance=display_instance())
    t = env.factory.get("x1carbon")
    t.script(lambda argv, stdin: argv[:2] == ["podman", "inspect"],
             exec_result(stdout=json.dumps([{"Config": {"Env": [f"VNC_PASSWORD={TOKEN}"]}}])))
    out = run_tool(env, kind="display", instance_id="i-ab12cd34")
    assert TOKEN not in json.dumps(out)


def test_tool_display_error_tunnel_is_error_json(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.access.tunnels._port_probe = lambda p: False
    env.access.tunnels.ready_timeout = 0.0
    out = run_tool(env, kind="display", instance_id="i-ab12cd34")
    assert "error" in out and TOKEN not in json.dumps(out) and not env.opened


def test_tool_port_instance_and_host(env):
    env.add(instance=sample_instance(ports={"8000": "127.0.0.1:38000"}))
    out = run_tool(env, kind="port", instance_id="i-ab12cd34", remote_port=8000)
    assert out["url"] == "http://127.0.0.1:45678/" and out["tunnel_id"] and out["expires_at"]
    out = run_tool(env, kind="port", host_id="x1carbon", remote_port=3000)
    assert set(out) == {"url", "tunnel_id", "expires_at"}


@pytest.mark.parametrize("args", [
    {}, {"kind": "bogus"}, {"kind": "terminal"}, {"kind": "display"},
    {"kind": "port", "host_id": "x1carbon"}, {"kind": "port", "host_id": "x1carbon", "remote_port": "80"},
    {"kind": "port", "host_id": "x1carbon", "remote_port": 70000},
    {"kind": "terminal", "instance_id": 5},
])
def test_tool_invalid_args_return_error_json(env, args):
    env.add()
    out = run_tool(env, **args)
    assert out["error"]["code"] == "invalid"


def test_tool_missing_and_unpublished_errors(env):
    env.add(instance=sample_instance())
    assert run_tool(env, kind="terminal", instance_id="i-nope")["error"]["code"] == "not_found"
    out = run_tool(env, kind="port", instance_id="i-ab12cd34", remote_port=8000)
    assert out["error"]["code"] == "conflict"


def test_tool_never_raises_on_unexpected_error(env):
    class Boom:
        @property
        def access(self):
            raise RuntimeError("boom")
    assert api.env_open({"kind": "terminal", "host_id": "x"}, Boom())["error"]["code"] == "internal"
    assert api.env_open("not a dict", env.services)["error"]["code"] == "invalid"


def test_tool_unavailable_service_is_error_json():
    class Missing:
        @property
        def access(self):
            raise ApiError(503, "unavailable", "access not installed")
    out = api.env_open({"kind": "terminal", "host_id": "x"}, Missing())
    assert out["error"]["code"] == "unavailable"


# --------------------------------------------------------------------------- background


def test_background_task_registered_30s(env):
    (task,) = api.background_tasks()
    assert task.name == "access.tunnel_sweep" and task.interval_s(env.services) == 30.0


def test_background_task_sweeps_expired(env):
    env.add()
    env.store.upsert_tunnel(sample_tunnel(pid=None, expires_at="2000-01-01T00:00:00Z"))
    (task,) = api.background_tasks()
    task.run(env.services)
    assert env.store.get_tunnel("t-5e6f7a8b").status == "closed"
    assert env.events.payloads("tunnels.changed") == [{"ids": ["t-5e6f7a8b"]}]


# --------------------------------------------------------------------------- strict tool, card


def test_tool_unknown_parameter_is_named_with_the_valid_ones(env):
    env.add(instance=sample_instance())
    out = run_tool(env, kind="terminal", instance_id="i-ab12cd34", local_port=9000, bogus=1)
    err = out["error"]
    assert err["code"] == "unknown_parameter"
    assert "'bogus'" in err["message"] and "'local_port'" in err["message"]
    assert ("Valid parameters: host_id, instance_id, job_id, kind, remote_port."
            in err["message"])
    assert env.launched == []                         # nothing was opened


def test_tool_unknown_parameter_is_checked_before_kind(env):
    assert run_tool(env, kind="nope", port=1)["error"]["code"] == "unknown_parameter"


def test_tool_schema_is_closed_and_describes_remote_port_and_kinds():
    (spec,) = api.tools()
    schema = spec.schema
    assert schema["parameters"]["additionalProperties"] is False
    text = schema["description"]
    assert len(text.split()) <= 70
    for needle in ("kind=terminal", "kind=display", "kind=port", "INSIDE the environment",
                   "host-side port", "published", "card"):
        assert needle in text
    assert "INSIDE" in schema["parameters"]["properties"]["remote_port"]["description"]


def test_tool_results_carry_the_environment_card(env):
    env.add(instance=sample_instance(display=True, ports={"novnc": "127.0.0.1:41234", "8000": "127.0.0.1:38000"}))
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    card = '::cc-env{id="i-ab12cd34"}'
    assert run_tool(env, kind="terminal", instance_id="i-ab12cd34")["card"] == card
    assert run_tool(env, kind="display", instance_id="i-ab12cd34")["card"] == card
    assert run_tool(env, kind="port", instance_id="i-ab12cd34", remote_port=8000)["card"] == card
    # Not about an environment: no card.
    assert "card" not in run_tool(env, kind="port", host_id="x1carbon", remote_port=3000)


def test_tool_port_accepts_the_host_side_port_and_says_what_was_forwarded(env):
    env.add(instance=sample_instance(display=True, ports={"novnc": "127.0.0.1:33319"}))
    script_inspect(env, {6080: 33319})
    out = run_tool(env, kind="port", instance_id="i-ab12cd34", remote_port=33319)
    assert "error" not in out and out["remote_port"] == 6080
    assert out["url"] == "http://127.0.0.1:45678/" and out["tunnel_id"]
    assert out["note"] == ("33319 is the host-side port of port 6080 inside the environment, "
                           "so port 6080 was forwarded.")
    out = run_tool(env, kind="port", instance_id="i-ab12cd34", remote_port=6080)
    assert out["remote_port"] == 6080 and "note" not in out


def test_tool_port_not_published_names_the_published_ports(env):
    env.add(instance=sample_instance(display=True, ports={"novnc": "127.0.0.1:33319"}))
    script_inspect(env, {6080: 33319})
    out = run_tool(env, kind="port", instance_id="i-ab12cd34", remote_port=8888)
    assert out["error"]["code"] == "conflict"
    assert out["error"]["message"] == (
        "Port 8888 is not published by python-dev-1. Published ports: 6080. Add the port under "
        "Published ports in the template and start a new environment.")


def test_tool_display_failure_is_never_internal(env):
    _template(env, "ubuntu-dev", "docker.io/library/ubuntu:24.04")
    env.add(instance=display_instance(template_id="ubuntu-dev"))
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.http_ok = False
    out = run_tool(env, kind="display", instance_id="i-ab12cd34")
    assert out["error"]["code"] == "needs_setup"
    assert out["error"]["message"].startswith("python-dev-1 has no screen: its template uses "
                                              "the image docker.io/library/ubuntu:24.04")
    assert not env.opened and env.store.list_tunnels(open_only=False) == []
    assert TOKEN not in json.dumps(out)
