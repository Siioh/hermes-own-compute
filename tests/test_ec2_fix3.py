"""Regression tests for the EC2 host type found by a run from the real Compute page: removing
the environments of a machine that is stopped, the placeholder address that must never be
dialled, spawning on an EC2 host from the page, the stop timer that must not block logins, and
the wording of a stopped or missing machine. They use the in-process fake AWS of ``test_ec2``.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_ec2 import FakeAws, T0, _environment, _host, _keygen, _start, env  # noqa: E402,F401

from cloud_compute_core import ec2 as ec2_mod, ec2_api, ec2_chat, hosts_api  # noqa: E402
from cloud_compute_core import interfaces  # noqa: E402
from cloud_compute_core.interfaces import ApiError, ApiRequest  # noqa: E402
from cloud_compute_core.models import Ec2Settings, HostSpec  # noqa: E402
from cloud_compute_core.reaper import Reaper  # noqa: E402
from cloud_compute_core.runtimes import LABEL_INSTALL, LABEL_INSTANCE, MANAGED_LABEL  # noqa: E402
from cloud_compute_core.testing import exec_result  # noqa: E402

NO_MACHINE = ec2_mod.NO_MACHINE


class NeverDial:
    """Transports that fail the test at once when asked for a connection to the placeholder
    address of a machine that is not running (the real factory raises a typed error)."""

    def __init__(self, inner):
        self.inner = inner

    def get(self, host_id):
        return self.inner.get(host_id)

    def for_host(self, host, **kw):
        if getattr(host, "address", "") in ("", NO_MACHINE):
            raise AssertionError(f"dialled the placeholder address of {host.id}")
        return self.inner.for_host(host, **kw)


def _stopped_with_environment(env):
    """A machine that was started and then stopped, holding one environment."""
    _start(env)
    rec = _environment(env, status="stopped")
    env.mgr.stop_machine(_host(env))
    assert _host(env).ec2.machine_state == "stopped"
    assert _host(env).address == NO_MACHINE
    env.services.set("transports", NeverDial(env.transports))
    return rec


# --------------------------------------------------------------------------- the placeholder


def test_the_placeholder_address_is_never_dialled():
    from cloud_compute_core.config import Config
    from cloud_compute_core.transport import TransportFactory, ssh_base_args
    host = HostSpec(id="aws", name="AWS box", kind="ec2", address=NO_MACHINE,
                    ec2=Ec2Settings(region="us-east-1"))
    with pytest.raises(interfaces.MachineNotRunning) as info:
        TransportFactory(Config()).for_host(host)
    assert info.value.message == "The machine of AWS box is not running. Start it first."
    assert info.value.code == "host_offline" and isinstance(info.value, ApiError)
    with pytest.raises(interfaces.MachineNotRunning):
        ssh_base_args(host)


# --------------------------------------------------------------------------- terminate


def test_terminate_without_saving_works_on_a_stopped_machine(env):
    rec = _stopped_with_environment(env)
    out = env.mgr.terminate_machine(_host(env), delete_environments=True, save=False)
    assert "TerminateInstances" in env.fake.actions()
    gone = env.store.get_instance(rec.id)
    assert gone.status == "destroyed"
    assert gone.error == "Deleted with its machine, files not saved."
    assert out["terminated"] and out["environments"][0]["name"] == rec.name
    assert "files were not saved" in out["environments"][0]["message"]
    assert _host(env).ec2.machine_state == "none"


def test_terminate_without_saving_from_the_page_route(env):
    rec = _stopped_with_environment(env)
    out = ec2_api.machine_action(env.services, _host(env), "terminate",
                                 {"delete_environments": True, "save": False})
    assert out["terminated"] is True
    assert env.store.get_instance(rec.id).status == "destroyed"


def test_save_all_on_a_stopped_machine_offers_the_two_choices_and_starts_nothing(env):
    rec = _stopped_with_environment(env)
    with pytest.raises(ApiError) as info:
        env.mgr.terminate_machine(_host(env), delete_environments=True, save=True)
    text = info.value.message
    assert info.value.code == "not_saved"
    assert "Saving needs the machine running" in text and "python-dev-1" in text
    assert "start the machine (AWS bills while it runs)" in text
    assert "Terminate without saving" in text
    assert "StartInstances" not in env.fake.actions()           # nothing is started silently
    assert "TerminateInstances" not in env.fake.actions()
    assert env.store.get_instance(rec.id).status == "stopped"


def test_terminate_card_names_the_environments_and_says_the_files_are_not_saved(env):
    _stopped_with_environment(env)
    card = ec2_chat.describe_machine("terminate", _host(env),
                                     {"delete_environments": True, "save": False}, None,
                                     ["python-dev-1"])
    assert "python-dev-1" in card and "NOT saved" in card and "WITHOUT saving" in card
    blocked = ec2_chat.describe_machine("terminate", _host(env),
                                        {"delete_environments": True}, None, ["python-dev-1"])
    assert type(blocked).__name__ == "BlockedMessage"
    assert "Terminate without saving".lower() in blocked.lower() or "save=false" in blocked


# --------------------------------------------------------------------------- one environment


def test_delete_without_saving_on_a_stopped_machine_closes_the_record(env):
    rec = _stopped_with_environment(env)
    sandboxes = env.services.sandboxes
    out = {}
    sandboxes.destroy(rec.id, save=False, report=out, notify=False)
    gone = env.store.get_instance(rec.id)
    assert gone.status == "destroyed"
    assert gone.error == "Its container is removed when the machine next starts or is terminated."
    assert "without saving its files" in out["message"]
    assert sandboxes.has_leftover(rec.id) is True


def test_delete_with_saving_on_a_stopped_machine_is_refused_plainly(env):
    rec = _stopped_with_environment(env)
    with pytest.raises(ApiError) as info:
        env.services.sandboxes.destroy(rec.id, save=True)
    assert info.value.code == "not_saved"
    assert info.value.message == (
        "The machine of AWS box is stopped, so the files of python-dev-1 cannot be saved. "
        "Saving needs the machine running: start it (AWS bills while it runs) and delete "
        "again, or delete without saving, which loses the files.")
    assert env.store.get_instance(rec.id).status == "stopped"


def test_delete_without_saving_when_the_machine_does_not_answer(env):
    _start(env)
    rec = _environment(env, status="running")
    env.transports.get("aws").script(re.compile(r"podman rm"), exec_result(
        rc=255, stderr="System is going down. Unprivileged users are not permitted to log in "
                       "anymore. Connection closed by 203.0.113.9 port 22"))
    env.services.sandboxes.destroy(rec.id, save=False, notify=False)
    assert env.store.get_instance(rec.id).status == "destroyed"
    assert env.services.sandboxes.has_leftover(rec.id)


def test_a_container_left_behind_is_removed_not_adopted_when_the_machine_returns(env):
    rec = _stopped_with_environment(env)
    env.services.sandboxes.destroy(rec.id, save=False, notify=False)
    env.services.set("transports", env.transports)           # reachable again
    env.mgr.start_machine(_host(env), wait=True)
    assert _host(env).ec2.machine_state == "ready"
    install = env.services.sandboxes.install_id()
    row = {"Id": "abc123", "Names": [f"cc-{rec.id}"], "State": "exited", "Status": "Exited (143)",
           "Labels": {MANAGED_LABEL: "1", LABEL_INSTANCE: rec.id, LABEL_INSTALL: install}}
    t = env.transports.get("aws")
    t.script(re.compile(r"podman ps"), exec_result(rc=0, stdout=json.dumps([row])))
    t.script(re.compile(r"podman rm"), exec_result(rc=0))
    report = Reaper(env.services).sweep_host("aws", datetime.now(timezone.utc))
    assert report.adopted == [] and report.orphans == []
    assert report.cleaned == [f"aws:cc-{rec.id}"]
    assert any(a[:2] == ["podman", "rm"] and f"cc-{rec.id}" in a for a in t.argvs())
    assert env.store.get_instance(rec.id).status == "destroyed"
    assert env.services.sandboxes.has_leftover(rec.id) is False


# --------------------------------------------------------------------------- removing the host


def test_the_host_can_be_removed_after_a_terminate_without_saving(env):
    rec = _stopped_with_environment(env)
    req = ApiRequest(path_params={"host_id": "aws"}, query={}, body=None, services=env.services)
    with pytest.raises(ApiError) as info:                     # a machine still exists
        hosts_api.delete_host(req)
    assert "environment" in info.value.message
    env.mgr.terminate_machine(_host(env), delete_environments=True, save=False)
    assert env.store.get_instance(rec.id).status == "destroyed"
    assert hosts_api.delete_host(req)["deleted"] is True
    assert env.store.get_host("aws") is None


def test_the_host_is_refused_with_a_plain_sentence_while_a_machine_exists(env):
    _start(env)
    env.mgr.stop_machine(_host(env))
    req = ApiRequest(path_params={"host_id": "aws"}, query={}, body=None, services=env.services)
    with pytest.raises(ApiError) as info:
        hosts_api.delete_host(req)
    assert "still has an EC2 machine" in info.value.message
    assert "Terminate the machine first." in info.value.message


# --------------------------------------------------------------------------- spawn from the page


def test_the_spawn_check_the_agent_check_and_the_lists_use_one_rule(env):
    from cloud_compute_core.models import fitting_host_kinds
    from cloud_compute_core.sandbox import _fits
    tpl = env.services.templates.get("python-dev")
    assert fitting_host_kinds(["local"]) == ["local"]
    assert fitting_host_kinds(["ssh"]) == ["ssh", "ec2"]
    assert fitting_host_kinds(["ssh", "ec2"]) == ["ssh", "ec2"]
    assert _fits("ec2", tpl) and not _fits("slurm", tpl)
    host = _host(env)
    # a cloud agent template is checked with the same rule
    agents = env.services.agents
    agent_tpl = env.services.templates.get("hermes-agent")
    try:
        agents._check_template(agent_tpl, {"mode": "hermes", "image_base": None}, host)
    except (ApiError, KeyError) as exc:        # later checks need a model; the kind must pass
        assert "does not run on" not in str(exc)
    listed = hosts_api._template_entries(env.services, False, "ec2")
    assert "python-dev" in [t["id"] for t in listed]
    assert "slurm-python" not in [t["id"] for t in listed]


# --------------------------------------------------------------------------- the stop timer


def _unit_text(minutes=15):
    return ec2_mod.user_data(minutes, "ec2-user")


def test_the_stop_is_a_systemd_timer_and_never_a_scheduled_shutdown():
    text = _unit_text(15)
    assert "shutdown -h" not in text                  # that creates /run/nologin 5 minutes before
    assert text.count("systemd-run --unit=cloud-compute-stop --on-active=15m") == 2
    assert text.count("--timer-property=AccuracySec=1s systemctl poweroff") == 2
    assert text.count("systemctl stop cloud-compute-stop.timer") == 2    # re-arming replaces it
    assert "cloud-compute-limit.service" in text and "WantedBy=multi-user.target" in text
    assert not re.search(r">\s*/etc/", text)           # the security scan rejects this form
    assert "tee /etc/systemd/system/cloud-compute-limit.service" in text


def test_only_a_whole_number_of_minutes_is_put_into_the_script():
    with pytest.raises(ValueError):
        ec2_mod.user_data("15; reboot")
    assert "--on-active=1m" in ec2_mod.user_data(0)    # the smallest lifetime is one minute


@pytest.mark.skipif(__import__("shutil").which("sh") is None, reason="needs a POSIX shell")
def test_the_first_boot_script_is_valid_shell():
    import subprocess
    out = subprocess.run(["sh", "-n"], input=_unit_text(90), text=True, capture_output=True)
    assert out.returncode == 0, out.stderr


def test_the_boot_unit_lifetime_is_the_part_extend_rewrites():
    unit = _unit_text(15)
    pattern = re.compile(r"--on-active=[0-9][0-9]*m")  # the sed expression of the re-arm command
    assert ec2_mod._REARM.count("s/--on-active=[0-9][0-9]*m/--on-active=$2m/") == 1
    exec_line = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
    assert len(exec_line) == 1 and len(pattern.findall(exec_line[0])) == 1
    assert pattern.sub("--on-active=60m", exec_line[0]).count("--on-active=60m") == 1


@pytest.mark.skipif(__import__("shutil").which("sh") is None, reason="needs a POSIX shell")
def test_extend_replaces_the_timer_in_order_and_schedules_no_shutdown(tmp_path):
    """Run the real re-arm command with stand-ins for sudo, systemctl and systemd-run."""
    import os
    import subprocess
    log = tmp_path / "calls.log"
    for name in ("systemctl", "systemd-run", "shutdown"):
        (tmp_path / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log.as_posix()}"\n')
    (tmp_path / "sudo").write_text('#!/bin/sh\n[ "$1" = "-n" ] && shift\nexec "$@"\n')
    env_ = dict(os.environ, PATH=f"{tmp_path.as_posix()}{os.pathsep}{os.environ['PATH']}")
    res = subprocess.run(["sh", "-c", ec2_mod._REARM, "sh", "45", "120"], env=env_,
                         capture_output=True, text=True)
    assert res.returncode == 3, res.stderr             # armed; no unit file on this computer
    calls = log.read_text().splitlines()
    stop = next(i for i, c in enumerate(calls) if c == "systemctl stop cloud-compute-stop.timer")
    run = next(i for i, c in enumerate(calls) if c.startswith("systemd-run "))
    assert stop < run
    assert "--on-active=45m" in calls[run] and calls[run].endswith("systemctl poweroff")
    assert not any(c.startswith("shutdown -h") for c in calls)


def test_a_login_refused_because_the_machine_is_going_down_gets_a_plain_sentence(monkeypatch):
    from cloud_compute_core import transport as tr
    from cloud_compute_core.config import Config
    from cloud_compute_core.models import ExecResult
    raw = ("System is going down. Unprivileged users are not permitted to log in anymore. "
           "For technical details, see pam_nologin(8).\r\nConnection closed by 203.0.113.9 port 22")
    monkeypatch.setattr(tr.SubprocessSSHTransport, "run",
                        lambda self, argv, **kw: ExecResult(rc=255, stdout="", stderr=raw,
                                                            duration_s=0.1))
    host = HostSpec(id="aws", name="AWS box", kind="ec2", address="203.0.113.9",
                    ec2=Ec2Settings(region="us-east-1"))
    res = tr.TransportFactory(Config()).for_host(host).run(["true"])
    assert res.rc == 255
    assert res.stderr == ("The machine is about to stop at its time limit and no longer accepts "
                          "connections. Start it again after it has stopped to reach its files; "
                          "the disk is kept.")
    other = ExecResult(rc=255, stdout="", stderr="Connection timed out", duration_s=0.1)
    assert tr.going_down_text(other.stderr) == "Connection timed out"


# --------------------------------------------------------------------------- calm states


def _mark_online(env):
    """The host was last seen online, so the next offline state is a change."""
    from cloud_compute_core.models import Capabilities, HostState
    env.store.set_host_state(HostState(host_id="aws", status="online", last_seen=None,
                                       last_error=None, latency_ms=None,
                                       capabilities=Capabilities(), resources=None,
                                       probed_at=None))


def test_no_machine_and_a_stopped_machine_are_expected_offline_states(env):
    env.mgr.offline_state(_host(env))                      # no machine yet
    first = env.events.payloads("host.status")[-1]
    assert first["status"] == "offline" and first["expected"] is True
    _start(env)
    env.mgr.stop_machine(_host(env))
    _mark_online(env)
    env.mgr.offline_state(_host(env))
    assert env.events.payloads("host.status")[-1]["expected"] is True


def test_a_machine_in_error_is_not_an_expected_offline_state(env):
    _start(env)
    env.mgr._set("aws", machine_state="error", machine_detail="The machine did not become ready.")
    _mark_online(env)
    env.mgr._offline(_host(env))
    assert env.events.payloads("host.status")[-1]["expected"] is False


# --------------------------------------------------------------------------- confirm before Start / Extend / Stop


def _confirm(env, action, **query):
    req = ApiRequest(path_params={"host_id": "aws", "action": action}, query=query, body=None,
                     services=env.services)
    return ec2_api.machine_confirm(req)


def test_start_is_explained_with_the_chat_card_text(env):
    out = _confirm(env, "start")
    from cloud_compute_core import approval
    chat = approval.approval_for("compute_host", {"action": "start", "host_id": "aws"},
                                 next(t for t in hosts_api.tools() if t.name == "compute_host"),
                                 env.services)
    assert out["blocked"] is False and out["message"] == chat["message"]
    for part in ("Region: us-east-1", "Instance type: t3.micro", "Disk: 10 GB", "Max lifetime",
                 "Idle stop", "SSH allowed from", "Start number 1", "Starts billing now: yes",
                 "AWS bills the account while it runs"):
        assert part in out["message"], part


def test_extend_shows_the_old_and_the_new_stop_time_and_stop_the_disk_charge(env):
    _start(env)
    out = _confirm(env, "extend", minutes="30")
    assert out["blocked"] is False
    assert "It now stops itself at" in out["message"] and "after this it stops itself at" in out["message"]
    stop = _confirm(env, "stop")
    assert "disk is still billed until the machine is terminated" in stop["message"]
    too_far = _confirm(env, "extend", minutes="1440")
    assert too_far["blocked"] is True and "cannot be extended beyond" in too_far["message"]
    with pytest.raises(ApiError):
        _confirm(env, "extend", minutes="soon")
    with pytest.raises(ApiError):
        _confirm(env, "terminate")


def test_stopping_a_stopped_machine_is_a_refusal_not_a_card(env):
    out = _confirm(env, "stop")
    assert out["blocked"] is True and "nothing to stop" in out["message"]


# --------------------------------------------------------------------------- the chat says how to start it


def test_a_stopped_machine_is_started_with_the_plugin_not_in_the_aws_console(env):
    _start(env)
    env.mgr.stop_machine(_host(env))
    status = hosts_api.compute_status({"host_id": "aws"}, env.services, {})
    text = status["hosts"][0]["cost_state"]
    assert 'compute_host(action="start"' in text and "Start machine button on the Hosts tab" in text
    assert "do not start it in the AWS console" in text


def test_the_guide_skill_says_the_same():
    skill = (Path(__file__).parent.parent / "skills" / "guide" / "SKILL.md").read_text(
        encoding="utf-8")
    assert "never in the AWS console or with the AWS command line" in skill


# --------------------------------------------------------------------------- plain wording of a number field


@pytest.mark.parametrize("key,value,sentence", [
    ("max_lifetime_minutes", 10, "Max lifetime (minutes) must be a whole number from 15 to 1440."),
    ("disk_gb", 5, "Root disk (GB) must be a whole number from 8 to 500."),
    ("idle_stop_minutes", "soon", "Stop when idle (minutes) must be a whole number from 0 to 1440."),
])
def test_a_number_field_error_uses_the_label_of_the_field(key, value, sentence):
    with pytest.raises(ApiError) as info:
        ec2_api.validate_ec2({"region": "us-east-1", key: value})
    assert info.value.message == sentence
    assert "ec2." not in info.value.message and "_" not in info.value.message
