"""Display (Screen) support: state detection and per-instance access token helpers.

Pure and stdlib only. ``display_state`` decides whether the Screen button is
shown and in which form; ``display_run_overrides`` supplies the extra container
settings for a display-enabled instance; the remaining helpers build and redact
the token URL used to open the screen.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from .models import Capabilities, DisplayState, InstanceRecord, TemplateSpec
from .runtimes import DISPLAY_LABEL_VALUE, DISPLAY_PORT, LABEL_DISPLAY
from .transport import SAFE_TOKEN

__all__ = [
    "EMBEDDABLE", "TOKEN_ENV", "token_key", "display_state", "display_run_overrides",
    "clear_display_token",
    "get_display_token", "display_url", "redact_display_url", "parse_ports_entry",
]

EMBEDDABLE = True
"""The screen may be shown inside a sandboxed iframe.

The display image's web server adds ``Access-Control-Allow-Origin: *`` to every
response and performs no Origin check on the websocket, so a frame served from
another origin can load it. Access control is the per-instance token in the URL
path instead of a VNC password prompt, which a sandboxed frame could not show."""

#: Env var carrying the token into the container (name kept for compatibility).
TOKEN_ENV = "VNC_PASSWORD"

_KV_PREFIX = "display-token:"
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_URL_RE = re.compile(
    r"^(http://127\.0\.0\.1:\d+/)([0-9a-f]{32})(/vnc_lite\.html\?path=)([0-9a-f]{32})(/websockify.*)$")


def _check_id(instance_id: object) -> str:
    if (not isinstance(instance_id, str) or not instance_id or len(instance_id) > 255
            or instance_id.startswith("-") or not SAFE_TOKEN.match(instance_id)):
        raise ValueError("invalid instance id")
    return instance_id


def token_key(instance_id: str) -> str:
    """Key under which an instance's display token is stored in the key-value store."""
    return _KV_PREFIX + _check_id(instance_id)


def display_state(caps: Capabilities, template: TemplateSpec | None,
                  inst: InstanceRecord | None) -> tuple[DisplayState, str]:
    """Return ``(state, reason)`` for the Screen button.

    Order of checks: a given instance wins (started without a screen ->
    ``unsupported``; running with one -> ``supported``); then the template (one
    that does not ask for a display -> ``unsupported``, so the UI hides the
    button for it); then the host (Slurm or no container runtime ->
    ``unsupported``; display image present -> ``supported``; runtime but image
    missing -> ``buildable``; never probed -> ``unknown``).
    Reasons are short human strings.
    """
    if inst is not None:
        if not inst.display:
            return "unsupported", "This instance was started without a screen"
        if inst.status == "running":
            return "supported", "The screen is running"
    if template is not None and not template.display:
        return "unsupported", "This template does not ask for a screen"

    is_slurm = caps.scheduler == "slurm" or (caps.batch_jobs and not caps.containers)
    if is_slurm:
        return "unsupported", "Compute nodes sit behind the login node"
    if caps.display == "unknown" and caps.runtime is None and not caps.containers:
        return "unknown", "This host has not been probed for a screen yet"
    if caps.runtime is None or not caps.containers:
        return "unsupported", "No container runtime on the host"
    if caps.display == "supported":
        return "supported", caps.display_reason or "The noVNC display image is present"
    if caps.display == "buildable":
        return "buildable", "The display image is missing; one click builds it on this host"
    if caps.display == "unsupported":
        return "unsupported", caps.display_reason or "This host cannot show a screen"
    return "unknown", caps.display_reason or "This host has not been probed for a screen yet"


def display_run_overrides(instance_id: str, *, store: Any = None
                          ) -> tuple[dict[str, str], list[tuple[str, int | None, int]], dict[str, str]]:
    """Extra run settings for a display instance: ``(env, publish, labels)``.

    A fresh 32-hex token is returned as ``env[VNC_PASSWORD]``; the display port
    is published on loopback with a host-chosen port. When ``store`` is given the
    token is saved under ``display-token:<instance_id>``.
    """
    key = token_key(instance_id)
    token = secrets.token_hex(16)
    if store is not None:
        store.kv_set(key, token)
    return ({TOKEN_ENV: token},
            [("127.0.0.1", None, DISPLAY_PORT)],
            {LABEL_DISPLAY: DISPLAY_LABEL_VALUE})


def clear_display_token(store: Any, instance_id: str) -> bool:
    """Delete the stored display token for an instance; ``True`` if one was removed."""
    return bool(store.kv_delete(token_key(instance_id)))


def get_display_token(store: Any, instance_id: str) -> str | None:
    """The stored display token for an instance, or ``None``."""
    value = store.kv_get(token_key(instance_id))
    return value if isinstance(value, str) and _TOKEN_RE.match(value) else None


def display_url(local_port: int, token: str) -> str:
    """URL that opens the screen; the token is in the path, never a ``password=`` query."""
    if not isinstance(local_port, int) or isinstance(local_port, bool) or not 0 < local_port < 65536:
        raise ValueError("invalid port")
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        raise ValueError("invalid display token")
    return f"http://127.0.0.1:{local_port}/{token}/vnc_lite.html?path={token}/websockify&scale=true"


def redact_display_url(url: str) -> str:
    """Replace the token in a display URL with ``<token>`` (safe to show in chat)."""
    m = _URL_RE.match(url)
    if not m:
        return url
    return f"{m.group(1)}<token>{m.group(3)}<token>{m.group(5)}"


def parse_ports_entry(entry: str) -> int | None:
    """Port from an ``InstanceRecord.ports`` entry such as ``127.0.0.1:41234``."""
    if not isinstance(entry, str):
        return None
    _, sep, port = entry.strip().rpartition(":")
    if not sep or not port.isdigit():
        return None
    n = int(port)
    return n if 0 < n < 65536 else None
