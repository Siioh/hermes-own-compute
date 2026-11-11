"""Sandbox lifecycle: spawn, monitor and clean up containers built from templates.

Every host command goes through ``services.transports.for_host(host)`` and every
container argv is built by :mod:`.runtimes`. Secret values never appear in an
argv, a record, an event or a log line: they are written to a 0600 env file on
the host over the transport's stdin, passed to ``<rt> run --env-file`` and the
file is deleted right after ``run`` returns (also when it fails). Command output
returned to callers is redacted against the values sent at spawn (kept in memory
only), the current values of the recorded secret names and the display values;
when the in-memory set is gone and a recorded name cannot be resolved, every
``NAME=...`` line for the recorded names is masked as well.
"""

from __future__ import annotations

import inspect
import io
import json
import logging
import os
import re
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import CONFIG_FIELDS
from .interfaces import NO_MACHINE, ApiError
from .redact import redact_tokens
from .models import (
    ExecResult, HostSpec, InstanceRecord, InstanceStats, SendPreview, TemplateSpec,
    VariablePreview, fitting_host_kinds, new_id, parse_iso,
)
from .templates import DISPLAY_IMAGE_MESSAGE, image_provides_display
from .transport import _is_windows
from .runtimes import (
    DISPLAY_IMAGE_TAG, DISPLAY_PORT, LABEL_DISPLAY, LABEL_INSTALL, LABEL_INSTANCE, LABEL_TEMPLATE, LOOPBACK,
    MANAGED_LABEL, RunSpec, container_name, instance_labels, runtime_for,
)

__all__ = [
    "NO_OUTPUT_NOTE", "PORT_WAIT_S", "PORT_POLL_S", "human_size", "delete_message",
    "INSTALL_ID_KEY", "OUTPUT_CAP_BYTES", "ENV_DIR", "VARS_KEY_PREFIX", "MIN_MINUTES",
    "MAX_MINUTES", "EXTEND_MAX_MINUTES", "STOPPED_AT_KEY_PREFIX", "NOT_SAVED", "LABEL_NAME",
    "get_install_id", "SandboxManager", "provide", "OUTPUT_WITHHELD_MESSAGE",
]

log = logging.getLogger(__name__)

INSTALL_ID_KEY = "install_id"
OUTPUT_CAP_BYTES = 20 * 1024
ENV_DIR = "$HOME/.cache/cloud-compute"   # on remote hosts
LOCAL_ENV_SUBDIR = "envfiles"            # under the plugin data dir, for the local Windows computer
_DISPLAY_TOKEN_PREFIX = "display-token:"   # same key as display.token_key()
VARS_KEY_PREFIX = "instance_vars:"   # store kv: sent variable names per instance, never values
MIN_MINUTES, MAX_MINUTES = 5, 1440
EXTEND_MAX_MINUTES = 10080            # one week per Extend
#: store kv: when an environment was last stopped (ISO UTC). Kept out of InstanceRecord so the
#: REST shape stays unchanged; used by the opt-in ``auto_delete_days`` clean-up.
STOPPED_AT_KEY_PREFIX = "stopped_at:"
#: store kv: the working folder an environment was created with. Podman cannot start a
#: container in a folder the image lacks, so such a container starts in ``/``; every exec
#: therefore passes this folder (it is created at spawn) instead of relying on the container.
WORKDIR_KEY_PREFIX = "workdir:"
#: Error code raised by :meth:`SandboxManager.destroy` when the files could not be saved first.
NOT_SAVED = "not_saved"
#: Optional display-name label on a container (used when an orphan is adopted).
LABEL_NAME = "io.cloud-compute.name"
#: ``stop(reason=...)`` -> ``InstanceRecord.stop_reason``. Anything else is a user stop.
_STOP_REASON_FOR = {"expired": "expired", "idle": "idle", "reaped": "idle",
                    "host-gone": "host-gone", "orphan": "orphan", "user": "user"}
_MIN_LITERAL_REDACT = 4

# New ids are 12 hex chars; an existing stored id is kept as long as it is a safe
# label value, so containers labelled with it are never orphaned by a format change.
_INSTALL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Only the runtimes' own "container is gone" texts. A bare "not found" would also match
# "sh: podman: not found" (rc 127), and the record would be closed while the container lives.
_NO_SUCH = re.compile(r"no such container|no container with name or id", re.IGNORECASE)
_MISSING_WORKDIR = re.compile(r"workdir \S+ does not exist", re.IGNORECASE)
_NOT_RUNNING = re.compile(r"can only create exec sessions on running|is not running|"
                          r"container state improper", re.IGNORECASE)


def _dir_tar(parts: list[str]) -> bytes:
    """A tar archive holding the nested empty folders ``parts`` (mode 0755)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for i in range(len(parts)):
            info = tarfile.TarInfo("/".join(parts[: i + 1]))
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = int(time.time())
            tar.addfile(info)
    return buf.getvalue()


def exec_result_like(res: ExecResult, **changes: Any) -> ExecResult:
    """A copy of ``res`` with some fields replaced."""
    data = {"rc": res.rc, "stdout": res.stdout, "stderr": res.stderr,
            "duration_s": res.duration_s, "timed_out": res.timed_out}
    data.update(changes)
    return ExecResult(**data)

# Write stdin to $HOME/.cache/cloud-compute/<id>.env as a freshly created 0600 file
# (any old file is removed first and noclobber refuses to reuse one); print the path.
_WRITE_ENV = ('set -e; umask 077; d="$HOME/.cache/cloud-compute"; mkdir -p "$d"; '
              'chmod 700 "$d" 2>/dev/null || :; '
              'f="$d/$1.env"; rm -f -- "$f"; set -C; cat > "$f"; printf "%s\\n" "$f"')
_REMOVE_ENV = 'rm -f -- "$HOME/.cache/cloud-compute/$1.env"'
# Delete env files older than $1 minutes (left behind when a removal failed); print each.
_SWEEP_ENV = ('d="$HOME/.cache/cloud-compute"; [ -d "$d" ] || exit 0; '
              'find "$d" -maxdepth 1 -type f -name \'*.env\' -mmin +"$1" -print -delete')

_ACTIVE = ("creating", "running", "stopped", "exited")
_NOT_DESTROYED = ("creating", "running", "stopped", "exited", "error")


def _local_env_dir(store: Any) -> Path:
    """Private folder for env files written by Python (local Windows hosts).

    It sits beside the database, in the plugin's data directory, so the files stay inside the
    active profile. Remote hosts keep ``$HOME/.cache/cloud-compute`` on the remote machine.
    """
    path = getattr(store, "path", None)
    if path:
        return Path(path).parent / LOCAL_ENV_SUBDIR
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore[import-not-found]
        return Path(plugin_data_dir("cloud-compute")) / LOCAL_ENV_SUBDIR
    except Exception:  # noqa: BLE001 - running outside Hermes
        raise ApiError(503, "unavailable", "plugin storage not available") from None


def _fits(host_kind: str, tpl: Any) -> bool:
    """True when ``tpl`` may run on hosts of ``host_kind``. An EC2 machine is an ordinary SSH
    container host, so templates for SSH hosts run there too."""
    return host_kind in fitting_host_kinds(getattr(tpl, "host_kinds", None))


#: store kv: an environment of an EC2 machine was deleted without saving while the machine could
#: not be reached; its container is removed (not adopted) the next time the machine answers.
LEFTOVER_KEY_PREFIX = "container_leftover:"
#: Why a record was closed together with its EC2 machine.
WITH_MACHINE_NOTE = "Deleted with its machine, files not saved."


def _no_machine_running(host: Any) -> bool:
    """True for an EC2 host that has no address to dial (no machine, or a stopped one)."""
    return getattr(host, "kind", None) == "ec2" and (
        not getattr(host, "address", "") or host.address == NO_MACHINE)


def _machine_state_text(host: Any) -> str:
    state = getattr(getattr(host, "ec2", None), "machine_state", "none") or "none"
    return "has no machine" if state == "none" else f"is {state}"


def saving_needs_machine(name: str, host: Any) -> str:
    """The refusal for saving the files of one environment when the machine is not running."""
    return (f"The machine of {host.name} {_machine_state_text(host)}, so the files of {name} "
            "cannot be saved. Saving needs the machine running: start it (AWS bills while it "
            "runs) and delete again, or delete without saving, which loses the files.")


def _left_with_machine_note(host: Any) -> tuple[str, bool]:
    """``(note for the closed record, container left on the machine)`` for a delete without
    saving when the machine cannot be reached."""
    if getattr(getattr(host, "ec2", None), "instance_id", None):
        return ("Its container is removed when the machine next starts or is terminated.", True)
    return ("The machine it ran on no longer exists.", False)


def _local_native(host: Any) -> bool:
    """True when the host is this Windows computer: Git's sh prints MSYS paths native docker can't open."""
    return getattr(host, "kind", None) == "local" and _is_windows()
_INSTALL_LOCK = threading.Lock()

STOP_TIMEOUT_S = 3
#: After a container starts, the runtime may take a moment to report its published ports.
#: Wait at most this long (polling every PORT_POLL_S) instead of saving an empty map.
PORT_WAIT_S = 5.0
PORT_POLL_S = 0.25
#: Shown instead of exec output or logs when the secret values an environment was started
#: with are not known in this process and cannot be read back from its container: the output
#: is never returned unmasked. ``{what}``, ``{name}`` and ``{host}`` are filled in.
OUTPUT_WITHHELD_MESSAGE = (
    "{what} withheld: {name} was started with secret values, and this computer cannot read "
    "them back from its container right now to hide them. Check that the host {host} is "
    "reachable and that the environment still exists, then try again.")
#: Shown with empty container logs (the main process printed nothing).
NO_OUTPUT_NOTE = ("No output: the main process of this environment prints nothing. Run commands "
                  "with env_exec or open a terminal.")
_RUN_TIMEOUT = 900.0
_CMD_TIMEOUT = 120.0
_BUILD_TIMEOUT = 1800.0

PLUGIN_DIR = Path(__file__).resolve().parents[1]
DISPLAY_CONTAINERFILE = PLUGIN_DIR / "images" / "novnc" / "Containerfile"


def get_install_id(store: Any) -> str:
    """This installation's id (12 hex chars), created once and kept in the store."""
    with _INSTALL_LOCK:
        value = store.kv_get(INSTALL_ID_KEY)
        if isinstance(value, str) and _INSTALL_ID_RE.fullmatch(value):
            return value
        value = uuid.uuid4().hex[:12]
        store.kv_set(INSTALL_ID_KEY, value)
        return value


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tail_bytes(text: str, cap: int = OUTPUT_CAP_BYTES) -> str:
    data = (text or "").encode("utf-8", "replace")
    if len(data) <= cap:
        return text or ""
    return data[-cap:].decode("utf-8", "ignore")


def _short(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    return text[-limit:]


def _display_overrides() -> Callable[[str], Any] | None:
    """``display_run_overrides`` from the access module, or ``None`` if not installed."""
    try:
        from .access import display_run_overrides  # type: ignore[attr-defined]
        return display_run_overrides
    except ImportError:
        pass
    try:
        from .display import display_run_overrides  # type: ignore[attr-defined]
        return display_run_overrides
    except ImportError:
        return None


def _bad_value(value: str) -> bool:
    return "\n" in value or "\r" in value or "\x00" in value


def _port_key(container_port: int) -> str:
    return "novnc" if container_port == DISPLAY_PORT else str(container_port)


def human_size(num: Any) -> str:
    """``812 B``, ``3.2 KB``, ``1.4 MB``: the size shown with a saved copy."""
    try:
        size = float(num)
    except (TypeError, ValueError):
        return "unknown size"
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"       # pragma: no cover


def _files_text(count: Any) -> str:
    try:
        n = int(count)
    except (TypeError, ValueError):
        return "files"
    return "1 file" if n == 1 else f"{n} files"


def delete_message(name: str, workdir: str, saved: dict | None, *, nothing_to_save: bool = False,
                   unsaved: bool = False) -> str:
    """The one sentence that says what a Delete saved (also used in notifications).

    ``saved`` is ``{export_id, path, bytes, files}`` for a verified copy; ``nothing_to_save``
    means the work folder held no files; ``unsaved`` means the user deleted without saving.
    """
    if saved:
        text = (f"Deleted {name}. The files in {workdir} ({_files_text(saved.get('files'))}, "
                f"{human_size(saved.get('bytes'))}) were saved to {saved.get('path')}.")
        if saved.get("note"):                  # agent-internal folders left out (files.py)
            text += " " + str(saved["note"])
        return text
    if nothing_to_save:
        return f"Deleted {name}. Nothing in {workdir} needed saving."
    if unsaved:
        return f"Deleted {name} without saving its files."
    return f"Deleted {name}."


class SandboxManager:
    """Containers from templates on SSH and local hosts (see the module docstring)."""

    def __init__(self, services: Any, *, clock: Callable[[], datetime] | None = None):
        self.services = services
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._instance_locks: dict[str, threading.RLock] = {}
        # instance id -> values sent at spawn that must be redacted. Memory only, never stored.
        self._sent_values: dict[str, set[str]] = {}
        # instance id -> (values, names not found) read back from a container's own
        # environment when this process did not spawn it. Memory only, never stored or logged.
        self._read_values: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {}

    # ------------------------------------------------------------------ helpers

    def instance_lock(self, instance_id: str) -> threading.RLock:
        """Per-environment lock. Delete holds it from the save until the container is
        removed; Start and file uploads take it, so neither lands in between."""
        with self._lock:
            lock = self._instance_locks.get(instance_id)
            if lock is None:
                lock = self._instance_locks[instance_id] = threading.RLock()
            return lock

    def _workdir_of(self, rec: InstanceRecord) -> str | None:
        """The folder a Delete saves: the one the environment was created with (the value
        :meth:`exec_workdir` uses), else its template's folder for an old record that has none,
        else ``None``. ``None`` is never turned into ``/work``: a Delete that cannot tell which
        folder holds the files refuses (see :meth:`_save_files`)."""
        return self.work_folder(rec)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return _iso(self._now())

    @property
    def store(self) -> Any:
        return self.services.store

    def install_id(self) -> str:
        return get_install_id(self.store)

    def _host(self, host_id: str) -> HostSpec:
        host = self.store.get_host(host_id)
        if host is None:
            raise ApiError(404, "not_found", f"host {host_id} not found")
        return host

    def _template(self, template_id: str) -> TemplateSpec:
        try:
            tpl = self.services.templates.get(template_id)
        except KeyError:
            tpl = None
        if tpl is None:
            raise ApiError(404, "not_found", f"template {template_id} not found")
        return tpl

    def _record(self, instance_id: str) -> InstanceRecord:
        rec = self.store.get_instance(instance_id) if isinstance(instance_id, str) else None
        if rec is None:
            raise ApiError(404, "not_found", f"instance {instance_id} not found")
        return rec

    def _transport(self, host: HostSpec) -> Any:
        return self.services.transports.for_host(host)

    def _runtime_name(self, host: HostSpec) -> str:
        if host.runtime_pref in ("podman", "docker"):
            return host.runtime_pref
        try:
            state = self.store.get_host_state(host.id)
        except Exception:  # noqa: BLE001 - an unreadable state falls back to podman
            state = None
        rt = getattr(getattr(state, "capabilities", None), "runtime", None)
        return rt if rt in ("podman", "docker") else "podman"

    def _rt_for_record(self, rec: InstanceRecord) -> Any:
        return runtime_for(rec.runtime if rec.runtime in ("podman", "docker") else "podman")

    def _emit(self, ids: Iterable[str], host_id: str | None, reason: str) -> None:
        ids = list(ids)
        if not ids:
            return
        try:
            self.services.events.emit("instances.changed",
                                      {"ids": ids, "host_id": host_id, "reason": reason})
        except Exception:  # noqa: BLE001 - events never break lifecycle calls
            log.debug("cloud-compute: could not emit instances.changed", exc_info=True)

    def _save(self, rec: InstanceRecord) -> InstanceRecord:
        return self.store.upsert_instance(rec)

    def _update(self, instance_id: str, mutate: Callable[[InstanceRecord], None], *,
                expect: Iterable[str] | None = None) -> InstanceRecord | None:
        """Re-read the record, apply ``mutate`` and save it.

        Callers hold a record read before a slow remote call; saving that copy would
        overwrite a Stop or Destroy made meanwhile. This re-reads the stored record
        right before writing and changes only what ``mutate`` touches. With ``expect``
        the write is skipped (``None`` is returned) unless the stored status is in it.
        """
        with self._lock:
            fresh = self.store.get_instance(instance_id)
            if fresh is None:
                return None
            if expect is not None and fresh.status not in tuple(expect):
                return None
            mutate(fresh)
            return self._save(fresh)

    # ------------------------------------------------------------------ redaction

    @staticmethod
    def _redact_plan(variables: Iterable[Any], env: dict[str, str], host_id: str,
                     display_env: dict[str, str]) -> tuple[set[str], list[dict]]:
        """Values to redact and the name records to persist for what spawn sends.

        Secret-sourced values are always redacted. Literal values are redacted only when
        the variable's scope is ``redacted`` (the default) and the value has at least four
        characters. Display values (e.g. a VNC password) are always redacted.
        """
        meta: dict[str, dict] = {}
        for spec in variables:
            name = getattr(spec, "name", None)
            if name not in env or name in display_env:
                continue
            hosts = getattr(spec, "hosts", None)
            if hosts is not None and host_id not in hosts:
                continue
            source = getattr(spec, "source", "") or ""
            entry = meta.setdefault(name, {"name": name, "secret": None, "redact": False})
            if source.startswith("secret:") and source[len("secret:"):]:
                # Conservative: any applicable secret source makes the variable a secret.
                entry["secret"] = source[len("secret:"):]
                entry["redact"] = True
            elif source.startswith("literal:"):
                value = source[len("literal:"):]
                if (getattr(spec, "scope", "redacted") == "redacted"
                        and len(value) >= _MIN_LITERAL_REDACT):
                    entry["redact"] = True
        for name in display_env:
            meta[name] = {"name": name, "secret": None, "redact": True, "display": True}
        values: set[str] = set()
        for name, entry in meta.items():
            value = env.get(name) or ""
            if not value or not entry["redact"]:
                continue
            if entry["secret"] or entry.get("display") or len(value) >= _MIN_LITERAL_REDACT:
                values.add(value)
        return values, [e for e in meta.values() if e["redact"]]

    def _remember_vars(self, inst_id: str, values: set[str], names: list[dict]) -> None:
        with self._lock:
            self._sent_values[inst_id] = set(values)
        try:
            self.store.kv_set(VARS_KEY_PREFIX + inst_id, json.dumps(names, sort_keys=True))
        except Exception:  # noqa: BLE001 - redaction still works from memory
            log.warning("cloud-compute: could not record variable names for %s", inst_id)

    def _call_overrides(self, overrides: Callable[..., Any], inst_id: str) -> Any:
        """Call the display provider, handing it the store so the token reaches kv."""
        try:
            params = inspect.signature(overrides).parameters
        except (TypeError, ValueError):
            params = {}
        if "store" in params or any(p.kind is p.VAR_KEYWORD for p in params.values()):
            return overrides(inst_id, store=self.store)
        return overrides(inst_id)

    def _forget_display_token(self, inst_id: str) -> None:
        """Drop the stored display token (plaintext) for an instance, best effort."""
        try:
            kv_delete = getattr(self.store, "kv_delete", None)
            if kv_delete is not None:
                kv_delete(_DISPLAY_TOKEN_PREFIX + inst_id)
        except Exception:  # noqa: BLE001
            log.debug("cloud-compute: could not drop display token for %s", inst_id,
                      exc_info=True)

    def _close_tunnels(self, inst_id: str) -> None:
        """Close the instance's screen/port tunnels and their ssh processes, best effort.

        The access service is looked up lazily (access.py is a sibling module and must
        not be imported here). A missing service or any failure never fails destroy or
        stop.
        """
        try:
            self.services.access.close_instance_tunnels(inst_id)
        except Exception as exc:  # noqa: BLE001 - destroy/stop must not fail on tunnel cleanup
            log.debug("cloud-compute: could not close tunnels for %s: %s", inst_id,
                      type(exc).__name__)

    def _remember_workdir(self, inst_id: str, workdir: Any) -> None:
        if isinstance(workdir, str) and workdir.startswith("/"):
            try:
                self.store.kv_set(WORKDIR_KEY_PREFIX + inst_id, workdir)
            except Exception:  # noqa: BLE001 - exec falls back to the template's folder
                log.debug("cloud-compute: could not record the workdir of %s", inst_id)

    def exec_workdir(self, instance_id: str) -> str | None:
        """The folder commands and terminals start in: the one the environment was created
        with, else its template's, else ``None`` (the container's own default)."""
        return self.work_folder(self._record(instance_id))

    def work_folder(self, rec: InstanceRecord) -> str | None:
        """The work folder of ``rec``: recorded at spawn, else its template's current folder
        (old records only), else ``None``. Saves, deletes, sizes and listings use this."""
        try:
            value = self.store.kv_get(WORKDIR_KEY_PREFIX + rec.id)
        except Exception:  # noqa: BLE001
            value = None
        if isinstance(value, str) and value.startswith("/"):
            return value
        try:
            value = getattr(self.services.templates.get(rec.template_id), "workdir", None)
        except Exception:  # noqa: BLE001 - template gone
            value = None
        return value if isinstance(value, str) and value.startswith("/") else None

    def _forget_vars(self, inst_id: str) -> None:
        with self._lock:
            self._sent_values.pop(inst_id, None)
            self._read_values.pop(inst_id, None)
        try:
            kv_delete = getattr(self.store, "kv_delete", None)
            if kv_delete is not None:
                kv_delete(VARS_KEY_PREFIX + inst_id)
        except Exception:  # noqa: BLE001
            log.debug("cloud-compute: could not drop variable names for %s", inst_id,
                      exc_info=True)

    def _recorded_vars(self, inst_id: str) -> list[dict]:
        try:
            raw = self.store.kv_get(VARS_KEY_PREFIX + inst_id)
            data = json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001
            return []
        return [e for e in data if isinstance(e, dict) and isinstance(e.get("name"), str)
                and _ENV_NAME_RE.fullmatch(e["name"])] if isinstance(data, list) else []

    def _redaction(self, rec: InstanceRecord, what: str = "Output", *,
                   assume: Iterable[dict] = ()) -> tuple[set[str], list[str]]:
        """``(values, names)`` to redact in this instance's output; fails closed.

        ``values`` are the values sent at spawn when this process spawned the environment.
        Otherwise (another process spawned it, Hermes restarted, the secret was changed,
        renamed or deleted since, or this process cannot read Hermes secrets) they are read
        back once from the container's own configuration (``<runtime> inspect``, which also
        works on a stopped container) for the variable names recorded at spawn, and kept in
        memory only. ``names``: recorded names whose value was not found there; every
        ``NAME=...`` line for them is masked too. When nothing can be read back, raises
        ``ApiError(503)`` with :data:`OUTPUT_WITHHELD_MESSAGE`: output is never returned
        with secrets it cannot mask. ``assume``: variable entries (``{"name", "secret"}``) to
        read back when no names were recorded for this environment (a cloud agent's model key).
        """
        with self._lock:
            sent = self._sent_values.get(rec.id)
            read = self._read_values.get(rec.id)
        recorded = self._recorded_vars(rec.id) or [dict(e) for e in assume]
        # Current values by name as well (a rotated secret's new value is hidden too).
        by_name = self._values_by_name(rec, recorded)
        if sent is not None:
            return set(sent) | by_name, []
        if read is not None:
            return set(read[0]) | by_name, list(read[1])
        if not recorded:
            return by_name, []
        found = self._values_from_container(rec, recorded)
        if found is None:
            raise ApiError(503, "unavailable", OUTPUT_WITHHELD_MESSAGE.format(
                what=what, name=rec.name or rec.id, host=rec.host_id))
        values, missing = found
        with self._lock:
            self._read_values[rec.id] = (frozenset(values), tuple(missing))
        return set(values) | by_name, list(missing)

    def _values_by_name(self, rec: InstanceRecord, recorded: list[dict]) -> set[str]:
        """Current values of the recorded secret names and template literals (best effort)."""
        literals: dict[str, str] = {}
        if any(not e.get("secret") and not e.get("display") for e in recorded):
            try:
                tpl = self.services.templates.get(rec.template_id)
                for spec in tpl.variables or []:
                    src = spec.source or ""
                    if (src.startswith("literal:") and (spec.hosts is None
                                                         or rec.host_id in spec.hosts)):
                        literals[spec.name] = src[len("literal:"):]
            except Exception:  # noqa: BLE001 - deleted template: literals are unknown now
                literals = {}
        values: set[str] = set()
        for entry in recorded:
            try:
                if entry.get("secret"):
                    current = self.services.secrets.get(entry["secret"])
                elif not entry.get("display"):
                    current = literals.get(entry["name"])
                else:
                    current = None
            except Exception:  # noqa: BLE001 - e.g. no secret scope bound
                current = None
            if current:
                values.add(current)
        return values

    def _values_from_container(self, rec: InstanceRecord, recorded: list[dict]
                               ) -> tuple[set[str], list[str]] | None:
        """Current values of the recorded names in the container's configuration, or ``None``."""
        try:
            host = self._host(rec.host_id)
            info = self._inspect(self._transport(host), self._rt_for_record(rec),
                                 container_name(rec.id))
        except Exception:  # noqa: BLE001 - host gone or unreachable: nothing could be read
            return None
        env_list = ((info or {}).get("Config") or {}).get("Env")
        if not isinstance(env_list, list):
            return None
        current: dict[str, str] = {}
        for item in env_list:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                current[key] = value
        values: set[str] = set()
        missing: list[str] = []
        for entry in recorded:
            value = current.get(entry["name"])
            if not value:
                missing.append(entry["name"])
            elif entry.get("secret") or entry.get("display") or len(value) >= _MIN_LITERAL_REDACT:
                values.add(value)
        return values, missing

    def _redact_output(self, text: str, values: Iterable[str], names: Iterable[str]) -> str:
        """Environment output for the chat or the page: the given values, the given names'
        ``NAME=...`` lines and secret-shaped tokens (vendor keys, private keys, bearer tokens,
        passwords in URLs) are masked; ordinary words are left alone even when a nearby name
        contains "secret" or "key"."""
        out = text or ""
        for v in sorted({v for v in values if v}, key=len, reverse=True):
            out = out.replace(v, "[redacted]")
        out = redact_tokens(out)
        for name in names:
            out = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(name)}=[^\r\n]*",
                         f"{name}=[redacted]", out)
        return out

    def output_mask(self, instance_id: str, *, assume: Iterable[dict] = ()
                    ) -> Callable[[str], str]:
        """A function that masks what ``instance_id`` was started with (the same rule as
        exec output and logs: values known here or read back from the container, plus
        secret-shaped tokens), for text a cloud agent run shows.

        Raises ``ApiError(404)`` when the environment is gone (nothing to read back) and
        ``ApiError(503)`` when its values cannot be determined: the caller must not show
        the text then.
        """
        rec = self._record(instance_id)
        if rec.status == "destroyed":
            raise ApiError(404, "not_found", f"instance {instance_id} no longer exists")
        values, names = self._redaction(rec, "Output", assume=assume)
        return lambda text: self._redact_output(text, values, names)

    def _redact(self, text: str, values: Iterable[str]) -> str:
        try:
            return self.services.secrets.redact(text or "", list(values))
        except Exception:  # noqa: BLE001
            out = text or ""
            for v in sorted({v for v in values if v}, key=len, reverse=True):
                out = out.replace(v, "[redacted]")
            return out

    def _fail_cmd(self, what: str, res: ExecResult, values: Iterable[str] = ()) -> ApiError:
        detail = _short(self._redact(res.stderr or res.stdout or f"rc={res.rc}", values), 300)
        if res.timed_out:
            return ApiError(504, "internal", f"{what} timed out")
        return ApiError(502, "internal", f"{what} failed: {detail}")

    # ------------------------------------------------------------------ preview

    def preview(self, template_id: str, host_id: str) -> SendPreview:
        """What a spawn would send to the host. Never contains variable values."""
        tpl = self._template(template_id)
        host = self._host(host_id)
        secrets = self.services.secrets
        variables: list[VariablePreview] = []
        warnings: list[str] = []
        for spec in tpl.variables or []:
            source = spec.source or ""
            kind = "secret" if source.startswith("secret:") else "literal"
            send, reason = False, ""
            if spec.hosts is not None and host_id not in spec.hosts:
                reason = f"not allowed on host {host_id}"
            elif source.startswith("literal:"):
                send = True
            elif source.startswith("secret:") and source[len("secret:"):]:
                name = source[len("secret:"):]
                if secrets.is_set(name):
                    send = True
                else:
                    reason = f"secret {name} not set"
            else:
                reason = "invalid source"
            variables.append(VariablePreview(name=spec.name, source_kind=kind, scope=spec.scope,
                                             will_send=send, reason=reason))
            if not send and reason.startswith("secret "):
                warnings.append(f"{spec.name}: {reason}; it will not be sent")

        if tpl.kind != "container":
            warnings.append("this template runs as a Slurm job, not a container")
        if not _fits(host.kind, tpl):
            warnings.append(f"template {tpl.id} does not run on {host.kind} hosts")
        if host.kind == "ec2":
            warnings.extend(self._ec2_warnings(host, tpl))
        publish: list[str] = []
        if tpl.display:
            publish.append(f"{LOOPBACK}::{DISPLAY_PORT}")
            if tpl.kind == "container" and not image_provides_display(tpl.image):
                warnings.append(DISPLAY_IMAGE_MESSAGE)
            if _display_overrides() is None:
                warnings.append("display support is not installed")
            caps = getattr(self._host_state(host_id), "capabilities", None)
            state = getattr(caps, "display", "unknown")
            if state == "unsupported":
                why = getattr(caps, "display_reason", "") or "host reports no display support"
                warnings.append(f"display is not supported on this host: {why}")
        for port in self._template_ports(tpl):
            publish.append(f"{LOOPBACK}::{port}")
        limits = host.limits
        active = self._active_count(host_id)
        if limits is not None and active >= limits.max_instances:
            warnings.append(f"host limit reached: {active} of {limits.max_instances} instances")
        if limits is not None and limits.max_cpus is not None and tpl.cpus > limits.max_cpus:
            warnings.append(f"template asks for {tpl.cpus:g} CPUs; host allows {limits.max_cpus:g}")
        if (limits is not None and limits.max_memory_gb is not None
                and tpl.memory_gb > limits.max_memory_gb):
            warnings.append(f"template asks for {tpl.memory_gb:g} GB memory; "
                            f"host allows {limits.max_memory_gb:g}")
        labels = instance_labels("<instance-id>", self.install_id(), tpl.id, display=tpl.display)
        return SendPreview(template_id=tpl.id, host_id=host_id, image=tpl.image,
                           command=list(tpl.command or []), cpus=tpl.cpus,
                           memory_gb=tpl.memory_gb, gpus=tpl.gpus, network=tpl.network,
                           display=tpl.display, variables=variables, labels=labels,
                           publish=publish, warnings=warnings)

    def _host_state(self, host_id: str) -> Any:
        try:
            return self.store.get_host_state(host_id)
        except Exception:  # noqa: BLE001
            return None

    def _active_count(self, host_id: str) -> int:
        return len(self.store.list_instances(host_id, statuses=set(_ACTIVE)))

    # ------------------------------------------------------------------ spawn

    @staticmethod
    def _template_ports(tpl: TemplateSpec) -> list[int]:
        """Container ports a template publishes (valid, unique, never the screen's own)."""
        out: list[int] = []
        for port in getattr(tpl, "ports", None) or []:
            if (isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535
                    and port not in out and not (tpl.display and port == DISPLAY_PORT)):
                out.append(port)
        return out[:10]

    @staticmethod
    def _is_agent_template(tpl: TemplateSpec) -> bool:
        return isinstance(tpl.agent, dict) and bool(tpl.agent.get("enabled", True))

    def _spawnable_template_ids(self, host_kind: str | None = None) -> list[str]:
        """Ids ``env_spawn`` accepts: container templates that are not cloud-agent templates."""
        try:
            found = self.services.templates.list()
        except Exception:  # noqa: BLE001 - an unreadable list falls back to no suggestions
            return []
        return [t.id for t in found
                if t.kind == "container" and not self._is_agent_template(t)
                and (host_kind is None or _fits(host_kind, t))]

    def _host_ids(self) -> list[str]:
        try:
            return [h.id for h in self.store.list_hosts()]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def limit_message(host: HostSpec, active: int, limit: int) -> str:
        return (f"{host.name or host.id} already runs {active} of {limit} environments. Stop or "
                "delete one, or raise Max environments for this host.")

    def validate_spawn(self, template_id: Any, host_id: Any, *, allow_agent: bool = False,
                       check_limit: bool = True) -> tuple[TemplateSpec, HostSpec]:
        """Check a spawn request before anything is shown or started.

        Returns ``(template, host)`` or raises an :class:`ApiError` whose message says what is
        wrong and what to do: an unknown template lists the valid template ids, an unknown host
        lists the host ids, a template that cannot run on the host's kind, a cloud-agent template
        (``cloud_agent_spawn``), a Slurm job template (``slurm_submit``) and a host at its limit
        are named as such. ``allow_agent`` is for the agent manager, which spawns agent templates;
        ``check_limit=False`` leaves the host limit to :meth:`spawn`, which checks it under its lock.
        """
        if not allow_agent:
            try:
                getattr(self.services, "agents")      # seeds the built-in agent templates
            except Exception:  # noqa: BLE001 - templates must list without the agents module
                pass
        tpl: TemplateSpec | None = None
        try:
            tpl = self.services.templates.get(template_id) if isinstance(template_id, str) else None
        except KeyError:
            tpl = None
        except ApiError as exc:
            if exc.code != "not_found":
                raise
            tpl = None
        host = self.store.get_host(host_id) if isinstance(host_id, str) and host_id else None
        problems: list[str] = []
        if tpl is None:
            valid = ", ".join(self._spawnable_template_ids(host.kind if host else None))
            problems.append(f"Template {template_id!r} not found."
                            + (f" Valid templates: {valid}." if valid else ""))
        if host is None:
            ids = ", ".join(self._host_ids())
            problems.append(f"Host {host_id!r} not found."
                            + (f" Valid host ids: {ids}." if ids else " Add a host first."))
        if problems:
            raise ApiError(404, "not_found", " ".join(problems))
        assert tpl is not None and host is not None
        if tpl.kind != "container":
            raise ApiError(400, "invalid",
                           f"Template {tpl.id} is a Slurm job template. Submit it with "
                           "slurm_submit instead of env_spawn.")
        if self._is_agent_template(tpl) and not allow_agent:
            raise ApiError(400, "invalid",
                           f"Template {tpl.id} is a cloud agent template. Use cloud_agent_spawn "
                           "to start a cloud agent; env_spawn is for plain environments.")
        if not _fits(host.kind, tpl):
            fits = ", ".join(self._spawnable_template_ids(host.kind)) if not allow_agent else ""
            raise ApiError(400, "invalid",
                           f"Template {tpl.id} cannot run on {host.kind} hosts "
                           f"(it runs on: {', '.join(tpl.host_kinds or [])})."
                           + (f" Templates for {host.kind} hosts: {fits}." if fits else ""))
        if host.kind == "ec2":
            self._check_machine(host)
        limits = host.limits
        if limits is not None and check_limit:
            active = self._active_count(host.id)
            if active >= limits.max_instances:
                raise ApiError(409, "limit_reached",
                               self.limit_message(host, active, limits.max_instances))
        return tpl, host

    # -- EC2 hosts: the machine must run first ------------------------------------

    def _check_machine(self, host: HostSpec) -> None:
        """Refuse a spawn on an EC2 host whose machine is not ready (unless it auto-starts)."""
        ec2 = getattr(host, "ec2", None)
        state = getattr(ec2, "machine_state", "none")
        if state == "ready":
            return
        if state in ("none", "stopped") and getattr(ec2, "auto_start", False):
            return                       # spawn() starts it; the approval card says so
        hint = {"none": "Press Start machine on the host (or compute_host action=start) to "
                        "launch it.",
                "stopped": "Press Start machine on the host (or compute_host action=start).",
                "launching": "It is starting; try again when the host shows ready.",
                "starting": "It is starting; try again when the host shows ready."}.get(
            state, "Check the host card for what went wrong.")
        raise ApiError(409, "not_running",
                       f"The EC2 machine of {host.name} is {state.replace('none', 'not launched')}. "
                       f"{hint}")

    @staticmethod
    def _ec2_warnings(host: HostSpec, tpl: TemplateSpec) -> list[str]:
        out = []
        ec2 = getattr(host, "ec2", None)
        if getattr(ec2, "machine_state", "") != "ready" and getattr(ec2, "auto_start", False):
            out.append("The host's EC2 machine is not running; it will be started first, and AWS "
                       "bills the account while it runs.")
        if tpl.display:
            out.append("The Desktop (noVNC) image needs about 2 GB of memory to build and run; "
                       "on a 1 GB instance such as t3.micro it may fail. Use a larger instance "
                       "type for screens.")
        return out

    def mark_stopped(self, instance_id: str, reason: str = "host-gone") -> None:
        """Record that an environment stopped because its machine stopped (no remote call)."""
        def apply(r: InstanceRecord) -> None:
            r.status, r.stop_reason, r.ports = "stopped", reason, {}
        if self._update(instance_id, apply, expect=("running", "creating", "exited")) is not None:
            self._mark_stopped_at(instance_id)
            self._close_tunnels(instance_id)
            rec = self.store.get_instance(instance_id)
            self._emit([instance_id], rec.host_id if rec else None, "stopped")

    def spawn(self, template_id: str, host_id: str, *, name: str | None = None,
              ttl_minutes: int | None = None, idle_minutes: int | None = None,
              session_id: str | None = None) -> InstanceRecord:
        tpl, host = self.validate_spawn(template_id, host_id, allow_agent=True,
                                         check_limit=False)
        if host.kind == "ec2" and getattr(host.ec2, "machine_state", "") != "ready":
            # auto_start is on (validate_spawn refused otherwise): start and wait for it.
            host = self.services.ec2.start_machine(host, wait=True)
            if getattr(host.ec2, "machine_state", "") != "ready":
                raise ApiError(502, "not_running",
                               getattr(host.ec2, "machine_detail", "") or
                               "The EC2 machine did not become ready.")
        ttl, idle = self._effective_limits(tpl, ttl_minutes, idle_minutes)
        limits = host.limits
        if limits is not None and limits.max_cpus is not None and tpl.cpus > limits.max_cpus:
            raise ApiError(409, "limit_reached",
                           f"template asks for {tpl.cpus:g} CPUs; host {host.id} allows "
                           f"{limits.max_cpus:g}")
        if (limits is not None and limits.max_memory_gb is not None
                and tpl.memory_gb > limits.max_memory_gb):
            raise ApiError(409, "limit_reached",
                           f"template asks for {tpl.memory_gb:g} GB; host {host.id} allows "
                           f"{limits.max_memory_gb:g}")
        overrides = None
        if tpl.display:
            overrides = _display_overrides()
            if overrides is None:
                raise ApiError(503, "unavailable", "display support is not installed")

        env, _previews = self.services.secrets.resolve(list(tpl.variables or []), host.id)
        for key, value in env.items():
            if not _ENV_NAME_RE.fullmatch(key):
                raise ApiError(400, "invalid", f"invalid variable name {key!r}")
            if _bad_value(value):
                raise ApiError(400, "invalid", f"variable {key} contains a line break")

        rt_name = self._runtime_name(host)
        rt = runtime_for(rt_name)
        install_id = self.install_id()
        now = self._now()
        with self._lock:
            active = self._active_count(host.id)
            if limits is not None and active >= limits.max_instances:
                raise ApiError(409, "limit_reached",
                               self.limit_message(host, active, limits.max_instances))
            inst_id = new_id("i")
            while self.store.get_instance(inst_id) is not None:
                inst_id = new_id("i")
            if not name:
                name = self._default_name(tpl.id)
            rec = InstanceRecord(
                id=inst_id, name=str(name)[:80], host_id=host.id, template_id=tpl.id,
                runtime=rt_name, container_id=None, status="creating", created_at=_iso(now),
                started_at=None,
                expires_at=None if tpl.persistent else _iso(now + timedelta(minutes=ttl)),
                last_activity_at=_iso(now), idle_minutes=idle, display=bool(tpl.display),
                display_state="supported" if tpl.display else "unsupported", ports={},
                stats=None, error=None, session_id=session_id,
                ttl_minutes=None if tpl.persistent else ttl)
            self._save(rec)

        cname = container_name(inst_id)
        values = list(env.values())
        t = self._transport(host)
        display_env: dict[str, str] = {}
        try:
            labels = instance_labels(inst_id, install_id, tpl.id, display=bool(tpl.display))
            publish: list[tuple[str, int | None, int]] = []
            if overrides is not None:
                d_env, d_publish, d_labels = self._call_overrides(overrides, inst_id)
                d_env = dict(d_env or {})
                for key, value in d_env.items():
                    if (not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key)
                            or not isinstance(value, str) or _bad_value(value)):
                        raise ApiError(400, "invalid", f"invalid display variable {key!r}")
                env = dict(env, **d_env)
                values = list(env.values())
                publish = [(str(p[0]), p[1], int(p[2])) for p in (d_publish or [])]
                labels = dict(labels, **(d_labels or {}))
                display_env = d_env
            for port in self._template_ports(tpl):
                if all(p[2] != port for p in publish):
                    publish.append((LOOPBACK, None, port))
            # The secret NAME actually used for this run (a cloud agent's spawn-time override of
            # the key variable), so the output is still redacted after a Hermes restart.
            from .secrets import apply_variable_sources
            redact_values, redact_names = self._redact_plan(
                apply_variable_sources(list(tpl.variables or [])), env, host.id, display_env)
            self._remember_vars(inst_id, redact_values, redact_names)
            self._remember_workdir(inst_id, tpl.workdir)
            res, cid = self._create(t, rt, inst_id, cname, tpl, labels, env, publish, values,
                                   host)
            info = self._inspect(t, rt, cname)
            state = str(((info or {}).get("State") or {}).get("Status") or "running").lower()
            ports = self._ports(t, rt, cname, [p[2] for p in publish], info,
                                wait=state == "running", fixed={p[2]: p[1] for p in publish
                                                                if p[1] is not None})
            started = self._now_iso()
            container_id = cid or str((info or {}).get("Id") or "")[:64] or None

            def mark_created(r: InstanceRecord) -> None:
                r.container_id = container_id
                r.status = "running" if state == "running" else "exited"
                r.ports = ports
                r.started_at = started
                r.last_activity_at = started

            # Re-read under the lock: a Destroy made while the container was being
            # created must not be undone by this save.
            saved = self._update(inst_id, mark_created, expect=_NOT_DESTROYED)
            if saved is not None:
                rec = saved
        except Exception as exc:
            self._fail_spawn(t, rt, rec, cname, host.id, values, exc)
        if saved is None:
            self._discard_orphan(t, rt, cname, inst_id)
            raise ApiError(409, "conflict",
                           f"environment {inst_id} was destroyed while it was starting")
        self._emit([inst_id], host.id, "spawned")
        return rec

    def _discard_orphan(self, t: Any, rt: Any, cname: str, inst_id: str) -> None:
        """Remove a container created for an instance destroyed meanwhile; drop its secrets."""
        try:
            t.run(rt.lifecycle_argv("rm", cname), timeout=_CMD_TIMEOUT)
        except Exception:  # noqa: BLE001 - best effort; the reaper sweeps leftovers
            log.debug("cloud-compute: cleanup of %s failed", cname, exc_info=True)
        self._forget_vars(inst_id)
        self._forget_display_token(inst_id)
        self._close_tunnels(inst_id)

    def _fail_spawn(self, t: Any, rt: Any, rec: InstanceRecord, cname: str, host_id: str,
                    values: list[str], exc: Exception) -> None:
        inst_id = rec.id
        msg = exc.message if isinstance(exc, ApiError) else f"{type(exc).__name__}: {exc}"
        err = _short(self._redact(msg, values), 500)

        def mark_error(r: InstanceRecord) -> None:
            r.status, r.error = "error", err

        # A Destroy made meanwhile wins; the error is still reported to the caller.
        if self._update(inst_id, mark_error, expect=_NOT_DESTROYED) is not None:
            self._emit([inst_id], host_id, "error")
        try:
            t.run(rt.lifecycle_argv("rm", cname), timeout=_CMD_TIMEOUT)
        except Exception:  # noqa: BLE001 - best effort; the reaper sweeps leftovers
            log.debug("cloud-compute: cleanup of %s failed", cname, exc_info=True)
        self._forget_display_token(inst_id)
        if isinstance(exc, ApiError):
            raise ApiError(exc.status, exc.code, err) from None
        raise ApiError(500, "internal", err) from None

    def _default_name(self, template_id: str) -> str:
        """``<template>-<n>``: n is the smallest positive integer no live instance uses.

        Destroyed instances free their number. The display name is separate from the
        container name (``cc-<id>``), so reuse cannot collide with a container.
        """
        taken = {r.name for r in self.store.list_instances()
                 if r.template_id == template_id and r.status != "destroyed"}
        n = 1
        while f"{template_id}-{n}" in taken:
            n += 1
        return f"{template_id}-{n}"

    def effective_limits(self, template_id: str, ttl_minutes: int | None = None,
                         idle_minutes: int | None = None) -> tuple[int, int]:
        """``(ttl, idle)`` minutes a spawn of ``template_id`` would get right now.

        Same rule as :meth:`spawn`; used by approval text so it names what will be applied.
        """
        return self._effective_limits(self._template(template_id), ttl_minutes, idle_minutes)

    def _effective_limits(self, tpl: TemplateSpec, ttl_minutes: Any,
                          idle_minutes: Any) -> tuple[int, int]:
        """The TTL and idle timeout (minutes) a new instance gets.

        Precedence: the request, then the template, then the ``default_ttl_minutes`` /
        ``default_idle_minutes`` settings. A template value of ``None`` means "use the
        Settings default", so a TTL is always in force (it is the spend cap). The settings
        are re-read now, so a value saved a moment ago applies without a restart.
        """
        ttl = self._minutes(ttl_minutes, tpl.ttl_minutes, "ttl_minutes")
        idle = self._minutes(idle_minutes, tpl.idle_minutes, "idle_minutes")
        if ttl is None:
            ttl = self._config_minutes("default_ttl_minutes")
        if idle is None:
            idle = self._config_minutes("default_idle_minutes")
        return ttl, idle

    def _config_minutes(self, name: str) -> int:
        cfg = getattr(self.services, "config", None)
        reload = getattr(cfg, "reload", None)
        if callable(reload):            # LiveConfig: a spawn is a user action, never use stale values
            try:
                reload()
            except Exception:  # noqa: BLE001 - keep the last good values
                pass
        meta = CONFIG_FIELDS[name]
        value = getattr(cfg, name, meta["default"]) if cfg is not None else meta["default"]
        if isinstance(value, bool) or not isinstance(value, int) or value < meta["min"]:
            return meta["default"]
        return min(value, meta["max"])

    @staticmethod
    def _minutes(value: Any, default: Any, what: str) -> int | None:
        if value is None:
            return default
        if (isinstance(value, bool) or not isinstance(value, int)
                or not MIN_MINUTES <= value <= MAX_MINUTES):
            raise ApiError(400, "invalid",
                           f"{what} must be an integer from {MIN_MINUTES} to {MAX_MINUTES}")
        return value

    def _create(self, t: Any, rt: Any, inst_id: str, cname: str, tpl: TemplateSpec,
                labels: dict[str, str], env: dict[str, str],
                publish: list[tuple[str, int | None, int]], values: list[str],
                host: HostSpec | None = None) -> tuple[ExecResult, str | None]:
        """Write the env file, ``run -d`` and always delete the file.

        Remote hosts get the file over stdin (umask 077 shell). The local Windows computer
        gets it written by Python as a native path, because Git's sh prints MSYS paths
        that docker.exe/podman.exe cannot open.
        """
        env_path: str | None = None
        wrote = False
        native = bool(env) and _local_native(host)
        try:
            if env:
                data = "".join(f"{k}={v}\n" for k, v in env.items()).encode("utf-8")
                wrote = True
                if native:
                    env_path = self._write_local_env(inst_id, data)
                else:
                    res = t.run(["sh", "-c", _WRITE_ENV, "sh", inst_id], stdin=data, timeout=60)
                    lines = (res.stdout or "").strip().splitlines()
                    if res.rc != 0 or not lines:
                        raise self._fail_cmd("writing the env file", res, values)
                    env_path = lines[-1].strip()
            spec = RunSpec(name=cname, image=tpl.image, labels=labels,
                           command=list(tpl.command or []), workdir=tpl.workdir or None,
                           cpus=tpl.cpus, memory_gb=tpl.memory_gb, gpus=tpl.gpus,
                           env_file=env_path, publish=publish, network=tpl.network)
            res = t.run(rt.run_argv(spec), timeout=_RUN_TIMEOUT)
            cid_hint: str | None = None
            if res.rc != 0 and spec.workdir and _MISSING_WORKDIR.search(res.stderr or ""):
                # Podman refuses -w for a directory the image lacks (Docker creates it).
                workdir = spec.workdir
                res, cid_hint = self._podman_make_workdir(t, rt, cname, spec, res, values)
                if res.rc == 0:
                    self._own_workdir(t, rt, cname, workdir)
            elif res.rc == 0 and spec.workdir and rt.name == "docker":
                self._own_workdir(t, rt, cname, spec.workdir)
            if res.rc != 0:
                raise self._fail_cmd(f"{rt.name} run", res, values)
        finally:
            if native:
                self._remove_local_env(env_path)
            elif wrote:
                try:
                    t.run(["sh", "-c", _REMOVE_ENV, "sh", inst_id], timeout=60)
                except Exception:  # noqa: BLE001
                    log.warning("cloud-compute: could not remove the env file for %s", inst_id)
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        return res, (lines[-1][:64] if lines else cid_hint)

    def _podman_make_workdir(self, t: Any, rt: Any, cname: str, spec: RunSpec,
                             failed: ExecResult, values: list[str]) -> tuple[ExecResult, str | None]:
        """Start a container whose image lacks the working folder (Podman only).

        Podman leaves the container created (with ``-w`` set) when the folder is missing.
        The folder is copied into that stopped container, which gives it to the container's
        own user, and the container is started: the template's own command can use the
        folder from its first second. If the created container is gone, the container is
        started without ``-w`` and the folder is made as root and handed to the user.
        """
        workdir = spec.workdir or "/work"
        match = re.search(r"on container ([0-9a-f]{12,64})", failed.stderr or "")
        cid_hint = match.group(1) if match else None
        if self._copy_workdir_in(t, rt, cname, workdir):
            started = t.run(rt.lifecycle_argv("start", cname), timeout=_RUN_TIMEOUT)
            if started.rc != 0:
                return started, None
            return exec_result_like(started, stdout=""), cid_hint
        t.run(rt.lifecycle_argv("rm", cname), timeout=_CMD_TIMEOUT)
        spec.workdir = None
        res = t.run(rt.run_argv(spec), timeout=_RUN_TIMEOUT)
        if res.rc == 0:
            self._make_workdir_by_exec(t, rt, cname, workdir, values)
        return res, None

    @staticmethod
    def _copy_workdir_in(t: Any, rt: Any, cname: str, workdir: str) -> bool:
        """Copy an empty ``workdir`` into the created container; only missing folders are sent.

        ``podman cp`` hands what it copies to the container's primary user. The deepest
        folder that exists is found by trying one level up each time, so a folder the image
        already has (``/home``) is never given to anyone else.
        """
        parts = [p for p in workdir.split("/") if p]
        if not parts:
            return True
        for depth in range(1, len(parts) + 1):
            dest = "/" + "/".join(parts[:-depth])
            made = t.run(rt.cp_in_argv(cname, dest), stdin=_dir_tar(parts[-depth:]),
                         timeout=_CMD_TIMEOUT)
            if made.rc == 0:
                return True
            if _NO_SUCH.search(made.stderr or ""):
                return False
        return False

    def _make_workdir_by_exec(self, t: Any, rt: Any, cname: str, workdir: str,
                              values: list[str]) -> None:
        """Make ``workdir`` as root inside the running container and hand it to its user."""
        said = (f"The working folder {workdir} does not exist in the image and could not be "
                f"created. Set the template's Working folder to a folder the image has "
                f"(for example /tmp), then start again.")
        ids = []
        for flag in ("-u", "-g"):
            got = t.run(rt.exec_argv(cname, ["id", flag]), timeout=_CMD_TIMEOUT)
            text = (got.stdout or "").strip()
            if got.rc == 0 and text.isdigit():
                ids.append(text)
            elif got.rc != 0 and _NOT_RUNNING.search(got.stderr or ""):
                raise ApiError(502, "internal",
                               f"The container stopped before the working folder {workdir} "
                               f"could be created. If the template's command uses that "
                               f"folder, set the template's Working folder to a folder the "
                               f"image has (for example /tmp).")
        made = t.run(rt.exec_argv(cname, ["mkdir", "-p", "--", workdir], user="0"),
                     timeout=_CMD_TIMEOUT)
        if made.rc != 0:
            detail = _short(self._redact(made.stderr or made.stdout or "", values), 200)
            raise ApiError(502, "internal", f"{said} ({detail})")
        if len(ids) == 2:
            fixed = t.run(rt.exec_argv(cname, ["chown", f"{ids[0]}:{ids[1]}", "--", workdir],
                                       user="0"), timeout=_CMD_TIMEOUT)
        else:
            fixed = t.run(rt.exec_argv(cname, ["chmod", "0777", "--", workdir], user="0"),
                          timeout=_CMD_TIMEOUT)
        if fixed.rc != 0:
            raise ApiError(502, "internal", said)

    @staticmethod
    def _own_workdir(t: Any, rt: Any, cname: str, workdir: str) -> None:
        """Make sure the container's own user can write the working folder.

        Docker creates a missing ``-w`` folder owned by root. Best effort: a folder the
        container's user can already write is left alone, and nothing here fails the spawn.
        """
        try:
            if t.run(rt.exec_argv(cname, ["test", "-w", workdir]), timeout=_CMD_TIMEOUT).rc == 0:
                return
            ids = []
            for flag in ("-u", "-g"):
                got = t.run(rt.exec_argv(cname, ["id", flag]), timeout=_CMD_TIMEOUT)
                text = (got.stdout or "").strip()
                if got.rc != 0 or not text.isdigit():
                    return
                ids.append(text)
            t.run(rt.exec_argv(cname, ["chown", f"{ids[0]}:{ids[1]}", "--", workdir], user="0"),
                  timeout=_CMD_TIMEOUT)
        except Exception:  # noqa: BLE001 - never fail a spawn over this
            log.debug("cloud-compute: could not hand the working folder to the container user")

    def _write_local_env(self, inst_id: str, data: bytes) -> str:
        """Write ``data`` to a fresh private file and return its native path."""
        d = _local_env_dir(self.store)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        fd, path = tempfile.mkstemp(prefix=f"{inst_id}-", suffix=".env", dir=str(d))
        try:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    @staticmethod
    def _remove_local_env(path: str | None) -> None:
        if not path:
            return
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("cloud-compute: could not remove the env file %s", Path(path).name)

    def _inspect(self, t: Any, rt: Any, cname: str) -> dict | None:
        try:
            res = t.run(rt.inspect_argv(cname), timeout=_CMD_TIMEOUT)
        except Exception:  # noqa: BLE001
            return None
        if res.rc != 0:
            return None
        try:
            data = json.loads(res.stdout or "")
        except ValueError:
            return None
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _bound_ports(info: dict | None) -> dict[int, int]:
        """``{container port: host port}`` for every tcp binding in a ``<rt> inspect`` result."""
        found: dict[int, int] = {}
        bound = ((info or {}).get("NetworkSettings") or {}).get("Ports") or {}
        if not isinstance(bound, dict):
            return found
        for key, entries in bound.items():
            m = re.fullmatch(r"(\d{1,5})/tcp", str(key))
            if not m or not isinstance(entries, list):
                continue
            for e in entries:
                if isinstance(e, dict) and str(e.get("HostPort") or "").isdigit():
                    found.setdefault(int(m.group(1)), int(e["HostPort"]))
                    break
        return found

    def _ports_once(self, t: Any, rt: Any, cname: str, expected: list[int], info: dict | None,
                    fixed: dict[int, int] | None = None) -> dict[str, str]:
        known = self._bound_ports(info)
        ports: dict[str, str] = {}
        for cport, hp in known.items():
            ports[_port_key(cport)] = f"{LOOPBACK}:{hp}"
        for cport in expected:
            key = _port_key(cport)
            if key in ports:
                continue
            hp: int | None = (fixed or {}).get(cport)
            if hp is None:
                try:
                    res = t.run(rt.port_argv(cname, cport), timeout=_CMD_TIMEOUT)
                    hp = rt.parse_port(res.stdout) if res.rc == 0 else None
                except Exception:  # noqa: BLE001
                    hp = None
            if hp is not None:
                ports[key] = f"{LOOPBACK}:{hp}"
        return ports

    def _ports(self, t: Any, rt: Any, cname: str, expected: list[int], info: dict | None, *,
               wait: bool = False, fixed: dict[int, int] | None = None) -> dict[str, str]:
        """Published ports of a container: ``{"novnc": "127.0.0.1:41234", "8000": ...}``.

        ``expected`` are the container ports that should be published. With ``wait`` the
        runtime is asked again (bounded by :data:`PORT_WAIT_S`) while any expected port is
        still unreported, instead of saving an empty map: right after ``run -d`` or ``start``
        the binding is not always visible yet.
        """
        ports = self._ports_once(t, rt, cname, expected, info, fixed)
        want = {_port_key(c) for c in expected}
        if not wait or want <= set(ports):
            return ports
        deadline = time.monotonic() + PORT_WAIT_S
        while not want <= set(ports) and time.monotonic() < deadline:
            time.sleep(PORT_POLL_S)
            ports = self._ports_once(t, rt, cname, expected, self._inspect(t, rt, cname), fixed)
        return ports

    def _expected_ports(self, rec: InstanceRecord) -> list[int]:
        """Container ports a record's environment should publish (its screen plus template ports)."""
        out: list[int] = [DISPLAY_PORT] if rec.display else []
        try:
            out += [p for p in self._template_ports(self.services.templates.get(rec.template_id))
                    if p not in out]
        except Exception:  # noqa: BLE001 - template gone: whatever the runtime reports
            pass
        return out

    def refresh_ports(self, instance_id: str) -> InstanceRecord:
        """Re-read the published ports of a running environment from the runtime and save them.

        The runtime picks a new host port every time a container starts, so a stored port can
        go stale. A stopped environment publishes nothing and is returned unchanged; a runtime
        that cannot be reached leaves the record as it was. Raises ``not_found`` only.
        """
        rec = self._record(instance_id)
        if rec.status != "running":
            return rec
        try:
            host = self._host(rec.host_id)
            t, rt, cname = self._transport(host), self._rt_for_record(rec), container_name(rec.id)
            info = self._inspect(t, rt, cname)
            if info is None:
                return rec
            ports = self._ports(t, rt, cname, self._expected_ports(rec), info, wait=True)
        except ApiError:
            return rec
        except Exception:  # noqa: BLE001 - a refresh never breaks the caller
            log.debug("cloud-compute: could not refresh ports of %s", instance_id, exc_info=True)
            return rec
        if ports == rec.ports:
            return rec
        fresh = self._update(rec.id, lambda r: setattr(r, "ports", ports), expect=("running",))
        if fresh is None:
            return self.store.get_instance(rec.id) or rec
        self._emit([fresh.id], fresh.host_id, "ports")
        return fresh

    # ------------------------------------------------------------------ queries

    def list(self, host_id: str | None = None, refresh: bool = False, *,
             include_destroyed: bool = False) -> list[InstanceRecord]:
        if refresh:
            hosts = {r.host_id for r in self.store.list_instances(host_id, statuses=set(_ACTIVE))}
            for hid in sorted(hosts):
                try:
                    self._reconcile(hid)
                except Exception as exc:  # noqa: BLE001 - an offline host keeps its records
                    log.info("cloud-compute: could not refresh %s: %s", hid, exc)
        recs = self.store.list_instances(host_id)
        if not include_destroyed:
            recs = [r for r in recs if r.status != "destroyed"]
        return recs

    def _reconcile(self, host_id: str) -> None:
        host = self.store.get_host(host_id)
        if host is None:
            return
        recs = self.store.list_instances(host_id, statuses={"running", "stopped", "exited"})
        if not recs or _no_machine_running(host):
            return                      # an EC2 machine that is not running has nothing to read
        rt = runtime_for(self._runtime_name(host))
        res = self._transport(host).run(rt.ps_argv(f"{LABEL_INSTALL}={self.install_id()}"),
                                        timeout=60)
        if res.rc != 0:
            raise self._fail_cmd(f"{rt.name} ps", res)
        seen = {row["name"]: row for row in rt.parse_ps(res.stdout)}
        changed: dict[str, list[str]] = {}
        for rec in recs:
            row = seen.get(container_name(rec.id))
            if row is None:
                new, reason = "destroyed", "destroyed"
                rec.error = rec.error or "container no longer exists on the host"
                rec.ports = {}
            elif row["state"] == "running":
                new, reason = "running", "started"
            elif rec.status == "stopped":
                new, reason = "stopped", "stopped"
            else:
                new, reason = "exited", "stopped"
            if new != rec.status:
                # Re-read: a Stop/Destroy made during the ps call must not be overwritten.
                before, err, ports = rec.status, rec.error, rec.ports

                def apply(fresh: InstanceRecord, new=new, err=err, ports=ports) -> None:
                    fresh.status, fresh.error, fresh.ports = new, err, ports

                if self._update(rec.id, apply, expect=(before,)) is not None:
                    changed.setdefault(reason, []).append(rec.id)
                    if new == "exited":
                        self._mark_stopped_at(rec.id)
        for reason, ids in changed.items():
            self._emit(ids, host_id, reason)

    def get(self, instance_id: str) -> InstanceRecord:
        return self._record(instance_id)

    # ------------------------------------------------------------------ lifecycle

    def _lifecycle(self, rec: InstanceRecord, action: str) -> ExecResult:
        host = self._host(rec.host_id)
        rt = self._rt_for_record(rec)
        # Containers run with --init, so SIGTERM reaches the process; keep the grace short.
        argv = rt.lifecycle_argv(action, container_name(rec.id),
                                 timeout=STOP_TIMEOUT_S if action == "stop" else None,
                                 volumes=action == "rm" and self._is_agent_sandbox(rec))
        return self._transport(host).run(argv, timeout=_CMD_TIMEOUT)

    def _is_agent_sandbox(self, rec: InstanceRecord) -> bool:
        """A cloud-agent sandbox. Only its anonymous volume is removed with the container: the
        agent image declares ``/opt/data`` (empty and unwritable for the agent user; the run's
        files are in /work, which the save step copies first). An ordinary environment keeps
        any volume its image declares, because that data is not covered by the save."""
        try:
            agent = getattr(self.services.templates.get(rec.template_id), "agent", None)
        except Exception:  # noqa: BLE001 - template gone: decide by the id
            agent = None
        if isinstance(agent, dict) and agent.get("mode") in ("hermes", "script"):
            return True
        return rec.template_id in ("hermes-agent", "agent-script")

    def stop(self, instance_id: str, *, reason: str = "stopped") -> InstanceRecord:
        """Stop the container; its files stay. ``reason`` names why (see ``_STOP_REASON_FOR``).

        ``reason`` is also the ``instances.changed`` reason. ``expired`` / ``idle`` (and the
        legacy ``reaped``) set ``stop_reason`` accordingly; anything else is a user stop.
        """
        rec = self._record(instance_id)
        if rec.status == "destroyed":
            raise ApiError(409, "conflict", f"instance {instance_id} is destroyed")
        res = self._lifecycle(rec, "stop")
        if res.rc != 0:
            raise self._fail_cmd(f"{rec.runtime} stop", res)
        stop_reason = _STOP_REASON_FOR.get(reason, "user")

        def mark_stopped(r: InstanceRecord) -> None:
            r.status, r.stop_reason = "stopped", stop_reason

        fresh = self._update(rec.id, mark_stopped, expect=_NOT_DESTROYED)
        if fresh is None:   # destroyed (or deleted) while the stop command ran: keep that
            return self.store.get_instance(rec.id) or rec
        rec = fresh
        self._mark_stopped_at(rec.id)
        # The container's published ports are gone once it stops, so any open screen/port
        # tunnels would point at nothing. Close them exactly as destroy does. This covers
        # the single Stop, stop_all and the reaper's idle stop (all go through here).
        # start() deliberately does NOT reopen tunnels: the user reopens them on demand.
        self._close_tunnels(rec.id)
        self._emit([rec.id], rec.host_id, reason)
        return rec

    # -- stopped-at clock (store kv, not part of the REST record) -------------

    def _mark_stopped_at(self, instance_id: str, when: str | None = None) -> None:
        try:
            self.store.kv_set(STOPPED_AT_KEY_PREFIX + instance_id, when or self._now_iso())
        except Exception:  # noqa: BLE001 - only the opt-in auto-delete clock depends on it
            log.debug("cloud-compute: could not record stop time for %s", instance_id)

    def _set_leftover(self, instance_id: str) -> None:
        """Remember that the container of a deleted environment is still on its machine."""
        try:
            self.store.kv_set(LEFTOVER_KEY_PREFIX + instance_id, "1")
        except Exception:  # noqa: BLE001 - the record is still closed
            log.debug("cloud-compute: could not note the leftover of %s", instance_id)

    def has_leftover(self, instance_id: str) -> bool:
        """True while the container of a deleted environment still has to be removed."""
        try:
            return bool(self.store.kv_get(LEFTOVER_KEY_PREFIX + instance_id))
        except Exception:  # noqa: BLE001
            return False

    def clear_leftover(self, instance_id: str) -> None:
        try:
            self.store.kv_delete(LEFTOVER_KEY_PREFIX + instance_id)
        except Exception:  # noqa: BLE001
            log.debug("cloud-compute: could not clear the leftover of %s", instance_id)

    def clear_leftovers(self, host_id: str) -> None:
        """The machine of ``host_id`` is gone, and every container with it."""
        for rec in self.store.list_instances(host_id):
            if rec.status == "destroyed":
                self.clear_leftover(rec.id)

    def _clear_stopped_at(self, instance_id: str) -> None:
        try:
            kv_delete = getattr(self.store, "kv_delete", None)
            if kv_delete is not None:
                kv_delete(STOPPED_AT_KEY_PREFIX + instance_id)
        except Exception:  # noqa: BLE001
            log.debug("cloud-compute: could not clear stop time for %s", instance_id)

    def stopped_at(self, instance_id: str) -> str | None:
        """When a stopped environment was stopped (ISO UTC), or None if it is not stopped.

        A stopped record without a recorded time (stopped by an older version, or exited on
        its own) gets ``now``: the auto-delete clock starts when it is first seen, never
        earlier, so an old record is not deleted on the first tick after an upgrade.
        """
        rec = self.store.get_instance(instance_id)
        if rec is None or rec.status not in ("stopped", "exited"):
            return None
        try:
            value = self.store.kv_get(STOPPED_AT_KEY_PREFIX + instance_id)
        except Exception:  # noqa: BLE001
            value = None
        if isinstance(value, str) and parse_iso(value) is not None:
            return value
        now = self._now_iso()
        self._mark_stopped_at(instance_id, now)
        return now

    # -- expiry -----------------------------------------------------------------

    def _default_ttl(self) -> int:
        return self._config_minutes("default_ttl_minutes")

    def _needs_new_lifetime(self, rec: InstanceRecord, now: datetime) -> bool:
        """True when a start must grant a fresh time limit.

        That is when the limit has passed, or when the record has no limit only because it
        was adopted without a record (``stop_reason`` 'orphan') or stopped by expiry.
        Persistent environments (no ``expires_at`` and no such reason) never get one.
        """
        expires = parse_iso(rec.expires_at)
        if expires is None:
            return rec.stop_reason in ("expired", "orphan") and rec.ttl_minutes is not None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= now

    def lifetime_on_start(self, instance_id: str) -> int | None:
        """Minutes a start would grant (the environment's time limit has passed), else None."""
        rec = self._record(instance_id)
        if rec.status in ("destroyed", "creating") or not self._needs_new_lifetime(rec, self._now()):
            return None
        return rec.ttl_minutes or self._default_ttl()

    def mark_expiry_warned(self, instance_id: str) -> InstanceRecord | None:
        """Record that the expiry warning was sent; None if it already was (or not running)."""
        now = self._now_iso()
        sent: list[bool] = []

        def mark(r: InstanceRecord) -> None:
            if r.expiry_warned_at is None:
                r.expiry_warned_at = now
                sent.append(True)

        rec = self._update(instance_id, mark, expect=("running",))
        return rec if rec is not None and sent else None

    def extend(self, instance_id: str, minutes: int) -> InstanceRecord:
        """Push the time limit back: ``expires_at = max(now, expires_at) + minutes``.

        Resets the expiry warning. Only for running or stopped environments that have a
        time limit (persistent ones have none).
        """
        if (isinstance(minutes, bool) or not isinstance(minutes, int)
                or not 1 <= minutes <= EXTEND_MAX_MINUTES):
            raise ApiError(400, "invalid",
                           f"minutes must be an integer from 1 to {EXTEND_MAX_MINUTES}")
        rec = self._record(instance_id)
        if rec.status not in ("running", "stopped", "exited"):
            raise ApiError(409, "conflict", f"{rec.name} is {rec.status}; only a running or "
                                            "stopped environment can be extended")
        now = self._now()
        problem: list[str] = []

        def push(r: InstanceRecord) -> None:
            current = parse_iso(r.expires_at)
            if current is None:
                if r.stop_reason in ("expired", "orphan") and r.ttl_minutes is not None:
                    current = now
                else:
                    problem.append("none")
                    return
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            r.expires_at = _iso(max(now, current) + timedelta(minutes=minutes))
            r.expiry_warned_at = None

        fresh = self._update(rec.id, push, expect=("running", "stopped", "exited"))
        if problem:
            raise ApiError(409, "conflict", f"{rec.name} has no time limit to extend")
        if fresh is None:
            raise ApiError(409, "conflict", f"{rec.name} changed meanwhile; try again")
        self._emit([fresh.id], fresh.host_id, "extended")
        return fresh

    def start(self, instance_id: str) -> InstanceRecord:
        """Start a stopped container. If its time limit has passed, it gets a new one."""
        with self.instance_lock(instance_id):
            return self._start_locked(instance_id)

    def _start_locked(self, instance_id: str) -> InstanceRecord:
        rec = self._record(instance_id)
        if rec.status in ("destroyed", "creating"):
            raise ApiError(409, "conflict", f"instance {instance_id} is {rec.status}")
        res = self._lifecycle(rec, "start")
        if res.rc != 0:
            raise self._fail_cmd(f"{rec.runtime} start", res)
        now_dt = self._now()
        now = _iso(now_dt)
        default_ttl = self._default_ttl()
        # The runtime hands out new host ports on every start: a stored port would be dead.
        ports = self._read_started_ports(rec)

        def mark_started(r: InstanceRecord) -> None:
            if self._needs_new_lifetime(r, now_dt):
                ttl = r.ttl_minutes or default_ttl
                r.ttl_minutes = ttl
                r.expires_at = _iso(now_dt + timedelta(minutes=ttl))
                r.expiry_warned_at = None
            r.status, r.started_at, r.last_activity_at, r.error = "running", now, now, None
            r.stop_reason = None
            r.ports = ports

        fresh = self._update(rec.id, mark_started, expect=_NOT_DESTROYED)
        if fresh is None:
            return self.store.get_instance(rec.id) or rec
        self._clear_stopped_at(fresh.id)
        self._emit([fresh.id], fresh.host_id, "started")
        return fresh

    def _read_started_ports(self, rec: InstanceRecord) -> dict[str, str]:
        """Published ports of a container that was just started (``{}`` when unreadable)."""
        try:
            host = self._host(rec.host_id)
            t, rt, cname = self._transport(host), self._rt_for_record(rec), container_name(rec.id)
            return self._ports(t, rt, cname, self._expected_ports(rec),
                               self._inspect(t, rt, cname), wait=True)
        except Exception:  # noqa: BLE001 - the start itself succeeded
            log.debug("cloud-compute: could not read the ports of %s", rec.id, exc_info=True)
            return {}

    def _save_files(self, rec: InstanceRecord, export_reason: str) -> dict:
        """Save the files in the workdir to this computer; raise ``not_saved`` on any failure.

        Returns ``{"saved": {export_id, path, bytes, files} | None, "files": int | None}``:
        ``saved`` is None when the work folder held no files (nothing needed saving) and
        ``files`` is None when the file service could not count them. The file service's
        ``save_before_delete`` is used when it exists; otherwise the verified export.
        """
        workdir = self._workdir_of(rec)
        if workdir is None:
            # Neither a recorded folder nor a template: saving "/work" would be a guess, and
            # the real files could be elsewhere. Nothing is deleted.
            from .files import unknown_workdir_message
            raise ApiError(409, NOT_SAVED, unknown_workdir_message(rec.name, rec.template_id))
        try:
            files = self.services.files
            before_delete = getattr(files, "save_before_delete", None)
            if callable(before_delete):
                return self._outcome_of_save(before_delete(rec.id, reason=export_reason))
            verified = getattr(files, "export_verified", None)
            if callable(verified):
                record, count = verified(rec.id, reason=export_reason)
                count = int(count)
                if count == 0:
                    return {"saved": None, "files": 0}
                return {"saved": self._saved_entry(record, count), "files": count}
            return {"saved": self._saved_entry(files.export(rec.id, reason=export_reason), None),
                    "files": None}
        except Exception as exc:  # noqa: BLE001 - ANY failure blocks the delete
            detail = exc.message if isinstance(exc, ApiError) else f"{type(exc).__name__}: {exc}"
            detail = _short(str(detail).rstrip(". "), 300) or "unknown error"
            raise ApiError(409, NOT_SAVED,
                           f"The files in {workdir} could not be saved to this computer, so "
                           f"nothing was deleted: {detail}. Delete without saving to remove "
                           "it anyway.") from None

    @staticmethod
    def _saved_entry(record: Any, files: int | None) -> dict:
        """``{export_id, path, bytes, files}`` from an ExportRecord dict; ValueError if unusable."""
        data = record.to_dict() if hasattr(record, "to_dict") else record
        if (not isinstance(data, dict) or not data.get("id") or not data.get("local_path")):
            raise ValueError("the saved copy has no record")
        counted = files if files is not None else int(data.get("files") or 0)
        entry = {"export_id": str(data["id"]), "path": str(data["local_path"]),
                 "bytes": int(data.get("bytes") or 0), "files": int(counted)}
        if data.get("note"):                   # said in the delete message (files.py)
            entry["note"] = str(data["note"])
        return entry

    def _outcome_of_save(self, result: Any) -> dict:
        """Read the file service's ``save_before_delete`` result (see the file service's contract)."""
        if not isinstance(result, dict):
            raise ValueError("the file service returned no result")
        if result.get("saved"):
            files = int(result.get("files") or 0)
            entry = self._saved_entry(result.get("export"), files)
            if result.get("bytes"):
                entry["bytes"] = int(result["bytes"])
            return {"saved": entry, "files": entry["files"]}
        if int(result.get("files") or 0) != 0:
            raise ValueError("the file service reported files but no saved copy")
        return {"saved": None, "files": 0}

    def _notify(self, rec: InstanceRecord, title: str, body: str) -> None:
        try:
            self.services.events.emit("notify", {"level": "info", "title": title, "body": body,
                                                 "ref": {"kind": "instance", "id": rec.id}})
        except Exception:  # noqa: BLE001 - events never break lifecycle calls
            log.debug("cloud-compute: could not emit notify", exc_info=True)

    def _check_status(self, instance_id: str, only: tuple[str, ...] | None) -> None:
        """With ``only``, re-read the record under the manager lock; refuse any other status."""
        if only is None:
            return
        with self._lock:
            fresh = self.store.get_instance(instance_id)
        status = fresh.status if fresh is not None else "gone"
        if status not in only:
            name = fresh.name if fresh is not None else instance_id
            raise ApiError(409, "conflict", f"{name} changed meanwhile (it is {status} now), "
                           "so it was not deleted.")

    def destroy(self, instance_id: str, *, reason: str = "destroyed", save: bool = True,
                export_reason: str = "before-delete",
                only_if_status: Iterable[str] | None = None,
                report: dict | None = None, notify: bool = True,
                with_machine: bool = False) -> InstanceRecord:
        """Delete the container and close the record.

        ``with_machine`` (only with ``save=False``) is for an EC2 machine that was just
        terminated: the container went with the disk, so nothing is run and the record is
        closed with that note.

        With ``save`` (the default) the files in the template's workdir are saved to this
        computer first and the archive is verified. If that fails for any reason,
        ``ApiError(409, 'not_saved')`` is raised and the container and the record are left
        untouched. A verified archive with no files (an empty workdir) proceeds, with the
        notice "Nothing in <workdir> needed saving.". Files outside the workdir are never
        saved. ``save=False`` deletes without saving. When the host record is gone nothing is
        run remotely: the record is closed locally, unsaved.

        The environment's lock is held from the save until the container is removed, so a
        Start or an upload cannot land in between. With ``only_if_status`` the stored status
        is re-checked right before the save and again right before the removal; any other
        status raises ``conflict`` and nothing is deleted. A ``report`` dict receives
        ``saved_files`` (None when nothing was counted), ``workdir``, ``saved`` (``{export_id,
        path, bytes, files}`` or None) and ``message`` (the sentence that says what was saved
        and where). The same sentence goes into the "was deleted" notification unless
        ``notify`` is false (the caller then tells the user itself).
        """
        if not isinstance(save, bool):
            raise ApiError(400, "invalid", "save must be true or false")
        if with_machine and save:
            raise ApiError(400, "invalid", "with_machine is only for a delete without saving")
        only = tuple(only_if_status) if only_if_status is not None else None
        with self.instance_lock(instance_id):
            return self._destroy_locked(instance_id, reason=reason, save=save,
                                        export_reason=export_reason, only=only, report=report,
                                        notify=notify, with_machine=with_machine)

    def _destroy_locked(self, instance_id: str, *, reason: str, save: bool, export_reason: str,
                        only: tuple[str, ...] | None, report: dict | None,
                        notify: bool = True, with_machine: bool = False) -> InstanceRecord:
        rec = self._record(instance_id)
        if rec.status == "destroyed":
            return rec
        self._check_status(rec.id, only)
        workdir = self._workdir_of(rec)
        outcome: dict = {"saved": None, "files": None}
        warning: str | None = None
        saved_here = False
        left_behind = False
        host = self.store.get_host(rec.host_id)
        if host is None:
            # The host record was deleted: nothing can be run there any more. Destroy the
            # record locally so it does not linger (and 404) forever.
            warning = (f"host {rec.host_id} no longer exists; the container "
                       f"{container_name(rec.id)} may still be present on it and must be "
                       f"removed manually")
            log.warning("cloud-compute: %s: %s", rec.id, warning)
        elif with_machine:
            # The machine itself was terminated: its disk and every container went with it.
            warning = WITH_MACHINE_NOTE
        elif _no_machine_running(host):
            # An EC2 host without a running machine: no command can reach the container.
            if save:
                raise ApiError(409, NOT_SAVED, saving_needs_machine(rec.name, host))
            self._check_status(rec.id, only)
            warning, left_behind = _left_with_machine_note(host)
        else:
            if save:
                outcome = self._save_files(rec, export_reason)
                saved_here = True
            self._check_status(rec.id, only)
            res = self._lifecycle(rec, "rm")
            if res.rc != 0 and not _NO_SUCH.search(res.stderr or ""):
                if host.kind == "ec2" and not save and res.rc == 255:
                    # Deleting without saving was the user's choice and the machine does not
                    # answer (for example it is about to stop): the container is removed
                    # the next time the machine is reached.
                    warning, left_behind = _left_with_machine_note(host)
                else:
                    raise self._fail_cmd(f"{rec.runtime} rm", res)

        def mark_destroyed(r: InstanceRecord) -> None:
            r.status, r.ports, r.stats = "destroyed", {}, None
            if warning:
                r.error = warning

        if left_behind:             # the reaper removes it once the machine answers again
            self._set_leftover(rec.id)

        rec = self._update(rec.id, mark_destroyed) or rec
        self._forget_vars(rec.id)
        self._forget_display_token(rec.id)
        self._close_tunnels(rec.id)
        self._clear_stopped_at(rec.id)
        try:
            kv_delete = getattr(self.store, "kv_delete", None)
            if kv_delete is not None:
                kv_delete(WORKDIR_KEY_PREFIX + rec.id)
        except Exception:  # noqa: BLE001
            pass
        self._emit([rec.id], rec.host_id, reason)
        saved_files, saved = outcome["files"], outcome["saved"]
        message = delete_message(rec.name, workdir or "its work folder", saved,
                                 nothing_to_save=saved_here and saved is None and saved_files == 0,
                                 unsaved=saved is None and not (saved_here and saved_files == 0))
        if with_machine:
            message = f"Deleted {rec.name} with its machine; its files were not saved."
        elif warning and host is not None and host.kind == "ec2":
            message += " " + warning
        if report is not None:
            report.update(saved_files=saved_files, workdir=workdir or "", saved=saved,
                          message=message)
        if notify:
            self._notify(rec, f"{rec.name} was deleted", message)
        return rec

    # -- orphans ----------------------------------------------------------------

    def adopt_container(self, host_id: str, container: dict) -> InstanceRecord | None:
        """List a managed container of this install that has no live record.

        Never deletes anything. The record gets ``stop_reason='orphan'``, its name from the
        ``io.cloud-compute.name`` label or the container name, the template from its label
        and the status from the runtime. A running one gets a fresh time limit. Returns
        None when it cannot be adopted (another install, a name that does not map to an
        id, or a live record exists).
        """
        host = self._host(host_id)
        labels = dict(container.get("labels") or {})
        cname = str(container.get("name") or "")
        if labels.get(LABEL_INSTALL) != self.install_id():
            return None
        if not cname.startswith("cc-"):
            return None
        inst_id = cname[3:]
        try:
            if container_name(inst_id) != cname:
                return None
        except ValueError:
            return None
        template_id = str(labels.get(LABEL_TEMPLATE) or "unknown")
        persistent = False
        try:
            persistent = bool(getattr(self.services.templates.get(template_id),
                                      "persistent", False))
        except Exception:  # noqa: BLE001 - template gone: treat as a normal environment
            persistent = False
        running = str(container.get("state") or "").lower() == "running"
        now = self._now()
        ttl = None if persistent else self._default_ttl()
        idle = self._config_minutes("default_idle_minutes")
        with self._lock:
            old = self.store.get_instance(inst_id)
            if old is not None and old.status != "destroyed":
                return None
            name = str(labels.get(LABEL_NAME) or (old.name if old is not None else "")
                       or cname)[:80]
            rec = InstanceRecord(
                id=inst_id, name=name, host_id=host.id, template_id=template_id,
                runtime=self._runtime_name(host),
                container_id=str(container.get("container_id") or "")[:64] or None,
                status="running" if running else "stopped",
                created_at=old.created_at if old is not None else _iso(now),
                started_at=_iso(now) if running else None,
                expires_at=(_iso(now + timedelta(minutes=ttl))
                            if running and ttl is not None else None),
                last_activity_at=_iso(now), idle_minutes=idle,
                display=bool(labels.get(LABEL_DISPLAY)),
                display_state="unknown" if labels.get(LABEL_DISPLAY) else "unsupported",
                ports={}, stats=None, error=None, session_id=None,
                stop_reason="orphan", expiry_warned_at=None, ttl_minutes=ttl)
            self._save(rec)
        if not running:
            self._mark_stopped_at(inst_id)
        self._emit([inst_id], host.id, "adopted")
        return rec

    def stop_all(self, host_id: str | None = None) -> list[InstanceRecord]:
        stopped: list[InstanceRecord] = []
        for rec in self.store.list_instances(host_id, statuses={"running"}):
            try:
                stopped.append(self.stop(rec.id))
            except ApiError as exc:
                log.warning("cloud-compute: stop %s failed: %s", rec.id, exc.message)
        return stopped

    # ------------------------------------------------------------------ exec / logs

    def exec(self, instance_id: str, command: str | list[str], *, timeout_s: int = 120,
             workdir: str | None = None) -> ExecResult:
        rec = self._record(instance_id)
        if rec.status != "running":
            raise ApiError(409, "conflict", f"instance {instance_id} is {rec.status}")
        if isinstance(command, str):
            if not command.strip():
                raise ApiError(400, "invalid", "command must not be empty")
            cmd = ["sh", "-lc", command]
        elif isinstance(command, (list, tuple)) and command and all(isinstance(c, str) for c in command):
            cmd = list(command)
        else:
            raise ApiError(400, "invalid", "command must be a string or a list of strings")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise ApiError(400, "invalid", "timeout_s must be a positive number")
        host = self._host(rec.host_id)
        rt = self._rt_for_record(rec)
        try:
            argv = rt.exec_argv(container_name(rec.id), cmd,
                                workdir=workdir or self.exec_workdir(rec.id))
        except ValueError as exc:
            raise ApiError(400, "invalid", str(exc)) from None
        try:     # before the command: if its output could not be masked, do not run it
            values, names = self._redaction(rec)
        except ApiError as exc:
            if exc.status == 503:
                raise ApiError(503, "unavailable", exc.message + " The command was not run.") from None
            raise
        self._touch(rec)   # a long command must not look idle to the reaper meanwhile
        res = self._transport(host).run(argv, timeout=float(timeout_s))
        out = ExecResult(rc=res.rc,
                         stdout=_tail_bytes(self._redact_output(res.stdout, values, names)),
                         stderr=_tail_bytes(self._redact_output(res.stderr, values, names)),
                         duration_s=res.duration_s, timed_out=bool(res.timed_out))
        self._touch(rec)
        return out

    def logs(self, instance_id: str, tail: int = 200) -> str:
        rec = self._record(instance_id)
        if rec.status in ("destroyed", "creating"):
            raise ApiError(409, "conflict", f"instance {instance_id} is {rec.status}")
        if isinstance(tail, bool) or not isinstance(tail, int) or not 1 <= tail <= 10000:
            raise ApiError(400, "invalid", "tail must be an integer from 1 to 10000")
        host = self._host(rec.host_id)
        rt = self._rt_for_record(rec)
        values, names = self._redaction(rec, "Logs")
        res = self._transport(host).run(rt.logs_argv(container_name(rec.id), tail),
                                        timeout=_CMD_TIMEOUT)
        if res.rc != 0:
            detail = res.stderr or res.stdout or f"rc={res.rc}"
            if res.timed_out:
                raise ApiError(504, "internal", f"{rec.runtime} logs timed out")
            raise ApiError(502, "internal", f"{rec.runtime} logs failed: "
                           f"{_short(self._redact_output(detail, values, names), 300)}")
        text = res.stdout or ""
        if res.stderr:
            text = f"{text}\n{res.stderr}" if text else res.stderr
        return _tail_bytes(self._redact_output(text, values, names), 4 * OUTPUT_CAP_BYTES)

    def touch(self, instance_id: str) -> None:
        self._touch(self._record(instance_id))

    def _touch(self, rec: InstanceRecord) -> None:
        # Only last_activity_at: `rec` may be stale, never write its status back.
        now = self._now_iso()
        self._update(rec.id, lambda r: setattr(r, "last_activity_at", now))

    # ------------------------------------------------------------------ stats

    def refresh_stats(self, host_id: str) -> dict[str, InstanceStats]:
        host = self._host(host_id)
        recs = self.store.list_instances(host_id, statuses={"running"})
        if not recs:
            return {}
        rt = runtime_for(self._runtime_name(host))
        names = [container_name(r.id) for r in recs]
        res = self._transport(host).run(rt.stats_argv(names), timeout=30)
        if res.rc != 0 and not (res.stdout or "").strip():
            raise self._fail_cmd(f"{rt.name} stats", res)
        parsed = rt.parse_stats(res.stdout)
        now = self._now_iso()
        out: dict[str, InstanceStats] = {}
        for rec in recs:
            st = parsed.get(container_name(rec.id))
            if st is None and rec.container_id:
                st = parsed.get(rec.container_id)
                if st is None:
                    st = next((v for k, v in parsed.items()
                               if k.startswith(rec.container_id) or rec.container_id.startswith(k)),
                              None)
            if st is None:
                continue
            st = InstanceStats(cpu_pct=st.cpu_pct, mem_used_mb=st.mem_used_mb,
                               mem_limit_mb=st.mem_limit_mb, sampled_at=now)
            if self._update(rec.id, lambda r, st=st: setattr(r, "stats", st),
                            expect=("running",)) is not None:
                out[rec.id] = st
        self._emit(list(out), host_id, "stats")
        return out

    # ------------------------------------------------------------------ env file sweep

    def sweep_env_files(self, host_id: str, older_than_minutes: int = 15) -> int:
        """Delete leftover env files older than ``older_than_minutes`` on the host.

        One remote ``find`` call; returns how many files were deleted. Env files
        normally live for seconds, so anything older was left by a failed removal.
        """
        if (isinstance(older_than_minutes, bool) or not isinstance(older_than_minutes, int)
                or older_than_minutes < 1):
            raise ApiError(400, "invalid", "older_than_minutes must be a positive integer")
        host = self._host(host_id)
        if _local_native(host):
            return self._sweep_local_env(older_than_minutes)
        res = self._transport(host).run(["sh", "-c", _SWEEP_ENV, "sh", str(older_than_minutes)],
                                        timeout=60)
        if res.rc != 0:
            raise self._fail_cmd("env file sweep", res)
        return sum(1 for ln in (res.stdout or "").splitlines() if ln.strip().endswith(".env"))

    def _sweep_local_env(self, older_than_minutes: int) -> int:
        """Python twin of ``_SWEEP_ENV`` for the local Windows computer (no shell, native paths)."""
        d = _local_env_dir(self.store)
        if not d.is_dir():
            return 0
        cutoff = time.time() - older_than_minutes * 60
        deleted = 0
        for f in d.glob("*.env"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    deleted += 1
            except OSError:
                continue
        return deleted

    # ------------------------------------------------------------------ display image

    def build_display_image(self, host_id: str) -> ExecResult:
        host = self._host(host_id)
        path = DISPLAY_CONTAINERFILE
        if not path.is_file():
            raise ApiError(503, "unavailable", "the display image recipe is not installed")
        data = path.read_bytes()
        rt = runtime_for(self._runtime_name(host))
        res = self._transport(host).run(rt.build_argv(DISPLAY_IMAGE_TAG), stdin=data,
                                        timeout=_BUILD_TIMEOUT)
        return ExecResult(rc=res.rc, stdout=_tail_bytes(res.stdout), stderr=_tail_bytes(res.stderr),
                          duration_s=res.duration_s, timed_out=bool(res.timed_out))

    # ------------------------------------------------------------------ containers by label

    def list_containers(self, host_id: str) -> list[dict]:
        """All managed containers on the host (any install), from ``<rt> ps``."""
        host = self._host(host_id)
        rt = runtime_for(self._runtime_name(host))
        res = self._transport(host).run(rt.ps_argv(f"{MANAGED_LABEL}=1"), timeout=60)
        if res.rc != 0:
            raise self._fail_cmd(f"{rt.name} ps", res)
        return [{"name": row["name"], "container_id": row["id"], "state": row["state"],
                 "labels": dict(row["labels"]),
                 # creation time ('' if unknown) so the reaper's orphan grace applies
                 "created_at": row.get("created_at", "")}
                for row in rt.parse_ps(res.stdout)]

    def remove_container(self, host_id: str, name: str) -> ExecResult:
        """Force-remove a managed container, only if it carries this install's id."""
        host = self._host(host_id)
        match = next((c for c in self.list_containers(host_id)
                      if name in (c["name"], c["container_id"])), None)
        if match is None:
            raise ApiError(404, "not_found", f"container {name} not found on {host_id}")
        if match["labels"].get(LABEL_INSTALL) != self.install_id():
            raise ApiError(409, "conflict",
                           f"container {name} belongs to another installation; not removed")
        rt = runtime_for(self._runtime_name(host))
        res = self._transport(host).run(rt.lifecycle_argv("rm", match["name"] or name),
                                        timeout=_CMD_TIMEOUT)
        inst = (match["labels"] or {}).get(LABEL_INSTANCE)
        if res.rc == 0 and inst:
            self._forget_display_token(inst)
        return res


def provide(services: Any) -> SandboxManager:
    """Service provider used by the module registry."""
    return SandboxManager(services)
