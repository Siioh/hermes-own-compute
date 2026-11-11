"""Model endpoints the Hermes profile offers, for the cloud-agent model picker.

``detect`` reads the profile through public Hermes APIs only (passed in as ``api`` so tests use
fake objects): ``hermes_cli.config.load_config_readonly`` and ``get_compatible_custom_providers``
(the ``providers:`` / ``custom_providers:`` entries and the main ``model:`` block) and
``providers.get_provider_profile`` (a built-in provider's sign-in type and key variable names).

An endpoint is *usable* when its key is stored under a secret NAME (``key_env``, or the provider's
key variable): the name is what a cloud agent run sends, after approval. Everything else comes
back separately as *unavailable* with a plain reason: a sign-in, a key written into the config
file, a key command, no key name at all. No key value is ever read into the result: only
whether the secret is set.

Standard library only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

__all__ = ["ProfileApi", "detect", "tidy", "hermes_profile_api", "REASONS"]

_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(/[^\s@]*)?$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,128}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_MODELS = 50

#: Plain-words reasons an endpoint cannot be used by a cloud agent.
REASONS = {
    "sign_in": ("Uses a sign-in, which cannot be sent to a remote sandbox. Cloud agents need an "
                "API-key provider or a model server you run."),
    "inline_key": ("The key is written into the Hermes config file. Cloud agents only send a "
                   "key stored under a secret name: store it as a secret and name it with "
                   "key_env."),
    "key_command": ("The key comes from a command that runs on this computer, which cannot run "
                    "for a remote sandbox. Store the key as a secret and name it with key_env."),
    "no_key": ("No key secret name is set for it. Cloud agents need a key stored under a secret "
               "name (key_env)."),
    "bad_address": "Its address is not a plain http(s) address without credentials.",
}
LOCAL_NOTE = ("This address points to this computer. A sandbox on another host cannot reach it; "
              "use it only with a host on this computer.")


@dataclass
class ProfileApi:
    """The few Hermes calls detection needs; replaced by fakes in tests."""
    load_config: Callable[[], dict]
    custom_providers: Callable[[dict], list]
    provider_profile: Callable[[str], Any]


def hermes_profile_api() -> ProfileApi:
    """The real thing, imported lazily so this module loads without Hermes."""
    from hermes_cli.config import get_compatible_custom_providers, load_config_readonly
    from providers import get_provider_profile
    return ProfileApi(load_config_readonly, get_compatible_custom_providers, get_provider_profile)


def _host(base_url: str) -> str:
    try:
        parts = urlsplit(base_url)
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if host and port else host


def _is_local(base_url: str) -> bool:
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _model_ids(*sources: Any) -> list[str]:
    out: list[str] = []
    for src in sources:
        items = list(src.keys()) if isinstance(src, dict) else (
            list(src) if isinstance(src, (list, tuple)) else [src])
        for item in items:
            mid = _str(item)
            if mid and _MODEL_RE.match(mid) and mid not in out:
                out.append(mid)
    return out[:MAX_MODELS]


def _entry_id(name: str, base_url: str, key_secret: str) -> str:
    digest = hashlib.sha256(f"{name}|{base_url.rstrip('/')}|{key_secret}".encode()).hexdigest()
    return "d-" + digest[:8]


def detect(api: ProfileApi, secret_is_set: Callable[[str], bool]) -> dict:
    """``{"detected": [...], "unavailable": [...]}`` for this Hermes profile.

    A detected item: ``{id, name, source, base_url, endpoint_host, models, model, key_secret,
    key_set, note?}``. An unavailable one: ``{name, source, endpoint_host, kind, reason}``.
    Never raises: a profile that cannot be read gives two empty lists.
    """
    detected: list[dict] = []
    unavailable: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add_unavailable(name: str, source: str, base_url: str, kind: str) -> None:
        item = {"name": name, "source": source, "endpoint_host": _host(base_url),
                "kind": kind, "reason": REASONS[kind]}
        if item not in unavailable:
            unavailable.append(item)

    def add_usable(name: str, source: str, base_url: str, key_secret: str, models: list[str]
                   ) -> None:
        key = (base_url.rstrip("/").lower(), key_secret)
        if key in seen:
            return
        seen.add(key)
        item = {"id": _entry_id(name, base_url, key_secret), "name": name, "source": source,
                "base_url": base_url, "endpoint_host": _host(base_url), "models": models,
                "model": models[0] if models else None, "key_secret": key_secret,
                "key_set": bool(_safe(secret_is_set, key_secret))}
        if _is_local(base_url):
            item["note"] = LOCAL_NOTE
        detected.append(item)

    try:
        config = api.load_config()
        config = config if isinstance(config, dict) else {}
    except Exception:  # noqa: BLE001 - a profile that cannot be read offers nothing
        return {"detected": [], "unavailable": []}

    # -- the main model of the profile
    main = config.get("model")
    if isinstance(main, dict):
        provider = _str(main.get("provider"))
        default = _str(main.get("default")) or _str(main.get("model"))
        base_url = _str(main.get("base_url"))
        if provider and provider != "custom" and not provider.startswith("custom:"):
            profile = _safe(api.provider_profile, provider)
            label = (getattr(profile, "display_name", "") or provider) if profile else provider
            url = base_url or (_str(getattr(profile, "base_url", "")) if profile else "")
            auth = getattr(profile, "auth_type", "api_key") if profile else None
            if profile is None:
                pass                                   # unknown provider: nothing to say
            elif auth != "api_key":
                add_unavailable(label, "profile_model", url, "sign_in")
            elif not url or not _URL_RE.match(url):
                add_unavailable(label, "profile_model", url, "bad_address")
            else:
                names = [n for n in (getattr(profile, "env_vars", ()) or ())
                         if isinstance(n, str) and _SECRET_NAME_RE.match(n)]
                key_name = next((n for n in names if _safe(secret_is_set, n)),
                                names[0] if names else "")
                if key_name:
                    add_usable(label, "profile_model", url, key_name, _model_ids(default))
                else:
                    add_unavailable(label, "profile_model", url, "no_key")
        elif base_url:
            _custom_entry(main, "Main model", "profile_model", add_usable, add_unavailable,
                          default)

    # -- the custom endpoints
    try:
        entries = api.custom_providers(config)
    except Exception:  # noqa: BLE001
        entries = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            _custom_entry(entry, _str(entry.get("name")) or "Custom endpoint",
                          "custom_provider", add_usable, add_unavailable, "")
    return {"detected": detected, "unavailable": unavailable}


def _endpoint_key(base_url: Any) -> str:
    return str(base_url or "").strip().rstrip("/").lower()


def tidy(saved: list[dict], detected: list[dict], unavailable: list[dict]
         ) -> tuple[list[dict], list[dict]]:
    """The profile's entries as the picker shows them: each endpoint and model once.

    * a profile entry that has the same endpoint and model as a saved choice is hidden (the
      saved choice is the one that gets picked);
    * profile entries with the same endpoint and model under different provider names are
      merged into the first;
    * an unavailable entry is hidden when a saved or usable profile entry already uses its
      address, and unavailable entries for one address and reason are shown once.
    """
    taken = {(_endpoint_key(m.get("base_url")), str(m.get("model") or "").strip())
             for m in saved if isinstance(m, dict)}
    kept: list[dict] = []
    for item in detected:
        key = (_endpoint_key(item.get("base_url")), str(item.get("model") or "").strip())
        if key in taken:
            continue
        taken.add(key)
        kept.append(item)
    hosts = {str(m.get("endpoint_host") or "").lower() for m in saved if isinstance(m, dict)}
    hosts |= {_host(str(m.get("base_url") or "")).lower() for m in saved if isinstance(m, dict)}
    hosts |= {str(d.get("endpoint_host") or "").lower() for d in detected}
    hosts.discard("")
    shown: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in unavailable:
        host = str(item.get("endpoint_host") or "").lower()
        key = (host, str(item.get("kind") or ""))
        if (host and host in hosts) or (host and key in seen):
            continue
        seen.add(key)
        shown.append(item)
    return kept, shown


def _custom_entry(entry: dict, name: str, source: str, add_usable, add_unavailable,
                  default: str) -> None:
    base_url = _str(entry.get("base_url"))
    key_env = _str(entry.get("key_env")) or _str(entry.get("api_key_env"))
    if not base_url or not _URL_RE.match(base_url):
        add_unavailable(name, source, "", "bad_address")
        return
    if key_env and _SECRET_NAME_RE.match(key_env):
        models = _model_ids(default, entry.get("model"), entry.get("models"))
        add_usable(name, source, base_url, key_env, models)
    elif _str(entry.get("api_key")):
        add_unavailable(name, source, base_url, "inline_key")
    elif _str(entry.get("key_cmd")):
        add_unavailable(name, source, base_url, "key_command")
    else:
        add_unavailable(name, source, base_url, "no_key")


def _safe(fn: Callable, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - detection is advisory
        return None
