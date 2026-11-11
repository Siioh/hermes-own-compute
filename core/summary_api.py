"""Overview routes: ``/health``, ``/summary``, ``/host-types`` and ``/events``.

``/summary`` is computed only from the store snapshot (no SSH), so it is cheap
enough to poll every few seconds. :func:`summarize` and :func:`chip_text` are
pure helpers that other code (chat context, slash commands) can reuse.

Standard library only.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import logging
import re
from types import SimpleNamespace
from pathlib import Path
from typing import Any

from . import events as _events
from . import hosttypes as _hosttypes
from . import registry as _registry
from . import slurm as _slurm
from .interfaces import SERVICE_NAMES, ApiError, ApiRequest, BackgroundTask, Route, ToolSpec
from .models import ACTIVE_JOB_STATES, parse_iso, utc_now_iso

__all__ = [
    "routes", "tools", "background_tasks",
    "health", "summary", "host_types", "events",
    "summarize", "chip_text",
]

log = logging.getLogger(__name__)

#: Window used for "finished recently" job counts and failure alerts.
RECENT_WINDOW = _dt.timedelta(hours=24)
#: Job states that raise an attention entry when they ended inside the window.
FAILED_JOB_STATES: tuple[str, ...] = ("FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL")

DEFAULT_EVENT_LIMIT = 200
MAX_EVENT_LIMIT = 500
_MAX_DETAIL_CHARS = 200

_INT_RE = re.compile(r"[+-]?[0-9]+", re.ASCII)
_VERSION_RE = re.compile(r"""^version:[ \t]*["']?([^"'\s#]+)["']?""", re.MULTILINE)
_FALLBACK_VERSION = "0.0.0"
_version_cache: dict[str, str] = {}


# --------------------------------------------------------------------------- /health


def _plugin_yaml_path() -> Path:
    """The plugin manifest, one directory above this package."""
    return Path(__file__).resolve().parents[1] / "plugin.yaml"


def _plugin_version() -> str:
    """The ``version:`` value of the plugin manifest (cached), or ``"0.0.0"``."""
    path = _plugin_yaml_path()
    key = str(path)
    cached = _version_cache.get(key)
    if cached is not None:
        return cached
    version = _FALLBACK_VERSION
    try:
        match = _VERSION_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            version = match.group(1)
    except OSError as exc:
        log.debug("cloud-compute: cannot read %s: %s", path, exc)
    _version_cache[key] = version
    return version


def _asyncssh_available() -> bool:
    try:
        return importlib.util.find_spec("asyncssh") is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _is_configured(req: ApiRequest) -> bool:
    """True when at least one host is stored; False if the store is unavailable."""
    try:
        return len(req.services.store.list_hosts()) > 0
    except ApiError:
        return False
    except Exception as exc:  # noqa: BLE001 - health must keep answering
        log.debug("cloud-compute: store unreadable in /health: %s", exc)
        return False


def health(req: ApiRequest) -> dict:
    """Liveness and capability report. Never builds a service that opens SSH."""
    modules = _registry.module_status()
    built = set(req.services.built())
    services: dict[str, bool] = {}
    for name in SERVICE_NAMES:
        if name in built:
            services[name] = True
        else:
            module = _registry.SERVICE_MODULES.get(name)
            services[name] = bool(module and modules.get(module, False))
    return {
        "ok": True,
        "version": _plugin_version(),
        "configured": _is_configured(req),
        "services": services,
        "broadcast": _events.broadcast_available(),
        "asyncssh": _asyncssh_available(),
        "modules": modules,
        "errors": _registry.import_errors(),
    }


# --------------------------------------------------------------------------- /summary


def _one_line(text: Any, limit: int = _MAX_DETAIL_CHARS) -> str:
    out = " ".join(str(text or "").split())
    return out if len(out) <= limit else out[: limit - 3] + "..."


def _get(mapping: Any, key: str, default: Any = None) -> Any:
    return mapping.get(key, default) if isinstance(mapping, dict) else default


def summarize(snapshot: dict) -> dict:
    """Condense a ``Store.snapshot()`` into counts and attention entries.

    Pure: the input is not modified and no clock is read; the 24-hour window
    ends at the snapshot's own ``generated_at``.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    generated_at = snapshot.get("generated_at")
    now = parse_iso(generated_at) if isinstance(generated_at, str) else None
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
        generated_at = utc_now_iso()
    cutoff = now - RECENT_WINDOW

    counts = {"total": 0, "online": 0, "degraded": 0, "auth_required": 0, "offline": 0}
    attention: list[dict] = []
    host_names: dict[str, str] = {}

    for entry in snapshot.get("hosts") or []:
        spec = _get(entry, "spec", {})
        state = _get(entry, "state")
        host_id = str(_get(spec, "id", "") or "")
        name = str(_get(spec, "name", "") or host_id)
        host_names[host_id] = name
        counts["total"] += 1
        status = _get(state, "status")
        if _calm(spec, status):          # an EC2 host with no running machine is not a failure
            continue
        if status in ("online", "degraded", "auth_required", "offline"):
            counts[status] += 1
        if status == "offline":
            attention.append({"kind": "host", "id": host_id, "message": f"{name} is offline"})
        elif status == "auth_required":
            attention.append({"kind": "host", "id": host_id,
                              "message": f"{name} needs login: run ssh {host_id or name} once"})
        elif status == "degraded":
            detail = _one_line(_get(state, "last_error"))
            attention.append({"kind": "host", "id": host_id,
                              "message": f"{name} is degraded" + (f": {detail}" if detail else "")})

    running = 0
    total_instances = 0
    for inst in snapshot.get("instances") or []:
        status = _get(inst, "status")
        if status == "destroyed":
            continue
        total_instances += 1
        if status == "running":
            running += 1
        elif status in ("stopped", "exited") and _get(inst, "stop_reason") in ("expired", "orphan"):
            inst_id = str(_get(inst, "id", "") or "")
            label = str(_get(inst, "name", "") or inst_id)
            host_id = str(_get(inst, "host_id", "") or "")
            where = host_names.get(host_id, host_id) or "its host"
            if _get(inst, "stop_reason") == "expired":
                message = (f"{label} reached its time limit and was stopped; its files are "
                           "kept. Start it again or delete it.")
            else:
                message = f"{label} was found on {where} without a record; it is stopped."
            attention.append({"kind": "instance", "id": inst_id, "message": message})
        elif status == "error":
            inst_id = str(_get(inst, "id", "") or "")
            label = str(_get(inst, "name", "") or inst_id)
            host_id = str(_get(inst, "host_id", "") or "")
            detail = _one_line(_get(inst, "error"))
            where = f" on {host_names.get(host_id, host_id)}" if host_id else ""
            attention.append({"kind": "instance", "id": inst_id,
                              "message": f"{label} failed{where}" + (f": {detail}" if detail else "")})

    active = 0
    finished = 0
    for job in snapshot.get("jobs") or []:
        state = _get(job, "state")
        if state in ACTIVE_JOB_STATES:
            active += 1
            continue
        # Terminal, or UNKNOWN with an end time (closed without a confirmed state).
        if not _slurm.is_finished(SimpleNamespace(state=state, ended_at=_get(job, "ended_at"))):
            continue
        ended = parse_iso(_get(job, "ended_at"))
        # The snapshot only lists recent terminal jobs, so one with no usable
        # end time still counts as finished within the window.
        if ended is not None and not (cutoff <= ended <= now + RECENT_WINDOW):
            continue
        finished += 1
        if state in FAILED_JOB_STATES:
            job_id = str(_get(job, "id", "") or "")
            label = str(_get(job, "name", "") or job_id)
            host_id = str(_get(job, "host_id", "") or "")
            where = f" on {host_names.get(host_id, host_id)}" if host_id else ""
            attention.append({"kind": "job", "id": job_id,
                              "message": f"job {label} {state}{where}"})

    return {
        "hosts": counts,
        "instances": {"running": running, "total": total_instances},
        "jobs": {"active": active, "finished_24h": finished},
        "attention": attention,
        "generated_at": generated_at,
    }


def _calm(spec: Any, status: Any) -> str:
    from .ec2_chat import calm_label
    return calm_label(spec if isinstance(spec, dict) else {}, status)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def chip_text(summary: dict) -> str:
    """Status chip text such as ``● 2/3 hosts · 3 envs · 1 job``."""
    hosts = _get(summary, "hosts", {})
    online = int(_get(hosts, "online", 0) or 0)
    total = int(_get(hosts, "total", 0) or 0)
    envs = int(_get(_get(summary, "instances", {}), "running", 0) or 0)
    jobs = int(_get(_get(summary, "jobs", {}), "active", 0) or 0)
    host_word = "host" if total == 1 else "hosts"
    return f"● {online}/{total} {host_word} · {_plural(envs, 'env')} · {_plural(jobs, 'job')}"


def summary(req: ApiRequest) -> dict:
    out = summarize(req.services.store.snapshot())
    try:
        from .approval import approvals_status
        out["approvals"] = approvals_status()       # {mode, off}: the page warns when off
    except Exception:  # noqa: BLE001 - the summary never fails for this
        out["approvals"] = {"mode": None, "off": False}
    return out


# --------------------------------------------------------------------------- /host-types


def host_types(req: ApiRequest) -> list:
    return _hosttypes.host_types()


# --------------------------------------------------------------------------- /events


def _int_param(query: dict, name: str) -> int | None:
    raw = (query or {}).get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not _INT_RE.fullmatch(text):
        raise ApiError(400, "invalid", f"{name} must be an integer")
    return int(text)


def events(req: ApiRequest) -> dict:
    """Stored events after ``since`` (poll fallback for the live event push)."""
    since = _int_param(req.query, "since")
    if since is None:
        since = 0
    if since < 0:
        raise ApiError(400, "invalid", "since must not be negative")
    limit = _int_param(req.query, "limit")
    if limit is None:
        limit = DEFAULT_EVENT_LIMIT
    limit = max(1, min(MAX_EVENT_LIMIT, limit))
    found = req.services.store.events_since(since, limit)
    return {"events": found, "next": found[-1]["seq"] if found else since}


# --------------------------------------------------------------------------- module contract


def routes() -> list[Route]:
    return [
        Route("GET", "/health", health, "Plugin health and capability report"),
        Route("GET", "/summary", summary, "Hosts, environments and jobs at a glance"),
        Route("GET", "/host-types", host_types, "Host type catalog"),
        Route("GET", "/events", events, "Stored events after a sequence number"),
    ]


def tools() -> list[ToolSpec]:
    return []


def background_tasks() -> list[BackgroundTask]:
    return []
