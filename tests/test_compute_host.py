"""The ``compute_host`` chat tool and the discovery additions of ``compute_status``.

No network: the prober is a fake that answers from a table of test rows.
"""

from __future__ import annotations

import json

import pytest

from cloud_compute_core import approval, hosts_api
from cloud_compute_core.config import Config
from cloud_compute_core.interfaces import ERROR_CODES
from cloud_compute_core.models import TemplateSpec, VariableSpec
from cloud_compute_core.prober import TestResult, TestStep
from cloud_compute_core.store import Store
from cloud_compute_core.testing import (
    FakeEvents, FakeSecrets, make_services, sample_capabilities, sample_host, sample_host_state,
    sample_instance, sample_job, sample_local_host, sample_slurm_host,
)

# Built from parts so the repository secret scan has nothing to match.
FAKE_TOKEN = "sk-" + "abcdefghijklmnop" + "1234567890"
FAKE_KEY_TEXT = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"

PASS_ROWS = [
    TestStep("dns", True, "labbox resolves"), TestStep("tcp", True, "Connected in 3 ms"),
    TestStep("auth", True, "Logged in"), TestStep("runtime", True, "podman 5.6.2 (rootless)"),
    TestStep("scheduler", None, "Not a Slurm host"), TestStep("display", True, "buildable"),
    TestStep("gpu", None, "No GPU detected"),
]
OFFLINE_ROWS = [
    TestStep("dns", False, "Could not resolve nosuchbox: Name or service not known",
             "Check the address for typos, or that it is an alias in your SSH config"),
    TestStep("tcp", None, "Skipped"), TestStep("auth", None, "Skipped"),
    TestStep("runtime", None, "Skipped"), TestStep("scheduler", None, "Skipped"),
    TestStep("display", None, "Skipped"), TestStep("gpu", None, "Skipped"),
]
AUTH_ROWS = [PASS_ROWS[0], PASS_ROWS[1],
             TestStep("auth", False, "Permission denied (publickey)", "Run `ssh labbox` once"),
             TestStep("runtime", None, "Skipped")]
NO_RUNTIME_ROWS = [*PASS_ROWS[:3],
                   TestStep("runtime", False, "No Podman or Docker found",
                            "Install Podman (rootless) or Docker on the host"),
                   *PASS_ROWS[4:]]


class TableProber:
    """``test_connection`` answers from ``rows_for`` (address -> rows); probes are recorded."""

    def __init__(self, store):
        self.store = store
        self.rows_for: dict[str, list[TestStep]] = {}
        self.default = PASS_ROWS
        self.tested: list = []
        self.probed: list[str] = []

    def test_connection(self, spec):
        self.tested.append(spec)
        rows = self.rows_for.get(spec.address, self.default)
        bad = any(r.ok is False for r in rows)
        return TestResult(ok=not bad, status="online" if not bad else "offline", steps=list(rows),
                          capabilities=None if bad else sample_capabilities())

    def probe(self, host):
        self.probed.append(host.id)
        state = sample_host_state(host_id=host.id)
        self.store.set_host_state(state)
        return state

    def liveness(self, host):
        return "online"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "ch.db")
    prober = TableProber(store)
    events = FakeEvents()
    secrets = FakeSecrets({"SET_TOKEN": "hunter2-do-not-leak"})
    services = make_services(store=store, prober=prober, events=events, config=Config(),
                             secrets=secrets)
    yield services, store, prober, events
    store.close()


def host(services, **args):
    return hosts_api.compute_host(args, services, {})


# --------------------------------------------------------------------------- schema and parameters


def test_description_is_short_and_names_every_action():
    desc = hosts_api._COMPUTE_HOST_SCHEMA["description"]
    assert len(desc.split()) <= 75
    for action in ("types", "list", "test", "add", "update", "remove"):
        assert action in desc


def test_unknown_parameter_is_refused_and_lists_valid_ones(env):
    services, store, *_ = env
    out = host(services, action="add", kind="ssh", address="labbox", color="red")
    assert out["error"]["code"] == "unknown_parameter" and "'color'" in out["error"]["message"]
    assert "address" in out["error"]["message"] and "identity_file" in out["error"]["message"]
    assert store.list_hosts() == []


@pytest.mark.parametrize("secret_name", ["password", "passphrase", "private_key", "key"])
def test_credentials_are_never_accepted(env, secret_name):
    services, store, *_ = env
    out = host(services, action="add", kind="ssh", address="labbox", **{secret_name: "x"})
    assert out["error"]["code"] == "unknown_parameter"
    assert "identity_file" in out["error"]["message"] and "Add host" in out["error"]["message"]
    assert store.list_hosts() == []


def test_unknown_parameter_on_compute_status(env):
    services, *_ = env
    out = hosts_api.compute_status({"hostid": "x"}, services, {})
    assert out["error"]["code"] == "unknown_parameter" and "details" in out["error"]["message"]


def test_bad_action_and_arguments(env):
    services, *_ = env
    assert host(services, action="explode")["error"]["code"] == "invalid"
    assert host(services)["error"]["code"] == "invalid"
    assert hosts_api.compute_host("nope", services, {})["error"]["code"] == "invalid"
    assert hosts_api.compute_host(None, services, {})["error"]["code"] == "invalid"


def test_every_error_code_is_a_known_code(env):
    services, store, prober, _ = env
    prober.rows_for["nosuchbox"] = OFFLINE_ROWS
    outs = [host(services, action="add", kind="ssh", address="nosuchbox"),
            host(services, action="add"), host(services, action="remove", host_id="zz"),
            host(services, action="add", kind="ssh", address="a", color=1)]
    for out in outs:
        assert out["error"]["code"] in ERROR_CODES


# --------------------------------------------------------------------------- types / list


def test_types_describes_each_kind_with_a_valid_example(env):
    services, *_ = env
    out = host(services, action="types")
    assert [t["kind"] for t in out["types"]] == ["ssh", "slurm", "ec2", "local"]
    for t in out["types"]:
        assert t["when_to_use"] and t["required"] and "optional" in t
        assert t["example"]["action"] == "add" and t["example"]["kind"] == t["kind"]
        assert set(t["example"]) <= set(hosts_api._HOST_PARAMS)
        assert set(t["required"] + t["optional"]) <= set(hosts_api._HOST_PARAMS)
    assert any("password" in n for n in out["notes"]) and out["next"].startswith("Ask the user")


def test_types_examples_pass_the_add_validation(env):
    services, store, *_ = env
    services.secrets.values.update({"AWS_ACCESS_KEY_ID": "set", "AWS_SECRET_ACCESS_KEY": "set"})
    for t in hosts_api._TYPES:
        out = host(services, **{k: v for k, v in t["example"].items()})
        assert out.get("saved") is True, out
        assert out["card"].startswith("::cc-host{id=")


def test_list_empty_returns_setup_steps(env):
    services, *_ = env
    out = host(services, action="list")
    assert out["hosts"] == [] and out["configured"] is False
    assert out["next_steps"][0].startswith('Call compute_host(action="types")')


def test_list_counts_environments_and_jobs_and_has_cards(env):
    services, store, *_ = env
    store.upsert_host(sample_host(limits={"allow_agents": True}))
    store.upsert_host(sample_slurm_host())
    store.set_host_state(sample_host_state())
    store.upsert_instance(sample_instance())
    store.upsert_instance(sample_instance(id="i-dead0001", status="destroyed"))
    store.upsert_job(sample_job())
    store.upsert_job(sample_job(id="j-done0000", state="COMPLETED"))
    out = host(services, action="list")
    rows = {h["id"]: h for h in out["hosts"]}
    assert rows["x1carbon"] == {"id": "x1carbon", "name": "x1carbon", "kind": "ssh",
                                "status": "online", "allow_agents": True, "environments": 1,
                                "jobs": 0, "card": '::cc-host{id="x1carbon"}'}
    assert rows["slurm-x1"]["jobs"] == 1 and rows["slurm-x1"]["status"] == "unknown"
    assert rows["slurm-x1"]["allow_agents"] is False and out["configured"] is True


# --------------------------------------------------------------------------- test


def test_test_passes_without_saving(env):
    services, store, prober, events = env
    out = host(services, action="test", kind="ssh", address="labbox", user="alice")
    assert out["ok"] is True and out["can_save"] is True and out["status"] == "online"
    assert [r["check"] for r in out["rows"]][:3] == ["dns", "tcp", "auth"]
    assert all(set(r) >= {"check", "label", "result", "detail"} for r in out["rows"])
    assert {r["result"] for r in out["rows"]} == {"pass", "skipped"}
    assert out["capabilities"]["runtime"] == "podman"
    assert out["next"].startswith('Next: compute_host(action="add"') and '"labbox"' in out["next"]
    assert store.list_hosts() == [] and events.payloads("hosts.changed") == []
    assert prober.probed == []                                      # the test never probes/saves


def test_test_with_wrong_alias_shows_the_failing_row_and_fix(env):
    services, store, prober, _ = env
    prober.rows_for["nosuchbox"] = OFFLINE_ROWS
    out = host(services, action="test", kind="ssh", address="nosuchbox")
    assert out["ok"] is False and out["can_save"] is False
    dns = out["rows"][0]
    assert dns["result"] == "fail" and "nosuchbox" in dns["detail"] and "typos" in dns["fix"]
    assert "address" in out["next"].lower()
    assert store.list_hosts() == []


def test_test_of_a_saved_host_uses_its_stored_fields(env):
    services, store, prober, _ = env
    store.upsert_host(sample_host(address="labbox"))
    out = host(services, action="test", host_id="x1carbon")
    assert out["ok"] is True and out["card"] == '::cc-host{id="x1carbon"}'
    assert prober.tested[-1].address == "labbox" and prober.tested[-1].id == "x1carbon"
    assert out["next"] == 'Next: env_spawn(template="python-dev", host_id="x1carbon")'
    assert host(services, action="test", host_id="ghost")["error"]["code"] == "not_found"


def test_test_redacts_secrets_in_row_text(env):
    services, store, prober, _ = env
    prober.rows_for["leaky"] = [TestStep("auth", False, "denied for " + FAKE_TOKEN,
                                         "retry")]
    out = host(services, action="test", kind="ssh", address="leaky")
    assert FAKE_TOKEN not in json.dumps(out)


# --------------------------------------------------------------------------- add


def test_add_saves_probes_and_returns_card_and_next(env):
    services, store, prober, events = env
    out = host(services, action="add", kind="ssh", name="Lab box", address="labbox", user="alice")
    assert out["saved"] is True and out["checks"] == "all essential checks passed"
    assert out["card"] == '::cc-host{id="lab-box"}'
    assert out["next"] == 'Next: env_spawn(template="python-dev", host_id="lab-box")'
    assert out["host"]["id"] == "lab-box" and out["host"]["status"] == "online"
    assert out["host"]["address"] == "labbox" and out["host"]["allow_agents"] is False
    saved = store.get_host("lab-box")
    assert saved.user == "alice" and saved.kind == "ssh"
    assert prober.probed == ["lab-box"]
    assert events.payloads("hosts.changed") == [{"ids": ["lab-box"], "reason": "created"}]


def test_add_failure_saves_nothing_and_returns_failing_rows(env):
    services, store, prober, events = env
    prober.rows_for["nosuchbox"] = OFFLINE_ROWS
    out = host(services, action="add", kind="ssh", name="Ghost", address="nosuchbox")
    assert out["saved"] is False and out["error"]["code"] == "host_offline"
    assert [r["check"] for r in out["rows"]] == ["dns"] and out["rows"][0]["result"] == "fail"
    msg = out["error"]["message"]
    assert msg.startswith("Host not saved.") and "Address resolves" in msg and "nosuchbox" in msg
    assert "typos" in msg
    assert "Compute" in out["next"] and "Add host" in out["next"]     # human fallback
    assert "card" not in out
    assert store.list_hosts() == [] and prober.probed == []
    assert events.payloads("hosts.changed") == []


def test_add_auth_failure_tells_the_agent_to_hand_the_login_to_the_user(env):
    services, store, prober, _ = env
    prober.rows_for["labbox"] = AUTH_ROWS
    out = host(services, action="add", kind="ssh", address="labbox")
    assert out["error"]["code"] == "auth_required" and store.list_hosts() == []
    assert "ssh labbox" in out["next"] and "password" in out["next"]


def test_add_refuses_a_host_without_a_container_runtime(env):
    services, store, prober, _ = env
    prober.rows_for["bare"] = NO_RUNTIME_ROWS
    out = host(services, action="add", kind="ssh", address="bare")
    assert out["saved"] is False and out["error"]["code"] == "needs_setup"
    assert "Podman or Docker" in out["next"] and store.list_hosts() == []


def test_add_conflict_names_the_update_action(env):
    services, store, *_ = env
    host(services, action="add", kind="ssh", name="Lab box", address="labbox")
    out = host(services, action="add", kind="ssh", name="Lab box", address="other")
    assert out["error"]["code"] == "conflict" and 'action="update"' in out["next"]
    assert len(store.list_hosts()) == 1


def test_add_name_defaults_to_the_address(env):
    services, store, *_ = env
    out = host(services, action="add", kind="ssh", address="labbox")
    assert out["host"]["name"] == "labbox" and out["host"]["id"] == "labbox"


def test_add_slurm_maps_partition_account_qos_workdir(env):
    services, store, prober, _ = env
    prober.default = [*PASS_ROWS[:3], TestStep("runtime", None, "Not used on Slurm hosts"),
                      TestStep("scheduler", True, "Slurm 24.05 (cpu)")]
    out = host(services, action="add", kind="slurm", name="HPC", address="login.hpc.example.edu",
               partition="cpu", account="proj1", qos="normal", workdir="/scratch/cc")
    assert out["saved"] is True and out["next"].startswith('Next: slurm_submit(host_id="hpc"')
    assert "allow_agents" not in out["host"]
    saved = store.get_host("hpc")
    assert saved.slurm.default_partition == "cpu" and saved.slurm.account == "proj1"
    assert saved.slurm.qos == "normal" and saved.slurm.workdir == "/scratch/cc"
    assert saved.exec_prefix == []          # a command prefix is set on the Compute page only


def test_add_local_defaults_name_and_address(env):
    services, store, *_ = env
    out = host(services, action="add", kind="local")
    assert out["saved"] is True and out["host"]["id"] == "this-computer"
    saved = store.get_host("this-computer")
    assert saved.kind == "local" and saved.address == "localhost"


def test_add_limits_map_to_host_limits(env):
    services, store, *_ = env
    host(services, action="add", kind="ssh", address="labbox", allow_agents=True, max_instances=2,
         port="2222")
    saved = store.get_host("labbox")
    assert saved.limits.allow_agents is True and saved.limits.max_instances == 2
    assert saved.port == 2222


@pytest.mark.parametrize("args, fragment", [
    ({"kind": "ec2", "address": "x"}, "does not apply to ec2"),
    ({"kind": "vm", "address": "x"}, "kind must be one of"),
    ({"address": "x"}, "kind is required"),
    ({"kind": "ssh"}, "address is required"),
    ({"kind": "ssh", "address": "x", "partition": "cpu"}, "does not apply to ssh"),
    ({"kind": "slurm", "address": "x", "allow_agents": True}, "does not apply to slurm"),
    ({"kind": "local", "address": "other.example.com"}, "always this computer"),
    ({"kind": "local", "user": "bob"}, "does not apply to local"),
    ({"kind": "ssh", "address": "bad host;rm"}, "not allowed"),
    ({"kind": "ssh", "address": "x", "port": 70000}, "port"),
    ({"kind": "ssh", "address": "x", "identity_file": FAKE_KEY_TEXT},
     "not the key itself"),
    ({"kind": "ssh", "address": "x", "identity_file": "/no/such/file/id_x"}, "not a file on this"),
])
def test_add_validation_errors_are_clear_and_save_nothing(env, args, fragment):
    services, store, prober, _ = env
    out = host(services, action="add", **args)
    assert out["error"]["code"] == "invalid" and fragment in out["error"]["message"]
    assert store.list_hosts() == [] and prober.tested == []


def test_add_accepts_an_identity_file_that_exists(env, tmp_path):
    services, store, *_ = env
    key = tmp_path / "id_lab"
    key.write_text("not a real key", encoding="utf-8")
    out = host(services, action="add", kind="ssh", address="labbox", identity_file=str(key))
    assert out["saved"] is True and store.get_host("labbox").identity_file == str(key)
    assert str(key.read_text()) not in json.dumps(out)


# --------------------------------------------------------------------------- update


def test_update_changes_limits_without_a_connection_test(env):
    services, store, prober, events = env
    store.upsert_host(sample_host())
    out = host(services, action="update", host_id="x1carbon", allow_agents=True, name="Lab")
    assert out["updated"] == ["limits", "name"] and out["host"]["allow_agents"] is True
    assert out["card"] == '::cc-host{id="x1carbon"}' and out["next"].startswith("Next: env_spawn")
    assert store.get_host("x1carbon").name == "Lab" and prober.tested == []
    assert events.payloads("hosts.changed")[-1]["reason"] == "updated"


def test_update_of_a_connection_field_tests_first_and_refuses_a_bad_one(env):
    services, store, prober, _ = env
    store.upsert_host(sample_host(address="labbox"))
    prober.rows_for["nosuchbox"] = OFFLINE_ROWS
    out = host(services, action="update", host_id="x1carbon", address="nosuchbox")
    assert out["saved"] is False and out["error"]["code"] == "host_offline"
    assert store.get_host("x1carbon").address == "labbox"
    ok = host(services, action="update", host_id="x1carbon", address="labbox2")
    assert ok["saved"] is True and store.get_host("x1carbon").address == "labbox2"
    assert prober.probed == ["x1carbon"]


def test_update_slurm_fields_merge_into_slurm_settings(env):
    services, store, *_ = env
    store.upsert_host(sample_slurm_host())
    host(services, action="update", host_id="slurm-x1", partition="gpu")
    slurm = store.get_host("slurm-x1").slurm
    assert slurm.default_partition == "gpu" and slurm.workdir == "/data/cloud-compute"


def test_update_errors(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    assert host(services, action="update", host_id="ghost", name="x")["error"]["code"] == "not_found"
    assert host(services, action="update", name="x")["error"]["code"] == "not_found"
    nothing = host(services, action="update", host_id="x1carbon")
    assert nothing["error"]["code"] == "invalid" and "nothing to update" in nothing["error"]["message"]
    kind = host(services, action="update", host_id="x1carbon", kind="slurm")
    assert "kind cannot be changed" in kind["error"]["message"]
    wrong = host(services, action="update", host_id="x1carbon", partition="cpu")
    assert "does not apply to ssh" in wrong["error"]["message"]


# --------------------------------------------------------------------------- remove


def test_remove_deletes_an_idle_host(env):
    services, store, _, events = env
    store.upsert_host(sample_host())
    out = host(services, action="remove", host_id="x1carbon")
    assert out["removed"] is True and out["id"] == "x1carbon" and "card" not in out
    assert "Nothing on that machine" in out["note"] and store.get_host("x1carbon") is None
    assert events.payloads("hosts.changed")[-1]["reason"] == "deleted"


def test_remove_refuses_while_environments_or_jobs_exist(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    store.upsert_host(sample_slurm_host())
    store.upsert_instance(sample_instance())
    store.upsert_job(sample_job())
    env_out = host(services, action="remove", host_id="x1carbon")
    assert env_out["removed"] is False and env_out["error"]["code"] == "conflict"
    assert "still has 1 environment(s) (1 running)" in env_out["error"]["message"]
    assert "env_control" in env_out["next"] and 'compute_status(host_id="x1carbon")' in env_out["next"]
    job_out = host(services, action="remove", host_id="slurm-x1")
    assert "open Slurm job" in job_out["error"]["message"] and "slurm_jobs" in job_out["next"]
    assert store.get_host("x1carbon") is not None and store.get_host("slurm-x1") is not None


def test_remove_unknown_host(env):
    services, *_ = env
    out = host(services, action="remove", host_id="ghost")
    assert out["error"]["code"] == "not_found" and "list" in out["next"]
    assert host(services, action="remove")["error"]["message"] == "host_id is required"


# --------------------------------------------------------------------------- approval


def test_add_and_test_cards_list_the_address_once(env):
    services, *_ = env
    d = hosts_api.describe_compute_host
    for kind in ("ssh", "slurm"):
        add = d({"action": "add", "kind": kind, "name": "Lab box", "address": "labbox"}, services)
        assert add.count("Address:") == 1 and "Address: labbox" in add, add
    test = d({"action": "test", "kind": "ssh", "address": "labbox"}, services)
    assert test.count("Address:") == 1 and "Address: labbox" in test, test


def test_approval_text_for_add_update_remove(env):
    services, store, *_ = env
    store.upsert_host(sample_host(name="Lab box"))
    d = hosts_api.describe_compute_host
    ssh = d({"action": "add", "kind": "ssh", "name": "Lab box", "address": "labbox",
             "user": "alice"}, services)
    assert ssh.startswith("Adds SSH host Lab box to Own Compute after a connection test.")
    assert "Address: labbox" in ssh and "User: alice" in ssh and "Port: 22" in ssh
    assert "Jump host: none" in ssh and "Key file: none" in ssh
    assert "Allow cloud agents: no" in ssh and "Most environments at once: 4" in ssh
    assert "only inside containers" not in ssh
    slurm = d({"action": "add", "kind": "slurm", "name": "HPC", "address": "login"}, services)
    assert slurm.startswith("Adds Slurm host HPC to Own Compute")
    assert "sbatch" in slurm and "login node" in slurm and "Work directory: ~/cloud-compute" in slurm
    assert "this computer" in d({"action": "add", "kind": "local"}, services)
    assert d({"action": "remove", "host_id": "x1carbon"}, services) == (
        "Removes host Lab box (x1carbon) from Own Compute. Nothing on the machine itself is "
        "changed.")
    upd = d({"action": "update", "host_id": "x1carbon", "address": "new"}, services)
    assert upd.startswith("Changes host Lab box (x1carbon) in Own Compute.")
    assert "Address: " in upd and " -> new" in upd and "tested with the new values" in upd
    for action in ("types", "list", "bogus", None):
        assert d({"action": action}, services) is None
    assert d({"action": "test", "host_id": "x1carbon"}, services) is None
    assert d(None, services) is None


def test_approval_gate_asks_for_writes_only_and_keys_are_per_target(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    tool = hosts_api.tools()[1]
    add = approval.approval_for("compute_host", {"action": "add", "kind": "ssh", "address": "a"},
                                tool, services)
    add2 = approval.approval_for("compute_host", {"action": "add", "kind": "ssh", "address": "b"},
                                 tool, services)
    rem = approval.approval_for("compute_host", {"action": "remove", "host_id": "x1carbon"}, tool,
                                services)
    upd = approval.approval_for("compute_host", {"action": "update", "host_id": "x1carbon",
                                                 "name": "n"}, tool, services)
    assert add["action"] == "approve" and add["message"].startswith("Adds SSH host a ")
    assert len({add["rule_key"], add2["rule_key"], rem["rule_key"], upd["rule_key"]}) == 4
    assert rem["rule_key"] == "cloud-compute:compute_host:remove:x1carbon"
    for action in ("types", "list"):
        assert approval.approval_for("compute_host", {"action": action}, tool, services) is None
    assert approval.approval_for("compute_host", {"action": "test", "host_id": "x1carbon"},
                                 tool, services) is None


def test_handler_never_raises_on_broken_services():
    class Broken:
        @property
        def store(self):
            raise RuntimeError("db gone")
    for action in ("list", "add", "remove", "update", "test"):
        out = hosts_api.compute_host({"action": action, "kind": "ssh", "address": "a",
                                      "host_id": "x"}, Broken(), {})
        assert "error" in out


# --------------------------------------------------------------------------- compute_status


def test_status_host_filter_limits_hosts_environments_and_jobs(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    store.upsert_host(sample_slurm_host())
    store.upsert_host(sample_local_host())
    store.upsert_instance(sample_instance())
    store.upsert_instance(sample_instance(id="i-loc00001", name="loc-1", host_id="local"))
    store.upsert_job(sample_job())
    everything = hosts_api.compute_status({}, services, {})
    assert {i["id"] for i in everything["instances"]} == {"i-ab12cd34", "i-loc00001"}
    one = hosts_api.compute_status({"host_id": "local"}, services, {})
    assert [h["id"] for h in one["hosts"]] == ["local"]
    assert [i["id"] for i in one["instances"]] == ["i-loc00001"] and one["jobs"] == []
    assert one["card"] == '::cc-host{id="local"}'
    slurm = hosts_api.compute_status({"host_id": "slurm-x1"}, services, {})
    assert slurm["instances"] == [] and [j["id"] for j in slurm["jobs"]] == ["j-1a2b3c4d"]


def test_status_entries_carry_cards(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    store.upsert_host(sample_slurm_host())
    store.upsert_instance(sample_instance())
    store.upsert_job(sample_job())
    out = hosts_api.compute_status({}, services, {})
    assert out["instances"][0]["card"] == '::cc-env{id="i-ab12cd34"}'
    assert out["jobs"][0]["card"] == '::cc-job{id="j-1a2b3c4d"}'
    assert "card" not in out                       # only a one-host answer has a top-level card


def test_status_lists_templates_compactly(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    out = hosts_api.compute_status({}, services, {})
    by_id = {t["id"]: t for t in out["templates"]}
    assert {"ubuntu-dev", "python-dev", "desktop-novnc", "slurm-python", "hermes-agent",
            "agent-script"} <= set(by_id)
    assert by_id["python-dev"] == {"id": "python-dev", "name": "Python dev", "kind": "environment",
                                   "host_kinds": ["ssh", "local"], "screen": False}
    assert by_id["desktop-novnc"]["screen"] is True
    assert by_id["slurm-python"]["host_kinds"] == ["slurm"]
    assert by_id["hermes-agent"]["kind"] == "agent" and by_id["agent-script"]["kind"] == "agent"
    assert all("variables" not in t and "description" not in t for t in out["templates"])


def test_status_templates_follow_the_host_filter(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    store.upsert_host(sample_slurm_host())
    ssh = {t["id"] for t in hosts_api.compute_status({"host_id": "x1carbon"}, services, {})["templates"]}
    slurm = {t["id"] for t in hosts_api.compute_status({"host_id": "slurm-x1"}, services, {})["templates"]}
    assert "python-dev" in ssh and "slurm-python" not in ssh
    assert "slurm-python" in slurm and "python-dev" not in slurm


def test_status_details_lists_variable_names_and_flags_never_values(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    services.templates.save(TemplateSpec(
        id="lab-env", name="Lab env", description="Lab environment",
        variables=[VariableSpec(name="SET_TOKEN", source="secret:SET_TOKEN"),
                   VariableSpec(name="MISSING_TOKEN", source="secret:MISSING_TOKEN"),
                   VariableSpec(name="MODE", source="literal:very-secret-literal")]))
    plain = hosts_api.compute_status({}, services, {})
    assert "variables" not in {k for t in plain["templates"] for k in t}
    out = hosts_api.compute_status({"details": True}, services, {})
    lab = next(t for t in out["templates"] if t["id"] == "lab-env")
    assert lab["variables"] == [{"name": "SET_TOKEN", "secret": True, "set": True},
                                {"name": "MISSING_TOKEN", "secret": True, "set": False},
                                {"name": "MODE", "secret": False, "set": True}]
    assert lab["description"] == "Lab environment"
    blob = json.dumps(out)
    assert "hunter2-do-not-leak" not in blob and "very-secret-literal" not in blob
    assert hosts_api.compute_status({"details": "true"}, services, {})["templates"][0]["description"]


def test_status_without_hosts_says_how_to_set_up(env):
    services, *_ = env
    out = hosts_api.compute_status({}, services, {})
    assert out["configured"] is False and out["hosts"] == []
    steps = out["next_steps"]
    assert steps[0].startswith('Call compute_host(action="types")')
    assert "SSH alias" in steps[1] and "Never ask for a password" in steps[1]
    assert 'compute_host(action="test"' in steps[2] and 'compute_host(action="add"' in steps[3]
    assert "Add host" in steps[-1] and len(steps) <= 6
    assert out["templates"]                                  # ids are known before a host exists


def test_status_with_hosts_has_no_setup_steps(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    out = hosts_api.compute_status({}, services, {})
    assert out["configured"] is True and "next_steps" not in out


def test_status_survives_a_broken_template_store(env):
    services, store, *_ = env
    store.upsert_host(sample_host())

    class Boom:
        def list(self):
            raise RuntimeError("templates gone")
    services.set("templates", Boom())
    out = hosts_api.compute_status({}, services, {})
    assert out["hosts"] and "templates" not in out and "template list" in out["warning"]


def test_status_result_stays_compact(env):
    services, store, *_ = env
    store.upsert_host(sample_host())
    store.upsert_host(sample_slurm_host())
    store.upsert_host(sample_local_host())
    for h in ("x1carbon", "slurm-x1", "local"):
        store.set_host_state(sample_host_state(host_id=h))
    store.upsert_instance(sample_instance())
    store.upsert_job(sample_job())
    assert len(json.dumps(hosts_api.compute_status({}, services, {}))) < 5000
    assert len(json.dumps(hosts_api.compute_status({"host_id": "x1carbon"}, services, {}))) < 2600
    assert len(json.dumps(hosts_api._COMPUTE_STATUS_SCHEMA)) < 1300
    assert len(json.dumps(hosts_api._COMPUTE_HOST_SCHEMA)) < 3300     # was 2300 before EC2 could be added from chat
