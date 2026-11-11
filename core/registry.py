"""Discovers feature modules and builds the service container.

Feature modules are optional: a module that is not present is skipped
silently, and one that fails to import is logged, recorded and skipped, so
the rest of the plugin keeps working.

Module contract:

- API modules (:data:`API_MODULES`) export ``routes() -> list[Route]``,
  ``tools() -> list[ToolSpec]`` and ``background_tasks() -> list[BackgroundTask]``;
  any of them may be omitted (treated as ``[]``).
- Service modules (:data:`SERVICE_MODULES` values) export
  ``provide(services) -> <service object>``.
"""

from __future__ import annotations

import importlib
import logging
import threading
from types import ModuleType
from typing import Any, Callable

from .interfaces import ApiError, BackgroundTask, Route, Services, ToolSpec

__all__ = [
    "API_MODULES", "SERVICE_MODULES", "collect_routes", "collect_tools", "collect_background",
    "import_errors", "module_status", "build_services", "clear_errors",
    "shared_services", "reset_shared_services", "is_closed",
]

log = logging.getLogger(__name__)

API_MODULES: tuple[str, ...] = (
    "hosts_api", "instances_api", "slurm_api", "access_api", "docs_api", "summary_api", "agents_api",
    "files_api", "ec2_api",
)

#: service name -> module (inside this package) exporting ``provide(services)``.
SERVICE_MODULES: dict[str, str] = {
    "store": "store",
    "config": "config",
    "secrets": "secrets",
    "events": "events",
    "transports": "transport",
    "prober": "prober",
    "templates": "templates",
    "sandboxes": "sandbox",
    "reaper": "reaper",
    "slurm": "slurm",
    "access": "access",
    "agents": "agents",
    "files": "files",
    "ec2": "ec2",
}

# (module, stage) -> short error text; stage is "import" or a function name.
_errors: dict[tuple[str, str], str] = {}
_errors_lock = threading.Lock()


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:297] + "..."


def _record(module: str, stage: str, exc: BaseException | str) -> None:
    text = exc if isinstance(exc, str) else _short(exc)
    with _errors_lock:
        _errors[(module, stage)] = text


def _clear(module: str, stage: str) -> None:
    with _errors_lock:
        _errors.pop((module, stage), None)


def _full_name(name: str) -> str:
    return f"{__package__}.{name}"


def _import(name: str) -> ModuleType | None:
    """Import a sibling module; ``None`` if it is absent or failed to import."""
    try:
        module = importlib.import_module("." + name, __package__)
    except ModuleNotFoundError as exc:
        if exc.name == _full_name(name):
            _clear(name, "import")
            return None
        log.warning("cloud-compute: module %s failed to import: %s", name, exc)
        _record(name, "import", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - any import failure must not break the plugin
        log.warning("cloud-compute: module %s failed to import: %s", name, exc, exc_info=True)
        _record(name, "import", exc)
        return None
    _clear(name, "import")
    return module


def _collect(func_name: str, item_type: type, key: Callable[[Any], Any], what: str) -> list:
    items: list = []
    seen: dict[Any, str] = {}
    for name in API_MODULES:
        module = _import(name)
        if module is None:
            continue
        func = getattr(module, func_name, None)
        if func is None:
            _clear(name, func_name)
            continue
        try:
            produced = list(func() or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("cloud-compute: %s.%s() failed: %s", name, func_name, exc, exc_info=True)
            _record(name, func_name, f"{func_name}() failed: {_short(exc)}")
            continue
        bad = [type(i).__name__ for i in produced if not isinstance(i, item_type)]
        if bad:
            _record(name, func_name,
                    f"{func_name}() returned non-{item_type.__name__} item(s): {', '.join(bad)}")
            log.warning("cloud-compute: %s.%s() returned invalid items: %s", name, func_name, bad)
        else:
            _clear(name, func_name)
        for item in produced:
            if not isinstance(item, item_type):
                continue
            k = key(item)
            if k in seen:
                log.warning("cloud-compute: duplicate %s %r from %s ignored (first from %s)",
                            what, k, name, seen[k])
                continue
            seen[k] = name
            items.append(item)
    return items


def collect_routes() -> list[Route]:
    """All routes from the present API modules; the first of a duplicate (method, path) wins."""
    return _collect("routes", Route, lambda r: (str(r.method).upper(), r.path), "route")


def collect_tools() -> list[ToolSpec]:
    """All tools from the present API modules; the first of a duplicate name wins."""
    return _collect("tools", ToolSpec, lambda t: t.name, "tool")


def collect_background() -> list[BackgroundTask]:
    """All background tasks from the present API modules; the first of a duplicate name wins."""
    return _collect("background_tasks", BackgroundTask, lambda b: b.name, "background task")


def import_errors() -> dict[str, str]:
    """Module name -> short error text for modules that failed to import or collect."""
    with _errors_lock:
        snapshot = sorted(_errors.items())
    out: dict[str, str] = {}
    for (module, stage), text in snapshot:
        out[module] = f"{out[module]}; {text}" if module in out else text
    return out


def clear_errors() -> None:
    """Forget all recorded errors (mainly for tests)."""
    with _errors_lock:
        _errors.clear()


def module_status() -> dict[str, bool]:
    """Each API and service module -> whether it imports, for ``/health``."""
    status: dict[str, bool] = {}
    for name in API_MODULES + tuple(dict.fromkeys(SERVICE_MODULES.values())):
        status[name] = _import(name) is not None
    return status


def _provider(service: str, module_name: str) -> Callable[[Services], Any]:
    def build(services: Services) -> Any:
        module = _import(module_name)
        if module is None:
            raise ApiError(503, "unavailable", f"{service} not installed")
        provide = getattr(module, "provide", None)
        if not callable(provide):
            log.warning("cloud-compute: module %s has no provide(services)", module_name)
            _record(module_name, "provide", "module has no provide(services)")
            raise ApiError(503, "unavailable", f"{service} not installed")
        return provide(services)

    build.__name__ = f"provide_{service}"
    return build


def build_services(store: Any = None, config: Any = None, **overrides: Any) -> Services:
    """Build a lazy :class:`Services` container.

    Each service is built on first access by its module's ``provide()``; a
    missing module raises ``ApiError(503, "unavailable", "<service> not
    installed")`` at that point, never here. ``store``, ``config`` and other
    keyword overrides (when not ``None``) are injected as ready instances.
    """
    providers = {svc: _provider(svc, mod) for svc, mod in SERVICE_MODULES.items()}
    instances = {k: v for k, v in overrides.items() if v is not None}
    if store is not None:
        instances["store"] = store
    if config is not None:
        instances["config"] = config
    return Services(providers=providers, **instances)


_shared: Services | None = None
_shared_lock = threading.Lock()


def is_closed(services: Any) -> bool:
    """True when ``services`` already has a store and that store was closed.

    Never builds anything; anything that is not a container (or has no store yet)
    counts as open.
    """
    try:
        if "store" not in services.built():
            return False
        return bool(getattr(services.store, "closed", False))
    except Exception:  # noqa: BLE001 - not a container, or a store we cannot ask
        return False


def shared_services() -> Services:
    """The one service container of this process, built on first use.

    The chat tools and hooks and the dashboard REST routes both call this *on every
    call* (never cache the result), so they share one store, one set of locks and
    one background loop instead of each building their own. When the plugin is
    unloaded (Capabilities -> Plugins off) the container is dropped and the store
    closed; a container whose store is closed is replaced here by a new one that
    reopens the same database file, so a reload or a stale reference never leaves
    callers on a dead store.
    """
    global _shared
    services = _shared
    if services is None or is_closed(services):
        with _shared_lock:
            services = _shared
            if services is None or is_closed(services):
                services = build_services()
                _shared = services
    return services


def reset_shared_services() -> None:
    """Forget the shared container (tests only)."""
    global _shared
    with _shared_lock:
        _shared = None
