"""Restore reported ``io: read/write on closed pipe`` although the files were unpacked.

``podman cp -`` stops reading at the end-of-archive marker. ``tarfile`` pads the stream to a
multiple of 10240 bytes; when that padding arrives in a later read than the marker, Podman
exits 125 with ``io: read/write on closed pipe`` after unpacking every file. The upload now
ends at the marker, and a failure with that message is only accepted as a success when the
destination holds every file of the archive with the same content.
"""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.testing import sample_instance
from cloud_compute_core.transport import StreamResult

_spec = importlib.util.spec_from_file_location(
    "files_manager_fakes", Path(__file__).with_name("test_files_manager.py"))
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)
env = _fakes.env            # the container-filesystem fixture of test_files_manager

CLOSED = "Error: 1 error occurred:\n\t* io: read/write on closed pipe\n"


def _fail_after_unpack(env, stderr=CLOSED, mutate=None):
    """The copy unpacks everything (the fake container filesystem is updated), then exits 125."""
    original = env.t.run_stream

    def wrapped(argv, *, sink=None, source=None, **kw):
        res = original(argv, sink=sink, source=source, **kw)
        if source is None:
            return res
        if mutate is not None:
            mutate(env.fs)
        return StreamResult(rc=125, stderr=stderr, bytes=res.bytes, duration_s=0.0)

    env.t.run_stream = wrapped


def _saved_copy(env):
    rec = env.fm.export("i-ab12cd34")
    env.store.upsert_instance(sample_instance(id="i-99999999", name="fresh", status="running"))
    env.fs.entries = {"/work": None}
    return rec


def test_upload_ends_at_the_end_marker_without_record_padding(env, tmp_path):
    rec = _saved_copy(env)
    env.fm.restore(rec["id"], "i-99999999", "/work")
    local = tmp_path / "in.txt"
    local.write_bytes(b"payload")
    env.fm.put("i-ab12cd34", str(local), "/work/inbox")
    for raw in env.t.uploaded:
        assert len(raw) % 512 == 0 and len(raw) % 10240 != 0
        assert raw.endswith(b"\0" * 1024)
        trailing = len(raw) - len(raw.rstrip(b"\0"))
        assert trailing < 1024 + 512            # the marker and at most one data block's padding
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:       # still a complete archive
            assert tf.getnames()


def test_closed_pipe_after_a_complete_unpack_is_a_success(env, caplog):
    rec = _saved_copy(env)
    _fail_after_unpack(env)
    out = env.fm.restore(rec["id"], "i-99999999", "/work")
    assert out["export_id"] == rec["id"] and out["instance_id"] == "i-99999999"
    assert env.fs.entries["/work/results/a.txt"] == b"hello\n"
    assert "same content" in caplog.text


def test_closed_pipe_with_a_file_missing_is_still_a_failure(env):
    rec = _saved_copy(env)
    _fail_after_unpack(env, mutate=lambda fs: fs.entries.pop("/work/results/a.txt"))
    with pytest.raises(ApiError) as ei:
        env.fm.restore(rec["id"], "i-99999999", "/work")
    assert ei.value.status == 502 and "closed pipe" in ei.value.message


def test_closed_pipe_with_different_content_is_still_a_failure(env):
    rec = _saved_copy(env)
    _fail_after_unpack(env, mutate=lambda fs: fs.entries.update({"/work/results/a.txt": b"hellO\n"}))
    with pytest.raises(ApiError):
        env.fm.restore(rec["id"], "i-99999999", "/work")


def test_other_errors_are_failures_even_when_the_files_are_there(env):
    rec = _saved_copy(env)
    _fail_after_unpack(env, stderr="Error: no space left on device")
    with pytest.raises(ApiError) as ei:
        env.fm.restore(rec["id"], "i-99999999", "/work")
    assert ei.value.status == 502 and "no space" in ei.value.message


def test_closed_pipe_into_the_root_folder_cannot_be_checked_so_it_fails(env):
    rec = _saved_copy(env)
    _fail_after_unpack(env)
    with pytest.raises(ApiError):
        env.fm.restore(rec["id"], "i-99999999", "/")


def test_closed_pipe_when_the_read_back_fails_is_a_failure(env):
    rec = _saved_copy(env)
    original = env.t.run_stream

    def wrapped(argv, *, sink=None, source=None, **kw):
        if sink is not None and argv[2].endswith(":/work"):
            return StreamResult(rc=125, stderr="Error: gone", bytes=0, duration_s=0.0)
        res = original(argv, sink=sink, source=source, **kw)
        if source is not None:
            return StreamResult(rc=125, stderr=CLOSED, bytes=res.bytes, duration_s=0.0)
        return res

    env.t.run_stream = wrapped
    with pytest.raises(ApiError):
        env.fm.restore(rec["id"], "i-99999999", "/work")


def test_put_of_a_plain_folder_with_a_closed_pipe_error_stays_a_failure(env, tmp_path):
    """Only an unpacked archive has a checklist; a plain upload keeps the old behaviour."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "x.py").write_text("print(1)")
    _fail_after_unpack(env)
    with pytest.raises(ApiError):
        env.fm.put("i-ab12cd34", str(src), "/work")
