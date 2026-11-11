"""SlurmManager against FakeTransport (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cloud_compute_core.config import Config
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.models import JobRequest, SlurmSettings
from cloud_compute_core.slurm import (
    POLL_CALL_TIMEOUT_S, SACCT_FIELDS, SQUEUE_FORMAT, TIME_ENV, SlurmManager, provide,
)
from cloud_compute_core.testing import exec_result, sample_host, sample_job, sample_slurm_host

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "slurm"
HOST = "slurm-x1"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def mgr(services):
    services.store.upsert_host(sample_slurm_host())
    return SlurmManager(services)


@pytest.fixture
def t(services, fake_transports):
    return fake_transports.get(HOST)


@pytest.fixture
def ev(fake_events):
    return fake_events


def req(**kw) -> JobRequest:
    base = dict(host_id=HOST, name="hello", command="hostname")
    base.update(kw)
    return JobRequest(**base)


def is_squeue(argv, stdin):
    return argv[:3] == ["env", TIME_ENV, "squeue"]


def is_sacct(argv, stdin):
    return argv[:3] == ["env", TIME_ENV, "sacct"]


def count(t, name):
    return sum(1 for a in t.argvs() if a[:3] == ["env", TIME_ENV, name])


def add_job(services, **kw):
    job = sample_job(**kw)
    services.store.upsert_job(job)
    return job


# --------------------------------------------------------------------------- provide / basics


def test_provide_and_registry(services):
    assert isinstance(provide(services), SlurmManager)
    services.store.upsert_host(sample_slurm_host())
    assert isinstance(services.slurm, SlurmManager)


def test_get_missing_and_list(mgr, services):
    with pytest.raises(ApiError) as exc:
        mgr.get("j-00000000")
    assert exc.value.code == "not_found"
    a = add_job(services, id="j-00000001", submitted_at="2026-11-08T10:00:00Z")
    b = add_job(services, id="j-00000002", submitted_at="2026-11-08T11:00:00Z", state="COMPLETED")
    add_job(services, id="j-00000003", host_id="other")
    assert [j.id for j in mgr.list(HOST)] == [b.id, a.id]
    assert [j.id for j in mgr.list(HOST, active_only=True)] == [a.id]
    assert len(mgr.list()) == 3
    assert mgr.get(a.id).id == a.id


# --------------------------------------------------------------------------- submit


def test_submit_writes_script_via_stdin_and_records(mgr, t, ev, services):
    t.script(("sh", "-c"), exec_result(stdout="123\n"))
    rec = mgr.submit(req(cpus=2), session_id="sess-1")
    assert rec.id.startswith("j-") and len(rec.id) == 10
    assert rec.slurm_job_id == "123" and rec.state == "PENDING"
    assert rec.session_id == "sess-1" and rec.partition == "cpu"
    assert rec.job_dir == f"/data/cloud-compute/{rec.id}"
    (call,) = t.calls
    assert call.argv[:2] == ["sh", "-c"] and call.argv[-1] == rec.job_dir
    assert "mkdir -p" in call.argv[2] and "sbatch --parsable job.sh" in call.argv[2]
    assert call.timeout and call.timeout <= 60
    script = call.stdin.decode()
    assert f"#SBATCH --comment=cloud-compute:{rec.id}" in script
    assert f"#SBATCH --chdir={rec.job_dir}" in script and "#SBATCH -c 2" in script
    assert services.store.get_job(rec.id).session_id == "sess-1"
    assert ev.payloads("jobs.changed") == [{"ids": [rec.id], "host_id": HOST}]


def test_submit_parsable_with_cluster_suffix(mgr, t):
    t.script(("sh", "-c"), exec_result(stdout="77;linux\n"))
    assert mgr.submit(req()).slurm_job_id == "77"


def test_submit_home_workdir_is_resolved(mgr, t, services):
    services.store.upsert_host(sample_slurm_host(slurm=SlurmSettings(workdir="~/cloud-compute")))
    t.script(lambda a, s: "pwd -P" in " ".join(a), exec_result(stdout="/home/u/cloud-compute/x\n"))
    t.script(lambda a, s: "sbatch" in " ".join(a), exec_result(stdout="5\n"))
    rec = mgr.submit(req())
    assert rec.job_dir == "/home/u/cloud-compute/x"
    assert t.argvs()[0][-1].startswith("cloud-compute/j-")
    assert "#SBATCH --chdir=/home/u/cloud-compute/x" in t.calls[1].stdin.decode()


def test_submit_invalid_requests(mgr, t):
    with pytest.raises(ApiError) as exc:
        mgr.submit(req(command=None))
    assert exc.value.code == "invalid"
    with pytest.raises(ApiError) as exc:
        mgr.submit(req(time_limit="1\n#SBATCH -p x"))
    assert exc.value.code == "invalid"
    assert t.calls == []                     # nothing reached the host


def test_submit_host_errors(mgr, services, t):
    with pytest.raises(ApiError) as exc:
        mgr.submit(req(host_id="nope"))
    assert exc.value.code == "not_found"
    services.store.upsert_host(sample_host())
    with pytest.raises(ApiError) as exc:
        mgr.submit(req(host_id="x1carbon"))
    assert exc.value.code == "invalid"


def test_submit_default_settings_when_none(mgr, services, t):
    services.store.upsert_host(sample_slurm_host(slurm=None))
    t.script(lambda a, s: "sbatch" in " ".join(a) and "$HOME" not in " ".join(a),
             exec_result(stdout="9\n"))
    t.script(lambda a, s: "pwd -P" in " ".join(a), exec_result(stdout="/home/u/cloud-compute/j\n"))
    rec = mgr.submit(req())
    assert rec.slurm_job_id == "9" and rec.partition is None


def test_submit_offline_and_sbatch_failure(mgr, t):
    t.script(("sh", "-c"), [exec_result(rc=255, stderr="ssh: connect to host x1carbon: timed out"),
                            exec_result(rc=1, stderr="sbatch: error: invalid partition specified"),
                            exec_result(rc=0, stdout="garbage")])
    with pytest.raises(ApiError) as exc:
        mgr.submit(req())
    assert exc.value.code == "host_offline"
    with pytest.raises(ApiError) as exc:
        mgr.submit(req())
    assert exc.value.code == "invalid" and "invalid partition" in exc.value.message
    with pytest.raises(ApiError) as exc:
        mgr.submit(req())
    assert exc.value.code == "internal"
    assert mgr.list() == []


def test_submit_timeout_is_offline(mgr, t):
    t.script(("sh", "-c"), exec_result(rc=124, timed_out=True))
    with pytest.raises(ApiError) as exc:
        mgr.submit(req())
    assert exc.value.code == "host_offline"


def test_max_active_jobs(mgr, services, t):
    services.store.upsert_host(sample_slurm_host(
        slurm=SlurmSettings(workdir="/data/cloud-compute", max_active_jobs=2)))
    add_job(services, id="j-00000001", slurm_job_id="1", state="RUNNING")
    add_job(services, id="j-00000002", slurm_job_id="2", state="PENDING")
    add_job(services, id="j-00000003", slurm_job_id="3", state="COMPLETED")
    with pytest.raises(ApiError) as exc:
        mgr.submit(req())
    assert exc.value.code == "limit_reached"
    assert t.calls == []


# --------------------------------------------------------------------------- poll


def script_poll(t, squeue: str, sacct: str):
    t.script(is_squeue, exec_result(stdout=squeue))
    t.script(is_sacct, exec_result(stdout=sacct))


def test_poll_one_squeue_one_sacct(mgr, services, t, ev):
    for i, sid in enumerate(["5", "6", "7", "8"]):
        add_job(services, id=f"j-0000000{i}", slurm_job_id=sid, state="PENDING")
    script_poll(t, fixture("squeue_running.txt"), fixture("sacct_running.txt"))
    mgr.poll(HOST)
    assert count(t, "squeue") == 1 and count(t, "sacct") == 1 and len(t.calls) == 2
    sq, sa = t.argvs()
    assert sq == ["env", "SLURM_TIME_FORMAT=%s", "squeue", "-h", "--me", "-o", SQUEUE_FORMAT]
    assert sa == ["env", "SLURM_TIME_FORMAT=%s", "sacct", "-X", "-P", "-n", "-j", "5,6,7,8",
                  "-o", SACCT_FIELDS]
    assert all(c.timeout <= POLL_CALL_TIMEOUT_S for c in t.calls)
    assert 2 * POLL_CALL_TIMEOUT_S < 10
    states = {j.slurm_job_id: j.state for j in mgr.list(HOST)}
    assert states == {"5": "COMPLETED", "6": "FAILED", "7": "RUNNING", "8": "RUNNING"}
    arr = next(j for j in mgr.list(HOST) if j.slurm_job_id == "8")
    assert arr.nodes == "c2" and arr.started_at == "2026-11-08T07:20:54" and arr.ended_at is None
    changed = ev.payloads("jobs.changed")
    assert len(changed) == 1 and changed[0]["host_id"] == HOST and len(changed[0]["ids"]) == 4


def test_poll_finished_once_and_notified_persisted(mgr, services, t, ev):
    for i, sid in enumerate(["5", "6", "7", "8"]):
        add_job(services, id=f"j-0000000{i}", slurm_job_id=sid, state="RUNNING", name=f"n{sid}")
    script_poll(t, "", fixture("sacct_finished.txt"))
    mgr.poll(HOST)
    finished = ev.payloads("job.finished")
    assert sorted(p["slurm_job_id"] for p in finished) == ["5", "6", "7", "8"]
    by_sid = {p["slurm_job_id"]: p for p in finished}
    assert by_sid["6"] == {"id": "j-00000001", "host_id": HOST, "slurm_job_id": "6", "name": "n6",
                           "state": "FAILED", "exit_code": "3:0"}
    assert by_sid["7"]["state"] == "CANCELLED"
    assert by_sid["8"]["state"] == "COMPLETED" and by_sid["8"]["exit_code"] == "0:0"
    assert all(j.notified for j in mgr.list(HOST))
    assert all(services.store.get_job(j.id).notified for j in mgr.list(HOST))
    arr = next(j for j in mgr.list(HOST) if j.slurm_job_id == "8")
    assert arr.ended_at == "2026-11-08T07:21:56" and arr.nodes == "c2,c1"
    # notify events (config default notify_job_complete=True)
    notes = ev.payloads("notify")
    assert len(notes) == 4
    n6 = next(n for n in notes if n["ref"] == {"kind": "job", "id": "j-00000001"})
    assert n6["level"] == "error" and "failed" in n6["title"].lower()
    assert next(n for n in notes if n["ref"]["id"] == "j-00000003")["level"] == "info"
    # second poll: no open jobs -> no calls, no new events
    t.reset_calls()
    ev.clear()
    assert mgr.poll(HOST) == []
    assert t.calls == [] and ev.events == []


def test_poll_no_notify_when_disabled(mgr, services, t, ev):
    services.set("config", Config(notify_job_complete=False))
    add_job(services, id="j-00000001", slurm_job_id="5", state="RUNNING")
    script_poll(t, "", fixture("sacct_finished.txt"))
    mgr.poll(HOST)
    assert len(ev.payloads("job.finished")) == 1 and ev.payloads("notify") == []


def test_poll_no_change_emits_nothing(mgr, services, t, ev):
    add_job(services, id="j-00000001", slurm_job_id="7", state="RUNNING", partition="cpu",
            nodes="c1", started_at="2026-11-08T07:20:53")
    script_poll(t, fixture("squeue_running.txt"), fixture("sacct_running.txt"))
    mgr.poll(HOST)
    assert ev.events == []


def test_poll_without_open_jobs_makes_no_calls(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="5", state="COMPLETED", notified=True)
    assert mgr.poll(HOST) == []
    assert t.calls == []


def test_poll_job_not_visible_yet_is_kept(mgr, services, t, ev):
    add_job(services, id="j-00000001", slurm_job_id="99", state="PENDING", started_at=None,
            nodes=None)
    script_poll(t, "", "")
    (rec,) = mgr.poll(HOST)
    assert rec.state == "PENDING" and ev.events == []


def test_poll_array_pending_range_then_tasks(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="8", state="PENDING")
    # squeue is fresher than sacct: the old pending range is ignored once tasks show up
    script_poll(t, "8_3|PENDING|cpu||N/A\n8_2|RUNNING|cpu|c1|2026-11-08T07:21:15\n",
                "8_1|COMPLETED|0:0|2026-11-08T07:20:54|2026-11-08T07:21:14|c2\n"
                "8_[2-3%1]|PENDING|0:0|Unknown|Unknown|None assigned\n")
    (rec,) = mgr.poll(HOST)
    assert rec.state == "RUNNING" and rec.started_at == "2026-11-08T07:20:54"


def test_poll_unknown_state_stays_open(mgr, services, t, ev):
    add_job(services, id="j-00000001", slurm_job_id="9", state="RUNNING")
    script_poll(t, "9|SUSPENDED|cpu|c1|2026-11-08T07:00:00\n",
                "9|SUSPENDED|0:0|2026-11-08T07:00:00|Unknown|c1\n")
    (rec,) = mgr.poll(HOST)
    assert rec.state == "UNKNOWN" and ev.payloads("job.finished") == []
    assert mgr.hosts_with_active_jobs() == [HOST]


def test_poll_squeue_offline_skips_sacct(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="7", state="RUNNING")
    t.script(is_squeue, exec_result(rc=255, stderr="ssh: Could not resolve hostname"))
    with pytest.raises(ApiError) as exc:
        mgr.poll(HOST)
    assert exc.value.code == "host_offline"
    assert count(t, "squeue") == 1 and count(t, "sacct") == 0


def test_poll_sacct_failure_uses_squeue(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="7", state="PENDING")
    t.script(is_squeue, exec_result(stdout=fixture("squeue_running.txt")))
    t.script(is_sacct, exec_result(rc=1, stderr="sacct: error: Slurm accounting storage is disabled"))
    (rec,) = mgr.poll(HOST)
    assert rec.state == "RUNNING" and rec.nodes == "c1"


def test_poll_announces_terminal_jobs_not_yet_notified(mgr, services, t, ev):
    add_job(services, id="j-00000001", slurm_job_id="5", state="COMPLETED", exit_code="0:0")
    mgr.poll(HOST)
    assert [p["id"] for p in ev.payloads("job.finished")] == ["j-00000001"]
    ev.clear()
    mgr.poll(HOST)
    assert ev.payloads("job.finished") == []


def test_helpers(mgr, services):
    add_job(services, id="j-00000001", slurm_job_id="1", state="RUNNING")
    add_job(services, id="j-00000002", host_id="b-host", slurm_job_id="2", state="COMPLETED")
    assert mgr.hosts_with_active_jobs() == [HOST]
    services.store.upsert_host(sample_slurm_host(
        slurm=SlurmSettings(workdir="/data/cloud-compute", poll_seconds=10)))
    assert mgr.poll_interval(HOST) == 30
    services.store.upsert_host(sample_slurm_host(
        slurm=SlurmSettings(workdir="/data/cloud-compute", poll_seconds=120)))
    assert mgr.poll_interval(HOST) == 120


# --------------------------------------------------------------------------- cancel


def test_cancel_runs_scancel_then_refreshes(mgr, services, t, ev):
    add_job(services, id="j-00000001", slurm_job_id="7", state="RUNNING")
    t.script(["scancel", "7"], exec_result())
    script_poll(t, "", fixture("sacct_finished.txt"))
    rec = mgr.cancel("j-00000001")
    assert rec.state == "CANCELLED" and rec.notified
    assert t.argvs()[0] == ["scancel", "7"]
    assert count(t, "squeue") == 1 and count(t, "sacct") == 1
    assert [p["state"] for p in ev.payloads("job.finished")] == ["CANCELLED"]


def test_cancel_finished_job_is_noop(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="5", state="COMPLETED")
    assert mgr.cancel("j-00000001").state == "COMPLETED"
    assert t.calls == []


def test_cancel_failure(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="7", state="RUNNING")
    t.script(["scancel", "7"], exec_result(rc=1, stderr="scancel: error: Access/permission denied"))
    script_poll(t, fixture("squeue_running.txt"), fixture("sacct_running.txt"))
    with pytest.raises(ApiError) as exc:
        mgr.cancel("j-00000001")
    assert exc.value.code == "internal"
    with pytest.raises(ApiError) as exc:
        mgr.cancel("j-nothere")
    assert exc.value.code == "not_found"


# --------------------------------------------------------------------------- output


def test_output_plain_job(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="5", job_dir="/data/cloud-compute/j-00000001")
    t.script(("sh", "-c"), exec_result(stdout="@@cc dir=1 files=1 nonempty=1\nc1\n"))
    assert mgr.output("j-00000001", tail=50) == "c1\n"
    argv = t.argvs()[0]
    assert argv[-5:] == ["/data/cloud-compute/j-00000001", "5", "out", "50", ""]
    assert "tail -n" in argv[2]
    mgr.output("j-00000001", stream="err")
    assert t.argvs()[1][-5:] == ["/data/cloud-compute/j-00000001", "5", "err", "200", ""]


def test_output_array_task(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="8", job_dir="/d/j-00000001")
    t.script(("sh", "-c"), exec_result(stdout="@@cc dir=1 files=1 nonempty=1\ntask 2\n"))
    assert mgr.output("j-00000001", task=2) == "task 2\n"
    assert t.argvs()[0][-5:] == ["/d/j-00000001", "8", "out", "200", "2"]
    with pytest.raises(ApiError):
        mgr.output("j-00000001", task="2; rm")


def test_output_validation_and_errors(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="5")
    with pytest.raises(ApiError) as exc:
        mgr.output("j-00000001", stream="log")
    assert exc.value.code == "invalid"
    with pytest.raises(ApiError) as exc:
        mgr.output("j-00000001", tail=0)
    assert exc.value.code == "invalid"
    with pytest.raises(ApiError) as exc:
        mgr.output("j-missing0")
    assert exc.value.code == "not_found"
    t.script(("sh", "-c"), exec_result(rc=255, stderr="ssh: connection refused"))
    with pytest.raises(ApiError) as exc:
        mgr.output("j-00000001")
    assert exc.value.code == "host_offline"


# --------------------------------------------------------------------------- partitions


def test_partitions(mgr, t):
    out = fixture("sinfo_partitions.txt") + "==nodes\n" + fixture("sinfo_nodes.txt")
    t.script(("sh", "-c"), exec_result(stdout=out))
    parts = mgr.partitions(HOST)
    assert [p.name for p in parts] == ["cpu", "gpu"] and parts[0].default
    assert len(t.calls) == 1 and "sinfo -h" in t.argvs()[0][2]
    assert "scontrol show node -o" in t.argvs()[0][2]
    assert parts[0].mem_free_mb == 1908                    # no scontrol output: sinfo fallback


def test_partitions_free_memory_is_unallocated_memory(mgr, t):
    out = (fixture("sinfo_partitions.txt") + "==nodes\n" + fixture("sinfo_nodes.txt")
           + "==scontrol\n" + fixture("scontrol_nodes_busy.txt"))
    t.script(("sh", "-c"), exec_result(stdout=out))
    parts = mgr.partitions(HOST)
    assert parts[0].mem_total_mb == 31448 and parts[0].mem_free_mb == 31448 - 1536
    assert parts[1].mem_free_mb is None


def test_partitions_failure(mgr, t):
    t.script(("sh", "-c"), exec_result(rc=1, stderr="slurm_load_partitions: Unable to contact"))
    with pytest.raises(ApiError) as exc:
        mgr.partitions(HOST)
    assert exc.value.code == "internal"


def test_poll_converts_epoch_times_to_utc_iso(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="13", state="RUNNING")
    add_job(services, id="j-00000002", slurm_job_id="18", state="PENDING")
    script_poll(t, fixture("squeue_epoch.txt"), fixture("sacct_epoch.txt"))
    mgr.poll(HOST)
    by_id = {j.slurm_job_id: j for j in mgr.list(HOST)}
    assert by_id["13"].started_at == "2026-11-08T07:29:47Z"
    assert by_id["13"].ended_at == "2026-11-08T07:30:04Z"
    assert by_id["18"].state == "RUNNING"
    assert by_id["18"].started_at == "2026-11-08T07:35:33Z" and by_id["18"].ended_at is None


# --------------------------------------------------------------------------- no accounting / orphans


def _lost(mgr, services, t, ev, *, polls, sacct_result=None, age_s=3600):
    import datetime as dt
    submitted = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_s)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    add_job(services, id="j-00000001", slurm_job_id="5", state="RUNNING", started_at=None,
            nodes=None, submitted_at=submitted)
    t.script(is_squeue, exec_result(stdout=""))
    t.script(is_sacct, sacct_result or exec_result(
        rc=1, stderr="sacct: error: Slurm accounting storage is disabled"))
    for _ in range(polls):
        mgr.poll(HOST)
    return services.store.get_job("j-00000001")


def test_job_finishes_without_sacct(mgr, services, t, ev):
    rec = _lost(mgr, services, t, ev, polls=5)
    assert rec.state == "UNKNOWN" and rec.ended_at and rec.exit_code is None and rec.notified
    (fin,) = ev.payloads("job.finished")                       # exactly once
    assert fin["state"] == "UNKNOWN" and fin["confirmed"] is False
    (note,) = ev.payloads("notify")
    assert note["level"] == "warn" and "could not be confirmed" in note["body"]
    assert mgr.hosts_with_active_jobs() == []                  # slot freed, no more polling
    assert [j.id for j in mgr.list(HOST, active_only=True)] == []


def test_job_missing_everywhere_is_kept_until_the_third_poll(mgr, services, t, ev):
    rec = _lost(mgr, services, t, ev, polls=2)
    assert rec.state == "RUNNING" and ev.payloads("job.finished") == []
    mgr.poll(HOST)
    assert services.store.get_job("j-00000001").state == "UNKNOWN"


def test_young_job_is_not_closed_by_accounting_lag(mgr, services, t, ev):
    rec = _lost(mgr, services, t, ev, polls=6, age_s=5)
    assert rec.state == "RUNNING" and ev.payloads("job.finished") == []


def test_sighting_resets_the_missing_count(mgr, services, t, ev):
    _lost(mgr, services, t, ev, polls=2)
    t._run_rules.clear()
    t.script(is_squeue, exec_result(stdout="5|RUNNING|cpu|c1|1762586987\n"))
    t.script(is_sacct, exec_result(rc=1, stderr="sacct: error"))
    mgr.poll(HOST)
    t._run_rules.clear()
    t.script(is_squeue, exec_result(stdout=""))
    t.script(is_sacct, exec_result(rc=1, stderr="sacct: error"))
    mgr.poll(HOST)
    mgr.poll(HOST)
    assert services.store.get_job("j-00000001").state == "RUNNING"


def test_sacct_timeout_does_not_abort_the_poll(mgr, services, t, ev):
    rec = _lost(mgr, services, t, ev, polls=3, sacct_result=exec_result(rc=255, stderr="timeout"))
    assert rec.state == "UNKNOWN" and len(ev.payloads("job.finished")) == 1


def test_sacct_record_still_wins_over_unconfirmed(mgr, services, t, ev):
    rec = _lost(mgr, services, t, ev, polls=4, sacct_result=exec_result(
        stdout="5|COMPLETED|0:0|1762586987|1762586988|c1\n"))
    assert rec.state == "COMPLETED"
    (fin,) = ev.payloads("job.finished")
    assert "confirmed" not in fin


def test_unknown_with_ended_at_is_never_polled_or_cancelled_again(mgr, services, t, ev):
    _lost(mgr, services, t, ev, polls=3)
    t.reset_calls()
    assert mgr.poll(HOST) == [] and mgr.cancel("j-00000001").state == "UNKNOWN"
    assert t.calls == []


def test_suspended_unknown_without_ended_at_stays_open(mgr, services, t, ev):
    add_job(services, id="j-00000001", slurm_job_id="9", state="UNKNOWN", ended_at=None)
    assert mgr.hosts_with_active_jobs() == [HOST]


def test_orphaned_jobs_of_a_deleted_host_are_closed_once(mgr, services, t, ev, caplog):
    add_job(services, id="j-00000009", host_id="gone", slurm_job_id="9", state="RUNNING")
    add_job(services, id="j-0000000a", host_id=HOST, slurm_job_id="10", state="RUNNING")
    with caplog.at_level("WARNING"):
        assert mgr.hosts_with_active_jobs() == [HOST]
        assert mgr.hosts_with_active_jobs() == [HOST]
    rec = services.store.get_job("j-00000009")
    assert rec.state == "UNKNOWN" and rec.ended_at and rec.notified
    assert [p["ids"] for p in ev.payloads("jobs.changed")] == [["j-00000009"]]   # once
    assert ev.payloads("job.finished") == [] and not caplog.records
    assert services.store.get_job("j-0000000a").state == "RUNNING"


# --------------------------------------------------------------------------- output explained


def _marker(dir_=1, files=0, nonempty=0, text=""):
    return exec_result(stdout=f"@@cc dir={dir_} files={files} nonempty={nonempty}\n{text}")


def test_finished_job_without_output_file_is_explained_not_empty(mgr, services, t):
    """A finished job whose folder is not shared returned a bare ''."""
    add_job(services, id="j-00000001", slurm_job_id="121", state="COMPLETED",
            started_at="2026-11-09T21:53:55Z", ended_at="2026-11-09T21:53:56Z",
            job_dir="/root/cloud-compute/j-00000001")
    t.script(("sh", "-c"), _marker(files=0))
    info = mgr.output_info("j-00000001")
    assert info["status"] == "missing" and info["text"] == ""
    note = info["note"]
    assert "/root/cloud-compute/j-00000001" in note and "slurm-121.out" in note
    assert "login node" in note and "shared" in note and "Work directory" in note


def test_empty_output_file_says_the_job_printed_nothing(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="5", state="COMPLETED",
            started_at="2026-11-09T21:53:55Z", ended_at="2026-11-09T21:53:56Z")
    t.script(("sh", "-c"), _marker(files=1, nonempty=0))
    assert mgr.output_info("j-00000001") == {"text": "", "status": "empty",
                                             "note": "The job printed nothing."}
    assert mgr.output_info("j-00000001", stream="err")["status"] == "empty"


def test_pending_job_has_not_started(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="5", state="PENDING", started_at=None)
    t.script(("sh", "-c"), _marker(files=0))
    info = mgr.output_info("j-00000001")
    assert info["status"] == "not_started" and "not started" in info["note"]


def test_job_cancelled_before_it_started(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="5", state="CANCELLED", started_at=None,
            ended_at="2026-11-09T21:53:56Z")
    t.script(("sh", "-c"), _marker(files=0))
    info = mgr.output_info("j-00000001")
    assert info["status"] == "not_started" and "before it started" in info["note"]


def test_running_job_without_file_explains_and_names_the_array_pattern(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="9", state="RUNNING",
            started_at="2026-11-09T21:53:55Z", job_dir="/d/j-00000001")
    t.script(("sh", "-c"), _marker(files=0))
    info = mgr.output_info("j-00000001")
    assert info["status"] == "missing" and "yet" in info["note"]
    assert "slurm-9_<task>.out" in info["note"]
    info = mgr.output_info("j-00000001", task=3, stream="err")
    assert "slurm-9_3.err" in info["note"]


def test_missing_job_folder_is_reported(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="5", state="COMPLETED",
            started_at="2026-11-09T21:53:55Z", ended_at="2026-11-09T21:53:56Z",
            job_dir="/gone/j-00000001")
    t.script(("sh", "-c"), _marker(dir_=0, files=0))
    info = mgr.output_info("j-00000001")
    assert info["status"] == "missing" and "/gone/j-00000001 does not exist" in info["note"]


def test_output_script_really_distinguishes_missing_empty_and_present(tmp_path):
    """Run the real remote script with a local POSIX sh (skipped without one)."""
    import shutil
    import subprocess
    from cloud_compute_core.slurm import _OUTPUT_SCRIPT, _split_marker
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no POSIX sh on this computer")
    d = tmp_path / "job"
    d.mkdir()

    def run(sid, stream="out", task=""):
        res = subprocess.run([sh, "-c", _OUTPUT_SCRIPT, "sh", d.as_posix(), sid, stream, "50",
                              task], capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        return _split_marker(res.stdout.replace("\r\n", "\n"))

    assert run("1") == ("", {"dir": 1, "files": 0, "nonempty": 0})
    (d / "slurm-2.out").write_text("")
    assert run("2") == ("", {"dir": 1, "files": 1, "nonempty": 0})
    (d / "slurm-3.out").write_text("c1\n")
    assert run("3") == ("c1\n", {"dir": 1, "files": 1, "nonempty": 1})
    for n in (1, 2, 3):
        (d / f"slurm-4_{n}.out").write_text(f"task {n}\n")
    text, facts = run("4")
    assert facts == {"dir": 1, "files": 3, "nonempty": 3}
    assert all(f"task {n}" in text for n in (1, 2, 3)) and "==>" in text
    assert run("4", task="2") == ("task 2\n", {"dir": 1, "files": 1, "nonempty": 1})
    d2 = tmp_path / "nope"
    res = subprocess.run([sh, "-c", _OUTPUT_SCRIPT, "sh", d2.as_posix(), "1", "out", "5", ""],
                         capture_output=True, text=True, timeout=30)
    assert _split_marker(res.stdout)[1]["dir"] == 0


# --------------------------------------------------------------------------- delete


def test_delete_finished_job_keeps_the_cluster_folder(mgr, services, t, ev):
    add_job(services, id="j-00000001", slurm_job_id="5", state="COMPLETED",
            ended_at="2026-11-09T21:53:56Z", job_dir="/data/cloud-compute/j-00000001")
    out = mgr.delete("j-00000001")
    assert out["deleted"] == "j-00000001" and out["job_dir"] == "/data/cloud-compute/j-00000001"
    assert "/data/cloud-compute/j-00000001" in out["note"] and "not deleted" in out["note"]
    assert services.store.get_job("j-00000001") is None
    assert t.calls == []                                   # nothing runs on the cluster
    assert ev.payloads("jobs.changed")[-1] == {"ids": ["j-00000001"], "host_id": HOST}


def test_delete_refuses_an_open_job(mgr, services):
    add_job(services, id="j-00000001", slurm_job_id="5", state="RUNNING")
    with pytest.raises(ApiError) as exc:
        mgr.delete("j-00000001")
    assert exc.value.code == "conflict" and "cancel" in exc.value.message
    assert services.store.get_job("j-00000001") is not None
    with pytest.raises(ApiError) as exc:
        mgr.delete("j-nothere")
    assert exc.value.code == "not_found"


def test_clear_finished_removes_only_finished_jobs(mgr, services, t):
    add_job(services, id="j-00000001", slurm_job_id="5", state="COMPLETED",
            ended_at="2026-11-09T21:53:56Z")
    add_job(services, id="j-00000002", slurm_job_id="6", state="UNKNOWN",
            ended_at="2026-11-09T21:53:56Z")
    add_job(services, id="j-00000003", slurm_job_id="7", state="RUNNING")
    add_job(services, id="j-00000004", host_id="other", slurm_job_id="8", state="FAILED",
            ended_at="2026-11-09T21:53:56Z")
    out = mgr.clear_finished(HOST)
    assert sorted(out["deleted"]) == ["j-00000001", "j-00000002"] and out["count"] == 2
    assert "not deleted" in out["note"]
    assert [j.id for j in mgr.list()] in (["j-00000003", "j-00000004"],
                                          ["j-00000004", "j-00000003"])
    assert mgr.clear_finished()["deleted"] == ["j-00000004"]
    assert mgr.clear_finished()["count"] == 0
    assert t.calls == []


# --------------------------------------------------------------------------- partition


def test_partition_is_filled_from_sacct_when_squeue_never_saw_the_job(mgr, services, t):
    """A job on the default partition that ended between polls kept null."""
    add_job(services, id="j-00000001", slurm_job_id="132", state="PENDING", partition=None,
            started_at=None)
    t.script(is_squeue, exec_result(stdout=""))
    t.script(is_sacct, exec_result(stdout="132|COMPLETED|0:0|1762729520|1762729521|c1|cpu\n"))
    (job,) = mgr.poll(HOST)
    assert job.state == "COMPLETED" and job.partition == "cpu"
    sacct_argv = next(a for a in t.argvs() if a[:3] == ["env", TIME_ENV, "sacct"])
    assert sacct_argv[-1] == SACCT_FIELDS and SACCT_FIELDS.endswith(",Partition")


def test_six_column_sacct_still_parses():
    from cloud_compute_core.slurm import parse_sacct
    row = parse_sacct("5|COMPLETED|0:0|1762729520|1762729521|c1\n")["5"]
    assert row["partition"] is None and row["nodes"] == "c1"
