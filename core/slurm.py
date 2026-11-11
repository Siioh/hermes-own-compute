"""Slurm batch jobs on a login node (or a containerized cluster through ``exec_prefix``).

Pure helpers:

- :func:`render_sbatch` renders ``job.sh`` from a :class:`JobRequest`;
- :func:`parse_squeue`, :func:`parse_sacct` and :func:`parse_sinfo` parse the
  scheduler's pipe-separated output.

:class:`SlurmManager` submits, tracks, cancels and reads jobs. Every host
command runs through ``services.transports.for_host(host)`` with a bounded
timeout. :meth:`SlurmManager.poll` issues one ``squeue`` and one ``sacct`` per
host per cycle and emits ``job.finished`` exactly once per terminal job.

Notes on behaviour:

- ``squeue`` is filtered with ``--me`` (the submitting uid) rather than
  ``-u $USER``: commands are passed as argv, so ``$USER`` would not expand, and
  inside containers ``USER`` is often unset.
- Array jobs write ``slurm-<array id>_<task>.out`` (``%A_%a``) instead of
  ``slurm-%j.out``, because ``%j`` is the per-task job id that Own Compute does
  not track. :meth:`SlurmManager.output` reads one task file or all of them.
- ``squeue`` and ``sacct`` run as ``env SLURM_TIME_FORMAT=%s ...`` so Slurm prints
  times as epoch seconds, which are timezone-free; they are converted to
  ISO-8601 UTC (``YYYY-MM-DDTHH:MM:SSZ``). ``Unknown``/``None``/``N/A`` become
  ``None``. The old printed form (``YYYY-MM-DDTHH:MM:SS``, cluster local time,
  zone unknown) is still accepted and kept unchanged as a best effort.

A job that leaves ``squeue`` while ``sacct`` has no record of it (accounting storage
disabled, or ``sacct`` failing) cannot be confirmed. After :data:`MISSING_POLLS` polls in a
row and :data:`MISSING_GRACE_S` seconds since submission it is closed as ``UNKNOWN`` with
``ended_at`` set and no exit code: ``UNKNOWN`` plus ``ended_at`` is the "ended, final state
could not be confirmed" marker (``UNKNOWN`` without ``ended_at`` stays an open job, e.g. a
suspended one). ``job.finished`` is emitted once for it with ``confirmed: false``.

Standard library only.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import re
import shlex
import threading
import time
from typing import Any, Iterable

from .interfaces import ApiError
from .models import (
    TERMINAL_JOB_STATES, HostSpec, JobRecord, JobRequest, Partition, SlurmSettings, new_id,
    parse_iso, utc_now_iso,
)

__all__ = [
    "SlurmManager", "provide", "render_sbatch", "parse_squeue", "parse_sacct", "parse_sinfo",
    "normalize_state", "sanitize_job_name", "poll_interval_seconds", "aggregate_state",
    "SQUEUE_FORMAT", "SACCT_FIELDS", "TIME_ENV", "MIN_POLL_SECONDS", "POLL_CALL_TIMEOUT_S",
    "MISSING_POLLS", "MISSING_GRACE_S", "is_finished",
]

log = logging.getLogger(__name__)

SQUEUE_FORMAT = "%i|%T|%P|%N|%S"
#: ``Partition`` is last so older six-column output still parses.
SACCT_FIELDS = "JobID,State,ExitCode,Start,End,NodeList,Partition"
SINFO_PART_FORMAT = "%P|%a|%l|%D|%C|%m|%G"
SINFO_NODE_FORMAT = "%N|%P|%C|%m|%e|%T"

#: Never poll a host more often than this.
MIN_POLL_SECONDS = 30
#: Timeout of each of the two poll calls; together they stay under 10 s per host.
POLL_CALL_TIMEOUT_S = 4.5
#: A job missing from squeue and sacct this many polls in a row (and for at least
#: ``MISSING_GRACE_S`` after submission) is closed as UNKNOWN (see the module docstring).
MISSING_POLLS = 3
MISSING_GRACE_S = 30.0
SUBMIT_TIMEOUT_S = 45.0
COMMAND_TIMEOUT_S = 20.0
MAX_TAIL = 5000

_ACTIVE = ("PENDING", "RUNNING", "COMPLETING")
_TERMINAL = frozenset(TERMINAL_JOB_STATES)
#: Severity used to pick one state for an array whose tasks ended differently.
_SEVERITY = ("NODE_FAIL", "OUT_OF_MEMORY", "TIMEOUT", "FAILED", "CANCELLED")

_STATE_ALIASES = {
    "PENDING": "PENDING", "PD": "PENDING", "CONFIGURING": "PENDING", "CF": "PENDING",
    "REQUEUED": "PENDING", "REQUEUE_HOLD": "PENDING", "REQUEUE_FED": "PENDING",
    "RESV_DEL_HOLD": "PENDING",
    "RUNNING": "RUNNING", "R": "RUNNING",
    "COMPLETING": "COMPLETING", "CG": "COMPLETING", "STAGE_OUT": "COMPLETING",
    "SIGNALING": "COMPLETING",
    "COMPLETED": "COMPLETED", "CD": "COMPLETED",
    "FAILED": "FAILED", "F": "FAILED",
    "CANCELLED": "CANCELLED", "CA": "CANCELLED",
    "TIMEOUT": "TIMEOUT", "TO": "TIMEOUT", "DEADLINE": "TIMEOUT", "DL": "TIMEOUT",
    "OUT_OF_MEMORY": "OUT_OF_MEMORY", "OOM": "OUT_OF_MEMORY",
    "NODE_FAIL": "NODE_FAIL", "NF": "NODE_FAIL", "BOOT_FAIL": "NODE_FAIL", "BF": "NODE_FAIL",
}

_NAME_BAD = re.compile(r"[^A-Za-z0-9_.-]+")
_ARRAY_RE = re.compile(r"^\d+(-\d+(:\d+)?)?(,\d+(-\d+(:\d+)?)?)*(%\d+)?$")
_TIME_RE = re.compile(r"^(\d+-\d+(:\d+){0,2}|\d+(:\d+){0,2}|infinite|INFINITE|unlimited|UNLIMITED)$")
_PARTITION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}(,[A-Za-z0-9_.-]{1,64})*$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_LOCAL_ID_RE = re.compile(r"^j-[0-9a-f]{8}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_./~+-]+$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9_./:@+=-]+$")
_SLURM_ID_RE = re.compile(r"^\d+$")
_TIME_VALUE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
_EPOCH_RE = re.compile(r"^\d{9,12}$")
#: Makes squeue/sacct print times as epoch seconds (timezone independent).
TIME_ENV = "SLURM_TIME_FORMAT=%s"
_NO_VALUE = frozenset({"", "unknown", "none", "n/a", "none assigned", "(null)"})


# --------------------------------------------------------------------------- validation


def sanitize_job_name(name: Any) -> str:
    """Reduce ``name`` to ``[A-Za-z0-9_.-]`` (max 64 chars); ``ValueError`` if nothing is left."""
    text = re.sub(r"-{2,}", "-", _NAME_BAD.sub("-", str(name or ""))).strip("-.")[:64].strip("-.")
    if not text:
        raise ValueError("job name must contain letters or digits")
    return text


def _check(value: str, rx: re.Pattern, what: str) -> str:
    text = str(value).strip()
    if not rx.fullmatch(text):
        raise ValueError(f"invalid {what}: {value!r}")
    return text


def _positive_int(value: Any, what: str, minimum: int = 1, maximum: int = 100000) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid {what}: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {what}: {value!r}") from None
    if not math.isfinite(number) or number != int(number) or not minimum <= number <= maximum:
        raise ValueError(f"{what} must be a whole number between {minimum} and {maximum}")
    return int(number)


def _mem_mb(mem_gb: Any) -> int:
    if isinstance(mem_gb, bool):
        raise ValueError(f"invalid mem_gb: {mem_gb!r}")
    try:
        value = float(mem_gb)
    except (TypeError, ValueError):
        raise ValueError(f"invalid mem_gb: {mem_gb!r}") from None
    if not math.isfinite(value) or value <= 0 or value > 65536:
        raise ValueError("mem_gb must be greater than 0 and at most 65536")
    return max(1, int(math.ceil(value * 1024)))


def _check_path(path: str, what: str) -> str:
    text = str(path or "").strip()
    if not text or not _PATH_RE.fullmatch(text) or "%" in text:
        raise ValueError(f"invalid {what}: {path!r} (letters, digits and _ . / ~ + - only)")
    return text.rstrip("/") or "/"


# --------------------------------------------------------------------------- render


def render_sbatch(req: JobRequest, settings: SlurmSettings, job_dir: str, local_id: str) -> str:
    """Render ``job.sh`` for ``req`` (pure). Raises ``ValueError`` on invalid input.

    Directives come first; the first non-comment line (``export CC_JOB_ID=...``)
    ends ``#SBATCH`` parsing, so nothing in the command or script can add
    directives. ``job_dir`` must be the absolute path the job runs in.
    """
    settings = settings or SlurmSettings()
    if not _LOCAL_ID_RE.fullmatch(str(local_id or "")):
        raise ValueError(f"invalid local job id: {local_id!r}")
    job_dir = _check_path(job_dir, "job directory")
    if job_dir.startswith("~"):
        raise ValueError("job directory must be absolute (Slurm does not expand ~)")
    name = sanitize_job_name(req.name)
    has_cmd = bool(req.command and str(req.command).strip())
    has_script = bool(req.script and str(req.script).strip())
    if has_cmd == has_script:
        raise ValueError("give exactly one of command or script")
    cpus = _positive_int(req.cpus, "cpus", 1, 4096)
    mem = _mem_mb(req.mem_gb)
    gpus = _positive_int(req.gpus or 0, "gpus", 0, 64)
    time_limit = _check(req.time_limit or "00:30:00", _TIME_RE, "time_limit")
    partition = req.partition if req.partition else settings.default_partition
    if partition:
        partition = _check(partition, _PARTITION_RE, "partition")
    array = _check(req.array, _ARRAY_RE, "array") if req.array else None
    image = str(req.image).strip() if req.image else None
    if image:
        if settings.container != "apptainer":
            raise ValueError("this host runs jobs without containers; remove the image")
        image = _check(image, _IMAGE_RE, "image")

    pattern = "slurm-%A_%a" if array else "slurm-%j"
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=cc-{name}",
        f"#SBATCH --output={job_dir}/{pattern}.out",
        f"#SBATCH --error={job_dir}/{pattern}.err",
        f"#SBATCH --chdir={job_dir}",
        f"#SBATCH --comment=cloud-compute:{local_id}",
    ]
    if partition:
        lines.append(f"#SBATCH -p {partition}")
    lines += [f"#SBATCH -c {cpus}", f"#SBATCH --mem={mem}M", f"#SBATCH -t {time_limit}"]
    if gpus:
        lines.append(f"#SBATCH --gpus={gpus}")
    if array:
        lines.append(f"#SBATCH --array={array}")
    if settings.account:
        lines.append(f"#SBATCH -A {_check(settings.account, _ACCOUNT_RE, 'account')}")
    if settings.qos:
        lines.append(f"#SBATCH --qos={_check(settings.qos, _ACCOUNT_RE, 'qos')}")
    lines.append(f"export CC_JOB_ID={local_id}")

    body = str(req.command if has_cmd else req.script).replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in body:
        raise ValueError("command contains a NUL byte")
    if has_script and body.startswith("#!"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    body = body.strip("\n")
    if image:
        nv = " --nv" if gpus else ""
        lines.append(f"apptainer exec{nv} {shlex.quote(image)} bash -lc {shlex.quote(body)}")
    else:
        lines.append(body)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- parsers


def normalize_state(raw: Any) -> str:
    """Map a Slurm state (``CANCELLED by 1000``, ``CD``, ``RUNNING+``) to a JobState."""
    text = str(raw or "").strip()
    if not text:
        return "UNKNOWN"
    token = text.split()[0].rstrip("+*").upper()
    return _STATE_ALIASES.get(token, "UNKNOWN")


def is_finished(job: Any) -> bool:
    """True once a job will not change again: terminal, or UNKNOWN with ``ended_at`` set."""
    state = getattr(job, "state", None)
    return state in _TERMINAL or (state == "UNKNOWN" and bool(getattr(job, "ended_at", None)))


def _value(text: str) -> str | None:
    text = (text or "").strip()
    return None if text.lower() in _NO_VALUE else text


def _time(text: str) -> str | None:
    value = _value(text)
    if value is None:
        return None
    if _EPOCH_RE.match(value):
        try:
            moment = _dt.datetime.fromtimestamp(int(value), _dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return moment.replace(tzinfo=None).isoformat() + "Z"
    if not _TIME_VALUE_RE.match(value):
        return None
    return value


def _split_id(job_id: str) -> tuple[str, str | None]:
    base, sep, task = job_id.partition("_")
    return base, (task if sep else None)


def parse_squeue(out: str) -> dict[str, dict]:
    """Parse ``squeue -h -o '%i|%T|%P|%N|%S'``.

    Keys are the printed ids (``7``, ``8_1``, ``8_[2-3%1]``); values hold
    ``job_id`` (array or plain id), ``task``, ``state``, ``partition``,
    ``nodes`` and ``start``.
    """
    result: dict[str, dict] = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        parts += [""] * (5 - len(parts))
        job_id, state, partition, nodes, start = (p.strip() for p in parts[:5])
        if not job_id or "." in job_id or not job_id[0].isdigit():
            continue
        base, task = _split_id(job_id)
        result[job_id] = {
            "job_id": base, "task": task, "state": normalize_state(state),
            "raw_state": state, "partition": _value(partition), "nodes": _value(nodes),
            "start": _time(start),
        }
    return result


def parse_sacct(out: str) -> dict[str, dict]:
    """Parse ``sacct -X -P -n -o JobID,State,ExitCode,Start,End,NodeList,Partition``.

    Job steps (``.batch``, ``.extern``, ``.0``) are skipped. Values hold
    ``job_id``, ``task``, ``state``, ``exit_code``, ``start``, ``end``, ``nodes`` and
    ``partition`` (``None`` when the column is absent, as in six-column output).
    """
    result: dict[str, dict] = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        parts += [""] * (7 - len(parts))
        job_id, state, exit_code, start, end, nodes, partition = (p.strip() for p in parts[:7])
        if not job_id or "." in job_id or not job_id[0].isdigit():
            continue
        base, task = _split_id(job_id)
        result[job_id] = {
            "job_id": base, "task": task, "state": normalize_state(state), "raw_state": state,
            "exit_code": _value(exit_code), "start": _time(start), "end": _time(end),
            "nodes": _value(nodes), "partition": _value(partition),
        }
    return result


def _int(text: str) -> int | None:
    try:
        return int(str(text).strip().rstrip("+"))
    except (TypeError, ValueError):
        return None


_SCN_KEYS = re.compile(r"(?:^|\s)(NodeName|RealMemory|AllocMem|Partitions)=(\S*)")


def _unallocated_by_partition(scontrol_out: str) -> dict[str, int]:
    """Configured minus allocated memory (MB) per partition from ``scontrol show node -o``."""
    result: dict[str, int] = {}
    seen: set[str] = set()
    for line in (scontrol_out or "").splitlines():
        found = dict(_SCN_KEYS.findall(line))
        name, real, alloc = found.get("NodeName"), _int(found.get("RealMemory")), _int(found.get("AllocMem"))
        if not name or name in seen or real is None or alloc is None:
            continue
        seen.add(name)
        for part in found.get("Partitions", "").split(","):
            part = part.strip().rstrip("*")
            if part:
                result[part] = result.get(part, 0) + max(0, real - alloc)
    return result


def parse_sinfo(part_out: str, node_out: str = "", scontrol_out: str = "") -> list[Partition]:
    """Parse ``sinfo -h -o '%P|%a|%l|%D|%C|%m|%G'`` plus optional per-node output.

    ``Partition.mem_free_mb`` is the memory Slurm has not handed out to jobs
    (``RealMemory - AllocMem``, from ``scontrol show node -o``) when ``scontrol_out`` is given;
    otherwise it falls back to the sum of the OS ``FreeMem`` column of ``node_out``.
    """
    free: dict[str, int] = {}
    seen_nodes: set[tuple[str, str]] = set()
    for line in (node_out or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) < 6:
            continue
        node, part = parts[0].strip(), parts[1].strip().rstrip("*")
        mb = _int(parts[4])
        if mb is None or (node, part) in seen_nodes:
            continue
        seen_nodes.add((node, part))
        free[part] = free.get(part, 0) + mb
    unallocated = _unallocated_by_partition(scontrol_out)
    if unallocated:
        free = unallocated
    partitions: list[Partition] = []
    for line in (part_out or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) < 7:
            continue
        pname, avail, limit, count, cpus, mem, gres = (p.strip() for p in parts[:7])
        if not pname:
            continue
        raw = [_int(x) for x in cpus.split("/")]
        nums = [n or 0 for n in raw] if len(raw) == 4 and None not in raw else [0, 0, 0, 0]
        nodes = _int(count) or 0
        is_default = pname.endswith("*")
        pname = pname.rstrip("*")
        partitions.append(Partition(
            name=pname, state=avail, nodes=nodes, cpus_alloc=nums[0], cpus_idle=nums[1],
            cpus_total=nums[3], mem_total_mb=(_int(mem) or 0) * nodes,
            mem_free_mb=free.get(pname), gres=_value(gres), time_limit=_value(limit),
            default=is_default,
        ))
    return partitions


def aggregate_state(states: Iterable[str]) -> str:
    """One state for a job from the states of its array tasks."""
    states = list(states)
    if not states:
        return "UNKNOWN"
    if any(s in ("RUNNING", "COMPLETING") for s in states):
        return "RUNNING"
    if "PENDING" in states:
        return "PENDING"
    if "UNKNOWN" in states or any(s not in _TERMINAL for s in states):
        return "UNKNOWN"
    if all(s == "COMPLETED" for s in states):
        return "COMPLETED"
    for state in _SEVERITY:
        if state in states:
            return state
    return "UNKNOWN"


def poll_interval_seconds(settings: SlurmSettings | None) -> float:
    """Seconds between polls of a host: ``max(30, settings.poll_seconds)``."""
    seconds = getattr(settings, "poll_seconds", None)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        seconds = SlurmSettings().poll_seconds
    return float(max(MIN_POLL_SECONDS, seconds))


# --------------------------------------------------------------------------- output helpers

#: Lists a job's output files on the login node and tails them. The first line is a
#: marker ``@@cc dir=<0|1> files=<n> nonempty=<n>`` so "no file" and "empty file" differ.
#: Args: job dir, Slurm id, stream, lines, array task ('' for every file).
_OUTPUT_SCRIPT = (
    'd="$1"; id="$2"; s="$3"; n="$4"; t="$5"; set --; '
    'if [ -d "$d" ]; then dir=1; else dir=0; fi; '
    'if [ -n "$t" ]; then f="$d/slurm-${id}_$t.$s"; [ -e "$f" ] && set -- "$f"; '
    'else for f in "$d/slurm-$id.$s" "$d/slurm-${id}_"*."$s"; do '
    '[ -e "$f" ] && set -- "$@" "$f"; done; fi; '
    'full=0; for f in "$@"; do [ -s "$f" ] && full=$((full+1)); done; '
    'echo "@@cc dir=$dir files=$# nonempty=$full"; '
    '[ $# -gt 0 ] || exit 0; tail -n "$n" -- "$@"'
)
_MARKER_RE = re.compile(r"^@@cc dir=(\d) files=(\d+) nonempty=(\d+)\s*$")

_SHARED_HINT = ("The job's folder may not be on storage shared with the compute nodes: set "
                "this host's Work directory to a shared folder.")
_EMPTY_NOTE = {"out": "The job printed nothing.",
               "err": "The job wrote nothing to its error stream."}


def _split_marker(stdout: str) -> tuple[str, dict[str, int]]:
    """Split the :data:`_OUTPUT_SCRIPT` marker line from the output text."""
    first, sep, rest = stdout.partition("\n")
    match = _MARKER_RE.match(first.rstrip("\r"))
    if not match:
        return stdout, {}
    return (rest if sep else ""), {"dir": int(match.group(1)), "files": int(match.group(2)),
                                   "nonempty": int(match.group(3))}


def _removed_note(jobs: list[JobRecord], host_label: str | None) -> str:
    where = f" on {host_label}" if host_label else " on the cluster"
    if len(jobs) == 1:
        return (f"Removed job {jobs[0].id} from this computer's job list only. Its folder "
                f"{jobs[0].job_dir}{where} was not deleted.")
    return (f"Removed {len(jobs)} finished jobs from this computer's job list only. Their "
            f"folders{where} were not deleted.")


# --------------------------------------------------------------------------- manager


def _is_unreachable(res: Any) -> bool:
    return bool(getattr(res, "timed_out", False)) or getattr(res, "rc", 0) == 255


def _short(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _lines(text: str, limit: int = 400) -> str:
    """The lines of a command's error text, one per line (not run together)."""
    out = "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())
    return out if len(out) <= limit else out[:limit - 3] + "..."


_TASK_HEADER_RE = re.compile(r"^==> .*slurm-\d+_(\d+)\.(?:out|err) <==\s*$", re.MULTILINE)


def label_tasks(text: str) -> str:
    """Array-job output has one ``==> file <==`` header per task file: say ``Task N`` instead."""
    return _TASK_HEADER_RE.sub(lambda m: f"Task {m.group(1)}", text or "")


class SlurmManager:
    """Submit, track, cancel and read Slurm jobs; see the module docstring."""

    def __init__(self, services: Any):
        self.services = services
        self._lock = threading.RLock()
        self._missing: dict[str, int] = {}     # job id -> polls with no squeue/sacct record

    # -- helpers -------------------------------------------------------------

    @property
    def _store(self) -> Any:
        return self.services.store

    def _emit(self, name: str, payload: dict) -> None:
        try:
            self.services.events.emit(name, payload)
        except Exception as exc:  # noqa: BLE001 - events never break job handling
            log.warning("could not emit %s: %s", name, exc)

    def _notify_enabled(self) -> bool:
        try:
            return bool(getattr(self.services.config, "notify_job_complete", True))
        except Exception:  # noqa: BLE001 - config unavailable: default on
            return True

    def _host(self, host_id: str) -> tuple[HostSpec, SlurmSettings]:
        host = self._store.get_host(host_id) if host_id else None
        if host is None:
            raise ApiError(404, "not_found", f"host {host_id!r} not found")
        if host.kind != "slurm":
            raise ApiError(400, "invalid", f"host {host_id!r} is not a Slurm host")
        return host, host.slurm or SlurmSettings()

    def _transport(self, host: HostSpec) -> Any:
        return self.services.transports.for_host(host)

    def _run(self, host: HostSpec, argv: list[str], *, timeout: float,
             stdin: bytes | None = None, what: str) -> Any:
        try:
            res = self._transport(host).run(argv, stdin=stdin, timeout=timeout)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiError(503, "host_offline", f"{what} on {host.id} failed: {exc}") from exc
        if _is_unreachable(res):
            detail = "timed out" if res.timed_out else _short(res.stderr) or "connection failed"
            raise ApiError(503, "host_offline", f"{what} on {host.id}: {detail}")
        return res

    def _job(self, job_id: str) -> JobRecord:
        rec = self._store.get_job(job_id) if job_id else None
        if rec is None:
            raise ApiError(404, "not_found", f"job {job_id!r} not found")
        return rec

    def _open_jobs(self, host_id: str | None = None) -> list[JobRecord]:
        return [j for j in self._store.list_jobs(host_id)
                if not is_finished(j) and j.slurm_job_id]

    # -- public API ----------------------------------------------------------

    def partitions(self, host_id: str) -> list[Partition]:
        """Partitions from ``sinfo`` (one remote call)."""
        host, _ = self._host(host_id)
        script = (f"sinfo -h -o {shlex.quote(SINFO_PART_FORMAT)} && echo '==nodes' && "
                  f"sinfo -h -N -o {shlex.quote(SINFO_NODE_FORMAT)} && "
                  "{ echo '==scontrol'; scontrol show node -o 2>/dev/null || true; }")
        res = self._run(host, ["sh", "-c", script], timeout=COMMAND_TIMEOUT_S, what="sinfo")
        if res.rc != 0:
            raise ApiError(502, "internal", f"sinfo failed on {host.id}: {_short(res.stderr)}")
        part_out, _, rest = res.stdout.partition("==nodes")
        node_out, _, scontrol_out = rest.partition("==scontrol")
        return parse_sinfo(part_out, node_out, scontrol_out)

    def submit(self, req: JobRequest, *, session_id: str | None = None) -> JobRecord:
        """Write ``job.sh`` into a fresh job directory and ``sbatch --parsable`` it."""
        if not isinstance(req, JobRequest):
            raise ApiError(400, "invalid", "a job request is required")
        host, settings = self._host(req.host_id)
        limit = int(settings.max_active_jobs or 0)
        if limit > 0 and len(self._open_jobs(host.id)) >= limit:
            raise ApiError(403, "limit_reached",
                           f"host {host.id} already has {limit} active jobs (max_active_jobs)")
        local_id = new_id("j")
        try:
            workdir = _check_path(settings.workdir or SlurmSettings().workdir, "slurm workdir")
            # Validate everything before touching the host (placeholder dir for ~ paths).
            render_sbatch(req, settings, "/" + local_id if workdir.startswith("~") else
                          f"{workdir}/{local_id}", local_id)
        except ValueError as exc:
            raise ApiError(400, "invalid", str(exc)) from None

        job_dir = f"{workdir}/{local_id}"
        if job_dir.startswith("~"):
            job_dir = self._resolve_home_dir(host, job_dir)
        script = render_sbatch(req, settings, job_dir, local_id)
        shell = ('umask 022 && mkdir -p -- "$1" && cat > "$1/job.sh" && cd -- "$1" && '
                 'sbatch --parsable job.sh')
        res = self._run(host, ["sh", "-c", shell, "sh", job_dir], stdin=script.encode("utf-8"),
                        timeout=SUBMIT_TIMEOUT_S, what="sbatch")
        first = (res.stdout.strip().splitlines() or [""])[-1].split(";")[0].strip()
        if res.rc != 0 or not _SLURM_ID_RE.fullmatch(first):
            msg = _lines(res.stderr) or _lines(res.stdout) or f"exit code {res.rc}"
            sep = "\n" if "\n" in msg else " "           # one error per line
            raise ApiError(400 if "sbatch: error" in (res.stderr or "") else 502,
                           "invalid" if "sbatch: error" in (res.stderr or "") else "internal",
                           f"sbatch failed on {host.id}:{sep}{msg}")
        partition = req.partition or settings.default_partition
        rec = JobRecord(
            id=local_id, host_id=host.id, slurm_job_id=first, name=sanitize_job_name(req.name),
            state="PENDING", partition=partition or None, nodes=None, submitted_at=utc_now_iso(),
            started_at=None, ended_at=None, exit_code=None, job_dir=job_dir,
            image=(req.image or None), session_id=session_id,
        )
        self._store.upsert_job(rec)
        self._emit("jobs.changed", {"ids": [rec.id], "host_id": host.id})
        return rec

    def _resolve_home_dir(self, host: HostSpec, path: str) -> str:
        rel = path[1:].lstrip("/")
        res = self._run(host, ["sh", "-c", 'mkdir -p -- "$HOME/$1" && cd -- "$HOME/$1" && pwd -P',
                               "sh", rel], timeout=COMMAND_TIMEOUT_S, what="mkdir")
        resolved = (res.stdout.strip().splitlines() or [""])[-1].strip()
        if res.rc != 0 or not resolved.startswith("/"):
            raise ApiError(502, "internal",
                           f"could not create {path} on {host.id}: {_short(res.stderr)}")
        try:
            return _check_path(resolved, "job directory")
        except ValueError as exc:
            raise ApiError(400, "invalid", f"{exc}; set an absolute slurm workdir") from None

    def list(self, host_id: str | None = None, active_only: bool = False) -> list[JobRecord]:
        """Stored jobs, newest first; ``active_only`` keeps jobs that have not ended."""
        jobs = self._store.list_jobs(host_id)
        if active_only:
            jobs = [j for j in jobs if not is_finished(j)]
        return sorted(jobs, key=lambda j: j.submitted_at or "", reverse=True)

    def get(self, job_id: str) -> JobRecord:
        return self._job(job_id)

    def cancel(self, job_id: str) -> JobRecord:
        """``scancel`` the job, then refresh the host's job states once."""
        rec = self._job(job_id)
        if is_finished(rec) or not rec.slurm_job_id:
            return rec
        host, _ = self._host(rec.host_id)
        res = self._run(host, ["scancel", rec.slurm_job_id], timeout=COMMAND_TIMEOUT_S,
                        what="scancel")
        if res.rc != 0:
            log.info("scancel %s on %s: rc=%s %s", rec.slurm_job_id, host.id, res.rc,
                     _short(res.stderr))
        try:
            self.poll(host.id)
        except ApiError as exc:
            log.info("refresh after cancel of %s failed: %s", job_id, exc.message)
        rec = self._job(job_id)
        if res.rc != 0 and not is_finished(rec):
            raise ApiError(502, "internal", f"scancel failed on {host.id}: {_short(res.stderr)}")
        return rec

    def output(self, job_id: str, *, tail: int = 200, stream: str = "out",
               task: str | int | None = None) -> str:
        """Last ``tail`` lines of the job's stdout or stderr (text only; see :meth:`output_info`)."""
        return self.output_info(job_id, tail=tail, stream=stream, task=task)["text"]

    def output_info(self, job_id: str, *, tail: int = 200, stream: str = "out",
                    task: str | int | None = None) -> dict:
        """Last ``tail`` lines of stdout (``stream='out'``) or stderr (``'err'``), explained.

        Returns ``{"text", "status", "note"}``. ``status`` is ``ok`` (output found),
        ``empty`` (the file exists and is empty: the job printed nothing),
        ``not_started`` (the job has not run, so there is no file yet) or ``missing``
        (the job ran but there is no output file on the login node, usually because
        the job folder is not on storage shared with the compute nodes). ``note`` is a
        sentence for the user, ``None`` when there is output.

        Array jobs: ``task`` selects ``slurm-<id>_<task>.<stream>``; without it all
        task files are shown, each with a ``==> file <==`` header.
        """
        rec = self._job(job_id)
        if stream not in ("out", "err"):
            raise ApiError(400, "invalid", "stream must be 'out' or 'err'")
        try:
            lines = _positive_int(tail, "tail", 1, MAX_TAIL)
        except ValueError as exc:
            raise ApiError(400, "invalid", str(exc)) from None
        task_text = ""
        if task is not None and str(task).strip() != "":
            task_text = str(task).strip()
            if not task_text.isdigit():
                raise ApiError(400, "invalid", f"invalid array task: {task!r}")
        if not rec.slurm_job_id:
            return {"text": "", "status": "not_started",
                    "note": "The job was never handed to Slurm, so it has no output."}
        host, _ = self._host(rec.host_id)
        sid = rec.slurm_job_id
        res = self._run(host, ["sh", "-c", _OUTPUT_SCRIPT, "sh", rec.job_dir, sid, stream,
                               str(lines), task_text],
                        timeout=COMMAND_TIMEOUT_S, what="tail")
        if res.rc != 0:
            raise ApiError(502, "internal", f"could not read output: {_short(res.stderr)}")
        text, facts = _split_marker(res.stdout or "")
        text = label_tasks(text)
        files, nonempty, has_dir = facts.get("files"), facts.get("nonempty"), facts.get("dir")
        if files is None:                     # no marker (unexpected shell): plain text
            return {"text": text, "status": "ok" if text else "empty",
                    "note": None if text else _EMPTY_NOTE[stream]}
        if files > 0:
            if nonempty == 0 and not text.strip():
                return {"text": text, "status": "empty", "note": _EMPTY_NOTE[stream]}
            return {"text": text, "status": "ok", "note": None}
        wanted = (f"slurm-{sid}_{task_text}.{stream}" if task_text
                  else f"slurm-{sid}.{stream} or slurm-{sid}_<task>.{stream}")
        if rec.state == "PENDING" or (is_finished(rec) and not rec.started_at
                                      and rec.state in ("CANCELLED", "UNKNOWN")):
            note = ("The job has not started yet, so there is no output."
                    if rec.state == "PENDING" else
                    "The job ended before it started, so it has no output.")
            return {"text": "", "status": "not_started", "note": note}
        if has_dir == 0:
            note = (f"The job folder {rec.job_dir} does not exist on the login node, so its "
                    "output cannot be read (the folder may have been deleted).")
        elif is_finished(rec):
            note = (f"No output file ({wanted}) in {rec.job_dir} on the login node. "
                    + _SHARED_HINT)
        else:
            note = (f"No output file ({wanted}) in {rec.job_dir} on the login node yet. Slurm "
                    "creates it when the job starts; if it does not appear while the job "
                    "runs: " + _SHARED_HINT[0].lower() + _SHARED_HINT[1:])
        return {"text": "", "status": "missing", "note": note}

    def delete(self, job_id: str) -> dict:
        """Remove a finished job's local record; the job folder on the cluster stays.

        Returns ``{"deleted", "host_id", "job_dir", "note"}``. A job that has not
        finished is refused with ``conflict`` (cancel it first).
        """
        with self._lock:
            rec = self._job(job_id)
            if not is_finished(rec) and rec.slurm_job_id:
                raise ApiError(409, "conflict",
                               f"job {job_id} has not finished ({rec.state}); cancel it first")
            self._store.delete_job(rec.id)
            self._missing.pop(rec.id, None)
        self._emit("jobs.changed", {"ids": [rec.id], "host_id": rec.host_id})
        return {"deleted": rec.id, "host_id": rec.host_id, "job_dir": rec.job_dir,
                "note": _removed_note([rec], self._host_label(rec.host_id))}

    def clear_finished(self, host_id: str | None = None) -> dict:
        """Remove the local records of every finished job (optionally of one host).

        Returns ``{"deleted": [ids], "count", "job_dirs", "note"}``; nothing on the
        cluster is touched.
        """
        removed: list[JobRecord] = []
        with self._lock:
            for job in self._store.list_jobs(host_id):
                if is_finished(job) or not job.slurm_job_id:
                    self._store.delete_job(job.id)
                    self._missing.pop(job.id, None)
                    removed.append(job)
        by_host: dict[str, list[str]] = {}
        for job in removed:
            by_host.setdefault(job.host_id, []).append(job.id)
        for hid, ids in by_host.items():
            self._emit("jobs.changed", {"ids": ids, "host_id": hid})
        note = (_removed_note(removed, None) if removed
                else "No finished jobs to remove.")
        return {"deleted": [j.id for j in removed], "count": len(removed),
                "job_dirs": [j.job_dir for j in removed], "note": note}

    def _host_label(self, host_id: str) -> str:
        try:
            host = self._store.get_host(host_id)
        except Exception:  # noqa: BLE001 - label only
            host = None
        return (getattr(host, "name", None) or host_id) if host_id else "the cluster"

    # -- polling ---------------------------------------------------------------

    def hosts_with_active_jobs(self) -> list[str]:
        """Host ids that have submitted jobs which have not finished.

        Open jobs whose host record no longer exists are closed first (once, see
        :meth:`_close_orphans`), so they are never polled or logged about again.
        """
        self._close_orphans()
        return sorted({j.host_id for j in self._open_jobs()})

    def _close_orphans(self) -> None:
        """Close open jobs of deleted hosts as UNKNOWN (ended, unconfirmed), silently."""
        try:
            jobs = self._open_jobs()
            known: dict[str, bool] = {}
            changed: dict[str, list[str]] = {}
            for job in jobs:
                if job.host_id not in known:
                    known[job.host_id] = self._store.get_host(job.host_id) is not None
                if known[job.host_id]:
                    continue
                with self._lock:
                    current = self._store.get_job(job.id)
                    if current is None or is_finished(current):
                        continue
                    current.state = "UNKNOWN"
                    current.ended_at = current.ended_at or utc_now_iso()
                    current.notified = True      # its host is gone: nobody to tell about it
                    self._store.upsert_job(current)
                    self._missing.pop(current.id, None)
                    changed.setdefault(current.host_id, []).append(current.id)
                log.info("closed job %s: host %s no longer exists", job.id, job.host_id)
            for host_id, ids in changed.items():
                self._emit("jobs.changed", {"ids": ids, "host_id": host_id})
        except Exception as exc:  # noqa: BLE001 - housekeeping must not break polling
            log.warning("could not close orphaned jobs: %s", exc)

    def poll_interval(self, host_id: str) -> float:
        """Seconds between polls of ``host_id`` (``max(30, slurm.poll_seconds)``)."""
        host = self._store.get_host(host_id)
        return poll_interval_seconds(getattr(host, "slurm", None))

    def poll(self, host_id: str) -> list[JobRecord]:
        """Refresh every open job on ``host_id`` with one ``squeue`` and one ``sacct``.

        No remote call is made when the host has no open jobs. When ``squeue``
        cannot reach the host, ``sacct`` is skipped and ``host_offline`` raised.
        Terminal jobs not yet announced get ``job.finished`` (and ``notify`` when
        ``notify_job_complete`` is on) once; ``notified`` is persisted.
        Returns the polled records after the update.
        """
        host, _ = self._host(host_id)
        with self._lock:
            jobs = self._open_jobs(host.id)
            pending_notice = [j for j in self._store.list_jobs(host.id)
                              if is_finished(j) and not j.notified]
            if not jobs:
                self._announce(host, pending_notice)
                return []
            sq = self._run(host, ["env", TIME_ENV, "squeue", "-h", "--me", "-o", SQUEUE_FORMAT],
                           timeout=POLL_CALL_TIMEOUT_S, what="squeue")
            ids = ",".join(sorted({str(j.slurm_job_id) for j in jobs}, key=int))
            try:
                sa = self._run(host, ["env", TIME_ENV, "sacct", "-X", "-P", "-n", "-j", ids,
                                      "-o", SACCT_FIELDS],
                               timeout=POLL_CALL_TIMEOUT_S, what="sacct")
            except ApiError as exc:
                if sq.rc != 0:
                    raise
                log.info("sacct on %s unavailable: %s", host.id, exc.message)
                sa = None                     # squeue answered, so the host is up
            squeue = parse_squeue(sq.stdout) if sq.rc == 0 else None
            sacct = parse_sacct(sa.stdout) if sa is not None and sa.rc == 0 else {}
            if squeue is None:
                log.info("squeue on %s failed (rc=%s): %s", host.id, sq.rc, _short(sq.stderr))
            changed: list[str] = []
            updated: list[JobRecord] = []
            for job in jobs:
                new = self._merge(job, squeue, sacct)
                if squeue is not None and self._unconfirmed(job, squeue, sacct):
                    new = JobRecord.from_dict(new.to_dict())       # _merge may return ``job``
                    new.state, new.ended_at = "UNKNOWN", utc_now_iso()
                    new.exit_code = None
                    log.info("job %s (slurm %s) left squeue and sacct has no record: closed "
                             "as UNKNOWN", job.id, job.slurm_job_id)
                if new.to_dict() != job.to_dict():
                    self._store.upsert_job(new)
                    changed.append(new.id)
                updated.append(new)
            if changed:
                self._emit("jobs.changed", {"ids": changed, "host_id": host.id})
            notice = {j.id: j for j in pending_notice}
            for j in updated:
                if is_finished(j) and not j.notified:
                    notice[j.id] = j
            self._announce(host, list(notice.values()))
            return [self._store.get_job(j.id) or j for j in updated]

    def _unconfirmed(self, job: JobRecord, squeue: dict[str, dict],
                     sacct: dict[str, dict]) -> bool:
        """True when ``job`` has been in neither ``squeue`` nor ``sacct`` long enough to close.

        Counts consecutive polls (``squeue`` succeeded, no row for the job anywhere); any
        sighting resets the count. Needs :data:`MISSING_POLLS` polls and
        :data:`MISSING_GRACE_S` since submission, so accounting lag right after ``sbatch``
        does not close a job.
        """
        sid = job.slurm_job_id
        seen = any(e["job_id"] == sid for e in squeue.values()) or any(
            e["job_id"] == sid for e in sacct.values())
        if seen:
            self._missing.pop(job.id, None)
            return False
        count = self._missing.get(job.id, 0) + 1
        self._missing[job.id] = count
        if count < MISSING_POLLS:
            return False
        try:
            age = (_dt.datetime.now(_dt.timezone.utc)
                   - parse_iso(job.submitted_at)).total_seconds()
        except Exception:  # noqa: BLE001 - unparsable submit time: treat as old
            age = MISSING_GRACE_S
        if age < MISSING_GRACE_S:
            return False
        self._missing.pop(job.id, None)
        return True

    def _announce(self, host: HostSpec, jobs: list[JobRecord]) -> None:
        for job in jobs:
            current = self._store.get_job(job.id)
            if current is None or current.notified or not is_finished(current):
                continue
            current.notified = True
            self._store.upsert_job(current)
            payload = {
                "id": current.id, "host_id": current.host_id,
                "slurm_job_id": current.slurm_job_id, "name": current.name,
                "state": current.state, "exit_code": current.exit_code,
            }
            if current.state == "UNKNOWN":
                payload["confirmed"] = False     # closed without a final state from Slurm
            self._emit("job.finished", payload)
            if self._notify_enabled():
                ok = current.state == "COMPLETED"
                unconfirmed = current.state == "UNKNOWN"
                level = ("warn" if unconfirmed or current.state == "CANCELLED"
                         else "info" if ok else "error")
                exit_text = f", exit {current.exit_code}" if current.exit_code else ""
                title = (f"Job {current.name} ended" if unconfirmed else
                         f"Job {current.name} {current.state.lower().replace('_', ' ')}")
                body = (f"Slurm job {current.slurm_job_id} on {host.name or host.id} left the "
                        "queue; its final state could not be confirmed (no accounting record)."
                        if unconfirmed else
                        f"Slurm job {current.slurm_job_id} on {host.name or host.id} "
                        f"finished: {current.state}{exit_text}.")
                self._emit("notify", {
                    "level": level,
                    "title": title,
                    "body": body,
                    "ref": {"kind": "job", "id": current.id},
                })

    @staticmethod
    def _merge(job: JobRecord, squeue: dict[str, dict] | None, sacct: dict[str, dict]) -> JobRecord:
        sid = job.slurm_job_id
        tasks: dict[str, dict] = {}
        for key, entry in sacct.items():
            if entry["job_id"] == sid:
                tasks[key] = dict(entry)
        for key, entry in (squeue or {}).items():
            if entry["job_id"] != sid:
                continue
            merged = tasks.setdefault(key, {})
            merged.update({k: v for k, v in entry.items() if v is not None or k not in merged})
        if not tasks:
            return job                         # not visible yet (e.g. accounting lag): keep
        # A pending range that sacct still lists but squeue no longer shows was
        # expanded into tasks; ignore it when concrete tasks exist.
        if squeue is not None:
            concrete = [k for k in tasks if "[" not in k]
            for key in [k for k in tasks if "[" in k]:
                if key not in squeue and concrete:
                    tasks.pop(key)
        states = [t.get("state", "UNKNOWN") for t in tasks.values()]
        state = aggregate_state(states)
        new = JobRecord.from_dict(job.to_dict())
        new.state = state
        starts = sorted(t["start"] for t in tasks.values() if t.get("start"))
        new.started_at = starts[0] if starts else job.started_at
        nodes: list[str] = []
        for t in tasks.values():
            n = t.get("nodes")
            if n and n not in nodes:
                nodes.append(n)
        new.nodes = ",".join(nodes) if nodes else job.nodes
        parts = [t.get("partition") for t in tasks.values() if t.get("partition")]
        if parts:
            new.partition = parts[0]
        if state in _TERMINAL:
            ends = sorted(t["end"] for t in tasks.values() if t.get("end"))
            new.ended_at = ends[-1] if ends else (job.ended_at or utc_now_iso())
            if state == "COMPLETED":
                codes = [t.get("exit_code") for t in tasks.values() if t.get("exit_code")]
                new.exit_code = codes[0] if len(tasks) == 1 and codes else "0:0"
            else:
                worst = [t for t in tasks.values() if t.get("state") == state]
                new.exit_code = next((t.get("exit_code") for t in worst if t.get("exit_code")),
                                     job.exit_code)
        return new


def provide(services: Any) -> SlurmManager:
    """Service provider used by the module registry."""
    return SlurmManager(services)
