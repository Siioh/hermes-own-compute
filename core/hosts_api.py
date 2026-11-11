"""Host routes, the ``compute_status`` tool and the host background tasks.

Routes: ``GET/POST /hosts``, ``POST /hosts/test``, ``GET/PATCH/DELETE
/hosts/{host_id}`` and ``POST /hosts/{host_id}/probe``. Probing is delegated
to ``services.prober``; when that module is missing the routes that need it
answer ``503`` while saving a host still works (its state stays ``null``).

Background tasks return quickly: the liveness task runs short TCP checks
concurrently and gives up waiting after a few seconds, the deep-probe task
hands each due host to its own daemon thread and returns immediately.

Standard library only.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any

from .background import track_worker as _track_worker
from .interfaces import ApiError, ApiRequest, BackgroundTask, Route, ToolSpec
from .models import (
    HOST_KINDS, TERMINAL_JOB_STATES, HostSpec, HostState, fitting_host_kinds, is_valid_host_id,
    slugify_host_id, utc_now_iso,
)
from .redact import redact_text

__all__ = [
    "routes", "tools", "background_tasks",
    "list_hosts", "create_host", "test_host", "get_host", "patch_host", "delete_host",
    "probe_host", "compute_status", "compute_host", "describe_compute_host", "run_liveness",
    "run_probe", "needs_test_approval", "PAGE_ONLY_MESSAGE",
]

log = logging.getLogger(__name__)

#: Same token rule as the transport layer (kept local so this module has no hard dependency).
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.@:/=+-]+$")

_RUNTIME_PREFS = ("auto", "podman", "docker")
_SLURM_CONTAINERS = ("apptainer", "none")
_PATCH_NESTED = ("slurm", "limits", "ec2")
_MAX_TEXT = 200

#: Wall-time caps (seconds).
LIVENESS_BUDGET_S = 4.0
REFRESH_BUDGET_S = 60.0
REFRESH_WORKERS = 4
LIVENESS_WORKERS = 16

# Hosts with a deep probe / liveness check currently running (module level so that
# overlapping background ticks and manual refreshes never double-dispatch).
_inflight_lock = threading.Lock()
_probing: set[str] = set()
_checking: set[str] = set()
_last_probe_start: dict[str, float] = {}


# --------------------------------------------------------------------------- helpers


def _with_basis(resources: Any) -> Any:
    """Mark Slurm meters: CPU and memory are what Slurm has *allocated*, not OS usage."""
    if isinstance(resources, dict) and resources.get("source") == "slurm":
        return {**resources, "basis": "allocated"}
    return resources


def _state_dict(state: Any) -> dict | None:
    if state is None:
        return None
    data = state.to_dict() if hasattr(state, "to_dict") else dict(state)
    if isinstance(data, dict) and data.get("resources"):
        data["resources"] = _with_basis(data["resources"])
    return data


def _bad(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _body(req: ApiRequest) -> dict:
    if not isinstance(req.body, dict):
        raise _bad("request body must be a JSON object")
    return req.body


def _host_id(req: ApiRequest) -> str:
    return str((req.path_params or {}).get("host_id", ""))


def _require_host(req: ApiRequest) -> HostSpec:
    host_id = _host_id(req)
    host = req.services.store.get_host(host_id) if is_valid_host_id(host_id) else None
    if host is None:
        raise ApiError(404, "not_found", f"host {host_id!r} not found")
    return host


def _safe_str(data: dict, key: str, *, required: bool = False) -> None:
    value = data.get(key)
    if value is None:
        if required:
            raise _bad(f"{key} is required")
        return
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{key} must be a non-empty string")
    if len(value) > 255 or not SAFE_TOKEN.match(value):
        raise _bad(f"{key} contains characters that are not allowed")


def _prefix_list(data: dict, key: str) -> None:
    value = data.get(key)
    if value is None:
        data[key] = []
        return
    if not isinstance(value, list):
        raise _bad(f"{key} must be a list of strings")
    for item in value:
        if not isinstance(item, str) or not item or "\n" in item or "\r" in item or "\0" in item:
            raise _bad(f"{key} must contain only non-empty single-line strings")


def _number(value: Any, what: str, *, integer: bool, minimum: float, maximum: float | None = None,
            nullable: bool = False) -> None:
    if value is None and nullable:
        return
    ok = isinstance(value, int) if integer else isinstance(value, (int, float))
    if not ok or isinstance(value, bool) or value != value or value < minimum \
            or (maximum is not None and value > maximum):
        raise _bad(f"{what} must be a number" + (f" between {minimum:g} and {maximum:g}"
                                                   if maximum is not None else f" >= {minimum:g}"))


def _validate_spec(data: dict, saved: HostSpec | None = None) -> HostSpec:
    """Validate a complete spec dict and build the :class:`HostSpec`.

    For an EC2 host the address is the machine's (managed by the plugin, never taken from the
    request) and the fields the plugin writes are kept from ``saved``."""
    data = dict(data)
    kind = data.get("kind")
    if kind not in HOST_KINDS:
        raise _bad(f"kind must be one of: {', '.join(HOST_KINDS)}")
    if kind == "ec2":
        from .ec2 import NO_MACHINE
        from .ec2_api import validate_ec2
        saved_ec2 = saved.ec2.to_dict() if saved is not None and saved.ec2 is not None else None
        data["ec2"] = validate_ec2(data.get("ec2"), saved_ec2)
        data["address"] = saved.address if saved is not None else NO_MACHINE
        for key in ("user", "identity_file", "jump_host", "exec_prefix", "interactive_prefix",
                    "slurm"):
            data.pop(key, None)
        data["port"] = 22
    else:
        data.pop("ec2", None)
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _bad("name is required")
    if len(name) > 100 or "\n" in name or "\r" in name:
        raise _bad("name must be a single line of at most 100 characters")
    data["name"] = name.strip()
    if kind == "local" and not data.get("address"):
        data["address"] = "localhost"
    _safe_str(data, "address", required=True)
    _safe_str(data, "user")
    _safe_str(data, "jump_host")
    identity = data.get("identity_file")
    if identity is not None and (not isinstance(identity, str) or not identity.strip()
                                 or "\n" in identity or "\r" in identity or "\0" in identity):
        raise _bad("identity_file must be a path on one line")
    data.setdefault("port", 22)
    _number(data["port"], "port", integer=True, minimum=1, maximum=65535)
    if data.get("runtime_pref", "auto") not in _RUNTIME_PREFS:
        raise _bad(f"runtime_pref must be one of: {', '.join(_RUNTIME_PREFS)}")
    _prefix_list(data, "exec_prefix")
    _prefix_list(data, "interactive_prefix")

    limits = data.get("limits")
    if limits is not None:
        if not isinstance(limits, dict):
            raise _bad("limits must be an object")
        _number(limits.get("max_instances", 4), "limits.max_instances", integer=True,
                minimum=0, maximum=1000)
        _number(limits.get("max_cpus"), "limits.max_cpus", integer=False, minimum=0, nullable=True)
        _number(limits.get("max_memory_gb"), "limits.max_memory_gb", integer=False, minimum=0,
                nullable=True)
        if not isinstance(limits.get("allow_agents", False), bool):
            raise _bad("limits.allow_agents must be true or false")
    else:
        data.pop("limits", None)

    slurm = data.get("slurm")
    if slurm is not None:
        if not isinstance(slurm, dict):
            raise _bad("slurm must be an object")
        if slurm.get("container", "apptainer") not in _SLURM_CONTAINERS:
            raise _bad("slurm.container must be apptainer or none")
        _number(slurm.get("max_active_jobs", 20), "slurm.max_active_jobs", integer=True, minimum=1,
                maximum=10000)
        _number(slurm.get("poll_seconds", 60), "slurm.poll_seconds", integer=True, minimum=30,
                maximum=3600)
        for key in ("default_partition", "account", "qos"):
            value = slurm.get(key)
            if value is not None and (not isinstance(value, str) or not SAFE_TOKEN.match(value)):
                raise _bad(f"slurm.{key} contains characters that are not allowed")
        workdir = slurm.get("workdir", "~/cloud-compute")
        if not isinstance(workdir, str) or not workdir.strip() or "\n" in workdir:
            raise _bad("slurm.workdir must be a path on one line")
    else:
        data.pop("slurm", None)

    try:
        spec = HostSpec.from_dict(data)
    except (ValueError, TypeError) as exc:
        raise _bad(str(exc)) from exc
    if spec.kind == "slurm" and spec.slurm is None:
        from .models import SlurmSettings
        spec = dataclasses.replace(spec, slurm=SlurmSettings())
    return spec


def _emit_changed(req: ApiRequest, host_id: str, reason: str) -> None:
    try:
        req.services.events.emit("hosts.changed", {"ids": [host_id], "reason": reason})
    except Exception as exc:  # noqa: BLE001 - events never fail a request
        log.debug("cloud-compute: hosts.changed not emitted: %s", exc)


def _probe_quietly(services: Any, host: HostSpec) -> HostState | None:
    """Probe ``host``; ``None`` when the prober is missing or the probe fails."""
    try:
        return services.prober.probe(host)
    except ApiError as exc:
        log.debug("cloud-compute: probe of %s unavailable: %s", host.id, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("cloud-compute: probe of %s failed: %s", host.id, exc)
    return None


# --------------------------------------------------------------------------- routes


def _page_spec(services: Any, host: Any) -> dict:
    """``host.to_dict()`` for the page. An EC2 host also carries how much longer its machine
    may be extended (``extend_room_minutes``) and the Extend step to offer
    (``extend_step_minutes``, 0 = none): the rule lives in :func:`ec2.extend_offer`."""
    out = host.to_dict()
    if getattr(host, "kind", None) == "ec2" and getattr(host, "ec2", None) is not None:
        from .ec2 import extend_offer
        now = None
        manager = getattr(services, "ec2", None)
        if manager is not None and hasattr(manager, "_now"):
            try:
                now = manager._now()
            except Exception:  # noqa: BLE001 - the wall clock is as good
                now = None
        room, step = extend_offer(host.ec2, now or datetime.now(timezone.utc))
        out.setdefault("ec2", {})
        out["ec2"]["extend_room_minutes"] = room
        out["ec2"]["extend_step_minutes"] = step
    return out


def list_hosts(req: ApiRequest) -> list:
    kind = (req.query or {}).get("kind")
    if kind in (None, ""):
        kind = None
    elif kind not in HOST_KINDS:
        raise _bad(f"kind must be one of: {', '.join(HOST_KINDS)}")
    store = req.services.store
    return [{"spec": _page_spec(req.services, h), "state": _state_dict(store.get_host_state(h.id))}
            for h in store.list_hosts(kind)]


def create_host(req: ApiRequest) -> dict:
    data = dict(_body(req))
    name = data.get("name")
    if not data.get("id"):
        if not isinstance(name, str) or not name.strip():
            raise _bad("name is required")
        data["id"] = slugify_host_id(name)
    if not is_valid_host_id(data["id"]):
        raise _bad("id must be 1-32 characters of a-z, 0-9 and '-'")
    data["created_at"] = utc_now_iso()
    spec = _validate_spec(data)
    store = req.services.store
    if store.get_host(spec.id) is not None:
        raise ApiError(409, "conflict", f"a host with id {spec.id!r} already exists")
    spec = store.upsert_host(spec)
    _emit_changed(req, spec.id, "created")
    state = _probe_quietly(req.services, spec)
    if state is None:
        state = store.get_host_state(spec.id)
    if spec.kind == "slurm":
        _apply_tested(req, spec)
        state = store.get_host_state(spec.id) or state
    return {"spec": _page_spec(req.services, spec), "state": _state_dict(state)}


#: The last connection test of an unsaved Slurm host, by (id, address): the wizard saves the
#: host right after testing it, and the saved host card must show a failed work-directory
#: check from that test at once (not only after Test is pressed again).
_TESTED: dict[tuple[str, str], tuple[float, Any]] = {}
_TESTED_TTL_S = 1800.0
_TESTED_MAX = 20


def _note_tested(spec: Any, result: Any) -> None:
    if spec.kind != "slurm":
        return
    now = time.time()
    for key in [k for k, (at, _r) in _TESTED.items() if now - at > _TESTED_TTL_S]:
        _TESTED.pop(key, None)
    while len(_TESTED) >= _TESTED_MAX:
        _TESTED.pop(next(iter(_TESTED)), None)
    _TESTED[(spec.id, spec.address)] = (now, result)


def _apply_tested(req: ApiRequest, spec: Any) -> None:
    """Give a just-saved Slurm host the work-directory result of the test the wizard ran."""
    entry = _TESTED.pop((spec.id, spec.address), None)
    remember = getattr(req.services.prober, "remember_workdir", None)
    if entry is None or not callable(remember) or time.time() - entry[0] > _TESTED_TTL_S:
        return
    try:
        remember(spec, entry[1])
    except Exception:  # noqa: BLE001 - optional information
        log.debug("cloud-compute: could not keep the work-directory result", exc_info=True)


def test_host(req: ApiRequest) -> dict:
    """Test an unsaved spec. Writes nothing, except that a saved Slurm host of the same id
    and address keeps the work-directory result (so its card can show the fix)."""
    data = dict(_body(req))
    data.setdefault("id", slugify_host_id(str(data.get("name") or "test")))
    if not is_valid_host_id(data["id"]):
        data["id"] = slugify_host_id(str(data.get("name") or "test"))
    spec = _validate_spec(data)
    prober = req.services.prober
    result = prober.test_connection(spec)
    remember = getattr(prober, "remember_workdir", None)
    if callable(remember) and spec.kind == "slurm":
        remember(spec, result)      # the saved host card shows a failed check later
    _note_tested(spec, result)      # ... and so does a host saved right after this test
    return result.to_dict() if hasattr(result, "to_dict") else dict(result)


def get_host(req: ApiRequest) -> dict:
    host = _require_host(req)
    return {"spec": _page_spec(req.services, host), "state": _state_dict(req.services.store.get_host_state(host.id))}


def patch_host(req: ApiRequest) -> dict:
    host = _require_host(req)
    body = _body(req)
    for key in ("id", "kind"):
        if key in body and body[key] != getattr(host, key):
            raise _bad(f"{key} cannot be changed")
    spec = _validate_spec(_merged_body(host, body), saved=host)
    store = req.services.store
    spec = store.upsert_host(spec)
    _emit_changed(req, spec.id, "updated")
    return {"spec": _page_spec(req.services, spec), "state": _state_dict(store.get_host_state(spec.id))}


def delete_host(req: ApiRequest) -> dict:
    host = _require_host(req)
    store = req.services.store
    instances = [i for i in store.list_instances(host.id) if i.status != "destroyed"]
    jobs = [j for j in store.list_jobs(host.id)
            if j.state not in TERMINAL_JOB_STATES
            and not (j.state == "UNKNOWN" and j.ended_at)]
    if instances or jobs:
        parts = []
        if instances:
            kinds: dict[str, int] = {}
            for inst in instances:
                kinds[inst.status] = kinds.get(inst.status, 0) + 1
            parts.append(f"{len(instances)} environment(s) ("
                         + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) + ")")
        if jobs:
            parts.append(f"{len(jobs)} open Slurm job(s)")
        raise ApiError(409, "conflict",
                       f"host {host.id!r} still has {' and '.join(parts)}; destroy the "
                       "environments and cancel the jobs first")
    notes: list[str] = []
    if host.kind == "ec2":
        try:
            host = req.services.ec2.resolve_pending(host)   # an unfinished launch is a machine
        except ApiError as exc:
            raise ApiError(409, "conflict", f"{host.name} may have an unfinished launch in AWS "
                           f"that could not be checked: {exc.message}") from None
        if host.ec2 is not None and host.ec2.instance_id:
            raise ApiError(409, "conflict", f"{host.name} still has an EC2 machine "
                           f"({host.ec2.instance_id}). Terminate the machine first.")
        try:
            notes = req.services.ec2.remove_host(host)
        except ApiError as exc:
            notes = [f"The AWS key pair and security group were not removed: {exc.message}"]
    store.delete_host(host.id)
    _emit_changed(req, host.id, "deleted")
    return {"deleted": True, "notes": notes} if notes else {"deleted": True}


def probe_host(req: ApiRequest) -> dict:
    host = _require_host(req)
    return _state_dict(req.services.prober.probe(host)) or {}


def routes() -> list[Route]:
    return [
        Route("GET", "/hosts", list_hosts, "List hosts with their last known state"),
        Route("POST", "/hosts", create_host, "Add a host and probe it"),
        Route("POST", "/hosts/test", test_host, "Test an unsaved host connection"),
        Route("GET", "/hosts/{host_id}", get_host, "One host with its state"),
        Route("PATCH", "/hosts/{host_id}", patch_host, "Edit a host"),
        Route("DELETE", "/hosts/{host_id}", delete_host, "Remove a host"),
        Route("POST", "/hosts/{host_id}/probe", probe_host, "Probe a host now"),
    ]


# --------------------------------------------------------------------------- compute_status


def _card(kind: str, entity_id: Any) -> str:
    return f'::cc-{kind}{{id="{entity_id}"}}'


def _host_entry(spec: dict, state: dict | None) -> dict:
    state = state or {}
    entry = _host_entry_base(spec, state)
    if spec.get("kind") == "ec2":
        from .ec2_chat import cost_state
        entry["cost_state"] = cost_state(spec.get("ec2") or {})
    return entry


def _host_entry_base(spec: dict, state: dict) -> dict:
    return {
        "id": spec.get("id"),
        "name": spec.get("name"),
        "kind": spec.get("kind"),
        "status": state.get("status") or "unknown",
        "allow_agents": bool((spec.get("limits") or {}).get("allow_agents")),
        "last_seen": state.get("last_seen"),
        "capabilities": state.get("capabilities") or {},
        "resources": _with_basis(state.get("resources") or {}),
        "card": _card("host", spec.get("id")),
    }


def _refresh(services: Any, hosts: list[HostSpec]) -> str | None:
    """Probe ``hosts`` with bounded concurrency and total time; returns a warning or ``None``."""
    try:
        prober = services.prober
    except ApiError as exc:
        return exc.message
    pool = ThreadPoolExecutor(max_workers=REFRESH_WORKERS, thread_name_prefix="cc-refresh")
    try:
        futures = [pool.submit(prober.probe, h) for h in hosts]
        done, pending = wait(futures, timeout=REFRESH_BUDGET_S)
        for fut in pending:
            fut.cancel()
        problems = sum(1 for f in done if f.exception() is not None)
        if pending:
            return f"refresh timed out for {len(pending)} host(s); showing last known state"
        if problems:
            return f"refresh failed for {problems} host(s); showing last known state"
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _untracked_on_aws(services: Any, hosts: list[dict]) -> str:
    """Name (and mark on the host entry) instances of an EC2 host's region that carry this
    install's tag but belong to no host: AWS bills for them until someone stops them."""
    notes: list[str] = []
    for entry in hosts:
        if entry.get("kind") != "ec2":
            continue
        try:
            found = services.ec2.untracked(entry["id"]).get("untracked") or []
        except Exception:  # noqa: BLE001 - the status never fails for this
            continue
        ids = [str(i.get("instance_id")) for i in found if i.get("instance_id")]
        if ids:
            entry["found_on_aws_not_tracked"] = ids[:10]
            notes.append(f"{', '.join(ids[:5])} on AWS carry this install's tag but belong to "
                         f"no host; AWS bills the account for them until they are stopped or "
                         f"terminated (open host {entry['id']} to stop or terminate them)")
    return "; ".join(notes)


#: Steps for a model (or the person it talks to) when no host exists yet.
SETUP_STEPS = (
    'Call compute_host(action="types") to see the host types and the fields each one needs.',
    "Ask the user which machine to use: the SSH alias or address (an alias from their SSH config "
    "file is easiest) and the user name if the alias does not set it. Never ask for a password "
    "or key text in chat.",
    'Call compute_host(action="test", kind="ssh", address="<alias or address>") and read every '
    "row; each failing row has a fix.",
    'Call compute_host(action="add", kind="ssh", name="<name>", address="<alias or address>"). '
    "The user approves a card.",
    'To use this computer instead: compute_host(action="add", kind="local", name="This computer").',
    "If the user prefers to set it up themselves, tell them to open Compute in the Hermes "
    "sidebar and press Add host.",
)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def _is_agent_template(tpl: Any) -> bool:
    agent = getattr(tpl, "agent", None)
    return isinstance(agent, dict) and agent.get("enabled", True) is not False \
        and agent.get("mode") in ("hermes", "script")


def _template_entries(services: Any, details: bool, host_kind: str | None) -> list[dict]:
    """Compact template list. Names and yes/no flags only, never a value."""
    templates = list(services.templates.list())
    try:      # shipped agent templates exist from the start, stored only after first use
        from .agents import AGENT_TEMPLATES
        have = {t.id for t in templates}
        templates.extend(t for t in AGENT_TEMPLATES if t.id not in have)
    except Exception:  # noqa: BLE001 - the list works without the agents module
        pass
    out: list[dict] = []
    for tpl in templates:
        kinds = [str(k) for k in (tpl.host_kinds or [])]
        if host_kind is not None and host_kind not in fitting_host_kinds(kinds):
            continue
        entry: dict[str, Any] = {
            "id": tpl.id, "name": tpl.name,
            "kind": "agent" if _is_agent_template(tpl) else "environment",
            "host_kinds": kinds, "screen": bool(tpl.display)}
        if details:
            entry["description"] = (tpl.description or "")[:160]
            entry["variables"] = [_variable_entry(services, v) for v in tpl.variables or []]
        out.append(entry)
    return out


def _variable_entry(services: Any, var: Any) -> dict:
    source = str(getattr(var, "source", "") or "")
    secret = source.startswith("secret:")
    is_set = bool(source[len("literal:"):]) if source.startswith("literal:") else False
    if secret:
        try:
            is_set = bool(services.secrets.is_set(source[len("secret:"):]))
        except Exception:  # noqa: BLE001 - unknown means not set
            is_set = False
    return {"name": var.name, "secret": secret, "set": is_set}


def compute_status(args: dict, services: Any, ctx: dict | None = None) -> dict:
    """Handler of the ``compute_status`` tool. Never raises."""
    try:
        args = args if isinstance(args, dict) else {}
        unknown = _unknown_parameters("compute_status", args, _STATUS_PARAMS)
        if unknown:
            return unknown
        host_id = args.get("host_id")
        if host_id in ("", None):
            host_id = None
        elif not isinstance(host_id, str):
            return {"error": {"code": "invalid", "message": "host_id must be a string"}}
        refresh = _truthy(args.get("refresh", False))
        details = _truthy(args.get("details", False))
        store = services.store
        host_kind = None
        if host_id is not None:
            host = store.get_host(host_id)
            if host is None:
                return {"error": {"code": "not_found",
                                  "message": f"host {host_id!r} not found; call compute_status "
                                             "without host_id to list host ids"}}
            targets = [host]
            host_kind = host.kind
        else:
            targets = store.list_hosts()
        warning = _refresh(services, targets) if refresh and targets else None
        snap = store.snapshot()
        hosts = [_host_entry(e.get("spec") or {}, e.get("state")) for e in snap.get("hosts") or []
                 if host_id is None or (e.get("spec") or {}).get("id") == host_id]
        instances = [{**i, "card": _card("env", i.get("id"))}
                     for i in snap.get("instances") or []]
        jobs = [{**j, "card": _card("job", j.get("id"))} for j in snap.get("jobs") or []
                if j.get("state") in ("PENDING", "RUNNING", "COMPLETING")]
        if host_id is not None:
            instances = [i for i in instances if i.get("host_id") == host_id]
            jobs = [j for j in jobs if j.get("host_id") == host_id]
        found = _untracked_on_aws(services, hosts)
        if found:
            warning = (warning + "; " if warning else "") + found
        out: dict[str, Any] = {"configured": bool(snap.get("hosts") or targets),
                               "hosts": hosts, "instances": instances, "jobs": jobs}
        if host_id is not None:
            out["card"] = _card("host", host_id)
        if not out["configured"]:
            out["next_steps"] = list(SETUP_STEPS)
        try:
            out["templates"] = _template_entries(services, details, host_kind)
        except Exception as exc:  # noqa: BLE001 - hosts still answer without the template list
            log.debug("cloud-compute: template list unavailable: %s", exc)
            warning = warning or "the template list is not available right now"
        if warning:
            out["warning"] = warning
        try:
            from .approval import APPROVALS_OFF_WARNING, hermes_approvals_mode
            if hermes_approvals_mode() == "off":
                out["approvals_warning"] = APPROVALS_OFF_WARNING
        except Exception:  # noqa: BLE001 - the status never fails for this
            pass
        return out
    except ApiError as exc:
        return exc.to_dict()
    except Exception as exc:  # noqa: BLE001 - tool handlers never raise
        log.warning("cloud-compute: compute_status failed: %s", exc, exc_info=True)
        return {"error": {"code": "internal", "message": str(exc)[:_MAX_TEXT] or type(exc).__name__}}


_STATUS_PARAMS = ("host_id", "refresh", "details")

_COMPUTE_STATUS_SCHEMA = {
    "name": "compute_status",
    "description": (
        "Show the user's Own Compute hosts (SSH machines, Slurm clusters, this computer): "
        "online or not, capabilities, free resources, plus running environments, active Slurm "
        "jobs and the template ids env_spawn and cloud_agent_spawn accept. Call this first: "
        "host ids and template ids come from here. With no host, configured is false and next_steps "
        "says how to add one (compute_host). host_id limits hosts, environments, jobs and "
        "templates to one host. refresh re-probes now (slower). details lists each template's "
        "variable names and whether each is set (never values)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "host_id": {"type": "string",
                        "description": "Only this host (id from this tool's output); omit for all."},
            "refresh": {"type": "boolean", "default": False,
                        "description": "Probe the host(s) now before answering."},
            "details": {"type": "boolean", "default": False,
                        "description": "Also list each template's variables (name, secret, set)."},
        },
        "required": [],
    },
}


# --------------------------------------------------------------------------- compute_host

_HOST_ACTIONS = ("types", "list", "test", "add", "update", "remove", "start", "stop", "terminate",
                 "extend")
#: The actions on an EC2 host's machine (each asks the user; see ec2_api).
_MACHINE_ACTIONS = ("start", "stop", "terminate", "extend")
_MACHINE_FIELDS = ("minutes", "delete_environments", "save")
#: What an EC2 host takes from chat (see ec2_chat). The image, subnet, security group and SSH
#: user are page-only; AWS key values are never accepted.
_EC2_FIELDS = ("region", "instance_type", "disk_gb", "max_lifetime_minutes", "idle_stop_minutes",
               "auto_start", "ssh_cidr", "access_key_secret", "secret_key_secret")
_TOOL_KINDS = ("ssh", "slurm", "local", "ec2")
_SSH_FIELDS = ("user", "port", "identity_file", "jump_host")
_SLURM_FIELDS = ("partition", "account", "qos", "workdir")
_PREFIX_FIELDS = ("exec_prefix", "interactive_prefix")
_LIMIT_FIELDS = ("allow_agents", "max_instances")
#: What the chat tool takes. The command prefixes are NOT here: a prefix is a shell command the
#: host runs before every command, so only the user can set it, on the Compute page.
_HOST_PARAMS = ("action", "host_id", "kind", "name", "address", *_SSH_FIELDS, *_SLURM_FIELDS,
                *_LIMIT_FIELDS, *_MACHINE_FIELDS, *_EC2_FIELDS)
PAGE_ONLY_MESSAGE = "This setting can only be changed on the Compute page."
#: With a saved ``host_id``, these may be given and a test still connects only where the host
#: was saved. Any other field changes where or how the test connects, so it asks.
_TEST_FREE_FIELDS = ("action", "host_id", "kind", "name")
_CONNECTION_FIELDS = ("address", "user", "port", "identity_file", "jump_host", *_PREFIX_FIELDS)
#: Parameter names that would carry a login secret; named in the refusal.
_SECRET_PARAMS = frozenset({
    "password", "passwd", "passphrase", "private_key", "privatekey", "key", "key_data", "ssh_key",
    "token", "secret", "credentials", "api_key"})
_LOCAL_ADDRESSES = ("localhost", "127.0.0.1", "::1")

_TYPES: tuple[dict, ...] = (
    {"kind": "ssh", "title": "SSH host",
     "when_to_use": "A Linux machine reached over SSH with Podman or Docker. Runs sandbox "
                    "environments (env_spawn) and cloud agents. The usual choice.",
     "required": ["kind", "address"],
     "optional": ["name", "user", "port", "identity_file", "jump_host", "allow_agents",
                  "max_instances"],
     "example": {"action": "add", "kind": "ssh", "name": "Lab box", "address": "labbox",
                 "user": "alice"}},
    {"kind": "slurm", "title": "Slurm cluster",
     "when_to_use": "An HPC cluster, reached through its login node. Runs batch jobs "
                    "(slurm_submit) only: no environments, no cloud agents.",
     "required": ["kind", "address"],
     "optional": ["name", "user", "port", "identity_file", "jump_host", "partition", "account",
                  "qos", "workdir"],
     "example": {"action": "add", "kind": "slurm", "name": "Campus HPC",
                 "address": "login.hpc.example.edu", "user": "alice", "partition": "cpu"}},
    {"kind": "ec2", "title": "AWS EC2 machine",
     "when_to_use": "A machine started on demand in the user's own AWS account. AWS bills the "
                    "account while it runs. Adding the host starts nothing; start_machine "
                    "(action=start) launches it. Needs the AWS key in the Hermes secrets.",
     "required": ["kind"],
     "optional": ["name", "region", "instance_type", "disk_gb", "max_lifetime_minutes",
                  "idle_stop_minutes", "auto_start", "ssh_cidr", "access_key_secret",
                  "secret_key_secret"],
     "example": {"action": "add", "kind": "ec2", "name": "AWS small", "region": "us-east-1",
                 "instance_type": "t3.micro", "disk_gb": 10, "max_lifetime_minutes": 120}},
    {"kind": "local", "title": "This computer",
     "when_to_use": "Docker or Podman on the computer Hermes runs on. Quick tests and small "
                    "builds; no network hop.",
     "required": ["kind"],
     "optional": ["name", "allow_agents", "max_instances"],
     "example": {"action": "add", "kind": "local", "name": "This computer"}},
)
_TYPES_NOTES = (
    "address can be an alias from the user's SSH config file; keys, users and jump hosts set "
    "there are used automatically.",
    "Never send a password or key text. identity_file is only the path of a key file on this "
    "computer. If a login needs a password or passphrase, tell the user to run `ssh <address>` "
    "once in a terminal, or to open Compute and press Add host.",
    "action=test checks a connection and saves nothing. A test of a saved host (host_id only) "
    "runs at once; a test of an address that is not saved asks the user to approve first. "
    "action=add runs the same test and saves only if every essential row passes. add, update "
    "and remove ask the user to approve.",
    "Command prefixes (exec_prefix, interactive_prefix) can only be set on the Compute page, "
    "never through this tool.",
    "Cloud agents run only on hosts with allow_agents=true.",
    "An ec2 host is added with a name and, if needed, region, instance_type, disk_gb, "
    "max_lifetime_minutes, idle_stop_minutes, auto_start and ssh_cidr. The AWS key is never "
    "sent in chat: it must already be in the Hermes secrets (access_key_secret and "
    "secret_key_secret are only the NAMES of those secrets). The image, subnet, security group "
    "and SSH user can only be set on the Compute page. The approval card states the cost; "
    "adding the host starts nothing, start does and AWS bills the account while it runs.",
)

_CHECK_LABELS = {
    "credentials": "AWS key stored", "identity": "Key accepted", "region": "Region",
    "image": "Machine image", "launch": "Allowed to launch", "address": "Who may use SSH",
    "dns": "Address resolves", "tcp": "Port reachable", "auth": "Login works",
    "runtime": "Container runtime", "scheduler": "Slurm scheduler", "display": "Screen support",
    "gpu": "GPU"}
_FAIL_CODES = {"dns": "host_offline", "tcp": "host_offline", "auth": "auth_required",
               "runtime": "needs_setup", "scheduler": "needs_setup"}
_FAIL_NEXT = {
    "dns": "Check the address for typos and that the machine is on and reachable (VPN or "
           "Tailscale up), then test again.",
    "tcp": "Check the address for typos and that the machine is on and reachable (VPN or "
           "Tailscale up), then test again.",
    "auth": "The user has to log in once from a terminal (`ssh {address}`) to accept the host "
            "key or unlock the key, then test again. Do not ask for a password in chat.",
    "runtime": "Ask the user to install Podman or Docker on that machine, then test again.",
    "scheduler": "Check that the Slurm tools are on the login node's PATH (or ask the user to fix "
                 "the host's command prefix on the Compute page), then test again.",
}


def _err(code: str, message: str, next_hint: str | None = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"error": {"code": code, "message": message}}
    out.update(extra)
    if next_hint:
        out["next"] = next_hint
    return out


def _unknown_parameters(tool: str, args: dict, valid: tuple[str, ...]) -> dict | None:
    unknown = [k for k in args if k not in valid]
    if not unknown:
        return None
    names = ", ".join(repr(str(k)) for k in unknown)
    secrets = [k for k in unknown if str(k).lower() in _SECRET_PARAMS]
    if secrets:
        return _err("unknown_parameter",
                    f"{tool} never accepts {names}: no passwords, passphrases or key text. Use "
                    "identity_file (a path to a key file on this computer) or an SSH alias from "
                    "the user's SSH config file. If a login needs a password, tell the user to "
                    "run `ssh <address>` once in a terminal or to use Compute > Add host. "
                    f"Valid parameters: {', '.join(valid)}.")
    return _err("unknown_parameter",
                f"Unknown parameter {names} for {tool}. Valid parameters: {', '.join(valid)}.")


def _call_text(action: str, fields: dict) -> str:
    """The exact tool call text a model can copy, e.g. ``compute_host(action="add", ...)``."""
    parts = [f'action="{action}"']
    for key in _HOST_PARAMS[1:]:
        value = fields.get(key)
        if value in (None, "", []):
            continue
        parts.append(f"{key}={json.dumps(value)}")
    return f"compute_host({', '.join(parts)})"


def _clean_args(args: dict) -> dict:
    """Drop nulls and apply forgiving coercions (digit strings, 'true', a prefix string)."""
    out = {k: v for k, v in args.items() if v is not None}
    for key in ("port", "max_instances", "minutes", "disk_gb", "max_lifetime_minutes",
                "idle_stop_minutes"):
        if isinstance(out.get(key), str) and out[key].strip().isdigit():
            out[key] = int(out[key].strip())
    for flag in ("allow_agents", "delete_environments", "save", "auto_start"):
        if isinstance(out.get(flag), str) and out[flag].strip().lower() in ("true", "false"):
            out[flag] = out[flag].strip().lower() == "true"
    for key in ("kind", "name", "address", "user", "jump_host", "partition", "account", "qos",
                "workdir", "identity_file", "host_id", "region", "instance_type", "ssh_cidr",
                "access_key_secret", "secret_key_secret"):
        if isinstance(out.get(key), str):
            out[key] = out[key].strip()
            if key == "kind":
                out[key] = out[key].lower()
    return out


def _check_fields_for_kind(kind: str, args: dict) -> None:
    """Refuse fields the host kind does not take, naming what it does take."""
    if kind == "local":
        foreign = [k for k in (*_SSH_FIELDS, *_SLURM_FIELDS, *_PREFIX_FIELDS, *_EC2_FIELDS)
                   if k in args]
    elif kind == "ssh":
        foreign = [k for k in (*_SLURM_FIELDS, *_EC2_FIELDS) if k in args]
    elif kind == "ec2":
        foreign = [k for k in ("address", *_SSH_FIELDS, *_SLURM_FIELDS, *_LIMIT_FIELDS)
                   if k in args]
    else:
        foreign = [k for k in (*_LIMIT_FIELDS, *_EC2_FIELDS) if k in args]
    if foreign:
        table = next(t for t in _TYPES if t["kind"] == kind)
        raise _bad(f"{', '.join(foreign)} does not apply to {kind} hosts. A {kind} host takes: "
                   f"{', '.join(table['required'] + table['optional'])}.")


def _check_identity(args: dict) -> None:
    path = args.get("identity_file")
    if path is None:
        return
    if not isinstance(path, str) or not path.strip():
        raise _bad("identity_file must be a path to a key file on this computer")
    if "PRIVATE KEY" in path or "\n" in path or "\r" in path:
        raise _bad("identity_file must be a path to a key file on this computer, not the key "
                   "itself; never send key text")
    if not os.path.isfile(os.path.expanduser(path)):
        raise _bad(f"identity_file {path!r} is not a file on this computer; give the path of the "
                   "key file, or leave it out to use the user's SSH configuration")


def _body_from_args(args: dict, kind: str) -> dict:
    """Host fields (the shape of ``POST /hosts``) from tool parameters."""
    body: dict[str, Any] = {"kind": kind}
    for key in ("name", "address", *_SSH_FIELDS):
        if key in args:
            body[key] = args[key]
    slurm = {}
    for param, field_name in (("partition", "default_partition"), ("account", "account"),
                              ("qos", "qos"), ("workdir", "workdir")):
        if param in args:
            slurm[field_name] = args[param]
    if slurm:
        body["slurm"] = slurm
    limits = {k: args[k] for k in _LIMIT_FIELDS if k in args}
    if limits:
        body["limits"] = limits
    ec2 = {k: args[k] for k in _EC2_FIELDS if k in args}
    if ec2:
        body["ec2"] = ec2
    return body


def _refuse_stray_host_id(args: dict) -> None:
    """``host_id`` names an existing host; a new host's id is made from its name."""
    if args.get("host_id") is not None:
        raise _bad("host_id does not apply when adding a host: the id is made from the name. "
                   "Leave host_id out (to change an existing host use action=update).")


def _new_ec2_spec(args: dict, services: Any) -> HostSpec:
    """Validated spec of an EC2 host that does not exist yet. No AWS call."""
    from .ec2_chat import build_new
    _check_fields_for_kind("ec2", args)
    settings = build_new(args, services)
    name = args.get("name") if isinstance(args.get("name"), str) and args["name"].strip() \
        else "AWS machine"
    return _validate_spec({"kind": "ec2", "name": name, "id": slugify_host_id(name),
                           "created_at": utc_now_iso(), "ec2": settings.to_dict()})


def _new_spec(args: dict, services: Any = None) -> HostSpec:
    """Validated spec of a host that does not exist yet (``test`` and ``add``)."""
    kind = args.get("kind")
    if not kind:
        raise _bad('kind is required: one of ssh, slurm, local, ec2. Call '
                   'compute_host(action="types") for what each needs.')
    if kind == "ec2":
        return _new_ec2_spec(args, services)
    if kind not in _TOOL_KINDS:
        raise _bad(f"kind must be one of: {', '.join(_TOOL_KINDS)}")
    _check_fields_for_kind(kind, args)
    _check_identity(args)
    body = _body_from_args(args, kind)
    address = args.get("address")
    if kind == "local":
        if address not in (None, "") and str(address).lower() not in _LOCAL_ADDRESSES:
            raise _bad("a local host is always this computer. For another machine use kind ssh "
                       "with its address")
        body["address"] = "localhost"
    elif not address:
        raise _bad("address is required: an SSH alias or host name")
    if not body.get("name"):
        body["name"] = "This computer" if kind == "local" else str(body["address"])
    body["id"] = slugify_host_id(str(body["name"]))
    body["created_at"] = utc_now_iso()
    return _validate_spec(body)


def _caps_summary(caps: Any) -> dict | None:
    if not isinstance(caps, dict):
        return None
    out: dict[str, Any] = {"os": caps.get("os"), "runtime": caps.get("runtime"),
                           "containers": bool(caps.get("containers")),
                           "batch_jobs": bool(caps.get("batch_jobs")),
                           "display": caps.get("display"), "gpus": len(caps.get("gpus") or [])}
    parts = [p.get("name") for p in caps.get("partitions") or [] if isinstance(p, dict)]
    if parts:
        out["partitions"] = parts
    return out


def _rows(result: dict) -> list[dict]:
    rows = []
    for step in result.get("steps") or []:
        ok = step.get("ok")
        row = {"check": step.get("name"), "label": _CHECK_LABELS.get(step.get("name"), ""),
               "result": "pass" if ok is True else "fail" if ok is False else "skipped",
               "detail": redact_text(str(step.get("detail") or ""))[:240]}
        if ok is not True and step.get("hint"):
            row["fix"] = str(step["hint"])[:300]
        rows.append(row)
    return rows


def _run_test(services: Any, spec: HostSpec) -> dict:
    result = services.prober.test_connection(spec)
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    rows = _rows(data)
    return {"ok": bool(data.get("ok")), "status": data.get("status"), "rows": rows,
            "failing": [r for r in rows if r["result"] == "fail"],
            "capabilities": _caps_summary(data.get("capabilities"))}


def _failure(tested: dict, spec: HostSpec, retry: str) -> dict:
    """The refusal for a host whose essential checks fail: failing rows, a fix, nothing saved."""
    first = tested["failing"][0]
    names = ", ".join(r["label"] or r["check"] for r in tested["failing"])
    hint = _FAIL_NEXT.get(first["check"], "Fix the failing rows, then test again.")
    hint = hint.format(address=spec.address)
    return _err(_FAIL_CODES.get(first["check"], "invalid"),
                f"Host not saved. Failed check: {names}. {first['detail']}. "
                f"{first.get('fix') or ''}".strip(),
                f"Next: {hint} Retry with {retry}. If it keeps failing, tell the user to open "
                "Compute and press Add host.",
                saved=False, rows=tested["failing"])


def _next_for(host: dict) -> str:
    hid = host.get("id")
    if host.get("kind") == "ec2":
        return (f'Next: compute_host(action="start", host_id="{hid}") launches the machine; the '
                "user approves a card that states the cost. Then env_spawn.")
    if host.get("kind") == "slurm":
        return f'Next: slurm_submit(host_id="{hid}", name="hello", command="hostname")'
    return f'Next: env_spawn(template="python-dev", host_id="{hid}")'


def _host_summary(spec: dict, state: dict | None) -> dict:
    limits = spec.get("limits") or {}
    out: dict[str, Any] = {"id": spec.get("id"), "name": spec.get("name"),
                           "kind": spec.get("kind"), "address": spec.get("address"),
                           "status": (state or {}).get("status") or "unknown"}
    if spec.get("kind") == "ec2":
        from .ec2_chat import cost_state
        e = spec.get("ec2") or {}
        out.pop("address", None)
        out.update({"region": e.get("region"), "instance_type": e.get("instance_type"),
                    "disk_gb": e.get("disk_gb"), "max_lifetime_minutes": e.get("max_lifetime_minutes"),
                    "idle_stop_minutes": e.get("idle_stop_minutes"),
                    "auto_start": bool(e.get("auto_start")), "cost_state": cost_state(e)})
    if spec.get("kind") != "slurm":
        out["allow_agents"] = bool(limits.get("allow_agents"))
        out["max_instances"] = limits.get("max_instances")
    caps = _caps_summary((state or {}).get("capabilities"))
    if caps:
        out["capabilities"] = caps
    return out


def _saved(services: Any, host_id: str, extra: dict) -> dict:
    spec = services.store.get_host(host_id)
    state = _state_dict(services.store.get_host_state(host_id))
    host = _host_summary(spec.to_dict() if spec else {"id": host_id}, state)
    out = {**extra, "saved": True, "host": host, "card": _card("host", host_id)}
    if host.get("cost_state"):
        out["cost_state"] = host["cost_state"]
    out["next"] = _next_for(host)
    return out


def _action_types() -> dict:
    return {"types": [dict(t) for t in _TYPES], "notes": list(_TYPES_NOTES),
            "next": 'Ask the user which machine to use, then compute_host(action="test", '
                    'kind="ssh", address="<alias or address>").'}


def _counts(store: Any, host_id: str) -> tuple[int, int]:
    envs = sum(1 for i in store.list_instances(host_id) if i.status != "destroyed")
    jobs = sum(1 for j in store.list_jobs(host_id)
               if j.state not in TERMINAL_JOB_STATES and not (j.state == "UNKNOWN" and j.ended_at))
    return envs, jobs


def _action_list(services: Any) -> dict:
    store = services.store
    hosts = []
    for h in store.list_hosts():
        state = store.get_host_state(h.id)
        envs, jobs = _counts(store, h.id)
        hosts.append({"id": h.id, "name": h.name, "kind": h.kind,
                      "status": getattr(state, "status", None) or "unknown",
                      "allow_agents": bool(h.limits.allow_agents),
                      "environments": envs, "jobs": jobs, "card": _card("host", h.id)})
    out: dict[str, Any] = {"hosts": hosts, "configured": bool(hosts)}
    if hosts:
        out["next"] = 'Next: compute_status() for capabilities and templates, or compute_host(' \
                      'action="test", host_id="<id>") to re-check one.'
    else:
        out["next_steps"] = list(SETUP_STEPS)
    return out


def _action_test(services: Any, args: dict) -> dict:
    host_id = args.get("host_id")
    if host_id:
        base = services.store.get_host(host_id) if is_valid_host_id(host_id) else None
        if base is None:
            return _err("not_found", f"host {host_id!r} not found",
                        'Next: compute_host(action="list") shows the host ids.')
        merged = _merged_body(base, _update_body(base.kind, args))
        spec = _validate_spec(merged, saved=base)
    else:
        spec = _new_spec(args, services)
    tested = _run_test(services, spec)
    can_add = not tested["failing"]
    out: dict[str, Any] = {"ok": tested["ok"], "status": tested["status"], "can_save": can_add,
                           "rows": tested["rows"]}
    if tested["capabilities"]:
        out["capabilities"] = tested["capabilities"]
    if host_id:
        out["card"] = _card("host", host_id)
        out["next"] = _next_for({"id": host_id, "kind": spec.kind}) if can_add else \
            "Next: " + _FAIL_NEXT.get(tested["failing"][0]["check"],
                                      "Fix the failing rows, then test again.").format(
                address=spec.address)
    elif can_add:
        out["next"] = "Next: " + _call_text("add", args)
    else:
        out["next"] = "Next: " + _FAIL_NEXT.get(
            tested["failing"][0]["check"], "Fix the failing rows, then test again.").format(
            address=spec.address)
    return out


def _action_add(services: Any, args: dict) -> dict:
    if args.get("kind") == "ec2":
        _refuse_stray_host_id(args)
    spec = _new_spec(args, services)
    if services.store.get_host(spec.id) is not None:
        return _err("conflict", f"a host with id {spec.id!r} already exists",
                    f'Next: compute_host(action="update", host_id="{spec.id}", ...) to change it, '
                    "or add with a different name.")
    tested = _run_test(services, spec)
    if tested["failing"]:
        return _failure(tested, spec, _call_text("add", args))
    created = create_host(ApiRequest(path_params={}, query={}, body=spec.to_dict(),
                                     services=services))
    out = _saved(services, created["spec"]["id"], {"checks": "all essential checks passed"})
    if spec.kind == "ec2":
        out["cost_state"] = ("No machine is running. Adding the host started nothing; starting "
                             "the machine later is billed by AWS to the account these keys "
                             "belong to.")
    return out


def _update_body(kind: str, args: dict) -> dict:
    changes = {k: v for k, v in args.items() if k not in ("action", "host_id", "kind")}
    if "kind" in args and args["kind"] != kind:
        raise _bad("kind cannot be changed; remove the host and add it again")
    _check_fields_for_kind(kind, changes)
    _check_identity(changes)
    return _body_from_args(changes, kind)


def _merged_body(host: HostSpec, body: dict) -> dict:
    merged = host.to_dict()
    for key, value in body.items():
        if key in ("id", "kind", "created_at"):
            continue
        if key in _PATCH_NESTED and isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _action_update(services: Any, args: dict) -> dict:
    host_id = args.get("host_id")
    host = services.store.get_host(host_id) if host_id and is_valid_host_id(host_id) else None
    if host is None:
        return _err("not_found", f"host {host_id!r} not found" if host_id else "host_id is required",
                    'Next: compute_host(action="list") shows the host ids.')
    body = _update_body(host.kind, args)
    body.pop("kind", None)
    if not body:
        return _err("invalid", "nothing to update: give at least one field to change",
                    "Fields: name, address, user, port, identity_file, jump_host, allow_agents, "
                    "max_instances (partition, account, qos, workdir for Slurm; region, "
                    "instance_type, disk_gb, max_lifetime_minutes, idle_stop_minutes, "
                    "auto_start, ssh_cidr for EC2).")
    if host.kind == "ec2":
        return _update_ec2(services, host, args, body)
    if any(k in body for k in _CONNECTION_FIELDS):
        spec = _validate_spec(_merged_body(host, body))
        tested = _run_test(services, spec)
        if tested["failing"]:
            return _failure(tested, spec, _call_text("update", args))
    updated = patch_host(ApiRequest(path_params={"host_id": host.id}, query={}, body=body,
                                    services=services))
    if any(k in body for k in _CONNECTION_FIELDS):
        _probe_quietly(services, services.store.get_host(host.id))
    return _saved(services, updated["spec"]["id"], {"updated": sorted(body)})


#: EC2 settings that decide what a launch would do or who may use it: a changed one is checked
#: against AWS (read-only calls and a DryRun) before it is saved.
_EC2_TESTED = ("region", "instance_type", "disk_gb", "access_key_secret", "secret_key_secret",
               "ssh_cidr")


def _update_ec2(services: Any, host: HostSpec, args: dict, body: dict) -> dict:
    from .ec2_chat import cost_state
    spec = _validate_spec(_merged_body(host, body), saved=host)
    changed = body.get("ec2") or {}
    if any(k in changed for k in _EC2_TESTED):
        tested = _run_test(services, spec)
        if tested["failing"]:
            return _failure(tested, spec, _call_text("update", args))
    updated = patch_host(ApiRequest(path_params={"host_id": host.id}, query={}, body=body,
                                    services=services))
    fields = sorted([k for k in body if k != "ec2"] + list(changed))
    out = _saved(services, updated["spec"]["id"], {"updated": fields})
    out["cost_state"] = cost_state(updated["spec"].get("ec2") or {})
    return out


def _action_remove(services: Any, args: dict) -> dict:
    host_id = args.get("host_id")
    host = services.store.get_host(host_id) if host_id and is_valid_host_id(host_id) else None
    if host is None:
        return _err("not_found", f"host {host_id!r} not found" if host_id else "host_id is required",
                    'Next: compute_host(action="list") shows the host ids.')
    try:
        deleted = delete_host(ApiRequest(path_params={"host_id": host.id}, query={}, body=None,
                                         services=services))
    except ApiError as exc:
        if host.kind == "ec2":
            return _err(exc.code, exc.message,
                        'Next: compute_host(action="terminate", host_id="' + host.id + '") (the '
                        "user approves a card; environments are saved first), then remove "
                        "again.", removed=False)
        return _err(exc.code, exc.message,
                    'Next: env_control(action="destroy", instance_id=...) for each environment '
                    '(the files are saved first) and slurm_jobs(action="cancel", job_id=...) for '
                    'open jobs, then remove again. compute_status(host_id="' + host.id +
                    '") lists them.', removed=False)
    if host.kind == "ec2":
        return {"removed": True, "id": host.id, "name": host.name,
                "note": "The host is no longer listed in Own Compute. It had no machine, so "
                        "no running charge ended or started.",
                "notes": (deleted or {}).get("notes") or [],
                "cost_state": "No machine exists for this host.",
                "next": 'Next: compute_host(action="list")'}
    return {"removed": True, "id": host.id, "name": host.name,
            "note": "The host is no longer listed in Own Compute. Nothing on that machine was "
                    "changed.",
            "next": 'Next: compute_host(action="list")'}


def _action_machine(services: Any, args: dict) -> dict:
    host_id = args.get("host_id")
    host = services.store.get_host(host_id) if host_id and is_valid_host_id(host_id) else None
    if host is None:
        return _err("not_found", f"host {host_id!r} not found" if host_id else "host_id is required",
                    'Next: compute_host(action="list") shows the host ids.')
    if host.kind != "ec2":
        return _err("invalid", f"{args['action']} applies to EC2 hosts only; {host.name} is a "
                    f"{host.kind} host.")
    from .ec2_api import machine_action
    action = args["action"]
    body = {k: args[k] for k in _MACHINE_FIELDS if k in args}
    try:
        out = machine_action(services, host, action, body)
    except ApiError as exc:
        hint = None
        if exc.code == "conflict" and "Terminating" in exc.message:
            hint = ('Next: ask the user, then compute_host(action="terminate", host_id="'
                    + host.id + '", delete_environments=true) saves and deletes each first.')
        if exc.code == "not_saved":
            hint = ('Next: tell the user the two choices: compute_host(action="start", host_id="'
                    + host.id + '") (billed) and then terminate with the files saved, or '
                    'terminate with save=false, which deletes the files.')
        return _err(exc.code, exc.message, hint)
    machine = out.get("machine") or {}
    result = {"host_id": host.id, "machine": machine, "card": _card("host", host.id)}
    from .ec2_chat import cost_state
    fresh = services.store.get_host(host.id)
    if fresh is not None and fresh.ec2 is not None:
        result["cost_state"] = cost_state(fresh.ec2)
    if action == "start":
        result["next"] = ("The machine is starting; it takes a few minutes. compute_status("
                          f'host_id="{host.id}") shows when it is ready, then env_spawn. '
                          "AWS bills the account while it runs.")
    if out.get("environments"):
        result["environments"] = out["environments"]
    return result


_HANDLERS = {"types": lambda services, args: _action_types(),
             "list": lambda services, args: _action_list(services),
             "test": _action_test, "add": _action_add, "update": _action_update,
             "remove": _action_remove,
             **{a: _action_machine for a in _MACHINE_ACTIONS}}


def compute_host(args: dict, services: Any, ctx: dict | None = None) -> dict:
    """Handler of the ``compute_host`` tool. Never raises."""
    try:
        if not isinstance(args, dict):
            return _err("invalid", "arguments must be a JSON object with an action")
        if any(args.get(k) is not None for k in _PREFIX_FIELDS):
            return _err("invalid", PAGE_ONLY_MESSAGE,
                        "Next: tell the user to open Compute, pick the host and change it there.")
        from .ec2_chat import is_page_only, reject_key_values
        try:
            reject_key_values(args)
        except ApiError as exc:
            return _err("invalid", exc.message)
        if is_page_only(args, services):
            return _err("invalid", PAGE_ONLY_MESSAGE,
                        "Next: tell the user to open Compute, pick the host and change it there.")
        unknown = _unknown_parameters("compute_host", args, _HOST_PARAMS)
        if unknown:
            return unknown
        action = args.get("action")
        if action not in _HANDLERS:
            return _err("invalid", f"action must be one of: {', '.join(_HOST_ACTIONS)}",
                        'Next: compute_host(action="types")')
        return _HANDLERS[action](services, _clean_args(args))
    except ApiError as exc:
        return _err(exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - tool handlers never raise
        log.warning("cloud-compute: compute_host failed: %s", exc, exc_info=True)
        return _err("internal", str(exc)[:_MAX_TEXT] or type(exc).__name__)


def _host_prop(kind: str, description: str) -> dict:
    return {"type": kind, "description": description}


_COMPUTE_HOST_SCHEMA = {
    "name": "compute_host",
    "description": (
        "Set up and manage the machines Own Compute can use. types: host kinds and fields. "
        "list: hosts with status. test: check a connection, saves nothing. add: test, then "
        "save. update, remove: host_id. kind ec2: an AWS host (adding starts nothing); start, "
        "stop, terminate, extend act on its machine (AWS bills while it runs). Changes ask the "
        "user. Prefixes and EC2 image, subnet, security group: Compute page only. Never send "
        "passwords, key text or AWS keys."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_HOST_ACTIONS), "description": "What to do."},
            "host_id": _host_prop("string", "Host id (update, remove; test of a saved host, which needs no approval)."),
            "kind": _host_prop("string", "ssh, slurm, local or ec2 (see types)."),
            "name": _host_prop("string", "Display name; defaults to the address."),
            "address": _host_prop("string", "SSH alias, host name or IP (not for local)."),
            "user": _host_prop("string", "SSH user if the alias does not set it."),
            "port": _host_prop("integer", "SSH port, default 22."),
            "identity_file": _host_prop("string", "Path of a key file on this computer."),
            "jump_host": _host_prop("string", "Host to connect through."),
            "workdir": _host_prop("string", "Slurm: working folder on the shared file system."),
            "partition": _host_prop("string", "Slurm: default partition."),
            "account": _host_prop("string", "Slurm account."),
            "qos": _host_prop("string", "Slurm QOS."),
            "allow_agents": _host_prop("boolean", "Allow cloud agents on this host."),
            "max_instances": _host_prop("integer", "Most environments at once."),
            "region": _host_prop("string", "EC2: AWS region (default us-east-1)."),
            "instance_type": _host_prop("string", "EC2: instance type (default t3.micro)."),
            "disk_gb": _host_prop("integer", "EC2: root disk GB, 8-500 (default 10)."),
            "max_lifetime_minutes": _host_prop("integer", "EC2: machine stops itself after this long, 15-1440 (default 120)."),
            "idle_stop_minutes": _host_prop("integer", "EC2: stop after this many idle minutes, 0 = never (default 30)."),
            "auto_start": _host_prop("boolean", "EC2: start the machine when an environment needs it (default false)."),
            "ssh_cidr": _host_prop("string", "EC2: address or range allowed to use SSH (default: this computer)."),
            "access_key_secret": _host_prop("string", "EC2: NAME of the Hermes secret with the access key id. Never the key."),
            "secret_key_secret": _host_prop("string", "EC2: NAME of the Hermes secret with the secret key. Never the key."),
            "minutes": _host_prop("integer", "extend: minutes to add to an EC2 machine's time limit."),
            "delete_environments": _host_prop("boolean", "terminate: save and delete every environment first."),
            "save": _host_prop("boolean", "terminate: save files before deleting (default true)."),
        },
        "required": ["action"],
    },
}


def needs_test_approval(args: dict) -> bool:
    """True unless ``test`` checks a saved host exactly as it is saved.

    Only a ``host_id`` with nothing else that decides where or how to connect runs at once.
    A test of an address that is not saved, or of a saved host with other values, makes this
    computer log in to a machine the user has not approved, so it asks.
    """
    host_id = args.get("host_id") if isinstance(args, dict) else None
    if not isinstance(host_id, str) or not host_id.strip():
        return True
    return any(v is not None for k, v in args.items() if k not in _TEST_FREE_FIELDS)


def _text(value: Any, empty: str = "not set") -> str:
    """One value for a card: strings as they are, booleans yes/no, nothing as ``empty``."""
    if value is None or value == "" or value == []:
        return empty
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value)


#: Card label, tool parameter, the host's sub-object ("" = the host itself) and its attribute.
_CARD_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("Name", "name", "", "name"),
    ("Address", "address", "", "address"),
    ("User", "user", "", "user"),
    ("Port", "port", "", "port"),
    ("Jump host", "jump_host", "", "jump_host"),
    ("Key file", "identity_file", "", "identity_file"),
    ("Slurm partition", "partition", "slurm", "default_partition"),
    ("Slurm account", "account", "slurm", "account"),
    ("Slurm QOS", "qos", "slurm", "qos"),
    ("Work directory", "workdir", "slurm", "workdir"),
    ("Allow cloud agents", "allow_agents", "limits", "allow_agents"),
    ("Most environments at once", "max_instances", "limits", "max_instances"),
)
_UNSET_NOTE = {"user": "not set (the SSH settings decide)", "jump_host": "none",
               "identity_file": "none (the SSH settings decide)"}


def _saved_value(host: Any, group: str, attr: str) -> Any:
    if host is None:
        return None
    owner = getattr(host, group, None) if group else host
    return getattr(owner, attr, None) if owner is not None else None


def _prefix_note(host: Any) -> str:
    """The command prefix the user set on the Compute page, when a test would use it."""
    words = list(getattr(host, "exec_prefix", None) or [])
    if not words:
        return ""
    shown = " ".join(words)
    shown = shown if len(shown) <= 200 else shown[:200] + " ..."
    return (" The command prefix set for this host on the Compute page is put before the "
            f"commands it runs: {shown}")


def _add_fields_text(args: dict, kind: str) -> str:
    """Every value an ``add`` saves, defaults named, one ``Label: value`` per field."""
    defaults = {"port": "22", "allow_agents": "no", "max_instances": "4",
                "workdir": "~/cloud-compute (default)"}
    parts = []
    for label, param, group, _attr in _CARD_FIELDS:
        if label == "Name":
            continue
        if kind == "local" and param in ("address", "user", "port", "jump_host", "identity_file"):
            continue
        if kind == "slurm" and group == "limits":
            continue
        if kind != "slurm" and group == "slurm":
            continue
        shown = _text(args.get(param), defaults.get(param) or _UNSET_NOTE.get(param, "not set"))
        parts.append(f"{label}: {shown}")
    return "; ".join(parts) + "."


def _changes_text(args: dict, host: Any) -> str:
    """``Label: old -> new`` for each field an update or a test sets."""
    parts = []
    for label, param, group, attr in _CARD_FIELDS:
        if args.get(param) is None:
            continue
        old = _text(_saved_value(host, group, attr), _UNSET_NOTE.get(param, "not set"))
        parts.append(f"{label}: {old} -> {_text(args[param])}")
    return "; ".join(parts) + "."


def _test_text(args: dict, host: Any) -> str:
    kind = str(args.get("kind") or (getattr(host, "kind", "") or "ssh"))
    checks = ("a short built-in check there (operating system, Podman or Docker, Slurm, GPU, "
              "screen support)")
    if host is not None:
        changed = _changes_text({k: v for k, v in args.items() if k not in _TEST_FREE_FIELDS},
                                host)
        return (f"Tests the connection to saved host {host.name} ({host.id}) with these values "
                f"instead of the saved ones: {changed} Nothing is saved. Own Compute logs in "
                f"over SSH and runs {checks}." + _prefix_note(host))
    if kind == "local":
        return ("Tests the container runtime (Podman or Docker) on this computer. Nothing is "
                "saved.")
    fields = _add_fields_text(args, kind)
    return (f"Tests a connection to a machine that is not saved. Nothing is saved. Cloud "
            f"Compute logs in over SSH and runs {checks}. {fields}")


def describe_compute_host(args: dict, services: Any) -> str | None:
    """Approval text for ``compute_host``.

    Asks for ``add``, ``update``, ``remove`` and for ``test`` of anything but a saved host as
    it is saved (:func:`needs_test_approval`); ``types`` and ``list`` do not ask. A command
    prefix is refused outright (a blocked call, not a card): only the Compute page sets one.
    Every card shows each value that will be used. Store lookups only (no network). Never
    contains key text or secrets.
    """
    from .approval import BlockedMessage
    args = args if isinstance(args, dict) else {}
    if any(args.get(k) is not None for k in _PREFIX_FIELDS):
        return BlockedMessage(PAGE_ONLY_MESSAGE)
    action = args.get("action")
    from . import ec2_chat
    try:
        ec2_chat.reject_key_values(args)
    except ApiError as exc:
        return BlockedMessage(exc.message)
    if ec2_chat.is_page_only(args, services):
        return BlockedMessage(PAGE_ONLY_MESSAGE)
    if action in _MACHINE_ACTIONS:
        try:
            host = services.store.get_host(args.get("host_id")) \
                if is_valid_host_id(args.get("host_id")) else None
            if host is None or host.kind != "ec2":
                return f"{action.capitalize()} the machine of host {args.get('host_id')}?"
            try:
                now = services.ec2._now()          # the manager's clock (tests set it)
            except Exception:  # noqa: BLE001
                now = None
            envs = [r.name for r in services.store.list_instances(host.id)
                    if r.status != "destroyed"]
            return ec2_chat.describe_machine(action, host, _clean_args(args), now, envs)
        except Exception:  # noqa: BLE001 - fail closed to a generic request
            return (f"{str(action).capitalize()} an EC2 machine? AWS bills the account while a "
                    "machine runs, and for its disk while the machine exists.")
    if action not in ("test", "add", "update", "remove"):
        return None
    try:
        args = _clean_args(args)
        if action == "test":
            if not needs_test_approval(args):
                return None
            host = None
            if args.get("host_id"):
                host = services.store.get_host(args["host_id"]) \
                    if is_valid_host_id(args["host_id"]) else None
            if host is not None and host.kind == "ec2" or args.get("kind") == "ec2":
                try:
                    _check_fields_for_kind("ec2", {k: v for k, v in args.items()
                                                   if k not in _TEST_FREE_FIELDS})
                    return ec2_chat.describe_test(args, host, services)
                except ApiError as exc:
                    return BlockedMessage(exc.message)
            return _test_text(args, host)
        if action == "add":
            kind = args.get("kind")
            if kind == "ec2":
                try:
                    _refuse_stray_host_id(args)
                    _check_fields_for_kind("ec2", args)
                    return ec2_chat.describe_add(args, services)
                except ApiError as exc:
                    return BlockedMessage(exc.message)
            address = args.get("address") or "localhost"
            name = args.get("name") or ("this computer" if kind == "local" else address)
            fields = _add_fields_text(args, str(kind))
            if kind == "slurm":
                return (f"Adds Slurm host {name} to Own Compute after a connection test. Cloud "
                        "Compute logs in to the login node over SSH and runs Slurm commands "
                        "(sbatch, squeue, sacct, scancel) and file commands there as that user. "
                        f"{fields}")
            if kind == "local":
                return (f"Adds this computer ({name}) to Own Compute. Own Compute runs "
                        "Podman or Docker commands on this computer for it; environments run "
                        f"inside containers. {fields}")
            return (f"Adds SSH host {name} to Own Compute after a connection test. Cloud "
                    "Compute logs in over SSH and runs Podman or Docker commands on that "
                    f"machine as that user; environments run inside containers. "
                    f"{fields}")
        host = services.store.get_host(args.get("host_id")) if args.get("host_id") else None
        if host is not None and host.kind == "ec2":
            try:
                if action == "remove":
                    return ec2_chat.describe_remove(host, services)
                changes = {k: v for k, v in args.items() if k not in ("action", "host_id", "kind")}
                _check_fields_for_kind("ec2", changes)
                return ec2_chat.describe_update(changes, host, services)
            except ApiError as exc:
                return BlockedMessage(exc.message)
        label = f"{host.name} ({host.id})" if host is not None else str(args.get("host_id") or
                                                                          "(unknown host)")
        if action == "remove":
            return (f"Removes host {label} from Own Compute. Nothing on the machine itself is "
                    "changed.")
        changes = {k: v for k, v in args.items() if k not in ("action", "host_id", "kind")}
        shown = _changes_text(changes, host) if changes else "no fields."
        tested = ""
        if any(k in changes for k in _CONNECTION_FIELDS):
            tested = (" The connection is tested with the new values before they are saved."
                      + _prefix_note(host))
        return f"Changes host {label} in Own Compute. {shown}{tested}"
    except Exception:  # noqa: BLE001 - fail closed to a generic request
        return "Change the hosts Own Compute uses?"


def tools() -> list[ToolSpec]:
    return [ToolSpec(name="compute_status", schema=_COMPUTE_STATUS_SCHEMA, handler=compute_status,
                     approval=None, toolset="cloud_compute"),
            ToolSpec(name="compute_host", schema=_COMPUTE_HOST_SCHEMA, handler=compute_host,
                     approval=describe_compute_host, toolset="cloud_compute")]


# --------------------------------------------------------------------------- background tasks


def _config_seconds(services: Any, name: str, default: float) -> float:
    try:
        return float(getattr(services.config, name))
    except Exception:  # noqa: BLE001
        return default


def run_liveness(services: Any) -> None:
    """TCP-check every host concurrently; waits at most :data:`LIVENESS_BUDGET_S`."""
    hosts = services.store.list_hosts()
    if not hosts:
        return
    prober = services.prober
    with _inflight_lock:
        todo = [h for h in hosts if h.id not in _checking]
        _checking.update(h.id for h in todo)
    if not todo:
        return

    def check(host: HostSpec) -> None:
        try:
            prober.liveness(host)
        except Exception as exc:  # noqa: BLE001
            log.debug("cloud-compute: liveness of %s failed: %s", host.id, exc)
        finally:
            with _inflight_lock:
                _checking.discard(host.id)

    threads = []
    for host in todo[:max(LIVENESS_WORKERS, 1)] if len(todo) > LIVENESS_WORKERS else todo:
        t = _track_worker(threading.Thread(target=check, args=(host,), name=f"cc-live-{host.id}",
                                           daemon=True))
        t.start()
        threads.append(t)
    with _inflight_lock:                       # hosts beyond the cap wait for the next tick
        for host in todo[len(threads):]:
            _checking.discard(host.id)
    deadline = time.monotonic() + LIVENESS_BUDGET_S
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))


def _probe_thread(services: Any, host: HostSpec) -> None:
    try:
        services.prober.probe(host)
    except Exception as exc:  # noqa: BLE001
        log.warning("cloud-compute: probe of %s failed: %s", host.id, exc)
    finally:
        with _inflight_lock:
            _probing.discard(host.id)


def _prober_busy(prober: Any, host_id: str) -> bool:
    """True while the prober itself (e.g. a manual probe) is probing ``host_id``."""
    check = getattr(prober, "is_probing", None)
    if not callable(check):
        return False
    try:
        return bool(check(host_id))
    except Exception:  # noqa: BLE001
        return False


def run_probe(services: Any) -> None:
    """Start a deep probe for every due host on its own daemon thread and return."""
    hosts = services.store.list_hosts()
    if not hosts:
        return
    prober = services.prober                   # raises 503 here when the module is absent
    poll = _config_seconds(services, "poll_seconds", 60.0)
    now = time.monotonic()
    started: list[tuple[HostSpec, threading.Thread]] = []
    with _inflight_lock:
        for host in hosts:
            if host.id in _probing or _prober_busy(prober, host.id):
                continue
            if host.kind == "slurm":
                every = max(poll, float(getattr(host.slurm, "poll_seconds", 0) or 0))
                last = _last_probe_start.get(host.id)
                if last is not None and now - last < every * 0.9:
                    continue
            _probing.add(host.id)
            _last_probe_start[host.id] = now
            thread = _track_worker(threading.Thread(target=_probe_thread, args=(services, host),
                                                    name=f"cc-probe-{host.id}", daemon=True))
            started.append((host, thread))
    for _host, thread in started:
        thread.start()


def background_tasks() -> list[BackgroundTask]:
    return [
        BackgroundTask("hosts.liveness",
                       lambda s: _config_seconds(s, "liveness_seconds", 15.0), run_liveness),
        BackgroundTask("hosts.probe",
                       lambda s: _config_seconds(s, "poll_seconds", 60.0), run_probe),
    ]
