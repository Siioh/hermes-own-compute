"""Tests for core/runtimes.py: argv builders and parsers for podman and docker."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

rt = importlib.import_module("cloud_compute_core.runtimes")
RunSpec = rt.RunSpec

FIX = Path(__file__).parent / "fixtures" / "hosts"
BOTH = pytest.mark.parametrize("name", ["podman", "docker"])


def spec(**kw):
    base = dict(name="cc-i-1", image="img:1", labels={"a": "b"}, command=[])
    base.update(kw)
    return RunSpec(**base)


def test_constants():
    assert rt.MANAGED_LABEL == "io.cloud-compute.managed"
    assert rt.DISPLAY_IMAGE_TAG == "localhost/cloud-compute-novnc:0.1"
    assert rt.DISPLAY_LABEL_VALUE == "novnc:6080" and rt.DISPLAY_PORT == 6080
    assert rt.container_name("i-0a1b2c3d") == "cc-i-0a1b2c3d"
    labels = rt.instance_labels("i-1", "inst", "tpl", display=True)
    assert labels == {
        "io.cloud-compute.managed": "1", "io.cloud-compute.instance": "i-1",
        "io.cloud-compute.install": "inst", "io.cloud-compute.template": "tpl",
        "io.cloud-compute.display": "novnc:6080",
    }
    assert "io.cloud-compute.display" not in rt.instance_labels("i-1", "inst", "tpl")


def test_runtime_for():
    assert rt.runtime_for("podman").name == "podman"
    assert rt.runtime_for("docker").name == "docker"
    with pytest.raises(ValueError):
        rt.runtime_for("lxc")


@BOTH
def test_run_minimal(name):
    argv = rt.runtime_for(name).run_argv(spec(command=["sleep", "infinity"]))
    assert argv == [name, "run", "-d", "--name", "cc-i-1", "--init", "--security-opt",
                    "no-new-privileges", "--label", "a=b", "img:1", "sleep", "infinity"]


@BOTH
def test_run_full(name):
    s = spec(labels=rt.instance_labels("i-1", "x", "t", display=True), cpus=2.0, memory_gb=4,
             gpus=1, network="none", env_file="/tmp/e.env", workdir="/work",
             publish=[("127.0.0.1", None, 6080), ("127.0.0.1", 8080, 80)],
             command=["bash", "-c", "echo hi there"])
    argv = rt.runtime_for(name).run_argv(s)
    gpu = ["--device", "nvidia.com/gpu=all"] if name == "podman" else ["--gpus", "1"]
    assert argv[:8] == [name, "run", "-d", "--name", "cc-i-1", "--init", "--security-opt",
                        "no-new-privileges"]
    for k, v in s.labels.items():
        i = argv.index(f"{k}={v}")
        assert argv[i - 1] == "--label"
    tail = argv[argv.index("--cpus"):]
    assert tail == ["--cpus", "2", "--memory", "4g", *gpu, "--network", "none",
                    "--env-file", "/tmp/e.env", "-w", "/work",
                    "-p", "127.0.0.1::6080", "-p", "127.0.0.1:8080:80",
                    "img:1", "bash", "-c", "echo hi there"]


@BOTH
def test_run_fractional(name):
    argv = rt.runtime_for(name).run_argv(spec(cpus=0.5, memory_gb=1.5))
    assert argv[argv.index("--cpus") + 1] == "0.5"
    assert argv[argv.index("--memory") + 1] == "1536m"
    assert "--gpus" not in argv and "--device" not in argv and "--network" not in argv


@BOTH
@pytest.mark.parametrize("bad", [
    dict(name="-x"), dict(name="a b"), dict(name="a\nb"), dict(name="a\x00b"), dict(name=""),
    dict(image="-evil"), dict(image="a b"), dict(image=""),
    dict(labels={"k k": "v"}), dict(labels={"k": "v\nw"}), dict(labels={"k=": "v"}),
    dict(workdir="/a b"), dict(env_file="/a\nb"), dict(cpus=0), dict(memory_gb=-1),
    dict(network="host"), dict(command=["a\x00"]),
    dict(publish=[("0.0.0.0", None, 80)]), dict(publish=[("", 1, 80)]),
    dict(publish=[("127.0.0.1", 0, 80)]), dict(publish=[("127.0.0.1", None, 70000)]),
])
def test_run_rejects(name, bad):
    with pytest.raises(ValueError):
        rt.runtime_for(name).run_argv(spec(**bad))


@BOTH
def test_ps_argv(name):
    fmt = "json" if name == "podman" else "{{json .}}"
    r = rt.runtime_for(name)
    assert r.ps_argv() == [name, "ps", "-a", "--filter", "label=io.cloud-compute.managed=1",
                           "--format", fmt]
    assert r.ps_argv("x=y")[4] == "label=x=y"
    with pytest.raises(ValueError):
        r.ps_argv("a b")


@BOTH
def test_stats_argv(name):
    fmt = "json" if name == "podman" else "{{json .}}"
    r = rt.runtime_for(name)
    assert r.stats_argv(["a", "b"]) == [name, "stats", "--no-stream", "--format", fmt, "a", "b"]
    assert r.stats_argv([]) == [name, "stats", "--no-stream", "--format", fmt]
    with pytest.raises(ValueError):
        r.stats_argv(["-a"])


@BOTH
def test_inspect_lifecycle(name):
    r = rt.runtime_for(name)
    assert r.inspect_argv("abc") == [name, "inspect", "abc"]
    assert r.lifecycle_argv("start", "abc") == [name, "start", "abc"]
    assert r.lifecycle_argv("stop", "abc") == [name, "stop", "abc"]
    assert r.lifecycle_argv("rm", "abc") == [name, "rm", "-f", "abc"]
    assert r.lifecycle_argv("stop", "abc", timeout=3) == [name, "stop", "-t", "3", "abc"]
    assert r.lifecycle_argv("stop", "abc", timeout=0) == [name, "stop", "-t", "0", "abc"]
    assert r.lifecycle_argv("start", "abc", timeout=3) == [name, "start", "abc"]
    assert r.lifecycle_argv("rm", "abc", timeout=3) == [name, "rm", "-f", "abc"]
    for bad in (-1, "3", True, 1.5):
        with pytest.raises(ValueError):
            r.lifecycle_argv("stop", "abc", timeout=bad)
    with pytest.raises(ValueError):
        r.lifecycle_argv("kill", "abc")
    for fn in (r.inspect_argv, lambda c: r.lifecycle_argv("start", c)):
        for bad in ("-f", "a b", "", "a\nb"):
            with pytest.raises(ValueError):
                fn(bad)


@BOTH
def test_exec(name):
    r = rt.runtime_for(name)
    assert r.exec_argv("c1", ["ls", "-l"]) == [name, "exec", "c1", "ls", "-l"]
    assert r.exec_argv("c1", ["bash"], tty=True) == [name, "exec", "-it", "c1", "bash"]
    assert r.exec_argv("c1", ["pwd"], workdir="/w") == [name, "exec", "-w", "/w", "c1", "pwd"]
    assert r.exec_argv("c1", ["sh"], tty=True, workdir="/w") == \
        [name, "exec", "-it", "-w", "/w", "c1", "sh"]
    with pytest.raises(ValueError):
        r.exec_argv("c1", ["x"], workdir="a b")


@BOTH
def test_logs_port(name):
    r = rt.runtime_for(name)
    assert r.logs_argv("c1") == [name, "logs", "--tail", "200", "c1"]
    assert r.logs_argv("c1", 5) == [name, "logs", "--tail", "5", "c1"]
    assert r.port_argv("c1", 6080) == [name, "port", "c1", "6080/tcp"]
    with pytest.raises(ValueError):
        r.logs_argv("c1", -1)
    with pytest.raises(ValueError):
        r.port_argv("c1", 0)


@BOTH
def test_build_and_image_exists(name):
    r = rt.runtime_for(name)
    assert r.build_argv("t:1") == [name, "build", "-t", "t:1", "-"]
    assert r.build_argv("t:1", context="/tmp/ctx") == [name, "build", "-t", "t:1", "-f", "-",
                                                       "/tmp/ctx"]
    with pytest.raises(ValueError):
        r.build_argv("-t")
    with pytest.raises(ValueError):
        r.build_argv("t", context="a b")
    exp = [name, "image", "exists", "t:1"] if name == "podman" else \
        [name, "image", "inspect", "t:1"]
    assert r.image_exists_argv("t:1") == exp


# ---- parsers -------------------------------------------------------------

def test_parse_ps_podman_fixture():
    rows = rt.runtime_for("podman").parse_ps((FIX / "runtimes_podman_ps.json").read_text())
    assert len(rows) == 2
    assert rows[0]["name"] == "hermes-b310bf89" and rows[0]["state"] == "exited"
    mine = rows[1]
    assert mine["name"] == "cc-i-0a1b2c3d" and mine["state"] == "running"
    assert mine["labels"][rt.LABEL_INSTANCE] == "i-0a1b2c3d"
    assert mine["image"] == rt.DISPLAY_IMAGE_TAG


def test_parse_ps_docker_lines():
    out = "\n".join([
        json.dumps({"ID": "abc123", "Names": "cc-i-1", "Image": "img", "State": "running",
                    "Status": "Up 2 minutes",
                    "Labels": "io.cloud-compute.managed=1,io.cloud-compute.instance=i-1",
                    "Ports": "127.0.0.1:41234->6080/tcp"}),
        "garbage line",
        json.dumps({"ID": "def456", "Names": "/other", "Image": "x", "State": "Exited",
                    "Labels": ""}),
        "{broken json",
    ])
    rows = rt.runtime_for("docker").parse_ps(out)
    assert [r["id"] for r in rows] == ["abc123", "def456"]
    assert rows[0]["labels"] == {"io.cloud-compute.managed": "1",
                                 "io.cloud-compute.instance": "i-1"}
    assert rows[1]["name"] == "other" and rows[1]["state"] == "exited"
    assert rows[1]["labels"] == {}


def test_parse_ps_includes_creation_time():
    podman = json.dumps([{"Id": "a1", "Names": ["cc-i-1"], "Created": 1794220800,
                          "CreatedAt": "2 minutes ago", "State": "running"}])
    docker = json.dumps({"ID": "b2", "Names": "cc-i-2", "State": "running",
                         "CreatedAt": "2026-11-09 07:00:00 -0500 EST"})
    iso = json.dumps({"ID": "c3", "Names": "cc-i-3", "CreatedAt": "2026-11-09T12:00:00Z"})
    bad = json.dumps({"ID": "d4", "Names": "cc-i-4", "CreatedAt": "yesterday-ish"})
    assert rt.runtime_for("podman").parse_ps(podman)[0]["created_at"] == "2026-11-09T10:40:00+00:00"
    assert rt.runtime_for("docker").parse_ps(docker)[0]["created_at"] == "2026-11-09T12:00:00+00:00"
    assert rt.runtime_for("docker").parse_ps(iso)[0]["created_at"] == "2026-11-09T12:00:00+00:00"
    assert rt.runtime_for("docker").parse_ps(bad)[0]["created_at"] == ""


@BOTH
@pytest.mark.parametrize("junk", ["", "   ", "not json", "[1, 2]", "null", "[{}]", "{}"])
def test_parsers_tolerate_junk(name, junk):
    r = rt.runtime_for(name)
    assert r.parse_ps(junk) == []
    assert r.parse_stats(junk) == {}


def test_parse_stats_podman_fixture():
    st = rt.runtime_for("podman").parse_stats((FIX / "runtimes_podman_stats.json").read_text())
    s = st["14ec60af3856"]
    assert st["mysql"] is s
    assert s.cpu_pct == pytest.approx(0.24)
    assert s.mem_used_mb == 101 and s.mem_limit_mb == 15726  # decimal MB/GB -> MiB
    assert s.sampled_at
    assert st["slurmdbd"].mem_used_mb == 6


def test_parse_stats_docker_lines():
    out = "\n".join([
        json.dumps({"ID": "abc", "Name": "cc-i-1", "CPUPerc": "12.50%",
                    "MemUsage": "120MiB / 4GiB", "MemPerc": "2.93%"}),
        "oops",
        json.dumps({"ID": "def", "Name": "cc-i-2", "CPUPerc": "--", "MemUsage": "--"}),
    ])
    st = rt.runtime_for("docker").parse_stats(out)
    assert st["abc"].cpu_pct == 12.5 and st["abc"].mem_used_mb == 120
    assert st["cc-i-1"].mem_limit_mb == 4096
    assert st["def"].cpu_pct is None and st["def"].mem_used_mb is None


@BOTH
@pytest.mark.parametrize("out,expected", [
    ("0.0.0.0:41234\n", 41234),
    ("127.0.0.1:41234", 41234),
    ("[::]:41234", 41234),
    ("6080/tcp -> 127.0.0.1:35000\n", 35000),
    ("0.0.0.0:41234\n[::]:41234\n", 41234),
    ("", None), ("no ports here", None), ("host:99999", None),
])
def test_parse_port(name, out, expected):
    assert rt.runtime_for(name).parse_port(out) == expected


@BOTH
def test_cp_out_and_in(name):
    r = rt.runtime_for(name)
    assert r.cp_out_argv("cc-i-1", "/work") == [name, "cp", "cc-i-1:/work", "-"]
    assert r.cp_out_argv("cc-i-1", "/work/a b/../c/") == [name, "cp", "cc-i-1:/work/c", "-"]
    assert r.cp_in_argv("cc-i-1", "/") == [name, "cp", "-", "cc-i-1:/"]
    assert r.cp_in_argv("cc-i-1", "/work/in") == [name, "cp", "-", "cc-i-1:/work/in"]
    for bad in ("work", "", "/a\nb", "/a\x00b", None):
        with pytest.raises(ValueError):
            r.cp_out_argv("cc-i-1", bad)
        with pytest.raises(ValueError):
            r.cp_in_argv("cc-i-1", bad)
    with pytest.raises(ValueError):
        r.cp_out_argv("-rf", "/work")


def test_container_path_normalises():
    assert rt.container_path("/work/../../etc") == "/etc"
    assert rt.container_path("//work//x/") == "/work/x"
    assert rt.container_path("/") == "/"
    assert rt.container_path("  /work  ") == "/work"
