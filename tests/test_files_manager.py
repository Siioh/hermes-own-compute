"""FileManager against a fake container transport: export, list, put, restore, index."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_compute_core import files as files_mod
from cloud_compute_core import registry
from cloud_compute_core.files import EXPORT_KIND, FileManager
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.testing import exec_result, sample_host, sample_instance
from cloud_compute_core.transport import StreamResult

MB = 1024 * 1024


# --------------------------------------------------------------------------- fakes


class FakeFS:
    """An in-memory container filesystem: absolute path -> bytes (None = directory)."""

    def __init__(self, entries: dict[str, bytes | None] | None = None):
        self.entries: dict[str, bytes | None] = dict(entries or {})

    def tar_of(self, path: str) -> bytes:
        """What ``cp CID:PATH -`` writes: the folder itself first, named by its basename."""
        base = path.rstrip("/").rsplit("/", 1)[-1] or "root"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            if self.entries.get(path, b"") is None:
                tf.addfile(_dir(base))
                for p in sorted(self.entries):
                    if p.startswith(path.rstrip("/") + "/"):
                        rel = base + "/" + p[len(path.rstrip("/")) + 1:]
                        data = self.entries[p]
                        if data is None:
                            tf.addfile(_dir(rel))
                        else:
                            info = tarfile.TarInfo(rel)
                            info.size = len(data)
                            tf.addfile(info, io.BytesIO(data))
            else:
                data = self.entries[path]
                info = tarfile.TarInfo(base)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def unpack(self, dest: str, data: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tf:
            for m in tf:
                full = dest.rstrip("/") + "/" + m.name
                self.entries[full] = None if m.isdir() else tf.extractfile(m).read() \
                    if m.isfile() else b"<link>"


def _dir(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info


class FakeStreamTransport:
    """Transport double: cp out/in work on a FakeFS; ``run`` answers exec listing calls."""

    def __init__(self, host, fs: FakeFS):
        self.host = host
        self.fs = fs
        self.calls: list[tuple[str, list[str]]] = []
        self.fail: StreamResult | None = None
        self.out_override: bytes | None = None
        self.run_result = exec_result(stdout="")
        self.uploaded: list[bytes] = []

    def run(self, argv, *, stdin=None, timeout=60):
        self.calls.append(("run", list(argv)))
        return self.run_result

    def run_stream(self, argv, *, sink=None, source=None, max_bytes=None, timeout=3600):
        self.calls.append(("stream", list(argv)))
        if self.fail is not None:
            return self.fail
        if sink is not None:
            assert argv[1] == "cp" and argv[3] == "-"
            path = argv[2].split(":", 1)[1]
            if path not in self.fs.entries:
                return StreamResult(rc=125, stderr=f"Error: {path}: no such file or directory",
                                    bytes=0, duration_s=0.0)
            data = self.out_override if self.out_override is not None else self.fs.tar_of(path)
            sent = 0
            for i in range(0, len(data), 65536):
                chunk = data[i:i + 65536]
                sent += len(chunk)
                if max_bytes is not None and sent > max_bytes:
                    return StreamResult(rc=-9, stderr="", bytes=sent, duration_s=0.0,
                                        too_large=True)
                sink(chunk)
            return StreamResult(rc=0, stderr="", bytes=sent, duration_s=0.0)
        assert argv[1] == "cp" and argv[2] == "-"
        buf = io.BytesIO()
        state = {"n": 0, "over": False}

        class W:
            def write(self, b):
                state["n"] += len(b)
                if max_bytes is not None and state["n"] > max_bytes:
                    state["over"] = True
                    raise OverflowError
                buf.write(b)
                return len(b)

            def flush(self):
                pass

        try:
            source(W())
        except OverflowError:
            return StreamResult(rc=-9, stderr="", bytes=state["n"], duration_s=0.0,
                                too_large=True)
        self.uploaded.append(buf.getvalue())
        self.fs.unpack(argv[3].split(":", 1)[1], buf.getvalue())
        return StreamResult(rc=0, stderr="", bytes=state["n"], duration_s=0.0)


class Factory:
    def __init__(self, transport):
        self.transport = transport

    def for_host(self, host, prefixed=True):
        self.transport.host = host
        return self.transport


class FakeSandboxes:
    def __init__(self):
        self.touched: list[str] = []

    def touch(self, instance_id):
        self.touched.append(instance_id)


class FakeTemplates:
    def __init__(self, workdir="/work"):
        self.workdir = workdir

    def get(self, template_id):
        return SimpleNamespace(id=template_id, workdir=self.workdir)


@pytest.fixture
def env(tmp_path):
    from cloud_compute_core.store import Store

    data = tmp_path / "data"               # plugin data folder; local-host files sit beside it
    store = Store(data / "cloud-compute.db")
    fs = FakeFS({"/work": None, "/work/results": None, "/work/results/a.txt": b"hello\n",
                 "/data": None, "/data/b.bin": b"\x00\x01"})
    transport = FakeStreamTransport(sample_host(), fs)
    sandboxes = FakeSandboxes()
    services = registry.build_services(
        store=store, transports=Factory(transport), sandboxes=sandboxes,
        templates=FakeTemplates(), config=SimpleNamespace(export_max_mb=10))
    store.upsert_host(sample_host())
    store.upsert_instance(sample_instance())
    fm = FileManager(services)
    yield SimpleNamespace(fm=fm, fs=fs, t=transport, store=store, services=services,
                          sandboxes=sandboxes, tmp=data)
    store.close()


def _members(path: str) -> dict[str, bytes | None]:
    out = {}
    with tarfile.open(path, mode="r:gz") as tf:
        for m in tf:
            out[m.name] = tf.extractfile(m).read() if m.isfile() else None
    return out


# --------------------------------------------------------------------------- export


def test_export_streams_gzip_into_exports_folder_and_indexes(env):
    rec = env.fm.export("i-ab12cd34")
    assert rec["id"].startswith("x-") and rec["reason"] == "manual"
    assert rec["source_path"] == "/work" and rec["instance_name"] == "python-dev-1"
    path = Path(rec["local_path"])
    assert path.is_file() and path.name.endswith("-manual.tar.gz")
    assert path.parent.name == "python-dev-1-i-ab12cd34"
    assert path.parent.parent == env.tmp / "exports"
    assert rec["bytes"] == path.stat().st_size
    with path.open("rb") as fh:
        assert fh.read(2) == b"\x1f\x8b"                  # gzip on disk
    assert _members(str(path))["work/results/a.txt"] == b"hello\n"
    assert env.t.calls[-1] == ("stream", ["podman", "cp", "cc-i-ab12cd34:/work", "-"])
    assert env.store.get_record(EXPORT_KIND, rec["id"])["local_path"] == str(path)
    assert env.fm.list_exports() == [rec]
    assert env.sandboxes.touched == ["i-ab12cd34"]


def test_export_works_on_a_stopped_environment_without_touching_it(env):
    env.store.upsert_instance(sample_instance(status="stopped"))
    rec = env.fm.export("i-ab12cd34", "/work/results", reason="before-delete")
    assert rec["reason"] == "before-delete" and rec["source_path"] == "/work/results"
    assert "results/a.txt" in _members(rec["local_path"])
    assert env.sandboxes.touched == []


def test_export_default_path_is_template_workdir(env):
    env.services.templates.workdir = "/data"
    rec = env.fm.export("i-ab12cd34")
    assert rec["source_path"] == "/data"


def test_export_normalises_and_validates_paths(env):
    rec = env.fm.export("i-ab12cd34", "/work/results/../results/")
    assert rec["source_path"] == "/work/results"
    for bad in ("work", "/work\n/x", "/wo\x00rk", "relative/path"):
        with pytest.raises(ApiError) as info:
            env.fm.export("i-ab12cd34", bad)
        assert info.value.code == "invalid"
    with pytest.raises(ApiError) as info:
        env.fm.export("i-ab12cd34", reason="whenever")
    assert info.value.code == "invalid"


def test_export_over_cap_kills_deletes_partial_and_raises_413(env):
    env.t.out_override = b"\0" * (11 * MB)               # raw tar stream above 10 MB
    with pytest.raises(ApiError) as info:
        env.fm.export("i-ab12cd34")
    err = info.value
    assert (err.status, err.code) == (413, "too_large")
    assert err.message == ('The files in /work are larger than the export limit (10 MB). '
                           'Raise "Export max mb" in Settings or export a smaller folder.')
    assert not list((env.tmp / "exports").rglob("*.tar.gz"))
    assert env.fm.list_exports() == []


def test_export_transport_failure_deletes_partial_and_raises_502(env):
    env.t.fail = StreamResult(rc=255, stderr="ssh: connect to host x1carbon: Connection refused",
                              bytes=0, duration_s=0.1)
    with pytest.raises(ApiError) as info:
        env.fm.export("i-ab12cd34")
    assert info.value.status == 502 and "Connection refused" in info.value.message
    assert not list((env.tmp / "exports").rglob("*"))   # the empty folder is gone too


def test_export_missing_path_is_404_and_missing_container_is_409(env):
    with pytest.raises(ApiError) as info:
        env.fm.export("i-ab12cd34", "/nope")
    assert (info.value.status, info.value.code) == (404, "not_found")
    env.t.fail = StreamResult(rc=125, stderr="Error: no such container cc-i-ab12cd34",
                              bytes=0, duration_s=0.0)
    with pytest.raises(ApiError) as info:
        env.fm.export("i-ab12cd34")
    assert info.value.status == 409


def test_export_timeout_is_504(env):
    env.t.fail = StreamResult(rc=124, stderr="timed out", bytes=10, duration_s=1,
                              timed_out=True)
    with pytest.raises(ApiError) as info:
        env.fm.export("i-ab12cd34")
    assert info.value.status == 504


def test_export_refuses_destroyed_and_unknown(env):
    with pytest.raises(ApiError) as info:
        env.fm.export("i-00000000")
    assert info.value.status == 404
    env.store.upsert_instance(sample_instance(status="destroyed"))
    with pytest.raises(ApiError) as info:
        env.fm.export("i-ab12cd34")
    assert info.value.status == 409 and "deleted" in info.value.message


def test_export_file_names_are_unique_and_list_is_newest_first(env):
    clock = iter([1_700_000_000, 1_700_000_000, 1_700_000_100])
    env.fm._clock = lambda: next(clock)
    a = env.fm.export("i-ab12cd34")
    b = env.fm.export("i-ab12cd34")
    c = env.fm.fetch("i-ab12cd34", "/data")
    assert Path(b["local_path"]).name.endswith("-manual-2.tar.gz")
    assert c["reason"] == "fetch"
    order = [r["id"] for r in env.fm.list_exports()]
    assert order[0] == c["id"] and set(order) == {a["id"], b["id"], c["id"]}
    assert env.fm.list_exports("i-other") == []


def test_unsafe_instance_name_stays_inside_exports(env):
    env.store.upsert_instance(sample_instance(name="../../evil name"))
    rec = env.fm.export("i-ab12cd34")
    path = Path(rec["local_path"]).resolve()
    assert (env.tmp / "exports").resolve() in path.parents
    assert ".." not in path.parent.name


def test_list_exports_skips_files_removed_by_hand(env):
    rec = env.fm.export("i-ab12cd34")
    Path(rec["local_path"]).unlink()
    assert env.fm.list_exports() == []


def test_delete_export_removes_file_and_index(env):
    rec = env.fm.export("i-ab12cd34")
    assert env.fm.delete_export(rec["id"]) == {"deleted": True}
    assert not Path(rec["local_path"]).exists()
    assert env.store.get_record(EXPORT_KIND, rec["id"]) is None
    with pytest.raises(ApiError) as info:
        env.fm.delete_export(rec["id"])
    assert info.value.status == 404


def test_delete_export_never_deletes_outside_exports(env, tmp_path):
    outside = tmp_path / "keep.txt"
    outside.write_text("keep")
    env.store.put_record(EXPORT_KIND, "x-deadbeef", {
        "id": "x-deadbeef", "instance_id": "i-ab12cd34", "instance_name": "n", "host_id": "h",
        "source_path": "/work", "local_path": str(outside), "bytes": 4,
        "created_at": "2026-11-09T00:00:00Z", "reason": "manual"})
    assert env.fm.delete_export("x-deadbeef") == {"deleted": True}
    assert outside.read_text() == "keep"


# --------------------------------------------------------------------------- list_dir


def _listing(*rows: tuple[str, str, str, str]) -> str:
    return "".join("\t".join(r) + "\0" for r in rows)


def test_list_dir_parses_find_output(env):
    env.t.run_result = exec_result(stdout=_listing(
        ("f", "6", "1762689600.5", "a.txt"), ("d", "4096", "1762689600", "results"),
        ("l", "7", "1762689600", "link"), ("p", "0", "x", "fifo"),
        ("f", "3", "1762689600", "tab\tname")))
    out = env.fm.list_dir("i-ab12cd34", "/work")
    assert out["path"] == "/work" and out["truncated"] is False
    assert [e["name"] for e in out["entries"]] == ["results", "a.txt", "fifo", "link", "tab\tname"]
    first = out["entries"][1]
    assert first == {"name": "a.txt", "type": "file", "size": 6, "mtime": "2025-11-09T12:00:00Z"}
    assert {e["type"] for e in out["entries"]} == {"file", "dir", "link", "other"}
    argv = env.t.calls[-1][1]
    assert argv[:3] == ["podman", "exec", "cc-i-ab12cd34"] and argv[3:5] == ["sh", "-c"]
    assert argv[-2:] == ["/work", "501"]
    assert env.sandboxes.touched == ["i-ab12cd34"]


def test_list_dir_is_bounded_and_flags_truncation(env):
    rows = [("f", "1", "0", f"f{i:04d}") for i in range(600)]
    env.t.run_result = exec_result(stdout=_listing(*rows))
    out = env.fm.list_dir("i-ab12cd34")
    assert len(out["entries"]) == 500 and out["truncated"] is True
    env.t.run_result = exec_result(stdout=_listing(("f", "1", "0", "ok")) + "f\t1\t0\tcut-off")
    out = env.fm.list_dir("i-ab12cd34")
    assert [e["name"] for e in out["entries"]] == ["ok"] and out["truncated"] is True


def test_list_dir_requires_running(env):
    env.store.upsert_instance(sample_instance(status="stopped"))
    with pytest.raises(ApiError) as info:
        env.fm.list_dir("i-ab12cd34", "/work")
    assert (info.value.status, info.value.code) == (409, "not_running")
    assert info.value.message == "Start python-dev-1 to browse its files."
    assert env.t.calls == []


def test_list_dir_errors(env):
    env.t.run_result = exec_result(rc=3)
    with pytest.raises(ApiError) as info:
        env.fm.list_dir("i-ab12cd34", "/missing")
    assert info.value.status == 404
    env.t.run_result = exec_result(rc=4)
    with pytest.raises(ApiError) as info:
        env.fm.list_dir("i-ab12cd34", "/work/a.txt")
    assert info.value.status == 400
    env.t.run_result = exec_result(rc=125, stderr="Error: container state improper")
    with pytest.raises(ApiError) as info:
        env.fm.list_dir("i-ab12cd34", "/work")
    assert info.value.code == "not_running"


def test_list_script_runs_in_a_real_posix_shell(tmp_path):
    """The listing script itself, against a real folder (skipped without sh/find)."""
    import shutil
    import subprocess

    sh = shutil.which("sh")
    if not sh or not shutil.which("find"):
        pytest.skip("no POSIX sh")
    (tmp_path / "d").mkdir()
    (tmp_path / "f.txt").write_text("abc")
    res = subprocess.run([sh, "-c", files_mod._LIST_SCRIPT, "sh", tmp_path.as_posix(), "501"],
                         capture_output=True, timeout=30)
    if res.returncode != 0:
        pytest.skip(f"sh unusable here: {res.stderr[:200]!r}")
    entries, cut = FileManager._parse_listing(res.stdout.decode())
    assert {(e["name"], e["type"]) for e in entries} == {("d", "dir"), ("f.txt", "file")}
    assert not cut


# --------------------------------------------------------------------------- put / restore


def test_put_file_into_running_and_stopped(env, tmp_path):
    local = tmp_path / "in.txt"
    local.write_bytes(b"payload")
    out = env.fm.put("i-ab12cd34", str(local), "/work/inbox")
    assert out == {"instance_id": "i-ab12cd34", "dest": "/work/inbox", "bytes": out["bytes"]}
    assert out["bytes"] > 0
    assert env.fs.entries["/work/inbox/in.txt"] == b"payload"
    assert env.t.calls[-1] == ("stream", ["podman", "cp", "-", "cc-i-ab12cd34:/"])
    env.store.upsert_instance(sample_instance(status="stopped"))
    env.fm.put("i-ab12cd34", str(local), "/elsewhere")
    assert env.fs.entries["/elsewhere/in.txt"] == b"payload"


def test_put_directory_packs_tree_with_root_owner(env, tmp_path):
    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "x.py").write_text("print(1)")
    env.fm.put("i-ab12cd34", str(src), "/work")
    assert env.fs.entries["/work/proj/sub/x.py"] == b"print(1)"
    assert env.fs.entries["/work/proj/sub"] is None
    with tarfile.open(fileobj=io.BytesIO(env.t.uploaded[-1])) as tf:
        assert all(m.uid == 0 and m.gid == 0 for m in tf)


def test_put_extract_streams_archive_into_dest_with_clean_names(env, tmp_path):
    arc = tmp_path / "bundle.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        for name, data in (("a/one.txt", b"1"), ("../escape.txt", b"2"), ("/abs.txt", b"3")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        dev = tarfile.TarInfo("dev0")
        dev.type = tarfile.CHRTYPE
        tf.addfile(dev)
    env.fm.put("i-ab12cd34", str(arc), "/work/in", extract=True)
    assert env.fs.entries["/work/in/a/one.txt"] == b"1"
    assert env.fs.entries["/work/in/escape.txt"] == b"2"
    assert env.fs.entries["/work/in/abs.txt"] == b"3"
    assert "/work/in/dev0" not in env.fs.entries


def test_put_validation(env, tmp_path):
    for bad in ("", "relative.txt", str(tmp_path / "missing.txt")):
        with pytest.raises(ApiError):
            env.fm.put("i-ab12cd34", bad, "/work")
    plain = tmp_path / "plain.txt"
    plain.write_text("x")
    with pytest.raises(ApiError) as info:
        env.fm.put("i-ab12cd34", str(plain), "/work", extract=True)
    assert info.value.code == "invalid"
    with pytest.raises(ApiError) as info:
        env.fm.put("i-ab12cd34", str(plain), "work")
    assert info.value.code == "invalid"
    bogus = tmp_path / "bad.tar"
    bogus.write_bytes(b"not a tar at all" * 100)
    with pytest.raises(ApiError) as info:
        env.fm.put("i-ab12cd34", str(bogus), "/work", extract=True)
    assert info.value.code == "invalid"


def test_put_refuses_private_folders(env, tmp_path):
    secret = env.tmp / "cloud-compute.db"           # the plugin data folder
    with pytest.raises(ApiError) as info:
        env.fm.put("i-ab12cd34", str(secret), "/work")
    assert info.value.status == 403
    assert env.t.calls == []


def test_forbidden_roots_cover_hermes_home_and_ssh(env, monkeypatch, tmp_path):
    import sys
    import types
    home = tmp_path / "hh"
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    roots = env.fm._forbidden_roots()
    assert Path(home).resolve() in roots
    assert any(r.name == ".ssh" for r in roots)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)    # import fails
    assert any(r.name == "." + "hermes" for r in env.fm._forbidden_roots())
    assert not env.fm._privacy_rule()(Path.home() / ".ssh" / "id_x")


def test_put_over_cap(env, tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * (11 * MB))
    with pytest.raises(ApiError) as info:
        env.fm.put("i-ab12cd34", str(big), "/work")
    assert (info.value.status, info.value.code) == (413, "too_large")
    assert env.t.calls == []                        # refused before anything was sent
    arc = tmp_path / "big.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        tf.add(big, arcname="big.bin")
    with pytest.raises(ApiError) as info:
        env.fm.put("i-ab12cd34", str(arc), "/work", extract=True)
    assert info.value.code == "too_large" and "upload a smaller folder" in info.value.message


def test_restore_puts_export_contents_back_into_dest(env):
    rec = env.fm.export("i-ab12cd34")
    env.store.upsert_instance(sample_instance(id="i-99999999", name="fresh", status="stopped"))
    env.fs.entries = {"/work": None}
    out = env.fm.restore(rec["id"], "i-99999999", "/work")
    assert out["instance_id"] == "i-99999999" and out["export_id"] == rec["id"]
    assert env.fs.entries["/work/results/a.txt"] == b"hello\n"
    assert "/work/work" not in env.fs.entries
    assert env.t.calls[-1] == ("stream", ["podman", "cp", "-", "cc-i-99999999:/"])


def test_restore_single_file_export(env):
    rec = env.fm.export("i-ab12cd34", "/work/results/a.txt")
    env.fm.restore(rec["id"], "i-ab12cd34", "/restored")
    assert env.fs.entries["/restored/a.txt"] == b"hello\n"


def test_restore_errors(env):
    with pytest.raises(ApiError) as info:
        env.fm.restore("x-00000000", "i-ab12cd34")
    assert info.value.status == 404
    rec = env.fm.export("i-ab12cd34")
    Path(rec["local_path"]).unlink()
    with pytest.raises(ApiError) as info:
        env.fm.restore(rec["id"], "i-ab12cd34")
    assert info.value.status == 404


def test_put_transport_failure_is_502(env, tmp_path):
    local = tmp_path / "in.txt"
    local.write_text("x")
    env.t.fail = StreamResult(rc=125, stderr="Error: permission denied", bytes=0, duration_s=0.0)
    with pytest.raises(ApiError) as info:
        env.fm.put("i-ab12cd34", str(local), "/work")
    assert info.value.status == 502 and "permission denied" in info.value.message


def test_cap_setting_is_clamped_and_defaults(env):
    env.services.config.export_max_mb = 5
    assert env.fm._cap_mb() == 10
    env.services.config.export_max_mb = "x"
    assert env.fm._cap_mb() == 2048


def test_exports_dir_comes_from_the_store_location(env):
    assert env.fm.exports_dir() == (env.tmp / "exports").resolve()
    other = FileManager(env.services, exports_dir=env.tmp / "elsewhere")
    assert other.exports_dir() == (env.tmp / "elsewhere").resolve()


def test_transport_without_streaming_is_503(env):
    env.services.set("transports", Factory(SimpleNamespace(host=None, run=lambda *a, **k: None)))
    with pytest.raises(ApiError) as info:
        env.fm.export("i-ab12cd34")
    assert info.value.status == 503


def test_gzip_written_while_streaming_not_buffered(env, monkeypatch):
    """Each chunk reaches the gzip file as it arrives (sink is GzipFile.write)."""
    seen = []
    real = gzip.GzipFile.write

    def spy(self, data):
        seen.append(len(data))
        return real(self, data)

    monkeypatch.setattr(gzip.GzipFile, "write", spy)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("work/big.bin")
        info.size = 3 * 65536 + 5
        tf.addfile(info, io.BytesIO(b"a" * info.size))
    payload = buf.getvalue()
    env.t.out_override = payload
    env.fm.export("i-ab12cd34")
    full, rest = divmod(len(payload), 65536)
    assert seen == [65536] * full + ([rest] if rest else [])



# --------------------------------------------------------------------------- size


def test_size_measures_inside_a_running_environment(env):
    env.t.run_result = exec_result(stdout="1204 3435973837\n")
    out = env.fm.size("i-ab12cd34", ' "/work" ')
    assert out == {"path": "/work", "known": True, "bytes": 3435973837, "files": 1204,
                   "reason": None}
    argv = env.t.calls[-1][1]
    assert argv[:5] == ["podman", "exec", "cc-i-ab12cd34", "sh", "-c"]
    assert argv[5] == files_mod._SIZE_WRAP and argv[7:] == ["/work", files_mod._SIZE_SCRIPT]
    assert "timeout 10" in argv[5]


def test_size_unknown_when_stopped_or_slow(env):
    env.t.run_result = exec_result(rc=124, stderr="")
    out = env.fm.size("i-ab12cd34")
    assert out["known"] is False and out["bytes"] is None and out["files"] is None
    assert out["reason"] == ("Measuring /work in python-dev-1 took longer than 10 seconds (it "
                             "may hold very many files), so its size is not known.")
    env.t.run_result = exec_result(rc=3)
    with pytest.raises(ApiError) as info:
        env.fm.size("i-ab12cd34", "/nope")
    assert info.value.status == 404
    env.store.upsert_instance(sample_instance(status="stopped"))
    calls = len(env.t.calls)
    out = env.fm.size("i-ab12cd34")
    assert out["known"] is False and "Start it" in out["reason"]
    assert len(env.t.calls) == calls                     # nothing ran


def test_size_script_counts_hidden_files_not_empty_folders(tmp_path):
    import shutil
    import subprocess

    sh = shutil.which("sh")
    find = shutil.which("find")
    if not sh or not find:
        pytest.skip("no POSIX sh")
    # The script is written for the Linux containers (GNU find -printf, GNU stat -c). Run it
    # only where this host's find has -printf; BSD tools (macOS) would print "0 0".
    probe = subprocess.run([find, tmp_path.as_posix(), "-maxdepth", "0", "-printf", ""],
                           capture_output=True, timeout=30)
    if probe.returncode != 0:
        pytest.skip("this host's find has no -printf (BSD find, for example macOS); the size "
                    "script is meant for GNU or busybox tools inside Linux containers")
    (tmp_path / "empty" / "deeper").mkdir(parents=True)
    (tmp_path / ".hidden").write_bytes(b"12345")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_bytes(b"abc")
    res = subprocess.run([sh, "-c", files_mod._SIZE_WRAP, "sh", tmp_path.as_posix(),
                          files_mod._SIZE_SCRIPT], capture_output=True, timeout=30)
    if res.returncode != 0:
        pytest.skip(f"sh unusable here: {res.stderr[:200]!r}")
    assert res.stdout.decode().split() == ["2", "8"]
    res = subprocess.run([sh, "-c", files_mod._SIZE_WRAP, "sh", (tmp_path / "empty").as_posix(),
                          files_mod._SIZE_SCRIPT], capture_output=True, timeout=30)
    assert res.stdout.decode().split() == ["0", "0"]
    res = subprocess.run([sh, "-c", files_mod._SIZE_WRAP, "sh", (tmp_path / "x").as_posix(),
                          files_mod._SIZE_SCRIPT], capture_output=True, timeout=30)
    assert res.returncode == 3


# --------------------------------------------------------------------------- nothing to save


def test_manual_save_of_a_folder_without_files_keeps_nothing(env):
    env.fs.entries = {"/work": None, "/work/empty": None, "/work/empty/deeper": None}
    for call in (lambda: env.fm.export("i-ab12cd34"), lambda: env.fm.fetch("i-ab12cd34")):
        with pytest.raises(ApiError) as info:
            call()
        assert (info.value.status, info.value.code) == (409, "nothing_to_save")
        assert info.value.message == "Nothing to save: /work in python-dev-1 has no files."
    assert not (env.tmp / "exports").exists() or not list((env.tmp / "exports").rglob("*"))
    assert env.fm.list_exports() == [] and env.store.list_records(EXPORT_KIND) == []


def test_a_hidden_file_is_something_to_save(env):
    env.fs.entries = {"/work": None, "/work/.env.local": b"K=V"}
    rec = env.fm.fetch("i-ab12cd34")
    assert rec["files"] == 1 and "work/.env.local" in _members(rec["local_path"])


def test_every_export_fills_files(env):
    assert env.fm.export("i-ab12cd34")["files"] == 1
    assert env.fm.export("i-ab12cd34", "/work/results/a.txt")["files"] == 1


# --------------------------------------------------------------------------- save before delete


def test_save_before_delete_with_files(env):
    out = env.fm.save_before_delete("i-ab12cd34")
    assert out["saved"] is True and out["files"] == 1 and out["bytes"] == 6
    assert out["source_path"] == "/work" and out["export"]["reason"] == "before-delete"
    assert out["export"]["files"] == 1
    assert out["message"] == (f"The files in /work (1 file, 6 B) were saved to "
                              f"{out['export']['local_path']}.")
    assert Path(out["export"]["local_path"]).is_file()


def test_save_before_delete_without_files_keeps_nothing(env):
    env.fs.entries = {"/work": None, "/work/empty": None}
    out = env.fm.save_before_delete("i-ab12cd34", reason="auto-delete")
    assert out == {"saved": False, "export": None, "files": 0, "bytes": 0,
                   "source_path": "/work", "message": "Nothing in /work needed saving."}
    assert not list((env.tmp / "exports").rglob("*.tar.gz"))
    assert env.store.list_records(EXPORT_KIND) == []
    with pytest.raises(ApiError):
        env.fm.save_before_delete("i-ab12cd34", reason="manual")


def test_save_before_delete_raises_on_a_cut_off_archive(env):
    env.t.out_override = env.fs.tar_of("/work")[:700]
    with pytest.raises(ApiError) as info:
        env.fm.save_before_delete("i-ab12cd34")
    assert "could not be read back" in info.value.message
    assert not list((env.tmp / "exports").rglob("*.tar.gz"))


# --------------------------------------------------------------------------- where copies go


def test_exports_dir_setting_is_read_at_call_time(env, tmp_path):
    target = tmp_path / "proj" / "saved copies"
    env.services.config.exports_dir = f'  "{target}"  '
    assert env.fm.exports_dir() == target.resolve()
    rec = env.fm.export("i-ab12cd34")
    assert Path(rec["local_path"]).parent.parent == target.resolve()
    env.services.config.exports_dir = ""
    assert env.fm.exports_dir() == (env.tmp / "exports").resolve()


@pytest.mark.parametrize("bad", ["relative/folder", "./x"])
def test_exports_dir_setting_must_be_absolute(env, bad):
    env.services.config.exports_dir = bad
    with pytest.raises(ApiError) as info:
        env.fm.exports_dir()
    assert info.value.code == "invalid" and '"Exports dir" setting' in info.value.message
    with pytest.raises(ApiError):
        env.fm.export("i-ab12cd34")


def test_exports_dir_refuses_hermes_home_and_ssh_but_allows_own_folder(env, monkeypatch,
                                                                       tmp_path):
    import sys
    import types
    home = tmp_path / "hh"
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    for bad in (home / "anything", Path.home() / ".ssh" / "copies", env.tmp / "other"):
        env.services.config.exports_dir = str(bad)
        with pytest.raises(ApiError) as info:
            env.fm.exports_dir()
        assert info.value.status == 403 and "Hermes home or the SSH folder" in info.value.message
    env.services.config.exports_dir = str(env.tmp / "exports")
    assert env.fm.exports_dir() == (env.tmp / "exports").resolve()


def test_dest_dir_names_the_archive_and_must_exist(env, tmp_path):
    proj = tmp_path / "my project"
    proj.mkdir()
    env.fm._clock = lambda: 1_700_000_000
    rec = env.fm.fetch("i-ab12cd34", dest_dir=f"'{proj}'")
    assert Path(rec["local_path"]) == proj.resolve() / \
        "python-dev-1-20231114T221320Z-fetch.tar.gz"
    assert rec["extracted_to"] is None
    with pytest.raises(ApiError) as info:
        env.fm.fetch("i-ab12cd34", dest_dir=str(tmp_path / "missing"))
    assert info.value.status == 404 and "Create the folder first" in info.value.message
    (tmp_path / "file.txt").write_text("x")
    with pytest.raises(ApiError) as info:
        env.fm.fetch("i-ab12cd34", dest_dir=str(tmp_path / "file.txt"))
    assert info.value.code == "invalid"
    with pytest.raises(ApiError) as info:
        env.fm.fetch("i-ab12cd34", dest_dir="relative")
    assert info.value.code == "invalid"
    assert env.fm.get_export(rec["id"]) == rec
    assert {r["id"] for r in env.fm.list_exports()} == {rec["id"]}


def test_failed_save_into_dest_dir_never_removes_the_users_folder(env, tmp_path):
    proj = tmp_path / "empty-project"
    proj.mkdir()
    env.t.fail = StreamResult(rc=255, stderr="ssh: lost", bytes=0, duration_s=0.0)
    with pytest.raises(ApiError):
        env.fm.fetch("i-ab12cd34", dest_dir=str(proj))
    assert proj.is_dir() and list(proj.iterdir()) == []


def test_extract_unpacks_into_a_new_folder_byte_for_byte(env, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    env.fs.entries.update({"/work/.hidden": b"h" * 3000, "/work/results/sub": None,
                           "/work/results/sub/b.bin": bytes(range(256)) * 10})
    env.fm._clock = lambda: 1_700_000_000
    rec = env.fm.fetch("i-ab12cd34", dest_dir=str(proj), extract=True)
    out = Path(rec["extracted_to"])
    assert out == proj.resolve() / "python-dev-1-20231114T221320Z"
    assert (out / "results" / "a.txt").read_bytes() == b"hello\n"
    assert (out / ".hidden").read_bytes() == b"h" * 3000
    assert (out / "results" / "sub" / "b.bin").read_bytes() == bytes(range(256)) * 10
    assert not (out / "work").exists()
    assert env.fm.get_export(rec["id"])["extracted_to"] == str(out)
    again = env.fm.fetch("i-ab12cd34", dest_dir=str(proj), extract=True)
    assert Path(again["extracted_to"]).name == "python-dev-1-20231114T221320Z-2"
    deleted = env.fm.delete_export(rec["id"])
    assert deleted == {"deleted": True, "message": (
        f"Deleted the saved copy {Path(rec['local_path']).name}. The unpacked folder {out} was "
        "left in place.")}
    assert not Path(rec["local_path"]).exists() and out.is_dir()
    assert proj.is_dir()


def test_extract_into_the_default_folder(env):
    rec = env.fm.fetch("i-ab12cd34", extract=True)
    assert Path(rec["extracted_to"]).parent == Path(rec["local_path"]).parent
    assert (Path(rec["extracted_to"]) / "results" / "a.txt").read_bytes() == b"hello\n"


def test_delete_export_removes_archives_outside_the_exports_folder(env, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    rec = env.fm.fetch("i-ab12cd34", dest_dir=str(proj))
    assert env.fm.delete_export(rec["id"]) == {"deleted": True}
    assert not Path(rec["local_path"]).exists() and proj.is_dir()


def test_get_export_errors(env):
    with pytest.raises(ApiError) as info:
        env.fm.get_export("x-00000000")
    assert info.value.status == 404
    rec = env.fm.export("i-ab12cd34")
    Path(rec["local_path"]).unlink()
    with pytest.raises(ApiError) as info:
        env.fm.get_export(rec["id"])
    assert info.value.status == 404


# --------------------------------------------------------------------------- quoted paths


def test_quoted_paths_are_accepted(env, tmp_path, monkeypatch):
    local = tmp_path / "with space.txt"
    local.write_bytes(b"q")
    env.fm.put("i-ab12cd34", f'  "{local}"  ', "'/work/in'")
    assert env.fs.entries["/work/in/with space.txt"] == b"q"
    env.fm.put("i-ab12cd34", f"\u201c{local}\u201d", '"/work/in2"')
    assert env.fs.entries["/work/in2/with space.txt"] == b"q"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    env.fm.put("i-ab12cd34", '"~/with space.txt"', "/work/in3")
    assert env.fs.entries["/work/in3/with space.txt"] == b"q"
    assert env.fm.export("i-ab12cd34", '"/work/results"')["source_path"] == "/work/results"
    with pytest.raises(ApiError):
        env.fm.put("i-ab12cd34", '"', "/work")                 # one quote is not a pair


def test_clean_path_text():
    from cloud_compute_core.files import clean_path_text
    assert clean_path_text(' "C:/a b" ') == "C:/a b"
    assert clean_path_text("'/work'") == "/work"
    assert clean_path_text("\"'/x'\"") == "'/x'"                 # only one pair
    assert clean_path_text('"/work\'') == '"/work\''              # not matching
    assert clean_path_text(None) is None


def test_upload_refusal_says_what_to_do(env):
    with pytest.raises(ApiError) as info:
        env.fm.put("i-ab12cd34", str(env.tmp / "cloud-compute.db"), "/work")
    assert info.value.message == (
        "Files inside the Hermes home cannot be uploaded. Copy the file to another folder "
        "first (for example the project folder), then upload it from there.")


def test_human_sizes():
    from cloud_compute_core.files import file_count, human_size
    assert human_size(0) == "0 B" and human_size(1536) == "1.5 KB"
    assert human_size(int(3.2 * 1024 ** 3)) == "3.2 GB"
    assert file_count(1) == "1 file" and file_count(1204) == "1,204 files"


@pytest.mark.skipif(__import__("os").name == "nt",
                    reason="POSIX modes; Windows relies on the user-profile permissions")
def test_saved_copies_are_private_on_posix(env, tmp_path):
    import os
    import stat
    rec = env.fm.export("i-ab12cd34")
    path = Path(rec["local_path"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    made = tmp_path / "made" / "by-setting"
    env.services.config.exports_dir = str(made)
    rec = env.fm.export("i-ab12cd34")
    assert stat.S_IMODE(made.stat().st_mode) == 0o700
    assert stat.S_IMODE(made.parent.stat().st_mode) == 0o700
    env.services.config.exports_dir = ""
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    rec = env.fm.fetch("i-ab12cd34", dest_dir=str(shared), extract=True)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755          # existing folder untouched
    assert stat.S_IMODE(Path(rec["local_path"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(rec["extracted_to"]).stat().st_mode) == 0o700
