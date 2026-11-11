"""Deleting a cloud-agent sandbox removes its anonymous volume (after the save); an
ordinary environment keeps whatever volume its image declares."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.runtimes import runtime_for
from cloud_compute_core.testing import sample_template


def _load(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


_sm = _load("cloud_compute_tests_sandbox_manager", "test_sandbox_manager.py")
env = _sm.env  # noqa: F811 - the shared sandbox fixture
_script_ok, _rm_calls = _sm._script_ok, _sm._rm_calls


def _agent_template(env, template_id="hermes-agent", mode="hermes"):
    env.templates.save(sample_template(
        id=template_id, name="Agent", builtin=False, variables=[], ttl_minutes=60,
        agent={"enabled": True, "mode": mode}))


@pytest.mark.parametrize("name", ["podman", "docker"])
def test_rm_argv_removes_only_anonymous_volumes_when_asked(name):
    rt = runtime_for(name)
    assert rt.lifecycle_argv("rm", "abc") == [name, "rm", "-f", "abc"]
    assert rt.lifecycle_argv("rm", "abc", volumes=True) == [name, "rm", "-f", "-v", "abc"]
    assert rt.lifecycle_argv("stop", "abc", volumes=True) == [name, "stop", "abc"]


def test_deleting_an_agent_sandbox_removes_its_anonymous_volume_after_the_save(env):
    _agent_template(env)
    _script_ok(env.t)
    rec = env.mgr.spawn("hermes-agent", "x1carbon")
    env.t.reset_calls()
    env.mgr.destroy(rec.id)
    assert env.files.exports == [(rec.id, "before-delete")]      # the save came first
    calls = _rm_calls(env.t)
    assert len(calls) == 1 and env.t.calls[calls[0]].argv == ["podman", "rm", "-f", "-v", f"cc-{rec.id}"]


def test_a_failed_save_removes_neither_container_nor_volume(env):
    _agent_template(env, "agent-script", "script")
    _script_ok(env.t)
    rec = env.mgr.spawn("agent-script", "x1carbon")
    env.files.fail = RuntimeError("disk full")
    env.t.reset_calls()
    with pytest.raises(ApiError) as info:
        env.mgr.destroy(rec.id)
    assert info.value.code == "not_saved"
    assert _rm_calls(env.t) == []


def test_an_ordinary_environment_keeps_the_volumes_its_image_declares(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.t.reset_calls()
    env.mgr.destroy(rec.id)
    calls = _rm_calls(env.t)
    assert len(calls) == 1 and env.t.calls[calls[0]].argv == ["podman", "rm", "-f", f"cc-{rec.id}"]
