"""render_sbatch and the squeue/sacct/sinfo parsers (fixtures from the test cluster)."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from cloud_compute_core.models import JobRequest, SlurmSettings
from cloud_compute_core.slurm import (
    aggregate_state, normalize_state, parse_sacct, parse_sinfo, parse_squeue,
    poll_interval_seconds, render_sbatch, sanitize_job_name,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "slurm"
DIR = "/data/cloud-compute/j-0123abcd"
LID = "j-0123abcd"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def req(**kw) -> JobRequest:
    base = dict(host_id="slurm-x1", name="hello", command="hostname")
    base.update(kw)
    return JobRequest(**base)


def directives(script: str) -> list[str]:
    return [line for line in script.splitlines() if line.startswith("#SBATCH")]


# --------------------------------------------------------------------------- render


def test_render_spec_lines():
    script = render_sbatch(req(cpus=2, mem_gb=1.5, time_limit="01:00:00"),
                           SlurmSettings(default_partition="cpu"), DIR, LID)
    lines = script.splitlines()
    assert lines[0] == "#!/bin/bash"
    assert directives(script) == [
        "#SBATCH --job-name=cc-hello",
        f"#SBATCH --output={DIR}/slurm-%j.out",
        f"#SBATCH --error={DIR}/slurm-%j.err",
        f"#SBATCH --chdir={DIR}",
        f"#SBATCH --comment=cloud-compute:{LID}",
        "#SBATCH -p cpu",
        "#SBATCH -c 2",
        "#SBATCH --mem=1536M",
        "#SBATCH -t 01:00:00",
    ]
    assert lines[-1] == "hostname"
    assert f"export CC_JOB_ID={LID}" in lines


def test_render_partition_request_overrides_default_and_omitted_when_none():
    s = render_sbatch(req(partition="gpu"), SlurmSettings(default_partition="cpu"), DIR, LID)
    assert "#SBATCH -p gpu" in s and "#SBATCH -p cpu" not in s
    s = render_sbatch(req(), SlurmSettings(), DIR, LID)
    assert not any(line.startswith("#SBATCH -p") for line in s.splitlines())


def test_render_gpus_array_account_qos():
    s = render_sbatch(req(gpus=2, array="1-10%2"),
                      SlurmSettings(account="proj1", qos="normal"), DIR, LID)
    d = directives(s)
    assert "#SBATCH --gpus=2" in d
    assert "#SBATCH --array=1-10%2" in d
    assert "#SBATCH -A proj1" in d and "#SBATCH --qos=normal" in d
    # array jobs name output per task
    assert f"#SBATCH --output={DIR}/slurm-%A_%a.out" in d
    assert f"#SBATCH --error={DIR}/slurm-%A_%a.err" in d
    s = render_sbatch(req(), SlurmSettings(), DIR, LID)
    assert "--gpus" not in s and "--array" not in s and "-A " not in s and "--qos" not in s


def test_render_apptainer_wrap_and_quoting():
    cmd = "echo \"it's $HOME\"; python -V"
    s = render_sbatch(req(command=cmd, image="docker://docker.io/library/python:3.12-slim"),
                      SlurmSettings(), DIR, LID)
    last = s.splitlines()[-1]
    assert last.startswith("apptainer exec docker://docker.io/library/python:3.12-slim bash -lc ")
    assert shlex.split(last) == ["apptainer", "exec", "docker://docker.io/library/python:3.12-slim",
                                 "bash", "-lc", cmd]


def test_render_apptainer_gpu_uses_nv():
    s = render_sbatch(req(image="/data/img.sif", gpus=1), SlurmSettings(), DIR, LID)
    assert shlex.split(s.splitlines()[-1])[:3] == ["apptainer", "exec", "--nv"]


def test_render_image_rejected_without_apptainer():
    with pytest.raises(ValueError):
        render_sbatch(req(image="docker://x"), SlurmSettings(container="none"), DIR, LID)


def test_render_script_strips_shebang_and_cannot_add_directives():
    script = "#!/bin/sh\n#SBATCH --partition=evil\necho hi\n"
    s = render_sbatch(req(command=None, script=script), SlurmSettings(), DIR, LID)
    assert "#SBATCH --partition=evil" in s           # kept as text ...
    guard = s.splitlines().index(f"export CC_JOB_ID={LID}")
    evil = s.splitlines().index("#SBATCH --partition=evil")
    assert evil > guard                              # ... but after the first command line
    assert "#!/bin/sh" not in s


def test_render_script_with_image_runs_in_container():
    s = render_sbatch(req(command=None, script="echo a\necho b", image="docker://alpine"),
                      SlurmSettings(), DIR, LID)
    body = s.split(f"export CC_JOB_ID={LID}\n", 1)[1]
    assert shlex.split(body) == ["apptainer", "exec", "docker://alpine", "bash", "-lc",
                                 "echo a\necho b"]


@pytest.mark.parametrize("kw", [
    dict(command=None, script=None),
    dict(command="a", script="b"),
    dict(command="   "),
    dict(time_limit="1:00\n#SBATCH -p evil"),
    dict(time_limit="soon"),
    dict(array="1-3\n#SBATCH --x"),
    dict(array="1-3; rm"),
    dict(partition="cpu\n#SBATCH --qos=high"),
    dict(partition="cpu gpu"),
    dict(cpus=0), dict(cpus=1.5), dict(cpus=True),
    dict(mem_gb=0), dict(mem_gb="lots"),
    dict(gpus=-1),
    dict(image="docker://x y"), dict(image="docker://x\n#SBATCH"),
    dict(name="!!!"),
])
def test_render_rejects_invalid(kw):
    with pytest.raises(ValueError):
        render_sbatch(req(**kw), SlurmSettings(), DIR, LID)


@pytest.mark.parametrize("job_dir", ["~/cc/j-1", "/data/a b", "/data/x\n#SBATCH", "/data/%j", ""])
def test_render_rejects_bad_job_dir(job_dir):
    with pytest.raises(ValueError):
        render_sbatch(req(), SlurmSettings(), job_dir, LID)


def test_render_rejects_bad_account_and_local_id():
    with pytest.raises(ValueError):
        render_sbatch(req(), SlurmSettings(account="a\n#SBATCH"), DIR, LID)
    with pytest.raises(ValueError):
        render_sbatch(req(), SlurmSettings(), DIR, "j-bad\n")


def test_name_is_sanitized():
    assert sanitize_job_name("my job\n#SBATCH -p x") == "my-job-SBATCH-p-x"
    s = render_sbatch(req(name="my job\n#SBATCH"), SlurmSettings(), DIR, LID)
    assert "#SBATCH --job-name=cc-my-job-SBATCH" in s
    assert len([line for line in s.splitlines() if line.startswith("#SBATCH --job")]) == 1


@pytest.mark.parametrize("value", ["30", "00:30:00", "1-00:00:00", "2-12", "UNLIMITED", "10:00"])
def test_time_limit_formats(value):
    assert f"#SBATCH -t {value}" in render_sbatch(req(time_limit=value), SlurmSettings(), DIR, LID)


def test_no_directive_or_newline_injection_anywhere():
    s = render_sbatch(req(command="echo ok\n#SBATCH --qos=x", name="a\nb"),
                      SlurmSettings(), DIR, LID)
    first_cmd = next(i for i, line in enumerate(s.splitlines()[1:], 1)
                     if not line.startswith("#"))
    assert all(not line.startswith("#SBATCH --qos") for line in s.splitlines()[:first_cmd])


# --------------------------------------------------------------------------- states


@pytest.mark.parametrize("raw,state", [
    ("CANCELLED by 1000", "CANCELLED"), ("CANCELLED by 0", "CANCELLED"), ("CANCELLED+", "CANCELLED"),
    ("COMPLETED", "COMPLETED"), ("FAILED", "FAILED"), ("OUT_OF_MEMORY", "OUT_OF_MEMORY"),
    ("TIMEOUT", "TIMEOUT"), ("NODE_FAIL", "NODE_FAIL"), ("PENDING", "PENDING"), ("CD", "COMPLETED"),
    ("RUNNING", "RUNNING"), ("COMPLETING", "COMPLETING"), ("CONFIGURING", "PENDING"),
    ("SUSPENDED", "UNKNOWN"), ("WEIRD_NEW_STATE", "UNKNOWN"), ("", "UNKNOWN"),
])
def test_normalize_state(raw, state):
    assert normalize_state(raw) == state


def test_aggregate_state():
    assert aggregate_state(["COMPLETED", "RUNNING", "PENDING"]) == "RUNNING"
    assert aggregate_state(["COMPLETED", "PENDING"]) == "PENDING"
    assert aggregate_state(["COMPLETED"] * 3) == "COMPLETED"
    assert aggregate_state(["COMPLETED", "FAILED", "CANCELLED"]) == "FAILED"
    assert aggregate_state(["CANCELLED", "CANCELLED"]) == "CANCELLED"
    assert aggregate_state(["COMPLETED", "UNKNOWN"]) == "UNKNOWN"
    assert aggregate_state([]) == "UNKNOWN"


def test_poll_interval_minimum():
    assert poll_interval_seconds(SlurmSettings(poll_seconds=5)) == 30
    assert poll_interval_seconds(SlurmSettings(poll_seconds=90)) == 90
    assert poll_interval_seconds(None) == 60


# --------------------------------------------------------------------------- parsers (real fixtures)


def test_parse_squeue_real_running():
    rows = parse_squeue(fixture("squeue_running.txt"))
    assert set(rows) == {"7", "8_1", "8_[2-3%1]"}
    assert rows["7"] == {"job_id": "7", "task": None, "state": "RUNNING", "raw_state": "RUNNING",
                         "partition": "cpu", "nodes": "c1", "start": "2026-11-08T07:20:53"}
    assert rows["8_1"]["job_id"] == "8" and rows["8_1"]["task"] == "1"
    pend = rows["8_[2-3%1]"]
    assert pend["state"] == "PENDING" and pend["job_id"] == "8" and pend["task"] == "[2-3%1]"
    assert pend["nodes"] is None and pend["start"] is None


def test_parse_squeue_empty_and_garbage():
    assert parse_squeue("") == {}
    assert parse_squeue("squeue: error: something\n\n") == {}


def test_parse_sacct_real_running():
    rows = parse_sacct(fixture("sacct_running.txt"))
    assert rows["7"]["state"] == "RUNNING" and rows["7"]["end"] is None
    pend = rows["8_[2-3%1]"]
    assert pend["state"] == "PENDING" and pend["start"] is None and pend["nodes"] is None
    assert rows["6"]["state"] == "FAILED" and rows["6"]["exit_code"] == "3:0"


def test_parse_sacct_real_finished():
    rows = parse_sacct(fixture("sacct_finished.txt"))
    assert {k: v["state"] for k, v in rows.items()} == {
        "5": "COMPLETED", "6": "FAILED", "7": "CANCELLED",
        "8_1": "COMPLETED", "8_2": "COMPLETED", "8_3": "COMPLETED",
    }
    assert rows["7"]["raw_state"] == "CANCELLED by 0"
    assert rows["7"]["end"] == "2026-11-08T07:21:02"
    assert rows["8_2"]["job_id"] == "8" and rows["8_2"]["task"] == "2"
    assert rows["5"]["nodes"] == "c1" and rows["5"]["exit_code"] == "0:0"


def test_parse_sacct_skips_steps():
    rows = parse_sacct(fixture("sacct_with_steps.txt"))
    assert not any("." in k for k in rows)
    assert rows == parse_sacct(fixture("sacct_finished.txt"))
    extra = parse_sacct("9|RUNNING|0:0|2026-11-08T07:00:00|Unknown|c1\n"
                        "9.extern|RUNNING|0:0|2026-11-08T07:00:00|Unknown|c1\n"
                        "9.0|RUNNING|0:0|2026-11-08T07:00:00|Unknown|c1\n")
    assert list(extra) == ["9"]


def test_parse_sacct_unknown_state_and_none_times():
    rows = parse_sacct("10|SOMETHING_NEW|0:0|None|Unknown|None assigned\n")
    assert rows["10"]["state"] == "UNKNOWN"
    assert rows["10"]["start"] is None and rows["10"]["end"] is None and rows["10"]["nodes"] is None


def test_parse_sinfo_real():
    parts = parse_sinfo(fixture("sinfo_partitions.txt"), fixture("sinfo_nodes.txt"))
    assert [p.name for p in parts] == ["cpu", "gpu"]
    cpu, gpu = parts
    assert cpu.default and not gpu.default
    assert (cpu.nodes, cpu.cpus_idle, cpu.cpus_total, cpu.mem_total_mb) == (2, 16, 16, 31448)
    assert cpu.mem_free_mb == 1908 and cpu.gres is None and cpu.time_limit == "infinite"
    assert gpu.nodes == 0 and gpu.mem_free_mb is None


def test_parse_sinfo_scontrol_free_is_unallocated_memory():
    parts = parse_sinfo(fixture("sinfo_partitions.txt"), fixture("sinfo_nodes.txt"),
                        fixture("scontrol_nodes.txt"))
    cpu, gpu = parts
    assert cpu.mem_free_mb == 31448 and gpu.mem_free_mb is None      # idle: all unallocated
    busy = parse_sinfo(fixture("sinfo_partitions.txt"), fixture("sinfo_nodes.txt"),
                       fixture("scontrol_nodes_busy.txt"))
    assert busy[0].mem_free_mb == 31448 - 1024 - 512


def test_parse_sinfo_scontrol_tolerates_junk_and_duplicates():
    out = parse_sinfo("p*|up|infinite|1|0/4/0/4|1000|(null)\n", "",
                      "garbage\nNodeName=n1 RealMemory=1000 AllocMem=250 Partitions=p*\n"
                      "NodeName=n1 RealMemory=1000 AllocMem=999 Partitions=p\n"
                      "NodeName=n2 RealMemory=oops AllocMem=1 Partitions=p\n")
    assert out[0].mem_free_mb == 750


def test_parse_epoch_times_become_utc_iso():
    rows = parse_sacct(fixture("sacct_epoch.txt"))
    assert rows["12"]["start"] == "2026-11-08T07:29:47Z"
    assert rows["12"]["end"] == "2026-11-08T07:29:48Z"
    assert rows["15"]["end"] == "2026-11-08T07:30:18Z" and rows["15"]["state"] == "CANCELLED"
    assert rows["18"]["end"] is None
    assert parse_squeue(fixture("squeue_epoch.txt"))["18"]["start"] == "2026-11-08T07:35:33Z"


def test_parse_times_edge_values():
    row = parse_sacct("9|RUNNING|0:0|1794122987|Unknown|c1\n")["9"]
    assert row["start"] == "2026-11-08T07:29:47Z" and row["end"] is None
    row = parse_sacct("9|PENDING|0:0|None|N/A|\n")["9"]
    assert row["start"] is None and row["end"] is None
    assert parse_sacct("9|X|0:0|abc||\n")["9"]["start"] is None
    assert parse_squeue("8_[2-3%1]|PENDING|cpu||N/A\n")["8_[2-3%1]"]["start"] is None
    # legacy printed form (zone unknown) is kept as is
    assert parse_squeue("7|RUNNING|cpu|c1|2026-11-08T07:20:53\n")["7"]["start"] == "2026-11-08T07:20:53"
