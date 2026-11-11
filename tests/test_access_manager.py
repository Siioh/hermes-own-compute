"""AccessManager (core/access.py): terminal, display, forward, tunnels. No network."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from cloud_compute_core import access as A
from cloud_compute_core import tunnels as T
from cloud_compute_core.config import Config
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.models import TerminalLaunch
from cloud_compute_core.store import Store
from cloud_compute_core.testing import (
    FakeEvents, FakeTransportFactory, exec_result, make_services, sample_host, sample_instance,
    sample_job, sample_local_host, sample_slurm_host, sample_tunnel,
)

NOW = dt.datetime.now(dt.timezone.utc)
TOKEN = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


class FakePopen:
    instances: list = []
    next_pid = 9000

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        FakePopen.next_pid += 1
        self.pid = FakePopen.next_pid
        self.returncode = None
        FakePopen.instances.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class Sandboxes:
    def __init__(self):
        self.touched = []

    def touch(self, instance_id):
        self.touched.append(instance_id)


class SandboxesWithRefresh(Sandboxes):
    """The sandbox module's ``refresh_ports(instance_id)`` re-reads the mapping."""

    def __init__(self):
        super().__init__()
        self.refreshed = []

    def refresh_ports(self, instance_id):
        self.refreshed.append(instance_id)


@pytest.fixture(autouse=True)
def _reset():
    FakePopen.instances.clear()
    T._REGISTRY.clear()
    yield
    T._REGISTRY.clear()


class Env:
    def __init__(self, tmp_path):
        self.store = Store(tmp_path / "cc.db")
        self.events = FakeEvents()
        self.factory = FakeTransportFactory()
        self.sandboxes = Sandboxes()
        self.services = make_services(store=self.store, events=self.events, config=Config(),
                                      transports=self.factory, sandboxes=self.sandboxes)
        self.launched = []
        self.opened = []
        self.probes = []
        self.http_ok = True
        tm = T.TunnelManager(
            self.services, popen=lambda argv, **kw: FakePopen(argv, **kw),
            port_probe=lambda p: True, pid_alive=lambda p: True, pid_is_ssh=lambda p: True,
            kill_pid=lambda p: None, sleep=lambda s: None, port_is_free=lambda p: True,
            free_port=lambda: 45678, ready_timeout=0.5, poll_interval=0.0,
            http_probe=self._http_probe, http_ready_timeout=0.2)
        self.access = A.AccessManager(self.services, tunnels=tm, launcher=self._launch,
                                      opener=self._open, screen_waits=(0.2, 0.2))
        self.services._instances["access"] = self.access

    def _http_probe(self, port, path):
        self.probes.append((port, path))
        return self.http_ok

    def _launch(self, argv, title, *, prefer="wt"):
        self.launched.append((list(argv), title, prefer))
        return TerminalLaunch(launched=True, method="wt", command_preview="cmd",
                              copy_command="cmd")

    def _open(self, url):
        self.opened.append(url)
        return True

    def add(self, host=None, instance=None, job=None):
        host = host or sample_host()
        self.store.upsert_host(host)
        if instance is not None:
            self.store.upsert_instance(instance)
        if job is not None:
            self.store.upsert_job(job)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def display_instance(**kw):
    return sample_instance(display=True, display_state="supported",
                           ports={"novnc": "127.0.0.1:41234"}, **kw)


def inspect_json(token=TOKEN):
    return json.dumps([{"Config": {"Env": ["PATH=/usr/bin", f"VNC_PASSWORD={token}"]}}])


# --------------------------------------------------------------------------- terminal


def test_terminal_instance(env):
    env.add(instance=sample_instance())
    out = env.access.open_terminal(instance_id="i-ab12cd34")
    assert out.launched
    argv, title, prefer = env.launched[0]
    assert argv[0] == "ssh" and "cc-i-ab12cd34" in argv and prefer == "auto"
    assert env.sandboxes.touched == ["i-ab12cd34"]


def test_terminal_uses_default_terminal_config(env):
    env.add(instance=sample_instance())
    env.services.config.default_terminal = "copy"
    env.access.open_terminal(instance_id="i-ab12cd34")
    assert env.launched[0][2] == "copy"


def test_terminal_host_slurm_and_wrong_kind(env):
    env.add(host=sample_slurm_host())
    env.access.open_terminal(host_id="slurm-x1")
    assert "bash" in env.launched[0][0]
    env.store.upsert_host(sample_host())
    with pytest.raises(ApiError) as e:
        env.access.open_terminal(host_id="x1carbon")
    assert e.value.status == 400


def test_terminal_job(env):
    env.add(host=sample_slurm_host(), job=sample_job())
    env.access.open_terminal(job_id="j-1a2b3c4d")
    assert "--jobid=42" in env.launched[0][0]


def test_terminal_job_not_running(env):
    env.add(host=sample_slurm_host(), job=sample_job(state="PENDING"))
    with pytest.raises(ApiError) as e:
        env.access.open_terminal(job_id="j-1a2b3c4d")
    assert e.value.status == 400


@pytest.mark.parametrize("kw", [{}, {"instance_id": "a", "host_id": "b"}])
def test_terminal_needs_exactly_one_id(env, kw):
    with pytest.raises(ApiError) as e:
        env.access.open_terminal(**kw)
    assert e.value.code == "invalid"


@pytest.mark.parametrize("kw", [{"instance_id": "i-nope"}, {"host_id": "nope"}, {"job_id": "j-nope"}])
def test_terminal_missing_records(env, kw):
    with pytest.raises(ApiError) as e:
        env.access.open_terminal(**kw)
    assert e.value.status == 404


def test_terminal_instance_not_running(env):
    env.add(instance=sample_instance(status="stopped"))
    with pytest.raises(ApiError) as e:
        env.access.open_terminal(instance_id="i-ab12cd34")
    assert e.value.status == 400 and not env.launched


def test_terminal_without_sandbox_module_still_works(tmp_path):
    store = Store(tmp_path / "x.db")
    services = make_services(store=store, config=Config(), transports=FakeTransportFactory())
    store.upsert_host(sample_host())
    store.upsert_instance(sample_instance())
    mgr = A.AccessManager(services, launcher=lambda *a, **k: TerminalLaunch(
        launched=False, method="none", command_preview="c", copy_command="c"))
    assert mgr.open_terminal(instance_id="i-ab12cd34").method == "none"
    store.close()


# --------------------------------------------------------------------------- terminal: repeated clicks


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _with_clock(env):
    clock = Clock()
    env.access = A.AccessManager(env.services, tunnels=env.access.tunnels, launcher=env._launch,
                                 opener=env._open, clock=clock)
    return clock


def test_terminal_repeat_click_within_three_seconds_opens_one_window(env):
    env.add(instance=sample_instance())
    clock = _with_clock(env)
    first = env.access.open_terminal(instance_id="i-ab12cd34")
    clock.now += 2.9
    second = env.access.open_terminal(instance_id="i-ab12cd34")
    assert len(env.launched) == 1 and second is first
    assert set(second.to_dict()) == {"launched", "method", "command_preview", "copy_command", "error"}
    clock.now += 0.2                                     # 3.1 s after the first launch
    env.access.open_terminal(instance_id="i-ab12cd34")
    assert len(env.launched) == 2


def test_terminal_dedupe_is_per_target(env):
    env.add(instance=sample_instance(), job=sample_job(state="RUNNING", slurm_job_id="9"))
    env.store.upsert_host(sample_slurm_host(id="slurm-x1"))
    _with_clock(env)
    env.access.open_terminal(instance_id="i-ab12cd34")
    env.access.open_terminal(host_id="slurm-x1")
    env.access.open_terminal(host_id="slurm-x1")
    env.access.open_terminal(job_id="j-1a2b3c4d")
    assert len(env.launched) == 3


def test_terminal_failed_launch_is_not_remembered(env):
    env.add(instance=sample_instance())
    _with_clock(env)
    calls = []

    def failing(argv, title, *, prefer="auto"):
        calls.append(argv)
        return TerminalLaunch(launched=False, method="none", command_preview="c",
                              copy_command="c", error="no terminal")

    env.access._launcher = failing
    env.access.open_terminal(instance_id="i-ab12cd34")
    env.access.open_terminal(instance_id="i-ab12cd34")
    assert len(calls) == 2


def test_terminal_concurrent_clicks_open_one_window(env):
    import threading
    env.add(instance=sample_instance())
    _with_clock(env)
    results = []
    barrier = threading.Barrier(6)

    def click():
        barrier.wait()
        results.append(env.access.open_terminal(instance_id="i-ab12cd34"))

    threads = [threading.Thread(target=click) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 6 and len(env.launched) == 1


def test_terminal_slurm_host_prefix_gets_a_tty(env):
    """Regression: the stored ``podman exec -i`` prefix opened a shell with no tty."""
    env.store.upsert_host(sample_slurm_host(
        id="slurm-x1", address="x1carbon",
        interactive_prefix=["podman", "exec", "-i", "-w", "/data", "slurmctld"]))
    env.access.open_terminal(host_id="slurm-x1")
    argv = env.launched[0][0]
    assert argv[-9:] == ["podman", "exec", "-t", "-i", "-w", "/data", "slurmctld", "bash", "-l"]


# --------------------------------------------------------------------------- display


def test_display_token_from_kv_and_port_from_record(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    rec = env.access.open_display("i-ab12cd34")
    assert rec.status == "open" and rec.kind == "display" and rec.instance_id == "i-ab12cd34"
    assert rec.url == (f"http://127.0.0.1:45678/{TOKEN}/vnc_lite.html"
                       f"?path={TOKEN}/websockify&scale=true")
    assert rec.remote_port == 41234
    # The recorded port is confirmed against the runtime first (the host here answers neither,
    # so the record is kept); nothing but read-only queries runs.
    assert env.factory.get("x1carbon").argvs() == [["podman", "inspect", "cc-i-ab12cd34"],
                                                   ["podman", "port", "cc-i-ab12cd34", "6080/tcp"]]
    argv = FakePopen.instances[0].argv
    assert "127.0.0.1:45678:127.0.0.1:41234" in argv
    assert env.sandboxes.touched == ["i-ab12cd34"]


def test_display_token_from_inspect_fallback_is_cached(env):
    env.add(instance=display_instance())
    t = env.factory.get("x1carbon")
    t.script(lambda argv, stdin: argv[:2] == ["podman", "inspect"], exec_result(stdout=inspect_json()))
    rec = env.access.open_display("i-ab12cd34")
    assert TOKEN in rec.url
    assert env.store.kv_get("display-token:i-ab12cd34") == TOKEN
    assert t.argvs()[0] == ["podman", "inspect", "cc-i-ab12cd34"]


def test_display_no_token_is_conflict_and_leaks_nothing(env):
    env.add(instance=display_instance())
    t = env.factory.get("x1carbon")
    t.script(lambda argv, stdin: argv[:2] == ["podman", "inspect"],
             exec_result(stdout=json.dumps([{"Config": {"Env": ["PATH=/x"]}}])))
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.status == 409 and e.value.message == "screen token not found"
    assert not FakePopen.instances


def test_display_inspect_failure_is_conflict(env):
    env.add(instance=display_instance())      # transport unscripted -> rc 127
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.code == "conflict"


def test_display_port_from_port_argv(env):
    env.add(instance=sample_instance(display=True, ports={}))
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    t = env.factory.get("x1carbon")
    t.script(lambda argv, stdin: argv[:2] == ["podman", "port"],
             exec_result(stdout="0.0.0.0:43210\n"))
    rec = env.access.open_display("i-ab12cd34")
    assert rec.remote_port == 43210
    assert ["podman", "port", "cc-i-ab12cd34", "6080/tcp"] in t.argvs()


def test_display_port_unpublished(env):
    env.add(instance=sample_instance(display=True, ports={}))
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.status == 409


def test_display_reuses_open_tunnel(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    first = env.access.open_display("i-ab12cd34")
    second = env.access.open_display("i-ab12cd34")
    assert second.id == first.id and len(FakePopen.instances) == 1


def test_display_probes_vnc_lite_before_open(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    rec = env.access.open_display("i-ab12cd34")
    assert rec.status == "open"
    assert env.probes == [(45678, f"{TOKEN}/vnc_lite.html")]


def test_display_not_ready_raises_and_closes_ssh_and_leaves_no_record(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.http_ok = False
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.code != "internal" and TOKEN not in e.value.message
    assert FakePopen.instances[0].returncode is not None            # ssh ended
    assert env.store.list_tunnels(open_only=False) == []            # and no record is left
    assert env.store.kv_get("display-token:i-ab12cd34") == TOKEN    # a spawn-time token stays


def test_display_does_not_reuse_expired_or_closed(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    first = env.access.open_display("i-ab12cd34")
    env.store.upsert_tunnel(sample_tunnel(
        id=first.id, instance_id="i-ab12cd34", url=first.url, pid=None,
        expires_at="2000-01-01T00:00:00Z"))
    second = env.access.open_display("i-ab12cd34")
    assert second.id != first.id
    env.access.close_tunnel(second.id)
    third = env.access.open_display("i-ab12cd34")
    assert third.id != second.id


def test_display_local_host_uses_published_port(env):
    env.add(host=sample_local_host(), instance=display_instance(host_id="local", runtime="docker"))
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    rec = env.access.open_display("i-ab12cd34")
    assert rec.pid is None and rec.local_port == 41234 and not FakePopen.instances
    assert rec.url.startswith(f"http://127.0.0.1:41234/{TOKEN}/")


@pytest.mark.parametrize("inst,exc", [
    (dict(display=False), 400),
    (dict(status="stopped", display=True), 409),
])
def test_display_refused(env, inst, exc):
    env.add(instance=sample_instance(ports={"novnc": "127.0.0.1:41234"}, **inst))
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.status == exc


def test_display_refused_on_slurm_and_missing(env):
    env.add(host=sample_slurm_host(), instance=display_instance(host_id="slurm-x1"))
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.status == 400
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-zzzzzzzz")
    assert e.value.status == 404


def test_display_ssh_failure_is_a_clear_error_without_token(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.access.tunnels._port_probe = lambda p: False
    env.access.tunnels.ready_timeout = 0.0
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.code == "unavailable" and TOKEN not in json.dumps(e.value.to_dict())
    assert "could not be reached" in e.value.message
    assert env.store.list_tunnels(open_only=False) == []


def test_display_events_carry_no_token(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.access.open_display("i-ab12cd34")
    assert env.events.names() and TOKEN not in json.dumps(env.events.events)


# --------------------------------------------------------------------------- forward


def test_forward_instance_published_via_record(env):
    env.add(instance=sample_instance(ports={"8000": "127.0.0.1:38000"}))
    rec = env.access.forward_port(instance_id="i-ab12cd34", remote_port=8000, local_port=45000)
    assert rec.kind == "port" and rec.remote_port == 38000 and rec.local_port == 45000
    assert rec.url == "http://127.0.0.1:45000/"


def test_forward_instance_published_via_port_argv(env):
    env.add(instance=sample_instance())
    t = env.factory.get("x1carbon")
    t.script(lambda argv, stdin: argv[:2] == ["podman", "port"], exec_result(stdout="127.0.0.1:39000\n"))
    rec = env.access.forward_port(instance_id="i-ab12cd34", remote_port=8000)
    assert rec.remote_port == 39000 and rec.instance_id == "i-ab12cd34"
    assert ["podman", "port", "cc-i-ab12cd34", "8000/tcp"] in t.argvs()


def test_forward_instance_unpublished(env):
    env.add(instance=sample_instance())
    t = env.factory.get("x1carbon")
    t.script(lambda argv, stdin: argv[:2] == ["podman", "port"],
             exec_result(rc=125, stderr="no public port"))
    with pytest.raises(ApiError) as e:
        env.access.forward_port(instance_id="i-ab12cd34", remote_port=8000)
    assert e.value.status == 409 and "not published" in e.value.message
    assert not FakePopen.instances


def test_forward_instance_must_be_running(env):
    env.add(instance=sample_instance(status="stopped"))
    with pytest.raises(ApiError) as e:
        env.access.forward_port(instance_id="i-ab12cd34", remote_port=8000)
    assert e.value.status == 409


def test_forward_host_ssh(env):
    env.add()
    rec = env.access.forward_port(host_id="x1carbon", remote_port=3000)
    assert rec.kind == "port" and rec.instance_id is None and rec.remote_port == 3000
    assert "127.0.0.1:45678:127.0.0.1:3000" in FakePopen.instances[0].argv


@pytest.mark.parametrize("host", [sample_slurm_host(), sample_local_host()])
def test_forward_host_other_kinds_refused(env, host):
    env.add(host=host)
    with pytest.raises(ApiError) as e:
        env.access.forward_port(host_id=host.id, remote_port=3000)
    assert e.value.status == 400


@pytest.mark.parametrize("kw", [
    dict(remote_port=0), dict(remote_port=70000), dict(remote_port=True), dict(remote_port="80"),
    dict(remote_port=80, local_port=0),
])
def test_forward_validates_ports(env, kw):
    env.add()
    with pytest.raises(ApiError) as e:
        env.access.forward_port(host_id="x1carbon", **kw)
    assert e.value.code == "invalid"


def test_forward_needs_exactly_one_target(env):
    with pytest.raises(ApiError):
        env.access.forward_port(remote_port=80)
    with pytest.raises(ApiError):
        env.access.forward_port(instance_id="a", host_id="b", remote_port=80)


# --------------------------------------------------------------------------- tunnels


def test_close_list_and_sweep_delegate(env):
    env.add()
    rec = env.access.forward_port(host_id="x1carbon", remote_port=3000)
    assert [t.id for t in env.access.list_tunnels()] == [rec.id]
    closed = env.access.close_tunnel(rec.id)
    assert closed.status == "closed"
    assert env.access.list_tunnels() == []
    assert [t.id for t in env.access.list_tunnels(open_only=False)] == [rec.id]
    assert env.access.sweep() == []
    with pytest.raises(ApiError) as e:
        env.access.close_tunnel("t-missing")
    assert e.value.status == 404


def test_provide_builds_manager_with_tunnel_manager(env):
    mgr = A.provide(env.services)
    assert isinstance(mgr, A.AccessManager) and isinstance(mgr.tunnels, T.TunnelManager)
    assert env.services.access is not None


def test_open_in_browser_never_raises(env):
    def boom(url):
        raise RuntimeError(url)
    mgr = A.AccessManager(env.services, tunnels=env.access.tunnels, opener=boom)
    assert mgr.open_in_browser("http://x/") is False


# --------------------------------------------------------------------------- instance tunnels


def test_close_instance_tunnels_ends_ssh_and_spares_other_instances(env):
    other_inst = display_instance(id="i-bbbb0002")
    env.add(instance=sample_instance(display=True, display_state="supported",
                                     ports={"novnc": "127.0.0.1:41234", "8000": "127.0.0.1:41235"}))
    env.store.upsert_instance(other_inst)
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.store.kv_set("display-token:i-bbbb0002", OTHER)
    disp = env.access.open_display("i-ab12cd34")
    port = env.access.forward_port(instance_id="i-ab12cd34", remote_port=8000)
    other = env.access.open_display("i-bbbb0002")
    mine = [p for p in FakePopen.instances if p.pid in (disp.pid, port.pid)]
    assert len(mine) == 2 and all(p.returncode is None for p in mine)
    closed = env.access.close_instance_tunnels("i-ab12cd34")
    assert sorted(closed) == sorted([disp.id, port.id])
    assert all(p.returncode is not None for p in mine)          # ssh children terminated
    assert env.store.get_tunnel(disp.id).status == "closed"
    assert env.store.get_tunnel(port.id).status == "closed"
    assert env.store.get_tunnel(other.id).status == "open"
    other_proc = next(p for p in FakePopen.instances if p.pid == other.pid)
    assert other_proc.returncode is None
    assert TOKEN not in repr(env.events.events)


def test_sweep_closes_tunnel_of_destroyed_instance(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    rec = env.access.open_display("i-ab12cd34")
    inst = env.store.get_instance("i-ab12cd34")
    inst.status = "destroyed"
    env.store.upsert_instance(inst)
    assert env.access.sweep() == [rec.id]
    assert env.store.get_tunnel(rec.id).status == "closed"
    assert next(p for p in FakePopen.instances if p.pid == rec.pid).returncode is not None


# --------------------------------------------------------------------------- screen outcomes


def _template(env, tid, image, display_on=True):
    from cloud_compute_core.models import TemplateSpec
    env.store.upsert_template(TemplateSpec(id=tid, name=tid, description="", image=image,
                                           display=display_on))


def _started(seconds_ago):
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)
    return when.replace(microsecond=0, tzinfo=None).isoformat() + "Z"


SCREEN_IMAGE = "localhost/cloud-compute-novnc:0.1"


def test_display_still_starting_on_a_screen_image_right_after_start(env):
    _template(env, "desktop-novnc", SCREEN_IMAGE)
    env.add(instance=display_instance(template_id="desktop-novnc", started_at=_started(5)))
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.http_ok = False
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.message == ("The screen of python-dev-1 is still starting. "
                               "Try again in a few seconds.")
    assert e.value.code == "unavailable" and e.value.status == 503
    assert env.store.list_tunnels(open_only=False) == []


def test_display_waits_longer_right_after_a_start_of_a_screen_image(env):
    _template(env, "desktop-novnc", SCREEN_IMAGE)
    env.add(instance=display_instance(template_id="desktop-novnc", started_at=_started(5)))
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.access.screen_waits = (31.0, 8.0)
    seen = []
    real = env.access.tunnels.open_tunnel

    def spy(*a, **kw):
        seen.append(kw.get("http_timeout"))
        return real(*a, **kw)

    env.access.tunnels.open_tunnel = spy
    env.access.open_display("i-ab12cd34")
    assert seen == [31.0]
    # Not just started: the short wait.
    env.access.close_tunnel(env.store.list_tunnels()[0].id)
    env.store.upsert_instance(display_instance(template_id="desktop-novnc",
                                               started_at=_started(3600)))
    env.access.open_display("i-ab12cd34")
    assert seen == [31.0, 8.0]


def test_display_screen_image_up_a_while_and_silent_says_not_responding(env):
    _template(env, "desktop-novnc", SCREEN_IMAGE)
    env.add(instance=display_instance(template_id="desktop-novnc", started_at=_started(3600)))
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.http_ok = False
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.message.startswith("The screen of python-dev-1 is not responding.")
    assert e.value.code == "unavailable"


def test_display_plain_image_has_no_screen_and_leaves_no_tunnel_or_token(env):
    """Row 30: a template with Show a screen on a plain image (nothing listens on 6080)."""
    _template(env, "ubuntu-dev", "docker.io/library/ubuntu:24.04")
    env.add(instance=display_instance(template_id="ubuntu-dev", started_at=_started(5)))
    t = env.factory.get("x1carbon")
    t.script(lambda argv, stdin: argv[:2] == ["podman", "inspect"], exec_result(stdout=inspect_json()))
    env.http_ok = False
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.message == (
        "python-dev-1 has no screen: its template uses the image docker.io/library/ubuntu:24.04, "
        "which has no screen server. Use the Desktop (noVNC) template, or turn off Show a screen "
        "in this template.")
    assert e.value.code == "needs_setup" and e.value.status == 409
    assert env.store.list_tunnels(open_only=False) == []
    assert FakePopen.instances and all(p.returncode is not None for p in FakePopen.instances)
    assert env.store.kv_get("display-token:i-ab12cd34") is None     # the token it created
    assert TOKEN not in e.value.message


def test_display_has_no_screen_message_uses_the_container_image_when_the_template_is_gone(env):
    env.add(instance=display_instance(template_id="deleted", started_at=_started(5)))
    t = env.factory.get("x1carbon")
    body = json.dumps([{"Config": {"Image": "docker.io/library/alpine:3",
                                   "Env": [f"VNC_PASSWORD={TOKEN}"]}}])
    t.script(lambda argv, stdin: argv[:2] == ["podman", "inspect"], exec_result(stdout=body))
    env.http_ok = False
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert "uses the image docker.io/library/alpine:3" in e.value.message


def test_display_local_host_waits_for_the_screen_server_and_fails_clearly(env):
    _template(env, "ubuntu-dev", "docker.io/library/ubuntu:24.04")
    env.add(host=sample_local_host(), instance=display_instance(
        host_id="local", runtime="docker", template_id="ubuntu-dev", started_at=_started(5)))
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    env.http_ok = False
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert "has no screen" in e.value.message and env.probes           # it did probe the port
    assert env.store.list_tunnels(open_only=False) == []


def test_display_never_fails_with_internal(env):
    for image, age in (("docker.io/library/ubuntu:24.04", 5), (SCREEN_IMAGE, 5),
                       (SCREEN_IMAGE, 9999)):
        _template(env, "tpl", image)
        env.add(instance=display_instance(template_id="tpl", started_at=_started(age)))
        env.store.kv_set("display-token:i-ab12cd34", TOKEN)
        env.http_ok = False
        with pytest.raises(ApiError) as e:
            env.access.open_display("i-ab12cd34")
        assert e.value.code in ("needs_setup", "unavailable")


def test_display_token_created_by_a_failed_attempt_is_deleted_but_a_spawn_token_stays(env):
    _template(env, "ubuntu-dev", "docker.io/library/ubuntu:24.04")
    env.add(instance=display_instance(template_id="ubuntu-dev"))
    t = env.factory.get("x1carbon")
    t.script(lambda argv, stdin: argv[:2] == ["podman", "inspect"], exec_result(stdout=inspect_json()))
    env.http_ok = False
    with pytest.raises(ApiError):
        env.access.open_display("i-ab12cd34")
    assert env.store.kv_get("display-token:i-ab12cd34") is None
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    with pytest.raises(ApiError):
        env.access.open_display("i-ab12cd34")
    assert env.store.kv_get("display-token:i-ab12cd34") == TOKEN


# --------------------------------------------------------------------------- ports that moved


def inspect_with_ports(mapping, token=TOKEN, image=SCREEN_IMAGE):
    ports = {f"{c}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(h)}] for c, h in mapping.items()}
    return json.dumps([{"Config": {"Image": image, "Env": [f"VNC_PASSWORD={token}"]},
                        "NetworkSettings": {"Ports": ports}}])


def script_inspect(env, mapping, **kw):
    t = env.factory.get("x1carbon")
    t.script(lambda argv, stdin: argv[:2] == ["podman", "inspect"],
             exec_result(stdout=inspect_with_ports(mapping, **kw)))
    return t


def test_display_uses_the_live_port_when_it_moved_after_a_restart(env):
    """Local Docker: stop + start publishes a new host port; the record is stale."""
    env.add(instance=display_instance())                      # records 41234
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    script_inspect(env, {6080: 45745})
    rec = env.access.open_display("i-ab12cd34")
    assert rec.remote_port == 45745
    assert "127.0.0.1:45678:127.0.0.1:45745" in FakePopen.instances[0].argv


def test_display_asks_the_sandbox_module_to_refresh_a_moved_port(env):
    env.sandboxes = SandboxesWithRefresh()
    env.services._instances["sandboxes"] = env.sandboxes
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    script_inspect(env, {6080: 45745})
    env.access.open_display("i-ab12cd34")
    assert env.sandboxes.refreshed == ["i-ab12cd34"]


def test_display_and_forward_call_refresh_ports_before_using_a_port(env):
    env.sandboxes = SandboxesWithRefresh()
    env.services._instances["sandboxes"] = env.sandboxes
    env.add(instance=sample_instance(display=True, ports={"novnc": "127.0.0.1:41234", "8000": "127.0.0.1:38000"}))
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    script_inspect(env, {6080: 41234, 8000: 38000})
    env.access.open_display("i-ab12cd34")
    env.access.forward_port(instance_id="i-ab12cd34", remote_port=8000)
    assert env.sandboxes.refreshed == ["i-ab12cd34", "i-ab12cd34"]


def test_refresh_failure_does_not_break_the_screen(env):
    class Broken(SandboxesWithRefresh):
        def refresh_ports(self, instance_id):
            raise RuntimeError("boom")
    env.sandboxes = Broken()
    env.services._instances["sandboxes"] = env.sandboxes
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    script_inspect(env, {6080: 45745})
    assert env.access.open_display("i-ab12cd34").remote_port == 45745


def test_terminal_shell_uses_exec_workdir_of_the_sandbox_module(env):
    class WithWorkdir(Sandboxes):
        def exec_workdir(self, instance_id):
            return "/home/dev/proj"
    env.sandboxes = WithWorkdir()
    env.services._instances["sandboxes"] = env.sandboxes
    env.add(instance=sample_instance())
    env.access.open_terminal(instance_id="i-ab12cd34")
    assert env.launched[0][0][-1] == "/home/dev/proj"


def test_forward_reads_template_published_ports_from_the_record(env):
    env.add(instance=sample_instance(ports={"8000": "127.0.0.1:38000"}))
    script_inspect(env, {8000: 38000})
    rec, cport = env.access.forward_instance_port("i-ab12cd34", 38000)   # host-side given
    assert cport == 8000 and rec.remote_port == 38000


def test_display_reads_the_mapping_itself_when_refresh_ports_does_not_exist(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    script_inspect(env, {6080: 45745})
    assert not hasattr(env.sandboxes, "refresh_ports")
    assert env.access.open_display("i-ab12cd34").remote_port == 45745


def test_display_stale_tunnel_to_the_old_port_is_not_reused(env):
    env.add(instance=display_instance())
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    first = env.access.open_display("i-ab12cd34")
    assert first.remote_port == 41234
    script_inspect(env, {6080: 45745})
    second = env.access.open_display("i-ab12cd34")
    assert second.id != first.id and second.remote_port == 45745
    assert env.store.get_tunnel(first.id).status == "closed"


def test_display_without_a_published_screen_port_in_the_live_mapping_is_a_conflict(env):
    env.add(instance=display_instance())                      # the record claims a port
    env.store.kv_set("display-token:i-ab12cd34", TOKEN)
    script_inspect(env, {})
    with pytest.raises(ApiError) as e:
        env.access.open_display("i-ab12cd34")
    assert e.value.status == 409 and "not published" in e.value.message


def test_forward_uses_the_live_port_when_it_moved(env):
    env.add(instance=sample_instance(ports={"8000": "127.0.0.1:38000"}))
    script_inspect(env, {8000: 38111})
    rec = env.access.forward_port(instance_id="i-ab12cd34", remote_port=8000)
    assert rec.remote_port == 38111


def test_forward_instance_port_translates_a_host_side_port(env):
    """compute_status shows 127.0.0.1:33319; remote_port is the port INSIDE the environment."""
    env.add(instance=sample_instance(display=True, ports={"novnc": "127.0.0.1:33319"}))
    script_inspect(env, {6080: 33319})
    rec, container_port = env.access.forward_instance_port("i-ab12cd34", 33319)
    assert container_port == 6080 and rec.remote_port == 33319
    rec2, container_port2 = env.access.forward_instance_port("i-ab12cd34", 6080)
    assert container_port2 == 6080 and rec2.remote_port == 33319


def test_forward_prefers_the_container_port_over_a_host_port_with_the_same_number(env):
    env.add(instance=sample_instance(ports={"8000": "127.0.0.1:9000", "9000": "127.0.0.1:8000"}))
    script_inspect(env, {8000: 9000, 9000: 8000})
    _rec, container_port = env.access.forward_instance_port("i-ab12cd34", 8000)
    assert container_port == 8000


def test_forward_not_published_lists_the_published_ports(env):
    env.add(instance=display_instance())
    script_inspect(env, {6080: 33319})
    with pytest.raises(ApiError) as e:
        env.access.forward_port(instance_id="i-ab12cd34", remote_port=8888)
    assert e.value.status == 409 and e.value.message == (
        "Port 8888 is not published by python-dev-1. Published ports: 6080. Add the port under "
        "Published ports in the template and start a new environment.")
    assert not FakePopen.instances


def test_forward_not_published_when_nothing_is_published(env):
    env.add(instance=sample_instance())
    script_inspect(env, {})
    with pytest.raises(ApiError) as e:
        env.access.forward_port(instance_id="i-ab12cd34", remote_port=8888)
    assert e.value.message == (
        "Port 8888 is not published by python-dev-1. No ports are published. Add the port under "
        "Published ports in the template and start a new environment.")


# --------------------------------------------------------------------------- terminal folder


def test_terminal_shell_opens_in_the_template_workdir(env):
    from cloud_compute_core.models import TemplateSpec
    env.store.upsert_template(TemplateSpec(id="python-dev", name="p", description="",
                                           workdir="/srv/app"))
    env.add(instance=sample_instance())
    env.access.open_terminal(instance_id="i-ab12cd34")
    argv = env.launched[0][0]
    assert argv[-1] == "/srv/app" and any('cd "$1"' in a for a in argv)


def test_terminal_shell_falls_back_to_work_when_the_template_is_gone(env):
    env.add(instance=sample_instance())
    env.access.open_terminal(instance_id="i-ab12cd34")
    assert env.launched[0][0][-1] == "/work"
