"""Sandbox routes, the ``env_spawn`` / ``env_exec`` / ``env_control`` tools and the
instance background tasks (stats sampling and the idle/TTL reaper).

Routes: ``/templates*`` (list, create, read, patch, delete, send preview),
``/instances*`` (list, spawn, read, stop/start/destroy/extend, stop-all, logs) and
``POST /hosts/{host_id}/display-image``. Errors are raised as :class:`ApiError`
and mapped to HTTP statuses by the dashboard layer. Tool handlers never raise:
they return ``{"error": {...}}`` instead.

Standard library only.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import approval as _approval
from .background import track_worker as _track_worker
from .interfaces import ApiError, ApiRequest, BackgroundTask, Route, Services, ToolSpec
from .models import TemplateSpec
from .sandbox import NO_OUTPUT_NOTE

__all__ = [
    "routes", "tools", "background_tasks",
    "env_spawn", "env_exec", "env_control",
    "approval_env_spawn", "approval_env_control",
    "run_stats", "run_reaper",
]

log = logging.getLogger(__name__)

OUTPUT_CAP_BYTES = 20 * 1024
INSTANCE_ACTIONS = ("stop", "start", "destroy", "extend")
TOOL_ACTIONS = ("stop", "start", "destroy", "extend", "logs")
EXTEND_MIN, EXTEND_MAX = 1, 10080

#: Wall-time bounds for one stats tick (seconds).
STATS_BUDGET_S = 8.0
STATS_HOST_TIMEOUT_S = 4.0
REAPER_BUDGET_S = 8.0

GENERIC_SPAWN_MESSAGE = "Spawn a sandbox on a remote host?"
GENERIC_DESTROY_MESSAGE = _approval.GENERIC_DESTROY_MESSAGE


# --------------------------------------------------------------------------- helpers


def _bad(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _body(req: ApiRequest) -> dict:
    if not isinstance(req.body, dict):
        raise _bad("request body must be a JSON object")
    return req.body


def _param(req: ApiRequest, key: str) -> str:
    return str((req.path_params or {}).get(key, ""))


def _query_int(req: ApiRequest, key: str, default: int, minimum: int, maximum: int) -> int:
    raw = (req.query or {}).get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise _bad(f"{key} must be an integer") from None
    if not minimum <= value <= maximum:
        raise _bad(f"{key} must be between {minimum} and {maximum}")
    return value


def _query_flag(req: ApiRequest, key: str) -> bool:
    return str((req.query or {}).get(key, "")).strip().lower() in ("1", "true", "yes")


def _opt_minutes(body: dict, key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10080:
        raise _bad(f"{key} must be an integer from 1 to 10080")
    return value


def _opt_str(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 80 \
            or "\n" in value or "\r" in value:
        raise _bad(f"{key} must be a single-line string of at most 80 characters")
    return value.strip()


def _req_str(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{key} is required")
    return value.strip()


def _dict(obj: Any) -> Any:
    return obj.to_dict() if hasattr(obj, "to_dict") else obj


def _spec_from_dict(data: dict) -> TemplateSpec:
    try:
        return TemplateSpec.from_dict(data)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise _bad(f"invalid template: {exc}") from None


# --------------------------------------------------------------------------- templates


def _touch_agents(services: Any) -> None:
    """Build ``services.agents`` so it seeds the built-in agent templates.

    The agents provider saves ``hermes-agent`` and ``agent-script`` when it is first built;
    touching it here makes template reads deterministic on a fresh install. After the first
    build this is only a container lookup. Never raises: templates must list without agents.
    """
    try:
        services.agents
    except Exception as exc:  # noqa: BLE001 - ApiError 'unavailable' or a broken provider
        log.debug("cloud-compute: agents service unavailable, agent templates not seeded: %s", exc)


def list_templates(req: ApiRequest) -> list:
    _touch_agents(req.services)
    return [t.to_dict() for t in req.services.templates.list()]


def create_template(req: ApiRequest) -> dict:
    return req.services.templates.create(_body(req)).to_dict()


def get_template(req: ApiRequest) -> dict:
    _touch_agents(req.services)
    template_id = _param(req, "template_id")
    if _query_flag(req, "default"):         # the shipped version (404 for a user template)
        return req.services.templates.get_default(template_id).to_dict()
    return req.services.templates.get(template_id).to_dict()


def patch_template(req: ApiRequest) -> dict:
    patch = _body(req)
    templates = req.services.templates
    current = templates.get(_param(req, "template_id")).to_dict()
    current.update({k: v for k, v in patch.items() if k not in ("id", "builtin")})
    return templates.save(_spec_from_dict(current)).to_dict()


def delete_template(req: ApiRequest) -> dict:
    return {"deleted": bool(req.services.templates.delete(_param(req, "template_id")))}


def preview_template(req: ApiRequest) -> dict:
    host = str((req.query or {}).get("host", "")).strip()
    if not host:
        raise _bad("host is required")
    return req.services.sandboxes.preview(_param(req, "template_id"), host).to_dict()


# --------------------------------------------------------------------------- instances


def _entry(sandboxes: Any, rec: Any) -> dict:
    """An instance for the page: its record plus ``workdir``, the folder a Delete saves
    (recorded at spawn, else its template's; ``None`` when unknown, and Delete is refused)."""
    out = rec.to_dict()
    folder = getattr(sandboxes, "work_folder", None)
    if not callable(folder):
        return out
    try:
        out["workdir"] = folder(rec)
    except Exception:  # noqa: BLE001 - the list never fails for this
        out["workdir"] = None
    return out


def list_instances(req: ApiRequest) -> list:
    host = str((req.query or {}).get("host", "")).strip() or None
    recs = req.services.sandboxes.list(host, _query_flag(req, "refresh"),
                                       include_destroyed=_query_flag(req, "destroyed"))
    return [_entry(req.services.sandboxes, r) for r in recs]


def create_instance(req: ApiRequest) -> dict:
    body = _body(req)
    rec = req.services.sandboxes.spawn(
        _req_str(body, "template_id"), _req_str(body, "host_id"),
        name=_opt_str(body, "name"), ttl_minutes=_opt_minutes(body, "ttl_minutes"),
        idle_minutes=_opt_minutes(body, "idle_minutes"))
    return rec.to_dict()


def get_instance(req: ApiRequest) -> dict:
    return _entry(req.services.sandboxes, req.services.sandboxes.get(_param(req, "instance_id")))


def _save_flag(body: Any) -> bool:
    """``save`` from an optional JSON body: default True; anything but a boolean is refused."""
    if body is None:
        return True
    if not isinstance(body, dict):
        raise _bad("request body must be a JSON object")
    value = body.get("save", True)
    if not isinstance(value, bool):
        raise _bad("save must be true or false")
    return value


def _extend_minutes(body: Any) -> int:
    value = body.get("minutes") if isinstance(body, dict) else None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) \
            or not EXTEND_MIN <= value <= EXTEND_MAX:
        raise _bad(f"minutes must be an integer from {EXTEND_MIN} to {EXTEND_MAX}")
    return value


def instance_action(req: ApiRequest) -> dict:
    """``POST /instances/{id}/{action}``. Body: destroy ``{"save": bool}`` (default true),
    extend ``{"minutes": 1-10080}``. Every action returns the InstanceRecord dict; destroy adds
    ``saved`` (``{export_id, path, bytes, files}`` or null) and ``message``."""
    action = _param(req, "action")
    instance_id = _param(req, "instance_id")
    if action not in INSTANCE_ACTIONS or instance_id == "stop-all":
        raise _bad(f"action must be one of: {', '.join(INSTANCE_ACTIONS)}")
    sandboxes = req.services.sandboxes
    if action == "destroy":
        result, report = {}, {}
        rec = sandboxes.destroy(instance_id, save=_save_flag(req.body), report=report)
        result = rec.to_dict()
        # What the Delete saved and where (null when nothing was saved), as one sentence.
        result["saved"] = report.get("saved")
        result["message"] = report.get("message") or f"{rec.name} was already deleted."
        return result
    if action == "extend":
        return sandboxes.extend(instance_id, _extend_minutes(req.body)).to_dict()
    return getattr(sandboxes, action)(instance_id).to_dict()


def stop_all(req: ApiRequest) -> dict:
    body = req.body if isinstance(req.body, dict) else {}
    host_id = body.get("host_id")
    if host_id is not None and (not isinstance(host_id, str) or not host_id.strip()):
        raise _bad("host_id must be a non-empty string")
    recs = req.services.sandboxes.stop_all(host_id.strip() if host_id else None)
    return {"stopped": [r.id for r in recs], "instances": [r.to_dict() for r in recs]}


def instance_logs(req: ApiRequest) -> dict:
    tail = _query_int(req, "tail", 200, 1, 10000)
    text = req.services.sandboxes.logs(_param(req, "instance_id"), tail)
    return {"text": text, "note": NO_OUTPUT_NOTE} if not text.strip() else {"text": text}


def build_display_image(req: ApiRequest) -> dict:
    host_id = _param(req, "host_id")
    if req.services.store.get_host(host_id) is None:
        raise ApiError(404, "not_found", f"host {host_id!r} not found")
    res = req.services.sandboxes.build_display_image(host_id)
    tail = _tail_text((res.stdout or "") + (res.stderr or ""), 4000)
    return {"ok": res.rc == 0, "output_tail": tail}


def routes() -> list[Route]:
    return [
        Route("GET", "/templates", list_templates, "List environment templates"),
        Route("POST", "/templates", create_template, "Create a template"),
        Route("GET", "/templates/{template_id}", get_template, "One template"),
        Route("PATCH", "/templates/{template_id}", patch_template, "Edit a template"),
        Route("DELETE", "/templates/{template_id}", delete_template, "Delete a template"),
        Route("GET", "/templates/{template_id}/preview", preview_template,
              "What a spawn would send to a host"),
        Route("GET", "/instances", list_instances, "List sandbox instances"),
        Route("POST", "/instances", create_instance, "Spawn a sandbox from a template"),
        Route("POST", "/instances/stop-all", stop_all, "Stop every running sandbox"),
        Route("GET", "/instances/{instance_id}", get_instance, "One instance"),
        Route("POST", "/instances/{instance_id}/{action}", instance_action,
              "Stop, start, extend or destroy an instance"),
        Route("GET", "/instances/{instance_id}/logs", instance_logs, "Container logs"),
        Route("POST", "/hosts/{host_id}/display-image", build_display_image,
              "Build the noVNC display image on a host"),
    ]


# --------------------------------------------------------------------------- tools


def _error(exc: BaseException) -> dict:
    if isinstance(exc, ApiError):
        return exc.to_dict()
    log.warning("cloud-compute: sandbox tool failed: %s", type(exc).__name__, exc_info=True)
    return {"error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}}


def _tail_text(text: str, cap: int = OUTPUT_CAP_BYTES) -> str:
    data = (text or "").encode("utf-8", "replace")
    if len(data) <= cap:
        return text or ""
    return data[-cap:].decode("utf-8", "ignore")


def _tool_int(args: dict, key: str, default: int | None, lo: int, hi: int) -> int | None:
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise _bad(f"{key} must be an integer from {lo} to {hi}")
    return value


def _card(instance_id: Any) -> str:
    """The chat card directive for one environment."""
    return f'::cc-env{{id="{instance_id}"}}'


def _copy_card(export_id: Any) -> str:
    """The chat card directive for one saved copy."""
    return f'::cc-copy{{id="{export_id}"}}'


SPAWN_PARAMS = ("template", "host_id", "name", "ttl_minutes", "idle_minutes")
EXEC_PARAMS = ("instance_id", "command", "timeout_s", "workdir")


def _tool_str(args: dict, key: str, hint: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{key} is required: {hint}")
    return value.strip()


def _check_spawn(args: dict, services: Services) -> tuple[str, str, int | None, int | None]:
    """Everything that can be wrong with an ``env_spawn`` call, checked before any approval.

    Returns ``(template, host_id, ttl, idle)`` or raises an ApiError that says what to fix.
    """
    _approval.check_params("env_spawn", args, SPAWN_PARAMS)
    template = _tool_str(args, "template", "a template id such as python-dev "
                                           "(compute_status lists them)")
    host_id = _tool_str(args, "host_id", "an environment host id from compute_status")
    ttl = _tool_int(args, "ttl_minutes", None, 5, 1440)
    idle = _tool_int(args, "idle_minutes", None, 5, 1440)
    _opt_str(args, "name")
    _touch_agents(services)
    services.sandboxes.validate_spawn(template, host_id)
    return template, host_id, ttl, idle


def env_spawn(args: dict, services: Services, ctx: dict) -> dict:
    try:
        args = args or {}
        template, host_id, ttl, idle = _check_spawn(args, services)
        session_id = (ctx or {}).get("session_id") or None
        sandboxes = services.sandboxes
        preview = sandboxes.preview(template, host_id)
        rec = sandboxes.spawn(template, host_id, name=_opt_str(args, "name"), ttl_minutes=ttl,
                              idle_minutes=idle, session_id=session_id)
        return {"instance": rec.to_dict(), "preview": preview.to_dict(),
                "warnings": list(preview.warnings), "card": _card(rec.id)}
    except Exception as exc:  # noqa: BLE001 - tools report, never raise
        return _error(exc)


def _refuse_agent_sandbox(services: Services, instance_id: str) -> None:
    """The sandbox of a cloud agent run holds its model key: a command run from chat could
    print it into the conversation. The user's own Terminal on the page stays."""
    rec = services.store.get_instance(instance_id)
    if rec is not None and _approval.is_agent_sandbox(services, rec):
        raise ApiError(409, "conflict", _approval.AGENT_SANDBOX_MESSAGE)


def env_exec(args: dict, services: Services, ctx: dict) -> dict:
    try:
        args = args or {}
        _approval.check_params("env_exec", args, EXEC_PARAMS)
        instance_id = _tool_str(args, "instance_id", "the environment id from env_spawn")
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise _bad("command is required: the shell command to run")
        timeout = _tool_int(args, "timeout_s", 120, 1, 600)
        _refuse_agent_sandbox(services, instance_id)
        workdir = args.get("workdir")
        if workdir is not None and (not isinstance(workdir, str) or not workdir.strip()):
            raise _bad("workdir must be a non-empty string")
        res = services.sandboxes.exec(instance_id, command, timeout_s=timeout,
                                      workdir=workdir.strip() if workdir else None)
        return {"rc": res.rc, "stdout": _tail_text(res.stdout), "stderr": _tail_text(res.stderr),
                "timed_out": bool(res.timed_out), "card": _card(instance_id)}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def env_control(args: dict, services: Services, ctx: dict) -> dict:
    try:
        args = args or {}
        instance_id = _tool_str(args, "instance_id", "the environment id from env_spawn")
        action = args.get("action")
        if action not in TOOL_ACTIONS:
            raise _bad(f"action must be one of: {', '.join(TOOL_ACTIONS)}")
        _approval.check_env_control_params(args)
        sandboxes = services.sandboxes
        card = _card(instance_id)
        if action == "logs":
            tail = _tool_int(args, "tail", 200, 1, 10000)
            text = _tail_text(sandboxes.logs(instance_id, tail), 4 * OUTPUT_CAP_BYTES)
            out = {"logs": text, "card": card}
            if not text.strip():
                out["note"] = NO_OUTPUT_NOTE
            return out
        if action == "extend":
            minutes = _tool_int(args, "minutes", None, EXTEND_MIN, EXTEND_MAX)
            if minutes is None:
                raise _bad("minutes is required for extend (1-10080)")
            return {"instance": sandboxes.extend(instance_id, minutes).to_dict(), "card": card}
        if action == "destroy":
            save = args.get("save", True)
            if not isinstance(save, bool):
                raise _bad("save must be true or false")
            report: dict = {}
            rec = sandboxes.destroy(instance_id, save=save, report=report)
            saved = report.get("saved")
            out = {"instance": rec.to_dict(), "saved": saved,
                   "message": report.get("message") or f"{rec.name} was already deleted.",
                   "card": card}
            if saved:
                out["copy_card"] = _copy_card(saved.get("export_id"))
            return out
        return {"instance": getattr(sandboxes, action)(instance_id).to_dict(), "card": card}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _ttl_text(minutes: Any, persistent: bool) -> str:
    if persistent or minutes is None:
        return "no TTL"
    m = int(minutes)
    if m % 60 == 0:
        return f"TTL {m // 60} h"
    return f"TTL {m} min"


def _num(value: Any) -> str:
    return f"{float(value):g}"


def approval_env_spawn(args: dict, services: Services) -> str:
    """Approval text for ``env_spawn``; store lookups only, fails closed to a generic message."""
    try:
        args = args or {}
        try:
            _check_spawn(args, services)
        except ApiError as exc:        # cannot proceed: say so now, never ask first
            return _approval.BlockedMessage(exc.message)
        except Exception:  # noqa: BLE001 - a broken check must not hide the card
            log.debug("cloud-compute: spawn pre-check failed", exc_info=True)
        template_id = str(args.get("template") or "")
        host_id = str(args.get("host_id") or "")
        tpl = services.templates.get(template_id)
        host = services.store.get_host(host_id)
        host_name = host.name if host is not None else host_id
        ttl = args.get("ttl_minutes")
        ttl = tpl.ttl_minutes if ttl in (None, "") else ttl
        if ttl is None:          # template leaves it unset: the Settings default applies
            try:
                ttl = services.sandboxes.effective_limits(template_id, None, None)[0]
            except Exception:  # noqa: BLE001 - name the configured default instead
                ttl = getattr(getattr(services, "config", None), "default_ttl_minutes", None)
        parts = [f"{_num(tpl.cpus)} CPU", f"{_num(tpl.memory_gb)} GB"]
        if tpl.gpus:
            parts.append(f"{tpl.gpus} GPU")
        parts.append(_ttl_text(ttl, bool(tpl.persistent)))
        ports = [p for p in getattr(tpl, "ports", None) or [] if isinstance(p, int)]
        if ports:
            parts.append("publishes port " + ", ".join(str(p) for p in ports)
                         + " on the host's loopback")
        names: list[str] = []
        try:
            preview = services.sandboxes.preview(template_id, host_id)
            names = [v.name for v in preview.variables
                     if v.will_send and v.source_kind == "secret"]
        except Exception:  # noqa: BLE001 - fall back to the template's own variable list
            names = [v.name for v in tpl.variables
                     if str(v.source).startswith("secret:")
                     and (v.hosts is None or host_id in v.hosts)]
        sends = f", sends {', '.join(f'{n} [redacted]' for n in names)}" if names else ""
        text = f"Spawn {tpl.id} on {host_name} ({', '.join(parts)}{sends})?"
        if host is not None and host.kind == "ec2" and host.ec2 is not None:
            from .ec2_chat import spawn_text
            text += spawn_text(host, host.ec2)
        return text
    except Exception:  # noqa: BLE001 - fail closed
        return GENERIC_SPAWN_MESSAGE


def approval_env_control(args: dict, services: Services) -> str | None:
    """Approval text for ``env_control`` (destroy, extend, restart after the time limit)."""
    return _approval.describe_env_control(args, services)


_SPAWN_SCHEMA = {
    "name": "env_spawn",
    "description": (
        "Start a sandboxed container environment. Needs template (an id such as python-dev "
        "or ubuntu-dev; compute_status lists them) and host_id (an ssh or local host). Optional: "
        "name, ttl_minutes, idle_minutes. Everything is checked before the user is asked to "
        "approve. Returns instance, preview, warnings and card. Cloud agents use "
        "cloud_agent_spawn; Slurm jobs use slurm_submit."),
    "parameters": {
        "type": "object",
        "properties": {
            "template": {"type": "string",
                         "description": "Template id, e.g. python-dev (not an agent template)."},
            "host_id": {"type": "string",
                        "description": "Host id from compute_status (an online ssh or local host)."},
            "name": {"type": "string", "description": "Optional display name."},
            "ttl_minutes": {"type": "integer", "minimum": 5, "maximum": 1440,
                            "description": ("Time limit: stop the environment after this many "
                                            "minutes. Its files are kept.")},
            "idle_minutes": {"type": "integer", "minimum": 5, "maximum": 1440,
                             "description": "Stop the environment after this many idle minutes."},
        },
        "required": ["template", "host_id"],
        "additionalProperties": False,
    },
}

_EXEC_SCHEMA = {
    "name": "env_exec",
    "description": (
        "Run a shell command (sh -lc) in a running environment. Needs instance_id and command; "
        "optional timeout_s (1-600) and workdir (default: the template's working folder). Each "
        "call is a fresh shell: variables do not persist, files do. Returns rc, stdout, stderr "
        "(the last 20 KB of each, secrets redacted), timed_out and card. Refused on a cloud "
        "agent's sandbox (it holds the model key)."),
    "parameters": {
        "type": "object",
        "properties": {
            "instance_id": {"type": "string", "description": "Instance id from env_spawn."},
            "command": {"type": "string", "description": "Shell command, run with sh -lc."},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": 600, "default": 120,
                          "description": "Give up after this many seconds."},
            "workdir": {"type": "string",
                        "description": "Working directory inside the environment (absolute path)."},
        },
        "required": ["instance_id", "command"],
        "additionalProperties": False,
    },
}

_CONTROL_SCHEMA = {
    "name": "env_control",
    "description": (
        "Manage one environment by instance_id. stop, start: nothing else needed; returns the "
        "instance. extend: needs minutes (1-10080). logs: optional tail (default 200); returns "
        "the container output, with a note when it printed nothing. destroy: saves the work "
        "folder to this computer first unless save is false, and deletes nothing if that fails; "
        "returns saved (export_id, path, bytes, files), message and copy_card. Destroy, extend "
        "and a restart after the time limit need approval."),
    "parameters": {
        "type": "object",
        "properties": {
            "instance_id": {"type": "string", "description": "Instance id from env_spawn."},
            "action": {"type": "string", "enum": list(TOOL_ACTIONS)},
            "tail": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 200,
                     "description": "Log lines to return (action logs only)."},
            "minutes": {"type": "integer", "minimum": EXTEND_MIN, "maximum": EXTEND_MAX,
                        "description": "Minutes to add to the time limit (action extend only)."},
            "save": {"type": "boolean", "default": True,
                     "description": ("Save the work folder to this computer before deleting "
                                     "(action destroy only). false deletes ALL files without "
                                     "saving.")},
        },
        "required": ["instance_id", "action"],
        "additionalProperties": False,
    },
}


def tools() -> list[ToolSpec]:
    return [
        ToolSpec(name="env_spawn", schema=_SPAWN_SCHEMA, handler=env_spawn,
                 approval=approval_env_spawn),
        ToolSpec(name="env_exec", schema=_EXEC_SCHEMA, handler=env_exec, approval=None),
        ToolSpec(name="env_control", schema=_CONTROL_SCHEMA, handler=env_control,
                 approval=approval_env_control),
    ]


# --------------------------------------------------------------------------- background

_stats_lock = threading.Lock()
_stats_inflight: set[str] = set()
_stats_cursor = 0
_reap_lock = threading.Lock()
_reap_inflight: set[str] = set()


def _config_seconds(services: Any, name: str, default: float) -> float:
    try:
        return float(getattr(services.config, name))
    except Exception:  # noqa: BLE001
        return default


def _stats_one(sandboxes: Any, host_id: str) -> None:
    try:
        sandboxes.refresh_stats(host_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("cloud-compute: stats for %s failed: %s", host_id, exc)
    finally:
        with _stats_lock:
            _stats_inflight.discard(host_id)


def run_stats(services: Services) -> None:
    """Sample container stats for hosts with running instances, within a bounded time.

    Hosts are visited round-robin across ticks; each runs in its own daemon thread with a
    short wait, so one slow host never holds the tick (it is skipped while still in flight).
    """
    global _stats_cursor
    started = time.monotonic()
    try:
        running = services.store.list_instances(statuses={"running"})
        sandboxes = services.sandboxes
    except Exception as exc:  # noqa: BLE001
        log.debug("cloud-compute: stats unavailable: %s", exc)
        return
    host_ids = sorted({r.host_id for r in running})
    if not host_ids:
        return
    with _stats_lock:
        offset = _stats_cursor % len(host_ids)
        _stats_cursor += 1
    for host_id in host_ids[offset:] + host_ids[:offset]:
        left = STATS_BUDGET_S - (time.monotonic() - started)
        if left <= 0.1:
            break
        with _stats_lock:
            if host_id in _stats_inflight:
                continue
            _stats_inflight.add(host_id)
        worker = _track_worker(threading.Thread(target=_stats_one, args=(sandboxes, host_id),
                                                name=f"cc-stats-{host_id}", daemon=True))
        worker.start()
        worker.join(min(STATS_HOST_TIMEOUT_S, left))


def _reap_one(reaper: Any, host_id: str) -> None:
    try:
        report = reaper.sweep_host(host_id)
        if getattr(report, "errors", None):
            log.debug("cloud-compute: reaper on %s reported %d error(s)", host_id, len(report.errors))
    except Exception as exc:  # noqa: BLE001
        log.debug("cloud-compute: reaper sweep of %s failed: %s", host_id, exc)
    finally:
        with _reap_lock:
            _reap_inflight.discard(host_id)


def run_reaper(services: Services) -> None:
    """Sweep every host in parallel daemon threads within an overall time budget.

    A host whose sweep outlives the budget keeps running in the background and is skipped on
    later ticks until it finishes, so one unreachable host never holds the task lease.
    """
    deadline = time.monotonic() + REAPER_BUDGET_S
    try:
        reaper = services.reaper
        host_ids = reaper.host_ids()
    except Exception as exc:  # noqa: BLE001
        log.debug("cloud-compute: reaper unavailable: %s", exc)
        return
    workers = []
    for host_id in host_ids:
        with _reap_lock:
            if host_id in _reap_inflight:
                continue
            _reap_inflight.add(host_id)
        worker = _track_worker(threading.Thread(target=_reap_one, args=(reaper, host_id),
                                                name=f"cc-reap-{host_id}", daemon=True))
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001
            with _reap_lock:
                _reap_inflight.discard(host_id)
            log.debug("cloud-compute: reaper thread for %s failed: %s", host_id, exc)
            continue
        workers.append(worker)
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))


def background_tasks() -> list[BackgroundTask]:
    return [
        BackgroundTask("instances.stats",
                       lambda s: _config_seconds(s, "stats_seconds", 60.0), run_stats),
        BackgroundTask("instances.reaper",
                       lambda s: _config_seconds(s, "reaper_seconds", 60.0), run_reaper),
    ]
