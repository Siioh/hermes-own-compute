"""Interactive terminals: argv builders and a launcher for a terminal window on this computer.

Public API (all pure except :func:`launch`):

``instance_terminal(instance, host, factory)``, ``host_terminal(host, factory)`` and
``job_terminal(job, host, factory)`` return a :class:`TerminalTarget` holding the
local ``argv`` to run, a window ``title`` and a copyable ``command`` string.
Invalid input raises :class:`~.interfaces.ApiError` (status 400, code ``invalid``).

* instance -> ``[interactive_prefix...] <rt> exec -it cc-<id> sh -c '<bash -l or sh -l>'``
* host (``kind="slurm"`` only) -> ``[interactive_prefix...] bash -l``
* job (state ``RUNNING`` with a Slurm job id) ->
  ``[interactive_prefix...] srun --jobid=<id> --overlap --pty bash``

The remote argv is wrapped for the host with ``Transport.local_argv(remote, tty=True)``
of an *unprefixed* transport: ``ssh -t ... <host> -- <remote>`` for ssh hosts, the
command itself for ``kind="local"``. ``host.exec_prefix`` is never used for
terminals; only ``host.interactive_prefix`` is prepended, and a ``podman``/``docker``
``exec`` prefix always gets ``-i`` and ``-t`` (see :func:`tty_prefix`): without ``-t``
the container shell has no terminal, so it shows no prompt and has no line editing.
Every host address, user, container name and job id is checked against ``SAFE_TOKEN``.

:func:`launch` opens the argv in a new window, picked by platform (``prefer`` is
``Config.default_terminal``):

* Windows: ``wt.exe new-tab`` -> ``cmd.exe /c start``;
* macOS: Terminal.app (``open -a Terminal <file>.command``);
* Linux: ``$TERMINAL``, ``xdg-terminal-exec``, ``x-terminal-emulator``, ``gnome-terminal``, ``konsole``,
  ``xfce4-terminal``, ``kitty``, ``alacritty``, ``foot``, ``wezterm``, ``ptyxis`` (Fedora's
  default), ``kgx`` (GNOME Console), ``xterm``. Without ``DISPLAY`` or ``WAYLAND_DISPLAY`` in
  this process (a gateway started over ssh or as a service) nothing is tried and the result
  carries :data:`NO_DESKTOP_SESSION` with the copyable command;
* otherwise no launch (``method="none"``; the UI shows ``copy_command``).

``prefer="copy"`` never launches; ``"wt"``/``"cmd"``/``"macos"``/``"linux"`` pick that
family when the platform matches and behave like ``"auto"`` otherwise. Processes are
started from argv lists without a shell, with the filtered :func:`~.transport.child_env`
environment. The window stays open with "Press Enter to close" when the session ends
with an error (a small wrapper script on Windows and macOS, ``sh -c`` positional
arguments on Linux), so the error can be read.

NOTE: the macOS and Linux launchers cannot be run on the Windows development computer. They are
covered by unit tests that monkeypatch ``sys.platform`` and ``shutil.which`` and check the
exact argv; confirm them on a real Mac and Linux desktop.

``command_preview`` and ``copy_command`` use Windows quoting (:func:`subprocess.list2cmdline`)
on Windows and POSIX quoting (:func:`shlex.join`) elsewhere. The argvs contain no secrets.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from . import runtimes
from .interfaces import ApiError
from .models import HostSpec, InstanceRecord, JobRecord, TerminalLaunch
from .transport import SAFE_TOKEN, TransportFactory, child_env

log = logging.getLogger(__name__)

__all__ = [
    "TerminalTarget", "instance_terminal", "host_terminal", "job_terminal",
    "instance_remote_argv", "host_remote_argv", "job_remote_argv",
    "command_string", "safe_title", "launch", "tty_prefix", "SHELL_SCRIPT", "NO_DESKTOP_SESSION",
    "SHELL_SCRIPT_IN_DIR", "shell_workdir", "discover_desktop_session",
]

#: Remote login shell: bash when present, otherwise sh.
SHELL_SCRIPT = "command -v bash >/dev/null && exec bash -l || exec sh -l"
#: The same, starting in the folder given as the first argument (``sh -c SCRIPT sh /work``);
#: a missing folder is ignored. The folder is an argument, never part of the script text.
SHELL_SCRIPT_IN_DIR = 'cd "$1" 2>/dev/null; ' + SHELL_SCRIPT
_WORKDIR_RE = re.compile(r"^/[A-Za-z0-9_./-]{0,200}$")

_CREATE_NO_WINDOW = 0x08000000
_UNSAFE_TITLE = re.compile(r"[^A-Za-z0-9 _.@:=+-]")


@dataclass
class TerminalTarget:
    argv: list[str]      # local command that opens the session
    title: str           # window title (safe characters only)
    command: str         # human-readable form of ``argv`` (copy fallback)


def _invalid(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _safe(field: str, value: Any) -> str:
    if not isinstance(value, str) or not SAFE_TOKEN.match(value) or value.startswith("-"):
        raise _invalid(f"invalid {field} {value!r}: use letters, digits and _.@:/=+- only")
    return value


def _check_host(host: HostSpec) -> None:
    _safe("host address", host.address)
    if host.user:
        _safe("host user", host.user)
    for token in host.interactive_prefix:
        if not isinstance(token, str) or not token or "\0" in token:
            raise _invalid("invalid interactive prefix")


def safe_title(text: str) -> str:
    """Window title with only safe characters; always starts with ``cc `` (so it has a space)."""
    cleaned = _UNSAFE_TITLE.sub("", text).strip()[:60]
    if cleaned.startswith("cc "):
        return cleaned
    return f"cc {cleaned}" if cleaned else "cc terminal"


def command_string(argv: Sequence[str], *, windows: bool | None = None) -> str:
    """Render ``argv`` as one command line: Windows quoting on Windows, POSIX quoting elsewhere."""
    if windows is None:
        windows = sys.platform == "win32"
    items = [str(a) for a in argv]
    return subprocess.list2cmdline(items) if windows else shlex.join(items)


# Container CLIs whose ``exec`` needs ``-i`` and ``-t`` for an interactive shell.
_CONTAINER_CLIS = {"podman", "docker", "nerdctl"}
# ``exec`` options that take a value as the next token (``--opt=value`` does not).
_EXEC_VALUE_LONG = {"--workdir", "--user", "--env", "--env-file", "--detach-keys", "--preserve-fds"}
_EXEC_VALUE_SHORT = set("weuc")


def tty_prefix(prefix: Sequence[str]) -> list[str]:
    """``prefix`` with ``-i`` and ``-t`` ensured when it is a container ``exec`` command.

    ``podman exec -i -w /data slurmctld`` gives ``bash -l`` stdin but no terminal: bash
    starts non-interactive (no prompt, no line editing, no job control), so the window
    looks dead. Adds ``-t``/``-i`` right after ``exec`` when they are missing; any other
    prefix is returned unchanged.
    """
    tokens = [str(p) for p in prefix]
    if not tokens:
        return tokens
    cli = re.sub(r"\.exe$", "", os.path.basename(tokens[0].replace("\\", "/")).lower())
    if cli not in _CONTAINER_CLIS or "exec" not in tokens[1:]:
        return tokens
    pos = tokens.index("exec", 1)
    has_t = has_i = False
    i = pos + 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--" or not tok.startswith("-") or tok == "-":
            break                                     # the container name (or a command)
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            has_t = has_t or name == "--tty"
            has_i = has_i or name == "--interactive"
            if name in _EXEC_VALUE_LONG and "=" not in tok:
                i += 1
        else:
            letters = tok[1:]
            has_t = has_t or "t" in letters
            has_i = has_i or "i" in letters
            if letters[-1] in _EXEC_VALUE_SHORT:
                i += 1
        i += 1
    add = [flag for flag, have in (("-i", has_i), ("-t", has_t)) if not have]
    return tokens[:pos + 1] + add + tokens[pos + 1:]


# --------------------------------------------------------------------------- remote argv


def shell_workdir(workdir: object) -> str | None:
    """``workdir`` when it is a plain absolute folder path that is safe to pass, else ``None``."""
    if (isinstance(workdir, str) and _WORKDIR_RE.match(workdir) and SAFE_TOKEN.match(workdir)
            and ".." not in workdir.split("/")):
        return workdir
    return None


def instance_remote_argv(instance: InstanceRecord, host: HostSpec,
                         workdir: str | None = None) -> list[str]:
    _check_host(host)
    if instance.host_id != host.id:
        raise _invalid(f"instance {instance.id} does not belong to host {host.id}")
    if instance.status != "running":
        raise _invalid(f"instance {instance.id} is {instance.status}, not running")
    try:
        name = runtimes.container_name(instance.id)
        _safe("container name", name)
        rt = runtimes.runtime_for(instance.runtime)
        folder = shell_workdir(workdir)
        shell = (["sh", "-c", SHELL_SCRIPT_IN_DIR, "sh", folder] if folder
                 else ["sh", "-c", SHELL_SCRIPT])
        exec_cmd = rt.exec_argv(name, shell, tty=True)
    except ValueError as exc:
        raise _invalid(str(exc)) from exc
    return [*tty_prefix(host.interactive_prefix), *exec_cmd]


def host_remote_argv(host: HostSpec) -> list[str]:
    if host.kind != "slurm":
        raise _invalid(f"a host terminal is only available for slurm hosts, not {host.kind}")
    _check_host(host)
    return [*tty_prefix(host.interactive_prefix), "bash", "-l"]


def job_remote_argv(job: JobRecord, host: HostSpec) -> list[str]:
    _check_host(host)
    if job.host_id != host.id:
        raise _invalid(f"job {job.id} does not belong to host {host.id}")
    if job.state != "RUNNING":
        raise _invalid(f"job {job.id} is {job.state}, not RUNNING")
    if not job.slurm_job_id:
        raise _invalid(f"job {job.id} has no Slurm job id yet")
    jid = _safe("job id", str(job.slurm_job_id))
    return [*tty_prefix(host.interactive_prefix), "srun", f"--jobid={jid}", "--overlap", "--pty", "bash"]


# --------------------------------------------------------------------------- local argv


def _local_argv(remote: list[str], host: HostSpec, factory: TransportFactory) -> list[str]:
    transport = factory.for_host(host, prefixed=False)  # interactive prefix is already in `remote`
    try:
        return [str(a) for a in transport.local_argv(remote, tty=True)]
    except ValueError as exc:
        raise _invalid(str(exc)) from exc


def _target(remote: list[str], host: HostSpec, factory: TransportFactory, title: str) -> TerminalTarget:
    argv = _local_argv(remote, host, factory)
    return TerminalTarget(argv=argv, title=safe_title(title), command=command_string(argv))


def instance_terminal(instance: InstanceRecord, host: HostSpec,
                      factory: TransportFactory, workdir: str | None = None) -> TerminalTarget:
    """Terminal for an environment; the shell starts in ``workdir`` (the template's folder)."""
    return _target(instance_remote_argv(instance, host, workdir), host, factory,
                   instance.name or instance.id)


def host_terminal(host: HostSpec, factory: TransportFactory) -> TerminalTarget:
    return _target(host_remote_argv(host), host, factory, host.id)


def job_terminal(job: JobRecord, host: HostSpec, factory: TransportFactory) -> TerminalTarget:
    return _target(job_remote_argv(job, host), host, factory, f"job {job.id}")


# --------------------------------------------------------------------------- launch

#: Message shown (and the wait point) when the session ends with an error.
_PRESS_ENTER = "Press Enter to close"
_SCRIPT_MAX_AGE_S = 24 * 3600
_SCRIPT_DIR_NAME = "cloud-compute-terminals"

#: Shown (as the launch error) when a Linux Hermes process has no display variables.
NO_DESKTOP_SESSION = ("No desktop session is available to this Hermes process, so the command "
                      "is shown instead.")

# ``sh -c`` body for Linux: the real command arrives as positional arguments (no quoting).
_POSIX_WRAPPER = (
    '"$@"; status=$?; '
    'if [ "$status" -ne 0 ]; then '
    'printf \'\\nThe session ended with an error (exit code %s).\\n\' "$status"; '
    f'printf \'{_PRESS_ENTER} \'; read -r _; fi; exit "$status"'
)

# Linux terminal emulators, in the order tried: name -> (title flags builder, run flags).
# The run flags precede the command, which is passed as separate argv items (never one
# shell string built from user values).
#
# ``xdg-terminal-exec`` (the freedesktop standard, honours the user's choice) comes first, then
# the emulators a user installs on purpose; the desktop defaults a user may not even know about
# (ptyxis on Fedora, kgx = GNOME Console) come after them.
#
# ptyxis: ``ptyxis --new-window -T TITLE -- PROGRAM ARGS...`` (verified on Fedora 43: ``--``
# takes the program as separate arguments, ``-T`` sets the window title; the plain ``-e`` form
# runs but gives no title). kgx: ``kgx -- PROGRAM ARGS...`` (gnome-terminal compatible; kgx has
# no title option). foot: ``foot --title=T -- PROGRAM ARGS...``. xdg-terminal-exec takes the
# program directly and has no title option. Adjust the two ptyxis constants if a newer ptyxis
# changes its flags.
_PTYXIS_TITLE: Callable[[str], list[str]] = lambda t: ["--new-window", "-T", t]  # noqa: E731
_PTYXIS_RUN: list[str] = ["--"]
_LINUX_TERMINALS: list[tuple[str, Callable[[str], list[str]], list[str]]] = [
    ("xdg-terminal-exec", lambda t: [], []),
    ("x-terminal-emulator", lambda t: [], ["-e"]),
    ("gnome-terminal", lambda t: [f"--title={t}"], ["--"]),
    ("konsole", lambda t: [], ["-e"]),
    ("xfce4-terminal", lambda t: [f"--title={t}"], ["-x"]),
    ("kitty", lambda t: ["--title", t], []),
    ("alacritty", lambda t: ["--title", t], ["-e"]),
    ("foot", lambda t: [f"--title={t}"], ["--"]),
    ("wezterm", lambda t: [], ["start", "--"]),
    ("ptyxis", _PTYXIS_TITLE, _PTYXIS_RUN),
    ("kgx", lambda t: [], ["--"]),
    ("xterm", lambda t: ["-T", t], ["-e"]),
]
_LINUX_BY_NAME = {name: (title, flags) for name, title, flags in _LINUX_TERMINALS}


def _wt_arg(arg: str) -> str:
    # wt.exe treats an unescaped ';' as a command separator.
    return arg.replace(";", "\\;")


def _result(command: str, *, launched: bool = False, method: str = "none",
            error: str | None = None) -> TerminalLaunch:
    return TerminalLaunch(launched=launched, method=method, command_preview=command,  # type: ignore[arg-type]
                          copy_command=command, error=error)


def _script_dir(tmp_dir: str | os.PathLike | None) -> str:
    """A private (0700, owned by this user) directory for the launcher scripts."""
    base = os.fspath(tmp_dir) if tmp_dir is not None else tempfile.gettempdir()
    path = os.path.join(base, _SCRIPT_DIR_NAME)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if hasattr(os, "getuid"):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(f"{path} is not a private directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(path, 0o700)
    return path


def _clean_old_scripts(directory: str, now: float | None = None) -> None:
    """Remove launcher scripts older than a day; best effort."""
    cutoff = (time.time() if now is None else now) - _SCRIPT_MAX_AGE_S
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith("cc-"):
            continue
        full = os.path.join(directory, name)
        try:
            if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                os.remove(full)
        except OSError:
            pass


def _write_script(directory: str, suffix: str, text: str, *, newline: str = "\n") -> str:
    fd, path = tempfile.mkstemp(prefix="cc-", suffix=suffix, dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _batch_script(argv: Sequence[str], title: str) -> str | None:
    """Windows ``.cmd`` text that runs ``argv`` and waits for Enter on an error; ``None`` if unsafe.

    Arguments are double-quoted with ``%`` doubled. Arguments with a double quote, a control
    character or non-ASCII text cannot be quoted reliably for cmd, so those launch directly.
    """
    parts = []
    for arg in argv:
        if not arg.isascii() or '"' in arg or any(ord(c) < 32 for c in arg):
            return None
        parts.append('"' + arg.replace("%", "%%") + '"')
    return "\r\n".join([
        "@echo off",
        f"title {title}",
        " ".join(parts),
        'set "CC_RC=%errorlevel%"',
        'if not "%CC_RC%"=="0" (',
        "  echo.",
        "  echo The session ended with an error ^(exit code %CC_RC%^).",
        f'  set /p "CC_WAIT={_PRESS_ENTER} "',
        ")",
        "exit /b %CC_RC%",
        "",
    ])


def _posix_script(argv: Sequence[str], title: str) -> str:
    """macOS ``.command`` text: runs ``argv``, waits for Enter on an error, removes itself."""
    return "\n".join([
        "#!/bin/sh",
        "printf '\\033]0;%s\\007' " + shlex.quote(title),
        shlex.join(list(argv)),
        'status=$?',
        'if [ "$status" -ne 0 ]; then',
        '  printf \'\\nThe session ended with an error (exit code %s).\\n\' "$status"',
        f"  printf '{_PRESS_ENTER} '",
        "  read -r _",
        "fi",
        'rm -f -- "$0"',
        'exit "$status"',
        "",
    ])


def _spawn(popen: Callable[..., Any], full: Any, *, windows: bool,
           extra_env: dict[str, str] | None = None) -> None:
    env = child_env()
    if extra_env:
        env.update(extra_env)                 # the desktop session found for this launch only
    kwargs: dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True, shell=False,
                                  env=env)
    if windows:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    _reap_later(popen(full, **kwargs))


def _reap_later(child: Any) -> None:
    """Wait for a launcher child in the background, so a closed window leaves no zombie.

    The launcher child (a terminal emulator, ``open``, ``cmd /c start``) is a child of this
    process; until someone waits for it, it stays defunct after it exits. A daemon thread
    waits, so the request that opened the window is not held up and nothing keeps this
    process alive.
    """
    wait = getattr(child, "wait", None)
    if not callable(wait):
        return

    def reap() -> None:
        try:
            wait()
        except Exception:  # noqa: BLE001 - already reaped elsewhere, or the handle is gone
            pass

    try:
        threading.Thread(target=reap, name="cc-terminal-reap", daemon=True).start()
    except RuntimeError:                      # interpreter shutting down: nothing to reap for
        pass


def _launch_windows(argv: list[str], title: str, command: str, prefer: str,
                    which: Callable[[str], str | None], popen: Callable[..., Any],
                    tmp_dir: str | os.PathLike | None) -> TerminalLaunch:
    methods = ["cmd", "wt"] if prefer == "cmd" else ["wt", "cmd"]
    # A wrapper script keeps the window open on an error; without one (unquotable argv or an
    # unwritable temp dir) the argv runs directly and the window may close at once.
    run: list[str] = argv
    text = _batch_script(argv, title) if '"' not in title else None
    if text is not None:
        try:
            directory = _script_dir(tmp_dir)
            _clean_old_scripts(directory)
            run = [_write_script(directory, ".cmd", text, newline="")]
        except OSError:
            run = argv
    errors: list[str] = []
    for method in methods:
        if method == "wt":
            exe = which("wt.exe") or which("wt")
            if not exe:
                errors.append("Windows Terminal (wt.exe) not found")
                continue
            full: Any = [exe, "new-tab", "--title", title, "--suppressApplicationTitle",
                         *[_wt_arg(a) for a in run]]
        else:
            exe = which("cmd.exe") or which("cmd")
            if not exe:
                errors.append("cmd.exe not found")
                continue
            if '"' in title:  # safe_title never yields one; cmd would misparse it
                errors.append("unsafe window title")
                continue
            # ``start`` takes only a *quoted* first argument as the window title, which an
            # argv list cannot express (list2cmdline leaves a space-free title bare).
            full = (f'{subprocess.list2cmdline([exe])} /c start "{title}" '
                    f"{subprocess.list2cmdline(run)}")
        try:
            _spawn(popen, full, windows=True)
        except OSError as exc:
            errors.append(f"could not start {method}: {exc}")
            continue
        return _result(command, launched=True, method=method)
    return _result(command, error="; ".join(errors) or "no terminal available")


def _launch_macos(argv: list[str], title: str, command: str,
                  which: Callable[[str], str | None], popen: Callable[..., Any],
                  tmp_dir: str | os.PathLike | None) -> TerminalLaunch:
    opener = which("open") or ("/usr/bin/open" if os.path.exists("/usr/bin/open") else None)
    if not opener:
        return _result(command, error="the macOS 'open' command was not found; "
                                      "copy the command instead")
    try:
        directory = _script_dir(tmp_dir)
        _clean_old_scripts(directory)
        path = _write_script(directory, ".command", _posix_script(argv, title))
    except OSError as exc:
        return _result(command, error=f"could not prepare the terminal script: {exc}")
    try:
        _spawn(popen, [opener, "-a", "Terminal", path], windows=False)
    except OSError as exc:
        return _result(command, error=f"could not start Terminal: {exc}")
    return _result(command, launched=True, method="macos")


# --------------------------------------------------------------------------- desktop session

_WAYLAND_SOCKET = re.compile(r"^wayland-\d+$")
_X_SOCKET = re.compile(r"^X(\d{1,3})$")
_X_DISPLAY = re.compile(r"^:\d{1,3}(\.\d{1,2})?$")
_WAYLAND_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_X11_DIR = "/tmp/.X11-unix"
_SESSION_KEYS = ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                 "XAUTHORITY")


def _owned(path: str, uid: int, kind: str, lstat: Callable[[str], Any]) -> bool:
    """``path`` exists, is not a symlink, belongs to ``uid`` and is a ``dir``/``sock``/``file``."""
    try:
        info = lstat(path)
    except OSError:
        return False
    check = {"dir": stat.S_ISDIR, "sock": stat.S_ISSOCK, "file": stat.S_ISREG}[kind]
    return check(info.st_mode) and info.st_uid == uid


def _systemd_user_env(runtime: str | None, runner: Callable[..., Any]) -> dict[str, str]:
    """Variables printed by ``systemctl --user show-environment`` (empty when unavailable)."""
    env = child_env()
    if runtime:
        env["XDG_RUNTIME_DIR"] = runtime
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    try:
        res = runner(["systemctl", "--user", "show-environment"], capture_output=True, text=True,
                     timeout=5, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {}
    if getattr(res, "returncode", 1) != 0:
        return {}
    out: dict[str, str] = {}
    for line in str(res.stdout or "").splitlines():
        name, sep, value = line.partition("=")
        if sep and name in _SESSION_KEYS:
            out[name] = value.strip("'") if value.startswith("'") and value.endswith("'") else value
    return out


def discover_desktop_session(environ: dict[str, str], *, uid: int | None = None,
                             listdir: Callable[[str], list[str]] = os.listdir,
                             lstat: Callable[[str], Any] = os.lstat,
                             runner: Callable[..., Any] = subprocess.run,
                             home: str | None = None) -> dict[str, str]:
    """Find the graphical session of *this user* when the process has no display variables.

    A gateway started over ssh or as a service has neither ``DISPLAY`` nor ``WAYLAND_DISPLAY``,
    yet the user is usually logged in on the machine. Looked at, in order:

    1. a ``wayland-N`` socket in ``$XDG_RUNTIME_DIR`` (default ``/run/user/<uid>``);
    2. an X11 socket ``/tmp/.X11-unix/X<n>`` together with an X authority file of this user;
    3. the variables of ``systemctl --user show-environment``.

    Only files and directories owned by the current user are used, never a symlink, so another
    user's session is never picked. Returns the variables to add to the launched terminal
    (``WAYLAND_DISPLAY`` or ``DISPLAY``, plus ``XDG_RUNTIME_DIR``, ``DBUS_SESSION_BUS_ADDRESS``
    and ``XAUTHORITY`` when they are missing and found), or ``{}`` when nothing was found.
    Nothing in this process's environment is changed.
    """
    if uid is None:
        if not hasattr(os, "getuid"):
            return {}
        uid = os.getuid()
    if home is None:
        home = environ.get("HOME") or os.path.expanduser("~")
    runtime: str | None = None
    for candidate in (environ.get("XDG_RUNTIME_DIR"), f"/run/user/{uid}"):
        if candidate and candidate.startswith("/") and _owned(candidate, uid, "dir", lstat):
            runtime = candidate.rstrip("/") or "/"
            break
    found: dict[str, str] = {}

    def add_bus() -> None:
        if runtime and not environ.get("DBUS_SESSION_BUS_ADDRESS") \
                and _owned(f"{runtime}/bus", uid, "sock", lstat):
            found["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"

    systemd: dict[str, str] | None = None

    def systemd_env() -> dict[str, str]:
        nonlocal systemd
        if systemd is None:
            systemd = _systemd_user_env(runtime, runner)
        return systemd

    # 1. Wayland
    if runtime:
        try:
            names = sorted(n for n in listdir(runtime) if _WAYLAND_SOCKET.match(n)
                           and _owned(f"{runtime}/{n}", uid, "sock", lstat))
        except OSError:
            names = []
        if names:
            pick = names[0]
            if len(names) > 1 and systemd_env().get("WAYLAND_DISPLAY") in names:
                pick = systemd_env()["WAYLAND_DISPLAY"]
            found.update(WAYLAND_DISPLAY=pick, XDG_RUNTIME_DIR=runtime)
            add_bus()
            return found
    # 2. X11 with an authority file of this user
    try:
        sockets = sorted((n for n in listdir(_X11_DIR) if _X_SOCKET.match(n)
                          and _owned(f"{_X11_DIR}/{n}", uid, "sock", lstat)),
                         key=lambda n: int(n[1:]))
    except OSError:
        sockets = []
    if sockets:
        authority = None
        given = environ.get("XAUTHORITY")
        options = [given] if given else []
        options.append(f"{home}/.Xauthority")
        if runtime:
            try:
                options += [f"{runtime}/{n}" for n in sorted(listdir(runtime))
                            if n.startswith((".mutter-Xwaylandauth.", "xauth_"))]
            except OSError:
                pass
        for option in options:
            if option and _owned(option, uid, "file", lstat):
                authority = option
                break
        if authority:
            found["DISPLAY"] = ":" + sockets[0][1:]
            if authority != given:
                found["XAUTHORITY"] = authority
            if runtime and not environ.get("XDG_RUNTIME_DIR"):
                found["XDG_RUNTIME_DIR"] = runtime
            add_bus()
            return found
    # 3. what the user's systemd manager exports
    values = systemd_env()
    wayland, x_display = values.get("WAYLAND_DISPLAY", ""), values.get("DISPLAY", "")
    if wayland and _WAYLAND_NAME.match(wayland) and runtime \
            and _owned(f"{runtime}/{wayland}", uid, "sock", lstat):
        found["WAYLAND_DISPLAY"] = wayland
    elif x_display and _X_DISPLAY.match(x_display):
        found["DISPLAY"] = x_display
        auth = values.get("XAUTHORITY", "")
        if auth.startswith("/") and _owned(auth, uid, "file", lstat) and not environ.get("XAUTHORITY"):
            found["XAUTHORITY"] = auth
    if not found:
        return {}
    if runtime and not environ.get("XDG_RUNTIME_DIR"):
        found["XDG_RUNTIME_DIR"] = runtime
    add_bus()
    return found


def _linux_candidates(which: Callable[[str], str | None],
                      environ: dict[str, str]) -> list[tuple[str, str]]:
    """(name, path) of the terminal emulators found, in the order to try."""
    found: list[tuple[str, str]] = []
    custom = (environ.get("TERMINAL") or "").strip()
    if custom and SAFE_TOKEN.match(custom) and not custom.startswith("-"):
        path = which(custom)
        if path:
            found.append((os.path.basename(custom), path))
    for name, _title, _flags in _LINUX_TERMINALS:
        path = which(name)
        if path:
            found.append((name, path))
    return found


def _launch_linux(argv: list[str], title: str, command: str,
                  which: Callable[[str], str | None], popen: Callable[..., Any],
                  environ: dict[str, str],
                  discover: Callable[[dict[str, str]], dict[str, str]] | None = None
                  ) -> TerminalLaunch:
    extra: dict[str, str] = {}
    if not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
        # A gateway started over ssh or as a service: look for this user's desktop session.
        try:
            extra = dict(discover(environ)) if discover is not None else {}
        except Exception as exc:  # noqa: BLE001 - discovery never breaks the fallback
            log.debug("desktop session discovery failed: %s", type(exc).__name__)
            extra = {}
        if not (extra.get("DISPLAY") or extra.get("WAYLAND_DISPLAY")):
            return _result(command, error=NO_DESKTOP_SESSION)
    candidates = _linux_candidates(which, environ)
    if not candidates:
        return _result(command, error="no terminal emulator was found (set the TERMINAL "
                                      "environment variable or install one such as "
                                      "gnome-terminal or xterm); copy the command instead")
    errors: list[str] = []
    for name, path in candidates:
        title_flags, run_flags = _LINUX_BY_NAME.get(name, (lambda t: [], ["-e"]))
        full = [path, *title_flags(title), *run_flags, "sh", "-c", _POSIX_WRAPPER,
                "cc-terminal", *argv]
        try:
            _spawn(popen, full, windows=False, extra_env=extra)
        except OSError as exc:
            errors.append(f"could not start {name}: {exc}")
            continue
        return _result(command, launched=True, method="linux")
    return _result(command, error="; ".join(errors))


def launch(argv: Sequence[str], title: str, *, prefer: str = "auto",
           which: Callable[[str], str | None] = shutil.which,
           popen: Callable[..., Any] = subprocess.Popen,
           windows: bool | None = None, platform: str | None = None,
           tmp_dir: str | os.PathLike | None = None,
           environ: dict[str, str] | None = None,
           discover: Callable[[dict[str, str]], dict[str, str]] | None = None) -> TerminalLaunch:
    """Open ``argv`` in a new terminal window, or report how to run it by hand.

    The window depends on the platform (see the module docstring): Windows tries
    ``wt.exe`` then ``cmd.exe /c start`` (reversed for ``prefer="cmd"``), macOS opens
    Terminal.app with a temporary ``.command`` file, Linux tries the common terminal
    emulators. ``prefer="auto"`` (the default setting) follows the platform;
    ``"wt"``/``"cmd"``/``"macos"``/``"linux"`` behave like ``"auto"`` on other platforms.
    With ``prefer="copy"``, or when nothing works, nothing is launched and the result has
    ``method="none"`` with ``copy_command`` filled in (and ``error`` set unless copying
    was asked for). On Linux without ``DISPLAY``/``WAYLAND_DISPLAY`` the desktop session of the
    same user is looked for (:func:`discover_desktop_session`) and used for the launched
    terminal only. ``which``, ``popen``, ``platform`` (a ``sys.platform`` value),
    ``windows`` (shortcut for ``platform``), ``tmp_dir`` and ``environ`` can be replaced
    for tests.

    A launcher returning 0 does not mean the shell connected, so ``launched=True`` means
    "the launcher started". Call this only for an explicit user action (the new window
    takes focus). Closing the window does not stop anything running remotely, such as
    containers. The ``wt`` tab keeps the given title (``--suppressApplicationTitle``); the
    ``cmd`` fallback builds its command line as a string so that ``start`` sees a quoted
    title. Child stdio is ``DEVNULL`` (pipes would keep ``cmd`` waiting on inherited
    handles). ``argv`` should come from the transport's ``local_argv``, which already
    resolves the ssh binary.
    """
    if platform is None:
        if windows is None:
            platform = sys.platform
        else:
            platform = "win32" if windows else (sys.platform if sys.platform != "win32" else "linux")
    is_windows = platform.startswith("win")
    argv = [str(a) for a in argv]
    title = safe_title(title)
    command = command_string(argv, windows=is_windows)
    if prefer == "copy":
        return _result(command)
    if is_windows:
        return _launch_windows(argv, title, command, prefer, which, popen, tmp_dir)
    if platform == "darwin":
        return _launch_macos(argv, title, command, which, popen, tmp_dir)
    if platform.startswith("linux"):
        env = dict(os.environ) if environ is None else environ
        if discover is None and environ is None:   # injected environments stay hermetic
            discover = discover_desktop_session
        return _launch_linux(argv, title, command, which, popen, env, discover)
    return _result(command, error=f"opening a terminal window is not supported on {platform}; "
                                  "copy the command instead")
