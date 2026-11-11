"""Two page problems found by the last live test: Extend offered when it cannot succeed, and an
EC2 host without a running machine shown as offline in the summary and the chat line."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_ec2 import T0, _host, _start, env  # noqa: E402,F401  (env is a fixture)

from cloud_compute_core import chat_context, hosts_api, summary_api  # noqa: E402
from cloud_compute_core.interfaces import ApiRequest  # noqa: E402


def _patch(env, ec2):
    return hosts_api.patch_host(ApiRequest(path_params={"host_id": "aws"}, query={},
                                           body={"ec2": ec2}, services=env.services))


def _page_ec2(env):
    out = hosts_api.get_host(ApiRequest(path_params={"host_id": "aws"}, query={}, body=None,
                                        services=env.services))
    return out["spec"]["ec2"]


def _listed_ec2(env):
    out = hosts_api.list_hosts(ApiRequest(path_params={}, query={}, body=None,
                                          services=env.services))
    return next(h for h in out if h["spec"]["id"] == "aws")["spec"]["ec2"]


# --------------------------------------------------------------------------- Extend

def test_extend_offer_is_the_largest_step_that_fits_the_ceiling(env):
    _start(env)                                   # 120 min limit: room = 120
    e = _page_ec2(env)
    assert (e["extend_room_minutes"], e["extend_step_minutes"]) == (120, 60)
    assert _listed_ec2(env)["extend_step_minutes"] == 60
    env.mgr.extend_machine(_host(env), 90)        # room = 30
    e = _page_ec2(env)
    assert (e["extend_room_minutes"], e["extend_step_minutes"]) == (30, 30)


def test_extend_offer_at_a_15_minute_lifetime_is_15_minutes(env):
    _patch(env, {"max_lifetime_minutes": 15})
    _start(env)                                   # stops at start + 15, ceiling start + 30
    e = _page_ec2(env)
    assert (e["extend_room_minutes"], e["extend_step_minutes"]) == (15, 15)


def test_extend_offer_is_none_when_nothing_fits_or_there_is_no_running_machine(env):
    e = _page_ec2(env)
    assert (e["extend_room_minutes"], e["extend_step_minutes"]) == (0, 0)     # no machine
    _start(env)
    env.mgr.extend_machine(_host(env), 120)       # exactly the ceiling
    e = _page_ec2(env)
    assert (e["extend_room_minutes"], e["extend_step_minutes"]) == (0, 0)
    _patch(env, {"max_lifetime_minutes": 480})    # a longer lifetime moves the ceiling
    assert _page_ec2(env)["extend_step_minutes"] == 60


def test_extend_offer_below_the_smallest_step_is_not_offered(env):
    _start(env)
    env.mgr.extend_machine(_host(env), 110)       # 10 minutes of room left
    e = _page_ec2(env)
    assert (e["extend_room_minutes"], e["extend_step_minutes"]) == (10, 0)


def test_the_offer_follows_the_backend_rule(env):
    """Whatever step the page is told to offer is accepted by the backend."""
    from cloud_compute_core.ec2 import extend_offer
    _start(env)
    s = _host(env).ec2
    room, step = extend_offer(s, env.clock.now)
    assert step and env.mgr.extend_machine(_host(env), step)
    assert extend_offer(_host(env).ec2, env.clock.now + timedelta(0))[0] == room - step


def test_derived_fields_are_never_stored_from_a_request(env):
    _start(env)
    _patch(env, {"extend_room_minutes": 999, "extend_step_minutes": 60, "max_lifetime_minutes": 240})
    assert _host(env).ec2.max_lifetime_minutes == 240
    assert "extend_room_minutes" not in _host(env).ec2.to_dict()


# --------------------------------------------------------------------------- summary and chat

def _snap(*hosts):
    return {"generated_at": "2026-11-11T10:00:00Z", "hosts": list(hosts),
            "instances": [], "jobs": []}


def _ec2(host_id, machine, status="offline"):
    return {"spec": {"id": host_id, "name": host_id, "kind": "ec2",
                     "ec2": {"machine_state": machine} if machine else {}},
            "state": {"status": status, "last_error": "no machine"}}


@pytest.mark.parametrize("machine", ["none", "stopped", "stopping", "launching", "starting", ""])
def test_an_ec2_host_without_a_running_machine_is_not_offline_in_the_summary(machine):
    out = summary_api.summarize(_snap(_ec2("aws", machine)))
    assert out["hosts"]["offline"] == 0 and out["hosts"]["total"] == 1
    assert out["attention"] == []


def test_a_machine_in_error_or_an_ssh_host_offline_is_still_offline():
    ssh = {"spec": {"id": "lab", "name": "lab", "kind": "ssh"}, "state": {"status": "offline"}}
    out = summary_api.summarize(_snap(_ec2("aws", "error"), ssh))
    assert out["hosts"]["offline"] == 2
    assert [a["message"] for a in out["attention"]] == ["aws is offline", "lab is offline"]


def test_a_ready_machine_that_does_not_answer_is_offline():
    out = summary_api.summarize(_snap(_ec2("aws", "ready")))
    assert out["hosts"]["offline"] == 1


def test_the_chat_line_names_the_neutral_states_not_offline():
    text = chat_context.render_context(_snap(_ec2("aws", "none"), _ec2("box", "stopped")),
                                       max_chars=600)
    assert "Offline: none." in text
    assert "aws (No machine yet)" in text and "box (Machine stopped)" in text
    assert "(offline)" not in text


def test_the_chat_line_still_lists_a_real_offline_host():
    ssh = {"spec": {"id": "lab", "name": "lab", "kind": "ssh"}, "state": {"status": "offline"}}
    text = chat_context.render_context(_snap(_ec2("aws", "stopped"), ssh), max_chars=600)
    assert "Offline: lab (offline)." in text
