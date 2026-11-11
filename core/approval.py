"""Approval gate for tools that start, submit, destroy, extend or cancel remote work and for
file uploads, restores and saved-copy deletions (``pre_tool_call``).

``approval_for`` returns the directive Hermes turns into an approval card:
``{"action": "approve", "message": ..., "rule_key": ...}``. Read-only tools return None.
The check is fast (store lookups only, no network) and fails closed: any error still
yields an approval request with a generic message. Messages never contain secret values.

Hermes remembers a "session" or "always" answer by ``rule_key``. Every key therefore names
the action and its target (see :func:`rule_key`): approving one Extend never approves a
Delete, and approving one environment, run or saved copy never approves another.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .interfaces import ApiError

__all__ = [
    "APPROVAL_DEFAULTS", "approval_for", "make_pre_tool_call", "BlockedMessage", "check_params",
    "describe_env_control", "describe_env_files", "describe_agent_stop", "rule_key",
    "is_agent_sandbox", "AGENT_SANDBOX_MESSAGE", "bounded_text", "CARD_TEXT_LIMIT",
    "APPROVALS_OFF_WARNING", "hermes_approvals_mode", "approvals_status",
]

log = logging.getLogger(__name__)

class BlockedMessage(str):
    """What an approval describer returns when the call cannot proceed at all.

    ``approval_for`` turns it into a ``block`` directive carrying the text, so the model gets
    the reason at once and the user is never shown an approval card for it.
    """


def check_params(tool: str, args: dict, allowed: Any, *, context: str = "") -> None:
    """Raise ``unknown_parameter`` (HTTP 400) for any key in ``args`` that is not in ``allowed``.

    The message names the unknown parameters and lists the valid ones, so a model can fix
    the call in one step. ``context`` narrows the list, for example ``" for action destroy"``.
    """
    valid = sorted(allowed)
    unknown = sorted(str(k) for k in (args or {}) if k not in allowed)
    if unknown:
        names = ", ".join(repr(k) for k in unknown)
        raise ApiError(400, "unknown_parameter",
                       f"{tool} does not take {names}{context}. Valid parameters: "
                       f"{', '.join(valid)}.")


#: What the chat model is told when Hermes' own approvals are switched off.
APPROVALS_OFF_WARNING = (
    "Hermes approvals are switched off (approvals.mode: off), so requests to start, change or "
    "delete things on the user's machines run without an approval card. Tell the user, and "
    "say they can turn approvals on in Hermes settings.")


def _read_hermes_config() -> dict:
    """The Hermes profile's config, read-only (public Hermes API); ``{}`` when unreadable."""
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly()
    return config if isinstance(config, dict) else {}


def hermes_approvals_mode() -> str | None:
    """Hermes' own ``approvals.mode`` (``manual``, ``smart`` or ``off``), or ``None`` when it
    cannot be read. Hermes applies this setting itself; the plugin only reports it so the
    page and the chat model can say when no card will be shown. YAML turns an unquoted
    ``off`` into ``false``, which also means off."""
    try:
        section = _read_hermes_config().get("approvals")
        mode = section.get("mode") if isinstance(section, dict) else None
    except Exception:  # noqa: BLE001 - unknown is not off
        return None
    if mode is False:
        return "off"
    if isinstance(mode, str) and mode.strip():
        return mode.strip().lower()
    return None


def approvals_status() -> dict:
    """``{"mode": <approvals.mode or None>, "off": bool}`` for ``/summary``."""
    mode = hermes_approvals_mode()
    return {"mode": mode, "off": mode == "off"}


#: Every approval card of an EC2 host carries this word (see ec2_chat.MARK).
_EC2_MARK = "EC2"

GENERIC_MESSAGE = "Run this Own Compute action on a remote host?"
GENERIC_DESTROY_MESSAGE = ("Delete this environment? The files in its work folder may not be "
                           "saved first.")
GENERIC_FILES_MESSAGE = "Change files of an Own Compute environment?"


#: How much of a command, script, task or message an approval card shows.
CARD_TEXT_LIMIT = 1500


def bounded_text(text: Any, limit: int = CARD_TEXT_LIMIT) -> str:
    """``text`` for an approval card: the first ``limit`` characters, then ``(N more
    characters)`` when it is longer. Line breaks are kept (a script is shown as written)."""
    value = str(text if text is not None else "")
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... ({len(value) - limit} more characters)"


def _s(args: dict, key: str, default: str = "") -> str:
    v = args.get(key)
    return str(v) if v not in (None, "") else default


def _where(args: dict) -> str:
    return _s(args, "host_id", "the selected host")


def _ttl(minutes: Any) -> str:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return ""
    return f", TTL {m // 60} h" if m % 60 == 0 else f", TTL {m} min"


def _spawn(args: dict) -> str:
    return (f"Spawn {_s(args, 'template', 'a sandbox')} on {_where(args)}"
            f"{_ttl(args.get('ttl_minutes'))}?")


def _submit(args: dict) -> str:
    bits = [f"{args['cpus']} CPU"] if args.get("cpus") else []
    if args.get("gpus"):
        bits.append(f"{args['gpus']} GPU")
    if args.get("mem_gb"):
        bits.append(f"{args['mem_gb']} GB")
    extra = f" ({', '.join(bits)})" if bits else ""
    return f"Submit Slurm job {_s(args, 'name', '(unnamed)')} on {_where(args)}{extra}?"


def _agent_spawn(args: dict) -> str:
    return (f"Start a cloud agent on {_where(args)} for repo {_s(args, 'repo_path', '(unset)')} "
            f"(max {_s(args, 'max_turns', '30')} turns)?")


def _minutes_text(n: int) -> str:
    return "1 minute" if n == 1 else f"{n} minutes"


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _when(dt: datetime) -> str:
    """A time for an approval card: this computer's local time, else UTC."""
    try:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M") + " local time"
    except (OSError, ValueError, OverflowError):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"


def _instance(args: dict, services: Any) -> tuple[Any, str, str]:
    """``(record or None, display name, host name)`` from store lookups only."""
    instance_id = _s(args, "instance_id", "")
    rec = None
    try:
        rec = services.store.get_instance(instance_id) if instance_id else None
    except Exception:  # noqa: BLE001 - name what the arguments say
        rec = None
    name = str(getattr(rec, "name", "") or instance_id or "(unknown environment)")
    host_id = str(getattr(rec, "host_id", "") or _s(args, "host_id", ""))
    host_name = host_id or "the selected host"
    try:
        host = services.store.get_host(host_id) if host_id else None
        if host is not None:
            host_name = str(getattr(host, "name", "") or host_id)
    except Exception:  # noqa: BLE001
        pass
    return rec, name, host_name


def _workdir(rec: Any, services: Any) -> str:
    """The folder a Delete saves: the one the environment was created with (as files.py and
    the sandbox manager decide it), else its template's folder, else the words ``its work
    folder`` (never a guessed ``/work``)."""
    workdir = None
    try:
        ask = getattr(services.sandboxes, "work_folder", None)
        workdir = ask(rec) if callable(ask) else None
    except Exception:  # noqa: BLE001 - no sandbox service
        workdir = None
    if not (isinstance(workdir, str) and workdir.startswith("/")):
        try:
            workdir = getattr(services.templates.get(rec.template_id), "workdir", None)
        except Exception:  # noqa: BLE001 - template gone
            workdir = None
    return workdir if isinstance(workdir, str) and workdir.startswith("/") else "its work folder"


AGENT_SANDBOX_MESSAGE = ("This environment belongs to a cloud agent run and holds its model key. "
                         "Use cloud_agent_message to give it work, or open a terminal from the "
                         "Agents tab.")
_AGENT_TEMPLATES = ("hermes-agent", "agent-script")


def is_agent_sandbox(services: Any, rec: Any) -> bool:
    """True when ``rec`` is the sandbox of a cloud agent run. Four independent signs, any one
    counts: the file service's own test, a built-in agent template, a run record that names
    the environment, or the agent block of its template. A lookup that fails counts for
    nothing (it never turns an ordinary environment into an agent one)."""
    if rec is None:
        return False
    try:
        check = getattr(services.files, "is_agent_sandbox", None)
        if callable(check) and check(rec):
            return True
    except Exception:  # noqa: BLE001
        pass
    template_id = str(getattr(rec, "template_id", "") or "")
    if template_id in _AGENT_TEMPLATES:
        return True
    try:
        agent = getattr(services.templates.get(template_id), "agent", None)
        if isinstance(agent, dict) and agent.get("mode") in ("hermes", "script"):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        for run in services.store.list_records("agent_runs"):
            if isinstance(run, dict) and run.get("instance_id") == getattr(rec, "id", None):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _int_arg(args: dict, key: str) -> int | None:
    value = args.get(key)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


#: Parameters ``env_control`` accepts, by action. Shared by the tool handler and the approval.
ENV_CONTROL_COMMON = ("instance_id", "action")
ENV_CONTROL_PARAMS: dict[str, tuple[str, ...]] = {
    "stop": ENV_CONTROL_COMMON,
    "start": ENV_CONTROL_COMMON,
    "extend": ENV_CONTROL_COMMON + ("minutes",),
    "destroy": ENV_CONTROL_COMMON + ("save",),
    "logs": ENV_CONTROL_COMMON + ("tail",),
}


def check_env_control_params(args: dict) -> None:
    """``unknown_parameter`` for a key the chosen ``env_control`` action does not take."""
    action = (args or {}).get("action")
    if action in ENV_CONTROL_PARAMS:
        check_params("env_control", args, ENV_CONTROL_PARAMS[action],
                     context=f" with action {action}")
    else:
        check_params("env_control", args, set().union(*ENV_CONTROL_PARAMS.values()))


def describe_env_control(args: dict, services: Any) -> str | None:
    """Approval text for ``env_control``: destroy, extend, and a restart after the time limit.

    Store lookups only (no network). Stop, logs and an ordinary start need no approval. A call
    that cannot work (an unknown parameter, an unknown environment) raises :class:`BlockCall`
    instead of asking.
    """
    try:
        args = args if isinstance(args, dict) else {}
        action = args.get("action")
    except Exception:  # noqa: BLE001
        return GENERIC_DESTROY_MESSAGE
    if action not in ("destroy", "extend", "start"):
        return None
    try:
        check_env_control_params(args)
    except ApiError as exc:
        return BlockedMessage(exc.message)
    try:
        rec, name, host = _instance(args, services)
        if action == "destroy":
            workdir = _workdir(rec, services)
            if args.get("save", True) is False:
                return (f"Deletes {name} on {host} and every file in it. Nothing is saved, not "
                        f"even the files in {workdir}.")
            if not workdir.startswith("/"):
                return (f"Deletes {name} on {host}. Its work folder is not recorded and its "
                        "template is gone, so Own Compute cannot tell which folder to save: "
                        "the delete is refused and nothing is removed unless the files are "
                        "saved first.")
            return (f"Deletes {name} on {host}. The files in {workdir} are saved to this "
                    "computer first, and it is deleted only if that copy is complete. "
                    f"Files outside {workdir} are not saved.")
        if action == "extend":
            minutes = _int_arg(args, "minutes")
            if minutes is None or minutes <= 0:
                return f"Extends {name} by the requested time."
            now = datetime.now(timezone.utc)
            current = _parse_iso(getattr(rec, "expires_at", None)) or now
            until = max(now, current) + timedelta(minutes=minutes)
            return f"Extends {name} by {_minutes_text(minutes)} (until {_when(until)})."
        # start: only a restart after the time limit asks (it grants a new limit).
        if rec is None:
            return None
        try:
            minutes = services.sandboxes.lifetime_on_start(rec.id)
        except Exception:  # noqa: BLE001 - start reports its own error
            return None
        if not minutes:
            return None
        return f"Restarts {name} on {host} for another {_minutes_text(int(minutes))}."
    except Exception:  # noqa: BLE001 - fail closed
        return GENERIC_DESTROY_MESSAGE if action == "destroy" else GENERIC_MESSAGE


def describe_env_files(args: dict, services: Any) -> str | None:
    """Approval text for ``env_files`` from its arguments (plus the environment's name).

    ``put``, ``restore`` and ``delete_export`` ask; ``list``, ``fetch`` and ``exports`` do
    not. Any other action fails closed to a generic request.
    """
    try:
        args = args if isinstance(args, dict) else {}
        action = args.get("action")
        if action == "fetch":
            # A plain save to the exports folder is free; one to a chosen folder, or one that
            # unpacks, asks (files_api._fetch_approval asks in the same cases, and also for a
            # save of a known size above 200 MB; the fallback hook agrees).
            dest = _s(args, "dest_dir", "")
            extract = _flag(args.get("extract"))
            if not dest and not extract:
                return None
            _rec, name, _host = _instance(args, services)
            where = f"{dest} on this computer" if dest else "the exports folder on this computer"
            return (f"Saves {_s(args, 'path', 'the work folder')} from {name} to {where}"
                    + (", and unpacks it into a new folder there." if extract else "."))
        if action in ("list", "size", "exports"):
            return None
        if action == "delete_export":
            export_id = _s(args, "export_id", "(unknown)")
            return (f"Deletes the saved copy {export_id}{_export_file(export_id, services)} from "
                    "this computer. It cannot be recovered.")
        if action not in ("put", "restore"):
            return GENERIC_FILES_MESSAGE
        _rec, name, _host = _instance(args, services)
        dest = _s(args, "dest", "(default folder)")
        if action == "put":
            return (f"Uploads {_s(args, 'local_path', '(unknown file)')} from this computer to "
                    f"{name}:{dest}. Files with the same names are replaced.")
        return (f"Restores the saved copy {_s(args, 'export_id', '(unknown)')} into "
                f"{name}:{dest}. Files with the same names are replaced.")
    except Exception:  # noqa: BLE001 - fail closed
        return GENERIC_FILES_MESSAGE


def _export_file(export_id: str, services: Any) -> str:
    """`` (<file name>)`` of a saved copy from the store index, else empty."""
    try:
        data = services.store.get_record("export", export_id)
        name = str(data.get("local_path") or "").replace("\\", "/").rsplit("/", 1)[-1]
        return f" ({name})" if name else ""
    except Exception:  # noqa: BLE001 - unknown copy: the id alone names it
        return ""


def describe_agent_stop(args: dict, services: Any) -> str:
    """Approval text for ``cloud_agent_stop`` (store lookups only). Always asks: Stop deletes
    the run's sandbox after saving, and with ``keep_branch=false`` local branches too."""
    args = args if isinstance(args, dict) else {}
    run_id = _s(args, "run_id", "(unknown)")
    try:
        run = services.store.get_record("agent_runs", run_id)
    except Exception:  # noqa: BLE001 - name what the arguments say
        run = None
    run = run if isinstance(run, dict) else {}
    instance_id = str(run.get("instance_id") or "")
    text = f"Stops cloud agent run {run_id}."
    if instance_id or not run:
        workdir = "/work"
        if instance_id:
            try:
                rec = services.store.get_instance(instance_id)
                workdir = _workdir(rec, services) if rec is not None else workdir
            except Exception:  # noqa: BLE001
                pass
        text += (f" Its sandbox is deleted after the files in {workdir} are saved to this "
                 "computer.")
        text += " Commits the agent made that were not fetched yet are fetched to this computer first."
        if args.get("delete_anyway") is True:
            text += (" delete_anyway is set: the sandbox is deleted even if those commits "
                     "cannot be fetched; they would then be only in the saved copy of the files.")
        else:
            text += " If they cannot be fetched, nothing is deleted."
    if args.get("keep_branch", True) is False:
        ref = str(run.get("collected_ref") or "")
        ref = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        if ref:
            text += f" The collected branch {ref} on this computer is also deleted."
        else:
            text += (f" Any collected branches under cloud/{run_id}/ on this computer are "
                     "also deleted.")
    return text


def opens_host_port(args: dict) -> bool:
    """True for ``env_open kind=port`` with a ``host_id``: a forward of any loopback port of an
    ssh host to this computer (the instance form is limited to the published ports of its
    template)."""
    args = args if isinstance(args, dict) else {}
    return (str(args.get("kind") or "").strip().lower() == "port"
            and bool(str(args.get("host_id") or "").strip()))


def describe_env_open(args: dict, services: Any) -> str | None:
    """Approval text for ``env_open``: only a port forward to a host asks."""
    if not opens_host_port(args):
        return None
    try:
        host_id = _s(args, "host_id")
        name = host_id
        try:
            host = services.store.get_host(host_id)
            if host is not None:
                name = f"{host.name} ({host.id})"
        except Exception:  # noqa: BLE001 - the id alone names it
            pass
        port = _s(args, "remote_port", "(no port given)")
        return (f"Forwards port {port} on the loopback of {name} to a port on this computer "
                "through SSH. Anything listening on that port there (a database, an admin page, "
                "a container socket) can then be reached from this computer, and from any "
                "program on it, until the forward is closed.")
    except Exception:  # noqa: BLE001 - fail closed
        return GENERIC_MESSAGE


def _cancel(args: dict) -> str | None:
    if args.get("action") != "cancel":
        return None
    return f"Cancel Slurm job {_s(args, 'job_id', '(unknown)')} on {_where(args)}?"


def _agent_message(args: dict) -> str:
    return f"Send a follow-up to cloud agent run {_s(args, 'run_id', '(unknown)')}?"


def _agent_rerun(args: dict) -> str:
    return f"Start cloud agent run {_s(args, 'run_id', '(unknown)')} again as a new run?"


def _compute_host(args: dict) -> str | None:
    action = args.get("action")
    if action in ("start", "stop", "terminate", "extend"):
        return (f"{str(action).capitalize()} the EC2 machine of host {_s(args, 'host_id', '?')}? "
                "AWS bills the account while a machine runs, and for its disk while the machine "
                "exists.")
    if action not in ("add", "update", "remove"):
        return None
    target = _s(args, "host_id", "") or _s(args, "address", "") or _s(args, "name", "a host")
    verb = {"add": "Add", "update": "Change", "remove": "Remove"}[str(action)]
    return f"{verb} host {target} in Own Compute?"


#: Fail-closed defaults used when a ToolSpec has no ``approval`` of its own.
APPROVAL_DEFAULTS: dict[str, Callable[[dict], str | None]] = {
    "env_spawn": _spawn,
    "slurm_submit": _submit,
    "cloud_agent_spawn": _agent_spawn,
    "cloud_agent_message": _agent_message,
    "cloud_agent_rerun": _agent_rerun,
    "compute_host": _compute_host,
    "env_control": lambda args: describe_env_control(args, None),
    "slurm_jobs": _cancel,
}

#: Describers that also read the store (called with ``(args, services)``). Used only when a
#: tool has no ToolSpec approval of its own (``env_files`` ships one in files_api).
SERVICE_DESCRIBERS: dict[str, Callable[[dict, Any], str | None]] = {
    "env_control": describe_env_control,
    "env_files": describe_env_files,
    "env_open": describe_env_open,
    "cloud_agent_stop": describe_agent_stop,
}


def _resolve_host_id(args: dict, services: Any) -> str | None:
    """Host id from args, else via the instance/job record in the store."""
    host_id = args.get("host_id")
    if host_id:
        return str(host_id)
    try:
        store = services.store
        if args.get("instance_id"):
            rec = store.get_instance(str(args["instance_id"]))
            if rec is not None:
                return rec.host_id
        if args.get("job_id"):
            rec = store.get_job(str(args["job_id"]))
            if rec is not None:
                return rec.host_id
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_local(host_id: str | None, services: Any) -> bool:
    if not host_id:
        return False
    host = services.store.get_host(host_id)
    return host is not None and getattr(host, "kind", None) == "local"


def _part(value: Any) -> str:
    """One rule-key component: printable, without the separator, bounded."""
    text = str(value) if value not in (None, "") else "-"
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)[:80] or "-"


def values_digest(values: dict, skip: tuple[str, ...] = ()) -> str:
    """Short hash of the given values (``None`` values and the ``skip`` names left out), so a
    rule key changes whenever any value the user was shown changes."""
    try:
        text = json.dumps({str(k): v for k, v in sorted((values or {}).items())
                           if k not in skip and v is not None}, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        text = repr(sorted(str(k) for k in (values or {})))
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _flag(value: Any) -> bool:
    """The one meaning of a yes/no argument for approvals (same as the fallback hook)."""
    return value is True or (isinstance(value, str)
                             and value.strip().lower() in ("true", "1", "yes", "on"))


def rule_key(tool_name: str, args: dict, host_id: str | None, message: str | None = None) -> str:
    """The key Hermes caches a "session"/"always" answer under: tool, action and target.

    ``env_control``: ``extend|start|destroy-save|destroy-nosave:<instance_id>``;
    ``env_files``: ``put:<instance_id>:<hash of file, destination, unpack>``,
    ``restore:<instance_id>:<export_id>:<hash of destination>``, ``delete-export:<export_id>``;
    ``cloud_agent_stop``: ``stop|stop-delete-branch[-delete-anyway]:<run_id>``; ``slurm_jobs`` cancel:
    ``cancel:<job_id>``; spawns: ``spawn:<host>:<template or repo>``; submits: ``<host>``.
    """
    args = args if isinstance(args, dict) else {}
    base = f"cloud-compute:{tool_name}"
    action = str(args.get("action") or "")
    if tool_name == "env_control":
        if action == "destroy":
            action = "destroy-nosave" if args.get("save", True) is False else "destroy-save"
        return f"{base}:{_part(action)}:{_part(args.get('instance_id'))}"
    if tool_name == "env_open":
        return f"{base}:port:{_part(args.get('host_id'))}:{_part(args.get('remote_port'))}"
    if tool_name == "env_files":
        if action == "delete_export":
            return f"{base}:delete-export:{_part(args.get('export_id'))}"
        # env_files fetch: one key per copy (path, dest_dir, extract)
        if action == "fetch":
            from .files_api import fetch_rule_part
            return f"{base}:fetch:{_part(args.get('instance_id'))}:{fetch_rule_part(args)}"
        # One key per upload (file, destination, unpack) and per restore (copy, destination), so
        # an answer for one file or copy never approves another.
        if action == "put":
            digest = values_digest({"local_path": str(args.get("local_path") or "").strip(),
                                    "dest": str(args.get("dest") or "").strip(),
                                    "extract": _flag(args.get("extract"))})
            return f"{base}:put:{_part(args.get('instance_id'))}:{digest}"
        if action == "restore":
            digest = values_digest({"dest": str(args.get("dest") or "").strip()})
            return (f"{base}:restore:{_part(args.get('instance_id'))}:"
                    f"{_part(args.get('export_id'))}:{digest}")
        return f"{base}:{_part(action)}:{_part(args.get('instance_id'))}"
    # Cloud agent tools (core/agents_api.py): a key per run and message; a spawn's key names its model.
    if tool_name == "cloud_agent_message":
        digest = hashlib.sha256(str(args.get("message") or "").encode("utf-8", "replace")
                                ).hexdigest()[:16]
        return f"{base}:message:{_part(args.get('run_id'))}:{digest}"
    if tool_name == "cloud_agent_rerun":
        text = "|".join(str(args.get(k) or "") for k in
                        ("task", "model_id", "model", "base_url", "host_id", "max_turns"))
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        return f"{base}:rerun:{_part(args.get('run_id'))}:{digest}"
    if tool_name == "cloud_agent_spawn":
        # Everything that decides what runs and what it may spend: the template (a handoff
        # test sends no secrets, a model run sends the key), the model choice, the turn limit,
        # the whole task and criteria, and the card text (which names the resolved model and
        # endpoint), so one answer never covers a different run.
        digest = values_digest({
            "template": str(args.get("template") or "").strip(),
            "model_id": str(args.get("model_id") or "").strip(),
            "max_turns": str(args.get("max_turns") or "").strip(),
            "task": str(args.get("task") or args.get("goal") or ""),
            "criteria": str(args.get("criteria") or ""),
            "repo_path": str(args.get("repo_path") or "").strip(),
            "branch": str(args.get("branch") or "").strip(),
            "new_branch": str(args.get("new_branch", True)),
            "base": str(args.get("base") or "").strip(),
            "card": message or ""})
        model = f":{_part(args.get('model_id'))}" if args.get("model_id") else ""
        return (f"{base}:spawn:{_part(host_id)}:{_part(args.get('repo_path'))}{model}:"
                f"{_part(args.get('template'))}:{digest}")
    if tool_name == "cloud_agent_stop":
        kind = "stop-delete-branch" if args.get("keep_branch", True) is False else "stop"
        if args.get("delete_anyway") is True:      # its own answer: it can lose the commits
            kind += "-delete-anyway"
        return f"{base}:{kind}:{_part(args.get('run_id'))}"
    if tool_name == "slurm_jobs":
        return f"{base}:{_part(action)}:{_part(args.get('job_id'))}"
    if tool_name == "env_spawn":
        key = f"{base}:spawn:{_part(host_id)}:{_part(args.get('template'))}"
        if message and _EC2_MARK in message:     # EC2: the card's region/type/disk/lifetime
            key += ":" + values_digest({"card": message})
        return key
    # --- compute_host: a key names the values the card showed, so one answer never covers a
    # different address, user, key file or setting ---
    if tool_name == "compute_host":
        # An EC2 card (it carries the word EC2) is hashed whole with the arguments: it names the
        # saved values too (old -> new, region, type, disk, lifetime, who may use SSH), so one
        # answer never covers a different value, host or machine state.
        ec2 = {"card": message} if message and _EC2_MARK in message else {}
        if action == "add":
            return (f"{base}:add:{_part(args.get('kind'))}:{_part(args.get('address'))}:"
                    f"{_part(args.get('name'))}:{values_digest({**args, **ec2}, ('action',))}")
        if action == "update":
            return (f"{base}:update:{_part(args.get('host_id'))}:"
                    f"{values_digest({**args, **ec2}, ('action', 'host_id', 'kind'))}")
        if action == "test":
            return (f"{base}:test:{_part(args.get('host_id'))}:"
                    f"{values_digest({**args, **ec2}, ('action',))}")
        if action == "remove" and ec2:
            return (f"{base}:remove:{_part(args.get('host_id'))}:"
                    f"{values_digest(ec2)}")
        if action in ("start", "stop", "terminate", "extend"):
            # An EC2 machine: the card names region, type, disk and lifetime (and, for
            # terminate, whether environments are deleted and saved); the key covers all.
            return (f"{base}:{action}:{_part(args.get('host_id'))}:"
                    f"{values_digest({**args, 'card': message or ''}, ('action',))}")
        return f"{base}:{_part(action)}:{_part(args.get('host_id'))}"
    # --- end compute_host ---
    if tool_name == "slurm_submit":
        # One key per job: the command or script and every resource, so an answer for one job
        # never covers a different command on the same cluster.
        digest = values_digest({k: str(args.get(k)).strip() if isinstance(args.get(k), str)
                                else args.get(k)
                                for k in ("name", "command", "script", "partition", "cpus",
                                          "mem_gb", "gpus", "time_limit", "image", "array")})
        return f"{base}:{_part(host_id)}:{digest}"
    return f"{base}:{_part(host_id)}"


def _always_asks(tool_name: str, args: dict) -> bool:
    """Deletions on this computer, or without a save: these ask even in "remote-only"
    mode, because a local deletion is still data loss."""
    if tool_name == "env_control":
        return args.get("action") == "destroy" and args.get("save", True) is False
    if tool_name == "env_files":
        if args.get("action") == "fetch":      # a write to a chosen folder on this computer, or
            return bool(str(args.get("dest_dir") or "").strip()) or _flag(args.get("extract"))
        return args.get("action") == "delete_export"
    if tool_name == "cloud_agent_stop":
        return args.get("keep_branch", True) is False or args.get("delete_anyway") is True
    if tool_name == "compute_host":
        # A new EC2 host stores references to AWS keys and settings that decide later spending:
        # it asks in every approval mode, whatever host_id a call carries.
        return (args.get("action") == "add"
                and str(args.get("kind") or "").strip().lower() == "ec2")
    return False


def _fail_closed(tool_name: str, args: Any, services: Any = None) -> dict:
    """A generic request whose key is unique to these arguments, so a remembered answer to
    one unverified call never approves a different one."""
    try:
        text = repr(sorted((args or {}).items()))
    except Exception:  # noqa: BLE001
        text = repr(args)
    if tool_name == "env_spawn" or (tool_name == "compute_host" and isinstance(args, dict)
                                    and str(args.get("action")) in ("start", "extend")):
        text += "|" + uuid.uuid4().hex       # a machine start: no remembered answer covers a later one
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return {"action": "approve", "message": GENERIC_MESSAGE,
            "rule_key": f"cloud-compute:{tool_name}:unverified:{digest}"}


def approval_for(tool_name: str, args: dict, tool: Any, services: Any) -> dict | None:
    """Approve directive for ``tool_name`` or None. Any error returns a generic approve."""
    try:
        args = args if isinstance(args, dict) else {}
        approval = getattr(tool, "approval", None) if tool is not None else None
        if approval is not None:
            message = approval(args, services)
        elif tool_name in SERVICE_DESCRIBERS:
            message = SERVICE_DESCRIBERS[tool_name](args, services)
        elif tool_name in APPROVAL_DEFAULTS:
            message = APPROVAL_DEFAULTS[tool_name](args)
        else:
            return None
        if not message:
            return None
        if isinstance(message, BlockedMessage):
            return {"action": "block", "message": str(message)}
        host_id = _resolve_host_id(args, services)
        mode = getattr(services.config, "approval", "always")
        # The plugin's own setting is honoured as the user chose it (catalog rule 12: the
        # plugin never routes around the user's choices). Hermes applies its own bypasses
        # (yolo, approvals.mode: off) itself. config.py offers "always" and "remote-only";
        # an "off" value, should one ever be added there, turns these prompts off.
        if mode == "off":
            return None
        if (mode == "remote-only" and _is_local(host_id, services)
                and not _always_asks(tool_name, args)):
            return None
        return {"action": "approve", "message": str(message),
                "rule_key": rule_key(tool_name, args, host_id, str(message))}
    except Exception as exc:  # noqa: BLE001 - fail closed
        log.debug("cloud-compute approval check failed for %s: %s", tool_name, exc)
        return _fail_closed(tool_name, args, services)


def make_pre_tool_call(get_tools: Callable[[], dict[str, Any]],
                       get_services: Callable[[], Any]) -> Callable[..., dict | None]:
    """Hermes ``pre_tool_call`` callback; tools that are not ours return None; never raises."""

    def pre_tool_call(**kwargs: Any) -> dict | None:
        name = kwargs.get("tool_name")
        args = kwargs.get("args")
        try:
            if not isinstance(name, str):
                return None
            tools = get_tools() or {}
            if (name not in tools and name not in APPROVAL_DEFAULTS
                    and name not in SERVICE_DESCRIBERS):
                return None
            return approval_for(name, args if isinstance(args, dict) else {},
                                tools.get(name), get_services())
        except Exception as exc:  # noqa: BLE001 - fail closed for our own tools
            log.debug("cloud-compute approval hook error: %s", exc)
            if isinstance(name, str) and (name in APPROVAL_DEFAULTS or name in SERVICE_DESCRIBERS):
                return _fail_closed(name, args)
            return None

    return pre_tool_call
