"""A failed run says what went wrong and can be started again."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from cloud_compute_core import agents as agents_mod
from cloud_compute_core import agents_api
from cloud_compute_core.agents import diagnose_failure
from cloud_compute_core.interfaces import ApiError, ApiRequest
from cloud_compute_core.models import HostLimits, utc_now_iso
from cloud_compute_core.testing import sample_host


def _load(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


_am = _load("cloud_compute_tests_agents_manager", "test_agents_manager.py")
SECRET_VALUE, KEY_NAME = _am.SECRET_VALUE, _am.KEY_NAME
needs_shell, _wait_done, _git = _am.needs_shell, _am._wait_done, _am._git
_stored_run, _configure_hermes = _am._stored_run, _am._configure_hermes
env = _am.env  # noqa: F811 - the shared agent fixture

FAIL_404 = r"""#!/bin/sh
echo "custom didn't answer after 3 attempts - it looks temporarily unavailable. Wait a minute and send /retry, or switch models with /model."
echo "To add a fallback run: hermes fallback add"
echo 'Provider said: HTTP 404: {"error":"please check the model you provided"}'
echo "session_id: 20261110_000000_f404" >&2
exit 1
"""


def _shim(e, tmp_path, body: str) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "hermes"
    shim.write_bytes(body.encode())
    shim.chmod(0o755)
    e.emulator.extra_path = bin_dir


def _override_aware(e):
    from cloud_compute_core import secrets as secrets_mod
    plain = e.secrets.resolve
    e.secrets.resolve = lambda variables, host_id: plain(
        secrets_mod.apply_variable_sources(variables), host_id)


def _ready(e, tmp_path, body: str):
    _shim(e, tmp_path, body)
    _override_aware(e)
    e.secrets.set(KEY_NAME, SECRET_VALUE)
    _am._plain_hermes(e)
    return e.mgr.save_model({"name": "Swift", "base_url": "http://192.0.2.10:8080/v1",
                             "model": "hyperqwen-swift", "key_secret": KEY_NAME})


# --------------------------------------------------------------------------- B: the cause


@pytest.mark.parametrize("text,kind,needle", [
    ('Provider said: HTTP 404: {"error":"please check the model you provided"}',
     "model_not_found", "answered 404: model m1 was not found there"),
    ("Error code: 401 - {'error': {'message': 'Invalid API key'}}", "bad_key",
     "refused the key (HTTP 401)"),
    ("HTTP 403: forbidden", "bad_key", "Check the key secret MY_KEY"),
    ("Provider said: HTTP 429: rate limit reached", "rate_limit", "answered 429"),
    ("HTTP 402: insufficient_quota", "rate_limit", "answered 402"),
    ("HTTP 503: upstream unavailable", "server", "answered 503"),
    ("HTTP 400: this model's maximum context length is 8192 tokens", "context",
     "needs more room"),
    ("httpx.ConnectError: [Errno 111] Connection refused", "unreachable",
     "could not reach the model endpoint"),
    ("openai.APIConnectionError: Connection error.", "unreachable", "refused or timed out"),
    ("ReadTimeout: timed out", "unreachable", "reachable from the host"),
    ("HTTP 418: teapot", "endpoint", "answered 418"),
])
def test_diagnose_failure_names_the_cause_and_the_next_step(text, kind, needle):
    got = diagnose_failure(text, model="m1", endpoint="llm.example.net", key_secret="MY_KEY",
                           host_id="x1carbon")
    assert got is not None and got[0] == kind and needle in got[1]
    assert "start the run again" in got[1] or "start it again" in got[1] \
        or "Start the run again" in got[1]
    assert "/retry" not in got[1] and "/model" not in got[1]


def test_the_404_sentence_is_the_one_users_read():
    kind, text = diagnose_failure(
        'Provider said: HTTP 404: {"error":"please check the model you provided"}',
        model="deepseek-ai/Fastest", endpoint="inference.baseten.co", key_secret=KEY_NAME)
    assert text == ('The model endpoint inference.baseten.co answered 404: model '
                    'deepseek-ai/Fastest was not found there (or the endpoint address is '
                    'wrong). It said: "please check the model you provided". Choose another '
                    'model and start the run again.')


def test_no_known_cause_gives_none():
    assert diagnose_failure("Traceback: ZeroDivisionError", model="m", endpoint="h",
                            key_secret="K") is None
    assert diagnose_failure("", model=None, endpoint=None, key_secret=None) is None


@needs_shell
def test_a_wrong_model_run_fails_with_the_cause_in_error(env, tmp_path):
    e = env
    _ready(e, tmp_path, FAIL_404)
    run = e.mgr.spawn("Describe this repo", "x1carbon", repo_path=str(e.repo))
    done = _wait_done(e.mgr, run.id)
    assert done.status == "failed" and done.exit_code == 1
    assert done.error_kind == "model_not_found"
    assert done.error == (
        'The model endpoint 192.0.2.10:8080 answered 404: model hyperqwen-swift was not found '
        'there (or the endpoint address is wrong). It said: "please check the model you '
        'provided". Choose another model and start the run again.')
    # The reply keeps the real content and drops the Hermes chat advice that does not apply.
    assert "Provider said: HTTP 404" in done.reply
    assert "/retry" not in done.reply and "/model" not in done.reply
    assert "hermes fallback" not in done.reply
    assert SECRET_VALUE not in repr(done.to_dict())


@needs_shell
def test_an_unexplained_failure_keeps_the_exit_code_text(env, tmp_path):
    e = env
    _ready(e, tmp_path, "#!/bin/sh\necho 'Traceback: boom'\nexit 3\n")
    run = e.mgr.spawn("Describe this repo", "x1carbon", repo_path=str(e.repo))
    done = _wait_done(e.mgr, run.id)
    assert done.status == "failed" and done.error == "the agent exited with code 3"
    assert done.error_kind is None and "Traceback: boom" in done.reply


# --------------------------------------------------------------------------- G: rerun


@needs_shell
def test_rerun_starts_a_new_run_from_the_old_record(env, tmp_path):
    e = env
    m = _ready(e, tmp_path, FAIL_404)
    old = e.mgr.spawn("Describe this repo", "x1carbon", repo_path=str(e.repo),
                      criteria="a summary exists", max_turns=7, model_id=m.id)
    old = _wait_done(e.mgr, old.id)
    assert old.status == "failed"
    _shim(e, tmp_path, "#!/bin/sh\necho fixed\nexit 0\n")
    new = e.mgr.rerun(old.id)
    assert new.id != old.id and new.rerun_of == old.id and new.status == "running", new.error
    assert (new.goal, new.criteria, new.repo_path, new.branch, new.base_sha,
            new.template_id, new.max_turns, new.model_id, new.host_id) == (
        old.goal, old.criteria, old.repo_path, old.branch, old.base_sha,
        old.template_id, 7, m.id, old.host_id)
    done = _wait_done(e.mgr, new.id)
    assert done.status == "done" and done.reply == "fixed"
    kept = e.mgr._load(old.id)                      # the old record is kept as it was
    assert kept.status == "failed" and kept.error_kind == "model_not_found"
    assert kept.rerun_of is None


@needs_shell
def test_rerun_with_another_model_and_task(env, tmp_path):
    e = env
    first = _ready(e, tmp_path, FAIL_404)
    second = e.mgr.save_model({"name": "Other", "base_url": "http://192.0.2.99:9000/v1",
                               "model": "other-model", "key_secret": KEY_NAME})
    old = _wait_done(e.mgr, e.mgr.spawn("First task", "x1carbon", repo_path=str(e.repo),
                                        model_id=first.id).id)
    _shim(e, tmp_path, "#!/bin/sh\necho ok\nexit 0\n")
    by_id = e.mgr.rerun(old.id, model_id=second.id, task="  A changed task ")
    assert (by_id.model, by_id.endpoint_host, by_id.model_id, by_id.goal) == (
        "other-model", "192.0.2.99:9000", second.id, "A changed task")
    # model only: same endpoint and key, another model id (not saved)
    by_model = e.mgr.rerun(old.id, model="swift-2")
    assert (by_model.model, by_model.endpoint_host, by_model.model_id) == (
        "swift-2", "192.0.2.10:8080", None)
    assert by_model.key_secret == KEY_NAME
    # base_url of a saved choice picks that choice's key secret
    by_url = e.mgr.rerun(old.id, base_url="http://192.0.2.99:9000/v1/")
    assert (by_url.model_id, by_url.endpoint_host) == (second.id, "192.0.2.99:9000")


@needs_shell
def test_rerun_never_sends_a_key_to_an_endpoint_nobody_saved(env, tmp_path):
    e = env
    first = _ready(e, tmp_path, FAIL_404)
    old = _wait_done(e.mgr, e.mgr.spawn("T", "x1carbon", repo_path=str(e.repo),
                                        model_id=first.id).id)
    with pytest.raises(ApiError) as info:
        e.mgr.rerun(old.id, base_url="https://evil.example.net/v1")
    assert info.value.code == "invalid" and "only sent to an endpoint that was saved" in \
        info.value.message
    assert len(e.mgr._all()) == 1                   # nothing was started


@needs_shell
def test_rerun_on_another_host(env, tmp_path, fake_transports):
    e = env
    first = _ready(e, tmp_path, FAIL_404)
    old = _wait_done(e.mgr, e.mgr.spawn("T", "x1carbon", repo_path=str(e.repo),
                                        model_id=first.id).id)
    e.services.store.upsert_host(sample_host(id="other", name="Other",
                                             limits=HostLimits(allow_agents=True)))
    with pytest.raises(ApiError) as info:           # unknown host: the normal check answers
        e.mgr.rerun(old.id, host_id="nope")
    assert info.value.status == 404
    e.mgr.rerun(old.id, host_id="other")            # reaches the new host (its calls are faked)
    assert {r.host_id for r in e.mgr._all()} >= {"x1carbon", "other"}


def test_rerun_of_an_active_run_is_refused_with_a_clear_message(env):
    e = env
    run = _stored_run(e, instance_id="", status="starting", started_at=utc_now_iso())
    with pytest.raises(ApiError) as info:
        e.mgr.rerun(run.id)
    assert info.value.code == "conflict"
    assert info.value.message == (f"Cloud agent run {run.id} is still starting. Wait for it to "
                                  "end (or press Stop), then start it again.")
    assert len(e.mgr._all()) == 1


@needs_shell
@pytest.mark.parametrize("status", ["done", "stopped", "collected", "failed"])
def test_rerun_is_allowed_from_every_finished_state(env, status):
    e = env
    run = _stored_run(e, instance_id="", status=status, started_at=utc_now_iso(),
                      branch="notes", mode="script", template_id="agent-script")
    new = e.mgr.rerun(run.id)
    assert new.rerun_of == run.id and new.template_id == "agent-script"


@needs_shell
def test_rerun_never_overwrites_an_existing_branch(env):
    e = env
    run = _stored_run(e, instance_id="", status="failed", branch="notes",
                      base_sha=_git(e.repo, "rev-parse", "main"), new_branch=True)
    _git(e.repo, "branch", "notes", "main")
    _git(e.repo, "checkout", "-q", "notes")
    (e.repo / "x.txt").write_text("user work\n", encoding="utf-8")
    _git(e.repo, "add", "x.txt")
    _git(e.repo, "commit", "-q", "-m", "user commit")
    tip = _git(e.repo, "rev-parse", "notes")
    new = e.mgr.rerun(run.id)
    assert new.branch == "notes-2" and new.status == "running", new.error
    assert _git(e.repo, "rev-parse", "notes") == tip          # untouched
    assert new.base_sha == run.base_sha                        # same starting point
    _git(e.repo, "branch", "notes-2", "main")          # that name is taken now too
    again = e.mgr.rerun(run.id)
    assert again.branch == "notes-3"
    assert _git(e.repo, "rev-parse", "notes") == tip


@needs_shell
def test_rerun_continues_the_same_branch_when_the_first_run_did(env):
    e = env
    _git(e.repo, "branch", "feature", "main")
    run = _stored_run(e, instance_id="", status="done", branch="feature", new_branch=False)
    new = e.mgr.rerun(run.id)
    assert new.branch == "feature" and new.new_branch is False and new.status == "running"


def test_rerun_approval_text_is_the_text_of_a_new_run(env, tmp_path):
    e = env
    first = _ready(e, tmp_path, FAIL_404)
    run = _stored_run(e, instance_id="", status="failed", mode="hermes",
                      template_id="hermes-agent", goal="Fix the README", model_id=first.id,
                      branch="fix-the-readme", max_turns=9)
    text = e.mgr.describe_rerun({"run_id": run.id})
    same = e.mgr.describe_spawn({"task": "Fix the README", "host_id": "x1carbon",
                                 "repo_path": str(e.repo), "branch": "fix-the-readme",
                                 "max_turns": 9, "model_id": first.id})
    assert text == same
    assert "model hyperqwen-swift at 192.0.2.10:8080" in text and KEY_NAME in text
    other = e.mgr.describe_rerun({"run_id": run.id, "model": "swift-2", "task": "Other"})
    assert "model swift-2 at 192.0.2.10:8080" in other and "'Other'" in other
    assert SECRET_VALUE not in other
    # an unknown run still gets a plain approval text
    assert "again" in e.mgr.describe_rerun({"run_id": "a-00000000"})


# --------------------------------------------------------------------------- API shapes


def _run_dict(**kw):
    data = {"id": "a-11111111", "status": "failed", "error": "x", "error_kind": None,
            "commits_ahead": 0, "last_log": []}
    data.update(kw)
    return data


@pytest.mark.parametrize("kind", list(agents_mod.MODEL_FAILURE_KINDS))
def test_next_for_a_model_failure_points_to_rerun_not_to_a_follow_up(kind):
    nxt = agents_api._next(_run_dict(error_kind=kind))
    assert "cloud_agent_rerun(" in nxt and "Start it again with another model" in nxt
    assert "cloud_agent_message" not in nxt


def test_next_for_other_failures_offers_rerun_first():
    nxt = agents_api._next(_run_dict())
    assert nxt.index("cloud_agent_rerun") < nxt.index("cloud_agent_message")
    assert "cloud_agent_collect" in nxt


def test_failure_kind_lists_agree():
    assert agents_api.MODEL_FAILURE_KINDS == agents_mod.MODEL_FAILURE_KINDS


class FakeAgents:
    def __init__(self):
        self.calls = []

    def status(self, run_id=None):
        class R:
            branch = "notes"
        return [R()]

    def rerun(self, run_id, **kw):
        self.calls.append((run_id, kw))
        from types import SimpleNamespace
        return SimpleNamespace(branch="notes-2", to_dict=lambda: {
            "id": "a-22222222", "rerun_of": run_id, "status": "running", "branch": "notes-2"})

    def describe_rerun(self, args):
        return "Start a cloud agent ..."


def test_route_and_tool_pass_the_overrides_through():
    from cloud_compute_core.testing import make_services
    fake = FakeAgents()
    services = make_services(agents=fake)
    body = {"task": "t", "model_id": "m-00000001", "model": "x", "base_url": "http://h/v1",
            "host_id": "h", "max_turns": 5}
    route = next(r.handler for r in agents_api.routes()
                 if (r.method, r.path) == ("POST", "/agents/{run_id}/rerun"))
    out = route(ApiRequest(path_params={"run_id": "a-11111111"}, query={}, body=body,
                           services=services))
    assert out["rerun_of"] == "a-11111111"
    assert fake.calls[-1] == ("a-11111111", {"session_id": None, **body})
    with pytest.raises(ApiError) as info:
        route(ApiRequest(path_params={"run_id": "a-11111111"}, query={},
                         body={"surprise": 1}, services=services))
    assert info.value.code == "unknown_parameter"
    spec = next(t for t in agents_api.tools() if t.name == "cloud_agent_rerun")
    res = spec.handler({"run_id": "a-11111111", "model": "x"}, services, {"session_id": "s1"})
    assert res["rerun_of"] == "a-11111111" and res["run"]["id"] == "a-22222222"
    assert "notes-2" in res["note"] and "Nothing was overwritten" in res["note"]
    assert fake.calls[-1][1]["session_id"] == "s1" and fake.calls[-1][1]["model"] == "x"
    assert spec.approval({"run_id": "a-11111111"}, services) == "Start a cloud agent ..."
    err = spec.handler({"model": "x"}, services, {})
    assert err["error"]["code"] == "invalid"
    err = spec.handler({"run_id": "a-11111111", "bogus": 1}, services, {})
    assert err["error"]["code"] == "unknown_parameter"


# --------------------------------------------------------------------------- turn limit on follow-ups

ARGS_SHIM = r"""#!/bin/sh
for a in "$@"; do printf '%s\n' "$a"; done >> "$HERMES_HOME/args.log"
echo "---" >> "$HERMES_HOME/args.log"
# like Hermes: a resumed session refuses --max-turns
case "$*" in *--resume*--max-turns*|*--max-turns*--resume*)
  echo "Resume retains gateway session policy; creation overrides are unsupported on resume." >&2
  exit 2 ;; esac
echo "ok"
echo "session_id: 20261110_000000_n3" >&2
exit 0
"""


def _hermes_args(e, run) -> list[str]:
    path = e.sandboxes.hermes_home(run.instance_id) / "args.log"
    return path.read_text("utf-8").split("---\n")[:-1]


@needs_shell
def test_a_follow_up_never_passes_the_turn_limit_and_keeps_the_launch_value(env, tmp_path):
    e = env
    m = _ready(e, tmp_path, ARGS_SHIM)
    run = e.mgr.spawn("First", "x1carbon", repo_path=str(e.repo), max_turns=8, model_id=m.id)
    assert _wait_done(e.mgr, run.id).status == "done"
    with pytest.raises(ApiError) as info:            # another limit: refused, nothing changes
        e.mgr.message(run.id, "Second", max_turns=6)
    assert info.value.message == ("The turn limit is set when the run starts. Start it again "
                                  "with rerun to change it.")
    assert e.mgr._load(run.id).status == "done" and e.mgr._load(run.id).max_turns == 8
    again = e.mgr.message(run.id, "Second")           # no limit given
    assert again.max_turns == 8
    done = _wait_done(e.mgr, run.id)
    assert done.status == "done" and done.exit_code == 0 and done.max_turns == 8
    e.mgr.message(run.id, "Third", max_turns=8)       # repeating the launch value is fine
    assert _wait_done(e.mgr, run.id).status == "done"
    first, second, third = _hermes_args(e, run)
    assert "--max-turns\n8\n" in first and "--resume" not in first
    assert "--resume" in second and "--max-turns" not in second
    assert "--resume" in third and "--max-turns" not in third


@needs_shell
def test_a_run_damaged_by_an_older_follow_up_recovers(env, tmp_path):
    e = env
    m = _ready(e, tmp_path, ARGS_SHIM)
    run = e.mgr.spawn("First", "x1carbon", repo_path=str(e.repo), max_turns=8, model_id=m.id)
    _wait_done(e.mgr, run.id)
    damaged = e.mgr._load(run.id)
    damaged.max_turns = 6                             # what the old code stored
    e.mgr._save(damaged)
    e.mgr.message(run.id, "Second")
    done = _wait_done(e.mgr, run.id)
    assert done.status == "done" and done.exit_code == 0
    assert "--max-turns" not in _hermes_args(e, run)[1]


def test_the_runner_passes_the_limit_to_a_new_session_only():
    script = agents_mod._runner_script("a-0000abcd", "hermes", 9, "/home/agent/.cc-agent")
    assert 'mt="--max-turns 9"' in script and 'hermes chat -Q --query-file "$q" $mt "$@"' in script
    assert '*) set -- --resume "$sid"; mt="" ;; esac' in script


def test_message_schema_has_no_turn_limit_and_says_why():
    spec = next(t for t in agents_api.tools() if t.name == "cloud_agent_message")
    assert "max_turns" not in spec.schema["parameters"]["properties"]
    assert "turn limit stays the one the run started with" in spec.schema["description"]


# --------------------------------------------------------------------------- unknown model id


def test_unknown_model_id_says_only_the_relevant_sentence_and_the_valid_ids(env):
    e = env
    with pytest.raises(ApiError) as info:
        e.mgr.save_model({"id": "m-00000000", "base_url": "http://192.0.2.10:8080/v1",
                          "model": "x"})
    assert info.value.status == 404
    assert info.value.message == ("No saved cloud-agent model 'm-00000000'. No models are saved "
                                  "yet. Add one in Compute, Agents, New agent run, Model.")
    assert "chat agent" not in info.value.message and "No model is set up" not in info.value.message
    m = e.mgr.save_model({"name": "Swift", "base_url": "http://192.0.2.10:8080/v1",
                          "model": "hyperqwen-swift"})
    with pytest.raises(ApiError) as info:
        e.mgr.save_model({"id": "m-00000000", "base_url": "http://192.0.2.10:8080/v1",
                          "model": "x"})
    assert info.value.message == ("No saved cloud-agent model 'm-00000000'. Valid ids: "
                                  f"{m.id} (Swift: hyperqwen-swift at 192.0.2.10:8080, default).")


def test_a_used_up_turn_budget_is_said_plainly_and_a_follow_up_stays_possible():
    log = "⚠️  Iteration budget exhausted (3/3 iterations used)\nsession_id: x"
    kind, text = diagnose_failure(log, model="m", endpoint="h", key_secret="K", max_turns=3)
    assert kind == "turn_limit" and kind in agents_mod.FAILURE_KINDS
    assert kind not in agents_mod.MODEL_FAILURE_KINDS
    assert text == ("The agent used all 3 of its turns before it finished. Its work so far is on "
                    "the branch. Start the run again with a higher turn limit (rerun with "
                    "max_turns), or send a follow-up to let it continue.")
    nxt = agents_api._next(_run_dict(error_kind="turn_limit"))
    assert "cloud_agent_rerun" in nxt and "cloud_agent_message" in nxt


# --------------------------------------------------------------------------- D: reply text

DIFF_REPLY = ("\u2502 NOTES.md\n\u2502 @@ -0,0 +1 @@\n\u2502 +tiny-calc is a small library.\n"
              "Created and committed NOTES.md.\n\n"
              "  Created and committed NOTES.md.")


def test_tool_previews_are_dropped_from_the_start_of_a_reply():
    got = agents_mod.strip_tool_previews(DIFF_REPLY)
    assert got == "Created and committed NOTES.md.\n\n  Created and committed NOTES.md."
    assert "\u2502" not in got and "@@" not in got


def test_a_preview_block_in_the_middle_goes_but_real_box_text_stays():
    text = "Intro\n\u2502 a.py\n\u2502 @@ -1 +1 @@\n\u2502 -x\n\u2502 +y\nDone."
    assert agents_mod.strip_tool_previews(text) == "Intro\nDone."
    tree = "Layout:\n\u2502   \u251c\u2500\u2500 src\n\u2502   \u2514\u2500\u2500 tests\nDone."
    assert agents_mod.strip_tool_previews(tree) == tree          # no hunk header: real content
    assert agents_mod.strip_tool_previews("plain answer") == "plain answer"
    assert agents_mod.strip_tool_previews("") == ""


def test_a_reply_that_is_only_a_preview_is_not_emptied():
    only = "\u2502 a.py\n\u2502 @@ -1 +1 @@\n\u2502 +y"
    assert agents_mod.strip_tool_previews(only) == only


@needs_shell
def test_the_captured_reply_of_a_run_has_no_diff_preview(env, tmp_path):
    e = env
    body = ("#!/bin/sh\nprintf '%s\n' '\u2502 NOTES.md' '\u2502 @@ -0,0 +1 @@' '\u2502 +hello' "
            "'Created NOTES.md.'\nexit 0\n")
    _ready(e, tmp_path, body)
    run = e.mgr.spawn("T", "x1carbon", repo_path=str(e.repo))
    done = _wait_done(e.mgr, run.id)
    assert done.status == "done" and done.reply == "Created NOTES.md."


def test_the_wording_the_live_agent_used_for_a_used_up_budget_is_recognised():
    reply = ("Stopped at the iteration limit mid-task. Here's where things stand:\n\nDone:\n"
             "- Created A.md containing `a`.\n\nNot done:\n- B.md")
    got = diagnose_failure(reply, model="m", endpoint="h", key_secret="K", max_turns=3)
    assert got is not None and got[0] == "turn_limit" and "all 3 of its turns" in got[1]
    # a normal failure text does not mention turns
    assert diagnose_failure("Traceback: ZeroDivisionError", model="m", endpoint="h",
                            key_secret="K", max_turns=3) is None
