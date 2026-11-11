"""Test doubles in cloud_compute_core.testing behave as documented."""

from __future__ import annotations

import pytest

from cloud_compute_core import testing as t
from cloud_compute_core.interfaces import ApiError, Services
from cloud_compute_core.models import ExecResult, HostSpec, VariableSpec


def test_exec_result_defaults():
    r = t.exec_result()
    assert r == ExecResult(rc=0, stdout="", stderr="", duration_s=0.01, timed_out=False)


def test_fake_transport_rules_and_recording(fake_transport):
    ft = fake_transport
    ft.script(["uname", "-m"], t.exec_result(stdout="x86_64\n"))
    ft.script(("podman", "ps"), t.exec_result(stdout="[]"))
    ft.script(r"^nvidia-smi\b", t.exec_result(rc=127, stderr="not found"))
    ft.script(lambda argv, stdin: stdin == b"data", t.exec_result(stdout="got stdin"))
    assert ft.run(["uname", "-m"]).stdout == "x86_64\n"
    assert ft.run(["uname", "-a"]).rc == 127  # exact list does not match
    assert ft.run(["podman", "ps", "-a", "--format", "json"]).stdout == "[]"
    assert ft.run(["nvidia-smi", "-L"]).stderr == "not found"
    assert ft.run(["cat"], stdin=b"data", timeout=5).stdout == "got stdin"
    assert ft.argvs()[0] == ["uname", "-m"]
    last = ft.calls[-1]
    assert (last.kind, last.argv, last.stdin, last.timeout) == ("run", ["cat"], b"data", 5)
    ft.reset_calls()
    assert ft.calls == []


def test_fake_transport_first_rule_wins_and_sequences(fake_transport):
    ft = fake_transport
    ft.script(("echo",), [t.exec_result(stdout="1"), t.exec_result(stdout="2")])
    ft.script(("echo", "hi"), t.exec_result(stdout="never"))
    outs = [ft.run(["echo", "hi"]).stdout for _ in range(4)]
    assert outs == ["1", "2", "2", "2"]
    ft.script(("dyn",), lambda argv, stdin: t.exec_result(stdout=" ".join(argv)))
    assert ft.run(["dyn", "a"]).stdout == "dyn a"


def test_fake_transport_unmatched(fake_transport):
    r = fake_transport.run(["ls", "-la"])
    assert r.rc == 127 and r.stderr == "fake: no script for ls -la"
    strict = t.FakeTransport(t.sample_host(), strict=True)
    with pytest.raises(AssertionError, match="ls -la"):
        strict.run(["ls", "-la"])


def test_fake_transport_run_script(fake_transport):
    ft = fake_transport
    ft.script_script(r"podman info", t.exec_result(stdout="{}"))
    ft.script_script(lambda s: "df" in s, lambda s: t.exec_result(stdout=str(len(s))))
    assert ft.run_script("set -e\npodman info --format json\n").stdout == "{}"
    assert ft.run_script("df -k").stdout == "5"
    assert ft.run_script("whoami").rc == 127
    assert ft.scripts() == ["set -e\npodman info --format json\n", "df -k", "whoami"]
    assert ft.calls[0].kind == "script" and ft.argvs() == []


def test_fake_transport_tcp_local_argv_close(fake_transport):
    ft = fake_transport
    assert ft.tcp_check() == 1.0
    ft.latency_ms = None
    assert ft.tcp_check(timeout=0.1) is None
    assert ft.local_argv(["bash", "-l"], tty=True) == ["ssh", "-t", "x1carbon", "bash", "-l"]
    assert ft.local_argv(None, forwards=["41234:127.0.0.1:6080"]) == [
        "ssh", "-L", "41234:127.0.0.1:6080", "x1carbon"]
    ft.close()
    assert ft.closed


def test_fake_transport_factory_caches(fake_transports):
    host = t.sample_host()
    early = fake_transports.get("x1carbon")
    early.script(["true"], t.exec_result())
    same = fake_transports.for_host(host)
    assert same is early and same.host == host
    assert fake_transports.for_host(host, prefixed=False) is early
    assert same.run(["true"]).rc == 0
    other = fake_transports.for_host(t.sample_slurm_host())
    assert other is not early
    assert fake_transports.requests == [("x1carbon", True), ("x1carbon", False), ("slurm-x1", True)]


def test_fake_events(fake_events):
    fake_events.emit("hosts.changed", {"ids": ["x1carbon"], "reason": "created"})
    fake_events.emit("notify", None)
    assert fake_events.names() == ["hosts.changed", "notify"]
    assert fake_events.events[0] == ("hosts.changed", {"ids": ["x1carbon"], "reason": "created"})


def test_fake_secrets_resolve():
    secrets = t.FakeSecrets({"HF_TOKEN": "hf_fake_value_123", "EMPTY": ""})
    assert secrets.get("HF_TOKEN") == "hf_fake_value_123" and secrets.is_set("HF_TOKEN")
    assert not secrets.is_set("EMPTY") and secrets.get("MISSING") is None
    variables = [
        VariableSpec(name="HF_TOKEN", source="secret:HF_TOKEN"),
        VariableSpec(name="MODE", source="literal:dev", scope="agent-visible"),
        VariableSpec(name="OPENAI", source="secret:OPENAI_KEY", scope="runtime"),
        VariableSpec(name="ONLY_SLURM", source="literal:1", hosts=["slurm-x1"]),
        VariableSpec(name="BAD", source="env:HOME"),
    ]
    env, previews = secrets.resolve(variables, "x1carbon")
    assert env == {"HF_TOKEN": "hf_fake_value_123", "MODE": "dev"}
    got = [(p.name, p.source_kind, p.scope, p.will_send, p.reason) for p in previews]
    assert got == [
        ("HF_TOKEN", "secret", "redacted", True, ""),
        ("MODE", "literal", "agent-visible", True, ""),
        ("OPENAI", "secret", "runtime", False, "secret OPENAI_KEY not set"),
        ("ONLY_SLURM", "literal", "redacted", False, "not allowed on host x1carbon"),
        ("BAD", "literal", "redacted", False, "invalid source"),
    ]
    env2, _ = secrets.resolve(variables, "slurm-x1")
    assert env2["ONLY_SLURM"] == "1"


def test_fake_secrets_redact():
    s = t.FakeSecrets()
    text = "token=abcdef and abc and abcdef"
    assert s.redact(text, ["abc", "abcdef", ""]) == "token=[redacted] and [redacted] and [redacted]"
    assert s.redact("nothing", []) == "nothing"


def test_samples_are_valid_and_fresh():
    for factory in (t.sample_host, t.sample_slurm_host, t.sample_local_host, t.sample_capabilities,
                    t.sample_resources, t.sample_host_state, t.sample_template, t.sample_instance,
                    t.sample_job, t.sample_tunnel, t.sample_slurm_capabilities, t.sample_gpu):
        a, b = factory(), factory()
        assert a == b and a is not b
        assert type(a).from_dict(a.to_dict()) == a
    slurm = t.sample_slurm_host()
    assert slurm.kind == "slurm" and slurm.address == "x1carbon"
    assert slurm.exec_prefix == ["podman", "exec", "-i", "-w", "/data", "slurmctld"]
    assert slurm.interactive_prefix == ["podman", "exec", "-it", "-w", "/data", "slurmctld"]
    assert slurm.slurm.workdir == "/data/cloud-compute" and slurm.slurm.default_partition == "cpu"
    assert t.sample_host(name="Other").name == "Other"
    assert t.sample_template().id == "python-dev"
    assert t.sample_local_host().kind == "local"
    h1 = t.sample_host()
    h1.exec_prefix.append("x")
    assert t.sample_host().exec_prefix == []


def test_make_services_defaults_and_overrides(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "cloud_compute_core.agents", None)
    s = t.make_services()
    assert isinstance(s, Services)
    assert isinstance(s.events, t.FakeEvents)
    assert isinstance(s.secrets, t.FakeSecrets)
    assert isinstance(s.transports, t.FakeTransportFactory)
    with pytest.raises(ApiError):
        s.agents
    custom = t.FakeEvents()
    assert t.make_services(events=custom).events is custom


def test_services_fixture(services, fake_events, fake_transports):
    assert services.events is fake_events
    assert services.transports is fake_transports
    host = HostSpec(id="h1", name="h1", kind="ssh", address="h1")
    assert services.transports.for_host(host).host is host
