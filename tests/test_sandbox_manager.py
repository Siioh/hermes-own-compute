"""SandboxManager against FakeTransport: secrets over stdin, labels, limits, lifecycle."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cloud_compute_core import sandbox as sandbox_mod
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.models import HostLimits, VariableSpec
from cloud_compute_core.runtimes import (
    LABEL_INSTALL, LABEL_INSTANCE, LABEL_TEMPLATE, MANAGED_LABEL,
)
from cloud_compute_core.sandbox import INSTALL_ID_KEY, SandboxManager, get_install_id
from cloud_compute_core.testing import (
    exec_result, sample_capabilities, sample_host, sample_host_state, sample_template,
)

SECRET = "s3cr3t-value-XYZ"
LITERAL = "literal-value-123"
T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


class FakeTemplates:
    def __init__(self, *specs):
        self.items = {s.id: copy.deepcopy(s) for s in specs}

    def get(self, template_id):
        if template_id not in self.items:
            raise ApiError(404, "not_found", f"template {template_id} not found")
        return copy.deepcopy(self.items[template_id])

    def save(self, spec):
        self.items[spec.id] = copy.deepcopy(spec)
        return spec


def _template(**o):
    defaults = dict(
        network="none", cpus=1.5, memory_gb=2, workdir="/work", ttl_minutes=120, idle_minutes=30,
        variables=[
            VariableSpec(name="API_TOKEN", source="secret:MY_TOKEN"),
            VariableSpec(name="MODE", source=f"literal:{LITERAL}", scope="agent-visible"),
            VariableSpec(name="MISSING", source="secret:NOT_SET"),
            VariableSpec(name="ELSEWHERE", source="literal:x", hosts=["other-host"]),
        ])
    defaults.update(o)
    return sample_template(**defaults)


class FakeFiles:
    """Stand-in for ``services.files``: records exports; ``fail`` makes export raise it."""

    def __init__(self, fail: BaseException | None = None):
        self.fail = fail
        self.exports: list[tuple[str, str]] = []

    def export(self, instance_id, path=None, *, reason="manual"):
        if self.fail is not None:
            raise self.fail
        self.exports.append((instance_id, reason))
        return {"id": "x-00000001", "instance_id": instance_id, "reason": reason,
                "local_path": "C:/exports/work-i-1/x.tar.gz", "bytes": 2048, "files": 3}


@pytest.fixture
def env(services, fake_transports, fake_events, fake_secrets):
    fake_secrets.set("MY_TOKEN", SECRET)
    services.set("files", FakeFiles())
    host = sample_host(limits=HostLimits(max_instances=2), runtime_pref="podman")
    services.store.upsert_host(host)
    templates = FakeTemplates(_template())
    services.set("templates", templates)
    clock = Clock()
    mgr = SandboxManager(services, clock=clock)
    t = fake_transports.get(host.id)

    return SimpleNamespace(services=services, host=host, templates=templates, clock=clock,
                           mgr=mgr, t=t, events=fake_events, secrets=fake_secrets,
                           store=services.store, files=services.files)


def _is_write(argv, stdin=None):
    return argv[:2] == ["sh", "-c"] and "cat >" in argv[2]


def _is_rm_env(argv, stdin=None):
    return argv[:2] == ["sh", "-c"] and argv[2].startswith("rm -f")


def _inspect_json(status="running", port=None, env=None):
    d = {"Id": "f00dbabe" * 8, "State": {"Status": status, "StartedAt": "x"},
         "NetworkSettings": {"Ports": {}},
         "Config": {"Env": ["PATH=/usr/local/bin:/usr/bin:/bin", *(env or [])]}}
    if port:
        d["NetworkSettings"]["Ports"]["6080/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
    return json.dumps([d])


def _sent_env(t):
    """``NAME=value`` lines of the last env file written to the host (what ``run`` got)."""
    for c in reversed(t.calls):
        if c.kind == "run" and _is_write(c.argv, c.stdin) and c.stdin:
            return [ln for ln in c.stdin.decode("utf-8").splitlines() if "=" in ln]
    return []


def _script_ok(t, run_result=None):
    t.script(_is_write, lambda argv, stdin: exec_result(
        stdout=f"/home/u/.cache/cloud-compute/{argv[4]}.env\n"))
    t.script(_is_rm_env, exec_result())
    t.script(("podman", "run"), run_result or exec_result(stdout="3f2a9c1d0b7e\n"))
    # Like a real runtime: the container's configuration holds what the env file sent.
    t.script(("podman", "inspect"), lambda argv, stdin: exec_result(
        stdout=_inspect_json(env=_sent_env(t))))
    for verb in ("stop", "start", "rm", "exec", "logs", "stats", "ps"):
        t.script(("podman", verb), exec_result())
    return t


def _prepend(t, match, result):
    """Script a rule that wins over the ones already registered."""
    t.script(match, result)
    t._run_rules.insert(0, t._run_rules.pop())


def _calls(t, pred):
    return [i for i, c in enumerate(t.calls) if c.kind == "run" and pred(c.argv, c.stdin)]


# --------------------------------------------------------------------------- install id


def test_install_id_created_once_and_stable(store):
    first = get_install_id(store)
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    assert get_install_id(store) == first
    assert store.kv_get(INSTALL_ID_KEY) == first == store.kv_get("install_id")


def test_install_id_keeps_existing_value_and_replaces_garbage(store):
    store.kv_set(INSTALL_ID_KEY, "inst-mine")
    assert get_install_id(store) == "inst-mine"
    store.kv_set(INSTALL_ID_KEY, "bad value\n")
    fresh = get_install_id(store)
    assert re.fullmatch(r"[0-9a-f]{12}", fresh) and store.kv_get(INSTALL_ID_KEY) == fresh


def test_provide_returns_manager(services):
    assert isinstance(sandbox_mod.provide(services), SandboxManager)


# --------------------------------------------------------------------------- spawn


def test_spawn_sends_env_on_stdin_only_and_removes_file(env):
    status_at_run = []

    def on_run(argv, stdin):
        status_at_run.append(env.store.list_instances()[0].status)
        return exec_result(stdout="Trying to pull...\n3f2a9c1d0b7e\n")

    _script_ok(env.t, on_run)
    rec = env.mgr.spawn("python-dev", "x1carbon", session_id="sess-1")

    assert status_at_run == ["creating"]
    writes = _calls(env.t, _is_write)
    runs = _calls(env.t, lambda a, s: a[:2] == ["podman", "run"])
    rms = _calls(env.t, _is_rm_env)
    assert len(writes) == len(runs) == len(rms) == 1
    assert writes[0] < runs[0] < rms[0]

    stdin = env.t.calls[writes[0]].stdin.decode()
    assert f"API_TOKEN={SECRET}\n" in stdin and f"MODE={LITERAL}\n" in stdin
    assert "MISSING" not in stdin and "ELSEWHERE" not in stdin
    assert "umask 077" in env.t.calls[writes[0]].argv[2]
    for c in env.t.calls:
        assert SECRET not in " ".join(c.argv or [])
        assert LITERAL not in " ".join(c.argv or [])
        if c.stdin is not None:
            assert c is env.t.calls[writes[0]]

    argv = env.t.calls[runs[0]].argv
    assert argv[argv.index("--name") + 1] == f"cc-{rec.id}"
    assert argv[argv.index("--env-file") + 1] == f"/home/u/.cache/cloud-compute/{rec.id}.env"
    labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
    install = get_install_id(env.store)
    assert f"{MANAGED_LABEL}=1" in labels
    assert f"{LABEL_INSTANCE}={rec.id}" in labels
    assert f"{LABEL_INSTALL}={install}" in labels
    assert f"{LABEL_TEMPLATE}=python-dev" in labels
    assert argv[argv.index("--cpus") + 1] == "1.5"
    assert argv[argv.index("--memory") + 1] == "2g"
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("-w") + 1] == "/work"
    assert "--init" in argv
    assert argv[-2:] == ["sleep", "infinity"]
    assert not any(a == "-p" for a in argv)

    assert rec.status == "running" and rec.runtime == "podman"
    assert rec.container_id == "3f2a9c1d0b7e"
    assert rec.created_at == rec.started_at == "2026-11-08T12:00:00Z"
    assert rec.expires_at == "2026-11-08T14:00:00Z"
    assert rec.idle_minutes == 30 and rec.session_id == "sess-1"
    assert env.store.get_instance(rec.id).status == "running"
    assert env.events.payloads("instances.changed") == [
        {"ids": [rec.id], "host_id": "x1carbon", "reason": "spawned"}]
    dumped = json.dumps(env.store.get_instance(rec.id).to_dict()) + json.dumps(env.events.events)
    assert SECRET not in dumped and LITERAL not in dumped


def test_spawn_overrides_and_persistent_has_no_expiry(env):
    env.templates.save(_template(id="keep", persistent=True))
    _script_ok(env.t)
    rec = env.mgr.spawn("keep", "x1carbon", name="mine", ttl_minutes=60, idle_minutes=10)
    assert rec.expires_at is None and rec.idle_minutes == 10 and rec.name == "mine"
    rec2 = env.mgr.spawn("python-dev", "x1carbon", ttl_minutes=60)
    assert rec2.expires_at == "2026-11-08T13:00:00Z"
    for bad in (0, 4, 1441, 10080, True, "60"):
        with pytest.raises(ApiError) as ei:
            env.mgr.spawn("python-dev", "x1carbon", ttl_minutes=bad)
        assert ei.value.status == 400 and "from 5 to 1440" in ei.value.message
    with pytest.raises(ApiError):
        env.mgr.spawn("python-dev", "x1carbon", idle_minutes=3)
    env.mgr.destroy(rec2.id)
    rec3 = env.mgr.spawn("python-dev", "x1carbon", ttl_minutes=1440, idle_minutes=5)
    assert rec3.idle_minutes == 5


def test_spawn_without_variables_writes_no_env_file(env):
    env.templates.save(_template(id="plain", variables=[]))
    _script_ok(env.t)
    env.mgr.spawn("plain", "x1carbon")
    assert not _calls(env.t, _is_write) and not _calls(env.t, _is_rm_env)
    run = env.t.argvs()[_calls(env.t, lambda a, s: a[:2] == ["podman", "run"])[0]]
    assert "--env-file" not in run


def test_env_file_removed_when_run_fails(env):
    _script_ok(env.t, exec_result(rc=125, stderr=f"Error: bad image; token {SECRET}"))
    with pytest.raises(ApiError) as ei:
        env.mgr.spawn("python-dev", "x1carbon")
    assert SECRET not in ei.value.message
    assert len(_calls(env.t, _is_rm_env)) == 1
    assert _calls(env.t, _is_rm_env)[0] > _calls(env.t, lambda a, s: a[:2] == ["podman", "run"])[0]
    rec = env.store.list_instances()[0]
    assert rec.status == "error" and "bad image" in rec.error and SECRET not in rec.error
    assert "[redacted]" in rec.error
    assert env.events.payloads("instances.changed")[-1]["reason"] == "error"
    cleanup = _calls(env.t, lambda a, s: a[:3] == ["podman", "rm", "-f"])
    assert cleanup and env.t.calls[cleanup[0]].argv[-1] == f"cc-{rec.id}"


def test_missing_workdir_falls_back_and_creates_it(env):
    missing = exec_result(rc=126, stderr='Error: workdir "/work" does not exist on container abc')
    _script_ok(env.t, [missing, exec_result(stdout="3f2a9c1d0b7e\n")])
    env.t.script(lambda a, s: a[:2] == ["podman", "cp"], exec_result(rc=125, stderr="no such container"))
    rec = env.mgr.spawn("python-dev", "x1carbon")
    assert rec.status == "running"
    runs = [a for a in env.t.argvs() if a[:2] == ["podman", "run"]]
    assert len(runs) == 2 and "-w" in runs[0] and "-w" not in runs[1]
    assert all("--env-file" in r for r in runs)
    argvs = env.t.argvs()
    assert ["podman", "exec", "--user", "0", f"cc-{rec.id}", "mkdir", "-p", "--", "/work"] in argvs
    assert len(_calls(env.t, _is_rm_env)) == 1
    assert _calls(env.t, _is_rm_env)[0] > _calls(env.t, lambda a, s: a[:2] == ["podman", "run"])[1]


def test_env_file_removed_when_write_fails(env):
    env.t.script(_is_write, exec_result(rc=1, stderr="disk full"))
    _script_ok(env.t)
    with pytest.raises(ApiError):
        env.mgr.spawn("python-dev", "x1carbon")
    assert len(_calls(env.t, _is_rm_env)) == 1
    assert not _calls(env.t, lambda a, s: a[:2] == ["podman", "run"])
    assert env.store.list_instances()[0].status == "error"


def test_spawn_rejects_newline_values(env):
    env.secrets.set("MY_TOKEN", "a\nB=c")
    _script_ok(env.t)
    with pytest.raises(ApiError) as ei:
        env.mgr.spawn("python-dev", "x1carbon")
    assert ei.value.status == 400 and env.store.list_instances() == []
    assert not env.t.calls


def test_max_instances_limit(env):
    _script_ok(env.t)
    env.mgr.spawn("python-dev", "x1carbon")
    second = env.mgr.spawn("python-dev", "x1carbon")
    with pytest.raises(ApiError) as ei:
        env.mgr.spawn("python-dev", "x1carbon")
    assert (ei.value.status, ei.value.code) == (409, "limit_reached")
    env.mgr.destroy(second.id)
    env.mgr.spawn("python-dev", "x1carbon")  # destroyed instances do not count


def test_cpu_limit(env):
    host = sample_host(limits=HostLimits(max_cpus=1.0))
    env.store.upsert_host(host)
    with pytest.raises(ApiError) as ei:
        env.mgr.spawn("python-dev", "x1carbon")
    assert ei.value.code == "limit_reached"


def test_rejects_slurm_templates_and_host_kinds(env):
    env.templates.save(_template(id="slurm-python", kind="slurm_job", host_kinds=["slurm"]))
    env.templates.save(_template(id="local-only", host_kinds=["local"]))
    for tid in ("slurm-python", "local-only"):
        with pytest.raises(ApiError) as ei:
            env.mgr.spawn(tid, "x1carbon")
        assert (ei.value.status, ei.value.code) == (400, "invalid")
    with pytest.raises(ApiError) as ei:
        env.mgr.spawn("nope", "x1carbon")
    assert ei.value.status == 404
    with pytest.raises(ApiError) as ei:
        env.mgr.spawn("python-dev", "no-host")
    assert ei.value.status == 404
    assert env.store.list_instances() == []


def test_display_template_without_display_support(env, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "_display_overrides", lambda: None)
    env.templates.save(_template(id="desktop-novnc", display=True))
    with pytest.raises(ApiError) as ei:
        env.mgr.spawn("desktop-novnc", "x1carbon")
    assert (ei.value.status, ei.value.code) == (503, "unavailable")
    assert "display support is not installed" in ei.value.message
    assert env.store.list_instances() == []


def test_display_template_with_overrides(env, monkeypatch):
    def overrides(instance_id):
        return ({"VNC_PASSWORD": "vnc-pass-999"}, [("127.0.0.1", None, 6080)],
                {"io.cloud-compute.display": "novnc:6080"})

    monkeypatch.setattr(sandbox_mod, "_display_overrides", lambda: overrides)
    env.templates.save(_template(id="desktop-novnc", display=True, variables=[]))
    _script_ok(env.t)
    _prepend(env.t, ("podman", "inspect"), exec_result(stdout=_inspect_json(port=41234)))
    rec = env.mgr.spawn("desktop-novnc", "x1carbon")
    write = env.t.calls[_calls(env.t, _is_write)[0]]
    assert b"VNC_PASSWORD=vnc-pass-999" in write.stdin
    run = env.t.argvs()[_calls(env.t, lambda a, s: a[:2] == ["podman", "run"])[0]]
    assert "127.0.0.1::6080" in run and "vnc-pass-999" not in " ".join(run)
    assert "io.cloud-compute.display=novnc:6080" in run
    assert rec.display and rec.ports == {"novnc": "127.0.0.1:41234"}
    assert len(_calls(env.t, _is_rm_env)) == 1


def test_agent_template_with_localhost_image(env):
    tpl = _template(id="hermes-agent", image="localhost/cloud-compute-agent:0.1",
                    agent={"enabled": True, "mode": "hermes", "key_secret": "MY_TOKEN"},
                    variables=[VariableSpec(name="CC_AGENT_API_KEY", source="secret:MY_TOKEN",
                                            scope="runtime")])
    env.templates.save(tpl)
    _script_ok(env.t)
    rec = env.mgr.spawn("hermes-agent", "x1carbon", name="agent-a-1234", session_id="s")
    assert rec.name == "agent-a-1234" and rec.runtime == "podman"
    assert env.mgr.get(rec.id).runtime == "podman"
    argvs = env.t.argvs()
    assert not any(a[:2] in (["podman", "pull"], ["podman", "build"]) for a in argvs)
    run = argvs[_calls(env.t, lambda a, s: a[:2] == ["podman", "run"])[0]]
    assert "localhost/cloud-compute-agent:0.1" in run and "--pull" not in " ".join(run)
    write = env.t.calls[_calls(env.t, _is_write)[0]]
    assert f"CC_AGENT_API_KEY={SECRET}".encode() in write.stdin


def test_runtime_from_host_state_when_pref_auto(env, fake_transports):
    host = sample_host(id="dock", address="dock", runtime_pref="auto")
    env.store.upsert_host(host)
    env.store.set_host_state(sample_host_state(host_id="dock",
                                               capabilities=sample_capabilities(runtime="docker")))
    t = fake_transports.get("dock")
    t.script(lambda a, s: True, exec_result(stdout="abc\n"))
    rec = env.mgr.spawn("python-dev", "dock")
    assert rec.runtime == "docker"
    assert any(a[:2] == ["docker", "run"] for a in t.argvs())


# --------------------------------------------------------------------------- preview


def test_preview_never_contains_values(env):
    p = env.mgr.preview("python-dev", "x1carbon")
    blob = json.dumps(p.to_dict())
    assert SECRET not in blob and LITERAL not in blob
    by = {v.name: v for v in p.variables}
    assert by["API_TOKEN"].will_send and by["API_TOKEN"].source_kind == "secret"
    assert by["MODE"].will_send and by["MODE"].scope == "agent-visible"
    assert not by["MISSING"].will_send and by["MISSING"].reason == "secret NOT_SET not set"
    assert not by["ELSEWHERE"].will_send and by["ELSEWHERE"].reason == "not allowed on host x1carbon"
    assert p.labels[MANAGED_LABEL] == "1"
    assert p.labels[LABEL_INSTALL] == get_install_id(env.store)
    assert p.network == "none" and p.publish == []
    assert any("NOT_SET" in w for w in p.warnings)
    assert not env.t.calls


def test_preview_does_not_read_secret_values(env):
    class CountingSecrets(type(env.secrets)):
        def get(self, name):  # noqa: D401 - only is_set is allowed
            raise AssertionError("preview must not read values")

        def is_set(self, name):
            return name in self.values

    env.services.set("secrets", CountingSecrets({"MY_TOKEN": SECRET}))
    p = env.mgr.preview("python-dev", "x1carbon")
    assert [v.will_send for v in p.variables] == [True, True, False, False]


def test_preview_warnings_for_kind_and_display(env, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "_display_overrides", lambda: None)
    env.templates.save(_template(id="desktop-novnc", display=True, host_kinds=["local"]))
    env.store.set_host_state(sample_host_state(
        capabilities=sample_capabilities(display="unsupported", display_reason="no X")))
    p = env.mgr.preview("desktop-novnc", "x1carbon")
    text = " | ".join(p.warnings)
    assert "does not run on ssh hosts" in text
    assert "display support is not installed" in text
    assert "display is not supported on this host: no X" in text
    assert p.publish == ["127.0.0.1::6080"] and p.display


# --------------------------------------------------------------------------- lifecycle


def test_stop_start_destroy_transitions_and_events(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.events.clear()
    env.clock.advance(minutes=5)

    stopped = env.mgr.stop(rec.id)
    assert stopped.status == "stopped" == env.store.get_instance(rec.id).status
    stop_argv = env.t.argvs()[-1]
    assert stop_argv[:2] == ["podman", "stop"] and stop_argv[-1] == f"cc-{rec.id}"
    assert stop_argv == ["podman", "stop", "-t", "3", f"cc-{rec.id}"]

    started = env.mgr.start(rec.id)
    assert started.status == "running"
    assert started.started_at == started.last_activity_at == "2026-11-08T12:05:00Z"

    gone = env.mgr.destroy(rec.id)
    assert gone.status == "destroyed"
    assert env.t.argvs()[-1] == ["podman", "rm", "-f", f"cc-{rec.id}"]
    kept = env.store.get_instance(rec.id)
    assert kept is not None and kept.status == "destroyed"
    assert [p["reason"] for p in env.events.payloads("instances.changed")] == [
        "stopped", "started", "destroyed"]
    assert all(p["host_id"] == "x1carbon" and p["ids"] == [rec.id]
               for p in env.events.payloads("instances.changed"))
    with pytest.raises(ApiError) as ei:
        env.mgr.start(rec.id)
    assert ei.value.status == 409
    assert env.mgr.list() == [] and len(env.mgr.list(include_destroyed=True)) == 1


def test_reaped_reason_and_destroy_tolerates_missing_container(env):
    _script_ok(env.t)
    a = env.mgr.spawn("python-dev", "x1carbon")
    b = env.mgr.spawn("python-dev", "x1carbon")
    env.events.clear()
    env.mgr.stop(a.id, reason="reaped")
    _prepend(env.t,
        lambda argv, s: argv[:2] == ["podman", "rm"],
        exec_result(rc=1, stderr="Error: no such container cc-x"))
    env.mgr.destroy(b.id, reason="reaped")
    assert [p["reason"] for p in env.events.payloads("instances.changed")] == ["reaped", "reaped"]
    assert env.store.get_instance(b.id).status == "destroyed"


def test_stop_failure_raises_and_keeps_status(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    _prepend(env.t,
        lambda argv, s: argv[:2] == ["podman", "stop"], exec_result(rc=125, stderr="boom"))
    with pytest.raises(ApiError):
        env.mgr.stop(rec.id)
    assert env.store.get_instance(rec.id).status == "running"


def test_stop_all(env):
    _script_ok(env.t)
    a = env.mgr.spawn("python-dev", "x1carbon")
    b = env.mgr.spawn("python-dev", "x1carbon")
    out = env.mgr.stop_all("x1carbon")
    assert {r.id for r in out} == {a.id, b.id}
    assert {r.status for r in env.mgr.list()} == {"stopped"}


def test_missing_instance_is_404(env):
    for call in (env.mgr.get, env.mgr.stop, env.mgr.start, env.mgr.destroy, env.mgr.touch,
                 lambda i: env.mgr.exec(i, "true"), env.mgr.logs):
        with pytest.raises(ApiError) as ei:
            call("i-00000000")
        assert (ei.value.status, ei.value.code) == (404, "not_found")


# --------------------------------------------------------------------------- exec / logs / touch


def test_exec_redacts_caps_and_touches(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    big = "A" * 50_000 + f" tail {SECRET} {LITERAL}"
    _prepend(env.t,
        lambda argv, s: argv[:2] == ["podman", "exec"],
        exec_result(rc=0, stdout=big, stderr=f"err {SECRET}"))
    env.clock.advance(minutes=7)
    res = env.mgr.exec(rec.id, "echo hi", workdir="/tmp")
    argv = env.t.argvs()[-1]
    assert argv == ["podman", "exec", "-w", "/tmp", f"cc-{rec.id}", "sh", "-lc", "echo hi"]
    assert SECRET not in res.stdout and SECRET not in res.stderr
    # MODE is an agent-visible literal: shown as is.
    assert res.stdout.endswith(f"tail [redacted] {LITERAL}")
    assert len(res.stdout.encode()) <= 20 * 1024
    assert res.stderr == "err [redacted]"
    assert env.store.get_instance(rec.id).last_activity_at == "2026-11-08T12:07:00Z"
    stored = json.dumps([r.to_dict() for r in env.store.list_instances()])
    assert SECRET not in stored


def test_exec_timeout_and_list_command(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    _prepend(env.t,
        lambda argv, s: argv[:2] == ["podman", "exec"],
        exec_result(rc=124, stderr="timed out", timed_out=True))
    res = env.mgr.exec(rec.id, ["python", "-V"], timeout_s=5)
    assert res.timed_out and res.rc == 124
    assert env.t.calls[-1].timeout == 5 and env.t.argvs()[-1][-2:] == ["python", "-V"]


def test_exec_requires_running(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.mgr.stop(rec.id)
    with pytest.raises(ApiError) as ei:
        env.mgr.exec(rec.id, "true")
    assert ei.value.status == 409


def test_logs_redacted(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    _prepend(env.t,
        lambda argv, s: argv[:2] == ["podman", "logs"],
        exec_result(stdout=f"started with {SECRET}\n", stderr="warn\n"))
    text = env.mgr.logs(rec.id, tail=50)
    assert SECRET not in text and "[redacted]" in text and "warn" in text
    assert env.t.argvs()[-1] == ["podman", "logs", "--tail", "50", f"cc-{rec.id}"]


def test_touch_updates_activity(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.clock.advance(hours=1)
    env.mgr.touch(rec.id)
    assert env.mgr.get(rec.id).last_activity_at == "2026-11-08T13:00:00Z"


# --------------------------------------------------------------------------- stats / refresh


def test_refresh_stats_one_call_per_host(env):
    _script_ok(env.t)
    a = env.mgr.spawn("python-dev", "x1carbon")
    b = env.mgr.spawn("python-dev", "x1carbon")
    rows = [{"name": f"cc-{a.id}", "id": "aaa", "cpu_percent": "3.5%",
             "mem_usage": "120MiB / 4GiB"},
            {"name": f"cc-{b.id}", "id": "bbb", "cpu_percent": "1.0%",
             "mem_usage": "10MiB / 2GiB"}]
    _prepend(env.t,
        lambda argv, s: argv[:2] == ["podman", "stats"], exec_result(stdout=json.dumps(rows)))
    env.events.clear()
    env.t.reset_calls()
    out = env.mgr.refresh_stats("x1carbon")
    assert len(env.t.argvs()) == 1 and env.t.argvs()[0][:3] == ["podman", "stats", "--no-stream"]
    assert out[a.id].cpu_pct == 3.5 and out[a.id].mem_used_mb == 120
    assert out[a.id].sampled_at == "2026-11-08T12:00:00Z"
    assert env.store.get_instance(b.id).stats.mem_limit_mb == 2048
    [payload] = env.events.payloads("instances.changed")
    assert sorted(payload["ids"]) == sorted([a.id, b.id])
    assert (payload["host_id"], payload["reason"]) == ("x1carbon", "stats")


def test_list_refresh_reconciles_with_ps(env):
    _script_ok(env.t)
    a = env.mgr.spawn("python-dev", "x1carbon")
    b = env.mgr.spawn("python-dev", "x1carbon")
    install = get_install_id(env.store)
    rows = [{"Id": "aaa", "Names": [f"cc-{a.id}"], "State": "exited",
             "Labels": {LABEL_INSTALL: install}}]
    _prepend(env.t,
        lambda argv, s: argv[:2] == ["podman", "ps"], exec_result(stdout=json.dumps(rows)))
    env.events.clear()
    recs = {r.id: r for r in env.mgr.list(refresh=True)}
    assert recs[a.id].status == "exited"
    assert b.id not in recs and env.store.get_instance(b.id).status == "destroyed"
    ps = [a for a in env.t.argvs() if a[:2] == ["podman", "ps"]]
    assert ps and f"label={LABEL_INSTALL}={install}" in ps[0]
    assert {p["reason"] for p in env.events.payloads("instances.changed")} == {"stopped", "destroyed"}


# --------------------------------------------------------------------------- containers by label


def test_list_and_remove_containers_checks_install_id(env):
    install = get_install_id(env.store)
    rows = [
        {"Id": "aaa111", "Names": ["cc-i-mine0001"], "State": "running",
         "Labels": {MANAGED_LABEL: "1", LABEL_INSTALL: install}},
        {"Id": "bbb222", "Names": ["cc-i-other001"], "State": "exited",
         "Labels": {MANAGED_LABEL: "1", LABEL_INSTALL: "ffffffffffff"}},
    ]
    env.t.script(("podman", "ps"), exec_result(stdout=json.dumps(rows)))
    env.t.script(("podman", "rm"), exec_result(stdout="cc-i-mine0001\n"))
    found = env.mgr.list_containers("x1carbon")
    assert found == [
        {"name": "cc-i-mine0001", "container_id": "aaa111", "state": "running",
         "labels": {MANAGED_LABEL: "1", LABEL_INSTALL: install}, "created_at": ""},
        {"name": "cc-i-other001", "container_id": "bbb222", "state": "exited",
         "labels": {MANAGED_LABEL: "1", LABEL_INSTALL: "ffffffffffff"}, "created_at": ""},
    ]
    assert f"label={MANAGED_LABEL}=1" in env.t.argvs()[0]

    with pytest.raises(ApiError) as ei:
        env.mgr.remove_container("x1carbon", "cc-i-other001")
    assert (ei.value.status, ei.value.code) == (409, "conflict")
    assert not any(a[:2] == ["podman", "rm"] for a in env.t.argvs())

    res = env.mgr.remove_container("x1carbon", "cc-i-mine0001")
    assert res.rc == 0 and env.t.argvs()[-1] == ["podman", "rm", "-f", "cc-i-mine0001"]
    with pytest.raises(ApiError) as ei:
        env.mgr.remove_container("x1carbon", "cc-i-unknown")
    assert ei.value.status == 404


# --------------------------------------------------------------------------- display image


def test_build_display_image(env, tmp_path, monkeypatch):
    missing = tmp_path / "nope" / "Containerfile"
    monkeypatch.setattr(sandbox_mod, "DISPLAY_CONTAINERFILE", missing)
    with pytest.raises(ApiError) as ei:
        env.mgr.build_display_image("x1carbon")
    assert (ei.value.status, ei.value.code) == (503, "unavailable")

    cf = tmp_path / "Containerfile"
    cf.write_bytes(b"FROM debian:bookworm-slim\n")
    monkeypatch.setattr(sandbox_mod, "DISPLAY_CONTAINERFILE", cf)
    env.t.script(("podman", "build"), exec_result(stdout="x" * 50_000 + "done"))
    res = env.mgr.build_display_image("x1carbon")
    call = env.t.calls[-1]
    assert call.argv == ["podman", "build", "-t", "localhost/cloud-compute-novnc:0.1", "-"]
    assert call.stdin == b"FROM debian:bookworm-slim\n"
    assert res.rc == 0 and res.stdout.endswith("done") and len(res.stdout) <= 20 * 1024


# --------------------------------------------------------------------------- real template store


def test_spawn_with_real_template_store(services, fake_transports):
    templates = pytest.importorskip("cloud_compute_core.templates")
    store = services.store
    store.upsert_host(sample_host())
    services.set("templates", templates.TemplateStore(store))
    mgr = SandboxManager(services, clock=Clock())
    _script_ok(fake_transports.get("x1carbon"))
    rec = mgr.spawn("python-dev", "x1carbon")
    assert rec.status == "running" and rec.template_id == "python-dev"
    p = mgr.preview("python-dev", "x1carbon")
    assert p.template_id == "python-dev"
    with pytest.raises(ApiError) as ei:
        mgr.spawn("slurm-python", "x1carbon")
    assert ei.value.status == 400


# --------------------------------------------------------------------------- redaction hardening


def _exec_out(env, rec, stdout, mgr=None):
    _prepend(env.t, lambda argv, s: argv[:2] == ["podman", "exec"], exec_result(stdout=stdout))
    return (mgr or env.mgr).exec(rec.id, "env").stdout


def _logs_out(env, rec, stdout, mgr=None):
    _prepend(env.t, lambda argv, s: argv[:2] == ["podman", "logs"], exec_result(stdout=stdout))
    return (mgr or env.mgr).logs(rec.id)


def test_redaction_survives_template_delete_edit_and_secret_rotation(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.secrets.set("MY_TOKEN", "rotated-value-777")
    out = _exec_out(env, rec, f"old {SECRET} new rotated-value-777")
    assert out == "old [redacted] new [redacted]"
    env.templates.save(_template(variables=[]))          # edited: variable gone
    assert SECRET not in _logs_out(env, rec, f"x {SECRET}\n")
    del env.templates.items["python-dev"]                # deleted
    env.secrets.values.clear()                           # unset / scope unbound
    out = _exec_out(env, rec, f"x {SECRET}\nAPI_TOKEN=other\n")
    assert SECRET not in out
    assert "API_TOKEN=other" in out   # in-memory values present: no name masking


def test_kv_records_names_only_and_destroy_drops_them(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    raw = env.store.kv_get(f"instance_vars:{rec.id}")
    assert raw and SECRET not in raw and LITERAL not in raw
    assert json.loads(raw) == [{"name": "API_TOKEN", "redact": True, "secret": "MY_TOKEN"}]
    env.mgr.destroy(rec.id)
    assert env.store.kv_get(f"instance_vars:{rec.id}") is None
    assert rec.id not in env.mgr._sent_values


def test_after_restart_resolves_recorded_secret_names(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    del env.templates.items["python-dev"]
    fresh = SandboxManager(env.services, clock=env.clock)   # in-memory values gone
    env.secrets.set("MY_TOKEN", "rotated-value-777")
    out = _exec_out(env, rec, "v rotated-value-777\nAPI_TOKEN=rotated-value-777\n", fresh)
    assert out == "v [redacted]\nAPI_TOKEN=[redacted]\n"


def test_after_restart_unresolvable_secret_fails_closed(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    fresh = SandboxManager(env.services, clock=env.clock)
    env.secrets.values.clear()
    text = f"ok\nAPI_TOKEN={SECRET} trailing\nXAPI_TOKEN=keep\nMODE={LITERAL}\n"
    out = _exec_out(env, rec, text, fresh)
    assert SECRET not in out
    assert "API_TOKEN=[redacted] trailing\n" in out and "XAPI_TOKEN=keep" in out
    assert f"MODE={LITERAL}" in out   # agent-visible, not a recorded name
    logs = _logs_out(env, rec, text, fresh)
    assert SECRET not in logs and "API_TOKEN=[redacted]" in logs


# On macOS the chat tool ran in another process than the spawn and could not
# read the secret by name, so a bare value came back unmasked. Each case below is a separate
# manager (memory empty), as a separate process or a restarted gateway would be.


def _other_process(env, secrets):
    services = env.services
    other = SandboxManager(services, clock=env.clock)
    services.set("secrets", secrets)
    return other


def test_a_chat_in_another_process_without_secret_access_still_masks(env, fake_secrets):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")                 # (a) spawned by the page
    other = _other_process(env, type(fake_secrets)())              # `hermes chat`: no scope
    out = _exec_out(env, rec, f"leak={SECRET}\nplain=hello secret_set=yes\n", other)
    assert out == "leak=[redacted]\nplain=hello secret_set=yes\n"


def test_after_a_restart_values_come_from_the_container_once(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    fresh = SandboxManager(env.services, clock=env.clock)          # (b) gateway restarted
    before = len(_calls(env.t, lambda a, s: a[:2] == ["podman", "inspect"]))
    assert _exec_out(env, rec, f"v {SECRET}", fresh) == "v [redacted]"
    assert SECRET not in _logs_out(env, rec, f"x {SECRET}\n", fresh)
    after = len(_calls(env.t, lambda a, s: a[:2] == ["podman", "inspect"]))
    assert after == before + 1                                     # read once, then memory
    raw = env.store.kv_get(f"instance_vars:{rec.id}")
    assert SECRET not in raw                                       # never stored


def test_another_profile_or_scope_with_a_different_value_still_masks(env, fake_secrets):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    other = _other_process(env, type(fake_secrets)({"MY_TOKEN": "other-profile-value"}))  # (c)
    out = _exec_out(env, rec, f"a {SECRET} b other-profile-value", other)
    assert out == "a [redacted] b [redacted]"


@pytest.mark.parametrize("change", ["deleted", "renamed"])
def test_a_deleted_or_renamed_secret_still_masks(env, change):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.secrets.values.pop("MY_TOKEN")                             # (d)
    if change == "renamed":
        env.secrets.set("MY_TOKEN_NEW", SECRET)
    fresh = SandboxManager(env.services, clock=env.clock)
    assert _exec_out(env, rec, f"{SECRET}\n", fresh) == "[redacted]\n"
    assert SECRET not in _logs_out(env, rec, f"{SECRET}\n", fresh)


def test_output_is_withheld_when_the_container_cannot_be_read(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    fresh = SandboxManager(env.services, clock=env.clock)
    env.secrets.values.clear()
    _prepend(env.t, lambda a, s: a[:2] == ["podman", "inspect"],
             exec_result(rc=125, stderr="Error: no such container"))
    runs = len(_calls(env.t, lambda a, s: a[:2] == ["podman", "exec"]))
    with pytest.raises(ApiError) as ei:
        fresh.exec(rec.id, f"echo {SECRET}")
    assert ei.value.status == 503
    assert ei.value.message == (
        f"Output withheld: {rec.name} was started with secret values, and this computer cannot "
        "read them back from its container right now to hide them. Check that the host "
        "x1carbon is reachable and that the environment still exists, then try again. "
        "The command was not run.")
    assert len(_calls(env.t, lambda a, s: a[:2] == ["podman", "exec"])) == runs
    with pytest.raises(ApiError) as ei:
        fresh.logs(rec.id)
    assert ei.value.message.startswith("Logs withheld: ")


def test_the_chat_tool_returns_the_withheld_sentence_not_the_output(env):
    from cloud_compute_core import instances_api

    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.services.set("sandboxes", SandboxManager(env.services, clock=env.clock))
    env.secrets.values.clear()
    _prepend(env.t, lambda a, s: a[:2] == ["podman", "inspect"], exec_result(rc=1))
    _prepend(env.t, lambda a, s: a[:2] == ["podman", "exec"], exec_result(stdout=SECRET))
    out = instances_api.env_exec({"instance_id": rec.id, "command": "env"}, env.services, {})
    text = json.dumps(out)
    assert SECRET not in text and "Output withheld" in text


def test_an_environment_without_secrets_needs_no_read_back(env):
    env.templates.save(_template(id="plain", variables=[
        VariableSpec(name="MODE", source="literal:hello-mac", scope="runtime")]))
    _script_ok(env.t)
    rec = env.mgr.spawn("plain", "x1carbon")
    fresh = SandboxManager(env.services, clock=env.clock)
    _prepend(env.t, lambda a, s: a[:2] == ["podman", "inspect"], exec_result(rc=1))
    assert _exec_out(env, rec, "plain=hello-mac secret_set=yes", fresh) == \
        "plain=hello-mac secret_set=yes"


def test_secret_shaped_tokens_are_still_masked_in_output(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    key = "sk-" + "a1B2c3D4e5F6g7H8i9J0"                           # made-up, vendor-shaped
    out = _exec_out(env, rec, f"{key} Authorization: Bearer abcdefgh12345678")
    assert key not in out and "abcdefgh12345678" not in out


def test_logs_failure_message_is_redacted(env):
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    del env.templates.items["python-dev"]
    env.secrets.values.clear()
    _prepend(env.t, lambda argv, s: argv[:2] == ["podman", "logs"],
             exec_result(rc=1, stderr=f"boom {SECRET}"))
    with pytest.raises(ApiError) as ei:
        env.mgr.logs(rec.id)
    assert SECRET not in ei.value.message and "[redacted]" in ei.value.message


def _display(env, monkeypatch, password="vnc-pass-999"):
    def overrides(instance_id):
        return ({"VNC_PASSWORD": password}, [("127.0.0.1", None, 6080)], {})
    monkeypatch.setattr(sandbox_mod, "_display_overrides", lambda: overrides)
    env.templates.save(_template(id="desktop-novnc", display=True, variables=[]))
    _script_ok(env.t)
    return env.mgr.spawn("desktop-novnc", "x1carbon")


def test_display_values_are_redacted_in_exec_and_logs(env, monkeypatch):
    rec = _display(env, monkeypatch)
    assert _exec_out(env, rec, "pw vnc-pass-999") == "pw [redacted]"
    assert "vnc-pass-999" not in _logs_out(env, rec, "VNC_PASSWORD=vnc-pass-999\n")
    raw = env.store.kv_get(f"instance_vars:{rec.id}")
    assert "vnc-pass-999" not in raw and "VNC_PASSWORD" in raw
    fresh = SandboxManager(env.services, clock=env.clock)
    out = _exec_out(env, rec, "VNC_PASSWORD=vnc-pass-999\n", fresh)
    assert out == "VNC_PASSWORD=[redacted]\n"


def test_display_value_with_nul_rejected(env, monkeypatch):
    def overrides(instance_id):
        return ({"VNC_PASSWORD": "a\x00b"}, [], {})
    monkeypatch.setattr(sandbox_mod, "_display_overrides", lambda: overrides)
    env.templates.save(_template(id="desktop-novnc", display=True, variables=[]))
    _script_ok(env.t)
    with pytest.raises(ApiError) as ei:
        env.mgr.spawn("desktop-novnc", "x1carbon")
    assert ei.value.status == 400
    assert not _calls(env.t, _is_write)
    assert not _calls(env.t, lambda a, s: a[:2] == ["podman", "run"])


def test_redaction_scope_and_length_rules(env):
    env.secrets.set("SHORT", "ab")
    env.templates.save(_template(id="mixed", variables=[
        VariableSpec(name="TINY_SECRET", source="secret:SHORT"),
        VariableSpec(name="TINY_LIT", source="literal:xyz"),
        VariableSpec(name="LONG_LIT", source="literal:long-literal-42"),
        VariableSpec(name="RUNTIME_LIT", source="literal:runtime-lit-43", scope="runtime"),
        VariableSpec(name="SHOWN", source="literal:visible-literal-44", scope="agent-visible"),
    ]))
    _script_ok(env.t)
    rec = env.mgr.spawn("mixed", "x1carbon")
    out = _exec_out(env, rec, "<ab> <xyz> long-literal-42 runtime-lit-43 visible-literal-44")
    # only secrets and literals scoped "redacted" are hidden ("runtime" was hidden too on macOS)
    assert out == "<[redacted]> <xyz> [redacted] runtime-lit-43 visible-literal-44"
    names = {e["name"] for e in json.loads(env.store.kv_get(f"instance_vars:{rec.id}"))}
    assert names == {"TINY_SECRET", "LONG_LIT"}


def test_env_file_is_created_fresh_with_noclobber(env):
    _script_ok(env.t)
    env.mgr.spawn("python-dev", "x1carbon")
    script = env.t.calls[_calls(env.t, _is_write)[0]].argv[2]
    order = [script.index(p) for p in ("umask 077", 'rm -f -- "$f"', "set -C", 'cat > "$f"')]
    assert order == sorted(order)


def test_sweep_env_files(env):
    def is_sweep(argv, stdin=None):
        return argv[:2] == ["sh", "-c"] and "find" in argv[2]
    env.t.script(is_sweep, exec_result(
        stdout="/home/u/.cache/cloud-compute/i-1.env\n/home/u/.cache/cloud-compute/i-2.env\n"))
    assert env.mgr.sweep_env_files("x1carbon") == 2
    call = env.t.calls[-1]
    assert call.argv[-2:] == ["sh", "15"]
    script = call.argv[2]
    assert "-maxdepth 1" in script and "'*.env'" in script and '-mmin +"$1"' in script
    assert "-print -delete" in script and ".cache/cloud-compute" in script
    assert env.mgr.sweep_env_files("x1carbon", older_than_minutes=60) == 2
    assert env.t.calls[-1].argv[-1] == "60"
    for bad in (0, -1, True, "15"):
        with pytest.raises(ApiError) as ei:
            env.mgr.sweep_env_files("x1carbon", older_than_minutes=bad)
        assert ei.value.status == 400
    with pytest.raises(ApiError) as ei:
        env.mgr.sweep_env_files("no-host")
    assert ei.value.status == 404
    _prepend(env.t, is_sweep, exec_result(rc=1, stderr="find: denied"))
    with pytest.raises(ApiError):
        env.mgr.sweep_env_files("x1carbon")


# --------------------------------------------------------------------------- display token kv


def _real_display(env, monkeypatch):
    from cloud_compute_core import display as display_mod
    monkeypatch.setattr(sandbox_mod, "_display_overrides", lambda: display_mod.display_run_overrides)
    env.templates.save(_template(id="desktop-novnc", display=True, variables=[]))
    _script_ok(env.t)


def test_display_spawn_persists_token_and_destroy_clears_it(env, monkeypatch):
    _real_display(env, monkeypatch)
    rec = env.mgr.spawn("desktop-novnc", "x1carbon")
    token = env.store.kv_get(f"display-token:{rec.id}")
    assert token and re.fullmatch(r"[0-9a-f]{32}", token)
    write = env.t.calls[_calls(env.t, _is_write)[0]]
    assert f"VNC_PASSWORD={token}".encode() in write.stdin
    env.mgr.destroy(rec.id)
    assert env.store.kv_get(f"display-token:{rec.id}") is None


def test_failed_display_spawn_clears_token(env, monkeypatch):
    _real_display(env, monkeypatch)
    _prepend(env.t, ("podman", "run"), exec_result(rc=125, stderr="boom"))
    with pytest.raises(ApiError):
        env.mgr.spawn("desktop-novnc", "x1carbon")
    rec = env.store.list_instances()[0]
    assert rec.status == "error"
    assert env.store.kv_get(f"display-token:{rec.id}") is None


def test_remove_orphan_container_clears_token(env):
    install = get_install_id(env.store)
    env.store.kv_set("display-token:i-mine0001", "0" * 32)
    rows = [{"Id": "aaa111", "Names": ["cc-i-mine0001"], "State": "running",
             "Labels": {MANAGED_LABEL: "1", LABEL_INSTALL: install,
                        "io.cloud-compute.instance": "i-mine0001"}}]
    env.t.script(("podman", "ps"), exec_result(stdout=json.dumps(rows)))
    env.t.script(("podman", "rm"), exec_result(stdout="cc-i-mine0001"))
    assert env.mgr.remove_container("x1carbon", "cc-i-mine0001").rc == 0
    assert env.store.kv_get("display-token:i-mine0001") is None


# --------------------------------------------------------------------------- tunnels on destroy


class _FakeAccess:
    def __init__(self, error=None):
        self.closed = []
        self.error = error

    def close_instance_tunnels(self, instance_id):
        self.closed.append(instance_id)
        if self.error is not None:
            raise self.error
        return []


def test_destroy_closes_that_instances_tunnels(env):
    _script_ok(env.t)
    access = _FakeAccess()
    env.services.set("access", access)
    keep = env.mgr.spawn("python-dev", "x1carbon")
    gone = env.mgr.spawn("python-dev", "x1carbon")
    env.mgr.destroy(gone.id)
    assert access.closed == [gone.id]
    assert env.store.get_instance(keep.id).status == "running"
    env.mgr.destroy(gone.id)                      # already destroyed: nothing more to do
    assert access.closed == [gone.id]


def test_reaped_destroy_also_closes_tunnels(env):
    _script_ok(env.t)
    access = _FakeAccess()
    env.services.set("access", access)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.mgr.destroy(rec.id, reason="reaped")
    assert access.closed == [rec.id]


def test_destroy_without_access_service_still_works(env):
    _script_ok(env.t)
    env.services.__dict__["_providers"].pop("access", None)
    env.services.__dict__["_instances"].pop("access", None)
    assert not env.services.available("access")
    rec = env.mgr.spawn("python-dev", "x1carbon")
    assert env.mgr.destroy(rec.id).status == "destroyed"
    assert env.store.get_instance(rec.id).status == "destroyed"


def test_destroy_survives_a_failing_access_service(env):
    _script_ok(env.t)
    access = _FakeAccess(error=RuntimeError("tunnel store down"))
    env.services.set("access", access)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    assert env.mgr.destroy(rec.id, reason="reaped").status == "destroyed"
    assert access.closed == [rec.id]
    assert env.store.get_instance(rec.id).status == "destroyed"
    assert [p["reason"] for p in env.events.payloads("instances.changed")][-1] == "reaped"


def test_destroy_closes_real_tunnel_records(env):
    from cloud_compute_core import tunnels as tunnels_mod
    from cloud_compute_core.access import AccessManager
    from cloud_compute_core.testing import sample_tunnel

    _script_ok(env.t)
    env.services.set("access", AccessManager(env.services))
    mine = env.mgr.spawn("python-dev", "x1carbon")
    other = env.mgr.spawn("python-dev", "x1carbon")
    for tid, iid, kind in (("t-mine0001", mine.id, "display"), ("t-mine0002", mine.id, "port"),
                           ("t-other001", other.id, "display")):
        env.store.upsert_tunnel(sample_tunnel(id=tid, instance_id=iid, kind=kind, pid=None))
    env.mgr.destroy(mine.id)
    assert env.store.get_tunnel("t-mine0001").status == "closed"
    assert env.store.get_tunnel("t-mine0002").status == "closed"
    assert env.store.get_tunnel("t-other001").status == "open"
    assert tunnels_mod._REGISTRY.get("t-other001") is None


# --------------------------------------------------------------------------- tunnels on stop


def test_stop_closes_that_instances_tunnels_and_start_does_not_reopen(env):
    _script_ok(env.t)
    access = _FakeAccess()
    env.services.set("access", access)
    keep = env.mgr.spawn("python-dev", "x1carbon")
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.mgr.stop(rec.id)
    assert access.closed == [rec.id]
    assert env.store.get_instance(keep.id).status == "running"
    env.mgr.start(rec.id)                         # start never reopens or re-closes tunnels
    assert access.closed == [rec.id]
    assert env.store.get_instance(rec.id).status == "running"


def test_stop_all_closes_every_stopped_instances_tunnels(env):
    _script_ok(env.t)
    access = _FakeAccess()
    env.services.set("access", access)
    a = env.mgr.spawn("python-dev", "x1carbon")
    b = env.mgr.spawn("python-dev", "x1carbon")
    env.mgr.stop_all("x1carbon")
    assert sorted(access.closed) == sorted([a.id, b.id])


def test_reaped_stop_closes_tunnels(env):
    _script_ok(env.t)
    access = _FakeAccess()
    env.services.set("access", access)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    env.mgr.stop(rec.id, reason="reaped")
    assert access.closed == [rec.id]


def test_failed_stop_keeps_tunnels_open(env):
    _script_ok(env.t)
    access = _FakeAccess()
    env.services.set("access", access)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    _prepend(env.t, lambda argv, s: argv[:2] == ["podman", "stop"],
             exec_result(rc=125, stderr="boom"))
    with pytest.raises(ApiError):
        env.mgr.stop(rec.id)
    assert access.closed == []
    assert env.store.get_instance(rec.id).status == "running"


def test_stop_without_access_service_still_works(env):
    _script_ok(env.t)
    env.services.__dict__["_providers"].pop("access", None)
    env.services.__dict__["_instances"].pop("access", None)
    assert not env.services.available("access")
    rec = env.mgr.spawn("python-dev", "x1carbon")
    assert env.mgr.stop(rec.id).status == "stopped"


def test_stop_survives_a_failing_access_service(env):
    _script_ok(env.t)
    access = _FakeAccess(error=RuntimeError("tunnel store down"))
    env.services.set("access", access)
    rec = env.mgr.spawn("python-dev", "x1carbon")
    assert env.mgr.stop(rec.id, reason="reaped").status == "stopped"
    assert access.closed == [rec.id]
    assert env.store.get_instance(rec.id).status == "stopped"
    assert [p["reason"] for p in env.events.payloads("instances.changed")][-1] == "reaped"


# --------------------------------------------------------------------------- default names


def _roomy(env):
    env.host.limits = HostLimits(max_instances=10)
    env.store.upsert_host(env.host)
    env.templates.save(_template(id="ubuntu-dev"))


def test_default_names_are_template_dash_smallest_free_number(env):
    _script_ok(env.t)
    _roomy(env)
    a = env.mgr.spawn("python-dev", "x1carbon")
    b = env.mgr.spawn("python-dev", "x1carbon")
    d = env.mgr.spawn("ubuntu-dev", "x1carbon")
    assert (a.name, b.name, d.name) == ("python-dev-1", "python-dev-2", "ubuntu-dev-1")
    env.mgr.destroy(a.id)                         # destroyed instances free their number
    c = env.mgr.spawn("python-dev", "x1carbon")
    assert c.name == "python-dev-1"
    assert env.mgr.spawn("python-dev", "x1carbon").name == "python-dev-3"


def test_stopped_instances_keep_their_number(env):
    _script_ok(env.t)
    a = env.mgr.spawn("python-dev", "x1carbon")
    env.mgr.stop(a.id)
    assert env.mgr.spawn("python-dev", "x1carbon").name == "python-dev-2"


def test_default_name_skips_custom_names_and_is_not_the_container_name(env):
    _script_ok(env.t)
    env.mgr.spawn("python-dev", "x1carbon", name="python-dev-1")
    rec = env.mgr.spawn("python-dev", "x1carbon")
    assert rec.name == "python-dev-2"
    assert not rec.name.startswith("cc-") and rec.id not in rec.name


# --------------------------------------------------------------------------- default idle / TTL settings


def _live_config(**values):
    """A LiveConfig over a mutable dict; ``min_interval`` is huge so only reload() re-reads."""
    from cloud_compute_core.config import Config, LiveConfig
    state = dict(values)
    cfg = LiveConfig(loader=lambda: Config(**state), min_interval=3600)
    return cfg, state


def test_unset_template_limits_use_the_settings_defaults(env):
    cfg, _state = _live_config(default_ttl_minutes=90, default_idle_minutes=15)
    env.services.set("config", cfg)
    env.templates.save(_template(id="unset", ttl_minutes=None, idle_minutes=None))
    _script_ok(env.t)
    rec = env.mgr.spawn("unset", "x1carbon")
    assert rec.expires_at == "2026-11-08T13:30:00Z" and rec.idle_minutes == 15
    assert env.store.get_instance(rec.id).idle_minutes == 15      # recorded on the instance


def test_without_settings_an_unset_template_still_gets_a_ttl_and_idle_stop(env):
    env.templates.save(_template(id="unset", ttl_minutes=None, idle_minutes=None))
    _script_ok(env.t)
    rec = env.mgr.spawn("unset", "x1carbon")        # the Config default is 240 / 60
    assert rec.expires_at == "2026-11-08T16:00:00Z" and rec.idle_minutes == 60
    env.services.set("config", None)                 # no config service at all
    rec2 = env.mgr.spawn("unset", "x1carbon")
    assert rec2.expires_at == "2026-11-08T16:00:00Z" and rec2.idle_minutes == 60


def test_template_values_beat_the_settings_defaults(env):
    cfg, _state = _live_config(default_ttl_minutes=90, default_idle_minutes=15)
    env.services.set("config", cfg)
    _script_ok(env.t)
    rec = env.mgr.spawn("python-dev", "x1carbon")    # the test template says 120 / 30
    assert rec.expires_at == "2026-11-08T14:00:00Z" and rec.idle_minutes == 30
    env.templates.save(_template(id="half", ttl_minutes=60, idle_minutes=None))
    half = env.mgr.spawn("half", "x1carbon")         # mixed: TTL explicit, idle from Settings
    assert half.expires_at == "2026-11-08T13:00:00Z" and half.idle_minutes == 15


def test_request_values_beat_template_and_settings(env):
    cfg, _state = _live_config(default_ttl_minutes=90, default_idle_minutes=15)
    env.services.set("config", cfg)
    env.templates.save(_template(id="unset", ttl_minutes=None, idle_minutes=None))
    _script_ok(env.t)
    rec = env.mgr.spawn("unset", "x1carbon", ttl_minutes=45, idle_minutes=7)
    assert rec.expires_at == "2026-11-08T12:45:00Z" and rec.idle_minutes == 7


def test_a_saved_setting_applies_to_the_next_spawn_without_a_restart(env):
    cfg, state = _live_config(default_ttl_minutes=90, default_idle_minutes=15)
    env.services.set("config", cfg)
    env.templates.save(_template(id="unset", ttl_minutes=None, idle_minutes=None))
    _script_ok(env.t)
    first = env.mgr.spawn("unset", "x1carbon")
    assert first.idle_minutes == 15 and first.expires_at == "2026-11-08T13:30:00Z"
    state.update(default_ttl_minutes=600, default_idle_minutes=120)      # the user saves Settings
    second = env.mgr.spawn("unset", "x1carbon")
    assert second.idle_minutes == 120 and second.expires_at == "2026-11-08T22:00:00Z"
    assert env.store.get_instance(first.id).idle_minutes == 15           # running one is untouched


def test_persistent_template_still_has_no_expiry_but_gets_the_idle_default(env):
    cfg, _state = _live_config(default_idle_minutes=20)
    env.services.set("config", cfg)
    env.templates.save(_template(id="keep", persistent=True, ttl_minutes=None, idle_minutes=None))
    _script_ok(env.t)
    rec = env.mgr.spawn("keep", "x1carbon")
    assert rec.expires_at is None and rec.idle_minutes == 20


def test_effective_limits_reports_what_a_spawn_would_apply(env):
    cfg, _state = _live_config(default_ttl_minutes=90, default_idle_minutes=15)
    env.services.set("config", cfg)
    env.templates.save(_template(id="unset", ttl_minutes=None, idle_minutes=None))
    assert env.mgr.effective_limits("unset") == (90, 15)
    assert env.mgr.effective_limits("unset", 30, None) == (30, 15)
    assert env.mgr.effective_limits("python-dev") == (120, 30)
    with pytest.raises(ApiError):
        env.mgr.effective_limits("unset", 2, None)


def test_spawn_approval_names_the_settings_ttl_for_an_unset_template(env):
    from cloud_compute_core import instances_api
    cfg, _state = _live_config(default_ttl_minutes=90, default_idle_minutes=15)
    env.services.set("config", cfg)
    env.services.set("sandboxes", env.mgr)
    env.templates.save(_template(id="unset", ttl_minutes=None, idle_minutes=None, variables=[]))
    msg = instances_api.approval_env_spawn({"template": "unset", "host_id": "x1carbon"}, env.services)
    assert "TTL 90 min" in msg and "no TTL" not in msg


def test_the_reaper_enforces_the_values_recorded_at_spawn(env):
    from cloud_compute_core.reaper import Reaper
    cfg, state = _live_config(default_ttl_minutes=30, default_idle_minutes=10)
    env.services.set("config", cfg)
    env.services.set("sandboxes", env.mgr)
    env.templates.save(_template(id="unset", ttl_minutes=None, idle_minutes=None))
    _script_ok(env.t)
    idle_rec = env.mgr.spawn("unset", "x1carbon")
    state.update(default_ttl_minutes=1440, default_idle_minutes=1440)    # Settings changed afterwards
    env.clock.advance(minutes=11)
    report = Reaper(env.services).sweep(env.clock.now)
    assert idle_rec.id in report.stopped, report                         # 10 min idle, as recorded
    assert env.store.get_instance(idle_rec.id).status == "stopped"
    env.mgr.start(idle_rec.id)                                           # running again
    env.clock.advance(minutes=20)                                        # 31 min after spawn
    report = Reaper(env.services).sweep(env.clock.now)
    assert idle_rec.id in report.expired, report                         # TTL 30, as recorded
    assert report.destroyed == []                                        # stopped, never deleted
    assert env.store.get_instance(idle_rec.id).stop_reason == "expired"


# --------------------------------------------------------------------------- data safety


def _rm_calls(t):
    return _calls(t, lambda a, s: a[:2] == ["podman", "rm"])


def _spawned(env, **kw):
    _script_ok(env.t)
    return env.mgr.spawn("python-dev", "x1carbon", **kw)


def test_spawn_records_the_applied_ttl(env):
    rec = _spawned(env, ttl_minutes=45)
    assert rec.ttl_minutes == 45
    assert env.store.get_instance(rec.id).ttl_minutes == 45
    env.templates.save(_template(id="keep", persistent=True))
    _script_ok(env.t)
    assert env.mgr.spawn("keep", "x1carbon").ttl_minutes is None


def test_stop_reasons_user_idle_expired_and_stop_time(env):
    rec = _spawned(env)
    assert env.mgr.stop(rec.id).stop_reason == "user"
    assert env.store.kv_get(sandbox_mod.STOPPED_AT_KEY_PREFIX + rec.id) == "2026-11-08T12:00:00Z"
    env.mgr.start(rec.id)
    assert env.store.get_instance(rec.id).stop_reason is None
    assert env.store.kv_get(sandbox_mod.STOPPED_AT_KEY_PREFIX + rec.id) is None
    assert env.mgr.stop(rec.id, reason="idle").stop_reason == "idle"
    env.mgr.start(rec.id)
    assert env.mgr.stop(rec.id, reason="reaped").stop_reason == "idle"     # legacy name
    env.mgr.start(rec.id)
    assert env.mgr.stop(rec.id, reason="expired").stop_reason == "expired"


def test_stopped_at_starts_the_clock_when_unknown(env):
    rec = _spawned(env)
    assert env.mgr.stopped_at(rec.id) is None                          # running
    env.mgr.stop(rec.id)
    env.store.kv_delete(sandbox_mod.STOPPED_AT_KEY_PREFIX + rec.id)    # e.g. older version
    env.clock.advance(hours=3)
    assert env.mgr.stopped_at(rec.id) == "2026-11-08T15:00:00Z"
    env.clock.advance(hours=3)
    assert env.mgr.stopped_at(rec.id) == "2026-11-08T15:00:00Z"        # kept, not moved


def test_start_after_expiry_grants_a_new_time_limit(env):
    rec = _spawned(env, ttl_minutes=30)
    env.clock.advance(minutes=31)
    env.mgr.mark_expiry_warned(rec.id)
    env.mgr.stop(rec.id, reason="expired")
    assert env.mgr.lifetime_on_start(rec.id) == 30
    env.clock.advance(days=2)
    fresh = env.mgr.start(rec.id)
    assert fresh.status == "running" and fresh.stop_reason is None
    assert fresh.expiry_warned_at is None
    assert fresh.expires_at == "2026-11-10T13:01:00Z"                 # now + ttl_minutes
    assert env.mgr.lifetime_on_start(rec.id) is None


def test_start_before_expiry_keeps_the_time_limit(env):
    rec = _spawned(env, ttl_minutes=120)
    env.mgr.stop(rec.id)
    env.clock.advance(minutes=10)
    assert env.mgr.lifetime_on_start(rec.id) is None
    assert env.mgr.start(rec.id).expires_at == rec.expires_at


def test_start_of_old_expired_record_without_ttl_uses_the_default(env):
    rec = _spawned(env)
    env.mgr._update(rec.id, lambda r: setattr(r, "ttl_minutes", None))
    env.clock.advance(days=1)
    env.mgr.stop(rec.id, reason="expired")
    assert env.mgr.start(rec.id).expires_at == "2026-11-09T16:00:00Z"   # Config default 240


def test_expiry_warning_marks_once(env):
    rec = _spawned(env)
    first = env.mgr.mark_expiry_warned(rec.id)
    assert first is not None and first.expiry_warned_at == "2026-11-08T12:00:00Z"
    assert env.mgr.mark_expiry_warned(rec.id) is None
    env.mgr.stop(rec.id)
    assert env.mgr.mark_expiry_warned(rec.id) is None                  # only while running


def test_extend_pushes_the_limit_and_rearms_the_warning(env):
    rec = _spawned(env, ttl_minutes=60)                                # expires 13:00
    env.mgr.mark_expiry_warned(rec.id)
    out = env.mgr.extend(rec.id, 30)
    assert out.expires_at == "2026-11-08T13:30:00Z" and out.expiry_warned_at is None
    assert env.events.payloads("instances.changed")[-1]["reason"] == "extended"
    env.clock.advance(hours=5)                                         # limit long gone
    assert env.mgr.extend(rec.id, 10).expires_at == "2026-11-08T17:10:00Z"   # from now
    env.mgr.stop(rec.id)
    assert env.mgr.extend(rec.id, 10080).expires_at == "2026-11-15T17:10:00Z"  # stopped ok


@pytest.mark.parametrize("minutes", [0, 10081, -5, True, "30", 1.5, None])
def test_extend_rejects_bad_minutes(env, minutes):
    rec = _spawned(env)
    with pytest.raises(ApiError) as exc:
        env.mgr.extend(rec.id, minutes)
    assert exc.value.status == 400


def test_extend_refuses_destroyed_and_persistent(env):
    env.templates.save(_template(id="keep", persistent=True))
    _script_ok(env.t)
    keep = env.mgr.spawn("keep", "x1carbon")
    with pytest.raises(ApiError) as exc:
        env.mgr.extend(keep.id, 30)
    assert exc.value.status == 409 and "no time limit" in exc.value.message
    rec = _spawned(env)
    env.mgr.destroy(rec.id, save=False)
    with pytest.raises(ApiError) as exc:
        env.mgr.extend(rec.id, 30)
    assert exc.value.status == 409
    with pytest.raises(ApiError) as exc:
        env.mgr.extend("i-00000000", 30)
    assert exc.value.status == 404


def test_destroy_saves_files_first(env):
    rec = _spawned(env)
    env.t.reset_calls()
    out = env.mgr.destroy(rec.id)
    assert out.status == "destroyed"
    assert env.files.exports == [(rec.id, "before-delete")]
    assert len(_rm_calls(env.t)) == 1


@pytest.mark.parametrize("failure", [
    ApiError(501, "not_implemented", "File transfer is not available yet."),
    ApiError(413, "limit_reached", "the export is larger than 2048 MB"),
    RuntimeError("disk full"),
    OSError("permission denied"),
])
def test_destroy_save_failure_leaves_everything_untouched(env, failure):
    rec = _spawned(env)
    env.files.fail = failure
    before = env.store.get_instance(rec.id).to_dict()
    names = env.store.kv_get(sandbox_mod.VARS_KEY_PREFIX + rec.id)
    env.t.reset_calls()
    env.events.clear()
    with pytest.raises(ApiError) as exc:
        env.mgr.destroy(rec.id)
    assert exc.value.status == 409 and exc.value.code == "not_saved"
    assert exc.value.message.startswith(
        "The files in /work could not be saved to this computer, so nothing was deleted: ")
    assert exc.value.message.endswith(". Delete without saving to remove it anyway.")
    assert _rm_calls(env.t) == []                                      # container kept
    assert env.store.get_instance(rec.id).to_dict() == before          # record kept
    assert env.store.kv_get(sandbox_mod.VARS_KEY_PREFIX + rec.id) == names
    assert env.events.names() == []


def test_destroy_save_failure_message_names_the_detail(env):
    rec = _spawned(env)
    env.files.fail = ApiError(503, "host_offline", "x1carbon is offline.")
    with pytest.raises(ApiError) as exc:
        env.mgr.destroy(rec.id)
    assert exc.value.message == ("The files in /work could not be saved to this computer, so "
                                 "nothing was deleted: x1carbon is offline. Delete without "
                                 "saving to remove it anyway.")


def test_destroy_with_the_real_stub_files_service_refuses(env):
    from cloud_compute_core.files import FileManager
    env.services.set("files", FileManager(env.services))
    rec = _spawned(env)
    with pytest.raises(ApiError) as exc:
        env.mgr.destroy(rec.id)
    assert exc.value.code == "not_saved"
    assert env.store.get_instance(rec.id).status == "running"


def test_destroy_without_saving_skips_the_export(env):
    rec = _spawned(env)
    env.files.fail = RuntimeError("must not be called")
    assert env.mgr.destroy(rec.id, save=False).status == "destroyed"
    assert env.files.exports == []
    with pytest.raises(ApiError) as exc:
        env.mgr.destroy(_spawned(env).id, save="no")
    assert exc.value.status == 400


def test_destroy_passes_the_export_reason(env):
    rec = _spawned(env)
    env.mgr.destroy(rec.id, reason="auto-delete", export_reason="auto-delete")
    assert env.files.exports == [(rec.id, "auto-delete")]
    assert env.events.payloads("instances.changed")[-1]["reason"] == "auto-delete"


def test_destroy_with_the_host_gone_skips_the_save(env):
    rec = _spawned(env)
    env.files.fail = RuntimeError("must not be called")
    env.store.delete_host(env.host.id)
    assert env.mgr.destroy(rec.id).status == "destroyed"
    assert env.files.exports == []


def _orphan(iid, *, install=None, state="running", **labels):
    return {"name": f"cc-{iid}", "container_id": "c0ffee", "state": state,
            "labels": {MANAGED_LABEL: "1", LABEL_INSTANCE: iid, LABEL_TEMPLATE: "python-dev",
                       LABEL_INSTALL: install, **labels}}


def test_adopt_running_orphan_lists_it_with_a_time_limit(env):
    mine = env.mgr.install_id()
    env.t.reset_calls()
    rec = env.mgr.adopt_container("x1carbon", _orphan("i-0rphan01", install=mine))
    assert rec is not None and rec.id == "i-0rphan01" and rec.name == "cc-i-0rphan01"
    assert rec.status == "running" and rec.stop_reason == "orphan"
    assert rec.template_id == "python-dev" and rec.runtime == "podman"
    assert rec.expires_at == "2026-11-08T16:00:00Z" and rec.ttl_minutes == 240
    assert env.store.get_instance(rec.id).status == "running"
    assert env.t.calls == []                                           # nothing run, nothing rm'd
    assert env.events.payloads("instances.changed")[-1] == {
        "ids": ["i-0rphan01"], "host_id": "x1carbon", "reason": "adopted"}


def test_adopt_stopped_orphan_gets_a_limit_when_started(env):
    mine = env.mgr.install_id()
    rec = env.mgr.adopt_container(
        "x1carbon", _orphan("i-0rphan02", install=mine, state="exited",
                            **{sandbox_mod.LABEL_NAME: "results-box"}))
    assert rec.status == "stopped" and rec.name == "results-box" and rec.expires_at is None
    assert env.mgr.stopped_at(rec.id) == "2026-11-08T12:00:00Z"
    _script_ok(env.t)
    started = env.mgr.start(rec.id)
    assert started.expires_at == "2026-11-08T16:00:00Z" and started.stop_reason is None


def test_adopt_refuses_foreign_live_and_odd_names(env):
    mine = env.mgr.install_id()
    assert env.mgr.adopt_container("x1carbon", _orphan("i-f0reign1", install="other")) is None
    live = _spawned(env)
    assert env.mgr.adopt_container("x1carbon", _orphan(live.id, install=mine)) is None
    odd = _orphan("i-0dd00001", install=mine)
    odd["name"] = "something-else"
    assert env.mgr.adopt_container("x1carbon", odd) is None
    assert env.store.get_instance("i-f0reign1") is None


def test_adopt_reuses_a_destroyed_record(env):
    mine = env.mgr.install_id()
    rec = _spawned(env, name="old-name")
    env.mgr.destroy(rec.id, save=False)
    out = env.mgr.adopt_container("x1carbon", _orphan(rec.id, install=mine))
    assert out.status == "running" and out.name == "old-name" and out.created_at == rec.created_at
