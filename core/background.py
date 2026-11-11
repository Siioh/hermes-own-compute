"""The single background loop.

One daemon thread runs every registered :class:`BackgroundTask` when its
interval has elapsed. Only one loop runs across processes and module copies:
the loop must hold the ``bg`` lease in the store (TTL = 3 x tick), renewed
before every task. A second loop on the same database file stays idle until the
holder stops or its lease expires, then takes over on its next tick.

Tasks run one after another on the loop thread, so a task never overlaps with
itself or another task. Exceptions are logged and never escape.

Standard library only. Nothing starts at import time.
"""

from __future__ import annotations

import contextvars
import logging
import math
import os
import socket
import threading
import time
import uuid
import weakref
from typing import Callable

from .interfaces import ApiError, BackgroundTask, Services

__all__ = ["BackgroundLoop", "start", "stop", "current", "ensure_started", "MIN_INTERVAL_S",
           "track_worker", "workers", "join_workers", "SHUTDOWN_WAIT_S"]

log = logging.getLogger(__name__)

#: Smallest interval a task may ask for, in seconds.
MIN_INTERVAL_S = 1.0
#: How long :func:`shutdown` waits in total for the loop thread and the worker threads.
SHUTDOWN_WAIT_S = 5.0


# ---------------------------------------------------------------------- worker threads

#: Threads the plugin starts besides the loop (probes, liveness checks, stats, reaper
#: sweeps, Slurm polls). They use the store, so :func:`shutdown` waits for them before
#: the unload callback closes it.
_workers: "weakref.WeakSet[threading.Thread]" = weakref.WeakSet()
_workers_lock = threading.Lock()


def track_worker(thread: threading.Thread) -> threading.Thread:
    """Remember a worker thread (call before ``start()``); returns it unchanged."""
    with _workers_lock:
        _workers.add(thread)
    return thread


def workers() -> list[threading.Thread]:
    """Tracked worker threads that are still running."""
    with _workers_lock:
        return [t for t in _workers if t.is_alive()]


def join_workers(timeout: float) -> list[str]:
    """Wait at most ``timeout`` seconds in total for the tracked workers.

    Returns the names of the ones still running afterwards (empty when all ended).
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    me = threading.current_thread()
    for thread in workers():
        if thread is not me:
            thread.join(max(0.0, deadline - time.monotonic()))
    return sorted(t.name for t in workers() if t is not me)


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:297] + "..."


class BackgroundLoop:
    """Runs background tasks while holding the store lease ``lease_name``."""

    def __init__(self, services: Services, tasks: list[BackgroundTask] | None = None, *,
                 owner: str | None = None, tick_s: float = 5.0, lease_name: str = "bg",
                 clock: Callable[[], float] = time.monotonic):
        self.services = services
        self.tick_s = max(0.001, float(tick_s))
        self.lease_name = lease_name
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.lease_ttl_s = 3.0 * self.tick_s
        self._clock = clock
        self._tasks: list[BackgroundTask] | None = list(tasks) if tasks is not None else None
        self._lock = threading.RLock()        # guards the thread handle and state flags
        self._run_lock = threading.Lock()     # one tick at a time
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._holds = False
        self._last_start: dict[str, float] = {}
        self.last_runs: dict[str, float] = {}
        self.last_errors: dict[str, str] = {}
        self._store_error_logged = False
        self._logged_interval_errors: dict[str, str] = {}

    # ------------------------------------------------------------------ state

    @property
    def tasks(self) -> list[BackgroundTask]:
        """The tasks to run; collected from the registry on first use when not given."""
        with self._lock:
            if self._tasks is None:
                from .registry import collect_background

                self._tasks = list(collect_background())
            return list(self._tasks)

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop_event.is_set()

    @property
    def holds_lease(self) -> bool:
        return self._holds

    # ------------------------------------------------------------------ lease

    def _acquire(self) -> bool:
        """Take or renew the lease. A store that cannot be used means 'not held'."""
        try:
            held = bool(self.services.store.acquire_lease(
                self.lease_name, self.owner, self.lease_ttl_s))
        except ApiError as exc:
            if not self._store_error_logged:
                log.warning("cloud-compute: background loop idle, store unavailable: %s", exc)
                self._store_error_logged = True
            held = False
        except Exception as exc:  # noqa: BLE001 - a broken store must not kill the loop
            if not self._store_error_logged:
                log.warning("cloud-compute: background loop idle, lease failed: %s", _short(exc))
                self._store_error_logged = True
            held = False
        else:
            self._store_error_logged = False
        self._holds = held
        return held

    def _release(self) -> None:
        self._holds = False
        try:
            self.services.store.release_lease(self.lease_name, self.owner)
        except Exception as exc:  # noqa: BLE001
            log.debug("cloud-compute: releasing background lease failed: %s", exc)

    # ------------------------------------------------------------------ ticks

    def run_once(self, now: float | None = None) -> list[str]:
        """Run every task that is due; returns the names of the tasks that ran.

        Returns ``[]`` without running anything when the lease is held by
        another loop (or the store is unavailable).
        """
        return self._run(now, None)

    def _interval(self, task: BackgroundTask) -> float | None:
        """The task's interval clamped to >= 1 s, or ``None`` to skip it this tick."""
        try:
            value = task.interval_s(self.services)
        except Exception as exc:  # noqa: BLE001
            self._interval_problem(task.name, f"interval_s failed: {_short(exc)}", exc)
            return None
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or (isinstance(value, float) and math.isnan(value))):
            self._interval_problem(task.name, f"interval_s returned {value!r}", None)
            return None
        self._logged_interval_errors.pop(task.name, None)
        return max(MIN_INTERVAL_S, float(value))

    def _interval_problem(self, name: str, text: str, exc: BaseException | None) -> None:
        self.last_errors[name] = text
        if self._logged_interval_errors.get(name) == text:
            return                       # same problem as last tick: do not repeat the log line
        self._logged_interval_errors[name] = text
        log.warning("cloud-compute: background task %s skipped: %s", name, text,
                    exc_info=exc is not None)

    def _run(self, now: float | None, abort: threading.Event | None) -> list[str]:
        ran: list[str] = []
        with self._run_lock:
            if not self._acquire():
                return ran
            for task in self.tasks:
                if abort is not None and abort.is_set():
                    break
                t = float(now) if now is not None else float(self._clock())
                interval = self._interval(task)
                if interval is None:
                    continue
                last = self._last_start.get(task.name)
                if last is not None and t - last < interval:
                    continue
                if not self._acquire():          # renew before each task
                    break
                self._last_start[task.name] = t
                ran.append(task.name)
                try:
                    task.run(self.services)
                except Exception as exc:  # noqa: BLE001 - tasks must never kill the loop
                    self.last_errors[task.name] = _short(exc)
                    log.warning("cloud-compute: background task %s failed: %s", task.name,
                                _short(exc), exc_info=True)
                else:
                    self.last_errors.pop(task.name, None)
                self.last_runs[task.name] = t
        return ran

    # ------------------------------------------------------------------ thread

    def start(self) -> "BackgroundLoop":
        """Start the daemon thread (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set():
                return self
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=1.0)    # a stop is in flight; let it finish
            self.tasks                              # resolve the task list at start time
            self._stop_event = threading.Event()
            # Carry the caller's context (for example Hermes's per-profile secret
            # scope) into the loop thread; plain threads start with an empty one.
            ctx = contextvars.copy_context()
            stop_event = self._stop_event
            thread = threading.Thread(target=ctx.run, args=(self._loop, stop_event),
                                      name="cloud-compute-bg", daemon=True)
            self._thread = thread
            thread.start()
        return self

    def _loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self._run(None, stop_event)
            except BaseException as exc:  # noqa: BLE001 - never let the thread die
                log.warning("cloud-compute: background tick failed: %s", _short(exc), exc_info=True)
            if stop_event.wait(self.tick_s):
                break

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread, wait for it and release the lease."""
        with self._lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("cloud-compute: background thread did not stop within %.1fs", timeout)
        with self._lock:
            if thread is not None and not thread.is_alive() and self._thread is thread:
                self._thread = None
        self._release()

    def __repr__(self) -> str:
        return (f"BackgroundLoop(owner={self.owner!r}, running={self.is_running}, "
                f"holds_lease={self.holds_lease})")


# ---------------------------------------------------------------------- process-wide loop

_singleton: BackgroundLoop | None = None
_singleton_lock = threading.Lock()


def start(services: Services, *, tasks: list[BackgroundTask] | None = None,
          tick_s: float = 5.0) -> BackgroundLoop:
    """Start the process-wide loop if it is not running; return the running loop."""
    global _singleton
    with _singleton_lock:
        loop = _singleton
        if loop is not None and loop.is_running:
            if loop.services is services:
                return loop
            loop.stop()                  # it works on another container (e.g. a closed store)
        loop = BackgroundLoop(services, tasks, tick_s=tick_s)
        loop.start()
        _singleton = loop
        return loop


def stop(timeout: float = 5.0) -> None:
    """Stop the process-wide loop (if any) and forget it."""
    global _singleton
    with _singleton_lock:
        loop, _singleton = _singleton, None
    if loop is not None:
        loop.stop(timeout)


def shutdown(timeout: float = SHUTDOWN_WAIT_S) -> list[str]:
    """Stop the loop, wait for the worker threads and forget the start state.

    For the Hermes unload callback, which closes the store afterwards: the loop thread
    and every tracked worker (see :func:`track_worker`) get at most ``timeout`` seconds
    in total to finish, so none of them is still using the store when it closes. A
    worker still running after that (for example a probe waiting on a slow network) is
    logged and left alone: the store refuses its later calls cleanly. Returns the names
    of those workers. Idempotent; never raises; a later reload can start the loop again.
    """
    global _ensured, _retry_at, _retry_delay
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        stop(max(0.0, deadline - time.monotonic()))
    except Exception as exc:  # noqa: BLE001 - unload must never fail
        log.debug("cloud-compute: background stop failed: %s", _short(exc))
    left: list[str] = []
    try:
        left = join_workers(max(0.0, deadline - time.monotonic()))
        if left:
            log.warning("cloud-compute: %d worker thread(s) still running at shutdown: %s",
                        len(left), ", ".join(left))
    except Exception as exc:  # noqa: BLE001
        log.debug("cloud-compute: waiting for worker threads failed: %s", _short(exc))
    with _ensure_lock:
        _ensured, _retry_at, _retry_delay = False, 0.0, 0.0
        _logged_failures.clear()
    return left


def current() -> BackgroundLoop | None:
    """The process-wide loop, or ``None`` when it was never started or has been stopped."""
    with _singleton_lock:
        return _singleton


# ---------------------------------------------------------------------- lazy, retrying start

#: First and longest wait before another start attempt after a failed one, in seconds.
RETRY_FIRST_S = 5.0
RETRY_MAX_S = 300.0

_monotonic: Callable[[], float] = time.monotonic     # tests replace this
_ensure_lock = threading.Lock()
_ensured = False
_retry_at = 0.0
_retry_delay = 0.0
_logged_failures: set[str] = set()


def _settled(services: Services | None = None) -> bool:
    """True when a start already succeeded and its loop is still running (or was stopped on purpose)."""
    loop = _singleton
    return _ensured and (loop is None or (loop.is_running and (
        services is None or loop.services is services)))


def ensure_started(services: Services, *, tick_s: float = 5.0) -> bool:
    """Start the process-wide loop once; cheap to call on every request or tool call.

    Returns ``True`` only when this call started it. A failed start is retried by a
    later call after a backoff (5 s, doubling to 5 min); the failure is logged once
    per exception type. A loop whose thread died is started again; a loop that was
    stopped with :func:`stop` is not. A running loop that works on a different service
    container than ``services`` (for example one whose store was closed) is replaced.
    """
    global _ensured, _retry_at, _retry_delay
    if _settled(services):
        return False
    with _ensure_lock:
        if _settled(services):
            return False
        now = _monotonic()
        if now < _retry_at:
            return False
        try:
            start(services, tick_s=tick_s)
        except Exception as exc:  # noqa: BLE001 - never raise into a request or a tool call
            kind = type(exc).__name__
            _retry_delay = min(RETRY_MAX_S, _retry_delay * 2 if _retry_delay else RETRY_FIRST_S)
            _retry_at = now + _retry_delay
            if kind not in _logged_failures:
                _logged_failures.add(kind)
                log.warning("cloud-compute: background loop did not start (%s: %s); "
                            "will retry in %.0f s", kind, _short(exc), _retry_delay)
            else:
                log.debug("cloud-compute: background loop start failed again: %s", _short(exc))
            return False
        _ensured, _retry_at, _retry_delay = True, 0.0, 0.0
        return True


def _reset_ensure_for_tests() -> None:
    global _ensured, _retry_at, _retry_delay
    with _ensure_lock:
        _ensured, _retry_at, _retry_delay = False, 0.0, 0.0
        _logged_failures.clear()
