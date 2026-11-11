"""Regression tests for the EC2 host type: launch-only settings, the watcher for starts and
stops, the launch token and untracked instances, the Extend ceiling, and the IAM policy and Help
wording. They use the in-process fake AWS of ``test_ec2``.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_ec2 import T0, _host, _start, env  # noqa: E402,F401  (env is a fixture)

from cloud_compute_core import approval, hosts_api  # noqa: E402
from cloud_compute_core.interfaces import ApiError, ApiRequest  # noqa: E402


def _tool():
    return next(t for t in hosts_api.tools() if t.name == "compute_host")


def _card(env, **args):
    got = approval.approval_for("compute_host", args, _tool(), env.services)
    assert got and got["action"] == "approve", got
    return got["message"], got["rule_key"]


def _patch(env, ec2):
    return hosts_api.patch_host(ApiRequest(path_params={"host_id": "aws"}, query={},
                                           body={"ec2": ec2}, services=env.services))


def _stopped(env):
    _start(env)
    env.mgr.stop_machine(_host(env))
    env.mgr._finish_stop("aws")


# --------------------------------------------------------------------------- launch-only settings


@pytest.mark.parametrize("change", [
    {"instance_type": "c7i.48xlarge"}, {"disk_gb": 500}, {"image": "auto-ubuntu"},
    {"subnet_id": "subnet-0123456789abcdef0"}, {"security_group_id": "sg-0123456789abcdef0"}])
@pytest.mark.parametrize("state", ["stopped", "ready"])
def test_f1_launch_only_settings_are_refused_while_an_instance_exists(env, change, state):
    if state == "stopped":
        _stopped(env)
    else:
        _start(env)
    before = _host(env).ec2.to_dict()
    with pytest.raises(ApiError) as info:
        _patch(env, change)
    assert "apply when a machine is created" in info.value.message
    assert "Terminate the machine first, then change them." in info.value.message
    assert _host(env).ec2.to_dict() == before


def test_f1_settings_that_apply_at_every_start_can_still_change(env):
    _stopped(env)
    _patch(env, {"max_lifetime_minutes": 240, "idle_stop_minutes": 10, "auto_start": True})
    assert _host(env).ec2.max_lifetime_minutes == 240


def test_f1_launch_only_settings_change_after_terminate(env):
    _stopped(env)
    env.mgr.terminate_machine(_host(env))
    _patch(env, {"instance_type": "t3.small", "disk_gb": 20})
    assert _host(env).ec2.instance_type == "t3.small"


def test_f1_start_card_names_the_machine_that_starts_not_the_edited_settings(env):
    _stopped(env)
    host = _host(env)
    real = env.fake.instances[host.ec2.instance_id]["type"]
    # An older record (or any path that bypasses the refusal) must still not mislead.
    s = host.ec2
    s.instance_type, s.disk_gb = "c7i.48xlarge", 500
    env.store.upsert_host(host)
    text, _key = _card(env, action="start", host_id="aws")
    assert real in text and "10 GB" in text
    assert "c7i.48xlarge" not in text and "500 GB" not in text
    assert "Starts the stopped EC2 machine" in text and host.ec2.instance_id in text
    env.mgr.start_machine(_host(env), wait=True)
    assert env.fake.instances[host.ec2.instance_id]["type"] == real


def test_f1_start_card_says_a_new_machine_is_created_when_none_exists(env):
    text, _key = _card(env, action="start", host_id="aws")
    assert "Launches a new EC2 machine" in text and "Starts the stopped" not in text
    _start(env)
    env.mgr.terminate_machine(_host(env))
    text, _key = _card(env, action="start", host_id="aws")
    assert "Launches a new EC2 machine" in text and "Starts the stopped" not in text


def test_f1_host_card_reports_the_launched_type_and_disk(env):
    _stopped(env)
    host = _host(env)
    host.ec2.instance_type, host.ec2.disk_gb = "c7i.48xlarge", 500
    machine = env.mgr.machine(host)
    assert machine["instance_type"] == "t3.micro" and machine["disk_gb"] == 10


# --------------------------------------------------------------------------- the watcher for starts and stops

from cloud_compute_core import ec2 as ec2_mod  # noqa: E402
from cloud_compute_core import ec2_api  # noqa: E402
from test_ec2 import _err, _keygen  # noqa: E402


def _restart(env):
    """A new manager over the same store and AWS: Hermes quit and started again."""
    mgr = ec2_mod.Ec2Manager(env.services, http=env.fake, keygen=_keygen, sleep=lambda s: None,
                             clock=lambda: env.clock.now, background=False, poll_s=0)
    env.services.set("ec2", mgr)
    env.mgr = mgr
    return mgr


def _dead_start(env):
    """The machine was stopped; Start was pressed; Hermes quit before the wait finished."""
    _stopped(env)
    env.mgr.background = True
    env.mgr.run_in_background = lambda fn, name: None        # the worker thread never runs
    env.mgr.start_machine(_host(env))
    assert _host(env).ec2.machine_state == "starting"
    return _restart(env)


def _count(env, action):
    return env.fake.actions().count(action)


def _rearms(env):
    return [a for a in env.transports.get("aws").argvs() if "systemd-run" in " ".join(a)]


def test_f2_a_start_without_a_worker_past_its_time_limit_is_stopped_and_says_so(env):
    mgr = _dead_start(env)
    stops = _count(env, "StopInstances")
    env.clock.now = T0 + timedelta(hours=30)
    mgr.tick(_host(env))
    h = _host(env)
    assert _count(env, "StopInstances") == stops + 1
    assert h.ec2.machine_state == "error"
    assert "time limit has passed" in h.ec2.machine_detail and "was stopped" in h.ec2.machine_detail
    mgr.tick(_host(env))                       # AWS now shows it stopped: the card follows
    assert _host(env).ec2.machine_state == "stopped"


def test_f2_a_start_without_a_worker_is_resumed_and_armed(env):
    mgr = _dead_start(env)
    before = len(_rearms(env))
    env.clock.now = T0 + timedelta(minutes=1)
    mgr.tick(_host(env))
    h = _host(env)
    assert h.ec2.machine_state == "ready", h.ec2.machine_detail
    assert len(_rearms(env)) > before                      # the on-instance timer is set again


def test_f2_the_background_watcher_picks_up_a_dead_start(env):
    assert any(t.name == "ec2.watch" for t in ec2_api.background_tasks())
    _dead_start(env)
    env.clock.now = T0 + timedelta(minutes=1)
    ec2_api.run_watch(env.services)
    assert _host(env).ec2.machine_state == "ready"


def test_f2_a_live_worker_is_not_started_twice(env):
    mgr = _dead_start(env)
    assert mgr._claim("aws") is True                       # a worker of this process
    stops = _count(env, "StopInstances")
    mgr.tick(_host(env))
    mgr.watch(_host(env))
    assert _host(env).ec2.machine_state == "starting" and _count(env, "StopInstances") == stops
    assert mgr._claim("aws") is False


def test_f2_an_errored_machine_that_still_runs_is_stopped_again(env):
    _start(env)
    env.mgr._set("aws", machine_state="error", machine_detail="The machine did not answer.")
    stops = _count(env, "StopInstances")
    env.fake.errors["StopInstances"] = (400, "IncorrectInstanceState", "busy")
    env.mgr.tick(_host(env))
    detail = _host(env).ec2.machine_detail
    assert "may still be running and billed" in detail and "Stop machine" in detail
    env.fake.errors.pop("StopInstances")
    env.mgr.tick(_host(env))                               # the next tick tries again
    assert _count(env, "StopInstances") > stops
    assert "stopped again" in _host(env).ec2.machine_detail


def test_f2_a_failed_automatic_stop_is_retried_and_warned(env):
    _start(env)
    env.clock.now = T0 + timedelta(hours=5)                # past the time limit
    env.fake.errors["StopInstances"] = (400, "IncorrectInstanceState", "busy")
    env.mgr.tick(_host(env))
    h = _host(env)
    assert h.ec2.machine_state == "ready" and "may still be running and billed" in h.ec2.machine_detail
    assert any(p["level"] == "warn" for p in env.events.payloads("notify"))
    env.fake.errors.pop("StopInstances")
    env.mgr.tick(_host(env))
    assert _host(env).ec2.machine_state in ("stopping", "stopped")


def test_f2_stop_is_possible_while_starting_and_in_error(env):
    mgr = _dead_start(env)
    stops = _count(env, "StopInstances")
    mgr.stop_machine(_host(env))
    assert _count(env, "StopInstances") == stops + 1
    assert _host(env).ec2.machine_state in ("stopping", "stopped")
    mgr._set("aws", machine_state="error")
    stops = _count(env, "StopInstances")
    mgr.stop_machine(_host(env))
    assert _count(env, "StopInstances") == stops + 1


def test_f2_a_worker_never_overwrites_a_terminated_machine(env):
    mgr = _dead_start(env)
    mgr.terminate_machine(_host(env))
    mgr._finish_start("aws")                               # the late worker
    h = _host(env)
    assert h.ec2.machine_state == "none" and h.ec2.instance_id is None


def test_f2_the_timer_is_set_as_soon_as_ssh_works_not_after_the_status_checks(env):
    seen = []
    real = env.fake.do_DescribeInstanceStatus

    def slow(p):
        seen.append(len(_rearms(env)))
        if len(seen) < 6:
            return 200, {"instanceStatusSet": [{"instanceId": p["InstanceId.1"],
                                                "systemStatus": {"status": "ok"},
                                                "instanceStatus": {"status": "initializing"}}]}
        return real(p)
    env.fake.do_DescribeInstanceStatus = slow
    _start(env)
    assert len(seen) >= 6 and seen[1] >= 1, seen            # armed during the status wait


def test_f2_first_boot_script_sets_the_limit_again_on_every_later_boot():
    text = ec2_mod.user_data(90, "ec2-user")
    assert "cloud-compute-limit.service" in text and "systemctl enable cloud-compute-limit" in text
    assert text.count("--on-active=90m") == 2
    assert text.index("cloud-compute-limit.service") < text.index("dnf install")


# --------------------------------------------------------------------------- the launch token and untracked instances

import threading  # noqa: E402

from cloud_compute_core import aws as aws_mod  # noqa: E402


def _live(env):
    return [i for i in env.fake.instances.values() if i["state"] != "terminated"]


def _runs(env):
    return [p for _s, a, p in env.fake.calls if a == "RunInstances" and p.get("DryRun") != "true"]


def test_f3_a_server_error_after_the_launch_does_not_launch_twice(env):
    fake = env.fake
    orig = fake.do_RunInstances
    n = {"c": 0}

    def flaky(p):
        status, content = orig(p)
        n["c"] += 1
        if n["c"] == 1 and p.get("DryRun") != "true":
            return 500, b"<Response><Errors><Error><Code>InternalError</Code></Error></Errors></Response>"
        return status, content
    fake.do_RunInstances = flaky
    _start(env)
    assert len(_live(env)) == 1
    runs = _runs(env)
    assert len(runs) == 2 and runs[0]["ClientToken"] == runs[1]["ClientToken"]
    assert _host(env).ec2.launch_token is None                 # cleared once recorded


def test_f3_a_launch_is_never_retried_without_a_client_token():
    calls = []

    def http(method, url, headers, body, timeout):
        calls.append(body)
        return 500, b"<Response><Errors><Error><Code>InternalError</Code></Error></Errors></Response>"
    client = aws_mod.AwsClient("us-east-1", lambda: aws_mod.Credentials("AK", "SK"), http=http,
                               sleep=lambda s: None)
    with pytest.raises(aws_mod.AwsError):
        client.ec2("RunInstances", {"ImageId": "ami-0123456789abcdef0"})
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(aws_mod.AwsError):
        client.ec2("RunInstances", {"ImageId": "ami-0123456789abcdef0", "ClientToken": "t1"})
    assert len(calls) == 3 and len(set(calls)) == 1            # retried, same body, same token


def test_f3_a_lost_answer_is_found_again_by_its_token(env):
    orig = env.fake.__call__
    state = {"armed": True}

    def http(method, url, headers, body, timeout):
        status, data = orig(method, url, headers, body, timeout)
        if state["armed"] and body and b"Action=RunInstances" in body and b"DryRun" not in body:
            state["armed"] = False
            raise TimeoutError("read timed out")
        return status, data
    env.mgr._http = http
    with pytest.raises(ApiError):
        env.mgr.start_machine(_host(env), wait=True)
    h = _host(env)
    assert h.ec2.instance_id is None and h.ec2.launch_token       # stored before the call, kept
    env.mgr.start_machine(_host(env), wait=True)
    assert len(_live(env)) == 1 and len(_runs(env)) == 1          # found, not launched again
    h = _host(env)
    assert h.ec2.instance_id in env.fake.instances and h.ec2.launch_token is None
    assert h.ec2.machine_state == "ready"


def test_f3_the_token_is_stored_before_the_launch_call(env):
    seen = []
    orig = env.fake.__call__

    def http(method, url, headers, body, timeout):
        if body and b"Action=RunInstances" in body and b"DryRun" not in body:
            seen.append(_host(env).ec2.launch_token)
        return orig(method, url, headers, body, timeout)
    env.mgr._http = http
    _start(env)
    assert seen and seen[0] and _runs(env)[0]["ClientToken"] == seen[0]


def test_f3_a_refused_launch_clears_the_token_but_a_doubtful_one_keeps_it(env):
    env.fake.errors["RunInstances"] = (400, "InsufficientInstanceCapacity", "no capacity")
    with pytest.raises(ApiError):
        env.mgr.start_machine(_host(env), wait=True)
    assert _host(env).ec2.launch_token is None
    env.fake.errors["RunInstances"] = (503, "ServiceUnavailable", "later")
    with pytest.raises(ApiError):
        env.mgr.start_machine(_host(env), wait=True)
    assert _host(env).ec2.launch_token                            # in doubt: kept


def test_f3_terminate_and_removal_find_an_unfinished_launch(env):
    from cloud_compute_core import hosts_api as hapi
    orig = env.fake.__call__
    state = {"armed": True}

    def http(method, url, headers, body, timeout):
        status, data = orig(method, url, headers, body, timeout)
        if state["armed"] and body and b"Action=RunInstances" in body and b"DryRun" not in body:
            state["armed"] = False
            raise TimeoutError("read timed out")
        return status, data
    env.mgr._http = http
    with pytest.raises(ApiError):
        env.mgr.start_machine(_host(env), wait=True)
    with pytest.raises(ApiError) as info:                         # the host has a machine
        hapi.delete_host(ApiRequest(path_params={"host_id": "aws"}, query={}, body=None,
                                    services=env.services))
    assert "Terminate the machine first" in info.value.message
    env.mgr.terminate_machine(_host(env))
    assert not _live(env) and _host(env).ec2.launch_token is None


def test_f3_two_parallel_starts_launch_once(env):
    fake = env.fake
    orig = fake.do_RunInstances
    inside = threading.Event()

    def slow(p):
        if p.get("DryRun") != "true":
            inside.set()
            threading.Event().wait(0.3)
        return orig(p)
    fake.do_RunInstances = slow
    env.mgr.background = True
    env.mgr.run_in_background = lambda fn, name: None
    out, errs = [], []

    def go():
        try:
            out.append(env.mgr.start_machine(_host(env)))
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))
    first = threading.Thread(target=go)
    first.start()
    inside.wait(5)
    second = threading.Thread(target=go)
    second.start()
    first.join(10)
    second.join(10)
    assert not errs, errs
    assert len(_live(env)) == 1 and len(_runs(env)) == 1
    assert _host(env).ec2.instance_id in env.fake.instances


def test_f3_a_second_waiting_caller_gets_the_first_start(env):
    fake = env.fake
    orig = fake.do_RunInstances
    inside = threading.Event()

    def slow(p):
        if p.get("DryRun") != "true":
            inside.set()
            threading.Event().wait(0.3)
        return orig(p)
    fake.do_RunInstances = slow
    got = []
    first = threading.Thread(target=lambda: env.mgr.start_machine(_host(env), wait=True))
    first.start()
    inside.wait(5)
    got.append(env.mgr.start_machine(_host(env), wait=True))   # waits for the first
    first.join(10)
    assert got[0].ec2.machine_state == "ready" and len(_live(env)) == 1 and len(_runs(env)) == 1


def test_f3_instances_found_on_aws_are_loaded_by_the_card_and_in_the_status(env):
    from cloud_compute_core import hosts_api as hapi
    stray = env.fake.add_instance({"cloud-compute:install": env.mgr.install_id()})
    out = ec2_api.get_machine(ApiRequest(path_params={"host_id": "aws"}, query={}, body=None,
                                         services=env.services))
    assert [i["instance_id"] for i in out["untracked"]["untracked"]] == [stray]
    assert any(p["level"] == "warn" and stray in p["body"] for p in env.events.payloads("notify"))
    status = hapi.compute_status({}, env.services, {})
    assert stray in status["warning"] and "bills the account" in status["warning"]
    assert status["hosts"][0]["found_on_aws_not_tracked"] == [stray]


# --------------------------------------------------------------------------- the Extend ceiling


def _extend_card(env, minutes):
    return approval.approval_for("compute_host", {"action": "extend", "host_id": "aws",
                                                  "minutes": minutes}, _tool(), env.services)


def test_f4_every_extend_card_names_the_old_and_new_stop_time_and_a_new_key(env):
    _start(env)
    first = _extend_card(env, 30)
    assert first["action"] == "approve"
    old = _host(env).ec2.auto_stop_at
    assert "It now stops itself at" in first["message"] and "after this it stops itself at" in first["message"]
    assert "keeps being billed until then" in first["message"]
    env.mgr.extend_machine(_host(env), 30)
    second = _extend_card(env, 30)
    assert _host(env).ec2.auto_stop_at != old
    assert first["rule_key"] != second["rule_key"]           # a session answer never covers a repeat
    assert first["message"] != second["message"]


def test_f4_the_total_is_capped_at_twice_the_max_lifetime_from_the_last_start(env):
    _start(env)                                              # 120 min limit: latest = start + 240
    env.mgr.extend_machine(_host(env), 120)                  # exactly the cap
    assert _host(env).ec2.auto_stop_at == "2026-11-10T16:00:00Z"
    with pytest.raises(ApiError) as info:
        env.mgr.extend_machine(_host(env), 1)
    assert info.value.code == "limit_reached" and "twice its max lifetime" in info.value.message
    assert "Compute page" in info.value.message
    blocked = _extend_card(env, 1)
    assert blocked == {"action": "block", "message": info.value.message}
    out = hosts_api.compute_host({"action": "extend", "host_id": "aws", "minutes": 1},
                                 env.services, {})
    assert out["error"]["code"] == "limit_reached"
    assert _host(env).ec2.auto_stop_at == "2026-11-10T16:00:00Z"


def test_f4_one_call_cannot_go_past_the_cap_and_a_longer_lifetime_moves_it(env):
    _start(env)
    with pytest.raises(ApiError):
        env.mgr.extend_machine(_host(env), 1440)
    _patch(env, {"max_lifetime_minutes": 480})                # the page changed the setting
    env.mgr.extend_machine(_host(env), 600)                   # start + 960 min is allowed now
    assert _host(env).ec2.auto_stop_at == "2026-11-11T00:00:00Z"


def test_f4_a_restart_counts_from_the_new_start(env):
    _start(env)
    env.mgr.extend_machine(_host(env), 120)
    env.mgr.stop_machine(_host(env))
    env.clock.now = T0 + timedelta(hours=10)
    env.mgr.start_machine(_host(env), wait=True)
    env.mgr.extend_machine(_host(env), 120)                   # allowed again: new last start
    assert _host(env).ec2.auto_stop_at == "2026-11-11T02:00:00Z"


# --------------------------------------------------------------------------- the IAM policy and the Help wording

import json  # noqa: E402
import re  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_f5_the_iam_policy_the_help_text_points_at_ships_in_the_repository():
    from cloud_compute_core import docs_content
    body = docs_content.get_topic("ec2")["body_md"]
    assert "docs/ec2-iam-policy.json" in body
    path = ROOT / "docs" / "ec2-iam-policy.json"
    policy = json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    assert policy["Statement"] and not re.search(r"(?<![\w-])\d{12}(?![\w-])", text), \
        "no account number in the policy"
    assert "ec2:RunInstances" in text and "cloud-compute:install" in text
    assert (ROOT / "docs" / "ec2-iam-policy.md").is_file()


def test_f5_the_help_text_claims_only_what_the_code_does():
    from cloud_compute_core import docs_content
    body = docs_content.get_topic("ec2")["body_md"]
    assert "even if this computer is off" not in body
    assert "first SSH login trusts the key it sees" in body             # the fallback
    assert "Nothing stops a machine this computer cannot reach" not in body   # it contradicted the timer
    assert "While Hermes is closed only that timer stops the machine" in body   # what is true
    assert "listed on the host card when it shows" in body              # true: the card lists them
    assert "twice the max lifetime" in body                             # true: the ceiling
    js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "even if this computer is off" not in js
    assert "exact policy" not in (ROOT / "core" / "aws.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- minors


def test_m1_a_redirect_is_never_followed_with_the_signature():
    import urllib.request
    handlers = [h for h in aws_mod._OPENER.handlers if isinstance(h, aws_mod._NoRedirect)]
    assert handlers, "the opener must refuse redirects"
    req = urllib.request.Request("https://ec2.us-east-1.amazonaws.com/", data=b"x", method="POST",
                                 headers={"Authorization": "AWS4-HMAC-SHA256 SIG",
                                          "X-Amz-Security-Token": "TOK"})
    for code in (301, 302, 303, 307, 308):
        assert handlers[0].redirect_request(req, None, code, "x", {}, "https://evil.example/") \
            is None


@pytest.mark.parametrize("body", [b"999.1.1.1\n", b"1.2.3\n", b"<html>1.2.3.4</html>", b"",
                                  b"10.0.0.5\n", b"127.0.0.1\n", b"169.254.1.1\n", b"0.0.0.0\n",
                                  b"224.0.0.1\n", b"1.2.3.4\n5.6.7.8\n", b"::1\n"])
def test_m2_the_public_address_must_be_one_routable_ipv4_address(body):
    with pytest.raises(aws_mod.AwsError):
        aws_mod.lookup_public_ip(lambda *a: (200, body))


def test_m2_a_good_address_is_accepted():
    assert aws_mod.lookup_public_ip(lambda *a: (200, b"203.0.113.9\n")) == "203.0.113.9"
    assert aws_mod.lookup_public_ip(lambda *a: (200, b"8.8.4.4")) == "8.8.4.4"


def test_m4_the_start_card_says_who_may_use_ssh(env):
    env.mgr._set("aws", ssh_cidr="203.0.113.0/24")
    text, _key = _card(env, action="start", host_id="aws")
    assert "SSH allowed from: SSH OPEN TO THIS RANGE: 203.0.113.0/24 (256 addresses)" in text


def test_m6_no_leftover_of_the_first_design_in_the_ec2_module():
    src = (ROOT / "core" / "ec2.py").read_text(encoding="utf-8")
    assert "DirectRuntime" not in src and "instance_host" not in src
    assert "def secret_values" not in src


def test_m7_a_store_error_in_the_reaper_check_does_not_abort_the_pass():
    from cloud_compute_core.reaper import Reaper

    class Broken:
        def get_host(self, _id):
            raise RuntimeError("store down")
    assert Reaper._machine_up(Broken(), "aws") is True


def test_m8_the_ec2_worker_threads_are_tracked_for_shutdown(env):
    from cloud_compute_core import background
    seen = []
    real = background.track_worker
    ec2_mod.track_worker = lambda thread: (seen.append(thread.name), real(thread))[1]
    try:
        mgr = ec2_mod.Ec2Manager(env.services, http=env.fake, keygen=_keygen,
                                 sleep=lambda s: None, clock=lambda: env.clock.now,
                                 background=True, poll_s=0)
        mgr.start_machine(_host(env))
        background.join_workers(10)
    finally:
        ec2_mod.track_worker = real
    assert "cc-ec2-aws" in seen
