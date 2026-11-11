"""Git relay tests on temporary repositories (local git only, no network)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from cloud_compute_core import gitrelay as gr

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

SANDBOX_PREFIX = "refs/heads/cloud/a-0000abcd/"


def sh(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)
    return proc.stdout.decode("utf-8", "replace").strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    sh(path, "init", "-q", "-b", "main")
    sh(path, "config", "user.name", "Test User")
    sh(path, "config", "user.email", "test@example.invalid")
    sh(path, "config", "commit.gpgsign", "false")
    return path


def commit(repo: Path, name: str, text: str, msg: str | None = None) -> str:
    f = repo / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    sh(repo, "add", "--", name)
    sh(repo, "commit", "-q", "-m", msg or f"edit {name}")
    return sh(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = init_repo(tmp_path / "orig repo")  # space in path on purpose
    commit(r, "a.txt", "one\n")
    commit(r, "b.txt", "two\n")
    sh(r, "branch", "feature")
    return r


def snapshot(repo: Path) -> dict:
    return {
        "refs": sh(repo, "for-each-ref"),
        "head": sh(repo, "rev-parse", "HEAD"),
        "status": sh(repo, "status", "--porcelain=v1"),
        "index": sh(repo, "ls-files", "-s"),
    }


def clone_to(bundle: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(bundle), str(dest)], check=True, capture_output=True)
    sh(dest, "config", "user.name", "Sandbox")
    sh(dest, "config", "user.email", "sandbox@example.invalid")
    sh(dest, "config", "commit.gpgsign", "false")
    return dest


def test_git_version_and_is_repo(repo: Path, tmp_path: Path):
    assert gr.git_version() >= (2, 0, 0)
    assert gr.is_repo(repo)
    assert not gr.is_repo(tmp_path / "missing")
    (tmp_path / "plain").mkdir()
    assert not gr.is_repo(tmp_path / "plain")


def test_rev_parse_and_ref_exists(repo: Path):
    head = gr.rev_parse(repo, "HEAD")
    assert len(head) == 40
    assert gr.ref_exists(repo, "refs/heads/feature")
    assert not gr.ref_exists(repo, "refs/heads/nope")
    with pytest.raises(gr.GitError):
        gr.rev_parse(repo, "no-such-rev")
    with pytest.raises(gr.GitError):
        gr.rev_parse(repo, "--help")


def test_git_error_fields(repo: Path):
    with pytest.raises(gr.GitError) as ei:
        gr.git(repo, "rev-parse", "--verify", "nonexistent-thing")
    assert ei.value.rc != 0
    assert isinstance(ei.value.stderr, str)


def test_safe_branch_name():
    assert gr.safe_branch_name("feature/x-1") == "feature/x-1"
    for bad in ["", "-x", "a..b", "a b", "a~1", "refs/heads/x", "x.lock", "HEAD", "a\nb", "end/"]:
        with pytest.raises(gr.GitError):
            gr.safe_branch_name(bad)


def test_bundle_existing_branch(repo: Path, tmp_path: Path):
    before = snapshot(repo)
    bundle = gr.bundle_branch(repo, "feature", "HEAD")
    try:
        assert bundle.is_file()
        heads = sh(repo, "bundle", "list-heads", str(bundle))
        assert heads.splitlines() == [f"{gr.rev_parse(repo, 'feature')} refs/heads/feature"]
        assert snapshot(repo) == before
    finally:
        shutil.rmtree(bundle.parent, ignore_errors=True)


def test_bundle_new_branch_from_base(repo: Path):
    (repo / "dirty.txt").write_text("untracked\n")
    (repo / "a.txt").write_text("modified\n")
    before = snapshot(repo)
    bundle = gr.bundle_branch(repo, "brand-new", "main")
    try:
        heads = sh(repo, "bundle", "list-heads", str(bundle)).splitlines()
        assert heads == [f"{gr.rev_parse(repo, 'main')} refs/heads/brand-new"]
        assert snapshot(repo) == before  # no new branch, no temp refs, tree and index untouched
        assert "cloud-compute" not in sh(repo, "for-each-ref")
        assert not gr.ref_exists(repo, "refs/heads/brand-new")
        assert (repo / "a.txt").read_text() == "modified\n"
    finally:
        shutil.rmtree(bundle.parent, ignore_errors=True)


def test_bundle_to_explicit_out_and_bad_inputs(repo: Path, tmp_path: Path):
    out = tmp_path / "nested dir" / "x.bundle"
    assert gr.bundle_branch(repo, "feature", "HEAD", out) == out
    assert out.is_file()
    with pytest.raises(gr.GitError):
        gr.bundle_branch(repo, "bad..name", "HEAD")
    with pytest.raises(gr.GitError):
        gr.bundle_branch(repo, "fresh", "no-such-base")
    assert "cloud-compute" not in sh(repo, "for-each-ref")


def test_bundle_bytes(repo: Path):
    data, sha = gr.bundle_bytes(repo, "feature", "HEAD")
    assert data.startswith(b"# v2 git bundle") or data.startswith(b"# v3 git bundle")
    assert sha == gr.rev_parse(repo, "feature")
    data2, sha2 = gr.bundle_bytes(repo, "newbr", "main")
    assert sha2 == gr.rev_parse(repo, "main")
    assert b"refs/heads/newbr" in data2


def test_round_trip_and_worktree(repo: Path, tmp_path: Path):
    bundle = gr.bundle_branch(repo, "work", "main")
    sandbox = clone_to(bundle, tmp_path / "sandbox")
    shutil.rmtree(bundle.parent, ignore_errors=True)
    sh(sandbox, "checkout", "-q", "work")
    new_sha = commit(sandbox, "agent.txt", "from sandbox\n", "agent change")
    back = tmp_path / "back.bundle"
    sh(sandbox, "bundle", "create", str(back), "refs/heads/work")

    before_refs = sh(repo, "for-each-ref", "--format=%(refname)")
    refs = gr.import_bundle(repo, back, SANDBOX_PREFIX)
    assert refs == [SANDBOX_PREFIX + "work"]
    assert gr.rev_parse(repo, refs[0]) == new_sha
    after_refs = sh(repo, "for-each-ref", "--format=%(refname)").splitlines()
    assert set(after_refs) - set(before_refs.splitlines()) == {refs[0]}
    assert not gr.ref_exists(repo, "refs/heads/work")  # user's namespace untouched

    # re-import identical is fine
    assert gr.import_bundle(repo, back, SANDBOX_PREFIX) == refs

    wt = tmp_path / "wt dir"
    assert gr.create_worktree(repo, refs[0], wt) == wt
    assert (wt / "agent.txt").read_text() == "from sandbox\n"
    with pytest.raises(gr.GitError):
        gr.create_worktree(repo, refs[0], wt)  # non-empty path refused
    sh(repo, "worktree", "remove", "--force", str(wt))


def test_create_worktree_detached_and_unknown(repo: Path, tmp_path: Path):
    wt = tmp_path / "det"
    gr.create_worktree(repo, "main", wt)
    assert (wt / "a.txt").exists()
    sh(repo, "worktree", "remove", "--force", str(wt))
    with pytest.raises(gr.GitError):
        gr.create_worktree(repo, "refs/heads/zzz", tmp_path / "w2")


def test_incremental_bundle_with_prerequisite(repo: Path, tmp_path: Path):
    bundle = gr.bundle_branch(repo, "work", "main")
    sandbox = clone_to(bundle, tmp_path / "sandbox")
    shutil.rmtree(bundle.parent, ignore_errors=True)
    sh(sandbox, "checkout", "-q", "work")
    base_sha = sh(sandbox, "rev-parse", "HEAD")
    c1 = commit(sandbox, "one.txt", "1\n")
    c2 = commit(sandbox, "two.txt", "2\n")
    inc = tmp_path / "inc.bundle"
    sh(sandbox, "bundle", "create", str(inc), f"{base_sha}..refs/heads/work", "refs/heads/work")
    assert f"-{base_sha}".encode() in inc.read_bytes()[:400]  # prerequisite line in the header
    refs = gr.import_bundle(repo, inc, SANDBOX_PREFIX)
    assert gr.rev_parse(repo, refs[0]) == c2
    assert gr.ahead_behind(repo, refs[0], "main") == (2, 0)
    assert c1 in sh(repo, "rev-list", refs[0])


def test_import_bytes(repo: Path, tmp_path: Path):
    data, _ = gr.bundle_bytes(repo, "feature", "HEAD")
    refs = gr.import_bundle_bytes(repo, data, "refs/cloud-compute/runs/r1/")
    assert refs == ["refs/cloud-compute/runs/r1/feature"]
    assert gr.rev_parse(repo, refs[0]) == gr.rev_parse(repo, "feature")


@pytest.mark.parametrize("prefix", [
    "refs/heads/", "refs/heads/cloud", "refs/heads/other/", "refs/tags/", "refs/",
    "heads/cloud/", "refs/heads/cloud/../x/", "refs/heads/cloud//x/", "", "refs/remotes/origin/",
    "refs/heads/cloud/a b/", "refs/cloud-computeX/",
])
def test_import_refuses_bad_prefix(repo: Path, tmp_path: Path, prefix: str):
    bundle = gr.bundle_branch(repo, "feature", "HEAD", tmp_path / "f.bundle")
    before = sh(repo, "for-each-ref")
    with pytest.raises(gr.GitError):
        gr.import_bundle(repo, bundle, prefix)
    with pytest.raises(gr.GitError):
        gr.import_bundle_bytes(repo, bundle.read_bytes(), prefix)
    assert sh(repo, "for-each-ref") == before


def test_import_refuses_non_fast_forward(repo: Path, tmp_path: Path):
    b1 = gr.bundle_branch(repo, "feature", "HEAD", tmp_path / "1.bundle")
    refs = gr.import_bundle(repo, b1, SANDBOX_PREFIX)
    original = gr.rev_parse(repo, refs[0])

    # a different, unrelated history under the same branch name
    other = init_repo(tmp_path / "other")
    commit(other, "z.txt", "z\n")
    sh(other, "branch", "-m", "feature")
    b2 = tmp_path / "2.bundle"
    sh(other, "bundle", "create", str(b2), "refs/heads/feature")
    with pytest.raises(gr.GitError):
        gr.import_bundle(repo, b2, SANDBOX_PREFIX)
    assert gr.rev_parse(repo, refs[0]) == original


def test_import_rejects_corrupt_bundle(repo: Path, tmp_path: Path):
    bad = tmp_path / "bad.bundle"
    bad.write_bytes(b"not a bundle")
    with pytest.raises(gr.GitError):
        gr.import_bundle(repo, bad, SANDBOX_PREFIX)


def test_ahead_behind(repo: Path):
    # feature == main
    assert gr.ahead_behind(repo, "feature", "main") == (0, 0)
    # ahead only
    sh(repo, "checkout", "-q", "feature")
    commit(repo, "f1.txt", "f1\n")
    commit(repo, "f2.txt", "f2\n")
    assert gr.ahead_behind(repo, "feature", "main") == (2, 0)
    # behind only
    assert gr.ahead_behind(repo, "main", "feature") == (0, 2)
    # diverged
    sh(repo, "checkout", "-q", "main")
    commit(repo, "m1.txt", "m1\n")
    assert gr.ahead_behind(repo, "feature", "main") == (2, 1)
    with pytest.raises(gr.GitError):
        gr.ahead_behind(repo, "feature", "missing")


def _merge_setup(repo: Path, conflict: bool) -> None:
    sh(repo, "checkout", "-q", "-b", "theirs", "main")
    commit(repo, "a.txt" if conflict else "only-theirs.txt", "theirs\n")
    sh(repo, "checkout", "-q", "main")
    commit(repo, "a.txt" if conflict else "only-ours.txt", "ours\n")


@pytest.mark.parametrize("fallback", [False, True])
def test_merge_conflicts_dry_run(repo: Path, monkeypatch, fallback: bool):
    if fallback:
        monkeypatch.setattr(gr, "git_version", lambda: (2, 30, 0))
    _merge_setup(repo, conflict=False)
    (repo / "scratch.txt").write_text("keep me\n")
    before = snapshot(repo)
    assert gr.merge_conflicts_dry_run(repo, "main", "theirs") == []
    assert snapshot(repo) == before
    assert (repo / "scratch.txt").read_text() == "keep me\n"

    sh(repo, "checkout", "-q", "-B", "theirs2", "main~1")
    commit(repo, "a.txt", "theirs-different\n")
    sh(repo, "checkout", "-q", "main")
    commit(repo, "a.txt", "ours-different\n")
    before = snapshot(repo)
    assert gr.merge_conflicts_dry_run(repo, "main", "theirs2") == ["a.txt"]
    assert snapshot(repo) == before


def test_merge_conflict_path_with_spaces(repo: Path):
    sh(repo, "checkout", "-q", "-b", "t", "main")
    commit(repo, "dir x/file y.txt", "t\n")
    sh(repo, "checkout", "-q", "main")
    commit(repo, "dir x/file y.txt", "m\n")
    assert gr.merge_conflicts_dry_run(repo, "main", "t") == ["dir x/file y.txt"]


def test_bundle_has_no_remote_urls_or_credentials(repo: Path, tmp_path: Path):
    secret_url = "https://user:s3cr3t-token@secret-host.test/org/repo.git"
    sh(repo, "remote", "add", "origin", secret_url)
    sh(repo, "config", "credential.helper", "store --file=/nonexistent")
    for branch, base in (("feature", "HEAD"), ("fresh", "main")):
        data, _ = gr.bundle_bytes(repo, branch, base)
        assert b"s3cr3t-token" not in data
        assert b"secret-host.test" not in data
        assert b"credential" not in data
        assert b"[remote" not in data
    bundle = gr.bundle_branch(repo, "feature", "HEAD", tmp_path / "x.bundle")
    clone = clone_to(bundle, tmp_path / "clone")
    cfg = (clone / ".git" / "config").read_text(encoding="utf-8")
    assert "s3cr3t-token" not in cfg and "secret-host.test" not in cfg
    assert "credential" not in cfg


# --------------------------------------------------------------------------- no console window


class _Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(dict(kwargs, argv=list(argv)))
        return subprocess.CompletedProcess(argv, 0, stdout=b"git version 2.45.1\n", stderr=b"")


def test_git_calls_hide_the_console_window_on_windows(monkeypatch, tmp_path):
    rec = _Recorder()
    monkeypatch.setattr(gr, "_is_windows", lambda: True)
    monkeypatch.setattr(gr.subprocess, "run", rec)
    gr.git(tmp_path, "status")
    gr.git_version()
    gr.safe_branch_name("some goal")                         # runs git check-ref-format
    assert len(rec.calls) >= 3
    for call in rec.calls:
        assert call["creationflags"] == gr._CREATE_NO_WINDOW == 0x08000000, call["argv"]


def test_git_calls_pass_no_creationflags_elsewhere(monkeypatch, tmp_path):
    rec = _Recorder()
    monkeypatch.setattr(gr, "_is_windows", lambda: False)
    monkeypatch.setattr(gr.subprocess, "run", rec)
    gr.git(tmp_path, "status")
    gr.git_version()
    assert rec.calls and all("creationflags" not in c for c in rec.calls)


def test_every_subprocess_call_goes_through_the_flagged_helper():
    src = Path(gr.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"subprocess\.(?:run|Popen|call|check_output|check_call)\(", src)) == 1


# --------------------------------------------------------------------------- re-import into a checked-out branch


def _sandbox_with_worktree(repo: Path, tmp_path: Path):
    """A sandbox clone, its first result imported and checked out in a worktree."""
    bundle = gr.bundle_branch(repo, "work", "main")
    sandbox = clone_to(bundle, tmp_path / "sandbox")
    shutil.rmtree(bundle.parent, ignore_errors=True)
    sh(sandbox, "checkout", "-q", "work")
    commit(sandbox, "agent.txt", "one\n", "agent one")
    first = tmp_path / "first.bundle"
    sh(sandbox, "bundle", "create", str(first), "refs/heads/work")
    (ref,) = gr.import_bundle(repo, first, SANDBOX_PREFIX)
    wt = tmp_path / "wt"
    gr.create_worktree(repo, ref, wt)
    return sandbox, ref, wt, first


def _bundle_of(sandbox: Path, tmp_path: Path, name: str) -> Path:
    out = tmp_path / name
    sh(sandbox, "bundle", "create", str(out), "refs/heads/work")
    return out


def test_reimport_into_checked_out_branch_is_idempotent(repo: Path, tmp_path: Path):
    sandbox, ref, wt, first = _sandbox_with_worktree(repo, tmp_path)
    tip = gr.rev_parse(repo, ref)
    # Plain git refuses this; the relay does not need to fetch at all.
    (item,) = gr.import_bundle_detailed(repo, first, SANDBOX_PREFIX)
    assert (item.ref, item.status, item.sha) == (ref, "unchanged", tip) and item.ok
    assert gr.import_bundle(repo, first, SANDBOX_PREFIX) == [ref]
    assert gr.rev_parse(repo, ref) == tip
    assert not sh(repo, "for-each-ref", "refs/cloud-compute/")
    assert sh(wt, "status", "--porcelain=v1") == ""


def test_reimport_with_new_commits_fast_forwards_the_worktree(repo: Path, tmp_path: Path):
    sandbox, ref, wt, _ = _sandbox_with_worktree(repo, tmp_path)
    new = commit(sandbox, "more.txt", "two\n", "agent two")
    (item,) = gr.import_bundle_detailed(repo, _bundle_of(sandbox, tmp_path, "2.bundle"), SANDBOX_PREFIX)
    assert (item.status, item.sha) == ("updated", new) and item.ok
    assert Path(item.worktree).resolve() == wt.resolve()
    assert gr.rev_parse(repo, ref) == new
    assert sh(wt, "rev-parse", "HEAD") == new
    assert (wt / "more.txt").read_text() == "two\n"           # the worktree files followed
    assert sh(wt, "status", "--porcelain=v1") == ""
    assert not sh(repo, "for-each-ref", "refs/cloud-compute/")  # no temporary ref left behind
    assert gr.rev_parse(repo, "main") == sh(repo, "rev-parse", "main")


def test_reimport_new_commits_without_a_worktree_fast_forwards_the_ref(repo: Path, tmp_path: Path):
    sandbox, ref, wt, _ = _sandbox_with_worktree(repo, tmp_path)
    sh(repo, "worktree", "remove", "--force", str(wt))
    new = commit(sandbox, "more.txt", "two\n", "agent two")
    (item,) = gr.import_bundle_detailed(repo, _bundle_of(sandbox, tmp_path, "2.bundle"), SANDBOX_PREFIX)
    assert (item.status, item.worktree) == ("updated", None)
    assert gr.rev_parse(repo, ref) == new


def test_reimport_diverged_worktree_changes_nothing_and_keeps_the_tip(repo: Path, tmp_path: Path):
    sandbox, ref, wt, _ = _sandbox_with_worktree(repo, tmp_path)
    mine = commit(wt, "mine.txt", "local\n", "my own commit")        # the user committed in the worktree
    theirs = commit(sandbox, "more.txt", "two\n", "agent two")
    bundle = _bundle_of(sandbox, tmp_path, "2.bundle")
    refs_before = sh(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/")

    (item,) = gr.import_bundle_detailed(repo, bundle, SANDBOX_PREFIX)
    assert item.status == "diverged" and not item.ok
    assert item.worktree in item.message and "nothing was changed" in item.message
    assert item.incoming_ref == "refs/cloud-compute/incoming/heads/cloud/a-0000abcd/work"
    assert item.incoming_ref in item.message
    assert gr.rev_parse(repo, ref) == mine                            # never forced
    assert sh(wt, "rev-parse", "HEAD") == mine and not (wt / "more.txt").exists()
    assert gr.rev_parse(repo, item.incoming_ref) == theirs            # the sandbox commits are not lost
    assert sh(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/") == refs_before
    assert sh(repo, "for-each-ref", "--format=%(refname)", "refs/cloud-compute/").splitlines() \
        == [item.incoming_ref]                                        # and no temporary ref remains

    with pytest.raises(gr.GitError) as exc:                           # the list API still refuses
        gr.import_bundle(repo, bundle, SANDBOX_PREFIX)
    assert "nothing was changed" in str(exc.value)
    assert gr.rev_parse(repo, ref) == mine


def test_reimport_blocked_by_local_changes_in_the_worktree(repo: Path, tmp_path: Path):
    sandbox, ref, wt, _ = _sandbox_with_worktree(repo, tmp_path)
    old = gr.rev_parse(repo, ref)
    commit(sandbox, "agent.txt", "changed by the agent\n", "agent edits agent.txt")
    (wt / "agent.txt").write_text("edited by the user\n", encoding="utf-8")   # uncommitted
    (item,) = gr.import_bundle_detailed(repo, _bundle_of(sandbox, tmp_path, "2.bundle"), SANDBOX_PREFIX)
    assert item.status == "blocked" and not item.ok
    assert "Commit or stash" in item.message and item.incoming_ref in item.message
    assert gr.rev_parse(repo, ref) == old
    assert (wt / "agent.txt").read_text() == "edited by the user\n"


def test_worktree_for_ref(repo: Path, tmp_path: Path):
    _, ref, wt, _ = _sandbox_with_worktree(repo, tmp_path)
    assert Path(gr.worktree_for_ref(repo, ref)).resolve() == wt.resolve()
    assert gr.worktree_for_ref(repo, "refs/heads/feature") is None
    assert gr.worktree_for_ref(repo, SANDBOX_PREFIX + "nope") is None


# --------------------------------------------------------------------------- minimal child environment


def test_git_children_get_a_minimal_environment(monkeypatch, tmp_path):
    for name, value in (("CC_AGENT_API_KEY", "x"), ("LLAMA_SWAP_API_KEY", "x"),
                        ("GITHUB_TOKEN", "x"), ("GIT_ASKPASS", "x"), ("GIT_DIR", "x")):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "UTC")
    rec = _Recorder()
    monkeypatch.setattr(gr.subprocess, "run", rec)
    gr.git(tmp_path, "status", _env_extra={"GIT_INDEX_FILE": "idx"})
    env = rec.calls[0]["env"]
    assert "CC_AGENT_API_KEY" not in env and "LLAMA_SWAP_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env and "GIT_ASKPASS" not in env and "GIT_DIR" not in env
    assert env["PATH"] == "/usr/bin" and env["HOME"] == str(tmp_path) and env["TZ"] == "UTC"
    # Only the variables the code itself sets for git are added.
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["LC_ALL"] == "C" and env["GIT_INDEX_FILE"] == "idx"
    allowed = gr._ENV_ALLOWLIST | {"GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_INDEX_FILE"}
    assert {k.upper() for k in env} <= allowed


def test_git_env_allowlist_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(gr.os, "environ", {"Path": "C:/bin", "SystemRoot": "C:/Windows",
                                           "Secret_Token": "x"})
    env = gr._env()
    assert env["Path"] == "C:/bin" and env["SystemRoot"] == "C:/Windows" and "Secret_Token" not in env
