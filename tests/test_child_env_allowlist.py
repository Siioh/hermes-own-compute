"""Local child processes get an allowlisted environment and nothing else.

Covers the allowlist itself (:func:`core.transport.child_env`), real child processes started
through each path the plugin uses, and a structural check that every process start in the
plugin's own code passes an environment built by the allowlist helper.
"""

from __future__ import annotations

import ast
import json
import os
import platform
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from core import ec2
from core import gitrelay
from core import terminal as term
from core import transport as tr
from core import tunnels as T
from core.models import HostSpec

sys.path.insert(0, str(Path(__file__).parent))
from test_integration_register import bg_calls  # noqa: E402,F401  (a fixture)

ROOT = Path(__file__).resolve().parents[1]

#: Variables that must never reach a child, whatever their names look like.
PLANTED = {
    "FAL_KEY": "fake-fal",                      # ws-scan:allow fake test value
    "OPENROUTER_KEY": "fake-openrouter",        # ws-scan:allow fake test value
    "OPENAI_KEY": "fake-openai",                # ws-scan:allow fake test value
    "AWS_ACCESS_KEY_ID": "fake-akid",           # ws-scan:allow fake test value
    "AWS_SECRET_ACCESS_KEY": "fake-aws",        # ws-scan:allow fake test value
    "GH_PAT": "fake-pat",                       # ws-scan:allow fake test value
    "DATABASE_URL": "postgres://u:fake@db/x",   # ws-scan:allow fake test value
    "HTTPS_PROXY": "http://u:fake@proxy:3128",  # ws-scan:allow fake test value
    "ANTHROPIC_AUTH": "fake-auth",              # ws-scan:allow fake test value
    "MY_CUSTOM_VAR": "anything",
    "CC_AGENT_API_KEY": "fake-cc",              # ws-scan:allow fake test value
    "HF_TOKEN": "fake-hf",                      # ws-scan:allow fake test value
}

#: The allowed names, spelled the way Windows and users mix case.
ALLOWED_MIXED = {
    "Path": "/usr/bin", "HOME": "/home/dev", "UserProfile": r"C:\Users\dev",
    "SystemRoot": r"C:\Windows", "temp": r"C:\Temp", "TMP": "/tmp", "Lang": "C.UTF-8",
    "LC_ALL": "C.UTF-8", "lc_messages": "C", "SSH_AUTH_SOCK": "/run/user/1000/agent.sock",
    "ssh_agent_pid": "4242",
}
TERMINAL_MIXED = {
    "DISPLAY": ":0", "Wayland_Display": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
}
OTHER = {"XAUTHORITY": "/run/user/1000/xauth_abc", "ProgramData": r"C:\ProgramData",
         "PATHEXT": ".EXE", "COMSPEC": r"C:\Windows\system32\cmd.exe", "SHELL": "/bin/bash",
         "LCX": "x", "MY_LC_ALL": "x", "XDG_CONFIG_HOME": "/home/dev/.config"}
FAKE = {**PLANTED, **ALLOWED_MIXED, **TERMINAL_MIXED, **OTHER}


@pytest.fixture(params=[True, False], ids=["windows", "posix"])
def windows(request, monkeypatch):
    monkeypatch.setattr(tr, "_is_windows", lambda: request.param)
    return request.param


# --------------------------------------------------------------------------- the allowlist


def _spelled(names, windows: bool) -> set[str]:
    """The names that match: any ASCII case on Windows, the upper-case spelling elsewhere."""
    return {n for n in names if windows or n == n.upper()}


def test_child_env_keeps_exactly_the_allowed_names(windows):
    out = tr.child_env(FAKE)
    expected = _spelled(ALLOWED_MIXED, windows) | ({"ProgramData"} if windows else set())
    assert set(out) == expected
    assert all(out[k] == FAKE[k] for k in out)                 # values unchanged
    assert "FAL_KEY" in FAKE                                   # the input is not modified


def test_terminal_children_also_get_the_desktop_session(windows):
    out = tr.child_env(FAKE, terminal=True)
    expected = (_spelled(ALLOWED_MIXED, windows) | _spelled(TERMINAL_MIXED, windows)
                | ({"ProgramData"} if windows else {"XAUTHORITY"}))
    assert set(out) == expected
    assert not set(TERMINAL_MIXED) & set(tr.child_env(FAKE))   # never for other children


#: Windows terminal children need the system drive and the profile folder; spelled in mixed case.
WINDOWS_TERMINAL = {"SystemDrive": "C:", "LocalAppData": r"C:\Users\dev\AppData\Local"}
#: Other folder and account variables that stay out of every child.
NOT_PASSED_ON_WINDOWS = {"APPDATA": r"C:\Users\dev\AppData\Roaming", "HOMEDRIVE": "C:",
                         "HOMEPATH": r"\Users\dev", "USERNAME": "dev", "WINDIR": r"C:\Windows",
                         "ALLUSERSPROFILE": r"C:\ProgramData", "PUBLIC": r"C:\Users\Public"}


def test_windows_terminal_children_get_the_system_drive_and_the_profile_folder(windows):
    # Windows Terminal loads its settings only when SYSTEMDRIVE is set; the browser keeps the
    # user's profile and running instance only when LOCALAPPDATA is set.
    env = {**WINDOWS_TERMINAL, **NOT_PASSED_ON_WINDOWS, "PATH": "/bin"}
    out = tr.child_env(env, terminal=True)
    assert set(out) == {"PATH"} | (set(WINDOWS_TERMINAL) if windows else set())
    assert all(out[k] == env[k] for k in out)
    assert tr.child_env(env) == {"PATH": "/bin"}                  # never for other children
    assert tr.child_env({}, extra=WINDOWS_TERMINAL) == {}
    assert set(tr.child_env({}, terminal=True, extra=WINDOWS_TERMINAL)) == \
        (set(WINDOWS_TERMINAL) if windows else set())


def test_the_windows_terminal_list_holds_those_two_names_only():
    assert tr.TERMINAL_ENV_NAMES_WINDOWS == {"SYSTEMDRIVE", "LOCALAPPDATA"}
    assert not tr.TERMINAL_ENV_NAMES_WINDOWS & (tr.CHILD_ENV_NAMES | tr.CHILD_ENV_NAMES_WINDOWS
                                                | tr.TERMINAL_ENV_NAMES)


def test_windows_children_get_the_program_folders_only(windows):
    # Docker Desktop's CLI finds its plugins (buildx) under %ProgramFiles%; OpenSSH for
    # Windows needs %ProgramData%. The other program-folder names are not passed.
    out = tr.child_env({"ProgramFiles": r"C:\Program Files", "ProgramData": r"C:\ProgramData",
                        "ProgramFiles(x86)": r"C:\Program Files (x86)",
                        "ProgramW6432": r"C:\Program Files"})
    assert set(out) == ({"ProgramFiles", "ProgramData"} if windows else set())


def test_lc_prefix_is_matched_as_a_prefix_only(windows):
    out = tr.child_env({"LC_ALL": "C", "LC_CTYPE": "C", "lc_time": "C", "LCX": "x",
                        "MY_LC_ALL": "x", "LC": "x", "LC_": "x"})
    assert set(out) == {"LC_ALL", "LC_CTYPE"} | ({"lc_time"} if windows else set())


#: Names that are not plain ASCII letters, digits and underscores: never passed. Several
#: upper-case to an allowed name through a non-ASCII letter (U+017F, U+0131).
_NOT_PLAIN = ["\u017fYSTEMROOT", "USERPROF\u0131LE", "SSH_AUTH_\u017fOCK", "D\u0131SPLAY",
              "H\u00d6ME", "LC_\u00c4LL", "", " PATH", "PATH ", "PA TH", "PATH=x", "LC_ALL=C",
              "PATH\x00", "PATH-1", "LC_X.Y"]


def test_only_plain_ascii_names_are_eligible(windows):
    env = {name: "x" for name in _NOT_PLAIN}
    assert tr.child_env(env, terminal=True) == {}
    assert tr.child_env({"PATH": "/bin"}, extra=env) == {"PATH": "/bin"}
    assert not [n for n in _NOT_PLAIN if tr.env_name_allowed(n, terminal=True)]


def test_posix_names_match_with_exact_case(monkeypatch):
    monkeypatch.setattr(tr, "_is_windows", lambda: False)
    wrong = ["path", "Path", "home", "Home", "lc_secret", "Lc_ALL", "lC_ALL", "ssh_auth_sock",
             "display", "xauthority", "Wayland_Display", "systemroot", "tmp"]
    assert tr.child_env({n: "x" for n in wrong}, terminal=True) == {}
    right = {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "LC_X": "x", "DISPLAY": ":0",
             "XAUTHORITY": "/run/user/1000/a"}
    assert tr.child_env(right, terminal=True) == right


def test_windows_names_ignore_ascii_case_and_keep_one_spelling(monkeypatch):
    monkeypatch.setattr(tr, "_is_windows", lambda: True)
    assert tr.child_env({"Path": "a", "SystemRoot": "b", "lc_time": "c"}) == \
        {"Path": "a", "SystemRoot": "b", "lc_time": "c"}
    # Several spellings of one name: the one that sorts first wins, whatever the order given.
    assert tr.child_env({"path": "1", "Path": "2", "PATH": "3"}) == {"PATH": "3"}
    assert tr.child_env({"PATH": "3", "path": "1", "Path": "2"}) == {"PATH": "3"}
    assert tr.child_env({"path": "1", "Path": "2"}) == {"Path": "2"}
    # A value set for the child replaces every inherited spelling.
    assert tr.child_env({"PATH": "3", "Path": "2"}, extra={"path": "new"}) == {"path": "new"}


def test_reads_os_environ_by_default(monkeypatch):
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    out = {k.upper() for k in tr.child_env()}
    assert not out & set(PLANTED) and "PATH" in out


def test_extra_values_pass_the_same_list(windows):
    out = tr.child_env({"PATH": "/usr/bin"}, terminal=True,
                       extra={"DISPLAY": ":1", "MY_CUSTOM_VAR": "x", "HTTPS_PROXY": "x",
                              "XAUTHORITY": "/run/user/1000/a"})
    assert out["DISPLAY"] == ":1" and "MY_CUSTOM_VAR" not in out and "HTTPS_PROXY" not in out
    assert ("XAUTHORITY" in out) is (not windows)
    base = tr.child_env({"PATH": "/usr/bin"}, extra={"DISPLAY": ":1"})
    assert base == {"PATH": "/usr/bin"}                        # not a terminal child


def test_extra_replaces_a_differently_spelled_name_on_windows(monkeypatch):
    monkeypatch.setattr(tr, "_is_windows", lambda: True)
    out = tr.child_env({"Path": r"C:\old"}, extra={"PATH": r"C:\new"})
    assert out == {"PATH": r"C:\new"}


def test_no_allowlisted_name_looks_like_a_secret():
    secret = re.compile(r"KEY|TOKEN|SECRET|PASSW|PAT$|AUTH$|CRED|PROXY|URL", re.IGNORECASE)
    names = (tr.CHILD_ENV_NAMES | tr.CHILD_ENV_NAMES_WINDOWS | tr.TERMINAL_ENV_NAMES
             | tr.TERMINAL_ENV_NAMES_POSIX | tr.TERMINAL_ENV_NAMES_WINDOWS)
    assert not [n for n in names if secret.search(n)]


def _ticked(text: str) -> set[str]:
    return set(re.findall(r"`([^`]+)`", text))


def test_the_readme_lists_exactly_the_names_in_the_code():
    """The README's Disclosures name every passed variable; the lists cannot drift apart."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    line = next(ln for ln in readme.splitlines()
                if ln.startswith("- Child processes on this computer get only these"))
    base = re.search(r"when set: (.*?), plus (.*?) on Windows", line)
    term_ = re.search(r"opener also get (.*?), and (`\w+`) on Linux and macOS, "
                      r"and (.*?) on Windows", line)
    git = re.search(r"`git` gets its own list: (.*?), plus the values the plugin sets: (.*?)\. ",
                    readme)
    assert base and term_ and git, "the README sentences changed shape; update this test"
    assert _ticked(base.group(1)) == tr.CHILD_ENV_NAMES | {"LC_*"}
    assert _ticked(base.group(2)) == tr.CHILD_ENV_NAMES_WINDOWS
    assert _ticked(term_.group(1)) == tr.TERMINAL_ENV_NAMES
    assert _ticked(term_.group(2)) == tr.TERMINAL_ENV_NAMES_POSIX
    assert _ticked(term_.group(3)) == tr.TERMINAL_ENV_NAMES_WINDOWS
    assert _ticked(git.group(1)) == gitrelay._ENV_ALLOWLIST
    fixed = {t.split("=")[0] for t in _ticked(git.group(2))}
    assert set(gitrelay._env({})) - gitrelay._ENV_ALLOWLIST <= fixed


def test_the_readme_lists_the_systemctl_child_and_the_asyncssh_rules():
    """The child-process list names the command that finds the desktop session."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    line = next(ln for ln in readme.splitlines()
                if ln.startswith("- Child processes on this computer: "))
    assert "`systemctl --user show-environment`" in line
    assert "show-environment" in (ROOT / "core" / "terminal.py").read_text(encoding="utf-8")
    limit = next(ln for ln in readme.splitlines() if "With `use_asyncssh` on" in ln)
    for word in ("ProxyCommand", "IdentitiesOnly", "IdentityAgent", "without an ssh program"):
        assert word.lower() in limit.lower()


def test_env_with_path_keeps_only_allowed_names(monkeypatch):
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    env = tr._env_with_path([os.path.join("x", "bin")])
    assert not {k.upper() for k in env} & set(PLANTED)
    key = next(k for k in env if k.upper() == "PATH")
    assert env[key].startswith(os.path.join("x", "bin"))


# --------------------------------------------------------------------------- real children

PROBE = "import json, os; print(json.dumps(sorted(os.environ)))"


@pytest.fixture
def planted(monkeypatch):
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    return set(PLANTED)


def _minimal_env() -> dict[str, str]:
    root = os.environ.get("SystemRoot")
    return {"SystemRoot": root} if os.name == "nt" and root else {}


def _injected(argv: list[str], parse) -> set[str]:
    """Names the OS or the interpreter adds to a child that was given a minimal environment."""
    env = _minimal_env()
    res = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60,
                         stdin=subprocess.DEVNULL)
    return {n.upper() for n in parse(res.stdout)} - {k.upper() for k in env}


def _check(names, injected: set[str], planted: set[str], *, terminal: bool = False) -> None:
    upper = {n.upper() for n in names}
    assert not upper & planted, upper & planted
    allowed = {k.upper() for k in tr.child_env(terminal=terminal)}
    assert upper - allowed <= injected, upper - allowed - injected


@pytest.fixture(scope="module")
def py_injected():
    # macOS adds __CF_USER_TEXT_ENCODING to every process; Windows and Linux add nothing.
    return _injected([sys.executable, "-c", PROBE], json.loads)


def test_run_process_child_sees_only_allowed_names(planted, py_injected):
    res = tr._run_process([sys.executable, "-c", PROBE], None, timeout=60)
    assert res.rc == 0, res.stderr
    _check(json.loads(res.stdout), py_injected, planted)


def test_run_process_filters_an_env_given_by_the_caller(planted, py_injected):
    res = tr._run_process([sys.executable, "-c", PROBE], None, timeout=60,
                          env={**os.environ, "EXTRA_FROM_CALLER": "x"})
    assert res.rc == 0, res.stderr
    assert "EXTRA_FROM_CALLER" not in json.loads(res.stdout)
    _check(json.loads(res.stdout), py_injected, planted)


def test_local_transport_run_child_sees_only_allowed_names(planted, py_injected):
    local = tr.LocalTransport(HostSpec(id="local", name="This computer", kind="local",
                                       address="localhost"))
    res = local.run([sys.executable, "-c", PROBE], timeout=60)
    assert res.rc == 0, res.stderr
    _check(json.loads(res.stdout), py_injected, planted)


def _env_names(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^([A-Za-z_][A-Za-z0-9_]*)=", text or "", re.M)]


@pytest.mark.skipif(tr._find_sh() is None, reason="no POSIX sh on this computer")
def test_local_run_script_child_sees_only_allowed_names(planted):
    sh = tr._find_sh()
    assert sh is not None
    # The shell adds names of its own (PWD, SHLVL; Git for Windows adds more).
    injected = _injected([sh, "-c", "env"], _env_names)
    local = tr.LocalTransport(HostSpec(id="local", name="This computer", kind="local",
                                       address="localhost"))
    res = local.run_script("env\n", timeout=60)
    assert res.rc == 0, res.stderr
    _check(_env_names(res.stdout), injected, planted)


def test_ssh_transport_child_sees_only_allowed_names(planted, py_injected, monkeypatch):
    # A Python stub in place of ssh: argv is [python, -c, PROBE, <address>, --, <command>].
    monkeypatch.setattr(tr, "ssh_base_args", lambda host, **kw: ["-c", PROBE])
    ssh = tr.SubprocessSSHTransport(HostSpec(id="h", name="h", kind="ssh", address="box"),
                                    sys.executable)
    res = ssh.run(["true"], timeout=60)
    assert res.rc == 0, res.stderr
    _check(json.loads(res.stdout), py_injected, planted)


def test_tunnel_child_sees_only_allowed_names(planted, py_injected, tmp_path):
    out = tmp_path / "names.json"
    code = f"import json, os; open({str(out)!r}, 'w').write(json.dumps(sorted(os.environ)))"
    child = T.TunnelManager(types.SimpleNamespace())._spawn([sys.executable, "-c", code])
    try:
        assert child.proc.wait(timeout=60) == 0
    finally:
        if child.stderr is not None:
            child.stderr.close()
    _check(json.loads(out.read_text()), py_injected, planted)


def test_ec2_keygen_child_sees_only_allowed_names(planted, py_injected, monkeypatch, tmp_path):
    real_run = subprocess.run
    seen: list[str] = []

    def stub_keygen(argv, **kw):                 # a real child, with the env the code passes
        res = real_run([sys.executable, "-c", PROBE], **kw)
        seen.append(res.stdout)
        Path(argv[argv.index("-f") + 1]).write_text("stub key")
        return res

    monkeypatch.setattr(ec2.shutil, "which", lambda name: "ssh-keygen-stub")
    monkeypatch.setattr(ec2.subprocess, "run", stub_keygen)
    ec2._ssh_keygen(tmp_path / "key", "cc-test")
    _check(json.loads(seen[0]), py_injected, planted)


class _Popen:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return types.SimpleNamespace(wait=lambda: 0)


def test_terminal_launch_env_drops_a_discovered_name_off_the_list(planted, monkeypatch):
    monkeypatch.setattr(tr, "_is_windows", lambda: False)
    popen = _Popen()
    term._spawn(popen, ["xterm"], windows=False,
                extra_env={"DISPLAY": ":0", "XAUTHORITY": "/run/user/1000/xauth_a",
                           "MY_CUSTOM_VAR": "x", "HTTPS_PROXY": "x"})
    env = popen.calls[0][1]["env"]
    assert env["DISPLAY"] == ":0" and env["XAUTHORITY"] == "/run/user/1000/xauth_a"
    assert not {k.upper() for k in env} & planted
    assert set(env) <= set(tr.child_env(terminal=True)) | {"DISPLAY", "XAUTHORITY"}


def test_systemctl_child_gets_the_terminal_env(planted):
    calls = []

    def runner(argv, **kw):
        calls.append(kw["env"])
        return types.SimpleNamespace(returncode=1, stdout="")

    term._systemd_user_env("/run/user/1000", runner)
    env = calls[0]
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert not {k.upper() for k in env} & planted


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_open_url_starts_the_opener_with_the_terminal_env(planted, platform):
    popen = _Popen()
    which = {"open": "/usr/bin/open", "xdg-open": "/usr/bin/xdg-open"}.get
    assert term.open_url("http://127.0.0.1:8080/t/vnc.html", which=which, popen=popen,
                         platform=platform)
    (argv, kw), = popen.calls
    assert argv[-1] == "http://127.0.0.1:8080/t/vnc.html"
    assert not {k.upper() for k in kw["env"]} & planted
    assert kw["env"] == tr.child_env(terminal=True)


def test_windows_opener_and_terminal_window_start_with_the_system_drive_and_profile_folder(
        planted, monkeypatch):
    monkeypatch.setattr(tr, "_is_windows", lambda: True)
    monkeypatch.setenv("SYSTEMDRIVE", "C:")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\dev\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\dev\AppData\Roaming")
    popen = _Popen()
    assert term.open_url("http://127.0.0.1:8080/t/vnc.html", popen=popen, platform="win32")
    term._spawn(popen, ["wt.exe", "new-tab"], windows=True)
    assert len(popen.calls) == 2
    for _argv, kw in popen.calls:
        env = {k.upper(): v for k, v in kw["env"].items()}
        assert env["SYSTEMDRIVE"] == "C:" and env["LOCALAPPDATA"] == r"C:\Users\dev\AppData\Local"
        assert "APPDATA" not in env and not set(env) & planted


@pytest.mark.parametrize("url", ["file:///etc/passwd", "-x", 'http://a" b', "http://a b", 5])
def test_open_url_refuses_anything_but_a_plain_web_address(url):
    popen = _Popen()
    assert term.open_url(url, which=lambda n: "/usr/bin/xdg-open", popen=popen,
                         platform="linux") is False
    assert not popen.calls


# --------------------------------------------------------------------------- hidden starts


def _allowlisted(env) -> bool:
    """True for an environment built by ``child_env`` or by the git helper's own list."""
    if env is None:
        return False
    if dict(env) == tr.child_env(dict(env), terminal=True):
        return True
    return all(n.upper() in gitrelay._ENV_ALLOWLIST or n.upper().startswith("GIT_") for n in env)


@pytest.fixture
def no_hidden_starts(monkeypatch):
    """Records every process start without an allowlisted environment, and refuses it.

    Covers ``subprocess`` (``run``, ``check_output`` and the rest go through ``Popen``),
    ``os.system``, ``os.popen`` and ``os.startfile``. The platform module's caches are
    cleared and WMI is made to fail, so a ``platform`` query takes the path that Python
    3.11 and older always take on Windows: ``cmd /c ver``.
    """
    bad: list[str] = []
    base = subprocess.Popen

    class Guard(base):                                   # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            if not _allowlisted(kw.get("env")):
                bad.append(f"Popen {str(args)[:80]}")
                raise OSError("process start refused by the test")
            super().__init__(args, *a, **kw)

    def refuse(name):
        def start(*a, **kw):
            bad.append(f"{name} {str(a)[:80]}")
            raise OSError("process start refused by the test")
        return start

    monkeypatch.setattr(subprocess, "Popen", Guard)
    for name in ("system", "popen", "startfile"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, refuse(f"os.{name}"))
    monkeypatch.setattr(platform, "_uname_cache", None, raising=False)

    def no_wmi(*a, **kw):
        raise OSError("WMI not available in this test")
    monkeypatch.setattr(platform, "_wmi_query", no_wmi, raising=False)
    return bad


def test_local_facts_start_no_process_without_the_allowlist(no_hidden_starts):
    from test_local_host import WIN_DOCKER, FakeLocal, docker_answers
    from cloud_compute_core import prober as pr
    from cloud_compute_core.testing import sample_local_host

    facts = pr._os_facts()
    pr._nvidia_smi()
    pr._engine_down_message(pr.LocalEngine(name="docker", path=WIN_DOCKER))
    pr.native_probe(FakeLocal(sample_local_host(), {"docker": WIN_DOCKER}, docker_answers()),
                    "auto")
    assert not no_hidden_starts, no_hidden_starts
    assert facts[0] in {"Windows", "macOS", "Linux"} or facts[0] == pr._system()


def test_local_facts_match_the_platform_module(monkeypatch):
    from cloud_compute_core import prober as pr
    monkeypatch.setattr(platform, "_uname_cache", None, raising=False)
    assert pr._system() == platform.system()
    assert pr._machine() == platform.machine()
    if os.name != "nt" or sys.version_info >= (3, 12):      # 3.11 names Windows 11 "10"
        assert pr._release() == platform.release()


@pytest.mark.parametrize("version, client, expected", [
    ((10, 0, 26100), 1, "11"), ((10, 0, 22631), 1, "11"), ((10, 0, 19045), 1, "10"),
    ((6, 3, 9600), 1, "8.1"), ((10, 0, 20348), 3, "10.0.20348"),
])
def test_windows_release_is_named_from_the_version(monkeypatch, version, client, expected):
    from cloud_compute_core import prober as pr
    monkeypatch.setattr(pr.os, "name", "nt")
    fake = types.SimpleNamespace(platform_version=version, product_type=client)
    monkeypatch.setattr(pr.sys, "getwindowsversion", lambda: fake, raising=False)
    assert pr._release() == expected


def test_register_starts_no_process_without_the_allowlist(no_hidden_starts, bg_calls):
    from test_integration_register import FakeCtx, _load_plugin
    _load_plugin().register(FakeCtx())
    assert not no_hidden_starts, no_hidden_starts


# --------------------------------------------------------------------------- structure

#: subprocess functions that start a process.
_SUBPROCESS_STARTS = {"run", "Popen", "call", "check_call", "check_output", "getoutput",
                      "getstatusoutput"}
#: Process starts that cannot take an environment at all.
_FORBIDDEN_OS = re.compile(r"^(system|popen|startfile|exec\w*|spawn\w*|posix_spawnp?|fork\w*)$")
#: Modules whose job is to start processes (browsers, worker processes, pseudo-terminals,
#: pagers); none of them takes an allowlisted environment.
_FORBIDDEN_IMPORTS = {"webbrowser", "multiprocessing", "pty", "pydoc"}
#: Other ways to start a process that cannot take an allowlisted environment.
_FORBIDDEN_CALLS = {"concurrent.futures.ProcessPoolExecutor",
                    "concurrent.futures.process.ProcessPoolExecutor"}
#: Win32 process starts reached through ctypes (``kernel32.CreateProcessW``,
#: ``shell32.ShellExecuteW`` and the like): they inherit the whole environment.
_FORBIDDEN_WIN32 = re.compile(r"^(ShellExecute\w*|CreateProcess\w*|WinExec)$")
#: Callables injected for tests that stand in for subprocess.Popen / subprocess.run.
_INJECTED_STARTERS = {"popen", "_popen", "runner"}
#: Standard-library functions that start a child process themselves, with the full
#: environment, on some supported platform or Python version. The plugin must not call them.
_HIDDEN_STARTS = {
    # Windows: uname() -> win32_ver() -> `cmd /c ver` (shell, no env) on Python 3.11 and
    # older, and on newer ones when WMI does not answer. node() and the rest call uname().
    **{f"platform.{n}": "cmd /c ver on Windows" for n in (
        "system", "uname", "release", "version", "machine", "node", "platform", "win32_ver")},
    # POSIX: `uname -p` (uname().processor is computed by a child process).
    "platform.processor": "uname -p on POSIX, cmd /c ver on Windows",
    # POSIX: runs `file -b <executable>`.
    "platform.architecture": "file -b on POSIX",
    # POSIX: may run ifconfig, ip, arp, netstat or lanscan to find a MAC address.
    "uuid.getnode": "ifconfig/ip/arp on POSIX", "uuid.uuid1": "ifconfig/ip/arp on POSIX",
    # Linux and BSD: runs ldconfig, gcc, ld or objdump.
    "ctypes.util.find_library": "ldconfig/gcc on POSIX",
}


def _import_names(tree: ast.AST) -> dict[str, str]:
    """Local names bound by imports, mapped to what they stand for (``sp`` -> ``subprocess``,
    ``run`` -> ``subprocess.run``)."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    names[a.asname] = a.name
                else:
                    head = a.name.split(".")[0]
                    names[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                names[a.asname or a.name] = f"{node.module}.{a.name}"
    return names


def _qualified(func: ast.AST, names: dict[str, str]) -> str:
    """``module.attr`` that a called expression stands for, with import aliases resolved."""
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return ""
    parts.append(names.get(func.id, func.id))
    return ".".join(reversed(parts))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_start(call: ast.Call, names: dict[str, str]) -> bool:
    """A process start that takes ``env=``: ``subprocess`` (also under another name),
    ``asyncio.create_subprocess_*``, an event loop's ``subprocess_exec``/``_shell``, or an
    injected starter."""
    name = _qualified(call.func, names)
    module, _, attr = name.rpartition(".")
    if module == "subprocess" and attr in _SUBPROCESS_STARTS:
        return True
    if module in ("asyncio", "asyncio.subprocess") and attr.startswith("create_subprocess"):
        return True
    if isinstance(call.func, ast.Attribute) and call.func.attr in ("subprocess_exec",
                                                                 "subprocess_shell"):
        return True
    return _call_name(call.func) in _INJECTED_STARTERS


def _helper_call(node: ast.AST | None, helpers: set[str]) -> bool:
    return isinstance(node, ast.Call) and _call_name(node.func) in helpers


def _assignments(fn: ast.AST, name: str) -> list[ast.AST | None]:
    """Values bound to ``name`` in ``fn`` (``None`` for a binding that is not a plain value)."""
    values: list[ast.AST | None] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    values.append(node.value)
                elif any(isinstance(t, ast.Name) and t.id == name for t in ast.walk(target)):
                    values.append(None)        # tuple unpacking: origin unknown
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and \
                isinstance(node.target, ast.Name) and node.target.id == name:
            values.append(getattr(node, "value", None) if isinstance(node, ast.AnnAssign) else None)
        elif isinstance(node, ast.arg) and node.arg == name:
            values.append(None)                # a parameter: origin unknown
    return values


def _mutated(fn: ast.AST, name: str) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for t in targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                        and t.value.id == name:
                    return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == name \
                and node.func.attr in {"update", "setdefault", "pop", "popitem", "clear", "__setitem__"}:
            return True
    return False


def _env_ok(value: ast.AST | None, fn: ast.AST, helpers: set[str]) -> bool:
    if _helper_call(value, helpers):
        return True
    if isinstance(value, ast.Name):
        bound = _assignments(fn, value.id)
        return bool(bound) and all(_helper_call(v, helpers) for v in bound) \
            and not _mutated(fn, value.id)
    return False


def _env_of_kwargs(name: str, fn: ast.AST) -> list[ast.AST | None]:
    """The ``env`` entries of a ``**kwargs`` dict built in ``fn``."""
    found: list[ast.AST | None] = []
    for value in _assignments(fn, name):
        if isinstance(value, ast.Dict):
            for k, v in zip(value.keys, value.values):
                if isinstance(k, ast.Constant) and k.value == "env":
                    found.append(v)
        elif isinstance(value, ast.Call) and _call_name(value.func) == "dict":
            found += [kw.value for kw in value.keywords if kw.arg == "env"]
    for node in ast.walk(fn):                  # kwargs["env"] = ...
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                        and t.value.id == name and isinstance(t.slice, ast.Constant) \
                        and t.slice.value == "env":
                    found.append(None)
    return found


def scan_source(text: str, filename: str) -> tuple[list[str], int]:
    """Problems found in one module, and the number of process starts seen."""
    tree = ast.parse(text, filename)
    helpers = {"child_env"} | ({"_env"} if Path(filename).name == "gitrelay.py" else set())
    problems: list[str] = []
    starts = 0
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    names = _import_names(tree)
    for node in ast.walk(tree):
        where = f"{filename}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Call) and _qualified(node.func, names) in _HIDDEN_STARTS:
            name = _qualified(node.func, names)
            problems.append(f"{where}: {name}() starts a process itself "
                            f"({_HIDDEN_STARTS[name]})")
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(m.split(".")[0] in _FORBIDDEN_IMPORTS for m in mods):
                problems.append(f"{where}: imports {mods}, which starts processes without env")
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess" and \
                    any(a.name in _SUBPROCESS_STARTS for a in node.names):
                problems.append(f"{where}: import subprocess functions by module name")
            if isinstance(node, ast.ImportFrom) and any(
                    f"{node.module}.{a.name}" in _FORBIDDEN_CALLS for a in node.names):
                problems.append(f"{where}: imports a process pool")
        if isinstance(node, ast.Attribute) and _FORBIDDEN_WIN32.match(node.attr):
            problems.append(f"{where}: {node.attr} starts a process with the whole environment")
        if not isinstance(node, ast.Call):
            continue
        name = _qualified(node.func, names)
        module, _, attr = name.rpartition(".")
        if module == "os" and _FORBIDDEN_OS.match(attr):
            problems.append(f"{where}: os.{attr} cannot take an allowlisted environment")
            continue
        if name in _FORBIDDEN_CALLS:
            problems.append(f"{where}: {name} starts processes without an allowlisted env")
            continue
        if not _is_start(node, names):
            continue
        starts += 1
        fn: ast.AST = node
        while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = parents[fn]
        envs = [kw.value for kw in node.keywords if kw.arg == "env"]
        for kw in node.keywords:
            if kw.arg is None and isinstance(kw.value, ast.Name):
                envs += _env_of_kwargs(kw.value.id, fn)
        if not envs:
            problems.append(f"{where}: process started without env=")
        elif not all(_env_ok(v, fn, helpers) for v in envs):
            problems.append(f"{where}: env= is not built by {sorted(helpers)}")
    return problems, starts


def _plugin_sources() -> list[Path]:
    return [p for p in ROOT.rglob("*.py")
            if not any(part.startswith(".") or part in {"tests", "node_modules", "__pycache__"}
                       for part in p.relative_to(ROOT).parts)]


def test_every_process_start_uses_the_allowlisted_env():
    """Every process start in the plugin's own code passes ``env=`` built by ``child_env``.

    Seen: ``subprocess`` calls (also as ``import subprocess as sp`` or ``from subprocess import
    run``), ``asyncio.create_subprocess_*``, an event loop's ``subprocess_exec``/``_shell``, the
    injected starters (``popen``, ``_popen``, ``runner``), ``os.system``/``popen``/``startfile``/
    ``exec*``/``spawn*``/``posix_spawn*``/``fork*`` (also imported by name or under another module
    name), the modules ``webbrowser``, ``multiprocessing``, ``pty`` and ``pydoc``, a
    ``ProcessPoolExecutor``, Win32 ``ShellExecute*``/``CreateProcess*``/``WinExec`` through ctypes,
    and the standard-library calls in ``_HIDDEN_STARTS`` that start a process themselves.

    Not seen, so this is a guard and not a proof: a starter reached through ``getattr``, a
    variable, a callback passed without being called here, or a renamed injected parameter; a
    local function that happens to be called ``child_env``; process starts inside third-party
    libraries (asyncssh, for example) and inside standard-library functions not listed above;
    and code outside the plugin's ``.py`` files.
    """
    problems: list[str] = []
    starts = 0
    for path in _plugin_sources():
        found, n = scan_source(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
        problems += found
        starts += n
    assert not problems, "\n".join(problems)
    # transport (3), terminal (2), tunnels (4), ec2 (1), gitrelay (1): the scan is not empty.
    assert starts >= 11, starts


@pytest.mark.parametrize("snippet", [
    "import subprocess\ndef f():\n    subprocess.run(['x'])\n",
    "import subprocess, os\ndef f():\n    subprocess.Popen(['x'], env=dict(os.environ))\n",
    "import subprocess\ndef f(env):\n    subprocess.run(['x'], env=env)\n",
    "import subprocess\ndef f():\n    env = child_env()\n    env['K'] = 'v'\n"
    "    subprocess.run(['x'], env=env)\n",
    "import subprocess, os\ndef f(popen):\n    kw = {'env': os.environ}\n    popen(['x'], **kw)\n",
    "import os\ndef f():\n    os.system('x')\n",
    "import os\ndef f():\n    os.startfile('x')\n",
    "import webbrowser\n",
    "from subprocess import run\n",
    "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('x')\n",
    "import subprocess as sp\ndef f():\n    sp.run(['x'])\n",
    "from subprocess import Popen as P\ndef f():\n    P(['x'])\n",
    "from os import system\ndef f():\n    system('x')\n",
    "from os import startfile as sf\ndef f():\n    sf('x')\n",
    "import os as o\ndef f():\n    o.popen('x')\n",
    "import os\ndef f():\n    os.spawnl(0, 'x')\n",
    "import os\ndef f():\n    os.execv('x', ['x'])\n",
    "import os\ndef f():\n    os.posix_spawn('x', ['x'], {})\n",
    "import asyncio.subprocess\nasync def f():\n"
    "    await asyncio.subprocess.create_subprocess_shell('x')\n",
    "async def f(loop, proto):\n    await loop.subprocess_exec(proto, 'x')\n",
    "import pty\n",
    "import pydoc\n",
    "import multiprocessing\n",
    "from concurrent.futures import ProcessPoolExecutor\n",
    "import concurrent.futures\ndef f():\n    concurrent.futures.ProcessPoolExecutor()\n",
    "import ctypes\ndef f():\n    ctypes.windll.shell32.ShellExecuteW(None, 'open', 'x', None, None, 1)\n",
    "import ctypes\ndef f(k32):\n    k32.CreateProcessW(None, 'x')\n",
])
def test_the_scan_catches_a_start_without_the_helper(snippet):
    problems, _ = scan_source(snippet, "core/example.py")
    assert problems


@pytest.mark.parametrize("snippet", [
    "import platform\ndef f():\n    return platform.system()\n",
    "import platform as p\ndef f():\n    return p.uname().machine\n",
    "from platform import release as r\ndef f():\n    return r()\n",
    "import platform\ndef f():\n    return platform.processor()\n",
    "import uuid\ndef f():\n    return uuid.getnode()\n",
    "from uuid import uuid1\ndef f():\n    return uuid1()\n",
    "import ctypes.util\ndef f():\n    return ctypes.util.find_library('c')\n",
])
def test_the_scan_refuses_stdlib_calls_that_start_a_process(snippet):
    problems, _ = scan_source(snippet, "core/example.py")
    assert problems and "starts a process itself" in problems[0]


def test_the_scan_allows_the_process_free_stdlib_calls():
    ok = ("import platform, uuid, os, sys\n"
          "def f():\n    return (platform.mac_ver(), platform.freedesktop_os_release(),\n"
          "            uuid.uuid4(), os.uname(), sys.getwindowsversion())\n")
    assert scan_source(ok, "core/example.py") == ([], 0)


def test_the_scan_accepts_the_helper_forms():
    ok = ("import subprocess\n"
          "def a():\n    subprocess.run(['x'], env=child_env())\n"
          "def b(popen, extra):\n    env = child_env(terminal=True, extra=extra)\n"
          "    kwargs = dict(stdin=None, env=env)\n    popen(['x'], **kwargs)\n"
          "def c(self):\n    kw = {'stdin': None, 'env': child_env()}\n    self._popen(['x'], **kw)\n")
    assert scan_source(ok, "core/example.py") == ([], 3)
    aliased = ("import subprocess as sp\nfrom subprocess import DEVNULL\n"
               "def a():\n    sp.run(['x'], env=child_env(), stdin=DEVNULL)\n"
               "async def b(loop, p):\n    await loop.subprocess_exec(p, 'x', env=child_env())\n")
    assert scan_source(aliased, "core/example.py") == ([], 2)
    assert scan_source("import subprocess\ndef g():\n    subprocess.run(['git'], env=_env())\n",
                       "core/gitrelay.py") == ([], 1)
    assert scan_source("import subprocess\ndef g():\n    subprocess.run(['git'], env=_env())\n",
                       "core/other.py")[0]
