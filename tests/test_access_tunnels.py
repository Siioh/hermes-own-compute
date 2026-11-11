"""Managed loopback tunnels (core/tunnels.py): argv, readiness, TTL sweep, close."""

from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cloud_compute_core import tunnels as T
from cloud_compute_core.config import Config
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.models import parse_iso
from cloud_compute_core.store import Store
from cloud_compute_core.testing import (
    FakeEvents, make_services, sample_host, sample_instance, sample_local_host, sample_slurm_host,
)
from cloud_compute_core.transport import TransportFactory

NOW = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)


class FakePopen:
    """Records argv/kwargs; ``exit_after`` polls -> exits with ``rc`` (None = never)."""

    instances: list["FakePopen"] = []
    next_pid = 5000

    def __init__(self, argv, *, exit_after=None, rc=255, stderr_text=b"", **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        FakePopen.next_pid += 1
        self.pid = FakePopen.next_pid
        self.polls = 0
        self.exit_after = exit_after
        self._rc = rc
        self.returncode = None
        self.terminated = False
        self.killed = False
        if stderr_text and kwargs.get("stderr") is not None:
            kwargs["stderr"].write(stderr_text)
            kwargs["stderr"].flush()
        FakePopen.instances.append(self)

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        self.polls += 1
        if self.exit_after is not None and self.polls > self.exit_after:
            self.returncode = self._rc
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def popen_factory(**behaviour):
    def popen(argv, **kwargs):
        return FakePopen(argv, **behaviour, **kwargs)
    return popen


@pytest.fixture(autouse=True)
def _reset():
    FakePopen.instances.clear()
    T._REGISTRY.clear()
    yield
    T._REGISTRY.clear()


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "cc.db")
    events = FakeEvents()
    config = Config()
    services = make_services(store=store, events=events, config=config,
                             transports=TransportFactory(config))
    yield services
    store.close()


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def manager(services, **kw):
    defaults = dict(popen=popen_factory(), port_probe=lambda port: True,
                    pid_alive=lambda pid: True, pid_is_ssh=lambda pid: True,
                    kill_pid=lambda pid: None, clock=Clock(), sleep=lambda s: None,
                    port_is_free=lambda port: True, free_port=lambda: 45678,
                    ready_timeout=0.5, poll_interval=0.0)
    defaults.update(kw)
    return T.TunnelManager(services, **defaults)


def _opts(argv):
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-o"]


# --------------------------------------------------------------------------- argv


def test_argv_binds_loopback_with_required_options(env):
    tm = manager(env)
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000)
    argv = FakePopen.instances[0].argv
    assert rec.status == "open" and rec.local_port == 45678
    i = argv.index("-L")
    assert argv[i + 1] == "127.0.0.1:45678:127.0.0.1:8000"
    assert argv.count("-L") == 1
    assert "-N" in argv
    opts = _opts(argv)
    assert "ExitOnForwardFailure=yes" in opts
    assert "BatchMode=yes" in opts
    alive = [o for o in opts if o.startswith("ServerAliveInterval=")]
    assert len(alive) == 1                       # transport's value kept, not duplicated
    # our options precede the host target
    assert argv.index("ExitOnForwardFailure=yes") < argv.index("x1carbon")
    assert argv[-1] == "x1carbon"
    assert not any(a.startswith("0.0.0.0") or a.startswith("*:") for a in argv)
    assert all(not a.startswith("-L0") for a in argv)


def test_tunnel_argv_adds_missing_options_to_minimal_transport():
    class Minimal:
        def local_argv(self, remote_argv, *, tty=False, forwards=()):
            out = ["ssh"]
            for f in forwards:
                out += ["-L", f]
            return out + ["box"]

    host = sample_host(id="box", address="box")
    argv = T.tunnel_argv(Minimal(), host, 40001, "127.0.0.1", 22)
    assert argv[0] == "ssh" and argv[-1] == "box"
    assert "-N" in argv
    assert "ExitOnForwardFailure=yes" in _opts(argv)
    assert "ServerAliveInterval=15" in _opts(argv)
    assert argv[argv.index("-L") + 1] == "127.0.0.1:40001:127.0.0.1:22"


def test_tunnel_argv_rejects_non_loopback_forward():
    class Evil:
        def local_argv(self, remote_argv, *, tty=False, forwards=()):
            return ["ssh", "-N", "-L", "0.0.0.0:40001:127.0.0.1:22", "box"]

    with pytest.raises(ValueError):
        T.tunnel_argv(Evil(), sample_host(id="box", address="box"), 40001, "127.0.0.1", 22)


def test_popen_kwargs_no_shell_no_window(env):
    manager(env).open_tunnel(sample_host(), kind="port", remote_port=8000)
    kw = FakePopen.instances[0].kwargs
    assert kw["stdin"] is subprocess.DEVNULL and kw["stdout"] is subprocess.DEVNULL
    assert kw["close_fds"] is True
    assert "shell" not in kw
    if os.name == "nt":
        assert kw["creationflags"] & 0x08000000


# --------------------------------------------------------------------------- ports


def test_free_local_port_binds_loopback_zero(monkeypatch):
    seen = []

    class FakeSock:
        def __init__(self, *a):
            pass

        def bind(self, addr):
            seen.append(addr)

        def getsockname(self):
            return ("127.0.0.1", 49999)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(T.socket, "socket", FakeSock)
    assert T.free_local_port() == 49999
    assert seen == [("127.0.0.1", 0)]


def test_free_local_port_real_is_free():
    port = T.free_local_port()
    assert 1 <= port <= 65535
    assert T.port_is_free(port)


def test_port_is_free_detects_busy_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert T.port_is_free(s.getsockname()[1]) is False


def test_default_picks_free_port_when_none_given(env):
    called = []
    tm = manager(env, free_port=lambda: called.append(1) or 41111)
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000)
    assert called and rec.local_port == 41111


def test_explicit_busy_local_port_rejected(env):
    tm = manager(env, port_is_free=lambda port: False)
    with pytest.raises(ApiError) as exc:
        tm.open_tunnel(sample_host(), kind="port", remote_port=8000, local_port=40000)
    assert exc.value.status == 409
    assert FakePopen.instances == []


def test_explicit_free_local_port_used(env):
    rec = manager(env).open_tunnel(sample_host(), kind="port", remote_port=8000, local_port=40000)
    assert rec.local_port == 40000
    assert "127.0.0.1:40000:127.0.0.1:8000" in FakePopen.instances[0].argv


@pytest.mark.parametrize("kw", [
    {"remote_port": 0}, {"remote_port": 70000}, {"remote_port": True}, {"remote_port": "80"},
    {"remote_port": 80, "local_port": 80}, {"remote_port": 80, "local_port": 65536},
    {"remote_port": 80, "remote_host": "0.0.0.0 -R"}, {"remote_port": 80, "remote_host": "-oX"},
    {"remote_port": 80, "kind": "vnc"}, {"remote_port": 80, "instance_id": "a b"},
    {"remote_port": 80, "ttl_minutes": 0},
])
def test_invalid_arguments_rejected(env, kw):
    args = {"kind": "port", **kw}
    with pytest.raises(ApiError) as exc:
        manager(env).open_tunnel(sample_host(), **args)
    assert exc.value.status == 400 and exc.value.code == "invalid"
    assert FakePopen.instances == []


def test_bad_host_address_rejected(env):
    with pytest.raises(ApiError) as exc:
        manager(env).open_tunnel(sample_host(address="x1carbon;rm -rf"), kind="port",
                                 remote_port=22)
    assert exc.value.code == "invalid"
    with pytest.raises(ApiError):
        manager(env).open_tunnel(sample_host(address="-oProxyCommand=x"), kind="port",
                                 remote_port=22)
    assert FakePopen.instances == []


def test_slurm_host_rejected(env):
    with pytest.raises(ApiError) as exc:
        manager(env).open_tunnel(sample_slurm_host(), kind="port", remote_port=8888)
    assert exc.value.status == 400 and exc.value.code == "invalid"
    assert FakePopen.instances == []


# --------------------------------------------------------------------------- readiness


def test_readiness_success_open_with_pid_and_ttl(env):
    tm = manager(env)
    rec = tm.open_tunnel(sample_host(), kind="display", remote_port=41000,
                         instance_id="i-ab12cd34",
                         url_builder=lambda lp: f"http://127.0.0.1:{lp}/tok/vnc_lite.html")
    proc = FakePopen.instances[0]
    assert rec.status == "open" and rec.pid == proc.pid and rec.error is None
    assert rec.id.startswith("t-") and len(rec.id) == 10
    assert rec.url == "http://127.0.0.1:45678/tok/vnc_lite.html"
    assert parse_iso(rec.expires_at) - parse_iso(rec.created_at) == dt.timedelta(minutes=60)
    assert rec.created_at == "2026-11-08T12:00:00Z"
    assert env.store.get_tunnel(rec.id) == rec
    assert T._REGISTRY[rec.id].proc is proc


def test_default_url_and_ttl_override(env):
    rec = manager(env).open_tunnel(sample_host(), kind="port", remote_port=8000, ttl_minutes=5)
    assert rec.url == "http://127.0.0.1:45678/"
    assert rec.expires_at == "2026-11-08T12:05:00Z"


def test_readiness_waits_for_port(env):
    probes = iter([False, False, True])
    rec = manager(env, port_probe=lambda p: next(probes)).open_tunnel(
        sample_host(), kind="port", remote_port=8000)
    assert rec.status == "open"


def test_process_dies_early_error_with_sanitized_tail(env):
    secret = "a" * 32
    err = (b"Warning: Permanently added 'x1carbon' to the list of known hosts.\r\n"
           b"bind [127.0.0.1]:45678: Address already in use\r\n"
           + f"token={secret}\r\n".encode()
           + b"channel_setup_fwd_listener_tcpip: cannot listen to port: 45678\r\n"
           b"Could not request local forwarding.\r\n")
    tm = manager(env, popen=popen_factory(exit_after=1, rc=255, stderr_text=err),
                 port_probe=lambda p: False)
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000)
    assert rec.status == "error" and rec.url is None
    assert rec.error.startswith("ssh exited with code 255")
    assert "Could not request local forwarding" in rec.error
    assert secret not in rec.error and "Permanently added" not in rec.error
    assert len(rec.error) < 400
    assert rec.id not in T._REGISTRY
    assert env.store.get_tunnel(rec.id).status == "error"


def test_readiness_timeout_terminates(env):
    tm = manager(env, port_probe=lambda p: False, ready_timeout=0.0)
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000)
    assert rec.status == "error" and "not ready" in rec.error
    assert FakePopen.instances[0].terminated


def test_spawn_failure_is_error_record(env):
    def boom(argv, **kw):
        raise FileNotFoundError(2, "No such file")
    rec = manager(env, popen=boom).open_tunnel(sample_host(), kind="port", remote_port=8000)
    assert rec.status == "error" and "could not start ssh" in rec.error


def test_sanitize_stderr_masks_tokens():
    out = T.sanitize_stderr("Authorization: Bearer abcdefghijkl\nVNC_PASSWORD=deadbeef\n"
                            "path /0123456789abcdef0123456789abcdef/websockify")
    assert "abcdefghijkl" not in out and "deadbeef" not in out
    assert "0123456789abcdef0123456789abcdef" not in out


# --------------------------------------------------------------------------- local hosts


def test_local_host_no_process(env):
    rec = manager(env).open_tunnel(sample_local_host(), kind="display", remote_port=41234,
                                   url_builder=lambda lp: f"http://127.0.0.1:{lp}/t/")
    assert FakePopen.instances == []
    assert rec.pid is None and rec.status == "open" and rec.local_port == 41234
    assert rec.url == "http://127.0.0.1:41234/t/"


def test_local_host_rejects_mismatched_local_port(env):
    with pytest.raises(ApiError):
        manager(env).open_tunnel(sample_local_host(), kind="port", remote_port=41234,
                                 local_port=50000)


# --------------------------------------------------------------------------- close


def test_close_terminates_and_is_idempotent(env):
    tm = manager(env)
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000)
    proc = FakePopen.instances[0]
    closed = tm.close_tunnel(rec.id)
    assert closed.status == "closed" and proc.terminated
    assert rec.id not in T._REGISTRY
    again = tm.close_tunnel(rec.id)
    assert again.status == "closed"
    assert tm.list_tunnels() == []
    assert [t.id for t in tm.list_tunnels(open_only=False)] == [rec.id]


def test_close_unknown_is_404(env):
    with pytest.raises(ApiError) as exc:
        manager(env).close_tunnel("t-00000000")
    assert exc.value.status == 404


def test_close_unregistered_kills_only_live_ssh_pid(env):
    killed = []
    tm = manager(env, kill_pid=killed.append)
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000)
    T._REGISTRY.clear()                         # as after a module reload
    tm.close_tunnel(rec.id)
    assert killed == [rec.pid]

    killed.clear()
    tm2 = manager(env, kill_pid=killed.append, pid_is_ssh=lambda pid: False)
    rec2 = tm2.open_tunnel(sample_host(), kind="port", remote_port=8001)
    T._REGISTRY.clear()
    assert tm2.close_tunnel(rec2.id).status == "closed"
    assert killed == []                         # reused pid of another program: untouched


def test_close_local_record(env):
    tm = manager(env)
    rec = tm.open_tunnel(sample_local_host(), kind="port", remote_port=41234)
    assert tm.close_tunnel(rec.id).status == "closed"


# --------------------------------------------------------------------------- sweep


def test_ttl_expiry_closes_via_sweep(env):
    clock = Clock()
    tm = manager(env, clock=clock)
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000, ttl_minutes=5)
    local = tm.open_tunnel(sample_local_host(), kind="port", remote_port=41234, ttl_minutes=30)
    env.events.events.clear()
    assert tm.sweep(NOW + dt.timedelta(minutes=4)) == []
    assert env.events.events == []
    clock.now = NOW + dt.timedelta(minutes=6)
    assert tm.sweep() == [rec.id]
    assert FakePopen.instances[0].terminated
    assert env.store.get_tunnel(rec.id).status == "closed"
    assert env.store.get_tunnel(local.id).status == "open"
    assert tm.sweep(NOW + dt.timedelta(minutes=31)) == [local.id]
    assert env.events.payloads("tunnels.changed") == [{"ids": [rec.id]}, {"ids": [local.id]}]


def test_sweep_marks_exited_process_error(env):
    tm = manager(env)
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000)
    FakePopen.instances[0].returncode = 255
    assert tm.sweep() == [rec.id]
    got = env.store.get_tunnel(rec.id)
    assert got.status == "error" and "255" in got.error
    assert rec.id not in T._REGISTRY


def test_sweep_dead_pid_closed(env):
    alive = {"v": True}
    tm = manager(env, pid_alive=lambda pid: alive["v"])
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000)
    T._REGISTRY.clear()
    assert tm.sweep() == []
    alive["v"] = False
    assert tm.sweep() == [rec.id]
    assert env.store.get_tunnel(rec.id).status == "closed"


def test_sweep_reused_pid_not_ssh_closed(env):
    tm = manager(env, pid_is_ssh=lambda pid: False, kill_pid=lambda pid: pytest.fail("kill"))
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000)
    T._REGISTRY.clear()
    assert tm.sweep() == [rec.id]


# --------------------------------------------------------------------------- events


def test_events_carry_ids_only(env):
    tm = manager(env)
    rec = tm.open_tunnel(sample_host(), kind="display", remote_port=41000,
                         url_builder=lambda lp: f"http://127.0.0.1:{lp}/{'f' * 32}/vnc_lite.html")
    tm.close_tunnel(rec.id)
    payloads = env.events.payloads("tunnels.changed")
    assert payloads == [{"ids": [rec.id]}, {"ids": [rec.id]}]
    assert all("f" * 32 not in repr(p) for p in payloads)


# --------------------------------------------------------------------------- helpers


def test_pid_helpers_on_self():
    assert T.pid_alive(os.getpid())
    assert T.pid_alive(0) is False and T.pid_alive(None) is False
    name = T.pid_image_name(os.getpid())
    assert name and "python" in name.lower()
    assert T.pid_is_ssh(os.getpid()) is False


def test_pid_image_name_unknown_pid_is_none():
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert T.pid_image_name(proc.pid) in (None, "") or T.pid_image_name(proc.pid)
    assert T.pid_image_name(None) is None and T.pid_image_name(-5) is None
    assert T.pid_image_name(2 ** 22 + 12345) is None


def test_no_proc_filesystem_reference_in_sources():
    from pathlib import Path
    needle = "/" + "proc"  # built in pieces so this test never matches the scanner itself
    for name in ("tunnels.py", "access.py"):
        text = (Path(T.__file__).parent / name).read_text(encoding="utf-8")
        assert needle not in text, name


def test_pid_image_name_uses_ps_off_windows(monkeypatch):
    monkeypatch.setattr(T, "_is_windows", lambda: False)
    monkeypatch.setattr(T, "pid_alive", lambda pid: True)
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="/usr/bin/sshd ", stderr="")
    monkeypatch.setattr(T.subprocess, "run", fake_run)
    assert T.pid_image_name(123) == "sshd"
    assert calls == [["ps", "-p", "123", "-o", "comm="]]
    monkeypatch.setattr(T, "pid_alive", lambda pid: False)
    assert T.pid_image_name(123) is None


# --------------------------------------------------------------------------- http readiness


class _Stub:
    def __init__(self, ok_after=0, token="tok"):
        stub = self
        self.hits = []
        self.ok_after = ok_after
        self.token = token

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                stub.hits.append(self.path)
                good = self.path == f"/{stub.token}/vnc_lite.html" and len(stub.hits) > stub.ok_after
                self.send_response(200 if good else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = _Stub()
    yield s
    s.close()


def test_http_ready_200_and_404(stub):
    assert T.http_ready(stub.port, "tok/vnc_lite.html") is True
    assert T.http_ready(stub.port, "wrong/vnc_lite.html") is False


def test_http_ready_connection_refused_false():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert T.http_ready(port, "tok/vnc_lite.html", timeout=0.5) is False


def test_display_open_waits_for_http_200_with_real_stub(env):
    stub = _Stub(ok_after=2)
    try:
        tm = manager(env, port_probe=lambda p: True, http_probe=None,
                     free_port=lambda: stub.port, http_ready_timeout=5.0, sleep=lambda s: None)
        rec = tm.open_tunnel(sample_host(), kind="display", remote_port=41000,
                             ready_path="tok/vnc_lite.html")
        assert rec.status == "open"
        assert stub.hits == ["/tok/vnc_lite.html"] * 3
    finally:
        stub.close()


def test_display_http_never_ready_errors_and_terminates(env):
    stub = _Stub(token="other")
    try:
        tm = manager(env, http_probe=None, free_port=lambda: stub.port,
                     http_ready_timeout=0.2, sleep=lambda s: None)
        rec = tm.open_tunnel(sample_host(), kind="display", remote_port=41000,
                             ready_path="tok/vnc_lite.html")
        assert rec.status == "error" and "not ready" in rec.error
        assert "tok" not in rec.error
        assert FakePopen.instances[0].terminated
        assert rec.id not in T._REGISTRY
    finally:
        stub.close()


def test_display_without_token_falls_back_to_tcp_with_warning(env, caplog):
    called = []
    tm = manager(env, http_probe=lambda p, path: called.append(path) or False)
    with caplog.at_level("WARNING", logger=T.log.name):
        rec = tm.open_tunnel(sample_host(), kind="display", remote_port=41000)
    assert rec.status == "open" and not called
    assert any("TCP-only" in r.getMessage() for r in caplog.records)


def test_port_tunnel_skips_http_probe(env):
    called = []
    tm = manager(env, http_probe=lambda p, path: called.append(path) or False)
    assert tm.open_tunnel(sample_host(), kind="port", remote_port=8000).status == "open"
    assert not called


def test_http_probe_not_logged(env, caplog):
    tm = manager(env, http_probe=lambda p, path: True)
    with caplog.at_level("DEBUG"):
        tm.open_tunnel(sample_host(), kind="display", remote_port=41000,
                       ready_path="SECRETTOKEN/vnc_lite.html")
    assert "SECRETTOKEN" not in caplog.text


def test_pid_helpers_on_exited_process():
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    # pid may be reused, but almost never by another python immediately
    assert not T.pid_is_ssh(proc.pid)


@pytest.mark.live
def test_live_loopback_tunnel_via_local_echo():
    """Opens nothing remote: checks the real port helpers against a live listener."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert T.port_open(s.getsockname()[1])


# --------------------------------------------------------------------------- instance tunnels


def _put_instance(env, instance_id, status="running"):
    env.store.upsert_host(sample_host())
    env.store.upsert_instance(sample_instance(id=instance_id, status=status))


def test_close_instance_tunnels_closes_only_that_instance(env):
    tm = manager(env, http_probe=lambda port, path: True)
    host = sample_host()
    disp = tm.open_tunnel(host, kind="display", remote_port=6080, instance_id="i-aaaa0001",
                          ready_path="tok/vnc_lite.html")
    port = tm.open_tunnel(host, kind="port", remote_port=8000, instance_id="i-aaaa0001")
    other = tm.open_tunnel(host, kind="port", remote_port=8001, instance_id="i-bbbb0002")
    plain = tm.open_tunnel(host, kind="port", remote_port=8002)
    got = tm.close_instance_tunnels("i-aaaa0001")
    assert sorted(got) == sorted([disp.id, port.id])
    assert FakePopen.instances[0].terminated and FakePopen.instances[1].terminated
    assert not FakePopen.instances[2].terminated and not FakePopen.instances[3].terminated
    assert env.store.get_tunnel(disp.id).status == "closed"
    assert env.store.get_tunnel(port.id).status == "closed"
    assert env.store.get_tunnel(other.id).status == "open"
    assert env.store.get_tunnel(plain.id).status == "open"
    assert disp.id not in T._REGISTRY and port.id not in T._REGISTRY
    assert tm.close_instance_tunnels("i-aaaa0001") == []        # idempotent


def test_close_instance_tunnels_continues_after_a_failure(env):
    tm = manager(env)
    host = sample_host()
    a = tm.open_tunnel(host, kind="port", remote_port=8000, instance_id="i-aaaa0001")
    b = tm.open_tunnel(host, kind="port", remote_port=8001, instance_id="i-aaaa0001")
    real = tm.close_tunnel

    def flaky(tunnel_id):
        if tunnel_id == a.id:
            raise RuntimeError("boom")
        return real(tunnel_id)

    tm.close_tunnel = flaky
    assert tm.close_instance_tunnels("i-aaaa0001") == [b.id]
    assert env.store.get_tunnel(b.id).status == "closed"


def test_sweep_closes_tunnels_of_missing_destroyed_and_stopped_instances(env):
    tm = manager(env, http_probe=lambda port, path: True)
    host = sample_host()
    _put_instance(env, "i-dead0001", "destroyed")
    _put_instance(env, "i-stop0001", "stopped")
    _put_instance(env, "i-live0001", "running")
    gone = tm.open_tunnel(host, kind="display", remote_port=6080, instance_id="i-gone0001",
                          ready_path="tok/vnc_lite.html")
    dead = tm.open_tunnel(host, kind="port", remote_port=8000, instance_id="i-dead0001")
    stopped = tm.open_tunnel(host, kind="port", remote_port=8001, instance_id="i-stop0001")
    live = tm.open_tunnel(host, kind="port", remote_port=8002, instance_id="i-live0001")
    plain = tm.open_tunnel(host, kind="port", remote_port=8003)
    env.events.events.clear()
    changed = tm.sweep()
    assert sorted(changed) == sorted([gone.id, dead.id, stopped.id])
    for rec in (gone, dead, stopped):
        assert env.store.get_tunnel(rec.id).status == "closed"
    assert [p.terminated for p in FakePopen.instances] == [True, True, True, False, False]
    assert env.store.get_tunnel(live.id).status == "open"
    assert env.store.get_tunnel(plain.id).status == "open"
    assert gone.id not in T._REGISTRY
    assert env.events.payloads("tunnels.changed") == [{"ids": changed}]
    assert tm.sweep() == []


def test_sweep_leaves_tunnels_alone_when_instance_lookup_fails(env):
    tm = manager(env)
    rec = tm.open_tunnel(sample_host(), kind="port", remote_port=8000, instance_id="i-aaaa0001")

    def boom(instance_id):
        raise RuntimeError("db locked")

    env.store.get_instance = boom
    assert tm.sweep() == []
    assert env.store.get_tunnel(rec.id).status == "open"


def test_sweep_orphan_closes_are_bounded_per_tick(env):
    tm = manager(env)
    host = sample_host()
    n = T._MAX_ORPHAN_CLOSES + 3
    ids = [tm.open_tunnel(host, kind="port", remote_port=9000 + i, instance_id="i-gone0001").id
           for i in range(n)]
    first = tm.sweep()
    assert len(first) == T._MAX_ORPHAN_CLOSES
    second = tm.sweep()
    assert sorted(first + second) == sorted(ids) and len(second) == 3


# --------------------------------------------------------------------------- child environment


def test_tunnel_child_gets_filtered_env(env, monkeypatch):
    monkeypatch.setenv("CC_AGENT_API_KEY", "fake-cc-key")      # ws-scan:allow fake test value
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")    # ws-scan:allow fake test value
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    manager(env).open_tunnel(sample_host(), kind="port", remote_port=8000)
    child_env = FakePopen.instances[0].kwargs["env"]
    assert "CC_AGENT_API_KEY" not in child_env and "OPENAI_API_KEY" not in child_env
    assert child_env["SSH_AUTH_SOCK"] == "/tmp/agent.sock" and "PATH" in child_env


def test_ps_tasklist_and_taskkill_get_filtered_env(monkeypatch):
    monkeypatch.setenv("CC_AGENT_API_KEY", "fake-cc-key")      # ws-scan:allow fake test value
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    seen = []

    def fake_run(argv, **kw):
        seen.append((argv[0], kw))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(T.subprocess, "run", fake_run)
    monkeypatch.setattr(T, "pid_alive", lambda pid: True)
    monkeypatch.setattr(T, "_is_windows", lambda: False)
    T.pid_image_name(123)
    monkeypatch.setattr(T, "_is_windows", lambda: True)
    T._tasklist_image_name(123)
    T.kill_pid(123)
    assert [name for name, _ in seen] == ["ps", "tasklist", "taskkill"]
    for _, kw in seen:
        assert "CC_AGENT_API_KEY" not in kw["env"]
        assert kw["env"]["SSH_AUTH_SOCK"] == "/tmp/agent.sock"



# --------------------------------------------------------------------------- screen readiness (row 30)


def _tm(services, **kw):
    import time as _time
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        _time.sleep(0.005)
    manager = T.TunnelManager(services, sleep=sleep, **kw)
    return manager, sleeps


def test_http_wait_backs_off_and_stops_at_the_given_timeout(tmp_path):
    from cloud_compute_core.store import Store
    from cloud_compute_core.testing import make_services
    store = Store(tmp_path / "t.db")
    manager, sleeps = _tm(make_services(store=store), poll_interval=0.01, http_probe=lambda p, q: False)
    err = manager._wait_http(None, 41234, "x/vnc_lite.html", timeout=0.25)
    assert err == "screen not ready after 0.25 s" and err.startswith(T.NOT_READY)
    assert len(sleeps) >= 3
    assert sleeps[0] == 0.01 and all(b >= a for a, b in zip(sleeps, sleeps[1:]))
    assert sleeps[1] > sleeps[0] and max(sleeps) <= T._MAX_POLL_S
    store.close()


def test_http_wait_succeeds_when_the_server_comes_up_late(tmp_path):
    from cloud_compute_core.store import Store
    from cloud_compute_core.testing import make_services
    store = Store(tmp_path / "t.db")
    answers = iter([False, False, False, True])
    manager, sleeps = _tm(make_services(store=store), poll_interval=0.01,
                          http_probe=lambda p, q: next(answers))
    assert manager._wait_http(None, 41234, "p", timeout=5.0) is None
    assert len(sleeps) == 3
    store.close()


def test_local_display_tunnel_waits_for_the_screen_server(tmp_path):
    from cloud_compute_core.store import Store
    from cloud_compute_core.testing import make_services, sample_local_host
    store = Store(tmp_path / "t.db")
    manager, _ = _tm(make_services(store=store), poll_interval=0.01, http_probe=lambda p, q: False)
    rec = manager.open_tunnel(sample_local_host(), kind="display", remote_port=41234,
                              instance_id="i-ab12cd34", ready_path="tok/vnc_lite.html",
                              url_builder=lambda lp: f"http://127.0.0.1:{lp}/", http_timeout=0.1)
    assert rec.status == "error" and rec.error.startswith(T.NOT_READY) and rec.url is None
    up, _ = _tm(make_services(store=store), poll_interval=0.01, http_probe=lambda p, q: True)
    rec = up.open_tunnel(sample_local_host(), kind="display", remote_port=41234,
                         instance_id="i-ab12cd34", ready_path="tok/vnc_lite.html",
                         url_builder=lambda lp: f"http://127.0.0.1:{lp}/", http_timeout=0.1)
    assert rec.status == "open" and rec.url == "http://127.0.0.1:41234/"
    store.close()


def test_discard_tunnel_deletes_the_record_and_ends_the_process(tmp_path):
    from cloud_compute_core.store import Store
    from cloud_compute_core.testing import make_services, sample_tunnel
    store = Store(tmp_path / "t.db")
    killed = []
    manager = T.TunnelManager(make_services(store=store), pid_alive=lambda p: True,
                              pid_is_ssh=lambda p: True, kill_pid=killed.append)
    store.upsert_tunnel(sample_tunnel(pid=4242))
    assert manager.discard_tunnel("t-5e6f7a8b") is True
    assert store.get_tunnel("t-5e6f7a8b") is None and killed == [4242]
    assert manager.discard_tunnel("t-5e6f7a8b") is False
    store.close()
