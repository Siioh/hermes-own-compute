"""The folder guards for uploads, save destinations, folder
uploads and saved-copy deletion.

Windows spellings (extended-length ``\\\\?\\C:\\...``, device ``\\\\.\\``, network and
admin shares) must not get past the "never the SSH folder or the Hermes home" guard.
The guard also covers the whole Hermes root and every profile under it, not only the active
profile, except the plugin's own exports folder.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_compute_core import files as files_mod
from cloud_compute_core import registry
from cloud_compute_core.files import FileManager, is_special_windows_path
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.testing import sample_host, sample_instance

WINDOWS = os.name == "nt"
BS = chr(92)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Hermes root ``tmp/hermes`` with the active profile ``tmp/hermes/profiles/p``; the plugin
    data folder ``tmp/data``; a project folder outside all of them."""
    from cloud_compute_core.store import Store

    root = tmp_path / "hermes"
    profile = root / "profiles" / "p"
    other = root / "profiles" / "other"
    for folder in (profile, other, root / "skills"):
        folder.mkdir(parents=True)
    (root / ".env").write_text("SECRET=1")
    (root / "auth.json").write_text("{}")
    (other / "notes.txt").write_text("x")
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("a")
    store = Store(tmp_path / "data" / "cloud-compute.db")
    store.upsert_host(sample_host())
    store.upsert_instance(sample_instance())
    services = registry.build_services(store=store, config=SimpleNamespace(export_max_mb=10))
    fm = FileManager(services)
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: profile
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    yield SimpleNamespace(fm=fm, root=root, profile=profile, other=other, project=project,
                          fake=fake, data=tmp_path / "data", tmp=tmp_path)
    store.close()


def refused(call):
    with pytest.raises(ApiError) as info:
        call()
    assert info.value.status in (403, 400)
    return info.value.message


# ------------------------------------------------------------------ the Hermes root


@pytest.mark.parametrize("with_root_helper", [False, True], ids=["layout", "hermes-helper"])
def test_the_whole_hermes_root_and_every_profile_is_forbidden(world, with_root_helper):
    if with_root_helper:
        world.fake.get_default_hermes_root = lambda: world.root
    fm = world.fm
    for secret in (world.root / ".env", world.root / "auth.json", world.other / "notes.txt"):
        assert "Hermes home" in refused(lambda s=secret: fm._local_source(str(s)))
    for folder in (world.root, world.root / "skills", world.other, world.profile):
        assert "Hermes home or the SSH folder" in refused(
            lambda f=folder: fm._check_folder(str(f), "dest_dir"))
        assert fm._privacy_rule()(folder / "x.txt") is False
    assert fm._privacy_rule()(world.root / ".env") is False
    assert fm._local_source(str(world.project / "a.txt")) == (world.project / "a.txt").resolve()
    assert fm._check_folder(str(world.project), "dest_dir") == world.project.resolve()


def test_roots_are_listed_and_a_plain_home_is_not_widened(world):
    roots = world.fm._forbidden_roots()
    assert world.root.resolve() in roots and world.profile.resolve() in roots
    world.fake.get_hermes_home = lambda: world.tmp / "plainhome"      # no profiles layout
    roots = world.fm._forbidden_roots()
    assert (world.tmp / "plainhome").resolve() in roots
    assert world.root.resolve() not in roots and world.tmp.resolve() not in roots


def test_the_plugins_own_exports_folder_stays_allowed(world):
    exports = world.fm._default_exports_dir()
    exports.mkdir(parents=True, exist_ok=True)
    assert world.fm._check_folder(str(exports), "dest_dir") == exports
    assert world.fm._privacy_rule()(exports / "a.tar.gz") is True


def test_deletable_refuses_archives_inside_the_hermes_root_or_a_profile(world):
    name = "box-20260101T000000Z-manual.tar.gz"
    for folder in (world.root, world.other, world.profile):
        (folder / name).write_bytes(b"x")
        assert world.fm._deletable(folder / name) is False
    (world.project / name).write_bytes(b"x")
    assert world.fm._deletable(world.project / name) is True


def test_a_folder_upload_leaves_out_nothing_it_may_not_read(world):
    assert world.fm._privacy_rule()(world.other / "notes.txt") is False
    assert world.fm._privacy_rule()(world.project / "a.txt") is True


# ------------------------------------------------------------------ the special spellings

SPECIAL = [
    BS * 2 + "?" + BS + "C:" + BS + "Users" + BS + "me" + BS + ".ssh" + BS + "id_ed25519",
    BS * 2 + "." + BS + "C:" + BS + "Users" + BS + "me" + BS + "x",
    BS * 2 + "?" + BS + "UNC" + BS + "localhost" + BS + "C$" + BS + "Users" + BS + "me",
    BS * 2 + "localhost" + BS + "C$" + BS + "Users" + BS + "me" + BS + ".ssh",
    BS * 2 + "server" + BS + "share" + BS + "proj",
]


@pytest.mark.parametrize("text", SPECIAL, ids=["extended", "device", "extended-unc",
                                              "admin-share", "share"])
def test_extended_device_and_share_paths_are_refused_for_uploads_and_destinations(world, text):
    assert is_special_windows_path(text)
    up = refused(lambda: world.fm._local_source(text))
    dest = refused(lambda: world.fm._check_folder(text, "dest_dir"))
    for message in (up, dest):
        assert "two backslashes" in message and message == files_mod.special_path_message()


@pytest.mark.parametrize("windows, platform, example", [
    (True, "win32", "drive-letter path instead, for example C:\\Users\\name\\project."),
    (False, "darwin", "ordinary folder path instead, for example /Users/name/project."),
    (False, "linux", "ordinary folder path instead, for example /home/name/project."),
])
def test_the_refusal_gives_an_example_path_of_this_system(monkeypatch, windows, platform, example):
    """On macOS a user was told to use a drive-letter path."""
    monkeypatch.setattr(files_mod, "_on_windows", lambda: windows)
    monkeypatch.setattr(files_mod.sys, "platform", platform)
    message = files_mod.special_path_message()
    assert message.startswith("Paths that start with two backslashes") and message.endswith(example)
    assert ("C:\\Users" in message) is windows


def test_the_two_slash_spelling_is_refused_on_windows_only(world, monkeypatch):
    monkeypatch.setattr(files_mod, "_on_windows", lambda: True)
    assert is_special_windows_path("//server/share/x")
    assert "two backslashes" in refused(lambda: world.fm._check_folder("//server/share/x", "d"))
    monkeypatch.setattr(files_mod, "_on_windows", lambda: False)
    assert not is_special_windows_path("//tmp/x") and not is_special_windows_path("/tmp/x")


def test_a_normal_drive_letter_path_is_not_special():
    assert not is_special_windows_path("C:" + BS + "Users" + BS + "me")
    assert not is_special_windows_path("C:/Users/me")
    assert not is_special_windows_path(None)


def test_a_path_that_resolves_to_a_share_is_refused_too(world, monkeypatch):
    """A mapped drive or other link whose final path is a network form cannot be compared."""
    share = Path(BS * 2 + "localhost" + BS + "C$" + BS + "x")
    real = files_mod._resolve
    monkeypatch.setattr(files_mod, "_resolve",
                        lambda p: share if world.project in (Path(p), *Path(p).parents) else real(p))
    assert "two backslashes" in refused(lambda: world.fm._check_folder(str(world.project), "d"))
    assert files_mod._is_network_final_path(share)
    assert world.fm._privacy_rule()(world.project) is False
    assert world.fm._deletable(world.project / "box-20260101T000000Z-manual.tar.gz") is False


# ------------------------------------------------------------------ the spellings that were already refused


def _guarded(world) -> Path:
    return world.profile


def test_dotdot_and_forward_slash_spellings_are_refused(world):
    secret = _guarded(world) / "token.txt"
    secret.write_text("t")
    sneaky = str(world.project / ".." / "hermes" / "profiles" / "p" / "token.txt")
    assert "Hermes home" in refused(lambda: world.fm._local_source(sneaky))
    assert "Hermes home" in refused(lambda: world.fm._local_source(sneaky.replace(BS, "/")))
    assert refused(lambda: world.fm._check_folder(
        str(world.project / ".." / "hermes" / "profiles" / "p"), "dest_dir"))


def test_a_symlink_into_the_guarded_folder_is_refused(world):
    secret = _guarded(world) / "token.txt"
    secret.write_text("t")
    link = world.project / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links need a privilege here")
    assert "Hermes home" in refused(lambda: world.fm._local_source(str(link)))
    dlink = world.project / "dlink"
    dlink.symlink_to(_guarded(world), target_is_directory=True)
    assert refused(lambda: world.fm._check_folder(str(dlink), "dest_dir"))


@pytest.mark.skipif(not WINDOWS, reason="junctions, short names and drive-letter case are Windows")
def test_junction_short_name_and_letter_case_are_refused(world):
    secret = _guarded(world) / "token.txt"
    secret.write_text("t")
    junction = world.project / "junc"
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(_guarded(world))],
                          capture_output=True)
    assert done.returncode == 0, done.stderr
    assert "Hermes home" in refused(lambda: world.fm._local_source(str(junction / "token.txt")))
    assert refused(lambda: world.fm._check_folder(str(junction), "dest_dir"))
    upper = str(secret).upper()
    assert "Hermes home" in refused(lambda: world.fm._local_source(upper))
    lower_drive = str(secret)[0].lower() + str(secret)[1:]
    assert "Hermes home" in refused(lambda: world.fm._local_source(lower_drive))
    import ctypes
    buf = ctypes.create_unicode_buffer(520)
    folder = world.root / "profiles" / "a-long-profile-folder-name"
    folder.mkdir()
    (folder / "k.txt").write_text("k")
    if ctypes.windll.kernel32.GetShortPathNameW(str(folder), buf, 520) and buf.value != str(folder):
        assert "Hermes home" in refused(lambda: world.fm._local_source(buf.value + BS + "k.txt"))
