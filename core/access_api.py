"""Access routes, the ``env_open`` tool and the tunnel sweep task.

Routes: ``POST /instances/{instance_id}/terminal``, ``/hosts/{host_id}/terminal``,
``/jobs/{job_id}/terminal``, ``POST /instances/{instance_id}/display``,
``POST /instances/{instance_id}/forward``, ``POST /hosts/{host_id}/forward``,
``GET /tunnels`` (open ones; ``?all=1`` lists every record) and
``DELETE /tunnels/{tunnel_id}``.

A display tunnel's ``url`` holds the screen token and is returned by the REST
route only. The ``env_open`` tool opens it in the browser on the user's computer and
returns it with the token replaced by ``<token>``.

Only ports published on the host's loopback can be forwarded for an environment;
``remote_port`` is the port inside the environment (the host-side port that
``compute_status`` shows is accepted and translated). ``env_open`` rejects parameters it
does not take with the code ``unknown_parameter`` and returns ``card``
(``::cc-env{id="i-..."}``) whenever an environment is involved. Standard library only.
"""

from __future__ import annotations

import logging
from typing import Any

from .display import redact_display_url
from .interfaces import ApiError, ApiRequest, BackgroundTask, Route, ToolSpec

__all__ = ["routes", "tools", "background_tasks", "env_open", "SWEEP_INTERVAL_S"]

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 30.0
_MAX_TEXT = 200


def _bad(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _param(req: ApiRequest, name: str) -> str:
    return str((req.path_params or {}).get(name, ""))


def _body(req: ApiRequest) -> dict:
    body = req.body if req.body is not None else {}
    if not isinstance(body, dict):
        raise _bad("request body must be a JSON object")
    return body


def _port(body: dict, key: str, *, required: bool) -> int | None:
    value = body.get(key)
    if value is None:
        if required:
            raise _bad(f"{key} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        raise _bad(f"{key} must be an integer between 1 and 65535")
    return value


# --------------------------------------------------------------------------- routes


def terminal_instance(req: ApiRequest) -> dict:
    return req.services.access.open_terminal(instance_id=_param(req, "instance_id")).to_dict()


def terminal_host(req: ApiRequest) -> dict:
    return req.services.access.open_terminal(host_id=_param(req, "host_id")).to_dict()


def terminal_job(req: ApiRequest) -> dict:
    return req.services.access.open_terminal(job_id=_param(req, "job_id")).to_dict()


def display_instance(req: ApiRequest) -> dict:
    return req.services.access.open_display(_param(req, "instance_id")).to_dict()


def forward_instance(req: ApiRequest) -> dict:
    body = _body(req)
    return req.services.access.forward_port(
        instance_id=_param(req, "instance_id"), remote_port=_port(body, "remote_port", required=True),
        local_port=_port(body, "local_port", required=False)).to_dict()


def forward_host(req: ApiRequest) -> dict:
    body = _body(req)
    return req.services.access.forward_port(
        host_id=_param(req, "host_id"), remote_port=_port(body, "remote_port", required=True),
        local_port=_port(body, "local_port", required=False)).to_dict()


def list_tunnels(req: ApiRequest) -> list:
    show_all = str((req.query or {}).get("all", "")).strip().lower() in ("1", "true", "yes")
    return [t.to_dict() for t in req.services.access.list_tunnels(open_only=not show_all)]


def close_tunnel(req: ApiRequest) -> dict:
    return req.services.access.close_tunnel(_param(req, "tunnel_id")).to_dict()


def routes() -> list[Route]:
    return [
        Route("POST", "/instances/{instance_id}/terminal", terminal_instance,
              "Open a terminal in an environment"),
        Route("POST", "/hosts/{host_id}/terminal", terminal_host,
              "Open a terminal on a Slurm login node"),
        Route("POST", "/jobs/{job_id}/terminal", terminal_job,
              "Open a terminal inside a running Slurm job"),
        Route("POST", "/instances/{instance_id}/display", display_instance,
              "Open a tunnel to an environment's screen"),
        Route("POST", "/instances/{instance_id}/forward", forward_instance,
              "Forward a published environment port to this computer"),
        Route("POST", "/hosts/{host_id}/forward", forward_host,
              "Forward a port on an ssh host's loopback to this computer"),
        Route("GET", "/tunnels", list_tunnels, "List open tunnels (all=1 for every record)"),
        Route("DELETE", "/tunnels/{tunnel_id}", close_tunnel, "Close a tunnel"),
    ]


# --------------------------------------------------------------------------- env_open


def _optional_id(args: dict, key: str) -> str | None:
    value = args.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise _bad(f"{key} must be a string")
    return value


def _tunnel_result(rec: Any, url: str | None, what: str) -> dict:
    if rec.status != "open":
        raise ApiError(503, "unavailable",
                       f"The {what} could not be opened"
                       + (f": {rec.error}" if rec.error else "") + ". Try again in a few seconds.")
    return {"url": url, "tunnel_id": rec.id, "expires_at": rec.expires_at}


def _card(instance_id: str | None) -> str | None:
    """The chat card that shows the environment (``::cc-env{id="i-..."}``), or ``None``."""
    return f'::cc-env{{id="{instance_id}"}}' if instance_id else None


def _unknown_parameter(args: dict) -> ApiError | None:
    extra = sorted(str(k) for k in args if k not in _ENV_OPEN_PARAMS)
    if not extra:
        return None
    names = ", ".join(repr(k) for k in extra)
    return ApiError(400, "unknown_parameter",
                    f"env_open does not take {names}. Valid parameters: "
                    f"{', '.join(sorted(_ENV_OPEN_PARAMS))}.")


def env_open(args: dict, services: Any, ctx: dict | None = None) -> dict:
    """Handler of the ``env_open`` tool. Never raises; never returns a screen token."""
    try:
        args = args if isinstance(args, dict) else {}
        bad = _unknown_parameter(args)
        if bad is not None:
            raise bad
        kind = args.get("kind")
        if kind not in ("terminal", "display", "port"):
            raise _bad("kind must be one of: terminal, display, port")
        instance_id = _optional_id(args, "instance_id")
        host_id = _optional_id(args, "host_id")
        job_id = _optional_id(args, "job_id")
        access = services.access
        if kind == "terminal":
            if instance_id:
                from .approval import AGENT_SANDBOX_MESSAGE, is_agent_sandbox
                inst = services.store.get_instance(instance_id)
                if inst is not None and is_agent_sandbox(services, inst):
                    raise ApiError(409, "conflict", AGENT_SANDBOX_MESSAGE)   # holds the model key
            out = access.open_terminal(instance_id=instance_id, host_id=host_id,
                                       job_id=job_id).to_dict()
            if _card(instance_id):
                out["card"] = _card(instance_id)
            return out
        if kind == "display":
            if not instance_id:
                raise _bad("instance_id is required to open a screen")
            rec = access.open_display(instance_id)
            if rec.status != "open" or not rec.url:
                return _tunnel_result(rec, None, "screen")
            opened = access.open_in_browser(rec.url)
            out = _tunnel_result(rec, redact_display_url(rec.url), "screen")
            out.update(opened=opened, embeddable=True, card=_card(instance_id))
            return out
        port = args.get("remote_port")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise _bad("remote_port (1-65535, the port inside the environment) is required "
                       "for kind=port")
        if instance_id and not host_id:
            rec, container_port = access.forward_instance_port(instance_id, port)
            out = _tunnel_result(rec, rec.url, "port forward")
            out["remote_port"] = container_port
            if container_port != port:
                out["note"] = (f"{port} is the host-side port of port {container_port} inside the "
                               f"environment, so port {container_port} was forwarded.")
            out["card"] = _card(instance_id)
            return out
        rec = access.forward_port(instance_id=instance_id, host_id=host_id, remote_port=port)
        return _tunnel_result(rec, rec.url, "port forward")
    except ApiError as exc:
        return exc.to_dict()
    except Exception as exc:  # noqa: BLE001 - tool handlers never raise
        log.warning("cloud-compute: env_open failed: %s", type(exc).__name__)
        return {"error": {"code": "internal",
                          "message": f"env_open failed unexpectedly ({type(exc).__name__}). "
                                     "Try again; if it keeps failing, check the Own Compute "
                                     "logs."}}


_ENV_OPEN_SCHEMA = {
    "name": "env_open",
    "description": (
        "Open a view of an environment for the user. kind=terminal: instance_id, or Slurm "
        "host_id or job_id; opens a window. kind=display: instance_id with a screen; opens the "
        "browser. kind=port: instance_id plus remote_port, the port INSIDE the environment "
        "(a host-side port from compute_status is translated; published only). An ssh host_id "
        "instead forwards any loopback port of that host, after the user approves. Returns a "
        "url or window result and a card."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "instance_id": {"type": "string",
                            "description": "Environment id (from compute_status)."},
            "host_id": {"type": "string",
                        "description": "Host id (from compute_status): a Slurm host for a "
                                       "terminal, an ssh host for a port forward."},
            "job_id": {"type": "string",
                       "description": "Running Slurm job id (from compute_status)."},
            "kind": {"type": "string", "enum": ["terminal", "display", "port"],
                     "description": "What to open."},
            "remote_port": {"type": "integer", "minimum": 1, "maximum": 65535,
                            "description": "Port INSIDE the environment to forward "
                                           "(kind=port). Must be published by the template."},
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
}
_ENV_OPEN_PARAMS = frozenset(_ENV_OPEN_SCHEMA["parameters"]["properties"])


def tools() -> list[ToolSpec]:
    return [ToolSpec(name="env_open", schema=_ENV_OPEN_SCHEMA, handler=env_open,
                     approval=None, toolset="cloud_compute")]


# --------------------------------------------------------------------------- background


def _sweep(services: Any) -> None:
    services.access.sweep()


def background_tasks() -> list[BackgroundTask]:
    return [BackgroundTask("access.tunnel_sweep", lambda services: SWEEP_INTERVAL_S, _sweep)]
