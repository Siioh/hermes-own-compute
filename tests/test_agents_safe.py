"""The model key never lands in a saved copy of a cloud agent's work folder.

Part 1: the agent's Hermes home and run state live outside ``/work``. Part 2: saves made by the
plugin leave the agent-internal folders out of the archive even when an old sandbox still has
them below ``/work`` (and say so), and never do that for an ordinary environment.
"""

from __future__ import annotations

import importlib.util
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_compute_core import agents as agents_mod
from cloud_compute_core import files as files_mod
from cloud_compute_core.agents import AgentRun
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.testing import sample_instance


def _load(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


_am = _load("cloud_compute_tests_agents_manager", "test_agents_manager.py")
_fm = _load("cloud_compute_tests_files_manager", "test_files_manager.py")
SECRET_VALUE, KEY_NAME, KEY_ENV = _am.SECRET_VALUE, _am.KEY_NAME, agents_mod.KEY_ENV
needs_shell, _wait_done = _am.needs_shell, _am._wait_done
env = _am.env  # noqa: F811 - the shared agent fixture
FakeFS, _members = _fm.FakeFS, _fm._members


@pytest.fixture
def files_env(tmp_path):
    """The file-manager fixture of test_files_manager (own copy: both are called ``env``)."""
    from cloud_compute_core import registry
    from cloud_compute_core.files import FileManager
    from cloud_compute_core.store import Store
    from cloud_compute_core.testing import sample_host

    data = tmp_path / "data"
    store = Store(data / "cloud-compute.db")
    fs = FakeFS({"/work": None, "/work/results": None, "/work/results/a.txt": b"hello\n"})
    transport = _fm.FakeStreamTransport(sample_host(), fs)
    services = registry.build_services(
        store=store, transports=_fm.Factory(transport), sandboxes=_fm.FakeSandboxes(),
        templates=_fm.FakeTemplates(), config=SimpleNamespace(export_max_mb=10))
    store.upsert_host(sample_host())
    store.upsert_instance(sample_instance())
    yield SimpleNamespace(fm=FileManager(services), fs=fs, t=transport, store=store,
                          services=services, tmp=data)
    store.close()

#: A Hermes shim that behaves like the live one: it writes the shell snapshot, which holds the
#: whole exported environment (the key included), below its Hermes home.
SNAP_SHIM = r"""#!/bin/sh
mkdir -p "$HERMES_HOME/cache/scratch"
env > "$HERMES_HOME/cache/scratch/hermes-snap-0123456789ab.sh"
echo "turn" >> NOTES.md
git add NOTES.md && git commit -q -m "agent turn"
echo "Done."
echo "session_id: 20261110_000000_abc1" >&2
exit 0
"""


def _all_bytes(root: Path) -> bytes:
    out = b""
    for p in root.rglob("*"):
        if p.is_file():
            out += p.read_bytes()
    return out


# --------------------------------------------------------------------------- A.1 agents


@needs_shell
def test_hermes_home_and_run_state_are_outside_the_saved_work_folder(env, tmp_path):
    e = env
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    shim = bin_dir / "hermes"
    shim.write_bytes(SNAP_SHIM.encode())
    shim.chmod(0o755)
    e.emulator.extra_path = bin_dir
    _am._configure_hermes(e)
    e.secrets.set(KEY_NAME, SECRET_VALUE)
    run = e.mgr.spawn("Write notes", "x1carbon", repo_path=str(e.repo))
    assert run.status == "running", run.error
    done = _wait_done(e.mgr, run.id)
    assert done.status == "done", done.last_log
    assert done.state_dir == "/home/agent/.cc-agent"

    work = e.sandboxes.work_dir(run.instance_id)
    # The work folder (what Stop/Delete saves) holds the repo only: no agent folder, no key.
    assert sorted(p.name for p in work.iterdir()) == ["repo"]
    assert SECRET_VALUE.encode() not in _all_bytes(work)
    # The snapshot with the key exists, but under the home, which is never saved.
    home = e.sandboxes.hermes_home(run.instance_id)
    snaps = list((home / "cache" / "scratch").glob("hermes-snap-*.sh"))
    assert snaps and SECRET_VALUE.encode() in snaps[0].read_bytes()
    assert e.sandboxes.state_dir(run.instance_id).is_dir()
    # and the repository (what Collect brings back) stays clean of agent files
    status = _am._git(work / "repo", "status", "--porcelain")
    assert status == ""


@needs_shell
def test_follow_up_resumes_in_the_moved_home(env, tmp_path):
    e = env
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    shim = bin_dir / "hermes"
    shim.write_bytes((SNAP_SHIM.replace(
        'echo "Done."', 'echo "args: $*"')).encode())
    shim.chmod(0o755)
    e.emulator.extra_path = bin_dir
    _am._configure_hermes(e)
    e.secrets.set(KEY_NAME, SECRET_VALUE)
    run = e.mgr.spawn("First", "x1carbon", repo_path=str(e.repo))
    _wait_done(e.mgr, run.id)
    e.mgr.message(run.id, "Second")
    done = _wait_done(e.mgr, run.id)
    assert done.turns == 2 and done.status == "done"
    assert "--resume 20261110_000000_abc1" in done.reply
    assert not (e.sandboxes.work_dir(run.instance_id) / ".cc-agent").exists()
    assert not (e.sandboxes.work_dir(run.instance_id) / ".hermes").exists()


def test_a_run_from_an_older_version_keeps_its_old_layout():
    old = AgentRun.from_dict({
        "id": "a-0000abcd", "instance_id": "i-1", "host_id": "h", "goal": "g",
        "repo_path": "/r", "branch": "b", "base_sha": "s", "head_sha": None, "commits_ahead": 0,
        "status": "running", "started_at": "2026-11-09T00:00:00Z", "ended_at": None,
        "session_id": None})
    assert old.state_dir is None
    assert agents_mod.state_dir_of(old) == "/work/.cc-agent"
    assert agents_mod.hermes_home_of(old) == "/work/.hermes"
    assert "export HERMES_HOME=/work/.hermes" in agents_mod._runner_script("a-0000abcd", "hermes", 3)
    new = agents_mod._runner_script("a-0000abcd", "hermes", 3, "/home/agent/.cc-agent")
    assert "export HERMES_HOME=/home/agent/.cc-agent/hermes" in new
    assert "/work/.hermes" not in new and "/work/.cc-agent" not in new


def test_internal_folder_lists_agree_between_agents_and_files():
    assert tuple(d.format(work="/work") for d in files_mod.AGENT_INTERNAL_DIRS) \
        == agents_mod.AGENT_INTERNAL


# --------------------------------------------------------------------------- A.2 saved copies


def _agent_fs() -> FakeFS:
    return FakeFS({
        "/work": None, "/work/repo": None, "/work/repo/main.py": b"print(1)\n",
        "/work/.gitignore": b"*.pyc\n",
        "/work/.hermes": None, "/work/.hermes/cache": None, "/work/.hermes/cache/scratch": None,
        "/work/.hermes/cache/scratch/hermes-snap-1.sh": f"{KEY_ENV}={SECRET_VALUE}\n".encode(),
        "/work/.hermes/config.yaml": b"model: x\n",
        "/work/.cc-agent": None, "/work/.cc-agent/log": f"{SECRET_VALUE}\n".encode(),
        "/home/agent": None, "/home/agent/.profile": b"# profile\n",
        "/home/agent/.cc-agent": None,
        "/home/agent/.cc-agent/log": SECRET_VALUE.encode(),
    })


@pytest.fixture
def agent_env(files_env):
    e = files_env
    e.fs.entries = _agent_fs().entries
    e.store.upsert_instance(sample_instance(id="i-aa000001", name="agent-a-12345678",
                                            template_id="hermes-agent"))
    return e


def _raw_bytes(path: str) -> bytes:
    import gzip
    with gzip.open(path, "rb") as fh:
        return fh.read()


def test_a_saved_copy_of_an_agent_sandbox_leaves_the_internal_folders_out(agent_env):
    e = agent_env
    out = e.fm.save_before_delete("i-aa000001")
    names = _members(out["export"]["local_path"])
    assert "work/repo/main.py" in names and "work/.gitignore" in names
    assert not [n for n in names if ".hermes" in n or ".cc-agent" in n]
    assert SECRET_VALUE.encode() not in _raw_bytes(out["export"]["local_path"])
    assert out["files"] == 2 and out["bytes"] == len(b"print(1)\n") + len(b"*.pyc\n")
    assert out["export"]["left_out"] == ["/work/.cc-agent", "/work/.hermes"]
    assert out["message"].endswith(
        "Left out of the saved copy: /work/.cc-agent, /work/.hermes. They are internal folders "
        "of the cloud agent and can hold the model key, so they are never saved. Only your "
        "files were saved.")
    assert "left_out" not in e.store.get_record(files_mod.EXPORT_KIND, out["export"]["id"])


def test_manual_save_and_fetch_of_an_agent_sandbox_are_filtered_too(agent_env):
    e = agent_env
    for call in (lambda: e.fm.export("i-aa000001"), lambda: e.fm.fetch("i-aa000001"),
                 lambda: e.fm.export("i-aa000001", reason="auto-delete")):
        rec = call()
        assert not [n for n in _members(rec["local_path"]) if ".hermes" in n]
        assert rec["note"].startswith("Left out of the saved copy")
        assert SECRET_VALUE.encode() not in _raw_bytes(rec["local_path"])


def test_the_new_home_location_is_filtered_and_refused_as_a_source(agent_env):
    e = agent_env
    rec = e.fm.export("i-aa000001", "/home/agent")
    assert not [n for n in _members(rec["local_path"]) if ".cc-agent" in n]
    assert rec["left_out"] == ["/home/agent/.cc-agent"]
    for path in ("/work/.hermes", "/work/.hermes/cache", "/home/agent/.cc-agent"):
        with pytest.raises(ApiError) as info:
            e.fm.fetch("i-aa000001", path)
        assert info.value.code == "invalid" and "never saved" in info.value.message
    with pytest.raises(ApiError):
        e.fm.fetch("i-aa000001", "/")


def test_a_user_template_with_an_agent_block_counts_as_an_agent(agent_env):
    e = agent_env
    e.store.upsert_instance(sample_instance(id="i-aa000002", name="my-helper",
                                            template_id="my-agent"))
    e.services.templates.get = lambda tid: SimpleNamespace(
        id=tid, workdir="/work", agent={"mode": "hermes"})
    rec = e.fm.export("i-aa000002")
    assert not [n for n in _members(rec["local_path"]) if ".hermes" in n]


def test_an_ordinary_environment_keeps_its_own_dot_folders(files_env):
    e = files_env
    e.fs.entries = _agent_fs().entries           # same files, but a python-dev environment
    out = e.fm.save_before_delete("i-ab12cd34")
    names = _members(out["export"]["local_path"])
    assert "work/.hermes/config.yaml" in names and "work/.cc-agent/log" in names
    assert "left_out" not in out["export"] and "Left out" not in out["message"]


def test_a_cut_off_stream_of_an_agent_sandbox_still_fails_and_keeps_nothing(agent_env):
    e = agent_env
    data = e.fs.tar_of("/work")
    e.t.out_override = data[:1536]                # whole blocks, but no end marker
    with pytest.raises(ApiError) as info:
        e.fm.save_before_delete("i-aa000001")
    assert "could not be read completely" in info.value.message
    assert not list((e.tmp / "exports").rglob("*.tar.gz"))
    e.t.out_override = data[:700]
    with pytest.raises(ApiError):
        e.fm.save_before_delete("i-aa000001")
    assert not list((e.tmp / "exports").rglob("*.tar.gz"))


def test_a_large_agent_save_streams_through_the_filter(agent_env):
    e = agent_env
    big = b"x" * (3 * 1024 * 1024)
    e.fs.entries["/work/repo/big.bin"] = big
    out = e.fm.save_before_delete("i-aa000001")
    with tarfile.open(out["export"]["local_path"], mode="r:gz") as tf:
        member = tf.extractfile("work/repo/big.bin")
        assert member is not None and member.read() == big
