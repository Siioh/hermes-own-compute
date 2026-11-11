"""The normal approval path and the fallback hook ask in the same cases."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_compute_core import approval, files_api

PLUGIN_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def plugin():
    name = "cc_plugin_safety_fallback"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    for key in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        del sys.modules[key]


class Files:
    def size(self, iid, path=None):
        return {"path": path or "/work", "known": True, "bytes": 1000, "files": 3, "reason": None}


def services():
    store = SimpleNamespace(
        get_host=lambda h: SimpleNamespace(id=h, name="Lab box", kind="ssh"),
        get_instance=lambda i: SimpleNamespace(id=i, name="box", host_id="lab", template_id="t"),
        get_record=lambda kind, rid: None)
    return SimpleNamespace(config=SimpleNamespace(approval="always"), store=store, files=Files())


def normal(tool_name, args):
    tools = {"env_files": next(t for t in files_api.tools() if t.name == "env_files")}
    return approval.approval_for(tool_name, args, tools.get(tool_name), services())


CASES = [
    ("env_open", {"kind": "port", "host_id": "lab", "remote_port": 2375}),
    ("env_open", {"kind": "port", "instance_id": "i-1", "remote_port": 8080}),
    ("env_open", {"kind": "terminal", "instance_id": "i-1"}),
    ("env_open", {"kind": "display", "instance_id": "i-1"}),
    ("env_open", {"kind": "port", "host_id": "", "instance_id": "i-1", "remote_port": 80}),
    ("env_files", {"action": "fetch", "instance_id": "i-1"}),
    ("env_files", {"action": "fetch", "instance_id": "i-1", "extract": True}),
    ("env_files", {"action": "fetch", "instance_id": "i-1", "extract": "true"}),
    ("env_files", {"action": "fetch", "instance_id": "i-1", "extract": False}),
    ("env_files", {"action": "fetch", "instance_id": "i-1", "dest_dir": "C:/out"}),
    ("env_files", {"action": "fetch", "instance_id": "i-1", "dest_dir": "C:/out", "extract": True}),
    ("env_files", {"action": "list", "instance_id": "i-1"}),
    ("env_files", {"action": "put", "instance_id": "i-1", "local_path": "C:/a"}),
]


@pytest.mark.parametrize("tool_name, args", CASES, ids=[f"{t}-{json.dumps(a, sort_keys=True)}"
                                                      for t, a in CASES])
def test_fallback_asks_exactly_when_the_normal_path_asks(plugin, tool_name, args):
    assert plugin._fallback_needs_approval(tool_name, args) == (normal(tool_name, args) is not None)


def test_fetch_extract_without_dest_dir_asks_and_says_where_it_lands():
    out = normal("env_files", {"action": "fetch", "instance_id": "i-1", "extract": True})
    assert out["action"] == "approve"
    assert "exports folder on this computer" in out["message"]
    assert "unpacks it into a new folder" in out["message"]


def test_the_extract_fetch_key_differs_from_the_plain_one():
    plain = approval.rule_key("env_files", {"action": "fetch", "instance_id": "i-1"}, "lab")
    unpack = approval.rule_key("env_files", {"action": "fetch", "instance_id": "i-1",
                                             "extract": True}, "lab")
    assert plain != unpack
