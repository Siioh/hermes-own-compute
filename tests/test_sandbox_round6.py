"""Delete shows what was saved, early spawn checks, published ports,
screen templates, ports after a restart, empty-log note, strict tool parameters, workdir."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cloud_compute_core import approval, instances_api, sandbox as sandbox_mod
from cloud_compute_core.interfaces import ApiError, ApiRequest
from cloud_compute_core.models import HostLimits, TemplateSpec
from cloud_compute_core.reaper import Reaper
from cloud_compute_core.sandbox import NO_OUTPUT_NOTE, SandboxManager, delete_message
from cloud_compute_core.templates import DISPLAY_IMAGE_MESSAGE, TemplateStore
from cloud_compute_core.testing import exec_result, sample_host

EXPORT = {"id": "x-0000abcd", "instance_id": "i-1", "local_path": "C:/exports/work-i-1/a.tar.gz",
          "bytes": 3355443, "files": 12, "source_path": "/work"}


class Files:
    """services.files with the ``save_before_delete`` contract."""

    def __init__(self):
        self.calls = []
        self.result = {"saved": True, "export": dict(EXPORT), "files": 12, "bytes": 3355443,
                       "source_path": "/work", "message": "saved"}
        self.fail = None

    def save_before_delete(self, instance_id, *, reason="before-delete"):
        self.calls.append((instance_id, reason))
        if self.fail:
            raise self.fail
        return self.result


class LegacyFiles:
    """services.files without save_before_delete: the verified export path."""

    def __init__(self, count=4):
        self.count = count

    def export_verified(self, instance_id, path=None, *, reason="manual"):
        return dict(EXPORT, files=0), self.count


def inspect_json(ports=None):
    bound = {f"{c}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(h)}]
             for c, h in (ports or {}).items()}
    return json.dumps([{"Id": "f0" * 32, "State": {"Status": "running"},
                        "NetworkSettings": {"Ports": bound}}])


@pytest.fixture
def env(services, fake_transports, fake_secrets, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "PORT_POLL_S", 0.0)
    monkeypatch.setattr(sandbox_mod, "PORT_WAIT_S", 0.05)
    host = sample_host(name="x1carbon", runtime_pref="podman", limits=HostLimits(max_instances=2))
    services.store.upsert_host(host)
    services.set("templates", TemplateStore(services.store))
    services.set("sandboxes", SandboxManager(services))
    services.set("reaper", Reaper(services))
    services.set("files", Files())
    t = fake_transports.get(host.id)
    t.script(lambda a, s: a[:2] == ["sh", "-c"], exec_result(stdout="/h/.cache/x.env\n"))
    t.script(("podman", "run"), exec_result(stdout="3f2a9c1d0b7e\n"))
    t.script(("podman", "inspect"), exec_result(stdout=inspect_json()))
    t.script(("podman", "logs"), exec_result(stdout=""))
    for verb in ("stop", "start", "rm", "exec", "stats", "ps", "port"):
        t.script(("podman", verb), exec_result())
    return SimpleNamespace(services=services, host=host, t=t, store=services.store,
                           mgr=services.sandboxes, files=services.files)


def tool(name):
    return {t.name: t for t in instances_api.tools()}[name]


def run_tool(env, name, args):
    return tool(name).handler(args, env.services, {})


def spawn(env, **kw):
    out = run_tool(env, "env_spawn", dict({"template": "python-dev", "host_id": "x1carbon"}, **kw))
    assert "instance" in out, out
    return out


def rm_calls(env):
    return [a for a in env.t.argvs() if a[:2] == ["podman", "rm"]]


def notices(env):
    return [p["body"] for p in env.services.events.payloads("notify")]


# ---------------------------------------------------------------- 1. delete shows what was saved


def test_destroy_result_names_the_saved_copy_size_files_and_path(env):
    iid = spawn(env)["instance"]["id"]
    out = run_tool(env, "env_control", {"instance_id": iid, "action": "destroy"})
    assert env.files.calls == [(iid, "before-delete")]
    assert out["saved"] == {"export_id": "x-0000abcd", "path": EXPORT["local_path"],
                            "bytes": 3355443, "files": 12}
    name = out["instance"]["name"]
    assert out["message"] == (f"Deleted {name}. The files in /work (12 files, 3.2 MB) were saved "
                              f"to {EXPORT['local_path']}.")
    assert out["card"] == f'::cc-env{{id="{iid}"}}'
    assert out["copy_card"] == '::cc-copy{id="x-0000abcd"}'
    assert out["instance"]["status"] == "destroyed" and len(rm_calls(env)) == 1
    assert out["message"] in notices(env)          # the same sentence goes into the notification


def test_destroy_empty_workdir_says_nothing_needed_saving(env):
    iid = spawn(env)["instance"]["id"]
    env.files.result = {"saved": False, "export": None, "files": 0, "bytes": 0,
                        "source_path": "/work", "message": "empty"}
    out = run_tool(env, "env_control", {"instance_id": iid, "action": "destroy"})
    assert out["saved"] is None and "copy_card" not in out
    assert out["message"] == f"Deleted {out['instance']['name']}. Nothing in /work needed saving."
    assert out["message"] in notices(env)


def test_destroy_without_saving_says_so(env):
    iid = spawn(env)["instance"]["id"]
    out = run_tool(env, "env_control", {"instance_id": iid, "action": "destroy", "save": False})
    assert env.files.calls == []
    assert out["saved"] is None
    assert out["message"] == f"Deleted {out['instance']['name']} without saving its files."


def test_destroy_rest_response_has_saved_and_message(env):
    iid = spawn(env)["instance"]["id"]
    req = ApiRequest(path_params={"instance_id": iid, "action": "destroy"}, query={}, body=None,
                     services=env.services)
    out = instances_api.instance_action(req)
    assert out["status"] == "destroyed" and out["saved"]["export_id"] == "x-0000abcd"
    assert "were saved to C:/exports" in out["message"]


def test_a_save_that_cannot_be_verified_deletes_nothing(env):
    iid = spawn(env)["instance"]["id"]
    env.files.fail = RuntimeError("tar stream ends early")
    out = run_tool(env, "env_control", {"instance_id": iid, "action": "destroy"})
    assert out["error"]["code"] == "not_saved" and rm_calls(env) == []
    assert env.store.get_instance(iid).status == "running"


def test_saved_true_without_a_recorded_copy_is_not_trusted(env):
    iid = spawn(env)["instance"]["id"]
    env.files.result = {"saved": True, "export": None, "files": 3, "bytes": 10}
    out = run_tool(env, "env_control", {"instance_id": iid, "action": "destroy"})
    assert out["error"]["code"] == "not_saved" and rm_calls(env) == []


def test_files_reported_but_not_saved_is_not_trusted(env):
    iid = spawn(env)["instance"]["id"]
    env.files.result = {"saved": False, "export": None, "files": 5, "bytes": 0}
    out = run_tool(env, "env_control", {"instance_id": iid, "action": "destroy"})
    assert out["error"]["code"] == "not_saved" and rm_calls(env) == []


def test_destroy_falls_back_to_the_verified_export(env):
    env.services.set("files", LegacyFiles(count=4))
    iid = spawn(env)["instance"]["id"]
    out = run_tool(env, "env_control", {"instance_id": iid, "action": "destroy"})
    assert out["saved"]["files"] == 4 and out["saved"]["path"] == EXPORT["local_path"]
    assert "(4 files, 3.2 MB)" in out["message"]


def test_delete_message_sentences():
    saved = {"export_id": "x-1", "path": "/p/a.tar.gz", "bytes": 1, "files": 1}
    assert delete_message("a", "/work", saved) == (
        "Deleted a. The files in /work (1 file, 1 B) were saved to /p/a.tar.gz.")
    assert delete_message("a", "/data", None, nothing_to_save=True) == (
        "Deleted a. Nothing in /data needed saving.")
    assert delete_message("a", "/work", None, unsaved=True) == "Deleted a without saving its files."


def test_auto_delete_uses_the_same_call_and_wording(env):
    iid = spawn(env)["instance"]["id"]
    env.mgr.stop(iid)
    env.services.config.auto_delete_days = 1
    env.store.kv_set(sandbox_mod.STOPPED_AT_KEY_PREFIX + iid, "2020-01-01T00:00:00Z")
    env.services.events.clear()
    rep = env.services.reaper.sweep_host("x1carbon")
    assert iid in rep.destroyed and env.files.calls[-1] == (iid, "auto-delete")
    body = notices(env)[-1]
    assert "were saved to C:/exports/work-i-1/a.tar.gz." in body and "(12 files, 3.2 MB)" in body


def test_failed_spawn_cleanup_saves_with_the_same_call(env):
    iid = spawn(env)["instance"]["id"]
    env.mgr._update(iid, lambda r: setattr(r, "status", "error"))
    env.t.script(lambda a, s: a[:2] == ["podman", "ps"], exec_result())
    rec = env.store.get_instance(iid)
    assert rec.status == "error"
    env.mgr.destroy(iid, reason="reaped", export_reason="auto-delete", only_if_status=("error",))
    assert env.files.calls[-1] == (iid, "auto-delete")


# ---------------------------------------------------------------- 2. early spawn validation


def spawn_args(**kw):
    return dict({"template": "python-dev", "host_id": "x1carbon"}, **kw)


def asked(env, args):
    return approval.approval_for("env_spawn", args, tool("env_spawn"), env.services)


def test_unknown_template_lists_valid_ids_and_never_asks(env):
    args = spawn_args(template="pyhton-dev")
    out = run_tool(env, "env_spawn", args)
    assert out["error"]["code"] == "not_found"
    msg = out["error"]["message"]
    assert "'pyhton-dev' not found" in msg and "python-dev" in msg and "ubuntu-dev" in msg
    assert "hermes-agent" not in msg and "slurm-python" not in msg
    directive = asked(env, args)
    assert directive["action"] == "block" and directive["message"] == msg
    assert "rule_key" not in directive
    assert env.t.argvs() == []                      # nothing reached the host


def test_unknown_host_lists_host_ids(env):
    args = spawn_args(host_id="nowhere")
    msg = run_tool(env, "env_spawn", args)["error"]["message"]
    assert "'nowhere' not found" in msg and "x1carbon" in msg
    assert asked(env, args)["action"] == "block"


def test_template_that_cannot_run_on_the_host_kind(env):
    env.store.upsert_host(sample_host(id="cluster", name="cluster", kind="slurm"))
    args = spawn_args(host_id="cluster")
    msg = run_tool(env, "env_spawn", args)["error"]["message"]
    assert "python-dev cannot run on slurm hosts" in msg and "ssh, local" in msg
    assert asked(env, args)["action"] == "block"


def test_agent_template_points_to_cloud_agent_spawn(env):
    for tid in ("hermes-agent", "agent-script"):
        args = spawn_args(template=tid)
        out = run_tool(env, "env_spawn", args)
        assert out["error"]["code"] == "invalid" and "cloud_agent_spawn" in out["error"]["message"]
        assert asked(env, args)["action"] == "block"


def test_slurm_job_template_points_to_slurm_submit(env):
    msg = run_tool(env, "env_spawn", spawn_args(template="slurm-python"))["error"]["message"]
    assert "slurm_submit" in msg


def test_host_at_its_limit_says_how_to_fix_it(env):
    spawn(env)
    spawn(env)
    args = spawn_args()
    out = run_tool(env, "env_spawn", args)
    assert out["error"]["code"] == "limit_reached"
    assert out["error"]["message"] == ("x1carbon already runs 2 of 2 environments. Stop or "
                                       "delete one, or raise Max environments for this host.")
    assert asked(env, args)["action"] == "block"


def test_a_valid_spawn_still_asks_with_the_old_text(env):
    directive = asked(env, spawn_args())
    assert directive["action"] == "approve" and directive["message"].startswith("Spawn python-dev")


# ---------------------------------------------------------------- 3. published ports


def test_template_ports_are_validated_on_save(env):
    store = env.services.templates

    def save(ports, **kw):
        spec = TemplateSpec(id="web", name="Web", description="", ports=ports, **kw)
        return store.save(spec)

    assert save([8000, 5432]).ports == [8000, 5432]
    for bad, text in (([0], "1 to 65535"), ([70000], "1 to 65535"), (["80"], "not valid"),
                      ([True], "not valid"), ([80, 80], "listed twice"),
                      (list(range(8000, 8011)), "at most 10")):
        with pytest.raises(ApiError) as exc:
            save(bad)
        assert text in exc.value.message
    with pytest.raises(ApiError) as exc:
        TemplateStore(env.store).save(TemplateSpec(
            id="scr", name="S", description="", image="localhost/cloud-compute-novnc:0.1",
            display=True, ports=[6080]))
    assert "used by the screen" in exc.value.message


def test_spawn_publishes_template_ports_on_loopback_and_records_them(env):
    env.services.templates.save(TemplateSpec(id="web", name="Web", description="",
                                             ports=[8000, 9000]))
    env.t.script(("podman", "inspect"), exec_result(stdout=inspect_json({8000: 45001, 9000: 45002})))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    out = spawn(env, template="web")
    run = next(a for a in env.t.argvs() if a[:2] == ["podman", "run"])
    assert run.count("-p") == 2
    assert "127.0.0.1::8000" in run and "127.0.0.1::9000" in run
    assert out["instance"]["ports"] == {"8000": "127.0.0.1:45001", "9000": "127.0.0.1:45002"}
    assert "127.0.0.1::8000" in out["preview"]["publish"]


def test_port_forward_finds_a_published_template_port(env):
    from cloud_compute_core import access
    env.services.templates.save(TemplateSpec(id="web", name="Web", description="", ports=[8000]))
    env.t.script(("podman", "inspect"), exec_result(stdout=inspect_json({8000: 45001})))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    rec = env.store.get_instance(spawn(env, template="web")["instance"]["id"])
    mgr = access.AccessManager.__new__(access.AccessManager)
    assert mgr._published_port(rec, env.host, 8000) == 45001      # uses the recorded mapping


def test_approval_text_mentions_published_ports(env):
    env.services.templates.save(TemplateSpec(id="web", name="Web", description="", ports=[8000]))
    msg = asked(env, spawn_args(template="web"))["message"]
    assert "publishes port 8000" in msg


# ---------------------------------------------------------------- 4. screen templates


def test_display_on_a_plain_image_is_refused_on_save(env):
    store = env.services.templates
    with pytest.raises(ApiError) as exc:
        store.save(TemplateSpec(id="ud", name="UD", description="",
                                image="docker.io/library/ubuntu:24.04", display=True))
    assert exc.value.message == DISPLAY_IMAGE_MESSAGE == (
        "Show a screen needs the Desktop (noVNC) image. Start from the Desktop (noVNC) "
        "template, or turn this switch off.")
    # the shipped Desktop template and a retagged copy of its image are fine
    assert store.get("desktop-novnc").display is True
    store.save(TemplateSpec(id="ok", name="OK", description="",
                            image="localhost/cloud-compute-novnc:0.2", display=True))
    store.save(TemplateSpec(id="plain", name="Plain", description="", display=False))


def test_creating_through_the_api_hits_the_same_rule(env):
    with pytest.raises(ApiError) as exc:
        env.services.templates.create({"name": "Mine", "image": "ubuntu:24.04", "display": True})
    assert exc.value.message == DISPLAY_IMAGE_MESSAGE


def test_a_stored_broken_template_spawns_with_a_warning_not_a_rewrite(env):
    broken = TemplateSpec(id="ubuntu-dev", name="Ubuntu dev", description="", display=True,
                          image="docker.io/library/ubuntu:24.04")
    env.store.upsert_template(broken)               # stored before the rule existed
    out = spawn(env, template="ubuntu-dev")
    assert DISPLAY_IMAGE_MESSAGE in out["warnings"]
    assert DISPLAY_IMAGE_MESSAGE in out["preview"]["warnings"]
    assert env.store.get_template("ubuntu-dev").display is True           # not rewritten


# ---------------------------------------------------------------- 5. ports after a restart


def test_start_rereads_the_published_ports(env):
    env.services.templates.save(TemplateSpec(id="web", name="Web", description="", ports=[8000]))
    env.t.script(("podman", "inspect"), exec_result(stdout=inspect_json({8000: 45001})))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    iid = spawn(env, template="web")["instance"]["id"]
    assert env.store.get_instance(iid).ports == {"8000": "127.0.0.1:45001"}
    env.mgr.stop(iid)
    env.t.script(("podman", "inspect"), exec_result(stdout=inspect_json({8000: 46777})))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    assert env.mgr.start(iid).ports == {"8000": "127.0.0.1:46777"}
    assert env.store.get_instance(iid).ports == {"8000": "127.0.0.1:46777"}


def test_spawn_waits_for_the_runtime_to_report_the_ports(env):
    env.services.templates.save(TemplateSpec(id="web", name="Web", description="", ports=[8000]))
    late = exec_result(stdout=inspect_json({8000: 45099}))
    env.t.script(("podman", "inspect"), [exec_result(stdout=inspect_json()), late])
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    out = spawn(env, template="web")
    assert out["instance"]["ports"] == {"8000": "127.0.0.1:45099"}


def test_the_wait_is_bounded(env):
    env.services.templates.save(TemplateSpec(id="web", name="Web", description="", ports=[8000]))
    out = spawn(env, template="web")          # the runtime never reports a port
    assert out["instance"]["ports"] == {}


def test_refresh_ports_updates_the_record_and_leaves_a_stopped_one_alone(env):
    env.services.templates.save(TemplateSpec(id="web", name="Web", description="", ports=[8000]))
    iid = spawn(env, template="web")["instance"]["id"]
    env.t.script(("podman", "inspect"), exec_result(stdout=inspect_json({8000: 47000})))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    assert env.mgr.refresh_ports(iid).ports == {"8000": "127.0.0.1:47000"}
    env.mgr.stop(iid)
    before = env.store.get_instance(iid)
    assert env.mgr.refresh_ports(iid).ports == before.ports
    with pytest.raises(ApiError):
        env.mgr.refresh_ports("i-ffffffff")


# ---------------------------------------------------------------- 6. logs note


def test_empty_logs_carry_a_note(env):
    iid = spawn(env)["instance"]["id"]
    out = run_tool(env, "env_control", {"instance_id": iid, "action": "logs"})
    assert out["logs"] == "" and out["note"] == NO_OUTPUT_NOTE == (
        "No output: the main process of this environment prints nothing. Run commands with "
        "env_exec or open a terminal.")
    rest = instances_api.instance_logs(ApiRequest(path_params={"instance_id": iid}, query={},
                                                  body=None, services=env.services))
    assert rest["note"] == NO_OUTPUT_NOTE
    env.t.script(("podman", "logs"), exec_result(stdout="hello\n"))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    out = run_tool(env, "env_control", {"instance_id": iid, "action": "logs"})
    assert "note" not in out and out["logs"] == "hello\n"


# ---------------------------------------------------------------- 7. strict parameters and cards


def test_unknown_parameters_name_the_valid_ones(env):
    out = run_tool(env, "env_spawn", spawn_args(image="ubuntu"))
    assert out["error"]["code"] == "unknown_parameter"
    assert "'image'" in out["error"]["message"] and "template" in out["error"]["message"]
    assert asked(env, spawn_args(image="ubuntu"))["action"] == "block"
    iid = spawn(env)["instance"]["id"]
    out = run_tool(env, "env_exec", {"instance_id": iid, "command": "ls", "cwd": "/"})
    assert out["error"]["code"] == "unknown_parameter" and "workdir" in out["error"]["message"]
    out = run_tool(env, "env_control", {"instance_id": iid, "action": "stop", "minutes": 5})
    assert out["error"]["code"] == "unknown_parameter"
    assert "'minutes'" in out["error"]["message"] and "with action stop" in out["error"]["message"]
    assert env.store.get_instance(iid).status == "running"          # not stopped
    blocked = approval.approval_for("env_control", {"instance_id": iid, "action": "destroy",
                                                    "keep": True}, tool("env_control"),
                                    env.services)
    assert blocked["action"] == "block" and "'keep'" in blocked["message"]


def test_every_env_result_carries_a_card(env):
    out = spawn(env)
    iid = out["instance"]["id"]
    card = f'::cc-env{{id="{iid}"}}'
    assert out["card"] == card
    assert run_tool(env, "env_exec", {"instance_id": iid, "command": "true"})["card"] == card
    for args in ({"action": "stop"}, {"action": "start"}, {"action": "extend", "minutes": 5},
                 {"action": "logs"}):
        assert run_tool(env, "env_control", dict(args, instance_id=iid))["card"] == card


def test_descriptions_are_short_and_say_what_each_action_needs():
    for name in ("env_spawn", "env_exec", "env_control"):
        assert len(tool(name).schema["description"].split()) <= 80, name
    exec_desc = tool("env_exec").schema["description"]
    assert "fresh shell" in exec_desc and "variables do not persist" in exec_desc
    assert "20 KB" in exec_desc
    control = tool("env_control").schema["description"]
    for word in ("stop", "start", "extend", "logs", "destroy", "minutes", "tail", "save"):
        assert word in control


# ---------------------------------------------------------------- A. working folder (Podman)


def missing_workdir_run(env):
    env.t.script(lambda a, s: a[:2] == ["podman", "run"] and "-w" in a, exec_result(
        rc=125, stderr="Error: workdir /work does not exist on container abc"))
    env.t._run_rules.insert(0, env.t._run_rules.pop())


def test_podman_without_the_folder_creates_it_and_exec_defaults_to_it(env):
    missing_workdir_run(env)
    env.t.script(lambda a, s: a[:2] == ["podman", "cp"], exec_result())
    iid = spawn(env)["instance"]["id"]
    argvs = env.t.argvs()
    assert [a[1] for a in argvs if a[:2] in (["podman", "cp"], ["podman", "start"])] == ["cp", "start"]
    assert env.mgr.exec_workdir(iid) == "/work"
    env.t.reset_calls()
    run_tool(env, "env_exec", {"instance_id": iid, "command": "pwd"})
    ex = env.t.argvs()[0]
    assert ex[:5] == ["podman", "exec", "-w", "/work", "cc-" + iid]       # not left in /
    env.t.reset_calls()
    run_tool(env, "env_exec", {"instance_id": iid, "command": "pwd", "workdir": "/tmp"})
    assert env.t.argvs()[0][2:4] == ["-w", "/tmp"]


def test_failing_to_create_the_folder_fails_the_spawn(env):
    missing_workdir_run(env)
    env.t.script(lambda a, s: a[:2] == ["podman", "cp"], exec_result(rc=125, stderr="no such container"))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    env.t.script(lambda a, s: "mkdir" in a, exec_result(rc=1, stderr="read-only file system"))
    env.t._run_rules.insert(0, env.t._run_rules.pop())
    out = run_tool(env, "env_spawn", spawn_args())
    assert out["error"]["code"] == "internal" and "working folder /work" in out["error"]["message"]
    assert [r for r in env.store.list_instances() if r.status == "error"]


def test_exec_workdir_follows_the_folder_the_environment_was_made_with(env):
    env.services.templates.save(TemplateSpec(id="data", name="Data", description="",
                                             workdir="/data"))
    iid = spawn(env, template="data")["instance"]["id"]
    tpl = env.services.templates.get("data")
    tpl.workdir = "/elsewhere"
    env.services.templates.save(tpl)
    assert env.mgr.exec_workdir(iid) == "/data"


def test_destroy_forgets_the_recorded_folder(env):
    iid = spawn(env)["instance"]["id"]
    env.mgr.destroy(iid, save=False)
    assert env.store.kv_get(sandbox_mod.WORKDIR_KEY_PREFIX + iid) is None
