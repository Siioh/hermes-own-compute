"""Container runtime helpers: pure argv builders and tolerant output parsers.

Nothing here runs a process. Every builder returns a list of ``str`` that starts
with the runtime name (``podman`` or ``docker``); callers hand it to a transport.
Parsers never raise: malformed input is skipped.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Literal

from .models import InstanceStats, utc_now_iso

__all__ = [
    "MANAGED_LABEL", "LABEL_INSTANCE", "LABEL_INSTALL", "LABEL_TEMPLATE", "LABEL_DISPLAY",
    "DISPLAY_IMAGE_TAG", "DISPLAY_LABEL_VALUE", "DISPLAY_PORT", "RunSpec",
    "ContainerRuntime", "runtime_for", "container_name", "instance_labels", "container_path",
    "RUNTIME_NAMES", "well_known_runtime_paths", "find_runtime",
]

MANAGED_LABEL = "io.cloud-compute.managed"
LABEL_INSTANCE = "io.cloud-compute.instance"
LABEL_INSTALL = "io.cloud-compute.install"
LABEL_TEMPLATE = "io.cloud-compute.template"
LABEL_DISPLAY = "io.cloud-compute.display"
DISPLAY_IMAGE_TAG = "localhost/cloud-compute-novnc:0.1"
DISPLAY_LABEL_VALUE = "novnc:6080"
DISPLAY_PORT = 6080
LOOPBACK = "127.0.0.1"

_BAD_CHARS = re.compile(r"[\s\x00]")

#: Container runtimes the plugin can drive, in the order they are preferred.
RUNTIME_NAMES = ("podman", "docker")


# --------------------------------------------------------------------------- finding the binary


def _install_dirs(platform: str, env: "dict[str, str]", home: str) -> list[str]:
    """Folders where a container runtime is usually installed on ``platform``.

    ``platform`` is a ``sys.platform`` value. Desktop installers do not always put their
    command line tools on PATH (Docker Desktop on Windows does not for a gateway that
    was started from a shortcut), so these are looked at after PATH.
    """
    if platform.startswith("win"):
        join = ntpath.join
        roots = [env.get(k) for k in ("ProgramFiles", "ProgramW6432")]
        roots = [r for r in roots if r] or [r"C:\Program Files"]
        local = env.get("LOCALAPPDATA") or (join(home, "AppData", "Local") if home else "")
        dirs = [join(r, "Docker", "Docker", "resources", "bin") for r in roots]
        dirs += [join(r, "RedHat", "Podman") for r in roots]
        dirs += [join(r, "Rancher Desktop", "resources", "resources", "win32", "bin")
                 for r in roots]
        if local:
            dirs += [join(local, "Programs", "Podman"),
                     join(local, "Programs", "Rancher Desktop", "resources", "resources",
                          "win32", "bin")]
        if home:
            dirs.append(join(home, ".rd", "bin"))
    elif platform == "darwin":
        join = posixpath.join
        dirs = ["/usr/local/bin", "/opt/homebrew/bin"]
        if home:
            dirs += [join(home, ".docker", "bin"), join(home, ".orbstack", "bin")]
        dirs += ["/Applications/Docker.app/Contents/Resources/bin", "/opt/podman/bin"]
        if home:
            dirs.append(join(home, ".rd", "bin"))
    else:
        join = posixpath.join
        dirs = ["/usr/bin", "/usr/local/bin"]
        if home:
            dirs += [join(home, ".local", "bin"), join(home, ".rd", "bin")]
        dirs.append("/snap/bin")
    seen: set[str] = set()
    unique = []
    for d in dirs:
        key = d.lower() if platform.startswith("win") else d
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def well_known_runtime_paths(name: str, *, platform: str | None = None,
                             env: "dict[str, str] | None" = None,
                             home: str | None = None) -> list[str]:
    """Full paths where ``name`` (``podman`` or ``docker``) is usually installed here.

    Defaults to this computer (``sys.platform``, ``os.environ``, the user's home folder).
    Nothing is checked on disk.
    """
    if name not in RUNTIME_NAMES:
        raise ValueError(f"unknown container runtime: {name!r}")
    plat = sys.platform if platform is None else platform
    environ = os.environ if env is None else env
    if home is None:
        home = os.path.expanduser("~")
    win = plat.startswith("win")
    exe = name + (".exe" if win else "")
    join = ntpath.join if win else posixpath.join
    return [join(d, exe) for d in _install_dirs(plat, dict(environ), home)]


def find_runtime(name: str, *, explicit: str = "") -> str | None:
    """The executable for ``name`` (``podman`` or ``docker``), or ``None`` when absent.

    Order: ``explicit`` (a path the host or a setting carries), then PATH, then the
    well-known install folders of this platform (:func:`well_known_runtime_paths`).
    """
    if name not in RUNTIME_NAMES:
        raise ValueError(f"unknown container runtime: {name!r}")
    if explicit and os.path.exists(explicit):
        return explicit
    found = shutil.which(name)
    if found:
        return found
    for candidate in well_known_runtime_paths(name):
        if os.path.exists(candidate):
            return candidate
    return None


def _token(value: object, what: str, *, no_dash: bool = False, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    if not value:
        if allow_empty:
            return value
        raise ValueError(f"{what} must not be empty")
    if _BAD_CHARS.search(value):
        raise ValueError(f"{what} must not contain whitespace or NUL: {value!r}")
    if no_dash and value.startswith("-"):
        raise ValueError(f"{what} must not start with '-': {value!r}")
    return value


def _path_arg(value: object, what: str) -> str:
    """A file path passed as one argv element (never through a shell): spaces are fine.

    Windows home folders such as ``C:\\Users\\John Smith`` contain them; remote ssh
    argv is ``shlex.join``ed and local argv is never shell-parsed.
    """
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    if not value:
        raise ValueError(f"{what} must not be empty")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{what} must not contain NUL or line breaks: {value!r}")
    if value.startswith("-"):
        raise ValueError(f"{what} must not start with '-': {value!r}")
    return value


def _nul_check(arg: object) -> str:
    if not isinstance(arg, str) or "\x00" in arg:
        raise ValueError("command arguments must be strings without NUL")
    return arg


def container_path(value: object, what: str = "path") -> str:
    """An absolute path inside a container, normalised (``..`` and ``//`` resolved).

    Raises ``ValueError`` for a non-string, a relative path, NUL or a line break.
    ``/work/../etc`` becomes ``/etc``; the result never climbs above ``/``.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{what} must not contain NUL or line breaks")
    value = value.strip()
    if not value.startswith("/"):
        raise ValueError(f"{what} must be an absolute path (starting with /): {value!r}")
    norm = posixpath.normpath("/" + value.lstrip("/"))
    return "/" + norm.lstrip("/")


def _port(p: object) -> int:
    if isinstance(p, bool) or not isinstance(p, int) or not 0 < p < 65536:
        raise ValueError(f"invalid port: {p!r}")
    return p


def container_name(instance_id: str) -> str:
    return "cc-" + _token(instance_id, "instance id", no_dash=True)


def instance_labels(instance_id: str, install_id: str, template_id: str,
                    display: bool = False) -> dict[str, str]:
    labels = {
        MANAGED_LABEL: "1",
        LABEL_INSTANCE: _token(instance_id, "instance id"),
        LABEL_INSTALL: _token(install_id, "install id"),
        LABEL_TEMPLATE: _token(template_id, "template id"),
    }
    if display:
        labels[LABEL_DISPLAY] = DISPLAY_LABEL_VALUE
    return labels


@dataclass
class RunSpec:
    name: str
    image: str
    labels: dict[str, str]
    command: list[str]
    workdir: str | None = None
    cpus: float | None = None
    memory_gb: float | None = None
    gpus: int = 0
    env_file: str | None = None
    publish: list[tuple[str, int | None, int]] = field(default_factory=list)
    network: Literal["default", "none"] = "default"


def _memory_arg(gb: float) -> str:
    if gb <= 0:
        raise ValueError("memory_gb must be positive")
    if float(gb).is_integer():
        return f"{int(gb)}g"
    return f"{max(1, int(round(gb * 1024)))}m"


_UNITS = {
    "b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
    "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4,
}
_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$")


def _to_mb(text: str) -> int | None:
    """Parse '120MiB', '105.4MB', '4GiB' into whole MiB."""
    m = _SIZE_RE.match(text or "")
    if not m:
        return None
    unit = _UNITS.get(m.group(2).lower() or "b")
    if unit is None:
        return None
    return int(round(float(m.group(1)) * unit / 1024**2))


def _pct(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _records(out: str) -> list[dict]:
    """JSON array, single object, or JSON lines -> list of dicts; bad parts skipped."""
    text = (out or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        return [data]
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            rows.append(d)
    return rows


def _labels(raw: object) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    if isinstance(raw, str):  # docker: "a=b,c=d"
        for part in raw.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k] = v
    return out


def _created_iso(d: dict) -> str:
    """Container creation time as a UTC ISO string ('' if unknown or unreadable).

    Podman gives ``Created`` as epoch seconds; Docker gives ``CreatedAt`` as
    ``2026-11-09 07:00:00 -0500 EST`` (some builds use ISO 8601 with a ``T``).
    """
    from datetime import datetime, timezone
    raw = _first(d, "Created", "CreatedAt", "created_at", "created")
    try:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            moment = datetime.fromtimestamp(raw, timezone.utc)
        else:
            text = str(raw or "").strip()
            if not text:
                return ""
            if text.isdigit():
                moment = datetime.fromtimestamp(int(text), timezone.utc)
            else:
                text = text.replace("Z", "+00:00")
                parts = text.split()
                if len(parts) >= 3 and parts[2][:1] in "+-":      # drop the trailing zone name
                    text = " ".join(parts[:3])
                    moment = datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
                else:
                    moment = datetime.fromisoformat(text.split(" m=")[0])
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return ""


def _first(d: dict, *keys: str):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


class ContainerRuntime:
    """Argv builders and parsers for one runtime (``podman`` or ``docker``)."""

    def __init__(self, name: str) -> None:
        if name not in ("podman", "docker"):
            raise ValueError(f"unknown container runtime: {name!r}")
        self.name = name

    def _fmt(self) -> list[str]:
        return ["--format", "json"] if self.name == "podman" else ["--format", "{{json .}}"]

    def run_argv(self, spec: RunSpec) -> list[str]:
        argv = [self.name, "run", "-d", "--name", _token(spec.name, "container name", no_dash=True),
                "--init", "--security-opt", "no-new-privileges"]
        for k, v in spec.labels.items():
            _token(k, "label key")
            if "=" in k:
                raise ValueError(f"label key must not contain '=': {k!r}")
            _token(v, "label value", allow_empty=True)
            argv += ["--label", f"{k}={v}"]
        if spec.cpus is not None:
            if spec.cpus <= 0:
                raise ValueError("cpus must be positive")
            argv += ["--cpus", f"{spec.cpus:g}"]
        if spec.memory_gb is not None:
            argv += ["--memory", _memory_arg(spec.memory_gb)]
        if spec.gpus and spec.gpus > 0:
            if self.name == "podman":
                argv += ["--device", "nvidia.com/gpu=all"]
            else:
                argv += ["--gpus", str(int(spec.gpus))]
        if spec.network == "none":
            argv += ["--network", "none"]
        elif spec.network != "default":
            raise ValueError(f"invalid network: {spec.network!r}")
        if spec.env_file:
            argv += ["--env-file", _path_arg(spec.env_file, "env file")]
        if spec.workdir:
            argv += ["-w", _token(spec.workdir, "workdir")]
        for host_ip, host_port, cport in spec.publish:
            if host_ip != LOOPBACK:
                raise ValueError(f"publish host ip must be {LOOPBACK}, got {host_ip!r}")
            hp = "" if host_port is None else str(_port(host_port))
            argv += ["-p", f"{LOOPBACK}:{hp}:{_port(cport)}"]
        argv.append(_token(spec.image, "image", no_dash=True))
        argv += [_nul_check(c) for c in spec.command]
        return argv

    def ps_argv(self, label: str = MANAGED_LABEL + "=1") -> list[str]:
        return [self.name, "ps", "-a", "--filter", f"label={_token(label, 'label filter')}",
                *self._fmt()]

    def stats_argv(self, ids: list[str]) -> list[str]:
        return [self.name, "stats", "--no-stream", *self._fmt(),
                *[_token(i, "container id", no_dash=True) for i in ids]]

    def inspect_argv(self, cid: str) -> list[str]:
        return [self.name, "inspect", _token(cid, "container id", no_dash=True)]

    def lifecycle_argv(self, action: Literal["start", "stop", "rm"], cid: str, *,
                       timeout: int | None = None, volumes: bool = False) -> list[str]:
        """Argv for start/stop/rm; ``timeout`` (seconds) only applies to ``stop`` (``-t N``).

        ``volumes`` only applies to ``rm``: ``-v`` also removes the container's ANONYMOUS
        volumes (the ones an image declares with VOLUME). Named volumes and bind mounts are
        never removed by it, with Podman and Docker alike."""
        cid = _token(cid, "container id", no_dash=True)
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int)
                                    or timeout < 0):
            raise ValueError("timeout must be a non-negative integer")
        if action == "start":
            return [self.name, "start", cid]
        if action == "stop":
            if timeout is not None:
                return [self.name, "stop", "-t", str(timeout), cid]
            return [self.name, "stop", cid]
        if action == "rm":
            if volumes:
                return [self.name, "rm", "-f", "-v", cid]
            return [self.name, "rm", "-f", cid]
        raise ValueError(f"unknown lifecycle action: {action!r}")

    def exec_argv(self, cid: str, cmd: list[str], *, tty: bool = False,
                  workdir: str | None = None, user: str | None = None) -> list[str]:
        argv = [self.name, "exec"]
        if tty:
            argv.append("-it")
        if user is not None:
            argv += ["--user", _token(str(user), "user", no_dash=True)]
        if workdir:
            argv += ["-w", _token(workdir, "workdir")]
        argv.append(_token(cid, "container id", no_dash=True))
        return argv + [_nul_check(c) for c in cmd]

    def cp_out_argv(self, cid: str, path: str) -> list[str]:
        """Write ``path`` from the container to stdout as a tar stream (``cp CID:PATH -``).

        Works on running and stopped containers with podman and docker.
        """
        cid = _token(cid, "container id", no_dash=True)
        return [self.name, "cp", f"{cid}:{container_path(path)}", "-"]

    def cp_in_argv(self, cid: str, dest: str) -> list[str]:
        """Unpack a tar stream read from stdin into ``dest`` in the container (``cp - CID:DEST``).

        ``dest`` must be an existing directory; works on running and stopped containers.
        """
        cid = _token(cid, "container id", no_dash=True)
        return [self.name, "cp", "-", f"{cid}:{container_path(dest, 'destination')}"]

    def logs_argv(self, cid: str, tail: int = 200) -> list[str]:
        if isinstance(tail, bool) or not isinstance(tail, int) or tail < 0:
            raise ValueError("tail must be a non-negative integer")
        return [self.name, "logs", "--tail", str(tail), _token(cid, "container id", no_dash=True)]

    def port_argv(self, cid: str, container_port: int) -> list[str]:
        return [self.name, "port", _token(cid, "container id", no_dash=True),
                f"{_port(container_port)}/tcp"]

    def build_argv(self, tag: str, context: str | None = None) -> list[str]:
        """Build an image from a Containerfile supplied on stdin.

        Without ``context`` there is no build context (``build -t TAG -``), so the
        Containerfile must not COPY files. With a context directory on the target
        host: ``build -t TAG -f - CONTEXT``.
        """
        tag = _token(tag, "image tag", no_dash=True)
        if context is None:
            return [self.name, "build", "-t", tag, "-"]
        return [self.name, "build", "-t", tag, "-f", "-", _token(context, "build context")]

    def image_exists_argv(self, tag: str) -> list[str]:
        tag = _token(tag, "image tag", no_dash=True)
        if self.name == "podman":
            return [self.name, "image", "exists", tag]
        return [self.name, "image", "inspect", tag]

    # ---- parsers -------------------------------------------------------

    def parse_ps(self, out: str) -> list[dict]:
        rows = []
        for d in _records(out):
            cid = _first(d, "Id", "ID", "id")
            names = _first(d, "Names", "Name", "name")
            if isinstance(names, list):
                names = names[0] if names else None
            if not cid and not names:
                continue
            if isinstance(names, str):
                names = names.split(",")[0].lstrip("/")
            rows.append({
                "id": str(cid or ""),
                "name": str(names or ""),
                "image": str(_first(d, "Image", "image") or ""),
                "state": str(_first(d, "State", "state") or "").lower(),
                "status": str(_first(d, "Status", "status") or ""),
                "labels": _labels(_first(d, "Labels", "labels")),
                "ports": _first(d, "Ports", "ports"),
                "created_at": _created_iso(d),
            })
        return rows

    def parse_stats(self, out: str) -> dict[str, InstanceStats]:
        now = utc_now_iso()
        result: dict[str, InstanceStats] = {}
        for d in _records(out):
            cid = _first(d, "id", "ID", "Id", "Container")
            name = _first(d, "name", "Name")
            if not cid and not name:
                continue
            cpu = _pct(_first(d, "cpu_percent", "CPUPerc", "cpu_pct"))
            usage = _first(d, "mem_usage", "MemUsage")
            used = limit = None
            if isinstance(usage, str):
                left, _, right = usage.partition("/")
                used, limit = _to_mb(left), _to_mb(right)
            st = InstanceStats(cpu_pct=cpu, mem_used_mb=used, mem_limit_mb=limit, sampled_at=now)
            for key in (cid, name):
                if key:
                    result[str(key)] = st
        return result

    def parse_port(self, out: str) -> int | None:
        for line in (out or "").splitlines():
            m = re.search(r":(\d{1,5})\s*$", line.strip())
            if m and 0 < int(m.group(1)) < 65536:
                return int(m.group(1))
        return None


def runtime_for(name: str) -> ContainerRuntime:
    return ContainerRuntime(name)
