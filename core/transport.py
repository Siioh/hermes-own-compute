"""Command transports: run argv on a host over SSH, locally, or through a prefix.

- :class:`SubprocessSSHTransport` starts one ``ssh`` process per command (the default).
- :class:`LocalTransport` runs commands on this computer (``kind="local"``).
- :class:`PrefixedTransport` prepends ``host.exec_prefix`` (for example
  ``podman exec -i -w /data slurmctld``) to every command.
- :class:`AsyncSSHTransport` keeps one SSH connection open per host using the
  optional ``asyncssh`` library. It is used only when ``use_asyncssh`` is enabled
  and the library is importable; otherwise the subprocess transport is used.

Rules shared by every transport:

- argv lists only; no shell on this computer. The remote argv is joined with
  :func:`shlex.join` so the remote shell sees exactly the given words.
- Output is read while the command runs and capped at ``OUTPUT_CAP_BYTES`` per stream
  (head and tail kept, the middle replaced by an "omitted" marker), so a runaway
  command cannot exhaust memory.
- ``run`` never raises for command failures: a nonzero exit code is returned as-is,
  a timeout kills the process and returns ``rc=124, timed_out=True``, and a missing
  executable returns ``rc=127``. Invalid host fields raise ``ValueError`` before any
  process starts.
- Connection multiplexing (``ControlMaster``/``ControlPath``) is never used: it is
  not supported by the Windows OpenSSH client.
- ``run_stream`` moves binary data (file transfers) without holding it in memory:
  stdout goes to a ``sink`` callable chunk by chunk, or a ``source`` callable writes
  stdin. A byte cap kills the process tree as soon as it is crossed.

Standard library only; ``asyncssh`` is imported lazily.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import ntpath
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol, Sequence, runtime_checkable

from .config import Config
from .interfaces import NO_MACHINE, MachineNotRunning
from .models import ExecResult, HostSpec
from .runtimes import RUNTIME_NAMES, find_runtime

__all__ = [
    "Transport", "LocalTransport", "SubprocessSSHTransport", "PrefixedTransport",
    "AsyncSSHTransport", "TransportFactory", "resolve_ssh_binary", "ssh_base_args",
    "validate_host", "validate_forward", "SAFE_TOKEN", "provide", "child_env",
    "StreamResult", "STREAM_CHUNK_BYTES",
]

log = logging.getLogger(__name__)

#: Allowed characters for host addresses, users, jump hosts and container ids.
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.@:/=+-]+$")

#: Names (case-insensitive) of variables that hold secrets and must not reach a child process.
_SECRET_ENV_NAME = re.compile(
    r"(?:(?:^|_)(?:API_?KEY|TOKEN|PAT|PASSWD)$)|SECRET|PASSWORD", re.IGNORECASE)
_SECRET_ENV_EXACT = frozenset({"CC_AGENT_API_KEY", "LLAMA_SWAP_API_KEY", "HF_TOKEN"})
#: Never stripped, even if a future pattern would match: what ssh and the OS need to work.
_KEEP_ENV = frozenset({"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP",
                       "SSH_AUTH_SOCK", "SSH_AGENT_PID"})


def child_env(base: "dict[str, str] | None" = None) -> dict[str, str]:
    """A copy of the environment for a local child process, without secret-looking variables.

    Drops names such as ``*_API_KEY``, ``*_TOKEN``, ``*_PAT``, ``*SECRET*``, ``*PASSWORD*``,
    ``CC_AGENT_API_KEY``, ``LLAMA_SWAP_API_KEY`` and ``HF_TOKEN``, so the gateway's keys never
    reach ssh, podman, docker, terminal windows or the process-listing tools. Everything else
    (``PATH``, ``HOME``, ``SSH_AUTH_SOCK``, the Windows system variables, ...) is kept, so ssh
    and its agent keep working. ``base`` defaults to ``os.environ``.
    """
    src = os.environ if base is None else base
    out: dict[str, str] = {}
    for name, value in src.items():
        upper = name.upper()
        if upper not in _KEEP_ENV and (upper in _SECRET_ENV_EXACT
                                       or _SECRET_ENV_NAME.search(name)):
            continue
        out[name] = value
    return out


#: Local port-forward spec accepted by ``local_argv``: must bind the loopback address.
_FORWARD = re.compile(r"^127\.0\.0\.1:(\d{1,5}):([A-Za-z0-9_.-]+|\[[0-9A-Fa-f:.]+\]):(\d{1,5})$")

RC_TIMEOUT = 124
RC_NOT_FOUND = 127
SSH_G_TIMEOUT = 5.0
_KILL_GRACE_S = 5.0
_CREATE_NO_WINDOW = 0x08000000
#: Most output kept per stream (stdout, stderr) while a command runs: a quarter from the
#: start, the rest from the end. Anything in between is dropped and marked.
OUTPUT_CAP_BYTES = 1024 * 1024


def _is_windows() -> bool:
    return os.name == "nt"


# --------------------------------------------------------------------------- validation


def _check_token(field: str, value: str) -> None:
    if not isinstance(value, str) or not SAFE_TOKEN.match(value) or value.startswith("-"):
        raise ValueError(f"invalid {field} {value!r}: use letters, digits and _.@:/=+- only, "
                         f"not starting with '-'")


def _check_path(field: str, value: str) -> None:
    if (not isinstance(value, str) or not value.strip() or value.startswith("-")
            or any(c in value for c in "\r\n\0")):
        raise ValueError(f"invalid {field} {value!r}: must be a file path without line breaks, "
                         f"not starting with '-'")


def validate_host(host: HostSpec) -> None:
    """Raise ``ValueError`` if any field that reaches an argv is unsafe."""
    _check_token("address", host.address)
    if host.user:
        _check_token("user", host.user)
    if host.jump_host:
        _check_token("jump_host", host.jump_host)
    if host.identity_file:
        _check_path("identity_file", host.identity_file)
    port = host.port
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"invalid port {port!r}")


def validate_forward(spec: str) -> str:
    """Check a ``127.0.0.1:<local>:<host>:<remote>`` forward spec and return it."""
    m = _FORWARD.match(spec) if isinstance(spec, str) else None
    if not m or not all(1 <= int(p) <= 65535 for p in (m.group(1), m.group(3))):
        raise ValueError(f"invalid forward {spec!r}: expected 127.0.0.1:<local-port>:<host>:<port>")
    return spec


# --------------------------------------------------------------------------- ssh binary + argv


def _git_roots() -> list[Path]:
    """Candidate Git for Windows install roots (derived from ``git`` on PATH, then defaults).

    The roots are Windows paths; when Windows is only being assumed on another OS (tests),
    they are built with Windows path rules so ``.parent`` and ``/`` still work.
    """
    win_path = PureWindowsPath if os.name != "nt" else Path
    roots: list[Path] = []
    git = shutil.which("git")
    if git:
        p = win_path(git)
        if os.name == "nt":
            p = p.resolve()
        p = p.parent
        # git.exe lives in <root>/cmd, <root>/bin or <root>/mingw64/bin
        for parent in (p, p.parent, p.parent.parent):
            roots.append(parent)
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            roots.append(win_path(base) / "Git")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(win_path(local) / "Programs" / "Git")
    roots += [win_path(r"C:\Program Files\Git"), win_path(r"C:\Program Files (x86)\Git")]
    seen: set[str] = set()
    unique = []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def resolve_ssh_binary(config: Config | None = None) -> str:
    """The ssh executable to use.

    Order: ``config.ssh_path`` (if it exists) → ``%SystemRoot%\\System32\\OpenSSH\\ssh.exe``
    → Git for Windows ``usr\\bin\\ssh.exe`` → ``ssh`` on PATH → ``"ssh"``.
    """
    configured = getattr(config, "ssh_path", "") if config is not None else ""
    if configured and os.path.exists(configured):
        return configured
    if _is_windows():
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        candidate = ntpath.join(system_root, "System32", "OpenSSH", "ssh.exe")
        if os.path.exists(candidate):
            return candidate
        for root in _git_roots():
            candidate = str(root / "usr" / "bin" / "ssh.exe")
            if os.path.exists(candidate):
                return candidate
    found = shutil.which("ssh")
    return found or "ssh"


def _find_sh() -> str | None:
    """A POSIX ``sh`` for local scripts (Git for Windows ``usr\\bin\\sh.exe`` on Windows)."""
    if _is_windows():
        for root in _git_roots():
            for rel in (("usr", "bin", "sh.exe"), ("bin", "sh.exe")):
                candidate = str(root.joinpath(*rel))
                if os.path.exists(candidate):
                    return candidate
    return shutil.which("sh")


def _sh_tool_dirs(sh: str) -> list[str]:
    """Folders next to a Windows ``sh.exe`` that hold the tools its scripts call.

    Git for Windows keeps ``sh.exe`` beside ``uname``, ``grep``, ``df`` and the rest; a
    gateway started from a shortcut does not have that folder on PATH. Empty on other
    platforms, where ``sh`` already finds them.
    """
    if not _is_windows():
        return []
    folder = os.path.dirname(sh)
    root = os.path.dirname(folder)
    dirs = [folder]
    if os.path.basename(folder).lower() == "bin" and os.path.basename(root).lower() == "usr":
        dirs.append(os.path.join(os.path.dirname(root), "bin"))
    return dirs


def _env_with_path(front: Sequence[str]) -> dict[str, str]:
    """:func:`child_env` with ``front`` folders put before the existing PATH."""
    env = child_env()
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    folders = [d for d in front if d]
    current = env.get(key, "")
    env[key] = os.pathsep.join(folders + ([current] if current else []))
    return env


def ssh_base_args(host: HostSpec, *, batch: bool = True) -> list[str]:
    """Common ssh options for ``host`` (validated first).

    ``batch=False`` drops ``BatchMode=yes`` for interactive sessions, where the user
    may need to type a passphrase or password.
    """
    if host.kind == "ec2" and (not host.address or host.address == NO_MACHINE):
        raise MachineNotRunning(host.name or host.id)
    if host.kind == "ec2" and getattr(host, "ec2", None) is not None:
        # The machine's login: the key pair generated for this host and the image's user.
        import dataclasses as _dc
        host = _dc.replace(host, identity_file=host.ec2.key_file or host.identity_file,
                           user=host.ec2.ssh_user or host.user or "ec2-user", jump_host=None)
    validate_host(host)
    args: list[str] = []
    if batch:
        args += ["-o", "BatchMode=yes"]
    strict, extra = _ec2_options(host) if host.kind == "ec2" else ("accept-new", [])
    args += ["-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
             "-o", f"StrictHostKeyChecking={strict}"]
    if host.port != 22:
        args += ["-p", str(host.port)]
    if host.identity_file:
        # Only the configured key: without IdentitiesOnly ssh also offers the agent's and the
        # default keys, so a wrong key file could still log in (or hit the server's try limit).
        args += ["-i", host.identity_file, "-o", "IdentitiesOnly=yes"]
    if host.jump_host:
        args += ["-J", host.jump_host]
    if host.user:
        args += ["-l", host.user]
    if extra:
        args += extra
    return args


def _ec2_options(host: HostSpec) -> tuple[str, list[str]]:
    """``(StrictHostKeyChecking value, extra options)`` for an EC2 instance.

    The plugin's own known-hosts file is used (never the user's), host keys are stored under
    the instance id (``HostKeyAlias``) so a new public address after a restart is still the same
    machine, only the host's generated key is offered, and checking is strict once the
    instance's key is pinned (first use otherwise, for that instance only).
    """
    ec2 = getattr(host, "ec2", None)
    known = getattr(ec2, "known_hosts_file", None)
    alias = getattr(ec2, "instance_id", None)
    extra = ["-o", "IdentitiesOnly=yes", "-o", "GlobalKnownHostsFile=" + os.devnull]
    pinned = False
    if known:
        _check_path("known_hosts_file", known)
        extra += ["-o", "UserKnownHostsFile=" + known]
    if alias:
        _check_token("host_key_alias", alias)
        extra += ["-o", "HostKeyAlias=" + alias]
        if known:
            try:
                with open(known, encoding="utf-8") as fh:
                    pinned = any(line.split(" ", 1)[0] == alias for line in fh)
            except OSError:
                pinned = False
    return ("yes" if pinned else "accept-new"), extra


# --------------------------------------------------------------------------- process runner


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


class _Capture:
    """Bounded copy of one output stream: the first and the last bytes, plus a count.

    Keeps at most ``max_bytes`` (a quarter as head, the rest as a rolling tail), so a
    command that prints without end uses constant memory. The tail matters most:
    callers cut the output to its last few kilobytes anyway.
    """

    def __init__(self, max_bytes: int):
        self.max_bytes = max(2, int(max_bytes))
        self._head_cap = max(1, self.max_bytes // 4)
        self._tail_cap = max(1, self.max_bytes - self._head_cap)
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        room = self._head_cap - len(self._head)
        if room > 0:
            self._head += data[:room]
            data = data[room:]
        if data:
            self._tail += data
            if len(self._tail) > self._tail_cap:
                del self._tail[:len(self._tail) - self._tail_cap]

    @property
    def dropped(self) -> int:
        return self.total - len(self._head) - len(self._tail)

    def text(self) -> str:
        if not self.dropped:
            return _decode(bytes(self._head) + bytes(self._tail))
        return (_decode(bytes(self._head)) + f"\n[... {self.dropped} bytes omitted ...]\n"
                + _decode(bytes(self._tail)))


def _clip(text: str, max_bytes: int) -> str:
    """Bound already-decoded output the same way :class:`_Capture` does."""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return text
    cap = _Capture(max_bytes)
    cap.feed(data)
    return cap.text()


def _pump(stream: Any, cap: _Capture) -> None:
    """Read ``stream`` to EOF into ``cap`` (runs on a daemon thread)."""
    read = getattr(stream, "read1", None) or stream.read
    try:
        while True:
            chunk = read(65536)
            if not chunk:
                break
            cap.feed(chunk)
    except (OSError, ValueError):   # pipe closed under us
        pass


def _feed_stdin(stream: Any, data: bytes) -> None:
    try:
        stream.write(data)
        stream.flush()
    except (OSError, ValueError):   # the process exited without reading it all
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _kill_tree(proc: Any) -> None:
    """Kill the process and everything it started; never raises."""
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            if _is_windows():
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], timeout=_KILL_GRACE_S,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW,
                               env=child_env())
            else:
                import signal

                os.killpg(pid, signal.SIGKILL)
        except Exception as exc:  # noqa: BLE001 - fall back to killing just the process
            log.debug("cloud-compute: killing the process tree of %s failed: %s", pid, exc)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 - already gone
        pass


def _run_process(argv: list[str], stdin: bytes | None, timeout: float,
                 max_bytes: int = OUTPUT_CAP_BYTES, env: "dict[str, str] | None" = None
                 ) -> ExecResult:
    """Run a local process without a shell and collect its output; never raises.

    stdout and stderr are read while the command runs and only ``max_bytes`` of each
    are kept (head and tail; the middle is replaced by an "omitted" marker). On a
    timeout the whole process tree is killed. ``env`` replaces the filtered default
    environment (:func:`child_env`) when given.
    """
    start = time.monotonic()
    kwargs: dict[str, Any] = {}
    if _is_windows():
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True       # own process group, so a timeout can kill the tree
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env() if env is None else env, **kwargs,
        )
    except OSError as exc:  # includes FileNotFoundError / PermissionError
        return ExecResult(rc=RC_NOT_FOUND, stdout="",
                          stderr=f"could not start {argv[0] if argv else '?'}: {exc}",
                          duration_s=time.monotonic() - start)
    out_cap, err_cap = _Capture(max_bytes), _Capture(max_bytes)
    threads = [threading.Thread(target=_pump, args=(proc.stdout, out_cap), daemon=True,
                                name="cloud-compute-out"),
               threading.Thread(target=_pump, args=(proc.stderr, err_cap), daemon=True,
                                name="cloud-compute-err")]
    if stdin is not None:
        threads.append(threading.Thread(target=_feed_stdin, args=(proc.stdin, stdin), daemon=True,
                                        name="cloud-compute-in"))
    for t in threads:
        t.start()
    deadline = start + timeout
    timed_out = False
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        for t in threads:                       # a grandchild holding the pipe counts too
            t.join(max(0.0, deadline - time.monotonic()))
        timed_out = any(t.is_alive() for t in threads)
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out:
        _kill_tree(proc)
        for t in threads:
            t.join(_KILL_GRACE_S)
        text = err_cap.text()
        note = f"timed out after {timeout:g} s"
        return ExecResult(rc=RC_TIMEOUT, stdout=out_cap.text(),
                          stderr=f"{text}\n{note}" if text else note,
                          duration_s=time.monotonic() - start, timed_out=True)
    if out_cap.dropped or err_cap.dropped:
        log.debug("cloud-compute: command output cut to %d bytes per stream (%d/%d bytes dropped)",
                  max_bytes, out_cap.dropped, err_cap.dropped)
    return ExecResult(rc=proc.returncode, stdout=out_cap.text(), stderr=err_cap.text(),
                      duration_s=time.monotonic() - start)


#: Read/write size for streamed transfers.
STREAM_CHUNK_BYTES = 256 * 1024
_STREAM_ERR_CAP = 64 * 1024


@dataclass
class StreamResult:
    """Outcome of :meth:`Transport.run_stream`.

    ``bytes`` counts what passed through the stream (stdout for a ``sink``, stdin for a
    ``source``). ``too_large`` is set when that count went over ``max_bytes``: the process
    tree was killed at that point. ``stdout`` holds a bounded copy of stdout only when no
    sink was given.
    """

    rc: int
    stderr: str
    bytes: int
    duration_s: float
    timed_out: bool = False
    too_large: bool = False
    stdout: str = ""


class _CapExceeded(Exception):
    pass


class _StdinWriter:
    """File-like ``write`` target for a ``source``: counts bytes and enforces the cap."""

    def __init__(self, stream: Any, max_bytes: int | None, state: dict):
        self._stream = stream
        self._max = max_bytes
        self._state = state

    def write(self, data: Any) -> int:
        data = bytes(data)
        if not data:
            return 0
        self._state["bytes"] += len(data)
        if self._max is not None and self._state["bytes"] > self._max:
            raise _CapExceeded()
        self._stream.write(data)
        return len(data)

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:       # the runner closes the pipe itself
        pass

    def writable(self) -> bool:
        return True


def _run_stream_process(argv: list[str], *, sink: Any = None, source: Any = None,
                        max_bytes: int | None = None, timeout: float = 3600,
                        env: "dict[str, str] | None" = None) -> StreamResult:
    """Run a local process and stream its stdout into ``sink`` or ``source`` into its stdin.

    Exactly one of ``sink`` (``callable(bytes)``) and ``source`` (``callable(writer)``,
    where ``writer.write(bytes)`` feeds stdin) is given. Nothing is buffered beyond one
    chunk. Over ``max_bytes`` the process tree is killed and ``too_large`` is set; on a
    timeout the tree is killed and ``timed_out`` is set (rc 124). An exception raised by
    ``sink`` or ``source`` kills the tree and is re-raised after the process is gone. A
    missing executable returns rc 127.
    """
    if (sink is None) == (source is None):
        raise ValueError("run_stream needs exactly one of sink and source")
    start = time.monotonic()
    kwargs: dict[str, Any] = {}
    if _is_windows():
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE if source is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env() if env is None else env, **kwargs)
    except OSError as exc:
        return StreamResult(rc=RC_NOT_FOUND, stderr=f"could not start {argv[0] if argv else '?'}: "
                            f"{exc}", bytes=0, duration_s=time.monotonic() - start)
    state: dict[str, Any] = {"bytes": 0, "too_large": False, "error": None, "killed": False}
    err_cap, out_cap = _Capture(_STREAM_ERR_CAP), _Capture(_STREAM_ERR_CAP)

    def pump_out() -> None:
        read = getattr(proc.stdout, "read1", None) or proc.stdout.read
        while True:
            try:
                chunk = read(STREAM_CHUNK_BYTES)
            except (OSError, ValueError) as exc:
                # A pipe closed by our own kill (cap, timeout, failed source) ends quietly;
                # any other read failure fails the transfer, or a stream cut short with rc 0
                # would pass as complete.
                if not (state["killed"] or state["too_large"]) and state["error"] is None:
                    state["error"] = OSError(f"reading the stream failed: {exc}")
                break
            if not chunk:
                break
            if sink is None:
                out_cap.feed(chunk)
                continue
            state["bytes"] += len(chunk)
            if max_bytes is not None and state["bytes"] > max_bytes:
                state["too_large"] = True
                _kill_tree(proc)
                break
            try:
                sink(chunk)
            except BaseException as exc:  # noqa: BLE001 - the caller's sink failed
                state["error"] = exc
                _kill_tree(proc)
                break

    def feed_in() -> None:
        try:
            source(_StdinWriter(proc.stdin, max_bytes, state))
        except _CapExceeded:
            state["too_large"] = True
            _kill_tree(proc)
        except (BrokenPipeError, ConnectionResetError):
            pass                            # the process exited early; its rc tells why
        except OSError as exc:
            if getattr(exc, "errno", None) in (22, 32):     # EINVAL/EPIPE on a closed pipe
                pass
            else:
                state["error"] = exc
                _kill_tree(proc)
        except BaseException as exc:  # noqa: BLE001 - the caller's source failed
            state["error"] = exc
            _kill_tree(proc)
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    threads = [threading.Thread(target=pump_out, daemon=True, name="cloud-compute-sout"),
               threading.Thread(target=_pump, args=(proc.stderr, err_cap), daemon=True,
                                name="cloud-compute-serr")]
    if source is not None:
        threads.append(threading.Thread(target=feed_in, daemon=True, name="cloud-compute-sin"))
    for t in threads:
        t.start()
    deadline = start + timeout
    timed_out = False
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        timed_out = any(t.is_alive() for t in threads)
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out:
        state["killed"] = True
        _kill_tree(proc)
        for t in threads:
            t.join(_KILL_GRACE_S)
    try:
        rc = proc.wait(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        rc = RC_TIMEOUT
    if state["error"] is not None:
        raise state["error"]
    err = err_cap.text()
    if timed_out and not state["too_large"]:
        note = f"timed out after {timeout:g} s"
        return StreamResult(rc=RC_TIMEOUT, stderr=f"{err}\n{note}" if err else note,
                            bytes=state["bytes"], duration_s=time.monotonic() - start,
                            timed_out=True, stdout=out_cap.text())
    return StreamResult(rc=rc if rc is not None else RC_TIMEOUT, stderr=err, bytes=state["bytes"],
                        duration_s=time.monotonic() - start, too_large=bool(state["too_large"]),
                        stdout=out_cap.text())


def _tcp_latency(hostname: str, port: int, timeout: float) -> float | None:
    start = time.perf_counter()
    try:
        sock = socket.create_connection((hostname, port), timeout=timeout)
    except (OSError, ValueError):
        return None
    try:
        return (time.perf_counter() - start) * 1000.0
    finally:
        try:
            sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- protocol


@runtime_checkable
class Transport(Protocol):
    host: HostSpec

    def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: float = 60) -> ExecResult: ...

    def run_script(self, script: str, *, timeout: float = 60) -> ExecResult: ...

    def run_stream(self, argv: list[str], *, sink: Any = None, source: Any = None,
                   max_bytes: int | None = None, timeout: float = 3600) -> StreamResult: ...

    def tcp_check(self, timeout: float = 3.0) -> float | None: ...

    def local_argv(self, remote_argv: list[str] | None, *, tty: bool = False,
                   forwards: Sequence[str] = ()) -> list[str]: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- local


class LocalTransport:
    """Runs commands on this computer (``kind="local"``); argv is executed directly.

    No shell and no POSIX tool is needed. ``docker`` and ``podman`` at the start of an
    argv are replaced by the full path of the installed binary (:meth:`runtime_binary`),
    so the container engine is found even when it is not on PATH (Docker Desktop on
    Windows, Homebrew on macOS), and the folder of that binary is put on the child's PATH
    for the helpers it calls (credential helpers, CLI plugins).
    """

    def __init__(self, host: HostSpec, runtime_paths: "dict[str, str] | None" = None):
        self.host = host
        #: explicit executable per runtime name, from settings (may be empty)
        self.runtime_paths: dict[str, str] = dict(runtime_paths or {})

    def runtime_binary(self, name: str) -> str | None:
        """Full path of the ``podman`` or ``docker`` executable, or ``None`` if not installed.

        Order: the explicit path from settings, PATH, then the usual install folders of
        this platform.
        """
        return find_runtime(name, explicit=self.runtime_paths.get(name, ""))

    def _resolve(self, argv: list[str]) -> "tuple[list[str], dict[str, str] | None]":
        """argv with a runtime name (or ``sh``) replaced by a full path, and the PATH to use.

        The second item is ``None`` when argv is unchanged (the default environment applies).
        """
        head = argv[0]
        if head in RUNTIME_NAMES:
            path = self.runtime_binary(head)
            if path and os.path.isabs(path):
                return [path, *argv[1:]], _env_with_path([os.path.dirname(path)])
        elif os.path.isabs(head) and os.path.splitext(os.path.basename(head))[0].lower() in RUNTIME_NAMES:
            return argv, _env_with_path([os.path.dirname(head)])     # already a full path
        elif head == "sh":
            sh = _find_sh()
            if sh and os.path.isabs(sh):
                return [sh, *argv[1:]], _env_with_path(_sh_tool_dirs(sh))
        return argv, None

    def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: float = 60,
            output_cap: int = OUTPUT_CAP_BYTES) -> ExecResult:
        argv = [str(a) for a in argv]
        if not argv:
            raise ValueError("empty argv")
        argv, env = self._resolve(argv)
        return _run_process(argv, stdin, timeout, output_cap, env)

    def run_script(self, script: str, *, timeout: float = 60) -> ExecResult:
        """Run ``script`` with a POSIX ``sh``. Only used for a step that needs a shell."""
        sh = _find_sh()
        if not sh:
            return ExecResult(rc=RC_NOT_FOUND, stdout="",
                              stderr="no POSIX sh found on this computer (on Windows, install Git for Windows)",
                              duration_s=0.0)
        dirs = _sh_tool_dirs(sh) if os.path.isabs(sh) else []
        return _run_process([sh, "-s"], script.encode("utf-8"), timeout,
                            env=_env_with_path(dirs) if dirs else None)

    def run_stream(self, argv: list[str], *, sink: Any = None, source: Any = None,
                   max_bytes: int | None = None, timeout: float = 3600) -> StreamResult:
        argv = [str(a) for a in argv]
        if not argv:
            raise ValueError("empty argv")
        argv, env = self._resolve(argv)
        return _run_stream_process(argv, sink=sink, source=source, max_bytes=max_bytes,
                                   timeout=timeout, env=env)

    def tcp_check(self, timeout: float = 3.0) -> float | None:
        return 0.0

    def local_argv(self, remote_argv: list[str] | None, *, tty: bool = False,
                   forwards: Sequence[str] = ()) -> list[str]:
        """The command itself (``tty`` is ignored); with no command, the user's shell.

        A leading ``docker`` or ``podman`` becomes the full path of the installed binary,
        so a terminal window opened from the gateway finds it too.
        """
        if forwards:
            raise ValueError("port forwards are not supported for local hosts")
        if remote_argv:
            argv = [str(a) for a in remote_argv]
            return self._resolve(argv)[0]
        if _is_windows():
            return [os.environ.get("COMSPEC") or "cmd.exe"]
        return [os.environ.get("SHELL") or "sh"]

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- ssh (subprocess)


def _parse_ssh_g(text: str) -> dict[str, str]:
    """``ssh -G`` output → {lowercase key: value} (first occurrence wins)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() not in values:
            values[parts[0].lower()] = parts[1].strip()
    return values


def _split_jump(jump: str) -> tuple[str, int | None]:
    """First hop of a ProxyJump value ``[user@]host[:port][,...]`` → (host, port)."""
    hop = jump.split(",", 1)[0].rsplit("@", 1)[-1]
    if hop.startswith("["):
        host, _, rest = hop[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = hop.partition(":")
    return host, int(port) if port.isdigit() else None


class SubprocessSSHTransport:
    """One ``ssh`` process per command (works with the Windows OpenSSH client and agent)."""

    def __init__(self, host: HostSpec, ssh_bin: str = "ssh"):
        validate_host(host)
        self.host = host
        self.ssh_bin = ssh_bin
        self._tcp_target: tuple[str, int] | None = None
        self._lock = threading.Lock()

    def argv(self, remote_argv: list[str]) -> list[str]:
        """Full local argv that runs ``remote_argv`` on the host."""
        return [self.ssh_bin, *ssh_base_args(self.host), self.host.address, "--",
                shlex.join([str(a) for a in remote_argv])]

    def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: float = 60,
            output_cap: int = OUTPUT_CAP_BYTES) -> ExecResult:
        if not argv:
            raise ValueError("empty argv")
        return _run_process(self.argv(argv), stdin, timeout, output_cap)

    def run_script(self, script: str, *, timeout: float = 60) -> ExecResult:
        return self.run(["sh", "-s"], stdin=script.encode("utf-8"), timeout=timeout)

    def run_stream(self, argv: list[str], *, sink: Any = None, source: Any = None,
                   max_bytes: int | None = None, timeout: float = 3600) -> StreamResult:
        if not argv:
            raise ValueError("empty argv")
        return _run_stream_process(self.argv(argv), sink=sink, source=source,
                                   max_bytes=max_bytes, timeout=timeout)

    def tcp_target(self) -> tuple[str, int]:
        """Where a TCP check should connect: from ``ssh -G`` (cached), else address/port.

        With a jump host the first hop is checked, since the target may not be
        reachable directly.
        """
        with self._lock:
            if self._tcp_target is not None:
                return self._tcp_target
        target = (self.host.address, self.host.port)
        jump = self.host.jump_host
        try:
            res = _run_process([self.ssh_bin, "-G", self.host.address], None, SSH_G_TIMEOUT)
            if res.rc == 0:
                cfg = _parse_ssh_g(res.stdout)
                name = cfg.get("hostname") or self.host.address
                port = self.host.port
                if port == 22 and cfg.get("port", "").isdigit():
                    port = int(cfg["port"])
                target = (name, port)
                if not jump and cfg.get("proxyjump", "none").lower() != "none":
                    jump = cfg["proxyjump"]
        except Exception as exc:  # noqa: BLE001 - fall back to address/port
            log.debug("ssh -G %s failed: %s", self.host.address, exc)
        if jump:
            jhost, jport = _split_jump(jump)
            if jhost:
                target = (jhost, jport or 22)
        with self._lock:
            self._tcp_target = target
        return target

    def tcp_check(self, timeout: float = 3.0) -> float | None:
        hostname, port = self.tcp_target()
        return _tcp_latency(hostname, port, timeout)

    def local_argv(self, remote_argv: list[str] | None, *, tty: bool = False,
                   forwards: Sequence[str] = ()) -> list[str]:
        """argv for a local ``ssh`` process (terminal or tunnel).

        ``tty`` adds ``-t`` and omits ``BatchMode=yes`` so the user can answer a
        passphrase or password prompt. ``forwards`` are ``127.0.0.1:<l>:<host>:<r>``
        specs; with forwards and no command, ``-N`` keeps the tunnel open.
        """
        specs = [validate_forward(f) for f in forwards]
        argv = [self.ssh_bin, *ssh_base_args(self.host, batch=not tty)]
        if tty:
            argv.append("-t")
        if specs and not remote_argv:
            argv.append("-N")
        for spec in specs:
            argv += ["-L", spec]
        argv.append(self.host.address)
        if remote_argv:
            argv += ["--", shlex.join([str(a) for a in remote_argv])]
        return argv

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- prefix


#: What a machine prints to a login while it is going down (pam_nologin / /run/nologin).
_GOING_DOWN = re.compile(r"system is going down|pam_nologin|nologin\(8\)", re.IGNORECASE)
#: The sentence that replaces it for the user.
GOING_DOWN_TEXT = ("The machine is about to stop at its time limit and no longer accepts "
                   "connections. Start it again after it has stopped to reach its files; the "
                   "disk is kept.")


def going_down_text(stderr: str) -> str:
    """``stderr`` unchanged, or the plain sentence when a login was refused because the machine
    is shutting down."""
    return GOING_DOWN_TEXT if stderr and _GOING_DOWN.search(stderr) else stderr


class _GoingDownTransport:
    """Wraps the transport of an EC2 machine: a login refused because the machine is shutting
    down (``System is going down ... pam_nologin``) is reported with a plain sentence instead
    of the raw SSH text, wherever the command came from."""

    def __init__(self, inner: Transport):
        self.inner = inner
        self.host: HostSpec = inner.host

    @staticmethod
    def _plain(res: Any) -> Any:
        text = going_down_text(getattr(res, "stderr", "") or "")
        if text != (getattr(res, "stderr", "") or ""):
            res.stderr = text
        return res

    def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: float = 60) -> ExecResult:
        return self._plain(self.inner.run(argv, stdin=stdin, timeout=timeout))

    def run_script(self, script: str, *, timeout: float = 60) -> ExecResult:
        return self._plain(self.inner.run_script(script, timeout=timeout))

    def run_stream(self, argv: list[str], *, sink: Any = None, source: Any = None,
                   max_bytes: int | None = None, timeout: float = 3600) -> StreamResult:
        return self._plain(self.inner.run_stream(argv, sink=sink, source=source,
                                                 max_bytes=max_bytes, timeout=timeout))

    def tcp_check(self, timeout: float = 3.0) -> float | None:
        return self.inner.tcp_check(timeout)

    def local_argv(self, remote_argv: list[str] | None, *, tty: bool = False,
                   forwards: Sequence[str] = ()) -> list[str]:
        return self.inner.local_argv(remote_argv, tty=tty, forwards=forwards)

    def close(self) -> None:
        self.inner.close()


class PrefixedTransport:
    """Prepends a command prefix (``host.exec_prefix``) to everything run on ``inner``."""

    def __init__(self, inner: Transport, prefix: Sequence[str]):
        self.inner = inner
        self.prefix = [str(p) for p in prefix]
        self.host: HostSpec = inner.host

    def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: float = 60) -> ExecResult:
        return self.inner.run(self.prefix + [str(a) for a in argv], stdin=stdin, timeout=timeout)

    def run_script(self, script: str, *, timeout: float = 60) -> ExecResult:
        return self.inner.run(self.prefix + ["sh", "-s"], stdin=script.encode("utf-8"), timeout=timeout)

    def run_stream(self, argv: list[str], *, sink: Any = None, source: Any = None,
                   max_bytes: int | None = None, timeout: float = 3600) -> StreamResult:
        return self.inner.run_stream(self.prefix + [str(a) for a in argv], sink=sink,
                                     source=source, max_bytes=max_bytes, timeout=timeout)

    def tcp_check(self, timeout: float = 3.0) -> float | None:
        return self.inner.tcp_check(timeout)

    def local_argv(self, remote_argv: list[str] | None, *, tty: bool = False,
                   forwards: Sequence[str] = ()) -> list[str]:
        """Prefixed command; ``tty`` uses ``host.interactive_prefix`` when set.

        A tunnel without a command (forwards only) is not prefixed.
        """
        if remote_argv is None and forwards:
            return self.inner.local_argv(None, tty=tty, forwards=forwards)
        prefix = self.prefix
        if tty and self.host.interactive_prefix:
            prefix = [str(p) for p in self.host.interactive_prefix]
        return self.inner.local_argv(prefix + [str(a) for a in (remote_argv or [])],
                                     tty=tty, forwards=forwards)

    def close(self) -> None:
        self.inner.close()


# --------------------------------------------------------------------------- asyncssh (optional)


def _import_asyncssh() -> Any | None:
    """The ``asyncssh`` module, or ``None`` when it is not installed or fails to import."""
    try:
        import asyncssh  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    return asyncssh


class _LoopThread:
    """A daemon thread running one asyncio event loop, shared by asyncssh transports."""

    _instance: "_LoopThread | None" = None
    _guard = threading.Lock()

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="cloud-compute-ssh",
                                       daemon=True)
        self.thread.start()

    @classmethod
    def get(cls) -> "_LoopThread":
        with cls._guard:
            if cls._instance is None or not cls._instance.thread.is_alive():
                cls._instance = cls()
            return cls._instance

    def submit(self, coro: Any) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


class _ConnectFailed(Exception):
    pass


class AsyncSSHTransport:
    """Keeps one SSH connection per host open with ``asyncssh`` (optional, faster).

    Uses ``~/.ssh/config`` and the default keys / agent like the ssh client. If the
    connection cannot be established (unknown host key, auth method unsupported,
    library error), the call falls back to :class:`SubprocessSSHTransport`, which
    also records a new host key (``accept-new``) so a later connection can succeed.
    ``tcp_check`` and ``local_argv`` always use the subprocess transport.
    """

    def __init__(self, host: HostSpec, ssh_bin: str = "ssh", *, asyncssh_module: Any = None):
        self.fallback = SubprocessSSHTransport(host, ssh_bin)
        self.host = host
        self._asyncssh = asyncssh_module if asyncssh_module is not None else _import_asyncssh()
        if self._asyncssh is None:
            raise ImportError("asyncssh is not installed")
        self._conn: Any = None
        self._conn_lock: asyncio.Lock | None = None
        self._runner = _LoopThread.get()

    # -- connection -------------------------------------------------------

    def _connect_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"connect_timeout": 15, "keepalive_interval": 30}
        cfg = Path.home() / ".ssh" / "config"
        if cfg.is_file():
            kw["config"] = [str(cfg)]
        if self.host.port != 22:
            kw["port"] = self.host.port
        if self.host.user:
            kw["username"] = self.host.user
        if self.host.identity_file:
            kw["client_keys"] = [self.host.identity_file]
            kw["agent_path"] = None           # only that key, as IdentitiesOnly=yes for ssh
        if self.host.jump_host:
            kw["tunnel"] = self.host.jump_host
        return kw

    async def _connection(self) -> Any:
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        async with self._conn_lock:
            if self._conn is None:
                try:
                    self._conn = await self._asyncssh.connect(self.host.address, **self._connect_kwargs())
                except Exception as exc:  # noqa: BLE001 - any connect failure falls back
                    raise _ConnectFailed(f"{type(exc).__name__}: {exc}") from exc
            return self._conn

    async def _run(self, command: str, stdin: bytes | None, timeout: float) -> ExecResult:
        conn = await self._connection()
        start = time.monotonic()
        timeout_types = tuple(t for t in (getattr(self._asyncssh, "TimeoutError", None),
                                          asyncio.TimeoutError) if isinstance(t, type))
        try:
            res = await conn.run(command, input=stdin, encoding=None, check=False, timeout=timeout)
        except timeout_types as exc:
            out = getattr(exc, "stdout", b"") or b""
            err = getattr(exc, "stderr", b"") or b""
            text = _decode(err if isinstance(err, bytes) else str(err).encode())
            note = f"timed out after {timeout:g} s"
            return ExecResult(rc=RC_TIMEOUT,
                              stdout=_decode(out if isinstance(out, bytes) else str(out).encode()),
                              stderr=f"{text}\n{note}" if text else note,
                              duration_s=time.monotonic() - start, timed_out=True)
        except Exception as exc:  # noqa: BLE001 - connection lost mid-command
            self._conn = None
            return ExecResult(rc=255, stdout="", stderr=f"ssh connection error: {exc}",
                              duration_s=time.monotonic() - start)
        rc = res.exit_status if res.exit_status is not None else 255
        out, err = res.stdout, res.stderr
        # asyncssh buffers the whole output itself; at least bound what is returned.
        return ExecResult(rc=rc,
                          stdout=_clip(out if isinstance(out, str) else _decode(out), OUTPUT_CAP_BYTES),
                          stderr=_clip(err if isinstance(err, str) else _decode(err), OUTPUT_CAP_BYTES),
                          duration_s=time.monotonic() - start)

    # -- Transport protocol --------------------------------------------------

    def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: float = 60) -> ExecResult:
        if not argv:
            raise ValueError("empty argv")
        validate_host(self.host)
        command = shlex.join([str(a) for a in argv])
        future = self._runner.submit(self._run(command, stdin, timeout))
        try:
            return future.result(timeout + 20)
        except _ConnectFailed as exc:
            log.info("asyncssh connect to %s failed (%s); using ssh executable", self.host.address, exc)
            return self.fallback.run(argv, stdin=stdin, timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return ExecResult(rc=RC_TIMEOUT, stdout="", stderr=f"timed out after {timeout:g} s",
                              duration_s=float(timeout), timed_out=True)

    def run_script(self, script: str, *, timeout: float = 60) -> ExecResult:
        return self.run(["sh", "-s"], stdin=script.encode("utf-8"), timeout=timeout)

    def run_stream(self, argv: list[str], *, sink: Any = None, source: Any = None,
                   max_bytes: int | None = None, timeout: float = 3600) -> StreamResult:
        """Transfers use the ssh executable: asyncssh would buffer the whole output."""
        return self.fallback.run_stream(argv, sink=sink, source=source, max_bytes=max_bytes,
                                        timeout=timeout)

    def tcp_check(self, timeout: float = 3.0) -> float | None:
        return self.fallback.tcp_check(timeout)

    def local_argv(self, remote_argv: list[str] | None, *, tty: bool = False,
                   forwards: Sequence[str] = ()) -> list[str]:
        return self.fallback.local_argv(remote_argv, tty=tty, forwards=forwards)

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return

        async def _close() -> None:
            conn.close()
            try:
                await asyncio.wait_for(conn.wait_closed(), 5)
            except Exception:  # noqa: BLE001
                pass

        try:
            self._runner.submit(_close()).result(10)
        except Exception:  # noqa: BLE001 - closing is best effort
            pass


# --------------------------------------------------------------------------- factory


def _conn_key(host: HostSpec) -> tuple:
    return (host.id, host.address, host.user, host.port, host.identity_file, host.jump_host)


class TransportFactory:
    """Builds the transport for a host.

    ``kind="local"`` → :class:`LocalTransport`; otherwise :class:`SubprocessSSHTransport`
    (a new object per call), or a cached :class:`AsyncSSHTransport` per host when
    ``config.use_asyncssh`` is set and ``asyncssh`` is importable. With
    ``prefixed=True`` and a non-empty ``host.exec_prefix`` the result is wrapped in
    :class:`PrefixedTransport`.
    """

    def __init__(self, config: Config | None = None):
        self.config = config if config is not None else Config()
        self._ssh_bin: str | None = None
        self._ssh_input: Any = None       # the ssh_path setting _ssh_bin was resolved from
        self._async: dict[str, tuple[tuple, AsyncSSHTransport]] = {}
        self._lock = threading.Lock()

    @property
    def ssh_bin(self) -> str:
        """The resolved ssh executable.

        Cached, but resolved again when the ``ssh_path`` setting changed since (the
        Settings page applies without a restart).
        """
        configured = getattr(self.config, "ssh_path", "")
        if self._ssh_bin is None or configured != self._ssh_input:
            self._ssh_bin = resolve_ssh_binary(self.config)
            self._ssh_input = configured
        return self._ssh_bin

    def _ssh_transport(self, host: HostSpec) -> Transport:
        if getattr(self.config, "use_asyncssh", False):
            module = _import_asyncssh()
            if module is not None:
                ssh_bin = self.ssh_bin
                key = _conn_key(host) + (ssh_bin,)
                with self._lock:
                    cached = self._async.get(host.id)
                    if cached and cached[0] == key:
                        cached[1].host = host
                        return cached[1]
                    t = AsyncSSHTransport(host, ssh_bin, asyncssh_module=module)
                    self._async[host.id] = (key, t)
                old = cached[1] if cached else None
                if old is not None:
                    old.close()
                return t
            log.debug("asyncssh not installed; using the ssh executable")
        return SubprocessSSHTransport(host, self.ssh_bin)

    def _runtime_paths(self) -> dict[str, str]:
        """Explicit ``docker`` / ``podman`` executables from settings (when the settings carry any)."""
        paths = {}
        for name in RUNTIME_NAMES:
            value = getattr(self.config, f"{name}_path", "")
            if isinstance(value, str) and value:
                paths[name] = value
        return paths

    def for_host(self, host: HostSpec, *, prefixed: bool = True) -> Transport:
        if host.kind == "local":
            inner: Transport = LocalTransport(host, self._runtime_paths())
        elif host.kind == "ec2":
            if not host.address or host.address == NO_MACHINE:
                raise MachineNotRunning(host.name or host.id)   # nothing to dial
            # One machine per host, with its own known-hosts options: never pooled.
            inner = _GoingDownTransport(SubprocessSSHTransport(host, self.ssh_bin))
        else:
            inner = self._ssh_transport(host)
        if prefixed and host.exec_prefix:
            return PrefixedTransport(inner, host.exec_prefix)
        return inner

    def close(self) -> None:
        """Close cached connections (asyncssh only)."""
        with self._lock:
            cached = [t for _, t in self._async.values()]
            self._async.clear()
        for t in cached:
            t.close()


def provide(services: Any) -> TransportFactory:
    """Service provider used by the module registry."""
    try:
        config = services.config
    except Exception:  # noqa: BLE001 - config unavailable -> defaults
        config = Config()
    return TransportFactory(config)
