"""Managed loopback tunnels: ``ssh -N -L`` child processes with a TTL.

Public API
==========

``TunnelManager(services, *, popen=None, port_probe=None, pid_alive=None,
pid_is_ssh=None, kill_pid=None, clock=None, sleep=None, port_is_free=None,
free_port=None, ready_timeout=10.0, poll_interval=0.1)``
    ``services`` provides ``store``, ``config``, ``events`` and ``transports``.
    Every keyword is an injection point for tests; the defaults use the real
    system.

``TunnelManager.open_tunnel(host, *, kind, remote_port, remote_host="127.0.0.1",
local_port=None, instance_id=None, url=None, url_builder=None,
ttl_minutes=None) -> TunnelRecord``
    ``kind`` is ``"display"`` or ``"port"``. ``url_builder(local_port) -> str``
    (or a fixed ``url``) sets ``TunnelRecord.url``; the default is
    ``http://127.0.0.1:<local_port>/``. ``ttl_minutes`` defaults to
    ``Config.tunnel_ttl_minutes``.

    * ``host.kind == "local"``: no process is started. The published loopback
      port is used directly (``local_port == remote_port``, ``pid=None``,
      status ``open``). A display tunnel with a ``ready_path`` first waits for the
      screen server like an ssh one does.
    * ``host.kind == "ssh"``: starts
      ``ssh -o ExitOnForwardFailure=yes <base options> -N -L
      127.0.0.1:<lp>:<remote_host>:<remote_port> <host>`` without a shell or a
      console window. ``<lp>`` is a free port picked by binding
      ``127.0.0.1:0`` unless ``local_port`` (1024-65535, must be free on
      127.0.0.1) is given. The call waits up to ``ready_timeout`` seconds for
      ``127.0.0.1:<lp>`` to accept connections: success -> status ``open``;
      the process exiting or the wait timing out -> status ``error`` with a
      short, redacted stderr tail in ``error``.
    * other host kinds (``slurm``, ``ec2``): ``ApiError(400, "invalid")``.

    Invalid ports, kinds, host fields or remote hosts raise
    ``ApiError(400, "invalid")``. The record is stored (``store.upsert_tunnel``)
    and ``tunnels.changed`` ``{"ids": [id]}`` is emitted.

``TunnelManager.close_tunnel(tunnel_id) -> TunnelRecord``
    Terminates the process (``terminate`` then ``kill``; for a process started
    by an earlier run of the module only a live ``ssh`` process with the stored
    pid is killed) and marks the record ``closed``. Idempotent: closing a
    closed or failed tunnel returns it unchanged. Unknown id ->
    ``ApiError(404, "not_found")``.

``TunnelManager.close_instance_tunnels(instance_id) -> list[str]``
    Closes every open tunnel bound to the instance (display and port) and
    ends their ssh processes; returns the closed ids.

``TunnelManager.discard_tunnel(tunnel_id) -> bool``
    Ends the tunnel's process and deletes its record (a failed attempt leaves nothing).

``open_tunnel(..., ready_path=..., http_timeout=...)``: a display tunnel is open only once
    ``GET /<ready_path>`` answers 200; the wait backs off (0.1 s growing to 2 s) and lasts
    ``http_timeout`` seconds (default ``http_ready_timeout``). A miss gives status ``error``
    with an error starting with :data:`NOT_READY`.

``TunnelManager.get_tunnel(tunnel_id) -> TunnelRecord | None``

``TunnelManager.list_tunnels(open_only=True) -> list[TunnelRecord]``

``TunnelManager.sweep(now=None) -> list[str]``
    Closes open tunnels whose ``expires_at`` has passed and marks records
    whose process has exited (``error``) or whose pid is gone or no longer an
    ssh process (``closed``). Records without a pid expire by TTL only. Also
    closes tunnels whose instance is missing from the store or is destroyed or
    stopped (at most 8 per call; the rest wait for the next tick). No
    network access. Emits one ``tunnels.changed`` with the changed ids when
    there are any.

Module helpers: ``free_local_port()``, ``port_is_free(port)``,
``port_open(port, timeout=0.5)``, ``pid_alive(pid)``, ``pid_image_name(pid)``,
``pid_is_ssh(pid)``, ``kill_pid(pid)``, ``tunnel_argv(transport, host,
local_port, remote_host, remote_port)``, ``sanitize_stderr(text)``.

Started processes are kept in a module-level registry (tunnel id -> process)
so any manager in the same process can close them. Events carry tunnel ids
only, never URLs (a display URL contains an access token). Standard library
only.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import IO, Any, Callable, Sequence

from .interfaces import ApiError
from .models import HostSpec, TunnelRecord, new_id, parse_iso
from .redact import redact_text
from .transport import SAFE_TOKEN, child_env, validate_forward, validate_host

__all__ = [
    "TunnelManager", "free_local_port", "port_is_free", "port_open", "pid_alive",
    "pid_image_name", "pid_is_ssh", "kill_pid", "tunnel_argv", "sanitize_stderr",
    "EVENT_CHANGED", "MIN_LOCAL_PORT", "DEFAULT_READY_TIMEOUT", "NOT_READY",
]

log = logging.getLogger(__name__)

EVENT_CHANGED = "tunnels.changed"
MIN_LOCAL_PORT = 1024
DEFAULT_READY_TIMEOUT = 10.0
DEFAULT_SERVER_ALIVE = "15"
LOOPBACK = "127.0.0.1"
_KINDS = ("display", "port")
_OPEN = ("starting", "open")
_TERMINATE_WAIT_S = 3.0
# One sweep tick closes at most this many orphaned tunnels (each may wait for ssh to exit);
# the rest are picked up on the next tick, so a tick stays short.
_MAX_ORPHAN_CLOSES = 8
_GONE_STATES = frozenset({"destroyed", "stopped"})
_STDERR_TAIL_CHARS = 300
_CREATE_NO_WINDOW = 0x08000000
_SSH_NAMES = frozenset({"ssh", "ssh.exe"})
_REMOTE_HOST = re.compile(r"^[A-Za-z0-9_.-]+$")
# Long opaque strings (hex or base64-like) that could be tokens.
_OPAQUE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/_=-]{24,}(?![A-Za-z0-9])")
_HEX_TOKEN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{16,}(?![0-9A-Fa-f])")


def _is_windows() -> bool:
    return os.name == "nt"


def _invalid(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _iso(value: _dt.datetime) -> str:
    return value.astimezone(_dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# --------------------------------------------------------------------------- ports


def free_local_port() -> int:
    """A port that is currently free on 127.0.0.1 (bind ``127.0.0.1:0``, read it, close)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return int(sock.getsockname()[1])


def port_is_free(port: int) -> bool:
    """True if ``127.0.0.1:<port>`` can be bound right now."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if _is_windows() and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((LOOPBACK, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def port_open(port: int, timeout: float = 0.5) -> bool:
    """True if something accepts TCP connections on ``127.0.0.1:<port>``."""
    try:
        with socket.create_connection((LOOPBACK, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _check_port(field: str, value: Any, low: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= 65535:
        raise _invalid(f"invalid {field} {value!r}: expected an integer {low}-65535")
    return value


# --------------------------------------------------------------------------- processes


def _win_image_name(pid: int) -> str | None:
    """Executable base name of a live process via the Win32 API; ``None`` if not running."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
        if code.value != 259:  # STILL_ACTIVE
            return None
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)


def _tasklist_image_name(pid: int) -> str | None:
    """Fallback: ``tasklist /FI "PID eq <pid>" /FO CSV /NH`` -> image name or ``None``."""
    try:
        res = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=_CREATE_NO_WINDOW, env=child_env())
    except (OSError, subprocess.SubprocessError):
        return None
    for line in res.stdout.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].strip('"') == str(int(pid)):
            return parts[0].strip('"')
    return None


HTTP_READY_TIMEOUT = 10.0  # default total budget for the HTTP readiness check (seconds)
_HTTP_ATTEMPT_TIMEOUT = 2.0
#: The wait between readiness checks grows by this factor up to ``_MAX_POLL_S`` (backoff).
_POLL_GROWTH = 1.6
_MAX_POLL_S = 2.0
#: Start of the error text when the screen server did not answer in time.
NOT_READY = "screen not ready after"


def http_ready(local_port: int, path: str, *, timeout: float = _HTTP_ATTEMPT_TIMEOUT) -> bool:
    """One ``GET http://127.0.0.1:<port>/<path>``; True only for status 200. Never logs the path."""
    url = f"http://{LOOPBACK}:{int(local_port)}/{path.lstrip('/')}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback http
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def pid_image_name(pid: int | None) -> str | None:
    """Executable base name of a running process, or ``None`` when it is not running."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if _is_windows():
        try:
            return _win_image_name(pid)
        except OSError as exc:
            if getattr(exc, "errno", None) == 87:  # ERROR_INVALID_PARAMETER: no such process
                return None
            return _tasklist_image_name(pid)
        except Exception:  # noqa: BLE001 - ctypes unavailable
            return _tasklist_image_name(pid)
    if not pid_alive(pid):
        return None
    try:
        res = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True,
                             text=True, timeout=5, env=child_env())
    except (OSError, subprocess.SubprocessError):
        return ""
    return os.path.basename(res.stdout.strip())


def pid_alive(pid: int | None) -> bool:
    """True if a process with this pid is running."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if _is_windows():
        return pid_image_name(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pid_is_ssh(pid: int | None) -> bool:
    """True if ``pid`` is a running ``ssh``/``ssh.exe`` process."""
    name = pid_image_name(pid)
    return bool(name) and name.lower() in _SSH_NAMES


def kill_pid(pid: int) -> None:
    """Forcefully end a process (and its children on Windows)."""
    if _is_windows():
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True,
                       timeout=10, creationflags=_CREATE_NO_WINDOW, env=child_env())
    else:
        os.kill(int(pid), signal.SIGTERM)


# --------------------------------------------------------------------------- argv


def _option_value(argv: Sequence[str], name: str) -> str | None:
    """Value of the first ``-o <name>=<value>`` in argv (case-insensitive name)."""
    want = name.lower() + "="
    for i, arg in enumerate(argv):
        opt = None
        if arg == "-o" and i + 1 < len(argv):
            opt = argv[i + 1]
        elif arg.startswith("-o") and len(arg) > 2:
            opt = arg[2:]
        if opt and opt.lower().startswith(want):
            return opt[len(want):]
    return None


def tunnel_argv(transport: Any, host: HostSpec, local_port: int, remote_host: str,
                remote_port: int) -> list[str]:
    """Local ``ssh -N -L 127.0.0.1:<lp>:<remote_host>:<rp>`` argv for ``host``.

    Built with ``transport.local_argv(None, forwards=[...])``; our options are
    placed right after the executable so they win (ssh uses the first value of
    an option). Raises ``ValueError`` for anything unsafe.
    """
    validate_host(host)
    spec = validate_forward(f"{LOOPBACK}:{int(local_port)}:{remote_host}:{int(remote_port)}")
    base = [str(a) for a in transport.local_argv(None, forwards=[spec])]
    if not base:
        raise ValueError("empty tunnel argv")
    extra: list[str] = []
    exit_on_failure = _option_value(base, "ExitOnForwardFailure")
    if exit_on_failure is None:
        extra += ["-o", "ExitOnForwardFailure=yes"]
    elif exit_on_failure.lower() != "yes":
        raise ValueError("tunnel argv disables ExitOnForwardFailure")
    if _option_value(base, "ServerAliveInterval") is None:
        extra += ["-o", f"ServerAliveInterval={DEFAULT_SERVER_ALIVE}"]
    if "-N" not in base:
        extra.append("-N")
    argv = [base[0], *extra, *base[1:]]
    # Every forward must bind the loopback address.
    forwards = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-L"]
    if forwards != [spec] or any(a.startswith("-L") and a != "-L" for a in argv):
        raise ValueError("unexpected port forward in tunnel argv")
    if any(a in ("-R", "-D", "-g") for a in argv):
        raise ValueError("unexpected forwarding option in tunnel argv")
    if host.address not in argv:
        raise ValueError("host missing from tunnel argv")
    return argv


def sanitize_stderr(text: str | bytes | None, limit: int = _STDERR_TAIL_CHARS) -> str:
    """Short, single-line, redacted tail of ssh's stderr (safe to store and show)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.lower().startswith("warning: permanently added")]
    out = redact_text(" | ".join(lines[-4:]))
    out = _HEX_TOKEN.sub("[redacted]", out)
    out = _OPAQUE.sub("[redacted]", out)
    if len(out) > limit:
        out = "…" + out[-(limit - 1):]
    return out


# --------------------------------------------------------------------------- registry


@dataclass
class _Child:
    proc: Any
    stderr: IO[bytes] | None


_REGISTRY: dict[str, _Child] = {}
_REGISTRY_LOCK = threading.Lock()


def _read_tail(child: _Child) -> str:
    fh = child.stderr
    if fh is None:
        return ""
    try:
        fh.flush()
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 4096))
        return fh.read().decode("utf-8", errors="replace")
    except (OSError, ValueError, AttributeError):
        return ""


def _close_file(child: _Child) -> None:
    if child.stderr is not None:
        try:
            child.stderr.close()
        except (OSError, ValueError):
            pass


def _terminate(child: _Child) -> None:
    proc = child.proc
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=_TERMINATE_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=_TERMINATE_WAIT_S)
                except subprocess.TimeoutExpired:
                    log.warning("tunnel process %s did not exit", getattr(proc, "pid", "?"))
    except OSError as exc:
        log.debug("terminating tunnel process failed: %s", exc)


# --------------------------------------------------------------------------- manager


class TunnelManager:
    """Opens, tracks, sweeps and closes loopback tunnels (see the module docstring)."""

    def __init__(self, services: Any, *,
                 popen: Callable[..., Any] | None = None,
                 port_probe: Callable[[int], bool] | None = None,
                 pid_alive: Callable[[int], bool] | None = None,
                 pid_is_ssh: Callable[[int], bool] | None = None,
                 kill_pid: Callable[[int], None] | None = None,
                 clock: Callable[[], _dt.datetime] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 port_is_free: Callable[[int], bool] | None = None,
                 free_port: Callable[[], int] | None = None,
                 ready_timeout: float = DEFAULT_READY_TIMEOUT,
                 poll_interval: float = 0.1,
                 http_probe: Callable[[int, str], bool] | None = None,
                 http_ready_timeout: float = HTTP_READY_TIMEOUT):
        self.services = services
        self._popen = popen or subprocess.Popen
        self._port_probe = port_probe or port_open
        self._pid_alive = pid_alive or globals()["pid_alive"]
        self._pid_is_ssh = pid_is_ssh or globals()["pid_is_ssh"]
        self._kill_pid = kill_pid or globals()["kill_pid"]
        self._clock = clock or _utcnow
        self._sleep = sleep or time.sleep
        self._port_is_free = port_is_free or globals()["port_is_free"]
        self._free_port = free_port or free_local_port
        self.ready_timeout = float(ready_timeout)
        self.poll_interval = float(poll_interval)
        self._http_probe = http_probe or http_ready
        self.http_ready_timeout = float(http_ready_timeout)
        self._lock = threading.RLock()

    # -- helpers -----------------------------------------------------------

    @property
    def store(self) -> Any:
        return self.services.store

    def _emit(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        try:
            self.services.events.emit(EVENT_CHANGED, {"ids": list(ids)})
        except Exception as exc:  # noqa: BLE001 - events never break tunnel handling
            log.debug("emit %s failed: %s", EVENT_CHANGED, exc)

    def _ttl_minutes(self, ttl_minutes: int | None) -> int:
        if ttl_minutes is None:
            try:
                ttl_minutes = int(getattr(self.services.config, "tunnel_ttl_minutes", 60))
            except Exception:  # noqa: BLE001 - config unavailable -> default
                ttl_minutes = 60
        if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int) or ttl_minutes < 1:
            raise _invalid(f"invalid ttl_minutes {ttl_minutes!r}")
        return ttl_minutes

    def _spawn(self, argv: list[str]) -> _Child:
        errfile = tempfile.TemporaryFile()
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                  "stderr": errfile, "close_fds": True,
                                  "env": child_env()}
        if _is_windows():
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            proc = self._popen(argv, **kwargs)
        except BaseException:
            errfile.close()
            raise
        return _Child(proc=proc, stderr=errfile)

    def _wait_ready(self, child: _Child, local_port: int) -> str | None:
        """``None`` when the forward accepts connections, else an error message."""
        deadline = time.monotonic() + self.ready_timeout
        while True:
            rc = child.proc.poll()
            if rc is not None:
                tail = sanitize_stderr(_read_tail(child))
                return f"ssh exited with code {rc}" + (f": {tail}" if tail else "")
            if self._port_probe(local_port):
                rc = child.proc.poll()
                if rc is None:
                    return None
                continue
            if time.monotonic() >= deadline:
                tail = sanitize_stderr(_read_tail(child))
                return (f"tunnel not ready after {self.ready_timeout:g} s"
                        + (f": {tail}" if tail else ""))
            self._sleep(self.poll_interval)

    def _wait_http(self, child: _Child | None, local_port: int, ready_path: str,
                   timeout: float | None = None) -> str | None:
        """``None`` once ``GET /<ready_path>`` returns 200, else an error.

        Waits up to ``timeout`` seconds (default ``http_ready_timeout``), checking again after
        a growing pause (backoff). ``child`` is ``None`` for a local host (no ssh process).
        """
        budget = self.http_ready_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + budget
        pause = self.poll_interval
        while True:
            if child is not None and child.proc.poll() is not None:
                return "ssh exited before the screen became ready"
            if self._http_probe(local_port, ready_path):
                return None
            if time.monotonic() >= deadline:
                return f"{NOT_READY} {budget:g} s"
            self._sleep(pause)
            pause = min(max(pause, 0.05) * _POLL_GROWTH, _MAX_POLL_S) if pause else 0.0

    # -- public API ----------------------------------------------------------

    def open_tunnel(self, host: HostSpec, *, kind: str, remote_port: int,
                    remote_host: str = LOOPBACK, local_port: int | None = None,
                    instance_id: str | None = None, url: str | None = None,
                    url_builder: Callable[[int], str] | None = None,
                    ready_path: str | None = None,
                    http_timeout: float | None = None,
                    ttl_minutes: int | None = None) -> TunnelRecord:
        if kind not in _KINDS:
            raise _invalid(f"invalid tunnel kind {kind!r}: expected display or port")
        remote_port = _check_port("remote_port", remote_port)
        if local_port is not None:
            local_port = _check_port("local_port", local_port, MIN_LOCAL_PORT)
        if not isinstance(remote_host, str) or not _REMOTE_HOST.match(remote_host) \
                or remote_host.startswith("-"):
            raise _invalid(f"invalid remote_host {remote_host!r}")
        if instance_id is not None and (not isinstance(instance_id, str)
                                        or not SAFE_TOKEN.match(instance_id)
                                        or instance_id.startswith("-")):
            raise _invalid(f"invalid instance_id {instance_id!r}")
        if not isinstance(host, HostSpec):
            raise _invalid("host must be a HostSpec")
        ttl = self._ttl_minutes(ttl_minutes)
        if host.kind not in ("local", "ssh", "ec2"):
            raise _invalid(f"port forwarding is not supported for {host.kind} hosts")

        def build_url(port: int) -> str:
            if url_builder is not None:
                return url_builder(port)
            return url if url is not None else f"http://{LOOPBACK}:{port}/"

        now = self._clock()
        rec = TunnelRecord(
            id=new_id("t"), kind=kind, instance_id=instance_id, host_id=host.id,
            local_port=0, remote_host=remote_host, remote_port=remote_port, pid=None, url=None,
            created_at=_iso(now), expires_at=_iso(now + _dt.timedelta(minutes=ttl)),
            status="starting")

        if host.kind == "local":
            if remote_host not in (LOOPBACK, "localhost"):
                raise _invalid("local tunnels must target the loopback address")
            if local_port is not None and local_port != remote_port:
                raise _invalid("local hosts use the published port; local_port must match")
            error = None
            if kind == "display" and ready_path:
                # No ssh here, but the screen server may still be starting (or absent).
                error = self._wait_http(None, remote_port, ready_path, http_timeout)
            if error is None:
                rec = replace(rec, local_port=remote_port, status="open",
                              url=build_url(remote_port))
            else:
                rec = replace(rec, local_port=remote_port, status="error", error=error)
            self.store.upsert_tunnel(rec)
            self._emit([rec.id])
            return rec

        try:
            validate_host(host)
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
        if local_port is None:
            local_port = self._free_port()
        elif not self._port_is_free(local_port):
            raise ApiError(409, "conflict", f"local port {local_port} is already in use")
        transport = self.services.transports.for_host(host, prefixed=False)
        try:
            argv = tunnel_argv(transport, host, local_port, remote_host, remote_port)
        except ValueError as exc:
            raise _invalid(str(exc)) from exc

        rec = replace(rec, local_port=local_port)
        try:
            child = self._spawn(argv)
        except OSError as exc:
            rec = replace(rec, status="error", error=f"could not start ssh: {exc.strerror or exc}")
            self.store.upsert_tunnel(rec)
            self._emit([rec.id])
            return rec
        rec = replace(rec, pid=getattr(child.proc, "pid", None))
        with _REGISTRY_LOCK:
            _REGISTRY[rec.id] = child
        self.store.upsert_tunnel(rec)

        error = self._wait_ready(child, local_port)
        if error is None and kind == "display":
            if ready_path:
                error = self._wait_http(child, local_port, ready_path, http_timeout)
            else:
                log.warning("display tunnel %s: access token unknown, TCP-only readiness", rec.id)
        if error is None:
            rec = replace(rec, status="open", url=build_url(local_port))
        else:
            with _REGISTRY_LOCK:
                _REGISTRY.pop(rec.id, None)
            _terminate(child)
            _close_file(child)
            rec = replace(rec, status="error", error=error)
        self.store.upsert_tunnel(rec)
        self._emit([rec.id])
        return rec

    def get_tunnel(self, tunnel_id: str) -> TunnelRecord | None:
        return self.store.get_tunnel(tunnel_id)

    def list_tunnels(self, open_only: bool = True) -> list[TunnelRecord]:
        return self.store.list_tunnels(open_only=open_only)

    def _stop_process(self, rec: TunnelRecord) -> None:
        with _REGISTRY_LOCK:
            child = _REGISTRY.pop(rec.id, None)
        if child is not None:
            _terminate(child)
            _close_file(child)
            return
        pid = rec.pid
        if pid and self._pid_alive(pid) and self._pid_is_ssh(pid):
            try:
                self._kill_pid(pid)
            except (OSError, subprocess.SubprocessError) as exc:
                log.debug("killing tunnel pid %s failed: %s", pid, exc)

    def close_tunnel(self, tunnel_id: str) -> TunnelRecord:
        with self._lock:
            rec = self.store.get_tunnel(tunnel_id)
            if rec is None:
                raise ApiError(404, "not_found", f"tunnel {tunnel_id!r} not found")
            if rec.status not in _OPEN:
                with _REGISTRY_LOCK:
                    child = _REGISTRY.pop(rec.id, None)
                if child is not None:
                    _terminate(child)
                    _close_file(child)
                return rec
            if rec.pid is not None:
                self._stop_process(rec)
            rec = replace(rec, status="closed")
            self.store.upsert_tunnel(rec)
        self._emit([rec.id])
        return rec

    def discard_tunnel(self, tunnel_id: str) -> bool:
        """End the tunnel's ssh process (if any) and delete its record; ``True`` if it existed.

        For a failed attempt that must leave nothing behind (no record, no process).
        """
        with self._lock:
            rec = self.store.get_tunnel(tunnel_id)
            if rec is None:
                return False
            if rec.pid is not None:
                self._stop_process(rec)
            with _REGISTRY_LOCK:
                child = _REGISTRY.pop(rec.id, None)
            if child is not None:
                _terminate(child)
                _close_file(child)
            self.store.delete_tunnel(rec.id)
        self._emit([rec.id])
        return True

    def close_instance_tunnels(self, instance_id: str) -> list[str]:
        """Close every open tunnel bound to ``instance_id`` (display and port) and end its ssh.

        One failing tunnel never stops the others. Returns the ids that were closed.
        """
        closed: list[str] = []
        if not instance_id:
            return closed
        for rec in self.store.list_tunnels(open_only=True):
            if rec.instance_id != instance_id:
                continue
            try:
                self.close_tunnel(rec.id)
                closed.append(rec.id)
            except Exception as exc:  # noqa: BLE001 - keep closing the rest
                log.debug("closing tunnel %s of %s failed: %s", rec.id, instance_id,
                          type(exc).__name__)
        return closed

    def _instance_gone(self, instance_id: str, cache: dict[str, bool]) -> bool:
        """True if the instance is missing from the store or destroyed/stopped (cached per tick)."""
        if instance_id not in cache:
            try:
                inst = self.store.get_instance(instance_id)
                cache[instance_id] = inst is None or inst.status in _GONE_STATES
            except Exception as exc:  # noqa: BLE001 - unknown state: leave the tunnel alone
                log.debug("instance lookup for tunnel sweep failed: %s", type(exc).__name__)
                cache[instance_id] = False
        return cache[instance_id]

    def sweep(self, now: _dt.datetime | None = None) -> list[str]:
        now = now or self._clock()
        changed: list[str] = []
        gone: dict[str, bool] = {}
        orphans = 0
        with self._lock:
            for rec in self.store.list_tunnels(open_only=True):
                new = self._sweep_one(rec, now)
                if (new is None and rec.instance_id and orphans < _MAX_ORPHAN_CLOSES
                        and self._instance_gone(rec.instance_id, gone)):
                    orphans += 1
                    if rec.pid is not None:
                        self._stop_process(rec)
                    new = replace(rec, status="closed")
                if new is not None:
                    self.store.upsert_tunnel(new)
                    changed.append(rec.id)
        self._emit(changed)
        return changed

    def _sweep_one(self, rec: TunnelRecord, now: _dt.datetime) -> TunnelRecord | None:
        expires = parse_iso(rec.expires_at)
        if expires is not None and expires <= now:
            if rec.pid is not None:
                self._stop_process(rec)
            return replace(rec, status="closed")
        if rec.pid is None:
            return None
        with _REGISTRY_LOCK:
            child = _REGISTRY.get(rec.id)
        if child is not None:
            rc = child.proc.poll()
            if rc is None:
                return None
            tail = sanitize_stderr(_read_tail(child))
            with _REGISTRY_LOCK:
                _REGISTRY.pop(rec.id, None)
            _close_file(child)
            return replace(rec, status="error",
                           error=f"ssh exited with code {rc}" + (f": {tail}" if tail else ""))
        if self._pid_alive(rec.pid) and self._pid_is_ssh(rec.pid):
            return None
        return replace(rec, status="closed", error="tunnel process is no longer running")
