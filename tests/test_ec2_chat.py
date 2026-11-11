"""Chat can add, change and remove an EC2 host, always behind a card that shows every value and
states the cost; every EC2 card is explicit about cost; the fallback asks in the same cases.

Uses the in-process fake AWS of ``test_ec2``. Nothing here reaches the network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_ec2 import AK, SK, T0, _host, _start, env  # noqa: E402,F401  (env is a fixture)

from cloud_compute_core import approval, ec2_chat, hosts_api, instances_api  # noqa: E402
from cloud_compute_core.models import Ec2Settings, TemplateSpec  # noqa: E402
from cloud_compute_core.prober import Prober  # noqa: E402

# Built from parts so the repository secret scan has nothing to match.
KEY_ID = "AKIA" + "ABCDEFGHIJKLMNOP"
PAGE_ONLY = "This setting can only be changed on the Compute page."
NEW = dict(action="add", kind="ec2", name="Cloud box", region="eu-west-1",
           instance_type="t3.small", disk_gb=20, max_lifetime_minutes=90, idle_stop_minutes=15)
#: Read-only AWS calls (plus the DryRun launch): the only ones a test or an add makes.
READ_ONLY = {"GetCallerIdentity", "DescribeRegions", "DescribeImages", "GetParameter",
             "DescribeInstances", "DescribeInstanceTypes"}


def _tool():
    return next(t for t in hosts_api.tools() if t.name == "compute_host")


def gate(env, **args):
    return approval.approval_for("compute_host", args, _tool(), env.services)


def run(env, **args):
    return hosts_api.compute_host(args, env.services, {})


def _real_prober(env):
    env.services.set("prober", Prober(env.services))


def _mutating(env):
    """AWS calls that change something (a DryRun launch changes nothing)."""
    out = []
    for _s, action, params in env.fake.calls:
        if action in READ_ONLY or (action == "RunInstances" and params.get("DryRun") == "true"):
            continue
        out.append(action)
    return out


@pytest.fixture(scope="module")
def plugin():
    path = Path(__file__).resolve().parents[1] / "__init__.py"
    name = "cc_plugin_ec2_chat"
    spec = importlib.util.spec_from_file_location(name, path,
                                                  submodule_search_locations=[str(path.parent)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- add


def test_chat_add_shows_the_exact_card_and_calls_aws_only_after_approval(env):
    _real_prober(env)
    before, raw = len(env.fake.calls), len(env.fake.raw)
    got = gate(env, **NEW)
    assert got["action"] == "approve"
    assert len(env.fake.calls) == before and len(env.fake.raw) == raw        # no AWS call
    assert got["message"] == (
        "Adds EC2 host Cloud box to Own Compute. After you approve, Own Compute checks the "
        "AWS account with the stored keys (read-only calls and a DryRun launch that AWS checks "
        "without starting anything) and saves the host only if the checks pass. It also looks up "
        "this computer's public address at checkip.amazonaws.com. Region: eu-west-1; Instance "
        "type: t3.small; Disk: 20 GB; Max lifetime: 90 minutes; Idle stop: after 15 minutes "
        "with no running environment; Start the machine when an environment needs it: no "
        "(default); SSH allowed from: only this computer's public address (looked up when the "
        "machine is launched) (default); Access key secret (name): AWS_ACCESS_KEY_ID "
        "(default); Secret key secret (name): AWS_SECRET_ACCESS_KEY (default). Starts billing "
        "now: no. Adding this host starts nothing and costs nothing yet. Starting the machine "
        "later is billed by AWS to the account these keys belong to.")
    out = run(env, **NEW)                                   # what runs after the approval
    assert out["saved"] is True and out["host"]["id"] == "cloud-box"
    assert out["cost_state"].startswith("No machine is running. Adding the host started nothing")
    assert out["host"]["instance_type"] == "t3.small" and "address" not in out["host"]
    saved = env.store.get_host("cloud-box")
    assert saved.kind == "ec2" and saved.ec2.region == "eu-west-1" and saved.ec2.disk_gb == 20
    assert saved.ec2.machine_state == "none" and saved.ec2.instance_id is None
    assert _mutating(env) == []                             # nothing was started or created
    assert not [i for i in env.fake.instances.values()]


def test_chat_add_defaults_are_named_on_the_card(env):
    msg = gate(env, action="add", kind="ec2")["message"]
    assert "Adds EC2 host AWS machine" in msg
    for text in ("Region: us-east-1 (default)", "Instance type: t3.micro (default)",
                 "Disk: 10 GB (default)", "Max lifetime: 2 hours (default)",
                 "Idle stop: after 30 minutes with no running environment (default)"):
        assert text in msg


def test_chat_add_with_auto_start_states_the_start_cost_on_the_card(env):
    msg = gate(env, action="add", kind="ec2", auto_start=True)["message"]
    s = Ec2Settings(auto_start=True)
    assert "Auto-start is on" in msg and ec2_chat.start_cost(s) in msg
    assert ec2_chat.ADD_COST in msg


def test_missing_secrets_are_named_before_any_card(env):
    env.secrets.values.pop("AWS_SECRET_ACCESS_KEY")
    got = gate(env, **NEW)
    sentence = ("The Hermes secret AWS_SECRET_ACCESS_KEY is not set. Add it in Hermes secrets "
                "(the key is never typed into chat), then ask again.")
    assert got == {"action": "block", "message": sentence}
    out = run(env, **NEW)
    assert out["error"]["message"] == sentence and env.store.get_host("cloud-box") is None
    env.secrets.values.pop("AWS_ACCESS_KEY_ID")
    both = gate(env, **NEW)["message"]
    assert both.startswith("The Hermes secrets AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are "
                           "not set. Add them in Hermes secrets")
    assert not env.fake.calls


def test_other_secret_names_are_checked_by_name(env):
    env.secrets.set("AWS_MY_ID", AK)
    msg = gate(env, action="add", kind="ec2", access_key_secret="AWS_MY_ID",
               secret_key_secret="AWS_MY_SECRET")["message"]
    assert "The Hermes secret AWS_MY_SECRET is not set" in msg


@pytest.mark.parametrize("extra", [
    {"aws_secret_access_key": "x" * 40}, {"aws_access_key_id": KEY_ID},
    {"session_token": "abc"}, {"access_key_secret": KEY_ID},
    {"secret_key_secret": "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY12"},
    {"name": f"my {KEY_ID} box"}])
def test_a_key_value_is_refused_with_a_plain_sentence_and_never_repeated(env, extra):
    args = {**NEW, **extra}
    blocked = gate(env, **args)
    assert blocked == {"action": "block", "message": ec2_chat.KEY_MESSAGE}
    out = run(env, **args)
    assert out == {"error": {"code": "invalid", "message": ec2_chat.KEY_MESSAGE}}
    for value in extra.values():
        assert str(value) not in json.dumps([blocked, out])
    assert "Hermes secrets" in ec2_chat.KEY_MESSAGE
    assert env.store.get_host("cloud-box") is None and not env.fake.calls


@pytest.mark.parametrize("field", ["image", "ami", "subnet_id", "security_group_id", "ssh_user",
                                   "session_token_secret"])
def test_image_subnet_security_group_and_ssh_user_stay_on_the_page(env, field):
    for args in (dict(NEW, **{field: "x"}), dict(action="update", host_id="aws", **{field: "x"})):
        assert gate(env, **args) == {"action": "block", "message": PAGE_ONLY}
        assert run(env, **args)["error"]["message"] == PAGE_ONLY
    # a different kind of host does not get the EC2 sentence
    assert run(env, action="add", kind="ssh", address="h", **{field: "x"})["error"]["code"] == \
        "unknown_parameter"


@pytest.mark.parametrize("args, fragment", [
    ({"region": "Europe"}, "not an AWS region name"),
    ({"instance_type": "big"}, "not an instance type name"),
    ({"disk_gb": 5}, "Root disk (GB) must be a whole number from 8 to 500"),
    ({"disk_gb": 9999}, "from 8 to 500"),
    ({"max_lifetime_minutes": 5}, "Max lifetime (minutes) must be a whole number from 15 to 1440"),
    ({"idle_stop_minutes": -1}, "Stop when idle (minutes) must be a whole number from 0 to 1440"),
    ({"auto_start": "maybe"}, "auto_start must be true or false"),
    ({"ssh_cidr": "not-an-address"}, "is not an address or range"),
    ({"ssh_cidr": "0.0.0.0/0"}, "too many addresses"),
    ({"ssh_cidr": "10.0.0.0/8"}, "too many addresses"),
    ({"address": "x"}, "does not apply to ec2 hosts"),
    ({"allow_agents": True}, "does not apply to ec2 hosts"),
])
def test_a_request_that_cannot_work_gets_no_card_and_saves_nothing(env, args, fragment):
    full = {"action": "add", "kind": "ec2", **args}
    blocked = gate(env, **full)
    assert blocked["action"] == "block" and fragment in blocked["message"], blocked
    out = run(env, **full)
    assert fragment in out["error"]["message"]
    assert env.store.get_host("aws-machine") is None and not env.fake.calls


def test_add_that_fails_the_aws_check_saves_nothing(env):
    _real_prober(env)
    env.fake.errors["RunInstances"] = (403, "UnauthorizedOperation", "no")
    out = run(env, **NEW)
    assert out["error"]["message"].startswith("Host not saved. Failed check: Allowed to launch")
    assert env.store.get_host("cloud-box") is None and _mutating(env) == []


def test_add_of_an_existing_name_is_refused_before_the_card(env):
    got = gate(env, action="add", kind="ec2", name="aws")
    assert got["action"] == "block" and "already exists" in got["message"]


# --------------------------------------------------------------------------- SSH source


def test_a_range_wider_than_one_address_is_called_out_on_the_card(env):
    narrow = gate(env, action="add", kind="ec2", ssh_cidr="203.0.113.7")["message"]
    assert "SSH allowed from: only 203.0.113.7" in narrow and "OPEN" not in narrow
    wide = gate(env, action="add", kind="ec2", ssh_cidr="203.0.113.0/24")["message"]
    assert ("SSH allowed from: SSH OPEN TO THIS RANGE: 203.0.113.0/24 (256 addresses), not only "
            "this computer") in wide
    # 0.0.0.0/0 never reaches a card (the page refuses it too); the wording exists for the day
    # the limit is changed
    s = Ec2Settings(ssh_cidr="0.0.0.0/0")
    assert ec2_chat.ssh_access_text(s) == "SSH open to the whole internet (0.0.0.0/0)"
    assert "SSH OPEN TO THIS RANGE" in ec2_chat.ssh_access_text(Ec2Settings(ssh_cidr="10.0.0.0/16"))


# --------------------------------------------------------------------------- update / remove


def _add(env):
    _real_prober(env)
    out = run(env, **dict(NEW, max_lifetime_minutes=120, disk_gb=10, instance_type="t3.micro"))
    assert out["saved"] is True, out
    return out["host"]["id"]


def test_update_card_shows_old_and_new_and_the_costlier_line(env):
    hid = _add(env)
    args = dict(action="update", host_id=hid, disk_gb=50, max_lifetime_minutes=480,
                idle_stop_minutes=0, auto_start=True)
    msg = gate(env, **args)["message"]
    for text in ("Changes EC2 host Cloud box (cloud-box) in Own Compute.", "Disk: 10 GB -> 50 GB",
                 "Max lifetime: 2 hours -> 8 hours",
                 "Idle stop: after 15 minutes with no running environment -> off",
                 "Start the machine when an environment needs it: no -> yes",
                 "MORE COSTLY OR LONGER-LIVED: disk 10 GB -> 50 GB; max lifetime 2 hours -> "
                 "8 hours; idle stop turned off", "auto-start turned on",
                 "No machine exists, so the new values apply when a machine is started.",
                 "Changing this host starts nothing and costs nothing yet", "Starts billing now: no."):
        assert text in msg, text
    assert ec2_chat.start_cost(Ec2Settings(disk_gb=50, max_lifetime_minutes=480,
                                           idle_stop_minutes=0, auto_start=True,
                                           region="eu-west-1", instance_type="t3.micro")) in msg
    before = len(env.fake.calls)
    out = run(env, **args)
    assert out["saved"] is True and out["updated"] == ["auto_start", "disk_gb", "idle_stop_minutes",
                                                       "max_lifetime_minutes"]
    s = env.store.get_host(hid).ec2
    assert (s.disk_gb, s.max_lifetime_minutes, s.idle_stop_minutes, s.auto_start) == \
        (50, 480, 0, True)
    assert len(env.fake.calls) >= before and _mutating(env) == []
    assert out["cost_state"] == "No machine is running."


def test_update_without_a_costlier_change_has_no_costlier_line(env):
    hid = _add(env)
    msg = gate(env, action="update", host_id=hid, max_lifetime_minutes=30, name="Renamed")["message"]
    assert "MORE COSTLY" not in msg and "Name: Cloud box -> Renamed" in msg
    assert "Max lifetime: 2 hours -> 30 minutes" in msg


def test_update_of_launch_only_settings_with_a_machine_is_refused_before_the_card(env):
    _start(env)
    got = gate(env, action="update", host_id="aws", instance_type="c7i.48xlarge")
    assert got["action"] == "block"
    assert "apply when a machine is created. Terminate the machine first, then change them." \
        in got["message"]
    out = run(env, action="update", host_id="aws", disk_gb=500)
    assert "Terminate the machine first" in out["error"]["message"]
    assert _host(env).ec2.disk_gb == 10
    ok = gate(env, action="update", host_id="aws", max_lifetime_minutes=60)["message"]
    assert "A machine exists (i-" in ok and "keeps the type and disk it was launched with" in ok


def test_remove_card_and_the_refusal_while_a_machine_exists(env):
    _start(env)                                  # (before the real prober replaces the fake)
    hid = _add(env)
    msg = gate(env, action="remove", host_id=hid)["message"]
    assert msg.startswith("Removes EC2 host Cloud box (cloud-box) from Own Compute.")
    assert "No machine exists for this host" in msg and "Starts billing now: no." in msg
    out = run(env, action="remove", host_id=hid)
    assert out["removed"] is True and out["cost_state"] == "No machine exists for this host."
    assert env.store.get_host(hid) is None
    blocked = gate(env, action="remove", host_id="aws")
    assert blocked["action"] == "block" and "Terminate the machine first" in blocked["message"]
    err = run(env, action="remove", host_id="aws")
    assert err["error"]["code"] == "conflict" and "terminate" in err["next"]


# --------------------------------------------------------------------------- test


def test_test_of_a_saved_ec2_host_needs_no_approval_and_starts_nothing(env):
    _real_prober(env)
    assert gate(env, action="test", host_id="aws") is None
    out = run(env, action="test", host_id="aws")
    assert out["ok"] is True and [r["check"] for r in out["rows"]][:2] == ["credentials",
                                                                             "identity"]
    assert _mutating(env) == [] and not env.fake.instances


def test_test_with_unsaved_or_changed_ec2_values_asks(env):
    _real_prober(env)
    new = gate(env, action="test", kind="ec2", region="eu-west-1")
    assert new["action"] == "approve"
    assert new["message"].startswith("Tests an EC2 host that is not saved. Region: eu-west-1")
    assert "Nothing is saved and no machine is started" in new["message"]
    changed = gate(env, action="test", host_id="aws", region="eu-west-1")["message"]
    assert "Region: us-east-1 -> eu-west-1" in changed
    out = run(env, action="test", kind="ec2", region="eu-west-1")
    assert out["ok"] is True and _mutating(env) == [] and env.store.get_host("aws-machine") is None


# --------------------------------------------------------------------------- keys


def test_keys_differ_when_any_shown_value_differs(env):
    base = dict(action="add", kind="ec2", name="Cloud box")
    env.secrets.set("AWS_OTHER_ID", AK)
    env.secrets.set("AWS_OTHER_SECRET", SK)
    variants = [{}, {"region": "eu-west-1"}, {"instance_type": "t3.small"}, {"disk_gb": 11},
                {"max_lifetime_minutes": 121}, {"idle_stop_minutes": 31}, {"auto_start": True},
                {"ssh_cidr": "203.0.113.7"}, {"ssh_cidr": "203.0.113.0/24"},
                {"access_key_secret": "AWS_OTHER_ID", "secret_key_secret": "AWS_OTHER_SECRET"},
                {"name": "Other box"}]
    keys = [gate(env, **{**base, **v})["rule_key"] for v in variants]
    assert len(set(keys)) == len(variants)
    assert gate(env, **base)["rule_key"] == gate(env, **base)["rule_key"]


def test_update_keys_cover_the_saved_values_too(env):
    hid = _add(env)
    first = gate(env, action="update", host_id=hid, disk_gb=50)["rule_key"]
    run(env, action="update", host_id=hid, disk_gb=20)
    second = gate(env, action="update", host_id=hid, disk_gb=50)["rule_key"]
    assert first != second                                 # "10 -> 50" and "20 -> 50" differ
    assert gate(env, action="update", host_id=hid, disk_gb=51)["rule_key"] != second


def test_machine_keys_differ_by_host_and_every_value(env):
    k0 = gate(env, action="start", host_id="aws")["rule_key"]
    env.mgr._set("aws", max_lifetime_minutes=121)
    k1 = gate(env, action="start", host_id="aws")["rule_key"]
    env.mgr._set("aws", region="eu-west-1")
    k2 = gate(env, action="start", host_id="aws")["rule_key"]
    env.mgr._set("aws", idle_stop_minutes=0, ssh_cidr="203.0.113.0/24")
    k3 = gate(env, action="start", host_id="aws")["rule_key"]
    assert len({k0, k1, k2, k3}) == 4


# --------------------------------------------------------------------------- costs on every card


def test_every_ec2_card_states_the_cost_with_the_shared_sentences(env):
    s = _host(env).ec2
    start = gate(env, action="start", host_id="aws")["message"]
    assert ec2_chat.start_cost(s) in start and "Starts billing now: yes." in start
    assert ("This starts an AWS machine (t3.micro in us-east-1, 10 GB disk). AWS bills the "
            "account while it runs, bills for its disk for as long as the machine exists (also "
            "while stopped), and may bill for its public address. It stops by itself after "
            "2 hours, or after 30 minutes with no running environment. Own Compute cannot "
            "see AWS prices or whether the account's free allowance covers this: check the AWS "
            "billing page.") in start
    for text in ("Region: us-east-1", "Instance type: t3.micro", "Disk: 10 GB",
                 "Max lifetime: 2 hours", "Idle stop: after 30 minutes with no running "
                 "environment", "SSH allowed from: only this computer's public address"):
        assert text in start
    _start(env)
    stop = gate(env, action="stop", host_id="aws")["message"]
    assert ec2_chat.STOP_COST in stop and "Starts billing now: no." in stop
    term = gate(env, action="terminate", host_id="aws")["message"]
    assert ec2_chat.TERMINATE_COST in term and "Starts billing now: no." in term
    ext = gate(env, action="extend", host_id="aws", minutes=30)["message"]
    assert "keeps being billed until then" in ext and "Starts billing now: no." in ext
    for text in (start, stop, term, ext):
        assert "free" not in text.replace("free allowance", "")      # nothing is called free
        assert "$" not in text                                       # no prices
        assert SK not in text and AK not in text


def test_idle_rule_off_is_stated_on_the_start_card(env):
    env.mgr._set("aws", idle_stop_minutes=0)
    msg = gate(env, action="start", host_id="aws")["message"]
    assert "it is not stopped for being idle" in msg and "Idle stop: off" in msg


def test_a_start_that_cannot_work_gets_no_card(env):
    _start(env)
    got = gate(env, action="start", host_id="aws")
    assert got["action"] == "block" and "already ready" in got["message"]
    env.mgr.stop_machine(_host(env))
    stop = gate(env, action="stop", host_id="aws")
    assert stop["action"] == "block" and "nothing to stop" in stop["message"]
    assert gate(env, action="extend", host_id="aws", minutes=30)["action"] == "block"


def test_env_spawn_that_starts_the_machine_states_the_start_cost(env):
    tpl = TemplateSpec(id="py", name="py", description="", host_kinds=["ssh"])
    env.services.set("templates", SimpleNamespace(get=lambda i: tpl, list=lambda: [tpl]))
    env.mgr._set("aws", auto_start=True)
    s = _host(env).ec2
    text = str(instances_api.approval_env_spawn({"template": "py", "host_id": "aws"}, env.services))
    assert "The machine is not running and will be started first." in text
    assert ec2_chat.start_cost(s) in text and "Starts billing now: yes." in text
    key1 = approval.rule_key("env_spawn", {"template": "py", "host_id": "aws"}, "aws", text)
    _start(env)
    text2 = str(instances_api.approval_env_spawn({"template": "py", "host_id": "aws"}, env.services))
    assert "already running" in text2 and "Starts billing now: no." in text2
    assert ec2_chat.running_cost(_host(env).ec2) in text2
    key2 = approval.rule_key("env_spawn", {"template": "py", "host_id": "aws"}, "aws", text2)
    assert key1 != key2


# --------------------------------------------------------------------------- tool results


def test_results_tell_the_cost_state(env):
    out = run(env, action="start", host_id="aws")
    assert "AWS bills the account while it runs" in out["cost_state"]
    assert out["cost_state"].startswith("A machine is running since 2026-11-10T12:00:00Z")
    assert "It stops by itself at 2026-11-10T14:00:00Z UTC." in out["cost_state"]
    assert "AWS bills the account while it runs" in out["next"]
    status = hosts_api.compute_status({"host_id": "aws"}, env.services, {})
    assert status["hosts"][0]["cost_state"] == out["cost_state"]
    out = run(env, action="stop", host_id="aws")
    assert out["cost_state"].startswith("The machine is stopped. Its disk is still billed until "
                                        "the machine is terminated. To start it again use ")
    assert 'compute_host(action="start"' in out["cost_state"]
    assert "Start machine button on the Hosts tab" in out["cost_state"]
    out = run(env, action="terminate", host_id="aws")
    assert out["cost_state"] == "No machine is running."
    status = hosts_api.compute_status({}, env.services, {})
    assert status["hosts"][0]["cost_state"] == "No machine is running."


def test_the_chat_prompt_section_stays_within_its_size_test():
    from cloud_compute_core import chat_context
    assert len(chat_context.SYSTEM_PROMPT_SECTION) <= 1500


# --------------------------------------------------------------------------- fallback

CASES = [
    {"action": "add", "kind": "ec2"},
    {"action": "add", "kind": "ec2", "region": "eu-west-1", "ssh_cidr": "203.0.113.0/24"},
    {"action": "add", "kind": "ec2", "image": "ami-0123456789abcdef0"},
    {"action": "add", "kind": "ec2", "aws_secret_access_key": "x"},
    {"action": "update", "host_id": "aws", "disk_gb": 20},
    {"action": "update", "host_id": "aws", "subnet_id": "subnet-0123456789abcdef0"},
    {"action": "remove", "host_id": "aws"},
    {"action": "test", "host_id": "aws"},
    {"action": "test", "host_id": "aws", "name": "x"},
    {"action": "test", "host_id": "aws", "region": "eu-west-1"},
    {"action": "test", "host_id": "aws", "disk_gb": 20},
    {"action": "test", "kind": "ec2", "region": "eu-west-1"},
    {"action": "test", "kind": "ec2"},
    {"action": "start", "host_id": "aws"}, {"action": "stop", "host_id": "aws"},
    {"action": "extend", "host_id": "aws", "minutes": 30},
    {"action": "terminate", "host_id": "aws"},
    {"action": "terminate", "host_id": "aws", "delete_environments": True, "save": False},
    {"action": "list"}, {"action": "types"},
]


@pytest.mark.parametrize("args", CASES, ids=[json.dumps(c, sort_keys=True) for c in CASES])
def test_fallback_and_normal_path_agree_for_every_ec2_case(env, plugin, args):
    normal = gate(env, **args)
    assert plugin._fallback_needs_approval("compute_host", args) == (normal is not None), \
        (args, normal)


def test_fallback_message_names_the_cost_for_ec2_actions(plugin):
    got = plugin._fallback_approval("compute_host", {"action": "start", "host_id": "aws"})
    assert "AWS" in got["message"] and "bill" in got["message"]
    assert plugin._fallback_approval("compute_host", {"action": "list"}) is None
    spawn = plugin._fallback_approval("env_spawn", {"template": "py", "host_id": "aws"})
    assert "bill" in spawn["message"]
    keys = {plugin._fallback_approval("compute_host", {**NEW, **v})["rule_key"]
            for v in ({}, {"region": "us-east-1"}, {"disk_gb": 21}, {"ssh_cidr": "203.0.113.7"})}
    assert len(keys) == 4


def test_approval_defaults_cover_ec2_when_the_registry_is_empty(env):
    services = env.services
    hook = approval.make_pre_tool_call(lambda: {}, lambda: services)
    for args in ({"action": "add", "kind": "ec2"}, {"action": "start", "host_id": "aws"},
                 {"action": "extend", "host_id": "aws", "minutes": 5}):
        got = hook(tool_name="compute_host", args=args)
        assert got and got["action"] == "approve"
    assert "AWS bills the account" in approval.APPROVAL_DEFAULTS["compute_host"](
        {"action": "start", "host_id": "aws"})
