"""The model key of a cloud agent must not reach the chat.

* ``env_exec`` (and ``env_open terminal``) refuse a cloud agent's sandbox.
* ``env_files`` list / fetch from chat stay inside the agent's work folder.
* The redaction plan records the secret name the run really used, so output is still
  redacted after a Hermes restart.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cloud_compute_core import access_api, approval, files_api, instances_api
from cloud_compute_core import sandbox as sandbox_mod
from cloud_compute_core import secrets as secrets_mod
from cloud_compute_core.files import FileManager
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.models import HostLimits, VariableSpec
from cloud_compute_core.reaper import Reaper
from cloud_compute_core.sandbox import SandboxManager
from cloud_compute_core.templates import TemplateStore
from cloud_compute_core.testing import exec_result, sample_host, sample_instance, sample_template

SENTENCE = ("This environment belongs to a cloud agent run and holds its model key. Use "
            "cloud_agent_message to give it work, or open a terminal from the Agents tab.")
FAKE_KEY = "Zq81xLmPo93kd7Fh2aYtVbN6cWe4RuJs"            # made-up test value


def inspect_json(env=()):
    return json.dumps([{"Id": "f0" * 32, "State": {"Status": "running"},
                        "NetworkSettings": {"Ports": {}}, "Config": {"Env": list(env)}}])


def _sent_env(t):
    """``NAME=value`` lines of the last env file written to the host (what ``run`` got)."""
    for c in reversed(t.calls):
        if c.kind == "run" and c.argv[:2] == ["sh", "-c"] and "cat >" in c.argv[2] and c.stdin:
            return [ln for ln in c.stdin.decode("utf-8").splitlines() if "=" in ln]
    return []


@pytest.fixture
def env(services, fake_transports, fake_secrets, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "PORT_POLL_S", 0.0)
    monkeypatch.setattr(sandbox_mod, "PORT_WAIT_S", 0.05)
    vault = {"MY_LLM_KEY": FAKE_KEY}               # the real resolver honours the spawn override
    services.set("secrets", secrets_mod.SecretResolver(getter=lambda name: vault.get(name)))
    host = sample_host(name="x1carbon", runtime_pref="podman", limits=HostLimits(max_instances=4))
    services.store.upsert_host(host)
    templates = TemplateStore(services.store)
    services.set("templates", templates)
    templates.save(sample_template(
        id="my-agent", name="My agent", builtin=False, workdir="/work",
        agent={"mode": "hermes", "enabled": True},
        variables=[VariableSpec(name="CC_AGENT_API_KEY", source="secret:CC_AGENT_API_KEY",
                                scope="runtime")]))
    mgr = SandboxManager(services)
    services.set("sandboxes", mgr)
    services.set("files", FileManager(services))
    services.set("reaper", Reaper(services))
    t = fake_transports.get(host.id)
    t.script(lambda a, s: a[:2] == ["sh", "-c"], exec_result(stdout="/h/.cache/x.env\n"))
    t.script(("podman", "run"), exec_result(stdout="3f2a9c1d0b7e\n"))
    t.script(("podman", "inspect"), lambda a, s: exec_result(stdout=inspect_json(_sent_env(t))))
    for verb in ("logs", "stop", "start", "rm", "exec", "stats", "ps", "port"):
        t.script(("podman", verb), exec_result(stdout="ok\n"))
    return SimpleNamespace(services=services, host=host, t=t, store=services.store, mgr=mgr)


def _exec_calls(t):
    return [c for c in t.calls if c.kind == "run" and c.argv[:2] == ["podman", "exec"]]


def _instance(env, **kw):
    rec = sample_instance(host_id=env.host.id, status="running", **kw)
    env.store.upsert_instance(rec)
    return rec


# ------------------------------------------------------------------ env_exec / env_open


@pytest.mark.parametrize("make", [
    dict(id="i-0000a001", template_id="hermes-agent"),
    dict(id="i-0000a002", template_id="agent-script"),
    dict(id="i-0000a003", template_id="my-agent"),                     # user template with agent block
    dict(id="i-0000a004", template_id="python-dev", name="agent-a-1234abcd"),   # named like a run
], ids=["builtin-hermes", "builtin-script", "agent-block", "run-name"])
def test_env_exec_refuses_a_cloud_agent_sandbox(env, make):
    rec = _instance(env, **make)
    out = instances_api.env_exec({"instance_id": rec.id, "command": "echo $CC_AGENT_API_KEY"},
                                 env.services, {})
    assert out["error"]["message"] == SENTENCE
    assert _exec_calls(env.t) == []


def test_env_exec_refuses_when_only_a_run_record_names_the_environment(env):
    rec = _instance(env, id="i-0000a005", template_id="python-dev")
    env.store.put_record("agent_runs", "a-0000beef", {"instance_id": rec.id, "host_id": "x"})
    out = instances_api.env_exec({"instance_id": rec.id, "command": "env"}, env.services, {})
    assert out["error"]["message"] == SENTENCE and _exec_calls(env.t) == []


def test_env_exec_still_works_on_an_ordinary_environment(env):
    rec = _instance(env, id="i-0000b001", template_id="python-dev")
    out = instances_api.env_exec({"instance_id": rec.id, "command": "echo hi"}, env.services, {})
    assert out.get("rc") == 0 and "error" not in out
    assert len(_exec_calls(env.t)) == 1


def test_the_page_manager_can_still_run_a_command_in_an_agent_sandbox(env):
    rec = _instance(env, id="i-0000a006", template_id="hermes-agent")
    assert env.mgr.exec(rec.id, "echo hi").rc == 0          # the user's Terminal / agents code


def test_env_open_terminal_refuses_a_cloud_agent_sandbox(env):
    rec = _instance(env, id="i-0000a007", template_id="hermes-agent")
    opened = []
    env.services.set("access", SimpleNamespace(open_terminal=lambda **kw: opened.append(kw)))
    out = access_api.env_open({"kind": "terminal", "instance_id": rec.id}, env.services, {})
    assert out["error"]["message"] == SENTENCE and opened == []
    ordinary = _instance(env, id="i-0000b002", template_id="python-dev")
    env.services.set("access", SimpleNamespace(
        open_terminal=lambda **kw: SimpleNamespace(to_dict=lambda: {"opened": True})))
    ok = access_api.env_open({"kind": "terminal", "instance_id": ordinary.id}, env.services, {})
    assert ok.get("opened") is True


# ------------------------------------------------------------------ env_files stays in the work folder


@pytest.mark.parametrize("path", ["/proc/1", "/etc", "/home/agent", "/home/agent/.cc-agent/in",
                                  "/", "/tmp/.cc-agent", "/work/.hermes", "/workother"])
@pytest.mark.parametrize("action", ["list", "fetch"])
def test_env_files_from_chat_refuses_paths_outside_an_agents_work_folder(env, action, path):
    rec = _instance(env, id="i-0000a008", template_id="hermes-agent")
    out = files_api.env_files({"action": action, "instance_id": rec.id, "path": path},
                              env.services, {})
    assert "error" in out and "model key" in out["error"]["message"]
    assert _exec_calls(env.t) == []


def test_env_files_from_chat_allows_the_work_folder_of_an_agent_and_any_path_elsewhere(env):
    agent = _instance(env, id="i-0000a009", template_id="hermes-agent")
    ordinary = _instance(env, id="i-0000b003", template_id="python-dev")
    files = env.services.files
    files.refuse_outside_work(agent.id, None, "listed")
    files.refuse_outside_work(agent.id, "/work/repo/src", "saved")
    files.refuse_outside_work(ordinary.id, "/etc", "saved")
    files.refuse_outside_work(ordinary.id, "/", "saved")
    with pytest.raises(ApiError):
        files.refuse_outside_work(agent.id, "/etc", "saved")


# ------------------------------------------------------------------ redaction after a restart


def test_redaction_records_the_secret_name_the_run_really_used(env):
    with secrets_mod.variable_sources({"CC_AGENT_API_KEY": "MY_LLM_KEY"}):
        rec = env.mgr.spawn("my-agent", env.host.id, name="agent-a-1234abcd")
    shown = f"{FAKE_KEY}\n"
    values, names = env.mgr._redaction(rec)
    assert env.mgr._redact_output(shown, values, names) == "[redacted]\n"
    env.mgr._sent_values.clear()                      # what a Hermes restart does
    recorded = json.loads(env.store.kv_get(sandbox_mod.VARS_KEY_PREFIX + rec.id))
    assert recorded == [{"name": "CC_AGENT_API_KEY", "secret": "MY_LLM_KEY", "redact": True}]
    values, names = env.mgr._redaction(rec)
    assert env.mgr._redact_output(shown, values, names) == "[redacted]\n"
    assert FAKE_KEY not in env.mgr._redact_output(f"x {FAKE_KEY} y\n", values, names)
