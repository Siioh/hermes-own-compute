"""Access: terminals, the screen (noVNC) and port forwards for environments, hosts and jobs.

``provide(services)`` returns an :class:`AccessManager`. It owns one
:class:`~.tunnels.TunnelManager` and builds on :mod:`.terminal`, :mod:`.display`
and :mod:`.runtimes`.

* ``open_terminal`` opens a terminal window on this computer for an environment, a
  Slurm host or a running Slurm job.
* ``open_display`` opens (or reuses) a loopback tunnel to an environment's
  screen. The returned record's ``url`` contains the access token, so it is
  meant for the local REST API only; use :func:`~.display.redact_display_url`
  before showing it anywhere else.
* ``forward_port`` forwards a port to this computer. Only ports that are *published*
  on the host's loopback can be forwarded for an environment; a host forward
  targets a port on the host's own loopback. ``remote_port`` is the port *inside* the
  environment; the host-side port that ``compute_status`` shows is accepted too and
  translated to it.
* A host port recorded for a screen or a forward is checked against the runtime first
  (the runtime may publish a new one after a stop and start); a changed mapping is
  refreshed through ``SandboxManager.refresh_ports`` when that exists.
* A screen that does not come up ends in a clear error, never ``internal``: the screen
  server is waited for with backoff (up to 30 s right after a start), then the error says
  whether the environment is still starting, has no screen server, or stopped answering.
  The tunnel opened for the failed attempt is deleted, and so is a token created for it.

Environments and jobs are read straight from the store, so this module works
whether or not the sandbox and Slurm modules are installed. Standard library only.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import display, runtimes, terminal
from .interfaces import ApiError
from .models import HostSpec, InstanceRecord, TerminalLaunch, TunnelRecord, parse_iso
from .tunnels import NOT_READY, TunnelManager

__all__ = ["AccessManager", "provide"]

log = logging.getLogger(__name__)

#: Repeated terminal requests for the same target within this many seconds open one window.
TERMINAL_DEDUPE_S = 3.0

#: How long the screen server is waited for (seconds): right after a start of a screen image,
#: and otherwise (a server that is not expected to be slow).
SCREEN_WAIT_FRESH_S = 30.0
SCREEN_WAIT_SHORT_S = 8.0
#: An environment started less than this many seconds ago "only just started".
FRESH_START_S = 90.0


def _invalid(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _check_port(value: Any, what: str = "remote_port") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        raise _invalid(f"{what} must be an integer between 1 and 65535")
    return value


@dataclass
class _Container:
    """What ``<rt> inspect`` says about an environment's container."""

    token: str | None = None
    image: str = ""
    #: container port -> host port, from NetworkSettings.Ports; ``None`` = not reported.
    ports: dict[int, int] | None = None


def _parse_inspect(text: str) -> _Container | None:
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    item = data[0] if isinstance(data, list) and data else data
    if not isinstance(item, dict):
        return None
    out = _Container(token=_token_from_inspect(text))
    out.image = str((item.get("Config") or {}).get("Image") or "")
    net = item.get("NetworkSettings")
    if isinstance(net, dict) and "Ports" in net:
        mapping: dict[int, int] = {}
        bound = net.get("Ports")
        for key, entries in (bound.items() if isinstance(bound, dict) else []):
            cport, _, proto = str(key).partition("/")
            if not cport.isdigit() or proto not in ("", "tcp") or not isinstance(entries, list):
                continue
            hosts = [e for e in entries
                     if isinstance(e, dict) and str(e.get("HostPort") or "").isdigit()]
            hosts.sort(key=lambda e: e.get("HostIp") not in ("127.0.0.1", "", None))
            if hosts:
                mapping[int(cport)] = int(hosts[0]["HostPort"])
        out.ports = mapping
    return out


def _stored_ports(ports: dict[str, str]) -> dict[int, int]:
    """container port -> host port from ``InstanceRecord.ports`` (``novnc`` = the screen port)."""
    out: dict[int, int] = {}
    for key, entry in (ports or {}).items():
        cport = (runtimes.DISPLAY_PORT if key == "novnc"
                 else (int(key) if str(key).isdigit() else None))
        host_port = display.parse_ports_entry(entry or "")
        if cport and host_port:
            out[cport] = host_port
    return out


def _is_screen_image(image: str) -> bool:
    """True for the plugin's own screen image (it has a screen server)."""
    base = (image or "").strip()
    return base == runtimes.DISPLAY_IMAGE_TAG or base.startswith("localhost/cloud-compute-novnc")


class AccessManager:
    """Terminals, screens and port forwards. See the module docstring."""

    def __init__(self, services: Any, *, tunnels: TunnelManager | None = None,
                 launcher: Callable[..., TerminalLaunch] | None = None,
                 opener: Callable[[str], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], _dt.datetime] | None = None,
                 screen_waits: tuple[float, float] | None = None) -> None:
        self.services = services
        #: (wait right after a start of a screen image, wait otherwise) in seconds.
        self.screen_waits = screen_waits or (SCREEN_WAIT_FRESH_S, SCREEN_WAIT_SHORT_S)
        self.tunnels = tunnels if tunnels is not None else TunnelManager(services)
        self._launcher = launcher or terminal.launch
        self._opener = opener or terminal.open_url
        self._clock = clock
        self._now = now or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self._terminal_lock = threading.Lock()
        self._recent_terminals: dict[tuple[str, str], tuple[float, TerminalLaunch]] = {}

    # ------------------------------------------------------------------ lookups

    def _host(self, host_id: str) -> HostSpec:
        host = self.services.store.get_host(host_id)
        if host is None:
            raise ApiError(404, "not_found", f"host {host_id!r} not found")
        return host

    def _instance(self, instance_id: str) -> tuple[InstanceRecord, HostSpec]:
        inst = self.services.store.get_instance(instance_id) if isinstance(instance_id, str) else None
        if inst is None:
            raise ApiError(404, "not_found", f"environment {instance_id!r} not found; "
                                             "ids come from compute_status")
        return inst, self._host(inst.host_id)

    def _workdir_of(self, inst: InstanceRecord) -> str:
        """The folder the shell opens in: ``SandboxManager.exec_workdir`` when it exists, else
        the template's working folder, else ``/work``."""
        ask = getattr(getattr(self.services, "sandboxes", None), "exec_workdir", None)
        if callable(ask):
            try:
                folder = terminal.shell_workdir(ask(inst.id))
            except Exception:  # noqa: BLE001 - fall back to the template
                folder = None
            if folder:
                return folder
        try:
            tpl = self.services.store.get_template(inst.template_id)
        except Exception:  # noqa: BLE001 - a deleted template: the default
            tpl = None
        return terminal.shell_workdir(getattr(tpl, "workdir", None)) or "/work"

    def _touch(self, instance_id: str) -> None:
        """Mark the environment as in use (idle timer); never fails the call."""
        try:
            self.services.sandboxes.touch(instance_id)
        except Exception as exc:  # noqa: BLE001 - sandboxes may be missing; activity is best effort
            log.debug("cloud-compute: activity not recorded for %s: %s", instance_id, exc)

    # ------------------------------------------------------------------ terminal

    def open_terminal(self, *, instance_id: str | None = None, host_id: str | None = None,
                      job_id: str | None = None) -> TerminalLaunch:
        given = [x for x in (instance_id, host_id, job_id) if x not in (None, "")]
        if len(given) != 1:
            raise _invalid("give exactly one of instance_id, host_id or job_id")
        factory = self.services.transports
        store = self.services.store
        if instance_id:
            inst, host = self._instance(instance_id)
            target = terminal.instance_terminal(inst, host, factory,
                                                workdir=self._workdir_of(inst))
        elif job_id:
            job = store.get_job(job_id)
            if job is None:
                raise ApiError(404, "not_found", f"job {job_id!r} not found")
            target = terminal.job_terminal(job, self._host(job.host_id), factory)
        else:
            target = terminal.host_terminal(self._host(host_id), factory)  # type: ignore[arg-type]
        prefer = getattr(self.services.config, "default_terminal", "auto")
        key = ("instance", instance_id) if instance_id else (
            ("job", job_id) if job_id else ("host", host_id))  # type: ignore[assignment]
        # One lock for check + launch: two quick clicks must not both open a window.
        with self._terminal_lock:
            now = self._clock()
            for old_key in [k for k, (at, _) in self._recent_terminals.items()
                            if now - at >= TERMINAL_DEDUPE_S]:
                del self._recent_terminals[old_key]
            previous = self._recent_terminals.get(key)  # type: ignore[arg-type]
            if previous is not None:
                return previous[1]
            result = self._launcher(target.argv, target.title, prefer=prefer)
            if result.launched:                      # a failed launch may be retried at once
                self._recent_terminals[key] = (now, result)  # type: ignore[index]
        if instance_id:
            self._touch(instance_id)
        return result

    # ------------------------------------------------------------------ runtime view

    def _inspect_container(self, inst: InstanceRecord, host: HostSpec) -> _Container | None:
        """One ``<rt> inspect`` of the environment's container; ``None`` when it cannot be read."""
        try:
            rt = runtimes.runtime_for(inst.runtime)
            argv = rt.inspect_argv(runtimes.container_name(inst.id))
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
        try:
            res = self.services.transports.for_host(host).run(argv, timeout=30)
        except Exception as exc:  # noqa: BLE001 - an unreachable host is handled by the caller
            log.debug("cloud-compute: inspect of %s failed: %s", inst.id, type(exc).__name__)
            return None
        return _parse_inspect(res.stdout) if res.rc == 0 else None

    def _port_via_runtime(self, inst: InstanceRecord, host: HostSpec,
                          container_port: int) -> int | None:
        """Host port that ``<rt> port`` reports for ``container_port``; ``None`` if none/failed."""
        try:
            rt = runtimes.runtime_for(inst.runtime)
            cid = runtimes.container_name(inst.id) or inst.container_id or ""
            argv = rt.port_argv(cid, container_port)
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
        try:
            res = self.services.transports.for_host(host).run(argv, timeout=30)
        except Exception:  # noqa: BLE001
            return None
        return rt.parse_port(res.stdout) if res.rc == 0 else None

    def _published_ports(self, inst: InstanceRecord, host: HostSpec, info: _Container | None,
                         extra: tuple[int, ...] = ()) -> dict[int, int]:
        """container port -> host port as the runtime reports it *now*.

        The runtime may publish a new host port after a stop and start, so the recorded value is
        only used when the runtime cannot be asked.
        """
        if info is not None and info.ports is not None:
            return dict(info.ports)
        stored = _stored_ports(inst.ports)
        out: dict[int, int] = {}
        for cport in sorted(set(stored) | set(extra)):
            live = self._port_via_runtime(inst, host, cport)
            if live is not None:
                out[cport] = live
            elif cport in stored:                    # could not be asked: keep what is recorded
                out[cport] = stored[cport]
        return out

    def _published_port(self, inst: InstanceRecord, host: HostSpec,
                        container_port: int) -> int | None:
        """Host port recorded for ``container_port`` (``InstanceRecord.ports``), else asked of
        the runtime. Screens and forwards use :meth:`_published_ports`, which checks the runtime
        first; this is the cheap recorded-value lookup."""
        known = _stored_ports(inst.ports).get(container_port)
        return known if known else self._port_via_runtime(inst, host, container_port)

    def _refresh_recorded_ports(self, inst: InstanceRecord) -> InstanceRecord:
        """Have the sandbox module re-read the published ports (``refresh_ports``) when it can.

        Returns the environment record as stored afterwards. Without that method, or when it
        fails, the record is returned as it was; the live mapping is read from the runtime
        either way.
        """
        refresh = getattr(getattr(self.services, "sandboxes", None), "refresh_ports", None)
        if not callable(refresh):
            return inst
        try:
            fresh = refresh(inst.id)
        except Exception as exc:  # noqa: BLE001 - the live mapping is used regardless
            log.debug("cloud-compute: port refresh for %s failed: %s", inst.id, type(exc).__name__)
            return inst
        return fresh if isinstance(fresh, InstanceRecord) else inst

    # ------------------------------------------------------------------ display

    def _reusable_display(self, instance_id: str) -> TunnelRecord | None:
        now = self._now()
        for rec in self.tunnels.list_tunnels(open_only=True):
            if rec.kind != "display" or rec.instance_id != instance_id or rec.status != "open":
                continue
            expires = parse_iso(rec.expires_at)
            if expires is not None and expires <= now:
                continue
            if rec.url:
                return rec
        return None

    def _screen_wait(self, inst: InstanceRecord, image: str) -> tuple[float, bool]:
        """``(seconds to wait for the screen server, environment only just started)``."""
        started = parse_iso(inst.started_at) if inst.started_at else None
        fresh = started is not None and 0 <= (self._now() - started).total_seconds() < FRESH_START_S
        wait = self.screen_waits[0] if fresh and _is_screen_image(image) else self.screen_waits[1]
        return wait, fresh

    def _template_image(self, inst: InstanceRecord, info: _Container | None) -> str:
        try:
            tpl = self.services.store.get_template(inst.template_id)
        except Exception:  # noqa: BLE001
            tpl = None
        return (tpl.image if tpl is not None and tpl.image else (info.image if info else "")) or ""

    def _screen_error(self, inst: InstanceRecord, image: str, fresh: bool,
                      rec: TunnelRecord) -> ApiError:
        """The error for a screen that did not come up (one of three clear outcomes)."""
        name = inst.name or inst.id
        if not (rec.error or "").startswith(NOT_READY):
            why = f": {rec.error}" if rec.error else ""
            return ApiError(503, "unavailable",
                            f"The screen of {name} could not be reached{why}. Try again in a "
                            "few seconds.")
        if not _is_screen_image(image):
            return ApiError(
                409, "needs_setup",
                f"{name} has no screen: its template uses the image {image or 'unknown'}, which "
                "has no screen server. Use the Desktop (noVNC) template, or turn off Show a "
                "screen in this template.")
        if fresh:
            return ApiError(503, "unavailable",
                            f"The screen of {name} is still starting. Try again in a few seconds.")
        return ApiError(503, "unavailable",
                        f"The screen of {name} is not responding. Stop and start the environment, "
                        "then try again.")

    def open_display(self, instance_id: str) -> TunnelRecord:
        inst, host = self._instance(instance_id)
        if host.kind == "slurm":
            raise _invalid("a screen is not available on Slurm hosts")
        if inst.status != "running":
            raise ApiError(409, "conflict", f"environment {inst.id} is {inst.status}, not running")
        if not inst.display:
            raise _invalid(f"environment {inst.id} was started without a screen")
        store = self.services.store
        inst = self._refresh_recorded_ports(inst)    # a restart may have moved the port
        info = self._inspect_container(inst, host)
        ports = self._published_ports(inst, host, info, (runtimes.DISPLAY_PORT,))
        port = ports.get(runtimes.DISPLAY_PORT)
        existing = self._reusable_display(inst.id)
        if existing is not None:
            if port is None or existing.remote_port == port:
                self._touch(inst.id)
                return existing
            self.tunnels.close_tunnel(existing.id)   # the runtime published a new port: stale
        token_before = display.get_display_token(store, inst.id)
        token = token_before or (info.token if info is not None else None)
        if not token:
            raise ApiError(409, "conflict", "screen token not found")
        if token_before is None:
            store.kv_set(display.token_key(inst.id), token)
        if port is None:
            if token_before is None:
                display.clear_display_token(store, inst.id)
            raise ApiError(409, "conflict",
                           f"the screen port of environment {inst.id} is not published; "
                           "start a new environment")
        image = self._template_image(inst, info)
        wait_s, fresh = self._screen_wait(inst, image)
        rec = self.tunnels.open_tunnel(host, kind="display", remote_port=port, instance_id=inst.id,
                                       url_builder=lambda lp: display.display_url(lp, token),
                                       ready_path=f"{token}/vnc_lite.html", http_timeout=wait_s)
        if rec.status != "open":
            err = self._screen_error(inst, image, fresh, rec)
            try:
                self.tunnels.discard_tunnel(rec.id)
            except Exception as exc:  # noqa: BLE001 - the clear error matters more
                log.debug("cloud-compute: discarding tunnel failed: %s", type(exc).__name__)
            if token_before is None:                 # the token this attempt created
                display.clear_display_token(store, inst.id)
            raise err
        self._touch(inst.id)
        return rec

    # ------------------------------------------------------------------ forward

    @staticmethod
    def _not_published(inst: InstanceRecord, port: int, mapping: dict[int, int]) -> ApiError:
        name = inst.name or inst.id
        listed = (f"Published ports: {', '.join(str(p) for p in sorted(mapping))}."
                  if mapping else "No ports are published.")
        return ApiError(409, "conflict",
                        f"Port {port} is not published by {name}. {listed} Add the port under "
                        "Published ports in the template and start a new environment.")

    def forward_instance_port(self, instance_id: str, remote_port: int,
                              local_port: int | None = None) -> tuple[TunnelRecord, int]:
        """Forward a published port of an environment; returns ``(tunnel, container port)``.

        ``remote_port`` is the port inside the environment. A host-side port that maps to one
        (as ``compute_status`` shows) is translated to it.
        """
        remote_port = _check_port(remote_port)
        if local_port is not None:
            local_port = _check_port(local_port, "local_port")
        inst, host = self._instance(instance_id)
        if host.kind == "slurm":
            raise _invalid("port forwarding is not available for Slurm hosts")
        if inst.status != "running":
            raise ApiError(409, "conflict", f"environment {inst.id} is {inst.status}, not running")
        inst = self._refresh_recorded_ports(inst)    # a restart may have moved the ports
        info = self._inspect_container(inst, host)
        mapping = self._published_ports(inst, host, info, (remote_port,))
        if remote_port in mapping:
            container_port = remote_port
        else:
            by_host = [c for c, h in sorted(mapping.items()) if h == remote_port]
            if not by_host:
                raise self._not_published(inst, remote_port, mapping)
            container_port = by_host[0]
        rec = self.tunnels.open_tunnel(host, kind="port", remote_port=mapping[container_port],
                                       local_port=local_port, instance_id=inst.id)
        self._touch(inst.id)
        return rec, container_port

    def forward_port(self, *, instance_id: str | None = None, host_id: str | None = None,
                     remote_port: int, local_port: int | None = None) -> TunnelRecord:
        if bool(instance_id) == bool(host_id):
            raise _invalid("give exactly one of instance_id or host_id")
        remote_port = _check_port(remote_port)
        if local_port is not None:
            local_port = _check_port(local_port, "local_port")
        if instance_id:
            return self.forward_instance_port(instance_id, remote_port, local_port)[0]
        host = self._host(host_id)  # type: ignore[arg-type]
        if host.kind not in ("ssh", "ec2"):
            raise _invalid(f"port forwarding to a host is only available for ssh hosts, "
                           f"not {host.kind}")
        return self.tunnels.open_tunnel(host, kind="port", remote_port=remote_port,
                                        local_port=local_port)

    # ------------------------------------------------------------------ tunnels

    def close_tunnel(self, tunnel_id: str) -> TunnelRecord:
        return self.tunnels.close_tunnel(tunnel_id)

    def close_instance_tunnels(self, instance_id: str) -> list[str]:
        """Close every tunnel (screen and port forwards) bound to an environment."""
        return self.tunnels.close_instance_tunnels(instance_id)

    def list_tunnels(self, open_only: bool = True) -> list[TunnelRecord]:
        return self.tunnels.list_tunnels(open_only=open_only)

    def sweep(self) -> list[str]:
        return self.tunnels.sweep()

    # ------------------------------------------------------------------ browser

    def open_in_browser(self, url: str) -> bool:
        """Open ``url`` in the default browser on this computer; ``False`` if that failed."""
        try:
            return bool(self._opener(url))
        except Exception as exc:  # noqa: BLE001 - never include the URL (it may hold a token)
            log.debug("cloud-compute: could not open the browser: %s", type(exc).__name__)
            return False


def _token_from_inspect(text: str) -> str | None:
    """The display token (``VNC_PASSWORD``) from ``<rt> inspect`` JSON output."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    for item in data if isinstance(data, list) else [data]:
        env = ((item or {}).get("Config") or {}).get("Env") if isinstance(item, dict) else None
        for entry in env or []:
            name, sep, value = str(entry).partition("=")
            if sep and name == display.TOKEN_ENV and display._TOKEN_RE.match(value):
                return value
    return None


def provide(services: Any) -> AccessManager:
    return AccessManager(services)
