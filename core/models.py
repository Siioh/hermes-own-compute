"""Data models shared by every part of Own Compute.

All models are plain dataclasses that inherit :class:`Model`, which adds
JSON-safe ``to_dict()`` and a tolerant ``from_dict()``. Times are ISO-8601 UTC
strings (see :func:`utc_now_iso`). This module uses the standard library only.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import os
import re
import threading
import types
import typing
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Union

__all__ = [
    "HostKind", "HostStatus", "DisplayState", "InstanceStatus", "JobState",
    "HOST_KINDS", "fitting_host_kinds", "HOST_STATUSES", "DISPLAY_STATES", "INSTANCE_STATUSES", "JOB_STATES",
    "TERMINAL_JOB_STATES", "ACTIVE_JOB_STATES",
    "Model", "to_jsonable",
    "SlurmSettings", "Ec2Settings", "HostLimits", "HostSpec", "GpuInfo", "Partition", "Capabilities",
    "Resources", "HostState", "VariableSpec", "TemplateSpec", "VariablePreview",
    "SendPreview", "InstanceStats", "InstanceRecord", "JobRequest", "JobRecord",
    "TunnelRecord", "TerminalLaunch", "ExecResult", "ExportRecord", "STOP_REASONS", "EXPORT_REASONS",
    "utc_now_iso", "parse_iso", "new_id", "HOST_ID_RE", "is_valid_host_id", "slugify_host_id",
]

# --------------------------------------------------------------------------- literal types

HostKind = Literal["ssh", "slurm", "local", "ec2"]
HostStatus = Literal["online", "degraded", "auth_required", "offline", "unknown"]
DisplayState = Literal["supported", "buildable", "unsupported", "unknown"]
InstanceStatus = Literal["creating", "running", "stopped", "exited", "error", "destroyed"]
JobState = Literal["PENDING", "RUNNING", "COMPLETING", "COMPLETED", "FAILED", "CANCELLED",
                   "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "UNKNOWN"]

HOST_KINDS: tuple[str, ...] = typing.get_args(HostKind)
HOST_STATUSES: tuple[str, ...] = typing.get_args(HostStatus)


def fitting_host_kinds(host_kinds: Any) -> list[str]:
    """The host kinds a template can run on: its own ``host_kinds``, plus ``ec2`` when it runs
    on ``ssh`` hosts (an EC2 machine is an ordinary SSH container host). One rule for the
    spawn check, the agent check, the template lists and the Compute page."""
    kinds = [str(k) for k in (host_kinds or [])]
    if "ssh" in kinds and "ec2" not in kinds:
        kinds.append("ec2")
    return kinds

DISPLAY_STATES: tuple[str, ...] = typing.get_args(DisplayState)
INSTANCE_STATUSES: tuple[str, ...] = typing.get_args(InstanceStatus)
JOB_STATES: tuple[str, ...] = typing.get_args(JobState)
#: Job states that will not change any more.
TERMINAL_JOB_STATES: tuple[str, ...] = (
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
)
#: Job states that are still queued or running (polled by the background loop).
ACTIVE_JOB_STATES: tuple[str, ...] = ("PENDING", "RUNNING", "COMPLETING")

# --------------------------------------------------------------------------- serialization


def to_jsonable(obj: Any) -> Any:
    """Convert ``obj`` into JSON-safe data (dict/list/str/int/float/bool/None).

    Dataclasses become dicts (recursively), lists/tuples/sets become lists,
    mapping keys become strings, enums become their value, datetimes become
    ISO-8601 strings. Anything else falls back to ``str(obj)``.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        items = [to_jsonable(v) for v in obj]
        try:
            return sorted(items)
        except TypeError:
            return items
    if isinstance(obj, enum.Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, _dt.datetime):
        if obj.tzinfo is not None:
            obj = obj.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            return obj.isoformat(timespec="seconds") + "Z"
        return obj.isoformat()
    if isinstance(obj, (_dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return str(obj)


_HINTS: dict[type, dict[str, Any]] = {}
_HINTS_LOCK = threading.Lock()


def _type_hints(cls: type) -> dict[str, Any]:
    hints = _HINTS.get(cls)
    if hints is None:
        hints = typing.get_type_hints(cls)
        with _HINTS_LOCK:
            _HINTS[cls] = hints
    return hints


_UNION_TYPES: tuple[Any, ...] = (Union,)
if hasattr(types, "UnionType"):  # Python 3.10+: ``X | Y`` at runtime
    _UNION_TYPES = (Union, types.UnionType)


def _is_model_type(tp: Any) -> bool:
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


def _convert(tp: Any, value: Any, where: str) -> Any:
    """Rebuild nested dataclasses in ``value`` according to type hint ``tp``."""
    if value is None:
        return None
    if _is_model_type(tp):
        if isinstance(value, tp):
            return value
        if not isinstance(value, Mapping):
            raise ValueError(f"{where}: expected an object for {tp.__name__}, "
                             f"got {type(value).__name__}")
        return _from_mapping(tp, value, where)
    origin = typing.get_origin(tp)
    if origin is None:
        return value
    args = typing.get_args(tp)
    if origin in _UNION_TYPES:
        candidates = [a for a in args if a is not type(None)]
        for cand in candidates:
            if _is_model_type(cand) and isinstance(value, Mapping):
                return _convert(cand, value, where)
        for cand in candidates:
            if typing.get_origin(cand) in (list, tuple) and isinstance(value, (list, tuple)):
                return _convert(cand, value, where)
            if typing.get_origin(cand) is dict and isinstance(value, Mapping):
                return _convert(cand, value, where)
        return value
    if origin is list and isinstance(value, (list, tuple)):
        item_tp = args[0] if args else Any
        return [_convert(item_tp, v, f"{where}[{i}]") for i, v in enumerate(value)]
    if origin is tuple and isinstance(value, (list, tuple)):
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_convert(args[0], v, f"{where}[{i}]") for i, v in enumerate(value))
        if args and len(args) == len(value):
            return tuple(_convert(a, v, f"{where}[{i}]")
                         for i, (a, v) in enumerate(zip(args, value)))
        return tuple(value)
    if origin is dict and isinstance(value, Mapping):
        val_tp = args[1] if len(args) == 2 else Any
        return {k: _convert(val_tp, v, f"{where}.{k}") for k, v in value.items()}
    return value


def _from_mapping(cls: type, data: Any, where: str) -> Any:
    if not isinstance(data, Mapping):
        raise ValueError(f"{where}: expected an object, got {type(data).__name__}")
    hints = _type_hints(cls)
    kwargs: dict[str, Any] = {}
    missing: list[str] = []
    for f in dataclasses.fields(cls):
        if not f.init:
            continue
        if f.name in data:
            kwargs[f.name] = _convert(hints.get(f.name, Any), data[f.name], f"{where}.{f.name}")
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            missing.append(f.name)
    if missing:
        raise ValueError(f"{where}: missing required field(s): {', '.join(missing)}")
    return cls(**kwargs)


class Model:
    """Mixin for dataclasses: JSON-safe ``to_dict()`` and tolerant ``from_dict()``.

    Use it as a base of any ``@dataclass``::

        @dataclass
        class Thing(Model):
            name: str
            tags: list[str] = field(default_factory=list)
    """

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict of all fields (nested models included)."""
        return to_jsonable(self)

    @classmethod
    def from_dict(cls, data: Any):
        """Build an instance from a dict.

        Unknown keys are ignored, missing keys with defaults use the default,
        and nested dataclasses are rebuilt from the type hints. A non-dict
        input or a missing required field raises ``ValueError``.
        """
        name = getattr(cls, "__name__", "Model")
        if not dataclasses.is_dataclass(cls):
            raise TypeError(f"{name} is not a dataclass")
        return _from_mapping(typing.cast(type, cls), data, name)


# --------------------------------------------------------------------------- models


@dataclass
class SlurmSettings(Model):
    default_partition: str | None = None
    account: str | None = None
    qos: str | None = None
    workdir: str = "~/cloud-compute"          # on the shared FS of the login node
    container: Literal["apptainer", "none"] = "apptainer"
    max_active_jobs: int = 20
    poll_seconds: int = 60                     # min 30; one squeue + one sacct per host per cycle


@dataclass
class Ec2Settings(Model):
    """An EC2 host: an AWS account (by secret NAMES only), a region, launch defaults and the
    one instance the plugin runs for the host. That instance is an ordinary SSH container host
    once it is ready (Docker is installed by its first-boot script).

    Key values are never stored: ``*_secret`` fields hold the names of Hermes secrets. The
    fields after ``ssh_cidr`` record what the plugin created (ids, and local paths of the
    private key and known-hosts files) and the machine's current state.
    """
    region: str = "us-east-1"
    access_key_secret: str = "AWS_ACCESS_KEY_ID"
    secret_key_secret: str = "AWS_SECRET_ACCESS_KEY"
    session_token_secret: str | None = "AWS_SESSION_TOKEN"   # used only when that secret is set
    instance_type: str = "t3.micro"
    image: str = "auto-al2023"                 # auto-al2023 | auto-ubuntu | ami-...
    disk_gb: int = 10
    ssh_user: str | None = None                # None: ec2-user / ubuntu from the image
    max_lifetime_minutes: int = 120            # also enforced on the instance itself
    idle_stop_minutes: int = 30                # stop the machine after this long without a
                                               # running environment; 0 = never
    auto_start: bool = False                   # start a stopped machine when a spawn needs it
    subnet_id: str | None = None
    security_group_id: str | None = None       # the user's own group; None = one is created
    ssh_cidr: str | None = None                # who may SSH in; None = this computer's address
    # --- created by the plugin (ids and local paths only) ---
    key_name: str | None = None
    key_file: str | None = None
    known_hosts_file: str | None = None
    managed_group_id: str | None = None
    ssh_cidr_used: str | None = None
    # --- the machine ---
    instance_id: str | None = None             # also the HostKeyAlias of its pinned SSH keys
    machine_state: str = "none"                # none|launching|starting|ready|stopping|stopped|
                                               # terminating|error
    machine_detail: str = ""                   # progress or error sentence
    launched_at: str | None = None
    started_at: str | None = None
    auto_stop_at: str | None = None            # when the on-instance timer stops it
    launched_type: str | None = None           # what the existing instance was launched with
    launched_disk_gb: int | None = None        # (the settings above apply to the NEXT launch)
    launch_token: str | None = None            # ClientToken of a launch not yet recorded
    account_id: str | None = None              # AWS account the machine was started in
    start_count: int = 0                       # starts of this host's machine, ever (approval cards)
    stopped_at: str | None = None              # when the machine was last stopped
    unit_lifetime_minutes: int | None = None   # lifetime last written to the machine's boot timer


@dataclass
class HostLimits(Model):
    max_instances: int = 4
    max_cpus: float | None = None
    max_memory_gb: float | None = None
    allow_agents: bool = False


@dataclass
class HostSpec(Model):
    id: str                                    # slug [a-z0-9-]{1,32}
    name: str
    kind: HostKind
    address: str                               # ssh alias or hostname; "localhost" for kind=local
    user: str | None = None
    port: int = 22
    identity_file: str | None = None           # path only, never key material
    jump_host: str | None = None
    runtime_pref: Literal["auto", "podman", "docker"] = "auto"
    exec_prefix: list[str] = field(default_factory=list)
    interactive_prefix: list[str] = field(default_factory=list)
    slurm: SlurmSettings | None = None
    ec2: Ec2Settings | None = None
    limits: HostLimits = field(default_factory=HostLimits)
    created_at: str = ""


@dataclass
class GpuInfo(Model):
    index: int
    name: str
    mem_total_mb: int
    mem_used_mb: int
    util_pct: float | None = None


@dataclass
class Partition(Model):
    name: str
    state: str
    nodes: int
    cpus_alloc: int
    cpus_idle: int
    cpus_total: int
    mem_total_mb: int
    mem_free_mb: int | None
    gres: str | None
    time_limit: str | None
    default: bool = False


@dataclass
class Capabilities(Model):
    os: str | None = None
    arch: str | None = None
    runtime: Literal["podman", "docker"] | None = None
    runtime_version: str | None = None
    rootless: bool | None = None
    containers: bool = False
    batch_jobs: bool = False
    interactive_terminal: bool = False
    port_forward: bool = False
    display: DisplayState = "unknown"
    display_reason: str = ""
    gpus: list[GpuInfo] = field(default_factory=list)
    scheduler: Literal["slurm"] | None = None
    scheduler_version: str | None = None
    apptainer_version: str | None = None
    partitions: list[Partition] = field(default_factory=list)
    # Slurm only: result of the last connection test's work-directory check. True: the
    # folder is visible from a compute node; False: it is not (jobs then print nothing
    # the login node can read); None: not checked or not conclusive.
    workdir_shared: bool | None = None
    workdir_detail: str = ""                   # that check's sentence plus its fix hint


@dataclass
class Resources(Model):                        # used vs available; None = unknown
    cpu_total: float | None
    cpu_used: float | None                     # cores (load avg or Slurm alloc)
    mem_total_mb: int | None
    mem_used_mb: int | None
    disk_path: str | None
    disk_total_gb: float | None
    disk_free_gb: float | None
    gpus: list[GpuInfo]
    source: Literal["host", "slurm", "local"]
    sampled_at: str


@dataclass
class HostState(Model):
    host_id: str
    status: HostStatus
    last_seen: str | None
    last_error: str | None
    latency_ms: float | None
    capabilities: Capabilities
    resources: Resources | None
    probed_at: str | None


@dataclass
class VariableSpec(Model):
    name: str                                  # env var name in the sandbox
    source: str                                # "literal:<value>" | "secret:<HERMES_SECRET_NAME>"
    scope: Literal["runtime", "redacted", "agent-visible"] = "redacted"
    hosts: list[str] | None = None             # None = all hosts
    description: str = ""                      # plain-language help shown next to the variable


@dataclass
class TemplateSpec(Model):
    id: str
    name: str
    description: str
    kind: Literal["container", "slurm_job"] = "container"
    image: str = "docker.io/library/python:3.12-slim"
    command: list[str] = field(default_factory=lambda: ["sleep", "infinity"])
    workdir: str = "/work"
    cpus: float = 2
    memory_gb: float = 4
    gpus: int = 0
    ttl_minutes: int | None = 240
    idle_minutes: int | None = 60
    persistent: bool = False
    network: Literal["default", "none"] = "default"
    display: bool = False
    variables: list[VariableSpec] = field(default_factory=list)
    host_kinds: list[HostKind] = field(default_factory=lambda: ["ssh", "local"])
    agent: dict | None = None                  # {enabled, model, base_url, toolsets, max_turns, budget_usd}
    builtin: bool = False
    #: Container ports published on the host's 127.0.0.1 (automatic host port); 1-65535, at most 10.
    ports: list[int] = field(default_factory=list)


@dataclass
class VariablePreview(Model):
    name: str
    source_kind: Literal["literal", "secret"]
    scope: str
    will_send: bool
    reason: str                                # e.g. "secret HF_TOKEN not set"


@dataclass
class SendPreview(Model):                      # "What will be sent" — never contains values
    template_id: str
    host_id: str
    image: str
    command: list[str]
    cpus: float
    memory_gb: float
    gpus: int
    network: str
    display: bool
    variables: list[VariablePreview]
    labels: dict[str, str]
    publish: list[str]
    warnings: list[str]


@dataclass
class InstanceStats(Model):
    cpu_pct: float | None
    mem_used_mb: int | None
    mem_limit_mb: int | None
    sampled_at: str


@dataclass
class InstanceRecord(Model):
    id: str                                    # "i-" + 8 hex
    name: str
    host_id: str
    template_id: str
    runtime: str
    container_id: str | None
    status: InstanceStatus
    created_at: str
    started_at: str | None
    expires_at: str | None
    last_activity_at: str | None
    idle_minutes: int | None
    display: bool
    display_state: DisplayState
    ports: dict[str, str]                      # {"novnc": "127.0.0.1:41234"} on the host
    stats: InstanceStats | None
    error: str | None
    session_id: str | None
    #: Why the environment is no longer running: one of :data:`STOP_REASONS`; None while running.
    stop_reason: str | None = None
    #: When the "about to expire" warning was sent (ISO UTC); None = not warned yet.
    expiry_warned_at: str | None = None
    #: The lifetime applied at spawn, in minutes (``expires_at`` minus start); None = no limit recorded.
    ttl_minutes: int | None = None


@dataclass
class JobRequest(Model):
    host_id: str
    name: str
    command: str | None = None                 # exactly one of command / script
    script: str | None = None
    image: str | None = None                   # "docker://…" or a .sif path; None = bare job
    partition: str | None = None
    cpus: int = 1
    mem_gb: float = 1
    gpus: int = 0
    time_limit: str = "00:30:00"
    array: str | None = None                   # e.g. "1-10%2"


@dataclass
class JobRecord(Model):
    id: str                                    # "j-" + 8 hex (local)
    host_id: str
    slurm_job_id: str | None
    name: str
    state: JobState
    partition: str | None
    nodes: str | None
    submitted_at: str
    started_at: str | None
    ended_at: str | None
    exit_code: str | None
    job_dir: str
    image: str | None
    session_id: str | None
    notified: bool = False


#: Values of ``InstanceRecord.stop_reason``.
STOP_REASONS: tuple[str, ...] = ("user", "idle", "expired", "host-gone", "orphan")
#: Values of ``ExportRecord.reason``.
EXPORT_REASONS: tuple[str, ...] = ("before-delete", "auto-delete", "manual", "fetch")


@dataclass
class ExportRecord(Model):
    """A copy of an environment's files saved on this computer."""
    id: str                                    # "x-" + 8 hex
    instance_id: str
    instance_name: str
    host_id: str
    source_path: str                           # path inside the environment
    local_path: str                            # where the copy lives on this computer
    bytes: int
    created_at: str                            # ISO UTC
    reason: Literal["before-delete", "auto-delete", "manual", "fetch"] = "manual"
    files: int = 0                             # regular files in the saved copy
    extracted_to: str | None = None            # folder on this computer, when also unpacked


@dataclass
class TunnelRecord(Model):
    id: str                                    # "t-" + 8 hex
    kind: Literal["display", "port"]
    instance_id: str | None
    host_id: str
    local_port: int
    remote_host: str
    remote_port: int
    pid: int | None
    url: str | None
    created_at: str
    expires_at: str
    status: Literal["starting", "open", "closed", "error"]
    error: str | None = None


@dataclass
class TerminalLaunch(Model):
    launched: bool
    method: Literal["wt", "cmd", "macos", "linux", "none"]
    command_preview: str                       # shown in the UI
    copy_command: str                          # what the UI copies on fallback
    error: str | None = None


@dataclass
class ExecResult(Model):
    rc: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False


# --------------------------------------------------------------------------- helpers


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with seconds precision and a ``Z`` suffix."""
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0, tzinfo=None)
    return now.isoformat() + "Z"


def parse_iso(s: str | None) -> _dt.datetime | None:
    """Parse an ISO-8601 string into an aware UTC datetime.

    Accepts a ``Z`` or ``+00:00`` style offset; a naive value is taken as UTC.
    Returns ``None`` for ``None``, an empty string or an unparsable value.
    """
    if not s or not isinstance(s, str):
        return None
    text = s.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        value = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc)


def new_id(prefix: str) -> str:
    """Return ``<prefix>-<8 lowercase hex>``, e.g. ``new_id("i") -> "i-ab12cd34"``."""
    return f"{prefix}-{os.urandom(4).hex()}"


HOST_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")


def is_valid_host_id(s: Any) -> bool:
    """True when ``s`` is a valid host id (``[a-z0-9-]{1,32}``)."""
    return isinstance(s, str) and HOST_ID_RE.match(s) is not None and "\n" not in s


def slugify_host_id(name: str) -> str:
    """Turn a display name into a valid host id (falls back to ``"host"``)."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug[:32].strip("-")
    return slug or "host"
