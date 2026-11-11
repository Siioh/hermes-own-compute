"""A Delete saves the folder the environment was created with.

The template can be edited or deleted after the environment starts. Everything that decides
"the work folder" (the save before a delete, the delete card, the automatic delete, the size
and listing defaults) must use the folder recorded at spawn, the same value commands use.
"""

from __future__ import annotations

import copy
import io
import tarfile
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cloud_compute_core import approval as approval_mod
from cloud_compute_core import instances_api
from cloud_compute_core.files import FileManager
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.models import HostLimits
from cloud_compute_core.reaper import Reaper
from cloud_compute_core.sandbox import STOPPED_AT_KEY_PREFIX, WORKDIR_KEY_PREFIX, SandboxManager
from cloud_compute_core.testing import exec_result, sample_host, sample_instance, sample_template
from cloud_compute_core.transport import StreamResult

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


class Templates:
    """``workdir`` is what the template says NOW; ``gone`` makes every lookup fail."""

    def __init__(self, workdir="/work"):
        self.workdir = workdir
        self.gone = False

    def get(self, template_id):
        if self.gone:
            raise ApiError(404, "not_found", f"template {template_id!r} not found")
        return copy.deepcopy(replace(sample_template(workdir=self.workdir, persistent=False),
                                     id=template_id))


def _tar(entries):
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
    def __init__(self):
        self.payload = b""
        self.calls = []

    def __call__(self, argv, *, sink=None, source=None, max_bytes=None, timeout=3600):
        self.calls.append(list(argv))
        if source is not None:
            class W:
                def write(self, b):
                    return len(b)
            source(W())
            return StreamResult(rc=0, stderr="", bytes=1, duration_s=0.0)
        for i in range(0, len(self.payload), 4096):
            sink(self.payload[i:i + 4096])
        return StreamResult(rc=0, stderr="", bytes=len(self.payload), duration_s=0.0)


@pytest.fixture
def env(services, fake_transports, fake_events, tmp_path):
    host = sample_host(limits=HostLimits(max_instances=4), runtime_pref="podman")
    services.store.upsert_host(host)
    templates = Templates("/srv/project")            # edited after the environment was created
    services.set("templates", templates)
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
    services.store.kv_set(WORKDIR_KEY_PREFIX + rec.id, "/app")        # recorded at spawn
    return SimpleNamespace(services=services, mgr=mgr, fm=fm, t=t, stream=stream, rec=rec,
                           host=host, store=services.store, templates=templates,
                           events=fake_events)


def _rm_calls(t):
    return [c for c in t.calls if c.kind == "run" and c.argv[:2] == ["podman", "rm"]]


def _sources(stream):
    return [a for call in stream.calls for a in call if ":/" in a and a.startswith("cc-")]


def test_delete_saves_the_folder_recorded_at_spawn_not_the_templates_current_one(env):
    env.stream.payload = _tar({"app": None, "app/a.txt": b"hello"})
    out = env.mgr.destroy(env.rec.id, report=(report := {}))
    assert out.status == "destroyed" and len(_rm_calls(env.t)) == 1
    assert _sources(env.stream) == ["cc-i-0000dead:/app"]
    assert report["workdir"] == "/app" and "/app" in report["message"]
    assert "/srv/project" not in report["message"]


def test_an_old_record_without_a_recorded_folder_falls_back_to_the_template(env):
    env.store.kv_delete(WORKDIR_KEY_PREFIX + env.rec.id)
    env.stream.payload = _tar({"srv": None, "srv/project": None, "srv/project/a": b"1"})
    env.mgr.destroy(env.rec.id)
    assert _sources(env.stream) == ["cc-i-0000dead:/srv/project"]


def test_no_recorded_folder_and_no_template_refuses_and_removes_nothing(env):
    env.store.kv_delete(WORKDIR_KEY_PREFIX + env.rec.id)
    env.templates.gone = True
    with pytest.raises(ApiError) as exc:
        env.mgr.destroy(env.rec.id)
    assert exc.value.code == "not_saved"
    assert "not recorded" in exc.value.message and "Nothing was deleted" in exc.value.message
    assert "env_files fetch" in exc.value.message
    assert _rm_calls(env.t) == [] and env.stream.calls == []
    assert env.store.get_instance(env.rec.id).status == "running"


def test_the_file_service_refuses_the_same_way(env):
    env.store.kv_delete(WORKDIR_KEY_PREFIX + env.rec.id)
    env.templates.gone = True
    with pytest.raises(ApiError) as exc:
        env.fm.save_before_delete(env.rec.id)
    assert exc.value.code == "not_saved" and env.stream.calls == []


def test_delete_without_saving_still_works_when_the_folder_is_unknown(env):
    env.store.kv_delete(WORKDIR_KEY_PREFIX + env.rec.id)
    env.templates.gone = True
    out = env.mgr.destroy(env.rec.id, save=False)
    assert out.status == "destroyed" and len(_rm_calls(env.t)) == 1


def test_the_delete_card_names_the_recorded_folder(env):
    tool = next(t for t in instances_api.tools() if t.name == "env_control")
    ask = approval_mod.approval_for("env_control", {"action": "destroy",
                                                    "instance_id": env.rec.id}, tool,
                                    env.services)
    assert "The files in /app are saved to this computer first" in ask["message"]
    assert "/srv/project" not in ask["message"]
    nosave = approval_mod.approval_for("env_control", {"action": "destroy", "save": False,
                                                       "instance_id": env.rec.id}, tool,
                                       env.services)
    assert "not even the files in /app" in nosave["message"]


def test_the_delete_card_does_not_guess_work_when_nothing_is_known(env):
    env.store.kv_delete(WORKDIR_KEY_PREFIX + env.rec.id)
    env.templates.gone = True
    tool = next(t for t in instances_api.tools() if t.name == "env_control")
    ask = approval_mod.approval_for("env_control", {"action": "destroy",
                                                    "instance_id": env.rec.id}, tool,
                                    env.services)
    assert "/work" not in ask["message"] and "not recorded" in ask["message"]


def test_listing_and_size_default_to_the_recorded_folder(env):
    assert env.fm._workdir(env.rec) == "/app"
    env.store.kv_delete(WORKDIR_KEY_PREFIX + env.rec.id)
    assert env.fm._workdir(env.rec) == "/srv/project"


def test_automatic_delete_saves_the_recorded_folder_and_keeps_an_unknown_one(env, monkeypatch):
    env.services.config.auto_delete_days = 1
    monkeypatch.setattr(env.mgr, "list_containers", lambda h: [])
    known = replace(env.rec, id="i-0000ad01", name="known", status="stopped")
    unknown = replace(env.rec, id="i-0000ad02", name="unknown", status="stopped")
    for rec in (known, unknown):
        env.store.upsert_instance(rec)
        env.store.kv_set(STOPPED_AT_KEY_PREFIX + rec.id, "2026-11-01T00:00:00Z")
    env.store.kv_set(WORKDIR_KEY_PREFIX + known.id, "/app")
    env.store.kv_delete(WORKDIR_KEY_PREFIX + unknown.id)
    env.stream.payload = _tar({"app": None, "app/a.txt": b"x"})
    real_get = env.templates.get

    def get(template_id):                    # the template is gone for the second one only
        raise ApiError(404, "not_found", "gone")
    destroyed, kept = [], []
    for _ in range(2):
        report = Reaper(env.services, clock=lambda: T0).sweep_host(env.host.id)
        destroyed += report.destroyed
        kept += report.kept
        env.templates.get = get
    env.templates.get = real_get
    assert known.id in destroyed and "cc-i-0000ad01:/app" in _sources(env.stream)
    assert "cc-i-0000ad01:/srv/project" not in _sources(env.stream)
    assert unknown.id not in destroyed
    assert env.store.get_instance(unknown.id).status == "stopped"
    notes = [p.get("body", "") for p in env.events.payloads("notify")]
    assert not any("/work" in n for n in notes)
