"""Secret lookup and sandbox variable resolution.

Secrets are read from Hermes' per-profile secret scope, one call at a time and
never cached. Nothing here reads ``.env``, ``auth.json`` or any other file, and
the process environment is never used as a fallback.

Standard library only; Hermes is imported lazily inside functions.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import logging
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import VariablePreview, VariableSpec
from .redact import redact_text

__all__ = ["SecretResolver", "resolve_variables", "provide", "SECRET_NAME_RE",
           "variable_sources", "apply_variable_sources"]

log = logging.getLogger(__name__)

SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_REDACTED = "[redacted]"


_SOURCES: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "cloud_compute_variable_sources", default=None)


@contextlib.contextmanager
def variable_sources(overrides: Mapping[str, str] | None):
    """Within the block, a variable named in ``overrides`` reads the secret given there
    instead of its template source (``{"CC_AGENT_API_KEY": "MY_KEY"}``). Used by a cloud agent
    spawn so the model key comes from the secret its chosen model names. Context-local: other
    threads and requests are not affected. Names that are not valid secret names are ignored."""
    clean = {str(k): str(v) for k, v in (overrides or {}).items()
             if isinstance(v, str) and SECRET_NAME_RE.fullmatch(v)}
    token = _SOURCES.set(clean or None)
    try:
        yield
    finally:
        _SOURCES.reset(token)


def apply_variable_sources(variables: Sequence[VariableSpec]) -> list[VariableSpec]:
    """``variables`` with the active :func:`variable_sources` overrides applied (secret sources only)."""
    overrides = _SOURCES.get()
    out = list(variables)
    if not overrides:
        return out
    return [dataclasses.replace(v, source=f"secret:{overrides[v.name]}")
            if v.name in overrides and str(v.source or "").startswith("secret:") else v
            for v in out]


def resolve_variables(variables: Sequence[VariableSpec], host_id: str,
                      getter: Callable[[str], str | None]
                      ) -> tuple[dict[str, str], list[VariablePreview]]:
    """Decide which sandbox variables are sent to ``host_id``.

    Returns ``(env, previews)``. A variable is skipped when its ``hosts`` list
    excludes the host, when a ``secret:NAME`` is unset or empty, or when its
    source is neither ``literal:<value>`` nor ``secret:<NAME>``. Previews carry
    the reason and never any value.
    """
    env: dict[str, str] = {}
    previews: list[VariablePreview] = []
    for spec in apply_variable_sources(variables):
        source = spec.source or ""
        kind = "secret" if source.startswith("secret:") else "literal"
        send, reason, value = False, "", None
        if spec.hosts is not None and host_id not in spec.hosts:
            reason = f"not allowed on host {host_id}"
        elif source.startswith("literal:"):
            send, value = True, source[len("literal:"):]
        elif source.startswith("secret:") and source[len("secret:"):]:
            secret_name = source[len("secret:"):]
            value = getter(secret_name)
            if value is None:
                reason = f"secret {secret_name} not set"
            else:
                send = True
        else:
            reason = "invalid source"
        if send and value is not None:
            env[spec.name] = value
        previews.append(VariablePreview(name=spec.name, source_kind=kind, scope=spec.scope,
                                        will_send=send, reason=reason))
    return env, previews


class SecretResolver:
    """Reads secrets from the Hermes profile secret scope.

    ``getter`` replaces the Hermes lookup (used by tests). Call :meth:`get`
    only at use time, from a tool handler, dashboard route or similar request
    context, never at import or ``register()`` time: under a multiplexed
    gateway there is no profile scope bound there. Threads you start yourself
    must run inside ``contextvars.copy_context()`` so the scope carries over.
    """

    def __init__(self, getter: Callable[[str], str | None] | None = None):
        self._getter = getter

    def get(self, name: str) -> str | None:
        """The secret's value, or ``None`` if it is unset, empty or unreadable."""
        if not isinstance(name, str) or not SECRET_NAME_RE.fullmatch(name):
            return None
        getter = self._getter
        if getter is None:
            try:
                from agent.secret_scope import get_secret  # type: ignore[import-not-found]
            except ImportError:
                return None
            getter = get_secret
        try:
            value = getter(name)
        except Exception:  # noqa: BLE001 - e.g. UnscopedSecretError without a bound scope
            log.warning("secret %s unavailable: no profile scope", name)
            return None
        return value if isinstance(value, str) and value else None

    def is_set(self, name: str) -> bool:
        return self.get(name) is not None

    def resolve(self, variables: list[VariableSpec], host_id: str
                ) -> tuple[dict[str, str], list[VariablePreview]]:
        """Build the sandbox environment and the matching "what will be sent" previews."""
        return resolve_variables(variables, host_id, self.get)

    def redact(self, text: str, values: Iterable[str]) -> str:
        """Replace each non-empty value (longest first) with ``[redacted]``.

        The local pattern redactor (``core.redact``) is applied afterwards.
        """
        out = text or ""
        for value in sorted({v for v in values if v}, key=len, reverse=True):
            out = out.replace(value, _REDACTED)
        return redact_text(out)


def provide(services: Any) -> SecretResolver:
    """Service provider used by the module registry."""
    return SecretResolver()
