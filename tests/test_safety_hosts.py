"""``compute_host`` must not run a shell command on a remote machine
without the user's approval.

* ``exec_prefix`` and ``interactive_prefix`` cannot be set through the chat tool at all.
* ``test`` runs without approval only for a saved host given by ``host_id``.
* The add / update / test cards show every value; the keys change with every value.
* The fallback hook asks in exactly the same cases.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from cloud_compute_core import approval, hosts_api
from cloud_compute_core.config import Config
from cloud_compute_core.prober import TestResult, TestStep
from cloud_compute_core.store import Store
from cloud_compute_core.testing import (
    FakeEvents, FakeSecrets, make_services, sample_capabilities, sample_host, sample_host_state,
    sample_slurm_host,
)

PAYLOAD = ["sh", "-c", 'echo PWNED > "$HOME/pwned.txt"; exec "$@"', "x"]
PAGE_ONLY = "This setting can only be changed on the Compute page."
PASS = [TestStep("dns", True, "ok"), TestStep("tcp", True, "ok"), TestStep("auth", True, "ok"),
        TestStep("runtime", True, "podman")]


class Prober:
    def __init__(self, store):
        self.store = store
        self.tested = []

    def test_connection(self, spec):
        self.tested.append(spec)
        return TestResult(ok=True, status="online", steps=list(PASS),
                          capabilities=sample_capabilities())

    def probe(self, host):
        state = sample_host_state(host_id=host.id)
        self.store.set_host_state(state)
        return state

    def liveness(self, host):
        return "online"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "ch.db")
    prober = Prober(store)
    services = make_services(store=store, prober=prober, events=FakeEvents(), config=Config(),
                             secrets=FakeSecrets({}))
    yield services, store, prober
    store.close()


@pytest.fixture(scope="module")
def plugin():
    path = Path(__file__).resolve().parents[1] / "__init__.py"
    name = "cc_plugin_safety_hosts"
    spec = importlib.util.spec_from_file_location(name, path,
                                                  submodule_search_locations=[str(path.parent)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def tool():
    return hosts_api.tools()[1]


def gate(services, **args):
    return approval.approval_for("compute_host", args, tool(), services)


def run(services, **args):
    return hosts_api.compute_host(args, services, {})


# ------------------------------------------------------------------ prefixes are page-only


@pytest.mark.parametrize("field", ["exec_prefix", "interactive_prefix"])
@pytest.mark.parametrize("action_args", [
    {"action": "test", "kind": "ssh", "address": "labbox"},
    {"action": "add", "kind": "ssh", "address": "labbox"},
    {"action": "update", "host_id": "x1carbon", "name": "n"},
    {"action": "test", "host_id": "x1carbon"},
])
def test_prefixes_are_refused_with_a_plain_sentence_and_nothing_runs(env, field, action_args):
    services, store, prober = env
    store.upsert_host(sample_host())
    before = store.get_host("x1carbon").to_dict()
    out = run(services, **action_args, **{field: PAYLOAD})
    assert out["error"]["message"] == PAGE_ONLY
    assert prober.tested == []                          # nothing was connected to
    assert store.get_host("x1carbon").to_dict() == before and len(store.list_hosts()) == 1
    # a prefix given as a string is refused the same way
    assert run(services, **action_args, **{field: "sh -c x"})["error"]["message"] == PAGE_ONLY
    assert prober.tested == []


def test_the_prefixes_are_not_in_the_tool_schema_or_the_types_table():
    props = hosts_api._COMPUTE_HOST_SCHEMA["parameters"]["properties"]
    assert "exec_prefix" not in props and "interactive_prefix" not in props
    assert "exec_prefix" not in hosts_api._HOST_PARAMS
    for t in hosts_api._TYPES:
        assert "exec_prefix" not in t["required"] + t["optional"] + list(t["example"])
        assert "interactive_prefix" not in t["required"] + t["optional"]


@pytest.mark.parametrize("action", ["test", "add", "update", "remove", "list", "types"])
def test_a_call_with_a_prefix_is_blocked_before_any_card(env, action):
    services, store, _ = env
    store.upsert_host(sample_host())
    out = gate(services, action=action, kind="ssh", address="labbox", host_id="x1carbon",
               exec_prefix=PAYLOAD)
    assert out == {"action": "block", "message": PAGE_ONLY}


def test_a_saved_host_with_a_page_prefix_can_still_be_tested_and_used(env):
    services, store, prober = env
    store.upsert_host(sample_slurm_host())               # has exec_prefix set (as on the page)
    assert store.get_host("slurm-x1").exec_prefix
    assert gate(services, action="test", host_id="slurm-x1") is None
    out = run(services, action="test", host_id="slurm-x1")
    assert out["ok"] is True
    assert prober.tested[-1].exec_prefix == store.get_host("slurm-x1").exec_prefix


def test_the_page_can_still_add_a_slurm_host_with_a_prefix(env):
    services, store, _ = env
    body = {"kind": "slurm", "name": "HPC", "address": "login", "exec_prefix": ["podman", "exec"]}
    out = hosts_api.create_host(hosts_api.ApiRequest(path_params={}, query={}, body=body,
                                                     services=services))
    assert out["spec"]["exec_prefix"] == ["podman", "exec"]
    patched = hosts_api.patch_host(hosts_api.ApiRequest(
        path_params={"host_id": "hpc"}, query={}, body={"interactive_prefix": ["podman", "exec"]},
        services=services))
    assert patched["spec"]["interactive_prefix"] == ["podman", "exec"]


# ------------------------------------------------------------------ test needs approval


def test_test_of_an_address_that_is_not_saved_asks_and_shows_every_value(env):
    services, *_ = env
    out = gate(services, action="test", kind="ssh", address="labbox", user="bob", port=2222,
               jump_host="bastion", identity_file="C:/keys/id_lab")
    assert out["action"] == "approve"
    msg = out["message"]
    for value in ("labbox", "bob", "2222", "bastion", "C:/keys/id_lab"):
        assert value in msg
    assert "Nothing is saved" in msg and "not saved" in msg


@pytest.mark.parametrize("extra", [{"address": "other"}, {"user": "u"}, {"port": 2200},
                                   {"jump_host": "j"}, {"identity_file": "k"}])
def test_test_of_a_saved_host_with_any_other_connection_value_asks(env, extra):
    services, store, _ = env
    store.upsert_host(sample_host())
    out = gate(services, action="test", host_id="x1carbon", **extra)
    assert out["action"] == "approve"
    assert next(iter(extra.values())).__str__() in out["message"]
    assert " -> " in out["message"]                      # old -> new for the changed value


def test_test_of_a_saved_host_by_id_alone_needs_no_approval(env):
    services, store, _ = env
    store.upsert_host(sample_host())
    assert gate(services, action="test", host_id="x1carbon") is None
    assert gate(services, action="test", host_id="x1carbon", kind="ssh", name="x") is None


def test_test_without_a_host_id_always_asks(env):
    services, *_ = env
    assert gate(services, action="test", kind="ssh", address="labbox")["action"] == "approve"
    assert gate(services, action="test", kind="local")["action"] == "approve"
    assert gate(services, action="test")["action"] == "approve"


def test_test_keys_differ_with_every_value(env):
    services, *_ = env
    keys = {gate(services, action="test", kind="ssh", address=a, user=u)["rule_key"]
            for a in ("one", "two") for u in ("x", "y")}
    assert len(keys) == 4


# ------------------------------------------------------------------ add / update cards and keys


def test_add_card_shows_every_field_and_no_false_container_claim(env):
    services, *_ = env
    msg = gate(services, action="add", kind="ssh", name="Lab", address="labbox", user="bob",
               port=2222, jump_host="bastion", identity_file="C:/k", allow_agents=True,
               max_instances=2)["message"]
    for text in ("Address: labbox", "User: bob", "Port: 2222", "Jump host: bastion",
                 "Key file: C:/k", "Allow cloud agents: yes", "Most environments at once: 2"):
        assert text in msg
    assert "only inside containers" not in msg
    slurm = gate(services, action="add", kind="slurm", name="H", address="login", partition="gpu",
                 account="acc", qos="q", workdir="/scratch/x", user="bob")["message"]
    for text in ("Address: login", "User: bob", "Slurm partition: gpu", "Slurm account: acc",
                 "Slurm QOS: q", "Work directory: /scratch/x", "login node", "sbatch"):
        assert text in slurm
    assert "only inside containers" not in slurm


def test_add_key_changes_with_every_value(env):
    services, *_ = env
    base = dict(action="add", kind="ssh", name="Lab", address="labbox")
    keys = {gate(services, **base)["rule_key"]}
    for extra in ({"user": "bob"}, {"port": 2222}, {"jump_host": "j"}, {"identity_file": "k"},
                  {"allow_agents": True}, {"max_instances": 9}):
        keys.add(gate(services, **base, **extra)["rule_key"])
    assert len(keys) == 7


def test_update_card_shows_old_and_new_for_each_field(env):
    services, store, _ = env
    store.upsert_host(sample_host(address="labbox", user="alice", port=22))
    msg = gate(services, action="update", host_id="x1carbon", address="evil", user="bob",
               port=2222, allow_agents=True)["message"]
    assert "Address: labbox -> evil" in msg
    assert "User: alice -> bob" in msg
    assert "Port: 22 -> 2222" in msg
    assert "Allow cloud agents: no -> yes" in msg
    assert "tested with the new values" in msg


def test_update_card_names_a_page_prefix_that_the_test_will_use(env):
    services, store, _ = env
    store.upsert_host(sample_slurm_host())
    msg = gate(services, action="update", host_id="slurm-x1", address="new-login")["message"]
    assert "command prefix" in msg and "podman exec" in msg


def test_update_key_includes_the_changed_fields_and_new_values(env):
    services, store, _ = env
    store.upsert_host(sample_host())
    keys = {gate(services, action="update", host_id="x1carbon", **kw)["rule_key"]
            for kw in ({"address": "a"}, {"address": "b"}, {"user": "a"}, {"allow_agents": True},
                       {"identity_file": "k"})}
    assert len(keys) == 5
    same = [gate(services, action="update", host_id="x1carbon", address="a")["rule_key"]
            for _ in range(2)]
    assert same[0] == same[1] and same[0].startswith("cloud-compute:compute_host:update:x1carbon:")


def test_remove_key_is_unchanged(env):
    services, store, _ = env
    store.upsert_host(sample_host())
    assert gate(services, action="remove", host_id="x1carbon")["rule_key"] == \
        "cloud-compute:compute_host:remove:x1carbon"


# ------------------------------------------------------------------ the fallback agrees

CASES = [
    {"action": "types"}, {"action": "list"},
    {"action": "test", "host_id": "x1carbon"},
    {"action": "test", "host_id": "x1carbon", "kind": "ssh", "name": "n"},
    {"action": "test", "host_id": "x1carbon", "address": "o"},
    {"action": "test", "host_id": "x1carbon", "user": "o"},
    {"action": "test", "host_id": "x1carbon", "port": 1},
    {"action": "test", "host_id": "x1carbon", "jump_host": "j"},
    {"action": "test", "host_id": "x1carbon", "identity_file": "k"},
    {"action": "test", "kind": "ssh", "address": "labbox"},
    {"action": "test", "kind": "local"}, {"action": "test"},
    {"action": "add", "kind": "ssh", "address": "labbox"},
    {"action": "update", "host_id": "x1carbon", "name": "n"},
    {"action": "remove", "host_id": "x1carbon"},
    {"action": "test", "host_id": "x1carbon", "exec_prefix": ["a"]},
    {"action": "add", "kind": "ssh", "address": "labbox", "interactive_prefix": ["a"]},
]


@pytest.mark.parametrize("args", CASES, ids=[json.dumps(c, sort_keys=True) for c in CASES])
def test_fallback_and_normal_path_agree_for_every_compute_host_case(env, plugin, args):
    services, store, _ = env
    store.upsert_host(sample_host())
    normal = gate(services, **args)
    fallback = plugin._fallback_needs_approval("compute_host", args)
    assert fallback == (normal is not None), (args, normal)
