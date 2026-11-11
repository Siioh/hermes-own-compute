"""Cloud agent routes, Hermes tools and the status poll.

Routes (relative paths): ``GET /agents``, ``POST /agents``, ``GET /agents/{run_id}``,
``DELETE /agents/{run_id}``, ``POST /agents/{run_id}/collect``, ``POST /agents/{run_id}/stop``,
``POST /agents/{run_id}/message``, ``POST /agents/{run_id}/rerun``; saved model choices: ``GET /agent-models``,
``POST /agent-models``, ``DELETE /agent-models/{model_id}``,
``POST /agent-models/{model_id}/default``.

Every handler calls ``services.agents`` at call time only, so this module imports
without the manager module being present.

Standard library only.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .agent_models import tidy as tidy_models
from .interfaces import ApiError, ApiRequest, BackgroundTask, Route, Services, ToolSpec

__all__ = ["routes", "tools", "background_tasks", "POLL_INTERVAL_S", "MAX_LOG_LINES"]

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 15.0
#: Log lines returned per run by ``cloud_agent_status``.
MAX_LOG_LINES = 20

DEFAULT_TEMPLATE = "hermes-agent"
DEFAULT_BASE = "HEAD"
DEFAULT_MAX_TURNS = 30
COLLECT_TARGETS = ("worktree", "branch")

#: Hermes stops one tool call after 420 s (agent/tool_executor.py) and a plugin cannot raise
#: that, so a blocking wait plus the work before it stays under this.
WAIT_HARD_CAP_S = 360
DEFAULT_WAIT_MAX_S = 300

SPAWN_FIELDS = ("task", "goal", "criteria", "host_id", "repo_path", "branch", "new_branch",
                "base", "template", "max_turns", "model_id", "wait_s")
STATUS_FIELDS = ("run_id", "wait_s", "models")
COLLECT_FIELDS = ("run_id", "into")
STOP_FIELDS = ("run_id", "keep_branch", "delete_anyway")
MESSAGE_FIELDS = ("run_id", "message", "max_turns", "wait_s")
RERUN_FIELDS = ("run_id", "task", "model_id", "model", "base_url", "host_id", "max_turns",
                "wait_s")
#: Why a failed run failed when the cause is the model call (same list as core/agents.py): a
#: follow-up in the same sandbox would only repeat it, so ``next`` points to a new run.
MODEL_FAILURE_KINDS = ("model_not_found", "bad_key", "unreachable", "rate_limit", "server",
                       "context", "endpoint")

# Injectable for tests.
_monotonic = time.monotonic


# --------------------------------------------------------------------------- argument parsing


def _invalid(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _reject_unknown(args: dict, valid: tuple[str, ...], what: str) -> None:
    """``unknown_parameter`` naming every unexpected key and listing the valid ones."""
    extra = sorted(str(k) for k in set(args) - set(valid))
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


def _text(args: dict, name: str, *, required: bool = False, default: str | None = None) -> str | None:
    value = args.get(name)
    if value is None:
        if required:
            raise _invalid(f"{name} is required")
        return default
    if not isinstance(value, str):
        raise _invalid(f"{name} must be a string")
    value = value.strip()
    if not value:
        if required:
            raise _invalid(f"{name} is required")
        return default
    return value


def _flag(args: dict, name: str, default: bool) -> bool:
    value = args.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _invalid(f"{name} must be true or false")
    return value


def _turns(args: dict, default: int | None = DEFAULT_MAX_TURNS) -> int | None:
    value = args.get("max_turns")
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid("max_turns must be an integer")
    if value < 1:
        raise _invalid("max_turns must be at least 1")
    return value


def _spawn_kwargs(args: dict) -> dict[str, Any]:
    """Validate spawn arguments; returns the keyword arguments for ``AgentManager.spawn``.
    ``task`` is the preferred name of the instruction; ``goal`` still works."""
    if not isinstance(args, dict):
        raise _invalid("arguments must be an object")
    task = _text(args, "task")
    goal = _text(args, "goal")
    if task and goal and task != goal:
        raise _invalid("give the instruction once, as task (goal is the older name)")
    instruction = task or goal
    if not instruction:
        raise _invalid("task is required: say what the agent should do")
    return {
        "goal": instruction,
        "host_id": _text(args, "host_id", required=True),
        "repo_path": _text(args, "repo_path", required=True),
        "branch": _text(args, "branch"),
        "new_branch": _flag(args, "new_branch", True),
        "base": _text(args, "base", default=DEFAULT_BASE),
        "template": _text(args, "template", default=DEFAULT_TEMPLATE),
        "max_turns": _turns(args),
        "model_id": _text(args, "model_id"),
        "criteria": _text(args, "criteria"),
    }


def _into(args: dict) -> str:
    into = _text(args, "into", default="worktree")
    if into not in COLLECT_TARGETS:
        raise _invalid("into must be 'worktree' or 'branch'")
    return into


def _path_id(path_params: dict, name: str = "run_id") -> str:
    value = (path_params or {}).get(name)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{name} is required")
    return value.strip()


def _run_id(path_params: dict) -> str:
    return _path_id(path_params, "run_id")


def _dict(obj: Any) -> dict[str, Any]:
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return dict(obj)


def _wait_cap(services: Services) -> int:
    """Longest wait: the ``wait_max_seconds`` setting (default 300), at most 360 s."""
    try:
        value = getattr(services.config, "wait_max_seconds", DEFAULT_WAIT_MAX_S)
    except Exception:  # noqa: BLE001 - config unavailable
        value = DEFAULT_WAIT_MAX_S
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        value = DEFAULT_WAIT_MAX_S
    return int(max(1, min(WAIT_HARD_CAP_S, value)))


def _wait_s(args: dict) -> int | None:
    value = args.get("wait_s")
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise _invalid("wait_s must be a number of seconds")
    if value < 0:
        raise _invalid("wait_s must be 0 or more")
    return int(value)


# --------------------------------------------------------------------------- routes


def list_agents(req: ApiRequest) -> list:
    run_id = (req.query or {}).get("run_id") or None
    return [_dict(run) for run in req.services.agents.status(run_id)]


def spawn_agent(req: ApiRequest) -> dict:
    body = _body(req)
    kwargs = _spawn_kwargs(body)
    session_id = _text(body, "session_id")
    run = req.services.agents.spawn(session_id=session_id, **kwargs)
    return _dict(run)


def get_agent(req: ApiRequest) -> dict:
    run_id = _run_id(req.path_params)
    runs = req.services.agents.status(run_id)
    if not runs:
        raise ApiError(404, "not_found", f"agent run {run_id} not found")
    return _dict(runs[0])


def collect_agent(req: ApiRequest) -> dict:
    run_id = _run_id(req.path_params)
    into = _into(_body(req))
    return _dict(req.services.agents.collect(run_id, into=into))


def stop_agent(req: ApiRequest) -> dict:
    run_id = _run_id(req.path_params)
    body = _body(req)
    _reject_unknown(body, tuple(f for f in STOP_FIELDS if f != "run_id"), "POST /agents/{id}/stop")
    return _dict(req.services.agents.stop(run_id, keep_branch=_flag(body, "keep_branch", True),
                                          delete_anyway=_flag(body, "delete_anyway", False)))


def message_agent(req: ApiRequest) -> dict:
    run_id = _run_id(req.path_params)
    body = _body(req)
    _reject_unknown(body, ("message", "max_turns"), "POST /agents/{id}/message")
    message = _text(body, "message", required=True)
    return _dict(req.services.agents.message(run_id, message, max_turns=_turns(body, None)))


def _rerun_kwargs(body: dict) -> dict[str, Any]:
    """Validated overrides of a rerun (all optional): task, model_id, model, base_url,
    host_id, max_turns."""
    return {
        "task": _text(body, "task"),
        "model_id": _text(body, "model_id"),
        "model": _text(body, "model"),
        "base_url": _text(body, "base_url"),
        "host_id": _text(body, "host_id"),
        "max_turns": _turns(body, None),
    }


def rerun_agent(req: ApiRequest) -> dict:
    run_id = _run_id(req.path_params)
    body = _body(req)
    _reject_unknown(body, tuple(f for f in RERUN_FIELDS if f not in ("run_id", "wait_s"))
                    + ("session_id",), "POST /agents/{id}/rerun")
    run = req.services.agents.rerun(run_id, session_id=_text(body, "session_id"),
                                    **_rerun_kwargs(body))
    return _dict(run)


def remove_agent(req: ApiRequest) -> dict:
    return dict(req.services.agents.remove(_run_id(req.path_params)))


def list_models(req: ApiRequest) -> dict:
    agents = req.services.agents
    models = [agents.model_view(m) for m in agents.models()]
    default = next((m["id"] for m in models if m.get("is_default")), None)
    unavailable = getattr(agents, "unavailable_models", None)
    # Each endpoint and model once: a saved choice hides its identical profile entry.
    detected, unavailable_items = tidy_models(
        models, list(agents.detected_models()), list(unavailable()) if callable(unavailable) else [])
    return {"models": models, "detected": detected, "unavailable": unavailable_items,
            "default_id": default}


def save_model(req: ApiRequest) -> dict:
    body = _body(req)
    _reject_unknown(body, ("id", "name", "base_url", "model", "key_secret", "is_default"),
                    "POST /agent-models")
    agents = req.services.agents
    return agents.model_view(agents.save_model(body))


def delete_model(req: ApiRequest) -> dict:
    return dict(req.services.agents.delete_model(_path_id(req.path_params, "model_id")))


def default_model(req: ApiRequest) -> dict:
    agents = req.services.agents
    return agents.model_view(agents.set_default_model(_path_id(req.path_params, "model_id")))


def routes() -> list[Route]:
    return [
        Route("GET", "/agents", list_agents, "List cloud agent runs (optional run_id)"),
        Route("POST", "/agents", spawn_agent, "Start a cloud agent on a branch of a local repo"),
        Route("GET", "/agents/{run_id}", get_agent, "One cloud agent run"),
        Route("DELETE", "/agents/{run_id}", remove_agent,
              "Remove the record of a run that is not running and has no sandbox"),
        Route("POST", "/agents/{run_id}/collect", collect_agent,
              "Bring an agent's branch back to this computer"),
        Route("POST", "/agents/{run_id}/stop", stop_agent, "Stop a cloud agent"),
        Route("POST", "/agents/{run_id}/message", message_agent,
              "Send a follow-up to a cloud agent whose turn ended"),
        Route("POST", "/agents/{run_id}/rerun", rerun_agent,
              "Start a new run from a finished run, optionally with another model, task or host"),
        Route("GET", "/agent-models", list_models, "Saved cloud-agent model choices"),
        Route("POST", "/agent-models", save_model, "Create or update a saved model choice"),
        Route("DELETE", "/agent-models/{model_id}", delete_model, "Delete a saved model choice"),
        Route("POST", "/agent-models/{model_id}/default", default_model,
              "Make a saved model choice the default"),
    ]


# --------------------------------------------------------------------------- tools


def _error(exc: BaseException) -> dict:
    if isinstance(exc, ApiError):
        return exc.to_dict()
    log.warning("cloud-compute: agent tool failed: %s", type(exc).__name__, exc_info=True)
    return {"error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}}


def _trim_log(run: dict) -> dict:
    lines = run.get("last_log")
    if isinstance(lines, list) and len(lines) > MAX_LOG_LINES:
        run = dict(run)
        run["last_log"] = lines[-MAX_LOG_LINES:]
    return run


def _card(run_id: Any) -> str:
    return f'::cc-agent{{id="{run_id}"}}'


def _next(run: dict) -> str:
    """What to do next with a run, in one sentence a model can act on."""
    rid = run.get("id")
    status = run.get("status")
    ahead = run.get("commits_ahead") or 0
    if status in ("starting", "running"):
        return (f'Still running. Call cloud_agent_status(run_id="{rid}", wait_s=300) to keep '
                "waiting.")
    if status == "done":
        if ahead:
            return (f'Call cloud_agent_collect(run_id="{rid}") to bring the branch to this '
                    "computer.")
        return ("The agent made no commits. Read the reply; to ask for more, call "
                f'cloud_agent_message(run_id="{rid}", message="...").')
    if status == "failed":
        if run.get("error_kind") in MODEL_FAILURE_KINDS:
            # A follow-up would reuse the same model, key and endpoint and fail the same way.
            return ("Read error_text: the model, the key or the endpoint has to change first. "
                    "Start it again with another model: "
                    f'cloud_agent_rerun(run_id="{rid}", model_id="m-...") (saved models: '
                    f'cloud_agent_status models=true), or model="<id>" for another model at the '
                    f'same endpoint. To keep partial work, call cloud_agent_collect(run_id="{rid}").')
        return ("Read error and last_log. To start it again (optionally with a changed task, "
                f'model_id or host_id), call cloud_agent_rerun(run_id="{rid}"); to retry in the '
                f'same sandbox, call cloud_agent_message(run_id="{rid}", message="..."); to keep '
                f'partial work, call cloud_agent_collect(run_id="{rid}").')
    if status == "collected":
        where = run.get("worktree_path") or run.get("collected_ref")
        return (f"The branch is on this computer ({where}). To continue, call "
                f'cloud_agent_message(run_id="{rid}", message="..."), then collect again.')
    if status == "stopped" and run.get("instance_id"):
        return ("Stop kept the sandbox, so nothing was deleted. Read error: if the agent's newest "
                f'commits could not be fetched, call cloud_agent_collect(run_id="{rid}") to try '
                "again; the sandbox is deleted once they are fetched and cloud_agent_stop is "
                "called again.")
    if status == "stopped":
        return ("The sandbox is gone. To repeat the run, call "
                f'cloud_agent_rerun(run_id="{rid}"); otherwise start a new run with '
                "cloud_agent_spawn and pass base set to the collected branch to continue from it.")
    return ""


def _run_result(run: Any, services: Services, wait_s: int | None, started: float) -> dict:
    """``{run, card, status, reply, commits_ahead, next}``, plus the wait outcome when asked."""
    data = _trim_log(_dict(run))
    rid = data.get("id")
    result: dict[str, Any] = {"run": data, "card": _card(rid)}
    notes: list[str] = []
    if wait_s:
        cap = _wait_cap(services)
        budget = min(wait_s, cap)
        budget = int(max(0, min(budget, WAIT_HARD_CAP_S - (_monotonic() - started))))
        if wait_s > cap:
            notes.append(f"wait_s was capped at {cap} s (the wait_max_seconds setting and the "
                         "time limit of one tool call).")
        waited, finished = 0.0, data.get("status") not in ("starting", "running")
        if not finished and budget > 0:
            run, finished, waited = services.agents.wait(rid, budget)
            data = _trim_log(_dict(run))
            result["run"] = data
        result["finished"] = finished
        result["timed_out"] = not finished
        result["waited_s"] = waited
        if not finished:
            notes.append(f"The wait timed out after {waited:g} s; the run is still "
                         f"{data.get('status')}.")
    result["status"] = data.get("status")
    result["reply"] = data.get("reply")
    result["commits_ahead"] = data.get("commits_ahead")
    if data.get("status") == "failed" and data.get("error"):
        result["error_text"] = data.get("error")
    result["next"] = _next(data)
    if notes:
        result["note"] = " ".join(notes)
    return result


def _tool_spawn(args: dict, services: Services, ctx: dict) -> dict:
    try:
        started = _monotonic()
        args = args if isinstance(args, dict) else {}
        _reject_unknown(args, SPAWN_FIELDS, "cloud_agent_spawn")
        kwargs = _spawn_kwargs(args)
        wait_s = _wait_s(args)
        session_id = (ctx or {}).get("session_id") or None
        run = services.agents.spawn(session_id=session_id, **kwargs)
        result = _run_result(run, services, wait_s, started)
        if result["status"] == "failed" and not result.get("waited_s"):
            result["next"] = ("The run did not start. Fix the problem in error_text, then call "
                              "cloud_agent_spawn again with the same arguments.")
        return result
    except Exception as exc:  # noqa: BLE001 - tool handlers never raise
        return _error(exc)


def _model_line(m: Any, services: Services) -> dict:
    view = services.agents.model_view(m)
    return {k: view.get(k) for k in ("id", "name", "model", "endpoint_host", "key_set", "is_default")}


def _tool_status(args: dict, services: Services, ctx: dict) -> dict:
    try:
        started = _monotonic()
        args = args if isinstance(args, dict) else {}
        _reject_unknown(args, STATUS_FIELDS, "cloud_agent_status")
        run_id = _text(args, "run_id")
        wait_s = _wait_s(args)
        models = _flag(args, "models", False)
        if wait_s and not run_id:
            raise _invalid("wait_s needs run_id: say which run to wait for")
        if run_id:
            runs = services.agents.status(run_id)
            if not runs:
                raise ApiError(404, "not_found", f"agent run {run_id} not found")
            result = _run_result(runs[0], services, wait_s, started)
        else:
            result = {"runs": [_trim_log(_dict(r)) for r in services.agents.status(None)]}
        if models:
            result["models"] = [_model_line(m, services) for m in services.agents.models()]
            if not result["models"]:
                result["models_note"] = ("No cloud-agent models are saved. Tell the user: add one "
                                         "in Compute, Agents, New agent run, Model.")
        return result
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _tool_collect(args: dict, services: Services, ctx: dict) -> dict:
    try:
        args = args if isinstance(args, dict) else {}
        _reject_unknown(args, COLLECT_FIELDS, "cloud_agent_collect")
        run_id = _text(args, "run_id", required=True)
        result = _dict(services.agents.collect(run_id, into=_into(args)))
        result["card"] = _card(run_id)
        result["next"] = ("Nothing was merged. Review the branch or worktree, then merge it "
                          "yourself; send more work with cloud_agent_message.")
        return result
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _tool_stop(args: dict, services: Services, ctx: dict) -> dict:
    try:
        args = args if isinstance(args, dict) else {}
        _reject_unknown(args, STOP_FIELDS, "cloud_agent_stop")
        run_id = _text(args, "run_id", required=True)
        run = services.agents.stop(run_id, keep_branch=_flag(args, "keep_branch", True),
                                   delete_anyway=_flag(args, "delete_anyway", False))
        data = _dict(run)
        result = {"run": data, "card": _card(run_id)}
        if data.get("stop_note"):
            result["note"] = data["stop_note"]        # what Stop fetched, or where commits are
        return result
    except ApiError as exc:
        out = _error(exc)
        if exc.code == "not_fetched":
            out["next"] = ("Nothing was deleted. Tell the user why. Call cloud_agent_collect to "
                           "try fetching again, or call cloud_agent_stop again with "
                           "delete_anyway=true only if the user agrees to delete the sandbox "
                           "with the commits only in the saved copy.")
        return out
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _tool_message(args: dict, services: Services, ctx: dict) -> dict:
    try:
        started = _monotonic()
        args = args if isinstance(args, dict) else {}
        _reject_unknown(args, MESSAGE_FIELDS, "cloud_agent_message")
        run_id = _text(args, "run_id", required=True)
        message = _text(args, "message", required=True)
        wait_s = _wait_s(args)
        run = services.agents.message(run_id, message, max_turns=_turns(args, None))
        return _run_result(run, services, wait_s, started)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _tool_rerun(args: dict, services: Services, ctx: dict) -> dict:
    try:
        started = _monotonic()
        args = args if isinstance(args, dict) else {}
        _reject_unknown(args, RERUN_FIELDS, "cloud_agent_rerun")
        run_id = _text(args, "run_id", required=True)
        wait_s = _wait_s(args)
        old = services.agents.status(run_id)
        old_branch = old[0].branch if old else None
        session_id = (ctx or {}).get("session_id") or None
        run = services.agents.rerun(run_id, session_id=session_id, **_rerun_kwargs(args))
        result = _run_result(run, services, wait_s, started)
        result["rerun_of"] = run_id
        if old_branch and getattr(run, "branch", None) and run.branch != old_branch:
            result["note"] = (f"The branch {old_branch} already exists in the repository, so the "
                              f"new run works on {run.branch}. Nothing was overwritten.")
        if result["status"] == "failed" and not result.get("waited_s"):
            result["next"] = ("The new run did not start. Fix the problem in error_text, then call "
                              "cloud_agent_rerun again.")
        return result
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _rerun_approval(args: dict, services: Services) -> str | None:
    """Same text as a new run with the task, host, repository, branch and model it would use."""
    return services.agents.describe_rerun(args)


def _spawn_approval(args: dict, services: Services) -> str | None:
    return services.agents.describe_spawn(args)


def _message_approval(args: dict, services: Services) -> str | None:
    return services.agents.describe_message(args)


def _stop_approval(args: dict, services: Services) -> str | None:
    """Stop always asks: it deletes the run's sandbox (after saving the files in its
    workdir) and, with ``keep_branch=false``, the collected branches on this computer."""
    from .approval import describe_agent_stop
    return describe_agent_stop(args, services)


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }


def _wait_prop() -> dict:
    return {"type": "integer", "minimum": 0,
            "description": "Seconds to block until the run finishes (capped by the "
                           "wait_max_seconds setting, at most 360). 0 or absent: return at once."}


SPAWN_SCHEMA = _schema(
    "cloud_agent_spawn",
    "Start a cloud agent in a sandbox on a host. It works on a branch of repo_path, a git "
    "repository on this computer, and commits there; nothing is pushed. The model is the default "
    "cloud-agent model unless model_id is given (list: cloud_agent_status models=true). Returns "
    "the run; use wait_s to wait. Needs approval.",
    {
        "task": {"type": "string", "description": "What the agent should do (preferred name)."},
        "goal": {"type": "string", "description": "Older name for task."},
        "criteria": {"type": "string",
                     "description": "Optional: when the work counts as done (added as 'Done when: ...')."},
        "host_id": {"type": "string",
                    "description": "Host id from compute_status; the host must allow agents."},
        "repo_path": {"type": "string", "description": "Path of a git repository on this computer."},
        "model_id": {"type": "string",
                     "description": "Saved model id (m-...); omit for the default model."},
        "template": {"type": "string", "default": DEFAULT_TEMPLATE,
                     "description": "hermes-agent (a model works the task) or agent-script "
                                    "(handoff test: commits a marker file, no model)."},
        "branch": {"type": "string",
                   "description": "Branch the agent works on; default: named after the task."},
        "new_branch": {"type": "boolean", "default": True,
                       "description": "Create the branch from base; false continues an existing branch."},
        "base": {"type": "string", "default": DEFAULT_BASE,
                 "description": "Revision a new branch starts from."},
        "max_turns": {"type": "integer", "default": DEFAULT_MAX_TURNS,
                      "description": "Turn limit for the agent (1-200)."},
        "wait_s": _wait_prop(),
    },
    ["task", "host_id", "repo_path"],
)

STATUS_SCHEMA = _schema(
    "cloud_agent_status",
    "Cloud agent runs: status, reply (the agent's answer), commits_ahead, last log lines. "
    "Without run_id lists all runs. With run_id and wait_s, blocks until that run finishes or "
    "time runs out and returns finished, status, reply and a next step. models=true adds the "
    "saved model choices.",
    {
        "run_id": {"type": "string", "description": "A run id such as a-1234abcd."},
        "wait_s": _wait_prop(),
        "models": {"type": "boolean", "default": False,
                   "description": "Also list saved models (id, name, model, endpoint host, key set, default)."},
    },
    [],
)

COLLECT_SCHEMA = _schema(
    "cloud_agent_collect",
    "Bring a cloud agent run's branch back to this computer as a new branch cloud/<run>/<branch> "
    "and, by default, a new git worktree beside the repository. Nothing is merged and the user's "
    "branches are not moved. Can be called again after a follow-up.",
    {
        "run_id": {"type": "string", "description": "A run id from cloud_agent_status."},
        "into": {"type": "string", "enum": list(COLLECT_TARGETS), "default": "worktree",
                 "description": "worktree: also check the branch out in a new worktree; branch: ref only."},
    },
    ["run_id"],
)

STOP_SCHEMA = _schema(
    "cloud_agent_stop",
    "Stop a cloud agent and delete its sandbox. Commits not fetched yet are fetched to this "
    "computer first, then the files in /work are saved; if fetching fails nothing is deleted "
    "unless delete_anyway=true. keep_branch=false also deletes its collected branches on this "
    "computer. After Stop no follow-up is possible. Needs approval.",
    {
        "run_id": {"type": "string", "description": "A run id from cloud_agent_status."},
        "keep_branch": {"type": "boolean", "default": True,
                        "description": "Keep the agent's work so it can still be collected."},
        "delete_anyway": {"type": "boolean", "default": False,
                          "description": "Delete the sandbox even when the agent's newest commits "
                                         "could not be fetched (they are then only in the saved "
                                         "copy of its files). Only after the user agreed."},
    },
    ["run_id"],
)


MESSAGE_SCHEMA = _schema(
    "cloud_agent_message",
    "Send a follow-up instruction to a cloud agent run whose turn ended; the agent continues in "
    "the same sandbox and branch (and the same conversation when possible). Not for a running or "
    "stopped run. The turn limit stays the one the run started with (rerun changes it). Returns "
    "the run; use wait_s to wait, then collect again. Needs approval.",
    {
        "run_id": {"type": "string", "description": "A run id from cloud_agent_status."},
        "message": {"type": "string", "description": "The follow-up instruction."},
        "wait_s": _wait_prop(),
    },
    ["run_id", "message"],
)


RERUN_SCHEMA = _schema(
    "cloud_agent_rerun",
    "Start a NEW cloud agent run from a finished run (failed, done, stopped or collected) with "
    "the same task, repository, branch, template and model. After a failure pass a fixed "
    "model_id (cloud_agent_status models=true) or model, another host_id, or a changed task. "
    "The old run is kept. Needs approval.",
    {
        "run_id": {"type": "string", "description": "The finished run to start again."},
        "task": {"type": "string", "description": "A changed task; omit to repeat the old one."},
        "model_id": {"type": "string",
                     "description": "Saved model id (m-...) to use instead of the old model, or "
                                    "'default' for the current default model (not the old "
                                    "run's model)."},
        "model": {"type": "string",
                  "description": "Another model id at the same endpoint (or at the saved "
                                 "endpoint named by base_url)."},
        "base_url": {"type": "string",
                     "description": "Endpoint of a saved model; keys are only sent to saved "
                                    "endpoints."},
        "host_id": {"type": "string", "description": "Another host, for example when the old "
                                                      "host was full."},
        "max_turns": {"type": "integer", "description": "Turn limit (1-200)."},
        "wait_s": _wait_prop(),
    },
    ["run_id"],
)


def tools() -> list[ToolSpec]:
    return [
        ToolSpec("cloud_agent_spawn", SPAWN_SCHEMA, _tool_spawn, approval=_spawn_approval),
        ToolSpec("cloud_agent_status", STATUS_SCHEMA, _tool_status),
        ToolSpec("cloud_agent_collect", COLLECT_SCHEMA, _tool_collect),
        ToolSpec("cloud_agent_stop", STOP_SCHEMA, _tool_stop, approval=_stop_approval),
        ToolSpec("cloud_agent_message", MESSAGE_SCHEMA, _tool_message, approval=_message_approval),
        ToolSpec("cloud_agent_rerun", RERUN_SCHEMA, _tool_rerun, approval=_rerun_approval),
    ]


# --------------------------------------------------------------------------- background


def _poll(services: Services) -> None:
    try:
        agents = services.agents
        if not agents.has_active():
            return
        agents.poll()
    except ApiError as exc:
        if exc.code != "unavailable":
            log.warning("cloud-compute: agent poll failed: %s", exc.message)


def background_tasks() -> list[BackgroundTask]:
    return [BackgroundTask("agents.poll", lambda services: POLL_INTERVAL_S, _poll)]
