"""Reply capture, follow-ups, blocking wait, remove, model choices, capacity message,
handoff-test template and the argv image build. Same emulated sandbox as test_agents_manager:
real sandbox scripts run with a local ``sh`` and real ``git``; ``hermes`` is a fake shim."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from cloud_compute_core import agents as agents_mod
from cloud_compute_core import secrets as secrets_mod
from cloud_compute_core.agents import AGENT_TEMPLATES, KEY_ENV, SCRIPT_REPLY, AgentRun
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.testing import exec_result

import importlib.util
import sys

_name = "cloud_compute_tests_agents_manager"
if _name not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _name, Path(__file__).with_name("test_agents_manager.py"))
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _mod
    _spec.loader.exec_module(_mod)
_m = sys.modules[_name]
SECRET_VALUE, KEY_NAME = _m.SECRET_VALUE, _m.KEY_NAME
_fake_clock, _git, _plain_hermes = _m._fake_clock, _m._git, _m._plain_hermes
_stored_run, _wait_done, needs_shell = _m._stored_run, _m._wait_done, _m.needs_shell
env = _m.env  # noqa: F811 - the shared fixture

SHIM = r"""#!/bin/sh
for a in "$@"; do printf '%s\n' "$a"; done >> "$HERMES_HOME/args.log"
echo "---" >> "$HERMES_HOME/args.log"
q=""
while [ $# -gt 0 ]; do
  case "$1" in --query-file) q="$2"; shift ;; esac
  shift
done
n=$(cat "$HERMES_HOME/count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$HERMES_HOME/count"
echo "turn $n" >> NOTES.md
git add NOTES.md && git commit -q -m "agent turn $n"
echo "Answer $n: $(head -c 60 "$q")"
[ -n "$NO_SESSION" ] || echo "session_id: 20261109_120000_abc$n" >&2
exit 0
"""


def _shim(e, tmp_path, *, session=True):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "hermes"
    shim.write_bytes(SHIM.encode("utf-8"))
    shim.chmod(0o755)
    e.emulator.extra_path = bin_dir
    if not session:
        for env_vars in e.sandboxes.envs.values():
            env_vars["NO_SESSION"] = "1"
    return bin_dir


def _override_aware(e):
    """FakeSecrets resolves like the real resolver, including spawn-time key overrides."""
    plain = e.secrets.resolve
    e.secrets.resolve = lambda variables, host_id: plain(
        secrets_mod.apply_variable_sources(variables), host_id)


def _hermes_ready(e, tmp_path, **kw):
    _shim(e, tmp_path, **kw)
    _override_aware(e)
    e.secrets.set(KEY_NAME, SECRET_VALUE)
    _plain_hermes(e)
    return e.mgr.save_model({"name": "Swift", "base_url": "http://192.0.2.10:8080/v1",
                             "model": "hyperqwen-swift", "key_secret": KEY_NAME})


def _hermes_dir(e, run) -> Path:
    return e.sandboxes.hermes_home(run.instance_id)


# --------------------------------------------------------------------------- reply and follow-up


@needs_shell
def test_hermes_reply_model_and_finished_event(env, tmp_path):
    e = env
    m = _hermes_ready(e, tmp_path)
    run = e.mgr.spawn("Describe this repo", "x1carbon", repo_path=str(e.repo),
                      criteria="a summary is written")
    assert run.status == "running", run.error
    assert (run.model, run.endpoint_host, run.model_id, run.key_secret) == (
        "hyperqwen-swift", "192.0.2.10:8080", m.id, KEY_NAME)
    assert run.max_turns == 30 and run.criteria == "a summary is written"
    # The chosen model's secret reaches the sandbox as CC_AGENT_API_KEY, and only that.
    assert e.sandboxes.envs[run.instance_id] == {KEY_ENV: SECRET_VALUE}
    goal = (e.sandboxes.state_dir(run.instance_id) / "goal.txt").read_text("utf-8")
    assert goal == "Describe this repo\n\nDone when: a summary is written"

    done = _wait_done(e.mgr, run.id)
    assert done.status == "done" and done.reply.startswith("Answer 1: Describe this repo")
    assert done.commits_ahead == 1
    sid = (e.sandboxes.state_dir(run.instance_id) / "session_id").read_text().strip()
    assert sid == "20261109_120000_abc1"
    finished = e.events.payloads("agent.finished")
    assert finished == [{"id": run.id, "status": "done", "host_id": "x1carbon",
                         "reply_preview": agents_mod._short(done.reply, 200)}]
    notes = [p for p in e.events.payloads("notify") if p["ref"] == {"kind": "agent", "id": run.id}]
    assert len(notes) == 1 and notes[0]["level"] == "info"
    assert SECRET_VALUE not in repr(done.to_dict())


@needs_shell
def test_follow_up_resumes_the_same_session_and_collects_again(env, tmp_path):
    e = env
    _hermes_ready(e, tmp_path)
    run = e.mgr.spawn("First task", "x1carbon", repo_path=str(e.repo))
    _wait_done(e.mgr, run.id)
    first = e.mgr.collect(run.id)
    assert first.commits_ahead == 1

    again = e.mgr.message(run.id, "Now add a second line")
    assert (again.status, again.turns, again.max_turns) == ("running", 2, 30)
    done = _wait_done(e.mgr, run.id)
    assert done.status == "done" and done.turns == 2
    assert done.reply.startswith("Answer 2: Now add a second line")
    args = (_hermes_dir(e, run) / "args.log").read_text("utf-8").split("---\n")
    assert "--resume" not in args[0]
    # the resumed session keeps the limit it started with: Hermes refuses the flag on --resume
    assert "--resume\n20261109_120000_abc1\n" in args[1] and "--max-turns" not in args[1]
    assert "--max-turns\n30\n" in args[0]
    assert "--yolo" not in "".join(args)

    second = e.mgr.collect(run.id)
    assert second.commits_ahead == 2 and second.ref == first.ref
    wt = Path(second.worktree_path)
    assert (wt / "NOTES.md").read_text("utf-8").splitlines() == ["turn 1", "turn 2"]
    assert len(e.events.payloads("agent.finished")) == 2


@needs_shell
def test_follow_up_without_a_session_starts_fresh_with_context(env, tmp_path):
    e = env
    _hermes_ready(e, tmp_path)
    run = e.mgr.spawn("First task", "x1carbon", repo_path=str(e.repo))
    _wait_done(e.mgr, run.id)
    (e.sandboxes.state_dir(run.instance_id) / "session_id").unlink()  # none reported
    e.mgr.message(run.id, "More please")
    done = _wait_done(e.mgr, run.id)
    args = (_hermes_dir(e, run) / "args.log").read_text("utf-8").split("---\n")[1]
    assert "--resume" not in args and "prompt_fresh.txt" in args
    assert "The earlier task was:" in done.reply or done.reply.startswith("Answer 2: This is a follow-up")


def test_message_refusals(env):
    e = env
    running = _stored_run(e, status="running")
    e.mgr._refresh = lambda run, **kw: None                    # still working on its turn
    with pytest.raises(ApiError) as exc:
        e.mgr.message(running.id, "x")
    assert exc.value.code == "conflict" and "still working" in exc.value.message
    gone = _stored_run(e, status="collected", instance_id="",
                       collected_ref="refs/heads/cloud/a-1/cloud/g")
    with pytest.raises(ApiError) as exc:
        e.mgr.message(gone.id, "continue")
    msg = exc.value.message
    assert exc.value.code == "conflict" and "no longer exists" in msg
    assert "cloud_agent_spawn" in msg and "base='cloud/a-1/cloud/g'" in msg
    with pytest.raises(ApiError) as exc:
        e.mgr.message(gone.id, "   ")
    assert exc.value.code == "invalid"


# --------------------------------------------------------------------------- wait


@needs_shell
def test_wait_returns_when_the_run_finishes(env):
    e = env
    run = e.mgr.spawn("Write a short result note", "x1carbon", repo_path=str(e.repo),
                      template="agent-script")
    got, finished, waited = e.mgr.wait(run.id, 60)
    assert finished is True and got.status == "done" and got.reply == SCRIPT_REPLY
    assert waited < 60 and e.sandboxes.touched


def test_wait_times_out_on_a_running_run(env, fake_transports):
    e = env
    clk = _fake_clock(e)
    run = _stored_run(e, status="running")
    checks = []
    e.mgr._refresh = lambda run, **kw: checks.append(run.id)   # the runner keeps running
    got, finished, waited = e.mgr.wait(run.id, 20)
    assert finished is False and got.status in ("running",) and waited >= 20
    assert clk[0] >= 1020 and 3 <= len(checks) <= 5                # one check every 6 s


# --------------------------------------------------------------------------- remove


def test_remove_needs_stop_first_and_never_touches_branches(env):
    e = env
    run = _stored_run(e, status="done")
    with pytest.raises(ApiError) as exc:
        e.mgr.remove(run.id)
    assert exc.value.code == "conflict" and "Press Stop first" in exc.value.message
    e.mgr.stop(run.id)
    _git(e.repo, "branch", f"cloud/{run.id}/keep")
    out = e.mgr.remove(run.id)
    assert out["removed"] == run.id and "not changed" in out["note"]
    assert e.services.store.get_record("agent_runs", run.id) is None
    assert _git(e.repo, "branch", "--list", f"cloud/{run.id}/keep")
    active = _stored_run(e, status="running")
    with pytest.raises(ApiError):
        e.mgr.remove(active.id)


# --------------------------------------------------------------------------- capacity


def test_capacity_failure_is_plain_and_keeps_a_rerunnable_record(env):
    e = env
    e.sandboxes.spawn_error = ApiError(409, "limit_reached", "host x1carbon already runs 4 of 4 instances")
    run = e.mgr.spawn("Do it", "x1carbon", repo_path=str(e.repo), template="agent-script",
                      max_turns=12, branch="feature-x")
    assert run.status == "failed"
    host = e.services.store.get_host("x1carbon")
    assert run.error == (f"{host.name or host.id} already runs 4 of 4 environments. Stop or delete "
                         "one, or raise Max environments for this host.")
    stored = AgentRun.from_dict(e.services.store.get_record("agent_runs", run.id))
    assert (stored.goal, stored.host_id, stored.repo_path, stored.branch, stored.template_id,
            stored.max_turns) == ("Do it", "x1carbon", str(e.repo), "feature-x", "agent-script", 12)


# --------------------------------------------------------------------------- model choices


def test_saved_models_crud_and_default(env):
    e = env
    a = e.mgr.save_model({"base_url": "https://llm.example.net/v1", "model": "m1"})
    assert a.is_default and a.key_secret == "CC_AGENT_API_KEY" and a.name == "m1 at llm.example.net"
    b = e.mgr.save_model({"name": "B", "base_url": "http://10.0.0.5:8080/v1", "model": "m2",
                          "key_secret": KEY_NAME})
    assert not b.is_default
    e.mgr.set_default_model(b.id)
    assert [m.id for m in e.mgr.models()] == [b.id, a.id] and e.mgr.default_model().id == b.id
    view = e.mgr.model_view(b)
    assert view["key_set"] is False and view["endpoint_host"] == "10.0.0.5:8080"
    e.secrets.set(KEY_NAME, SECRET_VALUE)
    assert e.mgr.model_view(b)["key_set"] is True and SECRET_VALUE not in repr(e.mgr.model_view(b))
    e.mgr.save_model({"id": b.id, "name": "B2", "base_url": "http://10.0.0.5:8080/v1", "model": "m3",
                      "key_secret": KEY_NAME})
    assert e.mgr.model(b.id).model == "m3" and e.mgr.model(b.id).is_default
    e.mgr.delete_model(b.id)
    assert e.mgr.default_model().id == a.id
    for bad in ({"base_url": "https://user:pw@llm.example.net/v1", "model": "m"},
                {"base_url": "ftp://x/v1", "model": "m"},
                {"base_url": "https://x/v1", "model": "bad model"},
                {"base_url": "https://x/v1", "model": "m", "key_secret": "sk-not-a-name!"}):
        with pytest.raises(ApiError) as exc:
            e.mgr.save_model(bad)
        assert exc.value.code == "invalid"
    with pytest.raises(ApiError) as exc:
        e.mgr.model("m-00000000")
    assert exc.value.code == "not_found" and a.id in exc.value.message


def _resolve(e, model_id=None):
    host = e.services.store.get_host("x1carbon")
    tpl = e.templates.get("hermes-agent")
    cfg = e.mgr._agent_cfg(tpl)
    e.mgr._check_template(tpl, cfg, host, model_id)
    return cfg


def test_resolution_order(env):
    e = env
    e.secrets.set("CC_AGENT_API_KEY", SECRET_VALUE)
    e.secrets.set(KEY_NAME, SECRET_VALUE)
    tpl = _plain_hermes(e)
    with pytest.raises(ApiError) as exc:                       # nothing anywhere
        _resolve(e)
    assert exc.value.code == "needs_setup"
    assert "Compute, Agents, New agent run, Model" in exc.value.message
    e.services.config.agent_base_url = "http://10.0.0.9:8080/v1"
    e.services.config.agent_model = "from-settings"
    assert _resolve(e)["model"] == "from-settings"             # settings
    d = e.mgr.save_model({"base_url": "http://10.0.0.7:8080/v1", "model": "from-default",
                          "key_secret": KEY_NAME})
    cfg = _resolve(e)
    assert (cfg["model"], cfg["key_secret"], cfg["model_id"]) == ("from-default", KEY_NAME, d.id)
    tpl.agent = dict(tpl.agent, base_url="http://10.0.0.8:8080/v1", model="from-template")
    e.templates.save(tpl)
    assert _resolve(e)["model"] == "from-template"             # template beats default
    other = e.mgr.save_model({"base_url": "http://10.0.0.6:8080/v1", "model": "picked"})
    assert _resolve(e, other.id)["model"] == "picked"          # model_id beats all
    with pytest.raises(ApiError) as exc:
        _resolve(e, "m-deadbeef")
    assert exc.value.code == "not_found"


def test_missing_key_is_needs_setup_with_where_to_add_it(env):
    e = env
    _plain_hermes(e)
    m = e.mgr.save_model({"base_url": "http://10.0.0.7:8080/v1", "model": "x"})
    with pytest.raises(ApiError) as exc:
        _resolve(e, m.id)
    assert exc.value.code == "needs_setup" and "Cloud agent model key" in exc.value.message


def test_spawn_approval_names_model_endpoint_and_secret(env):
    e = env
    _plain_hermes(e)
    m = e.mgr.save_model({"base_url": "http://10.0.0.7:8080/v1", "model": "qwen-x",
                          "key_secret": KEY_NAME})
    text = e.mgr.describe_spawn({"task": "t", "host_id": "x1carbon", "repo_path": "/r/p",
                                 "model_id": m.id})
    assert "model qwen-x at 10.0.0.7:8080" in text and f"read from the secret {KEY_NAME}" in text
    script = e.mgr.describe_spawn({"task": "t", "host_id": "x1carbon", "repo_path": "/r/p",
                                   "template": "agent-script"})
    assert "handoff test" in script and "Sends no secrets" in script


def test_message_approval_text(env):
    e = env
    run = _stored_run(e, status="done")
    text = e.mgr.describe_message({"run_id": run.id, "message": "x" * 200})
    assert text == (f'Sends a follow-up to cloud agent run {run.id} on x1carbon: "{"x" * 200}". '
                    "The agent continues in the same sandbox and branch.")


# --------------------------------------------------------------------------- reply text


def test_reply_is_redacted_and_cut(env):
    e = env
    e.secrets.set("CC_AGENT_API_KEY", SECRET_VALUE)
    mask = e.mgr._mask_fn(AgentRun(id="a-0123abcd", instance_id="", host_id="x1carbon", goal="g",
                                   repo_path="/r", branch="b", base_sha="s", head_sha="s",
                                   commits_ahead=0, status="running", started_at="",
                                   ended_at=None, session_id=None)).fn
    text = e.mgr._clean_reply(f"key {SECRET_VALUE} " + "y" * 5000, mask)
    assert SECRET_VALUE not in text and "[redacted]" in text
    assert text.endswith("reply.txt in the sandbox.]") and "Reply cut at 4000 characters" in text
    assert e.mgr._clean_reply("   ", mask) is None


# --------------------------------------------------------------------------- templates and image


def test_handoff_test_template_and_legacy_upgrade(env):
    e = env
    script = next(t for t in AGENT_TEMPLATES if t.id == "agent-script")
    assert script.name == "Handoff test (no model)"
    assert script.description == ("Commits a marker file to prove the branch round trip. Does not "
                                  "use a model and ignores the task text.")
    old = copy.deepcopy(script)
    old.name, old.description = agents_mod._LEGACY_TEXT["agent-script"][0]
    e.templates.save(old)
    e.mgr.ensure_templates()
    assert e.templates.get("agent-script").name == "Handoff test (no model)"
    edited = copy.deepcopy(script)
    edited.name, edited.description = "Mine", "my own"
    e.templates.save(edited)
    e.mgr._templates_ready = False
    e.mgr.ensure_templates()
    assert e.templates.get("agent-script").name == "Mine"      # an edited copy stays


def test_image_build_is_a_plain_argv_without_a_shell(env):
    e = env
    t = e.transport
    t._run_rules.clear()
    t.script(("podman", "image", "exists"), exec_result(rc=1))
    t.script(("podman", "build"), exec_result(rc=0))
    host = e.services.store.get_host("x1carbon")
    tpl = next(x for x in AGENT_TEMPLATES if x.id == "agent-script")
    assert e.mgr.ensure_image(host, tpl) is True
    build = [a for a in t.argvs() if a[:2] == ["podman", "build"]]
    assert build, t.argvs()
    argv = build[-1]
    assert argv[0] == "podman" and "sh" not in argv and argv[-1] == "-"
    assert argv[argv.index("--build-arg") + 1].startswith("BASE=")


def test_runner_script_reads_the_reply_and_session():
    script = agents_mod._runner_script("a-0000abcd", "hermes", 9)
    assert '>"$S/reply.txt"' in script and "--resume" in script and "session_id" in script
    assert "prompt_fresh.txt" in script and "--max-turns 9" in script
    handoff = agents_mod._runner_script("a-0000abcd", "script", 9)
    assert SCRIPT_REPLY in handoff and "hermes" not in handoff


def test_variable_sources_override_is_scoped():
    from cloud_compute_core.models import VariableSpec
    variables = [VariableSpec(name=KEY_ENV, source="secret:CC_AGENT_API_KEY", scope="runtime"),
                 VariableSpec(name="OTHER", source="literal:x", scope="runtime")]
    resolver = secrets_mod.SecretResolver(getter={"CC_AGENT_API_KEY": "a", KEY_NAME: "b"}.get)
    assert resolver.resolve(variables, "h")[0] == {KEY_ENV: "a", "OTHER": "x"}
    with secrets_mod.variable_sources({KEY_ENV: KEY_NAME, "OTHER": KEY_NAME}):
        env_vars, previews = resolver.resolve(variables, "h")
    assert env_vars == {KEY_ENV: "b", "OTHER": "x"}         # literal sources are never replaced
    assert resolver.resolve(variables, "h")[0][KEY_ENV] == "a"
    with secrets_mod.variable_sources({KEY_ENV: "not a name"}):
        assert resolver.resolve(variables, "h")[0][KEY_ENV] == "a"
