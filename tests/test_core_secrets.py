"""Tests for cloud_compute_core.secrets."""

from __future__ import annotations

import builtins
import io
import pathlib
import sys
import types

import pytest

from cloud_compute_core.models import VariableSpec
from cloud_compute_core.secrets import SecretResolver, provide
from cloud_compute_core.testing import FakeSecrets

SECRET = "s3cr3t-value-XYZ"


def _specs():
    return [
        VariableSpec(name="LIT", source="literal:hello"),
        VariableSpec(name="TOKEN", source="secret:HF_TOKEN", scope="runtime"),
        VariableSpec(name="MISSING", source="secret:NOT_THERE"),
        VariableSpec(name="EMPTY", source="secret:EMPTY_ONE"),
        VariableSpec(name="ONLY_A", source="secret:HF_TOKEN", hosts=["a"]),
        VariableSpec(name="BAD", source="env:HOME"),
        VariableSpec(name="BAD2", source="secret:"),
        VariableSpec(name="NOSRC", source=""),
        VariableSpec(name="LITHOST", source="literal:x", hosts=["a"]),
    ]


def test_get_via_getter():
    r = SecretResolver(getter={"HF_TOKEN": SECRET}.get)
    assert r.get("HF_TOKEN") == SECRET
    assert r.get("OTHER") is None
    assert r.is_set("HF_TOKEN") is True
    assert r.is_set("OTHER") is False


def test_get_empty_value_is_unset():
    r = SecretResolver(getter=lambda n: "")
    assert r.get("X") is None
    assert r.is_set("X") is False


def test_get_does_not_cache():
    values = {"K": "one"}
    r = SecretResolver(getter=values.get)
    assert r.get("K") == "one"
    values["K"] = "two"
    assert r.get("K") == "two"
    del values["K"]
    assert r.get("K") is None


@pytest.mark.parametrize("name", ["", "1ABC", "A-B", "A B", "A.B", "A" * 129, "../x", "A\n", None, 5])
def test_get_invalid_names(name):
    called = []
    r = SecretResolver(getter=lambda n: called.append(n) or "v")
    assert r.get(name) is None
    assert called == []


def test_get_valid_name_edges():
    r = SecretResolver(getter=lambda n: "v")
    assert r.get("_A1") == "v"
    assert r.get("A" * 128) == "v"


def test_get_hermes_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.setenv("HF_TOKEN", "from-env")
    r = SecretResolver()
    assert r.get("HF_TOKEN") is None  # never falls back to os.environ
    assert r.is_set("HF_TOKEN") is False


def test_get_uses_hermes_secret_scope(monkeypatch):
    mod = types.ModuleType("agent.secret_scope")
    mod.get_secret = lambda name, default=None: {"HF_TOKEN": SECRET}.get(name, default)
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    r = SecretResolver()
    assert r.get("HF_TOKEN") == SECRET
    assert r.get("OTHER") is None


def test_get_unscoped_error_is_none_and_logs_name_only(monkeypatch, caplog):
    class UnscopedSecretError(RuntimeError):
        pass

    def boom(name, default=None):
        raise UnscopedSecretError(f"no scope for {name} value={SECRET}")

    mod = types.ModuleType("agent.secret_scope")
    mod.get_secret = boom
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    with caplog.at_level("WARNING"):
        assert SecretResolver().get("HF_TOKEN") is None
    assert "secret HF_TOKEN unavailable: no profile scope" in caplog.text
    assert SECRET not in caplog.text


def test_getter_exception_is_none():
    def boom(name):
        raise ValueError("x")

    assert SecretResolver(getter=boom).get("A") is None


def test_resolve_matrix():
    r = SecretResolver(getter={"HF_TOKEN": SECRET, "EMPTY_ONE": ""}.get)
    env, previews = r.resolve(_specs(), "b")
    assert env == {"LIT": "hello", "TOKEN": SECRET}
    by_name = {p.name: p for p in previews}
    assert [p.name for p in previews] == [s.name for s in _specs()]
    assert by_name["LIT"].will_send and by_name["LIT"].source_kind == "literal"
    assert by_name["TOKEN"].will_send and by_name["TOKEN"].source_kind == "secret"
    assert by_name["TOKEN"].scope == "runtime"
    assert not by_name["MISSING"].will_send and by_name["MISSING"].reason == "secret NOT_THERE not set"
    assert not by_name["EMPTY"].will_send and by_name["EMPTY"].reason == "secret EMPTY_ONE not set"
    assert by_name["ONLY_A"].reason == "not allowed on host b" and not by_name["ONLY_A"].will_send
    assert by_name["BAD"].reason == "invalid source" and by_name["BAD"].source_kind == "literal"
    assert by_name["BAD2"].reason == "invalid source" and by_name["BAD2"].source_kind == "secret"
    assert by_name["NOSRC"].reason == "invalid source"
    assert by_name["LITHOST"].reason == "not allowed on host b"
    assert SECRET not in repr(previews)
    assert "hello" not in repr(previews)


def test_resolve_allowed_host():
    r = SecretResolver(getter={"HF_TOKEN": SECRET}.get)
    env, previews = r.resolve(_specs(), "a")
    assert env["ONLY_A"] == SECRET and env["LITHOST"] == "x"
    assert SECRET not in repr(previews)


def test_resolve_invalid_secret_name_not_set():
    r = SecretResolver(getter=lambda n: "v")
    env, previews = r.resolve([VariableSpec(name="X", source="secret:bad name!")], "a")
    assert env == {} and previews[0].reason == "secret bad name! not set"


def test_resolve_matches_fake_secrets():
    values = {"HF_TOKEN": SECRET, "EMPTY_ONE": ""}
    real = SecretResolver(getter=values.get)
    fake = FakeSecrets(values)
    for host in ("a", "b"):
        assert real.resolve(_specs(), host) == fake.resolve(_specs(), host)
    assert real.resolve([], "a") == fake.resolve([], "a") == ({}, [])


def test_redact_longest_first():
    r = SecretResolver(getter=lambda n: None)
    out = r.redact("token abcdef and abc", ["abc", "abcdef", "", "abc"])
    assert out == "token [redacted] and [redacted]"
    assert r.redact("", ["a"]) == ""
    assert r.redact(None, ["a"]) == ""
    assert r.redact("plain", []) == "plain"


def test_redact_applies_value_then_pattern_pass():
    out = SecretResolver().redact("PASSWORD=hunter2 key=abcdef", ["abcdef"])
    assert out == "PASSWORD=[redacted] key=[redacted]"
    # longest-first still holds alongside the pattern pass
    out = SecretResolver().redact("x abcdef abc API_KEY=zz", ["abc", "abcdef"])
    assert out == "x [redacted] [redacted] API_KEY=[redacted]"


def test_redact_pattern_pass_without_values():
    assert SecretResolver().redact("Authorization: Bearer abcdefgh123", []) ==         "Authorization: Bearer [redacted]"
    assert SecretResolver().redact("retries=3 path=/tmp", []) == "retries=3 path=/tmp"


def test_plugin_source_never_imports_agent_redact():
    root = pathlib.Path(__file__).resolve().parent.parent
    needle = "agent" + ".redact"
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).parts
        if rel[0] in ("tests", ".worktrees") or any(p.startswith(".") for p in rel):
            continue
        assert needle not in path.read_text(encoding="utf-8"), path


def test_never_reads_files(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("file access attempted")

    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.setitem(sys.modules, "agent.redact", None)
    monkeypatch.setattr(builtins, "open", deny)
    monkeypatch.setattr(io, "open", deny)
    monkeypatch.setattr(pathlib.Path, "open", deny)
    monkeypatch.setattr(pathlib.Path, "read_text", deny)
    monkeypatch.setattr(pathlib.Path, "read_bytes", deny)
    r = SecretResolver()
    assert r.get("HF_TOKEN") is None
    assert r.is_set("HF_TOKEN") is False
    env, previews = r.resolve(_specs(), "a")
    assert env == {"LIT": "hello", "LITHOST": "x"}
    assert r.redact("abc", ["abc"]) == "[redacted]"
    g = SecretResolver(getter={"HF_TOKEN": SECRET}.get)
    assert g.get("HF_TOKEN") == SECRET
    g.resolve(_specs(), "a")


def test_provide_returns_resolver():
    assert isinstance(provide(None), SecretResolver)
