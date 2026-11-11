"""register(ctx) integration: speed, no network, manifest truthfulness, tool wrappers, hooks."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "cloud_compute_register_test"


# --- tiny YAML subset parser (plugin.yaml only; PyYAML is not available in the test env) ---

def _scalar(text: str):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(part) for part in inner.split(",")] if inner else []
    if text in ("true", "false"):
        return text == "true"
    try:
        return int(text)
    except ValueError:
        return text


def _lines(text: str):
    out = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append([len(raw) - len(raw.lstrip(" ")), stripped])
    return out


def _block(lines, i: int, indent: int):
    if lines[i][1].startswith("- "):
        items = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            lines[i] = [indent + 2, lines[i][1][2:]]
            value, i = _block(lines, i, indent + 2)
            items.append(value)
        return items, i
    mapping = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        key, _, rest = lines[i][1].partition(":")
        i += 1
        if rest.strip():
            mapping[key.strip()] = _scalar(rest)
        elif i < len(lines) and lines[i][0] > indent:
            mapping[key.strip()], i = _block(lines, i, lines[i][0])
        elif i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            mapping[key.strip()], i = _block(lines, i, indent)
        else:
            mapping[key.strip()] = None
    return mapping, i


def parse_manifest() -> dict:
    lines = _lines((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    data, end = _block(lines, 0, 0)
    assert end == len(lines), f"unparsed plugin.yaml line: {lines[end]}"
    assert isinstance(data, dict)
    return data


# --- plugin loading and a recording ctx ---

def _evict():
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(PACKAGE + ".")]:
        del sys.modules[name]


def _load_plugin():
    _evict()
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    def __init__(self):
        self.tools: dict[str, dict] = {}
        self.hooks: dict[str, Any] = {}
        self.sections: list[tuple] = []
        self.skills: list[tuple] = []
        self.commands: list[tuple] = []
        self.redaction: list[list] = []
        self.unload: list[Any] = []

    def on_unload(self, callback):
        assert callable(callback)
        self.unload.append(callback)

    def register_redaction_patterns(self, patterns):
        self.redaction.append(list(patterns))
        return len(self.redaction[-1])

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                      is_async=False, description="", emoji="", override=False):
        assert not override
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler,
                            "description": description, "is_async": is_async}

    def register_hook(self, hook_name, callback, *, hold_stream=False):
        assert hook_name not in self.hooks
        self.hooks[hook_name] = callback

    def register_system_prompt_section(self, id, content, *, position="after_memory", max_chars=2000):
        self.sections.append((id, content, max_chars))

    def register_skill(self, name, path, description="", frontmatter=None):
        assert Path(path).is_file()
        self.skills.append((name, Path(path), description))

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands.append((name, handler, description, args_hint))


@pytest.fixture
def bg_calls(monkeypatch):
    """Records background.ensure_started calls instead of starting a real loop."""
    calls = []
    monkeypatch.setattr(_core("background"), "ensure_started",
                        lambda services, **kw: calls.append(services) or False)
    return calls


@pytest.fixture
def plugin(monkeypatch, bg_calls):
    _core("registry").reset_shared_services()
    module = _load_plugin()

    def services_disabled():
        # Never open the real default store from a unit test; tests that need services inject them.
        raise RuntimeError("services are not available in this test")

    monkeypatch.setattr(_core("registry"), "build_services", services_disabled)
    yield module
    _core("registry").reset_shared_services()
    _evict()


def _core(name: str):
    # The plugin loads core/ once as the top-level package cloud_compute_core (the same copy
    # the dashboard API uses), not as a sub-package of itself.
    return importlib.import_module(f"cloud_compute_core.{name}")


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access during register()")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


# --- manifest ---

def test_manifest_shape():
    data = parse_manifest()
    assert data["name"] == "cloud-compute"
    assert data["version"] == "0.1.0"
    assert data["requires_hermes"] == ">=0.23.0"
    assert data["capabilities"] == []
    assert data["provides_hooks"] == ["pre_llm_call", "pre_tool_call"]
    assert len(data["provides_tools"]) == 15
    assert data["optional_env"] == [
        {"name": "CC_AGENT_API_KEY",
         "description": "Spend-capped model key used only inside cloud-agent sandboxes",
         "secret": True},
        {"name": "HF_TOKEN",
         "description": "Optional Hugging Face token passed into sandboxes that use the HF_TOKEN variable",
         "secret": True},
        {"name": "AWS_ACCESS_KEY_ID",
         "description": "EC2 hosts only: access key id of an IAM user made for this plugin (never the root user)",
         "secret": True},
        {"name": "AWS_SECRET_ACCESS_KEY",
         "description": "EC2 hosts only: the secret half of that IAM user's key",
         "secret": True},
        {"name": "AWS_SESSION_TOKEN",
         "description": "EC2 hosts only, optional: session token when the key is temporary",
         "secret": True},
    ]
    # v0.23.0 reads both keys (hermes_cli/plugins_manifest.py _KNOWN_MANIFEST_FIELDS, SUPPORTED_MANIFEST_VERSION = 2)
    assert (data["manifest_version"], data["api_version"]) == (2, 1)


def test_declared_secrets_cover_every_builtin_template_secret():
    """Every secret a built-in template reads is declared in optional_env (read with get_secret)."""
    from cloud_compute_core.agents import AGENT_TEMPLATES
    from cloud_compute_core.templates import builtin_templates

    declared = {e["name"] for e in parse_manifest()["optional_env"]}
    assert not [v for t in builtin_templates() for v in t.variables]     # built-ins send nothing
    used = {v.source.split(":", 1)[1] for t in [*builtin_templates(), *AGENT_TEMPLATES]
            for v in t.variables if v.source.startswith("secret:")}
    assert "CC_AGENT_API_KEY" in used
    assert used <= declared, used - declared


def test_description_discloses_what_the_plugin_does():
    text = parse_manifest()["description"].lower()
    for phrase in ("ssh/slurm hosts", "containers", "ssh tunnels", "after your approval", "background loop"):
        assert phrase in text, phrase
    assert len(text) < 500


def test_manifest_lists_match_register_constants(plugin):
    data = parse_manifest()
    assert list(plugin.PROVIDES_TOOLS) == data["provides_tools"]
    assert list(plugin.PROVIDES_HOOKS) == data["provides_hooks"]


def test_config_schema_mirrors_config_fields():
    from cloud_compute_core.config import CONFIG_FIELDS

    schema = parse_manifest()["config_schema"]
    assert list(schema) == list(CONFIG_FIELDS)
    for key, meta in CONFIG_FIELDS.items():
        assert schema[key] == meta, key
        if meta["type"] == "int":         # every int field declares min/max and shows its range
            assert "min" in schema[key] and "max" in schema[key], key
            assert f"({meta['min']}-{meta['max']})" in schema[key]["description"], key


# --- register() ---

def test_register_fast_and_offline(plugin, no_network):
    ctx = FakeCtx()
    started = time.perf_counter()
    plugin.register(ctx)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"register() took {elapsed:.3f}s"
    assert plugin._SERVICES is None  # nothing built at register time


def test_registered_tools_and_hooks_match_manifest(plugin):
    data = parse_manifest()
    ctx = FakeCtx()
    plugin.register(ctx)
    assert set(ctx.tools) <= set(data["provides_tools"])
    assert sorted(ctx.hooks) == sorted(data["provides_hooks"])
    for entry in ctx.tools.values():
        assert entry["toolset"] == "cloud_compute"
        assert entry["is_async"] is False
    if os.environ.get("CC_STRICT_MANIFEST") == "1":
        assert sorted(ctx.tools) == sorted(data["provides_tools"])


def _spec(name, handler, **kw):
    interfaces = _core("interfaces")
    schema = {"name": name, "description": f"{name} test tool",
              "parameters": {"type": "object", "properties": {}}}
    return interfaces.ToolSpec(name=name, schema=schema, handler=handler, **kw)


def _register_with(plugin, monkeypatch, specs, services: Any = "SERVICES"):
    registry = _core("registry")
    monkeypatch.setattr(registry, "collect_tools", lambda: list(specs))
    monkeypatch.setattr(plugin, "_SERVICES", services)
    ctx = FakeCtx()
    plugin.register(ctx)
    return ctx


def test_undeclared_tool_is_never_registered(plugin, monkeypatch):
    ctx = _register_with(plugin, monkeypatch, [
        _spec("compute_status", lambda a, s, c: {}),
        _spec("rogue_tool", lambda a, s, c: {}),
    ])
    assert set(ctx.tools) == {"compute_status"}
    assert ctx.tools["compute_status"]["description"] == "compute_status test tool"


def test_tool_wrapper_success_passes_session_id(plugin, monkeypatch):
    seen = {}

    def handler(args, services, ctx_kwargs):
        seen.update(args=args, services=services, ctx=ctx_kwargs)
        return {"ok": True, "when": time.gmtime(0)[:1]}

    ctx = _register_with(plugin, monkeypatch, [_spec("compute_status", handler)])
    out = ctx.tools["compute_status"]["handler"]({"host_id": "h1"}, session_id="s-1", task_id="t-1",
                                                 parent_agent=object())
    assert isinstance(out, str)
    assert json.loads(out) == {"ok": True, "when": [1970]}
    assert seen["args"] == {"host_id": "h1"}
    assert seen["services"] == "SERVICES"
    assert seen["ctx"]["session_id"] == "s-1"
    assert seen["ctx"]["task_id"] == "t-1"
    assert "parent_agent" not in seen["ctx"]


def test_tool_wrapper_maps_api_error(plugin, monkeypatch):
    interfaces = _core("interfaces")

    def handler(args, services, ctx_kwargs):
        raise interfaces.ApiError(404, "not_found", "host h9 not found")

    ctx = _register_with(plugin, monkeypatch, [_spec("env_open", handler)])
    out = ctx.tools["env_open"]["handler"]({"kind": "terminal"})
    assert json.loads(out) == {"error": {"code": "not_found", "message": "host h9 not found"}}


def test_tool_wrapper_never_raises(plugin, monkeypatch):
    def boom(args, services, ctx_kwargs):
        raise RuntimeError("kaput")

    def unencodable(args, services, ctx_kwargs):
        return {"bad": float("nan"), "obj": object()}

    ctx = _register_with(plugin, monkeypatch, [_spec("env_exec", boom), _spec("slurm_jobs", unencodable)])
    out = json.loads(ctx.tools["env_exec"]["handler"]({}))
    assert out["error"]["code"] == "internal"
    assert "kaput" in out["error"]["message"]
    bad_args = json.loads(ctx.tools["env_exec"]["handler"](["not", "a", "dict"]))
    assert bad_args["error"]["code"] == "invalid"
    assert isinstance(ctx.tools["slurm_jobs"]["handler"](None), str)


def test_tool_wrapper_reports_unavailable_services(plugin, monkeypatch):
    interfaces = _core("interfaces")
    registry = _core("registry")

    def no_services():
        raise interfaces.ApiError(503, "unavailable", "store not installed")

    monkeypatch.setattr(registry, "build_services", no_services)
    ctx = _register_with(plugin, monkeypatch, [_spec("compute_status", lambda a, s, c: {})], services=None)
    out = json.loads(ctx.tools["compute_status"]["handler"]({}))
    assert out == {"error": {"code": "unavailable", "message": "store not installed"}}


def test_services_built_once_across_threads(plugin, monkeypatch):
    registry = _core("registry")
    calls = []

    def build():
        calls.append(1)
        time.sleep(0.05)
        return object()

    monkeypatch.setattr(registry, "build_services", build)
    results = []
    threads = [threading.Thread(target=lambda: results.append(plugin._services())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert len({id(r) for r in results}) == 1


# --- hooks ---

def test_fallback_hooks_fail_closed_without_sibling_modules(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_optional", lambda name: None)
    ctx = FakeCtx()
    plugin.register(ctx)
    assert sorted(ctx.hooks) == ["pre_llm_call", "pre_tool_call"]
    assert ctx.sections == [] and ctx.commands == []
    pre_tool = ctx.hooks["pre_tool_call"]
    assert ctx.hooks["pre_llm_call"](session_id="s", is_first_turn=True) is None
    for name, args in [("env_spawn", {"host_id": "x1", "template": "python-dev"}), ("slurm_submit", {}),
                       ("cloud_agent_spawn", {}), ("env_control", {"action": "destroy"}),
                       ("slurm_jobs", {"action": "cancel"}), ("env_control", None)]:
        decision = pre_tool(tool_name=name, args=args, session_id="s", task_id="t", tool_call_id="c")
        assert decision and decision["action"] == "approve", name
        assert decision["rule_key"].startswith(f"cloud-compute:{name}:")
    assert pre_tool(tool_name="env_spawn", args={"host_id": "x1"})["rule_key"].startswith("cloud-compute:env_spawn:")
    assert (pre_tool(tool_name="env_spawn", args={"host_id": "x1"})["rule_key"]
            != pre_tool(tool_name="env_spawn", args={"host_id": "x2"})["rule_key"])
    for name, args in [("compute_status", {}), ("env_control", {"action": "logs"}),
                       ("slurm_jobs", {"action": "list"}), ("terminal", {"command": "ls"})]:
        assert pre_tool(tool_name=name, args=args) is None, name


def test_pre_tool_call_fails_closed_when_approval_raises(plugin, monkeypatch):
    approval = _core("approval") if (PLUGIN_DIR / "core" / "approval.py").is_file() else None
    if approval is None:
        pytest.skip("core/approval.py not present")

    def make(get_tools, get_services):
        def broken(**kwargs):
            raise RuntimeError("broken")
        return broken

    monkeypatch.setattr(approval, "make_pre_tool_call", make)
    ctx = FakeCtx()
    plugin.register(ctx)
    decision = ctx.hooks["pre_tool_call"](tool_name="env_spawn", args={"host_id": "h"})
    assert decision["action"] == "approve"
    assert ctx.hooks["pre_tool_call"](tool_name="compute_status", args={}) is None


def test_hooks_use_sibling_modules_when_present(plugin):
    ctx = FakeCtx()
    plugin.register(ctx)
    if (PLUGIN_DIR / "core" / "approval.py").is_file():
        decision = ctx.hooks["pre_tool_call"](tool_name="env_spawn",
                                              args={"template": "python-dev", "host_id": "h1"},
                                              session_id="s", task_id="t", tool_call_id="c")
        assert decision and decision["action"] == "approve"
        assert ctx.hooks["pre_tool_call"](tool_name="read_file", args={}) is None
    # pre_llm_call never raises, even when services cannot be built here.
    ctx.hooks["pre_llm_call"](session_id="s", user_message="hi", conversation_history=[],
                              is_first_turn=True, model="m", platform="cli")


# --- prompt section, skill, command ---

def test_system_prompt_section_registered(plugin):
    if not (PLUGIN_DIR / "core" / "chat_context.py").is_file():
        pytest.skip("core/chat_context.py not present")
    ctx = FakeCtx()
    plugin.register(ctx)
    assert len(ctx.sections) == 1
    section_id, content, max_chars = ctx.sections[0]
    assert section_id == "cloud-compute"
    assert max_chars == 1500
    assert isinstance(content, str) and 0 < len(content) <= 1500


def test_skill_registered(plugin):
    skill = PLUGIN_DIR / "skills" / "guide" / "SKILL.md"
    if not skill.is_file():
        pytest.skip("skills/guide/SKILL.md not present")
    ctx = FakeCtx()
    plugin.register(ctx)
    assert len(ctx.skills) == 1
    name, path, description = ctx.skills[0]
    assert name == "guide"
    assert path.resolve() == skill.resolve()
    assert description


def test_compute_command_registered(plugin, monkeypatch):
    if not (PLUGIN_DIR / "core" / "commands.py").is_file():
        pytest.skip("core/commands.py not present")
    ctx = FakeCtx()
    plugin.register(ctx)
    assert len(ctx.commands) == 1
    name, handler, description, args_hint = ctx.commands[0]
    assert name == "compute"
    assert description
    assert args_hint == "[status|hosts|jobs]"
    monkeypatch.setattr(plugin, "_SERVICES", None)
    registry = _core("registry")

    def broken():
        raise RuntimeError("no store here")

    monkeypatch.setattr(registry, "build_services", broken)
    assert isinstance(handler("status"), str)  # never raises


# --- redaction patterns ---

def test_redaction_patterns_registered(plugin):
    import re

    ctx = FakeCtx()
    plugin.register(ctx)
    assert len(ctx.redaction) == 1
    patterns = ctx.redaction[0]
    assert patterns and len(set(patterns)) == len(patterns)
    assert tuple(patterns) == _core("redact").HOST_PATTERN_SOURCES
    for pattern in patterns:
        re.compile(pattern)
        assert re.match(r"[A-Za-z_\-]{2}", pattern)  # host requires >= 2 literal leading chars
    assert re.fullmatch(patterns[0], "sk-ant-" + "a" * 20)  # ws-scan:allow fake value
    # redaction patterns are not a declared capability
    assert parse_manifest()["capabilities"] == []


def test_redaction_registration_failure_is_swallowed(plugin):
    ctx = FakeCtx()

    def boom(patterns):
        raise RuntimeError("host exploded")

    ctx.register_redaction_patterns = boom
    plugin.register(ctx)
    assert ctx.tools


def test_old_host_without_redaction_api(plugin):
    class OldCtx(FakeCtx):
        register_redaction_patterns = property(lambda self: (_ for _ in ()).throw(AttributeError()))

    ctx = OldCtx()
    assert not hasattr(ctx, "register_redaction_patterns")
    plugin.register(ctx)
    assert ctx.tools and ctx.redaction == []


# --- background loop and shared services without the Desktop page (chat-only use) ---

class _ServicesStub:
    pass


def test_tool_call_starts_the_background_loop(plugin, monkeypatch, bg_calls):
    services = _ServicesStub()
    ctx = _register_with(plugin, monkeypatch, [_spec("compute_status", lambda a, s, c: {"ok": True})],
                         services=services)
    assert bg_calls == []                                  # register() starts nothing
    out = json.loads(ctx.tools["compute_status"]["handler"]({}))
    assert out == {"ok": True}
    assert bg_calls == [services]


def test_pre_llm_call_hook_starts_the_background_loop(plugin, monkeypatch, bg_calls):
    services = _ServicesStub()
    chat_context = _core("chat_context")
    monkeypatch.setattr(chat_context, "make_pre_llm_call", lambda get_services: (lambda **kw: None))
    ctx = _register_with(plugin, monkeypatch, [], services=services)
    assert bg_calls == []
    ctx.hooks["pre_llm_call"](session_id="s", is_first_turn=True)
    assert bg_calls == [services]


def test_background_failure_never_breaks_a_tool_call(plugin, monkeypatch):
    monkeypatch.setattr(_core("background"), "ensure_started",
                        lambda services, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    ctx = _register_with(plugin, monkeypatch, [_spec("compute_status", lambda a, s, c: {"ok": 1})],
                         services=_ServicesStub())
    assert json.loads(ctx.tools["compute_status"]["handler"]({})) == {"ok": 1}


def test_loop_really_starts_after_a_tool_call_without_any_rest_request(plugin, monkeypatch):
    """No bg_calls stub: the real ensure_started runs and a real BackgroundLoop thread appears."""
    background = _core("background")
    monkeypatch.undo()                                     # drop the fixture's stub and builder block
    background.stop()
    background._reset_ensure_for_tests()
    registry = _core("registry")
    monkeypatch.setattr(registry, "collect_background", lambda: [])
    testing = _core("testing")
    services = testing.make_services()
    try:
        monkeypatch.setattr(plugin, "_SERVICES", services)
        monkeypatch.setattr(registry, "collect_tools", lambda: [
            _spec("compute_status", lambda a, s, c: {"ok": True})])
        ctx = FakeCtx()
        plugin.register(ctx)
        assert background.current() is None                # nothing started by register()
        assert json.loads(ctx.tools["compute_status"]["handler"]({})) == {"ok": True}
        loop = background.current()
        assert loop is not None and loop.is_running
        assert loop.services is services
    finally:
        background.stop()
        background._reset_ensure_for_tests()
        registry.reset_shared_services()


def test_tools_and_rest_share_one_service_container(plugin, monkeypatch):
    registry = _core("registry")
    built = []

    def build():
        built.append(1)
        return _ServicesStub()

    monkeypatch.setattr(registry, "build_services", build)
    from_tools = plugin._services()
    spec = importlib.util.spec_from_file_location(
        "cloud_compute_plugin_api_shared_test", PLUGIN_DIR / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    try:
        spec.loader.exec_module(api)
        assert api.get_services() is from_tools
        assert built == [1]
    finally:
        sys.modules.pop(spec.name, None)


def test_tools_use_the_same_core_copy_as_the_dashboard(plugin):
    assert plugin._core_module("registry") is sys.modules["cloud_compute_core.registry"]
    assert plugin._api_error_type() is sys.modules["cloud_compute_core.interfaces"].ApiError


# --- unload cleanup ---

def test_register_adds_one_unload_callback(plugin, monkeypatch):
    ctx = _register_with(plugin, monkeypatch, [_spec("compute_status", lambda a, s, c: {})])
    assert len(ctx.unload) == 1 and callable(ctx.unload[0])


def test_unload_stops_the_loop_closes_the_store_and_is_idempotent(plugin, monkeypatch, tmp_path):
    background, registry, testing = _core("background"), _core("registry"), _core("testing")
    monkeypatch.undo()                                     # real ensure_started
    background.stop()
    background._reset_ensure_for_tests()
    monkeypatch.setattr(registry, "collect_background", lambda: [])
    services = testing.make_services(store=_core("store").Store(tmp_path / "cloud-compute.db"))
    closed = []
    real_close = services.store.close
    monkeypatch.setattr(services.store, "close", lambda: (closed.append(1), real_close())[1])
    try:
        monkeypatch.setattr(plugin, "_SERVICES", services)
        monkeypatch.setattr(registry, "collect_tools", lambda: [
            _spec("compute_status", lambda a, s, c: {"ok": True})])
        ctx = FakeCtx()
        plugin.register(ctx)
        ctx.tools["compute_status"]["handler"]({})
        loop = background.current()
        assert loop is not None and loop.is_running
        thread = loop._thread
        ctx.unload[0]()
        assert background.current() is None
        assert not loop.is_running and not thread.is_alive()
        assert closed == [1] and plugin._SERVICES is None
        ctx.unload[0]()                                    # second call: harmless
        assert closed == [1]
        # a reload can start the loop again
        monkeypatch.setattr(plugin, "_SERVICES", services)
        ctx.tools["compute_status"]["handler"]({})
        assert background.current() is not None and background.current().is_running
    finally:
        background.stop()
        background._reset_ensure_for_tests()
        registry.reset_shared_services()


def test_unload_never_raises(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_core_module", lambda name: (_ for _ in ()).throw(RuntimeError("boom")))
    plugin._cleanup()
    plugin._cleanup()


def test_unload_without_a_loaded_core_is_a_no_op(plugin, monkeypatch):
    monkeypatch.delitem(sys.modules, "cloud_compute_core", raising=False)
    plugin._cleanup()


def test_unload_registration_failure_is_swallowed(plugin):
    ctx = FakeCtx()
    ctx.on_unload = lambda cb: (_ for _ in ()).throw(TypeError("no"))
    plugin.register(ctx)
    assert ctx.tools


# --- minimal hosts ---

class MinimalCtx:
    """Only the two methods every host has; every optional surface is missing."""

    def __init__(self):
        self.tools: dict = {}
        self.hooks: dict = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_hook(self, hook_name, callback, **kw):
        self.hooks[hook_name] = callback


def test_minimal_host_logs_no_warnings(plugin, caplog):
    import logging

    ctx = MinimalCtx()
    with caplog.at_level(logging.DEBUG):
        plugin.register(ctx)
    assert ctx.tools and set(ctx.hooks) == {"pre_llm_call", "pre_tool_call"}
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], [r.getMessage() for r in warnings]
    assert any("skipping" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


# --- plugin off/on: everything recovers after the unload callback ---

def _load_dashboard_api():
    name = "cloud_compute_reload_api_under_test"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def reload_env(monkeypatch, tmp_path):
    """The real plugin and dashboard API on one temporary on-disk store, as in a gateway."""
    background, registry, store = _core("background"), _core("registry"), _core("store")
    background.stop()
    background._reset_ensure_for_tests()
    registry.reset_shared_services()
    db = tmp_path / "plugin-data" / "cloud-compute.db"
    monkeypatch.setattr(store, "_default_path", lambda: db)
    monkeypatch.setattr(registry, "collect_background", lambda: [])
    module = _load_plugin()
    api = _load_dashboard_api()
    api._reset_background_for_tests()
    ctx = FakeCtx()
    module.register(ctx)
    env = type("Env", (), {})()
    env.plugin, env.api, env.ctx, env.db = module, api, ctx, db
    yield env
    module._cleanup()
    background.stop()
    background._reset_ensure_for_tests()
    registry.reset_shared_services()
    sys.modules.pop("cloud_compute_reload_api_under_test", None)
    _evict()


def _route_call(api, method, path, services=None):
    route = next(r for r in api._registry.collect_routes()
                 if str(r.method).upper() == method and r.path == path)
    return api.dispatch(route, {}, {}, None, services or api.get_services())


def _add_host(env):
    testing = _core("testing")
    env.api.get_services().store.upsert_host(testing.sample_host())


def test_dashboard_route_works_after_unload(reload_env):
    env = reload_env
    _add_host(env)
    status, payload = _route_call(env.api, "GET", "/hosts")
    assert status == 200 and [h["spec"]["id"] for h in payload] == ["x1carbon"]
    env.plugin._cleanup()
    status, payload = _route_call(env.api, "GET", "/hosts")          # same on-disk store, reopened
    assert status == 200 and [h["spec"]["id"] for h in payload] == ["x1carbon"]
    status, payload = _route_call(env.api, "GET", "/summary")
    assert status == 200 and "error" not in payload


def test_mounted_router_works_after_unload(reload_env):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    env = reload_env
    app = FastAPI()
    app.include_router(env.api.router, prefix="/api/plugins/cloud-compute")   # mounted once, never again
    client = TestClient(app)
    _add_host(env)
    assert client.get("/api/plugins/cloud-compute/summary").status_code == 200
    env.plugin._cleanup()
    r = client.get("/api/plugins/cloud-compute/hosts")
    assert r.status_code == 200 and [h["spec"]["id"] for h in r.json()] == ["x1carbon"]
    assert client.get("/api/plugins/cloud-compute/summary").status_code == 200


def test_tool_call_works_after_unload(reload_env):
    env = reload_env
    _add_host(env)
    handler = env.ctx.tools["compute_status"]["handler"]
    assert [h["id"] for h in json.loads(handler({}))["hosts"]] == ["x1carbon"]
    env.plugin._cleanup()
    data = json.loads(handler({}))
    assert "error" not in data and [h["id"] for h in data["hosts"]] == ["x1carbon"]


def test_background_loop_restarts_when_a_request_arrives_after_unload(reload_env):
    env, background = reload_env, _core("background")
    env.api.ensure_background(env.api.get_services())
    first = background.current()
    assert first is not None and first.is_running
    env.plugin._cleanup()
    assert background.current() is None and not first.is_running
    _route_call(env.api, "GET", "/hosts")
    env.api.ensure_background(env.api.get_services())                # what every endpoint does
    second = background.current()
    assert second is not None and second is not first and second.is_running
    assert second.services is env.api.get_services()
    assert second.services.store is not first.services.store         # a freshly opened store


def test_unload_twice_and_reload_twice(reload_env):
    env = reload_env
    _add_host(env)
    for _ in range(2):
        env.plugin._cleanup()
        env.plugin._cleanup()                                        # idempotent
        assert _route_call(env.api, "GET", "/hosts")[0] == 200
        assert json.loads(env.ctx.tools["compute_status"]["handler"]({}))["hosts"]


def test_a_container_with_a_closed_store_is_rebuilt_not_served(reload_env):
    env, registry = reload_env, _core("registry")
    stale = env.api.get_services()
    stale.store.upsert_host(_core("testing").sample_host())
    stale.store.close()                                              # closed behind everyone's back
    fresh = env.api.get_services()
    assert fresh is not stale and fresh is registry.shared_services()
    assert _route_call(env.api, "GET", "/hosts", fresh)[0] == 200
    assert json.loads(env.ctx.tools["compute_status"]["handler"]({}))["hosts"]
