"""Dashboard backend for the cloud-compute plugin.

Hermes imports this file and mounts its ``router`` under
``/api/plugins/cloud-compute``; the dashboard session token is checked before
a request reaches it. Every route comes from ``core.registry.collect_routes()``,
so feature modules add REST endpoints without touching this file.

The helpers :func:`dispatch`, :func:`parse_body`, :func:`order_routes` and
:func:`ensure_background` do not need FastAPI; it is imported only inside
:func:`build_router`.
"""

import asyncio
import importlib
import importlib.util
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("cloud_compute.dashboard")

CORE_PACKAGE = "cloud_compute_core"
_CORE_DIR = Path(__file__).resolve().parents[1] / "core"
_BODY_METHODS = ("POST", "PATCH", "PUT", "DELETE")


def _load_core():
    existing = sys.modules.get(CORE_PACKAGE)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        CORE_PACKAGE, _CORE_DIR / "__init__.py", submodule_search_locations=[str(_CORE_DIR)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_CORE_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[CORE_PACKAGE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(CORE_PACKAGE, None)
        raise
    return module


_load_core()
_interfaces = importlib.import_module(CORE_PACKAGE + ".interfaces")
_registry = importlib.import_module(CORE_PACKAGE + ".registry")
ApiError = _interfaces.ApiError
ApiRequest = _interfaces.ApiRequest


def _internal_error():
    return {"error": {"code": "internal", "message": "internal error"}}


# --------------------------------------------------------------------------- pure helpers


def dispatch(route, path_params, query, body, services):
    """Run ``route.handler`` and return ``(status, payload)``; never raises."""
    request = ApiRequest(path_params=dict(path_params or {}), query=dict(query or {}),
                         body=body, services=services)
    try:
        payload = route.handler(request)
    except ApiError as exc:
        return exc.status, exc.to_dict()
    except Exception as exc:  # noqa: BLE001 - nothing may leak to the client
        log.warning("cloud-compute: %s %s failed: %s", route.method, route.path,
                    type(exc).__name__, exc_info=log.isEnabledFor(logging.DEBUG))
        return 500, _internal_error()
    if payload is None:
        payload = {}
    return 200, payload


def parse_body(raw):
    """Decode a request body: empty -> ``None``; otherwise a JSON object.

    Raises ``ApiError(400, "invalid", ...)`` for bad JSON or a non-object.
    """
    if raw is None or not bytes(raw).strip():
        return None
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "invalid", "request body is not valid JSON") from None
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ApiError(400, "invalid", "request body must be a JSON object")
    return data


def order_routes(routes):
    """Stable order with static path segments ahead of ``{param}`` ones.

    ``/instances/stop-all`` must be tried before ``/instances/{id}``.
    """
    def key(route):
        segments = [s for s in route.path.split("/") if s]
        return tuple(1 if s.startswith("{") else 0 for s in segments)

    return sorted(routes, key=key)


def ensure_background(services):
    """Make sure the background loop runs (idempotent); failures are logged, never raised.

    Returns ``True`` when this call started it. A failed start is retried by a later
    request after a backoff (see ``background.ensure_started``).
    """
    try:
        return bool(importlib.import_module(CORE_PACKAGE + ".background").ensure_started(services))
    except Exception as exc:  # noqa: BLE001
        kind = type(exc).__name__
        if kind not in _bg_logged:
            _bg_logged.add(kind)
            log.warning("cloud-compute: background loop did not start: %s", kind)
        return False


_bg_logged = set()


def _reset_background_for_tests():
    _bg_logged.clear()
    importlib.import_module(CORE_PACKAGE + ".background")._reset_ensure_for_tests()


def get_services():
    """The process-wide service container (shared with the chat tools).

    Resolved on every call and never cached here: the router is mounted once per gateway
    process, but the plugin can be unloaded and loaded again (Capabilities -> Plugins
    off/on), which closes the container's store and drops the container. The registry
    hands out the current one, rebuilding it when its store is closed.
    """
    return _registry.shared_services()


# --------------------------------------------------------------------------- FastAPI layer


def _json_response(JSONResponse, status, payload):
    try:
        return JSONResponse(status_code=status, content=payload)
    except (TypeError, ValueError):
        return JSONResponse(status_code=500, content=_internal_error())


def _make_endpoint(route, services_getter, Request, JSONResponse):
    has_body = str(route.method).upper() in _BODY_METHODS

    def work(path_params, query, raw):
        try:
            services = services_getter()
            ensure_background(services)
            body = parse_body(raw) if has_body else None
        except ApiError as exc:
            return exc.status, exc.to_dict()
        except Exception as exc:  # noqa: BLE001
            log.warning("cloud-compute: setup failed: %s", type(exc).__name__)
            return 500, _internal_error()
        return dispatch(route, path_params, query, body, services)

    async def endpoint(request: Request):
        raw = await request.body() if has_body else None
        path_params = {k: str(v) for k, v in request.path_params.items()}
        status, payload = await asyncio.to_thread(work, path_params, dict(request.query_params), raw)
        return _json_response(JSONResponse, status, payload)

    endpoint.__name__ = "cc_" + str(route.method).lower() + "_" + (
        route.path.strip("/").replace("/", "_").replace("{", "").replace("}", "") or "root")
    return endpoint


_ALL_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def unmatched_error(path, method, routes):
    """``(status, payload, allow)`` for a request no route handled.

    A path that some route serves with another method gets 405 ``invalid``
    (``allow`` lists the methods it does accept); anything else gets 404
    ``not_found``. Both use the plugin's ``{"error": {...}}`` shape.
    """
    from starlette.routing import compile_path

    path = "/" + str(path or "").lstrip("/")
    allowed = []
    for route in routes:
        regex = compile_path(route.path)[0]
        if regex.match(path):
            m = str(route.method).upper()
            if m not in allowed:
                allowed.append(m)
    if allowed:
        message = f"method {str(method).upper()} not allowed (allowed: {', '.join(allowed)})"
        return 405, {"error": {"code": "invalid", "message": message}}, allowed
    return 404, {"error": {"code": "not_found", "message": "no such endpoint"}}, []


def _make_fallback(routes, Request, JSONResponse):
    routes = list(routes)

    async def cc_unmatched(request: Request):
        status, payload, allowed = unmatched_error(
            request.path_params.get("cc_rest", ""), request.method, routes)
        response = _json_response(JSONResponse, status, payload)
        if allowed:
            response.headers["Allow"] = ", ".join(allowed)
        return response

    return cc_unmatched


def build_router(routes, services_getter=None):
    """A FastAPI ``APIRouter`` serving ``routes``; imports FastAPI here only.

    A final catch-all answers every other path under the prefix in the
    plugin's error shape (404 ``not_found`` / 405 ``invalid``).
    """
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    getter = services_getter or get_services
    routes = order_routes(list(routes))
    router = APIRouter()
    for route in routes:
        router.add_api_route(
            route.path,
            _make_endpoint(route, getter, Request, JSONResponse),
            methods=[str(route.method).upper()],
            summary=getattr(route, "summary", "") or None,
            include_in_schema=False,
        )
    router.add_api_route(
        "/{cc_rest:path}",
        _make_fallback(routes, Request, JSONResponse),
        methods=list(_ALL_METHODS),
        include_in_schema=False,
    )
    return router


def _build_default_router():
    try:
        return build_router(_registry.collect_routes())
    except ImportError as exc:  # FastAPI missing: Hermes will log "no router"
        log.warning("cloud-compute: dashboard API unavailable: %s", exc)
        return None


router = _build_default_router()
