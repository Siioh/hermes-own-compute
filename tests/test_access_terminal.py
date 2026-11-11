"""Unit tests for core/terminal.py (no real terminals, no network)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from cloud_compute_core import terminal as term
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.testing import (sample_host, sample_instance, sample_job,
                                        sample_local_host, sample_slurm_host)
from cloud_compute_core.transport import TransportFactory

SSH = "C:/ssh/ssh.exe"
SCRIPT = "command -v bash >/dev/null && exec bash -l || exec sh -l"
INTERACTIVE = ["podman", "exec", "-it", "-w", "/data", "slurmctld"]


class Factory(TransportFactory):
    @property
    def ssh_bin(self) -> str:
        return SSH


@pytest.fixture
def factory():
    return Factory()


def runs_podman(first: str) -> bool:
    """True for ``podman`` and for a full path to it (``/usr/bin/podman``, ``C:\\...\\podman.exe``):
    a local host runs the runtime by the path it was found at."""
    name = first.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe") == "podman"


def ssh_prefix(host_args=()):
    return [SSH, "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
            "-o", "StrictHostKeyChecking=accept-new", *host_args]


# --------------------------------------------------------------------------- argv


def test_instance_on_ssh_host(factory):
    t = term.instance_terminal(sample_instance(), sample_host(), factory)
    remote = "podman exec -it cc-i-ab12cd34 sh -c '" + SCRIPT + "'"
    assert t.argv == [*ssh_prefix(), "-t", "x1carbon", "--", remote]
    assert "BatchMode" not in " ".join(t.argv)
    assert t.title == "cc python-dev-1"
    assert t.command.startswith(SSH)


def test_instance_docker_runtime(factory):
    t = term.instance_terminal(sample_instance(runtime="docker"), sample_host(), factory)
    assert t.argv[-1].startswith("docker exec -it cc-i-ab12cd34 sh -c")


def test_instance_ssh_options(factory):
    host = sample_host(user="dev", port=2222, identity_file="C:/keys/id", jump_host="jump.example")
    t = term.instance_terminal(sample_instance(), host, factory)
    assert t.argv[0] == SSH
    assert ["-p", "2222"] == t.argv[t.argv.index("-p"):t.argv.index("-p") + 2]
    assert ["-i", "C:/keys/id"] == t.argv[t.argv.index("-i"):t.argv.index("-i") + 2]
    # Only the configured key is offered (not the agent's or the default ones): right after -i.
    i = t.argv.index("-i")
    assert t.argv[i:i + 4] == ["-i", "C:/keys/id", "-o", "IdentitiesOnly=yes"]
    assert "IdentitiesOnly=yes" in t.command
    assert ["-J", "jump.example"] == t.argv[t.argv.index("-J"):t.argv.index("-J") + 2]
    assert ["-l", "dev"] == t.argv[t.argv.index("-l"):t.argv.index("-l") + 2]
    assert t.argv[-4:-2] == ["-t", "x1carbon"]


def test_instance_on_local_host_has_no_ssh(factory):
    t = term.instance_terminal(sample_instance(host_id="local"), sample_local_host(), factory)
    assert runs_podman(t.argv[0])
    assert t.argv[1:] == ["exec", "-it", "cc-i-ab12cd34", "sh", "-c", SCRIPT]


def test_instance_uses_interactive_prefix_not_exec_prefix(factory):
    host = sample_host(exec_prefix=["EXEC"], interactive_prefix=["pre", "-it"])
    t = term.instance_terminal(sample_instance(), host, factory)
    assert t.argv[-1].startswith("pre -it podman exec -it cc-i-ab12cd34")
    assert "EXEC" not in t.command


def test_instance_exec_prefix_alone_is_ignored(factory):
    t = term.instance_terminal(sample_instance(host_id="local"),
                               sample_local_host(exec_prefix=["EXEC"]), factory)
    assert runs_podman(t.argv[0])
    assert "EXEC" not in t.argv


def test_instance_must_be_running_and_on_host(factory):
    with pytest.raises(ApiError) as exc:
        term.instance_terminal(sample_instance(status="stopped"), sample_host(), factory)
    assert exc.value.status == 400
    with pytest.raises(ApiError):
        term.instance_terminal(sample_instance(host_id="other"), sample_host(), factory)


def test_host_terminal_slurm_endpoint_shape(factory):
    t = term.host_terminal(sample_slurm_host(), factory)
    remote = " ".join([*INTERACTIVE, "bash", "-l"])
    assert t.argv == [*ssh_prefix(), "-t", "x1carbon", "--", remote]
    assert t.title == "cc slurm-x1"


def test_host_terminal_slurm_without_prefix(factory):
    t = term.host_terminal(sample_slurm_host(interactive_prefix=[], exec_prefix=["x"]), factory)
    assert t.argv[-1] == "bash -l"


def test_host_terminal_rejected_for_ssh_and_local(factory):
    for host in (sample_host(), sample_local_host()):
        with pytest.raises(ApiError) as exc:
            term.host_terminal(host, factory)
        assert exc.value.code == "invalid"


def test_job_terminal(factory):
    t = term.job_terminal(sample_job(), sample_slurm_host(), factory)
    remote = " ".join([*INTERACTIVE, "srun", "--jobid=42", "--overlap", "--pty", "bash"])
    assert t.argv == [*ssh_prefix(), "-t", "x1carbon", "--", remote]
    assert t.title == "cc job j-1a2b3c4d"


@pytest.mark.parametrize("state", ["PENDING", "COMPLETED", "FAILED", "CANCELLED"])
def test_job_must_be_running(factory, state):
    with pytest.raises(ApiError):
        term.job_terminal(sample_job(state=state), sample_slurm_host(), factory)


def test_job_needs_slurm_id_and_matching_host(factory):
    with pytest.raises(ApiError):
        term.job_terminal(sample_job(slurm_job_id=None), sample_slurm_host(), factory)
    with pytest.raises(ApiError):
        term.job_terminal(sample_job(host_id="elsewhere"), sample_slurm_host(), factory)


# --------------------------------------------------------------------------- SAFE_TOKEN


def test_unsafe_address_rejected(factory):
    with pytest.raises(ApiError):
        term.instance_terminal(sample_instance(), sample_host(address="x1;rm"), factory)
    with pytest.raises(ApiError):
        term.host_terminal(sample_slurm_host(address="x1;rm"), factory)


def test_unsafe_user_rejected(factory):
    with pytest.raises(ApiError):
        term.instance_terminal(sample_instance(), sample_host(user="a b"), factory)


def test_unsafe_container_name_rejected(factory):
    with pytest.raises(ApiError):
        term.instance_terminal(sample_instance(id="i-ab 12"), sample_host(), factory)
    with pytest.raises(ApiError):
        term.instance_terminal(sample_instance(id="i-ab;12"), sample_host(), factory)


def test_unsafe_job_id_rejected(factory):
    for bad in ("42; rm -rf /", "4 2", "--x"):
        with pytest.raises(ApiError):
            term.job_terminal(sample_job(slurm_job_id=bad), sample_slurm_host(), factory)


def test_title_is_sanitised():
    assert term.safe_title("a;b&c|d") == "cc abcd"
    assert term.safe_title("") == "cc terminal"
    assert " " in term.safe_title("x")


def test_command_string_quotes_spaces():
    assert term.command_string(["a", "b c"], windows=True) == 'a "b c"'


def test_command_string_is_posix_quoted_off_windows():
    assert term.command_string(["a", "b c", "it's"], windows=False) == "a 'b c' 'it'\"'\"'s'"


# --------------------------------------------------------------------------- interactive prefix (Slurm shell)


@pytest.mark.parametrize("prefix,expected", [
    # the real x1carbon-slurm record: -i without -t gave a shell with no tty, no prompt
    (["podman", "exec", "-i", "-w", "/data", "slurmctld"],
     ["podman", "exec", "-t", "-i", "-w", "/data", "slurmctld"]),
    (["podman", "exec", "-it", "-w", "/data", "slurmctld"],
     ["podman", "exec", "-it", "-w", "/data", "slurmctld"]),
    (["docker", "exec", "-ti", "c"], ["docker", "exec", "-ti", "c"]),
    (["docker", "exec", "--interactive", "--tty", "c"], ["docker", "exec", "--interactive", "--tty", "c"]),
    (["podman", "exec", "slurmctld"], ["podman", "exec", "-i", "-t", "slurmctld"]),
    (["/usr/bin/podman", "exec", "-t", "slurmctld"], ["/usr/bin/podman", "exec", "-i", "-t", "slurmctld"]),
    (["podman.exe", "exec", "-u", "root", "c"], ["podman.exe", "exec", "-i", "-t", "-u", "root", "c"]),
    (["podman", "exec", "-w", "-t", "c"], ["podman", "exec", "-i", "-t", "-w", "-t", "c"]),  # -t is a value
    (["sudo", "-n", "podman", "exec", "c"], ["sudo", "-n", "podman", "exec", "c"]),         # not a bare CLI
    ([], []),
    (["ssh", "jump"], ["ssh", "jump"]),
])
def test_tty_prefix(prefix, expected):
    assert term.tty_prefix(prefix) == expected


def test_slurm_host_terminal_gets_a_tty_for_the_container_shell(factory):
    """Regression: ``podman exec -i`` alone starts bash without a terminal."""
    host = sample_slurm_host(interactive_prefix=["podman", "exec", "-i", "-w", "/data", "slurmctld"])
    argv = term.host_terminal(host, factory).argv
    remote = argv[argv.index("--") + 1]
    assert remote == "podman exec -t -i -w /data slurmctld bash -l"


def test_job_terminal_gets_a_tty_for_the_container_shell(factory):
    host = sample_slurm_host(interactive_prefix=["podman", "exec", "-i", "slurmctld"])
    job = sample_job(host_id=host.id, state="RUNNING", slurm_job_id="42")
    remote = term.job_terminal(job, host, factory).argv[-1]
    assert remote.startswith("podman exec -t -i slurmctld srun --jobid=42")


# --------------------------------------------------------------------------- launch


class FakePopen:
    def __init__(self, fail=()):
        self.calls: list[tuple[list, dict]] = []
        self.fail = set(fail)

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        head = argv if isinstance(argv, str) else argv[0]
        if any(f in head for f in self.fail):
            raise OSError("boom")


def which_of(*present):
    return lambda name: f"C:/bin/{name}" if name in present else None


ARGV = ["ssh.exe", "-t", "host", "--", "podman exec -it cc-i-1 sh -c 'a && b'"]
GUI = {"DISPLAY": ":0"}


def run(prefer="wt", present=("wt.exe", "cmd.exe"), fail=(), argv=ARGV, *, tmp_dir):
    popen = FakePopen(fail)
    res = term.launch(argv, "cc demo", prefer=prefer, which=which_of(*present), popen=popen,
                      platform="win32", tmp_dir=tmp_dir)
    return res, popen


def script_text(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def test_launch_wt(tmp_path):
    res, popen = run(tmp_dir=tmp_path)
    assert (res.launched, res.method, res.error) == (True, "wt", None)
    assert res.copy_command == res.command_preview == term.command_string(ARGV, windows=True)
    (argv, kw), = popen.calls
    assert argv[:5] == ["C:/bin/wt.exe", "new-tab", "--title", "cc demo", "--suppressApplicationTitle"]
    assert len(argv) == 6 and argv[5].endswith(".cmd")      # the wrapper script, not the bare argv
    assert isinstance(argv, list) and kw["shell"] is False and kw["close_fds"] is True
    assert kw["stdin"] is kw["stdout"] is kw["stderr"] is subprocess.DEVNULL
    assert kw["creationflags"] == 0x08000000


def test_windows_wrapper_keeps_the_window_open_on_error(tmp_path):
    res, popen = run(tmp_dir=tmp_path)
    text = script_text(popen.calls[0][0][5])
    assert '"ssh.exe" "-t" "host" "--" "podman exec -it cc-i-1 sh -c \'a && b\'"' in text
    assert "Press Enter to close" in text and 'if not "%CC_RC%"=="0"' in text
    assert text.rstrip().endswith("exit /b %CC_RC%")        # keeps the exit code
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")


def test_windows_wrapper_quotes_percent_and_skips_unquotable_args(tmp_path):
    res, popen = run(argv=["x", "50%"], tmp_dir=tmp_path)
    assert '"50%%"' in script_text(popen.calls[0][0][5])
    res, popen = run(argv=["x", 'say "hi"'], tmp_dir=tmp_path)          # cannot be quoted for cmd
    assert popen.calls[0][0][5:] == ["x", 'say "hi"']
    res, popen = run(argv=["x", "caf\u00e9"], tmp_dir=tmp_path)
    assert popen.calls[0][0][5:] == ["x", "caf\u00e9"]


@pytest.mark.skipif(sys.platform != "win32", reason="runs the generated .cmd with cmd.exe")
def test_windows_wrapper_really_waits_for_enter_on_error(tmp_path):
    ok = term._write_script(str(tmp_path), ".cmd",
                            term._batch_script(["cmd.exe", "/c", "echo", "100%", "&", "done"], "cc t"),
                            newline="")
    r = subprocess.run(["cmd.exe", "/d", "/c", ok], input=b"\r\n", capture_output=True, timeout=30)
    assert r.returncode == 0 and b"Press Enter" not in r.stdout
    bad = term._write_script(str(tmp_path), ".cmd",
                             term._batch_script(["cmd.exe", "/c", "exit 1"], "cc t"), newline="")
    r = subprocess.run(["cmd.exe", "/d", "/c", bad], input=b"\r\n", capture_output=True, timeout=30)
    assert r.returncode == 1 and b"Press Enter to close" in r.stdout


def test_launch_wt_escapes_semicolons(tmp_path):
    res, popen = run(argv=["x", "a;b"], tmp_dir=tmp_path)
    assert res.copy_command == "x a;b"
    assert "a;b" in script_text(popen.calls[0][0][5])


def test_launch_wt_escapes_semicolons_without_a_wrapper(tmp_path):
    res, popen = run(argv=["x", 'a;"b'], tmp_dir=tmp_path)
    assert popen.calls[0][0][-1] == 'a\\;"b'


def test_launch_cmd_when_wt_missing(tmp_path):
    res, popen = run(present=("cmd.exe",), tmp_dir=tmp_path)
    assert (res.launched, res.method, res.error) == (True, "cmd", None)
    (argv, kw), = popen.calls
    assert isinstance(argv, str)
    assert argv.startswith('C:/bin/cmd.exe /c start "cc demo" ') and argv.rstrip('"').endswith(".cmd")
    assert kw["creationflags"] == 0x08000000 and kw["shell"] is False
    assert kw["close_fds"] is True
    assert kw["stdin"] is kw["stdout"] is kw["stderr"] is subprocess.DEVNULL


def test_launch_cmd_without_wrapper_uses_the_bare_argv(tmp_path):
    bad = ["x", 'say "hi"']
    res, popen = run(present=("cmd.exe",), argv=bad, tmp_dir=tmp_path)
    assert popen.calls[0][0] == 'C:/bin/cmd.exe /c start "cc demo" ' + subprocess.list2cmdline(bad)


def test_launch_cmd_title_is_always_quoted_and_clean(tmp_path):
    popen = FakePopen()
    term.launch(ARGV, 'a"b & c', prefer="cmd", which=which_of("cmd.exe"), popen=popen,
                platform="win32", tmp_dir=tmp_path)
    line = popen.calls[0][0]
    assert line.startswith('C:/bin/cmd.exe /c start "cc ab  c" ')


def test_launch_cmd_when_wt_popen_raises(tmp_path):
    res, popen = run(fail=("wt.exe",), tmp_dir=tmp_path)
    assert (res.launched, res.method) == (True, "cmd")
    assert popen.calls[0][0][0] == "C:/bin/wt.exe"
    assert popen.calls[1][0].startswith("C:/bin/cmd.exe /c start ")


def test_launch_prefer_cmd_goes_first(tmp_path):
    res, popen = run(prefer="cmd", tmp_dir=tmp_path)
    assert res.method == "cmd" and len(popen.calls) == 1


def test_launch_nothing_available(tmp_path):
    res, popen = run(present=(), tmp_dir=tmp_path)
    assert (res.launched, res.method) == (False, "none")
    assert res.copy_command == term.command_string(ARGV, windows=True)
    assert "wt.exe" in res.error and "cmd.exe" in res.error
    assert popen.calls == []


def test_launch_all_popen_fail_records_errors(tmp_path):
    res, popen = run(fail=("wt.exe", "cmd.exe"), tmp_dir=tmp_path)
    assert (res.launched, res.method) == (False, "none")
    assert res.error.count("boom") == 2


def test_launch_prefer_copy_does_not_launch(tmp_path):
    for platform in ("win32", "darwin", "linux"):
        popen = FakePopen()
        res = term.launch(ARGV, "t", prefer="copy", which=which_of("wt.exe", "open", "xterm"),
                          popen=popen, platform=platform, tmp_dir=tmp_path, environ=GUI)
        assert (res.launched, res.method, res.error) == (False, "none", None)
        assert res.copy_command and popen.calls == []


def test_launch_auto_is_the_windows_chain_on_windows(tmp_path):
    res, popen = run(prefer="auto", tmp_dir=tmp_path)
    assert (res.launched, res.method) == (True, "wt") and len(popen.calls) == 1
    res, popen = run(prefer="auto", present=("cmd.exe",), tmp_dir=tmp_path)
    assert (res.launched, res.method) == (True, "cmd")


def test_launch_default_prefer_is_auto(tmp_path):
    popen = FakePopen()
    res = term.launch(ARGV, "t", which=which_of("wt.exe", "cmd.exe"), popen=popen,
                      windows=True, tmp_dir=tmp_path)
    assert (res.launched, res.method) == (True, "wt")


def test_launch_uses_sys_platform_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    popen = FakePopen()
    res = term.launch(ARGV, "t", which=which_of("wt.exe"), popen=popen, tmp_dir=tmp_path)
    assert res.method == "wt"
    monkeypatch.setattr(sys, "platform", "freebsd14")
    res = term.launch(ARGV, "t", which=which_of("wt.exe"), popen=popen, tmp_dir=tmp_path)
    assert (res.launched, res.method) == (False, "none") and "freebsd14" in res.error


def test_launch_windows_prefs_on_other_platforms_behave_like_auto(tmp_path):
    popen = FakePopen()
    res = term.launch(ARGV, "t", prefer="wt", which=which_of("wt.exe", "xterm"), popen=popen,
                      platform="linux", tmp_dir=tmp_path, environ=GUI)
    assert (res.launched, res.method) == (True, "linux")
    assert popen.calls[0][0][0] == "C:/bin/xterm"


def test_launch_default_popen_is_subprocess():
    import inspect
    assert inspect.signature(term.launch).parameters["popen"].default is subprocess.Popen


def test_launch_child_gets_filtered_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_AGENT_API_KEY", "fake-cc-key")      # ws-scan:allow fake test value
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")    # ws-scan:allow fake test value
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    cases = [("win32", "wt", ("wt.exe", "cmd.exe")), ("win32", "cmd", ("cmd.exe",)),
             ("darwin", "auto", ("open",)), ("linux", "auto", ("xterm",))]
    for platform, prefer, present in cases:
        popen = FakePopen()
        res = term.launch(ARGV, "cc demo", prefer=prefer, which=which_of(*present), popen=popen,
                          platform=platform, tmp_dir=tmp_path, environ=GUI)
        assert res.launched, (platform, res.error)
        env = popen.calls[0][1]["env"]
        assert "CC_AGENT_API_KEY" not in env and "OPENAI_API_KEY" not in env
        assert env["SSH_AUTH_SOCK"] == "/tmp/agent.sock" and "PATH" in env


# --------------------------------------------------------------------------- macOS


def test_launch_macos_opens_a_command_file_in_terminal_app(tmp_path):
    popen = FakePopen()
    argv = ["ssh", "-t", "host", "--", "podman exec -it cc-i-1 sh -c 'x; y'"]
    res = term.launch(argv, "cc demo", which=which_of("open"), popen=popen, platform="darwin",
                      tmp_dir=tmp_path)
    assert (res.launched, res.method, res.error) == (True, "macos", None)
    (full, kw), = popen.calls
    assert full[:3] == ["C:/bin/open", "-a", "Terminal"] and len(full) == 4
    assert full[3].endswith(".command") and str(tmp_path) in full[3]
    assert kw["shell"] is False and kw["start_new_session"] is True and "creationflags" not in kw
    text = script_text(full[3])
    assert text.startswith("#!/bin/sh\n")
    assert shlex.join(argv) in text                          # quoted with shlex, not hand-built
    assert "Press Enter to close" in text and 'if [ "$status" -ne 0 ]' in text
    assert text.rstrip().endswith('exit "$status"')
    assert res.copy_command == shlex.join(argv)
    if os.name == "posix":
        assert os.stat(full[3]).st_mode & 0o777 == 0o700
        assert os.stat(os.path.dirname(full[3])).st_mode & 0o077 == 0


def test_launch_macos_removes_old_scripts_only(tmp_path):
    d = tmp_path / "cloud-compute-terminals"
    d.mkdir()
    old, fresh, other = d / "cc-old.command", d / "cc-new.command", d / "keep.txt"
    for f in (old, fresh, other):
        f.write_text("x")
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    term.launch(ARGV, "t", which=which_of("open"), popen=FakePopen(), platform="darwin", tmp_dir=tmp_path)
    assert not old.exists() and fresh.exists() and other.exists()


def test_launch_macos_without_open_command(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    popen = FakePopen()
    res = term.launch(ARGV, "t", which=which_of(), popen=popen, platform="darwin", tmp_dir=tmp_path)
    assert (res.launched, res.method) == (False, "none") and "open" in res.error and popen.calls == []


def test_launch_macos_open_fails(tmp_path):
    res = term.launch(ARGV, "t", which=which_of("open"), popen=FakePopen(fail=("open",)),
                      platform="darwin", tmp_dir=tmp_path)
    assert (res.launched, res.method) == (False, "none") and "boom" in res.error


def test_macos_script_runs_and_waits_on_error(tmp_path):
    if os.name != "posix" or not shutil.which("sh"):
        pytest.skip("needs a POSIX sh")
    ok = term._write_script(str(tmp_path), ".command", term._posix_script(["sh", "-c", "exit 0"], "cc t"))
    assert subprocess.run(["sh", ok], input=b"\n", capture_output=True, timeout=30).returncode == 0
    bad = term._write_script(str(tmp_path), ".command",
                             term._posix_script(["sh", "-c", "echo oops >&2; exit 7"], "cc t"))
    r = subprocess.run(["sh", bad], input=b"\n", capture_output=True, timeout=30)
    assert r.returncode == 7 and b"Press Enter to close" in r.stdout


# --------------------------------------------------------------------------- Linux


def linux(present, *, prefer="auto", environ=GUI, fail=(), argv=ARGV, title="cc demo"):
    popen = FakePopen(fail)
    res = term.launch(argv, title, prefer=prefer, which=which_of(*present), popen=popen,
                      platform="linux", environ=environ)
    return res, popen


WRAP = term._POSIX_WRAPPER


@pytest.mark.parametrize("name,expected", [
    ("xdg-terminal-exec", []),
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--title=cc demo", "--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["--title=cc demo", "-x"]),
    ("kitty", ["--title", "cc demo"]),
    ("alacritty", ["--title", "cc demo", "-e"]),
    ("foot", ["--title=cc demo", "--"]),
    ("wezterm", ["start", "--"]),
    ("ptyxis", ["--new-window", "-T", "cc demo", "--"]),       # verified on Fedora 43
    ("kgx", ["--"]),
    ("xterm", ["-T", "cc demo", "-e"]),
])
def test_launch_linux_each_terminal(name, expected):
    res, popen = linux([name])
    assert (res.launched, res.method, res.error) == (True, "linux", None)
    (full, kw), = popen.calls
    assert full == [f"C:/bin/{name}", *expected, "sh", "-c", WRAP, "cc-terminal", *ARGV]
    assert kw["shell"] is False and kw["start_new_session"] is True and "creationflags" not in kw
    assert res.copy_command == shlex.join(ARGV)


def test_launch_linux_order_is_xdg_terminal_exec_first():
    allnames = [n for n, _t, _f in term._LINUX_TERMINALS]
    assert allnames == ["xdg-terminal-exec", "x-terminal-emulator", "gnome-terminal", "konsole",
                        "xfce4-terminal", "kitty", "alacritty", "foot", "wezterm", "ptyxis",
                        "kgx", "xterm"]
    res, popen = linux(allnames)
    assert popen.calls[0][0][0] == "C:/bin/xdg-terminal-exec"
    res, popen = linux(allnames[1:])
    assert popen.calls[0][0][0] == "C:/bin/x-terminal-emulator"
    # $TERMINAL still wins over everything.
    res, popen = linux(allnames, environ={**GUI, "TERMINAL": "kitty"})
    assert popen.calls[0][0][0] == "C:/bin/kitty"


def test_launch_linux_terminal_variable_wins_when_on_path():
    res, popen = linux(["tilix", "xterm"], environ={**GUI, "TERMINAL": "tilix"})
    assert popen.calls[0][0] == ["C:/bin/tilix", "-e", "sh", "-c", WRAP, "cc-terminal", *ARGV]
    res, popen = linux(["xterm"], environ={**GUI, "TERMINAL": "tilix"})     # not installed: skipped
    assert popen.calls[0][0][0] == "C:/bin/xterm"
    res, popen = linux(["xterm"], environ={**GUI, "TERMINAL": "foo bar; rm"})   # not a plain command
    assert popen.calls[0][0][0] == "C:/bin/xterm"


def test_launch_linux_terminal_variable_uses_known_flags():
    res, popen = linux(["gnome-terminal"], environ={**GUI, "TERMINAL": "gnome-terminal"})
    assert popen.calls[0][0].count("sh") == 1 and "--" in popen.calls[0][0]


def test_launch_linux_falls_through_when_one_fails():
    res, popen = linux(["x-terminal-emulator", "xterm"], fail=("x-terminal-emulator",))
    assert (res.launched, res.method) == (True, "linux") and len(popen.calls) == 2
    assert popen.calls[1][0][0] == "C:/bin/xterm"


def test_launch_linux_all_fail():
    res, popen = linux(["xterm"], fail=("xterm",))
    assert (res.launched, res.method) == (False, "none") and "boom" in res.error


def test_launch_linux_no_terminal_falls_back_to_copy():
    res, popen = linux([])
    assert (res.launched, res.method) == (False, "none") and popen.calls == []
    assert "no terminal emulator" in res.error and "TERMINAL" in res.error
    assert res.copy_command == shlex.join(ARGV)


def test_launch_linux_without_a_display_falls_back_to_copy():
    res, popen = linux(["xterm"], environ={})
    assert (res.launched, res.method) == (False, "none") and popen.calls == []
    assert res.error == ("No desktop session is available to this Hermes process, so the "
                         "command is shown instead.")
    assert res.copy_command == shlex.join(ARGV)
    res, popen = linux(["xterm"], environ={"WAYLAND_DISPLAY": "wayland-0"})
    assert res.launched


def test_launch_linux_without_a_desktop_session_tries_no_emulator_even_via_launch_default():
    """A gateway started over ssh or as a service: DISPLAY/WAYLAND_DISPLAY unset."""
    allnames = [n for n, _t, _f in term._LINUX_TERMINALS]
    for env in ({}, {"XDG_SESSION_TYPE": "tty", "TERMINAL": "kitty"}, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}):
        res, popen = linux(allnames, environ=env)
        assert popen.calls == [] and res.launched is False and res.method == "none"
        assert res.error == term.NO_DESKTOP_SESSION


def test_launch_linux_ptyxis_kgx_and_foot_pass_the_command_as_separate_argv_items():
    """Fedora's ptyxis, GNOME Console (kgx) and foot: argv lists, no shell string."""
    evil = "a b; touch /tmp/pwned"
    for name, head in (("ptyxis", ["--new-window", "-T", "cc demo", "--"]), ("kgx", ["--"]),
                       ("foot", ["--title=cc demo", "--"])):
        res, popen = linux([name], argv=["echo", evil])
        (full, _kw), = popen.calls
        assert full == [f"C:/bin/{name}", *head, "sh", "-c", WRAP, "cc-terminal", "echo", evil]
        assert all(isinstance(a, str) for a in full) and full[-1] == evil


def test_launch_linux_ptyxis_is_used_when_it_is_the_only_emulator():
    res, popen = linux(["ptyxis"])
    assert (res.launched, res.method) == (True, "linux")
    assert popen.calls[0][0][0] == "C:/bin/ptyxis"


def test_linux_wrapper_passes_the_command_as_positional_args_and_waits_on_error():
    assert WRAP.startswith('"$@"; status=$?;') and "Press Enter to close" in WRAP
    assert "read -r _" in WRAP and WRAP.endswith('exit "$status"')


def test_linux_wrapper_really_runs(tmp_path):
    if os.name != "posix" or not shutil.which("sh"):
        pytest.skip("needs a POSIX sh")
    evil = "a b; touch " + str(tmp_path / "pwned")
    r = subprocess.run(["sh", "-c", WRAP, "cc-terminal", "sh", "-c", 'echo "$1"; exit 5', "x", evil],
                       input=b"\n", capture_output=True, timeout=30)
    assert r.returncode == 5 and b"Press Enter to close" in r.stdout and evil.encode() in r.stdout
    assert not (tmp_path / "pwned").exists()
    r = subprocess.run(["sh", "-c", WRAP, "cc-terminal", "true"], input=b"", capture_output=True, timeout=30)
    assert r.returncode == 0 and b"Press Enter" not in r.stdout


def test_launch_unknown_platform_copies():
    popen = FakePopen()
    res = term.launch(ARGV, "t", which=which_of("xterm"), popen=popen, platform="sunos5")
    assert (res.launched, res.method) == (False, "none") and "sunos5" in res.error


def test_launch_result_has_the_same_keys():
    res, _ = linux(["xterm"])
    assert set(res.to_dict()) == {"launched", "method", "command_preview", "copy_command", "error"}


# --------------------------------------------------------------------------- shell folder


def test_instance_shell_starts_in_the_template_workdir(factory):
    t = term.instance_terminal(sample_instance(), sample_host(), factory, workdir="/work")
    remote = ("podman exec -it cc-i-ab12cd34 sh -c '" + term.SHELL_SCRIPT_IN_DIR + "' sh /work")
    assert t.argv == [*ssh_prefix(), "-t", "x1carbon", "--", remote]
    assert term.SHELL_SCRIPT_IN_DIR.startswith('cd "$1" 2>/dev/null; ')
    assert "exec bash -l" in term.SHELL_SCRIPT_IN_DIR and "exec sh -l" in term.SHELL_SCRIPT_IN_DIR


def test_instance_shell_in_workdir_on_a_local_host(factory):
    t = term.instance_terminal(sample_instance(host_id="local"), sample_local_host(), factory,
                               workdir="/srv/app")
    assert t.argv[-5:] == ["sh", "-c", term.SHELL_SCRIPT_IN_DIR, "sh", "/srv/app"]


def test_instance_shell_without_a_workdir_is_unchanged(factory):
    t = term.instance_terminal(sample_instance(), sample_host(), factory)
    assert t.argv[-1] == "podman exec -it cc-i-ab12cd34 sh -c '" + SCRIPT + "'"


@pytest.mark.parametrize("bad", ["work", "/a b", "/a;rm", "/a/../b", "-x", "", None, 5, "/a$HOME",
                                 "/" + "a" * 300])
def test_unsafe_workdirs_are_dropped_not_passed(factory, bad):
    assert term.shell_workdir(bad) is None
    t = term.instance_terminal(sample_instance(), sample_host(), factory, workdir=bad)
    assert t.argv[-1] == "podman exec -it cc-i-ab12cd34 sh -c '" + SCRIPT + "'"


def test_instance_shell_workdir_really_changes_directory(tmp_path):
    if os.name != "posix" or not shutil.which("sh"):
        pytest.skip("needs a POSIX sh")
    marker = tmp_path / "wd"
    script = term.SHELL_SCRIPT_IN_DIR.replace("exec bash -l", "pwd > " + str(marker)).replace(
        "exec sh -l", "pwd > " + str(marker))
    subprocess.run(["sh", "-c", script, "sh", str(tmp_path)], timeout=30)
    assert marker.read_text().strip() == str(tmp_path)
    subprocess.run(["sh", "-c", script, "sh", str(tmp_path / "missing")], timeout=30, cwd=str(tmp_path))
    assert marker.read_text().strip() == str(tmp_path)               # missing folder: ignored


# --------------------------------------------------------------------------- desktop session

import stat as _stat  # noqa: E402
from types import SimpleNamespace  # noqa: E402

UID = 1000


class FakeFs:
    """lstat/listdir over a dict path -> (kind, uid); kinds: dir, sock, file, link."""

    def __init__(self, entries):
        self.entries = dict(entries)

    def lstat(self, path):
        if path not in self.entries:
            raise FileNotFoundError(path)
        kind, uid = self.entries[path]
        mode = {"dir": _stat.S_IFDIR, "sock": _stat.S_IFSOCK, "file": _stat.S_IFREG,
                "link": _stat.S_IFLNK}[kind]
        return SimpleNamespace(st_mode=mode | 0o700, st_uid=uid)

    def listdir(self, path):
        prefix = path.rstrip("/") + "/"
        names = sorted({p[len(prefix):].split("/")[0] for p in self.entries
                        if p.startswith(prefix) and len(p) > len(prefix)})
        if path not in self.entries and not names:
            raise FileNotFoundError(path)
        return names


class FakeRun:
    def __init__(self, stdout="", rc=0, boom=None):
        self.stdout, self.rc, self.boom, self.calls = stdout, rc, boom, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.boom:
            raise self.boom
        return SimpleNamespace(returncode=self.rc, stdout=self.stdout)


RT = f"/run/user/{UID}"


def discover(entries, environ=None, run=None, home="/home/u"):
    fs = FakeFs(entries)
    run = run or FakeRun(rc=1)
    out = term.discover_desktop_session(environ or {}, uid=UID, listdir=fs.listdir, lstat=fs.lstat,
                                        runner=run, home=home)
    return out, run


def test_discovery_finds_the_wayland_socket_in_the_runtime_dir():
    out, run = discover({RT: ("dir", UID), f"{RT}/wayland-1": ("sock", UID),
                         f"{RT}/bus": ("sock", UID)})
    assert out == {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": RT,
                   "DBUS_SESSION_BUS_ADDRESS": f"unix:path={RT}/bus"}
    assert run.calls == []                                  # one socket: no systemctl needed


def test_discovery_uses_xdg_runtime_dir_from_the_environment():
    other = "/run/custom"
    out, _ = discover({other: ("dir", UID), f"{other}/wayland-0": ("sock", UID)},
                      environ={"XDG_RUNTIME_DIR": other})
    assert out["WAYLAND_DISPLAY"] == "wayland-0" and out["XDG_RUNTIME_DIR"] == other


def test_discovery_with_two_wayland_sockets_asks_systemd_which():
    entries = {RT: ("dir", UID), f"{RT}/wayland-0": ("sock", UID), f"{RT}/wayland-1": ("sock", UID)}
    out, run = discover(entries, run=FakeRun("WAYLAND_DISPLAY=wayland-1\nDISPLAY=:0\n"))
    assert out["WAYLAND_DISPLAY"] == "wayland-1"
    assert run.calls[0][0] == ["systemctl", "--user", "show-environment"]
    out, _ = discover(entries)                              # systemctl unavailable: the first
    assert out["WAYLAND_DISPLAY"] == "wayland-0"


def test_discovery_never_uses_another_users_session_or_a_symlink():
    entries = {RT: ("dir", UID), f"{RT}/wayland-0": ("sock", 1001), f"{RT}/wayland-1": ("link", UID)}
    out, _ = discover(entries)
    assert out == {}
    out, _ = discover({"/run/user/1001": ("dir", 1001), "/run/user/1001/wayland-0": ("sock", 1001)},
                      environ={"XDG_RUNTIME_DIR": "/run/user/1001"})
    assert out == {}                                         # a runtime dir of another user
    out, _ = discover({RT: ("link", UID), f"{RT}/wayland-0": ("sock", UID)})
    assert out == {}


def test_discovery_x11_needs_a_matching_authority_file():
    x11 = {"/tmp/.X11-unix/X0": ("sock", UID)}
    out, _ = discover({**x11})                              # no authority: not used
    assert out == {}
    out, _ = discover({**x11, "/home/u/.Xauthority": ("file", UID)})
    assert out == {"DISPLAY": ":0", "XAUTHORITY": "/home/u/.Xauthority"}
    out, _ = discover({**x11, "/home/u/.Xauthority": ("file", 1001)})     # someone else's file
    assert out == {}
    out, _ = discover({**x11, "/tmp/x.auth": ("file", UID)}, environ={"XAUTHORITY": "/tmp/x.auth"})
    assert out == {"DISPLAY": ":0"}                         # already in the environment
    out, _ = discover({"/tmp/.X11-unix/X1": ("sock", 1001), "/home/u/.Xauthority": ("file", UID)})
    assert out == {}                                         # someone else's X socket


def test_discovery_falls_back_to_systemctl_user_show_environment():
    env_text = ("PATH=/usr/bin\nWAYLAND_DISPLAY=wayland-1\nDISPLAY=:0\nXDG_RUNTIME_DIR=/run/user/1000\n"
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\nSECRET_THING=x\n")
    entries = {RT: ("dir", UID), f"{RT}/bus": ("sock", UID)}
    out, run = discover(entries, run=FakeRun(env_text))
    # wayland-1 is not a socket we can see: only DISPLAY from systemd is used.
    assert out["DISPLAY"] == ":0" and "WAYLAND_DISPLAY" not in out
    assert "SECRET_THING" not in out and "PATH" not in out
    entries[f"{RT}/wayland-1"] = ("sock", 1001)             # someone else's: still not used
    out, _ = discover(entries, run=FakeRun("WAYLAND_DISPLAY=wayland-1\n"))
    assert out == {}
    out, run = discover({RT: ("dir", UID), f"{RT}/bus": ("sock", UID)}, run=FakeRun("DISPLAY=:7\n"))
    assert out["DISPLAY"] == ":7" and out["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={RT}/bus"
    assert run.calls[0][1]["env"]["XDG_RUNTIME_DIR"] == RT  # the user's own manager is asked


@pytest.mark.parametrize("bad", ["WAYLAND_DISPLAY=../../etc/x\n", "DISPLAY=evil:0\n",
                                 "DISPLAY=:0;rm\n", "WAYLAND_DISPLAY=a b\n"])
def test_discovery_rejects_malformed_systemd_values(bad):
    out, _ = discover({RT: ("dir", UID)}, run=FakeRun(bad))
    assert out == {}


@pytest.mark.parametrize("run", [FakeRun(rc=1), FakeRun(boom=FileNotFoundError("systemctl")),
                                 FakeRun(boom=subprocess.TimeoutExpired("systemctl", 5))])
def test_discovery_survives_a_missing_or_failing_systemctl(run):
    out, _ = discover({RT: ("dir", UID)}, run=run)
    assert out == {}


def test_discovery_does_nothing_without_getuid(monkeypatch):
    monkeypatch.delattr(os, "getuid", raising=False)
    assert term.discover_desktop_session({}) == {}


def test_launch_uses_the_discovered_session_for_the_launched_terminal_only():
    seen = []

    def finder(environ):
        seen.append(dict(environ))
        return {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": RT}
    popen = FakePopen()
    environ = {"HOME": "/home/u"}
    res = term.launch(ARGV, "cc demo", which=which_of("kitty"), popen=popen, platform="linux",
                      environ=environ, discover=finder)
    assert (res.launched, res.method, res.error) == (True, "linux", None)
    (full, kw), = popen.calls
    assert full[0] == "C:/bin/kitty"
    assert kw["env"]["WAYLAND_DISPLAY"] == "wayland-1" and kw["env"]["XDG_RUNTIME_DIR"] == RT
    assert environ == {"HOME": "/home/u"}                    # the caller's environment is untouched


def test_launch_does_not_look_for_a_session_when_display_variables_exist():
    def finder(environ):
        raise AssertionError("must not be called")
    popen = FakePopen()
    res = term.launch(ARGV, "t", which=which_of("kitty"), popen=popen, platform="linux",
                      environ={"WAYLAND_DISPLAY": "wayland-0"}, discover=finder)
    assert res.launched


def test_launch_without_any_session_found_shows_the_command():
    popen = FakePopen()
    res = term.launch(ARGV, "cc demo", which=which_of("kitty"), popen=popen, platform="linux",
                      environ={}, discover=lambda e: {})
    assert (res.launched, res.method, popen.calls) == (False, "none", [])
    assert res.error == ("No desktop session is available to this Hermes process, so the "
                         "command is shown instead.")
    assert res.copy_command == shlex.join(ARGV)


def test_launch_survives_a_discovery_that_raises():
    def finder(environ):
        raise RuntimeError("boom")
    res = term.launch(ARGV, "t", which=which_of("kitty"), popen=FakePopen(), platform="linux",
                      environ={}, discover=finder)
    assert res.launched is False and res.error == term.NO_DESKTOP_SESSION


def test_launch_passes_the_discovered_display_variable():
    popen = FakePopen()
    term.launch(ARGV, "t", which=which_of("kitty"), popen=popen, platform="linux", environ={},
                discover=lambda e: {"DISPLAY": ":0"})
    env = popen.calls[0][1]["env"]
    assert env["DISPLAY"] == ":0"


# --------------------------------------------------------------------------- zombie children


def test_a_launcher_child_is_waited_for_so_a_closed_window_leaves_no_zombie():
    made = []

    def popen(argv, **kw):
        child = subprocess.Popen(argv, **kw)
        made.append(child)
        return child

    term._spawn(popen, [sys.executable, "-c", "pass"], windows=sys.platform == "win32")
    deadline = time.time() + 10
    while made[0].returncode is None and time.time() < deadline:
        time.sleep(0.02)
    assert made[0].returncode == 0       # somebody called wait(): the child is not defunct


def test_the_request_is_not_held_up_by_a_window_that_stays_open():
    import threading

    release = threading.Event()
    entered = threading.Event()

    class Child:
        def wait(self):
            entered.set()
            release.wait(10)

    started = time.time()
    term._spawn(lambda argv, **kw: Child(), ["x"], windows=False)
    assert time.time() - started < 2         # returned at once
    assert entered.wait(5)                   # while a background thread waits on the child
    release.set()


def test_a_launcher_without_wait_and_a_failing_wait_are_harmless():
    term._spawn(lambda argv, **kw: None, ["x"], windows=False)

    class Broken:
        def wait(self):
            raise OSError("gone")

    term._spawn(lambda argv, **kw: Broken(), ["x"], windows=False)
