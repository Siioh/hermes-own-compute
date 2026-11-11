"""Slurm routes, Hermes tools and the job poll task.

Routes (relative paths): ``GET /jobs``, ``POST /jobs``, ``GET /jobs/{job_id}``,
``DELETE /jobs/{job_id}`` (finished jobs: local record only), ``POST /jobs/clear-finished``,
``POST /jobs/{job_id}/cancel``, ``GET /jobs/{job_id}/output`` (``{text, status, note}``),
``GET /hosts/{host_id}/partitions``.

Tools: ``slurm_submit`` (always needs approval) and ``slurm_jobs`` (approval for
``action=cancel`` only; ``wait`` blocks until a job ends, capped by ``wait_max_seconds``).

The background task polls only Slurm hosts that have open (non-terminal) jobs,
every :data:`OPEN_POLL_S` (10 s) while any are open, one poll per host per cycle.
``GET /jobs``, ``GET /jobs/{id}`` and the ``slurm_jobs`` tool also refresh a host with
open jobs on demand when its last poll is more than :data:`STALE_S` (5 s) old; that wait
is bounded by :data:`ON_DEMAND_BUDGET_S` and, on timeout or failure, the cached records
are returned with ``"stale": true``. Events (``jobs.changed``, ``job.finished``,
notifications) are emitted by the manager, not here, exactly once per finished job.

Every handler reads ``services.slurm`` at call time only, so this module imports
without the manager module being present.

Standard library only.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from . import approval as _approval
from .background import track_worker as _track_worker
from .interfaces import ApiError, ApiRequest, BackgroundTask, Route, Services, ToolSpec
from .models import TERMINAL_JOB_STATES

__all__ = [
    "routes", "tools", "background_tasks", "MIN_POLL_S", "DEFAULT_POLL_S", "RUN_BUDGET_S",
    "OPEN_POLL_S", "STALE_S", "ON_DEMAND_BUDGET_S", "DEFAULT_TAIL", "MAX_TAIL",
]

log = logging.getLogger(__name__)

#: Idle floor for the task tick (no host has open jobs, so nothing is polled anyway).
MIN_POLL_S = 30.0
DEFAULT_POLL_S = 60.0
#: While a host has open jobs it is polled this often (the finish must show up promptly).
OPEN_POLL_S = 10.0
#: A request refreshes a host with open jobs when its last poll is older than this.
STALE_S = 5.0
#: Longest an API request or tool call waits for a refresh before answering from the cache.
ON_DEMAND_BUDGET_S = 7.0
#: Longest a single task run waits for host polls; slower polls keep running in their thread.
RUN_BUDGET_S = 8.0
DEFAULT_TAIL = 200
MAX_TAIL = 5000
STREAMS = ("out", "err")
ACTIONS = ("list", "status", "output", "wait", "cancel", "delete")
#: Hermes v0.23.0 ends a single tool call after 420 s by default
#: (agent/tool_executor.py:138 ``_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S = 420.0``, used for
#: sequential calls too at :838-845). ``wait`` stays well under it so the final reads fit.
HERMES_TOOL_CALL_LIMIT_S = 420.0
WAIT_HARD_CAP_S = 360
#: Used when the ``wait_max_seconds`` setting is absent.
DEFAULT_WAIT_MAX_S = 300
DEFAULT_WAIT_S = 120
#: How often ``wait`` re-reads the local record (cheap: no remote call).
WAIT_CHECK_S = 2.0

_JOB_FIELDS = (
    "host_id", "name", "command", "script", "image", "partition",
    "cpus", "mem_gb", "gpus", "time_limit", "array",
)
_JOBS_TOOL_FIELDS = ("action", "host_id", "job_id", "tail", "stream", "task", "wait_s")

# Injectable for tests.
_monotonic = time.monotonic
_sleep = time.sleep


# --------------------------------------------------------------------------- argument parsing


def _invalid(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _reject_unknown(args: dict, valid: tuple[str, ...], what: str,
                    ignored: tuple[str, ...] = ()) -> None:
    """``unknown_parameter`` naming every unexpected key and listing the valid ones."""
    extra = sorted(str(k) for k in set(args) - set(valid) - set(ignored))
    if extra:
        plural = "s" if len(extra) > 1 else ""
        raise ApiError(400, "unknown_parameter",
                       f"unknown parameter{plural} for {what}: {', '.join(extra)}. "
                       f"Valid parameters: {', '.join(valid)}.")


def _body(req: ApiRequest) -> dict:
    body = req.body
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise _invalid("request body must be a JSON object")
    return body


def _text(args: dict, name: str, *, required: bool = False, default: str | None = None,
          keep: bool = False) -> str | None:
    """A string argument. ``keep`` leaves whitespace alone (scripts); empty counts as unset."""
    value = args.get(name)
    if value is None:
        if required:
            raise _invalid(f"{name} is required")
        return default
    if not isinstance(value, str):
        raise _invalid(f"{name} must be a string")
    stripped = value.strip()
    if not stripped:
        if required:
            raise _invalid(f"{name} is required")
        return default
    return value if keep else stripped


def _int(args: dict, name: str, default: int, minimum: int) -> int:
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{name} must be an integer")
    if value < minimum:
        raise _invalid(f"{name} must be at least {minimum}")
    return value


def _mem(args: dict) -> float:
    value = args.get("mem_gb")
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid("mem_gb must be a number")
    if not value > 0 or value != value or value == float("inf"):
        raise _invalid("mem_gb must be greater than 0")
    return value


def _job_request(args: Any):
    """Validate submit arguments and build a ``JobRequest``."""
    from .models import JobRequest

    if not isinstance(args, dict):
        raise _invalid("arguments must be an object")
    _reject_unknown(args, _JOB_FIELDS, "slurm_submit", ignored=("session_id",))
    command = _text(args, "command", keep=True)
    script = _text(args, "script", keep=True)
    if (command is None) == (script is None):
        raise _invalid("give exactly one of command or script")
    return JobRequest(
        host_id=_text(args, "host_id", required=True),
        name=_text(args, "name", required=True),
        command=command,
        script=script,
        image=_text(args, "image"),
        partition=_text(args, "partition"),
        cpus=_int(args, "cpus", 1, 1),
        mem_gb=_mem(args),
        gpus=_int(args, "gpus", 0, 0),
        time_limit=_text(args, "time_limit", default="00:30:00"),
        array=_text(args, "array"),
    )


def _tail(args: dict) -> int:
    value = args.get("tail")
    if value is None or value == "":
        return DEFAULT_TAIL
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise _invalid("tail must be an integer") from None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid("tail must be an integer")
    if value < 1:
        raise _invalid("tail must be at least 1")
    return min(value, MAX_TAIL)


def _stream(args: dict) -> str:
    value = _text(args, "stream", default="out")
    if value not in STREAMS:
        raise _invalid("stream must be 'out' or 'err'")
    return value


def _param(path_params: dict, name: str) -> str:
    value = (path_params or {}).get(name)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{name} is required")
    return value.strip()


def _dict(obj: Any) -> dict[str, Any]:
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return dict(obj)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- routes


def list_jobs(req: ApiRequest) -> list:
    query = req.query or {}
    host_id = (query.get("host") or "").strip() or None
    active = _truthy(query.get("active", ""))
    slurm = req.services.slurm
    jobs = slurm.list(host_id, active)
    stale = _refresh_for(slurm, jobs)
    if stale is not None:
        jobs = slurm.list(host_id, active)
    return _mark_stale([_dict(j) for j in jobs], stale)


def submit_job(req: ApiRequest) -> dict:
    body = _body(req)
    job_req = _job_request(body)
    session_id = _text(body, "session_id")
    return _dict(req.services.slurm.submit(job_req, session_id=session_id))


def get_job(req: ApiRequest) -> dict:
    slurm = req.services.slurm
    job_id = _param(req.path_params, "job_id")
    job = slurm.get(job_id)
    stale = _refresh_for(slurm, [job])
    if stale is not None:
        job = slurm.get(job_id)
    return _mark_stale([_dict(job)], stale)[0]


def cancel_job(req: ApiRequest) -> dict:
    return _dict(req.services.slurm.cancel(_param(req.path_params, "job_id")))


def _output(slurm: Any, job_id: str, args: dict) -> dict:
    """``{"text", "status", "note"}`` for one job (``task`` selects one array task)."""
    task = args.get("task")
    if isinstance(task, bool) or (task is not None and not isinstance(task, (int, str))):
        raise _invalid("task must be an array task number")
    info = getattr(slurm, "output_info", None)
    if callable(info):
        out = info(job_id, tail=_tail(args), stream=_stream(args), task=task)
        return {"text": out.get("text", ""), "status": out.get("status"), "note": out.get("note")}
    text = slurm.output(job_id, tail=_tail(args), stream=_stream(args))
    return {"text": text, "status": "ok" if text else "empty", "note": None}


def job_output(req: ApiRequest) -> dict:
    job_id = _param(req.path_params, "job_id")
    return _output(req.services.slurm, job_id, req.query or {})


def delete_job(req: ApiRequest) -> dict:
    return req.services.slurm.delete(_param(req.path_params, "job_id"))


def clear_finished(req: ApiRequest) -> dict:
    body = _body(req)
    host_id = _text(body, "host_id") or ((req.query or {}).get("host") or "").strip() or None
    return req.services.slurm.clear_finished(host_id)


def host_partitions(req: ApiRequest) -> list:
    host_id = _param(req.path_params, "host_id")
    return [_dict(p) for p in req.services.slurm.partitions(host_id)]


def routes() -> list[Route]:
    return [
        Route("GET", "/jobs", list_jobs, "List Slurm jobs (host=, active=1)"),
        Route("POST", "/jobs", submit_job, "Submit a Slurm job"),
        Route("POST", "/jobs/clear-finished", clear_finished,
              "Remove every finished job from the local list (host_id= optional)"),
        Route("GET", "/jobs/{job_id}", get_job, "One Slurm job"),
        Route("DELETE", "/jobs/{job_id}", delete_job,
              "Remove a finished job from the local list (its folder on the cluster stays)"),
        Route("POST", "/jobs/{job_id}/cancel", cancel_job, "Cancel a Slurm job"),
        Route("GET", "/jobs/{job_id}/output", job_output,
              "Tail a job's output (tail=, stream=out|err, task=); text, status, note"),
        Route("GET", "/hosts/{host_id}/partitions", host_partitions, "Partitions of a Slurm host"),
    ]


# --------------------------------------------------------------------------- tools


def _error(exc: BaseException) -> dict:
    if isinstance(exc, ApiError):
        return exc.to_dict()
    log.warning("cloud-compute: slurm tool failed: %s", type(exc).__name__, exc_info=True)
    return {"error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}}


def _card(job_id: Any) -> str:
    """The chat card line for one job, e.g. ``::cc-job{id="j-1a2b3c4d"}``."""
    return f'::cc-job{{id="{job_id}"}}'


def _with_card(result: dict, job_id: Any) -> dict:
    if isinstance(result, dict) and "error" not in result and job_id:
        result["card"] = _card(job_id)
    return result


def _session(ctx: Any) -> str | None:
    value = (ctx or {}).get("session_id") if isinstance(ctx, dict) else None
    return value or None


def _tool_submit(args: dict, services: Services, ctx: dict) -> dict:
    try:
        job_req = _job_request(args)
        job = services.slurm.submit(job_req, session_id=_session(ctx))
        out = _dict(job)
        return {"job": out, "card": _card(out.get("id"))}
    except Exception as exc:  # noqa: BLE001 - tool handlers never raise
        return _error(exc)


def _action(args: dict) -> str:
    value = _text(args, "action", default="list").lower()
    if value not in ACTIONS:
        raise _invalid(f"action must be one of {', '.join(ACTIONS)}")
    return value


def _wait_cap(services: Services) -> int:
    """Longest ``wait``: the ``wait_max_seconds`` setting (default 300), at most 360 s."""
    try:
        value = getattr(services.config, "wait_max_seconds", DEFAULT_WAIT_MAX_S)
    except Exception:  # noqa: BLE001 - config unavailable
        value = DEFAULT_WAIT_MAX_S
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        value = DEFAULT_WAIT_MAX_S
    return int(max(1, min(WAIT_HARD_CAP_S, value)))


def _wait_seconds(args: dict, cap: int) -> tuple[int, bool]:
    value = args.get("wait_s")
    if value is None or value == "":
        value = DEFAULT_WAIT_S
    elif isinstance(value, str):
        try:
            value = int(float(value.strip()))
        except ValueError:
            raise _invalid("wait_s must be a number of seconds") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise _invalid("wait_s must be a number of seconds")
    if value < 1:
        raise _invalid("wait_s must be at least 1")
    value = int(value)
    return (cap, True) if value > cap else (value, False)


def wait_for_job(services: Services, job_id: str, wait_s: float) -> tuple[Any, bool, float]:
    """Block until ``job_id`` has finished or ``wait_s`` passes: ``(job, finished, waited)``.

    The local record is re-read every :data:`WAIT_CHECK_S`; the host itself is polled at
    most every :data:`OPEN_POLL_S` and that poll is shared with the background task and
    other readers (one ``squeue`` and one ``sacct`` per host per cycle).
    """
    manager = services.slurm
    start = _monotonic()
    deadline = start + max(0.0, float(wait_s))
    job = manager.get(job_id)
    while not _finished(job.state, job.ended_at):
        left = deadline - _monotonic()
        if left <= 0:
            break
        if getattr(job, "slurm_job_id", None):
            now = _state.clock()
            with _state.lock:
                last = _state.last.get(job.host_id)
                running = _state.inflight.get(job.host_id)
            event = running
            if running is None and (last is None or now - last >= OPEN_POLL_S - 1.0):
                event = _start_poll(manager, job.host_id, now)
            if event is not None:
                event.wait(max(0.0, min(ON_DEMAND_BUDGET_S, deadline - _monotonic())))
        job = manager.get(job_id)
        if _finished(job.state, job.ended_at):
            break
        _sleep(max(0.0, min(WAIT_CHECK_S, deadline - _monotonic())))
        job = manager.get(job_id)
    return job, _finished(job.state, job.ended_at), round(_monotonic() - start, 1)


def _tool_wait(args: dict, services: Services, job_id: str) -> dict:
    cap = _wait_cap(services)
    wait_s, capped = _wait_seconds(args, cap)
    _tail(args), _stream(args)                     # validate before blocking
    job, finished, waited = wait_for_job(services, job_id, wait_s)
    result: dict[str, Any] = {"job": _dict(job), "finished": finished, "waited_s": waited,
                              "wait_s": wait_s}
    notes = []
    if capped:
        notes.append(f"wait_s was capped at {cap} s (the wait_max_seconds setting and the time "
                     "limit of one tool call).")
    if not finished:
        notes.append(f"The job is still {job.state} after {waited:g} s; call wait again or check "
                     "back later.")
    try:
        result["output"] = _output(services.slurm, job_id, args)
    except ApiError as exc:
        result["output"] = {"text": "", "status": "unreadable", "note": exc.message}
    if finished and job.state != "COMPLETED" and _stream(args) == "out":
        try:
            result["error_output"] = _output(services.slurm, job_id, dict(args, stream="err"))
        except ApiError:
            pass
    if notes:
        result["note"] = " ".join(notes)
    return result


def _tool_jobs(args: dict, services: Services, ctx: dict) -> dict:
    try:
        args = args if isinstance(args, dict) else {}
        _reject_unknown(args, _JOBS_TOOL_FIELDS, "slurm_jobs")
        action = _action(args)
        if action == "list":
            host_id = _text(args, "host_id")
            jobs = services.slurm.list(host_id, False)
            stale = _refresh_for(services.slurm, jobs)
            if stale is not None:
                jobs = services.slurm.list(host_id, False)
            return {"jobs": _mark_stale([_dict(j) for j in jobs], stale)}
        job_id = _text(args, "job_id", required=True)
        if action == "status":
            job = services.slurm.get(job_id)
            stale = _refresh_for(services.slurm, [job])
            if stale is not None:
                job = services.slurm.get(job_id)
            return _with_card({"job": _mark_stale([_dict(job)], stale)[0]}, job_id)
        if action == "cancel":
            return _with_card({"job": _dict(services.slurm.cancel(job_id))}, job_id)
        if action == "delete":
            return _with_card(dict(services.slurm.delete(job_id)), job_id)
        if action == "wait":
            return _with_card(_tool_wait(args, services, job_id), job_id)
        return _with_card(_output(services.slurm, job_id, args), job_id)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _fmt_num(value: Any) -> str:
    if isinstance(value, float) and value == int(value):
        value = int(value)
    return str(value)


def _host_name(services: Services, host_id: str | None) -> str:
    if not host_id:
        return "the selected host"
    try:
        host = services.store.get_host(host_id)
        name = getattr(host, "name", None)
        if name:
            return str(name)
    except Exception:  # noqa: BLE001 - names are decoration only
        pass
    return str(host_id)


def _submit_approval(args: dict, services: Services) -> str | None:
    """Always asks. Store lookups only (no network)."""
    args = args if isinstance(args, dict) else {}
    where = _host_name(services, args.get("host_id") or None)
    bits = [f"{_fmt_num(args.get('cpus') or 1)} CPU", f"{_fmt_num(args.get('mem_gb') or 1)} GB"]
    if args.get("gpus"):
        bits.append(f"{_fmt_num(args['gpus'])} GPU")
    bits.append(str(args.get("time_limit") or "00:30:00"))
    if args.get("image"):
        bits.append(f"image {args['image']}")
    if args.get("array"):
        bits.append(f"array {args['array']}")
    if args.get("partition"):
        bits.append(f"partition {args['partition']}")
    name = args.get("name") or "(unnamed)"
    text = f"Submit Slurm job {name} on {where} ({', '.join(bits)})?"
    command, script = args.get("command"), args.get("script")
    if isinstance(script, str) and script.strip():
        text += f" It runs this script as you on the cluster:\n{_approval.bounded_text(script)}"
    elif isinstance(command, str) and command.strip():
        text += f" It runs this command as you on the cluster:\n{_approval.bounded_text(command)}"
    return text


def _jobs_approval(args: dict, services: Services) -> str | None:
    args = args if isinstance(args, dict) else {}
    action = args.get("action")
    if not (isinstance(action, str) and action.strip().lower() == "cancel"):
        return None
    job_id = str(args.get("job_id") or "(unknown)")
    name = ""
    host_id = args.get("host_id") or None
    try:
        rec = services.store.get_job(job_id) if args.get("job_id") else None
        if rec is not None:
            name = f" ({rec.name})"
            host_id = host_id or rec.host_id
    except Exception:  # noqa: BLE001
        pass
    return f"Cancel Slurm job {job_id}{name} on {_host_name(services, host_id)}?"


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }


SUBMIT_SCHEMA = _schema(
    "slurm_submit",
    "Submit a batch job to a Slurm cluster through its login node; the job runs on compute "
    "nodes, never on the login node. Needs host_id (a kind=slurm host from compute_status), "
    "name, and exactly one of command (one shell command) or script (a batch script body). "
    "Optional: image (docker://... or a .sif path) runs the command inside Apptainer; partition, "
    "cpus, mem_gb, gpus, time_limit, array (e.g. 1-3; each task sees $SLURM_ARRAY_TASK_ID). "
    "Unknown parameters are rejected with error code unknown_parameter. Returns {job: record} "
    "at once (state PENDING, id j-xxxxxxxx, job_dir = the job's folder inside the host's work "
    "directory). Stdout/stderr are written to files in job_dir (slurm-<slurm id>.out/.err, "
    "slurm-<slurm id>_<task>.out for arrays) and read back with slurm_jobs action=output; to "
    "block until the job ends and get its output, call slurm_jobs action=wait. The result's "
    "card is a line to paste into the reply to show the job. Needs the user's approval.",
    {
        "host_id": {"type": "string", "description": "Slurm host id from compute_status."},
        "name": {"type": "string", "description": "Short job name."},
        "command": {"type": "string", "description": "Shell command to run (or give script)."},
        "script": {"type": "string", "description": "Batch script body (or give command)."},
        "image": {"type": "string",
                  "description": "Apptainer image: docker://... or a .sif path. Omit for a bare job."},
        "partition": {"type": "string", "description": "Partition; omit for the host default."},
        "cpus": {"type": "integer", "minimum": 1, "default": 1, "description": "CPUs per task."},
        "mem_gb": {"type": "number", "exclusiveMinimum": 0, "default": 1,
                   "description": "Memory in GB."},
        "gpus": {"type": "integer", "minimum": 0, "default": 0, "description": "GPUs."},
        "time_limit": {"type": "string", "default": "00:30:00",
                       "description": "Wall time limit, HH:MM:SS or D-HH:MM:SS."},
        "array": {"type": "string", "description": "Array spec such as 1-10%2."},
    },
    ["host_id", "name"],
)

JOBS_SCHEMA = _schema(
    "slurm_jobs",
    "Track Slurm jobs submitted with slurm_submit. Actions: "
    "list (default; optional host_id, a kind=slurm host from compute_status) -> "
    "{jobs: [records]}, newest first. "
    "status (job_id) -> {job: record}; state is PENDING, RUNNING, COMPLETED, FAILED, "
    "CANCELLED, TIMEOUT, OUT_OF_MEMORY, NODE_FAIL or UNKNOWN. "
    "output (job_id; optional stream out|err, tail lines, task for one array task) -> "
    "{text, status, note}: the last lines of the job's output files in its folder in the "
    "host's work directory; status is ok, empty (the job printed nothing), not_started or "
    "missing (no output file on the login node; note explains why and what to change). "
    "wait (job_id; optional wait_s, default 120, capped by the wait_max_seconds setting) "
    "blocks until the job finishes or time runs out -> {job, finished, waited_s, wait_s, "
    "output, error_output when it did not complete, note}; use it instead of calling status "
    "in a tight loop. cancel (job_id) needs the user's approval. "
    "delete (job_id, finished jobs only) removes the job from this computer's list without "
    "approval; its folder on the cluster is kept. Unknown parameters are rejected with "
    "error code unknown_parameter. A record with stale=true is cached data; the user is "
    "notified when a job finishes. Every result about one job has card: a line such as "
    "::cc-job{id=\"j-1a2b3c4d\"} to paste into the reply to show the job.",
    {
        "action": {"type": "string", "enum": list(ACTIONS), "default": "list",
                   "description": "list, status, output, wait, cancel or delete."},
        "host_id": {"type": "string",
                    "description": "Slurm host id from compute_status (filters list)."},
        "job_id": {"type": "string",
                   "description": "Job id such as j-1a2b3c4d (every action except list)."},
        "tail": {"type": "integer", "minimum": 1, "default": DEFAULT_TAIL,
                 "description": "Output lines to return (output, wait)."},
        "stream": {"type": "string", "enum": list(STREAMS), "default": "out",
                   "description": "out = stdout, err = stderr (output, wait)."},
        "task": {"type": "integer", "minimum": 0,
                 "description": "Array task number: read only that task's file (output, wait)."},
        "wait_s": {"type": "integer", "minimum": 1, "default": DEFAULT_WAIT_S,
                   "description": "Longest time to block for action=wait, in seconds."},
    },
    [],
)


def tools() -> list[ToolSpec]:
    return [
        ToolSpec("slurm_submit", SUBMIT_SCHEMA, _tool_submit, approval=_submit_approval),
        ToolSpec("slurm_jobs", JOBS_SCHEMA, _tool_jobs, approval=_jobs_approval),
    ]


# --------------------------------------------------------------------------- background poll


class _PollState:
    """Per-host poll bookkeeping; the clock is injectable for tests.

    ``last`` is when the host's latest poll was *started*; ``inflight`` maps a host to the
    event set when that poll ends, so the task and on-demand refreshes share one poll.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.last: dict[str, float] = {}
        self.inflight: dict[str, threading.Event] = {}
        self.failed: set[str] = set()      # the latest poll of these hosts raised


_state = _PollState()


def _host_interval(host: Any) -> float:
    slurm = getattr(host, "slurm", None)
    seconds = getattr(slurm, "poll_seconds", None)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        seconds = DEFAULT_POLL_S
    return max(MIN_POLL_S, float(seconds))


def _tick_interval(services: Services) -> float:
    """How often the task runs: :data:`OPEN_POLL_S` while any job is open, else the idle tick."""
    try:
        if services.slurm.hosts_with_active_jobs():
            return OPEN_POLL_S
    except Exception:  # noqa: BLE001 - manager missing or store hiccup: use the idle tick
        pass
    try:
        hosts = services.store.list_hosts(kind="slurm")
        if hosts:
            return min(_host_interval(h) for h in hosts)
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_POLL_S


def _due_hosts(services: Services, now: float) -> list[str]:
    slurm = services.slurm        # ApiError(503, "unavailable") when the service is absent
    due = []
    for host_id in slurm.hosts_with_active_jobs():
        last = _state.last.get(host_id)
        # one second of slack so a loop tick that lands just early does not skip a cycle
        if last is None or now - last >= OPEN_POLL_S - 1.0:
            due.append(host_id)
    return due


def _start_poll(manager: Any, host_id: str, now: float) -> threading.Event | None:
    """Start a poll thread for ``host_id`` unless one is running; returns its end event.

    Returns the running poll's event when one is already in flight (the caller may wait for
    it) and ``None`` when no thread could be started.
    """
    with _state.lock:
        running = _state.inflight.get(host_id)
        if running is not None:
            return running
        done = threading.Event()
        _state.inflight[host_id] = done
        _state.last[host_id] = now
    thread = _track_worker(threading.Thread(target=_poll_host, args=(manager, host_id, done),
                                            name=f"cloud-compute-slurm-{host_id}", daemon=True))
    try:
        thread.start()
    except Exception:  # noqa: BLE001
        with _state.lock:
            _state.inflight.pop(host_id, None)
        done.set()
        log.warning("cloud-compute: could not start slurm poll for %s", host_id, exc_info=True)
        return None
    return done


def _finished(state: Any, ended_at: Any) -> bool:
    """Terminal, or UNKNOWN with ``ended_at`` (ended, final state not confirmed)."""
    return state in TERMINAL_JOB_STATES or (state == "UNKNOWN" and bool(ended_at))


def _open_job(job: Any) -> bool:
    return (not _finished(getattr(job, "state", None), getattr(job, "ended_at", None))
            and bool(getattr(job, "slurm_job_id", None)))


def _refresh_for(manager: Any, jobs: list) -> set[str] | None:
    """Refresh the hosts of the open ``jobs`` whose last poll is older than :data:`STALE_S`.

    Returns ``None`` when nothing needed refreshing, else the set of host ids that are
    still stale (poll failed, or did not finish within :data:`ON_DEMAND_BUDGET_S`);
    the set is empty when every refresh succeeded. Never raises.
    """
    try:
        hosts = sorted({j.host_id for j in jobs if _open_job(j)})
        now = _state.clock()
        waits: list[tuple[str, threading.Event]] = []
        for host_id in hosts:
            with _state.lock:
                last = _state.last.get(host_id)
                running = _state.inflight.get(host_id)
            if running is None and last is not None and now - last <= STALE_S:
                continue                          # fresh enough: also rate-limits requests
            event = _start_poll(manager, host_id, now)
            if event is not None:
                waits.append((host_id, event))
        if not waits:
            return None
        stale: set[str] = set()
        deadline = time.monotonic() + ON_DEMAND_BUDGET_S
        for host_id, event in waits:
            if not event.wait(max(0.0, deadline - time.monotonic())):
                stale.add(host_id)
            else:
                with _state.lock:
                    if host_id in _state.failed:
                        stale.add(host_id)
        return stale
    except Exception:  # noqa: BLE001 - a refresh problem must never fail a read
        log.warning("cloud-compute: slurm on-demand refresh failed", exc_info=True)
        return None


def _mark_stale(items: list[dict], stale: set[str] | None) -> list[dict]:
    """Add ``stale: true`` to open job dicts whose host could not be refreshed."""
    if stale:
        for item in items:
            if item.get("host_id") in stale and not _finished(item.get("state"),
                                                              item.get("ended_at")):
                item["stale"] = True
    return items


def _poll_host(manager: Any, host_id: str, done: threading.Event) -> None:
    failed = True
    try:
        manager.poll(host_id)
        failed = False
    except ApiError as exc:
        log.warning("cloud-compute: slurm poll of %s failed: %s", host_id, exc.message)
    except Exception as exc:  # noqa: BLE001 - one host must not stop the others
        log.warning("cloud-compute: slurm poll of %s failed: %s: %s", host_id,
                    type(exc).__name__, exc, exc_info=True)
    finally:
        with _state.lock:
            if failed:
                _state.failed.add(host_id)
            else:
                _state.failed.discard(host_id)
            _state.inflight.pop(host_id, None)
        done.set()


def _run(services: Services) -> None:
    try:
        now = _state.clock()
        manager = services.slurm
        hosts = _due_hosts(services, now)
        if not hosts:
            return
    except ApiError as exc:
        if exc.code != "unavailable":
            log.warning("cloud-compute: slurm poll failed: %s", exc.message)
        return
    events: list[threading.Event] = []
    for host_id in hosts:
        with _state.lock:
            if host_id in _state.inflight:
                continue                       # previous poll still running
        event = _start_poll(manager, host_id, now)
        if event is not None:
            events.append(event)
    deadline = time.monotonic() + RUN_BUDGET_S
    for event in events:
        event.wait(max(0.0, deadline - time.monotonic()))


def background_tasks() -> list[BackgroundTask]:
    return [BackgroundTask("slurm.poll", _tick_interval, _run)]
