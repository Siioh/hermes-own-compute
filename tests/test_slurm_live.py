"""Live Slurm round trip on the containerized test cluster (runs only with ``CC_LIVE=1``).

The real store, event bus and SSH transport are used; commands run inside the
``slurmctld`` container through ``exec_prefix``. Jobs: ``hostname``, an
Apptainer ``python -V`` job, a 3-task array and a ``sleep 300`` job that is
cancelled, plus a ``hostname`` job on a second host whose work directory is NOT
shared with the compute nodes (its output must be explained, not empty). Polls are
at least 30 s apart. Job directories created here on the login node are removed at
the end; the copies the compute nodes made of the non-shared folder
(``NONSHARED_WORKDIR`` inside the compute-node containers) cannot be reached through
the login node and are removed by hand.

Environment:
  CC_LIVE=1                     enable these tests
  CC_LIVE_SLURM_HOST=<alias>    ssh alias of the cluster host (default ``x1carbon``)
"""

from __future__ import annotations

import os
import re
import time

import pytest

from cloud_compute_core import prober as prober_mod
from cloud_compute_core import slurm as slurm_mod
from cloud_compute_core import slurm_api
from cloud_compute_core.config import Config
from cloud_compute_core.events import EventBus
from cloud_compute_core.interfaces import Services
from cloud_compute_core.models import TERMINAL_JOB_STATES, HostSpec, JobRequest, SlurmSettings
from cloud_compute_core.slurm import MIN_POLL_SECONDS, SlurmManager
from cloud_compute_core.store import Store
from cloud_compute_core.transport import TransportFactory

pytestmark = pytest.mark.live

HOST_ID = "slurm-live"
WORKDIR = "/data/cloud-compute"
NONSHARED_ID = "slurm-live-nonshared"
#: Private to each container on the test cluster (only /data is a shared volume).
NONSHARED_WORKDIR = "/root/cc-live-nonshared"
IMAGE = "docker://docker.io/library/python:3.12-slim"
DEADLINE_S = 15 * 60


def _host() -> HostSpec:
    return HostSpec(
        id=HOST_ID, name="slurm test cluster", kind="slurm",
        address=os.environ.get("CC_LIVE_SLURM_HOST", "x1carbon"),
        exec_prefix=["podman", "exec", "-i", "-w", "/data", "slurmctld"],
        slurm=SlurmSettings(workdir=WORKDIR, default_partition="cpu", poll_seconds=30),
    )


def _nonshared_host() -> HostSpec:
    """Same cluster, work directory that only the login node sees, no default partition."""
    return HostSpec(
        id=NONSHARED_ID, name="slurm test cluster (not shared)", kind="slurm",
        address=os.environ.get("CC_LIVE_SLURM_HOST", "x1carbon"),
        exec_prefix=["podman", "exec", "-i", "-w", "/data", "slurmctld"],
        slurm=SlurmSettings(workdir=NONSHARED_WORKDIR, poll_seconds=30),
    )


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("slurm-live") / "cloud-compute.db")
    config = Config()
    services = Services(store=store, config=config, events=EventBus(store),
                        transports=TransportFactory(config),
                        providers={"slurm": slurm_mod.provide, "prober": prober_mod.provide})
    store.upsert_host(_host())
    store.upsert_host(_nonshared_host())
    mgr = services.slurm
    assert isinstance(mgr, SlurmManager)
    jobs = {}
    poll_times: list[float] = []
    try:
        jobs["hostname"] = mgr.submit(JobRequest(host_id=HOST_ID, name="live-hostname",
                                                 command="hostname"), session_id="live-test")
        jobs["apptainer"] = mgr.submit(JobRequest(host_id=HOST_ID, name="live-apptainer",
                                                  command="python -V", image=IMAGE,
                                                  time_limit="00:15:00"))
        jobs["array"] = mgr.submit(JobRequest(host_id=HOST_ID, name="live-array", array="1-3",
                                              command='echo "task $SLURM_ARRAY_TASK_ID"'))
        jobs["sleep"] = mgr.submit(JobRequest(host_id=HOST_ID, name="live-sleep",
                                              command="sleep 300"))
        jobs["nonshared"] = mgr.submit(JobRequest(host_id=NONSHARED_ID, name="live-nonshared",
                                                  command="hostname"))
        cancelled = False
        start = time.monotonic()
        while True:
            time.sleep(MIN_POLL_SECONDS)
            poll_times.append(time.monotonic())
            if not cancelled:
                # cancel() refreshes the host once: that counts as this cycle's poll
                mgr.cancel(jobs["sleep"].id)
                cancelled = True
            else:
                mgr.poll(HOST_ID)
            mgr.poll(NONSHARED_ID)
            current = {k: mgr.get(j.id) for k, j in jobs.items()}
            if all(j.state in TERMINAL_JOB_STATES for j in current.values()):
                break
            if time.monotonic() - start > DEADLINE_S:
                pytest.fail(f"jobs did not finish in time: "
                            f"{ {k: j.state for k, j in current.items()} }")
        yield {"mgr": mgr, "jobs": {k: mgr.get(j.id) for k, j in jobs.items()},
               "store": store, "poll_times": poll_times, "services": services}
    finally:
        dirs = [j.job_dir for j in jobs.values()
                if re.fullmatch(rf"({re.escape(WORKDIR)}|{re.escape(NONSHARED_WORKDIR)})"
                                rf"/j-[0-9a-f]{{8}}", j.job_dir or "")]
        if dirs:
            transport = services.transports.for_host(_host())
            res = transport.run(["sh", "-c", 'rm -rf -- "$@"', "sh", *dirs], timeout=30)
            check = transport.run(["sh", "-c", 'for d in "$@"; do [ -e "$d" ] && echo "$d"; done; true',
                                   "sh", *dirs], timeout=30)
            print(f"cleanup rc={res.rc} left={check.stdout.split()}")
        store.close()


def test_polls_are_at_least_30s_apart(live):
    times = live["poll_times"]
    assert all(b - a >= MIN_POLL_SECONDS - 0.5 for a, b in zip(times, times[1:]))


def test_hostname_job(live):
    job = live["jobs"]["hostname"]
    assert job.state == "COMPLETED" and job.exit_code == "0:0"
    assert job.session_id == "live-test" and job.notified
    out = live["mgr"].output(job.id).strip()
    assert out in ("c1", "c2"), out


def test_apptainer_job(live):
    job = live["jobs"]["apptainer"]
    mgr = live["mgr"]
    if job.state != "COMPLETED":
        err = mgr.output(job.id, stream="err")
        pytest.fail(f"apptainer job {job.slurm_job_id} ended {job.state} ({job.exit_code}): "
                    f"{err[-2000:]}")
    assert re.search(r"Python 3\.12\.\d+", mgr.output(job.id)), mgr.output(job.id)


def test_array_job(live):
    job = live["jobs"]["array"]
    mgr = live["mgr"]
    assert job.state == "COMPLETED" and job.exit_code == "0:0"
    for task in (1, 2, 3):
        assert mgr.output(job.id, task=task).strip() == f"task {task}"
    combined = mgr.output(job.id)
    assert all(f"task {n}" in combined for n in (1, 2, 3))


def test_cancelled_job(live):
    job = live["jobs"]["sleep"]
    assert job.state == "CANCELLED" and job.notified
    assert "CANCELLED" in live["mgr"].output(job.id, stream="err")


def test_job_finished_events_once(live):
    events = live["store"].events_since(0, limit=1000)
    finished = [e for e in events if e["name"] == "job.finished"]
    ids = [e["payload"]["id"] for e in finished]
    assert sorted(ids) == sorted(j.id for j in live["jobs"].values())
    notes = [e for e in events if e["name"] == "notify"]
    assert len(notes) == len(live["jobs"])


def test_job_times_are_utc_iso(live):
    iso = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
    for key, job in live["jobs"].items():
        assert job.started_at and iso.match(job.started_at), (key, job.started_at)
        assert job.ended_at and iso.match(job.ended_at), (key, job.ended_at)
        assert job.started_at <= job.ended_at
        print(f"{key}: {job.started_at} -> {job.ended_at}")
    sub = live["jobs"]["hostname"]
    assert sub.submitted_at <= sub.ended_at          # same clock/zone family as submitted_at


def test_sacct_cross_check(live):
    jobs = live["jobs"]
    transport = live["services"].transports.for_host(_host())
    ids = ",".join(j.slurm_job_id for j in jobs.values())
    res = transport.run(["sacct", "-X", "-P", "-n", "-j", ids, "-o", "JobID,State,ExitCode"],
                        timeout=30)
    assert res.rc == 0, res.stderr
    print("sacct:\n" + res.stdout)
    rows = {line.split("|")[0]: line.split("|")[1] for line in res.stdout.splitlines() if "|" in line}
    sid = {k: j.slurm_job_id for k, j in jobs.items()}
    assert rows[sid["hostname"]] == "COMPLETED"
    assert rows[sid["sleep"]].startswith("CANCELLED")
    assert [rows[f"{sid['array']}_{n}"] for n in (1, 2, 3)] == ["COMPLETED"] * 3


# --------------------------------------------------------------------------- delete, wait


def test_output_status_ok_for_shared_jobs(live):
    mgr = live["mgr"]
    for key in ("hostname", "apptainer", "array"):
        info = mgr.output_info(live["jobs"][key].id)
        assert info["status"] == "ok" and info["text"].strip() and info["note"] is None, info
        print(f"{key}: {info['text'].strip()!r}")


def test_nonshared_workdir_job_explains_the_missing_output(live):
    job = live["jobs"]["nonshared"]
    assert job.state == "COMPLETED" and job.job_dir.startswith(NONSHARED_WORKDIR + "/")
    info = live["mgr"].output_info(job.id)
    print("nonshared output:", info)
    assert info["status"] == "missing" and info["text"] == ""
    assert job.job_dir in info["note"] and "Work directory" in info["note"]
    assert f"slurm-{job.slurm_job_id}.out" in info["note"]


def test_partition_filled_on_a_host_without_default_partition(live):
    job = live["jobs"]["nonshared"]
    assert job.partition == "cpu", job.partition


def test_wait_returns_when_the_job_ends(live):
    services, mgr = live["services"], live["mgr"]
    rec = mgr.submit(JobRequest(host_id=HOST_ID, name="live-wait",
                                command="sleep 15; echo waited-ok"))
    try:
        t0 = time.monotonic()
        out = slurm_api._tool_jobs({"action": "wait", "job_id": rec.id, "wait_s": 240},
                                   services, {})
        took = time.monotonic() - t0
        print("wait:", {k: v for k, v in out.items() if k != "job"}, f"{took:.1f}s")
        assert out["finished"] is True and out["job"]["state"] == "COMPLETED"
        assert out["output"]["status"] == "ok" and "waited-ok" in out["output"]["text"]
        assert took < 120
    finally:
        services.transports.for_host(_host()).run(
            ["sh", "-c", 'rm -rf -- "$1"', "sh", rec.job_dir], timeout=30)


def test_connection_test_workdir_row(live):
    prober = live["services"].prober
    shared = {s.name: s for s in prober.test_connection(_host()).steps}["workdir"]
    print("shared:", shared)
    assert shared.ok is True and WORKDIR in shared.detail
    other = prober.test_connection(_nonshared_host())
    row = {s.name: s for s in other.steps}["workdir"]
    print("nonshared:", row, other.status)
    assert row.ok is False and NONSHARED_WORKDIR in row.detail and row.hint
    assert other.status == "degraded"
