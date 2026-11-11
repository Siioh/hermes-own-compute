"""Models: round-trips, defaults, validation and helpers."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import typing
from dataclasses import dataclass, field

import pytest

from cloud_compute_core import models as m
from cloud_compute_core.models import (
    Capabilities, ExecResult, GpuInfo, HostLimits, HostSpec, HostState, InstanceRecord,
    ExportRecord, InstanceStats, JobRecord, JobRequest, Model, Partition, Resources, SendPreview, SlurmSettings,
    TemplateSpec, TerminalLaunch, TunnelRecord, VariablePreview, VariableSpec,
)

GPU = GpuInfo(index=1, name="NVIDIA L4", mem_total_mb=23034, mem_used_mb=1200, util_pct=37.5)
PART = Partition(name="gpu", state="up", nodes=2, cpus_alloc=4, cpus_idle=12, cpus_total=16,
                 mem_total_mb=64000, mem_free_mb=None, gres="gpu:l4:1", time_limit="2-00:00:00",
                 default=True)
CAPS = Capabilities(
    os="Fedora Linux 43", arch="aarch64", runtime="docker", runtime_version="27.1", rootless=False,
    containers=True, batch_jobs=True, interactive_terminal=True, port_forward=True,
    display="supported", display_reason="noVNC ready", gpus=[GPU], scheduler="slurm",
    scheduler_version="24.11", apptainer_version="1.4", partitions=[PART],
)
RES = Resources(cpu_total=8.0, cpu_used=2.5, mem_total_mb=16000, mem_used_mb=4000,
                disk_path="/home", disk_total_gb=500.0, disk_free_gb=100.5, gpus=[GPU],
                source="slurm", sampled_at="2026-11-08T10:00:00Z")
VAR = VariableSpec(name="HF_TOKEN", source="secret:HF_TOKEN", scope="runtime", hosts=["x1carbon"])
VPREV = VariablePreview(name="HF_TOKEN", source_kind="secret", scope="runtime", will_send=False,
                        reason="secret HF_TOKEN not set")
STATS = InstanceStats(cpu_pct=12.5, mem_used_mb=300, mem_limit_mb=4096,
                      sampled_at="2026-11-08T10:00:00Z")

SAMPLES = [
    SlurmSettings(default_partition="gpu", account="lab", qos="normal", workdir="/scratch/cc",
                  container="none", max_active_jobs=5, poll_seconds=90),
    HostLimits(max_instances=2, max_cpus=4.5, max_memory_gb=8.0, allow_agents=True),
    HostSpec(id="slurm-x1", name="Slurm", kind="slurm", address="login.example", user="alice",
             port=2222, identity_file="C:/Users/a/.ssh/id_test", jump_host="bastion",
             runtime_pref="podman", exec_prefix=["podman", "exec", "-i", "c"],
             interactive_prefix=["podman", "exec", "-it", "c"],
             slurm=SlurmSettings(workdir="/data/cloud-compute", default_partition="cpu"),
             limits=HostLimits(max_instances=1, allow_agents=True),
             created_at="2026-11-08T09:00:00Z"),
    HostSpec(id="x1carbon", name="x1carbon", kind="ssh", address="x1carbon", slurm=None),
    GPU,
    PART,
    CAPS,
    RES,
    HostState(host_id="x1carbon", status="degraded", last_seen="2026-11-08T10:00:00Z",
              last_error="podman not found", latency_ms=14.2, capabilities=CAPS, resources=RES,
              probed_at="2026-11-08T10:00:01Z"),
    HostState(host_id="x1carbon", status="offline", last_seen=None, last_error=None,
              latency_ms=None, capabilities=Capabilities(), resources=None, probed_at=None),
    VAR,
    TemplateSpec(id="gpu-dev", name="GPU dev", description="CUDA", kind="slurm_job",
                 image="docker://nvidia/cuda:12.4", command=["bash", "-l"], workdir="/w", cpus=8,
                 memory_gb=32, gpus=1, ttl_minutes=None, idle_minutes=None, persistent=True,
                 network="none", display=True, variables=[VAR], host_kinds=["slurm"],
                 agent={"enabled": True, "max_turns": 10}, builtin=True),
    VPREV,
    SendPreview(template_id="python-dev", host_id="x1carbon", image="img", command=["sleep", "1"],
                cpus=2.0, memory_gb=4.0, gpus=0, network="default", display=False,
                variables=[VPREV], labels={"io.cloud-compute.managed": "1"},
                publish=["127.0.0.1::6080"], warnings=["HF_TOKEN not set"]),
    STATS,
    InstanceRecord(id="i-ab12cd34", name="dev", host_id="x1carbon", template_id="python-dev",
                   runtime="podman", container_id="abc123", status="running",
                   created_at="2026-11-08T10:00:00Z", started_at="2026-11-08T10:00:05Z",
                   expires_at="2026-11-08T14:00:00Z", last_activity_at="2026-11-08T10:30:00Z",
                   idle_minutes=30, display=True, display_state="supported",
                   ports={"novnc": "127.0.0.1:41234"}, stats=STATS, error=None,
                   session_id="sess-1"),
    InstanceRecord(id="i-00000000", name="x", host_id="h", template_id="t", runtime="docker",
                   container_id=None, status="creating", created_at="", started_at=None,
                   expires_at=None, last_activity_at=None, idle_minutes=None, display=False,
                   display_state="unknown", ports={}, stats=None, error="boom", session_id=None),
    JobRequest(host_id="slurm-x1", name="train", command=None, script="#!/bin/sh\necho hi",
               image="docker://python:3.12", partition="gpu", cpus=4, mem_gb=8.5, gpus=1,
               time_limit="01:00:00", array="1-10%2"),
    JobRecord(id="j-1a2b3c4d", host_id="slurm-x1", slurm_job_id="77", name="train",
              state="COMPLETED", partition="gpu", nodes="c[1-2]",
              submitted_at="2026-11-08T10:00:00Z", started_at="2026-11-08T10:01:00Z",
              ended_at="2026-11-08T10:05:00Z", exit_code="0:0", job_dir="/data/cc/j-1a2b3c4d",
              image=None, session_id="s", notified=True),
    TunnelRecord(id="t-5e6f7a8b", kind="port", instance_id=None, host_id="x1carbon",
                 local_port=40000, remote_host="127.0.0.1", remote_port=8888, pid=None, url=None,
                 created_at="2026-11-08T10:00:00Z", expires_at="2026-11-08T11:00:00Z",
                 status="error", error="port in use"),
    InstanceRecord(id="i-0badc0de", name="old", host_id="h", template_id="t", runtime="docker",
                   container_id="c", status="stopped", created_at="2026-11-08T10:00:00Z",
                   started_at="2026-11-08T10:00:05Z", expires_at="2026-11-08T14:00:00Z",
                   last_activity_at=None, idle_minutes=60, display=False, display_state="unknown",
                   ports={}, stats=None, error=None, session_id=None, stop_reason="expired",
                   expiry_warned_at="2026-11-08T13:45:00Z", ttl_minutes=240),
    VariableSpec(name="MODE", source="literal:fast", description="Which mode to run in"),
    ExportRecord(id="x-1a2b3c4d", instance_id="i-ab12cd34", instance_name="dev", host_id="x1carbon",
                 source_path="/work", local_path="C:/Users/a/cc-exports/dev-x-1a2b3c4d.tar.gz",
                 bytes=1048576, created_at="2026-11-08T11:00:00Z", reason="auto-delete"),
    TerminalLaunch(launched=False, method="none", command_preview="ssh -t x1carbon bash",
                   copy_command="ssh -t x1carbon bash", error="wt not found"),
    ExecResult(rc=1, stdout="out", stderr="err", duration_s=0.5, timed_out=True),
]

ALL_MODELS = [
    SlurmSettings, HostLimits, HostSpec, GpuInfo, Partition, Capabilities, Resources, HostState,
    VariableSpec, TemplateSpec, VariablePreview, SendPreview, InstanceStats, InstanceRecord,
    JobRequest, JobRecord, TunnelRecord, TerminalLaunch, ExecResult, ExportRecord,
]


def _is_json_safe(obj) -> bool:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return True
    if isinstance(obj, list):
        return all(_is_json_safe(v) for v in obj)
    if isinstance(obj, dict):
        return all(isinstance(k, str) and _is_json_safe(v) for k, v in obj.items())
    return False


def test_every_model_has_a_sample():
    assert {type(s) for s in SAMPLES} == set(ALL_MODELS)
    for cls in ALL_MODELS:
        assert issubclass(cls, Model)
        assert dataclasses.is_dataclass(cls)


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_round_trip(obj):
    data = obj.to_dict()
    assert _is_json_safe(data)
    json.dumps(data)
    again = type(obj).from_dict(data)
    assert again == obj
    # survives an actual JSON round trip, too
    assert type(obj).from_dict(json.loads(json.dumps(data))) == obj


def test_nested_types_rebuilt():
    host = HostSpec.from_dict(SAMPLES[2].to_dict())
    assert isinstance(host.slurm, SlurmSettings)
    assert isinstance(host.limits, HostLimits)
    state = HostState.from_dict(SAMPLES[8].to_dict())
    assert isinstance(state.capabilities, Capabilities)
    assert isinstance(state.capabilities.gpus[0], GpuInfo)
    assert isinstance(state.capabilities.partitions[0], Partition)
    assert isinstance(state.resources, Resources)
    assert isinstance(state.resources.gpus[0], GpuInfo)
    tpl = TemplateSpec.from_dict(SAMPLES[11].to_dict())
    assert isinstance(tpl.variables[0], VariableSpec)


def test_optional_nested_none():
    host = HostSpec.from_dict({"id": "a", "name": "a", "kind": "ssh", "address": "a", "slurm": None})
    assert host.slurm is None
    assert isinstance(host.limits, HostLimits)


def test_field_order_matches_spec():
    assert [f.name for f in dataclasses.fields(HostSpec)] == [
        "id", "name", "kind", "address", "user", "port", "identity_file", "jump_host",
        "runtime_pref", "exec_prefix", "interactive_prefix", "slurm", "ec2", "limits", "created_at"]
    assert [f.name for f in dataclasses.fields(Partition)] == [
        "name", "state", "nodes", "cpus_alloc", "cpus_idle", "cpus_total", "mem_total_mb",
        "mem_free_mb", "gres", "time_limit", "default"]
    assert [f.name for f in dataclasses.fields(InstanceRecord)] == [
        "id", "name", "host_id", "template_id", "runtime", "container_id", "status", "created_at",
        "started_at", "expires_at", "last_activity_at", "idle_minutes", "display",
        "display_state", "ports", "stats", "error", "session_id",
            "stop_reason", "expiry_warned_at", "ttl_minutes"]
    assert [f.name for f in dataclasses.fields(ExecResult)] == [
        "rc", "stdout", "stderr", "duration_s", "timed_out"]


def test_to_dict_tuples_become_lists():
    @dataclass
    class Holder(Model):
        pairs: tuple = ()
        nested: dict = field(default_factory=dict)

    data = Holder(pairs=(1, ("a", 2)), nested={"k": (GPU,)}).to_dict()
    assert data == {"pairs": [1, ["a", 2]], "nested": {"k": [GPU.to_dict()]}}


def test_to_jsonable_helper():
    out = m.to_jsonable({"hosts": [SAMPLES[3]], "t": (1, 2), "n": None})
    assert out["hosts"][0]["id"] == "x1carbon"
    assert out["t"] == [1, 2]
    json.dumps(out)


def test_unknown_keys_ignored():
    data = SAMPLES[15].to_dict()
    data["extra"] = "ignored"
    data["stats"]["also_extra"] = 1
    assert InstanceRecord.from_dict(data) == SAMPLES[15]


def test_missing_required_raises_value_error():
    with pytest.raises(ValueError, match="address"):
        HostSpec.from_dict({"id": "a", "name": "a", "kind": "ssh"})
    with pytest.raises(ValueError, match="mem_total_mb"):
        GpuInfo.from_dict({"index": 0, "name": "x"})


def test_missing_required_in_nested_raises_value_error():
    data = SAMPLES[8].to_dict()
    del data["resources"]["source"]
    with pytest.raises(ValueError, match="source"):
        HostState.from_dict(data)


@pytest.mark.parametrize("bad", [None, [], "x", 3, ("a",)])
def test_non_dict_raises_value_error(bad):
    with pytest.raises(ValueError):
        HostSpec.from_dict(bad)


def test_nested_non_dict_raises_value_error():
    with pytest.raises(ValueError):
        HostSpec.from_dict({"id": "a", "name": "a", "kind": "ssh", "address": "a", "limits": 5})


def test_missing_optional_keys_use_defaults():
    host = HostSpec.from_dict({"id": "a", "name": "A", "kind": "ssh", "address": "a"})
    assert host == HostSpec(id="a", name="A", kind="ssh", address="a")
    tpl = TemplateSpec.from_dict({"id": "t", "name": "T", "description": ""})
    assert tpl.command == ["sleep", "infinity"]


def test_defaults_match_spec():
    host = HostSpec(id="a", name="a", kind="ssh", address="a")
    assert host.port == 22
    assert host.runtime_pref == "auto"
    assert host.exec_prefix == [] and host.interactive_prefix == []
    assert host.slurm is None and host.limits == HostLimits()
    assert host.created_at == ""
    lim = HostLimits()
    assert (lim.max_instances, lim.max_cpus, lim.max_memory_gb, lim.allow_agents) == (4, None, None, False)
    s = SlurmSettings()
    assert s.workdir == "~/cloud-compute"
    assert s.poll_seconds == 60
    assert s.max_active_jobs == 20
    assert s.container == "apptainer"
    t = TemplateSpec(id="t", name="t", description="")
    assert t.kind == "container"
    assert t.image == "docker.io/library/python:3.12-slim"
    assert t.command == ["sleep", "infinity"]
    assert t.workdir == "/work"
    assert (t.cpus, t.memory_gb, t.gpus) == (2, 4, 0)
    assert (t.ttl_minutes, t.idle_minutes, t.persistent) == (240, 60, False)
    assert t.network == "default" and t.display is False
    assert t.host_kinds == ["ssh", "local"]
    assert t.agent is None and t.builtin is False
    assert VariableSpec(name="A", source="literal:1").scope == "redacted"
    assert VariableSpec(name="A", source="literal:1").hosts is None
    j = JobRequest(host_id="h", name="n")
    assert (j.cpus, j.mem_gb, j.gpus, j.time_limit) == (1, 1, 0, "00:30:00")
    c = Capabilities()
    assert c.display == "unknown" and c.containers is False and c.gpus == []
    assert ExecResult(rc=0, stdout="", stderr="", duration_s=0).timed_out is False


def test_mutable_defaults_not_shared():
    a = HostSpec(id="a", name="a", kind="ssh", address="a")
    b = HostSpec(id="b", name="b", kind="ssh", address="b")
    a.exec_prefix.append("x")
    a.limits.max_instances = 9
    assert b.exec_prefix == [] and b.limits.max_instances == 4
    t1 = TemplateSpec(id="1", name="1", description="")
    t2 = TemplateSpec(id="2", name="2", description="")
    t1.command.append("more")
    t1.host_kinds.append("slurm")
    assert t2.command == ["sleep", "infinity"] and t2.host_kinds == ["ssh", "local"]
    c1, c2 = Capabilities(), Capabilities()
    c1.gpus.append(GPU)
    assert c2.gpus == []
    d1 = TemplateSpec.from_dict({"id": "x", "name": "x", "description": ""})
    d2 = TemplateSpec.from_dict({"id": "y", "name": "y", "description": ""})
    d1.command.append("z")
    assert d2.command == ["sleep", "infinity"]


def test_literal_value_tuples():
    assert m.HOST_KINDS == ("ssh", "slurm", "local", "ec2")
    assert m.HOST_STATUSES == ("online", "degraded", "auth_required", "offline", "unknown")
    assert m.DISPLAY_STATES == ("supported", "buildable", "unsupported", "unknown")
    assert m.INSTANCE_STATUSES == ("creating", "running", "stopped", "exited", "error", "destroyed")
    assert m.JOB_STATES == ("PENDING", "RUNNING", "COMPLETING", "COMPLETED", "FAILED", "CANCELLED",
                            "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "UNKNOWN")
    assert set(m.TERMINAL_JOB_STATES) | set(m.ACTIVE_JOB_STATES) | {"UNKNOWN"} == set(m.JOB_STATES)
    assert not set(m.TERMINAL_JOB_STATES) & set(m.ACTIVE_JOB_STATES)
    assert typing.get_args(m.HostKind) == m.HOST_KINDS


def test_subclass_in_other_module_works():
    @dataclass
    class Step(Model):
        name: str
        result: ExecResult | None = None
        hosts: list[HostSpec] = field(default_factory=list)
        env: dict[str, str] = field(default_factory=dict)

    s = Step(name="probe", result=ExecResult(0, "o", "", 0.1), hosts=[SAMPLES[3]], env={"A": "1"})
    again = Step.from_dict(s.to_dict())
    assert again == s and isinstance(again.result, ExecResult) and isinstance(again.hosts[0], HostSpec)


# --------------------------------------------------------------------------- helpers


def test_new_id_format():
    for prefix in ("i", "j", "t", "a"):
        value = m.new_id(prefix)
        assert re.fullmatch(prefix + r"-[0-9a-f]{8}", value), value
    assert len({m.new_id("i") for _ in range(200)}) == 200


def test_utc_now_iso_format():
    value = m.utc_now_iso()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value), value
    parsed = m.parse_iso(value)
    now = dt.datetime.now(dt.timezone.utc)
    assert abs((now - parsed).total_seconds()) < 5


def test_parse_iso():
    a = m.parse_iso("2026-11-08T12:34:56Z")
    b = m.parse_iso("2026-11-08T12:34:56+00:00")
    assert a == b == dt.datetime(2026, 11, 8, 12, 34, 56, tzinfo=dt.timezone.utc)
    assert a.tzinfo is not None and a.utcoffset() == dt.timedelta(0)
    c = m.parse_iso("2026-11-08T07:34:56-05:00")
    assert c == a and c.utcoffset() == dt.timedelta(0)
    assert m.parse_iso("2026-11-08T12:34:56") == a  # naive -> UTC
    assert m.parse_iso(None) is None
    assert m.parse_iso("") is None
    assert m.parse_iso("not a date") is None


def test_host_id_helpers():
    assert m.HOST_ID_RE.pattern == r"^[a-z0-9-]{1,32}$"
    for good in ("x1carbon", "slurm-x1", "a", "a" * 32, "0-9"):
        assert m.is_valid_host_id(good), good
    for bad in ("", "A", "a_b", "a b", "a" * 33, "x1carbon\n", None, 5, "é"):
        assert not m.is_valid_host_id(bad), bad
    assert m.slugify_host_id("My GPU Box #2") == "my-gpu-box-2"
    assert m.slugify_host_id("  x1carbon  ") == "x1carbon"
    assert m.slugify_host_id("Café_Server") == "cafe-server"
    assert m.slugify_host_id("!!!") == "host"
    assert m.slugify_host_id("") == "host"
    long = m.slugify_host_id("a" * 31 + " b c d")
    assert m.is_valid_host_id(long) and len(long) <= 32 and not long.endswith("-")
    for name in ("My GPU Box #2", "a" * 50, "---x---", "Ünïcödé"):
        assert m.is_valid_host_id(m.slugify_host_id(name))


def test_new_fields_default_for_old_rows():
    old = InstanceRecord(id="i-00000001", name="n", host_id="h", template_id="t", runtime="docker",
                         container_id=None, status="running", created_at="", started_at=None,
                         expires_at=None, last_activity_at=None, idle_minutes=None, display=False,
                         display_state="unknown", ports={}, stats=None, error=None,
                         session_id=None).to_dict()
    for key in ("stop_reason", "expiry_warned_at", "ttl_minutes"):
        old.pop(key)
    rec = InstanceRecord.from_dict(old)
    assert (rec.stop_reason, rec.expiry_warned_at, rec.ttl_minutes) == (None, None, None)
    assert VariableSpec.from_dict({"name": "A", "source": "literal:1"}).description == ""


def test_export_record_shape_and_constants():
    assert m.STOP_REASONS == ("user", "idle", "expired", "host-gone", "orphan")
    assert m.EXPORT_REASONS == ("before-delete", "auto-delete", "manual", "fetch")
    rec = ExportRecord.from_dict({"id": "x-00000000", "instance_id": "i-1", "instance_name": "n",
                                  "host_id": "h", "source_path": "/work", "local_path": "C:/e",
                                  "bytes": 5, "created_at": "2026-11-08T11:00:00Z"})
    assert rec.reason == "manual" and re.fullmatch(r"x-[0-9a-f]{8}", m.new_id("x"))
    assert (rec.files, rec.extracted_to) == (0, None)      # old stored rows still load
    full = ExportRecord.from_dict({**rec.to_dict(), "files": 12, "extracted_to": "C:/out"})
    assert (full.files, full.extracted_to) == (12, "C:/out")
    assert ExportRecord.from_dict(full.to_dict()) == full


def test_terminal_launch_methods_include_macos_and_linux():
    import typing
    from cloud_compute_core import models as m
    hints = typing.get_type_hints(m.TerminalLaunch)
    assert set(typing.get_args(hints["method"])) == {"wt", "cmd", "macos", "linux", "none"}
    for method in ("macos", "linux"):
        t = m.TerminalLaunch(launched=True, method=method, command_preview="x", copy_command="x")
        assert m.TerminalLaunch.from_dict(t.to_dict()) == t
