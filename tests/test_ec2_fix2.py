"""Regression tests for the EC2 host type: a stop that must finish, the AWS account of a
machine, the start counter, the boot-time stop unit, a start claimed by one process, and smaller
cases. They use the in-process fake AWS of ``test_ec2``.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_ec2 import FakeAws, T0, _err, _host, _keygen, _start, env  # noqa: E402,F401
from test_ec2_chat import plugin  # noqa: E402,F401  (a fixture)

from cloud_compute_core import approval, ec2 as ec2_mod, hosts_api  # noqa: E402
from cloud_compute_core.interfaces import ApiError, ApiRequest  # noqa: E402


def _tool():
    return next(t for t in hosts_api.tools() if t.name == "compute_host")


def gate(env, **args):
    return approval.approval_for("compute_host", args, _tool(), env.services)


def _restart(env, **kw):
    """A new manager over the same store and AWS: Hermes quit and started again."""
    mgr = ec2_mod.Ec2Manager(env.services, http=env.fake, keygen=_keygen, sleep=lambda s: None,
                             clock=lambda: env.clock.now, background=False, poll_s=0, **kw)
    env.services.set("ec2", mgr)
    env.mgr = mgr
    return mgr


def _stop_cut_short(env):
    """The machine is ready and a Stop is sent, but the process quits before the stop wait
    ran (shutdown waits 5 s for workers): the host is left in ``stopping``."""
    _start(env)
    env.mgr.background = True
    env.mgr.run_in_background = lambda fn, name: None
    env.mgr.stop_machine(_host(env))
    assert _host(env).ec2.machine_state == "stopping"
    return _restart(env)                       # a new process: no claims


def _hours(env, n):
    env.clock.now = T0 + timedelta(hours=n)
    env.mgr._last_reconcile.clear()


# --------------------------------------------------------------------------- a stop that must finish


def test_n1_a_cut_short_stop_is_settled_by_the_next_check(env):
    _stop_cut_short(env)
    iid = _host(env).ec2.instance_id
    _hours(env, 1)
    env.mgr.offline_state(_host(env))                       # what each probe tick does
    h = _host(env)
    assert env.fake.instances[iid]["state"] == "stopped"
    assert h.ec2.machine_state == "stopped"
    # Start is offered again: the page route and the chat card both accept it
    card = gate(env, action="start", host_id="aws")
    assert card["action"] == "approve" and "Starts the stopped" in card["message"]
    env.mgr.start_machine(_host(env), wait=True)
    assert _host(env).ec2.machine_state == "ready"


def test_n1_check_aws_on_the_page_settles_a_stuck_stopping_host(env):
    _stop_cut_short(env)
    env.mgr.reconcile(_host(env))                           # the Check AWS button
    assert _host(env).ec2.machine_state == "stopped"


def test_n1_the_watcher_settles_a_stuck_stopping_host(env):
    _stop_cut_short(env)
    from cloud_compute_core import ec2_api
    ec2_api.run_watch(env.services)
    assert _host(env).ec2.machine_state == "stopped"


def test_n1b_a_failed_read_during_the_stop_wait_is_not_a_terminal_state(env):
    _start(env)
    orig = env.fake.do_DescribeInstances
    env.fake.do_DescribeInstances = lambda p: (
        503, b"<Response><Errors><Error><Code>Unavailable</Code></Error></Errors></Response>"
    ) if "StopInstances" in env.fake.actions() else orig(p)
    env.mgr.wait_running_s = 0.05
    env.mgr.stop_machine(_host(env))            # inline wait: every read fails, nothing is decided
    assert _host(env).ec2.machine_state == "stopping"
    env.fake.do_DescribeInstances = orig
    _hours(env, 48)
    env.mgr.offline_state(_host(env))
    assert _host(env).ec2.machine_state == "stopped"


def test_n1_a_failed_read_during_a_check_is_tried_again_next_time(env):
    _stop_cut_short(env)
    orig = env.fake.do_DescribeInstances
    env.fake.do_DescribeInstances = lambda p: (503, b"<Response><Errors><Error>"
                                               b"<Code>Unavailable</Code></Error></Errors></Response>")
    env.mgr.offline_state(_host(env))
    assert _host(env).ec2.machine_state == "stopping"       # still waiting, not an error
    env.fake.do_DescribeInstances = orig
    env.mgr.offline_state(_host(env))
    assert _host(env).ec2.machine_state == "stopped"


def test_n1_a_stop_wait_that_ends_before_aws_says_stopped_does_not_claim_it(env):
    _start(env)
    iid = _host(env).ec2.instance_id

    def never_stopped(p):                         # AWS stays in "stopping"
        inst = env.fake.instances[iid]
        inst["state"] = "stopping"
        return 200, {"reservationSet": [{"instancesSet": [env.fake._item(inst)]}]}
    env.fake.do_DescribeInstances = never_stopped
    env.mgr.wait_running_s = 0.05
    env.mgr.stop_machine(_host(env))
    assert _host(env).ec2.machine_state == "stopping"       # was marked stopped on the old code


def test_n1_aws_running_after_the_stop_was_requested_is_stopped_again(env):
    _stop_cut_short(env)
    iid = _host(env).ec2.instance_id
    env.fake.instances[iid]["state"] = "running"             # the stop did not take
    before = env.fake.actions().count("StopInstances")
    env.mgr.offline_state(_host(env))
    assert env.fake.actions().count("StopInstances") == before + 1
    assert _host(env).ec2.machine_state == "stopping"
    env.mgr.offline_state(_host(env))                        # AWS is stopping/stopped now
    assert _host(env).ec2.machine_state == "stopped"


def test_n1_a_failed_second_stop_says_the_machine_may_still_be_billed(env):
    _stop_cut_short(env)
    iid = _host(env).ec2.instance_id
    env.fake.instances[iid]["state"] = "running"
    env.fake.errors["StopInstances"] = (500, "InternalError", "boom")
    env.mgr.offline_state(_host(env))
    h = _host(env)
    assert h.ec2.machine_state == "stopping"
    assert "may still be running and billed" in h.ec2.machine_detail


def test_n1_a_stopping_host_whose_instance_is_gone_is_cleared(env):
    _stop_cut_short(env)
    iid = _host(env).ec2.instance_id
    env.fake.instances[iid]["state"] = "terminated"
    env.mgr.offline_state(_host(env))
    assert _host(env).ec2.machine_state == "none" and _host(env).ec2.instance_id is None


@pytest.mark.parametrize("state", ["launching", "starting"])
def test_n1_check_aws_resolves_a_start_whose_worker_died(env, state):
    _start(env)
    if state == "starting":
        env.mgr.stop_machine(_host(env))
    env.mgr.background = True
    env.mgr.run_in_background = lambda fn, name: None
    if state == "starting":
        env.mgr.start_machine(_host(env))
    else:                                   # a launch whose worker never ran
        env.mgr.terminate_machine(_host(env))
        env.mgr.start_machine(_host(env))
    assert _host(env).ec2.machine_state == state
    _restart(env)                           # new process: no claims
    env.mgr.reconcile(_host(env))           # Check AWS
    assert _host(env).ec2.machine_state == "ready"


# --------------------------------------------------------------------------- the AWS account and key names of a machine


def _patch(env, ec2):
    return hosts_api.patch_host(ApiRequest(path_params={"host_id": "aws"}, query={},
                                           body={"ec2": ec2}, services=env.services))


def _other_keys(env):
    env.secrets.set("AWS_OTHER_AK", "AKIDOTHERFAKE0000000")      # ws-scan:allow (fake)
    env.secrets.set("AWS_OTHER_SK", "other/fake/secret/value/for/tests/only00")   # ws-scan:allow


@pytest.mark.parametrize("change", [
    {"access_key_secret": "AWS_OTHER_AK"}, {"secret_key_secret": "AWS_OTHER_SK"},
    {"session_token_secret": "AWS_OTHER_TOKEN"}, {"session_token_secret": None}])
@pytest.mark.parametrize("state", ["stopped", "ready"])
def test_n2_key_secret_names_cannot_change_while_a_machine_exists_page(env, change, state):
    _start(env)
    if state == "stopped":
        env.mgr.stop_machine(_host(env))
    before = _host(env).ec2.to_dict()
    with pytest.raises(ApiError) as info:
        _patch(env, change)
    assert "cannot change while the host has a machine" in info.value.message
    assert "Terminate the machine first" in info.value.message
    assert _host(env).ec2.to_dict() == before


def test_n2_key_secret_names_cannot_change_while_a_launch_is_unconfirmed(env):
    h = _host(env)
    h.ec2.launch_token = "tok"
    env.store.upsert_host(h)
    with pytest.raises(ApiError):
        _patch(env, {"access_key_secret": "AWS_OTHER_AK"})


def test_n2_key_secret_names_can_change_without_a_machine_and_same_values_pass(env):
    _other_keys(env)
    _patch(env, {"access_key_secret": "AWS_OTHER_AK", "secret_key_secret": "AWS_OTHER_SK"})
    assert _host(env).ec2.access_key_secret == "AWS_OTHER_AK"
    _start(env)
    _patch(env, {"access_key_secret": "AWS_OTHER_AK", "idle_stop_minutes": 5})   # unchanged name
    assert _host(env).ec2.idle_stop_minutes == 5


def test_n2_chat_update_of_key_names_is_blocked_while_a_machine_exists(env):
    _start(env)
    _other_keys(env)
    got = gate(env, action="update", host_id="aws", access_key_secret="AWS_OTHER_AK",
               secret_key_secret="AWS_OTHER_SK")
    assert got["action"] == "block" and "cannot change while the host has a machine" in got["message"]
    out = hosts_api.compute_host({"action": "update", "host_id": "aws",
                                  "access_key_secret": "AWS_OTHER_AK",
                                  "secret_key_secret": "AWS_OTHER_SK"}, env.services, {})
    assert out.get("error") or not out.get("saved")
    assert _host(env).ec2.access_key_secret == "AWS_ACCESS_KEY_ID"


def _other_account(env):
    """The same secret names now hold the keys of another account: that account cannot see
    the machine of the first one."""
    env.fake.account = "999999999999"
    iid = _host(env).ec2.instance_id
    return iid, env.fake.instances.pop(iid)


def test_n2_the_account_is_recorded_when_the_machine_is_launched(env):
    _start(env)
    assert _host(env).ec2.account_id == "123456789012"


def test_n2_reconcile_keeps_the_machine_when_the_keys_are_of_another_account(env):
    _start(env)
    iid, hidden = _other_account(env)
    env.mgr.reconcile(_host(env))
    h = _host(env)
    assert h.ec2.instance_id == iid and h.ec2.machine_state == "ready"
    assert h.ec2.machine_detail == ec2_mod.FOREIGN_ACCOUNT
    assert "different account than the one this machine was started in" in h.ec2.machine_detail
    assert "Restore the keys, or stop it in the AWS console." in h.ec2.machine_detail
    assert hidden["state"] == "running"


def test_n2_the_same_account_not_found_clears_as_before(env):
    _start(env)
    iid = _host(env).ec2.instance_id
    env.fake.instances.pop(iid)
    env.mgr.reconcile(_host(env))
    h = _host(env)
    assert h.ec2.instance_id is None and h.ec2.machine_state == "none" and h.ec2.account_id is None


def test_n2_an_account_that_cannot_be_determined_keeps_the_machine(env):
    _start(env)
    iid = _host(env).ec2.instance_id
    env.fake.instances.pop(iid)
    env.fake.errors["GetCallerIdentity"] = (503, "Unavailable", "later")
    env.mgr.reconcile(_host(env))
    h = _host(env)
    assert h.ec2.instance_id == iid
    assert h.ec2.machine_detail == ec2_mod.UNKNOWN_ACCOUNT
    del env.fake.errors["GetCallerIdentity"]
    env.mgr.reconcile(_host(env))                     # same account again: now it is gone
    assert _host(env).ec2.instance_id is None


@pytest.mark.parametrize("act", ["stop", "terminate", "start"])
def test_n2_stop_terminate_and_start_never_forget_or_replace_a_machine_of_another_account(env, act):
    _start(env)
    if act == "start":
        env.mgr.stop_machine(_host(env))
    launches = env.fake.actions().count("RunInstances")
    iid, _hidden = _other_account(env)
    call = {"stop": env.mgr.stop_machine, "terminate": env.mgr.terminate_machine,
            "start": env.mgr.start_machine}[act]
    with pytest.raises(ApiError) as info:
        call(_host(env))
    assert "different account" in info.value.message
    assert _host(env).ec2.instance_id == iid
    assert env.fake.actions().count("RunInstances") == launches      # no second instance


def test_n2_a_stopping_machine_of_another_account_is_kept_too(env):
    _stop_cut_short(env)
    iid, _hidden = _other_account(env)
    env.mgr.offline_state(_host(env))
    h = _host(env)
    assert h.ec2.instance_id == iid and h.ec2.machine_state == "stopping"
    assert h.ec2.machine_detail == ec2_mod.FOREIGN_ACCOUNT


def test_n2_the_warning_goes_when_the_keys_are_restored(env):
    _start(env)
    iid, hidden = _other_account(env)
    env.mgr.reconcile(_host(env))
    assert _host(env).ec2.machine_detail == ec2_mod.FOREIGN_ACCOUNT
    env.fake.account = "123456789012"
    env.fake.instances[iid] = hidden
    env.mgr.reconcile(_host(env))
    assert _host(env).ec2.machine_detail == "" and _host(env).ec2.instance_id == iid


def test_n2_a_machine_without_a_recorded_account_gets_it_when_it_is_seen(env):
    _start(env)
    h = _host(env)
    h.ec2.account_id = None
    env.store.upsert_host(h)
    env.mgr.reconcile(_host(env))
    assert _host(env).ec2.account_id == "123456789012"


# --------------------------------------------------------------------------- the start counter

from types import SimpleNamespace  # noqa: E402

from cloud_compute_core import instances_api  # noqa: E402
from cloud_compute_core.models import TemplateSpec  # noqa: E402


def _spawn_key(env):
    text = str(instances_api.approval_env_spawn({"template": "py", "host_id": "aws"}, env.services))
    return text, approval.rule_key("env_spawn", {"template": "py", "host_id": "aws"}, "aws", text)


def _one_cycle(env, hours):
    env.mgr.start_machine(_host(env), wait=True)
    env.clock.now = T0 + timedelta(hours=hours)
    env.mgr.stop_machine(_host(env))
    env.mgr._finish_stop("aws")


def test_n5_two_consecutive_starts_have_different_cards_and_keys(env):
    first = gate(env, action="start", host_id="aws")
    assert "Start number 1 of this host's machine; last stopped: never." in first["message"]
    _one_cycle(env, 1)
    second = gate(env, action="start", host_id="aws")
    _one_cycle(env, 2)
    third = gate(env, action="start", host_id="aws")
    assert "Start number 2 of this host's machine" in second["message"]
    assert "last stopped: never" not in second["message"]
    assert "Start number 3" in third["message"]
    assert len({first["rule_key"], second["rule_key"], third["rule_key"]}) == 3


def test_n5_an_approval_of_one_start_does_not_cover_the_next(env):
    _one_cycle(env, 1)
    approved = gate(env, action="start", host_id="aws")["rule_key"]     # "always" for this key
    env.mgr.start_machine(_host(env), wait=True)
    env.mgr.stop_machine(_host(env))
    env.mgr._finish_stop("aws")
    assert gate(env, action="start", host_id="aws")["rule_key"] != approved


def test_n5_an_unchanged_card_keeps_its_key_until_a_start_happens(env):
    _one_cycle(env, 1)
    assert (gate(env, action="start", host_id="aws")["rule_key"]
            == gate(env, action="start", host_id="aws")["rule_key"])


def test_n5_the_auto_start_part_of_env_spawn_changes_with_every_start(env):
    tpl = TemplateSpec(id="py", name="py", description="", host_kinds=["ssh"])
    env.services.set("templates", SimpleNamespace(get=lambda i: tpl, list=lambda: [tpl]))
    env.mgr._set("aws", auto_start=True)
    _one_cycle(env, 1)
    text1, key1 = _spawn_key(env)
    _one_cycle(env, 2)
    text2, key2 = _spawn_key(env)
    assert "Start number 2 of this host's machine" in text1 and "Start number 3" in text2
    assert key1 != key2


def test_n5_the_fallback_key_of_a_start_extend_or_spawn_is_new_every_time(plugin):
    for tool, args in (("compute_host", {"action": "start", "host_id": "aws"}),
                       ("compute_host", {"action": "extend", "host_id": "aws", "minutes": 30}),
                       ("env_spawn", {"template": "py", "host_id": "aws"})):
        a, b = plugin._fallback_approval(tool, args), plugin._fallback_approval(tool, args)
        assert a["rule_key"] != b["rule_key"], tool
    # what is not a machine start keeps its stable key
    s1 = plugin._fallback_approval("compute_host", {"action": "stop", "host_id": "aws"})
    s2 = plugin._fallback_approval("compute_host", {"action": "stop", "host_id": "aws"})
    assert s1["rule_key"] == s2["rule_key"]


def test_n5_the_unverified_key_of_a_start_is_new_every_time(env):
    a = approval._fail_closed("compute_host", {"action": "start", "host_id": "aws"})
    b = approval._fail_closed("compute_host", {"action": "start", "host_id": "aws"})
    assert a["rule_key"] != b["rule_key"]


# --------------------------------------------------------------------------- the boot-time stop unit

import base64  # noqa: E402

from cloud_compute_core.models import Ec2Settings, HostSpec  # noqa: E402
from cloud_compute_core.testing import exec_result  # noqa: E402


def _launched_with(env, minutes):
    """Replace the host with one whose machine is launched with a max lifetime of ``minutes``."""
    env.store.upsert_host(HostSpec(id="aws", name="AWS box", kind="ec2",
                                   address=ec2_mod.NO_MACHINE,
                                   ec2=Ec2Settings(region="us-east-1",
                                                   max_lifetime_minutes=minutes)))


def _rearm_calls(env):
    return [c.argv for c in env.transports.get("aws").calls
            if c.argv and any("systemd-run" in a for a in c.argv)]


def test_n3_the_lifetime_given_at_launch_is_recorded(env):
    _launched_with(env, 1440)
    _start(env)
    assert _host(env).ec2.unit_lifetime_minutes == 1440
    unit = base64.b64decode(env.fake.instances[_host(env).ec2.instance_id]["user_data"]).decode()
    assert "--on-active=1440m --timer-property=AccuracySec=1s systemctl poweroff\"" in unit


def test_n3_a_lowered_lifetime_is_written_to_the_machine_when_it_is_reached(env):
    _launched_with(env, 1440)
    _start(env)
    env.mgr.stop_machine(_host(env))
    _patch(env, {"max_lifetime_minutes": 15})
    env.mgr.start_machine(_host(env), wait=True)
    argv = _rearm_calls(env)[-1]
    assert argv[-1] == "15"                         # the lifetime the boot unit gets
    assert "cloud-compute-limit.service" in argv[2] and "sed -i" in argv[2]
    assert _host(env).ec2.unit_lifetime_minutes == 15


def test_n3_the_start_card_says_what_the_machine_does_if_hermes_closes_early(env):
    _launched_with(env, 1440)
    _start(env)
    env.mgr.stop_machine(_host(env))
    same = gate(env, action="start", host_id="aws")["message"]
    assert "If Hermes closes" not in same
    _patch(env, {"max_lifetime_minutes": 15})
    card = gate(env, action="start", host_id="aws")["message"]
    assert "It stops by itself after 15 minutes" in card
    assert ("If Hermes closes before it reaches the machine, the machine stops after "
            "24 hours instead.") in card
    assert ec2_mod.boot_timer_note(_host(env).ec2).endswith("24 hours instead.")
    assert env.mgr.machine(_host(env))["boot_timer_note"].endswith("24 hours instead.")
    env.mgr.start_machine(_host(env), wait=True)         # reached: written to the machine
    assert env.mgr.machine(_host(env))["boot_timer_note"] == ""


def test_n3_the_auto_start_spawn_card_carries_the_same_sentence(env):
    from cloud_compute_core import ec2_chat
    _launched_with(env, 1440)
    _start(env)
    env.mgr.stop_machine(_host(env))
    _patch(env, {"max_lifetime_minutes": 15})
    s = _host(env).ec2
    assert "stops after 24 hours instead" in ec2_chat.spawn_text(_host(env), s)


def test_n3_a_unit_that_could_not_be_rewritten_keeps_its_recorded_lifetime(env):
    _launched_with(env, 1440)
    _start(env)
    env.mgr.stop_machine(_host(env))
    _patch(env, {"max_lifetime_minutes": 15})
    env.transports.get("aws")._run_rules.insert(0, ec2_rule(3))
    env.mgr.start_machine(_host(env), wait=True)                 # the stop is armed (rc 3)
    assert _host(env).ec2.machine_state == "ready"
    assert _host(env).ec2.unit_lifetime_minutes == 1440
    assert "stops after 24 hours instead" in ec2_mod.boot_timer_note(_host(env).ec2)


def ec2_rule(rc):
    import re
    from cloud_compute_core.testing import _Rule, _argv_matcher
    return _Rule(_argv_matcher(re.compile("shutdown")), exec_result(rc=rc))


def test_n3_a_failed_stop_schedule_still_fails_the_start(env):
    _launched_with(env, 120)
    _start(env)
    env.mgr.stop_machine(_host(env))
    env.transports.get("aws")._run_rules.insert(0, ec2_rule(1))
    env.mgr.start_machine(_host(env), wait=True)
    assert _host(env).ec2.machine_state == "error"


def test_n3_changing_the_lifetime_of_a_reachable_machine_updates_its_boot_timer(env):
    _launched_with(env, 1440)
    _start(env)
    n = len(_rearm_calls(env))
    _patch(env, {"max_lifetime_minutes": 60})
    assert _host(env).ec2.unit_lifetime_minutes == 1440
    env.mgr.offline_state(_host(env))                  # the next check reaches the machine
    assert _host(env).ec2.unit_lifetime_minutes == 60
    assert len(_rearm_calls(env)) == n + 1 and _rearm_calls(env)[-1][-1] == "60"
    env.mgr.offline_state(_host(env))
    assert len(_rearm_calls(env)) == n + 1             # nothing more to write


def test_n3_a_failing_boot_timer_update_is_tried_again_not_every_check(env):
    _launched_with(env, 1440)
    _start(env)
    _patch(env, {"max_lifetime_minutes": 60})
    env.transports.get("aws")._run_rules.insert(0, ec2_rule(3))
    n = len(_rearm_calls(env))
    env.mgr.offline_state(_host(env))
    env.mgr.offline_state(_host(env))
    assert len(_rearm_calls(env)) == n + 1             # at most once a minute
    assert _host(env).ec2.unit_lifetime_minutes == 1440


def test_n3_terminate_forgets_the_recorded_boot_lifetime(env):
    _launched_with(env, 1440)
    _start(env)
    env.mgr.terminate_machine(_host(env))
    assert _host(env).ec2.unit_lifetime_minutes is None
    assert ec2_mod.boot_timer_note(_host(env).ec2) == ""


# --------------------------------------------------------------------------- a start claimed by one process

import threading  # noqa: E402


def _second_process(env):
    """Another Hermes process: its own manager over the same database and the same AWS."""
    return ec2_mod.Ec2Manager(env.services, http=env.fake, keygen=_keygen, sleep=lambda s: None,
                              clock=lambda: env.clock.now, background=False, poll_s=0)


def test_n4_the_launch_token_is_stored_before_any_aws_call(env):
    seen = []
    orig = env.fake.__call__

    def http(method, url, headers, body, timeout):
        if not seen:
            seen.append(_host(env).ec2.launch_token)      # the first AWS call of the attempt
        return orig(method, url, headers, body, timeout)
    env.mgr._http = http
    _start(env)
    assert seen and seen[0], "no token was stored when the first AWS call was made"


def test_n4_a_second_process_starting_at_the_same_moment_launches_nothing(env):
    m2 = _second_process(env)
    errors = []
    orig = env.fake.__call__

    def http(method, url, headers, body, timeout):
        if "ssm." in url and not errors:             # first process is inside _image, token stored
            try:
                m2.start_machine(_host(env))
            except ApiError as exc:
                errors.append(exc.message)
        return orig(method, url, headers, body, timeout)
    env.mgr._http = http
    env.mgr.start_machine(_host(env), wait=True)
    live = [i for i in env.fake.instances.values() if i["state"] != "terminated"]
    assert len(live) == 1
    assert errors and "already being started by another Hermes process" in errors[0]
    assert _host(env).ec2.instance_id == live[0]["id"]


def test_n4_the_claim_is_released_when_the_launch_is_recorded_or_fails(env):
    env.mgr.start_machine(_host(env), wait=True)
    m2 = _second_process(env)
    assert not m2.launch_in_flight("aws")
    env.fake.errors["RunInstances"] = (400, "InsufficientInstanceCapacity", "none")
    env.mgr.terminate_machine(_host(env))
    with pytest.raises(ApiError):
        env.mgr.start_machine(_host(env))
    assert not m2.launch_in_flight("aws")                # not held after a failed start


def test_n4_a_claim_of_a_dead_process_expires(env):
    env.store.acquire_lease("ec2-start:aws", "ec2-dead-process", 180.0)
    with pytest.raises(ApiError) as info:
        env.mgr.start_machine(_host(env))
    assert "already being started" in info.value.message
    assert env.mgr.launch_in_flight("aws")
    env.store.release_lease("ec2-start:aws", "ec2-dead-process")
    env.mgr.start_machine(_host(env), wait=True)
    assert _host(env).ec2.machine_state == "ready"


def test_n4_an_expired_claim_does_not_block_a_start(env):
    env.store.acquire_lease("ec2-start:aws", "ec2-dead-process", 0.0)    # already expired
    env.mgr.start_machine(_host(env), wait=True)
    assert _host(env).ec2.machine_state == "ready"


# --------------------------------------------------------------------------- secret names from chat


@pytest.mark.parametrize("param", ["access_key_secret", "secret_key_secret"])
@pytest.mark.parametrize("name", ["HF_TOKEN", "CC_AGENT_API_KEY", "GITHUB_TOKEN", "aws_lower"])
def test_chat_may_not_name_a_secret_that_is_not_an_aws_one(env, param, name):
    env.secrets.set(name, "some-other-service-secret")
    for args in ({"action": "add", "kind": "ec2", "name": "Cloud box", param: name},
                 {"action": "update", "host_id": "aws", param: name}):
        got = gate(env, **args)
        assert got["action"] == "block"
        assert "can only be AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY or a name that starts with AWS_" \
            in got["message"]
        assert "Compute page only" in got["message"]
    out = hosts_api.compute_host({"action": "add", "kind": "ec2", "name": "Cloud box",
                                  param: name}, env.services, {})
    assert out.get("error") and not out.get("saved")


def test_chat_does_not_say_whether_a_non_aws_secret_is_set(env):
    env.secrets.set("GITHUB_TOKEN", "x" * 20)
    a = gate(env, action="add", kind="ec2", access_key_secret="GITHUB_TOKEN")["message"]
    b = gate(env, action="add", kind="ec2", access_key_secret="NOPE_NOT_SET")["message"]
    assert "not set" not in a and "not set" not in b          # no oracle for other secrets


def test_chat_may_name_the_defaults_and_aws_prefixed_names_and_the_page_stays_free(env):
    env.secrets.set("AWS_PROJECT_AK", "AKIDPROJECTFAKE000000")      # ws-scan:allow (fake)
    env.secrets.set("AWS_PROJECT_SK", "project/fake/secret/value/for/tests/only")   # ws-scan:allow
    got = gate(env, action="add", kind="ec2", name="Project box",
               access_key_secret="AWS_PROJECT_AK", secret_key_secret="AWS_PROJECT_SK")
    assert got["action"] == "approve"
    got = gate(env, action="add", kind="ec2", name="Plain box",
               access_key_secret="AWS_ACCESS_KEY_ID", secret_key_secret="AWS_SECRET_ACCESS_KEY")
    assert got["action"] == "approve"
    env.secrets.set("MY_OWN_AK", "AKIDMYOWNFAKE0000000")            # ws-scan:allow (fake)
    _patch(env, {"access_key_secret": "MY_OWN_AK"})                 # the page has no such limit
    assert _host(env).ec2.access_key_secret == "MY_OWN_AK"


# --------------------------------------------------------------------------- add: always asks


def test_remote_only_mode_still_asks_for_an_ec2_add_even_with_a_stray_host_id(env):
    env.store.upsert_host(HostSpec(id="local", name="This computer", kind="local",
                                   address="localhost"))
    env.services.config.approval = "remote-only"
    args = {"action": "add", "kind": "ec2", "name": "Sneaky", "auto_start": True,
            "ssh_cidr": "1.2.0.0/16", "max_lifetime_minutes": 1440}
    assert gate(env, **args)["action"] == "approve"              # no host_id: asks
    got = gate(env, host_id="local", **args)                     # the bypass of the review
    assert got is not None
    assert got["action"] == "block" and "host_id does not apply when adding a host" in got["message"]
    before = len(env.fake.calls)
    out = hosts_api.compute_host({**args, "host_id": "local"}, env.services, {})
    assert "host_id does not apply" in out["error"]["message"]
    assert env.store.get_host("sneaky") is None and len(env.fake.calls) == before


def test_remote_only_mode_keeps_its_rule_for_other_host_kinds(env):
    env.store.upsert_host(HostSpec(id="local", name="This computer", kind="local",
                                   address="localhost"))
    env.services.config.approval = "remote-only"
    assert gate(env, action="update", host_id="local", name="This laptop") is None


# --------------------------------------------------------------------------- remove with a stale token


def _stale_token(env):
    h = _host(env)
    h.ec2.launch_token = "unused-token"
    env.store.upsert_host(h)


def test_remove_from_chat_works_when_a_launch_token_was_never_used_by_aws(env):
    _stale_token(env)
    got = gate(env, action="remove", host_id="aws")
    assert got["action"] == "approve" and "never reached a machine" in got["message"]
    out = hosts_api.compute_host({"action": "remove", "host_id": "aws"}, env.services, {})
    assert out.get("removed") is True
    assert env.store.get_host("aws") is None


def test_remove_from_chat_refuses_while_a_launch_may_be_in_flight(env):
    _stale_token(env)
    env.store.acquire_lease("ec2-start:aws", "ec2-other-process", 180.0)
    got = gate(env, action="remove", host_id="aws")
    assert got["action"] == "block" and "start of AWS box is in progress" in got["message"]
    env.store.release_lease("ec2-start:aws", "ec2-other-process")
    assert gate(env, action="remove", host_id="aws")["action"] == "approve"


def test_remove_is_refused_when_the_token_did_launch_a_machine(env):
    _stale_token(env)
    env.fake.add_instance({"cloud-compute:install": env.mgr.install_id()}, state="running")
    iid = next(iter(env.fake.instances))
    env.fake.instances[iid]["token"] = "unused-token"
    out = hosts_api.compute_host({"action": "remove", "host_id": "aws"}, env.services, {})
    assert "Terminate the machine first" in out["error"]["message"]
    assert env.store.get_host("aws") is not None


def test_terminate_card_is_offered_for_an_unconfirmed_launch(env):
    _stale_token(env)
    got = gate(env, action="terminate", host_id="aws")
    assert got["action"] == "approve" and "Looks up the unfinished launch" in got["message"]


# --------------------------------------------------------------------------- Help topic text


def test_the_ec2_help_topic_states_what_the_boot_timer_does_and_does_not_guarantee():
    from cloud_compute_core import docs_content
    body = docs_content.get_topic("ec2")["body_md"]
    assert "Nothing stops a machine this computer cannot reach" not in body     # contradicted it
    assert "max lifetime last written to the machine" in body
    assert "only that timer stops the machine" in body and "longer than the setting" in body
    assert "Start or Extend card differs every time" in body
    assert "only secret names that start with `AWS_`" in body
    assert "another account's keys" in body and "claim that lapses after 3 minutes" in body
    assert len(body.split()) <= 520
