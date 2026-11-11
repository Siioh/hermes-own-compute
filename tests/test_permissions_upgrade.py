"""Folders and archives saved by an older build are tightened to owner-only (POSIX).

Fresh data is already private; an upgrade must not leave the exports folder, the per-environment
folders and the archives of an older build at 0755/0644, nor the host-side cache folder where
env files are written at 0755. Folders the user chose are never touched, only what the plugin
created in them. Windows is left to the user profile's ACLs.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from cloud_compute_core import sandbox as sandbox_mod
from cloud_compute_core.testing import sample_instance

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions; Windows uses ACLs")

_spec = importlib.util.spec_from_file_location(
    "files_manager_fakes2", Path(__file__).with_name("test_files_manager.py"))
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)
env = _fakes.env            # the container-filesystem fixture of test_files_manager


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def loosen(path: Path, value: int) -> None:
    path.chmod(value)


@posix_only
def test_old_exports_folders_and_archives_become_owner_only(env):
    rec = env.fm.export("i-ab12cd34")
    archive = Path(rec["local_path"])
    env_folder, root = archive.parent, archive.parent.parent
    unpacked = env_folder / "unpacked-20260101"
    (unpacked / "inner").mkdir(parents=True)
    (unpacked / "inner" / "a.txt").write_text("keep my mode")
    stray = env_folder / "notes.txt"                     # not an archive: not ours to change
    stray.write_text("x")
    for path, value in ((root, 0o755), (env_folder, 0o755), (archive, 0o644), (unpacked, 0o755),
                        (unpacked / "inner", 0o755), (unpacked / "inner" / "a.txt", 0o644),
                        (stray, 0o644)):
        loosen(path, value)
    env.fm._tightened = False
    env.fm.exports_dir()                                  # first use after the upgrade
    assert (mode(root), mode(env_folder), mode(unpacked)) == (0o700, 0o700, 0o700)
    assert mode(archive) == 0o600
    assert mode(unpacked / "inner") == 0o755 and mode(unpacked / "inner" / "a.txt") == 0o644
    assert mode(stray) == 0o644


@posix_only
def test_a_folder_the_user_chose_is_never_tightened_only_the_plugin_files_in_it(env, tmp_path):
    custom = tmp_path / "my-saves"
    custom.mkdir()
    env.services.config.exports_dir = str(custom)
    rec = env.fm.export("i-ab12cd34")                    # lands in my-saves/<name>-<id>/
    archive = Path(rec["local_path"])
    assert archive.parent.parent == custom.resolve()
    here = tmp_path / "dest"
    here.mkdir()
    fetched = env.fm.export("i-ab12cd34", dest_dir=str(here), extract=True)
    other = custom / "mine.txt"
    other.write_text("mine")
    mine = here / "photo.jpg"
    mine.write_text("mine too")
    plugin_archive = Path(fetched["local_path"])
    unpacked = Path(fetched["extracted_to"])
    for path in (custom, here, archive.parent, archive, plugin_archive, unpacked, other, mine):
        loosen(path, 0o755 if path.is_dir() else 0o644)
    env.fm._tightened = False
    env.fm.exports_dir()
    assert mode(custom) == 0o755 and mode(here) == 0o755          # the user's folders
    assert mode(other) == 0o644 and mode(mine) == 0o644           # the user's files
    assert mode(archive) == 0o600 and mode(plugin_archive) == 0o600
    assert mode(archive.parent) == 0o700 and mode(unpacked) == 0o700


@posix_only
def test_entries_owned_by_someone_else_are_left_alone(env, monkeypatch):
    rec = env.fm.export("i-ab12cd34")
    archive = Path(rec["local_path"])
    loosen(archive.parent, 0o755)
    loosen(archive, 0o644)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(archive).st_uid + 1)
    assert env.fm.tighten_saved_copies() == 0
    assert mode(archive) == 0o644 and mode(archive.parent) == 0o755


@posix_only
def test_symlinks_are_not_followed(env, tmp_path):
    rec = env.fm.export("i-ab12cd34")
    folder = Path(rec["local_path"]).parent
    target = tmp_path / "elsewhere"
    target.mkdir()
    loosen(target, 0o755)
    (folder.parent / "link-i-00000000").symlink_to(target, target_is_directory=True)
    (folder / "x.tar.gz").symlink_to(target / "nothing")
    env.fm.tighten_saved_copies()
    assert mode(target) == 0o755


@posix_only
def test_tightening_twice_changes_nothing_more(env):
    env.fm.export("i-ab12cd34")
    env.fm.tighten_saved_copies()
    assert env.fm.tighten_saved_copies() == 0


def test_windows_is_left_alone(env, monkeypatch):
    rec = env.fm.export("i-ab12cd34")
    archive = Path(rec["local_path"])
    before = archive.stat().st_mode
    monkeypatch.setattr(os, "name", "nt")
    assert env.fm.tighten_saved_copies() == 0
    monkeypatch.undo()
    assert archive.stat().st_mode == before


@posix_only
def test_the_host_side_cache_folder_is_made_owner_only(tmp_path):
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no POSIX sh")
    home = tmp_path / "home"
    cache = home / ".cache" / "cloud-compute"
    cache.mkdir(parents=True)
    cache.chmod(0o755)                                   # left by an older build
    res = subprocess.run([sh, "-c", sandbox_mod._WRITE_ENV, "sh", "i-1"], input=b"A=1\n",
                         capture_output=True, env={"HOME": str(home), "PATH": os.environ["PATH"]},
                         timeout=30)
    assert res.returncode == 0, res.stderr
    assert mode(cache) == 0o700
    written = Path(res.stdout.decode().strip().splitlines()[-1])
    assert written.read_bytes() == b"A=1\n" and mode(written) == 0o600


def test_write_env_script_asks_for_owner_only_after_mkdir():
    script = sandbox_mod._WRITE_ENV
    assert script.index("mkdir -p") < script.index('chmod 700 "$d"')
    assert "|| :" in script                              # a folder that is not ours must not stop the run
