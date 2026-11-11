"""Reaper tests with a frozen clock and a hand-written fake SandboxManager."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.models import HostSpec
from cloud_compute_core.reaper import ReapReport, Reaper, provide
from cloud_compute_core.runtimes import LABEL_INSTALL, LABEL_INSTANCE, MANAGED_LABEL
from cloud_compute_core.testing import sample_host, sample_instance, sample_template

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
MINE = "inst-mine"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(minutes: float) -> str:
    return iso(NOW - timedelta(minutes=minutes))


class FakeSandboxes:
    def __init__(self, store=None) -> None:
        self.store = store
        self.stopped: list[tuple[str, str]] = []
        self.destroyed: list[tuple[str, str]] = []
        self.destroy_kw: list[dict] = []
        self.removed: list[tuple[str, str]] = []
        self.adopted: list[tuple[str, str]] = []
        self.warned: list[str] = []
        self.stop_times: dict[str, str] = {}
        self.destroy_error: Exception | None = None
        self.containers: dict[str, list[dict] | Exception] = {}
        self.list_calls: list[str] = []

    def stop(self, instance_id, *, reason="stopped"):
        self.stopped.append((instance_id, reason))

    def destroy(self, instance_id, *, reason="destroyed", save=True,
                export_reason="before-delete", only_if_status=None, report=None, notify=True):
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed.append((instance_id, reason))
        kw = {"save": save, "export_reason": export_reason}
        if only_if_status is not None:
            kw["only_if_status"] = tuple(only_if_status)
        self.destroy_kw.append(kw)
        if report is not None:
            report.update(saved_files=1, workdir="/work",
                          saved={"export_id": "x-00000001", "path": "C:/exports/w/x.tar.gz",
                                 "bytes": 2048, "files": 1},
                          message="Deleted env. The files in /work (1 file, 2.0 KB) were saved "
                                  "to C:/exports/w/x.tar.gz.")

    def mark_expiry_warned(self, instance_id):
        if instance_id in self.warned:
            return None
        self.warned.append(instance_id)
        return True

    def stopped_at(self, instance_id):
        return self.stop_times.get(instance_id)

    def adopt_container(self, host_id, c):
        self.adopted.append((host_id, c["name"]))
        return SimpleNamespace(id=c["name"][3:], name=c["name"])

    def list_containers(self, host_id):
        self.list_calls.append(host_id)
        out = self.containers.get(host_id, [])
        if isinstance(out, Exception):
            raise out
        return out

    def remove_container(self, host_id, name):
        self.removed.append((host_id, name))


class FakeTemplates:
    def __init__(self, persistent: set[str] | None = None) -> None:
        self.persistent = persistent or set()

    def get(self, template_id):
        return replace(sample_template(id=template_id), persistent=template_id in self.persistent)


@pytest.fixture
def env(services, store):
    store.upsert_host(sample_host())
    store.kv_set("install_id", MINE)
    fake = FakeSandboxes()
    services.set("sandboxes", fake)
    services.set("templates", FakeTemplates())
    return services, store, fake


def add(store, iid, **kw):
    base = dict(
        id=iid, name=f"n-{iid}", created_at=ago(500), started_at=ago(500),
        last_activity_at=ago(5), idle_minutes=60, expires_at=None, status="running",
    )
    base.update(kw)
    store.upsert_instance(sample_instance(**base))


def container(iid, install=MINE, name=None):
    return {
        "name": name or f"cc-{iid}", "container_id": "abc", "state": "running",
        "labels": {MANAGED_LABEL: "1", LABEL_INSTANCE: iid, LABEL_INSTALL: install},
    }


def sweep(services, **kw):
    return Reaper(services).sweep(NOW, **kw)


def test_idle_running_is_stopped(env):
    services, store, fake = env
    add(store, "i-idle0001", last_activity_at=ago(61))
    add(store, "i-busy0002", last_activity_at=ago(59))
    rep = sweep(services)
    assert rep.stopped == ["i-idle0001"]
    assert fake.stopped == [("i-idle0001", "idle")]
    assert rep.destroyed == [] and rep.errors == []


def test_idle_falls_back_to_started_then_created(env):
    services, store, fake = env
    add(store, "i-start001", last_activity_at=None, started_at=ago(90))
    add(store, "i-creat002", last_activity_at=None, started_at=None, created_at=ago(90))
    add(store, "i-fresh003", last_activity_at=None, started_at=ago(10))
    assert sorted(sweep(services).stopped) == ["i-creat002", "i-start001"]


def test_activity_resets_idle(env):
    services, store, fake = env
    add(store, "i-act00001", last_activity_at=ago(120))
    assert sweep(services).stopped == ["i-act00001"]
    fake.stopped.clear()
    add(store, "i-act00001", last_activity_at=ago(1))
    assert sweep(services).stopped == [] and fake.stopped == []


def test_idle_disabled_without_idle_minutes(env):
    services, store, fake = env
    add(store, "i-noidle01", idle_minutes=None, last_activity_at=ago(9999))
    assert sweep(services).stopped == []


def test_already_stopped_not_restopped(env):
    services, store, fake = env
    add(store, "i-stop0001", status="stopped", last_activity_at=ago(999))
    rep = sweep(services)
    assert rep.stopped == [] and fake.stopped == []


def test_ttl_stops_never_destroys_and_wins_over_idle(env):
    services, store, fake = env
    add(store, "i-ttl00001", expires_at=ago(1), last_activity_at=ago(500))
    add(store, "i-live0002", expires_at=iso(NOW + timedelta(minutes=50)))
    rep = sweep(services)
    assert rep.expired == ["i-ttl00001"]
    assert rep.destroyed == [] and fake.destroyed == []
    assert rep.stopped == []
    assert fake.stopped == [("i-ttl00001", "expired")]
    notes = services.events.payloads("notify")
    assert [n["body"] for n in notes] == [
        "n-i-ttl00001 reached its time limit and was stopped. Its files are kept. "
        "Start it again or delete it from Overview."]
    assert notes[0]["ref"] == {"kind": "instance", "id": "i-ttl00001"}


def test_ttl_leaves_stopped_instance_alone(env):
    services, store, fake = env
    add(store, "i-ttls0001", status="stopped", expires_at=ago(10))
    rep = sweep(services)
    assert rep.destroyed == [] and rep.expired == []
    assert fake.destroyed == [] and fake.stopped == []


def test_persistent_template_not_stopped_by_ttl(env):
    services, store, fake = env
    services.set("templates", FakeTemplates({"keep"}))
    add(store, "i-pers0001", template_id="keep", expires_at=ago(30), last_activity_at=ago(5))
    add(store, "i-gone0002", template_id="tmp", expires_at=ago(30), last_activity_at=ago(5))
    rep = sweep(services)
    assert rep.expired == ["i-gone0002"] and rep.destroyed == []
    assert fake.stopped == [("i-gone0002", "expired")] and fake.destroyed == []


def test_persistent_still_idle_stopped(env):
    services, store, fake = env
    services.set("templates", FakeTemplates({"keep"}))
    add(store, "i-pers0003", template_id="keep", expires_at=ago(30), last_activity_at=ago(300))
    rep = sweep(services)
    assert rep.destroyed == [] and rep.stopped == ["i-pers0003"]


def test_destroyed_records_ignored(env):
    services, store, fake = env
    add(store, "i-dead0001", status="destroyed", expires_at=ago(30))
    rep = sweep(services)
    assert rep.destroyed == [] and fake.destroyed == []


def test_orphan_with_our_install_adopted_never_removed(env):
    services, store, fake = env
    add(store, "i-known001")
    fake.containers["x1carbon"] = [container("i-known001"), container("i-orph0001")]
    rep = sweep(services)
    assert rep.orphans == ["x1carbon:cc-i-orph0001"]
    assert rep.adopted == ["i-orph0001"]
    assert fake.adopted == [("x1carbon", "cc-i-orph0001")]
    assert fake.removed == [] and fake.destroyed == []
    notes = services.events.payloads("notify")
    assert [n["body"] for n in notes] == [
        "Found cc-i-orph0001 on x1carbon without a record; it is now listed in Overview."]


def test_orphan_foreign_install_reported_only(env):
    services, store, fake = env
    fake.containers["x1carbon"] = [container("i-alien001", install="inst-other")]
    rep = sweep(services)
    assert rep.orphans == ["x1carbon:cc-i-alien001"]
    assert fake.removed == []


def test_adopt_orphans_false_reports_only(env):
    services, store, fake = env
    fake.containers["x1carbon"] = [container("i-orph0002")]
    rep = sweep(services, adopt_orphans=False)
    assert rep.orphans == ["x1carbon:cc-i-orph0002"]
    assert fake.removed == [] and fake.adopted == [] and rep.adopted == []


def test_orphan_matched_by_instance_label(env):
    services, store, fake = env
    add(store, "i-lbl00001")
    fake.containers["x1carbon"] = [container("i-lbl00001", name="renamed")]
    assert sweep(services).orphans == []


def test_unknown_install_id_never_removes(services, store):
    store.upsert_host(sample_host())
    fake = FakeSandboxes()
    fake.containers["x1carbon"] = [container("i-orph0003")]
    services.set("sandboxes", fake)
    rep = sweep(services)
    assert rep.orphans == ["x1carbon:cc-i-orph0003"] and fake.removed == []
    assert fake.adopted == []


def test_host_error_captured_and_sweep_continues(env):
    services, store, fake = env
    store.upsert_host(sample_host(id="box2", name="box2", address="box2"))
    fake.containers["x1carbon"] = ApiError(503, "host_offline", "host offline")
    fake.containers["box2"] = [container("i-orph0004")]
    add(store, "i-idle0003", last_activity_at=ago(200))
    rep = sweep(services)
    assert rep.stopped == ["i-idle0003"]
    assert rep.orphans == ["box2:cc-i-orph0004"]
    assert rep.errors == ["x1carbon: host_offline: host offline"]
    assert fake.removed == [] and fake.adopted == [("box2", "cc-i-orph0004")]


def test_record_error_captured(env):
    services, store, fake = env
    add(store, "i-bad00001", last_activity_at=ago(200))
    add(store, "i-good0002", last_activity_at=ago(200))

    def boom(iid, *, reason="stopped"):
        if iid == "i-bad00001":
            raise ApiError(409, "conflict", "busy")
        fake.stopped.append((iid, reason))

    fake.stop = boom
    rep = sweep(services)
    assert rep.stopped == ["i-good0002"]
    assert rep.errors == ["i-bad00001: conflict: busy"]


def test_slurm_hosts_skipped_one_list_per_host(env):
    services, store, fake = env
    store.upsert_host(HostSpec(id="slurm-x1", name="s", kind="slurm", address="s"))
    sweep(services)
    assert fake.list_calls == ["x1carbon"]


def test_report_exact_and_to_dict(env):
    services, store, fake = env
    add(store, "i-a0000001", last_activity_at=ago(100))
    add(store, "i-b0000002", expires_at=ago(1))
    add(store, "i-c0000003")
    fake.containers["x1carbon"] = [container("i-a0000001"), container("i-zz000009")]
    rep = sweep(services)
    assert rep.to_dict() == {
        "stopped": ["i-a0000001"], "destroyed": [],
        "orphans": ["x1carbon:cc-i-zz000009"], "errors": [],
        "expired": ["i-b0000002"], "warned": [], "adopted": ["i-zz000009"], "kept": [], "cleaned": [],
    }
    assert isinstance(rep, ReapReport)
    events = services.events.payloads("notify")
    assert len(events) == 3          # idle stop, time limit, adopted orphan
    for e in events:
        assert set(e) == {"level", "title", "body", "ref"}
        assert e["level"] in ("info", "warn", "error")
        assert e["ref"]["kind"] == "instance"
    assert {e["ref"]["id"] for e in events} == {"i-a0000001", "i-b0000002", "i-zz000009"}


def test_no_notify_when_nothing_reaped(env):
    services, store, fake = env
    add(store, "i-calm0001")
    sweep(services)
    assert services.events.names() == []


def test_sweep_uses_injected_clock(env):
    services, store, fake = env
    add(store, "i-clk00001", last_activity_at=ago(30))
    r = Reaper(services, clock=lambda: NOW + timedelta(hours=1))
    assert r.sweep().stopped == ["i-clk00001"]


def test_provide_returns_reaper(env):
    services, _, _ = env
    assert isinstance(provide(services), Reaper)


# ---- later fixes ------------------------------------------------------------


def test_sweep_host_only_touches_that_host(env):
    services, store, fake = env
    store.upsert_host(sample_host(id="box2", name="box2", address="box2"))
    add(store, "i-h1000001", last_activity_at=ago(200))
    add(store, "i-h2000002", host_id="box2", last_activity_at=ago(200))
    fake.containers["box2"] = [container("i-orph0009")]
    rep = Reaper(services).sweep_host("box2", NOW)
    assert rep.stopped == ["i-h2000002"]
    assert rep.orphans == ["box2:cc-i-orph0009"]
    assert fake.list_calls == ["box2"]


def test_host_ids_lists_container_hosts_and_record_hosts(env):
    services, store, _ = env
    store.upsert_host(HostSpec(id="slurm-x1", name="s", kind="slurm", address="s"))
    add(store, "i-rec00001", host_id="ghost")
    assert Reaper(services).host_ids() == ["x1carbon", "ghost"]


def test_orphan_with_live_record_is_skipped(env):
    """A container whose record appeared after the snapshot (mid-spawn) is not removed."""
    services, store, fake = env
    real_list = fake.list_containers

    def racing_list(host_id):
        out = real_list(host_id)
        add(store, "i-spawn001", status="starting")  # record lands after the snapshot
        return out

    fake.list_containers = racing_list
    fake.containers["x1carbon"] = [container("i-spawn001")]
    rep = sweep(services)
    assert rep.orphans == [] and fake.removed == []


def test_orphan_with_destroyed_record_is_adopted_not_removed(env):
    services, store, fake = env
    add(store, "i-gone0001", status="destroyed")
    fake.containers["x1carbon"] = [container("i-gone0001")]
    rep = sweep(services)
    assert fake.removed == [] and rep.orphans
    assert fake.adopted == [("x1carbon", "cc-i-gone0001")]


def test_young_orphan_not_removed_old_one_is(env):
    services, store, fake = env
    young = {**container("i-young001"), "created_at": ago(3)}
    old = {**container("i-old00001"), "created_at": ago(30)}
    fake.containers["x1carbon"] = [young, old]
    rep = sweep(services)
    assert sorted(rep.orphans) == ["x1carbon:cc-i-old00001", "x1carbon:cc-i-young001"]
    assert fake.removed == [] and fake.adopted == [("x1carbon", "cc-i-old00001")]


def test_error_record_container_destroyed(env):
    services, store, fake = env
    add(store, "i-err00001", status="error", idle_minutes=None)
    add(store, "i-err00002", status="error", idle_minutes=None)
    fake.containers["x1carbon"] = [
        container("i-err00001"), container("i-err00002", install="inst-other")]
    rep = sweep(services)
    assert rep.destroyed == ["i-err00001"]
    assert fake.destroyed == [("i-err00001", "reaped")]
    # failed spawn: saved first (an empty /work proceeds), only while still "error"
    assert fake.destroy_kw == [{"save": True, "export_reason": "auto-delete",
                                "only_if_status": ("error",)}]
    assert rep.orphans == [] and fake.removed == []


def test_error_record_without_container_left_alone(env):
    services, store, fake = env
    add(store, "i-err00003", status="error", idle_minutes=None)
    rep = sweep(services)
    assert rep.destroyed == [] and fake.destroyed == []


def test_env_file_sweep_hook_called_and_errors_captured(env):
    services, store, fake = env
    store.upsert_host(sample_host(id="box2", name="box2", address="box2"))
    calls = []

    def hook(host_id, older_than_minutes=15):
        calls.append((host_id, older_than_minutes))
        if host_id == "box2":
            raise RuntimeError("nope")
        return 0

    fake.sweep_env_files = hook
    rep = sweep(services)
    assert sorted(calls) == [("box2", 15), ("x1carbon", 15)]
    assert rep.errors == ["box2: env files: RuntimeError: nope"]


# ---- data safety -----------------------------------------------------


def in_(minutes: float) -> str:
    return iso(NOW + timedelta(minutes=minutes))


def test_expiry_warning_fires_exactly_once(env):
    services, store, fake = env
    add(store, "i-warn0001", expires_at=in_(10))            # default warning: 15 min
    add(store, "i-late0002", expires_at=in_(16))            # not yet
    rep = sweep(services)
    assert rep.warned == ["i-warn0001"] and rep.expired == [] and fake.stopped == []
    assert services.events.payloads("instance.expiring") == [{
        "id": "i-warn0001", "name": "n-i-warn0001", "host_id": "x1carbon",
        "expires_at": in_(10), "minutes_left": 10}]
    assert [n["body"] for n in services.events.payloads("notify")] == [
        "n-i-warn0001 reaches its time limit in 10 minutes and will be stopped. "
        "Extend it to keep it running."]
    services.events.clear()
    assert sweep(services).warned == []                     # the fake remembers the mark
    add(store, "i-warn0001", expires_at=in_(10), expiry_warned_at=ago(1))
    assert sweep(services).warned == []                     # the record says warned
    assert services.events.names() == []


def test_expiry_warning_disabled_and_persistent_exempt(env):
    services, store, fake = env
    services.config.expiry_warning_minutes = 0
    add(store, "i-warn0003", expires_at=in_(1))
    assert sweep(services).warned == []
    services.config.expiry_warning_minutes = 15
    services.set("templates", FakeTemplates({"keep"}))
    add(store, "i-warn0003", template_id="keep", expires_at=in_(1))
    assert sweep(services).warned == [] and services.events.names() == []


def test_expiry_warning_says_one_minute(env):
    services, store, fake = env
    add(store, "i-warn0004", expires_at=iso(NOW + timedelta(seconds=30)))
    sweep(services)
    body = services.events.payloads("notify")[0]["body"]
    assert "in 1 minute and" in body


def _stopped(store, fake, iid, days_ago, **kw):
    add(store, iid, status="stopped", **kw)
    fake.stop_times[iid] = iso(NOW - timedelta(days=days_ago))


def test_auto_delete_off_by_default(env):
    services, store, fake = env
    _stopped(store, fake, "i-old00001", 400)
    rep = sweep(services)
    assert rep.destroyed == [] and fake.destroyed == []


def test_auto_delete_saves_then_deletes_after_n_days(env):
    services, store, fake = env
    services.config.auto_delete_days = 7
    _stopped(store, fake, "i-old00001", 8)
    _stopped(store, fake, "i-new00002", 6)
    add(store, "i-run00003", expires_at=None)
    rep = sweep(services)
    assert rep.destroyed == ["i-old00001"]
    assert fake.destroyed == [("i-old00001", "auto-delete")]
    assert fake.destroy_kw == [{"save": True, "export_reason": "auto-delete"}]
    assert [n["body"] for n in services.events.payloads("notify")] == [
        "n-i-old00001 was stopped for 7 days. Deleted env. The files in /work (1 file, 2.0 KB) "
        "were saved to C:/exports/w/x.tar.gz. Files outside /work were not saved."]


def test_auto_delete_keeps_an_agent_sandbox_whose_commits_were_never_fetched(env):
    services, store, fake = env
    services.config.auto_delete_days = 7
    holds = {"i-old00001"}
    services.set("agents", SimpleNamespace(unfetched_commits=lambda iid: iid in holds))
    _stopped(store, fake, "i-old00001", 8)
    _stopped(store, fake, "i-old00002", 8)
    rep = sweep(services)
    assert rep.destroyed == ["i-old00002"] and rep.kept == ["i-old00001"]
    assert fake.destroyed == [("i-old00002", "auto-delete")]
    bodies = [n["body"] for n in services.events.payloads("notify") if "never fetched" in n["body"]]
    assert len(bodies) == 1 and "Get results" in bodies[0]
    sweep(services)                                  # a second sweep does not repeat the message
    assert len([n for n in services.events.payloads("notify") if "never fetched" in n["body"]]) == 1
    assert ("i-old00001", "auto-delete") not in fake.destroyed


def test_auto_delete_is_bounded_per_tick(env):
    services, store, fake = env
    services.config.auto_delete_days = 1
    for n in range(3):
        _stopped(store, fake, f"i-old0000{n}", 5)
    assert len(sweep(services).destroyed) == 1


def test_auto_delete_skips_persistent(env):
    services, store, fake = env
    services.config.auto_delete_days = 1
    services.set("templates", FakeTemplates({"keep"}))
    _stopped(store, fake, "i-keep0001", 30, template_id="keep")
    assert sweep(services).destroyed == [] and fake.destroyed == []


def test_auto_delete_keeps_the_environment_when_saving_fails(env):
    services, store, fake = env
    services.config.auto_delete_days = 2
    _stopped(store, fake, "i-keep0002", 3)
    fake.destroy_error = ApiError(409, "not_saved", "Files could not be saved")
    rep = sweep(services)
    assert rep.kept == ["i-keep0002"] and rep.destroyed == [] and rep.errors == []
    notes = services.events.payloads("notify")
    assert [n["body"] for n in notes] == [
        "The files in /work of n-i-keep0002 could not be saved to this computer before "
        "automatic deletion, so it was kept."]
    services.events.clear()
    assert sweep(services).kept == []                       # backs off for a day
    later = Reaper(services).sweep(NOW + timedelta(hours=25))
    assert later.kept == ["i-keep0002"]                     # retried ...
    assert services.events.payloads("notify") == []         # ... but told only once
    fake.destroy_error = None
    done = Reaper(services).sweep(NOW + timedelta(hours=50))
    assert done.destroyed == ["i-keep0002"]
    assert store.kv_get("auto_delete_kept:i-keep0002") is None


def test_auto_delete_other_errors_are_reported(env):
    services, store, fake = env
    services.config.auto_delete_days = 2
    _stopped(store, fake, "i-err00009", 3)
    fake.destroy_error = ApiError(502, "internal", "podman rm failed")
    rep = sweep(services)
    assert rep.errors == ["i-err00009: internal: podman rm failed"] and rep.kept == []


def test_adoption_is_bounded_per_tick(env):
    services, store, fake = env
    fake.containers["x1carbon"] = [container(f"i-orph000{n}") for n in range(5)]
    rep = sweep(services)
    assert len(rep.orphans) == 5 and len(rep.adopted) == 3 and fake.removed == []


def test_reaper_never_calls_remove_container(env):
    services, store, fake = env
    services.config.auto_delete_days = 0
    add(store, "i-exp00001", expires_at=ago(600))
    fake.containers["x1carbon"] = [container("i-orph0001"),
                                   container("i-alien001", install="inst-other")]
    for _ in range(3):
        sweep(services)
    assert fake.removed == [] and fake.destroyed == []


# ---- the same with the real SandboxManager ----------------------------------


class _Files:
    def __init__(self):
        self.fail = None
        self.exports = []

    def export(self, instance_id, path=None, *, reason="manual"):
        if self.fail:
            raise self.fail
        self.exports.append((instance_id, reason))
        return {"id": "x-00000001", "local_path": "C:/exports/work-i-1/x.tar.gz", "bytes": 2048,
                "files": 3}


@pytest.fixture
def real(services, store, fake_transports):
    import json as _json
    from cloud_compute_core.sandbox import SandboxManager
    from cloud_compute_core.testing import exec_result

    store.upsert_host(sample_host(runtime_pref="podman"))
    services.set("templates", FakeTemplates())
    files = _Files()
    services.set("files", files)
    clock = {"now": NOW}
    mgr = SandboxManager(services, clock=lambda: clock["now"])
    services.set("sandboxes", mgr)
    t = fake_transports.get("x1carbon")
    t.script(lambda a, s: a[:2] == ["sh", "-c"] and "cat >" in a[2],
             lambda a, s: exec_result(stdout=f"/h/.cache/cloud-compute/{a[4]}.env\n"))
    t.script(lambda a, s: a[:2] == ["sh", "-c"], exec_result())
    t.script(("podman", "run"), exec_result(stdout="abc123\n"))
    t.script(("podman", "inspect"), exec_result(stdout=_json.dumps(
        [{"Id": "ab" * 32, "State": {"Status": "running"}, "NetworkSettings": {"Ports": {}}}])))
    for verb in ("stop", "start", "rm", "ps"):
        t.script(("podman", verb), exec_result())
    return SimpleNamespace(services=services, store=store, mgr=mgr, t=t, files=files,
                           clock=clock)


def _rms(t):
    return [c for c in t.calls if c.kind == "run" and c.argv[:2] == ["podman", "rm"]]


def test_real_ttl_stops_then_start_grants_a_new_limit(real):
    rec = real.mgr.spawn("python-dev", "x1carbon", ttl_minutes=30)
    real.clock["now"] = NOW + timedelta(minutes=20)
    Reaper(real.services).sweep(real.clock["now"])
    assert real.store.get_instance(rec.id).expiry_warned_at is not None
    real.clock["now"] = NOW + timedelta(minutes=31)
    rep = Reaper(real.services).sweep(real.clock["now"])
    got = real.store.get_instance(rec.id)
    assert rep.expired == [rec.id] and got.status == "stopped" and got.stop_reason == "expired"
    assert _rms(real.t) == []
    fresh = real.mgr.start(rec.id)
    assert fresh.expires_at == "2026-11-08T13:01:00Z" and fresh.expiry_warned_at is None


def test_real_auto_delete_exports_then_removes(real):
    real.services.config.auto_delete_days = 1
    rec = real.mgr.spawn("python-dev", "x1carbon")
    real.mgr.stop(rec.id)
    real.clock["now"] = NOW + timedelta(days=2)
    rep = Reaper(real.services).sweep(real.clock["now"])
    assert rep.destroyed == [rec.id]
    assert real.files.exports == [(rec.id, "auto-delete")]
    assert len(_rms(real.t)) == 1


def test_real_auto_delete_save_failure_keeps_container_and_record(real):
    real.services.config.auto_delete_days = 1
    rec = real.mgr.spawn("python-dev", "x1carbon")
    real.mgr.stop(rec.id)
    real.files.fail = RuntimeError("disk full")
    real.clock["now"] = NOW + timedelta(days=2)
    rep = Reaper(real.services).sweep(real.clock["now"])
    assert rep.kept == [rec.id] and rep.destroyed == []
    assert real.store.get_instance(rec.id).status == "stopped"
    assert _rms(real.t) == []


def test_real_orphan_is_adopted_not_removed(real):
    from cloud_compute_core.runtimes import LABEL_TEMPLATE
    from cloud_compute_core.testing import exec_result
    import json as _json
    mine = real.mgr.install_id()
    row = {"Id": "cafe" * 16, "Names": ["cc-i-0rphan09"], "State": "running",
           "Labels": {MANAGED_LABEL: "1", LABEL_INSTANCE: "i-0rphan09",
                      LABEL_INSTALL: mine, LABEL_TEMPLATE: "python-dev"}}
    real.t._run_rules = [r for r in real.t._run_rules]
    real.t.script(("podman", "ps"), exec_result(stdout=_json.dumps([row])))
    real.t._run_rules.insert(0, real.t._run_rules.pop())
    rep = Reaper(real.services).sweep(NOW)
    assert rep.adopted == ["i-0rphan09"], rep
    rec = real.store.get_instance("i-0rphan09")
    assert rec.status == "running" and rec.stop_reason == "orphan"
    assert rec.expires_at == "2026-11-08T16:00:00Z"
    assert _rms(real.t) == []
    notes = real.services.events.payloads("notify")
    assert [n["body"] for n in notes] == [
        "Found cc-i-0rphan09 on x1carbon without a record; it is now listed in Overview."]
    assert Reaper(real.services).sweep(NOW).adopted == []      # listed now: not an orphan


@pytest.mark.parametrize("age_min,adopted", [(6, False), (11, True)])
def test_real_orphan_grace_uses_ps_creation_time(real, age_min, adopted):
    """list_containers passes the ps creation time through, so the grace period applies."""
    from cloud_compute_core.runtimes import LABEL_TEMPLATE
    from cloud_compute_core.testing import exec_result
    import json as _json
    created = int((NOW - timedelta(minutes=age_min)).timestamp())
    row = {"Id": "cafe" * 16, "Names": ["cc-i-0rphan10"], "State": "running",
           "Created": created,
           "Labels": {MANAGED_LABEL: "1", LABEL_INSTANCE: "i-0rphan10",
                      LABEL_INSTALL: real.mgr.install_id(), LABEL_TEMPLATE: "python-dev"}}
    real.t._run_rules = [r for r in real.t._run_rules]
    real.t.script(("podman", "ps"), exec_result(stdout=_json.dumps([row])))
    real.t._run_rules.insert(0, real.t._run_rules.pop())
    assert real.mgr.list_containers("x1carbon")[0]["created_at"]
    rep = Reaper(real.services).sweep(NOW)
    assert rep.adopted == (["i-0rphan10"] if adopted else [])
    assert _rms(real.t) == []
