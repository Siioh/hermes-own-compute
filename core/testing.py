"""Test doubles and sample data for Own Compute tests (standard library only).

- :class:`FakeTransport` / :class:`FakeTransportFactory`: scripted command results,
  every call recorded; nothing touches the network or spawns processes.
- :class:`FakeEvents`, :class:`FakeSecrets`: in-memory event bus and secret resolver.
- ``sample_*()`` factories: realistic records with ``**overrides``.
- :func:`make_services`: a :class:`Services` container wired with the fakes.
"""

from __future__ import annotations

import re
import shlex
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence, Union

from .models import (
    Capabilities, ExecResult, GpuInfo, HostSpec, HostState, InstanceRecord, InstanceStats,
    JobRecord, Partition, Resources, SlurmSettings, TemplateSpec, TunnelRecord, VariablePreview,
    VariableSpec,
)

__all__ = [
    "exec_result", "FakeCall", "FakeTransport", "FakeTransportFactory", "FakeEvents",
    "FakeSecrets", "make_services",
    "sample_host", "sample_slurm_host", "sample_local_host", "sample_capabilities",
    "sample_resources", "sample_host_state", "sample_template", "sample_instance",
    "sample_job", "sample_tunnel", "sample_slurm_capabilities", "sample_gpu", "SAMPLE_TIME",
]

#: Fixed timestamp used by the sample factories (deterministic tests).
SAMPLE_TIME = "2026-11-08T12:00:00Z"


def exec_result(rc: int = 0, stdout: str = "", stderr: str = "", duration_s: float = 0.01,
                timed_out: bool = False) -> ExecResult:
    """Shorthand for an :class:`ExecResult`."""
    return ExecResult(rc=rc, stdout=stdout, stderr=stderr, duration_s=duration_s,
                      timed_out=timed_out)


# --------------------------------------------------------------------------- transport


@dataclass
class FakeCall:
    """One recorded transport call."""

    kind: str                       # "run" | "script"
    argv: list[str] | None = None
    stdin: bytes | None = None
    script: str | None = None
    timeout: float | None = None


ResultLike = Union[ExecResult, Callable[..., ExecResult], Sequence[ExecResult]]


class _Rule:
    def __init__(self, match: Callable[..., bool], result: Any):
        self.match = match
        if isinstance(result, ExecResult) or callable(result):
            self._results: list[ExecResult] | None = None
            self._single = result
        else:
            results = list(result)
            if not results or not all(isinstance(r, ExecResult) for r in results):
                raise TypeError("result list must be a non-empty list of ExecResult")
            self._results = results
            self._single = None
        self._index = 0

    def produce(self, *args: Any) -> ExecResult:
        if self._results is not None:
            res = self._results[min(self._index, len(self._results) - 1)]
            self._index += 1
            return res
        if isinstance(self._single, ExecResult):
            return self._single
        res = self._single(*args)
        if not isinstance(res, ExecResult):
            raise TypeError(f"scripted callable returned {type(res).__name__}, not ExecResult")
        return res


def _argv_matcher(match: Any) -> Callable[[list[str], bytes | None], bool]:
    if isinstance(match, list):
        want = [str(a) for a in match]
        return lambda argv, stdin: list(argv) == want
    if isinstance(match, tuple):
        prefix = [str(a) for a in match]
        return lambda argv, stdin: list(argv[:len(prefix)]) == prefix
    if isinstance(match, (str, re.Pattern)):
        rx = re.compile(match) if isinstance(match, str) else match
        return lambda argv, stdin: rx.search(shlex.join(argv)) is not None
    if callable(match):
        return lambda argv, stdin: bool(match(argv, stdin))
    raise TypeError("match must be a list, tuple, regex string or callable")


def _script_matcher(match: Any) -> Callable[[str], bool]:
    if isinstance(match, (str, re.Pattern)):
        rx = re.compile(match) if isinstance(match, str) else match
        return lambda script: rx.search(script) is not None
    if callable(match):
        return lambda script: bool(match(script))
    raise TypeError("match must be a regex string or callable")


class FakeTransport:
    """Scripted stand-in for a Transport (see the Transport protocol).

    ``script(match, result)`` registers a rule for :meth:`run`; ``match`` is an
    exact argv list, a tuple prefix, a regex searched in ``shlex.join(argv)``
    or ``callable(argv, stdin) -> bool``. ``result`` is an ExecResult,
    ``callable(argv, stdin) -> ExecResult`` or a list of ExecResults used in
    order (the last one repeats). ``script_script(match, result)`` does the
    same for :meth:`run_script` (regex or ``callable(script)``; result
    callables receive ``script``). The first matching rule wins.

    Unmatched calls return rc 127 (or raise ``AssertionError`` when ``strict``).
    ``exec_prefix`` is not applied: tests script the argv as passed to ``run``.
    """

    def __init__(self, host: HostSpec, *, strict: bool = False, latency_ms: float | None = 1.0):
        self.host = host
        self.strict = strict
        self.latency_ms = latency_ms
        self.calls: list[FakeCall] = []
        self.tcp_checks = 0
        self.closed = False
        self._run_rules: list[_Rule] = []
        self._script_rules: list[_Rule] = []
        self._lock = threading.RLock()

    # -- scripting ---------------------------------------------------------

    def script(self, match: Any, result: ResultLike) -> "FakeTransport":
        with self._lock:
            self._run_rules.append(_Rule(_argv_matcher(match), result))
        return self

    def script_script(self, match: Any, result: ResultLike) -> "FakeTransport":
        with self._lock:
            self._script_rules.append(_Rule(_script_matcher(match), result))
        return self

    def argvs(self) -> list[list[str]]:
        """argv of every recorded ``run`` call, in order."""
        return [list(c.argv or []) for c in self.calls if c.kind == "run"]

    def scripts(self) -> list[str]:
        """Text of every recorded ``run_script`` call, in order."""
        return [c.script or "" for c in self.calls if c.kind == "script"]

    def reset_calls(self) -> None:
        with self._lock:
            self.calls.clear()

    # -- Transport protocol --------------------------------------------------

    def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: float = 60) -> ExecResult:
        argv = [str(a) for a in argv]
        with self._lock:
            self.calls.append(FakeCall(kind="run", argv=list(argv), stdin=stdin, timeout=timeout))
            for rule in self._run_rules:
                if rule.match(argv, stdin):
                    return rule.produce(argv, stdin)
        joined = shlex.join(argv)
        if self.strict:
            raise AssertionError(f"FakeTransport({self.host.id}): no script for {joined}")
        return exec_result(rc=127, stderr=f"fake: no script for {joined}")

    def run_script(self, script: str, *, timeout: float = 60) -> ExecResult:
        with self._lock:
            self.calls.append(FakeCall(kind="script", script=script, timeout=timeout))
            for rule in self._script_rules:
                if rule.match(script):
                    return rule.produce(script)
        first = (script.strip().splitlines() or [""])[0]
        if self.strict:
            raise AssertionError(f"FakeTransport({self.host.id}): no script for run_script: {first}")
        return exec_result(rc=127, stderr=f"fake: no script for run_script: {first}")

    def tcp_check(self, timeout: float = 3.0) -> float | None:
        """Return ``latency_ms`` (``None`` simulates an offline host)."""
        self.tcp_checks += 1
        return self.latency_ms

    def local_argv(self, remote_argv: list[str] | None, *, tty: bool = False,
                   forwards: Sequence[str] = ()) -> list[str]:
        argv = ["ssh"]
        if tty:
            argv.append("-t")
        for f in forwards:
            argv += ["-L", f]
        argv.append(self.host.address)
        argv += list(remote_argv or [])
        return argv

    def close(self) -> None:
        self.closed = True


class FakeTransportFactory:
    """Hands out one cached :class:`FakeTransport` per host id."""

    def __init__(self, strict: bool = False):
        self.strict = strict
        self.transports: dict[str, FakeTransport] = {}
        self.requests: list[tuple[str, bool]] = []
        self._lock = threading.Lock()

    def get(self, host_id: str) -> FakeTransport:
        """The transport for ``host_id``, created on demand (script it before use)."""
        with self._lock:
            t = self.transports.get(host_id)
            if t is None:
                placeholder = HostSpec(id=host_id, name=host_id, kind="ssh", address=host_id)
                t = self.transports[host_id] = FakeTransport(placeholder, strict=self.strict)
            return t

    def for_host(self, host: HostSpec, *, prefixed: bool = True) -> FakeTransport:
        with self._lock:
            self.requests.append((host.id, prefixed))
        t = self.get(host.id)
        t.host = host
        return t


# --------------------------------------------------------------------------- events, secrets


class FakeEvents:
    """Records emitted events in ``events`` as ``(name, payload)``; never raises."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, name: str, payload: dict) -> None:
        try:
            self.events.append((name, dict(payload or {})))
        except Exception:  # noqa: BLE001 - an event bus never raises
            self.events.append((name, {}))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def payloads(self, name: str) -> list[dict]:
        return [p for n, p in self.events if n == name]

    def clear(self) -> None:
        self.events.clear()


class FakeSecrets:
    """In-memory secret resolver with the same semantics as the real one.

    ``resolve(variables, host_id)`` returns ``(env, previews)``:
    host not in ``spec.hosts`` -> not sent ("not allowed on host <id>");
    ``literal:<v>`` -> sent; ``secret:<NAME>`` -> sent when set, else
    "secret <NAME> not set"; any other source -> "invalid source".
    Empty values count as not set.
    """

    def __init__(self, values: dict[str, str] | None = None):
        self.values: dict[str, str] = dict(values or {})

    def get(self, name: str) -> str | None:
        value = self.values.get(name)
        return value if value else None

    def is_set(self, name: str) -> bool:
        return self.get(name) is not None

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def resolve(self, variables: list[VariableSpec], host_id: str
                ) -> tuple[dict[str, str], list[VariablePreview]]:
        env: dict[str, str] = {}
        previews: list[VariablePreview] = []
        for spec in variables:
            source = spec.source or ""
            kind = "secret" if source.startswith("secret:") else "literal"
            send, reason, value = False, "", None
            if spec.hosts is not None and host_id not in spec.hosts:
                reason = f"not allowed on host {host_id}"
            elif source.startswith("literal:"):
                send, value = True, source[len("literal:"):]
            elif source.startswith("secret:") and source[len("secret:"):]:
                secret_name = source[len("secret:"):]
                value = self.get(secret_name)
                if value is None:
                    reason = f"secret {secret_name} not set"
                else:
                    send = True
            else:
                reason = "invalid source"
            if send and value is not None:
                env[spec.name] = value
            previews.append(VariablePreview(name=spec.name, source_kind=kind, scope=spec.scope,
                                            will_send=send, reason=reason))
        return env, previews

    def redact(self, text: str, values: Iterable[str]) -> str:
        out = text or ""
        for v in sorted({v for v in values if v}, key=len, reverse=True):
            out = out.replace(v, "[redacted]")
        return out


# --------------------------------------------------------------------------- samples


def _with(cls: type, defaults: dict[str, Any], overrides: dict[str, Any]) -> Any:
    return cls(**{**defaults, **overrides})


def sample_host(**overrides: Any) -> HostSpec:
    """SSH host ``x1carbon`` (rootless Podman test endpoint)."""
    return _with(HostSpec, dict(id="x1carbon", name="x1carbon", kind="ssh", address="x1carbon",
                                runtime_pref="auto", created_at=SAMPLE_TIME), overrides)


def sample_slurm_host(**overrides: Any) -> HostSpec:
    """Slurm login node reached through the ``slurmctld`` container on x1carbon."""
    return _with(HostSpec, dict(
        id="slurm-x1", name="slurm-x1", kind="slurm", address="x1carbon",
        exec_prefix=["podman", "exec", "-i", "-w", "/data", "slurmctld"],
        interactive_prefix=["podman", "exec", "-it", "-w", "/data", "slurmctld"],
        slurm=SlurmSettings(workdir="/data/cloud-compute", default_partition="cpu"),
        created_at=SAMPLE_TIME,
    ), overrides)


def sample_local_host(**overrides: Any) -> HostSpec:
    """This computer (local Docker/Podman)."""
    return _with(HostSpec, dict(id="local", name="This computer", kind="local", address="localhost",
                                created_at=SAMPLE_TIME), overrides)


def sample_capabilities(**overrides: Any) -> Capabilities:
    """Capabilities of a Fedora host with rootless Podman and no GPU."""
    return _with(Capabilities, dict(
        os="Fedora Linux 43", arch="x86_64", runtime="podman", runtime_version="5.6.2",
        rootless=True, containers=True, batch_jobs=False, interactive_terminal=True,
        port_forward=True, display="buildable", display_reason="noVNC image can be built",
    ), overrides)


def sample_slurm_capabilities(**overrides: Any) -> Capabilities:
    """Capabilities of a Slurm login node with one CPU partition."""
    return _with(Capabilities, dict(
        os="Rocky Linux 9.6", arch="x86_64", containers=False, batch_jobs=True,
        interactive_terminal=True, port_forward=False, display="unsupported",
        display_reason="no container runtime on the login node", scheduler="slurm",
        scheduler_version="24.11.5", apptainer_version="1.4.1",
        partitions=[Partition(name="cpu", state="up", nodes=2, cpus_alloc=0, cpus_idle=16,
                              cpus_total=16, mem_total_mb=31000, mem_free_mb=30000, gres=None,
                              time_limit="1-00:00:00", default=True)],
    ), overrides)


def sample_resources(**overrides: Any) -> Resources:
    return _with(Resources, dict(
        cpu_total=8.0, cpu_used=1.2, mem_total_mb=15974, mem_used_mb=6246, disk_path="/home",
        disk_total_gb=475.0, disk_free_gb=312.4, gpus=[], source="host", sampled_at=SAMPLE_TIME,
    ), overrides)


def sample_host_state(**overrides: Any) -> HostState:
    return _with(HostState, dict(
        host_id="x1carbon", status="online", last_seen=SAMPLE_TIME, last_error=None,
        latency_ms=12.5, capabilities=sample_capabilities(), resources=sample_resources(),
        probed_at=SAMPLE_TIME,
    ), overrides)


def sample_template(**overrides: Any) -> TemplateSpec:
    """The built-in ``python-dev`` template."""
    return _with(TemplateSpec, dict(
        id="python-dev", name="Python dev", description="Python 3.12 shell with a /work directory",
        builtin=True,
    ), overrides)


def sample_instance(**overrides: Any) -> InstanceRecord:
    return _with(InstanceRecord, dict(
        id="i-ab12cd34", name="python-dev-1", host_id="x1carbon", template_id="python-dev",
        runtime="podman", container_id="3f2a9c1d0b7e", status="running", created_at=SAMPLE_TIME,
        started_at=SAMPLE_TIME, expires_at="2026-11-08T16:00:00Z", last_activity_at=SAMPLE_TIME,
        idle_minutes=60, display=False, display_state="unknown", ports={},
        stats=InstanceStats(cpu_pct=3.5, mem_used_mb=120, mem_limit_mb=4096, sampled_at=SAMPLE_TIME),
        error=None, session_id=None,
    ), overrides)


def sample_job(**overrides: Any) -> JobRecord:
    return _with(JobRecord, dict(
        id="j-1a2b3c4d", host_id="slurm-x1", slurm_job_id="42", name="hello", state="RUNNING",
        partition="cpu", nodes="c1", submitted_at=SAMPLE_TIME, started_at=SAMPLE_TIME,
        ended_at=None, exit_code=None, job_dir="/data/cloud-compute/jobs/j-1a2b3c4d", image=None,
        session_id=None,
    ), overrides)


def sample_tunnel(**overrides: Any) -> TunnelRecord:
    return _with(TunnelRecord, dict(
        id="t-5e6f7a8b", kind="display", instance_id="i-ab12cd34", host_id="x1carbon",
        local_port=41234, remote_host="127.0.0.1", remote_port=6080, pid=4242,
        url="http://127.0.0.1:41234/vnc.html", created_at=SAMPLE_TIME,
        expires_at="2026-11-08T13:00:00Z", status="open",
    ), overrides)


def sample_gpu(**overrides: Any) -> GpuInfo:
    return _with(GpuInfo, dict(index=0, name="NVIDIA RTX A2000", mem_total_mb=8192,
                               mem_used_mb=512, util_pct=4.0), overrides)


# --------------------------------------------------------------------------- services


def make_services(**overrides: Any):
    """A :class:`Services` built by ``registry.build_services`` with fake
    ``events``, ``secrets`` and ``transports`` (each overridable by keyword)."""
    from .registry import build_services

    defaults: dict[str, Any] = {
        "events": FakeEvents(),
        "secrets": FakeSecrets(),
        "transports": FakeTransportFactory(),
    }
    defaults.update(overrides)
    return build_services(**defaults)
