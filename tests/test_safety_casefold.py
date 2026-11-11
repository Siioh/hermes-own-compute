"""The folder guard on file systems that ignore letter case (macOS, Windows).

On macOS ``~/.SSH/id_ed25519`` is the same file as ``~/.ssh/id_ed25519`` and a name may be
stored in another Unicode normal form than typed, yet a ``pathlib`` comparison there is exact.
Part 1 tests the comparison helper with a simulated platform (runs everywhere, on pure
paths). Part 2 uses real files and runs only where the temp folder really ignores case.
"""

from __future__ import annotations

import os
import sys
import types
import unicodedata
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from cloud_compute_core import files as files_mod
from cloud_compute_core import registry
from cloud_compute_core.files import FileManager, _is_within, _Roots, _same_place
from cloud_compute_core.interfaces import ApiError

P = PurePosixPath
MAC = {"case_insensitive": True}
LINUX = {"case_insensitive": False}
NFC = unicodedata.normalize("NFC", "caf\u00e9")
NFD = unicodedata.normalize("NFD", "caf\u00e9")


# ------------------------------------------------------------------ part 1: simulated platform


@pytest.mark.parametrize("path,root", [
    ("/Users/me/.SSH/id_ed25519", "/Users/me/.ssh"),
    ("/Users/me/.ssh/id_ed25519", "/Users/me/.SSH"),
    ("/Users/ME/.Ssh", "/users/me/.ssh"),
    ("/Volumes/Data/HERMES/PROFILES/x/.env", "/Volumes/Data/hermes"),
    ("/Volumes/Data/hermes/PROFILES/x", "/Volumes/Data/hermes/profiles/x"),
    ("/Users/me/.ssh", "/Users/me/.ssh"),
], ids=["upper-ssh", "upper-root", "mixed-user", "mixed-hermes-root", "profile-case", "same"])
def test_case_insensitive_platform_sees_other_case_spellings_as_inside(path, root):
    assert _is_within(P(path), P(root), **MAC) is True


def test_nfc_and_nfd_spellings_are_the_same_name_on_a_normalising_platform():
    assert NFC != NFD
    assert _is_within(P(f"/Users/me/{NFD}/k"), P(f"/Users/me/{NFC}"), **MAC) is True
    assert _is_within(P(f"/Users/me/{NFC}/k"), P(f"/Users/me/{NFD}"), **MAC) is True
    assert _is_within(P(f"/Users/me/{NFD.upper()}/k"), P(f"/Users/me/{NFC}"), **MAC) is True


@pytest.mark.parametrize("path,root", [
    ("/Users/me/.SSH/id_ed25519", "/Users/me/.ssh"),
    (f"/Users/me/{NFD}/k", f"/Users/me/{NFC}"),
    ("/Users/me/.ssh2/k", "/Users/me/.ssh"),
    ("/Users/me/project", "/Users/me/.ssh"),
    ("/Users/me", "/Users/me/.ssh"),
], ids=["case", "normal-form", "longer-name", "elsewhere", "parent-of-root"])
def test_linux_stays_exact_and_unrelated_paths_stay_outside(path, root):
    assert _is_within(P(path), P(root), **LINUX) is False


def test_unrelated_and_lookalike_paths_are_outside_on_a_case_insensitive_platform():
    for path in ("/Users/me/.ssh2/k", "/Users/me/project/.SSH/k", "/Users/me/ssh/k",
                 "/Users/me", "/Users/other/.ssh/k"):
        assert _is_within(P(path), P("/Users/me/.ssh"), **MAC) is False


def test_exact_spelling_is_inside_on_every_platform():
    assert _is_within(P("/Users/me/.ssh/k"), P("/Users/me/.ssh"), **LINUX) is True
    assert _is_within(P("/Users/me/.ssh/k"), P("/Users/me/.ssh"), **MAC) is True


def test_several_roots_and_same_place():
    roots = _Roots([P("/h/.ssh"), P("/Hermes/Root")], **MAC)
    assert roots.contains(P("/h/.SSH/k")) and roots.contains(P("/hermes/root/PROFILES/x"))
    assert not roots.contains(P("/h/project"))
    assert _same_place(P("/a/B"), P("/a/b"), **MAC) is True
    assert _same_place(P("/a/B"), P("/a/b"), **LINUX) is False


def test_the_platform_default(monkeypatch):
    monkeypatch.setattr(files_mod.sys, "platform", "darwin")
    assert files_mod._case_insensitive_fs() is True
    monkeypatch.setattr(files_mod.sys, "platform", "linux")
    monkeypatch.setattr(files_mod.os, "name", "posix")
    assert files_mod._case_insensitive_fs() is False
    monkeypatch.setattr(files_mod.os, "name", "nt")
    assert files_mod._case_insensitive_fs() is True


# ------------------------------------------------------------------ part 2: a real file system


def _ignores_case(folder: Path) -> bool:
    probe = folder / "CaseProbe.tmp"
    probe.write_text("x")
    try:
        return (folder / "caseprobe.TMP").exists()
    finally:
        probe.unlink()


@pytest.fixture
def real_world(tmp_path, monkeypatch):
    if not _ignores_case(tmp_path):
        pytest.skip("the temp folder is on a case-sensitive file system (Linux): "
                    "`.SSH` really is another folder there")
    from cloud_compute_core.store import Store

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("key")
    root = tmp_path / "hermes"
    profile = root / "profiles" / "p"
    profile.mkdir(parents=True)
    (root / ".env").write_text("SECRET=1")
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("a")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: profile
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    store = Store(tmp_path / "data" / "cloud-compute.db")
    services = registry.build_services(store=store, config=SimpleNamespace(export_max_mb=10))
    yield SimpleNamespace(fm=FileManager(services), home=home, root=root, profile=profile,
                          project=project, tmp=tmp_path)
    store.close()


def _refused(call):
    with pytest.raises(ApiError) as info:
        call()
    assert info.value.status in (403, 400)
    return info.value.message


def test_real_other_case_ssh_folder_is_refused(real_world):
    w = real_world
    shouted = str(w.home / ".SSH" / "id_ed25519")
    assert "SSH folder" in _refused(lambda: w.fm._local_source(shouted))
    assert _refused(lambda: w.fm._check_folder(str(w.home / ".SSH"), "dest_dir"))
    assert w.fm._privacy_rule()(w.home / ".SSH" / "id_ed25519") is False


def test_real_other_case_hermes_root_is_refused(real_world):
    w = real_world
    shouted = w.tmp / "HERMES" / "PROFILES" / "p"
    assert "Hermes home" in _refused(lambda: w.fm._local_source(str(w.tmp / "HERMES" / ".ENV")))
    assert _refused(lambda: w.fm._check_folder(str(shouted), "dest_dir"))
    assert _refused(lambda: w.fm._check_folder(str(w.tmp / "Hermes"), "dest_dir"))
    assert w.fm._privacy_rule()(shouted / "x.txt") is False
    name = "box-20260101T000000Z-manual.tar.gz"
    (w.root / name).write_bytes(b"x")
    assert w.fm._deletable(w.tmp / "HERMES" / name) is False


def test_real_a_path_that_does_not_exist_yet_is_compared_by_name(real_world):
    w = real_world
    assert w.fm._privacy_rule()(w.home / ".SSH" / "not-yet" / "k") is False
    assert w.fm._privacy_rule()(w.tmp / "HERMES" / "new" / "k") is False
    assert _refused(lambda: w.fm._check_folder(str(w.home / ".SSH" / "new"), "dest_dir"))


def test_real_the_plugins_own_exports_folder_and_other_places_stay_allowed(real_world):
    w = real_world
    # The same file: Windows hands back the case on disk, macOS keeps the case that was typed.
    assert os.path.samefile(w.fm._local_source(str(w.project / "A.TXT")), w.project / "a.txt")
    assert w.fm._check_folder(str(w.tmp / "PROJECT"), "dest_dir")
    exports = w.fm._default_exports_dir()
    exports.mkdir(parents=True, exist_ok=True)
    assert w.fm._check_folder(str(exports), "dest_dir") == exports
    assert w.fm._privacy_rule()(exports / "a.tar.gz") is True
    assert w.fm._privacy_rule()(w.project / "a.txt") is True


def test_real_same_file_check_uses_the_file_system(real_world):
    w = real_world
    assert _is_within(w.home / ".SSH" / "id_ed25519", w.home / ".ssh", case_insensitive=False)
    assert _same_place(w.home / ".SSH", w.home / ".ssh", case_insensitive=False)
    assert not _is_within(w.project / "a.txt", w.home / ".ssh", case_insensitive=False)
    assert os.path.samefile(w.home / ".SSH", w.home / ".ssh")
