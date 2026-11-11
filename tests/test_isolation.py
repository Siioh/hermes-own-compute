"""The test isolation layer in conftest.py: no test may touch the caller's Hermes home,
start a real ssh/podman/docker/hermes process, reach the network or leave a loop running."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "cloud_compute_isolation_test"


def _core(name: str):
    return importlib.import_module(f"cloud_compute_core.{name}")


def _evict():
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(PACKAGE + ".")]:
        del sys.modules[name]


def _load_plugin():
    _evict()
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


class _Ctx:
    """Just enough of Hermes' plugin context for register()."""

    def __init__(self):
        self.tools: dict[str, Any] = {}
        self.hooks: dict[str, Any] = {}
        self.unload: list[Any] = []

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_hook(self, name, callback, **kw):
        self.hooks[name] = callback

    def on_unload(self, callback):
        self.unload.append(callback)

    def register_system_prompt_section(self, *a, **kw):
        pass

    def register_skill(self, *a, **kw):
        pass

    def register_command(self, *a, **kw):
        pass

    def register_redaction_patterns(self, patterns):
        return len(list(patterns))


@pytest.fixture
def hermes_home_resolver(monkeypatch):
    """Hermes' public home resolver when this Python has it; otherwise a stand-in that, like
    the real one, follows HERMES_HOME. Either way the plugin finds its default store."""
    try:
        import hermes_constants  # type: ignore[import-not-found]  # noqa: F401
    except Exception:  # noqa: BLE001 - plain Python: provide the public resolver
        fake = types.ModuleType("hermes_constants")
        fake.get_hermes_home = lambda: Path(os.environ["HERMES_HOME"])
        monkeypatch.setitem(sys.modules, "hermes_constants", fake)
        monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)


def test_every_test_gets_its_own_hermes_home(_isolation, isolation):
    home = Path(os.environ["HERMES_HOME"])
    assert home == _isolation and home.is_dir()
    assert home.parent == isolation.sandbox_root
    for original in isolation.original_homes:
        assert home.resolve() != original.resolve()
    assert not list(home.iterdir())                          # fresh: nothing from other tests


def test_plugin_entry_points_never_write_the_original_home(hermes_home_resolver, isolation,
                                                           monkeypatch):
    """Load and register the plugin like Hermes does, call a tool and the dashboard API through
    the real shared container (no injected store), start and stop the real loop: the database
    lands in the test's home and the caller's plugin-data is byte-for-byte unchanged."""
    before = isolation.listing(isolation.original_homes)
    registry, background = _core("registry"), _core("background")
    monkeypatch.setattr(registry, "collect_background", lambda: [])     # an idle loop
    plugin = _load_plugin()
    try:
        ctx = _Ctx()
        plugin.register(ctx)
        out = json.loads(ctx.tools["compute_status"]({}))
        assert "error" not in out, out
        loop = background.current()
        assert loop is not None and loop.is_running           # the real loop, on the shared store

        store = registry.shared_services().store
        home = Path(os.environ["HERMES_HOME"]).resolve()
        assert Path(store.path).resolve().is_relative_to(home)
        assert Path(store.path).exists()

        spec = importlib.util.spec_from_file_location(
            "cloud_compute_isolation_api", PLUGIN_DIR / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = api
        try:
            spec.loader.exec_module(api)
            assert api.get_services() is registry.shared_services()
        finally:
            sys.modules.pop(spec.name, None)

        for callback in ctx.unload:
            callback()
        assert background.current() is None
    finally:
        _evict()
    assert isolation.listing(isolation.original_homes) == before
    assert isolation.home_changes() == []


def test_default_store_path_outside_the_sandbox_is_refused(monkeypatch, isolation):
    target = Path.home() / "cc-isolation-probe-must-not-exist"
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: target
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)
    with pytest.raises(isolation.IsolationError, match="outside the test's temporary folders"):
        _core("store").Store()
    isolation.forgive(1)
    assert not target.exists()


@pytest.mark.parametrize("argv", [
    ["ssh", "-o", "BatchMode=yes", "x1carbon", "true"],
    [r"C:\Windows\System32\OpenSSH\ssh.exe", "x1carbon", "true"],
    ["/usr/bin/podman", "ps"],
    ["docker.exe", "ps"],
    ["hermes", "chat"],
    ["git", "clone", "https://example.com/repo.git", "x"],
    ["git", "clone", "git@example.com:org/repo.git", "x"],
])
def test_real_remote_processes_are_refused(argv, isolation):
    with pytest.raises(isolation.IsolationError, match="refused to start a real"):
        subprocess.run(argv, capture_output=True, timeout=5)
    isolation.forgive(1)


@pytest.mark.parametrize("argv, name", [
    ([r"C:\Windows\System32\OpenSSH\ssh.exe", "x"], "ssh"),
    (["C:/Program Files/Docker/docker.exe", "ps"], "docker"),
    (["/usr/bin/podman", "ps"], "podman"),
    ([r"\\server\share\scp.EXE"], "scp"),
    (r'"C:\Windows\System32\OpenSSH\ssh.exe" host true', "ssh"),
])
def test_executable_names_are_split_on_both_separators_on_every_platform(isolation, argv, name):
    assert isolation.exe_name(argv) == name
    assert isolation.blocked(argv) == name


@pytest.mark.parametrize("argv", [
    ["osascript", "-e", 'tell application "Terminal" to do script "x"'],
    ["/usr/bin/osascript", "-e", "x"],
    ["launchctl", "list"],
    ["/bin/launchctl", "bootstrap", "gui/501", "x.plist"],
    ["/usr/bin/python3", "-m", "hermes_cli.main", "gateway", "run", "--idle-exit"],
    ["python", "-c", "import hermes_cli"],
    ["sh", "-c", "cd /work && hermes chat -Q --query-file q"],
    ["bash", "-lc", "exec /opt/hermes/bin/hermes gateway run"],
    [r"C:\Tools\hermes.EXE", "chat"],
    "sh -c 'hermes gateway run --quiet'",
])
def test_hermes_reached_another_way_and_macos_launchers_are_refused(argv, isolation):
    """A test reached Hermes by ``python -m hermes_cli`` / a shell."""
    assert isolation.blocked(argv) is not None


def test_a_stand_in_in_the_temp_folder_still_cannot_start_hermes(tmp_path, isolation):
    stand_in = str(tmp_path / "python3")
    assert isolation.blocked([stand_in, "-c", "print(1)"]) is None       # still allowed
    assert isolation.blocked([stand_in, "-m", "hermes_cli"]) == "Hermes"


def test_python_importing_hermes_is_refused_before_it_starts(isolation):
    with pytest.raises(isolation.IsolationError, match="refused to start a real Hermes"):
        subprocess.run([sys.executable, "-c", "import hermes_cli"], capture_output=True,
                       timeout=30)
    isolation.forgive(1)


@pytest.mark.parametrize("argv", [
    ["git", "commit", "-m", "hermes notes"],
    ["node", "C:/Users/x/Hermes-Plugin-Development/desktop/smoke.mjs"],
    ["sh", "-c", "echo hermes-agent image"],
])
def test_ordinary_commands_that_mention_hermes_still_run(argv, isolation):
    assert isolation.blocked(argv) is None


def test_ssh_transport_cannot_reach_a_real_host(isolation):
    transport = _core("transport")
    from cloud_compute_core.testing import sample_host

    t = transport.SubprocessSSHTransport(sample_host(address="x1carbon"),
                                         transport.resolve_ssh_binary())
    with pytest.raises(isolation.IsolationError):
        t.run(["true"], timeout=5)
    isolation.forgive(1)


def test_local_processes_and_local_git_still_run(tmp_path):
    out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True,
                         timeout=60)
    assert out.stdout.strip() == "ok"
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", str(src)], check=True, timeout=60)
    subprocess.run(["git", "clone", "-q", str(src), str(tmp_path / "dst")], check=True,
                   capture_output=True, timeout=60)
    assert (tmp_path / "dst" / ".git").is_dir()


def test_network_is_refused_but_loopback_works(isolation):
    with pytest.raises(isolation.IsolationError, match="refused a network connection"):
        socket.create_connection(("192.0.2.10", 22), timeout=1)
    with pytest.raises(isolation.IsolationError):
        socket.getaddrinfo("x1carbon", 22)
    isolation.forgive(2)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()
    finally:
        server.close()


def test_a_running_loop_is_stopped_and_reported(store, isolation):
    background = _core("background")
    from cloud_compute_core.testing import make_services

    loop = background.start(make_services(store=store), tasks=[], tick_s=0.05)
    assert loop.is_running
    leftovers = isolation.reset_shared_state()
    assert any("still running" in text for text in leftovers)
    assert not loop.is_running and background.current() is None
    assert isolation.reset_shared_state() == []


def test_home_change_detection_reports_a_write(tmp_path, isolation):
    """The session-end check: a file added under a watched home's plugin-data is reported."""
    home = tmp_path / "watched-home"
    (home / "plugin-data" / "cloud-compute").mkdir(parents=True)
    baseline = isolation.listing([home])
    assert isolation.home_changes([home], baseline) == []
    (home / "plugin-data" / "cloud-compute" / "cloud-compute.db").write_bytes(b"x")
    changes = isolation.home_changes([home], baseline)
    assert len(changes) == 1 and changes[0].startswith("new: ")
