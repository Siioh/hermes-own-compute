"""Regressions: stale-record overwrites, destroy with the host gone, local Windows env files."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_compute_core import sandbox as sandbox_mod
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.reaper import Reaper
from cloud_compute_core.runtimes import RunSpec, container_name, runtime_for
from cloud_compute_core.sandbox import SandboxManager
from cloud_compute_core.testing import exec_result, sample_host

sys.path.insert(0, os.path.dirname(__file__))
from test_sandbox_manager import (  # noqa: E402,F401  (fixture reuse)
    SECRET, Clock, FakeTemplates, _inspect_json, _prepend, _script_ok, _template, env,
)


# --------------------------------------------------------------------------- #1 stale records


def test_exec_does_not_resurrect_destroyed_instance(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")

    def on_exec(argv, stdin):
        env.mgr.destroy(rec.id)          # user presses Destroy while the command runs
        return exec_result(rc=137, stderr="killed")

    _prepend(env.t, ("podman", "exec"), on_exec)
    env.mgr.exec(rec.id, "sleep 30")
    assert env.store.get_instance(rec.id).status == "destroyed"


def test_exec_does_not_resurrect_stopped_instance_and_keeps_activity(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")

    def on_exec(argv, stdin):
        env.clock.advance(minutes=5)
        env.mgr.stop(rec.id)
        return exec_result()

    _prepend(env.t, ("podman", "exec"), on_exec)
    env.mgr.exec(rec.id, "true")
    got = env.store.get_instance(rec.id)
    assert got.status == "stopped"
    assert got.last_activity_at == env.mgr._now_iso()


def test_exec_touches_before_the_command_runs(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    seen = {}

    def on_exec(argv, stdin):
        seen["activity"] = env.store.get_instance(rec.id).last_activity_at
        return exec_result()

    env.clock.advance(minutes=20)
    _prepend(env.t, ("podman", "exec"), on_exec)
    env.mgr.exec(rec.id, "true")
    assert seen["activity"] == env.mgr._now_iso()


def test_stats_do_not_resurrect_stopped_instance(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    name = container_name(rec.id)

    def on_stats(argv, stdin):
        env.mgr.stop(rec.id)             # user (or reaper) stops it while stats run
        return exec_result(stdout=json.dumps([{"name": name, "cpu_percent": "1%",
                                               "mem_usage": "10MB / 1GB"}]))

    _prepend(env.t, ("podman", "stats"), on_stats)
    out = env.mgr.refresh_stats("x1carbon")
    got = env.store.get_instance(rec.id)
    assert got.status == "stopped" and got.stats is None and out == {}


def test_stop_does_not_undo_a_destroy_made_meanwhile(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")

    def on_stop(argv, stdin):
        env.mgr.destroy(rec.id)
        return exec_result()

    _prepend(env.t, ("podman", "stop"), on_stop)
    env.mgr.stop(rec.id)
    assert env.store.get_instance(rec.id).status == "destroyed"


def test_start_does_not_undo_a_destroy_made_meanwhile(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.mgr.stop(rec.id)

    def on_start(argv, stdin):
        env.mgr.destroy(rec.id)
        return exec_result()

    _prepend(env.t, ("podman", "start"), on_start)
    env.mgr.start(rec.id)
    assert env.store.get_instance(rec.id).status == "destroyed"


def test_reconcile_does_not_overwrite_a_status_changed_meanwhile(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    name = container_name(rec.id)
    row = {"ID": "abc", "Names": [name], "State": "running", "Labels": {}}

    def on_ps(argv, stdin):
        env.mgr.destroy(rec.id)
        return exec_result(stdout=json.dumps([row]))

    _prepend(env.t, ("podman", "ps"), on_ps)
    env.mgr.list(refresh=True)
    assert env.store.get_instance(rec.id).status == "destroyed"


def _only_instance(env):
    (rec,) = env.store.list_instances()
    return rec


def test_spawn_does_not_resurrect_an_instance_destroyed_meanwhile(env):
    """Destroy pressed while the container is being created: the final save must not undo it."""
    _script_ok(env.t)

    def on_inspect(argv, stdin):
        env.mgr.destroy(_only_instance(env).id)
        return exec_result(stdout=_inspect_json())

    _prepend(env.t, ("podman", "inspect"), on_inspect)
    with pytest.raises(ApiError) as err:
        env.mgr.spawn("python-dev", "x1carbon")
    assert err.value.status == 409
    got = _only_instance(env)
    assert got.status == "destroyed" and got.container_id is None and not got.ports
    assert all(pl.get("reason") != "spawned" for pl in env.events.payloads("instances.changed"))


def test_spawn_removes_the_container_and_secrets_when_destroyed_meanwhile(env):
    _script_ok(env.t)
    seen = {}

    def on_inspect(argv, stdin):
        rec = _only_instance(env)
        seen["id"] = rec.id
        # destroyed by something that did not clean up (e.g. another process): the spawn
        # itself must drop what it created
        rec.status = "destroyed"
        env.store.upsert_instance(rec)
        env.store.kv_set(f"display-token:{rec.id}", "tok-plain")
        return exec_result(stdout=_inspect_json())

    _prepend(env.t, ("podman", "inspect"), on_inspect)
    with pytest.raises(ApiError):
        env.mgr.spawn("python-dev", "x1carbon")
    iid = seen["id"]
    rm_calls = [c for c in env.t.calls if c.kind == "run" and c.argv[:2] == ["podman", "rm"]
                and container_name(iid) in c.argv]
    assert rm_calls, "the just-created container must be removed"
    assert env.store.get_instance(iid).status == "destroyed"
    assert env.store.kv_get(f"display-token:{iid}") is None
    assert env.store.kv_get(f"instance_vars:{iid}") is None
    assert iid not in env.mgr._sent_values


def test_spawn_failure_does_not_overwrite_a_destroy_made_meanwhile(env):
    _script_ok(env.t)

    def on_run(argv, stdin):
        env.mgr.destroy(_only_instance(env).id)
        return exec_result(rc=125, stderr="boom")

    _prepend(env.t, ("podman", "run"), on_run)
    with pytest.raises(ApiError):
        env.mgr.spawn("python-dev", "x1carbon")
    assert _only_instance(env).status == "destroyed"


# --------------------------------------------------------------------------- #4 host gone


def _drop_host(env):
    env.store.delete_host("x1carbon")


def test_destroy_succeeds_locally_when_host_is_gone(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.store.kv_set(f"display-token:{rec.id}", "tok")
    closed = []
    env.services.set("access", SimpleNamespace(close_instance_tunnels=closed.append))
    env.mgr.stop(rec.id)
    closed.clear()
    _drop_host(env)

    out = env.mgr.destroy(rec.id)

    assert out.status == "destroyed"
    assert env.store.get_instance(rec.id).status == "destroyed"
    assert container_name(rec.id) in out.error and "x1carbon" in out.error
    assert "manually" in out.error
    assert env.store.kv_get(f"display-token:{rec.id}") is None
    assert closed == [rec.id]
    assert ("instances.changed" in [n for n, _ in env.events.events])
    assert env.mgr.destroy(rec.id).status == "destroyed"      # idempotent


def test_stop_with_host_gone_is_still_a_404(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    _drop_host(env)
    with pytest.raises(ApiError) as e:
        env.mgr.stop(rec.id)
    assert e.value.status == 404


class _RecordingSandboxes:
    def __init__(self, mgr):
        self.mgr, self.listed = mgr, []

    def destroy(self, iid, *, reason="destroyed"):
        return self.mgr.destroy(iid, reason=reason)

    def stop(self, iid, *, reason="stopped"):
        return self.mgr.stop(iid, reason=reason)

    def list_containers(self, host_id):
        self.listed.append(host_id)
        raise ApiError(404, "not_found", f"host {host_id} not found")

    def remove_container(self, host_id, name):
        raise AssertionError("must not run")


def test_reaper_marks_records_of_a_missing_host_once_without_errors(env, caplog):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.mgr.stop(rec.id)
    _drop_host(env)
    sb = _RecordingSandboxes(env.mgr)
    env.services.set("sandboxes", sb)
    reaper = Reaper(env.services, clock=env.clock)

    with caplog.at_level("INFO"):
        first = reaper.sweep()
        second = reaper.sweep()

    assert first.errors == [] and second.errors == []
    assert env.store.get_instance(rec.id).status == "destroyed"
    assert sb.listed == []                       # no container sweep on a gone host
    assert not [r for r in caplog.records if r.levelno >= 30 and rec.id in r.getMessage()
                and r.name.endswith("reaper")]


# --------------------------------------------------------------------------- #5 local Windows


@pytest.fixture
def winlocal(tmp_path, monkeypatch, services, fake_transports, fake_events, fake_secrets):
    home = tmp_path / "fake home"             # a home with a space, like C:\Users\John Smith
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # The env dir sits beside the database. Point it at a data dir with a space in it (like a
    # Windows profile "John Smith"); only the path is used, the open connection is untouched.
    services.store.path = home / "plugin data" / "cloud-compute.db"
    monkeypatch.setattr(sandbox_mod, "_is_windows", lambda: True)
    fake_secrets.set("MY_TOKEN", SECRET)
    host = sample_host(id="pc", name="pc", kind="local", address="localhost",
                       runtime_pref="docker")
    services.store.upsert_host(host)
    services.set("templates", FakeTemplates(_template(host_kinds=["local", "ssh"])))
    t = fake_transports.get("pc")
    mgr = SandboxManager(services, clock=Clock())
    return SimpleNamespace(home=home, mgr=mgr, t=t, host=host, services=services)


def _plugin_envdir(winlocal):
    """Where local env files must go: ``<plugin data dir>/envfiles`` (beside the database)."""
    return Path(winlocal.services.store.path).parent / "envfiles"


def _script_docker(t, on_run):
    t.script(("docker", "run"), on_run)
    t.script(("docker", "inspect"), exec_result(stdout=_inspect_json()))
    t.script(("docker", "rm"), exec_result())


def test_local_windows_env_file_is_a_native_path_written_by_python(winlocal):
    seen = {}

    def on_run(argv, stdin):
        path = argv[argv.index("--env-file") + 1]
        seen["path"] = path
        seen["exists"] = os.path.exists(path)
        seen["content"] = Path(path).read_text(encoding="utf-8") if seen["exists"] else None
        seen["mode"] = os.stat(path).st_mode & 0o777 if seen["exists"] else None
        return exec_result(stdout="3f2a9c1d0b7e\n")

    _script_docker(winlocal.t, on_run)
    rec = winlocal.mgr.spawn("python-dev", "pc")

    assert rec.status == "running"
    assert seen["exists"] is True, "docker.exe must be able to open the env file"
    assert f"API_TOKEN={SECRET}\n" in seen["content"]
    assert " " in seen["path"] and str(winlocal.home) in seen["path"]   # space kept as one argv word
    assert Path(seen["path"]).parent == _plugin_envdir(winlocal)   # plugin data, not ~/.cache
    assert not (winlocal.home / ".cache").exists()
    if os.name != "nt":
        assert seen["mode"] == 0o600
    assert not os.path.exists(seen["path"])                              # removed after run
    assert not any(a[:1] == ["sh"] for a in winlocal.t.argvs())          # no MSYS shell involved


def test_local_windows_env_dir_is_inside_plugin_data_and_private(winlocal):
    seen = {}

    def on_run(argv, stdin):
        seen["dir"] = Path(argv[argv.index("--env-file") + 1]).parent
        seen["mode"] = os.stat(seen["dir"]).st_mode & 0o777
        return exec_result(stdout="3f2a9c1d0b7e\n")

    _script_docker(winlocal.t, on_run)
    winlocal.mgr.spawn("python-dev", "pc")
    data_dir = Path(winlocal.services.store.path).parent
    assert seen["dir"] == data_dir / "envfiles"
    if os.name != "nt":
        assert seen["mode"] == 0o700


def test_local_windows_env_file_removed_when_run_fails(winlocal):
    seen = {}

    def on_run(argv, stdin):
        seen["path"] = argv[argv.index("--env-file") + 1]
        return exec_result(rc=125, stderr="boom")

    _script_docker(winlocal.t, on_run)
    with pytest.raises(ApiError):
        winlocal.mgr.spawn("python-dev", "pc")
    assert seen["path"] and not os.path.exists(seen["path"])
    assert list(_plugin_envdir(winlocal).glob("*.env")) == []
    assert not (winlocal.home / ".cache").exists()


def test_local_windows_sweep_runs_in_python(winlocal):
    d = _plugin_envdir(winlocal)
    d.mkdir(parents=True)
    old, new = d / "old.env", d / "new.env"
    old.write_text("A=1\n")
    new.write_text("A=1\n")
    past = old.stat().st_mtime - 3600
    os.utime(old, (past, past))
    assert winlocal.mgr.sweep_env_files("pc", older_than_minutes=15) == 1
    assert not old.exists() and new.exists()
    assert winlocal.t.argvs() == []


def test_remote_hosts_keep_the_stdin_env_flow(env):
    _script_ok(env.t)
    env.mgr.spawn("python-dev", "x1carbon")
    writes = [c for c in env.t.calls if c.kind == "run" and c.argv[:2] == ["sh", "-c"]
              and "umask 077" in c.argv[2]]
    assert writes and SECRET.encode() in writes[0].stdin


def test_env_file_with_spaces_is_one_argv_word():
    spec = RunSpec(name="cc-x", image="img", labels={}, command=[],
                   env_file=r"C:\Users\John Smith\.cache\cloud-compute\i-1.env")
    for name in ("docker", "podman"):
        argv = runtime_for(name).run_argv(spec)
        assert argv[argv.index("--env-file") + 1] == spec.env_file
    for bad in ("-x", "a\nb", "a\x00b"):
        with pytest.raises(ValueError):
            runtime_for("docker").run_argv(RunSpec(name="cc-x", image="img", labels={},
                                                    command=[], env_file=bad))
