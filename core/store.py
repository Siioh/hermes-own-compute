"""SQLite persistence for hosts, templates, instances, jobs, tunnels and events.

The database lives at ``plugin_data_dir("cloud-compute")/cloud-compute.db``
(``<hermes home>/plugin-data/cloud-compute/``) unless a path is given. It uses
WAL journaling when available, a 5 s busy timeout and one connection per
thread (closed when the thread ends), and every write runs in a short explicit transaction, so several
:class:`Store` objects (or processes) can share one file. Each database operation holds its
connection's lock, and :meth:`Store.close` takes that lock before closing a connection, so
no connection is closed while a statement runs on it (that crashes CPython).

Schema (version 1, ``PRAGMA user_version``):

- ``hosts``, ``host_state``, ``templates``, ``instances``, ``jobs``, ``tunnels``:
  ``id TEXT PRIMARY KEY, host_id TEXT, status TEXT, data TEXT, updated_at TEXT``
  where ``data`` is the JSON of the model's ``to_dict()``.
- ``records(kind, id, host_id, status, data, updated_at)``: generic JSON
  records keyed by ``(kind, id)`` for feature areas without a dedicated table
  (for example agent runs). See :meth:`Store.put_record`.
- ``events(seq, ts, name, payload)``, ``leases(name, owner, expires_at)``,
  ``kv(key, value)``.

The store persists exactly what callers pass in. It never reads or adds
secret values. Standard library only.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os as _os
import sqlite3
import stat as _stat
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Iterable

from .interfaces import ApiError
from .models import (
    ACTIVE_JOB_STATES,
    TERMINAL_JOB_STATES,
    HostSpec,
    HostState,
    InstanceRecord,
    JobRecord,
    TemplateSpec,
    TunnelRecord,
    parse_iso,
    utc_now_iso,
)

__all__ = ["Store", "StoreClosedError", "provide", "SCHEMA_VERSION", "MAX_EVENTS",
           "PRUNE_EVERY", "DB_FILENAME", "OPEN_TUNNEL_STATUSES", "CLOSE_WAIT_S"]

log = logging.getLogger(__name__)

PLUGIN_NAME = "cloud-compute"
DB_FILENAME = "cloud-compute.db"
SCHEMA_VERSION = 1
#: Upper bound on rows kept in the ``events`` table.
MAX_EVENTS = 5000
#: Prune old events once every this many appends.
PRUNE_EVERY = 100
BUSY_TIMEOUT_MS = 10000
#: How long :meth:`Store.close` waits in total for statements still running on other
#: threads' connections. A connection still busy after that is closed by its own thread
#: when its statement ends (never from here while it runs).
CLOSE_WAIT_S = 3.0
#: Tunnel statuses counted as open by :meth:`Store.list_tunnels`.
OPEN_TUNNEL_STATUSES: tuple[str, ...] = ("starting", "open")
#: How long finished jobs stay in :meth:`Store.snapshot`.
RECENT_JOB_SECONDS = 24 * 3600

_ENTITY_TABLES = ("hosts", "host_state", "templates", "instances", "jobs", "tunnels")

_SCHEMA = "".join(
    f"""
CREATE TABLE IF NOT EXISTS {t} (
    id TEXT PRIMARY KEY, host_id TEXT, status TEXT, data TEXT NOT NULL, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_{t}_host ON {t}(host_id);
CREATE INDEX IF NOT EXISTS idx_{t}_status ON {t}(status);
""" for t in _ENTITY_TABLES
) + """
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL, id TEXT NOT NULL, host_id TEXT, status TEXT, data TEXT NOT NULL,
    updated_at TEXT, PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS idx_records_host ON records(kind, host_id);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(kind, status);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, name TEXT NOT NULL, payload TEXT
);
CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def _default_path() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - running outside Hermes
        plugin_data_dir = None
    if plugin_data_dir is not None:
        try:
            return Path(plugin_data_dir(PLUGIN_NAME)) / DB_FILENAME
        except Exception as exc:  # noqa: BLE001
            log.warning("cloud-compute: plugin_data_dir failed: %s", exc)
    # No env read: a bare HERMES_HOME could name another profile. get_hermes_home() is the
    # public, profile-aware resolver (context-local override first).
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
        return Path(get_hermes_home()) / "plugin-data" / PLUGIN_NAME / DB_FILENAME
    except Exception:  # noqa: BLE001 - running outside Hermes
        pass
    raise ApiError(503, "unavailable", "plugin storage not available")


#: POSIX modes for the data folder and the database files (owner only).
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_DB_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _owned(st: Any) -> bool:
    getuid = getattr(_os, "getuid", None)
    return getuid is not None and st.st_uid == getuid()


def _secure_files(db_path: Path, *, tighten_folder: bool) -> None:
    """Owner-only permissions on POSIX: folder 0700, database files 0600.

    A missing database file is created empty with 0600 before SQLite opens it (SQLite
    gives its -wal and -shm files the database file's mode). Existing files and the
    folder are tightened only when the current user owns them; ``tighten_folder`` is
    False for a caller-chosen folder the store did not create. Windows is left to the
    user profile's ACLs. Never raises: a filesystem that refuses chmod keeps working.
    """
    if _os.name != "posix":
        return
    try:
        folder = db_path.parent
        if tighten_folder:
            st = folder.stat()
            if _owned(st) and (st.st_mode & 0o777) != _DIR_MODE:
                folder.chmod(_DIR_MODE)
        if not db_path.exists():
            fd = _os.open(str(db_path), _os.O_CREAT | _os.O_WRONLY | getattr(_os, "O_NOFOLLOW", 0),
                          _FILE_MODE)
            _os.close(fd)
        for suffix in _DB_SUFFIXES:
            p = db_path.with_name(db_path.name + suffix)
            try:
                st = p.lstat()
            except FileNotFoundError:
                continue
            if _stat.S_ISREG(st.st_mode) and _owned(st) and st.st_mode & 0o077:
                p.chmod(_FILE_MODE)
    except OSError as exc:
        log.debug("cloud-compute: could not tighten database permissions: %s", exc)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


class StoreClosedError(RuntimeError):
    """The store was closed (plugin unloaded or shutting down); open a new one to go on."""

    def __init__(self, message: str = "store is closed"):
        super().__init__(message)


class _Use:
    """Context manager: this thread's connection, locked for one database operation.

    The holder's lock is held from the first statement to the last fetch (or to COMMIT
    for a transaction), so :meth:`Store.close` never closes a connection while a
    statement runs on it. Nothing else is locked while it is held, so it cannot
    deadlock with ``close()`` (which takes the store lock and the holder locks one at
    a time, never both).
    """

    def __init__(self, store: "Store", tx: bool = False, immediate: bool = True):
        self.store = store
        self.tx = tx
        self.immediate = immediate
        self.holder: _ConnHolder | None = None

    def __enter__(self) -> sqlite3.Connection:
        holder = self.store._holder()
        holder.lock.acquire()
        try:
            conn = holder.conn
            if conn is None or self.store._closed:
                raise StoreClosedError()
            if self.tx:
                conn.execute("BEGIN IMMEDIATE" if self.immediate else "BEGIN")
        except BaseException:
            holder.lock.release()
            raise
        self.holder = holder
        return conn

    def __exit__(self, exc_type, exc, tb) -> None:
        holder = self.holder
        assert holder is not None
        try:
            conn = holder.conn
            if self.tx and conn is not None:
                if exc_type is None:
                    conn.execute("COMMIT")
                else:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
        finally:
            try:
                if self.store._closed:
                    holder.close_locked()     # close() ran while this statement was busy
            finally:
                holder.lock.release()


class _ConnHolder:
    """One thread's SQLite connection; closed when the thread ends or on :meth:`close`.

    The holder lives in a ``threading.local``, so CPython drops it (and ``__del__``
    closes the connection) as soon as its thread exits. :meth:`Store._conn` also
    sweeps holders of dead threads, which keeps the count bounded on interpreters
    that clear thread-locals later.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn: sqlite3.Connection | None = conn
        self._thread = weakref.ref(threading.current_thread())
        #: Held for the whole of each database operation on ``conn`` (see :class:`_Use`)
        #: and by :meth:`close`, so a connection is never closed while a statement runs.
        self.lock = threading.RLock()

    @property
    def thread_alive(self) -> bool:
        thread = self._thread()
        return thread is not None and thread.is_alive()

    def close_locked(self) -> None:
        """Close the connection; the caller holds :attr:`lock`."""
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self, timeout: float | None = None) -> bool:
        """Close once no statement runs on the connection; ``False`` if still busy after ``timeout``."""
        if not self.lock.acquire(timeout=-1 if timeout is None else max(0.0, timeout)):
            return False
        try:
            self.close_locked()
        finally:
            self.lock.release()
        return True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass


class Store:
    """SQLite store for Own Compute state.

    ``path`` defaults to the Hermes plugin data directory (see module docs);
    ``clock`` returns wall-clock seconds and is used for leases and the
    recent-jobs window (tests inject a fake clock).

    Entity methods: ``list_*``, ``get_*`` (``None`` when absent), ``upsert_*``
    (returns the stored object) and ``delete_*`` (returns ``True`` if a row
    was removed) for hosts, templates, instances, jobs and tunnels.

    Additive API beyond the entity tables: generic records
    (:meth:`put_record`, :meth:`get_record`, :meth:`list_records`,
    :meth:`delete_record`) store plain JSON dicts keyed by ``(kind, id)`` with
    optional ``host_id``/``status`` columns for filtering; feature areas such
    as cloud agents persist their run records there. :meth:`kv_delete`
    complements ``kv_get``/``kv_set``.
    """

    def __init__(self, path: Path | str | None = None, *,
                 clock: Callable[[], float] | None = None):
        own_folder = path is None
        self.path = Path(path) if path is not None else _default_path()
        self._clock = clock or time.time
        self._local = threading.local()
        self._holders: "weakref.WeakSet[_ConnHolder]" = weakref.WeakSet()
        self._lock = threading.Lock()
        self._closed = False
        created = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        _secure_files(self.path, tighten_folder=created or own_folder)
        self._init_schema()

    # ------------------------------------------------------------------ connections

    @property
    def _conns(self) -> list[sqlite3.Connection]:
        """The connections that are still open (one per live thread that used the store)."""
        with self._lock:
            holders = list(self._holders)
        return [h.conn for h in holders if h.conn is not None]

    def _conn(self) -> sqlite3.Connection:
        """This thread's connection (tests and diagnostics; store methods use :class:`_Use`)."""
        conn = self._holder().conn
        if conn is None:
            raise StoreClosedError()
        return conn

    def _holder(self) -> _ConnHolder:
        holder = getattr(self._local, "holder", None)
        if self._closed:
            raise StoreClosedError()
        if holder is not None and holder.conn is not None:
            return holder
        self._sweep()
        conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000,
                               isolation_level=None, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.Error as exc:
            log.debug("cloud-compute: WAL unavailable: %s", exc)
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        holder = _ConnHolder(conn)
        with self._lock:
            if self._closed:                  # close() ran while this connection was opened
                holder.close()
                raise StoreClosedError()
            self._holders.add(holder)
        self._local.holder = holder
        return holder

    def _sweep(self) -> None:
        """Close the connections of threads that have ended."""
        with self._lock:
            dead = [h for h in self._holders if not h.thread_alive]
        for holder in dead:
            holder.close()

    def _init_schema(self) -> None:
        with self._tx(immediate=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _tx(self, immediate: bool = True) -> _Use:
        """One explicit transaction on this thread's connection, locked against close()."""
        return _Use(self, tx=True, immediate=immediate)

    def _use(self) -> _Use:
        """This thread's connection for one read, locked against close()."""
        return _Use(self)

    @property
    def closed(self) -> bool:
        """True after :meth:`close`; a closed store raises :class:`StoreClosedError` on every use."""
        return self._closed

    def close(self, timeout: float = CLOSE_WAIT_S) -> bool:
        """Close every connection this store opened (all threads). Idempotent.

        Each connection is closed only while no statement runs on it: this waits for a
        running statement (at most ``timeout`` seconds in total). A connection still busy
        after that is closed by its own thread as soon as its statement ends. Any later
        use from any thread raises :class:`StoreClosedError`. Returns ``False`` when some
        connection was still busy (it is logged), ``True`` otherwise.
        """
        with self._lock:
            self._closed = True
            holders = list(self._holders)
            self._holders = weakref.WeakSet()
        deadline = time.monotonic() + max(0.0, float(timeout))
        busy = 0
        for holder in holders:
            if not holder.close(timeout=max(0.0, deadline - time.monotonic())):
                busy += 1
        self._local = threading.local()
        if busy:
            log.warning("cloud-compute: %d database connection(s) were still busy when the store "
                        "closed; each closes when its statement ends", busy)
        return busy == 0

    # ------------------------------------------------------------------ generic rows

    def _put(self, table: str, row_id: str, host_id: str | None, status: str | None,
             data: dict) -> None:
        with self._tx() as conn:
            conn.execute(
                f"INSERT INTO {table} (id, host_id, status, data, updated_at) VALUES (?,?,?,?,?) "
                f"ON CONFLICT(id) DO UPDATE SET host_id=excluded.host_id, status=excluded.status, "
                f"data=excluded.data, updated_at=excluded.updated_at",
                (row_id, host_id, status, _dumps(data), utc_now_iso()),
            )

    def _delete(self, table: str, row_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
            return cur.rowcount > 0

    @staticmethod
    def _decode(cls: type | None, table: str, row_id: Any, data: str) -> Any:
        try:
            value = json.loads(data)
            return cls.from_dict(value) if cls is not None else value  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - one bad row must not break listing
            log.warning("cloud-compute: skipping unreadable %s row %r: %s", table, row_id, exc)
            return None

    def _get(self, table: str, cls: type, row_id: str) -> Any:
        with self._use() as conn:
            row = conn.execute(f"SELECT data FROM {table} WHERE id=?", (row_id,)).fetchone()
        if row is None:
            return None
        return self._decode(cls, table, row_id, row[0])

    def _list(self, table: str, cls: type, where: str = "", params: Iterable[Any] = ()
              ) -> list[tuple[Any, str | None]]:
        with self._use() as conn:
            rows = conn.execute(
                f"SELECT id, data, updated_at FROM {table} {where} ORDER BY id", tuple(params),
            ).fetchall()
        out: list[tuple[Any, str | None]] = []
        for row_id, data, updated_at in rows:
            obj = self._decode(cls, table, row_id, data)
            if obj is not None:
                out.append((obj, updated_at))
        return out

    @staticmethod
    def _in(column: str, values: Iterable[str]) -> tuple[str, list[str]]:
        vals = sorted(set(values))
        if not vals:
            return "0", []
        return f"{column} IN ({','.join('?' * len(vals))})", vals

    # ------------------------------------------------------------------ hosts

    def list_hosts(self, kind: str | None = None) -> list[HostSpec]:
        if kind is None:
            items = [h for h, _ in self._list("hosts", HostSpec)]
        else:
            items = [h for h, _ in self._list("hosts", HostSpec, "WHERE status=?", (kind,))]
        return sorted(items, key=lambda h: (h.name or "", h.id))

    def get_host(self, host_id: str) -> HostSpec | None:
        return self._get("hosts", HostSpec, host_id)

    def upsert_host(self, spec: HostSpec) -> HostSpec:
        if not spec.created_at:
            spec = dataclasses.replace(spec, created_at=utc_now_iso())
        # status column holds the host kind for the list_hosts(kind=...) filter.
        self._put("hosts", spec.id, spec.id, spec.kind, spec.to_dict())
        return spec

    def delete_host(self, host_id: str) -> bool:
        """Delete a host and its state row; instances/jobs are left for callers to check."""
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM hosts WHERE id=?", (host_id,))
            conn.execute("DELETE FROM host_state WHERE id=?", (host_id,))
            return cur.rowcount > 0

    def get_host_state(self, host_id: str) -> HostState | None:
        return self._get("host_state", HostState, host_id)

    def set_host_state(self, state: HostState) -> str | None:
        """Store ``state`` and return the previous status (``None`` if there was none)."""
        with self._tx(immediate=True) as conn:
            row = conn.execute("SELECT status FROM host_state WHERE id=?",
                               (state.host_id,)).fetchone()
            conn.execute(
                "INSERT INTO host_state (id, host_id, status, data, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET host_id=excluded.host_id, status=excluded.status, "
                "data=excluded.data, updated_at=excluded.updated_at",
                (state.host_id, state.host_id, state.status, _dumps(state.to_dict()), utc_now_iso()),
            )
        return row[0] if row is not None else None

    # ------------------------------------------------------------------ templates

    def list_templates(self) -> list[TemplateSpec]:
        return [t for t, _ in self._list("templates", TemplateSpec)]

    def get_template(self, template_id: str) -> TemplateSpec | None:
        return self._get("templates", TemplateSpec, template_id)

    def upsert_template(self, spec: TemplateSpec) -> TemplateSpec:
        self._put("templates", spec.id, None, spec.kind, spec.to_dict())
        return spec

    def delete_template(self, template_id: str) -> bool:
        return self._delete("templates", template_id)

    # ------------------------------------------------------------------ instances

    def list_instances(self, host_id: str | None = None,
                       statuses: set[str] | None = None) -> list[InstanceRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if host_id is not None:
            clauses.append("host_id=?")
            params.append(host_id)
        if statuses is not None:
            clause, vals = self._in("status", statuses)
            clauses.append(clause)
            params.extend(vals)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        items = [i for i, _ in self._list("instances", InstanceRecord, where, params)]
        return sorted(items, key=lambda i: (i.created_at or "", i.id))

    def get_instance(self, instance_id: str) -> InstanceRecord | None:
        return self._get("instances", InstanceRecord, instance_id)

    def upsert_instance(self, rec: InstanceRecord) -> InstanceRecord:
        self._put("instances", rec.id, rec.host_id, rec.status, rec.to_dict())
        return rec

    def delete_instance(self, instance_id: str) -> bool:
        return self._delete("instances", instance_id)

    # ------------------------------------------------------------------ jobs

    def _jobs_with_updated(self, host_id: str | None = None,
                           active_only: bool = False) -> list[tuple[JobRecord, str | None]]:
        clauses: list[str] = []
        params: list[Any] = []
        if host_id is not None:
            clauses.append("host_id=?")
            params.append(host_id)
        if active_only:
            clause, vals = self._in("status", ACTIVE_JOB_STATES)
            clauses.append(clause)
            params.extend(vals)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        items = self._list("jobs", JobRecord, where, params)
        return sorted(items, key=lambda p: (p[0].submitted_at or "", p[0].id))

    def list_jobs(self, host_id: str | None = None, active_only: bool = False) -> list[JobRecord]:
        return [j for j, _ in self._jobs_with_updated(host_id, active_only)]

    def get_job(self, job_id: str) -> JobRecord | None:
        return self._get("jobs", JobRecord, job_id)

    def upsert_job(self, rec: JobRecord) -> JobRecord:
        self._put("jobs", rec.id, rec.host_id, rec.state, rec.to_dict())
        return rec

    def delete_job(self, job_id: str) -> bool:
        return self._delete("jobs", job_id)

    # ------------------------------------------------------------------ tunnels

    def list_tunnels(self, open_only: bool = True) -> list[TunnelRecord]:
        if open_only:
            clause, vals = self._in("status", OPEN_TUNNEL_STATUSES)
            items = [t for t, _ in self._list("tunnels", TunnelRecord, "WHERE " + clause, vals)]
        else:
            items = [t for t, _ in self._list("tunnels", TunnelRecord)]
        return sorted(items, key=lambda t: (t.created_at or "", t.id))

    def get_tunnel(self, tunnel_id: str) -> TunnelRecord | None:
        return self._get("tunnels", TunnelRecord, tunnel_id)

    def upsert_tunnel(self, rec: TunnelRecord) -> TunnelRecord:
        self._put("tunnels", rec.id, rec.host_id, rec.status, rec.to_dict())
        return rec

    def delete_tunnel(self, tunnel_id: str) -> bool:
        return self._delete("tunnels", tunnel_id)

    # ------------------------------------------------------------------ generic records

    def put_record(self, kind: str, record_id: str, data: dict, *, host_id: str | None = None,
                   status: str | None = None) -> None:
        """Insert or replace the JSON record ``(kind, record_id)``."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO records (kind, id, host_id, status, data, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(kind, id) DO UPDATE SET "
                "host_id=excluded.host_id, status=excluded.status, data=excluded.data, "
                "updated_at=excluded.updated_at",
                (kind, record_id, host_id, status, _dumps(data), utc_now_iso()),
            )

    def get_record(self, kind: str, record_id: str) -> dict | None:
        with self._use() as conn:
            row = conn.execute("SELECT data FROM records WHERE kind=? AND id=?",
                               (kind, record_id)).fetchone()
        if row is None:
            return None
        return self._decode(None, f"records/{kind}", record_id, row[0])

    def list_records(self, kind: str, *, host_id: str | None = None,
                     statuses: Iterable[str] | None = None) -> list[dict]:
        """Records of ``kind`` ordered by id, optionally filtered by host and status."""
        clauses = ["kind=?"]
        params: list[Any] = [kind]
        if host_id is not None:
            clauses.append("host_id=?")
            params.append(host_id)
        if statuses is not None:
            clause, vals = self._in("status", statuses)
            clauses.append(clause)
            params.extend(vals)
        with self._use() as conn:
            rows = conn.execute(
                f"SELECT id, data FROM records WHERE {' AND '.join(clauses)} ORDER BY id", params,
            ).fetchall()
        out: list[dict] = []
        for row_id, data in rows:
            value = self._decode(None, f"records/{kind}", row_id, data)
            if value is not None:
                out.append(value)
        return out

    def delete_record(self, kind: str, record_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, record_id))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ events

    def append_event(self, name: str, payload: dict) -> int:
        """Append an event and return its sequence number."""
        with self._tx() as conn:
            cur = conn.execute("INSERT INTO events (ts, name, payload) VALUES (?,?,?)",
                               (utc_now_iso(), name, _dumps(payload)))
            seq = int(cur.lastrowid or 0)
            if seq % max(1, PRUNE_EVERY) == 0:
                conn.execute("DELETE FROM events WHERE seq <= ?", (seq - MAX_EVENTS,))
        return seq

    def events_since(self, seq: int, limit: int = 200) -> list[dict]:
        """Events with ``seq`` greater than the given one, ascending, at most ``limit``."""
        with self._use() as conn:
            rows = conn.execute(
                "SELECT seq, ts, name, payload FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                (int(seq), max(0, int(limit))),
            ).fetchall()
        out: list[dict] = []
        for s, ts, name, payload in rows:
            try:
                data = json.loads(payload) if payload else {}
            except ValueError:
                log.warning("cloud-compute: unreadable payload for event %s", s)
                data = {}
            out.append({"seq": s, "ts": ts, "name": name, "payload": data})
        return out

    # ------------------------------------------------------------------ leases

    def acquire_lease(self, name: str, owner: str, ttl_s: float) -> bool:
        """Take or renew lease ``name`` for ``owner``; ``False`` if someone else holds it."""
        now = float(self._clock())
        with self._tx(immediate=True) as conn:
            row = conn.execute("SELECT owner, expires_at FROM leases WHERE name=?",
                               (name,)).fetchone()
            if row is not None and row[0] != owner and (row[1] or 0) > now:
                return False
            conn.execute(
                "INSERT INTO leases (name, owner, expires_at) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, "
                "expires_at=excluded.expires_at",
                (name, owner, now + float(ttl_s)),
            )
        return True

    def release_lease(self, name: str, owner: str) -> None:
        """Release lease ``name`` if ``owner`` holds it."""
        with self._tx() as conn:
            conn.execute("DELETE FROM leases WHERE name=? AND owner=?", (name, owner))

    # ------------------------------------------------------------------ kv

    def kv_get(self, key: str) -> str | None:
        with self._use() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row is not None else None

    def kv_set(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute("INSERT INTO kv (key, value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def kv_delete(self, key: str) -> bool:
        with self._tx() as conn:
            return conn.execute("DELETE FROM kv WHERE key=?", (key,)).rowcount > 0

    # ------------------------------------------------------------------ snapshot

    def snapshot(self) -> dict:
        """JSON-safe overview: hosts with state, live instances, active and recent jobs."""
        states = {s.host_id: s for s, _ in self._list("host_state", HostState)}
        hosts = [{"spec": h.to_dict(),
                  "state": states[h.id].to_dict() if h.id in states else None}
                 for h in self.list_hosts()]
        instances = [i.to_dict() for i in self.list_instances() if i.status != "destroyed"]
        cutoff = _dt.datetime.fromtimestamp(float(self._clock()) - RECENT_JOB_SECONDS,
                                            tz=_dt.timezone.utc)
        jobs: list[dict] = []
        for job, updated_at in self._jobs_with_updated():
            if job.state in ACTIVE_JOB_STATES:
                jobs.append(job.to_dict())
            elif job.state in TERMINAL_JOB_STATES:
                finished = parse_iso(job.ended_at) or parse_iso(updated_at)
                if finished is not None and finished >= cutoff:
                    jobs.append(job.to_dict())
        return {"hosts": hosts, "instances": instances, "jobs": jobs,
                "generated_at": utc_now_iso()}


def provide(services: Any) -> Store:
    """Registry entry point: the store at the default plugin data path."""
    return Store()
