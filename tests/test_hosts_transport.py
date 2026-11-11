"""Unit tests for core/transport.py (no network, no real subprocess)."""

from __future__ import annotations

import ntpath
import os
import subprocess
import sys
import time
import types

import pytest

from cloud_compute_core import transport as tr
from cloud_compute_core.config import Config
from cloud_compute_core.interfaces import Services
from cloud_compute_core.models import ExecResult, HostSpec

SSH = "C:/ssh/ssh.exe"
BASE = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
        "-o", "StrictHostKeyChecking=accept-new"]
SLURM_PREFIX = ["podman", "exec", "-i", "-w", "/data", "slurmctld"]


def host(**kw) -> HostSpec:
    data = dict(id="x1", name="x1", kind="ssh", address="x1carbon")
    data.update(kw)
    return HostSpec(**data)


class _FakeStdin:
    def __init__(self, proc):
        self.proc = proc
        self.buf = b""

    def write(self, data):
        self.buf += data

    def flush(self):
        pass

    def close(self):
        self.proc.inputs.append(self.buf)


class FakeProc:
    """Stands in for ``subprocess.Popen``: pre-filled pipes, ``wait`` and ``kill``."""

    pid = None   # never a real pid: the tree kill must not touch anything

    def __init__(self, argv, rc=0, out=b"", err=b"", hang=False, stdin=None):
        import io

        self.argv = argv
        self.returncode = rc
        self.stdout, self.stderr = io.BytesIO(out), io.BytesIO(err)
        self.stdin = _FakeStdin(self) if stdin == subprocess.PIPE else None
        self.hang = hang
        self.killed = False
        self.inputs = []

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            time.sleep(min(timeout or 0, 0.05))
            raise subprocess.TimeoutExpired(self.argv, timeout)
        if self.killed:
            self.returncode = -9
        return self.returncode

    def kill(self):
        self.killed = True


@pytest.fixture
def popen(monkeypatch):
    """Replace subprocess.Popen in the transport module; records each call."""
    state = types.SimpleNamespace(calls=[], procs=[], result=dict(rc=0, out=b"ok\n", err=b""),
                                  raise_exc=None, by_argv=None)

    def fake(argv, **kwargs):
        if state.raise_exc is not None:
            raise state.raise_exc
        state.calls.append((list(argv), kwargs))
        res = state.by_argv(argv) if state.by_argv else state.result
        proc = FakeProc(argv, stdin=kwargs.get("stdin"), **res)
        state.procs.append(proc)
        return proc

    monkeypatch.setattr(tr.subprocess, "Popen", fake)
    return state


def no_control_master(argv):
    joined = " ".join(argv)
    assert "ControlMaster" not in joined and "ControlPath" not in joined and "ControlPersist" not in joined


# --------------------------------------------------------------------------- argv


def test_ssh_base_args_minimal():
    assert tr.ssh_base_args(host()) == BASE


def test_only_the_configured_key_is_offered_when_an_identity_file_is_set():
    """Without IdentitiesOnly ssh also tried the agent's keys."""
    with_key = tr.ssh_base_args(host(identity_file="C:/keys/id"))
    i = with_key.index("-i")
    assert with_key[i:i + 4] == ["-i", "C:/keys/id", "-o", "IdentitiesOnly=yes"]
    assert "IdentitiesOnly=yes" not in tr.ssh_base_args(host())        # unchanged without one
    assert "IdentitiesOnly=yes" in tr.ssh_base_args(host(identity_file="C:/keys/id"), batch=False)
    t = tr.SubprocessSSHTransport(host(identity_file="C:/keys/id"), "ssh")
    assert "IdentitiesOnly=yes" in t.local_argv(["bash", "-l"], tty=True)
    assert "IdentitiesOnly=yes" in t.local_argv(None, forwards=["127.0.0.1:5000:localhost:80"])
    assert "IdentitiesOnly=yes" not in tr.SubprocessSSHTransport(host(), "ssh").local_argv(
        ["bash", "-l"], tty=True)


def test_ssh_base_args_all_options():
    h = host(port=2222, identity_file=r"C:\Users\me\.ssh\id key", jump_host="bastion.example",
             user="alice")
    assert tr.ssh_base_args(h) == BASE + ["-p", "2222", "-i", r"C:\Users\me\.ssh\id key",
                                          "-o", "IdentitiesOnly=yes",
                                          "-J", "bastion.example", "-l", "alice"]
    assert tr.ssh_base_args(h, batch=False)[:2] == ["-o", "ConnectTimeout=15"]


def test_run_exact_argv_and_popen_kwargs(popen):
    t = tr.SubprocessSSHTransport(host(user="dev"), SSH)
    res = t.run(["echo", "hello world", "$HOME"], timeout=7)
    argv, kwargs = popen.calls[0]
    assert argv == [SSH, *BASE, "-l", "dev", "x1carbon", "--", "echo 'hello world' '$HOME'"]
    no_control_master(argv)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.PIPE
    assert "shell" not in kwargs
    if os.name == "nt":
        assert kwargs["creationflags"] == 0x08000000
    else:
        assert "creationflags" not in kwargs
    assert isinstance(res, ExecResult)
    assert (res.rc, res.stdout, res.stderr, res.timed_out) == (0, "ok\n", "", False)
    assert res.duration_s >= 0


def test_creationflags_only_on_windows(popen, monkeypatch):
    monkeypatch.setattr(tr, "_is_windows", lambda: False)
    tr.SubprocessSSHTransport(host(), SSH).run(["true"])
    assert "creationflags" not in popen.calls[0][1]
    monkeypatch.setattr(tr, "_is_windows", lambda: True)
    tr.SubprocessSSHTransport(host(), SSH).run(["true"])
    assert popen.calls[1][1]["creationflags"] == 0x08000000


def test_stdin_passed_through_pipe(popen):
    t = tr.SubprocessSSHTransport(host(), SSH)
    t.run(["cat"], stdin=b"data\x00bytes")
    assert popen.calls[0][1]["stdin"] == subprocess.PIPE
    assert popen.procs[0].inputs[0] == b"data\x00bytes"


def test_run_script_uses_sh_s(popen):
    t = tr.SubprocessSSHTransport(host(), SSH)
    t.run_script("echo hi\n")
    assert popen.calls[0][0][-2:] == ["--", "sh -s"]
    assert popen.procs[0].inputs[0] == b"echo hi\n"


def test_nonzero_rc_and_utf8_replace(popen):
    popen.result = dict(rc=3, out=b"caf\xc3\xa9 \xff", err=b"bad\n")
    res = tr.SubprocessSSHTransport(host(), SSH).run(["false"])
    assert res.rc == 3 and res.stdout == "caf\u00e9 \ufffd" and res.stderr == "bad\n"


def test_timeout_kills_and_returns_124(popen):
    popen.result = dict(rc=0, out=b"partial", err=b"", hang=True)
    res = tr.SubprocessSSHTransport(host(), SSH).run(["sleep", "99"], timeout=0.5)
    assert popen.procs[0].killed
    assert res.rc == 124 and res.timed_out is True
    assert res.stdout == "partial" and "timed out" in res.stderr


@pytest.mark.parametrize("exc", [FileNotFoundError(2, "No such file"), PermissionError(13, "denied"),
                                 OSError(8, "exec format")])
def test_missing_binary_rc_127(popen, exc):
    popen.raise_exc = exc
    res = tr.SubprocessSSHTransport(host(), "C:/nope/ssh.exe").run(["true"])
    assert res.rc == 127 and "could not start C:/nope/ssh.exe" in res.stderr and not res.timed_out


@pytest.mark.parametrize("field,value", [
    ("address", "-oProxyCommand=calc"), ("address", "x1 carbon"), ("address", "a;b"), ("address", ""),
    ("user", "-l"), ("user", "bob smith"), ("jump_host", "-J"), ("jump_host", "h$(x)"),
    ("identity_file", "-i"), ("identity_file", "C:\\key\nfile"), ("identity_file", "a\0b"),
    ("identity_file", "  "), ("port", 0), ("port", 70000),
])
def test_invalid_host_fields_raise_before_spawn(popen, field, value):
    with pytest.raises(ValueError):
        t = tr.SubprocessSSHTransport(host(), SSH)
        t.host = host(**{field: value})
        t.run(["true"])
    assert popen.calls == []


def test_identity_file_windows_path_allowed(popen):
    path = r"C:\Users\Some One\.ssh\id_ed25519"
    tr.SubprocessSSHTransport(host(identity_file=path), SSH).run(["true"])
    argv = popen.calls[0][0]
    assert argv[argv.index("-i") + 1] == path


def test_empty_argv_rejected(popen):
    with pytest.raises(ValueError):
        tr.SubprocessSSHTransport(host(), SSH).run([])


# --------------------------------------------------------------------------- prefix


def test_prefixed_slurm_endpoint(popen):
    h = host(kind="slurm", exec_prefix=SLURM_PREFIX)
    t = tr.TransportFactory(Config(ssh_path="")).for_host(h)
    t.inner.ssh_bin = SSH
    assert isinstance(t, tr.PrefixedTransport) and t.host is h
    t.run(["sinfo", "-h"])
    argv = popen.calls[0][0]
    assert argv[-3:] == ["x1carbon", "--", "podman exec -i -w /data slurmctld sinfo -h"]
    no_control_master(argv)


def test_prefixed_run_script_and_delegation(popen):
    inner = tr.SubprocessSSHTransport(host(), SSH)
    t = tr.PrefixedTransport(inner, SLURM_PREFIX)
    t.run_script("squeue\n")
    assert popen.calls[0][0][-1] == "podman exec -i -w /data slurmctld sh -s"
    assert popen.procs[0].inputs[0] == b"squeue\n"
    inner.tcp_check = lambda timeout=3.0: 12.5
    assert t.tcp_check() == 12.5


def test_prefixed_local_argv_uses_interactive_prefix_for_tty():
    h = host(exec_prefix=SLURM_PREFIX, interactive_prefix=["podman", "exec", "-it", "slurmctld"])
    t = tr.PrefixedTransport(tr.SubprocessSSHTransport(h, SSH), h.exec_prefix)
    tty = t.local_argv(["bash"], tty=True)
    assert tty[-2:] == ["--", "podman exec -it slurmctld bash"] and "-t" in tty
    plain = t.local_argv(["sinfo"])
    assert plain[-1] == "podman exec -i -w /data slurmctld sinfo"
    # tty without interactive_prefix falls back to exec_prefix
    h2 = host(exec_prefix=SLURM_PREFIX)
    t2 = tr.PrefixedTransport(tr.SubprocessSSHTransport(h2, SSH), h2.exec_prefix)
    assert t2.local_argv(["bash"], tty=True)[-1] == "podman exec -i -w /data slurmctld bash"
    # a pure tunnel is not prefixed
    tunnel = t.local_argv(None, forwards=["127.0.0.1:8080:localhost:80"])
    assert tunnel[-1] == "x1carbon" and "-N" in tunnel


def test_prefixed_close_delegates(fake_transport):
    t = tr.PrefixedTransport(fake_transport, ["x"])
    t.close()
    assert fake_transport.closed


# --------------------------------------------------------------------------- local_argv (ssh)


def test_local_argv_tty_drops_batchmode():
    t = tr.SubprocessSSHTransport(host(user="u"), SSH)
    argv = t.local_argv(["htop"], tty=True)
    assert argv == [SSH, "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
                    "-o", "StrictHostKeyChecking=accept-new", "-l", "u", "-t", "x1carbon", "--", "htop"]
    assert "BatchMode=yes" not in argv
    no_control_master(argv)


def test_local_argv_interactive_login_shell():
    argv = tr.SubprocessSSHTransport(host(), SSH).local_argv(None, tty=True)
    assert argv[-2:] == ["-t", "x1carbon"]


def test_local_argv_tunnel_only():
    t = tr.SubprocessSSHTransport(host(), SSH)
    argv = t.local_argv(None, forwards=["127.0.0.1:46001:localhost:6080", "127.0.0.1:8888:10.0.0.5:8888"])
    assert argv == [SSH, *BASE, "-N", "-L", "127.0.0.1:46001:localhost:6080",
                    "-L", "127.0.0.1:8888:10.0.0.5:8888", "x1carbon"]


def test_local_argv_forward_with_command_has_no_N():
    argv = tr.SubprocessSSHTransport(host(), SSH).local_argv(["sleep", "60"],
                                                             forwards=["127.0.0.1:1:h:2"])
    assert "-N" not in argv and argv[-2:] == ["--", "sleep 60"]


@pytest.mark.parametrize("spec", ["0.0.0.0:80:h:80", "8080:localhost:80", "*:80:h:80",
                                  "127.0.0.1:0:h:80", "127.0.0.1:80:h:99999", "127.0.0.1:80:h;x:80",
                                  "localhost:80:h:80", "-oX:1:h:2"])
def test_local_argv_rejects_non_loopback_forwards(spec):
    with pytest.raises(ValueError):
        tr.SubprocessSSHTransport(host(), SSH).local_argv(None, forwards=[spec])


# --------------------------------------------------------------------------- local transport


def test_local_transport_runs_argv_directly(popen, monkeypatch):
    monkeypatch.setattr(tr, "find_runtime", lambda name, explicit="": None)   # not installed here
    t = tr.LocalTransport(host(kind="local", address="localhost"))
    t.run(["podman", "ps"], stdin=b"x")
    argv, kwargs = popen.calls[0]
    assert argv == ["podman", "ps"] and "shell" not in kwargs and kwargs["stdin"] == subprocess.PIPE
    assert t.tcp_check() == 0.0


def test_local_run_script_uses_found_sh(popen, monkeypatch):
    monkeypatch.setattr(tr, "_find_sh", lambda: "C:/Git/usr/bin/sh.exe")
    tr.LocalTransport(host(kind="local", address="localhost")).run_script("uname\n")
    assert popen.calls[0][0] == ["C:/Git/usr/bin/sh.exe", "-s"]
    assert popen.procs[0].inputs[0] == b"uname\n"


def test_local_run_script_without_sh(popen, monkeypatch):
    monkeypatch.setattr(tr, "_find_sh", lambda: None)
    res = tr.LocalTransport(host(kind="local", address="localhost")).run_script("x")
    assert res.rc == 127 and "sh" in res.stderr and popen.calls == []


def test_find_sh_prefers_git_usr_bin(monkeypatch):
    monkeypatch.setattr(tr, "_is_windows", lambda: True)
    monkeypatch.setattr(tr.shutil, "which", lambda name: r"D:\Tools\Git\cmd\git.exe" if name == "git" else None)
    want = ntpath.join(r"D:\Tools\Git", "usr", "bin", "sh.exe")
    monkeypatch.setattr(tr.os.path, "exists", lambda p: ntpath.normcase(str(p)) == ntpath.normcase(want))
    monkeypatch.setattr(tr.Path, "resolve", lambda self, strict=False: self)
    assert ntpath.normcase(tr._find_sh()) == ntpath.normcase(want)


def test_local_argv_local_transport():
    t = tr.LocalTransport(host(kind="local", address="localhost"))
    assert t.local_argv(["python", "-V"], tty=True) == ["python", "-V"]
    assert len(t.local_argv(None)) == 1
    with pytest.raises(ValueError):
        t.local_argv(["x"], forwards=["127.0.0.1:1:h:2"])


# --------------------------------------------------------------------------- resolve_ssh_binary


@pytest.fixture
def fs(monkeypatch):
    """Fake filesystem for resolve_ssh_binary: set ``fs.existing`` and ``fs.which``."""
    state = types.SimpleNamespace(existing=set(), which={})
    norm = lambda p: ntpath.normcase(ntpath.normpath(str(p)))  # noqa: E731   (Windows paths on any OS)
    monkeypatch.setattr(tr.os.path, "exists", lambda p: norm(p) in {norm(e) for e in state.existing})
    monkeypatch.setattr(tr.shutil, "which", lambda name: state.which.get(name))
    monkeypatch.setattr(tr.Path, "resolve", lambda self, strict=False: self)
    monkeypatch.setattr(tr, "_is_windows", lambda: True)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(env, raising=False)
    return state


SYS_SSH = ntpath.join(r"C:\Windows", "System32", "OpenSSH", "ssh.exe")


def test_resolve_config_path_first(fs):
    fs.existing = {r"E:\ssh\ssh.exe", SYS_SSH}
    assert tr.resolve_ssh_binary(Config(ssh_path=r"E:\ssh\ssh.exe")) == r"E:\ssh\ssh.exe"


def test_resolve_missing_config_path_falls_through_to_system(fs):
    fs.existing = {SYS_SSH}
    assert tr.resolve_ssh_binary(Config(ssh_path=r"E:\missing\ssh.exe")) == SYS_SSH


def test_resolve_git_relative_to_which_git(fs):
    fs.which = {"git": r"D:\Tools\Git\cmd\git.exe", "ssh": r"C:\other\ssh.exe"}
    want = ntpath.join(r"D:\Tools\Git", "usr", "bin", "ssh.exe")
    fs.existing = {want}
    assert ntpath.normcase(tr.resolve_ssh_binary(Config())) == ntpath.normcase(want)


def test_resolve_git_program_files(fs, monkeypatch):
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    want = ntpath.join(r"C:\Program Files", "Git", "usr", "bin", "ssh.exe")
    fs.existing = {want}
    assert ntpath.normcase(tr.resolve_ssh_binary(Config())) == ntpath.normcase(want)


def test_resolve_which_then_bare(fs):
    fs.which = {"ssh": "/usr/bin/ssh"}
    assert tr.resolve_ssh_binary(Config()) == "/usr/bin/ssh"
    fs.which = {}
    assert tr.resolve_ssh_binary(None) == "ssh"


def test_resolve_non_windows_skips_windows_paths(fs, monkeypatch):
    monkeypatch.setattr(tr, "_is_windows", lambda: False)
    fs.existing = {SYS_SSH}
    fs.which = {"ssh": "/usr/bin/ssh"}
    assert tr.resolve_ssh_binary(Config()) == "/usr/bin/ssh"


# --------------------------------------------------------------------------- tcp_check / ssh -G


SSH_G = b"user dev\nhostname gpu-box.example.net\nport 22\nproxyjump none\n"


def test_ssh_g_parse():
    cfg = tr._parse_ssh_g(SSH_G.decode() + "hostname second\n")
    assert cfg["hostname"] == "gpu-box.example.net" and cfg["port"] == "22"


def test_split_jump():
    assert tr._split_jump("alice@bastion:2200,other") == ("bastion", 2200)
    assert tr._split_jump("bastion") == ("bastion", None)
    assert tr._split_jump("u@[::1]:22") == ("::1", 22)


class FakeSocket:
    def close(self):
        pass


def test_tcp_check_resolves_alias_once(popen, monkeypatch):
    popen.result = dict(rc=0, out=SSH_G, err=b"")
    targets = []
    monkeypatch.setattr(tr.socket, "create_connection",
                        lambda addr, timeout=None: targets.append((addr, timeout)) or FakeSocket())
    t = tr.SubprocessSSHTransport(host(), SSH)
    ms = t.tcp_check(2.0)
    t.tcp_check(2.0)
    assert isinstance(ms, float) and ms >= 0
    assert [c[0] for c in popen.calls] == [[SSH, "-G", "x1carbon"]]   # cached after the first call
    assert targets == [(("gpu-box.example.net", 22), 2.0)] * 2


def test_tcp_check_explicit_port_wins_and_g_port_used(popen, monkeypatch):
    popen.result = dict(rc=0, out=b"hostname h.example\nport 2022\n", err=b"")
    targets = []
    monkeypatch.setattr(tr.socket, "create_connection", lambda addr, timeout=None: targets.append(addr) or FakeSocket())
    tr.SubprocessSSHTransport(host(), SSH).tcp_check()
    tr.SubprocessSSHTransport(host(port=2200), SSH).tcp_check()
    assert targets == [("h.example", 2022), ("h.example", 2200)]


def test_tcp_check_ssh_g_failure_falls_back(popen, monkeypatch):
    popen.raise_exc = FileNotFoundError(2, "no ssh")
    targets = []
    monkeypatch.setattr(tr.socket, "create_connection", lambda addr, timeout=None: targets.append(addr) or FakeSocket())
    assert tr.SubprocessSSHTransport(host(port=2222), SSH).tcp_check() is not None
    assert targets == [("x1carbon", 2222)]


def test_tcp_check_jump_host_checks_first_hop(popen, monkeypatch):
    popen.result = dict(rc=0, out=b"hostname inner.lan\nport 22\nproxyjump gw.example:2201\n", err=b"")
    targets = []
    monkeypatch.setattr(tr.socket, "create_connection", lambda addr, timeout=None: targets.append(addr) or FakeSocket())
    tr.SubprocessSSHTransport(host(), SSH).tcp_check()
    tr.SubprocessSSHTransport(host(jump_host="me@bastion"), SSH).tcp_check()
    assert targets == [("gw.example", 2201), ("bastion", 22)]


def test_tcp_check_unreachable_returns_none(popen, monkeypatch):
    popen.result = dict(rc=255, out=b"", err=b"bad")

    def refuse(addr, timeout=None):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(tr.socket, "create_connection", refuse)
    assert tr.SubprocessSSHTransport(host(), SSH).tcp_check() is None


# --------------------------------------------------------------------------- factory / provide


def test_factory_kinds_and_prefix():
    f = tr.TransportFactory(Config())
    assert isinstance(f.for_host(host(kind="local", address="localhost")), tr.LocalTransport)
    assert isinstance(f.for_host(host()), tr.SubprocessSSHTransport)
    h = host(kind="slurm", exec_prefix=SLURM_PREFIX)
    assert isinstance(f.for_host(h), tr.PrefixedTransport)
    assert isinstance(f.for_host(h, prefixed=False), tr.SubprocessSSHTransport)


def test_factory_rejects_bad_host():
    with pytest.raises(ValueError):
        tr.TransportFactory(Config()).for_host(host(address="--evil"))


def test_factory_uses_resolved_binary(monkeypatch):
    monkeypatch.setattr(tr, "resolve_ssh_binary", lambda config: "Z:/ssh.exe")
    assert tr.TransportFactory(Config()).for_host(host()).ssh_bin == "Z:/ssh.exe"


def test_transports_satisfy_protocol(fake_transport):
    for t in (tr.LocalTransport(host(kind="local")), tr.SubprocessSSHTransport(host(), SSH),
              tr.PrefixedTransport(fake_transport, ["x"])):
        assert isinstance(t, tr.Transport)


def test_provide_with_and_without_config():
    cfg = Config(ssh_path="X")
    assert tr.provide(Services(config=cfg)).config is cfg
    f = tr.provide(Services())          # config service unavailable -> defaults
    assert isinstance(f, tr.TransportFactory) and f.config == Config()


# --------------------------------------------------------------------------- asyncssh (mocked)


class FakeRunResult:
    def __init__(self, exit_status=0, stdout=b"", stderr=b""):
        self.exit_status, self.stdout, self.stderr = exit_status, stdout, stderr


def make_fake_asyncssh(*, fail_connect=False, run=None):
    mod = types.SimpleNamespace()
    mod.connects = []

    class TimeoutError_(Exception):
        def __init__(self, stdout=b"", stderr=b""):
            super().__init__("timeout")
            self.stdout, self.stderr = stdout, stderr

    class Conn:
        def __init__(self):
            self.commands = []
            self.closed = False

        async def run(self, command, input=None, encoding="utf-8", check=False, timeout=None):
            self.commands.append((command, input, encoding, timeout))
            return run(command, input) if run else FakeRunResult(0, b"out", b"")

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    async def connect(address, **kwargs):
        mod.connects.append((address, kwargs))
        if fail_connect:
            raise OSError("host key not verifiable")
        mod.conn = Conn()
        return mod.conn

    mod.connect = connect
    mod.TimeoutError = TimeoutError_
    return mod


def test_factory_falls_back_without_asyncssh(monkeypatch):
    monkeypatch.setattr(tr, "_import_asyncssh", lambda: None)
    t = tr.TransportFactory(Config(use_asyncssh=True)).for_host(host())
    assert type(t) is tr.SubprocessSSHTransport


def test_import_asyncssh_absent_returns_none(monkeypatch):
    monkeypatch.setitem(sys.modules, "asyncssh", None)   # makes `import asyncssh` fail
    assert tr._import_asyncssh() is None


def test_asyncssh_disabled_by_default(monkeypatch):
    monkeypatch.setattr(tr, "_import_asyncssh", lambda: make_fake_asyncssh())
    assert type(tr.TransportFactory(Config()).for_host(host())) is tr.SubprocessSSHTransport


def test_asyncssh_reuses_one_connection(monkeypatch, popen):
    fake = make_fake_asyncssh()
    monkeypatch.setattr(tr, "_import_asyncssh", lambda: fake)
    f = tr.TransportFactory(Config(use_asyncssh=True))
    h = host(user="dev", port=2222, identity_file=r"C:\k\id", jump_host="gw")
    t = f.for_host(h)
    assert isinstance(t, tr.AsyncSSHTransport) and f.for_host(h) is t
    r1 = t.run(["echo", "a b"], stdin=b"in")
    r2 = t.run_script("true\n")
    assert (r1.rc, r1.stdout) == (0, "out") and r2.rc == 0
    assert len(fake.connects) == 1
    address, kw = fake.connects[0]
    assert address == "x1carbon" and kw["username"] == "dev" and kw["port"] == 2222
    assert kw["client_keys"] == [r"C:\k\id"] and kw["tunnel"] == "gw"
    assert "agent_path" in kw and kw["agent_path"] is None     # only the configured key
    assert fake.conn.commands[0][:3] == ("echo 'a b'", b"in", None)
    assert fake.conn.commands[1][:2] == ("sh -s", b"true\n")
    assert popen.calls == []
    # prefix is applied on top
    hp = host(id="s", exec_prefix=SLURM_PREFIX)
    pt = f.for_host(hp)
    pt.run(["sinfo", "-h"])
    assert fake.conn.commands[-1][0] == "podman exec -i -w /data slurmctld sinfo -h"
    f.close()
    assert fake.conn.closed


def test_asyncssh_connect_failure_falls_back_to_subprocess(monkeypatch, popen):
    fake = make_fake_asyncssh(fail_connect=True)
    t = tr.AsyncSSHTransport(host(), SSH, asyncssh_module=fake)
    res = t.run(["true"])
    assert res.rc == 0 and popen.calls[0][0][-3:] == ["x1carbon", "--", "true"]


def test_asyncssh_timeout_and_exit_codes(monkeypatch):
    holder = {}

    def run(command, stdin):
        if command == "slow":
            raise holder["mod"].TimeoutError(b"part", b"")
        if command == "sig":
            return FakeRunResult(None, b"", b"")
        return FakeRunResult(5, "text", "err")

    fake = make_fake_asyncssh(run=run)
    holder["mod"] = fake
    t = tr.AsyncSSHTransport(host(), SSH, asyncssh_module=fake)
    slow = t.run(["slow"], timeout=1)
    assert slow.rc == 124 and slow.timed_out and slow.stdout == "part"
    assert t.run(["sig"]).rc == 255
    other = t.run(["x"])
    assert (other.rc, other.stdout, other.stderr) == (5, "text", "err")


def test_asyncssh_rejects_bad_host_and_delegates_local_argv():
    with pytest.raises(ValueError):
        tr.AsyncSSHTransport(host(address="-x"), SSH, asyncssh_module=make_fake_asyncssh())
    t = tr.AsyncSSHTransport(host(), SSH, asyncssh_module=make_fake_asyncssh())
    assert t.local_argv(["htop"], tty=True)[-3:] == ["x1carbon", "--", "htop"]


# --------------------------------------------------------------------------- live


@pytest.mark.live
def test_live_x1carbon_podman_and_slurm_prefix():
    factory = tr.TransportFactory(Config())
    plain = factory.for_host(host())
    start = time.perf_counter()
    res = plain.run(["podman", "version", "--format", "{{.Client.Version}}"], timeout=30)
    t1 = time.perf_counter() - start
    assert res.rc == 0, res.stderr
    assert res.stdout.strip()

    slurm = factory.for_host(host(id="slurm", kind="slurm", exec_prefix=SLURM_PREFIX))
    start = time.perf_counter()
    res2 = slurm.run(["sinfo", "-V"], timeout=30)
    t2 = time.perf_counter() - start
    assert res2.rc == 0, res2.stderr
    assert res2.stdout.strip().startswith("slurm")
    latency = plain.tcp_check()
    assert latency is not None
    print(f"\nLIVE ssh_bin={factory.ssh_bin} podman={res.stdout.strip()} ({t1 * 1000:.0f} ms) "
          f"sinfo={res2.stdout.strip()} ({t2 * 1000:.0f} ms) tcp_check={latency:.1f} ms")


# --------------------------------------------------------------------------- output cap (bug-hunt #2)
# These use real (local, offline) python child processes: the point is the streaming behaviour.


def _py(code):
    return [sys.executable, "-c", code]


def test_capture_keeps_head_and_tail_and_counts():
    cap = tr._Capture(100)
    for i in range(50):
        cap.feed(b"%02d" % i * 10)       # 20 bytes per chunk, 1000 in total
    assert cap.total == 1000
    text = cap.text()
    assert text.startswith("00" * 10)    # head kept
    assert text.endswith("49" * 10)      # tail kept
    assert "bytes omitted" in text and cap.dropped == 1000 - 100
    assert len(text.encode()) < 200      # bounded (marker is small)


def test_capture_below_cap_is_verbatim():
    cap = tr._Capture(100)
    cap.feed(b"abc")
    cap.feed(b"def")
    assert cap.text() == "abcdef" and cap.dropped == 0


def test_runaway_output_is_capped_while_streaming():
    # a stand-in for `yes`: 1.5 s of endless output used to be buffered fully in memory
    code = ("import sys,time\nt=time.time()\nb=b'y\\n'*32768\n"
            "while time.time()-t<1.5: sys.stdout.buffer.write(b)")
    started = time.monotonic()
    res = tr._run_process(_py(code), None, timeout=30, max_bytes=8192)
    assert res.rc == 0 and not res.timed_out
    assert time.monotonic() - started < 15
    assert len(res.stdout) < 8192 + 200 and "bytes omitted" in res.stdout
    assert res.stdout.rstrip().endswith("y")


def test_stderr_is_capped_too_and_stdout_unaffected():
    code = "import sys; sys.stderr.write('e'*50000); sys.stdout.write('hi')"
    res = tr._run_process(_py(code), None, timeout=30, max_bytes=1000)
    assert res.stdout == "hi"
    assert len(res.stderr) < 1200 and "bytes omitted" in res.stderr


def test_output_below_cap_is_complete():
    res = tr._run_process(_py("import sys; sys.stdout.write('x'*5000)"), None, timeout=30)
    assert res.stdout == "x" * 5000 and res.rc == 0


def test_large_stdin_does_not_deadlock():
    code = "import sys; d=sys.stdin.buffer.read(); sys.stdout.write(str(len(d)))"
    res = tr._run_process(_py(code), b"z" * 3_000_000, timeout=30)
    assert res.stdout == "3000000"


def test_timeout_kills_process_tree_and_keeps_partial_output():
    code = ("import subprocess,sys,time\n"
            "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            "print(c.pid, time.time(), flush=True)\n"
            "time.sleep(60)")
    # What the test proves: a grandchild that exists well before the cut-off is killed with
    # its parent, and the output printed before the cut-off is kept. On a loaded machine two
    # interpreters can take longer than a short limit to start, and a grandchild created in
    # the instant the cut-off fires may escape the process-tree snapshot taken by the kill
    # (a race of the test setup, not of the code under test). So a run whose first line did
    # not appear at least a margin before the cut-off is repeated with a longer limit.
    margin = 1.5
    pid = None
    for limit in (3, 10, 40):
        begun = time.time()
        started = time.monotonic()
        res = tr._run_process(_py(code), None, timeout=limit)
        assert res.timed_out and res.rc == 124 and f"timed out after {limit} s" in res.stderr
        assert time.monotonic() - started < limit + 30, "the timeout did not return promptly"
        words = res.stdout.split()
        if len(words) >= 2 and float(words[1]) - begun < limit - margin:
            pid = int(words[0])
            break
    assert pid is not None, "the child never printed its first line in time, even with a 40 s limit"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    assert not _pid_alive(pid), "grandchild survived the timeout"


def _pid_alive(pid):
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True).stdout
        return f'"{pid}"' in out           # the PID column, not any number that contains it
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_endless_output_command_still_times_out():
    code = "import sys\nb=b'y'*65536\nwhile True: sys.stdout.buffer.write(b)"
    res = tr._run_process(_py(code), None, timeout=1.5, max_bytes=4096)
    assert res.timed_out and res.rc == 124
    assert len(res.stdout) < 4096 + 200


def test_transports_apply_the_default_cap(popen, monkeypatch):
    seen = {}

    def fake_run(argv, stdin, timeout, max_bytes=None, env=None):
        seen["max_bytes"] = max_bytes
        return ExecResult(rc=0, stdout="", stderr="", duration_s=0.0)

    monkeypatch.setattr(tr, "_run_process", fake_run)
    tr.LocalTransport(host(kind="local")).run(["true"], output_cap=1234)
    assert seen["max_bytes"] == 1234
    tr.SubprocessSSHTransport(host(), SSH).run(["true"], output_cap=77)
    assert seen["max_bytes"] == 77
    tr.LocalTransport(host(kind="local")).run(["true"])
    assert seen["max_bytes"] == tr.OUTPUT_CAP_BYTES


def test_clip_bounds_decoded_text():
    big = "a" * 10000
    clipped = tr._clip(big, 1000)
    assert len(clipped) < 1200 and "bytes omitted" in clipped
    assert tr._clip("short", 1000) == "short"


def test_factory_re_resolves_when_ssh_path_changes(monkeypatch):
    seen = []

    def fake_resolve(config):
        seen.append(config.ssh_path)
        return config.ssh_path or "ssh-default"

    monkeypatch.setattr(tr, "resolve_ssh_binary", fake_resolve)
    cfg = Config()
    f = tr.TransportFactory(cfg)
    assert f.for_host(host()).ssh_bin == "ssh-default"
    assert f.for_host(host()).ssh_bin == "ssh-default"
    assert seen == [""], "unchanged setting: the cached binary is reused"
    cfg.ssh_path = "D:/new/ssh.exe"
    assert f.for_host(host()).ssh_bin == "D:/new/ssh.exe"
    assert f.ssh_bin == "D:/new/ssh.exe"
    assert seen == ["", "D:/new/ssh.exe"]
    cfg.ssh_path = ""
    assert f.for_host(host()).ssh_bin == "ssh-default"
    assert seen == ["", "D:/new/ssh.exe", ""]


def test_factory_follows_a_live_ssh_path_setting(monkeypatch):
    from cloud_compute_core.config import LiveConfig
    state = {"ssh_path": ""}
    cfg = LiveConfig(loader=lambda: Config(**state), min_interval=0)
    monkeypatch.setattr(tr, "resolve_ssh_binary", lambda c: c.ssh_path or "ssh-default")
    f = tr.TransportFactory(cfg)
    assert f.ssh_bin == "ssh-default"
    state["ssh_path"] = "E:/ssh.exe"
    assert f.for_host(host()).ssh_bin == "E:/ssh.exe"


def test_asyncssh_connection_is_rebuilt_when_ssh_path_changes(monkeypatch):
    built = []

    class FakeAsync:
        def __init__(self, h, ssh_bin, asyncssh_module=None):
            self.host, self.ssh_bin, self.closed = h, ssh_bin, False
            built.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(tr, "AsyncSSHTransport", FakeAsync)
    monkeypatch.setattr(tr, "_import_asyncssh", lambda: object())
    monkeypatch.setattr(tr, "resolve_ssh_binary", lambda c: c.ssh_path or "ssh-default")
    cfg = Config(use_asyncssh=True)
    f = tr.TransportFactory(cfg)
    a = f.for_host(host(), prefixed=False)
    assert f.for_host(host(), prefixed=False) is a and len(built) == 1
    cfg.ssh_path = "F:/ssh.exe"
    b = f.for_host(host(), prefixed=False)
    assert b is not a and b.ssh_bin == "F:/ssh.exe" and a.closed


# --------------------------------------------------------------------------- child environment

FAKE_ENV = {
    "CC_AGENT_API_KEY": "fake-cc-key",          # ws-scan:allow fake test value
    "OPENAI_API_KEY": "fake-openai-key",        # ws-scan:allow fake test value
    "LLAMA_SWAP_API_KEY": "fake-swap-key",      # ws-scan:allow fake test value
    "HF_TOKEN": "fake-hf",                      # ws-scan:allow fake test value
    "GITHUB_TOKEN": "fake-gh",                  # ws-scan:allow fake test value
    "GH_PAT": "fake-pat",                       # ws-scan:allow fake test value
    "AWS_SECRET_ACCESS_KEY": "fake-aws",        # ws-scan:allow fake test value
    "DB_PASSWORD": "fake-pw",                   # ws-scan:allow fake test value
    "openai_api_key": "fake-lower",             # ws-scan:allow fake test value
    "SSH_AUTH_SOCK": "/run/user/1000/agent.sock",
    "SSH_AGENT_PID": "4242",
    "PATH": "/usr/bin",
    "HOME": "/home/dev",
    "USERPROFILE": r"C:\Users\dev",
    "SystemRoot": r"C:\Windows",
    "TEMP": r"C:\Temp",
    "PATHEXT": ".EXE;.CMD",
    "OPENSSH_AGENT_PIPE": r"\\.\pipe\openssh-ssh-agent",
}


def test_child_env_drops_secrets_and_keeps_what_ssh_needs():
    out = tr.child_env(FAKE_ENV)
    for name in ("CC_AGENT_API_KEY", "OPENAI_API_KEY", "LLAMA_SWAP_API_KEY", "HF_TOKEN",
                 "GITHUB_TOKEN", "GH_PAT", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD",
                 "openai_api_key"):
        assert name not in out, name
    for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "PATH", "HOME", "USERPROFILE", "SystemRoot",
                 "TEMP", "PATHEXT", "OPENSSH_AGENT_PIPE"):
        assert out[name] == FAKE_ENV[name], name
    assert "CC_AGENT_API_KEY" in FAKE_ENV          # the input mapping is not modified


def test_child_env_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("CC_AGENT_API_KEY", "fake-cc-key")      # ws-scan:allow fake test value
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")    # ws-scan:allow fake test value
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    out = tr.child_env()
    assert "CC_AGENT_API_KEY" not in out and "OPENAI_API_KEY" not in out
    assert out["SSH_AUTH_SOCK"] == "/tmp/agent.sock" and "PATH" in out


def test_ssh_run_child_gets_filtered_env(popen, monkeypatch):
    monkeypatch.setenv("CC_AGENT_API_KEY", "fake-cc-key")      # ws-scan:allow fake test value
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")    # ws-scan:allow fake test value
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    tr.SubprocessSSHTransport(host(), SSH).run(["true"])
    env = popen.calls[0][1]["env"]
    assert "CC_AGENT_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert env["SSH_AUTH_SOCK"] == "/tmp/agent.sock" and "PATH" in env


def test_local_child_gets_filtered_env_for_a_real_process(monkeypatch):
    monkeypatch.setenv("CC_AGENT_API_KEY", "fake-cc-key")      # ws-scan:allow fake test value
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")    # ws-scan:allow fake test value
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    code = ("import os; print(sorted(k for k in ('CC_AGENT_API_KEY','OPENAI_API_KEY',"
            "'SSH_AUTH_SOCK','PATH') if k in os.environ))")
    res = tr._run_process(_py(code), None, timeout=30)
    assert res.rc == 0, res.stderr
    assert res.stdout.strip() == "['PATH', 'SSH_AUTH_SOCK']"


def test_kill_tree_taskkill_gets_filtered_env(monkeypatch):
    monkeypatch.setenv("CC_AGENT_API_KEY", "fake-cc-key")      # ws-scan:allow fake test value
    monkeypatch.setattr(tr, "_is_windows", lambda: True)
    seen = []
    monkeypatch.setattr(tr.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)))
    tr._kill_tree(types.SimpleNamespace(pid=4321, kill=lambda: None))
    (argv, kw), = seen
    assert argv[0] == "taskkill" and "CC_AGENT_API_KEY" not in kw["env"] and "PATH" in kw["env"]


# --------------------------------------------------------------------------- run_stream


def _local():
    return tr.LocalTransport(HostSpec(id="local", name="This computer", kind="local", address="localhost"))


def _py(code):
    return [sys.executable, "-c", code]


def test_run_stream_sinks_stdout_in_chunks_without_buffering():
    chunks = []
    res = _local().run_stream(_py("import sys; sys.stdout.buffer.write(b'x' * 700000)"),
                              sink=chunks.append, max_bytes=10_000_000, timeout=60)
    assert res.rc == 0 and not res.too_large and not res.timed_out
    assert res.bytes == 700000 == sum(len(c) for c in chunks)
    assert len(chunks) >= 2 and max(len(c) for c in chunks) <= tr.STREAM_CHUNK_BYTES
    assert res.stdout == ""


def test_run_stream_cap_kills_the_process():
    got = []
    started = time.monotonic()
    res = _local().run_stream(
        _py("import sys, time\nwhile True:\n    sys.stdout.buffer.write(b'y' * 65536)\n"
            "    sys.stdout.flush()"), sink=got.append, max_bytes=300_000, timeout=60)
    assert res.too_large and res.bytes > 300_000
    assert sum(len(c) for c in got) <= 300_000          # the chunk over the cap is not sunk
    assert time.monotonic() - started < 30


def test_run_stream_source_feeds_stdin_and_counts():
    def source(writer):
        for _ in range(5):
            writer.write(b"z" * 100_000)

    res = _local().run_stream(
        _py("import sys; print(len(sys.stdin.buffer.read()))"), source=source,
        max_bytes=1_000_000, timeout=60)
    assert res.rc == 0 and res.bytes == 500_000 and res.stdout.strip() == "500000"


def test_run_stream_source_over_cap():
    def source(writer):
        while True:
            writer.write(b"z" * 65536)

    res = _local().run_stream(_py("import sys; sys.stdin.buffer.read()"), source=source,
                              max_bytes=200_000, timeout=60)
    assert res.too_large


def test_run_stream_timeout_kills_tree():
    res = _local().run_stream(_py("import time; time.sleep(30)"), sink=lambda b: None,
                              max_bytes=None, timeout=1)
    assert res.timed_out and res.rc == tr.RC_TIMEOUT


def test_run_stream_sink_error_is_reraised():
    def sink(_chunk):
        raise OSError(28, "No space left on device")

    with pytest.raises(OSError):
        _local().run_stream(_py("import sys; sys.stdout.buffer.write(b'x' * 1000)"),
                            sink=sink, timeout=60)


def test_run_stream_missing_binary_and_argument_checks():
    res = _local().run_stream(["definitely-not-a-binary-cc"], sink=lambda b: None)
    assert res.rc == tr.RC_NOT_FOUND
    with pytest.raises(ValueError):
        _local().run_stream([], sink=lambda b: None)
    with pytest.raises(ValueError):
        tr._run_stream_process(["x"], sink=None, source=None)


def test_run_stream_popen_kwargs_keep_child_env_and_no_window(monkeypatch):
    seen = {}

    class P:
        pid = 0
        returncode = 0

        def __init__(self, argv, **kw):
            seen.update(kw, argv=argv)
            import io
            self.stdout, self.stderr, self.stdin = io.BytesIO(b"abc"), io.BytesIO(b""), None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(tr.subprocess, "Popen", P)
    monkeypatch.setenv("CC_AGENT_API_KEY", "fake-not-real")   # ws-scan:allow
    monkeypatch.setattr(tr, "_is_windows", lambda: True)
    got = []
    res = tr.SubprocessSSHTransport(HostSpec(id="h", name="h", kind="ssh", address="x1carbon"),
                                    "ssh").run_stream(["podman", "cp", "c:/w", "-"],
                                                      sink=got.append)
    assert res.rc == 0 and b"".join(got) == b"abc"
    assert seen["creationflags"] == tr._CREATE_NO_WINDOW
    assert "CC_AGENT_API_KEY" not in seen["env"]
    assert seen["argv"][0] == "ssh" and seen["argv"][-1] == "podman cp c:/w -"


def test_prefixed_and_asyncssh_run_stream_delegate(monkeypatch):
    calls = []

    class Inner:
        host = HostSpec(id="h", name="h", kind="ssh", address="x1carbon")

        def run_stream(self, argv, **kw):
            calls.append((argv, kw))
            return tr.StreamResult(rc=0, stderr="", bytes=0, duration_s=0.0)

    p = tr.PrefixedTransport(Inner(), ["podman", "exec", "-i", "slurmctld"])
    p.run_stream(["podman", "cp", "a:/b", "-"], sink=print, max_bytes=5, timeout=7)
    assert calls[-1][0] == ["podman", "exec", "-i", "slurmctld", "podman", "cp", "a:/b", "-"]
    assert calls[-1][1] == {"sink": print, "source": None, "max_bytes": 5, "timeout": 7}
    a = tr.AsyncSSHTransport(Inner.host, asyncssh_module=types.SimpleNamespace())
    a.fallback = Inner()
    a.run_stream(["x"], sink=print)
    assert calls[-1][0] == ["x"]
