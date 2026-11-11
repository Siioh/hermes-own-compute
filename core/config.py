"""Plugin settings.

:class:`Config` holds the values of ``plugins.entries.cloud-compute.settings``.
Anything missing, of the wrong type or out of range falls back to the default
(or is clamped), so a hand-edited config file can never break the plugin.

Standard library only; Hermes is imported lazily and only inside :meth:`Config.load`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Callable, Mapping

__all__ = ["Config", "LiveConfig", "CONFIG_FIELDS", "PLUGIN_ID", "REFRESH_SECONDS", "provide"]

log = logging.getLogger(__name__)

PLUGIN_ID = "cloud-compute"

#: Metadata for every setting, in declaration order. Mirrors ``config_schema`` in
#: ``plugin.yaml``: ``type`` is ``int``/``bool``/``str``; ``min``/``max`` bound ints;
#: ``choices`` lists the allowed strings. A ``secret`` field is not a Config value: Hermes keeps
#: it in the profile's secrets under ``env`` (read with ``agent.secret_scope.get_secret``).
CONFIG_FIELDS: dict[str, dict[str, Any]] = {
    "liveness_seconds": {
        "type": "int", "default": 15, "min": 5, "max": 3600,
        "description": "Seconds between quick reachability checks of each host (5-3600).",
    },
    "poll_seconds": {
        "type": "int", "default": 60, "min": 30, "max": 3600,
        "description": "Seconds between full status polls of instances, jobs and resources (30-3600).",
    },
    "tunnel_ttl_minutes": {
        "type": "int", "default": 60, "min": 5, "max": 1440,
        "description": "Minutes before an idle SSH tunnel is closed (5-1440).",
    },
    "default_terminal": {
        "type": "str", "default": "auto", "choices": ["auto", "wt", "cmd", "macos", "linux", "copy"],
        "description": "How to open a terminal on this computer. auto (recommended) picks the usual terminal for this operating system. wt and cmd are for Windows, macos is for macOS and linux is for Linux; a value meant for another system behaves like auto. copy only puts the command on the clipboard.",
    },
    "default_idle_minutes": {
        "type": "int", "default": 60, "min": 5, "max": 1440,
        "description": "Default idle timeout for new environments and cloud agents, in minutes (5-1440). A template or request can override it.",
    },
    "default_ttl_minutes": {
        "type": "int", "default": 240, "min": 5, "max": 10080,
        "description": "Default maximum lifetime for new environments and cloud agents, in minutes (5-10080). A template or request can override it. A lower value is raised to 5.",
    },
    "chat_context": {
        "type": "bool", "default": True,
        "description": "Tell the model which compute environments are online.",
    },
    "chat_context_max_chars": {
        "type": "int", "default": 600, "min": 100, "max": 4000,
        "description": "Maximum size of the environment summary added to chat context, in characters (100-4000).",
    },
    "approval": {
        "type": "str", "default": "always", "choices": ["always", "remote-only"],
        "description": "When model-initiated actions need your approval.",
    },
    "notify_job_complete": {
        "type": "bool", "default": True,
        "description": "Show a notification when a Slurm job finishes.",
    },
    "ssh_path": {
        "type": "str", "default": "",
        "description": "Path to the ssh executable; empty uses the one on PATH.",
    },
    "use_asyncssh": {
        "type": "bool", "default": False,
        "description": "Use the asyncssh library instead of the ssh executable when installed.",
    },
    "expiry_warning_minutes": {
        "type": "int", "default": 15, "min": 0, "max": 1440,
        "description": "Warn this many minutes before an environment reaches its time limit (0-1440). 0 = no warning.",
    },
    "auto_delete_days": {
        "type": "int", "default": 0, "min": 0, "max": 365,
        "description": "Delete environments that have been stopped for this many days (0-365). Their files are saved to this computer first. 0 = never delete automatically.",
    },
    "export_max_mb": {
        "type": "int", "default": 2048, "min": 10, "max": 102400,
        "description": "Largest file export, in MB, when saving an environment's files to this computer (10-102400).",
    },
    "exports_dir": {
        "type": "str", "default": "",
        "description": "Folder on this computer for saved copies of environment files. Empty uses the plugin's own data folder.",
    },
    "agent_base_url": {
        "type": "str", "default": "", "label": "Cloud agent model endpoint",
        "description": "OpenAI-compatible base URL for cloud agents, e.g. https://llm.example.net/v1 (plain http only on a private network you trust). "
                       "Used when the agent template sets none.",
    },
    "agent_model": {
        "type": "str", "default": "", "label": "Cloud agent model",
        "description": "Model id cloud agents use. Used when the agent template sets none.",
    },
    "wait_max_seconds": {
        "type": "int", "default": 300, "min": 10, "max": 1800,
        "description": "Longest time a chat tool waits for a job or a cloud agent run to finish before returning (10-1800).",
    },
    "agent_api_key": {
        "type": "secret", "default": "", "env": "CC_AGENT_API_KEY", "label": "Cloud agent model key",
        "description": "Key for the default cloud-agent model, saved as the secret CC_AGENT_API_KEY in this Hermes profile. "
                       "Sent only into cloud-agent sandboxes, after you approve the run. Use a scoped, spend-capped key.",
    },
}

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return {1: True, 0: False}.get(value, default)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return default


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value and abs(value) != float("inf") and value == int(value) else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


_warned: set[tuple[str, int]] = set()
_warned_lock = threading.Lock()


def _warn_clamped(name: str, given: int, applied: int) -> None:
    """Log once per field and value (settings are re-read every second)."""
    with _warned_lock:
        if (name, given) in _warned:
            return
        _warned.add((name, given))
    log.warning("cloud-compute: setting %s=%d is outside the allowed range; using %d instead",
                name, given, applied)


def _coerce(meta: dict[str, Any], value: Any, name: str = "", clamped: dict | None = None) -> Any:
    """Coerce one setting. An int outside ``min``..``max`` is clamped, logged once and, when a
    ``clamped`` dict is given, recorded there as ``{name: {given, applied, min, max}}``."""
    default = meta["default"]
    kind = meta["type"]
    if kind == "bool":
        return _coerce_bool(value, default)
    if kind == "int":
        number = _coerce_int(value)
        if number is None:
            return default
        applied = max(meta["min"], min(meta["max"], number))
        if applied != number:
            _warn_clamped(name or "?", number, applied)
            if clamped is not None:
                clamped[name] = {"given": number, "applied": applied,
                                 "min": meta["min"], "max": meta["max"]}
        return applied
    if kind == "str":
        if not isinstance(value, str):
            return default
        text = value.strip()
        choices = meta.get("choices")
        if choices is not None:
            return text if text in choices else default
        return text
    return default


_NO_CLAMPS: Mapping[str, Mapping[str, int]] = MappingProxyType({})


@dataclass
class Config:
    liveness_seconds: int = 15
    poll_seconds: int = 60
    tunnel_ttl_minutes: int = 60
    default_terminal: str = "auto"
    default_idle_minutes: int = 60
    default_ttl_minutes: int = 240
    chat_context: bool = True
    chat_context_max_chars: int = 600
    approval: str = "always"
    notify_job_complete: bool = True
    ssh_path: str = ""
    use_asyncssh: bool = False
    expiry_warning_minutes: int = 15
    auto_delete_days: int = 0
    export_max_mb: int = 2048
    exports_dir: str = ""
    agent_base_url: str = ""
    agent_model: str = ""
    wait_max_seconds: int = 300

    #: Settings that were out of range and what was applied instead, as
    #: ``{name: {given, applied, min, max}}``. Not a setting (not in ``to_dict``).
    clamped = _NO_CLAMPS

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any] | None) -> "Config":
        """Build a Config from a settings mapping.

        ``None`` or a non-mapping gives the defaults. Unknown keys are ignored,
        "true"/"false"/"1"/"0" strings and numeric strings are coerced, wrong
        types fall back to the default and numbers are clamped to their range.
        """
        if not isinstance(m, Mapping):
            return cls()
        clamped: dict[str, dict[str, int]] = {}
        values = {name: _coerce(meta, m[name], name, clamped)
                  for name, meta in CONFIG_FIELDS.items()
                  if name in m and meta["type"] != "secret"}
        config = cls(**values)
        if clamped:
            object.__setattr__(config, "clamped", MappingProxyType(clamped))
        return config

    @classmethod
    def load(cls) -> "Config":
        """Read the settings from Hermes' loaded config; defaults if unavailable.

        The config object returned by Hermes is shared, so it is only read.
        """
        try:
            from hermes_cli.config import load_config_readonly  # type: ignore[import-not-found]
        except ImportError:
            return cls()
        try:
            return cls.from_mapping(_settings_from(load_config_readonly()))
        except Exception as exc:  # noqa: BLE001 - a bad config must not break the plugin
            log.warning("could not read cloud-compute settings: %s", exc)
            return cls()

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def effective(self) -> dict[str, Any]:
        """The values in force plus ``clamped`` (the settings that were out of range and what
        was applied instead); a small payload for health and settings summaries."""
        return {"settings": self.to_dict(),
                "clamped": {k: dict(v) for k, v in self.clamped.items()}}


def _settings_from(config: Any) -> Mapping[str, Any] | None:
    """``plugins.entries.cloud-compute.settings``, else the legacy ``config`` subtree."""
    node: Any = config
    for key in ("plugins", "entries", PLUGIN_ID):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    if not isinstance(node, Mapping):
        return None
    for key in ("settings", "config"):
        sub = node.get(key)
        if isinstance(sub, Mapping):
            return sub
    return None


#: Shortest time between two re-reads of the settings by a :class:`LiveConfig`.
REFRESH_SECONDS = 1.0

_FIELD_NAMES = frozenset(CONFIG_FIELDS)


class LiveConfig(Config):
    """A :class:`Config` whose values follow the saved settings without a restart.

    The Settings page writes Hermes' ``config.yaml``. Reading any setting from this
    object re-reads it through :meth:`Config.load` (Hermes' public
    ``load_config_readonly``, which is itself a cheap stat-validated cache) at most once per
    ``min_interval`` seconds, so a value saved in Settings is seen by the next read after
    that. :meth:`reload` re-reads at once; callers that act on a user's click (a cloud
    agent spawn) use it so a setting saved a moment ago is never missed.

    The object stays one stable instance, so services that hold on to ``services.config``
    (the transport factory, the managers) see new values on their next attribute read.
    A failed read keeps the last good values.
    """

    def __init__(self, loader: Callable[[], Config] | None = None,
                 min_interval: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        set_ = object.__setattr__
        set_(self, "_loader", loader if loader is not None else Config.load)
        set_(self, "_min_interval", max(0.0, float(REFRESH_SECONDS if min_interval is None else min_interval)))
        set_(self, "_clock", clock)
        set_(self, "_lock", threading.Lock())
        set_(self, "_checked", None)
        set_(self, "_snapshot", Config())
        self.reload()

    def __getattribute__(self, name: str) -> Any:
        if name in _FIELD_NAMES or name == "clamped":
            LiveConfig._refresh_if_due(self)
            return getattr(object.__getattribute__(self, "_snapshot"), name)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("LiveConfig is read-only; save the setting in Settings instead")

    def _refresh_if_due(self) -> None:
        get = object.__getattribute__
        checked = get(self, "_checked")
        if checked is not None and get(self, "_clock")() - checked < get(self, "_min_interval"):
            return
        LiveConfig.reload(self)

    def reload(self) -> "LiveConfig":
        """Re-read the settings now."""
        get = object.__getattribute__
        with get(self, "_lock"):
            object.__setattr__(self, "_checked", get(self, "_clock")())
            try:
                fresh = get(self, "_loader")()
            except Exception as exc:  # noqa: BLE001 - keep the last good values
                log.warning("could not re-read cloud-compute settings: %s", exc)
            else:
                if isinstance(fresh, Config):
                    object.__setattr__(self, "_snapshot", fresh)
        return self


def provide(services: Any) -> Config:
    """Service provider used by the module registry."""
    return LiveConfig()
