"""Tests for core/prober.py (parsing, classification, Prober behaviour, live smoke)."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_compute_core import prober as pr
from cloud_compute_core.testing import (
    FakeEvents, exec_result, sample_host, sample_host_state, sample_local_host,
    sample_slurm_host,
)

FIX = Path(__file__).parent / "fixtures" / "hosts"


def fixture_text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture
def x1_out() -> str:
    return fixture_text("probe_x1carbon.txt")


@pytest.fixture
def slurm_out() -> str:
    return fixture_text("probe_slurmctld.txt")


@pytest.fixture
def prober(services):
    return pr.Prober(services)


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 22))])


# --------------------------------------------------------------------------- script


def test_probe_script_sections_and_shape():
    script = pr.PROBE_SCRIPT
    for name in ("os", "cpu", "mem", "disk", "gpu", "runtime", "display", "slurm", "apptainer"):
        assert f'echo "=={name}"' in script
    order = [script.index(f'echo "=={n}"') for n in
             ("os", "cpu", "mem", "disk", "gpu", "runtime", "display", "slurm", "apptainer")]
    assert order == sorted(order)
    assert "localhost/cloud-compute-novnc:0.1" in script
    assert "@@IMAGE@@" not in script
    assert script.rstrip().endswith("exit 0")
    # every external tool is guarded
    for tool in ("nvidia-smi", "sinfo", "apptainer", "podman", "docker"):
        assert f"command -v {tool}" in script or f'command -v "$rt"' in script


# --------------------------------------------------------------------------- parse: fixtures


def test_parse_x1carbon(x1_out):
    caps, res = pr.parse_probe("ssh", x1_out)
    assert caps.runtime == "podman"
    assert caps.runtime_version == "5.8.4"
    assert caps.rootless is True
    assert caps.containers is True
    assert caps.port_forward and caps.interactive_terminal
    assert caps.batch_jobs is False and caps.scheduler is None
    assert caps.gpus == []
    assert caps.display == "buildable"
    assert caps.os == "Fedora Linux 43"
    assert caps.arch == "x86_64"
    assert res.source == "host"
    assert res.cpu_total == 8 and res.cpu_used == pytest.approx(0.16)
    assert res.mem_total_mb == 16102016 // 1024
    assert res.mem_used_mb == (16102016 - 12720924) // 1024
    assert res.disk_path == "/home/dev"
    assert res.disk_total_gb == pytest.approx(248.0, abs=0.1)
    assert res.disk_free_gb == pytest.approx(150.3, abs=0.1)
    assert res.gpus == []


def test_parse_slurmctld(slurm_out):
    caps, res = pr.parse_probe("slurm", slurm_out)
    assert caps.scheduler == "slurm"
    assert caps.scheduler_version == "26.05.5"
    assert caps.apptainer_version == "1.5.5-1"
    assert caps.batch_jobs is True and caps.containers is False
    assert caps.display == "unsupported" and "login node" in caps.display_reason
    assert caps.runtime is None
    assert caps.os == "Rocky Linux 9.8"
    names = {p.name: p for p in caps.partitions}
    assert set(names) == {"cpu", "gpu"}
    cpu, gpu = names["cpu"], names["gpu"]
    assert cpu.default and cpu.nodes == 2 and cpu.cpus_total == 16 and cpu.cpus_idle == 16
    assert cpu.cpus_alloc == 0 and cpu.time_limit == "infinite" and cpu.state == "up"
    # free memory is what Slurm has not allocated (RealMemory - AllocMem), not OS MemFree
    assert cpu.mem_total_mb == 15724 * 2 and cpu.mem_free_mb == 15724 * 2 and cpu.gres is None
    assert not gpu.default and gpu.nodes == 0 and gpu.cpus_total == 0
    assert res.source == "slurm"
    assert res.cpu_total == 16 and res.cpu_used == 0
    assert res.mem_total_mb == 15724 * 2 and res.mem_used_mb == 0      # idle cluster: 0 allocated


def test_parse_local_source_and_empty_input():
    _caps, res = pr.parse_probe("local", "==os\nLinux x86_64\n")
    assert res.source == "local"
    caps, res = pr.parse_probe("ssh", "")
    assert caps.runtime is None and caps.display == "unsupported"
    assert res.cpu_total is None and res.mem_total_mb is None and res.source == "host"


def test_parse_runtime_choice_and_display_image():
    out = ("==os\nLinux x86_64\n==runtime\nrt=podman\nversion=5.8.4\nrootless=true\n"
           "rt=docker\nversion=27.1.1\nrootless=false\n==display\npodman=no\ndocker=yes\n")
    caps, _ = pr.parse_probe("ssh", out)
    assert caps.runtime == "podman" and caps.display == "buildable"
    caps, _ = pr.parse_probe("ssh", out, runtime_pref="docker")
    assert caps.runtime == "docker" and caps.runtime_version == "27.1.1"
    assert caps.rootless is False and caps.display == "supported"
    # preferred runtime not usable -> fall back to the other one
    broken = out.replace("rt=docker\nversion=27.1.1\nrootless=false", "rt=docker\nversion=\nrootless=")
    caps, _ = pr.parse_probe("ssh", broken, runtime_pref="docker")
    assert caps.runtime == "podman"


def test_parse_installed_but_unusable_runtime():
    out = "==os\nLinux x86_64\n==runtime\nrt=docker\nversion=27.1.1\nrootless=\n==display\ndocker=no\n"
    caps, _ = pr.parse_probe("ssh", out)
    assert caps.runtime == "docker" and caps.containers is False
    assert caps.display == "unsupported"


def test_parse_gpu_and_slurm_node_dedupe():
    out = ("==gpu\n0, NVIDIA RTX A2000, 8192, 512, 4\n1, NVIDIA RTX A2000, 8192, 0, 0\n"
           "==slurm\nversion=slurm 24.11.5\nsinfo_rc=0\n"
           "part|a*|up|1-00:00:00|2|4/12/0/16|1000+|gpu:a100:4\n"
           "part|b|up|infinite|1|2/6/0/8|1000|(null)\n"
           "node|n1|a*|2/6/0/8|1000|N/A|mixed\nnode|n2|a*|2/6/0/8|1000|N/A|mixed\n"
           "node|n1|b|2/6/0/8|1000|N/A|mixed\n")
    caps, res = pr.parse_probe("slurm", out)
    assert [g.index for g in caps.gpus] == [0, 1] and caps.gpus[0].util_pct == 4.0
    assert res.cpu_total == 16 and res.cpu_used == 4  # n1 counted once
    assert caps.partitions[0].gres == "gpu:a100:4" and caps.partitions[0].mem_free_mb is None
    assert res.mem_used_mb is None


# --------------------------------------------------------------------------- classify


def test_classify_offline():
    assert pr.classify("ssh", False, None, None)[0] == "offline"


@pytest.mark.parametrize("stderr", [
    "dev@x1carbon: Permission denied (publickey,gssapi-keyex,gssapi-with-mic,password).",
    "Received disconnect: Too many authentication failures",
])
def test_classify_auth_required(stderr):
    status, err = pr.classify("ssh", True, exec_result(rc=255, stderr=stderr), None)
    assert status == "auth_required" and err


def test_classify_auth_failure_fixture():
    status, err = pr.classify("ssh", True, exec_result(rc=255, stderr=fixture_text("probe_auth_failure.txt")), None)
    assert status == "auth_required"
    assert "Permission denied" in err and "Warning" not in err


def test_classify_host_key_failure_has_hint():
    status, err = pr.classify("ssh", True, exec_result(rc=255, stderr="Host key verification failed."), None)
    assert status == "auth_required" and "ssh" in err


def test_classify_rc255_network_vs_other():
    assert pr.classify("ssh", True, exec_result(rc=255, stderr="ssh: connect to host x port 22: Connection refused"), None)[0] == "offline"
    assert pr.classify("ssh", True, exec_result(rc=255, stderr="ssh: Could not resolve hostname x"), None)[0] == "offline"
    assert pr.classify("ssh", True, exec_result(rc=255, stderr="kex_exchange_identification: weird"), None)[0] == "degraded"


def test_classify_degraded_cases(x1_out, slurm_out):
    ok = exec_result(stdout=x1_out)
    assert pr.classify("ssh", True, exec_result(rc=124, timed_out=True), None)[0] == "degraded"
    assert pr.classify("ssh", True, exec_result(rc=127, stderr="boom"), None)[0] == "degraded"
    assert pr.classify("ssh", True, exec_result(stdout="garbage"), None)[0] == "degraded"
    no_rt, _ = pr.parse_probe("ssh", "==os\nLinux x86_64\n==runtime\n")
    status, err = pr.classify("ssh", True, exec_result(stdout="==os\nLinux x86_64\n"), no_rt)
    assert status == "degraded" and "runtime" in err
    no_sinfo, _ = pr.parse_probe("slurm", "==os\nLinux x86_64\n==slurm\nversion=slurm 26.05.5\nsinfo_rc=1\n")
    assert pr.classify("slurm", True, exec_result(stdout="==os\n"), no_sinfo)[0] == "degraded"
    caps, _ = pr.parse_probe("ssh", x1_out)
    assert pr.classify("ssh", True, ok, caps) == ("online", None)
    caps, _ = pr.parse_probe("slurm", slurm_out)
    assert pr.classify("slurm", True, exec_result(stdout=slurm_out), caps) == ("online", None)


# --------------------------------------------------------------------------- Prober.probe


def _setup(services, host, **script):
    services.store.upsert_host(host)
    t = services.transports.get(host.id)
    return t


def test_probe_online_stores_state_and_emits_once(services, prober, x1_out):
    host = sample_host()
    t = _setup(services, host)
    t.script_script("==os", exec_result(stdout=x1_out))
    state = prober.probe(host)
    assert state.status == "online" and state.last_error is None
    assert state.latency_ms == 1.0 and state.last_seen and state.probed_at
    assert state.capabilities.runtime == "podman"
    assert state.resources.source == "host"
    stored = services.store.get_host_state(host.id)
    assert stored.status == "online"
    assert services.events.payloads("host.status") == [
        {"id": host.id, "status": "online", "prev": "unknown", "last_error": None}]
    assert t.calls[-1].timeout == pr.PROBE_TIMEOUT_S
    assert t.scripts()[-1] == pr.PROBE_SCRIPT
    prober.probe(host)
    assert len(services.events.payloads("host.status")) == 1  # no change, no event


def test_probe_uses_prefixed_transport(services, prober, x1_out):
    host = sample_slurm_host()
    t = _setup(services, host)
    t.script_script("==os", exec_result(stdout=x1_out))
    prober.probe(host)
    assert services.transports.requests[-1] == (host.id, True)


def test_probe_offline_keeps_previous_capabilities(services, prober, x1_out):
    host = sample_host()
    t = _setup(services, host)
    t.script_script("==os", exec_result(stdout=x1_out))
    prober.probe(host)
    services.events.clear()
    t.latency_ms = None
    state = prober.probe(host)
    assert state.status == "offline" and state.latency_ms is None
    assert state.capabilities.runtime == "podman" and state.resources is not None
    assert state.last_seen is not None
    assert services.events.payloads("host.status")[0]["prev"] == "online"


def test_probe_auth_required_and_degraded(services, prober, x1_out):
    host = sample_host()
    t = _setup(services, host)
    t.script_script("==os", [
        exec_result(rc=255, stderr="x: Permission denied (publickey)."),
        exec_result(stdout="==os\nLinux x86_64\n==runtime\n"),
    ])
    s1 = prober.probe(host)
    assert s1.status == "auth_required" and "Permission denied" in s1.last_error
    assert s1.last_seen is None
    s2 = prober.probe(host)
    assert s2.status == "degraded" and s2.capabilities.runtime is None
    assert [p["status"] for p in services.events.payloads("host.status")] == ["auth_required", "degraded"]
    assert services.events.payloads("host.status")[1]["prev"] == "auth_required"


def test_probe_slurm_fixture(services, prober, slurm_out):
    host = sample_slurm_host()
    t = _setup(services, host)
    t.script_script("==os", exec_result(stdout=slurm_out))
    state = prober.probe(host)
    assert state.status == "online"
    assert state.capabilities.scheduler_version == "26.05.5"
    assert state.resources.source == "slurm"


# --------------------------------------------------------------------------- liveness


def test_liveness_offline_then_unchanged(services, prober, x1_out):
    host = sample_host()
    t = _setup(services, host)
    t.script_script("==os", exec_result(stdout=x1_out))
    prober.probe(host)
    services.events.clear()
    t.latency_ms = None
    assert prober.liveness(host) == "offline"
    st = services.store.get_host_state(host.id)
    assert st.status == "offline" and st.capabilities.runtime == "podman"
    assert services.events.payloads("host.status") == [
        {"id": host.id, "status": "offline", "prev": "online", "last_error": "TCP connection failed"}]
    assert prober.liveness(host) == "offline"
    assert len(services.events.events) == 1


def test_liveness_never_marks_online_from_tcp(services, prober):
    host = sample_host()
    t = _setup(services, host)
    t.latency_ms = None
    prober.liveness(host)  # offline, no previous state
    t.latency_ms = 4.0
    assert prober.liveness(host) == "offline"
    assert services.store.get_host_state(host.id).status == "offline"
    # never probed: unknown and nothing stored
    other = sample_host(id="other", address="other")
    services.store.upsert_host(other)
    services.transports.get("other")
    assert prober.liveness(other) == "unknown"
    assert services.store.get_host_state("other") is None


def test_liveness_updates_latency_when_online(services, prober):
    host = sample_host()
    t = _setup(services, host)
    services.store.set_host_state(sample_host_state(latency_ms=99.0))
    t.latency_ms = 7.5
    assert prober.liveness(host) == "online"
    assert services.store.get_host_state(host.id).latency_ms == 7.5
    assert services.events.events == []


# --------------------------------------------------------------------------- refresh_all


def test_refresh_all_shallow_and_deep(services, prober, x1_out, slurm_out):
    a, b = sample_host(), sample_slurm_host()
    ta, tb = _setup(services, a), _setup(services, b)
    ta.script_script("==os", exec_result(stdout=x1_out))
    tb.script_script("==os", exec_result(stdout=slurm_out))
    states = prober.refresh_all(True)
    assert {s.host_id: s.status for s in states} == {"x1carbon": "online", "slurm-x1": "online"}
    ta.latency_ms = None
    states = prober.refresh_all(False)
    assert {s.host_id: s.status for s in states} == {"x1carbon": "offline", "slurm-x1": "online"}


def test_refresh_all_empty(services, prober):
    assert prober.refresh_all(True) == []


def test_refresh_all_never_overlaps_a_host(services, prober, x1_out):
    host = sample_host()
    t = _setup(services, host)
    started, release = threading.Event(), threading.Event()
    runs = []

    def slow(_script):
        runs.append(1)
        started.set()
        release.wait(5)
        return exec_result(stdout=x1_out)

    t.script_script("==os", slow)
    worker = threading.Thread(target=prober.probe, args=(host,), daemon=True)
    worker.start()
    assert started.wait(5)
    assert prober.is_probing(host.id)
    states = prober.refresh_all(True)  # skips the host that is still probing
    assert len(runs) == 1
    assert states == [] or all(s.status != "online" for s in states)
    release.set()
    worker.join(5)
    assert not prober.is_probing(host.id)
    assert services.store.get_host_state(host.id).status == "online"


# --------------------------------------------------------------------------- test_connection


STEP_ORDER = ["dns", "tcp", "auth", "runtime", "scheduler", "display", "gpu"]
SLURM_STEP_ORDER = ["dns", "tcp", "auth", "runtime", "scheduler", "workdir", "display", "gpu"]


def _assert_no_side_effects(services):
    assert services.store.get_host_state("x1carbon") is None
    assert services.store.get_host_state("slurm-x1") is None
    assert services.events.events == []


def test_connection_ssh_success(services, prober, x1_out):
    host = sample_host()
    t = services.transports.get(host.id)
    t.script(["true"], exec_result(stdout=""))
    t.script_script("==os", exec_result(stdout=x1_out))
    res = prober.test_connection(host)
    assert [s.name for s in res.steps] == STEP_ORDER
    by = {s.name: s for s in res.steps}
    assert res.ok and res.status == "online"
    assert by["dns"].ok and by["tcp"].ok and by["auth"].ok and by["runtime"].ok
    assert "podman 5.8.4" in by["runtime"].detail and "rootless" in by["runtime"].detail
    assert by["scheduler"].ok is None and by["scheduler"].detail == "Not a Slurm host"
    assert by["display"].ok is True
    assert by["gpu"].ok is None
    assert res.capabilities.runtime == "podman"
    assert res.to_dict()["steps"][0]["name"] == "dns"
    _assert_no_side_effects(services)
    assert services.transports.requests[0] == (host.id, False)  # auth over the raw connection


def test_connection_test_names_the_program_that_served_the_host(services, prober, x1_out):
    host = sample_host()
    t = services.transports.get(host.id)
    t.script(["true"], exec_result(stdout=""))
    t.script_script("==os", exec_result(stdout=x1_out))
    t.note = "Uses the ssh program, as the SSH config sets a ProxyCommand"
    by = {s.name: s for s in prober.test_connection(host).steps}
    assert by["auth"].detail == ("Logged in. Uses the ssh program, as the SSH config sets a "
                                 "ProxyCommand")


def test_connection_slurm_success(services, prober, slurm_out):
    host = sample_slurm_host()
    t = services.transports.get(host.id)
    t.script(["true"], exec_result())
    t.script_script("==os", exec_result(stdout=slurm_out))
    res = prober.test_connection(host)
    by = {s.name: s for s in res.steps}
    assert res.ok and by["scheduler"].ok and "26.05.5" in by["scheduler"].detail
    assert "cpu" in by["scheduler"].detail
    assert by["runtime"].ok is None
    assert by["display"].ok is None
    _assert_no_side_effects(services)


def test_connection_dns_failure_skips_rest(services, prober, monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror(-2, "Name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    host = sample_host(address="nope.invalid")
    t = services.transports.get(host.id)
    t.latency_ms = None
    res = prober.test_connection(host)
    assert [s.name for s in res.steps] == STEP_ORDER
    assert res.steps[0].ok is False and res.steps[0].hint
    assert all(s.ok is None and s.detail == "Skipped" for s in res.steps[1:])
    assert not res.ok and res.status == "offline"
    _assert_no_side_effects(services)


def test_connection_alias_dns_failure_but_tcp_ok(services, prober, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("x")))
    host = sample_host()
    t = services.transports.get(host.id)
    t.script(["true"], exec_result())
    t.script_script("==os", exec_result(stdout=fixture_text("probe_x1carbon.txt")))
    res = prober.test_connection(host)
    assert res.steps[0].ok is True and "SSH" in res.steps[0].detail
    assert res.ok


def test_connection_tcp_failure(services, prober):
    host = sample_host()
    services.transports.get(host.id).latency_ms = None
    res = prober.test_connection(host)
    by = {s.name: s for s in res.steps}
    assert by["dns"].ok and by["tcp"].ok is False and by["tcp"].hint
    assert all(by[n].detail == "Skipped" for n in STEP_ORDER[2:])
    assert res.status == "offline"
    _assert_no_side_effects(services)


def test_connection_auth_failure(services, prober):
    host = sample_host()
    services.transports.get(host.id).script(
        ["true"], exec_result(rc=255, stderr=fixture_text("probe_auth_failure.txt")))
    res = prober.test_connection(host)
    auth = {s.name: s for s in res.steps}["auth"]
    assert auth.ok is False
    assert auth.hint == ("Run `ssh x1carbon` once in a terminal to accept the host key / "
                         "unlock your key, or load it into ssh-agent")
    assert res.status == "auth_required" and not res.ok
    assert all(s.detail == "Skipped" for s in res.steps[3:])
    _assert_no_side_effects(services)


def test_connection_no_runtime_gives_hint(services, prober):
    host = sample_host()
    t = services.transports.get(host.id)
    t.script(["true"], exec_result())
    t.script_script("==os", exec_result(stdout="==os\nLinux x86_64\n==runtime\n"))
    res = prober.test_connection(host)
    rt = {s.name: s for s in res.steps}["runtime"]
    assert rt.ok is False and rt.hint and "Podman" in rt.hint
    assert res.status == "degraded" and not res.ok
    _assert_no_side_effects(services)


def test_connection_slurm_without_sinfo(services, prober):
    host = sample_slurm_host()
    t = services.transports.get(host.id)
    t.script(["true"], exec_result())
    t.script_script("==os", exec_result(stdout="==os\nLinux x86_64\n==slurm\n"))
    res = prober.test_connection(host)
    sched = {s.name: s for s in res.steps}["scheduler"]
    assert sched.ok is False and sched.hint
    assert res.status == "degraded"


def test_connection_probe_script_failure(services, prober):
    host = sample_slurm_host()
    t = services.transports.get(host.id)
    t.script(["true"], exec_result())
    t.script_script("==os", exec_result(rc=125, stderr="Error: no container with name or ID \"slurmctld\" found"))
    res = prober.test_connection(host)
    assert [s.name for s in res.steps] == SLURM_STEP_ORDER
    sched = {s.name: s for s in res.steps}["scheduler"]
    assert sched.ok is False and "slurmctld" in sched.detail and sched.hint
    assert res.status == "degraded"


def test_connection_local_host(services, prober, x1_out):
    host = sample_local_host()
    t = services.transports.get(host.id)
    t.script(["true"], exec_result())
    t.script_script("==os", exec_result(stdout=x1_out))
    res = prober.test_connection(host)
    assert res.steps[0].ok is None and res.ok


def test_provide_returns_prober(services):
    assert isinstance(pr.provide(services), pr.Prober)


# --------------------------------------------------------------------------- live


@pytest.mark.live
def test_live_probe_x1carbon_and_slurm(tmp_path):
    try:
        from cloud_compute_core import transport
    except ModuleNotFoundError:
        pytest.skip("transport module not present")
    from cloud_compute_core import config as config_module
    from cloud_compute_core import store as store_module

    st = store_module.Store(tmp_path / "live.db")
    events = FakeEvents()
    cfg = config_module.Config()
    holder = SimpleNamespace(store=st, events=events, config=cfg, transports=None)
    holder.transports = transport.TransportFactory(cfg)
    p = pr.Prober(holder)
    try:
        for host, timings in ((sample_host(), "x1carbon"), (sample_slurm_host(), "slurm")):
            st.upsert_host(host)
            t0 = time.perf_counter()
            state = p.probe(host)
            print(f"live probe {timings}: {state.status} in {time.perf_counter() - t0:.2f}s")
            assert state.status == "online", state.last_error
            if host.kind == "slurm":
                assert state.capabilities.scheduler == "slurm"
                assert {pt.name for pt in state.capabilities.partitions} >= {"cpu"}
            else:
                assert state.capabilities.runtime == "podman"
        res = p.test_connection(sample_host())
        assert [s.name for s in res.steps] == STEP_ORDER and res.ok
    finally:
        st.close()


def test_parse_slurmctld_busy_reports_allocated_cpu_and_memory():
    caps, res = pr.parse_probe("slurm", fixture_text("probe_slurmctld_busy.txt"))
    assert res.source == "slurm"
    assert res.cpu_total == 16 and res.cpu_used == 3          # CPUAlloc 2 + 1 of CPUTot 8 + 8
    assert res.mem_total_mb == 15724 * 2                      # RealMemory summed
    assert res.mem_used_mb == 1024 + 512                      # AllocMem summed, not RealMemory-FreeMem
    cpu = {p.name: p for p in caps.partitions}["cpu"]
    assert cpu.mem_free_mb == 15724 * 2 - (1024 + 512)        # unallocated, not MemFree (880 + 1700)


def test_slurm_memory_is_not_derived_from_os_free_memory():
    # An idle node whose OS has almost no free memory: the old FreeMem-based meter said "full".
    out = ("==slurm\nversion=slurm 26.05.5\nsinfo_rc=0\n"
           "part|cpu*|up|infinite|1|0/8/0/8|15724|(null)\n"
           "node|c1|cpu*|0/8/0/8|15724|100|idle\n"
           "scn|NodeName=c1 CPUAlloc=0 CPUTot=8 RealMemory=15724 AllocMem=0 FreeMem=100 "
           "State=IDLE Partitions=cpu\n")
    caps, res = pr.parse_probe("slurm", out)
    assert res.mem_used_mb == 0 and caps.partitions[0].mem_free_mb == 15724


def test_slurm_scontrol_multi_partition_node_counts_once_in_host_total():
    out = ("==slurm\nversion=slurm 26.05.5\nsinfo_rc=0\n"
           "part|a*|up|infinite|1|2/6/0/8|1000|(null)\npart|b|up|infinite|1|2/6/0/8|1000|(null)\n"
           "scn|NodeName=n1 CPUAlloc=2 CPUTot=8 RealMemory=1000 AllocMem=400 FreeMem=10 "
           "State=MIXED Partitions=a,b\n")
    caps, res = pr.parse_probe("slurm", out)
    assert [p.mem_free_mb for p in caps.partitions] == [600, 600]
    assert res.cpu_total == 8 and res.cpu_used == 2
    assert res.mem_total_mb == 1000 and res.mem_used_mb == 400


def test_slurm_without_scontrol_keeps_cpu_but_memory_used_is_unknown():
    out = ("==slurm\nversion=slurm 26.05.5\nsinfo_rc=0\n"
           "part|cpu*|up|infinite|1|2/6/0/8|1000|(null)\n"
           "node|c1|cpu*|2/6/0/8|1000|300|mixed\n")
    _caps, res = pr.parse_probe("slurm", out)
    assert res.cpu_total == 8 and res.cpu_used == 2
    assert res.mem_total_mb == 1000 and res.mem_used_mb is None


def test_slurm_scontrol_with_missing_allocmem_leaves_memory_unknown():
    out = ("==slurm\nversion=slurm 26.05.5\nsinfo_rc=0\n"
           "scn|NodeName=n1 CPUAlloc=1 CPUTot=4 RealMemory=1000 State=IDLE Partitions=a\n")
    _caps, res = pr.parse_probe("slurm", out)
    assert res.cpu_used == 1 and res.cpu_total == 4
    assert res.mem_total_mb == 1000 and res.mem_used_mb is None


def test_ssh_host_keeps_os_numbers(x1_out):
    _caps, res = pr.parse_probe("ssh", x1_out)
    assert res.mem_used_mb == (16102016 - 12720924) // 1024 and res.cpu_used == pytest.approx(0.16)


def test_probe_script_asks_scontrol_for_allocation():
    assert "scontrol show node -o" in pr.PROBE_SCRIPT and "scn|" in pr.PROBE_SCRIPT


# --------------------------------------------------------------------------- work directory row


def _is_workdir_check(argv, stdin):
    return argv[:2] == ["sh", "-c"] and "cc-workdir-check" in argv[2]


def _wd_answer(path, rc, *out):
    def produce(argv, stdin):
        token = argv[5]
        lines = [f"cc_path={path}", f"cc_rc={rc}"]
        lines += ["cc_out=" + o.replace("@TOKEN@", token) for o in out]
        return exec_result(stdout="\n".join(lines) + "\n")
    return produce


def _slurm_test(services, prober, slurm_out, answer, **host_kw):
    host = sample_slurm_host(**host_kw)
    t = services.transports.get(host.id)
    t.script(["true"], exec_result())
    t.script_script("==os", exec_result(stdout=slurm_out))
    if answer is not None:
        t.script(_is_workdir_check, answer)
    return prober.test_connection(host), t


def test_workdir_row_passes_for_a_shared_folder(services, prober, slurm_out):
    res, t = _slurm_test(services, prober, slurm_out,
                         _wd_answer("/data/cloud-compute", 0, "cc_node=c1", "@TOKEN@"))
    assert [s.name for s in res.steps] == SLURM_STEP_ORDER
    wd = {s.name: s for s in res.steps}["workdir"]
    assert wd.ok is True and "/data/cloud-compute" in wd.detail and "c1" in wd.detail
    assert res.ok and res.status == "online"
    (call,) = [c for c in t.calls if c.kind == "run" and _is_workdir_check(c.argv, None)]
    assert call.timeout <= 30
    assert call.argv[4] == "/data/cloud-compute"            # the host's workdir, as an argument
    assert "--immediate=" in call.argv[2] and "timeout 20 srun" in call.argv[2]
    _assert_no_side_effects(services)


def test_workdir_row_fails_for_a_folder_compute_nodes_cannot_see(services, prober, slurm_out):
    """~/cloud-compute is /root/... on the login container, not shared."""
    from cloud_compute_core.models import SlurmSettings
    res, _ = _slurm_test(
        services, prober, slurm_out,
        _wd_answer("/root/cloud-compute", 1, "cc_node=c1",
                   "cat: /root/cloud-compute/.cc-workdir-check-x: No such file or directory",
                   "srun: error: c1: task 0: Exited with exit code 1"),
        slurm=SlurmSettings(workdir="~/cloud-compute"))
    wd = {s.name: s for s in res.steps}["workdir"]
    assert wd.ok is False and "/root/cloud-compute" in wd.detail and "c1" in wd.detail
    assert "Work directory" in wd.hint and "share" in wd.hint
    assert res.status == "degraded" and not res.ok


def test_workdir_row_could_not_check_when_no_node_is_free(services, prober, slurm_out):
    res, _ = _slurm_test(services, prober, slurm_out, _wd_answer(
        "/data/cc", 1, "srun: error: Unable to allocate resources: Immediate execution impossible"))
    wd = {s.name: s for s in res.steps}["workdir"]
    assert wd.ok is None and "could not check" in wd.detail and wd.hint
    assert res.ok and res.status == "online"


def test_workdir_row_could_not_check_on_timeout_or_transport_error(services, prober, slurm_out):
    res, _ = _slurm_test(services, prober, slurm_out,
                         exec_result(rc=124, timed_out=True, stdout="cc_path=/data/cc\n"))
    wd = {s.name: s for s in res.steps}["workdir"]
    assert wd.ok is None and "timed out" in wd.detail and res.ok

    def boom(argv, stdin):
        raise OSError("pipe closed")
    res, _ = _slurm_test(services, prober, slurm_out, boom, id="slurm-x2")
    wd = {s.name: s for s in res.steps}["workdir"]
    assert wd.ok is None and "pipe closed" in wd.detail and res.ok


def test_workdir_row_not_writable_and_no_srun(services, prober, slurm_out):
    res, _ = _slurm_test(services, prober, slurm_out,
                         exec_result(stdout="cc_path=/ro\ncc_path=/ro\ncc_wd=not_writable\n"))
    wd = {s.name: s for s in res.steps}["workdir"]
    assert wd.ok is False and "/ro is not writable" in wd.detail and res.status == "degraded"
    res, _ = _slurm_test(services, prober, slurm_out,
                         exec_result(stdout="cc_path=/w\ncc_wd=no_srun\n"), id="slurm-x2")
    assert {s.name: s for s in res.steps}["workdir"].ok is None


def test_workdir_row_skipped_when_slurm_does_not_answer(services, prober):
    host = sample_slurm_host()
    t = services.transports.get(host.id)
    t.script(["true"], exec_result())
    t.script_script("==os", exec_result(stdout="==os\nLinux x86_64\n==slurm\n"))
    res = prober.test_connection(host)
    assert [s.name for s in res.steps] == SLURM_STEP_ORDER
    assert {s.name: s for s in res.steps}["workdir"].detail == "Skipped"
    assert not [c for c in t.calls if c.kind == "run" and _is_workdir_check(c.argv, None)]


def test_ssh_hosts_have_no_workdir_row(services, prober, x1_out):
    host = sample_host()
    t = services.transports.get(host.id)
    t.script(["true"], exec_result())
    t.script_script("==os", exec_result(stdout=x1_out))
    assert [s.name for s in prober.test_connection(host).steps] == STEP_ORDER


def test_deep_probe_never_runs_the_workdir_check(services, prober, slurm_out):
    host = sample_slurm_host()
    t = services.transports.get(host.id)
    t.script_script("==os", exec_result(stdout=slurm_out))
    prober.probe(host)
    assert not [c for c in t.calls if c.kind == "run"]


def test_workdir_check_script_passes_settings_as_arguments(services, prober, slurm_out):
    from cloud_compute_core.models import SlurmSettings
    _, t = _slurm_test(services, prober, slurm_out, _wd_answer("/x", 0, "@TOKEN@"),
                       slurm=SlurmSettings(workdir="/x; rm -rf /", default_partition="cpu",
                                           account="lab", qos="normal"))
    (call,) = [c for c in t.calls if c.kind == "run" and _is_workdir_check(c.argv, None)]
    assert call.argv[3:] == ["sh", "/x; rm -rf /", call.argv[5], "cpu", "lab", "normal"]
    assert "/x; rm -rf /" not in call.argv[2]
