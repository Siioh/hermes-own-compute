"""The model key of a cloud agent run is hidden in everything a run shows or stores.

The real SandboxManager and AgentManager run against the fake runtime and fake secrets. Each
"other process" is a new pair of managers (memory empty) with its own secret scope, as a
``hermes chat`` next to the gateway, a restarted gateway or another profile would be.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cloud_compute_core import agents_api
from cloud_compute_core import sandbox as sandbox_mod
from cloud_compute_core import secrets as secrets_mod
from cloud_compute_core.agents import (
    AGENT_TEMPLATES, DEFAULT_KEY_SECRET, KEY_ENV, AgentManager, AgentRun,
)
from cloud_compute_core.models import HostLimits
from cloud_compute_core.sandbox import SandboxManager
from cloud_compute_core.templates import TemplateStore
from cloud_compute_core.testing import FakeSecrets, exec_result, sample_host

KEY = "Zq81xLmPo93kd7Fh2aYtVbN6cWe4RuJs"          # made-up test value (ws-scan:allow)
OTHER = "Pp40wErTy12uIoQa7sDfGh3jKlZxCvBn"        # made-up test value (ws-scan:allow)
SECRET_NAME = "MODEL_B_KEY"                       # a per-model secret name, not the default
RUN_ID = "a-0000beef"
WITHHELD_START = "Log withheld: cloud agent run a-0000beef used a model key"


@pytest.fixture
def env(services, fake_transports, fake_secrets, fake_events, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "PORT_POLL_S", 0.0)
    monkeypatch.setattr(sandbox_mod, "PORT_WAIT_S", 0.05)
    vault = {SECRET_NAME: KEY}
    services.set("secrets", secrets_mod.SecretResolver(getter=lambda name: vault.get(name)))
    host = sample_host(name="x1carbon", runtime_pref="podman", limits=HostLimits(
        max_instances=4, allow_agents=True))
    services.store.upsert_host(host)
    templates = TemplateStore(services.store)
    services.set("templates", templates)
    tpl = next(t for t in AGENT_TEMPLATES if t.id == "hermes-agent")
    templates.save(tpl)
    sandboxes = SandboxManager(services)
    services.set("sandboxes", sandboxes)
    t = fake_transports.get(host.id)

    def sent_env():
        for c in reversed(t.calls):
            if c.kind == "run" and c.argv[:2] == ["sh", "-c"] and "cat >" in c.argv[2] and c.stdin:
                return [ln for ln in c.stdin.decode("utf-8").splitlines() if "=" in ln]
        return []

    def inspect_out(argv, stdin):
        return exec_result(stdout=json.dumps([{
            "Id": "f0" * 32, "State": {"Status": "running"}, "NetworkSettings": {"Ports": {}},
            "Config": {"Env": ["PATH=/usr/bin", *state["env"]]}}]))

    state = {"env": [], "poll": ""}
    t.script(lambda a, s: a[:2] == ["sh", "-c"], exec_result(stdout="/h/.cache/x.env\n"))
    t.script(("podman", "run"), exec_result(stdout="3f2a9c1d0b7e\n"))
    t.script(("podman", "inspect"), inspect_out)
    for verb in ("logs", "stop", "start", "rm", "stats", "ps", "port"):
        t.script(("podman", verb), exec_result(stdout="ok\n"))
    t.script(lambda a, s: a[:2] == ["podman", "exec"] and "==log" in " ".join(a),
             lambda a, s: exec_result(stdout=state["poll"]))
    t.script(("podman", "exec"), exec_result(stdout="ok\n"))
    mgr = AgentManager(services)
    with secrets_mod.variable_sources({KEY_ENV: SECRET_NAME}):
        inst = sandboxes.spawn("hermes-agent", host.id, name=f"agent-{RUN_ID}")
    state["env"] = sent_env()
    assert f"{KEY_ENV}={KEY}" in state["env"]
    return SimpleNamespace(services=services, host=host, t=t, store=services.store, inst=inst,
                           mgr=mgr, events=fake_events, state=state, vault=vault)


def _run(env, **kw) -> AgentRun:
    base = dict(id=RUN_ID, instance_id=env.inst.id, host_id=env.host.id, goal="fix it",
                repo_path="/r", branch="b", base_sha="s" * 40, head_sha="s" * 40,
                commits_ahead=0, status="running", started_at="2026-11-10T10:00:00Z",
                ended_at=None, session_id=None, mode="hermes", key_secret=SECRET_NAME,
                state_dir="/home/agent/.cc-agent", text_masked=True)
    base.update(kw)
    run = AgentRun(**base)
    env.store.put_record("agent_runs", run.id, run.to_dict(), host_id=run.host_id,
                         status=run.status)
    return run


def _poll(*, log=(), reply=None, code=0):
    lines = [f"exit={code if reply is not None else 'running'}", "head=", "==log", *log]
    if reply is not None:
        lines += ["==reply", reply]
    return "\n".join(lines) + "\n"


def _other_process(env, secrets):
    """A new pair of managers (nothing in memory) whose secret scope is ``secrets``."""
    env.services.set("secrets", secrets)
    env.services.set("sandboxes", SandboxManager(env.services))
    mgr = AgentManager(env.services)
    env.services.set("agents", mgr)
    return mgr


def _stored(env) -> str:
    return json.dumps(env.store.get_record("agent_runs", RUN_ID))


def _everything(env, shown) -> str:
    return (json.dumps(shown.to_dict()) + _stored(env) + repr(env.events.events))


def _finish(env, mgr):
    env.state["poll"] = _poll(log=[f"tool printed {KEY}", "plain line"],
                              reply=f"The key is {KEY}.\nDone.")
    mgr._refreshed.clear()
    return mgr.status(RUN_ID)[0]


CASES = {
    "spawning-process": lambda env: env.mgr,
    "no-secret-scope": lambda env: _other_process(env, FakeSecrets()),
    "restarted-same-scope": lambda env: _other_process(
        env, secrets_mod.SecretResolver(getter=lambda n: {SECRET_NAME: KEY}.get(n))),
    "different-value-same-name": lambda env: _other_process(env, FakeSecrets({SECRET_NAME: OTHER})),
    "secret-deleted": lambda env: _other_process(env, FakeSecrets({"UNRELATED": "x"})),
}


@pytest.mark.parametrize("case", list(CASES))
def test_the_bare_key_is_hidden_in_reply_and_log_in_every_process(env, case):
    _run(env)
    mgr = CASES[case](env)
    shown = _finish(env, mgr)
    assert shown.status == "done"
    assert shown.reply == "The key is [redacted].\nDone."
    assert shown.last_log == ["tool printed [redacted]", "plain line"]
    blob = _everything(env, shown)
    assert KEY not in blob and OTHER not in blob


def test_the_stored_record_never_holds_the_key(env):
    _run(env)
    mgr = _other_process(env, FakeSecrets())          # cannot resolve the key by name
    _finish(env, mgr)
    stored = env.store.get_record("agent_runs", RUN_ID)
    assert KEY not in json.dumps(stored)
    assert stored["reply"] == "The key is [redacted].\nDone." and stored["text_masked"] is True
    # the notification and the event carry the hidden text only
    assert KEY not in repr(env.events.events)
    assert any("[redacted]" in str(p.get("reply_preview")) for p in
               env.events.payloads("agent.finished"))


def test_the_default_secret_name_is_hidden_too(env):
    _run(env, key_secret=None)
    mgr = _other_process(env, FakeSecrets({DEFAULT_KEY_SECRET: OTHER}))
    env.state["poll"] = _poll(log=[f"a {OTHER} b {KEY}"], reply=f"{OTHER} {KEY}")
    shown = _finish_text(env, mgr)
    assert shown.reply == "[redacted] [redacted]" and OTHER not in _everything(env, shown)


def _finish_text(env, mgr):
    mgr._refreshed.clear()
    return mgr.status(RUN_ID)[0]


@pytest.mark.parametrize("case", ["spawning-process", "no-secret-scope"])
def test_ordinary_text_is_left_alone(env, case):
    _run(env)
    mgr = CASES[case](env)
    text = ("Set api_key in the config, check secret_set=yes and the password policy. "
            "token budget: 512 keys: 3 | the key point is the secret sauce.")
    env.state["poll"] = _poll(log=[text], reply=text)
    shown = _finish_text(env, mgr)
    assert shown.reply == text and shown.last_log == [text]


def test_secret_shaped_tokens_are_still_hidden(env):
    _run(env)
    mgr = _other_process(env, FakeSecrets())
    token = "sk-" + "a1B2c3D4e5F6g7H8i9J0"                  # made-up, vendor-shaped
    env.state["poll"] = _poll(log=[token], reply=f"use {token}")
    shown = _finish_text(env, mgr)
    assert token not in shown.reply and token not in _everything(env, shown)


def test_a_sandbox_that_cannot_be_read_withholds_the_log_and_stores_no_text(env):
    _run(env)
    mgr = _other_process(env, FakeSecrets())
    env.t.script(lambda a, s: a[:2] == ["podman", "inspect"],
                 exec_result(rc=125, stderr="Error: no such container"))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    shown = _finish(env, mgr)
    sentence = ("Log withheld: cloud agent run a-0000beef used a model key that is hidden in "
                "replies and logs, and this computer cannot read it back from the sandbox right "
                "now to hide it. Check that the host x1carbon is reachable and that the sandbox "
                "still exists, then try again.")
    assert shown.last_log == [sentence]
    assert shown.reply is None and shown.status == "running"      # read again next time
    assert KEY not in _everything(env, shown)


def test_the_withheld_run_recovers_when_the_sandbox_can_be_read(env):
    _run(env)
    mgr = _other_process(env, FakeSecrets())
    env.t.script(lambda a, s: a[:2] == ["podman", "inspect"], exec_result(rc=1))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    assert _finish(env, mgr).last_log[0].startswith("Log withheld: ")
    env.t._run_rules.pop(0)                                       # the runtime answers again
    shown = _finish(env, mgr)
    assert shown.status == "done" and shown.reply == "The key is [redacted].\nDone."


def test_a_failure_text_with_the_key_is_hidden_before_it_is_stored(env):
    run = _run(env)
    mgr = _other_process(env, FakeSecrets())
    mgr._fail(run, f"boom {KEY} and more", SECRET_NAME, emit=False)
    assert run.error == "boom [redacted] and more" and KEY not in _stored(env)
    run2 = _run(env)
    env.t.script(lambda a, s: a[:2] == ["podman", "inspect"], exec_result(rc=1))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    mgr2 = _other_process(env, FakeSecrets())
    mgr2._fail(run2, f"boom {KEY}", SECRET_NAME, emit=False)
    assert run2.error.startswith("Error withheld: ") and KEY not in _stored(env)


def test_the_chat_tool_and_the_route_show_the_hidden_text(env):
    _run(env)
    mgr = _other_process(env, FakeSecrets())
    _finish(env, mgr)
    out = agents_api._tool_status({"run_id": RUN_ID}, env.services, {})
    assert out["reply"] == "The key is [redacted].\nDone." and KEY not in json.dumps(out)
    listed = agents_api.list_agents(SimpleNamespace(services=env.services, query={}, body=None))
    assert KEY not in json.dumps(listed)


# ------------------------------------------------------------------ records of earlier versions


def _gone(env, how="deleted"):
    """The sandbox of the run no longer exists (collected and deleted)."""
    if how == "deleted":
        env.store.delete_instance(env.inst.id)
        return
    rec = env.store.get_instance(env.inst.id)
    rec.status = "destroyed"
    env.store.upsert_instance(rec)


def _old_run(env, **kw):
    stored = dict(text_masked=None, status="done", reply=f"old reply {KEY}",
                  last_log=[f"old log {KEY}", "second"], error=f"old error {KEY}",
                  stop_note=None)
    stored.update(kw)
    return _run(env, **stored)


def test_an_old_record_is_hidden_with_the_values_read_from_its_sandbox(env):
    _old_run(env)
    mgr = _other_process(env, FakeSecrets())
    shown = mgr.status(RUN_ID)[0]
    assert shown.reply == "old reply [redacted]" and shown.last_log == ["old log [redacted]", "second"]
    assert shown.error == "old error [redacted]"
    assert KEY not in json.dumps(shown.to_dict())


def test_an_old_record_of_a_gone_sandbox_is_hidden_by_resolving_the_secret_by_name(env):
    _old_run(env, status="collected")
    _gone(env)
    mgr = _other_process(env, secrets_mod.SecretResolver(getter=lambda n: {SECRET_NAME: KEY}.get(n)))
    shown = mgr.status(RUN_ID)[0]
    assert shown.reply == "old reply [redacted]" and shown.last_log[0] == "old log [redacted]"


@pytest.mark.parametrize("how", ["deleted", "destroyed"])
def test_an_old_record_with_nothing_to_resolve_withholds_reply_and_log(env, how):
    _old_run(env, status="collected")
    _gone(env, how)
    mgr = _other_process(env, FakeSecrets())
    shown = mgr.status(RUN_ID)[0]
    sentence = ("{} withheld: cloud agent run a-0000beef was recorded before the model key was "
                "hidden in replies and logs, its sandbox is gone, and the secret MODEL_B_KEY is "
                "not set on this computer, so the text cannot be checked for the key. Set the "
                "secret MODEL_B_KEY to the key the run used and open the run again, or start a "
                "new run.")
    assert shown.reply == sentence.format("Reply") and shown.last_log == [sentence.format("Log")]
    assert KEY not in json.dumps(shown.to_dict())
    assert shown.error == sentence.format("Error")


def test_an_old_script_record_needs_no_key_and_is_shown(env):
    _old_run(env, status="collected", mode="script", key_secret=None, reply="Handoff test",
             last_log=["plain"], error=None)
    _gone(env)
    mgr = _other_process(env, FakeSecrets())
    shown = mgr.status(RUN_ID)[0]
    assert shown.reply == "Handoff test" and shown.last_log == ["plain"]


def test_a_new_record_of_a_gone_sandbox_is_shown_as_stored(env):
    _run(env, status="collected", reply="stored [redacted] text", last_log=["x [redacted]"],
         text_masked=True)
    _gone(env)
    mgr = _other_process(env, FakeSecrets())
    shown = mgr.status(RUN_ID)[0]
    assert shown.reply == "stored [redacted] text" and shown.last_log == ["x [redacted]"]


def test_an_old_record_whose_sandbox_cannot_be_read_withholds_reply_and_log(env):
    _old_run(env)
    mgr = _other_process(env, FakeSecrets())
    env.t.script(lambda a, s: a[:2] == ["podman", "inspect"], exec_result(rc=1))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    shown = mgr.status(RUN_ID)[0]
    assert shown.reply.startswith("Reply withheld: cloud agent run a-0000beef used a model key")
    assert shown.last_log[0].startswith(WITHHELD_START.replace("Log", "Log"))
    assert KEY not in json.dumps(shown.to_dict())
