"""Event bus: records every event in the store and pushes it to the desktop UI.

``emit`` never raises. Events are stored first (so a reconnecting UI can catch
up with ``Store.events_since``) and then broadcast through Hermes, which is a
silent no-op when Hermes' event bridge is not available.

Standard library only; Hermes is imported lazily inside functions.
"""

from __future__ import annotations

import importlib.util
import logging
import re
from typing import Any, Callable

from .interfaces import ApiError
from .models import to_jsonable

__all__ = ["EventBus", "EVENT_NAMES", "EVENT_NAME_RE", "broadcast_available", "provide"]

log = logging.getLogger(__name__)

EVENT_NAMES: tuple[str, ...] = (
    "hosts.changed", "host.status", "instances.changed", "jobs.changed",
    "job.finished", "tunnels.changed", "notify", "agents.changed",
    "instance.expiring", "agent.finished",
)

EVENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")


def broadcast_available() -> bool:
    """True if Hermes' plugin event bridge can be imported."""
    try:
        return importlib.util.find_spec("hermes_cli.plugin_events") is not None
    except (ImportError, ValueError, AttributeError):
        return False


class EventBus:
    def __init__(self, store: Any = None, *,
                 broadcaster: Callable[[str, str, dict], Any] | None = None,
                 plugin_id: str = "cloud-compute"):
        self._store = store
        self._broadcaster = broadcaster
        self._plugin_id = plugin_id

    def emit(self, name: str, payload: dict) -> None:
        """Store and broadcast one event. Never raises."""
        try:
            self._emit(name, payload)
        except Exception as exc:  # noqa: BLE001 - an event bus never raises
            log.warning("event %r failed: %s", name, exc)

    def _emit(self, name: str, payload: dict) -> None:
        if not isinstance(name, str) or len(name) > 128 or not EVENT_NAME_RE.fullmatch(name):
            log.warning("event dropped: invalid name %r", name)
            return
        data = to_jsonable(payload if payload is not None else {})
        if not isinstance(data, dict):
            data = {"value": data}
        if self._store is not None:
            try:
                self._store.append_event(name, data)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not store event %s: %s", name, exc)
        broadcaster = self._broadcaster
        if broadcaster is None:
            try:
                from hermes_cli.plugin_events import broadcast_plugin_event  # type: ignore[import-not-found]
            except ImportError:
                return
            broadcaster = broadcast_plugin_event
        try:
            broadcaster(self._plugin_id, name, data)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not broadcast event %s: %s", name, exc)


def provide(services: Any) -> EventBus:
    """Service provider used by the module registry."""
    try:
        store = services.store
    except ApiError:
        store = None
    return EventBus(store)
