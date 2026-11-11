"""Summary routes: /health, /summary, /host-types, /events."""

from __future__ import annotations

import copy
import datetime as dt
import sys
import time

import pytest

from cloud_compute_core import hosttypes, registry, summary_api
from cloud_compute_core.interfaces import SERVICE_NAMES, ApiError, ApiRequest, Services
from cloud_compute_core.testing import (
    make_services, sample_host, sample_host_state, sample_instance, sample_job,
    sample_local_host, sample_slurm_host,
)

PKG = "cloud_compute_core"
NOW = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)


def iso(delta_hours: float = 0.0) -> str:
    return (NOW + dt.timedelta(hours=delta_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def req(services, **query):
    return ApiRequest(path_params={}, query={k: str(v) for k, v in query.items()},
                      body=None, services=services)


@pytest.fixture(autouse=True)
def _clean_errors():
    registry.clear_errors()
    yield
    registry.clear_errors()


# --------------------------------------------------------------------------- routes


def test_routes_declares_the_four_get_routes():
    got = [(r.method, r.path) for r in summary_api.routes()]
    assert got == [("GET", "/health"), ("GET", "/summary"),
                   ("GET", "/host-types"), ("GET", "/events")]
    handlers = {r.path: r.handler for r in summary_api.routes()}
    assert handlers["/health"] is summary_api.health
    assert handlers["/summary"] is summary_api.summary
    assert handlers["/host-types"] is summary_api.host_types
    assert handlers["/events"] is summary_api.events


def test_tools_and_background_tasks_are_empty():
    assert summary_api.tools() == []
    assert summary_api.background_tasks() == []


def test_registry_collects_the_routes():
    collected = {(r.method, r.path) for r in registry.collect_routes()}
    for route in [("GET", "/health"), ("GET", "/summary"),
                  ("GET", "/host-types"), ("GET", "/events")]:
        assert route in collected
    assert "summary_api" not in registry.import_errors()


# --------------------------------------------------------------------------- /health


def test_health_shape_and_types(services):
    out = summary_api.health(req(services))
    assert out["ok"] is True
    assert isinstance(out["version"], str) and out["version"]
    assert isinstance(out["configured"], bool)
    assert isinstance(out["broadcast"], bool)
    assert isinstance(out["asyncssh"], bool)
    assert set(out["services"]) == set(SERVICE_NAMES)
    assert all(isinstance(v, bool) for v in out["services"].values())
    assert isinstance(out["modules"], dict)
    assert isinstance(out["errors"], dict)


def test_health_configured_flips_after_adding_a_host(services):
    assert summary_api.health(req(services))["configured"] is False
    services.store.upsert_host(sample_host())
    assert summary_api.health(req(services))["configured"] is True


def test_health_reports_built_services_without_building_others(services):
    before = services.built()
    out = summary_api.health(req(services))
    assert services.built() == before
    for name in before:
        assert out["services"][name] is True


def test_health_works_without_a_store():
    services = make_services()  # store has no instance; its provider is the real default path
    services.__dict__["_providers"].pop("store")
    out = summary_api.health(req(services))
    assert out["ok"] is True
    assert out["configured"] is False


def test_health_with_bare_services_container():
    out = summary_api.health(req(Services()))
    assert out["ok"] is True
    assert out["configured"] is False
    assert set(out["services"]) == set(SERVICE_NAMES)


def test_health_flags_missing_service_module(services, monkeypatch):
    monkeypatch.setitem(sys.modules, f"{PKG}.sandbox", None)
    out = summary_api.health(req(services))
    assert out["services"]["sandboxes"] is False
    assert out["modules"]["sandbox"] is False
    assert out["services"]["store"] is True


def test_health_flags_missing_prober_module(services, monkeypatch):
    monkeypatch.setitem(sys.modules, f"{PKG}.prober", None)
    out = summary_api.health(req(services))
    assert out["services"]["prober"] is False


def test_health_reads_asyncssh_and_broadcast(services, monkeypatch):
    monkeypatch.setattr(summary_api, "_asyncssh_available", lambda: True)
    monkeypatch.setattr(summary_api._events, "broadcast_available", lambda: True)
    out = summary_api.health(req(services))
    assert out["asyncssh"] is True and out["broadcast"] is True
    monkeypatch.setattr(summary_api, "_asyncssh_available", lambda: False)
    monkeypatch.setattr(summary_api._events, "broadcast_available", lambda: False)
    out = summary_api.health(req(services))
    assert out["asyncssh"] is False and out["broadcast"] is False


def test_health_is_fast(services):
    summary_api.health(req(services))  # warm module imports
    start = time.perf_counter()
    summary_api.health(req(services))
    assert time.perf_counter() - start < 2.0


def test_version_parsed_from_plugin_yaml(tmp_path, monkeypatch):
    manifest = tmp_path / "plugin.yaml"
    manifest.write_text('name: x\nversion: "1.2.3"\nlabel: y\n', encoding="utf-8")
    monkeypatch.setattr(summary_api, "_plugin_yaml_path", lambda: manifest)
    monkeypatch.setattr(summary_api, "_version_cache", {})
    assert summary_api._plugin_version() == "1.2.3"
    # cached: later edits are not re-read
    manifest.write_text('version: "9.9.9"\n', encoding="utf-8")
    assert summary_api._plugin_version() == "1.2.3"


def test_version_accepts_unquoted_and_single_quoted(tmp_path, monkeypatch):
    manifest = tmp_path / "plugin.yaml"
    monkeypatch.setattr(summary_api, "_plugin_yaml_path", lambda: manifest)
    monkeypatch.setattr(summary_api, "_version_cache", {})
    manifest.write_text("version: 2.0.1  # note\n", encoding="utf-8")
    assert summary_api._plugin_version() == "2.0.1"
    monkeypatch.setattr(summary_api, "_version_cache", {})
    manifest.write_text("version: '3.1.4'\n", encoding="utf-8")
    assert summary_api._plugin_version() == "3.1.4"


def test_version_falls_back_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(summary_api, "_plugin_yaml_path", lambda: tmp_path / "absent.yaml")
    monkeypatch.setattr(summary_api, "_version_cache", {})
    assert summary_api._plugin_version() == "0.0.0"
    manifest = tmp_path / "plugin.yaml"
    manifest.write_text("name: x\n", encoding="utf-8")
    monkeypatch.setattr(summary_api, "_plugin_yaml_path", lambda: manifest)
    monkeypatch.setattr(summary_api, "_version_cache", {})
    assert summary_api._plugin_version() == "0.0.0"


def test_real_plugin_yaml_has_a_version():
    summary_api._version_cache.clear()
    manifest = summary_api._plugin_yaml_path()
    if not manifest.exists():
        pytest.skip("plugin.yaml not found")
    assert summary_api._plugin_version() != "0.0.0"


# --------------------------------------------------------------------------- /summary


def host_entry(spec, status=None, last_error=None):
    state = None if status is None else sample_host_state(
        host_id=spec.id, status=status, last_error=last_error).to_dict()
    return {"spec": spec.to_dict(), "state": state}


def make_snapshot(hosts=(), instances=(), jobs=()):
    return {"hosts": list(hosts),
            "instances": [i.to_dict() for i in instances],
            "jobs": [j.to_dict() for j in jobs],
            "generated_at": iso()}


def test_summarize_empty():
    out = summary_api.summarize(make_snapshot())
    assert out == {
        "hosts": {"total": 0, "online": 0, "degraded": 0, "auth_required": 0, "offline": 0},
        "instances": {"running": 0, "total": 0},
        "jobs": {"active": 0, "finished_24h": 0},
        "attention": [],
        "generated_at": iso(),
    }


def test_summarize_counts_hosts_by_state():
    hosts = [
        host_entry(sample_host(id="a", name="a"), "online"),
        host_entry(sample_host(id="b", name="b"), "degraded", "disk nearly full"),
        host_entry(sample_host(id="c", name="c"), "auth_required"),
        host_entry(sample_host(id="d", name="d"), "offline"),
        host_entry(sample_host(id="e", name="e"), "unknown"),
        host_entry(sample_host(id="f", name="f"), None),
    ]
    out = summary_api.summarize(make_snapshot(hosts))["hosts"]
    assert out == {"total": 6, "online": 1, "degraded": 1, "auth_required": 1, "offline": 1}


def test_summarize_instance_counts():
    instances = [
        sample_instance(id="i-00000001", status="running"),
        sample_instance(id="i-00000002", status="running"),
        sample_instance(id="i-00000003", status="stopped"),
        sample_instance(id="i-00000004", status="creating"),
        sample_instance(id="i-00000005", status="error", error="image pull failed"),
        sample_instance(id="i-00000006", status="destroyed"),
    ]
    out = summary_api.summarize(make_snapshot(instances=instances))["instances"]
    assert out == {"running": 2, "total": 5}


def test_summarize_job_counts_and_24h_window():
    jobs = [
        sample_job(id="j-00000001", state="RUNNING"),
        sample_job(id="j-00000002", state="PENDING"),
        sample_job(id="j-00000003", state="COMPLETING"),
        sample_job(id="j-00000004", state="COMPLETED", ended_at=iso(-1)),
        sample_job(id="j-00000005", state="FAILED", ended_at=iso(-23)),
        sample_job(id="j-00000006", state="COMPLETED", ended_at=iso(-25)),
        sample_job(id="j-00000007", state="TIMEOUT", ended_at=iso(-30)),
        sample_job(id="j-00000008", state="UNKNOWN"),
    ]
    out = summary_api.summarize(make_snapshot(jobs=jobs))
    assert out["jobs"] == {"active": 3, "finished_24h": 2}
    failed_ids = [a["id"] for a in out["attention"] if a["kind"] == "job"]
    assert failed_ids == ["j-00000005"]  # TIMEOUT ended 30h ago is outside the window


def test_summarize_terminal_job_without_end_time_counts_as_recent():
    jobs = [sample_job(id="j-00000001", state="FAILED", ended_at=None)]
    out = summary_api.summarize(make_snapshot(jobs=jobs))
    assert out["jobs"]["finished_24h"] == 1
    assert [a["id"] for a in out["attention"]] == ["j-00000001"]


def test_summarize_unknown_job_with_end_time_counts_as_finished():
    """A job closed without a confirmed state (UNKNOWN + ended_at) is finished, not 'other'."""
    jobs = [
        sample_job(id="j-00000001", state="UNKNOWN", ended_at=iso(-2)),    # finished, in window
        sample_job(id="j-00000002", state="UNKNOWN", ended_at=iso(-30)),   # finished, too old
        sample_job(id="j-00000003", state="UNKNOWN", ended_at=None),       # still unresolved
    ]
    out = summary_api.summarize(make_snapshot(jobs=jobs))
    assert out["jobs"] == {"active": 0, "finished_24h": 1}
    assert [a for a in out["attention"] if a["kind"] == "job"] == []


def test_summarize_attention_entries():
    hosts = [
        host_entry(sample_host(id="ok", name="ok"), "online"),
        host_entry(sample_host(id="x1carbon", name="x1carbon"), "offline"),
        host_entry(sample_host(id="gate", name="gate"), "auth_required"),
        host_entry(sample_host(id="slow", name="slow"), "degraded", "podman is not responding"),
        host_entry(sample_host(id="odd", name="odd"), "degraded"),
    ]
    instances = [sample_instance(id="i-0000000a", name="dev-1", host_id="x1carbon",
                                 status="error", error="container exited")]
    jobs = [sample_job(id="j-0000000a", name="train", host_id="slow", state="OUT_OF_MEMORY",
                       ended_at=iso(-2)),
            sample_job(id="j-0000000b", state="NODE_FAIL", ended_at=iso(-3)),
            sample_job(id="j-0000000c", state="CANCELLED", ended_at=iso(-1))]
    out = summary_api.summarize(make_snapshot(hosts, instances, jobs))
    by_id = {(a["kind"], a["id"]): a["message"] for a in out["attention"]}
    assert set(by_id) == {
        ("host", "x1carbon"), ("host", "gate"), ("host", "slow"), ("host", "odd"),
        ("instance", "i-0000000a"), ("job", "j-0000000a"), ("job", "j-0000000b"),
    }
    assert by_id[("host", "x1carbon")] == "x1carbon is offline"
    assert by_id[("host", "gate")] == "gate needs login: run ssh gate once"
    assert by_id[("host", "slow")] == "slow is degraded: podman is not responding"
    assert by_id[("host", "odd")] == "odd is degraded"
    assert "dev-1" in by_id[("instance", "i-0000000a")]
    assert "container exited" in by_id[("instance", "i-0000000a")]
    assert "x1carbon" in by_id[("instance", "i-0000000a")]
    assert "OUT_OF_MEMORY" in by_id[("job", "j-0000000a")]
    assert all(set(a) == {"kind", "id", "message"} for a in out["attention"])


def test_summarize_unknown_and_missing_state_are_not_attention():
    hosts = [host_entry(sample_host(id="a", name="a"), "unknown"),
             host_entry(sample_host(id="b", name="b"), None)]
    out = summary_api.summarize(make_snapshot(hosts))
    assert out["attention"] == []
    assert out["hosts"]["total"] == 2


def test_summarize_is_pure():
    snap = make_snapshot(
        [host_entry(sample_host(), "offline")],
        [sample_instance(status="error", error="boom")],
        [sample_job(state="FAILED", ended_at=iso(-1))],
    )
    frozen = copy.deepcopy(snap)
    first = summary_api.summarize(snap)
    second = summary_api.summarize(snap)
    assert snap == frozen
    assert first == second
    first["attention"].append({"kind": "x", "id": "y", "message": "z"})
    assert summary_api.summarize(snap) == second


def test_summarize_keeps_generated_at():
    assert summary_api.summarize(make_snapshot())["generated_at"] == iso()


def test_summarize_tolerates_garbage():
    out = summary_api.summarize({})
    assert out["hosts"]["total"] == 0
    assert isinstance(out["generated_at"], str)
    assert summary_api.summarize(None)["jobs"] == {"active": 0, "finished_24h": 0}


def test_attention_messages_hold_no_secrets():
    secret = "s3cr3t-token-value"
    host = sample_host(id="h", name="h", identity_file="C:/keys/" + secret)
    out = summary_api.summarize(make_snapshot(
        [host_entry(host, "offline")],
        [sample_instance(host_id="h", status="error", error="failed")]))
    assert secret not in repr(out)


def test_summary_handler_uses_store_snapshot(services):
    store = services.store
    store.upsert_host(sample_host())
    store.set_host_state(sample_host_state(status="online"))
    store.upsert_host(sample_slurm_host())
    store.set_host_state(sample_host_state(host_id="slurm-x1", status="offline"))
    store.upsert_host(sample_local_host())
    store.upsert_instance(sample_instance(id="i-00000001", status="running"))
    store.upsert_instance(sample_instance(id="i-00000002", status="destroyed"))
    store.upsert_job(sample_job(id="j-00000001", state="RUNNING"))
    out = summary_api.summary(req(services))
    assert out["hosts"] == {"total": 3, "online": 1, "degraded": 0, "auth_required": 0, "offline": 1}
    assert out["instances"] == {"running": 1, "total": 1}
    assert out["jobs"]["active"] == 1
    assert [(a["kind"], a["id"]) for a in out["attention"]] == [("host", "slurm-x1")]
    assert out["generated_at"].endswith("Z")


def test_summary_without_store_is_unavailable():
    with pytest.raises(ApiError) as exc:
        summary_api.summary(req(Services()))
    assert exc.value.status == 503 and exc.value.code == "unavailable"


# --------------------------------------------------------------------------- chip_text


def summary_of(online, total, envs, jobs):
    return {"hosts": {"online": online, "total": total}, "instances": {"running": envs},
            "jobs": {"active": jobs}}


def test_chip_text_examples():
    assert summary_api.chip_text(summary_of(2, 3, 3, 1)) == "● 2/3 hosts · 3 envs · 1 job"
    assert summary_api.chip_text(summary_of(1, 1, 1, 2)) == "● 1/1 host · 1 env · 2 jobs"
    assert summary_api.chip_text(summary_of(0, 0, 0, 0)) == "● 0/0 hosts · 0 envs · 0 jobs"


def test_chip_text_from_summarize():
    snap = make_snapshot(
        [host_entry(sample_host(id="a", name="a"), "online"),
         host_entry(sample_host(id="b", name="b"), "online"),
         host_entry(sample_host(id="c", name="c"), "offline")],
        [sample_instance(id=f"i-0000000{n}", status="running") for n in range(3)],
        [sample_job(state="RUNNING")],
    )
    assert summary_api.chip_text(summary_api.summarize(snap)) == "● 2/3 hosts · 3 envs · 1 job"


# --------------------------------------------------------------------------- /host-types


def test_host_types_matches_catalog(services):
    out = summary_api.host_types(req(services))
    assert out == hosttypes.host_types()
    assert isinstance(out, list) and out


# --------------------------------------------------------------------------- /events


def seed_events(store, n):
    return [store.append_event("hosts.changed", {"n": i}) for i in range(n)]


def test_events_default_returns_all_and_next(services):
    seqs = seed_events(services.store, 3)
    out = summary_api.events(req(services))
    assert [e["seq"] for e in out["events"]] == seqs
    assert out["next"] == seqs[-1]
    assert set(out["events"][0]) == {"seq", "ts", "name", "payload"}


def test_events_since_filters(services):
    seqs = seed_events(services.store, 4)
    out = summary_api.events(req(services, since=seqs[1]))
    assert [e["seq"] for e in out["events"]] == seqs[2:]
    assert out["next"] == seqs[-1]


def test_events_next_is_since_when_empty(services):
    seqs = seed_events(services.store, 2)
    out = summary_api.events(req(services, since=seqs[-1]))
    assert out == {"events": [], "next": seqs[-1]}
    assert summary_api.events(req(services))["next"] == seqs[-1]


def test_events_empty_store_defaults_to_zero(services):
    assert summary_api.events(req(services)) == {"events": [], "next": 0}


def test_events_limit(services):
    seqs = seed_events(services.store, 5)
    out = summary_api.events(req(services, limit=2))
    assert [e["seq"] for e in out["events"]] == seqs[:2]
    assert out["next"] == seqs[1]
    follow = summary_api.events(req(services, since=out["next"], limit=2))
    assert [e["seq"] for e in follow["events"]] == seqs[2:4]


def test_events_limit_is_clamped(services):
    seqs = seed_events(services.store, 3)
    assert len(summary_api.events(req(services, limit=0))["events"]) == 1
    assert len(summary_api.events(req(services, limit=-5))["events"]) == 1
    assert len(summary_api.events(req(services, limit=100000))["events"]) == len(seqs)


@pytest.mark.parametrize("since", ["abc", "-1", "1.5", "", " ", "0x10", "1e3"])
def test_events_invalid_since(services, since):
    with pytest.raises(ApiError) as exc:
        summary_api.events(req(services, since=since))
    assert exc.value.status == 400 and exc.value.code == "invalid"


def test_events_invalid_limit(services):
    with pytest.raises(ApiError) as exc:
        summary_api.events(req(services, limit="many"))
    assert exc.value.status == 400 and exc.value.code == "invalid"


def test_events_without_store_is_unavailable():
    with pytest.raises(ApiError) as exc:
        summary_api.events(req(Services()))
    assert exc.value.status == 503 and exc.value.code == "unavailable"


def test_summarize_lists_expired_and_adopted_environments():
    hosts = [host_entry(sample_host(id="x1carbon", name="Lab box"), "online")]
    instances = [
        sample_instance(id="i-0000000e", name="train", host_id="x1carbon", status="stopped",
                        stop_reason="expired"),
        sample_instance(id="i-0000000f", name="cc-i-0000000f", host_id="x1carbon",
                        status="stopped", stop_reason="orphan"),
        sample_instance(id="i-00000010", name="mine", host_id="x1carbon", status="stopped",
                        stop_reason="user"),
    ]
    out = summary_api.summarize(make_snapshot(hosts, instances, []))
    by_id = {a["id"]: a["message"] for a in out["attention"]}
    assert by_id == {
        "i-0000000e": ("train reached its time limit and was stopped; its files are kept. "
                       "Start it again or delete it."),
        "i-0000000f": "cc-i-0000000f was found on Lab box without a record; it is stopped.",
    }
    assert all(set(a) == {"kind", "id", "message"} for a in out["attention"])
    assert out["instances"]["total"] == 3
