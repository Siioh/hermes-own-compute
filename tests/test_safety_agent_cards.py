"""Cloud agent cards: the spawn, rerun and follow-up cards show what runs
(the whole task and criteria, the full repository path, template, model, endpoint host and
turn limit; the follow-up message), bounded the same way as every other card."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from cloud_compute_core import approval
from cloud_compute_core.approval import CARD_TEXT_LIMIT, bounded_text

_name = "cloud_compute_tests_agents_manager"
if _name not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _name, Path(__file__).with_name("test_agents_manager.py"))
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _mod
    _spec.loader.exec_module(_mod)
_m = sys.modules[_name]
KEY_NAME, SECRET_VALUE = _m.KEY_NAME, _m.SECRET_VALUE
_plain_hermes, _stored_run = _m._plain_hermes, _m._stored_run
env = _m.env  # noqa: F811 - the shared fixture


def _model(e):
    _plain_hermes(e)
    return e.mgr.save_model({"name": "Swift", "base_url": "http://192.0.2.10:8080/v1",
                             "model": "hyperqwen-swift", "key_secret": KEY_NAME})


def _spawn_args(e, **kw):
    return {"task": "Fix the README", "host_id": "x1carbon", "repo_path": str(e.repo), **kw}


def test_spawn_card_shows_template_model_endpoint_turns_repo_task_and_criteria(env):
    e = env
    model = _model(e)
    text = e.mgr.describe_spawn(_spawn_args(e, model_id=model.id, max_turns=77,
                                            criteria="All tests pass"))
    assert "template hermes-agent" in text
    assert "model hyperqwen-swift at 192.0.2.10:8080" in text
    assert "max 77 turns" in text
    assert f"on {e.repo} branch" in text                       # the full path, not the folder name
    assert "Task: 'Fix the README'" in text
    assert "Success criteria: 'All tests pass'" in text
    assert f"read from the secret {KEY_NAME}" in text and SECRET_VALUE not in text


def test_spawn_card_for_the_handoff_test_names_the_template_and_sends_no_secrets(env):
    e = env
    text = e.mgr.describe_spawn(_spawn_args(e, template="agent-script"))
    assert "template agent-script" in text and f"on {e.repo} branch" in text
    assert "Task: 'Fix the README'" in text and text.endswith("Sends no secrets.")


def test_two_repositories_with_the_same_folder_name_look_different(env, tmp_path):
    e = env
    a = e.mgr.describe_spawn(_spawn_args(e))
    other = tmp_path / "elsewhere" / e.repo.name
    b = e.mgr.describe_spawn(_spawn_args(e, repo_path=str(other)))
    assert str(e.repo) in a and str(other) in b and a != b


def test_spawn_card_bounds_a_long_task_and_a_long_criteria(env):
    e = env
    task = "t" * (CARD_TEXT_LIMIT + 250)
    criteria = "c" * (CARD_TEXT_LIMIT + 7)
    text = e.mgr.describe_spawn(_spawn_args(e, task=task, criteria=criteria))
    assert f"Task: '{'t' * CARD_TEXT_LIMIT}... (250 more characters)'" in text
    assert f"Success criteria: '{'c' * CARD_TEXT_LIMIT}... (7 more characters)'" in text


def test_the_old_80_character_cut_is_gone(env):
    e = env
    text = e.mgr.describe_spawn(_spawn_args(e, task="word " * 100))
    assert ("word " * 100).strip() in text


def test_rerun_card_shows_the_same_things_as_a_spawn_card(env, tmp_path):
    e = env
    first = _model(e)
    run = _stored_run(e, instance_id="", status="failed", mode="hermes",
                      template_id="hermes-agent", goal="Fix the README", model_id=first.id,
                      branch="fix-the-readme", max_turns=9, criteria="All tests pass")
    text = e.mgr.describe_rerun({"run_id": run.id})
    assert "template hermes-agent" in text and "max 9 turns" in text
    assert "model hyperqwen-swift at 192.0.2.10:8080" in text
    assert f"on {e.repo} branch fix-the-readme" in text or str(e.repo) in text
    assert "Task: 'Fix the README'" in text
    assert "Success criteria: 'All tests pass'" in text


def test_message_card_shows_the_whole_message_up_to_the_bound(env):
    e = env
    run = _stored_run(e, status="done")
    message = "line one\nline two " + "m" * 300
    text = e.mgr.describe_message({"run_id": run.id, "message": message})
    assert message in text and "(more" not in text
    long = "x" * (CARD_TEXT_LIMIT + 40)
    text = e.mgr.describe_message({"run_id": run.id, "message": long})
    assert bounded_text(long) in text and "x" * (CARD_TEXT_LIMIT + 1) not in text
    assert "(40 more characters)" in text


def test_the_spawn_key_through_the_gate_follows_what_the_card_shows(env):
    e = env
    model = _model(e)
    tool = type("T", (), {"approval": staticmethod(lambda a, s: e.mgr.describe_spawn(a))})()

    def key(**kw):
        return approval.approval_for("cloud_agent_spawn", _spawn_args(e, **kw), tool,
                                     e.services)["rule_key"]

    handoff = key(template="agent-script")
    run = key(model_id=model.id)
    assert handoff != run
    assert run != key(model_id=model.id, task="something else")
    assert run != key(model_id=model.id, max_turns=200)
    assert run != key(model_id=model.id, criteria="done when rich")
    assert run == key(model_id=model.id)
