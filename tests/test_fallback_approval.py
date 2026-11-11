"""The __init__ fallback approval (used only if the approval module fails to load)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "cloud_compute_fallback_test"


@pytest.fixture(scope="module")
def plugin():
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    yield module
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(PACKAGE + ".")]:
        del sys.modules[name]


GATED = [
    ("env_spawn", {"template": "python-dev"}),
    ("env_control", {"action": "stop", "instance_id": "i-1"}),
    ("env_control", {"action": "destroy", "instance_id": "i-1"}),
    ("env_files", {"action": "put", "instance_id": "i-1"}),
    ("env_files", {"action": "restore", "export_id": "e"}),
    ("env_files", {"action": "delete_export", "export_id": "e"}),
    ("slurm_submit", {"host_id": "h"}),
    ("slurm_jobs", {"action": "cancel", "job_id": "1"}),
    ("cloud_agent_spawn", {"task": "x"}),
    ("cloud_agent_stop", {"run_id": "r-1"}),
    ("cloud_agent_message", {"run_id": "a-11111111", "message": "go on"}),
    ("cloud_agent_rerun", {"run_id": "a-11111111"}),
    ("compute_host", {"action": "add", "kind": "ssh", "address": "h"}),
    ("compute_host", {"action": "update", "host_id": "h-1", "name": "x"}),
    ("compute_host", {"action": "remove", "host_id": "h-1"}),
    ("compute_host", {"action": "test", "kind": "ssh", "address": "h"}),
    ("compute_host", {"action": "test", "host_id": "h-1", "address": "other"}),
    ("env_files", {"action": "fetch", "instance_id": "i-1", "dest_dir": "C:/Users/x"}),
    ("env_files", {"action": "fetch", "instance_id": "i-1", "extract": True}),
    ("env_files", {"action": "fetch", "instance_id": "i-1", "extract": "true"}),
]
FREE = [
    ("env_control", {"action": "logs", "instance_id": "i-1"}),
    ("env_files", {"action": "list"}),
    ("env_files", {"action": "exports"}),
    ("env_files", {"action": "fetch", "instance_id": "i-1"}),
    ("env_files", {"action": "fetch", "instance_id": "i-1", "extract": False,
                   "dest_dir": "  "}),
    ("compute_host", {"action": "list"}),
    ("compute_host", {"action": "types"}),
    ("compute_host", {"action": "test", "host_id": "h-1"}),
    ("slurm_jobs", {"action": "list"}),
    ("env_status", {}),
]


@pytest.mark.parametrize("tool,args", GATED)
def test_state_changing_tools_are_gated(plugin, tool, args):
    got = plugin._fallback_approval(tool, args)
    assert got and got["action"] == "approve" and got["rule_key"].startswith(f"cloud-compute:{tool}:")


@pytest.mark.parametrize("tool,args", FREE)
def test_read_only_calls_pass(plugin, tool, args):
    assert plugin._fallback_approval(tool, args) is None


def test_unknown_env_files_action_and_bad_args_fail_closed(plugin):
    assert plugin._fallback_approval("env_files", {"action": "wipe"})
    assert plugin._fallback_approval("env_control", "not-a-dict")


def test_rule_key_unique_per_call_and_stable(plugin):
    a = plugin._fallback_approval("cloud_agent_stop", {"run_id": "r-1", "x": 1})
    b = plugin._fallback_approval("cloud_agent_stop", {"x": 1, "run_id": "r-1"})
    c = plugin._fallback_approval("cloud_agent_stop", {"run_id": "r-2"})
    assert a["rule_key"] == b["rule_key"] != c["rule_key"]
    d1 = plugin._fallback_approval("env_files", {"action": "delete_export", "export_id": "e1"})
    d2 = plugin._fallback_approval("env_files", {"action": "delete_export", "export_id": "e2"})
    assert d1["rule_key"] != d2["rule_key"]


def test_compute_host_changes_are_gated_in_any_letter_case_and_bad_args(plugin):
    assert plugin._fallback_approval("compute_host", {"action": " ADD "})
    assert plugin._fallback_approval("compute_host", "not-a-dict")
    assert plugin._fallback_approval("compute_host", {}) is None


def test_the_approval_module_covers_the_same_tools_when_the_registry_is_empty():
    """Same coverage in core/approval.py when its tool registry has no spec (fail closed)."""
    from types import SimpleNamespace

    from cloud_compute_core import approval

    services = SimpleNamespace(config=SimpleNamespace(approval="always"),
                               store=SimpleNamespace(get_host=lambda _id: None,
                                                     get_instance=lambda _id: None,
                                                     get_record=lambda *a: None))
    hook = approval.make_pre_tool_call(lambda: {}, lambda: services)
    for tool, args in (
            ("cloud_agent_message", {"run_id": "a-11111111", "message": "x"}),
            ("cloud_agent_rerun", {"run_id": "a-11111111"}),
            ("compute_host", {"action": "add", "kind": "ssh", "address": "h"}),
            ("compute_host", {"action": "update", "host_id": "h"}),
            ("compute_host", {"action": "remove", "host_id": "h"}),
            ("env_files", {"action": "fetch", "instance_id": "i-1", "dest_dir": "C:/x"}),
            ("env_files", {"action": "fetch", "instance_id": "i-1", "extract": True}),
            ("slurm_jobs", {"action": "cancel", "job_id": "1"})):
        got = hook(tool_name=tool, args=args)
        assert got and got["action"] == "approve", (tool, args)
        assert got["rule_key"].startswith(f"cloud-compute:{tool}:")
    for tool, args in (("compute_host", {"action": "list"}),
                       ("env_files", {"action": "fetch", "instance_id": "i-1"}),
                       ("slurm_jobs", {"action": "list"})):
        assert hook(tool_name=tool, args=args) is None, (tool, args)


def test_rerun_rule_keys_differ_by_run_and_by_what_changes():
    from cloud_compute_core.approval import rule_key

    a = rule_key("cloud_agent_rerun", {"run_id": "a-11111111"}, "x1")
    b = rule_key("cloud_agent_rerun", {"run_id": "a-11111111", "model": "other"}, "x1")
    c = rule_key("cloud_agent_rerun", {"run_id": "a-22222222"}, "x1")
    assert len({a, b, c}) == 3 and a.startswith("cloud-compute:cloud_agent_rerun:rerun:a-11111111:")
