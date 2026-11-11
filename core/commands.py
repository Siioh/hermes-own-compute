"""``/compute [status|hosts|jobs]``: a plain-text summary from the store snapshot.

Reads ``store.snapshot()`` only (no SSH). Standard library only.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .chat_context import TERMINAL_STATES, render_context

__all__ = ["COMMAND_NAME", "COMMAND_HELP", "ARGS_HINT", "render_command", "make_compute_command"]

log = logging.getLogger(__name__)

COMMAND_NAME = "compute"
ARGS_HINT = "[status|hosts|jobs]"
COMMAND_HELP = (
    "Usage: /compute [status|hosts|jobs]\n"
    "  status  hosts, environments and active jobs (default)\n"
    "  hosts   every host with its status and kind\n"
    "  jobs    active and recently finished Slurm jobs"
)


def _g(d: Any, key: str, default: Any = None) -> Any:
    return d.get(key, default) if isinstance(d, dict) else default


def _hosts_text(snap: dict) -> str:
    hosts = snap.get("hosts") or []
    if not hosts:
        return "No hosts configured."
    lines = []
    for h in hosts:
        spec, state = _g(h, "spec", {}), _g(h, "state") or {}
        line = (f"{_g(spec, 'id', '?')}  {_g(spec, 'kind', '?')}  "
                f"{_g(state, 'status', 'unknown')}")
        if _g(state, "last_error"):
            line += f"  ({_g(state, 'last_error')})"
        lines.append(line)
    return "\n".join(lines)


def _jobs_text(snap: dict) -> str:
    jobs = snap.get("jobs") or []
    if not jobs:
        return "No active or recent jobs."
    lines = []
    for j in jobs:
        tail = f"  exit {_g(j, 'exit_code')}" if _g(j, "state") in TERMINAL_STATES \
            and _g(j, "exit_code") else ""
        lines.append(f"{_g(j, 'id', '?')}  {_g(j, 'state', 'UNKNOWN')}  "
                     f"{_g(j, 'name', '')}  on {_g(j, 'host_id', '?')}{tail}")
    return "\n".join(lines)


def render_command(services: Any, raw_args: str) -> str:
    """Text for ``/compute <raw_args>`` (unknown argument: the help text)."""
    arg = (raw_args or "").strip().lower()
    if arg not in ("", "status", "hosts", "jobs"):
        return COMMAND_HELP
    snap = services.store.snapshot()
    if arg == "hosts":
        return _hosts_text(snap)
    if arg == "jobs":
        return _jobs_text(snap)
    return render_context(snap, max_chars=2000)


def make_compute_command(get_services: Callable[[], Any]) -> Callable[[str], str]:
    """Handler for ``register_command``; never raises."""

    def compute(raw_args: str = "") -> str:
        try:
            return render_command(get_services(), raw_args)
        except Exception as exc:  # noqa: BLE001
            log.debug("/compute failed: %s", exc)
            return "Own Compute: could not read the current state."

    return compute
