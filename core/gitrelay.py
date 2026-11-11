"""Git relay: move a branch to a sandbox and back as git bundles.

Pure local git through ``subprocess`` (argument lists, no shell). A bundle
carries objects and refs only: no remotes, no credentials, no config. Imports
land under a dedicated ref prefix and never move the user's own branches.

Standard library only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GitError", "git", "git_version", "is_repo", "rev_parse", "ref_exists",
    "bundle_branch", "bundle_bytes", "import_bundle", "import_bundle_bytes",
    "RefImport", "import_bundle_detailed", "import_bundle_bytes_detailed", "worktree_for_ref",
    "create_worktree", "ahead_behind", "merge_conflicts_dry_run", "safe_branch_name",
]

#: Windows ``CREATE_NO_WINDOW``: keeps git from flashing a console under a console-less gateway.
_CREATE_NO_WINDOW = 0x08000000

_ALLOWED_PREFIXES = ("refs/heads/cloud/", "refs/cloud-compute/")


class GitError(Exception):
    """A git command failed. ``args[0]`` is the message; ``rc`` and ``stderr`` carry detail."""

    def __init__(self, message: str, rc: int = -1, stderr: str = "") -> None:
        super().__init__(message)
        self.rc = rc
        self.stderr = stderr


def _is_windows() -> bool:
    return os.name == "nt"


#: Environment variables a git child may inherit (compared case-insensitively, as on Windows).
#: Everything else, notably API keys and tokens, stays out of git processes. The ``GIT_*``
#: variables git needs are set below, never copied.
_ENV_ALLOWLIST = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "HOME", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "TZ",
})


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOWLIST}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    if extra:
        env.update(extra)
    return env


def _run(argv: list[str], *, input: bytes | None, timeout: float,
         extra_env: dict[str, str] | None = None) -> tuple[int, bytes, str]:
    kwargs: dict = {}
    if _is_windows():
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            argv, input=input, capture_output=True, timeout=timeout,
            env=_env(extra_env), check=False, **kwargs,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found", -1, str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git timed out after {timeout:g}s", -1, "") from exc
    err = proc.stderr.decode("utf-8", "replace").strip()
    return proc.returncode, proc.stdout, err[-2000:]


def git(repo: Path, *args: str, input: bytes | None = None, timeout: float = 120,
        _env_extra: dict[str, str] | None = None, _ok: tuple[int, ...] = (0,)) -> bytes:
    """Run ``git -C <repo> <args>``; return stdout bytes, raise ``GitError`` on failure."""
    argv = ["git", "-C", str(repo), *args]
    rc, out, err = _run(argv, input=input, timeout=timeout, extra_env=_env_extra)
    if rc not in _ok:
        last = err.splitlines()[-1] if err else ""
        raise GitError(f"git {args[0] if args else ''} failed (rc={rc}): {last}", rc, err)
    return out


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace").strip()


def git_version() -> tuple[int, int, int]:
    rc, out, err = _run(["git", "--version"], input=None, timeout=30)
    if rc != 0:
        raise GitError("git --version failed", rc, err)
    text = out.decode("utf-8", "replace")
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        raise GitError("cannot parse git version", rc, text)
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def is_repo(path: Path) -> bool:
    p = Path(path)
    if not p.is_dir():
        return False
    for probe in ("--is-inside-work-tree", "--is-bare-repository"):
        try:
            if _text(git(p, "rev-parse", probe)) == "true":
                return True
        except GitError:
            return False
    return False


def rev_parse(repo: Path, rev: str) -> str:
    """Full commit sha for ``rev``; ``GitError`` if unknown."""
    if not rev or rev.startswith("-"):
        raise GitError(f"invalid revision: {rev!r}")
    return _text(git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", rev + "^{commit}"))


def ref_exists(repo: Path, ref: str) -> bool:
    if not ref or ref.startswith("-"):
        return False
    try:
        git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", ref)
        return True
    except GitError:
        return False


def safe_branch_name(name: str) -> str:
    bad = (not isinstance(name, str) or not name or name.startswith("-")
           or name.startswith("refs/") or "\n" in name or "\x00" in name
           or name in ("HEAD", "@"))
    if bad:
        raise GitError(f"invalid branch name: {name!r}")
    rc, _, err = _run(["git", "check-ref-format", "--branch", name], input=None, timeout=30)
    if rc != 0:
        raise GitError(f"invalid branch name: {name!r}", rc, err)
    return name


def _private_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="cc-relay-"))


def bundle_branch(repo: Path, branch: str, base: str, out: Path | None = None) -> Path:
    """Bundle whose single head is ``refs/heads/<branch>``.

    An existing local branch is bundled with its full history. Otherwise ``base``
    is resolved and carried under ``refs/heads/<branch>`` as a new branch. No
    branch of the user's is created or moved and the working tree is untouched.
    """
    repo = Path(repo)
    branch = safe_branch_name(branch)
    full = f"refs/heads/{branch}"
    private: Path | None = None
    if out is None:
        private = _private_dir()
        out = private / "relay.bundle"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if ref_exists(repo, full):
            git(repo, "bundle", "create", str(out), full, timeout=600)
        else:
            _bundle_new_branch(repo, full, rev_parse(repo, base), out)
        return out
    except BaseException:
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)
        raise


def _bundle_new_branch(repo: Path, full_ref: str, sha: str, out: Path) -> None:
    """Carry ``sha`` under ``full_ref`` using a scratch repository that borrows the objects.

    The user's repository receives no refs at all, so there is nothing to clean up in it.
    """
    objects = _text(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects"))
    scratch = _private_dir()
    try:
        sg = scratch / "s.git"
        rc, _, err = _run(["git", "init", "--bare", "--quiet", str(sg)], input=None, timeout=60)
        if rc != 0:
            raise GitError("git init failed", rc, err)
        info = sg / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        # bytes, not write_text: a CRLF line ending would corrupt the path on Windows
        (info / "alternates").write_bytes(objects.replace("\\", "/").encode("utf-8") + b"\n")
        git(sg, "update-ref", full_ref, sha)
        git(sg, "bundle", "create", str(out), full_ref, timeout=600)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def bundle_bytes(repo: Path, branch: str, base: str) -> tuple[bytes, str]:
    """``(bundle content, base_sha)``; base_sha is the branch tip or the resolved base."""
    repo = Path(repo)
    branch = safe_branch_name(branch)
    full = f"refs/heads/{branch}"
    base_sha = rev_parse(repo, full if ref_exists(repo, full) else base)
    path = bundle_branch(repo, branch, base)
    try:
        return path.read_bytes(), base_sha
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def _check_prefix(ref_prefix: str) -> str:
    ok = (isinstance(ref_prefix, str) and ref_prefix.endswith("/")
          and ref_prefix.startswith(_ALLOWED_PREFIXES)
          and ".." not in ref_prefix and "//" not in ref_prefix
          and not any(c.isspace() or c in "\\~^:?*[" for c in ref_prefix))
    if not ok:
        raise GitError(f"ref prefix not allowed: {ref_prefix!r}")
    return ref_prefix


@dataclass
class RefImport:
    """What importing one head of a bundle did.

    ``status`` is ``created`` (new ref), ``updated`` (fast-forwarded), ``unchanged`` (the ref
    already has these commits, or more), ``diverged`` (the ref and the bundle both moved on) or
    ``blocked`` (a checked-out worktree refused the fast-forward). In the last two nothing
    was changed; the bundle's tip is kept under ``incoming_ref`` when asked for.
    """

    ref: str
    status: str
    sha: str
    message: str = ""
    worktree: str | None = None
    incoming_ref: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("created", "updated", "unchanged")


def _incoming_ref(target: str) -> str:
    """Where a refused tip is kept: ``refs/cloud-compute/incoming/<target without refs/>``."""
    return "refs/cloud-compute/incoming/" + target[len("refs/"):]


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    rc, _, err = _run(["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
                      input=None, timeout=60)
    if rc == 0:
        return True
    if rc == 1:
        return False
    raise GitError(f"git merge-base failed (rc={rc}): {err.splitlines()[-1] if err else ''}", rc, err)


def worktree_for_ref(repo: Path, ref: str) -> str | None:
    """Path of the worktree that has branch ``ref`` checked out, else ``None``."""
    out = _text(git(Path(repo), "worktree", "list", "--porcelain"))
    path: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch ") and line[len("branch "):] == ref:
            return path
    return None


def import_bundle_detailed(repo: Path, bundle: Path, ref_prefix: str, *,
                           keep_incoming: bool = True) -> list[RefImport]:
    """Fetch every ``refs/heads/*`` of ``bundle`` into ``<ref_prefix><name>``, safely.

    - A new ref is created.
    - An existing ref that already has the bundle's commits is left alone, so importing the
      same result twice is harmless.
    - An existing ref that the bundle fast-forwards is moved forward. When a worktree has the
      ref checked out, the fetch goes to a private temporary ref and the worktree runs
      ``git merge --ff-only`` so its files follow (git refuses to fetch into a checked-out
      branch).
    - If a fast-forward is not possible nothing is changed (never forced) and the status is
      ``diverged`` or ``blocked``. With ``keep_incoming`` the bundle's tip is kept under
      ``refs/cloud-compute/incoming/...`` so the commits are not lost.
    """
    repo = Path(repo)
    ref_prefix = _check_prefix(ref_prefix)
    git(repo, "bundle", "verify", str(bundle))
    heads = _text(git(repo, "bundle", "list-heads", str(bundle)))
    names: list[str] = []
    for line in heads.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            names.append(parts[1][len("refs/heads/"):])
    if not names:
        raise GitError("bundle contains no branches")
    for name in names:
        rc, _, _ = _run(["git", "check-ref-format", ref_prefix + name], input=None, timeout=30)
        if rc != 0:
            raise GitError(f"invalid ref name: {ref_prefix + name}")
    scratch_prefix = f"refs/cloud-compute/tmp/{uuid.uuid4().hex}/"
    results: list[RefImport] = []
    for name in names:
        target = ref_prefix + name
        if not ref_exists(repo, target):
            # No leading '+': a ref that appears meanwhile is not overwritten.
            git(repo, "fetch", "--no-tags", "--quiet", "--no-write-fetch-head", str(bundle),
                f"refs/heads/{name}:{target}", timeout=600)
            results.append(RefImport(target, "created", rev_parse(repo, target)))
            continue
        tmp = scratch_prefix + name
        try:
            git(repo, "fetch", "--no-tags", "--quiet", "--no-write-fetch-head", str(bundle),
                f"+refs/heads/{name}:{tmp}", timeout=600)
            results.append(_settle(repo, target, tmp, keep_incoming))
        finally:
            _run(["git", "-C", str(repo), "update-ref", "-d", tmp], input=None, timeout=30)
    return results


def _settle(repo: Path, target: str, tmp: str, keep_incoming: bool) -> RefImport:
    """Move existing ``target`` to the fetched tip ``tmp`` only if that is a fast-forward."""
    new = rev_parse(repo, tmp)
    old = rev_parse(repo, target)
    if new == old or _is_ancestor(repo, new, old):
        return RefImport(target, "unchanged", new)
    worktree = worktree_for_ref(repo, target)
    if _is_ancestor(repo, old, new):
        if worktree is None:
            git(repo, "update-ref", target, new, old)
            return RefImport(target, "updated", new)
        try:
            git(Path(worktree), "merge", "--ff-only", "--quiet", new)
        except GitError as exc:
            return _refused(repo, target, new, "blocked", worktree, keep_incoming,
                            f"{worktree} has the branch checked out and could not be fast-forwarded "
                            f"({exc.args[0]}). Commit or stash the changes there, then Collect again.")
        return RefImport(target, "updated", new, worktree=worktree)
    where = f" (checked out in {worktree})" if worktree else ""
    return _refused(repo, target, new, "diverged", worktree, keep_incoming,
                    f"{target}{where} has commits that are not in the sandbox result, and the "
                    "sandbox result has commits that are not in it, so nothing was changed.")


def _refused(repo: Path, target: str, new: str, status: str, worktree: str | None,
             keep_incoming: bool, message: str) -> RefImport:
    incoming: str | None = None
    if keep_incoming:
        incoming = _incoming_ref(target)
        git(repo, "update-ref", incoming, new)
        message += f" The sandbox commits are kept as {incoming} to compare or merge."
    return RefImport(target, status, new, message, worktree, incoming)


def import_bundle(repo: Path, bundle: Path, ref_prefix: str) -> list[str]:
    """Fetch every ``refs/heads/*`` of ``bundle`` into ``<ref_prefix><name>``.

    Existing refs are only updated fast-forward (or left as is when identical); a ref that
    cannot be fast-forwarded raises :class:`GitError`.
    """
    results = import_bundle_detailed(repo, bundle, ref_prefix, keep_incoming=False)
    for item in results:
        if not item.ok:
            raise GitError(f"git fetch failed: {item.message}")
    return [item.ref for item in results]


def import_bundle_bytes_detailed(repo: Path, data: bytes, ref_prefix: str, *,
                                 keep_incoming: bool = True) -> list[RefImport]:
    _check_prefix(ref_prefix)
    scratch = _private_dir()
    try:
        path = scratch / "in.bundle"
        path.write_bytes(data)
        return import_bundle_detailed(repo, path, ref_prefix, keep_incoming=keep_incoming)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def import_bundle_bytes(repo: Path, data: bytes, ref_prefix: str) -> list[str]:
    _check_prefix(ref_prefix)
    scratch = _private_dir()
    try:
        path = scratch / "in.bundle"
        path.write_bytes(data)
        return import_bundle(repo, path, ref_prefix)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def create_worktree(repo: Path, ref: str, path: Path) -> Path:
    """Check ``ref`` out in a new worktree at ``path`` (refuses a non-empty existing path)."""
    repo, path = Path(repo), Path(path)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise GitError(f"worktree path exists and is not empty: {path}")
    if ref.startswith("refs/heads/"):
        if not ref_exists(repo, ref):
            raise GitError(f"unknown ref: {ref}")
        git(repo, "worktree", "add", str(path), ref[len("refs/heads/"):])
    else:
        git(repo, "worktree", "add", "--detach", str(path), rev_parse(repo, ref))
    return path


def ahead_behind(repo: Path, a: str, b: str) -> tuple[int, int]:
    """``(commits in a not in b, commits in b not in a)``."""
    sa, sb = rev_parse(repo, a), rev_parse(repo, b)
    out = _text(git(repo, "rev-list", "--left-right", "--count", f"{sa}...{sb}"))
    left, right = out.split()
    return int(left), int(right)


def merge_conflicts_dry_run(repo: Path, ours: str, theirs: str) -> list[str]:
    """Paths that would conflict merging ``theirs`` into ``ours``; changes no ref, index or file."""
    repo = Path(repo)
    o, t = rev_parse(repo, ours), rev_parse(repo, theirs)
    if git_version() >= (2, 38, 0):
        return _conflicts_merge_tree(repo, o, t)
    return _conflicts_read_tree(repo, o, t)


def _conflicts_merge_tree(repo: Path, o: str, t: str) -> list[str]:
    out = git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", o, t,
              _ok=(0, 1))
    # -z output: "<tree>\0" followed by NUL-separated conflicted paths (none when clean).
    parts = out.decode("utf-8", "replace").split("\0")
    return sorted({p for p in parts[1:] if p})


def _conflicts_read_tree(repo: Path, o: str, t: str) -> list[str]:
    """Fallback for old git: a three-way index in a throwaway index file.

    May also report a file where both sides edited different regions.
    """
    scratch = _private_dir()
    try:
        env = {"GIT_INDEX_FILE": str(scratch / "index")}
        try:
            base: str | None = _text(git(repo, "merge-base", o, t))
        except GitError:
            base = None
        args = ["read-tree", "-i", "-m", "--aggressive"]
        args += [base, o, t] if base else [o, t]
        git(repo, *args, _env_extra=env)
        out = git(repo, "ls-files", "-u", "-z", _env_extra=env)
        paths = {rec.split("\t", 1)[1] for rec in out.decode("utf-8", "replace").split("\0")
                 if "\t" in rec}
        return sorted(paths)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
