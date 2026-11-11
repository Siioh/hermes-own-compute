"""Tests for cloud_compute_core.events."""

from __future__ import annotations

import datetime as dt
import sys
import types

import pytest

from cloud_compute_core import events as events_mod
from cloud_compute_core.events import EVENT_NAMES, EventBus, broadcast_available, provide
from cloud_compute_core.interfaces import ApiError, Services


class FakeStore:
    def __init__(self):
        self.rows: list[tuple[str, dict]] = []

    def append_event(self, name, payload):
        self.rows.append((name, payload))
        return len(self.rows)


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, plugin_id, name, payload):
        self.calls.append((plugin_id, name, payload))


def _hide_hermes(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_events", None)


def test_event_names_match_spec():
    assert set(EVENT_NAMES) == {
        "hosts.changed", "host.status", "instances.changed", "jobs.changed",
        "job.finished", "tunnels.changed", "notify", "agents.changed",
        "instance.expiring", "agent.finished",
    }


def test_emit_stores_and_broadcasts():
    store, rec = FakeStore(), Recorder()
    EventBus(store, broadcaster=rec).emit("hosts.changed", {"ids": ["a"], "reason": "created"})
    assert store.rows == [("hosts.changed", {"ids": ["a"], "reason": "created"})]
    assert rec.calls == [("cloud-compute", "hosts.changed", {"ids": ["a"], "reason": "created"})]


def test_emit_custom_plugin_id():
    rec = Recorder()
    EventBus(None, broadcaster=rec, plugin_id="other").emit("notify", {})
    assert rec.calls == [("other", "notify", {})]


def test_emit_payload_is_json_safe():
    store, rec = FakeStore(), Recorder()
    when = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    EventBus(store, broadcaster=rec).emit("notify", {"at": when, "ids": {"b", "a"}, 1: object})
    payload = store.rows[0][1]
    assert payload["at"] == "2026-01-02T03:04:05Z"
    assert payload["ids"] == ["a", "b"]
    assert isinstance(payload["1"], str)
    assert rec.calls[0][2] == payload


def test_emit_none_payload_is_empty_dict():
    store = FakeStore()
    EventBus(store, broadcaster=Recorder()).emit("notify", None)
    assert store.rows == [("notify", {})]


def test_emit_without_store():
    rec = Recorder()
    EventBus(None, broadcaster=rec).emit("notify", {"title": "t"})
    assert len(rec.calls) == 1


def test_emit_store_failure_does_not_stop_broadcast(caplog):
    class Bad:
        def append_event(self, name, payload):
            raise RuntimeError("disk full")

    rec = Recorder()
    EventBus(Bad(), broadcaster=rec).emit("notify", {})
    assert len(rec.calls) == 1


def test_emit_broadcaster_failure_is_swallowed():
    store = FakeStore()

    def boom(*a):
        raise ValueError("bad")

    EventBus(store, broadcaster=boom).emit("notify", {})
    assert len(store.rows) == 1


@pytest.mark.parametrize("name", ["", "a..b", ".a", "a.", "a b", "../x", "a/b", "notify\n", None, 5, "x" * 200])
def test_invalid_names_are_dropped(name):
    store, rec = FakeStore(), Recorder()
    EventBus(store, broadcaster=rec).emit(name, {"a": 1})
    assert store.rows == [] and rec.calls == []


def test_unknown_but_valid_name_is_allowed():
    store, rec = FakeStore(), Recorder()
    EventBus(store, broadcaster=rec).emit("custom.thing-1", {})
    assert store.rows and rec.calls


def test_emit_noop_when_hermes_unavailable(monkeypatch):
    _hide_hermes(monkeypatch)
    store = FakeStore()
    EventBus(store).emit("hosts.changed", {"ids": []})
    assert store.rows == [("hosts.changed", {"ids": []})]
    assert broadcast_available() is False


def test_emit_uses_hermes_bridge(monkeypatch):
    seen = []
    mod = types.ModuleType("hermes_cli.plugin_events")
    mod.broadcast_plugin_event = lambda pid, ev, payload=None: seen.append((pid, ev, payload))
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_events", mod)
    EventBus(None).emit("jobs.changed", {"ids": ["j"], "host_id": "h"})
    assert seen == [("cloud-compute", "jobs.changed", {"ids": ["j"], "host_id": "h"})]


def test_emit_hermes_bridge_errors_swallowed(monkeypatch):
    mod = types.ModuleType("hermes_cli.plugin_events")

    def boom(*a, **k):
        raise TypeError("payload")

    mod.broadcast_plugin_event = boom
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_events", mod)
    EventBus(None).emit("notify", {})


def test_broadcast_available_guarded(monkeypatch):
    def boom(name):
        raise ValueError("x")

    monkeypatch.setattr(events_mod.importlib.util, "find_spec", boom)
    assert broadcast_available() is False


def test_emit_appends_to_real_store(store):
    rec = Recorder()
    bus = EventBus(store, broadcaster=rec)
    bus.emit("host.status", {"id": "a", "status": "online", "prev": "unknown", "last_error": ""})
    bus.emit("notify", {"level": "info", "title": "t", "body": "b", "ref": {"kind": "host", "id": "a"}})
    rows = store.events_since(0)
    assert [r["name"] for r in rows] == ["host.status", "notify"]
    assert rows[0]["payload"]["id"] == "a"
    assert [c[1] for c in rec.calls] == ["host.status", "notify"]


def test_provide_uses_services_store():
    store = FakeStore()
    bus = provide(Services(store=store))
    assert isinstance(bus, EventBus)
    bus._broadcaster = Recorder()
    bus.emit("notify", {})
    assert len(store.rows) == 1


def test_provide_without_store():
    services = Services()  # store provider missing -> ApiError(503)
    with pytest.raises(ApiError):
        services.store
    bus = provide(services)
    rec = Recorder()
    bus._broadcaster = rec
    bus.emit("notify", {})
    assert len(rec.calls) == 1
