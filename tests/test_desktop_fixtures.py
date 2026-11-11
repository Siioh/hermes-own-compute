"""Checks for the REST response fixtures used by the desktop test harness.

Fixture shapes are compared with key lists hard-coded here from the MVP spec
(section 6.1, 7.3, 7.7), adjusted where the real core modules differ (the core
modules win). Every model fixture also round-trips through
`Model.from_dict(obj).to_dict()` of `cloud_compute_core.models` with an identical
key set; `/health`, `/summary` and `/host-types` fixtures are compared with what
the real `summary_api` handlers return. `TestResult`/`TestStep` are not core
models yet, so their key lists stay hard-coded.
"""

from __future__ import annotations

import functools
import importlib
import importlib.machinery
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "desktop"

EXTRA_FILES = {
    "routes.json",
    "host_ec2.json",            # EC2 host cards in the smoke test (machine running / stopped)
    "health_unconfigured.json",
    "error_not_found.json",
    "instance_terminal_unlaunched.json",
    "job_request_keys.json",
    "agent_request_keys.json",
    "jobs_stale.json",
    "job_finished_unconfirmed.json",
    "hosts_slurm_mem_unknown.json",
    "agent_collect_diverged.json",
    "instance_destroyed.json",
    "instance_destroyed_empty.json",
}
ALLOWED_MODELS = {
    None,
    "HostWithState",
    "HostWithState[]",
    "HostState",
    "TestResult",
    "TemplateSpec",
    "TemplateSpec[]",
    "SendPreview",
    "InstanceRecord",
    "InstanceRecord[]",
    "JobRecord",
    "JobRecord[]",
    "TunnelRecord",
    "TunnelRecord[]",
    "TerminalLaunch",
    "Partition[]",
    "AgentRun",
    "AgentRun[]",
    "CollectResult",
    "ExportRecord",
}

# The exact set of UI calls (plugin.js __test__.API_CALLS must equal this).
EXPECTED_CALLS = {
    ("GET", "/health"),
    ("GET", "/summary"),
    ("GET", "/host-types"),
    ("GET", "/docs"),
    ("GET", "/docs/{topic}"),
    ("GET", "/hosts"),
    ("POST", "/hosts"),
    ("GET", "/hosts/{id}"),
    ("POST", "/hosts/test"),
    ("PATCH", "/hosts/{id}"),
    ("DELETE", "/hosts/{id}"),
    ("POST", "/hosts/{id}/probe"),
    ("POST", "/hosts/{id}/display-image"),
    ("POST", "/hosts/{id}/terminal"),
    ("GET", "/hosts/{id}/partitions"),
    ("GET", "/templates"),
    ("POST", "/templates"),
    ("GET", "/templates/{id}"),
    ("PATCH", "/templates/{id}"),
    ("DELETE", "/templates/{id}"),
    ("GET", "/templates/{id}/preview"),
    ("GET", "/instances"),
    ("GET", "/instances/{id}"),
    ("POST", "/instances"),
    ("POST", "/instances/{id}/{action}"),
    ("POST", "/instances/stop-all"),
    ("GET", "/instances/{id}/logs"),
    ("GET", "/instances/{id}/files"),
    ("GET", "/instances/{id}/files/size"),
    ("POST", "/instances/{id}/files/fetch"),
    ("POST", "/instances/{id}/files/put"),
    ("GET", "/exports"),
    ("GET", "/exports/{id}"),
    ("POST", "/exports/{id}/restore"),
    ("DELETE", "/exports/{id}"),
    ("POST", "/instances/{id}/terminal"),
    ("POST", "/instances/{id}/display"),
    ("POST", "/instances/{id}/forward"),
    ("GET", "/tunnels"),
    ("DELETE", "/tunnels/{id}"),
    ("GET", "/jobs"),
    ("GET", "/jobs/{id}"),
    ("POST", "/jobs"),
    ("POST", "/jobs/clear-finished"),
    ("DELETE", "/jobs/{id}"),
    ("POST", "/jobs/{id}/cancel"),
    ("GET", "/jobs/{id}/output"),
    ("POST", "/jobs/{id}/terminal"),
    ("GET", "/agents"),
    ("GET", "/agents/{id}"),
    ("POST", "/agents"),
    ("POST", "/agents/{id}/collect"),
    ("POST", "/agents/{id}/stop"),
    ("POST", "/agents/{id}/message"),
    ("POST", "/agents/{id}/rerun"),
    ("DELETE", "/agents/{id}"),
    ("GET", "/agent-models"),
    ("POST", "/agent-models"),
}

# ---------------------------------------------------------------- spec key lists (mvp-spec 6.1 / 7.3 / 7.7)

KEYS = {
    "SlurmSettings": ["default_partition", "account", "qos", "workdir", "container", "max_active_jobs", "poll_seconds"],
    "HostLimits": ["max_instances", "max_cpus", "max_memory_gb", "allow_agents"],
    "Ec2Settings": [
        "region", "access_key_secret", "secret_key_secret", "session_token_secret", "instance_type",
        "image", "disk_gb", "ssh_user", "max_lifetime_minutes", "idle_stop_minutes", "auto_start",
        "subnet_id", "security_group_id", "ssh_cidr", "key_name", "key_file", "known_hosts_file",
        "managed_group_id", "ssh_cidr_used", "instance_id", "machine_state", "machine_detail",
        "launched_at", "started_at", "auto_stop_at",
    ],
    "HostSpec": [
        "id", "name", "kind", "address", "user", "port", "identity_file", "jump_host", "runtime_pref",
        "exec_prefix", "interactive_prefix", "slurm", "ec2", "limits", "created_at",
    ],
    "GpuInfo": ["index", "name", "mem_total_mb", "mem_used_mb", "util_pct"],
    "Partition": [
        "name", "state", "nodes", "cpus_alloc", "cpus_idle", "cpus_total", "mem_total_mb", "mem_free_mb",
        "gres", "time_limit", "default",
    ],
    "Capabilities": [
        "os", "arch", "runtime", "runtime_version", "rootless", "containers", "batch_jobs", "interactive_terminal",
        "port_forward", "display", "display_reason", "gpus", "scheduler", "scheduler_version", "apptainer_version",
        "partitions", "workdir_shared", "workdir_detail",
    ],
    "Resources": [
        "cpu_total", "cpu_used", "mem_total_mb", "mem_used_mb", "disk_path", "disk_total_gb", "disk_free_gb",
        "gpus", "source", "sampled_at",
    ],
    "HostState": [
        "host_id", "status", "last_seen", "last_error", "latency_ms", "capabilities", "resources", "probed_at",
    ],
    "HostWithState": ["spec", "state"],
    "VariableSpec": ["name", "source", "scope", "hosts", "description"],
    "TemplateSpec": [
        "id", "name", "description", "kind", "image", "command", "workdir", "cpus", "memory_gb", "gpus",
        "ttl_minutes", "idle_minutes", "persistent", "network", "display", "variables", "host_kinds", "agent",
        "builtin", "ports",
    ],
    "AgentToggles": ["enabled", "mode", "model", "base_url", "key_secret", "max_turns", "image_base"],
    "AgentRun": [
        "id", "instance_id", "host_id", "goal", "repo_path", "branch", "base_sha", "head_sha", "commits_ahead", "status",
        "started_at", "ended_at", "session_id", "last_log", "mode", "template_id", "collected_ref", "worktree_path",
        "exit_code", "error", "reply", "turns", "model", "endpoint_host", "max_turns", "model_id", "criteria",
        "key_secret", "state_dir", "rerun_of", "error_kind", "new_branch", "collected_head_sha", "stop_note", "text_masked",
    ],
    "CollectResult": ["run_id", "ref", "commits_ahead", "worktree_path", "diverged", "conflicts_dry_run"],
    "ExportRecord": [
        "id", "instance_id", "instance_name", "host_id", "source_path", "local_path", "bytes", "created_at",
        "reason", "files", "extracted_to",
    ],
    "FileListing": ["path", "entries", "truncated"],
    "FileEntry": ["name", "type", "size", "mtime"],
    "Exports": ["exports", "folder"],
    "FilePut": ["instance_id", "dest", "bytes"],
    "FileRestore": ["instance_id", "dest", "bytes", "export_id"],
    "VariablePreview": ["name", "source_kind", "scope", "will_send", "reason"],
    "SendPreview": [
        "template_id", "host_id", "image", "command", "cpus", "memory_gb", "gpus", "network", "display",
        "variables", "labels", "publish", "warnings",
    ],
    "InstanceStats": ["cpu_pct", "mem_used_mb", "mem_limit_mb", "sampled_at"],
    "InstanceRecord": [
        "id", "name", "host_id", "template_id", "runtime", "container_id", "status", "created_at", "started_at",
        "expires_at", "last_activity_at", "idle_minutes", "display", "display_state", "ports", "stats", "error",
        "session_id", "stop_reason", "expiry_warned_at", "ttl_minutes",
    ],
    "JobRecord": [
        "id", "host_id", "slurm_job_id", "name", "state", "partition", "nodes", "submitted_at", "started_at",
        "ended_at", "exit_code", "job_dir", "image", "session_id", "notified",
    ],
    "TunnelRecord": [
        "id", "kind", "instance_id", "host_id", "local_port", "remote_host", "remote_port", "pid", "url",
        "created_at", "expires_at", "status", "error",
    ],
    "TerminalLaunch": ["launched", "method", "command_preview", "copy_command", "error"],
    "TestStep": ["name", "ok", "detail", "hint", "ms"],
    "TestResult": ["ok", "status", "steps", "capabilities"],
    "Health": ["ok", "version", "configured", "services", "broadcast", "asyncssh", "modules", "errors"],
    "Summary": ["hosts", "instances", "jobs", "attention", "generated_at", "approvals"],
    "SummaryHosts": ["total", "online", "degraded", "auth_required", "offline"],
    "SummaryInstances": ["running", "total"],
    "SummaryJobs": ["active", "finished_24h"],
    "Attention": ["kind", "id", "message"],
    "HostType": [
        "kind", "title", "one_liner", "when_to_use", "capabilities", "capability_notes", "needs",
        "learn_more_topic", "mvp",
    ],
    "DocTopic": ["topic", "title"],
    "DocPage": ["topic", "title", "body_md", "links"],
    "DocLink": ["label", "url"],
    "Text": ["text"],
    "JobOutput": ["text", "status", "note"],
    "Deleted": ["deleted"],
    "JobDeleted": ["deleted", "host_id", "job_dir", "note"],
    "FileSize": ["path", "known", "bytes", "files", "reason"],
    "AgentRemoved": ["removed", "note"],
    "AgentModels": ["models", "detected", "unavailable", "default_id"],
    "AgentModelView": ["id", "name", "base_url", "model", "key_secret", "is_default", "endpoint_host", "key_set"],
    "JobsCleared": ["deleted", "count", "job_dirs", "note"],
    "DisplayImage": ["ok", "output_tail"],
    "Error": ["error"],
    "ErrorBody": ["code", "message"],
}

ENUMS = {
    ("HostSpec", "kind"): {"ssh", "slurm", "local", "ec2"},
    ("HostSpec", "runtime_pref"): {"auto", "podman", "docker"},
    ("SlurmSettings", "container"): {"apptainer", "none"},
    ("HostState", "status"): {"online", "degraded", "auth_required", "offline", "unknown"},
    ("TestResult", "status"): {"online", "degraded", "auth_required", "offline", "unknown"},
    ("Capabilities", "display"): {"supported", "buildable", "unsupported", "unknown"},
    ("Capabilities", "runtime"): {"podman", "docker", None},
    ("Capabilities", "scheduler"): {"slurm", None},
    ("Resources", "source"): {"host", "slurm", "local"},
    ("Resources", "basis"): {"allocated"},
    ("VariableSpec", "scope"): {"runtime", "redacted", "agent-visible"},
    ("TemplateSpec", "kind"): {"container", "slurm_job"},
    ("TemplateSpec", "network"): {"default", "none"},
    ("VariablePreview", "source_kind"): {"literal", "secret"},
    ("InstanceRecord", "status"): {"creating", "running", "stopped", "exited", "error", "destroyed"},
    ("InstanceRecord", "display_state"): {"supported", "buildable", "unsupported", "unknown"},
    ("JobRecord", "state"): {
        "PENDING", "RUNNING", "COMPLETING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
        "NODE_FAIL", "UNKNOWN",
    },
    ("TunnelRecord", "kind"): {"display", "port"},
    ("TunnelRecord", "status"): {"starting", "open", "closed", "error"},
    ("TerminalLaunch", "method"): {"wt", "cmd", "macos", "linux", "none"},
    ("TestStep", "name"): {"dns", "tcp", "auth", "runtime", "scheduler", "workdir", "display",
                           "gpu"},
    ("AgentRun", "status"): {"starting", "running", "done", "failed", "stopped", "collected"},
    ("AgentRun", "mode"): {"hermes", "script"},
    ("ExportRecord", "reason"): {"before-delete", "auto-delete", "manual", "fetch"},
    ("FileEntry", "type"): {"file", "dir", "link", "other"},
}

ID_PATTERNS = {
    ("InstanceRecord", "id"): r"i-[0-9a-f]{8}",
    ("JobRecord", "id"): r"j-[0-9a-f]{8}",
    ("TunnelRecord", "id"): r"t-[0-9a-f]{8}",
    ("HostSpec", "id"): r"[a-z0-9-]{1,32}",
    ("AgentRun", "id"): r"a-[0-9a-f]{8}",
    ("ExportRecord", "id"): r"x-[0-9a-f]{8}",
    ("CollectResult", "run_id"): r"a-[0-9a-f]{8}",
}

ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
TIME_KEYS = {"created_at", "started_at", "expires_at", "last_activity_at", "last_seen", "probed_at", "sampled_at",
             "submitted_at", "ended_at", "generated_at", "mtime"}

SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{8,}",
    r"ghp_[A-Za-z0-9]{8,}",
    r"gho_[A-Za-z0-9]{8,}",
    r"github_pat_[A-Za-z0-9_]{8,}",
    r"AKIA[0-9A-Z]{12,}",
    r"xox[abprs]-[A-Za-z0-9-]{8,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY",
    r"AIza[0-9A-Za-z_-]{20,}",
]


# ---------------------------------------------------------------- shape checking


# Keys a route adds on top of the model (hosts_api._with_basis); optional in fixtures.
ROUTE_ONLY_KEYS = {"Resources": {"basis"}, "AgentModels": {"unavailable"}}


def _check_keys(model: str, obj, path: str, problems: list[str]) -> bool:
    if not isinstance(obj, dict):
        problems.append(f"{path}: expected an object for {model}, got {type(obj).__name__}")

        return False

    want, have = set(KEYS[model]), set(obj)

    if want - have or have - want - ROUTE_ONLY_KEYS.get(model, set()):
        problems.append(f"{path} ({model}): missing {sorted(want - have)}, unexpected {sorted(have - want)}")

    return True


def _check_list(model: str, items, path: str, problems: list[str]) -> None:
    if not isinstance(items, list):
        problems.append(f"{path}: expected a list of {model}")

        return

    for index, item in enumerate(items):
        check_shape(model, item, f"{path}[{index}]", problems)


def check_shape(model: str, obj, path: str, problems: list[str]) -> None:
    """Recursively compare key sets (and enum values) with the spec."""
    if not _check_keys(model, obj, path, problems):
        return

    for (m, key), allowed in ENUMS.items():
        if m == model and key in obj and obj[key] not in allowed:
            problems.append(f"{path}.{key}: {obj[key]!r} not in {sorted(map(str, allowed))}")

    for (m, key), pattern in ID_PATTERNS.items():
        if m == model and key in obj and not re.fullmatch(pattern, str(obj[key])):
            problems.append(f"{path}.{key}: {obj[key]!r} does not match {pattern}")

    for key in TIME_KEYS & set(obj):
        if obj[key] is not None and not (isinstance(obj[key], str) and ISO_UTC.match(obj[key])):
            problems.append(f"{path}.{key}: {obj[key]!r} is not an ISO-8601 UTC string")

    nested = {
        "HostSpec": {"slurm": ("SlurmSettings", True), "ec2": ("Ec2Settings", True),
                     "limits": ("HostLimits", False)},
        "HostState": {"capabilities": ("Capabilities", False), "resources": ("Resources", True)},
        "HostWithState": {"spec": ("HostSpec", False), "state": ("HostState", True)},
        "InstanceRecord": {"stats": ("InstanceStats", True)},
        "TestResult": {"capabilities": ("Capabilities", True)},
        "TemplateSpec": {"agent": ("AgentToggles", True)},
        "Summary": {"hosts": ("SummaryHosts", False), "instances": ("SummaryInstances", False), "jobs": ("SummaryJobs", False)},
        "Error": {"error": ("ErrorBody", False)},
    }.get(model, {})

    for key, (child, optional) in nested.items():
        if key not in obj:
            continue

        if obj[key] is None:
            if not optional:
                problems.append(f"{path}.{key}: must not be null")

            continue

        check_shape(child, obj[key], f"{path}.{key}", problems)

    lists = {
        "Capabilities": {"gpus": "GpuInfo", "partitions": "Partition"},
        "Resources": {"gpus": "GpuInfo"},
        "TemplateSpec": {"variables": "VariableSpec"},
        "SendPreview": {"variables": "VariablePreview"},
        "TestResult": {"steps": "TestStep"},
        "Summary": {"attention": "Attention"},
        "DocPage": {"links": "DocLink"},
        "FileListing": {"entries": "FileEntry"},
        "Exports": {"exports": "ExportRecord"},
        "AgentModels": {"models": "AgentModelView"},
    }.get(model, {})

    for key, child in lists.items():
        if key in obj:
            _check_list(child, obj[key], f"{path}.{key}", problems)


MODEL_CHECKS = {
    "HostWithState": ("HostWithState", False),
    "HostWithState[]": ("HostWithState", True),
    "HostState": ("HostState", False),
    "TestResult": ("TestResult", False),
    "TemplateSpec": ("TemplateSpec", False),
    "TemplateSpec[]": ("TemplateSpec", True),
    "SendPreview": ("SendPreview", False),
    "InstanceRecord": ("InstanceRecord", False),
    "InstanceRecord[]": ("InstanceRecord", True),
    "JobRecord": ("JobRecord", False),
    "JobRecord[]": ("JobRecord", True),
    "TunnelRecord": ("TunnelRecord", False),
    "TunnelRecord[]": ("TunnelRecord", True),
    "TerminalLaunch": ("TerminalLaunch", False),
    "Partition[]": ("Partition", True),
    "AgentRun": ("AgentRun", False),
    "AgentRun[]": ("AgentRun", True),
    "CollectResult": ("CollectResult", False),
    "ExportRecord": ("ExportRecord", False),
}

# Ad-hoc shapes, by (method, path) of the route that serves them.
ADHOC = {
    ("GET", "/health"): ("Health", False),
    ("GET", "/summary"): ("Summary", False),
    ("GET", "/host-types"): ("HostType", True),
    ("GET", "/docs"): ("DocTopic", True),
    ("GET", "/docs/{topic}"): ("DocPage", False),
    ("DELETE", "/hosts/{id}"): ("Deleted", False),
    ("DELETE", "/templates/{id}"): ("Deleted", False),
    ("POST", "/hosts/{id}/display-image"): ("DisplayImage", False),
    ("GET", "/instances/{id}/logs"): ("Text", False),
    ("GET", "/jobs/{id}/output"): ("JobOutput", False),
    ("DELETE", "/jobs/{id}"): ("JobDeleted", False),
    ("DELETE", "/agents/{id}"): ("AgentRemoved", False),
    ("GET", "/instances/{id}/files/size"): ("FileSize", False),
    ("GET", "/agent-models"): ("AgentModels", False),
    ("POST", "/agent-models"): ("AgentModelView", False),
    ("POST", "/jobs/clear-finished"): ("JobsCleared", False),
    ("GET", "/instances/{id}/files"): ("FileListing", False),
    ("POST", "/instances/{id}/files/put"): ("FilePut", False),
    ("GET", "/exports"): ("Exports", False),
    ("POST", "/exports/{id}/restore"): ("FileRestore", False),
    ("DELETE", "/exports/{id}"): ("Deleted", False),
}


# ---------------------------------------------------------------- loading


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def routes() -> list[dict]:
    data = _load("routes.json")

    assert isinstance(data, dict) and list(data) == ["routes"], "routes.json must be {'routes': [...]}"

    return data["routes"]


def _shape_of(route: dict) -> tuple[str, bool] | None:
    model = route["model"]

    if model is not None:
        return MODEL_CHECKS[model]

    return ADHOC.get((route["method"], route["path"]))


# ---------------------------------------------------------------- routes.json


def test_routes_entries_are_well_formed(routes):
    seen = set()

    assert routes, "routes.json has no routes"

    for route in routes:
        assert set(route) <= {"method", "path", "query", "fixture", "model"}, route
        assert {"method", "path", "fixture", "model"} <= set(route), route
        assert route["method"] in {"GET", "POST", "PATCH", "PUT", "DELETE"}, route
        assert route["path"].startswith("/") and "?" not in route["path"], route
        assert route["model"] in ALLOWED_MODELS, route
        assert set(re.findall(r"\{(\w+)\}", route["path"])) <= {"id", "topic", "action"}, route

        for param in re.findall(r"\{(\w+)\}", route["path"]):
            assert param in {"id", "topic", "action"}

        if "query" in route:
            assert isinstance(route["query"], dict) and route["query"], route

        key = (route["method"], route["path"], json.dumps(route.get("query"), sort_keys=True))

        assert key not in seen, f"duplicate route {key}"
        seen.add(key)


def test_every_route_fixture_exists_and_parses(routes):
    for route in routes:
        path = FIXTURES / route["fixture"]

        assert path.is_file(), f"missing fixture {route['fixture']}"
        json.loads(path.read_text(encoding="utf-8"))


def test_no_orphan_fixture_files(routes):
    referenced = {route["fixture"] for route in routes}
    present = {p.name for p in FIXTURES.glob("*.json")}

    assert present - referenced - EXTRA_FILES == set(), "fixture files not referenced by routes.json"
    assert EXTRA_FILES <= present, f"missing extra fixtures: {sorted(EXTRA_FILES - present)}"


def test_routes_cover_exactly_the_expected_ui_calls(routes):
    assert {(r["method"], r["path"]) for r in routes} == EXPECTED_CALLS


def test_query_variants_exist(routes):
    variants = {(r["method"], r["path"], json.dumps(r["query"], sort_keys=True)) for r in routes if "query" in r}

    assert ("GET", "/hosts", '{"kind": "slurm"}') in variants
    assert ("GET", "/jobs", '{"active": "1"}') in variants

    for method, path, _ in variants:
        assert any(r["method"] == method and r["path"] == path and "query" not in r for r in routes), (
            f"{method} {path} needs a default (no query) route too"
        )


# ---------------------------------------------------------------- plugin.js API_CALLS


def _node() -> str | None:
    return shutil.which("node")


@functools.lru_cache(maxsize=1)
def _dump() -> dict:
    node = _node()

    assert node

    proc = subprocess.run(
        [node, "--import", "./tests/js/register-stubs.mjs", "./tests/js/dump.mjs"],
        cwd=ROOT,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    assert proc.returncode == 0, f"dump.mjs failed:\n{proc.stderr}"

    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not on PATH")
def test_dump_shape():
    dump = _dump()

    assert set(dump) == {"copy", "api_calls", "default"}
    assert dump["default"]["id"] == "cloud-compute"
    assert dump["default"]["defaultEnabled"] is False
    assert isinstance(dump["copy"], dict) and dump["copy"]


@pytest.mark.skipif(_node() is None, reason="node is not on PATH")
def test_plugin_api_calls_have_no_duplicates_and_include_instance_get():
    calls = [(c["method"], c["path"]) for c in _dump()["api_calls"]]

    assert len(calls) == len(set(calls)) == len(EXPECTED_CALLS), f"API_CALLS has {len(calls)} entries, expected {len(EXPECTED_CALLS)}"
    assert ("GET", "/instances/{id}") in calls
    assert set(calls) == EXPECTED_CALLS


@pytest.mark.skipif(_node() is None, reason="node is not on PATH")
def test_plugin_api_calls_match_routes(routes):
    calls = {(c["method"], c["path"]) for c in _dump()["api_calls"]}
    served = {(r["method"], r["path"]) for r in routes}

    assert calls - served == set(), f"API_CALLS without a routes.json entry: {sorted(calls - served)}"
    assert served - calls == set(), f"routes.json entries plugin.js never calls: {sorted(served - calls)}"


# ---------------------------------------------------------------- shapes


def test_route_fixtures_have_the_spec_shapes(routes):
    problems: list[str] = []

    for route in routes:
        shape = _shape_of(route)

        assert shape is not None, f"no shape rule for {route['method']} {route['path']} (model None)"

        model, is_list = shape
        data = _load(route["fixture"])
        label = route["fixture"]

        if is_list:
            _check_list(model, data, label, problems)
            assert isinstance(data, list) and data, f"{label}: expected a non-empty list"
        else:
            check_shape(model, data, label, problems)

    assert not problems, "\n".join(problems)


def test_extra_fixtures_have_the_spec_shapes():
    problems: list[str] = []

    check_shape("Error", _load("error_not_found.json"), "error_not_found.json", problems)
    check_shape("Health", _load("health_unconfigured.json"), "health_unconfigured.json", problems)
    check_shape("TerminalLaunch", _load("instance_terminal_unlaunched.json"), "instance_terminal_unlaunched.json", problems)

    assert not problems, "\n".join(problems)
    assert _load("health_unconfigured.json")["configured"] is False
    assert _load("health.json")["configured"] is True
    assert _load("instance_terminal_unlaunched.json")["launched"] is False
    assert _load("instance_terminal.json")["launched"] is True
    assert _load("instance_terminal_unlaunched.json")["copy_command"]


def test_host_fixture_content():
    hosts = _load("hosts.json")
    by_id = {h["spec"]["id"]: h for h in hosts}
    statuses = {h["state"]["status"] for h in hosts if h["state"]}
    kinds = {h["spec"]["kind"] for h in hosts}

    assert {"online", "degraded", "offline", "auth_required"} <= statuses
    assert {"ssh", "slurm"} <= kinds

    x1 = by_id["x1carbon"]

    assert x1["spec"]["kind"] == "ssh" and x1["state"]["status"] == "online"
    assert x1["state"]["capabilities"]["runtime"] == "podman"
    assert x1["state"]["capabilities"]["runtime_version"].startswith("5.8")
    assert x1["state"]["capabilities"]["rootless"] is True

    slurm = by_id["slurm-x1"]

    assert slurm["spec"]["kind"] == "slurm" and slurm["state"]["status"] == "online"
    assert slurm["spec"]["exec_prefix"] == ["podman", "exec", "-i", "slurmctld"]
    assert slurm["state"]["capabilities"]["partitions"], "slurm host needs partitions"
    assert slurm["state"]["capabilities"]["display"] == "unsupported"

    assert all(h["state"] is None or h["state"]["host_id"] == h["spec"]["id"] for h in hosts)
    assert {h["spec"]["id"] for h in _load("hosts_slurm.json")} == {i for i, h in by_id.items() if h["spec"]["kind"] == "slurm"}
    assert any(h["state"]["capabilities"]["display"] == "buildable" for h in hosts if h["state"]), "need a buildable host"

    summary = _load("summary.json")
    counts = {s: sum(1 for h in hosts if h["state"] and h["state"]["status"] == s) for s in ("online", "degraded", "auth_required", "offline")}

    assert summary["hosts"] == {"total": len(hosts), **counts}


def test_instance_fixture_content():
    instances = _load("instances.json")
    states = {(i["display"], i["display_state"]) for i in instances}

    assert (True, "supported") in states
    assert (False, "unsupported") in states
    assert any(i["status"] == "stopped" for i in instances)

    shown = next(i for i in instances if i["display"] and i["display_state"] == "supported")

    assert shown["ports"] == {"novnc": "127.0.0.1:41234"}
    assert _load("summary.json")["instances"] == {
        "running": sum(1 for i in instances if i["status"] == "running"),
        "total": len(instances),
    }


def test_job_and_tunnel_fixture_content():
    jobs = _load("jobs.json")
    active = _load("jobs_active.json")

    assert {j["id"] for j in active} == {j["id"] for j in jobs if j["state"] in {"PENDING", "RUNNING", "COMPLETING"}}
    assert any(j["state"] == "COMPLETED" for j in jobs)
    assert _load("summary.json")["jobs"]["active"] == len(active)

    display = _load("instance_display.json")

    assert display["kind"] == "display" and "/vnc_lite.html?path=" in display["url"]
    assert _load("instance_forward.json")["kind"] == "port"
    assert _load("tunnel_closed.json")["status"] == "closed"


def test_job_fixtures_cover_every_row_state():
    states = {j["state"] for j in _load("jobs.json")}

    assert {"PENDING", "RUNNING", "COMPLETED", "FAILED"} <= states
    assert {p["name"] for p in _load("partitions.json")} >= {"debug", "batch"}
    assert _load("job_cancelled.json")["id"] and _load("job_created.json")["state"] == "PENDING"
    assert isinstance(_load("job_output.json")["text"], str)


def test_instance_get_fixture_is_a_plain_instance_from_the_list():
    one = _load("instance.json")
    listed = {i["id"]: i for i in _load("instances.json")}

    assert one == listed["i-ab12cd34"]
    assert one["display_state"] == "unsupported"
    assert re.fullmatch(r"i-[0-9a-f]{8}", one["id"])


def test_display_tunnel_fixtures_carry_a_url():
    for name in ("instance_display.json", "instance_forward.json"):
        assert _load(name)["url"].startswith("http://127.0.0.1:"), name

    assert {t["kind"] for t in _load("tunnels.json")} == {"display", "port"}
    assert all(t["status"] == "open" and t["url"] for t in _load("tunnels.json"))


def test_job_request_key_list_equals_core_job_request_fields():
    import dataclasses

    models = _core_models()
    keys = _load("job_request_keys.json")

    assert [f.name for f in dataclasses.fields(models.JobRequest)] == keys
    assert set(models.JobRequest.from_dict({"host_id": "h", "name": "n", "command": "true"}).to_dict()) == set(keys)


def test_preview_never_contains_values():
    preview = _load("template_preview.json")

    for variable in preview["variables"]:
        assert "value" not in variable

    assert any(not v["will_send"] and v["reason"] for v in preview["variables"])


def test_host_types_and_docs_fixture_content():
    types = _load("host_types.json")

    assert [t["kind"] for t in types] == ["ssh", "slurm", "local", "ec2"]

    for t in types:
        assert set(t["capabilities"].values()) <= {"yes", "no", "partial"}
        assert isinstance(t["needs"], list) and isinstance(t["mvp"], bool)

    topics = [t["topic"] for t in _load("docs_list.json")]

    assert {t["learn_more_topic"] for t in types} <= set(topics)
    assert _load("docs_topic.json")["topic"] in topics
    assert _load("docs_topic.json")["links"], "doc page needs links"


def test_no_secrets_in_fixtures():
    for path in FIXTURES.glob("*.json"):
        body = path.read_text(encoding="utf-8")

        for pattern in SECRET_PATTERNS:
            assert not re.search(pattern, body), f"{path.name} matches secret pattern {pattern}"

        assert "password=" not in body.lower(), f"{path.name} contains a password parameter"


# ---------------------------------------------------------------- core models round-trip

MODEL_CLASSES = {
    "HostState": "HostState",
    "TestResult": "TestResult",
    "TemplateSpec": "TemplateSpec",
    "SendPreview": "SendPreview",
    "InstanceRecord": "InstanceRecord",
    "JobRecord": "JobRecord",
    "TunnelRecord": "TunnelRecord",
    "TerminalLaunch": "TerminalLaunch",
    "Partition": "Partition",
    "AgentRun": "AgentRun",
    "CollectResult": "CollectResult",
    "ExportRecord": "ExportRecord",
}


def _core_models():
    """``cloud_compute_core.models`` (the tests conftest loads the package)."""
    return importlib.import_module("cloud_compute_core.models")


def _model_class(models, name: str):
    """A model class by name: ``models`` first, then ``agents`` (AgentRun, CollectResult live there)."""
    found = getattr(models, name, None)

    if found is None:
        found = getattr(importlib.import_module("cloud_compute_core.agents"), name, None)

    return found


def keyshape(value):
    """Recursive key structure: dict -> {key: shape}, list -> [shapes], scalar -> None."""
    if isinstance(value, dict):
        return {k: keyshape(v) for k, v in value.items()}

    if isinstance(value, list):
        return [keyshape(v) for v in value]

    return None


def _same_keys(expected, actual, path: str, problems: list[str]) -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            problems.append(f"{path}: expected an object, got {type(actual).__name__}")

            return

        if set(expected) != set(actual):
            problems.append(f"{path}: missing {sorted(set(expected) - set(actual))}, unexpected {sorted(set(actual) - set(expected))}")

        for key in set(expected) & set(actual):
            _same_keys(expected[key], actual[key], f"{path}.{key}", problems)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            problems.append(f"{path}: list length {len(expected)} vs {len(actual) if isinstance(actual, list) else actual!r}")

            return

        for index, (e, a) in enumerate(zip(expected, actual)):
            _same_keys(e, a, f"{path}[{index}]", problems)
    elif actual is not None and isinstance(actual, (dict, list)):
        problems.append(f"{path}: scalar became {type(actual).__name__}")


def _roundtrip(models, model: str, obj):
    if model == "HostWithState":
        # GET /hosts adds resources.basis on top of the model; use the real handler's helper.
        hosts_api = importlib.import_module("cloud_compute_core.hosts_api")

        return {
            "spec": models.HostSpec.from_dict(obj["spec"]).to_dict(),
            "state": None if obj["state"] is None else hosts_api._state_dict(models.HostState.from_dict(obj["state"])),
        }

    return _model_class(models, MODEL_CLASSES[model]).from_dict(obj).to_dict()


def test_model_fixtures_round_trip_through_core_models(routes):
    models = _core_models()
    problems: list[str] = []
    seen = set()

    for route in routes:
        if route["model"] is None or route["fixture"] in seen:
            continue

        model = route["model"].removesuffix("[]")

        # TestResult is not a core model yet; its key list is checked against the spec only.
        if model in MODEL_CLASSES and _model_class(models, MODEL_CLASSES[model]) is None:
            assert model == "TestResult", f"core models lack {MODEL_CLASSES[model]}"

            continue

        seen.add(route["fixture"])

        data = _load(route["fixture"])
        items = data if route["model"].endswith("[]") else [data]

        for index, item in enumerate(items):
            label = f"{route['fixture']}[{index}]"

            try:
                again = _roundtrip(models, model, item)
            except Exception as error:  # noqa: BLE001 - report every failing fixture
                problems.append(f"{label}: {model}.from_dict/to_dict raised {error!r}")

                continue

            _same_keys(keyshape(item), keyshape(again), label, problems)

    assert seen, "no model fixture was round-tripped"
    assert not problems, "\n".join(problems)


def _slurm_host_request(services, models, fixture_name: str):
    """Load the hosts of a fixture file into the real store; returns (request, hosts_api)."""
    interfaces = importlib.import_module("cloud_compute_core.interfaces")

    for item in _load(fixture_name):
        services.store.upsert_host(models.HostSpec.from_dict(item["spec"]))
        if item["state"] is not None:
            services.store.set_host_state(models.HostState.from_dict(item["state"]))

    req = interfaces.ApiRequest(path_params={}, query={"kind": "slurm"}, body=None, services=services)

    return req, importlib.import_module("cloud_compute_core.hosts_api")


@pytest.mark.parametrize("name", ["hosts_slurm.json", "hosts_slurm_mem_unknown.json"])
def test_slurm_host_fixtures_equal_the_real_list_hosts_output(services, name):
    models = _core_models()
    req, hosts_api = _slurm_host_request(services, models, name)
    real = {h["spec"]["id"]: h for h in hosts_api.list_hosts(req)}
    fixture = _load(name)

    assert real == {h["spec"]["id"]: h for h in fixture}
    assert all(h["state"]["resources"]["basis"] == "allocated" for h in fixture if h["state"]["resources"])


def test_unknown_memory_fixture_has_null_used_and_a_total():
    res = _load("hosts_slurm_mem_unknown.json")[0]["state"]["resources"]

    assert res["mem_used_mb"] is None and res["mem_total_mb"] and res["basis"] == "allocated"


def test_stale_jobs_fixture_matches_the_real_stale_marker():
    models = _core_models()
    slurm_api = importlib.import_module("cloud_compute_core.slurm_api")
    stale = _load("jobs_stale.json")
    plain = [{k: v for k, v in j.items() if k != "stale"} for j in stale]

    # every job round-trips as a JobRecord once the route-only flag is removed
    for item in plain:
        assert models.JobRecord.from_dict(item).to_dict() == item

    # the real handler flags open jobs of the stale host, and never a finished job
    real = slurm_api._mark_stale([dict(j) for j in plain], {"slurm-x1"})

    assert real == stale
    assert any(j.get("stale") for j in stale) and any(not j.get("stale") for j in stale)

    unconfirmed = [j for j in stale if j["state"] == "UNKNOWN" and j["ended_at"]]

    assert unconfirmed and all("stale" not in j for j in unconfirmed)
    assert unconfirmed[0] in [j for j in _load("jobs.json")]


def test_unconfirmed_finished_event_fixture_equals_the_real_payload(services, fake_transports, fake_events):
    """Drive the real SlurmManager until a job is closed without accounting; compare its job.finished payload."""
    import datetime as dt

    testing = importlib.import_module("cloud_compute_core.testing")
    slurm = importlib.import_module("cloud_compute_core.slurm")
    fixture = _load("job_finished_unconfirmed.json")
    job = next(j for j in _load("jobs.json") if j["id"] == fixture["id"])
    submitted = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    services.store.upsert_host(testing.sample_slurm_host(id=job["host_id"]))
    services.store.upsert_job(testing.sample_job(
        id=job["id"], host_id=job["host_id"], slurm_job_id=job["slurm_job_id"], name=job["name"],
        state="RUNNING", started_at=None, nodes=None, submitted_at=submitted,
    ))

    t = fake_transports.get(job["host_id"])

    t.script(lambda argv, stdin: argv[:3] == ["env", slurm.TIME_ENV, "squeue"], testing.exec_result(stdout=""))
    t.script(lambda argv, stdin: argv[:3] == ["env", slurm.TIME_ENV, "sacct"], testing.exec_result(
        rc=1, stderr="sacct: error: Slurm accounting storage is disabled"))

    mgr = slurm.SlurmManager(services)

    for _ in range(slurm.MISSING_POLLS + 2):
        mgr.poll(job["host_id"])

    (payload,) = fake_events.payloads("job.finished")

    assert payload == fixture
    assert payload["confirmed"] is False and payload["state"] == "UNKNOWN"


def test_delete_template_route_is_the_deleted_shape(routes):
    route = next(r for r in routes if r["method"] == "DELETE" and r["path"] == "/templates/{id}")

    assert route["model"] is None
    assert _load(route["fixture"]) == {"deleted": True}


# ---------------------------------------------------------------- core handlers


def _request(services):
    interfaces = importlib.import_module("cloud_compute_core.interfaces")

    return interfaces.ApiRequest(path_params={}, query={}, body=None, services=services)


def _same_shape(expected, actual, path: str, problems: list[str]) -> None:
    """Key sets must match recursively; maps keyed by name (not by field) compare their key sets only."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected) != set(actual):
            problems.append(f"{path}: fixture {sorted(expected)} vs core {sorted(actual)}")

            return

        for key in expected:
            _same_shape(expected[key], actual[key], f"{path}.{key}", problems)
    elif isinstance(expected, dict) != isinstance(actual, dict):
        problems.append(f"{path}: fixture {type(expected).__name__} vs core {type(actual).__name__}")
    elif type(expected) is not type(actual) and not (expected is None or actual is None):
        problems.append(f"{path}: fixture {type(expected).__name__} vs core {type(actual).__name__}")


def test_health_fixtures_have_the_core_keys(services):
    summary_api = importlib.import_module("cloud_compute_core.summary_api")
    real = summary_api.health(_request(services))

    for name in ("health.json", "health_unconfigured.json"):
        problems: list[str] = []

        _same_shape(_load(name), real, name, problems)

        assert not problems, "\n".join(problems)


def test_summary_fixture_has_the_core_keys(services):
    summary_api = importlib.import_module("cloud_compute_core.summary_api")
    testing = importlib.import_module("cloud_compute_core.testing")

    services.store.upsert_host(testing.sample_host())
    services.store.set_host_state(testing.sample_host_state(status="offline", last_error="Connection timed out"))

    real = summary_api.summary(_request(services))
    fixture = _load("summary.json")
    problems: list[str] = []

    assert real["attention"], "the offline host should raise an attention entry"

    _same_shape({**fixture, "attention": []}, {**real, "attention": []}, "summary.json", problems)

    assert not problems, "\n".join(problems)

    for entry in fixture["attention"]:
        assert set(entry) == set(real["attention"][0])
        assert entry["kind"] in {"host", "instance", "job"}


def test_host_types_fixture_equals_core_output(services):
    summary_api = importlib.import_module("cloud_compute_core.summary_api")
    hosttypes = importlib.import_module("cloud_compute_core.hosttypes")

    assert _load("host_types.json") == summary_api.host_types(_request(services))
    assert _load("host_types.json") == hosttypes.HOST_TYPES


def test_template_deleted_fixture_equals_core_handler_output(services):
    instances_api = importlib.import_module("cloud_compute_core.instances_api")
    interfaces = importlib.import_module("cloud_compute_core.interfaces")
    testing = importlib.import_module("cloud_compute_core.testing")

    services.templates.save(testing.sample_template(id="fixture-del"))

    def call(template_id):
        return instances_api.delete_template(
            interfaces.ApiRequest(path_params={"template_id": template_id}, query={}, body=None, services=services)
        )

    real = call("fixture-del")

    assert _load("template_deleted.json") == real == {"deleted": True}
    assert set(_load("template_deleted.json")) == set(call("fixture-del"))  # absent id: same keys, False


def test_instance_display_fixture_matches_core_display_url_and_record_keys():
    display = importlib.import_module("cloud_compute_core.display")
    models = _core_models()
    testing = importlib.import_module("cloud_compute_core.testing")
    fixture = _load("instance_display.json")
    match = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/([0-9a-f]{32})/vnc_lite\.html\?path=([0-9a-f]{32})/websockify&scale=true", fixture["url"])

    assert match, fixture["url"]

    port, token, path_token = int(match[1]), match[2], match[3]

    assert token == path_token and port == fixture["local_port"]
    assert fixture["url"] == display.display_url(port, token)
    assert display.redact_display_url(fixture["url"]) == f"http://127.0.0.1:{port}/<token>/vnc_lite.html?path=<token>/websockify&scale=true"
    assert "password" not in fixture["url"].lower()
    assert set(fixture) == set(models.TunnelRecord.from_dict(fixture).to_dict()) == set(testing.sample_tunnel().to_dict())


# ---------------------------------------------------------------- cloud agents (core/agents_api.py)


class _AgentsManager:
    """Stands in for AgentManager: hands back real AgentRun / CollectResult records built from fixtures."""

    def __init__(self):
        agents = importlib.import_module("cloud_compute_core.agents")
        self.agents = agents
        self.calls = []

    def status(self, run_id=None):
        runs = [self.agents.AgentRun.from_dict(d) for d in _load("agents.json")]

        return [r for r in runs if run_id in (None, r.id)]

    def spawn(self, goal, host_id, **kwargs):
        self.calls.append(("spawn", goal, host_id, kwargs))

        return self.agents.AgentRun.from_dict(_load("agent_created.json"))

    def collect(self, run_id, into="worktree"):
        self.calls.append(("collect", run_id, into))

        return self.agents.CollectResult.from_dict(_load("agent_collect.json"))

    def stop(self, run_id, keep_branch=True, delete_anyway=False):
        self.calls.append(("stop", run_id, keep_branch))

        return self.agents.AgentRun.from_dict(_load("agent_stopped.json"))


def _agent_request(manager, path_params=None, body=None):
    interfaces = importlib.import_module("cloud_compute_core.interfaces")
    testing = importlib.import_module("cloud_compute_core.testing")

    return interfaces.ApiRequest(
        path_params=path_params or {}, query={}, body=body, services=testing.make_services(agents=manager)
    )


def test_agent_fixtures_equal_the_real_handler_output():
    agents_api = importlib.import_module("cloud_compute_core.agents_api")
    manager = _AgentsManager()
    body = {"goal": "Add a --verbose flag", "host_id": "x1carbon", "repo_path": "/home/dev/code/demo"}

    assert agents_api.list_agents(_agent_request(manager)) == _load("agents.json")
    assert agents_api.spawn_agent(_agent_request(manager, body=body)) == _load("agent_created.json")
    assert agents_api.collect_agent(_agent_request(manager, {"run_id": "a-5e6f7a8b"}, {"into": "worktree"})) == _load("agent_collect.json")
    assert agents_api.stop_agent(_agent_request(manager, {"run_id": "a-1a2b3c4d"}, {"keep_branch": True})) == _load("agent_stopped.json")
    assert [c[0] for c in manager.calls] == ["spawn", "collect", "stop"]
    assert manager.calls[1] == ("collect", "a-5e6f7a8b", "worktree")
    assert manager.calls[2] == ("stop", "a-1a2b3c4d", True)


def test_agent_routes_are_served_by_the_real_router():
    agents_api = importlib.import_module("cloud_compute_core.agents_api")
    served = {(r.method, r.path.replace("{run_id}", "{id}")) for r in agents_api.routes()}

    assert {("GET", "/agents"), ("POST", "/agents"), ("POST", "/agents/{id}/collect"), ("POST", "/agents/{id}/stop")} <= served


def test_agent_request_keys_equal_the_spawn_arguments_core_accepts():
    agents_api = importlib.import_module("cloud_compute_core.agents_api")
    keys = _load("agent_request_keys.json")
    kwargs = agents_api._spawn_kwargs({"goal": "g", "host_id": "h", "repo_path": "/r"})

    assert set(kwargs) == set(keys)
    # The full body the New agent run dialog can send is accepted as is.
    full = {
        "goal": "g", "host_id": "h", "repo_path": "/r", "branch": "cloud/x", "new_branch": False, "base": "main",
        "template": "agent-script", "max_turns": 12, "model_id": "m-0a1b2c3d", "criteria": "tests pass",
    }

    assert set(full) == set(keys)
    assert agents_api._spawn_kwargs(full) == full


def test_agent_status_enums_equal_core_statuses():
    agents = importlib.import_module("cloud_compute_core.agents")

    assert set(agents.AGENT_STATUSES) == ENUMS[("AgentRun", "status")]
    assert set(agents.AGENT_MODES) == ENUMS[("AgentRun", "mode")]


def test_agent_fixture_content():
    runs = _load("agents.json")
    by_status = {r["status"]: r for r in runs}

    assert {"running", "done", "failed", "collected", "stopped"} <= set(by_status)
    assert by_status["failed"]["error"] and by_status["running"]["last_log"]
    assert by_status["collected"]["collected_ref"] and by_status["collected"]["worktree_path"]
    assert {r["mode"] for r in runs} == {"hermes", "script"}
    assert len({r["id"] for r in runs}) == len(runs)
    assert _load("agent_collect.json")["conflicts_dry_run"], "the collect fixture shows conflicts"
    assert _load("agent_created.json")["status"] == "starting"
    assert _load("agent_stopped.json")["status"] == "stopped"


def test_agent_template_fixture_has_the_real_agent_object():
    agents = importlib.import_module("cloud_compute_core.agents")
    fixture = next(t for t in _load("templates.json") if t["id"] == "hermes-agent")
    real = agents.AGENT_TEMPLATES[0]

    assert fixture["agent"] == real.agent
    assert (fixture["ttl_minutes"], fixture["idle_minutes"]) == (real.ttl_minutes, real.idle_minutes)
    assert real.ttl_minutes is None and real.idle_minutes is None     # Settings defaults apply
    assert set(fixture["agent"]) == set(KEYS["AgentToggles"])
    assert fixture["variables"][0]["name"] == agents.KEY_ENV


def _git(cwd, *args):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)
    return proc.stdout.decode("utf-8", "replace").strip()


def _commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", f"edit {name}")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_collect_diverged_fixture_equals_what_the_real_collect_path_produces(tmp_path):
    """Drive real git to a refused Collect; the fixture carries the message gitrelay produces."""
    agents = importlib.import_module("cloud_compute_core.agents")
    gitrelay = importlib.import_module("cloud_compute_core.gitrelay")
    fixture = _load("agent_collect_diverged.json")
    run_id, branch = fixture["run_id"], "cloud/write-readme"
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "T"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
        _git(repo, "config", key, value)
    _commit(repo, "a.txt", "one\n")
    base = _git(repo, "rev-parse", "HEAD")
    sandbox = tmp_path / "sandbox"
    subprocess.run(["git", "clone", "-q", str(repo), str(sandbox)], check=True, capture_output=True)
    for key, value in (("user.name", "S"), ("user.email", "s@example.invalid"), ("commit.gpgsign", "false")):
        _git(sandbox, "config", key, value)
    _git(sandbox, "checkout", "-q", "-b", branch)
    prefix = f"refs/heads/cloud/{run_id}/"

    def bundle(name):
        out = tmp_path / name
        _git(sandbox, "bundle", "create", str(out), f"refs/heads/{branch}")
        return out

    _commit(sandbox, "agent.txt", "1\n")
    (first,) = gitrelay.import_bundle_detailed(repo, bundle("1.bundle"), prefix)
    assert first.status == "created"
    _git(repo, "checkout", "-q", "-b", "mine", base)               # the user commits on their side
    _commit(repo, "mine.txt", "local\n")
    _git(repo, "branch", "-f", f"cloud/{run_id}/{branch}", "mine")
    _git(repo, "checkout", "-q", "main")
    _commit(sandbox, "agent2.txt", "2\n")
    (item,) = gitrelay.import_bundle_detailed(repo, bundle("2.bundle"), prefix)
    assert item.status == "diverged"

    real = agents.CollectResult(
        run_id=run_id, ref=item.ref, commits_ahead=1, worktree_path=None, diverged=True,
        conflicts_dry_run=[], message=item.message).to_dict()

    assert real == fixture
    assert item.incoming_ref in fixture["message"]
    assert set(fixture) == set(KEYS["CollectResult"]) | {"message"}


def test_clean_collect_result_has_no_message_key():
    agents = importlib.import_module("cloud_compute_core.agents")
    clean = agents.CollectResult(run_id="a-1", ref="r", commits_ahead=0, worktree_path=None,
                                 diverged=False).to_dict()

    assert "message" not in clean
    assert set(clean) == set(KEYS["CollectResult"])
    assert set(_load("agent_collect.json")) == set(KEYS["CollectResult"])


# ---------------------------------------------------------------- file routes against the real handlers


class _FileTransport:
    """Stands in for a host connection: a fixed folder listing, cp streams of a few bytes."""

    def __init__(self, listing: str):
        self.listing = listing

    def run(self, argv, *, stdin=None, timeout=60):
        testing = importlib.import_module("cloud_compute_core.testing")

        return testing.exec_result(stdout=self.listing)

    def run_stream(self, argv, *, sink=None, source=None, max_bytes=None, timeout=3600):
        import io

        transport = importlib.import_module("cloud_compute_core.transport")

        if sink is not None:
            import tarfile

            tar = io.BytesIO()

            with tarfile.open(fileobj=tar, mode="w") as tf:
                folder = tarfile.TarInfo("work")
                folder.type = tarfile.DIRTYPE
                tf.addfile(folder)

                member = tarfile.TarInfo("work/a.txt")
                member.size = 2
                tf.addfile(member, io.BytesIO(b"hi"))

            sink(tar.getvalue())

            return transport.StreamResult(rc=0, stderr="", bytes=len(tar.getvalue()), duration_s=0.0)

        buf = io.BytesIO()

        source(buf)

        return transport.StreamResult(rc=0, stderr="", bytes=len(buf.getvalue()), duration_s=0.0)


class _FileFactory:
    def __init__(self, transport):
        self.transport = transport

    def for_host(self, host, prefixed=True):
        return self.transport


@pytest.fixture
def file_services(services, tmp_path):
    testing = importlib.import_module("cloud_compute_core.testing")
    files_mod = importlib.import_module("cloud_compute_core.files")
    expected = _load("files_list.json")
    records = "".join(f"{e['type'][0]}\t{e['size']}\t{_epoch(e['mtime'])}\t{e['name']}\0" for e in expected["entries"])

    services.store.upsert_host(testing.sample_host())
    services.store.upsert_instance(testing.sample_instance())
    services.set("transports", _FileFactory(_FileTransport(records)))
    services.set("files", files_mod.FileManager(services))

    return services


def _epoch(iso: str) -> int:
    from datetime import datetime, timezone

    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def _list_exports(services, instance_id: str):
    files_api = importlib.import_module("cloud_compute_core.files_api")
    interfaces = importlib.import_module("cloud_compute_core.interfaces")

    return files_api.list_exports(
        interfaces.ApiRequest(path_params={}, query={"instance_id": instance_id}, body=None, services=services)
    )


def test_files_list_fixture_equals_the_real_listing(file_services):
    real = file_services.files.list_dir("i-ab12cd34", "/work")

    assert real == _load("files_list.json")


def test_export_and_restore_fixtures_have_the_real_shape(file_services, tmp_path_factory):
    problems: list[str] = []

    fetched = file_services.files.fetch("i-ab12cd34", "/work")

    _same_shape(_load("export_record.json"), fetched, "export_record.json", problems)

    listing = _list_exports(file_services, "i-ab12cd34")

    _same_shape(_load("exports.json"), listing, "exports.json", problems)
    assert listing["exports"] and listing["exports"][0]["id"] == fetched["id"]
    assert Path(listing["folder"]) == Path(listing["exports"][0]["local_path"]).parent.parent

    upload = tmp_path_factory.mktemp("upload") / "up.txt"   # outside the plugin data folder

    upload.write_text("hi", encoding="utf-8")

    put = file_services.files.put("i-ab12cd34", str(upload), "/work/data")

    _same_shape(_load("files_put.json"), put, "files_put.json", problems)

    restored = file_services.files.restore(fetched["id"], "i-ab12cd34", "/work")

    _same_shape(_load("export_restored.json"), restored, "export_restored.json", problems)

    deleted = file_services.files.delete_export(fetched["id"])

    assert deleted == _load("export_deleted.json") == {"deleted": True}
    assert not problems, "\n".join(problems)


def test_export_fixture_entries_round_trip_through_the_core_model():
    models = _core_models()

    for item in _load("exports.json")["exports"]:
        assert models.ExportRecord.from_dict(item).to_dict() == item


def test_error_codes_the_files_and_delete_dialogs_branch_on_exist():
    sandbox = importlib.import_module("cloud_compute_core.sandbox")
    interfaces = importlib.import_module("cloud_compute_core.interfaces")
    plugin_js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")

    assert sandbox.NOT_SAVED == "not_saved"

    err = interfaces.ApiError(409, "not_saved", "Files could not be saved, so nothing was deleted: x.")

    assert err.to_dict() == {"error": {"code": "not_saved", "message": "Files could not be saved, so nothing was deleted: x."}}

    for code in ("not_saved", "not_running", "too_large"):
        assert f"'{code}'" in plugin_js, code


def test_template_default_fixture_equals_the_real_shipped_template(services):
    instances_api = importlib.import_module("cloud_compute_core.instances_api")
    interfaces = importlib.import_module("cloud_compute_core.interfaces")
    testing = importlib.import_module("cloud_compute_core.testing")

    def get(template_id, query):
        return instances_api.get_template(
            interfaces.ApiRequest(path_params={"template_id": template_id}, query=query, body=None, services=services)
        )

    shipped = get("python-dev", {"default": "1"})

    assert _load("template_default.json") == shipped
    assert shipped["builtin"] is True

    # An edited built-in differs from the shipped one and no longer carries the flag.
    edited = dict(shipped, name="My Python", builtin=False)
    services.templates.save(importlib.import_module("cloud_compute_core.models").TemplateSpec.from_dict(edited))

    assert get("python-dev", {"default": "1"}) == shipped
    assert get("python-dev", {})["name"] == "My Python" and get("python-dev", {})["builtin"] is False

    # A user template has no shipped version.
    services.templates.save(testing.sample_template(id="mine"))

    with pytest.raises(interfaces.ApiError) as err:
        get("mine", {"default": "1"})

    assert err.value.status == 404

    one = _load("template.json")

    assert set(one) == set(KEYS["TemplateSpec"]) and one["id"] == "python-dev"
