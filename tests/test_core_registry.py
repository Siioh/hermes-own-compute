"""Registry discovery, Services container and ApiError."""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

import cloud_compute_core
from cloud_compute_core import registry
from cloud_compute_core.interfaces import (
    ERROR_CODES, SERVICE_NAMES, ApiError, ApiRequest, BackgroundTask, Route, Services, ToolSpec,
)

PKG = "cloud_compute_core"


@pytest.fixture(autouse=True)
def _clean_errors():
    registry.clear_errors()
    yield
    registry.clear_errors()


@pytest.fixture
def no_api_modules(monkeypatch):
    for name in registry.API_MODULES:
        monkeypatch.setitem(sys.modules, f"{PKG}.{name}", None)


def _handler(req):
    return {"ok": True}


def _fake_api(monkeypatch, name, *, routes=None, tools=None, background=None, raises=None):
    mod = types.ModuleType(f"{PKG}.{name}")
    if routes is not None:
        mod.routes = (lambda: (_ for _ in ()).throw(raises)) if raises else (lambda: list(routes))
    if tools is not None:
        mod.tools = lambda: list(tools)
    if background is not None:
        mod.background_tasks = lambda: list(background)
    monkeypatch.setitem(sys.modules, f"{PKG}.{name}", mod)
    return mod


# --------------------------------------------------------------------------- contract constants


def test_module_tables():
    assert registry.API_MODULES == ("hosts_api", "instances_api", "slurm_api", "access_api",
                                    "docs_api", "summary_api", "agents_api", "files_api",
                                    "ec2_api")
    assert registry.SERVICE_MODULES == {
        "store": "store", "config": "config", "secrets": "secrets", "events": "events",
        "transports": "transport", "prober": "prober", "templates": "templates",
        "sandboxes": "sandbox", "reaper": "reaper", "slurm": "slurm", "access": "access",
        "agents": "agents", "files": "files", "ec2": "ec2"}
    assert tuple(registry.SERVICE_MODULES) == SERVICE_NAMES


def test_api_error_shape():
    err = ApiError(404, "not_found", "host x not found")
    assert (err.status, err.code, err.message) == (404, "not_found", "host x not found")
    assert err.to_dict() == {"error": {"code": "not_found", "message": "host x not found"}}
    assert isinstance(err, Exception) and str(err) == "host x not found"
    assert ERROR_CODES == {"not_found", "invalid", "conflict", "unavailable", "host_offline",
                           "auth_required", "limit_reached", "approval_required", "internal",
                           "not_implemented", "not_saved", "too_large", "not_running",
                           "nothing_to_save", "unknown_parameter", "needs_setup", "not_fetched"}


def test_plumbing_dataclass_defaults():
    r = Route("GET", "/hosts", _handler)
    assert r.summary == ""
    t = ToolSpec("compute_status", {"type": "object"}, lambda a, s, k: {})
    assert t.approval is None and t.toolset == "cloud_compute"
    with pytest.raises(Exception):
        r.path = "/x"  # frozen
    req = ApiRequest(path_params={"host_id": "a"}, query={}, body=None, services=Services())
    assert req.path_params["host_id"] == "a"
    b = BackgroundTask("liveness", lambda s: 15.0, lambda s: None)
    assert b.interval_s(None) == 15.0


# --------------------------------------------------------------------------- collection


def test_collect_empty_when_all_api_modules_absent(no_api_modules):
    assert registry.collect_routes() == []
    assert registry.collect_tools() == []
    assert registry.collect_background() == []
    assert registry.import_errors() == {}


def test_collect_from_injected_module(monkeypatch, no_api_modules):
    route = Route("GET", "/hosts", _handler, "List hosts")
    tool = ToolSpec("compute_status", {"type": "object"}, lambda a, s, k: {})
    task = BackgroundTask("liveness", lambda s: 15.0, lambda s: None)
    _fake_api(monkeypatch, "hosts_api", routes=[route], tools=[tool], background=[task])
    assert registry.collect_routes() == [route]
    assert registry.collect_tools() == [tool]
    assert registry.collect_background() == [task]


def test_module_may_omit_functions(monkeypatch, no_api_modules):
    _fake_api(monkeypatch, "docs_api", routes=[Route("GET", "/docs", _handler)])
    assert len(registry.collect_routes()) == 1
    assert registry.collect_tools() == []
    assert registry.collect_background() == []
    assert registry.import_errors() == {}


def test_failing_module_is_skipped_and_recorded(monkeypatch, no_api_modules):
    _fake_api(monkeypatch, "hosts_api", routes=[], raises=RuntimeError("boom"))
    good = Route("GET", "/instances", _handler)
    _fake_api(monkeypatch, "instances_api", routes=[good])
    assert registry.collect_routes() == [good]
    errors = registry.import_errors()
    assert "hosts_api" in errors and "boom" in errors["hosts_api"]
    assert "instances_api" not in errors


def test_invalid_items_are_skipped(monkeypatch, no_api_modules):
    good = Route("GET", "/a", _handler)
    _fake_api(monkeypatch, "hosts_api", routes=[good, {"method": "GET"}])
    assert registry.collect_routes() == [good]
    assert "hosts_api" in registry.import_errors()


def test_duplicates_keep_first(monkeypatch, no_api_modules):
    first = Route("GET", "/hosts", _handler, "first")
    second = Route("get", "/hosts", _handler, "second")
    other = Route("POST", "/hosts", _handler)
    _fake_api(monkeypatch, "hosts_api", routes=[first],
              tools=[ToolSpec("t1", {}, lambda a, s, k: {}, toolset="first")])
    _fake_api(monkeypatch, "instances_api", routes=[second, other],
              tools=[ToolSpec("t1", {}, lambda a, s, k: {}, toolset="second")])
    routes = registry.collect_routes()
    assert routes == [first, other]
    tools = registry.collect_tools()
    assert [t.toolset for t in tools] == ["first"]


@pytest.fixture
def extra_package_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_compute_core, "__path__", list(cloud_compute_core.__path__) + [str(tmp_path)])
    return tmp_path


def test_broken_import_is_recorded(monkeypatch, extra_package_dir):
    (extra_package_dir / "zz_broken_api.py").write_text("raise RuntimeError('broken at import')\n")
    (extra_package_dir / "zz_dep_api.py").write_text("import zz_no_such_dependency_xyz\n")
    monkeypatch.setattr(registry, "API_MODULES", ("zz_broken_api", "zz_dep_api", "zz_absent_api"))
    try:
        assert registry.collect_routes() == []
        errors = registry.import_errors()
        assert "broken at import" in errors["zz_broken_api"]
        assert "zz_no_such_dependency_xyz" in errors["zz_dep_api"]
        assert "zz_absent_api" not in errors
        status = registry.module_status()
        assert status["zz_broken_api"] is False and status["zz_absent_api"] is False
    finally:
        for n in ("zz_broken_api", "zz_dep_api"):
            sys.modules.pop(f"{PKG}.{n}", None)


def test_module_status(monkeypatch, no_api_modules):
    _fake_api(monkeypatch, "hosts_api", routes=[])
    monkeypatch.setitem(sys.modules, f"{PKG}.sandbox", None)
    status = registry.module_status()
    assert set(status) == set(registry.API_MODULES) | set(registry.SERVICE_MODULES.values())
    assert status["hosts_api"] is True
    assert status["slurm_api"] is False
    assert status["sandbox"] is False


# --------------------------------------------------------------------------- services


def test_build_services_missing_module_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, f"{PKG}.sandbox", None)
    services = registry.build_services()  # never fails at build time
    with pytest.raises(ApiError) as info:
        services.sandboxes
    assert info.value.status == 503
    assert info.value.code == "unavailable"
    assert info.value.message == "sandboxes not installed"
    assert services.available("sandboxes")  # provider present; available() never builds


def test_build_services_uses_module_provide(monkeypatch):
    mod = types.ModuleType(f"{PKG}.prober")
    calls = []

    def provide(services):
        calls.append(services)
        return {"prober": True, "events": services.events}

    mod.provide = provide
    monkeypatch.setitem(sys.modules, f"{PKG}.prober", mod)
    events = object()
    services = registry.build_services(events=events)
    assert services.prober["events"] is events
    assert services.prober is services.prober
    assert len(calls) == 1


def test_module_without_provide_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, f"{PKG}.reaper", types.ModuleType(f"{PKG}.reaper"))
    with pytest.raises(ApiError) as info:
        registry.build_services().reaper
    assert info.value.status == 503


def test_injected_instances_returned_as_is():
    store, config, fake = object(), object(), object()
    services = registry.build_services(store=store, config=config, transports=fake)
    assert services.store is store
    assert services.config is config
    assert services.transports is fake
    assert "store" in services.built()


def test_none_overrides_are_not_injected(monkeypatch):
    monkeypatch.setitem(sys.modules, f"{PKG}.store", None)
    services = registry.build_services(store=None)
    with pytest.raises(ApiError):
        services.store


def test_provider_built_once_across_threads():
    count = 0
    lock = threading.Lock()

    def provider(services):
        nonlocal count
        with lock:
            count += 1
        time.sleep(0.05)
        return object()

    services = Services(providers={"prober": provider})
    results = []
    threads = [threading.Thread(target=lambda: results.append(services.prober)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert count == 1
    assert len(results) == 8 and all(r is results[0] for r in results)


def test_providers_can_use_other_services():
    services = Services(providers={
        "store": lambda s: "STORE",
        "events": lambda s: ("events", s.store),
    })
    assert services.events == ("events", "STORE")


def test_provider_errors():
    def missing(services):
        raise ModuleNotFoundError("No module named 'x'", name="x")

    def bug(services):
        raise KeyError("bug")

    services = Services(providers={"slurm": missing, "access": bug})
    with pytest.raises(ApiError) as info:
        services.slurm
    assert (info.value.status, info.value.code, info.value.message) == (503, "unavailable",
                                                                       "slurm not installed")
    with pytest.raises(KeyError):
        services.access
    assert services.built() == []  # failures are not cached


def test_no_provider_and_available():
    services = Services(providers={"store": lambda s: 1}, events="E")
    assert services.available("events")
    assert services.available("store")
    assert not services.available("agents")
    assert services.built() == ["events"]  # available() did not build store
    with pytest.raises(ApiError) as info:
        services.agents
    assert info.value.status == 503 and info.value.message == "agents not installed"


def test_set_and_unknown_names():
    services = Services()
    services.set("templates", "T")
    assert services.templates == "T"
    services.reaper = "R"
    assert services.reaper == "R"
    with pytest.raises(AttributeError):
        services.not_a_service
    with pytest.raises(ValueError):
        services.set("nope", 1)
    with pytest.raises(ValueError):
        Services(nope=1)


def test_shared_services_config_follows_saved_settings_without_restart(monkeypatch):
    """A value saved in Settings reaches services built earlier, with no gateway restart."""
    from cloud_compute_core import config as config_mod

    saved = {"plugins": {"entries": {"cloud-compute": {"settings": {"agent_model": ""}}}}}
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_config = types.ModuleType("hermes_cli.config")
    hermes_config.load_config_readonly = lambda: saved
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)
    monkeypatch.setattr(config_mod, "REFRESH_SECONDS", 0.0)
    registry.reset_shared_services()
    try:
        services = registry.shared_services()
        config = services.config
        assert config.agent_model == ""
        saved["plugins"]["entries"]["cloud-compute"]["settings"]["agent_model"] = "qwen-test"
        assert registry.shared_services() is services and services.config is config
        assert config.agent_model == "qwen-test"
        assert services.config.agent_model == "qwen-test"
    finally:
        registry.reset_shared_services()


def test_shared_services_rebuilds_a_container_whose_store_was_closed(tmp_path, monkeypatch):
    from cloud_compute_core import store as store_mod
    from cloud_compute_core.testing import sample_host

    monkeypatch.setattr(store_mod, "_default_path", lambda: tmp_path / "cloud-compute.db")
    registry.reset_shared_services()
    try:
        first = registry.shared_services()
        assert registry.shared_services() is first and not registry.is_closed(first)
        first.store.upsert_host(sample_host())
        first.store.close()
        assert registry.is_closed(first)
        second = registry.shared_services()
        assert second is not first and not registry.is_closed(second)
        assert [h.id for h in second.store.list_hosts()] == ["x1carbon"]      # same database file
        assert registry.shared_services() is second
    finally:
        if registry._shared is not None and "store" in registry._shared.built():
            registry._shared.store.close()
        registry.reset_shared_services()


def test_is_closed_is_false_for_anything_that_is_not_a_container_with_a_closed_store():
    assert registry.is_closed(None) is False and registry.is_closed("SERVICES") is False
    assert registry.is_closed(registry.build_services()) is False          # nothing built yet


def test_files_service_resolves_and_is_implemented(store):
    """The stub is replaced: unknown ids now give 404, never 501 not_implemented."""
    services = registry.build_services(store=store)
    files = services.files
    assert type(files).__name__ == "FileManager"
    calls = [
        lambda: files.export("i-00000000"),
        lambda: files.export("i-00000000", "/work", reason="fetch"),
        lambda: files.list_dir("i-00000000", "/work"),
        lambda: files.put("i-00000000", "C:/a.txt", "/work/a.txt", extract=True),
        lambda: files.delete_export("x-00000000"),
    ]
    for call in calls:
        with pytest.raises(ApiError) as info:
            call()
        assert (info.value.status, info.value.code) == (404, "not_found")


def test_files_api_is_registered():
    assert "files_api" in registry.API_MODULES
    assert registry.module_status()["files_api"] is True
    assert registry.module_status()["files"] is True
    mod = registry._import("files_api")
    assert mod.routes() and [t.name for t in mod.tools()] == ["env_files"]
    assert mod.background_tasks() == []
