"""The catalog of host types the plugin can connect to.

Each entry describes what a host type is for, which capabilities it offers and
what the user has to provide to add one. :data:`HOST_TYPES` is the exact JSON
shape served by ``GET /host-types``: a list of four objects with the keys
``kind``, ``title``, ``one_liner``, ``when_to_use``, ``capabilities``,
``capability_notes``, ``needs``, ``learn_more_topic`` and ``mvp``.

``capabilities`` says yes, no or partial per flag. ``capability_notes`` holds a
plain sentence for the flags that depend on the machine (the UI shows it
instead of the word "partial"). A flag without a note shows no extra text.

The module is pure data plus small lookups: no I/O, standard library only.
"""

from __future__ import annotations

import copy
from typing import Any

from .models import Capabilities

__all__ = [
    "CAPABILITY_FLAGS", "CAPABILITY_VALUES", "HOST_TYPES",
    "host_types", "get_host_type", "default_capabilities", "is_host_kind",
]

#: Every capability flag, in the order the UI shows its chips.
CAPABILITY_FLAGS: tuple[str, ...] = (
    "containers",
    "batch_jobs",
    "interactive_terminal",
    "display",
    "port_forward",
    "gpu",
    "agents",
    "costs_money",
)

#: One of these per flag in every entry's ``capabilities`` object.
CAPABILITY_VALUES: tuple[str, ...] = ("yes", "no", "partial")

# Titles, one-liners, "when to use" texts, capability notes and help topics below
# are the strings the Desktop page shows. Keep them neutral: no "we" or "I", and no
# reference to one operating system.
HOST_TYPES: list[dict[str, Any]] = [
    {
        "kind": "ssh",
        "title": "SSH host",
        "one_liner": "Run sandboxed containers on a Linux machine you can SSH into.",
        "when_to_use": "Your own laptop, workstation, homelab or VM.",
        "capabilities": {
            "containers": "yes",
            "batch_jobs": "no",
            "interactive_terminal": "yes",
            "display": "partial",
            "port_forward": "yes",
            "gpu": "partial",
            "agents": "partial",
            "costs_money": "no",
        },
        "capability_notes": {
            "display": "Needs the display image on the host; the Screen button offers to build it.",
            "gpu": "If the host has a GPU that Docker or Podman can use.",
            "agents": "Off until allowed in the host's sandbox defaults.",
        },
        "needs": [
            "An SSH alias from your SSH config, or host, user and port. Optional jump host.",
            "A key loaded in your SSH agent, or a key file path.",
            "Podman (rootless is fine) or Docker on the remote machine.",
        ],
        "learn_more_topic": "ssh-host",
        "mvp": True,
    },
    {
        "kind": "slurm",
        "title": "Slurm login node",
        "one_liner": "Submit batch jobs to an HPC cluster through its login node.",
        "when_to_use": "HPC or lab clusters, sweeps, scheduled work.",
        "capabilities": {
            "containers": "no",
            "batch_jobs": "yes",
            "interactive_terminal": "partial",
            "display": "no",
            "port_forward": "no",
            "gpu": "partial",
            "agents": "no",
            "costs_money": "partial",
        },
        "capability_notes": {
            "interactive_terminal": "A shell on the login node, or inside a running job.",
            "gpu": "If your cluster has GPUs.",
            "costs_money": "Only if your site bills for cluster time.",
        },
        "needs": [
            "An SSH alias or host, user and port, as for an SSH host. Optional partition and work directory.",
            "The same SSH key; if your site requires MFA, log in once in a terminal first.",
            "sbatch, squeue, sacct and scancel on the PATH of a login shell.",
        ],
        "learn_more_topic": "slurm",
        "mvp": True,
    },
    {
        "kind": "local",
        "title": "Local Docker/Podman",
        "one_liner": "Run containers on this computer.",
        "when_to_use": "Quick tests without another machine.",
        "capabilities": {
            "containers": "yes",
            "batch_jobs": "no",
            "interactive_terminal": "yes",
            "display": "partial",
            "port_forward": "yes",
            "gpu": "partial",
            "agents": "partial",
            "costs_money": "no",
        },
        "capability_notes": {
            "display": "Needs the display image on this computer; the Screen button offers to build it.",
            "port_forward": "Forwards the screen and any ports a template publishes.",
            "gpu": "If Docker or Podman GPU access works on this computer.",
            "agents": "Off until allowed in the host's sandbox defaults.",
        },
        "needs": [
            "No endpoint and no login: everything happens on this computer.",
            "Docker Desktop or Podman on this computer, reachable from the command line.",
        ],
        "learn_more_topic": "local",
        "mvp": True,
    },
    {
        "kind": "ec2",
        "title": "EC2",
        "one_liner": "Start an AWS machine on demand and run containers on it like an SSH host.",
        "when_to_use": "You need more CPU than you own, for a few hours, with a time limit.",
        "capabilities": {
            "containers": "yes",
            "batch_jobs": "no",
            "interactive_terminal": "yes",
            "display": "partial",
            "port_forward": "yes",
            "gpu": "partial",
            "agents": "partial",
            "costs_money": "yes",
        },
        "capability_notes": {
            "display": "Needs about 2 GB of memory; a 1 GB instance type is too small for it.",
            "gpu": "Only with a GPU instance type that the AWS account's quota allows.",
            "agents": "Off until allowed in the host's sandbox defaults.",
            "costs_money": "Billed by AWS while the machine runs, and a little for its disk while it is stopped.",
        },
        "needs": [
            "An AWS region and an IAM user's key, stored as Hermes secrets (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY).",
            "Nothing on a remote machine: the instance is created, given Docker and stopped for you.",
            "One lookup of this computer's public address (checkip.amazonaws.com) for the SSH rule, unless you type it.",
        ],
        "learn_more_topic": "ec2",
        "mvp": True,
    },
]

_BY_KIND: dict[str, dict[str, Any]] = {entry["kind"]: entry for entry in HOST_TYPES}


def host_types() -> list[dict[str, Any]]:
    """All host types in display order, as a list of copies.

    Callers may change the result freely; the catalog itself never changes.
    """
    return copy.deepcopy(HOST_TYPES)


def get_host_type(kind: str) -> dict[str, Any] | None:
    """The catalog entry for ``kind``, or ``None`` when it is not a host type."""
    entry = _BY_KIND.get(kind) if isinstance(kind, str) else None
    return copy.deepcopy(entry) if entry is not None else None


def _ssh_like() -> Capabilities:
    return Capabilities(
        containers=True,
        batch_jobs=False,
        interactive_terminal=True,
        port_forward=True,
        display="unknown",
    )


def default_capabilities(kind: str) -> Capabilities:
    """The capabilities a probe starts from for ``kind``.

    Only what the host type itself implies is set: everything that has to be
    measured on the machine (OS, runtime, GPUs, display support) stays unknown
    until a probe fills it in. An unknown kind raises ``ValueError``.
    """
    if kind in ("ssh", "local", "ec2"):
        return _ssh_like()
    if kind == "slurm":
        return Capabilities(
            containers=False,
            batch_jobs=True,
            interactive_terminal=True,
            port_forward=False,
            display="unsupported",
            display_reason="compute nodes sit behind the login node",
            scheduler="slurm",
        )
    raise ValueError(f"unknown host kind: {kind!r}")


def is_host_kind(kind: Any) -> bool:
    """True when ``kind`` names one of the catalog's host types."""
    return isinstance(kind, str) and kind in _BY_KIND
