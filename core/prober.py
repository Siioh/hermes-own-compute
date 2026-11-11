"""Host prober: liveness checks, one-script deep probes and connection tests.

A deep probe runs :data:`PROBE_SCRIPT` on the host (through the host's
``exec_prefix`` for Slurm hosts), parses the sectioned output with
:func:`parse_probe` and stores a :class:`HostState`. ``host.status`` is emitted
only when the status changes. The module uses the standard library only and
imports even when the transport or runtime modules are absent: the transport
factory is taken from ``services.transports`` at call time.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from .interfaces import MachineNotRunning
from .models import (
    Capabilities, ExecResult, GpuInfo, HostKind, HostSpec, HostState, HostStatus, Model,
    Partition, Resources, SlurmSettings, utc_now_iso,
)

__all__ = [
    "PROBE_SCRIPT", "PROBE_TIMEOUT_S", "TestStep", "TestResult", "Prober", "parse_probe",
    "classify", "provide", "WORKDIR_CHECK_SCRIPT", "parse_workdir_check", "native_probe",
    "local_status", "LocalEngine", "NativeProbe",
]

#: Wall-clock limit for one deep probe script.
PROBE_TIMEOUT_S = 45.0

_FALLBACK_DISPLAY_TAG = "localhost/cloud-compute-novnc:0.1"


def _display_tag() -> str:
    try:
        from . import runtimes
        tag = getattr(runtimes, "DISPLAY_IMAGE_TAG", None)
        if isinstance(tag, str) and tag:
            return tag
    except Exception:  # noqa: BLE001 - the runtimes module is optional here
        pass
    return _FALLBACK_DISPLAY_TAG


_SCRIPT_TEMPLATE = r"""
# Own Compute host probe: read-only, light, never fails because a tool is missing.
t() { if command -v timeout >/dev/null 2>&1; then timeout 10 "$@"; else "$@"; fi; }
echo "==os"
command -v uname >/dev/null 2>&1 && uname -sm 2>/dev/null
[ -r /etc/os-release ] && grep -E '^PRETTY_NAME=' /etc/os-release 2>/dev/null
echo "==cpu"
command -v nproc >/dev/null 2>&1 && echo "nproc=$(nproc 2>/dev/null)"
[ -r /proc/loadavg ] && echo "load=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)"
echo "==mem"
[ -r /proc/meminfo ] && grep -E '^(MemTotal|MemAvailable):' /proc/meminfo 2>/dev/null
echo "==disk"
if command -v df >/dev/null 2>&1; then
  echo "path=$HOME"
  df -Pk "$HOME" 2>/dev/null | tail -n 1
fi
echo "==gpu"
if command -v nvidia-smi >/dev/null 2>&1; then
  t nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits 2>/dev/null
fi
echo "==runtime"
for rt in podman docker; do
  if command -v "$rt" >/dev/null 2>&1; then
    echo "rt=$rt"
    echo "version=$(t "$rt" version --format '{{.Client.Version}}' 2>/dev/null)"
    if [ "$rt" = podman ]; then
      echo "rootless=$(t podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)"
    else
      docker_sec=$(t docker info --format '{{.SecurityOptions}}' 2>/dev/null)
      if [ -n "$docker_sec" ]; then
        case "$docker_sec" in *rootless*) echo "rootless=true";; *) echo "rootless=false";; esac
      else
        echo "rootless="
      fi
    fi
  fi
done
echo "==display"
for rt in podman docker; do
  if command -v "$rt" >/dev/null 2>&1; then
    if [ "$rt" = podman ]; then
      t podman image exists "@@IMAGE@@" >/dev/null 2>&1 && echo "$rt=yes" || echo "$rt=no"
    else
      t docker image inspect "@@IMAGE@@" >/dev/null 2>&1 && echo "$rt=yes" || echo "$rt=no"
    fi
  fi
done
echo "==slurm"
if command -v sinfo >/dev/null 2>&1; then
  echo "version=$(t sinfo -V 2>/dev/null)"
  sinfo_part=$(t sinfo -h -o '%P|%a|%l|%D|%C|%m|%G' 2>/dev/null); rc=$?
  echo "sinfo_rc=$rc"
  [ -n "$sinfo_part" ] && echo "$sinfo_part" | sed 's/^/part|/'
  sinfo_nodes=$(t sinfo -h -N -o '%N|%P|%C|%m|%e|%T' 2>/dev/null)
  [ -n "$sinfo_nodes" ] && echo "$sinfo_nodes" | sed 's/^/node|/'
fi
if command -v scontrol >/dev/null 2>&1; then
  scontrol_nodes=$(t scontrol show node -o 2>/dev/null)
  [ -n "$scontrol_nodes" ] && echo "$scontrol_nodes" | sed 's/^/scn|/'
fi
echo "==apptainer"
if command -v apptainer >/dev/null 2>&1; then
  t apptainer --version 2>/dev/null
elif command -v singularity >/dev/null 2>&1; then
  t singularity --version 2>/dev/null
fi
exit 0
"""

#: POSIX sh probe. Sections: ==os ==cpu ==mem ==disk ==gpu ==runtime ==display ==slurm ==apptainer.
PROBE_SCRIPT: str = _SCRIPT_TEMPLATE.replace("@@IMAGE@@", _display_tag())


# --------------------------------------------------------------------------- result types


@dataclass
class TestStep(Model):
    __test__ = False  # not a pytest class

    name: Literal["dns", "tcp", "auth", "runtime", "scheduler", "workdir", "display", "gpu"]
    ok: bool | None
    detail: str = ""
    hint: str | None = None
    ms: float | None = None


@dataclass
class TestResult(Model):
    __test__ = False  # not a pytest class

    ok: bool
    status: HostStatus
    steps: list[TestStep] = field(default_factory=list)
    capabilities: Capabilities | None = None


# --------------------------------------------------------------------------- parsing


def _sections(out: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in (out or "").splitlines():
        line = raw.rstrip("\r")
        if line.startswith("==") and re.fullmatch(r"==[a-z]+", line.strip()):
            current = sections.setdefault(line.strip()[2:], [])
        elif current is not None and line.strip():
            current.append(line.strip())
    return sections


def _int(text: Any) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _float(text: Any) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _kv(lines: list[str]) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in lines:
        if "=" in line:
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip()
    return data


def _parse_runtimes(lines: list[str]) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in lines:
        key, _, value = line.partition("=")
        if key == "rt":
            current = found.setdefault(value.strip(), {})
        elif current is not None and "=" in line:
            current[key.strip()] = value.strip()
    return found


def _parse_cpus(text: str) -> tuple[int, int, int, int] | None:
    parts = text.split("/")
    if len(parts) != 4:
        return None
    nums = [_int(p) for p in parts]
    if any(n is None for n in nums):
        return None
    return nums[0], nums[1], nums[2], nums[3]  # type: ignore[return-value]


def _clean_gres(text: str) -> str | None:
    text = text.strip()
    return None if text in ("", "(null)", "N/A") else text


_SCN_KEY_RE = re.compile(r"(?:^|\s)(NodeName|CPUAlloc|CPUTot|RealMemory|AllocMem|FreeMem|"
                         r"Partitions)=(\S*)")


def _parse_scontrol_node(line: str) -> dict[str, Any] | None:
    """One ``scontrol show node -o`` line -> allocation numbers (``None`` without a node name)."""
    found = {k: v for k, v in _SCN_KEY_RE.findall(line)}
    name = found.get("NodeName")
    if not name:
        return None
    parts = [p.strip().rstrip("*") for p in found.get("Partitions", "").split(",") if p.strip()]
    return {
        "name": name, "cpu_alloc": _int(found.get("CPUAlloc")), "cpu_total": _int(found.get("CPUTot")),
        "mem": _int(found.get("RealMemory")), "alloc": _int(found.get("AllocMem")),
        "free": _int(found.get("FreeMem")), "partitions": parts,
    }


def _parse_slurm(lines: list[str]) -> dict[str, Any]:
    """Parse the ``==slurm`` section.

    ``scontrol`` rows (``scn|...``) give what Slurm has *allocated* (``CPUAlloc``/``CPUTot``,
    ``AllocMem``/``RealMemory``); they feed the host meters and the per-partition "free" memory
    (configured minus allocated). Without them the ``sinfo`` rows are used: ``%C`` for CPUs, and
    ``%e`` (FreeMem, an OS number that is not what jobs can still get) for partitions only.
    """
    info: dict[str, Any] = {"version": None, "rc": None, "partitions": [], "nodes": {},
                            "allocated": False}
    node_rows: list[list[str]] = []
    part_rows: list[list[str]] = []
    scn_rows: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("version="):
            m = re.search(r"(\d+(?:\.\d+)+)", line)
            info["version"] = m.group(1) if m else None
        elif line.startswith("sinfo_rc="):
            info["rc"] = _int(line.partition("=")[2])
        elif line.startswith("part|"):
            part_rows.append(line[5:].split("|"))
        elif line.startswith("node|"):
            node_rows.append(line[5:].split("|"))
        elif line.startswith("scn|"):
            row = _parse_scontrol_node(line[4:])
            if row is not None:
                scn_rows.append(row)
    free_by_part: dict[str, int] = {}
    free_known: dict[str, bool] = {}
    nodes: dict[str, dict[str, Any]] = {}
    for row in node_rows:
        if len(row) < 6:
            continue
        name, part, cpus, mem, free, _state = row[:6]
        part = part.rstrip("*")
        free_mb = _int(free)
        if free_mb is not None:
            free_by_part[part] = free_by_part.get(part, 0) + free_mb
            free_known[part] = True
        cpu = _parse_cpus(cpus)
        nodes.setdefault(name, {"cpus": cpu, "mem": _int(mem), "free": free_mb, "alloc": None})
    # scontrol wins: allocated (not OS-free) numbers, one entry per node
    scn_nodes: dict[str, dict[str, Any]] = {}
    for row in scn_rows:
        scn_nodes.setdefault(row["name"], row)
    if scn_nodes:
        info["allocated"] = True
        for name, row in scn_nodes.items():
            cpu_alloc, cpu_total = row["cpu_alloc"], row["cpu_total"]
            cpus = None
            if cpu_alloc is not None and cpu_total is not None:
                cpus = (cpu_alloc, max(0, cpu_total - cpu_alloc), 0, cpu_total)
            old = nodes.get(name, {})
            nodes[name] = {"cpus": cpus or old.get("cpus"), "mem": row["mem"] or old.get("mem"),
                           "free": old.get("free", row["free"]), "alloc": row["alloc"]}
        free_by_part, free_known = {}, {}
        for row in scn_nodes.values():
            if row["mem"] is None or row["alloc"] is None:
                continue
            for part in row["partitions"]:
                free_by_part[part] = free_by_part.get(part, 0) + max(0, row["mem"] - row["alloc"])
                free_known[part] = True
    for row in part_rows:
        if len(row) < 7:
            continue
        pname, avail, limit, count, cpus, mem, gres = row[:7]
        cpu = _parse_cpus(cpus) or (0, 0, 0, 0)
        n_nodes = _int(count) or 0
        mem_per_node = _int(mem.rstrip("+")) or 0
        is_default = pname.endswith("*")
        pname = pname.rstrip("*")
        info["partitions"].append(Partition(
            name=pname, state=avail, nodes=n_nodes, cpus_alloc=cpu[0], cpus_idle=cpu[1],
            cpus_total=cpu[3], mem_total_mb=mem_per_node * n_nodes,
            mem_free_mb=free_by_part.get(pname) if free_known.get(pname) else None,
            gres=_clean_gres(gres), time_limit=limit or None, default=is_default,
        ))
    info["nodes"] = nodes
    return info


def parse_probe(kind: HostKind, out: str, *, runtime_pref: str = "auto"
                ) -> tuple[Capabilities, Resources]:
    """Parse :data:`PROBE_SCRIPT` output into capabilities and resources (pure, tolerant)."""
    sec = _sections(out)
    caps = Capabilities()
    is_slurm = kind == "slurm"

    # os / arch
    os_lines = sec.get("os", [])
    uname = next((ln for ln in os_lines if not ln.startswith("PRETTY_NAME=")), "")
    pretty = next((ln for ln in os_lines if ln.startswith("PRETTY_NAME=")), "")
    uname_parts = uname.split()
    if len(uname_parts) >= 2:
        caps.arch = uname_parts[-1]
    if pretty:
        name = pretty.partition("=")[2].strip().strip('"\'')
        caps.os = re.sub(r"\s*\([^)]*\)\s*$", "", name) or None
    if caps.os is None and uname_parts:
        caps.os = uname_parts[0]

    # resources: cpu / mem / disk / gpu
    cpu = _kv(sec.get("cpu", []))
    cpu_total = _float(cpu.get("nproc"))
    cpu_used = _float(cpu.get("load"))
    mem_total = mem_used = None
    meminfo: dict[str, int] = {}
    for line in sec.get("mem", []):
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            meminfo[m.group(1)] = int(m.group(2))
    if "MemTotal" in meminfo:
        mem_total = meminfo["MemTotal"] // 1024
        if "MemAvailable" in meminfo:
            mem_used = max(0, (meminfo["MemTotal"] - meminfo["MemAvailable"]) // 1024)
    disk_path = disk_total = disk_free = None
    for line in sec.get("disk", []):
        if line.startswith("path="):
            disk_path = line.partition("=")[2].strip() or None
            continue
        fields = line.split()
        if len(fields) >= 6 and _int(fields[1]) is not None and _int(fields[3]) is not None:
            disk_total = round(int(fields[1]) / 1048576, 1)
            disk_free = round(int(fields[3]) / 1048576, 1)
            if disk_path is None:
                disk_path = fields[-1]
    gpus: list[GpuInfo] = []
    for line in sec.get("gpu", []):
        cols = [c.strip() for c in line.split(",")]
        if len(cols) >= 4 and _int(cols[0]) is not None and _int(cols[2]) is not None:
            gpus.append(GpuInfo(index=int(cols[0]), name=cols[1], mem_total_mb=int(cols[2]),
                                mem_used_mb=_int(cols[3]) or 0,
                                util_pct=_float(cols[4]) if len(cols) > 4 else None))
    source: Literal["host", "slurm", "local"] = (
        "slurm" if is_slurm else "local" if kind == "local" else "host")

    # container runtime
    runtimes = _parse_runtimes(sec.get("runtime", []))
    display_imgs = _kv(sec.get("display", []))
    usable = {n: v for n, v in runtimes.items() if v.get("version")}
    chosen: str | None = None
    if not is_slurm:
        order = ["podman", "docker"]
        if runtime_pref in ("podman", "docker"):
            order = [runtime_pref] + [n for n in order if n != runtime_pref]
            if runtime_pref not in usable:
                order = [n for n in order if n != runtime_pref] + [runtime_pref]
        for name in order:
            if name in usable and usable[name].get("rootless") in ("true", "false"):
                chosen = name
                break
        if chosen is None:
            chosen = next((n for n in order if n in usable), None)
        if chosen is not None:
            info = usable[chosen]
            caps.runtime = chosen  # type: ignore[assignment]
            caps.runtime_version = info.get("version") or None
            caps.rootless = {"true": True, "false": False}.get(info.get("rootless", ""))
            caps.containers = info.get("rootless") in ("true", "false")
            caps.port_forward = caps.containers
            caps.interactive_terminal = caps.containers

    # display
    if is_slurm:
        caps.display, caps.display_reason = "unsupported", "Compute nodes sit behind the login node"
    elif chosen is None or not caps.containers:
        caps.display = "unsupported"
        caps.display_reason = "No container runtime on the host"
    elif display_imgs.get(chosen) == "yes":
        caps.display, caps.display_reason = "supported", "The noVNC display image is present"
    else:
        caps.display, caps.display_reason = "buildable", "The noVNC display image can be built"

    # slurm / apptainer
    resources_cpu_total, resources_cpu_used = cpu_total, cpu_used
    if is_slurm:
        caps.interactive_terminal = True
        slurm = _parse_slurm(sec.get("slurm", []))
        if slurm["version"] and slurm["rc"] in (0, None):
            caps.scheduler = "slurm"
            caps.scheduler_version = slurm["version"]
            caps.batch_jobs = True
        elif slurm["version"]:
            caps.scheduler_version = slurm["version"]
        caps.partitions = slurm["partitions"]
        node_cpus = [n["cpus"] for n in slurm["nodes"].values() if n["cpus"]]
        if node_cpus:
            resources_cpu_total = float(sum(c[3] for c in node_cpus))
            resources_cpu_used = float(sum(c[0] for c in node_cpus))
        elif caps.partitions:
            resources_cpu_total = float(sum(p.cpus_total for p in caps.partitions))
            resources_cpu_used = float(sum(p.cpus_alloc for p in caps.partitions))
        node_mem = [n["mem"] for n in slurm["nodes"].values() if n["mem"]]
        if node_mem:
            # Memory is what Slurm has handed out (AllocMem of RealMemory), not the OS's
            # MemFree: unknown (None) when scontrol did not say.
            mem_total = sum(node_mem)
            alloc = [n["alloc"] for n in slurm["nodes"].values() if n["mem"]]
            mem_used = sum(alloc) if alloc and None not in alloc else None
        elif caps.partitions:
            mem_total = sum(p.mem_total_mb for p in caps.partitions)
            mem_used = None
    for line in sec.get("apptainer", []):
        m = re.search(r"(\d+(?:\.\d+)+(?:-\w+)?)", line)
        if m:
            caps.apptainer_version = m.group(1)
            break
    caps.gpus = gpus

    resources = Resources(
        cpu_total=resources_cpu_total, cpu_used=resources_cpu_used, mem_total_mb=mem_total,
        mem_used_mb=mem_used, disk_path=disk_path, disk_total_gb=disk_total,
        disk_free_gb=disk_free, gpus=list(gpus), source=source, sampled_at=utc_now_iso(),
    )
    return caps, resources


# --------------------------------------------------------------------------- classification

_AUTH_RE = re.compile(r"Permission denied|Too many authentication|Host key verification failed",
                      re.I)
_NET_RE = re.compile(r"Connection refused|timed out|Could not resolve|No route to host|"
                     r"Network is unreachable|Connection closed|Connection reset", re.I)


def _short_error(result: Any, limit: int = 200) -> str:
    text = ""
    for line in (getattr(result, "stderr", "") or "").splitlines():
        line = line.strip()
        if line and not line.lower().startswith(("warning:", "pseudo-terminal")):
            text = line
    if not text:
        text = f"exit code {getattr(result, 'rc', '?')}"
    return text if len(text) <= limit else text[:limit - 3] + "..."


def classify(kind: HostKind, tcp_ok: bool, result: Any, caps: Capabilities | None
             ) -> tuple[HostStatus, str | None]:
    """Map a probe outcome to ``(status, last_error)``.

    ``result`` is the :class:`ExecResult` of the probe script (``None`` when TCP failed).
    """
    if not tcp_ok:
        return "offline", "TCP connection failed"
    if result is None:
        return "offline", "No result from the host"
    rc = result.rc
    stderr = result.stderr or ""
    if getattr(result, "timed_out", False):
        return "degraded", "Probe timed out"
    if rc == 255:
        if re.search(r"Host key verification failed", stderr, re.I):
            return "auth_required", ("Host key verification failed: connect once with ssh in a "
                                     "terminal to accept the host key")
        if _AUTH_RE.search(stderr):
            return "auth_required", _short_error(result)
        if _NET_RE.search(stderr):
            return "offline", _short_error(result)
        return "degraded", _short_error(result)
    if rc != 0:
        return "degraded", _short_error(result)
    if "==os" not in (result.stdout or ""):
        return "degraded", "Probe produced no usable output"
    caps = caps or Capabilities()
    if kind == "slurm":
        if caps.scheduler != "slurm":
            return "degraded", "Slurm is not responding (sinfo missing or failing)"
    elif not caps.containers:
        if caps.runtime is None:
            return "degraded", "No container runtime found (install Podman or Docker)"
        return "degraded", f"{caps.runtime} is installed but not usable"
    return "online", None


# --------------------------------------------------------------------------- prober


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 1)


# --------------------------------------------------------------------------- work directory check

#: Seconds Slurm may take to start the one-task check (``srun --immediate``).
WORKDIR_IMMEDIATE_S = 10
#: Wall-clock bound of ``srun`` inside the check; the transport call adds a little slack.
WORKDIR_SRUN_TIMEOUT_S = 20
WORKDIR_CHECK_TIMEOUT_S = 30.0

#: Writes a token file into the work directory on the login node, then reads it back from a
#: compute node with a one-task ``srun`` (1 CPU, 64 MB, 1 minute, bounded by ``timeout``).
#: The token file is removed afterwards. Args: workdir, token, partition, account, qos.
WORKDIR_CHECK_SCRIPT = r"""
d="$1"; tok="$2"; p="$3"; a="$4"; q="$5"
case "$d" in "~") d="$HOME";; "~/"*) d="$HOME/${d#\~/}";; esac
echo "cc_path=$d"
mkdir -p -- "$d" 2>/dev/null || { echo "cc_wd=mkdir_failed"; exit 0; }
d=$(cd -- "$d" 2>/dev/null && pwd -P) || { echo "cc_wd=mkdir_failed"; exit 0; }
echo "cc_path=$d"
f="$d/.cc-workdir-check-$tok"
printf '%s\n' "$tok" > "$f" 2>/dev/null || { echo "cc_wd=not_writable"; exit 0; }
if ! command -v srun >/dev/null 2>&1; then rm -f -- "$f"; echo "cc_wd=no_srun"; exit 0; fi
set -- -N1 -n1 --immediate=@@IMM@@ -t 1 --mem=64M -J cc-workdir-check --chdir=/
[ -n "$p" ] && set -- "$@" -p "$p"
[ -n "$a" ] && set -- "$@" -A "$a"
[ -n "$q" ] && set -- "$@" "--qos=$q"
if command -v timeout >/dev/null 2>&1; then
  out=$(timeout @@TMO@@ srun "$@" sh -c 'echo "cc_node=$(hostname)"; cat -- "$1"' sh "$f" 2>&1); rc=$?
else
  out=$(srun "$@" sh -c 'echo "cc_node=$(hostname)"; cat -- "$1"' sh "$f" 2>&1); rc=$?
fi
rm -f -- "$f"
echo "cc_rc=$rc"
printf '%s\n' "$out" | tail -n 6 | sed 's/^/cc_out=/'
exit 0
""".replace("@@IMM@@", str(WORKDIR_IMMEDIATE_S)).replace("@@TMO@@", str(WORKDIR_SRUN_TIMEOUT_S))

_SHARED_FIX = ("Set this host's Work directory to a folder on storage that the login node and the "
               "compute nodes share (often a project, scratch or /data path; ask the cluster's "
               "admins if unsure). Until then job output cannot be shown.")


def parse_workdir_check(stdout: str, result: Any, workdir: str, token: str) -> TestStep:
    """Turn :data:`WORKDIR_CHECK_SCRIPT` output into the ``workdir`` checklist row (pure)."""
    path, state, rc, node, out = workdir, None, None, None, []
    for raw in (stdout or "").splitlines():
        line = raw.rstrip("\r")
        key, _, value = line.partition("=")
        if key == "cc_path" and value:
            path = value
        elif key == "cc_wd":
            state = value
        elif key == "cc_rc":
            rc = _int(value)
        elif key == "cc_out":
            if value.startswith("cc_node="):
                node = value.partition("=")[2].strip() or None
            else:
                out.append(value)
    out_text = " ".join(x.strip() for x in out if x.strip())
    if state == "mkdir_failed":
        return TestStep("workdir", False, f"Could not create {path} on the login node",
                        "Choose a Work directory this account can create and write to")
    if state == "not_writable":
        return TestStep("workdir", False, f"{path} is not writable on the login node",
                        "Choose a Work directory this account can write to")
    if state == "no_srun":
        return TestStep("workdir", None, f"{path} is writable; srun was not found, so whether "
                        "the compute nodes can see it could not be checked",
                        "Make sure the Work directory is on storage shared with the compute nodes")
    if rc is None:
        if getattr(result, "timed_out", False):
            detail = "Could not check: the check timed out"
        else:
            detail = "Could not check the work directory"
            err = (getattr(result, "stderr", "") or "").strip().splitlines()
            if err:
                detail += f" ({err[-1][:160]})"
        return TestStep("workdir", None, detail, "Run the test again later")
    if rc == 0 and token in out_text:
        where = f"compute node {node}" if node else "a compute node"
        return TestStep("workdir", True, f"{path} is writable and visible from {where}")
    if re.search(r"No such file|cannot open|Permission denied", out_text, re.I) and node:
        return TestStep("workdir", False,
                        f"{path} is writable on the login node but compute node {node} cannot "
                        "see it, so job output written there cannot be read back", _SHARED_FIX)
    if re.search(r"No such file|cannot open", out_text, re.I):
        return TestStep("workdir", False,
                        f"{path} is writable on the login node but the compute nodes cannot see "
                        "it, so job output written there cannot be read back", _SHARED_FIX)
    if rc == 124 or re.search(r"Unable to allocate|immediate|unavailable|not available|"
                              r"Requested node configuration", out_text, re.I):
        return TestStep("workdir", None,
                        f"{path} is writable; could not check it from a compute node (no node "
                        f"was free within {WORKDIR_IMMEDIATE_S} s)",
                        "Run the test again when the cluster is less busy")
    short = out_text[:160] or f"srun exit code {rc}"
    return TestStep("workdir", None,
                    f"{path} is writable; could not check it from a compute node ({short})",
                    "Check the default partition, account and QOS, then run the test again")


class Prober:
    """Liveness, deep probes and connection tests for hosts."""

    def __init__(self, services: Any):
        self.services = services
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- plumbing ------------------------------------------------------------

    def _lock_for(self, host_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(host_id)
            if lock is None:
                lock = self._locks[host_id] = threading.Lock()
            return lock

    def is_probing(self, host_id: str) -> bool:
        """True while a deep probe of ``host_id`` is running."""
        lock = self._lock_for(host_id)
        if lock.acquire(blocking=False):
            lock.release()
            return False
        return True

    def _emit_change(self, host_id: str, status: str, prev: str | None,
                     last_error: str | None) -> None:
        if status == prev:
            return
        self.services.events.emit("host.status", {
            "id": host_id, "status": status, "prev": prev or "unknown", "last_error": last_error,
        })

    def _store_state(self, state: HostState) -> None:
        prev = self.services.store.set_host_state(state)
        self._emit_change(state.host_id, state.status, prev, state.last_error)

    # -- liveness ------------------------------------------------------------

    def _ec2_idle(self, host: HostSpec) -> bool:
        """True for an EC2 host whose machine is not ready: nothing to dial (no network)."""
        if host.kind != "ec2":
            return False
        return not self.services.ec2.is_ready(host)

    def liveness(self, host: HostSpec) -> HostStatus:
        """TCP-only check. Never marks a host online from TCP alone."""
        if self._ec2_idle(host):
            state = self.services.store.get_host_state(host.id)
            return state.status if state is not None else "offline"
        store = self.services.store
        try:
            transport = self.services.transports.for_host(host)
        except MachineNotRunning:       # the machine stopped between the check and now
            state = store.get_host_state(host.id)
            return state.status if state is not None else "offline"
        latency = transport.tcp_check(3.0)
        prev = store.get_host_state(host.id)
        if latency is None:
            if prev is not None and prev.status == "offline":
                return "offline"
            state = HostState(
                host_id=host.id, status="offline",
                last_seen=prev.last_seen if prev else None,
                last_error="TCP connection failed", latency_ms=None,
                capabilities=prev.capabilities if prev else Capabilities(),
                resources=prev.resources if prev else None,
                probed_at=prev.probed_at if prev else None,
            )
            self._store_state(state)
            return "offline"
        if prev is None:
            return "unknown"
        if prev.status in ("online", "degraded"):
            prev.latency_ms = latency
            prev.last_seen = utc_now_iso()
            store.set_host_state(prev)
        return prev.status

    # -- deep probe ----------------------------------------------------------

    def probe(self, host: HostSpec) -> HostState:
        """Run one batched probe script, store the state and emit on change.

        An EC2 host also gets its machine timers and AWS reconciliation here; while its machine
        is not ready nothing is dialled and the state says why."""
        if host.kind == "ec2":
            ec2 = self.services.ec2
            if not ec2.is_ready(host):
                return ec2.offline_state(host)
            try:
                with self._lock_for(host.id):
                    state = self._probe_unlocked(host)
            except MachineNotRunning:   # the machine stopped between the check and now
                return ec2.offline_state(host)
            try:
                ec2.tick(host)
            except Exception as exc:  # noqa: BLE001 - AWS problems never break the probe
                log.info("cloud-compute: EC2 check of %s failed: %s", host.id,
                         getattr(exc, "message", type(exc).__name__))
            return self.services.store.get_host_state(host.id) or state
        with self._lock_for(host.id):
            return self._probe_unlocked(host)

    def _probe_unlocked(self, host: HostSpec) -> HostState:
        store = self.services.store
        transport = self.services.transports.for_host(host)
        now = utc_now_iso()
        prev = store.get_host_state(host.id)
        prev_caps = prev.capabilities if prev else Capabilities()
        prev_res = prev.resources if prev else None
        prev_seen = prev.last_seen if prev else None
        latency = transport.tcp_check(5.0)
        if latency is None:
            state = HostState(
                host_id=host.id, status="offline", last_seen=prev_seen,
                last_error="TCP connection failed", latency_ms=None, capabilities=prev_caps,
                resources=prev_res, probed_at=now)
            self._store_state(state)
            return state
        if host.kind == "local" and _has_native_probe(transport):
            state = self._probe_local(host, transport, latency, prev_seen, now)
            self._store_state(state)
            return state
        result = transport.run_script(PROBE_SCRIPT, timeout=PROBE_TIMEOUT_S)
        caps, resources = parse_probe(host.kind, result.stdout or "",
                                      runtime_pref=host.runtime_pref)
        status, error = classify(host.kind, True, result, caps)
        if "==os" not in (result.stdout or ""):
            caps, resources = prev_caps, prev_res
        elif host.kind == "slurm":      # a probe never re-runs the work-directory check
            caps.workdir_shared = prev_caps.workdir_shared
            caps.workdir_detail = prev_caps.workdir_detail
        state = HostState(
            host_id=host.id, status=status,
            last_seen=prev_seen if status in ("offline", "auth_required") else now,
            last_error=error, latency_ms=latency, capabilities=caps, resources=resources,
            probed_at=now)
        self._store_state(state)
        return state

    # -- refresh -------------------------------------------------------------

    def refresh_all(self, deep: bool) -> list[HostState]:
        """Refresh every host; a host whose probe is still running is skipped."""
        hosts = self.services.store.list_hosts()
        if not hosts:
            return []

        def one(host: HostSpec) -> HostState | None:
            try:
                if not deep:
                    self.liveness(host)
                    return self.services.store.get_host_state(host.id)
                lock = self._lock_for(host.id)
                if not lock.acquire(blocking=False):
                    return self.services.store.get_host_state(host.id)
                try:
                    return self._probe_unlocked(host)
                finally:
                    lock.release()
            except Exception:  # noqa: BLE001 - one host must not break the rest
                return self.services.store.get_host_state(host.id)

        with ThreadPoolExecutor(max_workers=min(4, len(hosts))) as pool:
            results = list(pool.map(one, hosts))
        return [s for s in results if s is not None]

    # -- connection test -----------------------------------------------------

    def _check_workdir(self, spec: HostSpec) -> TestStep:
        """The ``workdir`` row: write a token on the login node, read it from a compute node.

        Bounded (``srun --immediate`` plus ``timeout``, and the transport timeout); any
        failure to run the check is "could not check" (``ok=None``), never an exception.
        Only the connection test runs it: periodic probes never put work in the queue.
        """
        settings = spec.slurm or SlurmSettings()
        workdir = (settings.workdir or SlurmSettings().workdir or "").strip()
        token = uuid.uuid4().hex[:16]
        t0 = time.perf_counter()
        result = None
        try:
            result = self.services.transports.for_host(spec).run(
                ["sh", "-c", WORKDIR_CHECK_SCRIPT, "sh", workdir, token,
                 settings.default_partition or "", settings.account or "", settings.qos or ""],
                timeout=WORKDIR_CHECK_TIMEOUT_S)
            step = parse_workdir_check(getattr(result, "stdout", "") or "", result, workdir, token)
        except Exception as exc:  # noqa: BLE001 - a check problem never fails the test
            step = TestStep("workdir", None, f"Could not check the work directory ({exc})",
                            "Run the test again later")
        step.ms = _ms(t0)
        return step

    def _test_ec2(self, spec: HostSpec) -> TestResult:
        ec2 = self.services.ec2
        data = ec2.test(spec)
        steps = [TestStep(st["name"], st["ok"], st["detail"], st.get("hint"))  # type: ignore[arg-type]
                 for st in data["steps"]]
        caps = None
        if data["ok"] and ec2.is_ready(spec):
            machine = self.test_connection(spec, _machine=True)
            steps += machine.steps
            caps = machine.capabilities
            return TestResult(ok=machine.ok, status=machine.status, steps=steps,
                              capabilities=caps)
        return TestResult(ok=bool(data["ok"]), status=data["status"], steps=steps,
                          capabilities=caps)

    def remember_workdir(self, spec: HostSpec, result: TestResult) -> bool:
        """Keep the work-directory row of a connection test on the SAVED host of the same
        id and address, so the host card can show a failed check with its fix hint.

        Returns True when something was stored. Never raises: remembering is optional.
        """
        try:
            if spec.kind != "slurm":
                return False
            store = self.services.store
            saved = store.get_host(spec.id)
            state = store.get_host_state(spec.id)
            if saved is None or state is None or saved.kind != "slurm"                     or saved.address != spec.address:
                return False
            step = next((s for s in result.steps if s.name == "workdir"), None)
            if step is None or step.detail == "Skipped":
                return False
            detail = step.detail or ""
            if step.ok is False and step.hint:
                detail = f"{detail}. Fix: {step.hint}" if detail else f"Fix: {step.hint}"
            state.capabilities.workdir_shared = step.ok
            state.capabilities.workdir_detail = detail
            store.set_host_state(state)
            return True
        except Exception:  # noqa: BLE001 - optional information
            return False

    def test_connection(self, spec: HostSpec, _machine: bool = False) -> TestResult:
        """Step-by-step check of a (possibly unsaved) host. No store writes, no events.

        An EC2 host is tested against AWS (read-only calls and a DryRun launch); when its
        machine is ready the usual SSH and container checks of the machine follow.
        ``_machine`` runs only the machine checks (used while a machine starts)."""
        if spec.kind == "ec2" and not _machine:
            return self._test_ec2(spec)
        steps: list[TestStep] = []
        factory = self.services.transports
        is_local = spec.kind == "local"
        is_slurm = spec.kind == "slurm"
        raw = factory.for_host(spec, prefixed=False)
        if is_local and _has_native_probe(raw):
            return self._test_local(spec, raw)

        def skipped(*names: str) -> None:
            for n in names:
                steps.append(TestStep(n, None, "Skipped"))  # type: ignore[arg-type]
                if n == "scheduler" and is_slurm:
                    steps.append(TestStep("workdir", None, "Skipped"))

        def finish(status: HostStatus, caps: Capabilities | None = None) -> TestResult:
            return TestResult(ok=status == "online", status=status, steps=steps,
                              capabilities=caps)

        # dns
        if is_local:
            steps.append(TestStep("dns", None, "Local host"))
            dns_failed = False
        else:
            host_name, dns_ms, dns_err = spec.address, None, None
            resolver = getattr(raw, "resolve_target", None)
            if callable(resolver):
                try:
                    target = resolver()
                    host_name = target[0] if isinstance(target, (tuple, list)) else str(target)
                except Exception:  # noqa: BLE001 - fall back to the address
                    pass
            t0 = time.perf_counter()
            try:
                socket.getaddrinfo(host_name, spec.port or 22, type=socket.SOCK_STREAM)
            except OSError as exc:
                dns_err = str(exc)
            dns_ms = _ms(t0)
            dns_failed = dns_err is not None
            if dns_failed:
                steps.append(TestStep(
                    "dns", False, f"Could not resolve {host_name}: {dns_err}",
                    "Check the address for typos, or that it is an alias in your SSH config "
                    "or reachable on your network or Tailscale", dns_ms))
            else:
                steps.append(TestStep("dns", True, f"{host_name} resolves", None, dns_ms))

        # tcp
        t0 = time.perf_counter()
        latency = raw.tcp_check(5.0)
        tcp_ms = _ms(t0)
        if latency is None:
            if dns_failed:
                skipped("tcp", "auth", "runtime", "scheduler", "display", "gpu")
                return finish("offline")
            steps.append(TestStep(
                "tcp", False, f"Could not connect to {spec.address}:{spec.port or 22}",
                "Check the host is on, the port is right and your VPN or Tailscale is up",
                tcp_ms))
            skipped("auth", "runtime", "scheduler", "display", "gpu")
            return finish("offline")
        if dns_failed:  # the address is an alias that SSH resolves itself
            steps[0] = TestStep("dns", True, "Resolved through SSH configuration", None,
                                steps[0].ms)
        steps.append(TestStep(
            "tcp", True, "Connected" if is_local else f"Connected in {latency:.0f} ms",
            None, latency if latency else tcp_ms))

        # auth
        t0 = time.perf_counter()
        auth = raw.run(["true"], timeout=15)
        auth_ms = _ms(t0)
        if auth.rc != 0:
            status, error = classify(spec.kind, True, auth, None)
            if status == "auth_required":
                hint = (f"Run `ssh {spec.address}` once in a terminal to accept the host key / "
                        "unlock your key, or load it into ssh-agent")
            else:
                hint = "Check that SSH works from a terminal: ssh " + spec.address
            steps.append(TestStep("auth", False, error or "Authentication failed", hint, auth_ms))
            skipped("runtime", "scheduler", "display", "gpu")
            return finish(status)
        steps.append(TestStep("auth", True, "Local shell" if is_local else "Logged in", None,
                              auth_ms))

        # probe script (through the exec prefix for Slurm hosts)
        t0 = time.perf_counter()
        result = factory.for_host(spec).run_script(PROBE_SCRIPT, timeout=PROBE_TIMEOUT_S)
        probe_ms = _ms(t0)
        caps, _res = parse_probe(spec.kind, result.stdout or "", runtime_pref=spec.runtime_pref)
        status, error = classify(spec.kind, True, result, caps)
        if result.rc != 0 or "==os" not in (result.stdout or ""):
            steps.append(TestStep(
                "runtime", None if is_slurm else False, error or "Probe failed",
                "Check the exec prefix and that the target shell is reachable"
                if is_slurm else "Check that a POSIX shell is available on the host", probe_ms))
            steps.append(TestStep("scheduler", False if is_slurm else None,
                                  (error or "Probe failed") if is_slurm else "Not a Slurm host",
                                  "Check the exec prefix: the login container or node must be "
                                  "running" if is_slurm else None))
            if is_slurm:
                steps.append(TestStep("workdir", None, "Skipped"))
            skipped("display", "gpu")
            return finish(status, None)

        # runtime
        if is_slurm:
            steps.append(TestStep("runtime", None, "Not used on Slurm hosts (jobs run with "
                                  "Apptainer)" if caps.apptainer_version else
                                  "Not used on Slurm hosts", None, probe_ms))
        elif caps.containers:
            rootless = "rootless" if caps.rootless else "rootful"
            steps.append(TestStep("runtime", True,
                                  f"{caps.runtime} {caps.runtime_version or ''} ({rootless})"
                                  .replace("  ", " "), None, probe_ms))
        elif caps.runtime:
            steps.append(TestStep("runtime", False, f"{caps.runtime} is installed but not usable",
                                  f"Check that `{caps.runtime} info` works for this user", probe_ms))
        else:
            steps.append(TestStep("runtime", False, "No Podman or Docker found",
                                  "Install Podman (rootless) or Docker on the host", probe_ms))

        # scheduler
        if is_slurm:
            if caps.scheduler == "slurm":
                names = ", ".join(p.name for p in caps.partitions) or "no partitions"
                steps.append(TestStep("scheduler", True,
                                      f"Slurm {caps.scheduler_version or ''} ({names})".replace(
                                          "  ", " "), None, probe_ms))
            else:
                steps.append(TestStep("scheduler", False, "sinfo missing or failing",
                                      "Check that Slurm client tools are on the PATH of the login "
                                      "node (or fix the exec prefix)", probe_ms))
            # work directory: writable on the login node and visible from a compute node
            if caps.scheduler == "slurm":
                step = self._check_workdir(spec)
                steps.append(step)
                if step.ok is False and status == "online":
                    status = "degraded"
            else:
                steps.append(TestStep("workdir", None, "Skipped"))
        else:
            steps.append(TestStep("scheduler", None, "Not a Slurm host"))

        # display
        if caps.display in ("supported", "buildable"):
            steps.append(TestStep("display", True, caps.display_reason or caps.display))
        else:
            steps.append(TestStep("display", None, caps.display_reason or "Not available"))

        # gpu
        if caps.gpus:
            names = ", ".join(g.name for g in caps.gpus)
            steps.append(TestStep("gpu", True, f"{len(caps.gpus)} GPU(s): {names}"))
        else:
            steps.append(TestStep("gpu", None, "No GPU detected"))
        return finish(status, caps)


    # -- this computer (kind=local): native checks, no shell and no POSIX tool --------

    def _probe_local(self, host: HostSpec, transport: Any, latency: float | None,
                     prev_seen: str | None, now: str) -> HostState:
        """Deep probe of this computer: Python facts plus the container engine's own output."""
        found = native_probe(transport, host.runtime_pref)
        caps, resources = parse_probe("local", found.result.stdout, runtime_pref=host.runtime_pref)
        status, error = local_status(found.engines, caps)
        return HostState(host_id=host.id, status=status, last_seen=now, last_error=error,
                         latency_ms=latency, capabilities=caps, resources=resources,
                         probed_at=now)

    def _test_local(self, spec: HostSpec, transport: Any) -> TestResult:
        """Connection test of this computer. Nothing runs through a shell."""
        steps: list[TestStep] = [
            TestStep("dns", None, "Local host"),
            TestStep("tcp", True, "Connected", None, 0.0),
            TestStep("auth", True, "Runs on this computer", None, 0.0),
        ]
        t0 = time.perf_counter()
        found = native_probe(transport, spec.runtime_pref)
        probe_ms = _ms(t0)
        caps, _res = parse_probe("local", found.result.stdout, runtime_pref=spec.runtime_pref)
        status, error = local_status(found.engines, caps)
        if caps.containers:
            engine = found.engine(caps.runtime)
            rootless = "rootless" if caps.rootless else "rootful"
            where = f", found at {engine.path}" if engine is not None else ""
            steps.append(TestStep("runtime", True,
                                  f"{caps.runtime} {caps.runtime_version or ''} ({rootless}){where}"
                                  .replace("  ", " "), None, probe_ms))
        elif found.engines:
            engine = found.engine(caps.runtime) or found.engines[0]
            hint = f"Found at {engine.path}"
            if engine.problem:
                hint += f". The engine answered: {engine.problem}"
            steps.append(TestStep("runtime", False, error or "The container engine is not usable",
                                  hint, probe_ms))
        else:
            steps.append(TestStep("runtime", False, error or "No Podman or Docker found",
                                  "Install Docker Desktop or Podman on this computer", probe_ms))
        steps.append(TestStep("scheduler", None, "Not a Slurm host"))
        if caps.display in ("supported", "buildable"):
            steps.append(TestStep("display", True, caps.display_reason or caps.display))
        else:
            steps.append(TestStep("display", None, "No container engine is running on this computer"
                                  if not caps.containers else caps.display_reason or "Not available"))
        if caps.gpus:
            names = ", ".join(g.name for g in caps.gpus)
            steps.append(TestStep("gpu", True, f"{len(caps.gpus)} GPU(s): {names}"))
        else:
            steps.append(TestStep("gpu", None, "No GPU detected"))
        return TestResult(ok=status == "online", status=status, steps=steps,
                          capabilities=caps)


# --------------------------------------------------------------------------- this computer


_ENGINE_TIMEOUT_S = 20.0
_NATIVE_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*")


@dataclass
class LocalEngine:
    """What this computer's Docker or Podman answered. ``path`` is the binary that was found."""

    name: str
    path: str
    version: str = ""
    running: bool = False
    rootless: bool | None = None
    cpus: int | None = None
    mem_bytes: int | None = None
    vm: bool = False          # the engine runs in a VM (Docker Desktop, a Podman machine)
    problem: str = ""         # what the engine said when it did not answer


@dataclass
class NativeProbe:
    """Result of :func:`native_probe`: sectioned text for :func:`parse_probe` plus engine facts."""

    result: ExecResult
    engines: list[LocalEngine] = field(default_factory=list)

    def engine(self, name: str | None) -> LocalEngine | None:
        return next((e for e in self.engines if e.name == name), None)


def _has_native_probe(transport: Any) -> bool:
    """True for a transport that can find the container binaries itself (``LocalTransport``)."""
    return callable(getattr(transport, "runtime_binary", None))


def _arch(machine: str) -> str:
    low = (machine or "").lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64" if platform.system() != "Linux"
            else "aarch64"}.get(low, machine or "unknown")


def _os_facts() -> tuple[str, str, str]:
    """``(short name, pretty name, architecture)`` of this computer, from Python alone."""
    system = platform.system() or "Unknown"
    arch = _arch(platform.machine())
    if system == "Windows":
        return "Windows", f"Windows {platform.release()}".strip(), arch
    if system == "Darwin":
        version = (platform.mac_ver()[0] or "").strip()
        return "macOS", f"macOS {version}".strip(), arch
    if system == "Linux":
        pretty = ""
        try:
            pretty = str(platform.freedesktop_os_release().get("PRETTY_NAME") or "")
        except (OSError, AttributeError):
            pass
        return "Linux", pretty or f"Linux {platform.release()}".strip(), arch
    return system, f"{system} {platform.release()}".strip(), arch


def _loadavg() -> float | None:
    getter = getattr(os, "getloadavg", None)
    if getter is None:
        return None
    try:
        return float(getter()[0])
    except (OSError, ValueError):
        return None


def _json_object(text: str) -> dict:
    try:
        data = json.loads(text or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _engine_facts(transport: Any, name: str, path: str) -> LocalEngine:
    """Ask one installed engine for its version, rootless state, CPUs and memory.

    CPUs and memory are what containers get: on Docker Desktop and a Podman machine that
    is the VM's share, not the whole computer.
    """
    engine = LocalEngine(name=name, path=path)
    res = transport.run([path, "version", "--format", "{{.Client.Version}}"],
                        timeout=_ENGINE_TIMEOUT_S)
    first = (res.stdout or "").strip().splitlines()[:1]
    if first and _NATIVE_VERSION_RE.match(first[0].strip()):
        engine.version = first[0].strip()
    fmt = ["{{json .}}"] if name == "docker" else ["json"]
    info = transport.run([path, "info", "--format", *fmt], timeout=_ENGINE_TIMEOUT_S)
    data = _json_object(info.stdout)
    if info.rc != 0 or not data:
        engine.problem = _short_error(info)
        return engine
    if name == "docker":
        if data.get("ServerErrors") or not data.get("ServerVersion"):
            engine.problem = _short_error(info)
            return engine
        engine.running = True
        options = [str(o) for o in (data.get("SecurityOptions") or [])]
        engine.rootless = any("rootless" in o for o in options)
        engine.cpus = _positive_int(data.get("NCPU"))
        engine.mem_bytes = _positive_int(data.get("MemTotal"))
        engine.vm = str(data.get("OperatingSystem") or "").startswith("Docker Desktop")
        if not engine.version:
            engine.version = str(data.get("ServerVersion") or "")
    else:
        host = data.get("host") if isinstance(data.get("host"), dict) else {}
        security = host.get("security") if isinstance(host.get("security"), dict) else {}
        engine.running = True
        engine.rootless = bool(security.get("rootless"))
        engine.cpus = _positive_int(host.get("cpus"))
        engine.mem_bytes = _positive_int(host.get("memTotal"))
        engine.vm = platform.system() != "Linux"
        if not engine.version:
            version = data.get("version") if isinstance(data.get("version"), dict) else {}
            engine.version = str(version.get("Version") or "")
    return engine


def _nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if platform.system() == "Windows":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("SystemRoot")):
            if base:
                for rel in (("NVIDIA Corporation", "NVSMI"), ("System32",)):
                    candidate = os.path.join(base, *rel, "nvidia-smi.exe")
                    if os.path.exists(candidate):
                        return candidate
    return None


def native_probe(transport: Any, runtime_pref: str = "auto") -> NativeProbe:
    """Probe this computer without a shell: Python facts plus the engine's own answers.

    Returns text in the layout :func:`parse_probe` reads, so one parser serves every host
    kind. The OS name comes from ``platform``, the CPU count from ``os.cpu_count`` (or the
    engine's, when it answers), disk space from ``shutil.disk_usage`` on the home folder,
    and the GPU list from ``nvidia-smi`` when installed. Commands run through ``transport``
    with the full path of the binary, and its own timeout handling.
    """
    engines: list[LocalEngine] = []
    for name in ("podman", "docker"):
        try:
            path = transport.runtime_binary(name)
            if path:
                engines.append(_engine_facts(transport, name, path))
        except Exception as exc:  # noqa: BLE001 - one broken engine must not hide the rest
            engines.append(LocalEngine(name=name, path=str(path or name),
                                       problem=f"{type(exc).__name__}: {exc}"))
    running = [e for e in engines if e.running]
    best = next((e for e in running if e.name == runtime_pref), None) or (
        running[0] if running else None)

    short, pretty, arch = _os_facts()
    lines = ["==os", f"{short} {arch}", f"PRETTY_NAME={pretty}", "==cpu"]
    cpus = best.cpus if best and best.cpus else os.cpu_count()
    if cpus:
        lines.append(f"nproc={cpus}")
    load = _loadavg() if platform.system() == "Linux" and not (best and best.vm) else None
    if load is not None:
        lines.append(f"load={load:.2f}")
    lines.append("==mem")
    if best and best.mem_bytes:
        lines.append(f"MemTotal: {best.mem_bytes // 1024} kB")
    lines.append("==disk")
    folder = os.path.expanduser("~")
    if not os.path.isdir(folder):
        folder = os.getcwd()
    try:
        usage = shutil.disk_usage(folder)
        lines += [f"path={folder}",
                  f"local {usage.total // 1024} {usage.used // 1024} {usage.free // 1024} "
                  f"{usage.used * 100 // max(1, usage.total)}% {folder}"]
    except OSError:
        pass
    lines.append("==gpu")
    smi = _nvidia_smi()
    if smi:
        gpu = transport.run([smi, "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
                             "--format=csv,noheader,nounits"], timeout=10)
        if gpu.rc == 0:
            lines += [ln for ln in (gpu.stdout or "").splitlines() if ln.strip()]
    lines.append("==runtime")
    for e in engines:
        lines += [f"rt={e.name}", f"version={e.version}",
                  "rootless=" + ({True: "true", False: "false"}.get(e.rootless, "")
                                 if e.running else "")]
    lines.append("==display")
    tag = _display_tag()
    for e in running:
        check = ([e.path, "image", "exists", tag] if e.name == "podman"
                 else [e.path, "image", "inspect", tag])
        lines.append(f"{e.name}=" + ("yes" if transport.run(check, timeout=15).rc == 0 else "no"))
    return NativeProbe(ExecResult(rc=0, stdout="\n".join(lines) + "\n", stderr="",
                                  duration_s=0.0), engines)


def _engine_down_message(engine: LocalEngine) -> str:
    """Plain sentence for an engine that is installed but did not answer."""
    label = "Docker" if engine.name == "docker" else "Podman"
    if "permission denied" in engine.problem.lower():
        return (f"{label} is installed but this account may not use it (permission denied). "
                f"Give this account access to {label}, then test again.")
    system = platform.system()
    if engine.name == "docker":
        start = "Start Docker Desktop" if system in ("Windows", "Darwin") else \
            "Start the Docker service"
        return f"Docker is installed but not running. {start}, then test again."
    if system in ("Windows", "Darwin"):
        return "Podman is installed but not running. Start the Podman machine, then test again."
    return ("Podman is installed but `podman info` failed. Check that Podman works for this "
            "user, then test again.")


def local_status(engines: list[LocalEngine], caps: Capabilities) -> tuple[HostStatus, str | None]:
    """Status and message for this computer: online when a container engine answers."""
    if caps.containers:
        return "online", None
    if not engines:
        return "degraded", ("No Podman or Docker found on this computer. Install Docker Desktop "
                            "or Podman, then test again.")
    engine = next((e for e in engines if e.name == caps.runtime), engines[0])
    return "degraded", _engine_down_message(engine)


def provide(services: Any) -> Prober:
    """Service factory used by the registry."""
    return Prober(services)
