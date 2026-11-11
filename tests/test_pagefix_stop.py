"""Stop fetches the agent's newest commits before it deletes the sandbox.

Same emulated sandbox as test_agents_manager: real sandbox scripts run with a local ``sh`` and
real ``git`` inside a temporary directory.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_compute_core import agents_api, approval, gitrelay
from cloud_compute_core import agents as agents_mod
from cloud_compute_core.interfaces import ApiError, ApiRequest

_name = "cloud_compute_tests_agents_manager"
if _name not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _name, Path(__file__).with_name("test_agents_manager.py"))
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _mod
    _spec.loader.exec_module(_mod)
_m = sys.modules[_name]
_git, _wait_done, needs_shell = _m._git, _m._wait_done, _m.needs_shell
env = _m.env  # noqa: F811 - the shared fixture


def _done_and_collected(e):
    run = e.mgr.spawn("Write a note", "x1carbon", repo_path=str(e.repo), template="agent-script")
    _wait_done(e.mgr, run.id)
    e.mgr.collect(run.id, into="branch")
    return e.mgr._load(run.id)


def _agent_commits_again(e, run, text="second.txt"):
    """What a follow-up turn does: one more commit inside the sandbox (never fetched)."""
    crepo = e.sandboxes.work_dir(run.instance_id) / "repo"
    (crepo / text).write_text("more\n", encoding="utf-8")
    _git(crepo, "add", text)
    _git(crepo, "commit", "-q", "-m", "more work")
    stored = e.mgr._load(run.id)
    stored.status = "done"             # the follow-up turn ended
    e.mgr._save(stored)
    return _git(crepo, "rev-parse", "HEAD")


@needs_shell
def test_stop_fetches_commits_made_after_the_last_fetch(env):
    e = env
    run = _done_and_collected(e)
    head = _agent_commits_again(e, run)
    ref = f"refs/heads/cloud/{run.id}/{run.branch}"
    assert gitrelay.rev_parse(e.repo, ref) != head          # not fetched yet

    stopped = e.mgr.stop(run.id)

    assert gitrelay.rev_parse(e.repo, ref) == head          # fetched before the sandbox went
    assert run.instance_id in e.sandboxes.destroyed and stopped.instance_id == ""
    assert stopped.stop_note == f"Fetched 1 new commit to branch cloud/{run.id}/{run.branch} before stopping."
    assert stopped.collected_head_sha == stopped.head_sha == head
    assert stopped.collected_ref == ref and stopped.error is None
    assert e.mgr._load(run.id).stop_note == stopped.stop_note        # stored, so the card shows it


@needs_shell
def test_stop_of_a_run_never_fetched_fetches_it(env):
    e = env
    run = e.mgr.spawn("Write a note", "x1carbon", repo_path=str(e.repo), template="agent-script")
    done = _wait_done(e.mgr, run.id)
    assert done.collected_ref is None
    stopped = e.mgr.stop(run.id)
    ref = f"refs/heads/cloud/{run.id}/{run.branch}"
    assert gitrelay.rev_parse(e.repo, ref) == done.head_sha
    assert stopped.stop_note == f"Fetched 1 new commit to branch cloud/{run.id}/{run.branch} before stopping."


@needs_shell
def test_stop_with_everything_fetched_says_nothing_about_new_commits(env):
    e = env
    run = _done_and_collected(e)
    stopped = e.mgr.stop(run.id)
    assert stopped.stop_note is None
    assert stopped.collected_head_sha == stopped.head_sha
    assert run.instance_id in e.sandboxes.destroyed


@needs_shell
def test_failed_fetch_keeps_the_sandbox_and_says_what_to_do(env):
    e = env
    run = _done_and_collected(e)
    _agent_commits_again(e, run)
    e.emulator.fail_matching = "git bundle create"           # reading the commits fails
    with pytest.raises(ApiError) as exc:
        e.mgr.stop(run.id)
    assert exc.value.code == "not_fetched" and exc.value.status == 409
    assert exc.value.message.startswith(agents_mod.NOT_FETCHED_PREFIX)
    assert "nothing was deleted" in exc.value.message and "Get results" in exc.value.message
    assert e.sandboxes.destroyed == []
    kept = e.mgr._load(run.id)
    assert kept.instance_id == run.instance_id and kept.status == "stopped"
    assert kept.error == exc.value.message
    assert agents_mod.AgentManager.unfetched_commits(e.mgr, run.instance_id) is True


@needs_shell
def test_delete_anyway_deletes_and_says_where_the_commits_are(env):
    e = env
    run = _done_and_collected(e)
    head = _agent_commits_again(e, run)
    e.emulator.fail_matching = "git bundle create"
    stopped = e.mgr.stop(run.id, delete_anyway=True)
    assert run.instance_id in e.sandboxes.destroyed and stopped.instance_id == ""
    saved = str(e.tmp / "containers" / "saved" / run.instance_id)
    assert saved in stopped.stop_note
    assert "could not be fetched" in stopped.stop_note and "only in the saved copy" in stopped.stop_note
    assert "Fetched" not in stopped.stop_note
    assert stopped.collected_head_sha != head          # the record does not claim they are here
    assert stopped.error is None


@needs_shell
def test_stop_again_after_a_kept_sandbox_fetches_and_deletes(env):
    e = env
    run = _done_and_collected(e)
    head = _agent_commits_again(e, run)
    e.emulator.fail_matching = "git bundle create"
    with pytest.raises(ApiError):
        e.mgr.stop(run.id)
    e.emulator.fail_matching = None
    stopped = e.mgr.stop(run.id)
    assert stopped.instance_id == "" and stopped.error is None
    assert gitrelay.rev_parse(e.repo, f"refs/heads/cloud/{run.id}/{run.branch}") == head
    assert stopped.stop_note.startswith("Fetched 1 new commit")


@needs_shell
def test_stop_that_deletes_the_branches_does_not_fetch(env):
    e = env
    run = _done_and_collected(e)
    _agent_commits_again(e, run)
    e.emulator.fail_matching = "git bundle create"          # would fail if Stop tried to read
    stopped = e.mgr.stop(run.id, keep_branch=False)
    assert stopped.instance_id == "" and stopped.stop_note is None
    assert not gitrelay.ref_exists(e.repo, f"refs/heads/cloud/{run.id}/{run.branch}")


@needs_shell
def test_a_sandbox_without_a_repository_stops_as_before(env):
    e = env
    run = _m._stored_run(e, status="failed")
    stopped = e.mgr.stop(run.id)
    assert stopped.instance_id == "" and stopped.stop_note is None


@needs_shell
def test_unfetched_commits_is_false_once_everything_is_fetched(env):
    e = env
    run = e.mgr.spawn("Write a note", "x1carbon", repo_path=str(e.repo), template="agent-script")
    done = _wait_done(e.mgr, run.id)
    assert e.mgr.unfetched_commits(done.instance_id) is True
    e.mgr.collect(run.id, into="branch")
    assert e.mgr.unfetched_commits(done.instance_id) is False
    assert e.mgr.unfetched_commits("i-nothing") is False


# --------------------------------------------------------------------------- routes and tool


class _Agents:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def stop(self, run_id, keep_branch=True, delete_anyway=False):
        self.calls.append((run_id, keep_branch, delete_anyway))
        if self.error:
            raise self.error
        return SimpleNamespace(to_dict=lambda: {"id": run_id, "status": "stopped",
                                                "stop_note": "Fetched 1 new commit to branch b before stopping."})


def _req(agents, body):
    return ApiRequest(path_params={"run_id": "a-1234abcd"}, query={}, body=body,
                      services=SimpleNamespace(agents=agents))


def test_the_stop_route_passes_delete_anyway_and_rejects_unknown_keys():
    agents = _Agents()
    agents_api.stop_agent(_req(agents, {"keep_branch": True, "delete_anyway": True}))
    agents_api.stop_agent(_req(agents, {}))
    assert agents.calls == [("a-1234abcd", True, True), ("a-1234abcd", True, False)]
    with pytest.raises(ApiError) as exc:
        agents_api.stop_agent(_req(agents, {"delete_anyway": "yes"}))
    assert exc.value.code == "invalid"
    with pytest.raises(ApiError) as exc:
        agents_api.stop_agent(_req(agents, {"force": True}))
    assert exc.value.code == "unknown_parameter"


def test_the_stop_tool_reports_the_note_and_explains_a_refusal():
    services = SimpleNamespace(agents=_Agents())
    out = agents_api._tool_stop({"run_id": "a-1234abcd"}, services, {})
    assert out["note"] == "Fetched 1 new commit to branch b before stopping."
    refused = SimpleNamespace(agents=_Agents(ApiError(409, "not_fetched", "kept")))
    out = agents_api._tool_stop({"run_id": "a-1234abcd"}, refused, {})
    assert out["error"]["code"] == "not_fetched" and "delete_anyway=true" in out["next"]
    services = SimpleNamespace(agents=_Agents())
    agents_api._tool_stop({"run_id": "a-1234abcd", "delete_anyway": True}, services, {})
    assert services.agents.calls == [("a-1234abcd", True, True)]


def test_the_stop_schema_and_approval_name_delete_anyway():
    props = agents_api.STOP_SCHEMA["parameters"]["properties"]
    assert props["delete_anyway"]["type"] == "boolean"
    services = SimpleNamespace(store=SimpleNamespace(
        get_record=lambda kind, rid: {"instance_id": "", "collected_ref": ""},
        get_instance=lambda i: None))
    plain = approval.describe_agent_stop({"run_id": "a-1"}, services)
    forced = approval.describe_agent_stop({"run_id": "a-1", "delete_anyway": True}, services)
    assert plain == "Stops cloud agent run a-1."                   # no sandbox: nothing to fetch
    services = SimpleNamespace(store=SimpleNamespace(
        get_record=lambda kind, rid: {"instance_id": "i-1", "collected_ref": ""},
        get_instance=lambda i: None))
    plain = approval.describe_agent_stop({"run_id": "a-1"}, services)
    forced = approval.describe_agent_stop({"run_id": "a-1", "delete_anyway": True}, services)
    assert "are fetched to this computer first" in plain and "nothing is deleted" in plain
    assert "delete_anyway is set" in forced and "only in the saved copy" in forced
    assert approval.rule_key("cloud_agent_stop", {"run_id": "a-1"}, None) != \
        approval.rule_key("cloud_agent_stop", {"run_id": "a-1", "delete_anyway": True}, None)
    assert approval._always_asks("cloud_agent_stop", {"run_id": "a-1", "delete_anyway": True})


def test_next_step_of_a_stopped_run_that_kept_its_sandbox_says_to_fetch_again():
    kept = agents_api._next({"id": "a-1234abcd", "status": "stopped", "instance_id": "i-1"})
    assert "cloud_agent_collect" in kept and "nothing was deleted" in kept
    gone = agents_api._next({"id": "a-1234abcd", "status": "stopped", "instance_id": ""})
    assert gone.startswith("The sandbox is gone.")
