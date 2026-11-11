"""Dashboard backend: dispatch mapping, route ordering, and (with FastAPI) the mounted router."""

import importlib
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_api():
    name = "cloud_compute_plugin_api_under_test"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = _load_api()
interfaces = importlib.import_module("cloud_compute_core.interfaces")
Route = interfaces.Route
ApiError = interfaces.ApiError


def _route(handler, method="GET", path="/x"):
    return Route(method, path, handler)


@pytest.fixture(autouse=True)
def _fresh_state():
    # Another test file may leave a real loop running; a loop on a different container is
    # (rightly) replaced by ensure_started, which would break the call counts below.
    importlib.import_module("cloud_compute_core.background").stop()
    api._reset_background_for_tests()
    yield
    importlib.import_module("cloud_compute_core.background").stop()
    api._reset_background_for_tests()


# ---------------------------------------------------------------- manifest


def test_manifest_matches_loader_expectations():
    data = json.loads((PLUGIN_DIR / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert data["name"] == "cloud-compute"
    assert data["api"] == "plugin_api.py"
    assert data["tab"] == {"hidden": True}
    assert (PLUGIN_DIR / "dashboard" / data["api"]).is_file()


def test_manifest_version_matches_plugin_yaml():
    # The dashboard plugin list shows "0.0.0" when the manifest omits its version.
    import re

    data = json.loads((PLUGIN_DIR / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r'^version:\s*"?([^"\s]+)"?\s*$', text, re.MULTILINE)
    assert match, "plugin.yaml has no version"
    assert data["version"] == match.group(1)
    assert data["description"].strip()


def test_core_is_shared_with_the_loaded_package():
    assert sys.modules["cloud_compute_core"] is importlib.import_module("cloud_compute_core")
    assert api.ApiError is ApiError


# ---------------------------------------------------------------- dispatch


def test_dispatch_passes_request_fields_and_returns_payload():
    seen = {}

    def handler(req):
        seen.update(path=req.path_params, query=req.query, body=req.body, services=req.services)
        return {"ok": True}

    status, payload = api.dispatch(_route(handler), {"id": "a"}, {"q": "1"}, {"k": 2}, "SVC")
    assert (status, payload) == (200, {"ok": True})
    assert seen == {"path": {"id": "a"}, "query": {"q": "1"}, "body": {"k": 2}, "services": "SVC"}


def test_dispatch_list_and_none_payloads():
    assert api.dispatch(_route(lambda r: [1, 2]), {}, {}, None, None) == (200, [1, 2])
    assert api.dispatch(_route(lambda r: None), {}, {}, None, None) == (200, {})


def test_dispatch_maps_api_error():
    def boom(req):
        raise ApiError(404, "not_found", "no such host")

    status, payload = api.dispatch(_route(boom), {}, {}, None, None)
    assert status == 404
    assert payload == {"error": {"code": "not_found", "message": "no such host"}}


def test_dispatch_internal_error_does_not_leak():
    def boom(req):
        raise RuntimeError("password=hunter2 /secret/path")

    status, payload = api.dispatch(_route(boom), {}, {}, None, None)
    assert status == 500
    assert payload["error"]["code"] == "internal"
    text = json.dumps(payload)
    assert "hunter2" not in text and "Traceback" not in text and "secret" not in text


def test_parse_body():
    assert api.parse_body(None) is None
    assert api.parse_body(b"") is None
    assert api.parse_body(b"  \n") is None
    assert api.parse_body(b'{"a": 1}') == {"a": 1}
    assert api.parse_body(b"null") is None
    for bad in (b"{nope", b"[1]", b"\xff\xfe"):
        with pytest.raises(ApiError) as info:
            api.parse_body(bad)
        assert info.value.status == 400 and info.value.code == "invalid"


def test_order_routes_puts_static_segments_first():
    routes = [
        _route(None, "POST", "/instances/{id}/{action}"),
        _route(None, "GET", "/instances/{id}"),
        _route(None, "POST", "/instances/stop-all"),
        _route(None, "GET", "/hosts/{host_id}/probe"),
    ]
    ordered = [r.path for r in api.order_routes(routes)]
    assert ordered.index("/instances/stop-all") < ordered.index("/instances/{id}")


# ---------------------------------------------------------------- background


def _patch_background(monkeypatch, fail=False):
    background = importlib.import_module("cloud_compute_core.background")
    calls = []

    def fake_start(services, **kwargs):
        calls.append(services)
        if fail:
            raise RuntimeError("no thread")

    monkeypatch.setattr(background, "start", fake_start)
    return calls


def test_background_started_once_across_threads(monkeypatch):
    calls = _patch_background(monkeypatch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(api.ensure_background("S")))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == ["S"]
    assert results.count(True) == 1


def test_background_failure_is_not_fatal_and_is_retried_after_a_backoff(monkeypatch, caplog):
    """bug-hunt #9: one failed start used to disable the loop until restart."""
    background = importlib.import_module("cloud_compute_core.background")
    now = [1000.0]
    monkeypatch.setattr(background, "_monotonic", lambda: now[0])
    calls = []
    state = {"fail": True}

    def flaky_start(services, **kwargs):
        calls.append(services)
        if state["fail"]:
            raise RuntimeError("store locked")

    monkeypatch.setattr(background, "start", flaky_start)
    with caplog.at_level("WARNING", logger=api.log.name):
        assert api.ensure_background("S") is False       # fails, never raises
        assert api.ensure_background("S") is False       # inside the backoff: no new attempt
        assert len(calls) == 1
        now[0] += background.RETRY_FIRST_S + 0.1
        assert api.ensure_background("S") is False       # second failure, longer backoff
        assert len(calls) == 2
        now[0] += background.RETRY_FIRST_S + 0.1         # still inside the doubled wait
        assert api.ensure_background("S") is False
        assert len(calls) == 2
        now[0] += background.RETRY_FIRST_S * 2
        state["fail"] = False
        assert api.ensure_background("S") is True        # recovered
        assert api.ensure_background("S") is False       # and now settled
        assert len(calls) == 3
    warnings = [r for r in caplog.records if "did not start" in r.getMessage()]
    assert len(warnings) == 1                            # logged once per failure kind


def test_background_backoff_is_capped(monkeypatch):
    background = importlib.import_module("cloud_compute_core.background")
    now = [0.0]
    monkeypatch.setattr(background, "_monotonic", lambda: now[0])
    monkeypatch.setattr(background, "start",
                        lambda services, **kw: (_ for _ in ()).throw(OSError("x")))
    for _ in range(12):
        now[0] += background.RETRY_MAX_S + 1
        api.ensure_background("S")
    assert background._retry_delay == background.RETRY_MAX_S


def test_get_services_uses_the_shared_registry_container(monkeypatch):
    registry = importlib.import_module("cloud_compute_core.registry")
    registry.reset_shared_services()
    built = []
    monkeypatch.setattr(registry, "build_services", lambda: built.append(1) or object())
    try:
        assert api.get_services() is registry.shared_services()
        assert built == [1]
    finally:
        registry.reset_shared_services()


def test_get_services_follows_the_registry_every_call_and_skips_a_closed_store(monkeypatch):
    """The router outlives a plugin unload, so the container must never be cached here."""
    registry = importlib.import_module("cloud_compute_core.registry")

    class Store:
        closed = False

    class Svc:
        def __init__(self):
            self.store = Store()

        def built(self):
            return ["store"]

    made = []
    monkeypatch.setattr(registry, "build_services", lambda: made.append(Svc()) or made[-1])
    registry.reset_shared_services()
    try:
        first = api.get_services()
        assert api.get_services() is first and made == [first]
        registry.reset_shared_services()                    # what the unload callback does
        second = api.get_services()
        assert second is not first and made == [first, second]
        second.store.closed = True                          # closed behind our back
        third = api.get_services()
        assert third is not second and made == [first, second, third]
    finally:
        registry.reset_shared_services()


# ---------------------------------------------------------------- FastAPI router

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:  # fastapi or httpx missing: the router tests are skipped
    FastAPI = TestClient = None
    HAVE_FASTAPI = False

needs_fastapi = pytest.mark.skipif(not HAVE_FASTAPI, reason="fastapi/httpx not importable")


def _client(routes, services="SVC"):
    app = FastAPI()
    app.include_router(api.build_router(routes, lambda: services), prefix="/api/plugins/cloud-compute")
    return TestClient(app)


BASE = "/api/plugins/cloud-compute"


@needs_fastapi
def test_router_serves_fake_routes(monkeypatch):
    _patch_background(monkeypatch)

    def echo(req):
        return {"path": req.path_params, "query": req.query, "body": req.body}

    client = _client([
        Route("GET", "/things/{thing_id}", echo),
        Route("POST", "/things/{thing_id}/go", echo),
    ])
    r = client.get(f"{BASE}/things/t1?a=1&b=two")
    assert r.status_code == 200
    assert r.json() == {"path": {"thing_id": "t1"}, "query": {"a": "1", "b": "two"}, "body": None}
    r = client.post(f"{BASE}/things/t1/go", json={"x": [1]})
    assert r.json()["body"] == {"x": [1]}
    assert client.post(f"{BASE}/things/t1/go").json()["body"] is None


@needs_fastapi
def test_router_maps_errors_and_bad_json(monkeypatch):
    _patch_background(monkeypatch)

    def missing(req):
        raise ApiError(409, "conflict", "already there")

    def crash(req):
        raise ValueError("token=abc123")

    client = _client([Route("GET", "/m", missing), Route("GET", "/c", crash),
                      Route("POST", "/b", lambda req: {"ok": True})])
    r = client.get(f"{BASE}/m")
    assert r.status_code == 409
    assert r.json() == {"error": {"code": "conflict", "message": "already there"}}
    r = client.get(f"{BASE}/c")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal" and "abc123" not in r.text
    r = client.post(f"{BASE}/b", content=b"{bad", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid"
    r = client.post(f"{BASE}/b", json=[1, 2])
    assert r.status_code == 400


@needs_fastapi
def test_router_static_route_wins_over_param(monkeypatch):
    _patch_background(monkeypatch)
    client = _client([
        Route("POST", "/instances/{instance_id}/{action}", lambda req: {"which": "param"}),
        Route("POST", "/instances/stop-all", lambda req: {"which": "static"}),
    ])
    assert client.post(f"{BASE}/instances/stop-all").json() == {"which": "static"}
    assert client.post(f"{BASE}/instances/a/stop").json() == {"which": "param"}


@needs_fastapi
def test_router_starts_background_once_on_first_request(monkeypatch):
    calls = _patch_background(monkeypatch)
    client = _client([Route("GET", "/ping", lambda req: {"ok": 1})])
    for _ in range(3):
        assert client.get(f"{BASE}/ping").status_code == 200
    assert calls == ["SVC"]


@needs_fastapi
def test_router_service_build_failure_maps_to_error(monkeypatch):
    _patch_background(monkeypatch)

    def bad_services():
        raise ApiError(503, "unavailable", "store not installed")

    app = FastAPI()
    app.include_router(api.build_router([Route("GET", "/ping", lambda req: {})], bad_services),
                       prefix=BASE)
    r = TestClient(app).get(f"{BASE}/ping")
    assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"


@needs_fastapi
def test_real_routes_serve_health_and_docs(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_background(monkeypatch)
    registry = importlib.import_module("cloud_compute_core.registry")
    routes = registry.collect_routes()
    paths = {r.path for r in routes}
    assert {"/health", "/docs", "/docs/{topic}"} <= paths

    app = FastAPI()
    app.include_router(api.build_router(routes), prefix=BASE)
    client = TestClient(app)

    r = client.get(f"{BASE}/health")
    assert r.status_code == 200 and isinstance(r.json(), dict)
    r = client.get(f"{BASE}/docs")
    assert r.status_code == 200
    r = client.get(f"{BASE}/docs/definitely-not-a-topic")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = client.get(f"{BASE}/summary")
    assert r.status_code == 200


@needs_fastapi
def test_router_unknown_path_uses_error_shape(monkeypatch):
    _patch_background(monkeypatch)
    client = _client([Route("GET", "/things/{thing_id}", lambda req: {"ok": 1}),
                      Route("POST", "/things/{thing_id}/go", lambda req: {"ok": 2})])
    for method, path in (("GET", "/nonexistent"), ("POST", "/nope/deeper"),
                         ("DELETE", "/things"), ("GET", "/things/t1/go/extra"), ("GET", "/")):
        r = client.request(method, f"{BASE}{path}")
        assert r.status_code == 404, (method, path)
        assert r.json() == {"error": {"code": "not_found", "message": "no such endpoint"}}
    assert client.get(f"{BASE}/things/t1").json() == {"ok": 1}


@needs_fastapi
def test_router_wrong_method_stays_405_with_error_shape(monkeypatch):
    _patch_background(monkeypatch)
    client = _client([Route("GET", "/things/{thing_id}", lambda req: {"ok": 1}),
                      Route("DELETE", "/things/{thing_id}", lambda req: {"ok": 3}),
                      Route("POST", "/things/{thing_id}/go", lambda req: {"ok": 2})])
    r = client.post(f"{BASE}/things/t1")
    assert r.status_code == 405
    body = r.json()
    assert set(body) == {"error"} and body["error"]["code"] == "invalid"
    assert "POST" in body["error"]["message"]
    assert r.headers["allow"] == "GET, DELETE"
    r = client.get(f"{BASE}/things/t1/go")
    assert r.status_code == 405 and r.headers["allow"] == "POST"
    assert client.delete(f"{BASE}/things/t1").json() == {"ok": 3}
    assert client.post(f"{BASE}/things/t1/go").json() == {"ok": 2}


@needs_fastapi
def test_real_routes_unknown_and_wrong_method(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_background(monkeypatch)
    registry = importlib.import_module("cloud_compute_core.registry")
    app = FastAPI()
    app.include_router(api.build_router(registry.collect_routes()), prefix=BASE)
    client = TestClient(app)
    r = client.get(f"{BASE}/nonexistent")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = client.post(f"{BASE}/health")
    assert r.status_code == 405 and r.json()["error"]["code"] == "invalid"
    assert client.get(f"{BASE}/health").status_code == 200
