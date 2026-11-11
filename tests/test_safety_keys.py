"""An approval key must change whenever what the user was shown changes.

Hermes caches a "session" or "always" answer under the key. A key that is too coarse lets one
answer approve a different file, copy, template, port or command later.
"""

from __future__ import annotations

import pytest

from cloud_compute_core.approval import rule_key


def key(tool, args, host="h1"):
    return rule_key(tool, args, host)


# ------------------------------------------------------------------ put and restore

PUT = {"action": "put", "instance_id": "i-1", "local_path": "C:/proj/a.txt", "dest": "/work"}


def test_put_key_names_the_file_destination_and_unpack_choice():
    base = key("env_files", PUT)
    assert base.startswith("cloud-compute:env_files:put:i-1:")
    others = [
        dict(PUT, local_path="C:/Users/me/.aws/credentials"),
        dict(PUT, dest="/etc"),
        dict(PUT, extract=True),
        dict(PUT, instance_id="i-2"),
    ]
    keys = {base} | {key("env_files", o) for o in others}
    assert len(keys) == 5
    assert key("env_files", dict(PUT)) == base                     # the same upload: same key
    assert key("env_files", dict(PUT, extract=False)) == base      # not unpacking is the default
    assert key("env_files", dict(PUT, extract="true")) == key("env_files", dict(PUT, extract=True))


RESTORE = {"action": "restore", "instance_id": "i-1", "export_id": "x-1", "dest": "/work"}


def test_restore_key_names_the_copy_and_the_destination():
    base = key("env_files", RESTORE)
    assert base.startswith("cloud-compute:env_files:restore:i-1:x-1:")
    keys = {base, key("env_files", dict(RESTORE, export_id="x-2")),
            key("env_files", dict(RESTORE, dest="/")),
            key("env_files", dict(RESTORE, instance_id="i-2")),
            key("env_files", dict(RESTORE, dest=""))}
    assert len(keys) == 5
    assert key("env_files", dict(RESTORE)) == base


def test_put_and_restore_keys_never_collide():
    assert key("env_files", PUT) != key("env_files", RESTORE)


# ------------------------------------------------------------------ cloud_agent_spawn

SPAWN = {"host_id": "h1", "repo_path": "C:/r/app", "task": "write tests", "template": "hermes-agent"}


def test_spawn_key_names_template_model_turns_and_the_whole_task():
    base = key("cloud_agent_spawn", SPAWN)
    variants = [
        dict(SPAWN, template="agent-script"),
        dict(SPAWN, model_id="m-1"),
        dict(SPAWN, max_turns=200),
        dict(SPAWN, task="delete everything"),
        dict(SPAWN, criteria="all green"),
        dict(SPAWN, repo_path="C:/other/app"),
        dict(SPAWN, branch="other"),
    ]
    keys = {base} | {key("cloud_agent_spawn", v) for v in variants}
    assert len(keys) == 8
    assert key("cloud_agent_spawn", dict(SPAWN)) == base


def test_spawn_key_differs_when_only_the_end_of_a_long_task_differs():
    long_task = "x" * 5000
    assert key("cloud_agent_spawn", dict(SPAWN, task=long_task + "a")) != \
        key("cloud_agent_spawn", dict(SPAWN, task=long_task + "b"))


def test_spawn_key_follows_the_card_so_a_changed_default_model_asks_again():
    one = rule_key("cloud_agent_spawn", SPAWN, "h1", "model m-1 at llm.example.net")
    two = rule_key("cloud_agent_spawn", SPAWN, "h1", "model m-2 at llm.example.net")
    assert one != two


def test_the_handoff_test_and_a_model_run_never_share_a_key():
    handoff = key("cloud_agent_spawn", dict(SPAWN, template="agent-script"))
    model = key("cloud_agent_spawn", {k: v for k, v in SPAWN.items() if k != "template"})
    assert handoff != model


# ------------------------------------------------------------------ env_open port of a host

from types import SimpleNamespace  # noqa: E402

from cloud_compute_core import approval  # noqa: E402


def _services():
    store = SimpleNamespace(get_host=lambda h: SimpleNamespace(id=h, name="Lab box", kind="ssh"),
                            get_instance=lambda i: None)
    return SimpleNamespace(config=SimpleNamespace(approval="always"), store=store)


def test_env_open_port_of_a_host_asks_naming_host_and_port_and_keys_on_both():
    args = {"kind": "port", "host_id": "lab", "remote_port": 2375}
    out = approval.approval_for("env_open", args, SimpleNamespace(approval=None), _services())
    assert out["action"] == "approve"
    assert "2375" in out["message"] and "Lab box (lab)" in out["message"]
    other_port = approval.approval_for("env_open", dict(args, remote_port=5432),
                                       SimpleNamespace(approval=None), _services())
    other_host = approval.approval_for("env_open", dict(args, host_id="other"),
                                       SimpleNamespace(approval=None), _services())
    assert len({out["rule_key"], other_port["rule_key"], other_host["rule_key"]}) == 3


@pytest.mark.parametrize("args", [
    {"kind": "port", "instance_id": "i-1", "remote_port": 8080},
    {"kind": "terminal", "instance_id": "i-1"},
    {"kind": "display", "instance_id": "i-1"},
    {"kind": "terminal", "host_id": "slurm"},
])
def test_env_open_without_a_host_port_forward_needs_no_approval(args):
    assert approval.approval_for("env_open", args, SimpleNamespace(approval=None),
                                 _services()) is None


# ------------------------------------------------------------------ fetch asks consistently

FETCH = {"action": "fetch", "instance_id": "i-1"}


@pytest.mark.parametrize("args, asks", [
    (dict(FETCH), False),
    (dict(FETCH, extract=True), True),
    (dict(FETCH, extract="true"), True),
    (dict(FETCH, extract=False), False),
    (dict(FETCH, dest_dir="C:/out"), True),
    (dict(FETCH, dest_dir="   "), False),
])
def test_always_asks_for_a_fetch_that_writes_to_a_chosen_folder_or_unpacks(args, asks):
    assert approval._always_asks("env_files", args) is asks


def test_remote_only_mode_still_asks_for_a_fetch_into_a_chosen_folder_on_a_local_host():
    local = SimpleNamespace(config=SimpleNamespace(approval="remote-only"),
                            store=SimpleNamespace(
                                get_host=lambda h: SimpleNamespace(id=h, name="This computer",
                                                                   kind="local"),
                                get_instance=lambda i: SimpleNamespace(
                                    id=i, name="box", host_id="local", template_id="t")))
    out = approval.approval_for("env_files", dict(FETCH, dest_dir="C:/out"), None, local)
    assert out is not None and out["action"] == "approve"
    plain = approval.approval_for("env_files", dict(FETCH), None, local)
    assert plain is None
