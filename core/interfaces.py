"""Plumbing types shared by the REST routes, Hermes tools and background tasks.

- :class:`ApiError` carries an HTTP status and a stable error code.
- :class:`ApiRequest`, :class:`Route`, :class:`ToolSpec` and :class:`BackgroundTask`
  describe what each feature module contributes.
- :class:`Services` is the lazy, thread-safe container that builds each
  service on first use.

Standard library only. Service types (Store, Config, ...) appear in
annotations as strings only; their modules are never imported here.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

__all__ = [
    "ApiError", "MachineNotRunning", "NO_MACHINE", "ERROR_CODES", "ApiRequest", "Route", "ToolSpec", "BackgroundTask",
    "Services", "SERVICE_NAMES", "HTTP_METHODS",
]

log = logging.getLogger(__name__)

#: Error codes used in ``{"error": {"code", "message"}}`` responses.
ERROR_CODES: frozenset[str] = frozenset({
    "not_found",           # 404
    "invalid",             # 400
    "conflict",            # 409
    "unavailable",         # 503: service/module not installed or not built yet
    "host_offline",        # host unreachable
    "auth_required",       # host needs credentials the user must supply
    "limit_reached",       # host or plugin limit hit
    "approval_required",   # action needs the user's approval
    "not_saved",           # 409: the files could not be saved to this computer first
    "not_fetched",         # 409: the agent's newest commits could not be fetched first
    "too_large",           # 413: a file or export is over the size limit
    "not_running",         # 409: the environment must be running for this action
    "internal",            # 500
    "not_implemented",     # 501: a planned feature that is not available yet
    "nothing_to_save",     # 409: there are no files to save
    "unknown_parameter",   # 400: a parameter the action does not take
    "needs_setup",         # 409: a setting or host option must be set first
})

HTTP_METHODS: tuple[str, ...] = ("GET", "POST", "PATCH", "PUT", "DELETE")


class ApiError(Exception):
    """An error with an HTTP status and a stable code (one of :data:`ERROR_CODES`)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"ApiError({self.status!r}, {self.code!r}, {self.message!r})"


#: The address of an EC2 host that has no running machine. It never resolves and is never
#: dialled: the transport factory refuses to build a connection to it.
NO_MACHINE = "no-machine.invalid"


class MachineNotRunning(ApiError):
    """A command needs the machine of an EC2 host, but the host has none running."""

    def __init__(self, host_name: str):
        super().__init__(409, "host_offline",
                         f"The machine of {host_name} is not running. Start it first.")
        self.host_name = host_name


@dataclass
class ApiRequest:
    path_params: dict[str, str]
    query: dict[str, str]
    body: dict | None
    services: "Services"


@dataclass(frozen=True)
class Route:
    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"]
    path: str                                       # relative, e.g. "/hosts/{host_id}/probe"
    handler: Callable[[ApiRequest], dict | list]    # sync; run in a worker thread by the API layer
    summary: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict                                    # OpenAI-function schema, static
    handler: Callable[[dict, "Services", dict], dict]   # (args, services, ctx_kwargs incl. session_id)
    approval: Callable[[dict, "Services"], str | None] | None = None   # message when approval is needed
    toolset: str = "cloud_compute"


@dataclass(frozen=True)
class BackgroundTask:
    name: str
    interval_s: Callable[["Services"], float]
    run: Callable[["Services"], None]


#: Every service a :class:`Services` container knows about.
SERVICE_NAMES: tuple[str, ...] = (
    "store", "config", "secrets", "events",
    "transports", "prober",
    "templates", "sandboxes", "reaper",
    "slurm",
    "access",
    "agents",
    "files",
    "ec2",
)
_SERVICE_SET = frozenset(SERVICE_NAMES)


class Services:
    """Lazy service container.

    Reading a known attribute (``services.store``) returns the cached instance
    or builds it once with ``providers[name](self)``. Providers may read other
    services while building. Keyword ``instances`` are used as-is (tests inject
    fakes this way).

    A missing provider, or a provider raising ``ImportError`` (including
    ``ModuleNotFoundError``), raises ``ApiError(503, "unavailable",
    "<name> not installed")``. Other provider errors propagate unchanged and
    nothing is cached, so the next access tries again.
    """

    store: "Store"
    config: "Config"
    secrets: "SecretResolver"
    events: "EventBus"
    transports: "TransportFactory"
    prober: "Prober"
    templates: "TemplateStore"
    sandboxes: "SandboxManager"
    reaper: "Reaper"
    slurm: "SlurmManager"
    access: "AccessManager"
    agents: "AgentManager"
    files: "FileManager"

    def __init__(self, *, providers: Mapping[str, Callable[["Services"], Any]] | None = None,
                 **instances: Any):
        unknown = sorted(set(instances) - _SERVICE_SET)
        if unknown:
            raise ValueError(f"unknown service name(s): {', '.join(unknown)}; "
                             f"known: {', '.join(SERVICE_NAMES)}")
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_providers", dict(providers or {}))
        object.__setattr__(self, "_instances", dict(instances))
        object.__setattr__(self, "_building", set())

    # -- access ------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Only called when normal lookup fails, i.e. for service names.
        if name.startswith("_") or name not in _SERVICE_SET:
            raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")
        return self._get(name)

    def get(self, name: str) -> Any:
        """Same as ``getattr(services, name)`` for a known service name."""
        if name not in _SERVICE_SET:
            raise AttributeError(f"unknown service {name!r}")
        return self._get(name)

    def _get(self, name: str) -> Any:
        instances = self.__dict__["_instances"]
        if name in instances:
            return instances[name]
        with self.__dict__["_lock"]:
            if name in instances:
                return instances[name]
            provider = self.__dict__["_providers"].get(name)
            if provider is None:
                raise ApiError(503, "unavailable", f"{name} not installed")
            building = self.__dict__["_building"]
            if name in building:
                raise RuntimeError(f"service dependency cycle while building {name!r}")
            building.add(name)
            try:
                obj = provider(self)
            except ApiError:
                raise
            except ImportError as exc:
                log.debug("service %s unavailable: %s", name, exc)
                raise ApiError(503, "unavailable", f"{name} not installed") from exc
            finally:
                building.discard(name)
            instances[name] = obj
            return obj

    def available(self, name: str) -> bool:
        """True if ``name`` is already built or has a provider. Never builds."""
        if name in self.__dict__["_instances"]:
            return True
        return name in self.__dict__["_providers"]

    def set(self, name: str, obj: Any) -> None:
        """Inject or replace the instance for a known service name."""
        if name not in _SERVICE_SET:
            raise ValueError(f"unknown service {name!r}")
        with self.__dict__["_lock"]:
            self.__dict__["_instances"][name] = obj

    def built(self) -> list[str]:
        """Names of the services that are currently built or injected."""
        return [n for n in SERVICE_NAMES if n in self.__dict__["_instances"]]

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _SERVICE_SET:
            self.set(name, value)
        else:
            object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return f"Services(built={self.built()!r})"
