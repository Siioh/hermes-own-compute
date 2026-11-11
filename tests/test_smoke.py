"""Smoke test: the plugin package imports from its path and register() works with a recording ctx."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "cloud_compute_smoke"


def _load_plugin():
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(PACKAGE + ".")]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    """Records every registration call."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, attr):
        def recorder(*args, **kwargs):
            self.calls.append((attr, args, kwargs))

        return recorder

    def names(self, method):
        return [c for c in self.calls if c[0] == method]


def test_package_imports_from_path():
    module = _load_plugin()
    assert callable(module.register)
    assert module.PLUGIN_ID == "cloud-compute"


def test_register_runs_and_registers_hooks():
    module = _load_plugin()
    ctx = FakeCtx()
    assert module.register(ctx) is None
    hooks = [c[1][0] for c in ctx.names("register_hook")]
    assert sorted(hooks) == ["pre_llm_call", "pre_tool_call"]
    for _, args, _ in ctx.names("register_hook"):
        assert callable(args[1])
    for _, _, kwargs in ctx.names("register_tool"):
        assert kwargs["name"] in module.PROVIDES_TOOLS


def test_register_builds_no_services():
    module = _load_plugin()
    module.register(FakeCtx())
    assert module._SERVICES is None


def test_manifest_basics():
    text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    assert "name: cloud-compute" in text
    assert 'version: "0.1.0"' in text
    assert 'requires_hermes: ">=0.23.0"' in text
    assert "provides_hooks: [pre_llm_call, pre_tool_call]" in text
    assert "capabilities: []" in text
