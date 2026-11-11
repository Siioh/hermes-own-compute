"""Data-safety regressions: a Delete removes an environment only after a verified save.

Each test reproduces one data-loss case against the real
SandboxManager, FileManager, Reaper and approval gate, over the FakeTransport with a
scripted ``run_stream`` standing in for ``<runtime> cp cc-<id>:/work -``.
"""

from __future__ import annotations

import copy
import io
import subprocess
import sys
import tarfile
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_compute_core import agents_api, files_api, instances_api
from cloud_compute_core import approval as approval_mod
from cloud_compute_core import files as files_mod
from cloud_compute_core import transport as transport_mod
from cloud_compute_core.files import FileManager
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.models import ExportRecord, HostLimits
from cloud_compute_core.reaper import Reaper
from cloud_compute_core.runtimes import LABEL_INSTALL, LABEL_INSTANCE, MANAGED_LABEL
from cloud_compute_core.sandbox import STOPPED_AT_KEY_PREFIX, SandboxManager
from cloud_compute_core.testing import exec_result, sample_host, sample_instance, sample_template
from cloud_compute_core.transport import StreamResult

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


class Templates:
    def __init__(self, spec):
        self.spec = spec

    def get(self, template_id):
        return copy.deepcopy(replace(self.spec, id=template_id))


def _tar(entries: dict[str, bytes | None]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            if data is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Stream:
    """Scripted run_stream: ``payload`` goes to the sink, then ``rc`` is returned."""

    def __init__(self):
        self.payload = b""
        self.rc = 0
        self.during = None            # called once, mid-stream
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, sink=None, source=None, max_bytes=None, timeout=3600):
        self.calls.append(list(argv))
        if source is not None:        # an upload: drain it
            class W:
                def write(self, b):
                    return len(b)
            source(W())
            return StreamResult(rc=0, stderr="", bytes=1, duration_s=0.0)
        sent = 0
        for i in range(0, len(self.payload), 4096):
            chunk = self.payload[i:i + 4096]
            sent += len(chunk)
            sink(chunk)
            if self.during is not None:
                during, self.during = self.during, None
                during()
        return StreamResult(rc=self.rc, stderr="" if self.rc == 0 else "ssh: connection reset",
                            bytes=sent, duration_s=0.0)


@pytest.fixture
def env(services, fake_transports, fake_events, tmp_path):
    host = sample_host(limits=HostLimits(max_instances=4), runtime_pref="podman")
    services.store.upsert_host(host)
    services.set("templates", Templates(sample_template(workdir="/work", persistent=False)))
    mgr = SandboxManager(services, clock=lambda: T0)
    services.set("sandboxes", mgr)
    fm = FileManager(services, exports_dir=tmp_path / "exports")
    services.set("files", fm)
    t = fake_transports.get(host.id)
    for verb in ("stop", "start", "rm", "ps", "inspect"):
        t.script(("podman", verb), exec_result())
    stream = Stream()
    t.run_stream = stream
    rec = sample_instance(id="i-0000dead", host_id=host.id, status="running")
    services.store.upsert_instance(rec)
    return SimpleNamespace(services=services, mgr=mgr, fm=fm, t=t, stream=stream, rec=rec,
                           host=host, exports=tmp_path / "exports", store=services.store,
                           events=fake_events)


def _rm_calls(t):
    return [c for c in t.calls if c.kind == "run" and c.argv[:2] == ["podman", "rm"]]


def _start_calls(t):
    return [c for c in t.calls if c.kind == "run" and c.argv[:2] == ["podman", "start"]]


def _archives(root: Path):
    return sorted(root.rglob("*.tar.gz")) if root.exists() else []


def _notices(env):
    return [p.get("body", "") for p in env.events.payloads("notify")]


def _container(iid, install):
    return {"name": f"cc-{iid}", "container_id": "abc", "state": "running",
            "labels": {MANAGED_LABEL: "1", LABEL_INSTANCE: iid, LABEL_INSTALL: install}}


def _tool(mod, name):
    return next(t for t in mod.tools() if t.name == name)


def _ask(env, tool_name, args, mod=None):
    tool = _tool(mod, tool_name) if mod is not None else None
    return approval_mod.approval_for(tool_name, args, tool, env.services)


# ---------------------------------------------------------------- verified saves


def test_b1_empty_workdir_proceeds_with_nothing_needed_saving(env):
    env.stream.payload = _tar({"work": None})
    out = env.mgr.destroy(env.rec.id)
    assert out.status == "destroyed" and len(_rm_calls(env.t)) == 1
    assert any("Nothing in /work needed saving." in n for n in _notices(env))


@pytest.mark.parametrize("payload", [
    b"Error: container state improper\n",                    # runtime text on stdout, rc 0
    _tar({"work/big.bin": b"x" * 50_000})[:20_000],          # cut mid-file, rc 0
    _tar({"work/a.txt": b"a" * 512})[:1024],                 # cut before the end marker, rc 0
], ids=["error-text", "truncated-mid-file", "no-end-marker"])
def test_b1_unreadable_archive_is_not_saved_and_nothing_is_deleted(env, payload):
    env.stream.payload = payload
    with pytest.raises(ApiError) as exc:
        env.mgr.destroy(env.rec.id)
    assert exc.value.code == "not_saved"
    assert "The files in /work could not be saved" in exc.value.message
    assert _rm_calls(env.t) == [] and _archives(env.exports) == []
    assert env.store.get_instance(env.rec.id).status == "running"


def test_b1_saved_archive_is_fsynced_and_read_back_before_rm(env, monkeypatch):
    env.stream.payload = _tar({"work": None, "work/results/a.txt": b"hello"})
    synced: list[int] = []
    real_fsync = files_mod.os.fsync

    def fsync(fd):
        synced.append(len(_rm_calls(env.t)))
        return real_fsync(fd)
    monkeypatch.setattr(files_mod.os, "fsync", fsync)
    env.mgr.destroy(env.rec.id)
    assert synced == [0]                                     # on disk before the rm
    [arc] = _archives(env.exports)
    with tarfile.open(arc, "r:gz") as tf:
        assert [m.name for m in tf if m.isfile()] == ["work/results/a.txt"]
    assert len(_rm_calls(env.t)) == 1
    assert not any("needed saving" in n for n in _notices(env))


def test_b1_export_verified_counts_files_and_keeps_the_record_shape(env):
    env.stream.payload = _tar({"work": None, "work/a": b"1", "work/sub": None, "work/sub/b": b"2"})
    record, files = env.fm.export_verified(env.rec.id)
    assert files == 2
    assert set(record) == set(ExportRecord.__dataclass_fields__)
    assert set(env.fm.export(env.rec.id)) == set(record)


def test_b1_delete_texts_say_exactly_what_is_saved(env):
    tool = _tool(instances_api, "env_control")
    keep = approval_mod.approval_for("env_control", {"action": "destroy", "instance_id": env.rec.id},
                                     tool, env.services)["message"]
    nuke = approval_mod.approval_for("env_control", {"action": "destroy", "save": False,
                                                     "instance_id": env.rec.id},
                                     tool, env.services)["message"]
    assert "The files in /work are saved to this computer first" in keep
    assert "Files outside /work are not saved." in keep
    assert "Nothing is saved" in nuke
    for text in (keep, nuke, approval_mod.GENERIC_DESTROY_MESSAGE):
        assert "its files" not in text.lower() and "this pc" not in text.lower()


def test_b1_auto_delete_notice_names_the_workdir(env, monkeypatch):
    env.services.config.auto_delete_days = 1
    monkeypatch.setattr(env.mgr, "list_containers", lambda h: [])
    full = replace(env.rec, id="i-0000ad01", name="full", status="stopped")
    empty = replace(env.rec, id="i-0000ad02", name="empty", status="stopped")
    for rec in (full, empty):
        env.store.upsert_instance(rec)
        env.store.kv_set(STOPPED_AT_KEY_PREFIX + rec.id, "2026-11-01T00:00:00Z")
    real = env.stream.__call__

    def stream(argv, **kw):
        full_one = any(full.id in str(a) for a in argv)
        env.stream.payload = _tar({"work/a": b"a"}) if full_one else _tar({"work": None})
        return real(argv, **kw)
    env.t.run_stream = stream
    destroyed: list[str] = []
    for _ in range(2):                    # one automatic deletion per sweep
        destroyed += Reaper(env.services, clock=lambda: T0).sweep_host(env.host.id).destroyed
    assert {full.id, empty.id} <= set(destroyed)
    notes = _notices(env)
    assert any("The files in /work (1 file, " in n and " were saved to " in n
               and "Files outside /work were not saved." in n for n in notes)
    assert any(n.endswith("Nothing in /work needed saving.") for n in notes)
    assert not any("Its files" in n for n in notes)


# ---------------------------------------------------------------- approval keys


def test_b2_each_env_control_action_has_its_own_key(env):
    iid = env.rec.id
    keys = {
        "extend": _ask(env, "env_control", {"action": "extend", "minutes": 30, "instance_id": iid},
                       instances_api)["rule_key"],
        "destroy-save": _ask(env, "env_control", {"action": "destroy", "instance_id": iid},
                             instances_api)["rule_key"],
        "destroy-nosave": _ask(env, "env_control", {"action": "destroy", "save": False,
                                                    "instance_id": iid}, instances_api)["rule_key"],
    }
    for action, key in keys.items():
        assert key == f"cloud-compute:env_control:{action}:{iid}"
    assert approval_mod.rule_key("env_control", {"action": "start", "instance_id": iid}, None) \
        == f"cloud-compute:env_control:start:{iid}"


def test_b2_approving_one_action_or_instance_does_not_approve_another(env):
    other = replace(env.rec, id="i-0000beef")
    env.store.upsert_instance(other)
    # Hermes remembers "session"/"always" answers by rule_key.
    remembered = {_ask(env, "env_control", {"action": "extend", "minutes": 30,
                                            "instance_id": env.rec.id}, instances_api)["rule_key"],
                  _ask(env, "env_control", {"action": "destroy", "instance_id": env.rec.id},
                       instances_api)["rule_key"],
                  _ask(env, "env_control", {"action": "destroy", "save": False,
                                            "instance_id": other.id}, instances_api)["rule_key"]}
    nuke = _ask(env, "env_control", {"action": "destroy", "save": False,
                                     "instance_id": env.rec.id}, instances_api)
    assert "Nothing is saved" in nuke["message"]
    assert nuke["rule_key"] not in remembered


def test_b2_fail_closed_key_is_unique_per_call():
    a = approval_mod._fail_closed("env_control", {"action": "extend", "instance_id": "i-1"})
    b = approval_mod._fail_closed("env_control", {"action": "destroy", "save": False,
                                                  "instance_id": "i-1"})
    assert a["rule_key"] != b["rule_key"]


# ---------------------------------------------------------------- failed-spawn cleanup


def test_m1_reaper_leaves_an_error_record_restarted_during_the_sweep(env, monkeypatch):
    rec = replace(env.rec, id="i-0000e44r", status="error", error="spawn timed out",
                  idle_minutes=None, expires_at=None)
    env.store.upsert_instance(rec)
    mine = env.mgr.install_id()

    def list_containers(host_id):
        env.mgr.start(rec.id)                  # the user restarts it meanwhile
        return [_container(rec.id, mine)]
    monkeypatch.setattr(env.mgr, "list_containers", list_containers)
    report = Reaper(env.services, clock=lambda: T0).sweep_host(env.host.id)
    assert rec.id not in report.destroyed
    assert env.store.get_instance(rec.id).status == "running"
    assert _rm_calls(env.t) == [] and env.stream.calls == []


def test_m1_reaper_failed_spawn_delete_saves_first(env, monkeypatch):
    rec = replace(env.rec, id="i-0000e55r", status="error", idle_minutes=None, expires_at=None)
    env.store.upsert_instance(rec)
    monkeypatch.setattr(env.mgr, "list_containers",
                        lambda h: [_container(rec.id, env.mgr.install_id())])
    env.stream.payload = _tar({"work/uploaded.txt": b"keep me"})
    env.stream.rc = 255                                       # the save fails: kept
    report = Reaper(env.services, clock=lambda: T0).sweep_host(env.host.id)
    assert rec.id in report.kept and _rm_calls(env.t) == []
    assert env.store.get_instance(rec.id).status == "error"
    env.stream.rc = 0                                         # the save works: deleted
    report = Reaper(env.services, clock=lambda: T0).sweep_host(env.host.id)
    assert rec.id in report.destroyed and len(_rm_calls(env.t)) == 1
    [arc] = _archives(env.exports)
    with tarfile.open(arc, "r:gz") as tf:
        assert [m.name for m in tf] == ["work/uploaded.txt"]


def test_m1_status_change_during_the_save_blocks_the_removal(env):
    rec = replace(env.rec, id="i-0000e66r", status="error")
    env.store.upsert_instance(rec)
    env.stream.payload = _tar({"work": None})

    def flip():
        fresh = env.store.get_instance(rec.id)
        fresh.status = "running"
        env.store.upsert_instance(fresh)
    env.stream.during = flip
    with pytest.raises(ApiError) as exc:
        env.mgr.destroy(rec.id, save=True, only_if_status=("error",))
    assert exc.value.code == "conflict" and _rm_calls(env.t) == []


# ---------------------------------------------------------------- cloud_agent_stop


def test_m2_cloud_agent_stop_asks_and_names_the_branch(env):
    run = {"id": "a-0000abcd", "instance_id": env.rec.id, "host_id": env.host.id,
           "collected_ref": "refs/heads/cloud/a-0000abcd/fix-bug", "status": "collected"}
    env.store.put_record("agent_runs", run["id"], run, host_id=env.host.id, status="collected")
    keep = _ask(env, "cloud_agent_stop", {"run_id": run["id"]}, agents_api)
    drop = _ask(env, "cloud_agent_stop", {"run_id": run["id"], "keep_branch": False}, agents_api)
    assert keep["message"].startswith("Stops cloud agent run a-0000abcd. Its sandbox is deleted after the "
                                      "files in /work are saved to this computer.")
    assert keep["message"].endswith("Commits the agent made that were not fetched yet are fetched to this "
                                    "computer first. If they cannot be fetched, nothing is deleted.")
    assert drop["message"].endswith(
        " The collected branch cloud/a-0000abcd/fix-bug on this computer is also deleted.")
    assert keep["rule_key"] == "cloud-compute:cloud_agent_stop:stop:a-0000abcd"
    assert drop["rule_key"] == "cloud-compute:cloud_agent_stop:stop-delete-branch:a-0000abcd"
    other = _ask(env, "cloud_agent_stop", {"run_id": "a-0000ffff", "keep_branch": False},
                 agents_api)
    assert other["rule_key"] != drop["rule_key"]


def test_m2_cloud_agent_stop_is_gated_by_the_hook(env):
    hook = approval_mod.make_pre_tool_call(lambda: {}, lambda: env.services)
    out = hook(tool_name="cloud_agent_stop", args={"run_id": "a-0000abcd", "keep_branch": False})
    assert out is not None and out["action"] == "approve"


# ---------------------------------------------------------------- saved copies


def test_m3_delete_export_key_is_per_copy_and_text_names_the_file(env):
    env.stream.payload = _tar({"work/a": b"a"})
    a = env.fm.export(env.rec.id)
    b = env.fm.export(env.rec.id)
    ka = _ask(env, "env_files", {"action": "delete_export", "export_id": a["id"]}, files_api)
    kb = _ask(env, "env_files", {"action": "delete_export", "export_id": b["id"]}, files_api)
    assert ka["rule_key"] == f"cloud-compute:env_files:delete-export:{a['id']}"
    assert ka["rule_key"] != kb["rule_key"]
    assert Path(a["local_path"]).name in ka["message"]
    assert "cannot be recovered" in ka["message"]


# ---------------------------------------------------------------- smaller cases


def test_minor_remote_only_still_asks_for_local_deletions(env):
    local = sample_host(id="local-pc", kind="local", address="localhost")
    env.store.upsert_host(local)
    rec = replace(env.rec, id="i-00001oca", host_id="local-pc")
    env.store.upsert_instance(rec)
    env.services.config.approval = "remote-only"
    nuke = _ask(env, "env_control", {"action": "destroy", "save": False, "instance_id": rec.id},
                instances_api)
    assert nuke is not None and "Nothing is saved" in nuke["message"]
    assert _ask(env, "env_files", {"action": "delete_export", "export_id": "x-00000001"},
                files_api) is not None
    # the user's remote-only choice still applies to everything else on this computer
    assert _ask(env, "env_control", {"action": "destroy", "instance_id": rec.id},
                instances_api) is None


def test_minor_missing_runtime_binary_is_not_a_gone_container(env):
    env.t.script(("podman", "rm"), exec_result(rc=127, stderr="sh: podman: not found"))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    with pytest.raises(ApiError):
        env.mgr.destroy(env.rec.id, save=False)
    assert env.store.get_instance(env.rec.id).status == "running"


def test_minor_upload_text_says_same_names_are_replaced(env):
    msg = _ask(env, "env_files", {"action": "put", "instance_id": env.rec.id,
                                  "local_path": "C:/data/x.csv", "dest": "/work"}, files_api)
    assert msg["message"].endswith("Files with the same names are replaced.")
    assert msg["rule_key"].startswith(f"cloud-compute:env_files:put:{env.rec.id}:")


def test_minor_stream_read_error_raises(monkeypatch):
    real_popen = subprocess.Popen

    class Broken:
        def __init__(self, raw):
            self.raw, self.n = raw, 0

        def read1(self, n):
            self.n += 1
            if self.n > 1:
                raise OSError(5, "Input/output error")
            return self.raw.read(1)

        def close(self):
            self.raw.close()

    def popen(*a, **kw):
        proc = real_popen(*a, **kw)
        proc.stdout = Broken(proc.stdout)
        return proc
    monkeypatch.setattr(transport_mod.subprocess, "Popen", popen)
    got: list[bytes] = []
    with pytest.raises(OSError):
        transport_mod._run_stream_process(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 100)"],
            sink=got.append, timeout=30)


def _blocked_during_export(env, call):
    """Run ``call`` on a thread while the Delete's export is streaming; return its outcome."""
    outcome: dict = {}
    started = threading.Event()

    def worker():
        started.set()
        try:
            outcome["result"] = call()
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc
    thread = threading.Thread(target=worker, daemon=True)

    def during():
        thread.start()
        started.wait(2)
        thread.join(0.3)
        outcome["finished_during_export"] = not thread.is_alive()
    env.stream.payload = _tar({"work/a": b"a"})
    env.stream.during = during
    env.mgr.destroy(env.rec.id)
    thread.join(5)
    return outcome


def test_minor_start_cannot_land_between_save_and_removal(env):
    out = _blocked_during_export(env, lambda: env.mgr.start(env.rec.id))
    assert out["finished_during_export"] is False
    assert isinstance(out.get("error"), ApiError)            # it now sees a deleted record
    assert _start_calls(env.t) == [] and len(_rm_calls(env.t)) == 1


def test_minor_upload_cannot_land_between_save_and_removal(env, tmp_path_factory):
    src = tmp_path_factory.mktemp("outside") / "upload.txt"
    src.write_text("new work")
    out = _blocked_during_export(env, lambda: env.fm.put(env.rec.id, str(src), "/work"))
    assert out["finished_during_export"] is False
    assert isinstance(out.get("error"), ApiError)
    assert len(env.stream.calls) == 1                        # only the export streamed



# ---------------------------------------------------------------- saves and unpacking


def _hostile(*members) -> bytes:
    """A tar stream: the saved folder 'work', a good file, then the given members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        top = tarfile.TarInfo("work")
        top.type, top.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(top)
        good = tarfile.TarInfo("work/good.txt")
        good.size = 4
        tf.addfile(good, io.BytesIO(b"good"))
        for name, kind, link in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = link
            if kind == tarfile.REGTYPE:
                info.size = 3
                tf.addfile(info, io.BytesIO(b"bad"))
            else:
                tf.addfile(info)
    return buf.getvalue()


HOSTILE = {
    "absolute-name": [("/etc/evil", tarfile.REGTYPE, "")],
    "dotdot-name": [("work/../../evil", tarfile.REGTYPE, "")],
    "symlink-escape": [("work/link", tarfile.SYMTYPE, "../../outside")],
    "symlink-absolute": [("work/link", tarfile.SYMTYPE, "/etc/passwd")],
    "hardlink-absolute": [("work/hard", tarfile.LNKTYPE, "/etc/passwd")],
    "hardlink-escape": [("work/hard", tarfile.LNKTYPE, "work/../../outside")],
    "hardlink-to-unknown": [("work/hard", tarfile.LNKTYPE, "work/not-in-archive")],
    "device-node": [("work/dev0", tarfile.CHRTYPE, "")],
    "fifo": [("work/pipe", tarfile.FIFOTYPE, "")],
    "overwrite": [("work/good.txt", tarfile.REGTYPE, "")],
}


REFUSAL = {"absolute-name": "absolute name", "dotdot-name": "climbs out with '..'",
           "symlink-escape": "points outside", "symlink-absolute": "points outside",
           "hardlink-absolute": "hard link", "hardlink-escape": "hard link",
           "hardlink-to-unknown": "hard link", "device-node": "special file",
           "fifo": "special file", "overwrite": "would overwrite"}


@pytest.mark.parametrize("name,members", list(HOSTILE.items()), ids=list(HOSTILE))
def test_hostile_archives_are_never_unpacked_and_the_archive_is_kept(env, tmp_path_factory,
                                                                     name, members):
    tmp_path = tmp_path_factory.mktemp("local")      # outside the plugin's data folder
    proj = tmp_path / "proj"
    proj.mkdir()
    env.stream.payload = _hostile(*members)
    with pytest.raises(ApiError) as exc:
        env.fm.fetch(env.rec.id, dest_dir=str(proj), extract=True)
    msg = exc.value.message
    assert exc.value.code == "invalid" and "it was not unpacked" in msg
    assert "the archive was kept" in msg
    [arc] = sorted(proj.glob("*.tar.gz"))                    # the archive stays
    assert [p for p in proj.iterdir() if p.is_dir()] == []   # the partial folder is gone
    assert not (tmp_path / "outside").exists() and not (tmp_path / "evil").exists()
    assert REFUSAL[name] in msg
    [row] = env.fm.list_exports()
    assert row["local_path"] == str(arc) and row["extracted_to"] is None and row["files"] >= 1


def test_safe_unpack_caps_the_total_size(tmp_path):
    arc = tmp_path / "big.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        for i in range(3):
            info = tarfile.TarInfo(f"f{i}")
            info.size = 600
            tf.addfile(info, io.BytesIO(b"x" * 600))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(files_mod._Refused) as exc:
        files_mod._safe_unpack(arc, out, 1000)
    assert "larger than the export limit" in str(exc.value)
    out2 = tmp_path / "out2"
    out2.mkdir()
    files_mod._safe_unpack(arc, out2, 1800)
    assert sorted(p.name for p in out2.iterdir()) == ["f0", "f1", "f2"]


def test_safe_unpack_copies_hard_links_inside_the_archive(tmp_path):
    arc = tmp_path / "links.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        info = tarfile.TarInfo("work")
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
        info = tarfile.TarInfo("work/a")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"hi"))
        info = tarfile.TarInfo("work/b")
        info.type, info.linkname = tarfile.LNKTYPE, "work/a"
        tf.addfile(info)
    out = tmp_path / "out"
    out.mkdir()
    files_mod._safe_unpack(arc, out, 1000)
    assert (out / "b").read_bytes() == b"hi" and (out / "a").read_bytes() == b"hi"


@pytest.mark.skipif(not hasattr(__import__("os"), "symlink") or sys.platform == "win32",
                    reason="symbolic links need POSIX here")
def test_safe_unpack_refuses_a_link_through_a_linked_folder(tmp_path):
    arc = tmp_path / "trick.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        for name, link in (("s", "."), ("s/l", "..")):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, link
            tf.addfile(info)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(files_mod._Refused):
        files_mod._safe_unpack(arc, out, 1000)


@pytest.mark.parametrize("payload", [
    b"Error: container state improper\n",
    _tar({"work/big.bin": b"x" * 50_000})[:20_000],
], ids=["error-text", "truncated"])
def test_save_before_delete_raises_and_keeps_nothing_on_a_bad_archive(env, payload):
    env.stream.payload = payload
    with pytest.raises(ApiError):
        env.fm.save_before_delete(env.rec.id)
    assert _archives(env.exports) == []


def test_save_before_delete_of_an_empty_workdir_keeps_no_archive(env):
    env.stream.payload = _tar({"work": None, "work/sub": None})
    out = env.fm.save_before_delete(env.rec.id)
    assert out["saved"] is False and out["export"] is None
    assert out["message"] == "Nothing in /work needed saving."
    assert _archives(env.exports) == []


def test_manual_save_of_an_empty_workdir_is_nothing_to_save(env):
    env.stream.payload = _tar({"work": None})
    with pytest.raises(ApiError) as exc:
        env.fm.export(env.rec.id)
    assert (exc.value.status, exc.value.code) == (409, "nothing_to_save")
    assert _archives(env.exports) == []
    # the Delete fallback (export_verified with a before-delete reason) still works
    record, files = env.fm.export_verified(env.rec.id, reason="before-delete")
    assert files == 0 and record["files"] == 0



def test_end_to_end_destroy_reports_the_real_saved_copy(env):
    """Real SandboxManager + real FileManager: the delete's report names a copy that exists."""
    env.stream.payload = _tar({"work": None, "work/a.txt": b"hello", "work/sub": None,
                               "work/sub/b.bin": b"x" * 2000})
    report: dict = {}
    out = env.mgr.destroy(env.rec.id, report=report)
    assert out.status == "destroyed" and len(_rm_calls(env.t)) == 1
    saved = report["saved"]
    [arc] = _archives(env.exports)
    assert saved["path"] == str(arc) and Path(saved["path"]).is_file()
    assert saved["files"] == 2 and saved["bytes"] == 2005
    stored = env.fm.get_export(saved["export_id"])
    assert stored["local_path"] == saved["path"] and stored["files"] == 2
    assert stored["reason"] == "before-delete"
    with tarfile.open(arc, "r:gz") as tf:
        assert sorted(m.name for m in tf if m.isfile()) == ["work/a.txt", "work/sub/b.bin"]
    assert report["message"] == (f"Deleted {env.rec.name}. The files in /work (2 files, "
                                 f"2.0 KB) were saved to {arc}.")


def test_end_to_end_destroy_of_an_empty_workdir_leaves_no_archive(env):
    env.stream.payload = _tar({"work": None, "work/emptydir": None})
    report: dict = {}
    out = env.mgr.destroy(env.rec.id, report=report)
    assert out.status == "destroyed" and len(_rm_calls(env.t)) == 1
    assert report["saved"] is None
    assert report["message"] == f"Deleted {env.rec.name}. Nothing in /work needed saving."
    assert _archives(env.exports) == [] and env.fm.list_exports() == []
