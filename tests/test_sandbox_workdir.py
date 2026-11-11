"""A working folder the image lacks is created and owned by the container's own user.

Podman refuses ``-w`` for a folder the image does not have and leaves the container created;
the folder is copied in (which hands it to the container's primary user) and the container is
started. If that is impossible the folder is made as root and handed to the user, or the
spawn fails with a sentence that says what to change.
"""

from __future__ import annotations

import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from cloud_compute_core import sandbox as sandbox_mod
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.models import HostLimits, TemplateSpec
from cloud_compute_core.runtimes import runtime_for
from cloud_compute_core.sandbox import SandboxManager
from cloud_compute_core.templates import TemplateStore
from cloud_compute_core.testing import exec_result, sample_host

MISSING = ('Error: workdir "/work" does not exist on container '
           '0c77b14f7dc79d230782453f728454d5a49b103cae3bd8b2a829c0fec7408ae7')


def _inspect():
    return json.dumps([{"Id": "f0" * 32, "State": {"Status": "running"},
                        "NetworkSettings": {"Ports": {}}}])


def _setup(services, fake_transports, monkeypatch, runtime):
    monkeypatch.setattr(sandbox_mod, "PORT_POLL_S", 0.0)
    monkeypatch.setattr(sandbox_mod, "PORT_WAIT_S", 0.05)
    host = sample_host(name="box", runtime_pref=runtime, limits=HostLimits(max_instances=3))
    services.store.upsert_host(host)
    services.set("templates", TemplateStore(services.store))
    mgr = SandboxManager(services)
    services.set("sandboxes", mgr)
    t = fake_transports.get(host.id)
    t.script(lambda a, s: a[:2] == ["sh", "-c"], exec_result(stdout="/h/.cache/x.env\n"))
    t.script((runtime, "inspect"), exec_result(stdout=_inspect()))
    for verb in ("stop", "rm", "logs", "stats", "ps", "port"):
        t.script((runtime, verb), exec_result())
    return SimpleNamespace(services=services, host=host, t=t, mgr=mgr, rt=runtime_for(runtime))


@pytest.fixture
def pod(services, fake_transports, monkeypatch):
    return _setup(services, fake_transports, monkeypatch, "podman")


@pytest.fixture
def dock(services, fake_transports, monkeypatch):
    return _setup(services, fake_transports, monkeypatch, "docker")


def _template(env, workdir="/work", command=None, tid="data"):
    env.services.templates.save(TemplateSpec(id=tid, name="Data", description="",
                                             workdir=workdir, command=command or []))
    return tid


def _is(*prefix):
    return lambda a, s=None: a[:len(prefix)] == list(prefix)


def _members(stdin):
    with tarfile.open(fileobj=io.BytesIO(stdin)) as tar:
        return [(m.name, m.isdir(), m.mode) for m in tar.getmembers()]


def _missing_run_then_ok(env, runs=("podman", "run")):
    """First ``run`` (with -w) fails like Podman; a retry without -w succeeds."""
    state = {"n": 0}

    def first_fails(argv, stdin):
        state["n"] += 1
        if "-w" in argv:
            return exec_result(rc=126, stderr=MISSING)
        return exec_result(stdout="3f2a9c1d0b7e\n")

    env.t.script(lambda a, s: a[:2] == list(runs), first_fails)


def test_missing_folder_is_copied_into_the_created_container_and_started(pod):
    _missing_run_then_ok(pod)
    pod.t.script(_is("podman", "cp"), exec_result())
    pod.t.script(_is("podman", "start"), exec_result(stdout="cc-x\n"))
    rec = pod.mgr.spawn(_template(pod), pod.host.id)
    assert rec.status == "running"
    argvs = pod.t.argvs()
    assert len([a for a in argvs if a[:2] == ["podman", "run"]]) == 1      # not run twice
    cp = [c for c in pod.t.calls if c.kind == "run" and c.argv[:2] == ["podman", "cp"]]
    assert len(cp) == 1 and cp[0].argv[2] == "-" and cp[0].argv[3].endswith(":/")
    assert _members(cp[0].stdin) == [("work", True, 0o755)]
    order = [a[1] for a in argvs if a[0] == "podman" and a[1] in ("run", "cp", "start")]
    assert order == ["run", "cp", "start"]                                  # folder before start
    assert not [a for a in argvs if a[:2] == ["podman", "rm"]]              # container kept
    assert not [a for a in argvs if "mkdir" in a]
    assert rec.container_id == "0c77b14f7dc79d230782453f728454d5a49b103cae3bd8b2a829c0fec7408ae7"
    assert pod.mgr.exec_workdir(rec.id) == "/work"


def test_command_that_needs_the_folder_starts_because_the_folder_exists_first(pod):
    _missing_run_then_ok(pod)
    pod.t.script(_is("podman", "cp"), exec_result())
    pod.t.script(_is("podman", "start"), exec_result(stdout="cc-x\n"))
    tid = _template(pod, command=["sh", "-c", "cd /work && sleep 3600"])
    pod.mgr.spawn(tid, pod.host.id)
    run = [a for a in pod.t.argvs() if a[:2] == ["podman", "run"]][0]
    assert run[-3:] == ["sh", "-c", "cd /work && sleep 3600"]
    assert [a[1] for a in pod.t.argvs() if a[:2] in (["podman", "cp"], ["podman", "start"])] \
        == ["cp", "start"]


def test_only_the_missing_folders_are_sent_so_existing_ones_keep_their_owner(pod):
    pod.t.script(lambda a, s: a[:2] == ["podman", "run"],
                 exec_result(rc=126, stderr=MISSING.replace("/work", "/home/user/proj")))
    seen = []

    def cp(argv, stdin):
        seen.append((argv[3].split(":", 1)[1], _members(stdin)))
        if len(seen) == 1:        # /home/user/ does not exist either
            return exec_result(rc=125, stderr="Error: could not copy: no such file or directory")
        return exec_result()

    pod.t.script(_is("podman", "cp"), cp)
    pod.t.script(_is("podman", "start"), exec_result(stdout="cc-x\n"))
    pod.mgr.spawn(_template(pod, workdir="/home/user/proj"), pod.host.id)
    assert seen[0] == ("/home/user", [("proj", True, 0o755)])
    assert seen[1] == ("/home", [("user", True, 0o755), ("user/proj", True, 0o755)])
    assert len(seen) == 2          # "/home" itself is never in an archive


def test_when_the_created_container_is_gone_the_folder_is_made_by_root_and_handed_over(pod):
    _missing_run_then_ok(pod)
    pod.t.script(_is("podman", "cp"), exec_result(rc=125, stderr="Error: no such container x"))
    pod.t.script(lambda a, s: a[:2] == ["podman", "exec"] and a[-2:] == ["id", "-u"],
                 exec_result(stdout="1000\n"))
    pod.t.script(lambda a, s: a[:2] == ["podman", "exec"] and a[-2:] == ["id", "-g"],
                 exec_result(stdout="1001\n"))
    pod.t.script(lambda a, s: a[:2] == ["podman", "exec"], exec_result())
    rec = pod.mgr.spawn(_template(pod), pod.host.id)
    assert rec.status == "running"
    argvs = pod.t.argvs()
    cname = "cc-" + rec.id
    assert ["podman", "exec", "--user", "0", cname, "mkdir", "-p", "--", "/work"] in argvs
    assert ["podman", "exec", "--user", "0", cname, "chown", "1000:1001", "--", "/work"] in argvs
    runs = [a for a in argvs if a[:2] == ["podman", "run"]]
    assert len(runs) == 2 and "-w" in runs[0] and "-w" not in runs[1]


def test_unreadable_user_gets_a_writable_folder_not_a_root_only_one(pod):
    _missing_run_then_ok(pod)
    pod.t.script(_is("podman", "cp"), exec_result(rc=125, stderr="no such container x"))
    pod.t.script(lambda a, s: a[:2] == ["podman", "exec"] and "id" in a,
                 exec_result(rc=127, stderr="executable file `id` not found"))
    pod.t.script(lambda a, s: a[:2] == ["podman", "exec"], exec_result())
    rec = pod.mgr.spawn(_template(pod), pod.host.id)
    assert ["podman", "exec", "--user", "0", "cc-" + rec.id, "chmod", "0777", "--", "/work"] \
        in pod.t.argvs()


def test_impossible_folder_fails_with_a_sentence_that_names_the_template_setting(pod):
    _missing_run_then_ok(pod)
    pod.t.script(_is("podman", "cp"), exec_result(rc=125, stderr="no such container x"))
    pod.t.script(lambda a, s: "mkdir" in a, exec_result(rc=1, stderr="read-only file system"))
    pod.t.script(lambda a, s: a[:2] == ["podman", "exec"], exec_result(stdout="1000\n"))
    with pytest.raises(ApiError) as ei:
        pod.mgr.spawn(_template(pod), pod.host.id)
    msg = ei.value.message
    assert msg.startswith("The working folder /work does not exist in the image and could not")
    assert "Set the template's Working folder" in msg and "read-only file system" in msg
    assert [r for r in pod.services.store.list_instances() if r.status == "error"]


def test_a_container_that_already_stopped_gets_a_sentence_about_its_command(pod):
    _missing_run_then_ok(pod)
    pod.t.script(_is("podman", "cp"), exec_result(rc=125, stderr="no such container x"))
    pod.t.script(lambda a, s: a[:2] == ["podman", "exec"], exec_result(
        rc=125, stderr="Error: can only create exec sessions on running containers: "
                       "container state improper"))
    with pytest.raises(ApiError) as ei:
        pod.mgr.spawn(_template(pod, command=["sh", "-c", "cd /work && true"]), pod.host.id)
    assert "stopped before the working folder /work could be created" in ei.value.message
    assert "can only create exec sessions" not in ei.value.message


def test_a_failing_start_after_the_copy_is_reported_as_a_run_failure(pod):
    _missing_run_then_ok(pod)
    pod.t.script(_is("podman", "cp"), exec_result())
    pod.t.script(_is("podman", "start"), exec_result(rc=125, stderr="Error: crun: bad thing"))
    with pytest.raises(ApiError) as ei:
        pod.mgr.spawn(_template(pod), pod.host.id)
    assert "podman run failed" in ei.value.message and "bad thing" in ei.value.message


def test_docker_hands_a_root_owned_folder_to_the_container_user(dock):
    dock.t.script(_is("docker", "run"), exec_result(stdout="3f2a9c1d0b7e\n"))
    dock.t.script(lambda a, s: a[-3:] == ["test", "-w", "/work"], exec_result(rc=1))
    dock.t.script(lambda a, s: a[-2:] == ["id", "-u"], exec_result(stdout="1000\n"))
    dock.t.script(lambda a, s: a[-2:] == ["id", "-g"], exec_result(stdout="1000\n"))
    dock.t.script(_is("docker", "exec"), exec_result())
    rec = dock.mgr.spawn(_template(dock), dock.host.id)
    assert rec.status == "running"
    runs = [a for a in dock.t.argvs() if a[:2] == ["docker", "run"]]
    assert len(runs) == 1 and "-w" in runs[0]                 # Docker still creates it itself
    assert ["docker", "exec", "--user", "0", "cc-" + rec.id, "chown", "1000:1000", "--", "/work"] \
        in dock.t.argvs()


def test_docker_leaves_a_writable_folder_alone_and_never_fails_the_spawn(dock):
    dock.t.script(_is("docker", "run"), exec_result(stdout="3f2a9c1d0b7e\n"))
    dock.t.script(_is("docker", "exec"), exec_result())        # test -w succeeds
    rec = dock.mgr.spawn(_template(dock), dock.host.id)
    assert not [a for a in dock.t.argvs() if "chown" in a]
    dock.t.script(_is("docker", "exec"), exec_result(rc=1, stderr="boom"))
    dock.t.reset_calls()
    assert dock.mgr.spawn(_template(dock), dock.host.id).status == "running"
    assert rec.status == "running"


def test_exec_argv_user_is_optional_and_placed_before_the_container():
    rt = runtime_for("podman")
    assert rt.exec_argv("c1", ["id"]) == ["podman", "exec", "c1", "id"]
    assert rt.exec_argv("c1", ["id"], user="0", workdir="/w") == \
        ["podman", "exec", "--user", "0", "-w", "/w", "c1", "id"]
    with pytest.raises(ValueError):
        rt.exec_argv("c1", ["id"], user="--privileged")


def test_podman_checks_the_folder_after_the_copy_and_fixes_a_root_owned_one(pod):
    _missing_run_then_ok(pod)
    pod.t.script(_is("podman", "cp"), exec_result())
    pod.t.script(_is("podman", "start"), exec_result(stdout="cc-x\n"))
    pod.t.script(lambda a, s: a[-3:] == ["test", "-w", "/work"], exec_result(rc=1))
    pod.t.script(lambda a, s: a[-2:] == ["id", "-u"], exec_result(stdout="1000\n"))
    pod.t.script(lambda a, s: a[-2:] == ["id", "-g"], exec_result(stdout="1000\n"))
    pod.t.script(_is("podman", "exec"), exec_result())
    rec = pod.mgr.spawn(_template(pod), pod.host.id)
    assert ["podman", "exec", "--user", "0", "cc-" + rec.id, "chown", "1000:1000", "--", "/work"] \
        in pod.t.argvs()


def test_the_recorded_folder_is_the_folder_that_was_made_on_every_route(pod):
    """Saves and deletes trust the ``workdir:<id>`` record: it must name the real folder."""
    _missing_run_then_ok(pod)
    pod.t.script(_is("podman", "cp"), exec_result())
    pod.t.script(_is("podman", "start"), exec_result(stdout="cc-x\n"))
    tid = _template(pod, workdir="/srv/data/proj")
    rec = pod.mgr.spawn(tid, pod.host.id)
    assert pod.services.store.kv_get(sandbox_mod.WORKDIR_KEY_PREFIX + rec.id) == "/srv/data/proj"
    assert pod.mgr.exec_workdir(rec.id) == "/srv/data/proj"
    copied = [c for c in pod.t.calls if c.kind == "run" and c.argv[:2] == ["podman", "cp"]]
    assert copied and _members(copied[-1].stdin)[-1][0].endswith("proj")


def test_the_recorded_folder_is_unchanged_on_the_root_fallback_route(pod):
    _missing_run_then_ok(pod)
    pod.t.script(_is("podman", "cp"), exec_result(rc=125, stderr="no such container x"))
    pod.t.script(lambda a, s: a[:2] == ["podman", "exec"], exec_result(stdout="1000\n"))
    rec = pod.mgr.spawn(_template(pod, workdir="/srv/data/proj"), pod.host.id)
    assert ["podman", "exec", "--user", "0", "cc-" + rec.id, "mkdir", "-p", "--", "/srv/data/proj"] \
        in pod.t.argvs()
    assert pod.services.store.kv_get(sandbox_mod.WORKDIR_KEY_PREFIX + rec.id) == "/srv/data/proj"
