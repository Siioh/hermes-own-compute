"""Chat context, approval gate and /compute command (no network, no SSH)."""

from __future__ import annotations

import json
import time

import pytest

from cloud_compute_core import approval, chat_context, commands
from cloud_compute_core.config import Config
from cloud_compute_core.interfaces import ToolSpec
from cloud_compute_core.testing import (
    sample_host, sample_host_state, sample_instance, sample_job, sample_local_host,
    sample_slurm_host,
)


# --------------------------------------------------------------------------- fakes


class _Boom(Exception):
    pass


class FakeStore:
    """Records every call; any method outside the allowed set fails the test."""

    def __init__(self, snapshot=None, hosts=None):
        self.calls: list[str] = []
        self._snapshot = snapshot if snapshot is not None else {
            "hosts": [], "instances": [], "jobs": [], "generated_at": "x"}
        self.kv: dict[str, str] = {}
        self.hosts = hosts or {}

    def snapshot(self):
        self.calls.append("snapshot")
        return self._snapshot

    def kv_get(self, key):
        self.calls.append("kv_get")
        return self.kv.get(key)

    def kv_set(self, key, value):
        self.calls.append("kv_set")
        self.kv[key] = value

    def get_host(self, host_id):
        self.calls.append("get_host")
        return self.hosts.get(host_id)

    def get_instance(self, i):
        self.calls.append("get_instance")
        return None

    def get_job(self, j):
        self.calls.append("get_job")
        return None

    def __getattr__(self, name):
        raise AssertionError(f"unexpected store access: {name}")


class FakeSvc:
    def __init__(self, store, config=None):
        self.store = store
        self.config = config or Config()

    def __getattr__(self, name):
        raise AssertionError(f"unexpected service access: {name}")


def _snap(store):
    return store.snapshot()


def _seed(store):
    store.upsert_host(sample_host())
    store.set_host_state(sample_host_state())
    store.upsert_host(sample_slurm_host())
    store.upsert_instance(sample_instance())
    store.upsert_job(sample_job())


# --------------------------------------------------------------------------- chat context


def test_system_prompt_section_size():
    assert 0 < len(chat_context.SYSTEM_PROMPT_SECTION) <= 1500
    assert "approval" in chat_context.SYSTEM_PROMPT_SECTION


def test_render_context_example(store):
    _seed(store)
    text = chat_context.render_context(store.snapshot(), max_chars=600)
    assert text.startswith("[cloud-compute] Online: x1carbon (ssh/podman, CPU 1.2/8, RAM 6.1/15.6 GB")
    assert "1 env: i-ab12cd34 python-dev" in text
    assert "slurm-x1 (unknown)" in text  # no state yet
    assert "j-1a2b3c4d RUNNING" in text
    assert len(text) <= 600


def test_render_context_truncates():
    snap = {"hosts": [], "instances": [], "jobs": []}
    assert len(chat_context.render_context(snap, max_chars=30)) <= 30


def test_render_context_finished():
    done = sample_job(id="j-99aa11bb", state="COMPLETED", exit_code="0:0").to_dict()
    snap = {"hosts": [{"spec": sample_host().to_dict(), "state": sample_host_state().to_dict()}],
            "jobs": [done]}
    text = chat_context.render_context(snap, finished=[done])
    assert "Finished since last turn: j-99aa11bb COMPLETED (0:0)" in text
    assert "Jobs: none" in text


def test_first_turn_then_silent_then_change(services):
    _seed(services.store)
    first = chat_context.context_for(services, "s1", True)
    assert first and "[cloud-compute]" in first
    assert chat_context.context_for(services, "s1", False) is None
    services.store.upsert_job(sample_job(state="COMPLETED", exit_code="0:0"))
    changed = chat_context.context_for(services, "s1", False)
    assert changed and "Finished since last turn: j-1a2b3c4d COMPLETED (0:0)" in changed
    assert chat_context.context_for(services, "s1", False) is None
    # other session has its own digest
    assert chat_context.context_for(services, "s2", False) is not None
    assert services.store.kv_get("ctx:s1")


def test_first_turn_always_injects(services):
    assert chat_context.context_for(services, "s1", True)
    assert chat_context.context_for(services, "s1", True)


def test_resource_jitter_does_not_reinject(services):
    _seed(services.store)
    assert chat_context.context_for(services, "s", True)
    st = sample_host_state()
    st.resources.cpu_used = 5.5
    services.store.set_host_state(st)
    assert chat_context.context_for(services, "s", False) is None


def test_respects_max_chars_and_switch(services):
    _seed(services.store)
    services.config.chat_context_max_chars = 120
    assert len(chat_context.context_for(services, "s", True)) <= 120
    services.config.chat_context = False
    assert chat_context.context_for(services, "s2", True) is None


def test_context_reads_snapshot_only():
    store = FakeStore()
    svc = FakeSvc(store)
    assert chat_context.context_for(svc, "s", True)
    assert set(store.calls) <= {"snapshot", "kv_get", "kv_set"}
    assert "snapshot" in store.calls


def test_pre_llm_call_hook(services):
    hook = chat_context.make_pre_llm_call(lambda: services)
    out = hook(session_id="s", is_first_turn=True, user_message="hi", extra=1)
    assert set(out) == {"context"} and isinstance(out["context"], str)
    assert hook(session_id="s", is_first_turn=False) is None


def test_pre_llm_call_never_raises():
    def boom():
        raise _Boom()

    assert chat_context.make_pre_llm_call(boom)(session_id="s", is_first_turn=True) is None


def test_pre_llm_call_fast(services):
    _seed(services.store)
    hook = chat_context.make_pre_llm_call(lambda: services)
    t = time.monotonic()
    hook(session_id="s", is_first_turn=True)
    assert time.monotonic() - t < 1.0


# --------------------------------------------------------------------------- approval


def _tools(**kw):
    return {n: ToolSpec(name=n, schema={}, handler=lambda *a: {}, approval=a)
            for n, a in kw.items()}


@pytest.mark.parametrize("tool,args,key", [
    ("env_spawn", {"template": "python-dev", "host_id": "x1carbon", "ttl_minutes": 240},
     "spawn:x1carbon:python-dev"),
    ("slurm_submit", {"host_id": "slurm-x1", "name": "train", "cpus": 4, "gpus": 1}, "slurm-x1"),
    ("cloud_agent_spawn", {"goal": "g", "host_id": "x1carbon", "repo_path": "/r"},
     "spawn:x1carbon:_r"),
    ("env_control", {"instance_id": "i-ab12cd34", "action": "destroy"},
     "destroy-save:i-ab12cd34"),
    ("slurm_jobs", {"action": "cancel", "job_id": "j-1", "host_id": "slurm-x1"}, "cancel:j-1"),
])
def test_approval_required(services, tool, args, key):
    res = approval.approval_for(tool, args, None, services)
    assert res["action"] == "approve"
    if tool in ("cloud_agent_spawn", "slurm_submit"):   # the key also carries a hash of the run
        assert res["rule_key"].startswith(f"cloud-compute:{tool}:{key}:")
    else:
        assert res["rule_key"] == f"cloud-compute:{tool}:{key}"
    assert args.get("host_id", "") in res["message"]    # env_control takes no host_id


def test_spawn_message_says_what_and_where(services):
    res = approval.approval_for("env_spawn", {"template": "python-dev", "host_id": "x1carbon",
                                              "ttl_minutes": 240}, None, services)
    assert res["message"] == "Spawn python-dev on x1carbon, TTL 4 h?"


@pytest.mark.parametrize("tool,args", [
    ("compute_status", {}),
    ("env_exec", {"instance_id": "i", "command": "ls"}),
    ("env_open", {"kind": "terminal", "instance_id": "i"}),
    ("slurm_jobs", {"action": "list"}),
    ("slurm_jobs", {"action": "status", "job_id": "j"}),
    ("slurm_jobs", {"action": "output", "job_id": "j"}),
    ("env_control", {"instance_id": "i", "action": "logs"}),
    ("env_control", {"instance_id": "i", "action": "stop"}),
    ("env_control", {"instance_id": "i", "action": "start"}),
    ("cloud_agent_status", {}),
    ("cloud_agent_collect", {"run_id": "r"}),
    ("some_other_tool", {"x": 1}),
])
def test_read_tools_need_no_approval(services, tool, args):
    assert approval.approval_for(tool, args, None, services) is None
    hook = approval.make_pre_tool_call(lambda: {}, lambda: services)
    assert hook(tool_name=tool, args=args, session_id="s") is None


def test_toolspec_approval_used(services):
    tools = _tools(env_spawn=lambda a, s: f"Spawn {a['template']} on {a['host_id']} (sends HF_TOKEN [redacted])?",
                   compute_status=None)
    hook = approval.make_pre_tool_call(lambda: tools, lambda: services)
    res = hook(tool_name="env_spawn", args={"template": "t", "host_id": "h"}, session_id="s",
               task_id="", tool_call_id="c")
    assert res == {"action": "approve", "rule_key": "cloud-compute:env_spawn:spawn:h:t",
                   "message": "Spawn t on h (sends HF_TOKEN [redacted])?"}
    assert hook(tool_name="compute_status", args={}) is None


def test_toolspec_returning_none_means_no_approval(services):
    tools = _tools(env_control=lambda a, s: None)
    assert approval.approval_for("env_control", {"action": "destroy"},
                                 tools["env_control"], services) is None


def test_rule_key_without_host(services):
    res = approval.approval_for("env_control", {"action": "destroy", "instance_id": "nope"},
                                None, services)
    assert res["rule_key"] == "cloud-compute:env_control:destroy-save:nope"


def test_rule_key_resolves_host_from_instance(services):
    services.store.upsert_instance(sample_instance())
    res = approval.approval_for("env_control", {"action": "destroy", "instance_id": "i-ab12cd34"},
                                None, services)
    assert res["rule_key"] == "cloud-compute:env_control:destroy-save:i-ab12cd34"


def test_remote_only_skips_local(services):
    services.store.upsert_host(sample_local_host())
    services.store.upsert_host(sample_host())
    services.config.approval = "remote-only"
    spawn = {"template": "python-dev", "host_id": "local"}
    assert approval.approval_for("env_spawn", spawn, None, services) is None
    assert approval.approval_for("env_spawn", {**spawn, "host_id": "x1carbon"}, None, services)
    services.config.approval = "always"
    assert approval.approval_for("env_spawn", spawn, None, services)


def test_fails_closed_on_toolspec_error(services):
    def bad(a, s):
        raise _Boom("secret-value-123")

    tools = _tools(env_spawn=bad)
    res = approval.approval_for("env_spawn", {"host_id": "h"}, tools["env_spawn"], services)
    assert res["action"] == "approve"
    assert "secret-value-123" not in res["message"]
    assert res["rule_key"].startswith("cloud-compute:env_spawn:unverified:")


def test_fails_closed_on_store_error():
    class BadStore:
        def get_host(self, h):
            raise _Boom()

        def get_instance(self, i):
            raise _Boom()

    svc = FakeSvc(BadStore(), Config(approval="remote-only"))
    res = approval.approval_for("env_spawn", {"template": "t", "host_id": "h"}, None, svc)
    assert res["action"] == "approve" and res["message"] == approval.GENERIC_MESSAGE


def test_fails_closed_on_services_error():
    def boom():
        raise _Boom()

    hook = approval.make_pre_tool_call(lambda: {}, boom)
    res = hook(tool_name="env_spawn", args={"host_id": "h"})
    assert res["action"] == "approve"
    assert hook(tool_name="compute_status", args={}) is None
    assert hook(tool_name=None, args=None) is None


def test_fails_closed_on_tools_error():
    def boom():
        raise _Boom()

    hook = approval.make_pre_tool_call(boom, lambda: None)
    assert hook(tool_name="slurm_submit", args={})["action"] == "approve"


def test_approval_uses_store_only():
    store = FakeStore(hosts={"h": sample_host(id="h")})
    svc = FakeSvc(store, Config(approval="remote-only"))
    assert approval.approval_for("env_spawn", {"template": "t", "host_id": "h"}, None, svc)
    assert set(store.calls) <= {"get_host", "get_instance", "get_job"}


def test_messages_have_no_secret_values(services):
    args = {"template": "t", "host_id": "h", "ttl_minutes": 60, "name": "n",
            "variables": [{"name": "HF_TOKEN", "source": "literal:hunter2"}], "script": "echo hunter2"}
    for tool in ("env_spawn", "slurm_submit", "cloud_agent_spawn"):
        assert "hunter2" not in approval.approval_for(tool, args, None, services)["message"]


# --------------------------------------------------------------------------- commands


def test_command_constants():
    assert commands.COMMAND_NAME == "compute"
    assert commands.ARGS_HINT == "[status|hosts|jobs]"
    assert "/compute" in commands.COMMAND_HELP


def test_command_views(services):
    _seed(services.store)
    cmd = commands.make_compute_command(lambda: services)
    status = cmd("")
    assert "[cloud-compute] Online: x1carbon" in status and cmd("status") == status
    hosts = cmd("hosts")
    assert "x1carbon  ssh  online" in hosts and "slurm-x1  slurm" in hosts
    jobs = cmd(" JOBS ")
    assert "j-1a2b3c4d  RUNNING  hello  on slurm-x1" in jobs


def test_command_empty_state(services):
    cmd = commands.make_compute_command(lambda: services)
    assert cmd("hosts") == "No hosts configured."
    assert cmd("jobs") == "No active or recent jobs."


def test_command_unknown_arg_gives_help(services):
    cmd = commands.make_compute_command(lambda: services)
    assert cmd("bogus") == commands.COMMAND_HELP


def test_command_never_raises():
    def boom():
        raise _Boom()

    out = commands.make_compute_command(boom)("status")
    assert isinstance(out, str) and out


def test_command_snapshot_only():
    store = FakeStore()
    svc = FakeSvc(store)
    for arg in ("", "hosts", "jobs", "x"):
        commands.render_command(svc, arg)
    assert set(store.calls) <= {"snapshot"}


def test_snapshot_is_json_safe(services):
    _seed(services.store)
    json.dumps(services.store.snapshot())


# --------------------------------------------------------------------------- data safety


def _inst(services, **kw):
    from cloud_compute_core.testing import sample_instance
    rec = sample_instance(**{"id": "i-ab12cd34", "name": "results", "host_id": "x1carbon", **kw})
    services.store.upsert_instance(rec)
    return rec


def test_destroy_approval_texts(services):
    _inst(services)
    save = approval.approval_for("env_control", {"instance_id": "i-ab12cd34",
                                                 "action": "destroy"}, None, services)
    assert save["message"] == ("Deletes results on x1carbon. The files in /work are saved to "
                               "this computer first, and it is deleted only if that copy is "
                               "complete. Files outside /work are not saved.")
    gone = approval.approval_for("env_control", {"instance_id": "i-ab12cd34", "action": "destroy",
                                                 "save": False}, None, services)
    assert gone["message"] == ("Deletes results on x1carbon and every file in it. Nothing is "
                               "saved, not even the files in /work.")


def test_extend_approval_text(services):
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _inst(services, expires_at=soon)
    res = approval.approval_for("env_control", {"instance_id": "i-ab12cd34", "action": "extend",
                                                "minutes": 90}, None, services)
    want = (datetime.now(timezone.utc) + timedelta(hours=2, minutes=90)).astimezone()
    assert res["message"].startswith("Extends results by 90 minutes (until ")
    assert want.strftime("%Y-%m-%d") in res["message"]
    assert res["message"].endswith(" local time).") or res["message"].endswith(" UTC).")


@pytest.mark.parametrize("args,want", [
    ({"action": "put", "instance_id": "i-ab12cd34", "local_path": "C:/data/in.csv",
      "dest": "/work/in.csv"},
     "Uploads C:/data/in.csv from this computer to results:/work/in.csv. "
     "Files with the same names are replaced."),
    ({"action": "restore", "instance_id": "i-ab12cd34", "export_id": "x-0011aabb",
      "dest": "/work"},
     "Restores the saved copy x-0011aabb into results:/work. "
     "Files with the same names are replaced."),
    ({"action": "delete_export", "export_id": "x-0011aabb"},
     "Deletes the saved copy x-0011aabb from this computer. It cannot be recovered."),
])
def test_env_files_approval_texts(services, args, want):
    _inst(services)
    res = approval.approval_for("env_files", args, None, services)
    assert res["action"] == "approve" and res["message"] == want
    hook = approval.make_pre_tool_call(lambda: {}, lambda: services)
    assert hook(tool_name="env_files", args=args, session_id="s")["message"] == want


@pytest.mark.parametrize("action", ["list", "fetch", "exports"])
def test_env_files_reads_need_no_approval(services, action):
    assert approval.approval_for("env_files", {"action": action, "instance_id": "i"},
                                 None, services) is None


def test_env_files_unknown_action_fails_closed(services):
    res = approval.approval_for("env_files", {"action": "wipe"}, None, services)
    assert res["action"] == "approve" and res["message"] == approval.GENERIC_FILES_MESSAGE


def test_approval_texts_have_no_first_person():
    import re
    from pathlib import Path
    for mod in ("approval.py", "instances_api.py", "reaper.py", "sandbox.py"):
        src = (Path(approval.__file__).parent / mod).read_text(encoding="utf-8")
        for literal in re.findall(r'"([^"\n]*)"', src):
            assert not re.search(r"\b(we|We|I|our|Our)\b", literal), (mod, literal)
