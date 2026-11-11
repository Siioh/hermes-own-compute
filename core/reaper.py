"""Time-limit, idle and orphan sweep for sandbox containers. Data-safe by design.

``Reaper.sweep`` is a single bounded pass: one ``list_containers`` call per host and a
small, fixed number of actions per host. Failures on one host or record are collected in
``ReapReport.errors`` and never abort the pass.

Nothing is ever deleted because of time:

- time limit reached: the environment is STOPPED (``stop_reason='expired'``), its files
  stay; ``expiry_warning_minutes`` before that, ``instance.expiring`` is emitted once;
- idle: stopped (``stop_reason='idle'``);
- ``auto_delete_days`` > 0 (opt-in, default 0 = never): environments stopped for that
  long are deleted after the files in their workdir are saved to this computer and the saved
  copy is verified; if saving fails they are kept. Files outside the workdir are not saved;
- failed spawns (status ``error``) with a lingering container are deleted the same way, only
  while the record is still ``error`` (re-checked right before the save and the removal);
- orphans (managed containers of this install without a record): adopted as records
  (``stop_reason='orphan'``), never removed.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .interfaces import ApiError, MachineNotRunning
from .models import parse_iso
from .runtimes import LABEL_INSTALL, LABEL_INSTANCE, container_name

log = logging.getLogger(__name__)

# Host kinds whose sandboxes are not plain containers.
_NON_CONTAINER_KINDS = frozenset({"slurm"})
_INSTALL_ID_KEY = "install_id"
_MIN_ORPHAN_AGE_MIN = 10  # never adopt containers younger than this (mid-spawn)
_ENV_FILE_MIN_AGE_MIN = 15
#: Per host and tick: orphans adopted and automatic deletions tried (an export can be slow).
MAX_ADOPT_PER_TICK = 3
MAX_AUTO_DELETE_PER_TICK = 1
#: store kv: an automatic deletion was refused because the files could not be saved.
AUTO_DELETE_KEPT_PREFIX = "auto_delete_kept:"
#: A refused automatic deletion is retried at most this often (the user was told once).
AUTO_DELETE_RETRY = timedelta(hours=24)
EXPIRING_EVENT = "instance.expiring"


@dataclass
class ReapReport:
    stopped: list[str] = field(default_factory=list)     # idle stops
    destroyed: list[str] = field(default_factory=list)   # auto-deleted (workdir saved) + failed spawns
    orphans: list[str] = field(default_factory=list)     # "host:container" without a record
    errors: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)     # stopped at their time limit
    warned: list[str] = field(default_factory=list)      # expiry warning sent
    adopted: list[str] = field(default_factory=list)     # orphans now listed as records
    kept: list[str] = field(default_factory=list)        # auto-delete refused: workdir not saved
    cleaned: list[str] = field(default_factory=list)     # leftover containers of deleted records

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def merge(self, other: "ReapReport") -> None:
        for key in self.__dataclass_fields__:
            getattr(self, key).extend(getattr(other, key))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


class Reaper:
    def __init__(self, services: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self._services = services
        self._clock = clock or _utc_now

    # -- public ------------------------------------------------------------

    def host_ids(self) -> list[str]:
        """Hosts a sweep covers: container hosts plus any host that has live records."""
        store = self._services.store
        ids: list[str] = []
        for host in store.list_hosts():
            if getattr(host, "kind", None) not in _NON_CONTAINER_KINDS and host.id not in ids:
                ids.append(host.id)
        for rec in store.list_instances():
            if rec.status != "destroyed" and rec.host_id not in ids:
                ids.append(rec.host_id)
        return ids

    def sweep(self, now: datetime | None = None, *, adopt_orphans: bool = True) -> ReapReport:
        """Sweep every host sequentially and return the merged report.

        ``adopt_orphans=False`` only reports orphans (and leaves failed-spawn containers).
        """
        report = ReapReport()
        now = self._aware(now or self._clock())
        try:
            host_ids = self.host_ids()
        except Exception as exc:  # noqa: BLE001 - services unavailable
            report.errors.append(f"reaper unavailable: {_msg(exc)}")
            return report
        for host_id in host_ids:
            report.merge(self._sweep_host(host_id, now, adopt_orphans))
        return report

    def sweep_host(
        self, host_id: str, now: datetime | None = None, *, adopt_orphans: bool = True
    ) -> ReapReport:
        """One host's records and containers; notifies for what it did."""
        return self._sweep_host(host_id, self._aware(now or self._clock()), adopt_orphans)

    def _sweep_host(self, host_id: str, now: datetime, adopt: bool) -> ReapReport:
        report = ReapReport()
        try:
            store = self._services.store
            sandboxes = self._services.sandboxes
            records = [
                r for r in store.list_instances()
                if r.status != "destroyed" and r.host_id == host_id
            ]
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{host_id}: reaper unavailable: {_msg(exc)}")
            return report

        if not self._host_exists(store, host_id):
            # Host record deleted: nothing can be reached. Close the records locally once
            # (destroy tolerates a missing host) instead of failing every minute.
            for rec in records:
                try:
                    sandboxes.destroy(rec.id, reason="destroyed")
                    log.debug("cloud-compute: host %s is gone; closed record %s locally",
                              host_id, rec.id)
                except Exception as exc:  # noqa: BLE001
                    log.debug("cloud-compute: could not close %s of missing host %s: %s",
                              rec.id, host_id, _msg(exc))
            return report

        host_name = self._host_name(store, host_id)
        for rec in records:
            try:
                self._reap_record(sandboxes, rec, now, report)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{rec.id}: {_msg(exc)}")

        self._auto_delete(store, sandboxes, records, now, report)

        if not self._is_slurm(store, host_id) and self._machine_up(store, host_id):
            self._sweep_containers(store, sandboxes, host_id, host_name, records, now, report,
                                   adopt)
        return report

    # -- records -----------------------------------------------------------

    def _reap_record(self, sandboxes: Any, rec: Any, now: datetime, report: ReapReport) -> None:
        if rec.status != "running":
            return            # stopped/exited environments cost nothing; never touched here
        expires = parse_iso(rec.expires_at)
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires is not None and not self._persistent(rec):
            if now >= expires:
                sandboxes.stop(rec.id, reason="expired")
                report.expired.append(rec.id)
                self._notify(
                    "warn", f"{rec.name} reached its time limit",
                    f"{rec.name} reached its time limit and was stopped. Its files are kept. "
                    "Start it again or delete it from Overview.", rec.id)
                return
            self._maybe_warn(sandboxes, rec, expires, now, report)
        if not rec.idle_minutes:
            return
        last = (
            parse_iso(rec.last_activity_at)
            or parse_iso(rec.started_at)
            or parse_iso(rec.created_at)
        )
        if last is None:
            return
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now - last >= timedelta(minutes=rec.idle_minutes):
            sandboxes.stop(rec.id, reason="idle")
            report.stopped.append(rec.id)
            self._notify(
                "info", f"{rec.name} was stopped",
                f"{rec.name} was idle for {_plural(rec.idle_minutes, 'minute')} and was "
                "stopped. Its files are kept.", rec.id)

    def _maybe_warn(self, sandboxes: Any, rec: Any, expires: datetime, now: datetime,
                    report: ReapReport) -> None:
        """Send the expiry warning once per lifetime (``Extend`` re-arms it)."""
        if rec.expiry_warned_at:
            return
        minutes = self._config_int("expiry_warning_minutes", 15)
        if minutes <= 0 or now < expires - timedelta(minutes=minutes):
            return
        mark = getattr(sandboxes, "mark_expiry_warned", None)
        if not callable(mark) or mark(rec.id) is None:
            return                         # already warned meanwhile, or no longer running
        left = max(1, math.ceil((expires - now).total_seconds() / 60))
        report.warned.append(rec.id)
        self._emit(EXPIRING_EVENT, {"id": rec.id, "name": rec.name, "host_id": rec.host_id,
                                    "expires_at": rec.expires_at, "minutes_left": left})
        self._notify(
            "warn", f"{rec.name} is about to reach its time limit",
            f"{rec.name} reaches its time limit in {_plural(left, 'minute')} and will be "
            "stopped. Extend it to keep it running.", rec.id)

    # -- opt-in automatic deletion -------------------------------------------

    def _auto_delete(self, store: Any, sandboxes: Any, records: list[Any], now: datetime,
                     report: ReapReport) -> None:
        days = self._config_int("auto_delete_days", 0)
        if days <= 0:
            return
        stopped_at = getattr(sandboxes, "stopped_at", None)
        if not callable(stopped_at):
            return
        tried = 0
        for rec in records:
            if tried >= MAX_AUTO_DELETE_PER_TICK:
                break
            if rec.status not in ("stopped", "exited") or self._persistent(rec):
                continue
            try:
                since = parse_iso(stopped_at(rec.id))
                if since is None:
                    continue
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
                if now - since < timedelta(days=days):
                    continue
                kept_at = parse_iso(self._kv_get(store, AUTO_DELETE_KEPT_PREFIX + rec.id))
                if kept_at is not None:
                    if kept_at.tzinfo is None:
                        kept_at = kept_at.replace(tzinfo=timezone.utc)
                    if now - kept_at < AUTO_DELETE_RETRY:
                        continue
                if self._holds_unfetched_commits(rec):
                    # A cloud agent's commits that were never fetched exist only in this
                    # sandbox: keep it, say so once, and look again later.
                    report.kept.append(rec.id)
                    first = self._kv_get(store, AUTO_DELETE_KEPT_PREFIX + rec.id) is None
                    self._kv_set(store, AUTO_DELETE_KEPT_PREFIX + rec.id, _iso(now))
                    if first:
                        self._notify(
                            "warn", f"{rec.name} was kept",
                            f"{rec.name} holds commits from a cloud agent that were never fetched "
                            "to this computer, so it was not deleted automatically. Press Get "
                            "results on the agent run first.", rec.id)
                    continue
                tried += 1
                outcome: dict[str, Any] = {}
                sandboxes.destroy(rec.id, reason="auto-delete", save=True,
                                  export_reason="auto-delete", report=outcome, notify=False)
            except ApiError as exc:
                if exc.code != "not_saved":
                    report.errors.append(f"{rec.id}: {_msg(exc)}")
                    continue
                report.kept.append(rec.id)
                first = self._kv_get(store, AUTO_DELETE_KEPT_PREFIX + rec.id) is None
                self._kv_set(store, AUTO_DELETE_KEPT_PREFIX + rec.id, _iso(now))
                if first:
                    self._notify(
                        "warn", f"{rec.name} was kept",
                        f"The files in {self._workdir(rec)} of {rec.name} could not be saved "
                        "to this computer before automatic deletion, so it was kept.",
                        rec.id)
                continue
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{rec.id}: {_msg(exc)}")
                continue
            report.destroyed.append(rec.id)
            self._kv_delete(store, AUTO_DELETE_KEPT_PREFIX + rec.id)
            workdir = str(outcome.get("workdir") or self._workdir(rec))
            # The same sentence a manual Delete shows: what was saved and where.
            saved = str(outcome.get("message") or f"Deleted {rec.name}.")
            outside = (f" Files outside {workdir} were not saved."
                       if outcome.get("saved") or outcome.get("saved_files") != 0 else "")
            self._notify(
                "info", f"{rec.name} was deleted",
                f"{rec.name} was stopped for {_plural(days, 'day')}. {saved}{outside}", rec.id)

    def _workdir(self, rec: Any) -> str:
        """The folder a Delete saves: the one the environment was created with, else its
        template's, else the words ``its work folder`` (never a guessed ``/work``)."""
        workdir = None
        try:
            ask = getattr(self._services.sandboxes, "work_folder", None)
            workdir = ask(rec) if callable(ask) else None
        except Exception:  # noqa: BLE001 - no sandbox service
            workdir = None
        if not (isinstance(workdir, str) and workdir.startswith("/")):
            try:
                workdir = getattr(self._services.templates.get(rec.template_id), "workdir", None)
            except Exception:  # noqa: BLE001 - template gone or service absent
                workdir = None
        return workdir if isinstance(workdir, str) and workdir.startswith("/") else "its work folder"

    def _persistent(self, rec: Any) -> bool:
        """Persistent templates are never destroyed by TTL.

        Without a resolvable template, a record with an ``expires_at`` is
        treated as non-persistent (it was given a deadline at spawn time).
        """
        try:
            spec = self._services.templates.get(rec.template_id)
        except Exception:  # noqa: BLE001 - template gone or service absent
            return False
        return bool(getattr(spec, "persistent", False))

    def _holds_unfetched_commits(self, rec: Any) -> bool:
        try:
            check = getattr(self._services.agents, "unfetched_commits", None)
            return bool(check(rec.id)) if callable(check) else False
        except Exception:  # noqa: BLE001 - no agents service: nothing to protect
            return False

    # -- containers --------------------------------------------------------

    def _sweep_containers(
        self, store: Any, sandboxes: Any, host_id: str, host_name: str, records: list[Any],
        now: datetime, report: ReapReport, adopt: bool,
    ) -> None:
        install_id = self._install_id(store)
        known_ids = {r.id for r in records}
        known_names = {container_name(i) for i in known_ids}
        try:
            containers = sandboxes.list_containers(host_id)
        except MachineNotRunning:
            return                # an EC2 machine that is not running has nothing to sweep
        except Exception as exc:  # noqa: BLE001 - host offline etc.
            report.errors.append(f"{host_id}: {_msg(exc)}")
            return
        containers = containers or []

        # Containers of failed spawns: the record says error, the container may linger. The
        # record is re-checked under the manager's lock right before the save and the
        # removal (only_if_status), so one the user started or uploaded into during this
        # sweep is left alone. The files in the workdir are saved first, like any Delete: a
        # real failed spawn has an empty workdir, so it proceeds; anything with files is
        # saved first, or kept when saving fails.
        handled = set(report.destroyed)
        for rec in records:
            if rec.status != "error" or rec.id in handled or not adopt:
                continue
            if any(self._mine(c, install_id) and self._belongs(c, rec.id) for c in containers):
                outcome: dict[str, Any] = {}
                try:
                    sandboxes.destroy(rec.id, reason="reaped", save=True,
                                      export_reason="auto-delete",
                                      only_if_status=("error",), report=outcome, notify=False)
                    report.destroyed.append(rec.id)
                    if outcome.get("saved"):     # a failed spawn normally has nothing to save
                        self._notify("info", f"{rec.name} was deleted",
                                     str(outcome.get("message") or f"Deleted {rec.name}."),
                                     rec.id)
                except ApiError as exc:
                    if exc.code == "not_saved":
                        report.kept.append(rec.id)
                    elif exc.code != "conflict":    # conflict: changed meanwhile, left alone
                        report.errors.append(f"{rec.id}: {_msg(exc)}")
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"{rec.id}: {_msg(exc)}")

        adopted = 0
        for c in containers:
            labels = c.get("labels") or {}
            name = str(c.get("name") or "")
            if name in known_names or labels.get(LABEL_INSTANCE) in known_ids:
                continue
            owner = labels.get(LABEL_INSTANCE)
            if owner and self._live_record(store, owner):
                continue  # spawned after the snapshot: not an orphan
            if owner and self._remove_leftover(sandboxes, host_id, c, owner, install_id, report):
                continue  # deleted on purpose while its machine was out of reach
            report.orphans.append(f"{host_id}:{name}")
            if not adopt or not self._mine(c, install_id):
                continue  # foreign install: report only, never touched
            if self._too_young(c, now):
                continue  # may still be mid-spawn
            if adopted >= MAX_ADOPT_PER_TICK:
                continue  # the next tick takes the rest
            adopt_fn = getattr(sandboxes, "adopt_container", None)
            if not callable(adopt_fn):
                continue
            try:
                rec = adopt_fn(host_id, c)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{host_id}:{name}: {_msg(exc)}")
                continue
            if rec is None:
                continue
            adopted += 1
            report.adopted.append(rec.id)
            self._notify(
                "warn", f"Found {rec.name}",
                f"Found {rec.name} on {host_name} without a record; it is now listed in "
                "Overview.", rec.id)

        self._forget_gone_leftovers(store, sandboxes, host_id, containers)

        sweep_env = getattr(sandboxes, "sweep_env_files", None)
        if callable(sweep_env):
            try:
                sweep_env(host_id, older_than_minutes=_ENV_FILE_MIN_AGE_MIN)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{host_id}: env files: {_msg(exc)}")

    @staticmethod
    def _remove_leftover(sandboxes: Any, host_id: str, container: dict, owner: str,
                         install_id: str | None, report: ReapReport) -> bool:
        """The container of an environment that was deleted without saving while its EC2
        machine could not be reached: remove it now (never adopt it). True when this
        container is such a leftover, whether or not the removal worked."""
        has = getattr(sandboxes, "has_leftover", None)
        if not callable(has) or not has(owner):
            return False
        name = str(container.get("name") or "")
        if not install_id or (container.get("labels") or {}).get(LABEL_INSTALL) != install_id:
            return False
        try:
            res = sandboxes.remove_container(host_id, name)
            if getattr(res, "rc", 0) == 0:
                sandboxes.clear_leftover(owner)
                report.cleaned.append(f"{host_id}:{name}")
            else:
                report.errors.append(f"{host_id}:{name}: could not remove the leftover container")
        except Exception as exc:  # noqa: BLE001 - tried again at the next sweep
            report.errors.append(f"{host_id}:{name}: {_msg(exc)}")
        return True

    def _forget_gone_leftovers(self, store: Any, sandboxes: Any, host_id: str,
                               containers: list[dict]) -> None:
        """Leftover marks whose container no longer exists on the (listed) machine."""
        clear = getattr(sandboxes, "clear_leftover", None)
        has = getattr(sandboxes, "has_leftover", None)
        try:
            host = store.get_host(host_id)
            if host is None or getattr(host, "kind", None) != "ec2" or not callable(clear):
                return
            names = {str(c.get("name") or "") for c in containers}
            for rec in store.list_instances(host_id):
                if (rec.status == "destroyed" and container_name(rec.id) not in names
                        and callable(has) and has(rec.id)):
                    clear(rec.id)
        except Exception:  # noqa: BLE001 - housekeeping only
            log.debug("cloud-compute: could not tidy leftover marks of %s", host_id, exc_info=True)

    @staticmethod
    def _mine(container: dict, install_id: str | None) -> bool:
        labels = container.get("labels") or {}
        return bool(install_id) and labels.get(LABEL_INSTALL) == install_id

    @staticmethod
    def _belongs(container: dict, instance_id: str) -> bool:
        labels = container.get("labels") or {}
        return labels.get(LABEL_INSTANCE) == instance_id or container.get("name") == container_name(
            instance_id
        )

    @staticmethod
    def _live_record(store: Any, instance_id: str) -> bool:
        try:
            rec = store.get_instance(instance_id)
        except Exception:  # noqa: BLE001
            return False
        return rec is not None and rec.status != "destroyed"

    @staticmethod
    def _too_young(container: dict, now: datetime) -> bool:
        """True when the container exposes a creation time under the grace period."""
        raw = container.get("created_at", container.get("created"))
        if raw is None:
            return False
        if isinstance(raw, (int, float)):
            created = datetime.fromtimestamp(raw, timezone.utc)
        else:
            created = parse_iso(str(raw))
        if created is None:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return now - created < timedelta(minutes=_MIN_ORPHAN_AGE_MIN)

    @staticmethod
    def _host_exists(store: Any, host_id: str) -> bool:
        try:
            return store.get_host(host_id) is not None
        except Exception:  # noqa: BLE001 - unreadable store: behave as before
            return True

    @staticmethod
    def _machine_up(store: Any, host_id: str) -> bool:
        """False for an EC2 host whose machine is not running: there is nothing to list."""
        try:
            host = store.get_host(host_id)
        except Exception:  # noqa: BLE001 - as _is_slurm: a store error never stops the pass
            return True
        if host is None or getattr(host, "kind", None) != "ec2":
            return True
        return getattr(getattr(host, "ec2", None), "machine_state", "") == "ready"

    @staticmethod
    def _is_slurm(store: Any, host_id: str) -> bool:
        try:
            host = store.get_host(host_id)
        except Exception:  # noqa: BLE001
            return False
        return host is not None and getattr(host, "kind", None) in _NON_CONTAINER_KINDS

    @staticmethod
    def _install_id(store: Any) -> str | None:
        try:
            from .sandbox import get_install_id  # lazy: sandbox is a sibling service

            return get_install_id(store) or None
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("install id unavailable: %s", exc)
            return None
        try:
            return store.kv_get(_INSTALL_ID_KEY) or None
        except Exception:  # noqa: BLE001
            return None

    # -- misc --------------------------------------------------------------

    def _config_int(self, name: str, default: int) -> int:
        try:
            value = getattr(self._services.config, name, default)
        except Exception:  # noqa: BLE001 - config unavailable
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value

    @staticmethod
    def _host_name(store: Any, host_id: str) -> str:
        try:
            host = store.get_host(host_id)
        except Exception:  # noqa: BLE001
            host = None
        return str(getattr(host, "name", "") or host_id)

    @staticmethod
    def _kv_get(store: Any, key: str) -> Any:
        try:
            return store.kv_get(key)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _kv_set(store: Any, key: str, value: str) -> None:
        try:
            store.kv_set(key, value)
        except Exception:  # noqa: BLE001
            log.debug("cloud-compute: could not write %s", key)

    @staticmethod
    def _kv_delete(store: Any, key: str) -> None:
        try:
            kv_delete = getattr(store, "kv_delete", None)
            if kv_delete is not None:
                kv_delete(key)
        except Exception:  # noqa: BLE001
            log.debug("cloud-compute: could not delete %s", key)

    def _emit(self, name: str, payload: dict) -> None:
        try:
            self._services.events.emit(name, payload)
        except Exception as exc:  # noqa: BLE001 - events are best effort
            log.debug("reaper event %s failed: %s", name, exc)

    def _notify(self, level: str, title: str, body: str, instance_id: str) -> None:
        """A user notification (the ``notify`` event the desktop shows as a toast)."""
        self._emit("notify", {"level": level, "title": title, "body": body,
                              "ref": {"kind": "instance", "id": instance_id}})

    @staticmethod
    def _aware(dt: datetime) -> datetime:
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return f"{exc.code}: {exc.message}"
    return f"{type(exc).__name__}: {exc}"


def provide(services: Any) -> Reaper:
    return Reaper(services)
