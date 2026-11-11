"""Agent routes, tools and the status poll (core/agents_api.py)."""

from __future__ import annotations

import pytest

from cloud_compute_core import agents_api, registry
from cloud_compute_core.interfaces import ApiError, ApiRequest, BackgroundTask, Route, ToolSpec
from cloud_compute_core.testing import make_services


class Obj:
    def __init__(self, **data):
        self.data = data

    def to_dict(self):
        return dict(self.data)


class FakeAgents:
    def __init__(self):
        self.calls = []
        self.runs = {"a-11111111": Obj(id="a-11111111", status="running", last_log=[])}
        self.active = True
        self.poll_error = None
        self.polls = 0

    def spawn(self, goal, host_id, **kw):
        self.calls.append(("spawn", goal, host_id, kw))
        return Obj(id="a-22222222", goal=goal, host_id=host_id, session_id=kw.get("session_id"))

    def status(self, run_id=None):
        self.calls.append(("status", run_id))
        if run_id is None:
            return list(self.runs.values())
        if run_id not in self.runs:
            raise ApiError(404, "not_found", f"no run {run_id}")
        return [self.runs[run_id]]

    def collect(self, run_id, into="worktree"):
        self.calls.append(("collect", run_id, into))
        if run_id not in self.runs:
            raise ApiError(404, "not_found", f"no run {run_id}")
        return Obj(run_id=run_id, ref=f"refs/heads/cloud/{run_id}/x", commits_ahead=2,
                   worktree_path=None, diverged=False, conflicts_dry_run=[])

    def stop(self, run_id, keep_branch=True, delete_anyway=False):
        self.calls.append(("stop", run_id, keep_branch))
        if run_id not in self.runs:
            raise ApiError(404, "not_found", f"no run {run_id}")
        return Obj(id=run_id, status="stopped")

    def message(self, run_id, message, max_turns=None):
        self.calls.append(("message", run_id, message, max_turns))
        return Obj(id=run_id, status="running", commits_ahead=0, reply=None, last_log=[])

    def describe_message(self, args):
        return f"Follow up: {args.get('message')}"

    def describe_spawn(self, args):
        return f"Start agent: {args.get('goal')}"

    def has_active(self):
        return self.active

    def poll(self):
        self.polls += 1
        if self.poll_error:
            raise self.poll_error
        return []


@pytest.fixture
def fake():
    return FakeAgents()


@pytest.fixture
def services(fake):
    return make_services(agents=fake)


def req(services, path_params=None, query=None, body=None):
    return ApiRequest(path_params=path_params or {}, query=query or {}, body=body,
                      services=services)


def route(method, path):
    for r in agents_api.routes():
        if (r.method, r.path) == (method, path):
            return r.handler
    raise AssertionError(f"no route {method} {path}")


def tool(name):
    return next(t for t in agents_api.tools() if t.name == name)


def test_message_tool_sends_a_follow_up_and_never_raises(services, fake):
    spec = tool("cloud_agent_message")
    assert spec.approval is not None and spec.toolset == "cloud_compute"
    assert spec.approval({"run_id": "a-11111111", "message": "go"}, services) == "Follow up: go"
    params = spec.schema["parameters"]
    assert params["required"] == ["run_id", "message"]
    assert set(params["properties"]) == {"run_id", "message", "wait_s"}   # no turn limit here
    out = spec.handler({"run_id": "a-11111111", "message": "go on", "max_turns": 4}, services, {})
    assert fake.calls[-1] == ("message", "a-11111111", "go on", 4)      # the manager answers
    assert out["status"] == "running" and out["card"] == '::cc-agent{id="a-11111111"}'
    assert "cloud_agent_status" in out["next"]
    for args in ({}, None, {"run_id": "a-11111111"}):
        assert spec.handler(args, make_services(agents=fake), {})["error"]["code"] == "invalid"


SPAWN = {"goal": "fix it", "host_id": "x1", "repo_path": "/r"}


# --------------------------------------------------------------------------- routes


def test_route_table():
    got = {(r.method, r.path) for r in agents_api.routes()}
    assert got == {
        ("GET", "/agents"), ("POST", "/agents"), ("GET", "/agents/{run_id}"),
        ("POST", "/agents/{run_id}/collect"), ("POST", "/agents/{run_id}/stop"),
        ("DELETE", "/agents/{run_id}"), ("POST", "/agents/{run_id}/message"),
        ("POST", "/agents/{run_id}/rerun"),
        ("GET", "/agent-models"), ("POST", "/agent-models"), ("DELETE", "/agent-models/{model_id}"),
        ("POST", "/agent-models/{model_id}/default"),
    }
    assert all(isinstance(r, Route) for r in agents_api.routes())


def test_list_and_query(services, fake):
    out = route("GET", "/agents")(req(services))
    assert out == [{"id": "a-11111111", "status": "running", "last_log": []}]
    route("GET", "/agents")(req(services, query={"run_id": "a-11111111"}))
    assert fake.calls[-1] == ("status", "a-11111111")
    with pytest.raises(ApiError) as e:
        route("GET", "/agents")(req(services, query={"run_id": "nope"}))
    assert e.value.status == 404


def test_get_one_and_404(services):
    h = route("GET", "/agents/{run_id}")
    assert h(req(services, {"run_id": "a-11111111"}))["id"] == "a-11111111"
    with pytest.raises(ApiError) as e:
        h(req(services, {"run_id": "zzz"}))
    assert (e.value.status, e.value.code) == (404, "not_found")


def test_get_empty_result_is_404(services, fake):
    fake.runs.clear()
    fake.status = lambda run_id=None: []
    with pytest.raises(ApiError) as e:
        route("GET", "/agents/{run_id}")(req(services, {"run_id": "a-1"}))
    assert e.value.status == 404


def test_spawn_route(services, fake):
    out = route("POST", "/agents")(req(services, body=dict(SPAWN, max_turns=5, session_id="s1")))
    assert out["id"] == "a-22222222"
    _, goal, host, kw = fake.calls[-1]
    assert (goal, host) == ("fix it", "x1")
    assert kw == {"session_id": "s1", "repo_path": "/r", "branch": None, "new_branch": True,
                  "base": "HEAD", "template": "hermes-agent", "max_turns": 5,
                  "model_id": None, "criteria": None}


@pytest.mark.parametrize("body", [
    None, {}, {"goal": "g", "host_id": "h"}, dict(SPAWN, goal="  "), dict(SPAWN, goal=3),
    dict(SPAWN, max_turns="5"), dict(SPAWN, max_turns=0), dict(SPAWN, max_turns=True),
    dict(SPAWN, new_branch="yes"), ["x"],
])
def test_spawn_route_invalid(services, body):
    with pytest.raises(ApiError) as e:
        route("POST", "/agents")(req(services, body=body))
    assert (e.value.status, e.value.code) == (400, "invalid")


def test_collect_route(services, fake):
    h = route("POST", "/agents/{run_id}/collect")
    out = h(req(services, {"run_id": "a-11111111"}, body={"into": "branch"}))
    assert out["commits_ahead"] == 2
    assert fake.calls[-1] == ("collect", "a-11111111", "branch")
    h(req(services, {"run_id": "a-11111111"}))
    assert fake.calls[-1] == ("collect", "a-11111111", "worktree")
    with pytest.raises(ApiError) as e:
        h(req(services, {"run_id": "a-11111111"}, body={"into": "elsewhere"}))
    assert e.value.status == 400
    with pytest.raises(ApiError) as e:
        h(req(services, {"run_id": "gone"}))
    assert e.value.status == 404


def test_stop_route(services, fake):
    h = route("POST", "/agents/{run_id}/stop")
    assert h(req(services, {"run_id": "a-11111111"}))["status"] == "stopped"
    assert fake.calls[-1] == ("stop", "a-11111111", True)
    h(req(services, {"run_id": "a-11111111"}, body={"keep_branch": False}))
    assert fake.calls[-1] == ("stop", "a-11111111", False)
    with pytest.raises(ApiError) as e:
        h(req(services, {"run_id": "a-11111111"}, body={"keep_branch": "no"}))
    assert e.value.status == 400
    with pytest.raises(ApiError) as e:
        h(req(services, {"run_id": "gone"}))
    assert e.value.status == 404


def test_routes_without_manager_are_unavailable():
    services = make_services()
    services.__dict__["_providers"]["agents"] = lambda s: (_ for _ in ()).throw(ImportError("x"))
    with pytest.raises(ApiError) as e:
        route("GET", "/agents")(req(services))
    assert e.value.code == "unavailable"


# --------------------------------------------------------------------------- tools


def test_tool_names_and_flags():
    specs = agents_api.tools()
    assert [t.name for t in specs] == [
        "cloud_agent_spawn", "cloud_agent_status", "cloud_agent_collect", "cloud_agent_stop",
        "cloud_agent_message", "cloud_agent_rerun"]
    assert tool("cloud_agent_rerun").approval is not None
    assert all(isinstance(t, ToolSpec) and t.toolset == "cloud_compute" for t in specs)
    assert tool("cloud_agent_spawn").approval is not None
    assert tool("cloud_agent_stop").approval is not None     # deletes the sandbox (and branches)
    assert tool("cloud_agent_message").approval is not None
    for name in ("status", "collect"):
        assert tool(f"cloud_agent_{name}").approval is None


def test_schemas_match_spec():
    for t in agents_api.tools():
        assert t.schema["name"] == t.name
        assert t.schema["description"]
        assert t.schema["parameters"]["type"] == "object"
    sp = tool("cloud_agent_spawn").schema["parameters"]
    assert sorted(sp["required"]) == ["host_id", "repo_path", "task"]
    assert set(sp["properties"]) == {"task", "goal", "criteria", "host_id", "template", "repo_path",
                                     "branch", "new_branch", "base", "max_turns", "model_id", "wait_s"}
    assert "base_url" not in sp["properties"] and "key_secret" not in sp["properties"]
    assert sp["properties"]["template"]["default"] == "hermes-agent"
    assert sp["properties"]["new_branch"] == {
        "type": "boolean", "default": True, "description": sp["properties"]["new_branch"]["description"]}
    assert sp["properties"]["base"]["default"] == "HEAD"
    assert sp["properties"]["max_turns"]["default"] == 30
    assert sp["properties"]["max_turns"]["type"] == "integer"
    st = tool("cloud_agent_status").schema["parameters"]
    assert st["required"] == [] and set(st["properties"]) == {"run_id", "wait_s", "models"}
    co = tool("cloud_agent_collect").schema["parameters"]
    assert co["required"] == ["run_id"]
    assert co["properties"]["into"]["enum"] == ["worktree", "branch"]
    assert co["properties"]["into"]["default"] == "worktree"
    sto = tool("cloud_agent_stop").schema["parameters"]
    assert sto["required"] == ["run_id"]
    assert sto["properties"]["keep_branch"]["default"] is True


def test_spawn_approval_uses_manager_text(services):
    args = dict(SPAWN)
    assert tool("cloud_agent_spawn").approval(args, services) == "Start agent: fix it"


def test_spawn_tool_records_session(services, fake):
    out = tool("cloud_agent_spawn").handler(dict(SPAWN), services, {"session_id": "sess-9"})
    assert out["run"]["id"] == "a-22222222"
    assert out["run"]["session_id"] == "sess-9"
    assert fake.calls[-1][3]["session_id"] == "sess-9"
    tool("cloud_agent_spawn").handler(dict(SPAWN), services, {})
    assert fake.calls[-1][3]["session_id"] is None


def test_spawn_tool_defaults_passed(services, fake):
    tool("cloud_agent_spawn").handler(dict(SPAWN), services, {})
    kw = fake.calls[-1][3]
    assert (kw["template"], kw["new_branch"], kw["base"], kw["max_turns"]) == (
        "hermes-agent", True, "HEAD", 30)


def test_status_tool_trims_log(services, fake):
    fake.runs["a-11111111"] = Obj(id="a-11111111", last_log=[f"l{i}" for i in range(50)])
    out = tool("cloud_agent_status").handler({"run_id": "a-11111111"}, services, {})
    log = out["run"]["last_log"]
    assert log == [f"l{i}" for i in range(30, 50)]
    assert len(fake.runs["a-11111111"].data["last_log"]) == 50   # source untouched
    assert len(tool("cloud_agent_status").handler({}, services, {})["runs"]) == 1


def test_collect_and_stop_tools(services, fake):
    out = tool("cloud_agent_collect").handler({"run_id": "a-11111111"}, services, {})
    assert out["ref"].startswith("refs/heads/cloud/")
    assert fake.calls[-1] == ("collect", "a-11111111", "worktree")
    out = tool("cloud_agent_stop").handler({"run_id": "a-11111111", "keep_branch": False},
                                           services, {})
    assert out == {"run": {"id": "a-11111111", "status": "stopped"},
                   "card": '::cc-agent{id="a-11111111"}'}
    assert fake.calls[-1] == ("stop", "a-11111111", False)


@pytest.mark.parametrize("name,args", [
    ("cloud_agent_spawn", SPAWN), ("cloud_agent_status", {"run_id": "gone"}),
    ("cloud_agent_collect", {"run_id": "gone"}), ("cloud_agent_stop", {"run_id": "gone"}),
])
def test_tools_map_api_errors(services, name, args):
    if name == "cloud_agent_spawn":
        services.agents.spawn = lambda *a, **k: (_ for _ in ()).throw(
            ApiError(409, "limit_reached", "too many"))
        expect = {"error": {"code": "limit_reached", "message": "too many"}}
    else:
        expect = {"error": {"code": "not_found", "message": "no run gone"}}
    assert tool(name).handler(dict(args), services, {}) == expect


@pytest.mark.parametrize("name,args", [
    ("cloud_agent_spawn", SPAWN), ("cloud_agent_status", {}),
    ("cloud_agent_collect", {"run_id": "a-1"}), ("cloud_agent_stop", {"run_id": "a-1"}),
])
def test_tools_never_raise(services, fake, name, args):
    def boom(*a, **k):
        raise RuntimeError("kaput")
    for attr in ("spawn", "status", "collect", "stop"):
        setattr(fake, attr, boom)
    out = tool(name).handler(dict(args), services, {})
    assert out["error"]["code"] == "internal"
    assert "kaput" in out["error"]["message"]


@pytest.mark.parametrize("name,args", [
    ("cloud_agent_spawn", {}), ("cloud_agent_spawn", dict(SPAWN, max_turns="x")),
    ("cloud_agent_collect", {}), ("cloud_agent_collect", {"run_id": "a", "into": "x"}),
    ("cloud_agent_stop", {}), ("cloud_agent_status", {"run_id": 5}),
])
def test_tools_validation_errors(services, name, args):
    out = tool(name).handler(args, services, {})
    assert out["error"]["code"] == "invalid"


def test_tool_unavailable_manager_is_error_dict():
    services = make_services()
    services.__dict__["_providers"]["agents"] = lambda s: (_ for _ in ()).throw(ImportError("x"))
    out = tool("cloud_agent_status").handler({}, services, {})
    assert out["error"]["code"] == "unavailable"


# --------------------------------------------------------------------------- background


def test_background_task_shape(services):
    (task,) = agents_api.background_tasks()
    assert isinstance(task, BackgroundTask)
    assert task.name == "agents.poll"
    assert task.interval_s(services) == 15.0


def test_poll_only_when_active(services, fake):
    (task,) = agents_api.background_tasks()
    fake.active = False
    task.run(services)
    assert fake.polls == 0
    fake.active = True
    task.run(services)
    assert fake.polls == 1


def test_poll_survives_unavailable(services, fake):
    (task,) = agents_api.background_tasks()
    fake.poll_error = ApiError(503, "unavailable", "agents not installed")
    task.run(services)
    fake.poll_error = ApiError(409, "conflict", "other")
    task.run(services)
    assert fake.polls == 2


def test_poll_survives_missing_service():
    services = make_services()
    services.__dict__["_providers"]["agents"] = lambda s: (_ for _ in ()).throw(ImportError("x"))
    (task,) = agents_api.background_tasks()
    task.run(services)   # no raise


# --------------------------------------------------------------------------- registry


def test_registry_collects_module():
    registry.clear_errors()
    assert {("GET", "/agents"), ("POST", "/agents/{run_id}/stop")} <= {
        (r.method, r.path) for r in registry.collect_routes()}
    names = [t.name for t in registry.collect_tools()]
    assert {"cloud_agent_spawn", "cloud_agent_status", "cloud_agent_collect",
            "cloud_agent_stop"} <= set(names)
    assert "agents.poll" in [b.name for b in registry.collect_background()]
    assert "agents_api" not in registry.import_errors()


def test_import_does_not_need_agents_module():
    import sys
    src = open(agents_api.__file__, encoding="utf-8").read()
    assert "from .agents import" not in src and "import agents\n" not in src
    assert "cloud_compute_core.agents_api" in sys.modules


def test_stop_route_reports_a_sandbox_kept_because_its_files_could_not_be_saved(services, fake):
    """Stop does not fail when saving fails: the run comes back stopped, still with its sandbox."""
    from cloud_compute_core.agents import KEPT_SANDBOX_MESSAGE
    fake.stop = lambda run_id, keep_branch=True, delete_anyway=False: Obj(
        id=run_id, status="stopped", instance_id="i-keep0001", error=KEPT_SANDBOX_MESSAGE)
    out = route("POST", "/agents/{run_id}/stop")(req(services, {"run_id": "a-11111111"}))
    assert (out["status"], out["instance_id"]) == ("stopped", "i-keep0001")
    assert out["error"] == ("The sandbox was kept because the files in /work could not be saved "
                            "to this computer. "
                            "Delete it from Overview when ready.")


# --------------------------------------------------------------------------- reply, wait, follow-up, remove


@pytest.mark.parametrize("name,args", [
    ("cloud_agent_spawn", dict(SPAWN, base_url="http://evil/v1")),
    ("cloud_agent_spawn", dict(SPAWN, key_secret="OTHER")),
    ("cloud_agent_status", {"run_id": "a-11111111", "verbose": True}),
    ("cloud_agent_collect", {"run_id": "a-11111111", "merge": True}),
    ("cloud_agent_stop", {"run_id": "a-11111111", "force": True}),
    ("cloud_agent_message", {"run_id": "a-11111111", "message": "x", "model": "m"}),
])
def test_unknown_parameters_are_named(services, name, args):
    out = tool(name).handler(dict(args), services, {})
    err = out["error"]
    assert err["code"] == "unknown_parameter"
    extra = (set(args) - {"run_id", "message"} - set(SPAWN)).pop()
    assert extra in err["message"] and "Valid parameters:" in err["message"]


def test_spawn_task_is_preferred_and_goal_still_works(services, fake):
    tool("cloud_agent_spawn").handler({"task": "do x", "criteria": "tests pass", "host_id": "h",
                                       "repo_path": "/r", "model_id": "m-0a1b2c3d"}, services, {})
    _, goal, _, kw = fake.calls[-1]
    assert goal == "do x" and kw["criteria"] == "tests pass" and kw["model_id"] == "m-0a1b2c3d"
    tool("cloud_agent_spawn").handler(dict(SPAWN), services, {})
    assert fake.calls[-1][1] == "fix it"
    out = tool("cloud_agent_spawn").handler(dict(SPAWN, task="other"), services, {})
    assert out["error"]["code"] == "invalid"


def test_status_wait_reports_finished_and_next(services, fake, monkeypatch):
    def wait(run_id, budget):
        fake.calls.append(("wait", run_id, budget))
        return Obj(id=run_id, status="done", commits_ahead=2, reply="All done.", last_log=[]), True, 12.0
    fake.wait = wait
    out = tool("cloud_agent_status").handler({"run_id": "a-11111111", "wait_s": 9999}, services, {})
    assert fake.calls[-1][0] == "wait" and fake.calls[-1][2] == 300      # wait_max_seconds default
    assert (out["finished"], out["timed_out"], out["status"], out["reply"]) == (True, False, "done", "All done.")
    assert out["commits_ahead"] == 2 and out["card"] == '::cc-agent{id="a-11111111"}'
    assert out["next"] == 'Call cloud_agent_collect(run_id="a-11111111") to bring the branch to this computer.'
    assert "capped at 300" in out["note"]


def test_status_wait_timeout_is_said(services, fake):
    fake.wait = lambda run_id, budget: (Obj(id=run_id, status="running", last_log=[]), False, float(budget))
    out = tool("cloud_agent_status").handler({"run_id": "a-11111111", "wait_s": 20}, services, {})
    assert out["finished"] is False and out["timed_out"] is True
    assert "timed out after 20 s" in out["note"] and "wait_s=300" in out["next"]
    assert tool("cloud_agent_status").handler({"wait_s": 5}, services, {})["error"]["code"] == "invalid"


def test_status_wait_cap_follows_the_setting(fake):
    from types import SimpleNamespace
    services = make_services(agents=fake, config=SimpleNamespace(wait_max_seconds=30))
    seen = []
    fake.wait = lambda run_id, budget: (seen.append(budget) or Obj(id=run_id, status="done", last_log=[]), True, 1.0)
    tool("cloud_agent_status").handler({"run_id": "a-11111111", "wait_s": 100}, services, {})
    assert seen == [30]


class FakeModel(Obj):
    pass


def test_models_routes_and_status_models(services, fake):
    m = FakeModel(id="m-0a1b2c3d", name="Swift", model="q", base_url="http://h:1/v1",
                  key_secret="K", is_default=True)
    fake.models = lambda: [m]
    fake.model_view = lambda x: dict(x.to_dict(), endpoint_host="h:1", key_set=False)
    fake.detected_models = lambda: []
    out = route("GET", "/agent-models")(req(services))
    assert out == {"models": [dict(m.to_dict(), endpoint_host="h:1", key_set=False)],
                   "detected": [], "unavailable": [], "default_id": "m-0a1b2c3d"}
    st = tool("cloud_agent_status").handler({"models": True}, services, {})
    assert st["models"] == [{"id": "m-0a1b2c3d", "name": "Swift", "model": "q",
                             "endpoint_host": "h:1", "key_set": False, "is_default": True}]
    assert "base_url" not in st["models"][0] and "key_secret" not in st["models"][0]
    fake.save_model = lambda body: (fake.calls.append(("save", body)) or m)
    route("POST", "/agent-models")(req(services, body={"base_url": "http://h:1/v1", "model": "q"}))
    assert fake.calls[-1][0] == "save"
    with pytest.raises(ApiError) as e:
        route("POST", "/agent-models")(req(services, body={"api_key": "x"}))
    assert e.value.code == "unknown_parameter"


def test_remove_and_message_routes(services, fake):
    fake.remove = lambda run_id: {"removed": run_id, "note": "n"}
    assert route("DELETE", "/agents/{run_id}")(req(services, {"run_id": "a-11111111"}))["removed"] == "a-11111111"
    out = route("POST", "/agents/{run_id}/message")(req(services, {"run_id": "a-11111111"},
                                                        body={"message": "more", "max_turns": 3}))
    assert out["status"] == "running" and fake.calls[-1] == ("message", "a-11111111", "more", 3)
    with pytest.raises(ApiError) as e:
        route("POST", "/agents/{run_id}/message")(req(services, {"run_id": "a-11111111"},
                                                      body={"message": "x", "wait_s": 5}))
    assert e.value.code == "unknown_parameter"


def test_descriptions_are_short_and_say_the_essentials():
    for t in agents_api.tools():
        assert len(t.schema["description"].split()) <= 65, t.name
    spawn = tool("cloud_agent_spawn").schema["description"]
    assert "git repository on this computer" in spawn and "nothing is pushed" in spawn
    assert "default cloud-agent model unless model_id" in spawn and "wait_s" in spawn
    assert "nothing is merged" in tool("cloud_agent_collect").schema["description"].lower()
    assert "cloud_agent_message" not in tool("cloud_agent_message").schema["description"]


def test_message_rule_key_is_per_run_and_message():
    from cloud_compute_core.approval import rule_key
    a = rule_key("cloud_agent_message", {"run_id": "a-11111111", "message": "one"}, "x1")
    b = rule_key("cloud_agent_message", {"run_id": "a-11111111", "message": "two"}, "x1")
    c = rule_key("cloud_agent_message", {"run_id": "a-22222222", "message": "one"}, "x1")
    assert len({a, b, c}) == 3 and a.startswith("cloud-compute:cloud_agent_message:message:a-11111111:")
    plain = rule_key("cloud_agent_spawn", {"repo_path": "/r"}, "x1")
    chosen = rule_key("cloud_agent_spawn", {"repo_path": "/r", "model_id": "m-0a1b2c3d"}, "x1")
    assert plain != chosen and ":m-0a1b2c3d:" in chosen
