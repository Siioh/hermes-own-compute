"""File transfer between environments and this computer (the artifact channel).

``provide(services)`` returns a :class:`FileManager`:

- :meth:`FileManager.export` saves a folder (default: the template's workdir) from an
  environment to ``<exports folder>/<name>-<id>/<UTC time>-<reason>.tar.gz`` (the exports
  folder is the ``exports_dir`` setting, else ``<plugin data>/exports``), or with
  ``dest_dir`` to ``<dest_dir>/<name>-<UTC time>-<reason>.tar.gz``. It uses
  ``<runtime> cp <container>:<path> -``, which also works on a STOPPED container, and
  gzips the tar stream while it arrives (nothing is held in memory). The archive is then
  flushed to disk (fsync) and read back end to end; a copy that cannot be read back
  completely is removed and the export fails. :meth:`FileManager.export_verified` also
  returns how many regular files the archive holds. A manual save or fetch of a folder
  without regular files keeps nothing and raises ``nothing_to_save``.
- :meth:`FileManager.save_before_delete` is the save a Delete runs first: zero files means
  nothing is kept and nothing needed saving; any other failure raises.
- :meth:`FileManager.size` measures a folder inside a running environment (bounded time).
- :meth:`FileManager.list_dir` lists a folder inside a running environment.
- :meth:`FileManager.put` uploads a file or folder from this computer (running or stopped);
  :meth:`FileManager.restore` unpacks a saved export into an environment.
- :meth:`FileManager.list_exports` / :meth:`FileManager.delete_export` manage the saved
  copies. The index lives in the store (generic records of kind ``export``).

Safety rules:

- Local writes happen only inside the exports folder or the chosen ``dest_dir``. A
  user-given upload path is only ever read, never written.
- Tar archives are unpacked on this computer only when ``extract`` is asked for, into a
  NEW folder, with the checks of :func:`_safe_unpack` (and tarfile's data filter). Uploads
  are re-packed as a stream (member names cleaned, devices dropped) and unpacked only
  inside the container by ``cp -``.
- Transfers are capped at the ``export_max_mb`` setting, counted on the raw tar stream.
- Local paths under ``~/.ssh``, the Hermes home and the plugin's data folder (except the
  exports folder) are never uploaded.
- File contents and secrets are never logged.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import posixpath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import unicodedata
import zlib
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Callable

from .config import CONFIG_FIELDS
from .interfaces import ApiError
from .models import EXPORT_REASONS, ExportRecord, new_id, utc_now_iso
from .runtimes import container_name, container_path, runtime_for

__all__ = [
    "FileManager", "provide", "NOT_AVAILABLE", "EXPORT_KIND", "EXPORTS_SUBDIR",
    "LIST_MAX_ENTRIES", "TRANSFER_TIMEOUT_S", "DEFAULT_WORKDIR", "SIZE_TIMEOUT_S",
    "LARGE_SAVE_BYTES", "clean_path_text", "human_size", "file_count",
]

log = logging.getLogger(__name__)

NOT_AVAILABLE = "File transfer is not available yet."     # kept for older imports
EXPORT_KIND = "export"            # store record kind of the export index
EXPORTS_SUBDIR = "exports"
PLUGIN_NAME = "cloud-compute"
DEFAULT_WORKDIR = "/work"
LIST_MAX_ENTRIES = 500
LIST_MAX_BYTES = 256 * 1024       # most listing output read from the container
TRANSFER_TIMEOUT_S = 3600.0
LIST_TIMEOUT_S = 60.0
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_NO_CONTAINER = re.compile(r"no such container|no container with name or id", re.IGNORECASE)
_NO_PATH = re.compile(r"no such file or directory|could not find|does not exist|not found",
                      re.IGNORECASE)
#: Zero bytes that end a tar archive (two 512-byte blocks). Both ``podman cp`` and
#: ``docker cp`` write them; a stream that stops before them was cut short.
_TAR_END = b"\0" * 1024
_TAR_BLOCK = 512
_VERIFY_CHUNK = 1024 * 1024
_NOT_RUNNING = re.compile(r"not running|container state improper|is stopped|is paused",
                          re.IGNORECASE)

# List one folder as NUL-terminated "type<TAB>size<TAB>mtime<TAB>name" records: GNU find
# -printf when available, otherwise a glob loop with stat. rc 3 = missing, 4 = not a folder.
_LIST_SCRIPT = (
    'p=$1; n=$2; '
    'if [ ! -e "$p" ] && [ ! -L "$p" ]; then exit 3; fi; '
    'if [ ! -d "$p" ]; then exit 4; fi; '
    "if find \"$p\" -maxdepth 0 -printf '' >/dev/null 2>&1; then "
    "find \"$p\" -mindepth 1 -maxdepth 1 -printf '%y\\t%s\\t%T@\\t%f\\0' 2>/dev/null "
    f"| head -c {LIST_MAX_BYTES}; "
    'else c=0; for f in "$p"/.* "$p"/*; do b=${f##*/}; '
    'case $b in .|..) continue;; esac; '
    '[ -e "$f" ] || [ -L "$f" ] || continue; '
    'c=$((c+1)); [ "$c" -gt "$n" ] && break; '
    'if [ -L "$f" ]; then t=l; elif [ -d "$f" ]; then t=d; elif [ -f "$f" ]; then t=f; '
    'else t=o; fi; '
    's=$(stat -c %s "$f" 2>/dev/null) || s=0; m=$(stat -c %Y "$f" 2>/dev/null) || m=0; '
    "printf '%s\\t%s\\t%s\\t%s\\0' \"$t\" \"$s\" \"$m\" \"$b\"; done; fi"
)
_TYPES = {"f": "file", "d": "dir", "l": "link"}

SIZE_TIMEOUT_S = 10
LARGE_SAVE_BYTES = 200 * 1024 * 1024      # chat saves above this ask first
# Count the regular files (hidden ones too) below $1 and their bytes: prints "<files> <bytes>".
# rc 3 = missing. GNU find -printf when available, else find -exec stat (busybox).
_SIZE_SCRIPT = (
    'p=$1; if [ ! -e "$p" ] && [ ! -L "$p" ]; then exit 3; fi; '
    "{ if find \"$p\" -maxdepth 0 -printf '' >/dev/null 2>&1; then "
    "find \"$p\" -type f -printf '%s\\n' 2>/dev/null; "
    'else find "$p" -type f -exec stat -c %s {} + 2>/dev/null; fi; } | '
    "if command -v awk >/dev/null 2>&1; then "
    "awk '{n++; b+=$1} END {printf \"%.0f %.0f\\n\", n, b}'; "
    'else n=0; b=0; while read -r s; do n=$((n+1)); b=$((b+s)); done; echo \"$n $b\"; fi'
)
# Runs _SIZE_SCRIPT ($2) on $1 under `timeout` when the container has it (rc 124 = timed out).
_SIZE_WRAP = (f'if command -v timeout >/dev/null 2>&1; then exec timeout {SIZE_TIMEOUT_S} '
              'sh -c "$2" sh "$1"; fi; exec sh -c "$2" sh "$1"')
_QUOTES = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}
_STAMP_RE = r"\d{8}T\d{6}Z"
#: File names this plugin gives saved copies (a delete never removes anything else).
_ARCHIVE_NAME = re.compile(r"^(?:.+-)?" + _STAMP_RE + r"-[a-z-]+(?:-\d+)?\.tar\.gz$")
_DRIVE = re.compile(r"^[A-Za-z]:")


def clean_path_text(value: Any, *, local: bool = False) -> Any:
    """A typed path without surrounding whitespace and one pair of matching quotes.

    With ``local`` a leading ``~`` is expanded to the home folder of this computer. Other
    values (None, numbers) are returned unchanged for the caller's own validation.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) >= 2 and _QUOTES.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    if local and text.startswith("~"):
        text = os.path.expanduser(text)
    return text


def human_size(n: Any) -> str:
    """``3.2 GB``-style size (1024-based)."""
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def file_count(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "an unknown number of files"
    return f"{n:,} file" + ("" if n == 1 else "s")


def _mkdirs_private(path: Path) -> None:
    """Create ``path`` and any missing parents with mode 0700 (POSIX; Windows relies on the
    user-profile permissions). Folders that already exist keep their mode."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            continue


def _open_private(path: Path) -> Any:
    """Create ``path`` exclusively with mode 0600 and return it open for binary writing."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


class _Refused(Exception):
    """An archive member that is never unpacked on this computer."""


def unknown_workdir_message(name: str, template_id: str = "") -> str:
    """Why a Delete refused: the environment's work folder is not recorded and its template is
    gone, so which folder holds the files cannot be told. Nothing was deleted."""
    tpl = f" (template {template_id})" if template_id else ""
    return (f"The work folder of {name} is not recorded and its template{tpl} no longer exists, "
            "so Own Compute cannot tell which folder to save. Nothing was deleted. Save the "
            "folder you need first (env_files fetch with its path), then delete without saving.")


def _bad(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _short(text: str, limit: int = 300) -> str:
    return " ".join((text or "").split())[-limit:]


def _iso_from_epoch(value: str) -> str | None:
    try:
        ts = float(value)
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _case_insensitive_fs() -> bool:
    """True where the default file system ignores letter case (Windows, macOS)."""
    return os.name == "nt" or sys.platform == "darwin"


def _fold_parts(path: PurePath, case_insensitive: bool) -> tuple[str, ...]:
    """The parts of ``path`` as they are compared: exact on a case-sensitive file system;
    Unicode-normalised (NFC) and case-folded on a case-insensitive one (macOS also stores
    names in another normal form than the one typed)."""
    if not case_insensitive:
        return tuple(path.parts)
    return tuple(unicodedata.normalize("NFC", p).casefold() for p in path.parts)


class _Roots:
    """``contains(path)``: is ``path`` one of these folders, or inside one?

    The file system decides where it can: a folder on the way to ``path`` that exists and
    is the same file as a root (same device and inode, what ``os.path.samefile`` tests)
    counts, however the names are spelled. Otherwise (the path, or part of it, does not
    exist yet) the names are compared part by part, case-folded and NFC-normalised on
    Windows and macOS and exactly elsewhere. ``case_insensitive`` overrides that default
    (tests). Answers per folder are remembered, so many paths cost few lookups.
    """

    def __init__(self, roots: Any, *, case_insensitive: bool | None = None) -> None:
        self._ci = _case_insensitive_fs() if case_insensitive is None else case_insensitive
        self._roots = [r for r in roots]
        self._folded = [_fold_parts(r, self._ci) for r in self._roots]
        self._ids: set[tuple[int, int]] = set()
        for root in self._roots:
            ident = self._ident(root)
            if ident is not None:
                self._ids.add(ident)
        self._memo: dict[Any, bool] = {}

    @staticmethod
    def _ident(path: Any) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            return None
        return (st.st_dev, st.st_ino) if st.st_ino else None

    def _is_a_root(self, folder: PurePath) -> bool:
        hit = self._memo.get(folder)
        if hit is None:
            ident = self._ident(folder) if self._ids else None
            hit = ident is not None and ident in self._ids
            self._memo[folder] = hit
        return hit

    def contains(self, path: PurePath) -> bool:
        parts = _fold_parts(path, self._ci)
        for folded in self._folded:
            if parts[:len(folded)] == folded:
                return True
        if not self._ids:
            return False
        return any(self._is_a_root(a) for a in (path, *path.parents))


def _is_within(path: PurePath, root: PurePath, *, case_insensitive: bool | None = None) -> bool:
    """True when ``path`` is ``root`` or inside it (see :class:`_Roots`)."""
    return _Roots([root], case_insensitive=case_insensitive).contains(path)


def _same_place(a: Path, b: Path, *, case_insensitive: bool | None = None) -> bool:
    """True when ``a`` and ``b`` are the same folder or file."""
    ci = _case_insensitive_fs() if case_insensitive is None else case_insensitive
    if _fold_parts(a, ci) == _fold_parts(b, ci):
        return True
    ident = _Roots._ident(a)
    return ident is not None and ident == _Roots._ident(b)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _on_windows() -> bool:
    return os.name == "nt"


def is_special_windows_path(text: Any) -> bool:
    """True for a path written in a form that can name a protected folder in a way that is
    not compared reliably: the extended-length and device-namespace forms, and a network
    share (the admin shares included), all of which start with two backslashes, and the
    two-slash spelling of a share on Windows. A drive-letter path is the normal form."""
    if not isinstance(text, str):
        return False
    if text.startswith("\\\\"):
        return True
    return _on_windows() and text.startswith("//")


def _is_network_final_path(path: Path) -> bool:
    """True when a RESOLVED path is still a network or device form (for example a mapped
    drive that points at an admin share): it cannot be compared with the private folders."""
    return is_special_windows_path(str(path))


_SPECIAL_PATH_START = (
    "Paths that start with two backslashes or two slashes (a network share such as "
    "\\\\server\\share, or a Windows device path such as \\\\?\\C:\\) are not accepted here, "
    "because they can point into a private folder (the SSH folder or the Hermes home) in a way "
    "that cannot be checked. ")
#: The refusal on Windows (kept under its old name); see :func:`special_path_message`.
SPECIAL_PATH_MESSAGE = (_SPECIAL_PATH_START + "Use the normal drive-letter path instead, for "
                        "example C:\\Users\\name\\project.")


def special_path_message() -> str:
    """The refusal for a two-backslash path, with an example path of the system the plugin
    runs on (a drive-letter path only on Windows)."""
    if _on_windows():
        return SPECIAL_PATH_MESSAGE
    example = "/Users/name/project" if sys.platform == "darwin" else "/home/name/project"
    return _SPECIAL_PATH_START + f"Use an ordinary folder path instead, for example {example}."


def _member_name(name: str) -> str | None:
    """A tar member name cleaned to a relative path, or ``None`` if it is empty."""
    name = (name or "").replace("\\", "/")
    if "\x00" in name:
        return None
    norm = posixpath.normpath("/" + name.lstrip("/")).lstrip("/")
    if not norm or norm == ".":
        return None
    return norm


def _archive_mode(path: Path) -> str | None:
    lower = path.name.lower()
    if lower.endswith((".tar.gz", ".tgz")):
        return "r|gz"
    if lower.endswith(".tar"):
        return "r|"
    return None


def _refuse_name(raw: str) -> str | None:
    """Why a raw member name is never unpacked here, or None when it is acceptable."""
    if "\x00" in raw:
        return "a name with a NUL character"
    if raw.startswith(("/", "\\")) or _DRIVE.match(raw):
        return f"the absolute name {raw!r}"
    parts = re.split(r"[\\/]", raw)
    if ".." in parts:
        return f"the name {raw!r}, which climbs out with '..'"
    if os.name == "nt" and any(":" in part for part in parts):
        return f"the name {raw!r}, which is not allowed on this computer"
    return None


def _safe_unpack(archive: Path, target: Path, cap_bytes: int) -> None:
    """Unpack a saved ``.tar.gz`` into the empty folder ``target``; raise :class:`_Refused`.

    Own checks on every member (tarfile's ``data_filter`` runs too where this Python has
    it): no absolute names, no ``..``, no links resolving outside ``target``, hard links
    only to files already unpacked from this archive, no devices or fifos, never an
    existing file overwritten, at most ``cap_bytes`` in total. The saved folder itself
    (the first member, a folder) is not re-created: its contents land in ``target``.
    The caller removes ``target`` on a refusal.
    """
    root = os.path.realpath(target)
    data_filter = getattr(tarfile, "data_filter", None)
    written: dict[str, str] = {}          # member name -> path of an unpacked regular file
    total = 0
    top: str | None = None
    first = True

    def inside(path: str) -> bool:
        real = os.path.realpath(path)
        return real == root or real.startswith(root.rstrip(os.sep) + os.sep)

    try:
        with tarfile.open(archive, mode="r|gz") as tf:
            for member in tf:
                why = _refuse_name(member.name)
                if why:
                    raise _Refused(f"the archive holds {why}.")
                name = posixpath.normpath(member.name.replace("\\", "/")).lstrip("/")
                if first:
                    first = False
                    if member.isdir() and "/" not in name and name not in ("", "."):
                        top = name
                        continue
                if top is not None:
                    if name == top:
                        continue
                    if name.startswith(top + "/"):
                        name = name[len(top) + 1:]
                if name in ("", "."):
                    continue
                if not (member.isreg() or member.isdir() or member.issym()
                        or member.islnk()):
                    raise _Refused(f"the archive holds {name!r}, a device or other special "
                                   "file.")
                dest = os.path.join(root, *name.split("/"))
                parent = os.path.dirname(dest)
                if not inside(dest) or not inside(parent):
                    raise _Refused(f"{name!r} would land outside the new folder.")
                link = None
                if member.issym():
                    why = _refuse_name(member.linkname)
                    if why or not inside(os.path.join(os.path.realpath(parent),
                                                      member.linkname)):
                        raise _Refused(f"the link {name!r} points outside the new folder "
                                       f"({member.linkname!r}).")
                    link = member.linkname
                elif member.islnk():
                    why = _refuse_name(member.linkname)
                    lname = posixpath.normpath(member.linkname.replace("\\", "/"))
                    if top is not None and lname.startswith(top + "/"):
                        lname = lname[len(top) + 1:]
                    if why or lname not in written:
                        raise _Refused(f"the hard link {name!r} points to "
                                       f"{member.linkname!r}, which is not a file unpacked "
                                       "from this archive.")
                    link = lname
                if data_filter is not None:
                    try:
                        check = member.replace(name=name, deep=False)
                        if member.islnk():
                            check = check.replace(linkname=link, deep=False)
                        data_filter(check, root)
                    except tarfile.FilterError as exc:
                        raise _Refused(f"{name!r} was refused ({exc}).") from None
                if os.path.lexists(dest) and not (member.isdir() and os.path.isdir(dest)
                                                  and not os.path.islink(dest)):
                    raise _Refused(f"{name!r} would overwrite something already unpacked.")
                os.makedirs(parent, exist_ok=True)
                if not inside(parent):
                    raise _Refused(f"{name!r} would land outside the new folder.")
                if member.isdir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                if member.issym():
                    try:
                        os.symlink(link, dest)
                    except (OSError, NotImplementedError):
                        log.info("cloud-compute: a symbolic link was not created (not "
                                 "permitted on this computer)")
                    continue
                if member.islnk():
                    size = os.path.getsize(written[str(link)])
                else:
                    size = max(int(member.size), 0)
                total += size
                if total > cap_bytes:
                    raise _Refused(f"the unpacked files would be larger than the export limit "
                                   f"({cap_bytes // (1024 * 1024)} MB).")
                with open(dest, "xb") as out:
                    if member.islnk():
                        with open(written[str(link)], "rb") as src:
                            shutil.copyfileobj(src, out, _VERIFY_CHUNK)
                    else:
                        fileobj = tf.extractfile(member)
                        if fileobj is not None:
                            shutil.copyfileobj(fileobj, out, _VERIFY_CHUNK)
                if os.name != "nt" and member.isreg():
                    try:
                        os.chmod(dest, member.mode & 0o755 | 0o600)
                    except OSError:
                        pass
                written[name] = dest
    except _Refused:
        raise
    except FileExistsError as exc:
        raise _Refused(f"{exc.filename or 'a file'} would overwrite something already "
                       "unpacked.") from None
    except (tarfile.TarError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise _Refused(f"the archive could not be read ({_short(str(exc), 200)}).") from None
    except OSError as exc:
        raise _Refused(f"a file could not be written "
                       f"({exc.strerror or type(exc).__name__}).") from None


class _NoPadTar(tarfile.TarFile):
    """A TarFile that ends right after the end-of-archive marker.

    ``tarfile`` pads the stream to a multiple of 10240 bytes. ``podman cp -`` stops reading
    at the marker; padding that arrives in a later read finds the pipe closed and Podman
    exits 125 with ``io: read/write on closed pipe`` although every file was unpacked.
    """

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.mode in ("a", "w", "x"):
                self.fileobj.write(tarfile.NUL * (tarfile.BLOCKSIZE * 2))
                self.offset += tarfile.BLOCKSIZE * 2
        finally:
            if not self._extfileobj:
                self.fileobj.close()


class _HashingReader:
    """Reads a member's data and keeps its size and SHA-256 for the check after a copy."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.size = 0
        self.digest = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        data = self._raw.read(n)
        self.size += len(data)
        self.digest.update(data)
        return data


#: Templates whose sandboxes are cloud agents (built in; a user template counts when its
#: ``agent`` block names a mode, see :meth:`FileManager._internal_dirs`).
AGENT_TEMPLATE_IDS = ("hermes-agent", "agent-script")
#: Folders of a cloud-agent sandbox that no saved copy contains: the agent's Hermes home and
#: run state, which can hold the model key. The first two are where earlier versions kept them
#: (below the work folder, ``{work}`` here); the last is where they live now.
AGENT_INTERNAL_DIRS = ("{work}/.hermes", "{work}/.cc-agent", "/home/agent/.cc-agent")


def left_out_note(names: list[str]) -> str:
    """Plain-words sentence for a saved copy that left agent-internal folders out."""
    listed = ", ".join(names)
    return (f"Left out of the saved copy: {listed}. They are internal folders of the cloud "
            "agent and can hold the model key, so they are never saved. Only your files "
            "were saved.")


class _TarScrubber:
    """Copies a tar stream into ``out`` without the members below ``excluded`` paths.

    The container's copy command (``cp CID:PATH -``) cannot exclude anything, so the stream
    is filtered here while it arrives: ``write`` is the sink (called from the transport's
    reader), a worker thread reads the members and writes the kept ones to ``out`` as a new
    tar stream. Nothing is held in memory beyond a few chunks and nothing is written to disk
    but the final archive. ``skipped`` collects the excluded folders that were found.
    """

    _QUEUE = 16

    def __init__(self, out: Any, excluded: tuple[str, ...], src: str) -> None:
        import queue
        import threading
        self._out = out
        self._excluded = excluded
        self._parent = posixpath.dirname(src.rstrip("/")) or "/"
        self._q: Any = queue.Queue(self._QUEUE)
        self._buf = b""
        self._eof = False
        self._aborted = False
        self.error: str | None = None
        self.skipped: list[str] = []
        self.raw_total = 0
        self.raw_tail = b""
        self._thread = threading.Thread(target=self._run, name="cc-tar-scrub", daemon=True)
        self._thread.start()

    # -- producer side
    def write(self, chunk: bytes) -> int:
        import queue
        self.raw_total += len(chunk)
        self.raw_tail = (self.raw_tail + chunk)[-len(_TAR_END):]
        while True:
            if not self._thread.is_alive():
                return len(chunk)           # the worker stopped (error is recorded); drop
            try:
                self._q.put(bytes(chunk), timeout=0.2)
                return len(chunk)
            except queue.Full:
                continue

    def finish(self, *, complete: bool) -> None:
        """End of the stream. ``complete`` False: the transfer failed; errors are ignored."""
        import queue
        self._aborted = not complete
        while self._thread.is_alive():
            try:
                self._q.put(None, timeout=0.2)
                break
            except queue.Full:
                continue
        self._thread.join(timeout=60)
        if complete and self.error is None:
            if self.raw_total == 0 or self.raw_total % _TAR_BLOCK or self.raw_tail != _TAR_END:
                self.error = "the tar stream ends before its end marker"

    # -- consumer side (worker thread)
    def read(self, n: int = -1) -> bytes:
        while not self._buf and not self._eof:
            item = self._q.get()
            if item is None:
                self._eof = True
            else:
                self._buf = item
        if n is None or n < 0 or n >= len(self._buf):
            data, self._buf = self._buf, b""
        else:
            data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _is_excluded(self, name: str) -> str | None:
        path = posixpath.normpath(posixpath.join(self._parent, name.lstrip("/")))
        for folder in self._excluded:
            if path == folder or path.startswith(folder + "/"):
                return folder
        return None

    def _run(self) -> None:
        try:
            with tarfile.open(fileobj=self, mode="r|") as tin, \
                    tarfile.open(fileobj=self._out, mode="w|",
                                 format=tarfile.PAX_FORMAT) as tout:
                for member in tin:
                    folder = self._is_excluded(member.name)
                    if folder is not None:
                        if folder not in self.skipped:
                            self.skipped.append(folder)
                        continue
                    tout.addfile(member, tin.extractfile(member) if member.isreg() else None)
            while not self._eof:             # the rest (padding) of the stream
                self.read(_VERIFY_CHUNK)
        except Exception as exc:  # noqa: BLE001 - reported by finish()/the caller
            if not self._aborted:
                self.error = _short(str(exc) or type(exc).__name__, 200)


class FileManager:
    """Copies files between environments and this computer (see the module docstring)."""

    def __init__(self, services: Any = None, *, exports_dir: str | Path | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.services = services
        self._exports_dir = Path(exports_dir) if exports_dir is not None else None
        self._clock = clock or time.time
        self._tightened = False

    # ------------------------------------------------------------------ paths

    def _data_dir(self) -> Path:
        """The plugin's data folder: beside the database, else the public Hermes helpers."""
        try:
            store_path = getattr(self.services.store, "path", None)
        except Exception:  # noqa: BLE001 - no store service
            store_path = None
        if store_path:
            return Path(store_path).parent
        try:
            from plugins.plugin_storage import plugin_data_dir  # type: ignore[import-not-found]
            return Path(plugin_data_dir(PLUGIN_NAME))
        except Exception:  # noqa: BLE001 - running outside Hermes
            pass
        try:
            from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
            return Path(get_hermes_home()) / "plugin-data" / PLUGIN_NAME
        except Exception:  # noqa: BLE001
            pass
        raise ApiError(503, "unavailable", "Plugin storage is not available.")

    def _default_exports_dir(self) -> Path:
        """The plugin's own exports folder (inside its data folder)."""
        return _resolve(self._data_dir() / EXPORTS_SUBDIR)

    def _config(self) -> Any:
        try:
            cfg = self.services.config
        except Exception:  # noqa: BLE001 - defaults
            return None
        reload = getattr(cfg, "reload", None)
        if callable(reload):        # a value saved in Settings a moment ago applies now
            try:
                reload()
            except Exception:  # noqa: BLE001 - keep the last good values
                pass
        return cfg

    def _configured_exports_dir(self) -> str:
        value = getattr(self._config(), "exports_dir", "")
        value = clean_path_text(value, local=True) if isinstance(value, str) else ""
        return value or ""

    def exports_dir(self, *, create: bool = False) -> Path:
        """Absolute folder that holds the saved copies: the ``exports_dir`` setting when it
        is set (validated at every call), else the plugin's own exports folder.

        Raises ``ApiError`` (400 ``invalid``) when the setting names an unusable folder.
        """
        self._tighten_once()
        if self._exports_dir is not None:
            return _resolve(self._exports_dir)
        configured = self._configured_exports_dir()
        if not configured:
            return self._default_exports_dir()
        try:
            return self._check_folder(configured, "The folder", create=create,
                                      must_exist=False)
        except ApiError as exc:
            raise ApiError(exc.status, "invalid",
                           f'The "Exports dir" setting cannot be used: {exc.message} Change it '
                           "in Settings, or clear it to use the plugin's own folder.") from None

    # -- permissions of copies saved by an older build ------------------------------------

    def _tighten_once(self) -> None:
        if self._tightened:
            return
        self._tightened = True
        try:
            self.tighten_saved_copies()
        except Exception:  # noqa: BLE001 - never stop a save or a listing over this
            log.debug("cloud-compute: could not tighten saved-copy permissions", exc_info=True)

    def tighten_saved_copies(self) -> int:
        """Owner-only permissions for what an older build saved (POSIX; Windows is unchanged).

        The plugin's own exports folder, the per-environment folders in it and the folders
        unpacked there become 0700; archives become 0600. For a folder the user chose
        (``exports_dir`` setting, ``dest_dir``) only what the plugin created there changes: the
        archives it recorded, their per-environment folder, an unpacked folder. Files inside an
        unpacked folder keep their own modes. Only entries owned by the current user are
        touched. Returns how many entries were changed.
        """
        getuid = getattr(os, "getuid", None)
        if os.name != "posix" or getuid is None:
            return 0
        me = getuid()
        changed = 0

        def tighten(path: Path, mode: int, kind: str) -> None:
            nonlocal changed
            try:
                st = path.lstat()
                is_kind = stat.S_ISDIR(st.st_mode) if kind == "dir" else stat.S_ISREG(st.st_mode)
                if is_kind and st.st_uid == me and st.st_mode & 0o077:
                    path.chmod(mode)
                    changed += 1
            except OSError:
                pass

        def children(folder: Path) -> list[Path]:
            try:
                return sorted(folder.iterdir())
            except OSError:
                return []

        try:
            own = self._default_exports_dir()
        except ApiError:
            own = None
        if own is not None and own.is_dir() and not own.is_symlink():
            tighten(own, 0o700, "dir")
            for env_folder in children(own):
                if env_folder.is_symlink() or not env_folder.is_dir():
                    continue
                tighten(env_folder, 0o700, "dir")
                for item in children(env_folder):
                    if item.is_symlink():
                        continue
                    if item.is_dir():
                        tighten(item, 0o700, "dir")          # an unpacked folder: its top only
                    elif item.name.endswith((".tar.gz", ".tgz", ".tar")):
                        tighten(item, 0o600, "file")
        root: Path | None
        try:
            configured = self._configured_exports_dir()
            root = _resolve(Path(configured)) if configured else None
        except (ApiError, OSError):
            root = None
        try:
            records = self.list_exports()
        except Exception:  # noqa: BLE001 - no store: nothing recorded
            records = []
        for record in records:
            archive = Path(str(record.get("local_path") or ""))
            if not archive.is_absolute() or not archive.name.endswith(".tar.gz"):
                continue
            tighten(archive, 0o600, "file")
            folder = archive.parent
            iid = str(record.get("instance_id") or "")
            if root is not None and iid and folder.parent == root and folder.name.endswith("-" + iid):
                tighten(folder, 0o700, "dir")                 # the plugin's per-environment folder
            unpacked = record.get("extracted_to")
            if unpacked and Path(str(unpacked)).parent == folder:
                tighten(Path(str(unpacked)), 0o700, "dir")
        return changed

    def _check_folder(self, value: Any, what: str, *, create: bool = False,
                      must_exist: bool = True) -> Path:
        """Validate a folder on this computer that saved copies are written to.

        Absolute; not inside the SSH folder, the Hermes home or the plugin's data (the
        plugin's own exports folder excepted); a folder that exists (or, when not
        ``must_exist``, can be created: it is created when ``create``).
        """
        text = clean_path_text(value, local=True)
        if not isinstance(text, str) or not text or "\x00" in text:
            raise _bad(f"{what} must be an absolute path to a folder on this computer.")
        if is_special_windows_path(text):
            raise ApiError(403, "invalid", special_path_message())
        folder = Path(text)
        if not folder.is_absolute():
            raise _bad(f"{what} must be an absolute path to a folder on this computer, not "
                       f"{text!r}.")
        resolved = _resolve(folder)
        if _is_network_final_path(resolved):
            raise ApiError(403, "invalid", special_path_message())
        if not self._writable_place(resolved):
            raise ApiError(403, "invalid",
                           f"{text} is inside the Hermes home or the SSH folder, where saved "
                           "copies are never written. Choose another folder, for example the "
                           "project folder.")
        if resolved.exists() or folder.exists():
            if not resolved.is_dir():
                raise _bad(f"{text} is not a folder.")
            return resolved
        if must_exist:
            raise ApiError(404, "not_found", f"{text} does not exist on this computer. Create "
                           "the folder first, or choose an existing one.")
        if create:
            try:
                _mkdirs_private(resolved)
            except OSError as exc:
                raise _bad(f"{text} could not be created: "
                           f"{exc.strerror or type(exc).__name__}.") from None
        return resolved

    def _writable_place(self, path: Path) -> bool:
        """True outside ~/.ssh, the Hermes home and the plugin data (its exports allowed)."""
        try:
            own = self._default_exports_dir()
        except ApiError:
            own = None
        if own is not None and _is_within(path, own):
            return True
        if _is_network_final_path(path):
            return False
        return not _Roots(self._forbidden_roots()).contains(path)

    def _forbidden_roots(self) -> list[Path]:
        roots = [Path.home() / ".ssh"]
        try:
            roots.append(self._data_dir())
        except ApiError:
            pass
        home: Path | None = None
        try:
            from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
            home = Path(get_hermes_home())
            roots.append(home)
        except Exception:  # noqa: BLE001 - running outside Hermes
            # Fallback: the default home-dir Hermes folder. The name is assembled
            # so no literal dot-hermes path appears in source (profile_path rule).
            roots.append(Path.home() / ("." + "hermes"))
        # The whole Hermes root, not only the active profile: its .env, auth.json, config,
        # skills, plugins and every other profile live there. Asked of Hermes when it offers
        # the answer, else read from the <root>/profiles/<name> layout of the active profile.
        try:
            from hermes_constants import get_default_hermes_root  # type: ignore[import-not-found]
            roots.append(Path(get_default_hermes_root()))
        except Exception:  # noqa: BLE001 - an older Hermes, or none
            pass
        if home is not None and home.parent.name == "profiles":
            roots.append(home.parent.parent)
        return [_resolve(r) for r in roots]

    def _privacy_rule(self) -> Callable[[Path], bool]:
        """``allowed(path)``: False inside ~/.ssh, the Hermes home or the plugin data.

        The exports folders (the plugin's own and the configured one) are allowed. The
        roots are resolved once per call of this method.
        """
        exports: list[Path] = []
        for getter in (self._default_exports_dir, self.exports_dir):
            try:
                exports.append(getter())
            except ApiError:
                pass
        inside_exports = _Roots(exports)
        private = _Roots(self._forbidden_roots())

        def allowed(path: Path) -> bool:
            path = _resolve(path)
            if inside_exports.contains(path):
                return True
            if _is_network_final_path(path):
                return False
            return not private.contains(path)

        return allowed

    def _local_allowed(self, path: Path) -> bool:
        return self._privacy_rule()(path)

    # ------------------------------------------------------------------ services

    @property
    def store(self) -> Any:
        return self.services.store

    def _record(self, instance_id: Any) -> Any:
        rec = self.store.get_instance(instance_id) if isinstance(instance_id, str) else None
        if rec is None:
            raise ApiError(404, "not_found", f"Environment {instance_id} was not found.")
        return rec

    @staticmethod
    def _usable(rec: Any) -> None:
        if rec.status == "destroyed":
            raise ApiError(409, "conflict", f"{rec.name} was deleted; the files in it are gone.")
        if rec.status == "creating":
            raise ApiError(409, "conflict", f"{rec.name} is still starting. Try again shortly.")

    def _transport(self, rec: Any) -> Any:
        host = self.store.get_host(rec.host_id)
        if host is None:
            raise ApiError(404, "not_found", f"Host {rec.host_id} of {rec.name} no longer exists.")
        return self.services.transports.for_host(host)

    def _instance_lock(self, instance_id: str) -> Any:
        """The sandbox manager's per-environment lock (held by Delete across save and
        removal), so an upload never lands between the save and the removal."""
        try:
            lock = getattr(self.services.sandboxes, "instance_lock", None)
        except Exception:  # noqa: BLE001 - no sandbox service (tests): nothing to order
            lock = None
        return lock(instance_id) if callable(lock) else nullcontext()

    @staticmethod
    def _stream_fn(transport: Any) -> Callable[..., Any]:
        fn = getattr(transport, "run_stream", None)
        if not callable(fn):
            raise ApiError(503, "unavailable", "This host's connection cannot transfer files.")
        return fn

    @staticmethod
    def _rt(rec: Any) -> Any:
        return runtime_for(rec.runtime if rec.runtime in ("podman", "docker") else "podman")

    def _recorded_workdir(self, rec: Any) -> str | None:
        """The work folder the environment was created with (what a command starts in), else
        its template's current folder for an old record that has none, else ``None``."""
        value = None
        try:
            ask = getattr(self.services.sandboxes, "work_folder", None)
            value = ask(rec) if callable(ask) else None
        except Exception:  # noqa: BLE001 - no sandbox service: the template decides
            value = None
        if not (isinstance(value, str) and value.startswith("/")):
            try:
                value = getattr(self.services.templates.get(rec.template_id), "workdir", None)
            except Exception:  # noqa: BLE001 - deleted template
                value = None
        return value if isinstance(value, str) and value.startswith("/") else None

    def _workdir(self, rec: Any) -> str:
        """The default folder of a listing, size or download. Never used to decide what a
        Delete saves (see :meth:`save_before_delete`)."""
        return self._recorded_workdir(rec) or DEFAULT_WORKDIR

    def _is_agent_sandbox(self, rec: Any) -> bool:
        """A cloud-agent sandbox: its template has an ``agent`` block with a mode, or it is
        one of the built-in agent templates, or it is named like a run's sandbox. Ordinary
        environments are never treated as agents (their dot-folders are the user's)."""
        template_id = str(getattr(rec, "template_id", "") or "")
        try:
            agent = getattr(self.services.templates.get(template_id), "agent", None)
        except Exception:  # noqa: BLE001 - template gone: fall back to the ids
            agent = None
        if isinstance(agent, dict) and agent.get("mode") in ("hermes", "script"):
            return True
        if template_id in AGENT_TEMPLATE_IDS:
            return True
        return bool(re.match(r"^agent-a-[0-9a-f]{8}$", str(getattr(rec, "name", "") or "")))

    def _internal_dirs(self, rec: Any) -> tuple[str, ...]:
        """Folders a saved copy of this environment never contains (empty for an ordinary
        environment)."""
        if not self._is_agent_sandbox(rec):
            return ()
        work = self._workdir(rec).rstrip("/") or DEFAULT_WORKDIR
        return tuple(d.format(work=work) for d in AGENT_INTERNAL_DIRS)

    def is_agent_sandbox(self, rec: Any) -> bool:
        """True for the sandbox of a cloud agent run (it holds the run's model key)."""
        return self._is_agent_sandbox(rec)

    def refuse_outside_work(self, instance_id: str, path: Any, what: str) -> None:
        """For the chat tool: a cloud-agent sandbox can be listed or saved only inside its
        work folder. The rest of it can hold the model key, and the chat model can read what
        is saved to this computer. (The page, which is the user's own, is not limited.)"""
        rec = self._record(instance_id)
        if not self._is_agent_sandbox(rec):
            return
        work = self._workdir(rec).rstrip("/") or DEFAULT_WORKDIR
        src = self._path(path, default=work)
        self._refuse_internal(rec, src)
        if src == work or src.startswith(work + "/"):
            return
        raise _bad(f"{src} is outside the work folder of the cloud agent in {rec.name}. Only "
                   f"its work folder ({work}) can be {what}, because the rest of the sandbox "
                   "can hold the model key.")

    def _refuse_internal(self, rec: Any, src: str) -> None:
        """Saving an agent-internal folder itself (or a folder above the whole filesystem)
        is refused with the reason in plain words."""
        dirs = self._internal_dirs(rec)
        if not dirs:
            return
        for folder in dirs:
            if src == folder or src.startswith(folder + "/"):
                raise _bad(f"{src} is an internal folder of the cloud agent in {rec.name}. It "
                           "can hold the model key, so it is never saved. Save the work "
                           f"folder ({self._workdir(rec)}) instead.")
        if src == "/":
            raise _bad(f"The whole file system of {rec.name} cannot be saved because it is a "
                       "cloud agent whose internal folders can hold the model key. Save the "
                       f"work folder ({self._workdir(rec)}) instead.")

    @staticmethod
    def _path(value: Any, what: str = "path", default: str | None = None) -> str:
        """A container path: quotes and whitespace stripped, normalised; ``default`` when
        the value is empty."""
        value = clean_path_text(value)
        if value in (None, "") and default is not None:
            value = default
        try:
            return container_path(value, what)
        except ValueError as exc:
            raise _bad(str(exc)) from None

    def _cap_mb(self) -> int:
        meta = CONFIG_FIELDS["export_max_mb"]
        value = getattr(self._config(), "export_max_mb", None)
        if isinstance(value, bool) or not isinstance(value, int):
            return int(meta["default"])
        return max(int(meta["min"]), min(int(value), int(meta["max"])))

    @staticmethod
    def _too_large(what: str, cap_mb: int, *, upload: bool = False) -> ApiError:
        smaller = "upload a smaller folder" if upload else "export a smaller folder"
        return ApiError(413, "too_large",
                        f'The files in {what} are larger than the export limit ({cap_mb} MB). '
                        f'Raise "Export max mb" in Settings or {smaller}.')

    def _touch(self, rec: Any) -> None:
        if rec.status != "running":
            return
        try:
            self.services.sandboxes.touch(rec.id)
        except Exception:  # noqa: BLE001 - activity tracking never fails a transfer
            log.debug("cloud-compute: could not record activity for %s", rec.id)

    @staticmethod
    def _cp_error(rec: Any, res: Any, what: str, *, copying_in: bool) -> ApiError:
        stderr = res.stderr or ""
        direction = "into" if copying_in else "from"
        if getattr(res, "timed_out", False):
            return ApiError(504, "internal", f"Copying {what} {direction} {rec.name} timed out "
                            f"after {int(TRANSFER_TIMEOUT_S // 60)} minutes.")
        if _NO_CONTAINER.search(stderr):
            return ApiError(409, "conflict",
                            f"The container of {rec.name} no longer exists on its host.")
        if not copying_in and _NO_PATH.search(stderr):
            return ApiError(404, "not_found", f"{what} does not exist in {rec.name}.")
        detail = _short(stderr or f"exit code {res.rc}")
        return ApiError(502, "internal", f"Could not copy {what} {direction} {rec.name}: {detail}")

    # ------------------------------------------------------------------ export

    @staticmethod
    def _safe_name(rec: Any) -> str:
        return _SAFE_NAME.sub("-", str(rec.name or "")).strip("-.")[:40] or "environment"

    def _stamp(self) -> str:
        return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self._clock()))

    def _new_target(self, rec: Any, reason: str,
                    dest_dir: Path | None = None) -> tuple[Path, Any]:
        """Create the export file exclusively; returns ``(path, open binary file)``.

        Default: ``<exports folder>/<name>-<id>/<stamp>-<reason>.tar.gz``; with ``dest_dir``
        (already validated): ``<dest_dir>/<name>-<stamp>-<reason>.tar.gz``.
        """
        safe = self._safe_name(rec)
        stamp = self._stamp()
        if dest_dir is None:
            root = self.exports_dir(create=True)
            safe_id = _SAFE_NAME.sub("-", str(rec.id)).strip("-.") or "unknown"
            folder = root / f"{safe}-{safe_id}"
            if not _is_within(_resolve(folder), root):
                raise ApiError(500, "internal", "Refusing to write outside the exports folder.")
            _mkdirs_private(folder)
            base = f"{stamp}-{reason}"
        else:
            folder = dest_dir
            base = f"{safe}-{stamp}-{reason}"
        for n in range(1, 1000):
            path = folder / f"{base}{'' if n == 1 else f'-{n}'}.tar.gz"
            try:
                return path, _open_private(path)
            except FileExistsError:
                continue
        raise ApiError(409, "conflict", "Too many exports in the same second; try again.")

    @staticmethod
    def _discard(path: Path, *, remove_folder: bool = True) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("cloud-compute: could not remove the partial export %s", path.name)
        if remove_folder:
            try:
                path.parent.rmdir()      # only succeeds when the folder is empty
            except OSError:
                pass

    @staticmethod
    def _verify_archive(target: Path) -> tuple[int, int]:
        """Read the saved archive back end to end; return ``(regular files, their bytes)``.

        Raises ``ValueError`` (with a short reason) when the gzip or the tar stream is
        damaged, cut short, or is not a tar stream at all (for example runtime error text).
        """
        class _Reader:
            def __init__(self, raw: Any) -> None:
                self.raw, self.total, self.tail = raw, 0, b""

            def read(self, n: int = -1) -> bytes:
                data = self.raw.read(n)
                self.total += len(data)
                self.tail = (self.tail + data)[-len(_TAR_END):]
                return data

        files = content = 0
        try:
            with open(target, "rb") as fh, gzip.GzipFile(fileobj=fh, mode="rb") as gz:
                reader = _Reader(gz)
                with tarfile.open(fileobj=reader, mode="r|") as tf:
                    for member in tf:
                        if member.isreg():
                            files += 1
                            content += max(int(member.size), 0)
                while reader.read(_VERIFY_CHUNK):     # to the gzip end: checks its CRC
                    pass
        except (tarfile.TarError, EOFError, gzip.BadGzipFile, OSError, zlib.error) as exc:
            raise ValueError(_short(str(exc) or type(exc).__name__, 200)) from None
        if reader.total == 0:
            raise ValueError("it is empty")
        if reader.total % _TAR_BLOCK or reader.tail != _TAR_END:
            raise ValueError("the tar stream ends before its end marker")
        return files, content

    def _save(self, rec: Any, src: str, reason: str, *, dest_dir: Path | None = None,
              keep_empty: bool = True, extract: bool = False) -> tuple[dict | None, int, int]:
        """Stream ``src`` into a verified archive; returns ``(ExportRecord dict, files,
        bytes of the files)``. With zero files and not ``keep_empty`` the archive is removed
        and the record is None. Any failure removes the partial archive and raises."""
        run_stream = self._stream_fn(self._transport(rec))
        argv = self._rt(rec).cp_out_argv(container_name(rec.id), src)
        cap_mb = self._cap_mb()
        internal = self._internal_dirs(rec)      # agent-internal folders: never in a saved copy
        target, fh = self._new_target(rec, reason, dest_dir)
        own_folder = dest_dir is None
        ok = False
        scrub: _TarScrubber | None = None
        try:
            try:
                with fh:
                    with gzip.GzipFile(filename="", mode="wb", fileobj=fh) as gz:
                        scrub = _TarScrubber(gz, internal, src) if internal else None
                        try:
                            res = run_stream(argv, sink=scrub.write if scrub else gz.write,
                                             max_bytes=cap_mb * 1024 * 1024,
                                             timeout=TRANSFER_TIMEOUT_S)
                        except BaseException:
                            if scrub is not None:
                                scrub.finish(complete=False)
                            raise
                        if scrub is not None:
                            scrub.finish(complete=(res.rc == 0 and not res.timed_out
                                                   and not res.too_large))
                    fh.flush()
                    os.fsync(fh.fileno())     # on disk before anything is deleted
            except OSError as exc:
                raise ApiError(502, "internal", f"Could not copy {src} from {rec.name} to this "
                               f"computer: {exc.strerror or exc or type(exc).__name__}") from None
            if res.too_large:
                raise self._too_large(src, cap_mb)
            if res.timed_out or res.rc != 0:
                raise self._cp_error(rec, res, src, copying_in=False)
            if res.bytes <= 0:
                raise ApiError(502, "internal", f"Copying {src} from {rec.name} returned no data.")
            if scrub is not None and scrub.error:
                raise ApiError(502, "internal",
                               f"The copy of {src} from {rec.name} could not be read completely "
                               f"({scrub.error}), so it was not kept.")
            try:
                files, content = self._verify_archive(target)
            except ValueError as exc:
                raise ApiError(502, "internal",
                               f"The copy of {src} from {rec.name} could not be read back "
                               f"completely ({exc}), so it was not kept.") from None
            if files == 0 and not keep_empty:
                return None, 0, 0           # `finally` removes the empty archive
            size = target.stat().st_size
            ok = True
        finally:
            if not ok:
                self._discard(target, remove_folder=own_folder)
        record = ExportRecord(id=new_id("x"), instance_id=rec.id, instance_name=rec.name,
                              host_id=rec.host_id, source_path=src, local_path=str(target),
                              bytes=int(size), created_at=utc_now_iso(), reason=reason,
                              files=int(files))
        while self.store.get_record(EXPORT_KIND, record.id) is not None:
            record.id = new_id("x")
        refused: str | None = None
        if extract:
            try:
                record.extracted_to = str(self._unpack(target, target.parent, rec,
                                                       cap_mb * 1024 * 1024))
            except _Refused as exc:
                refused = str(exc)
        self.store.put_record(EXPORT_KIND, record.id, record.to_dict(), host_id=rec.host_id,
                              status=rec.id)
        self._touch(rec)
        log.info("cloud-compute: saved %s of %s (%d files, %d bytes compressed)", src, rec.id,
                 files, size)
        if refused is not None:
            raise ApiError(400, "invalid",
                           f"{src} from {rec.name} was saved to {target} (saved copy "
                           f"{record.id}), but it was not unpacked: {refused} Nothing was "
                           "unpacked and the archive was kept; delete it if it is not trusted.")
        out = record.to_dict()
        if scrub is not None and scrub.skipped:    # not stored: only said in this result
            out["left_out"] = sorted(scrub.skipped)
            out["note"] = left_out_note(out["left_out"])
        return out, files, content

    def export(self, instance_id: str, path: str | None = None, *, reason: str = "manual",
               dest_dir: str | None = None, extract: bool = False) -> dict:
        """Copy ``path`` (default: the environment's work folder) to this computer; returns an
        ExportRecord dict. Works for running and stopped environments.

        ``dest_dir``: an existing folder on this computer for the archive (default: the
        exports folder). ``extract``: also unpack it into a new folder beside the archive.
        The copy is verified before it counts: see :meth:`export_verified`.
        """
        return self.export_verified(instance_id, path, reason=reason, dest_dir=dest_dir,
                                    extract=extract)[0]

    def export_verified(self, instance_id: str, path: str | None = None, *,
                        reason: str = "manual", dest_dir: str | None = None,
                        extract: bool = False) -> tuple[dict, int]:
        """:meth:`export`, plus the number of regular files in the saved archive.

        The archive is flushed and fsynced, then re-opened and read end to end. Any parse
        error, truncation or non-tar stream fails the export: the partial file is removed
        and ``ApiError`` is raised. A manual save or fetch of a path without regular files
        keeps nothing and raises ``nothing_to_save`` (409); the before-delete reasons keep
        an empty archive (callers that want nothing kept use :meth:`save_before_delete`).
        """
        if reason not in EXPORT_REASONS:
            raise _bad(f"reason must be one of: {', '.join(EXPORT_REASONS)}")
        rec = self._record(instance_id)
        self._usable(rec)
        src = self._path(path, default=self._workdir(rec))
        self._refuse_internal(rec, src)
        folder = None if clean_path_text(dest_dir, local=True) in (None, "") else \
            self._check_folder(dest_dir, "dest_dir")
        manual = reason in ("manual", "fetch")
        record, files, _content = self._save(rec, src, reason, dest_dir=folder,
                                             keep_empty=not manual, extract=bool(extract))
        if record is None:
            raise ApiError(409, "nothing_to_save",
                           f"Nothing to save: {src} in {rec.name} has no files.")
        return record, files

    def save_before_delete(self, instance_id: str, *, reason: str = "before-delete") -> dict:
        """The verified save a Delete runs first (the sandbox manager holds the per-instance
        lock around this call and the removal).

        Returns ``{saved, export, files, bytes, source_path, message}``: ``bytes`` is the
        size of the saved files (uncompressed). With no regular files in the workdir nothing
        is kept: ``saved`` False, ``export`` None. Any unreadable or cut-off archive raises
        and nothing may be deleted.
        """
        if reason not in ("before-delete", "auto-delete"):
            raise _bad("reason must be before-delete or auto-delete")
        rec = self._record(instance_id)
        self._usable(rec)
        folder = self._recorded_workdir(rec)
        if folder is None:
            raise ApiError(409, "not_saved", unknown_workdir_message(rec.name, rec.template_id))
        workdir = self._path(folder)
        record, files, content = self._save(rec, workdir, reason, keep_empty=False)
        if record is None:
            return {"saved": False, "export": None, "files": 0, "bytes": 0,
                    "source_path": workdir, "message": f"Nothing in {workdir} needed saving."}
        message = (f"The files in {workdir} ({file_count(files)}, {human_size(content)}) "
                   f"were saved to {record['local_path']}.")
        if record.get("note"):
            message += " " + record["note"]
        return {"saved": True, "export": record, "files": files, "bytes": content,
                "source_path": workdir, "message": message}

    def fetch(self, instance_id: str, path: str | None = None, *, dest_dir: str | None = None,
              extract: bool = False) -> dict:
        """Download ``path`` to this computer: :meth:`export` with reason ``fetch``."""
        return self.export(instance_id, path, reason="fetch", dest_dir=dest_dir,
                           extract=extract)

    # ------------------------------------------------------------------ size

    def size(self, instance_id: str, path: str | None = None) -> dict:
        """Measure ``path`` (default: the workdir) inside a running environment.

        Returns ``{path, known, bytes, files, reason}``: regular files (hidden ones too)
        and their bytes. Bounded to about :data:`SIZE_TIMEOUT_S` seconds; on a timeout or
        a stopped environment ``known`` is False and ``reason`` says why in plain words.
        """
        rec = self._record(instance_id)
        self._usable(rec)
        folder = self._path(path, default=self._workdir(rec))

        def unknown(reason: str) -> dict:
            return {"path": folder, "known": False, "bytes": None, "files": None,
                    "reason": reason}

        if rec.status != "running":
            return unknown(f"{rec.name} is not running, so the size of {folder} could not be "
                           "measured. Start it to see the size first.")
        argv = self._rt(rec).exec_argv(container_name(rec.id),
                                       ["sh", "-c", _SIZE_WRAP, "sh", folder, _SIZE_SCRIPT])
        try:
            res = self._transport(rec).run(argv, timeout=SIZE_TIMEOUT_S + 5)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - the size is optional information
            return unknown(f"The size of {folder} could not be measured "
                           f"({type(exc).__name__}).")
        if res.rc == 3:
            raise ApiError(404, "not_found", f"{folder} does not exist in {rec.name}.")
        if getattr(res, "timed_out", False) or res.rc == 124:
            return unknown(f"Measuring {folder} in {rec.name} took longer than "
                           f"{SIZE_TIMEOUT_S} seconds (it may hold very many files), so its "
                           "size is not known.")
        if res.rc != 0:
            if _NOT_RUNNING.search(res.stderr or ""):
                return unknown(f"{rec.name} is not running, so the size of {folder} could "
                               "not be measured. Start it to see the size first.")
            return unknown(f"The size of {folder} could not be measured: "
                           f"{_short(res.stderr or f'exit code {res.rc}', 200)}")
        try:
            files_s, bytes_s = (res.stdout or "").strip().splitlines()[-1].split()[:2]
            files, nbytes = int(float(files_s)), int(float(bytes_s))
        except (ValueError, IndexError):
            return unknown(f"The size of {folder} could not be read from {rec.name}.")
        self._touch(rec)
        return {"path": folder, "known": True, "bytes": nbytes, "files": files, "reason": None}

    # ------------------------------------------------------------------ unpack

    def _unpack(self, archive: Path, base: Path, rec: Any, cap_bytes: int) -> Path:
        """Unpack a saved archive into a NEW folder ``<base>/<name>-<stamp>/``.

        Raises :class:`_Refused` (and removes the partly unpacked folder) on any member
        that is never unpacked here; see :func:`_safe_unpack`.
        """
        stem = f"{self._safe_name(rec)}-{self._stamp()}"
        for n in range(1, 1000):
            folder = base / f"{stem}{'' if n == 1 else f'-{n}'}"
            try:
                folder.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
            except OSError as exc:
                raise _Refused(f"the folder {folder} could not be created "
                               f"({exc.strerror or type(exc).__name__}).") from None
        else:
            raise _Refused("too many unpacked folders with the same name.")
        try:
            _safe_unpack(archive, folder, cap_bytes)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return _resolve(folder)

    def list_exports(self, instance_id: str | None = None) -> list[dict]:
        """Saved exports (ExportRecord dicts), newest first; only one environment's when given.

        Entries whose file was removed from the exports folder by hand are left out.
        """
        rows = []
        for data in self.store.list_records(EXPORT_KIND):
            try:
                rec = ExportRecord.from_dict(data)
            except Exception:  # noqa: BLE001 - a damaged row is skipped
                continue
            if instance_id is not None and rec.instance_id != instance_id:
                continue
            if not Path(rec.local_path).is_file():
                continue
            rows.append(rec.to_dict())
        rows.sort(key=lambda r: (r["created_at"], Path(r["local_path"]).name), reverse=True)
        return rows

    def _export_record(self, export_id: Any) -> ExportRecord:
        data = self.store.get_record(EXPORT_KIND, export_id) if isinstance(export_id, str) else None
        if data is None:
            raise ApiError(404, "not_found", f"Saved export {export_id} was not found.")
        try:
            return ExportRecord.from_dict(data)
        except Exception:  # noqa: BLE001
            raise ApiError(500, "internal", f"Saved export {export_id} is damaged.") from None

    def get_export(self, export_id: str) -> dict:
        """One saved copy (ExportRecord dict); 404 when unknown or its file is gone."""
        rec = self._export_record(clean_path_text(export_id))
        if not Path(rec.local_path).is_file():
            raise ApiError(404, "not_found", f"The file of saved copy {rec.id} is no longer on "
                           f"this computer ({rec.local_path}).")
        return rec.to_dict()

    def _deletable(self, path: Path) -> bool:
        """A saved-copy archive this plugin wrote: a regular file named like one, outside the
        private folders (the plugin's own exports folder allowed)."""
        if path.is_symlink() or not _ARCHIVE_NAME.match(path.name):
            return False
        return self._writable_place(_resolve(path))

    def delete_export(self, export_id: str) -> dict:
        """Remove a saved copy from this computer: its archive and its index entry.

        An unpacked folder (``extracted_to``) is never removed; when it exists the result
        carries a ``message`` naming it.
        """
        rec = self._export_record(export_id)
        raw = Path(rec.local_path)
        path = _resolve(raw)
        try:
            own = self._default_exports_dir()
        except ApiError:
            own = None
        if self._deletable(raw):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ApiError(502, "internal",
                               f"Could not delete {path.name}: {exc.strerror or exc}") from None
            if own is not None and _is_within(path.parent, own) and not _same_place(path.parent, own):
                try:
                    path.parent.rmdir()      # the per-environment folder, when now empty
                except OSError:
                    pass
        else:   # never delete anything that is not one of the plugin's archives
            log.warning("cloud-compute: export %s does not point at a saved-copy archive; "
                        "only its index entry is removed", rec.id)
        self.store.delete_record(EXPORT_KIND, rec.id)
        out: dict[str, Any] = {"deleted": True}
        if rec.extracted_to and Path(rec.extracted_to).is_dir():
            out["message"] = (f"Deleted the saved copy {path.name}. The unpacked folder "
                              f"{rec.extracted_to} was left in place.")
        return out

    # ------------------------------------------------------------------ list

    def list_dir(self, instance_id: str, path: str | None = None) -> dict:
        """List a folder inside a running environment (at most 500 entries)."""
        rec = self._record(instance_id)
        self._usable(rec)
        if rec.status != "running":
            raise ApiError(409, "not_running", f"Start {rec.name} to browse its files.")
        folder = self._path(path, default=self._workdir(rec))
        argv = self._rt(rec).exec_argv(
            container_name(rec.id),
            ["sh", "-c", _LIST_SCRIPT, "sh", folder, str(LIST_MAX_ENTRIES + 1)])
        res = self._transport(rec).run(argv, timeout=LIST_TIMEOUT_S)
        if res.rc == 3:
            raise ApiError(404, "not_found", f"{folder} does not exist in {rec.name}.")
        if res.rc == 4:
            raise _bad(f"{folder} is not a folder.")
        if res.rc != 0:
            if _NOT_RUNNING.search(res.stderr or ""):
                raise ApiError(409, "not_running", f"Start {rec.name} to browse its files.")
            if getattr(res, "timed_out", False):
                raise ApiError(504, "internal", f"Listing {folder} in {rec.name} timed out.")
            raise ApiError(502, "internal", f"Could not list {folder} in {rec.name}: "
                           f"{_short(res.stderr or f'exit code {res.rc}')}")
        entries, cut = self._parse_listing(res.stdout or "")
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower(), e["name"]))
        self._touch(rec)
        return {"path": folder, "entries": entries[:LIST_MAX_ENTRIES],
                "truncated": cut or len(entries) > LIST_MAX_ENTRIES}

    @staticmethod
    def _parse_listing(text: str) -> tuple[list[dict], bool]:
        """NUL-terminated listing records -> entries; ``cut`` when the output was cut short."""
        parts = text.split("\x00")
        tail = parts.pop()               # "" when the output ended on a full record
        cut = bool(tail.strip()) or len(text.encode("utf-8", "replace")) >= LIST_MAX_BYTES
        entries = []
        for raw in parts:
            fields = raw.split("\t", 3)
            if len(fields) != 4:
                continue
            kind, size, mtime, name = fields
            if not name or name in (".", "..") or "/" in name:
                continue
            try:
                size_v: int | None = int(size)
            except ValueError:
                size_v = None
            entries.append({"name": name, "type": _TYPES.get(kind.strip(), "other"),
                            "size": size_v, "mtime": _iso_from_epoch(mtime)})
        return entries, cut

    # ------------------------------------------------------------------ put / restore

    def _local_source(self, local_path: Any) -> Path:
        local_path = clean_path_text(local_path, local=True)
        if not isinstance(local_path, str) or not local_path or "\x00" in local_path:
            raise _bad("local_path must be a path on this computer")
        if is_special_windows_path(local_path):
            raise ApiError(403, "invalid", special_path_message())
        src = Path(local_path)
        if not src.is_absolute():
            raise _bad("local_path must be an absolute path on this computer")
        if not src.exists():
            raise ApiError(404, "not_found", f"{local_path} does not exist on this computer.")
        if not (src.is_file() or src.is_dir()):
            raise _bad(f"{local_path} is not a file or a folder.")
        if _is_network_final_path(_resolve(src)):
            raise ApiError(403, "invalid", special_path_message())
        if not self._local_allowed(src):
            if _is_within(_resolve(src), _resolve(Path.home() / ".ssh")):
                raise ApiError(403, "invalid", f"{local_path} is in the SSH folder; files there "
                               "are never uploaded.")
            raise ApiError(403, "invalid",
                           "Files inside the Hermes home cannot be uploaded. Copy the file to "
                           "another folder first (for example the project folder), then upload "
                           "it from there.")
        return _resolve(src)

    @staticmethod
    def _over_cap(src: Path, cap_bytes: int) -> bool:
        """True when the local file or folder is already known to be over the cap."""
        if src.is_file():
            return src.stat().st_size > cap_bytes
        total = 0
        for root, _dirs, files in os.walk(src):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    continue
                if total > cap_bytes:
                    return True
        return False

    def _tar_source(self, src: Path, prefix: str, *, extract: bool, strip_top: bool,
                    manifest: dict | None = None) -> Callable[[Any], None]:
        """A ``run_stream`` source writing an uncompressed tar of ``src`` under ``prefix``.

        The stream ends at the end-of-archive marker (see :class:`_NoPadTar`). With
        ``manifest`` (an unpacked archive only) every regular file, folder and link written
        is recorded as ``name -> ("file", size, sha256) | ("dir",) | ("sym", target)``, so
        the result of a copy can be checked afterwards.
        """

        def join(name: str) -> str:
            return posixpath.join(prefix, name) if prefix else name

        def pack_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            if not (info.isfile() or info.isdir() or info.issym()):
                return None
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            return info

        def source(writer: Any) -> None:
            with _NoPadTar.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as out:
                if manifest is not None:
                    manifest.clear()
                if not extract:
                    self._add_tree(out, src, join(src.name), pack_filter)
                    return
                top: str | None = None
                seen = False
                with open(src, "rb") as raw, \
                        tarfile.open(fileobj=raw, mode=_archive_mode(src) or "r|") as arc:
                    for member in arc:
                        name = _member_name(member.name)
                        if name is None:
                            continue
                        first, seen = not seen, True
                        if strip_top:
                            if first and member.isdir():
                                top = name           # the saved folder itself: not re-created
                                continue
                            if top is not None:
                                if name == top:
                                    continue
                                if name.startswith(top + "/"):
                                    name = name[len(top) + 1:]
                        if not (member.isfile() or member.isdir() or member.issym()
                                or member.islnk()):
                            continue                 # devices, fifos: never unpacked
                        if member.islnk():
                            link = _member_name(member.linkname)
                            if link is None:
                                continue
                            if top is not None and link.startswith(top + "/"):
                                link = link[len(top) + 1:]
                            member.linkname = join(link)
                        member.name = join(name)
                        data = arc.extractfile(member) if member.isfile() else None
                        if data is not None and manifest is not None:
                            data = _HashingReader(data)
                        out.addfile(member, data)
                        if manifest is not None:
                            if member.isfile():
                                manifest[member.name] = ("file", data.size, data.digest.hexdigest())
                            elif member.isdir():
                                manifest[member.name] = ("dir",)
                            elif member.issym():
                                manifest[member.name] = ("sym", member.linkname)
                            else:
                                manifest[member.name] = ("link", member.linkname)

        return source

    def _add_tree(self, out: tarfile.TarFile, src: Path, arcname: str,
                  pack_filter: Callable[[tarfile.TarInfo], Any]) -> None:
        """Add ``src`` without following symlinks and without private folders."""
        out.add(str(src), arcname=arcname, recursive=False, filter=pack_filter)
        if not src.is_dir() or src.is_symlink():
            return
        allowed = self._privacy_rule()
        for root, dirs, files in os.walk(src):          # followlinks=False
            dirs.sort()
            keep = []
            for name in dirs + sorted(files):
                full = os.path.join(root, name)
                if not allowed(Path(full)):
                    continue
                rel = os.path.relpath(full, src).replace(os.sep, "/")
                out.add(full, arcname=posixpath.join(arcname, rel), recursive=False,
                        filter=pack_filter)
                if name in dirs and not os.path.islink(full):
                    keep.append(name)
            dirs[:] = keep

    def _put(self, rec: Any, src: Path, dest: str, *, extract: bool, strip_top: bool,
             what: str) -> dict:
        # Under the environment's lock a Delete cannot save and remove it mid-upload; the
        # record is re-read so an upload never starts into one deleted meanwhile.
        with self._instance_lock(rec.id):
            rec = self._record(rec.id)
            self._usable(rec)
            return self._put_locked(rec, src, dest, extract=extract, strip_top=strip_top,
                                    what=what)

    def _landed(self, rec: Any, run_stream: Callable[..., Any], res: Any, dest: str,
                manifest: dict | None, cap_bytes: int) -> bool:
        """True when a copy that exited with ``io: read/write on closed pipe`` still delivered
        every file: the destination is read back and each file, folder and link of the unpacked
        archive must be there with the same size and SHA-256. Any other failure, anything that
        cannot be checked and any difference counts as a failed copy."""
        if (not manifest or res.timed_out or getattr(res, "too_large", False)
                or "closed pipe" not in (res.stderr or "")):
            return False
        base = posixpath.dirname(dest.strip("/"))
        if not dest.strip("/") or any(v[0] == "link" for v in manifest.values()):
            return False                    # "/" is too big to read back; hard links: unchecked
        argv = self._rt(rec).cp_out_argv(container_name(rec.id), dest)
        found: dict[str, tuple] = {}
        try:
            with tempfile.TemporaryFile() as spool:
                got = run_stream(argv, sink=spool.write, max_bytes=cap_bytes,
                                 timeout=TRANSFER_TIMEOUT_S)
                if got.rc != 0 or got.timed_out or getattr(got, "too_large", False):
                    return False
                spool.seek(0)
                with tarfile.open(fileobj=spool, mode="r:") as arc:
                    for member in arc:
                        name = posixpath.join(base, _member_name(member.name) or "")
                        if member.isfile():
                            digest, size = hashlib.sha256(), 0
                            handle = arc.extractfile(member)
                            while True:
                                block = handle.read(1024 * 1024)
                                if not block:
                                    break
                                digest.update(block)
                                size += len(block)
                            found[name] = ("file", size, digest.hexdigest())
                        elif member.isdir():
                            found[name] = ("dir",)
                        elif member.issym():
                            found[name] = ("sym", member.linkname)
        except (tarfile.TarError, OSError, EOFError, ValueError):
            return False
        return all(found.get(name.rstrip("/")) == want for name, want in manifest.items())

    def _put_locked(self, rec: Any, src: Path, dest: str, *, extract: bool, strip_top: bool,
                    what: str) -> dict:
        run_stream = self._stream_fn(self._transport(rec))
        cap_mb = self._cap_mb()
        cap_bytes = cap_mb * 1024 * 1024
        if not extract and self._over_cap(src, cap_bytes):
            raise self._too_large(what, cap_mb, upload=True)
        # Member names carry the destination folder, so one copy into "/" creates it, even
        # in a stopped container where mkdir cannot run.
        argv = self._rt(rec).cp_in_argv(container_name(rec.id), "/")
        manifest: dict | None = {} if extract else None
        source = self._tar_source(src, dest.strip("/"), extract=extract, strip_top=strip_top,
                                  manifest=manifest)
        try:
            res = run_stream(argv, source=source, max_bytes=cap_bytes, timeout=TRANSFER_TIMEOUT_S)
        except (tarfile.TarError, EOFError, gzip.BadGzipFile):
            raise _bad(f"{what} is not a valid tar archive.") from None
        except OSError as exc:
            raise ApiError(502, "internal",
                           f"Could not read {what}: {exc.strerror or type(exc).__name__}") from None
        if res.too_large:
            raise self._too_large(what, cap_mb, upload=True)
        if res.timed_out or res.rc != 0:
            if not self._landed(rec, run_stream, res, dest, manifest, cap_bytes):
                raise self._cp_error(rec, res, what, copying_in=True)
            log.warning("cloud-compute: the copy into %s:%s reported an error, but every file "
                        "of the archive is there with the same content", rec.id, dest)
        self._touch(rec)
        log.info("cloud-compute: uploaded %d bytes into %s:%s", res.bytes, rec.id, dest)
        return {"instance_id": rec.id, "dest": dest, "bytes": int(res.bytes)}

    def put(self, instance_id: str, local_path: str, dest: str, *, extract: bool = False) -> dict:
        """Upload a file or folder from this computer into the folder ``dest`` of an environment.

        Running or stopped. With ``extract`` a ``.tar``/``.tar.gz``/``.tgz`` file is unpacked
        into ``dest`` (inside the container only); otherwise the file or folder is copied
        into ``dest`` under its own name.
        """
        rec = self._record(instance_id)
        self._usable(rec)
        dest = self._path(dest, "destination", default=self._workdir(rec))
        src = self._local_source(local_path)
        if extract and (not src.is_file() or _archive_mode(src) is None):
            raise _bad("Only .tar, .tar.gz or .tgz files can be unpacked; "
                       "turn off extract to copy it as it is.")
        return self._put(rec, src, dest, extract=bool(extract), strip_top=False, what=str(src))

    def restore(self, export_id: str, instance_id: str, dest: str = DEFAULT_WORKDIR) -> dict:
        """Unpack a saved export into ``dest`` of an environment (running or stopped).

        The saved folder's contents land directly in ``dest``: an export of ``/work``
        restored to ``/work`` puts ``results/a.txt`` back at ``/work/results/a.txt``.
        """
        export = self._export_record(export_id)
        rec = self._record(instance_id)
        self._usable(rec)
        dest = self._path(dest, "destination", default=self._workdir(rec))
        path = _resolve(Path(export.local_path))
        if not path.is_file() or not self._deletable(Path(export.local_path)):
            raise ApiError(404, "not_found",
                           f"The file of saved export {export.id} is missing from this computer.")
        out = self._put(rec, path, dest, extract=True, strip_top=True,
                        what=f"saved export {export.id}")
        out["export_id"] = export.id
        return out


def provide(services: Any) -> FileManager:
    """Service provider used by the module registry."""
    return FileManager(services)
