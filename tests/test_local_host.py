"""The ``local`` host type on an ordinary computer: no PATH entries and no POSIX tools needed.

Covers finding Docker or Podman (explicit path, PATH, well-known folders per platform), the
native probe and connection test (Python facts plus the engine's own ``info`` output), the
"engine installed but not running" message, and that no local argv uses a POSIX tool.
"""

from __future__ import annotations

import json
import ntpath
import os
import platform
import posixpath
import socket
import sys
from types import SimpleNamespace

import pytest

from cloud_compute_core import prober as pr
from cloud_compute_core import runtimes as rt
from cloud_compute_core import transport as tr
from cloud_compute_core.models import ExecResult, HostSpec
from cloud_compute_core.testing import FakeEvents, sample_local_host

WIN_DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
WIN_PODMAN = r"C:\Program Files\RedHat\Podman\podman.exe"
#: A docker executable path that is absolute on the OS running the tests.
HERE_DOCKER = os.path.abspath(os.path.join(os.sep, "opt", "engine", "bin", "docker"))
POSIX_TOOLS = {"true", "uname", "grep", "df", "timeout", "sh", "nproc", "cut", "sed", "tail",
               "bash", "cmd", "cmd.exe", "command"}


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 22))])


def res(rc=0, out="", err=""):
    return ExecResult(rc=rc, stdout=out, stderr=err, duration_s=0.0)


# --------------------------------------------------------------------------- finding the binary


def _fake_install(monkeypatch, platform_name, present, on_path=()):
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(rt.os.path, "exists", lambda p: str(p) in present)
    monkeypatch.setattr(rt.shutil, "which", lambda name: on_path[0] if on_path and
                        (ntpath.basename(on_path[0]) if "\\" in on_path[0] else posixpath.basename(on_path[0])
                         ).split(".")[0] == name else None)
    monkeypatch.setattr(rt.os.path, "expanduser", lambda p: "/home/u" if platform_name != "win32"
                        else r"C:\Users\u")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    monkeypatch.delenv("ProgramW6432", raising=False)


def test_explicit_path_beats_path_and_known_folders(monkeypatch):
    mine = r"D:\tools\docker.exe"
    _fake_install(monkeypatch, "win32", {mine, WIN_DOCKER}, on_path=[r"C:\bin\docker.exe"])
    assert rt.find_runtime("docker", explicit=mine) == mine


def test_missing_explicit_path_falls_back_to_path(monkeypatch):
    _fake_install(monkeypatch, "win32", {WIN_DOCKER}, on_path=[r"C:\bin\docker.exe"])
    assert rt.find_runtime("docker", explicit=r"D:\gone\docker.exe") == r"C:\bin\docker.exe"


def test_path_beats_known_folders(monkeypatch):
    _fake_install(monkeypatch, "win32", {WIN_DOCKER}, on_path=[r"C:\bin\docker.exe"])
    assert rt.find_runtime("docker") == r"C:\bin\docker.exe"


def test_windows_docker_desktop_found_without_path(monkeypatch):
    _fake_install(monkeypatch, "win32", {WIN_DOCKER})
    assert rt.find_runtime("docker") == WIN_DOCKER
    assert rt.find_runtime("podman") is None


def test_windows_podman_found_without_path(monkeypatch):
    _fake_install(monkeypatch, "win32", {WIN_PODMAN})
    assert rt.find_runtime("podman") == WIN_PODMAN


@pytest.mark.parametrize("platform_name,folder", [
    ("darwin", "/opt/homebrew/bin"), ("darwin", "/usr/local/bin"),
    ("darwin", "/home/u/.docker/bin"), ("darwin", "/home/u/.orbstack/bin"),
    ("darwin", "/Applications/Docker.app/Contents/Resources/bin"),
    ("darwin", "/opt/podman/bin"),
    ("linux", "/usr/bin"), ("linux", "/usr/local/bin"), ("linux", "/home/u/.local/bin"),
])
def test_unix_known_folders(monkeypatch, platform_name, folder):
    want = posixpath.join(folder, "docker")
    _fake_install(monkeypatch, platform_name, {want})
    assert rt.find_runtime("docker") == want


def test_nothing_installed(monkeypatch):
    _fake_install(monkeypatch, "linux", set())
    assert rt.find_runtime("docker") is None and rt.find_runtime("podman") is None


def test_well_known_paths_per_platform_do_not_touch_the_disk():
    win = rt.well_known_runtime_paths("docker", platform="win32",
                                      env={"ProgramFiles": r"C:\Program Files"}, home=r"C:\Users\u")
    assert ntpath.join(r"C:\Program Files", "Docker", "Docker", "resources", "bin",
                       "docker.exe") in win
    assert all(p.endswith("docker.exe") for p in win)
    pod = rt.well_known_runtime_paths("podman", platform="win32",
                                      env={"ProgramFiles": r"C:\Program Files"}, home=r"C:\Users\u")
    assert ntpath.join(r"C:\Program Files", "RedHat", "Podman", "podman.exe") in pod
    mac = rt.well_known_runtime_paths("docker", platform="darwin", env={}, home="/Users/u")
    assert "/Users/u/.docker/bin/docker" in mac and "/opt/homebrew/bin/docker" in mac
    assert mac.index("/Users/u/.orbstack/bin/docker") == mac.index("/Users/u/.docker/bin/docker") + 1
    lin = rt.well_known_runtime_paths("podman", platform="linux", env={}, home="/home/u")
    assert lin[:2] == ["/usr/bin/podman", "/usr/local/bin/podman"]
    assert "/home/u/.local/bin/podman" in lin


def test_find_runtime_rejects_other_names():
    with pytest.raises(ValueError):
        rt.find_runtime("sh")


# --------------------------------------------------------------------------- LocalTransport argv


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_run(argv, stdin, timeout, max_bytes=None, env=None):
        calls.append(SimpleNamespace(argv=list(argv), env=env, timeout=timeout))
        return res()

    monkeypatch.setattr(tr, "_run_process", fake_run)
    return calls


def _path_of(env):
    key = next(k for k in env if k.upper() == "PATH")
    return env[key].split(os.pathsep)


def test_local_run_uses_full_path_and_puts_its_folder_on_path(monkeypatch, captured):
    monkeypatch.setattr(tr, "find_runtime", lambda name, explicit="": HERE_DOCKER)
    tr.LocalTransport(sample_local_host()).run(["docker", "ps", "-a"])
    call = captured[0]
    assert call.argv == [HERE_DOCKER, "ps", "-a"]
    assert _path_of(call.env)[0] == os.path.dirname(HERE_DOCKER)


def test_local_run_leaves_argv_alone_when_not_installed(monkeypatch, captured):
    monkeypatch.setattr(tr, "find_runtime", lambda name, explicit="": None)
    tr.LocalTransport(sample_local_host()).run(["podman", "ps"])
    assert captured[0].argv == ["podman", "ps"] and captured[0].env is None


def test_local_run_with_a_full_path_also_adds_its_folder(monkeypatch, captured, tmp_path):
    exe = str(tmp_path / "bin" / "docker.exe")
    tr.LocalTransport(sample_local_host()).run([exe, "info"])
    assert captured[0].argv == [exe, "info"]
    assert _path_of(captured[0].env)[0] == str(tmp_path / "bin")


def test_local_run_other_commands_are_untouched(monkeypatch, captured):
    monkeypatch.setattr(tr, "find_runtime", lambda name, explicit="": "/never/used")
    tr.LocalTransport(sample_local_host()).run(["nvidia-smi", "-L"])
    assert captured[0].argv == ["nvidia-smi", "-L"] and captured[0].env is None


def test_explicit_path_from_settings_is_passed_to_the_lookup(monkeypatch):
    seen = []
    monkeypatch.setattr(tr, "find_runtime", lambda name, explicit="": seen.append((name, explicit)))
    t = tr.TransportFactory(SimpleNamespace(docker_path="D:/d/docker.exe", ssh_path="",
                                            use_asyncssh=False)).for_host(sample_local_host())
    t.runtime_binary("docker")
    t.runtime_binary("podman")
    assert seen == [("docker", "D:/d/docker.exe"), ("podman", "")]


def test_local_run_stream_resolves_too(monkeypatch):
    seen = {}

    def fake_stream(argv, **kw):
        seen.update(argv=argv, env=kw.get("env"))
        return tr.StreamResult(rc=0, stderr="", bytes=0, duration_s=0.0)

    monkeypatch.setattr(tr, "_run_stream_process", fake_stream)
    monkeypatch.setattr(tr, "find_runtime", lambda name, explicit="": HERE_DOCKER)
    tr.LocalTransport(sample_local_host()).run_stream(["docker", "cp", "c:/x", "-"],
                                                      sink=lambda b: None)
    assert seen["argv"][0] == HERE_DOCKER and seen["env"] is not None


def test_local_terminal_argv_carries_the_full_path(monkeypatch):
    monkeypatch.setattr(tr, "find_runtime", lambda name, explicit="": HERE_DOCKER)
    argv = tr.LocalTransport(sample_local_host()).local_argv(["docker", "exec", "-it", "c", "sh"],
                                                             tty=True)
    assert argv == [HERE_DOCKER, "exec", "-it", "c", "sh"]


def test_sh_step_maps_to_a_found_sh_with_its_tools_on_path(monkeypatch, captured, tmp_path):
    sh = str(tmp_path / "usr" / "bin" / "sh.exe")
    monkeypatch.setattr(tr, "_find_sh", lambda: sh)
    monkeypatch.setattr(tr, "_is_windows", lambda: True)
    tr.LocalTransport(sample_local_host()).run(["sh", "-c", "echo hi"])
    assert captured[0].argv == [sh, "-c", "echo hi"]
    assert _path_of(captured[0].env)[:2] == [str(tmp_path / "usr" / "bin"), str(tmp_path / "bin")]


def test_run_script_gets_the_shell_tools_on_path(monkeypatch, captured, tmp_path):
    sh = str(tmp_path / "usr" / "bin" / "sh.exe")
    monkeypatch.setattr(tr, "_find_sh", lambda: sh)
    monkeypatch.setattr(tr, "_is_windows", lambda: True)
    tr.LocalTransport(sample_local_host()).run_script("echo hi\n")
    assert captured[0].argv == [sh, "-s"]
    assert _path_of(captured[0].env)[0] == str(tmp_path / "usr" / "bin")


# --------------------------------------------------------------------------- native probe


DOCKER_INFO = {
    "ServerVersion": "28.1.1", "NCPU": 24, "MemTotal": 25197326336,
    "SecurityOptions": ["name=seccomp,profile=unconfined", "name=cgroupns"],
    "OperatingSystem": "Docker Desktop", "ServerErrors": None,
}
PODMAN_INFO = {"host": {"cpus": 8, "memTotal": 16 * 1024**3, "security": {"rootless": True}},
               "version": {"Version": "5.6.2"}}


class FakeLocal:
    """A LocalTransport stand-in: knows where the binaries are, answers engine commands."""

    def __init__(self, host, binaries, answers, *, smi=None):
        self.host = host
        self.binaries = binaries
        self.answers = answers          # {(binary path, subcommand): ExecResult}
        self.argvs: list[list[str]] = []

    def runtime_binary(self, name):
        return self.binaries.get(name)

    def run(self, argv, *, stdin=None, timeout=60):
        self.argvs.append(list(argv))
        for (path, sub), answer in self.answers.items():
            if argv[0] == path and argv[1] == sub:
                return answer
        return res(rc=127, err=f"fake: no answer for {argv}")

    def run_script(self, script, *, timeout=60):
        raise AssertionError("a local host must not run a shell script")

    def tcp_check(self, timeout=3.0):
        return 0.0


def docker_answers(path=WIN_DOCKER, *, running=True, info=None, image=True):
    if running:
        return {
            (path, "version"): res(out="28.1.1\n"),
            (path, "info"): res(out=json.dumps(info or DOCKER_INFO)),
            (path, "image"): res(rc=0 if image else 1),
        }
    return {
        (path, "version"): res(rc=1, out="28.1.1\n",
                               err="error during connect: dial tcp: connection refused"),
        (path, "info"): res(rc=1, out=json.dumps({"ServerErrors": ["dial"], "NCPU": 0}),
                            err="error during connect: connectex: refused"),
    }


def fake_platform(monkeypatch, system, *, release="11", machine="AMD64", mac="15.1"):
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "release", lambda: release)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(platform, "mac_ver", lambda: (mac, ("", "", ""), machine))
    monkeypatch.setattr(pr.shutil, "which", lambda name: None)      # no nvidia-smi
    monkeypatch.setattr(pr, "_nvidia_smi", lambda: None)


def test_windows_docker_desktop_native_probe(monkeypatch):
    fake_platform(monkeypatch, "Windows")
    t = FakeLocal(sample_local_host(), {"docker": WIN_DOCKER}, docker_answers())
    found = pr.native_probe(t, "auto")
    caps, resources = pr.parse_probe("local", found.result.stdout)
    assert caps.os == "Windows 11" and caps.arch == "x86_64"
    assert caps.runtime == "docker" and caps.runtime_version == "28.1.1"
    assert caps.rootless is False and caps.containers and caps.port_forward
    assert caps.interactive_terminal and caps.display == "supported"
    # CPUs and memory are the engine's (the Docker Desktop VM), not the computer's
    assert resources.cpu_total == 24
    assert resources.mem_total_mb == 25197326336 // (1024 * 1024)
    assert resources.cpu_used is None and resources.mem_used_mb is None
    assert resources.source == "local"
    # disk comes from the native home folder, not df
    assert resources.disk_path       # the home folder (or the working folder), not a df mount
    assert resources.disk_total_gb and resources.disk_free_gb is not None
    assert pr.local_status(found.engines, caps) == ("online", None)


def test_native_probe_never_uses_a_posix_tool_or_a_script(monkeypatch):
    fake_platform(monkeypatch, "Windows")
    t = FakeLocal(sample_local_host(), {"docker": WIN_DOCKER, "podman": WIN_PODMAN},
                  {**docker_answers(), **docker_answers(WIN_PODMAN, running=False)})
    pr.native_probe(t, "auto")
    assert t.argvs
    for argv in t.argvs:
        assert argv[0] in (WIN_DOCKER, WIN_PODMAN), argv       # always a full path
        assert ntpath.basename(argv[0]).lower().split(".")[0] not in POSIX_TOOLS
    assert not [a for argv in t.argvs for a in argv if a in POSIX_TOOLS]


def test_macos_podman_machine_native_probe(monkeypatch):
    fake_platform(monkeypatch, "Darwin", machine="arm64")
    path = "/opt/podman/bin/podman"
    t = FakeLocal(sample_local_host(), {"podman": path}, {
        (path, "version"): res(out="5.6.2\n"), (path, "info"): res(out=json.dumps(PODMAN_INFO)),
        (path, "image"): res(rc=1)})
    found = pr.native_probe(t, "auto")
    caps, resources = pr.parse_probe("local", found.result.stdout)
    assert caps.os == "macOS 15.1" and caps.arch == "arm64"
    assert caps.runtime == "podman" and caps.rootless is True and caps.containers
    assert caps.display == "buildable"
    assert resources.cpu_total == 8 and resources.mem_total_mb == 16 * 1024
    assert ["/opt/podman/bin/podman", "image", "exists"] == t.argvs[-1][:3]


def test_linux_native_probe_reports_a_distro_name(monkeypatch):
    fake_platform(monkeypatch, "Linux", release="6.8.0", machine="x86_64")
    monkeypatch.setattr(platform, "freedesktop_os_release",
                        lambda: {"PRETTY_NAME": "Fedora Linux 43 (Workstation Edition)"})
    monkeypatch.setattr(pr, "_loadavg", lambda: 0.5)
    path = "/usr/bin/podman"
    t = FakeLocal(sample_local_host(), {"podman": path}, {
        (path, "version"): res(out="5.6.2\n"), (path, "info"): res(out=json.dumps(PODMAN_INFO)),
        (path, "image"): res(rc=0)})
    caps, resources = pr.parse_probe("local", pr.native_probe(t, "auto").result.stdout)
    assert caps.os == "Fedora Linux 43" and caps.arch == "x86_64"
    assert resources.cpu_used == 0.5 and caps.display == "supported"


def test_native_cpu_count_is_used_when_the_engine_does_not_answer(monkeypatch):
    fake_platform(monkeypatch, "Windows")
    monkeypatch.setattr(pr.os, "cpu_count", lambda: 12)
    t = FakeLocal(sample_local_host(), {"docker": WIN_DOCKER}, docker_answers(running=False))
    found = pr.native_probe(t, "auto")
    caps, resources = pr.parse_probe("local", found.result.stdout)
    assert resources.cpu_total == 12 and resources.mem_total_mb is None
    assert caps.runtime == "docker" and not caps.containers


def test_gpu_listed_from_nvidia_smi(monkeypatch):
    fake_platform(monkeypatch, "Windows")
    smi = r"C:\Windows\System32\nvidia-smi.exe"
    monkeypatch.setattr(pr, "_nvidia_smi", lambda: smi)
    t = FakeLocal(sample_local_host(), {"docker": WIN_DOCKER},
                  {**docker_answers(), (smi, "--query-gpu=index,name,memory.total,memory.used,"
                                              "utilization.gpu"): res()})
    real = t.run

    def run(argv, *, stdin=None, timeout=60):
        if argv[0] == smi:
            t.argvs.append(list(argv))
            return res(out="0, NVIDIA GeForce RTX 3090, 24576, 1024, 3\n")
        return real(argv, stdin=stdin, timeout=timeout)

    t.run = run
    caps, _ = pr.parse_probe("local", pr.native_probe(t, "auto").result.stdout)
    assert [g.name for g in caps.gpus] == ["NVIDIA GeForce RTX 3090"]


# --------------------------------------------------------------------------- engine not running


@pytest.mark.parametrize("system,start", [("Windows", "Start Docker Desktop"),
                                          ("Darwin", "Start Docker Desktop"),
                                          ("Linux", "Start the Docker service")])
def test_docker_installed_but_not_running(monkeypatch, system, start):
    fake_platform(monkeypatch, system)
    t = FakeLocal(sample_local_host(), {"docker": WIN_DOCKER}, docker_answers(running=False))
    found = pr.native_probe(t, "auto")
    caps, _ = pr.parse_probe("local", found.result.stdout)
    status, error = pr.local_status(found.engines, caps)
    assert status == "degraded"
    assert error == f"Docker is installed but not running. {start}, then test again."
    assert found.engines[0].path == WIN_DOCKER and not found.engines[0].running


def test_permission_denied_is_not_called_not_running(monkeypatch):
    fake_platform(monkeypatch, "Linux")
    t = FakeLocal(sample_local_host(), {"docker": "/usr/bin/docker"}, {
        ("/usr/bin/docker", "version"): res(rc=1, out="28.1.1\n"),
        ("/usr/bin/docker", "info"): res(rc=1, err="permission denied while trying to connect "
                                                   "to the Docker daemon socket")})
    found = pr.native_probe(t, "auto")
    caps, _ = pr.parse_probe("local", found.result.stdout)
    status, error = pr.local_status(found.engines, caps)
    assert status == "degraded" and "permission denied" in error and "not running" not in error


def test_podman_machine_not_running(monkeypatch):
    fake_platform(monkeypatch, "Windows")
    t = FakeLocal(sample_local_host(), {"podman": WIN_PODMAN}, {
        (WIN_PODMAN, "version"): res(rc=125, out="5.6.2\n"),
        (WIN_PODMAN, "info"): res(rc=125, err="Cannot connect to Podman")})
    found = pr.native_probe(t, "auto")
    caps, _ = pr.parse_probe("local", found.result.stdout)
    assert pr.local_status(found.engines, caps) == (
        "degraded", "Podman is installed but not running. Start the Podman machine, then test again.")


def test_running_docker_wins_over_a_stopped_podman(monkeypatch):
    fake_platform(monkeypatch, "Windows")
    t = FakeLocal(sample_local_host(), {"docker": WIN_DOCKER, "podman": WIN_PODMAN},
                  {**docker_answers(), **docker_answers(WIN_PODMAN, running=False)})
    found = pr.native_probe(t, "auto")
    caps, _ = pr.parse_probe("local", found.result.stdout)
    assert caps.runtime == "docker" and caps.containers
    assert pr.local_status(found.engines, caps)[0] == "online"


def test_no_engine_at_all(monkeypatch):
    fake_platform(monkeypatch, "Linux")
    t = FakeLocal(sample_local_host(), {}, {})
    found = pr.native_probe(t, "auto")
    caps, _ = pr.parse_probe("local", found.result.stdout)
    status, error = pr.local_status(found.engines, caps)
    assert status == "degraded" and error.startswith("No Podman or Docker found on this computer")
    assert t.argvs == []


# --------------------------------------------------------------------------- Prober on a local host


class Factory:
    def __init__(self, transport):
        self.transport = transport

    def for_host(self, host, *, prefixed=True):
        self.transport.host = host
        return self.transport


def _prober(services, transport):
    return pr.Prober(SimpleNamespace(store=services.store, events=FakeEvents(),
                                     transports=Factory(transport)))


def test_connection_test_local_online(monkeypatch, services):
    fake_platform(monkeypatch, "Windows")
    t = FakeLocal(sample_local_host(), {"docker": WIN_DOCKER}, docker_answers())
    result = _prober(services, t).test_connection(sample_local_host())
    steps = {s.name: s for s in result.steps}
    assert [s.name for s in result.steps] == ["dns", "tcp", "auth", "runtime", "scheduler",
                                              "display", "gpu"]
    assert result.ok and result.status == "online"
    assert steps["dns"].ok is None and steps["tcp"].ok and steps["auth"].ok
    assert steps["runtime"].ok and "docker 28.1.1 (rootful)" in steps["runtime"].detail
    assert WIN_DOCKER in steps["runtime"].detail            # which binary was found
    assert steps["scheduler"].ok is None and steps["display"].ok and steps["gpu"].ok is None
    assert result.capabilities.os == "Windows 11"
    assert "we " not in steps["runtime"].detail.lower().split()


def test_connection_test_local_engine_down(monkeypatch, services):
    fake_platform(monkeypatch, "Windows")
    t = FakeLocal(sample_local_host(), {"docker": WIN_DOCKER}, docker_answers(running=False))
    result = _prober(services, t).test_connection(sample_local_host())
    runtime = {s.name: s for s in result.steps}["runtime"]
    assert not result.ok and result.status == "degraded"
    assert runtime.ok is False
    assert runtime.detail == "Docker is installed but not running. Start Docker Desktop, then test again."
    assert WIN_DOCKER in (runtime.hint or "")
    assert {s.name: s for s in result.steps}["display"].detail == (
        "No container engine is running on this computer")


def test_connection_test_local_no_engine(monkeypatch, services):
    fake_platform(monkeypatch, "Linux")
    result = _prober(services, FakeLocal(sample_local_host(), {}, {})).test_connection(
        sample_local_host())
    runtime = {s.name: s for s in result.steps}["runtime"]
    assert result.status == "degraded" and runtime.ok is False
    assert "No Podman or Docker found" in runtime.detail


def test_connection_test_local_runs_no_posix_command(monkeypatch, services):
    fake_platform(monkeypatch, "Windows")
    t = FakeLocal(sample_local_host(), {"docker": WIN_DOCKER}, docker_answers())
    _prober(services, t).test_connection(sample_local_host())
    assert t.argvs and all(a[0] == WIN_DOCKER for a in t.argvs)


def test_probe_stores_an_online_local_host(monkeypatch, services):
    fake_platform(monkeypatch, "Windows")
    host = sample_local_host()
    services.store.upsert_host(host)
    t = FakeLocal(host, {"docker": WIN_DOCKER}, docker_answers())
    state = _prober(services, t).probe(host)
    assert state.status == "online" and state.last_error is None
    assert state.capabilities.runtime == "docker" and state.resources.cpu_total == 24
    assert services.store.get_host_state(host.id).status == "online"


def test_probe_degraded_when_the_engine_is_down(monkeypatch, services):
    fake_platform(monkeypatch, "Windows")
    host = sample_local_host()
    services.store.upsert_host(host)
    t = FakeLocal(host, {"docker": WIN_DOCKER}, docker_answers(running=False))
    state = _prober(services, t).probe(host)
    assert state.status == "degraded"
    assert state.last_error == "Docker is installed but not running. Start Docker Desktop, then test again."


def test_ssh_and_slurm_hosts_still_use_the_probe_script(services):
    from cloud_compute_core.testing import sample_host
    host = sample_host()
    t = services.transports.get(host.id)
    t.script_script("==os", res(out="==os\nLinux x86_64\n"))
    pr.Prober(services).probe(host)
    assert t.scripts(), "ssh hosts keep the shell probe"
