"""Shared pytest setup: loads ``core/`` as ``cloud_compute_core`` and provides fakes.

It also isolates every test from the machine it runs on (see ``_isolation``):

- ``HERMES_HOME`` points at a fresh temporary folder for each test, so the plugin's
  default database, exports folder and env-file folder land there, never in the
  caller's Hermes home. Any default store path outside the temporary folders is an error.
- After each test the shared service container is closed and forgotten and the
  background loop is stopped; a loop thread still alive at teardown fails the test.
- Starting ``ssh``, ``podman``, ``docker`` or ``hermes``, or ``git`` against a network
  remote, raises :class:`IsolationError` unless the test is marked ``live`` and
  ``CC_LIVE=1``. Outbound connections to anything but loopback are refused the same way.
- At session end the original Hermes home's ``plugin-data`` folders are compared with
  a listing taken at session start; any change fails the run.
"""

from __future__ import annotations

import importlib
import importlib.util
import ipaddress
import itertools
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = PLUGIN_DIR / "core"
CORE_PACKAGE = "cloud_compute_core"
PLUGIN_NAME = "cloud-compute"
LIVE = os.environ.get("CC_LIVE") == "1"


class IsolationError(RuntimeError):
    """A test tried to leave its sandbox (real home, real process, real network)."""


# --------------------------------------------------------------------------- isolation: homes


def _original_homes() -> list[Path]:
    """Every folder the caller's Hermes could use as its home (read before any override)."""
    homes: list[Path] = []
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        homes.append(Path(env))
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
        homes.append(Path(get_hermes_home()))
    except Exception:  # noqa: BLE001 - not running inside a Hermes environment
        pass
    homes.append(Path.home() / ("." + "hermes"))
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        homes.append(Path(local) / "hermes")
    for root in list(homes):                 # every profile of each candidate Hermes root
        try:
            homes.extend(sorted(p for p in (root / "profiles").iterdir() if p.is_dir()))
        except OSError:
            pass
    unique: list[Path] = []
    for h in homes:
        if h not in unique:
            unique.append(h)
    return unique


def _listing(homes: list[Path]) -> dict[str, tuple[int, int]]:
    """{path: (size, mtime_ns)} of each home's ``plugin-data/cloud-compute`` folder (if any)
    and everything under it. Other plugins' folders are not ours to watch."""
    out: dict[str, tuple[int, int]] = {}
    for home in homes:
        ours = home / "plugin-data" / PLUGIN_NAME
        if ours.is_dir():
            out[str(ours)] = (0, 0)
            for p in ours.rglob("*"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


ORIGINAL_HOMES = _original_homes()
ORIGINAL_LISTING = _listing(ORIGINAL_HOMES)
#: Session-wide sandbox root; each test gets ``<root>/home-<n>`` as HERMES_HOME.
SANDBOX_ROOT = Path(tempfile.mkdtemp(prefix="cc-test-home-"))
_home_counter = itertools.count(1)
os.environ["HERMES_HOME"] = str(SANDBOX_ROOT / "home-0")     # imports and collection too
os.environ.pop("HERMES_DATA_DIR_SUFFIX", None)


def _resolved(p: Path | str) -> Path:
    try:
        return Path(os.path.realpath(p))
    except OSError:
        return Path(os.path.abspath(p))


def _inside(path: Path | str, roots: list[Path]) -> bool:
    rp = _resolved(path)
    for root in roots:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


_ALLOWED_ROOTS: list[Path] = [_resolved(SANDBOX_ROOT), _resolved(tempfile.gettempdir())]


# --------------------------------------------------------------------------- isolation: processes

_BLOCKED_EXES = {"ssh", "podman", "docker", "hermes", "scp", "sftp", "sbatch", "srun",
                 # macOS service launchers: Hermes hands gateways to launchd through these,
                 # and no unit test needs either.
                 "osascript", "launchctl"}
#: A Hermes reached some other way than by its executable name: ``python -m hermes_cli``,
#: ``import hermes_cli``, or ``hermes chat`` / ``hermes gateway`` inside a shell command line.
_HERMES_CMDLINE = re.compile(
    r"\bhermes_cli\b|(?:^|[\s;&|(`'\"/\\])hermes(?:\.exe)?\s+(?:gateway|chat)\b", re.I)
#: git subcommands that contact a remote; others (``remote add``, ``bundle``) stay local.
_GIT_NETWORK_CMDS = {"clone", "fetch", "pull", "push", "ls-remote", "submodule", "archive"}
_NETWORK_REMOTE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://(?!/)|[^\s/\\:@]+@[^\s/\\:]+:)", re.I)
_state = {"allow_real": LIVE, "violations": []}
_RealPopen = subprocess.Popen


def _exe_name(args) -> str:
    first = args[0] if isinstance(args, (list, tuple)) and args else args
    if isinstance(args, str):
        line = str(first).strip()
        text = line[1:].split('"', 1)[0] if line.startswith('"') else line.split(" ")[0]
    else:
        text = str(first)
    # Both separators on every platform: a Windows path must not slip past the guard on Linux.
    name = text.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _blocked(args) -> str | None:
    argv = [str(a) for a in args] if isinstance(args, (list, tuple)) else str(args).split()
    exe = _exe_name(args)
    first = argv[0] if argv else ""
    if _HERMES_CMDLINE.search(" ".join(argv)):
        return "Hermes"                  # checked first: no stand-in may start a real Hermes
    if exe in ("osascript", "launchctl"):
        return exe
    if os.path.isabs(first) and _inside(first, _ALLOWED_ROOTS):
        return None                      # a stand-in script a test wrote into its temp folder
    if exe in _BLOCKED_EXES:
        return exe
    if exe == "git" and _GIT_NETWORK_CMDS & set(argv[1:]) and any(
            _NETWORK_REMOTE.match(a) and not a.lower().startswith("file:") for a in argv[1:]):
        return "git (network remote)"
    return None


class _GuardedPopen(_RealPopen):
    def __init__(self, args, *a, **kw):
        if not _state["allow_real"]:
            what = _blocked(args)
            if what:
                msg = (f"test isolation: refused to start a real {what} process "
                       f"({str(args)[:160]}); use the fake transports, or mark the test "
                       f"'live' and run with CC_LIVE=1")
                _state["violations"].append(msg)
                raise IsolationError(msg)
        super().__init__(args, *a, **kw)


subprocess.Popen = _GuardedPopen         # before core is imported, so default args see it


# --------------------------------------------------------------------------- isolation: network

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", ""}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _is_local(host) -> bool:
    if host is None:
        return True
    host = host.decode() if isinstance(host, bytes) else str(host)
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _refuse_network(host) -> None:
    msg = (f"test isolation: refused a network connection to {host!r}; only loopback is "
           f"allowed outside live tests (CC_LIVE=1)")
    _state["violations"].append(msg)
    raise IsolationError(msg)


def _addr_host(address):
    return address[0] if isinstance(address, tuple) and address else None


def _guarded_connect(self, address):
    if not _state["allow_real"] and self.family in (socket.AF_INET, socket.AF_INET6) \
            and not _is_local(_addr_host(address)):
        _refuse_network(_addr_host(address))
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _state["allow_real"] and self.family in (socket.AF_INET, socket.AF_INET6) \
            and not _is_local(_addr_host(address)):
        _refuse_network(_addr_host(address))
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host, *a, **kw):
    if not _state["allow_real"] and not _is_local(host):
        _refuse_network(host)
    return _real_getaddrinfo(host, *a, **kw)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo


def _load_core():
    existing = sys.modules.get(CORE_PACKAGE)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        CORE_PACKAGE, CORE_DIR / "__init__.py", submodule_search_locations=[str(CORE_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[CORE_PACKAGE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(CORE_PACKAGE, None)
        raise
    return module


_core = _load_core()


def _optional(name: str):
    """Import ``cloud_compute_core.<name>``; ``None`` if that module is absent."""
    full = f"{CORE_PACKAGE}.{name}"
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name == full:
            return None
        raise


def pytest_collection_modifyitems(config, items):
    if os.environ.get("CC_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: set CC_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


# --------------------------------------------------------------------------- isolation: per test

_store_mod = importlib.import_module(f"{CORE_PACKAGE}.store")
_unguarded_default_path = _store_mod._default_path


def _guarded_default_path() -> Path:
    """The real resolver, refused when it lands outside this run's temporary folders."""
    path = _unguarded_default_path()
    if not _inside(path, _ALLOWED_ROOTS):
        msg = f"test isolation: the default store path {path} is outside the test's temporary folders"
        _state["violations"].append(msg)
        raise IsolationError(msg)
    return path


_store_mod._default_path = _guarded_default_path
_BG_THREAD = "cloud-compute-bg"
_registry_mod = importlib.import_module(f"{CORE_PACKAGE}.registry")
_background_mod = importlib.import_module(f"{CORE_PACKAGE}.background")
# The real functions, kept here: a test may monkeypatch them, and its patches are still in
# place while this teardown runs.
_REAL = {
    "current": _background_mod.current, "stop": _background_mod.stop,
    "reset_ensure": _background_mod._reset_ensure_for_tests,
    "reset_shared": _registry_mod.reset_shared_services, "is_closed": _registry_mod.is_closed,
    "join_workers": _background_mod.join_workers,
}
#: How long a test's teardown waits for the plugin's worker threads (probes, polls)
#: before it closes the shared store, as the unload callback does.
_WORKER_WAIT_S = 2.0


def _bg_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == _BG_THREAD and t.is_alive()]


def _reset_shared_state() -> list[str]:
    """Stop the background loop and drop the shared container; returns what was left running."""
    leftovers: list[str] = []
    try:
        loop = _REAL["current"]()
        if loop is not None and loop.is_running:
            leftovers.append(f"background loop {loop.owner} still running")
        _REAL["stop"]()
    finally:
        _REAL["reset_ensure"]()
    try:
        _REAL["join_workers"](_WORKER_WAIT_S)     # workers first, then the store (as on unload)
    except Exception:  # noqa: BLE001 - teardown must not mask test results
        pass
    shared = getattr(_registry_mod, "_shared", None)
    if shared is not None:
        try:
            if "store" in shared.built() and not _REAL["is_closed"](shared):
                shared.store.close()
        except Exception:  # noqa: BLE001 - teardown must not mask test results
            pass
        _REAL["reset_shared"]()
    stray = _bg_threads()
    for t in stray:
        t.join(timeout=2.0)
    alive = [t for t in stray if t.is_alive()]
    if alive:
        leftovers.append(f"{len(alive)} {_BG_THREAD} thread(s) still alive after stop")
    elif stray and not leftovers:
        leftovers.append(f"{len(stray)} {_BG_THREAD} thread(s) outlived the test")
    return leftovers


@pytest.fixture(autouse=True)
def _isolation(request):
    """Per-test sandbox: own HERMES_HOME, no real processes or network, nothing left running.

    Uses its own MonkeyPatch, not the ``monkeypatch`` fixture: requesting that here would
    create it before every other fixture and so undo the test's patches last, after
    fixtures whose teardown expects them already gone.
    """
    home = SANDBOX_ROOT / f"home-{next(_home_counter)}"
    home.mkdir()
    env = pytest.MonkeyPatch()
    env.setenv("HERMES_HOME", str(home))
    env.delenv("HERMES_DATA_DIR_SUFFIX", raising=False)
    allow = LIVE and request.node.get_closest_marker("live") is not None
    _state["allow_real"], before = allow, len(_state["violations"])
    try:
        yield home
    finally:
        _state["allow_real"] = LIVE
        try:
            leftovers = _reset_shared_state()
        finally:
            env.undo()
        violations = _state["violations"][before:]
    problems = leftovers + violations
    if problems:
        pytest.fail("test isolation:\n  " + "\n  ".join(problems), pytrace=False)


class _IsolationHandle:
    """What tests/test_isolation.py inspects: the sandbox, the original homes, the guards."""

    IsolationError = IsolationError
    sandbox_root = SANDBOX_ROOT
    original_homes = ORIGINAL_HOMES
    original_listing = ORIGINAL_LISTING
    listing = staticmethod(_listing)
    reset_shared_state = staticmethod(_reset_shared_state)
    allowed_roots = _ALLOWED_ROOTS
    exe_name = staticmethod(_exe_name)
    blocked = staticmethod(_blocked)

    @staticmethod
    def home_changes(homes: list[Path] | None = None,
                     baseline: dict[str, tuple[int, int]] | None = None) -> list[str]:
        return _home_changes(homes, baseline)

    def violations(self) -> list[str]:
        return list(_state["violations"])

    def forgive(self, count: int) -> None:
        """Drop the last ``count`` recorded violations (a test that provoked them on purpose)."""
        del _state["violations"][len(_state["violations"]) - count:]


@pytest.fixture
def isolation():
    return _IsolationHandle()


def pytest_sessionstart(session):
    factory = getattr(session.config, "_tmp_path_factory", None)
    if factory is not None:              # --basetemp may sit outside the system temp folder
        _ALLOWED_ROOTS.append(_resolved(factory.getbasetemp()))


def pytest_sessionfinish(session, exitstatus):
    _reset_shared_state()
    changed = _home_changes()
    shutil.rmtree(SANDBOX_ROOT, ignore_errors=True)
    if changed:
        lines = "\n".join(f"  {c}" for c in changed[:40])
        sys.stderr.write(
            "\n" + "!" * 78 + "\nTEST ISOLATION FAILURE: the test run changed the caller's Hermes "
            f"home:\n{lines}\n" + "!" * 78 + "\n")
        session.exitstatus = 1


def _home_changes(homes: list[Path] | None = None,
                  baseline: dict[str, tuple[int, int]] | None = None) -> list[str]:
    """Differences between the original homes' plugin-data now and at session start."""
    homes = ORIGINAL_HOMES if homes is None else homes
    baseline = ORIGINAL_LISTING if baseline is None else baseline
    now = _listing(homes)
    changes = [f"new: {p}" for p in sorted(set(now) - set(baseline))]
    changes += [f"removed: {p}" for p in sorted(set(baseline) - set(now))]
    changes += [f"modified: {p}" for p in sorted(set(now) & set(baseline)) if now[p] != baseline[p]]
    return changes


@pytest.fixture
def core():
    return _core


@pytest.fixture
def fake_transport():
    from cloud_compute_core.testing import FakeTransport, sample_host

    return FakeTransport(sample_host())


@pytest.fixture
def fake_transports():
    from cloud_compute_core.testing import FakeTransportFactory

    return FakeTransportFactory()


@pytest.fixture
def fake_events():
    from cloud_compute_core.testing import FakeEvents

    return FakeEvents()


@pytest.fixture
def fake_secrets():
    from cloud_compute_core.testing import FakeSecrets

    return FakeSecrets()


def _make_store(tmp_path: Path):
    module = _optional("store")
    if module is None:
        return None
    return module.Store(tmp_path / "cloud-compute.db")


def _close(obj) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - teardown must not mask test results
            pass


@pytest.fixture
def store(tmp_path):
    s = _make_store(tmp_path)
    if s is None:
        pytest.skip("store not installed")
    yield s
    _close(s)


@pytest.fixture
def services(tmp_path, fake_transports, fake_events, fake_secrets):
    from cloud_compute_core.testing import make_services

    kwargs = {"transports": fake_transports, "events": fake_events, "secrets": fake_secrets}
    s = _make_store(tmp_path)
    if s is not None:
        kwargs["store"] = s
    config_module = _optional("config")
    if config_module is not None:
        kwargs["config"] = config_module.Config()
    yield make_services(**kwargs)
    if s is not None:
        _close(s)
