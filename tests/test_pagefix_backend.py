"""Rerun with the default model, Hermes approvals off, card hints, a failed run, the model list."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_compute_core import agents_api, approval, chat_context, docs_content, hosts_api
from cloud_compute_core import summary_api
from cloud_compute_core.interfaces import ApiRequest
from cloud_compute_core.testing import make_services, sample_host

ROOT = Path(__file__).resolve().parents[1]


def _sibling(name: str, module_name: str):
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name(name))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[module_name]


_am = _sibling("test_agents_manager.py", "cloud_compute_tests_agents_manager")
_reg = _sibling("test_integration_register.py", "cloud_compute_tests_integration_register")
env, _stored_run = _am.env, _am._stored_run                  # noqa: F811 - the shared fixture
plugin, bg_calls = _reg.plugin, _reg.bg_calls                 # noqa: F811


# --------------------------------------------------------------------------- rerun


def _save_model(e, name, model, default=False):
    return e.mgr.save_model({"name": name, "base_url": "http://192.0.2.10:8080/v1", "model": model,
                             "is_default": default})


def test_rerun_without_a_choice_keeps_the_old_model_and_default_marker_drops_it(env):
    e = env
    wrong = _save_model(e, "wrong", "no-such-model")
    right = _save_model(e, "right", "good-model", default=True)
    run = _stored_run(e, status="failed", model_id=wrong.id, template_id="hermes-agent", mode="hermes")

    kept = e.mgr._rerun_plan(run.id)
    assert kept["model_id"] == wrong.id                          # the old behaviour, still true
    for marker in ("default", " Default "):
        plan = e.mgr._rerun_plan(run.id, model_id=marker)
        assert plan["model_id"] is None and plan["endpoint"] is None
    assert e.mgr._rerun_plan(run.id, model_id=right.id)["model_id"] == right.id

    # The card names the model the new run really uses: the default, not the old one.
    text = e.mgr.describe_rerun({"run_id": run.id, "model_id": "default"})
    assert "good-model" in text and "no-such-model" not in text
    old = e.mgr.describe_rerun({"run_id": run.id})
    assert "no-such-model" in old


def test_the_rerun_tool_and_guide_describe_the_default_marker():
    desc = agents_api.RERUN_SCHEMA["parameters"]["properties"]["model_id"]["description"]
    assert "default" in desc and "current default model" in desc
    guide = (ROOT / "skills" / "guide" / "SKILL.md").read_text(encoding="utf-8")
    assert "or `default` for the current default model" in guide


def test_the_rerun_route_accepts_the_default_marker():
    seen = {}

    class Agents:
        def rerun(self, run_id, session_id=None, **kw):
            seen.update(kw)
            return SimpleNamespace(to_dict=lambda: {"id": "a-new00001"})

    req = ApiRequest(path_params={"run_id": "a-1234abcd"}, query={}, body={"model_id": "default"},
                     services=SimpleNamespace(agents=Agents()))
    agents_api.rerun_agent(req)
    assert seen["model_id"] == "default"


# --------------------------------------------------------------------------- approvals off


@pytest.mark.parametrize("config,expected", [
    ({"approvals": {"mode": "off"}}, "off"),
    ({"approvals": {"mode": False}}, "off"),              # YAML reads an unquoted off as false
    ({"approvals": {"mode": "Manual"}}, "manual"),
    ({"approvals": {"mode": "smart"}}, "smart"),
    ({"approvals": {}}, None),
    ({}, None),
])
def test_the_approvals_mode_comes_from_the_hermes_config(monkeypatch, config, expected):
    monkeypatch.setattr(approval, "_read_hermes_config", lambda: config)
    assert approval.hermes_approvals_mode() == expected
    assert approval.approvals_status() == {"mode": expected, "off": expected == "off"}


def test_an_unreadable_config_is_not_reported_as_off(monkeypatch):
    def boom():
        raise RuntimeError("no hermes")
    monkeypatch.setattr(approval, "_read_hermes_config", boom)
    assert approval.hermes_approvals_mode() is None
    assert approval.approvals_status() == {"mode": None, "off": False}


def test_summary_carries_the_approvals_field(monkeypatch, tmp_path):
    from cloud_compute_core.store import Store
    store = Store(tmp_path / "s.db")
    services = make_services(store=store)
    monkeypatch.setattr(approval, "_read_hermes_config", lambda: {"approvals": {"mode": "off"}})
    out = summary_api.summary(ApiRequest(path_params={}, query={}, body=None, services=services))
    assert out["approvals"] == {"mode": "off", "off": True}
    monkeypatch.setattr(approval, "_read_hermes_config", lambda: {"approvals": {"mode": "manual"}})
    out = summary_api.summary(ApiRequest(path_params={}, query={}, body=None, services=services))
    assert out["approvals"] == {"mode": "manual", "off": False}
    store.close()


def test_compute_status_warns_the_model_only_when_approvals_are_off(monkeypatch, tmp_path):
    from cloud_compute_core.store import Store
    store = Store(tmp_path / "s.db")
    store.upsert_host(sample_host())
    services = make_services(store=store)
    monkeypatch.setattr(approval, "_read_hermes_config", lambda: {"approvals": {"mode": "off"}})
    out = hosts_api.compute_status({}, services, {})
    assert out["approvals_warning"] == approval.APPROVALS_OFF_WARNING
    assert "without an approval card" in out["approvals_warning"] and len(out["approvals_warning"]) < 300
    monkeypatch.setattr(approval, "_read_hermes_config", lambda: {"approvals": {"mode": "manual"}})
    assert "approvals_warning" not in hosts_api.compute_status({}, services, {})
    store.close()


def test_help_and_guide_say_when_hermes_approvals_are_on():
    guide = (ROOT / "skills" / "guide" / "SKILL.md").read_text(encoding="utf-8")
    help_text = json.dumps([docs_content.get_topic(t["topic"]) for t in docs_content.list_topics()])
    assert "Nothing is done until you approve" not in help_text
    assert "When Hermes approvals are on, these show an approval card" in help_text
    assert "When Hermes approvals are on, nothing starts until you approve" in help_text
    assert "approvals_warning" in guide
    for line in guide.splitlines():
        if line.startswith("Approval: ") and not line.startswith("Approval: No."):
            assert "Hermes approvals are on" in line, line


# --------------------------------------------------------------------------- card hints


def test_the_system_prompt_section_demands_a_line_of_its_own_and_fits():
    text = chat_context.SYSTEM_PROMPT_SECTION
    assert "line of its own" in text and "empty line before and after" in text
    assert len(text) <= 1500
    assert "empty line before it and after it" in chat_context.CARD_HINT


def test_a_tool_result_with_a_card_carries_the_card_hint(plugin):
    hint = chat_context.CARD_HINT
    out = plugin._with_card_hint({"run": {}, "card": '::cc-agent{id="a-1234abcd"}'})
    assert out["card_hint"] == hint and out["card"] == '::cc-agent{id="a-1234abcd"}'
    nested = plugin._with_card_hint({"instances": [{"id": "i-1", "card": '::cc-env{id="i-1"}'}]})
    assert nested["card_hint"] == hint
    assert "card_hint" not in plugin._with_card_hint({"hosts": []})
    assert "card_hint" not in plugin._with_card_hint({"card": "not a directive"})
    assert plugin._with_card_hint("text") == "text"
    guide = (ROOT / "skills" / "guide" / "SKILL.md").read_text(encoding="utf-8")
    assert "empty line before and after" in guide and "card_hint" in guide


# --------------------------------------------------------------------------- failed run


def test_a_model_failure_keeps_the_reply_but_the_notice_uses_the_cause(env, fake_transports):
    from cloud_compute_core.testing import exec_result
    e = env
    run = _stored_run(e, status="running", mode="hermes", template_id="hermes-agent",
                      model="no-such-model", endpoint_host="llm.example.net")
    t = fake_transports.get("x1carbon")
    t._run_rules.clear()
    raw = 'Provider said: HTTP 404: {"error":"please check the model you provided"}'
    t.script(("podman", "exec"), exec_result(
        stdout=f"exit=1\nhead={run.base_sha}\nahead=0\n==log\nline\n==reply\n{raw}\n"))
    e.mgr.poll()
    got = e.mgr._load(run.id)
    assert got.status == "failed" and got.error_kind == "model_not_found"
    assert "HTTP 404" in got.reply                             # the data is kept; the page shows the cause
    assert "answered 404" in got.error and "please check the model" in got.error
    notes = [n for n in e.events.payloads("notify") if "failed" in n["title"]]
    assert len(notes) == 1 and "answered 404" in notes[0]["body"]
    assert '{"error"' not in notes[0]["body"] and "Provider said" not in notes[0]["body"]


def test_a_failed_run_with_commits_keeps_its_reply(env, fake_transports):
    from cloud_compute_core.testing import exec_result
    e = env
    run = _stored_run(e, status="running", mode="hermes", template_id="hermes-agent",
                      model="m", endpoint_host="llm.example.net")
    t = fake_transports.get("x1carbon")
    t._run_rules.clear()
    t.script(("podman", "exec"), exec_result(
        stdout="exit=1\nhead=abc\nahead=2\n==log\nline\n==reply\nProvider said: HTTP 404: {}\n"))
    e.mgr.poll()
    assert e.mgr._load(run.id).reply


# --------------------------------------------------------------------------- model list


def test_the_model_list_shows_each_endpoint_and_model_once():
    from cloud_compute_core import agent_models
    saved = [{"id": "m-1", "name": "Inference.baseten.co", "base_url": "https://inference.baseten.co/v1/",
              "model": "deepseek-fast", "endpoint_host": "inference.baseten.co", "is_default": True}]
    detected = [
        {"id": "d-1", "name": "Inference.baseten.co", "base_url": "https://inference.baseten.co/v1",
         "model": "deepseek-fast", "endpoint_host": "inference.baseten.co"},
        {"id": "d-2", "name": "Llama", "base_url": "http://192.0.2.10:8080/v1", "model": "llama",
         "endpoint_host": "192.0.2.10:8080"},
        {"id": "d-3", "name": "Llama (alias)", "base_url": "http://192.0.2.10:8080/v1", "model": "llama",
         "endpoint_host": "192.0.2.10:8080"},
    ]
    unavailable = [
        {"name": "inference-baseten-co", "endpoint_host": "inference.baseten.co", "kind": "no_key", "reason": "x"},
        {"name": "Other", "endpoint_host": "other.example.net", "kind": "no_key", "reason": "x"},
        {"name": "Other again", "endpoint_host": "other.example.net", "kind": "no_key", "reason": "x"},
    ]
    kept, shown = agent_models.tidy(saved, detected, unavailable)
    assert [d["id"] for d in kept] == ["d-2"]                  # saved hides d-1; alias d-3 merged
    assert [u["name"] for u in shown] == ["Other"]             # duplicate of an available one hidden


def test_the_models_route_returns_the_tidied_lists():
    class Agents:
        def models(self):
            return [SimpleNamespace()]

        def model_view(self, m):
            return {"id": "m-1", "base_url": "https://x.example.net/v1", "model": "a",
                    "endpoint_host": "x.example.net", "is_default": True}

        def detected_models(self):
            return [{"id": "d-1", "base_url": "https://x.example.net/v1", "model": "a",
                     "endpoint_host": "x.example.net"}]

        def unavailable_models(self):
            return [{"name": "x", "endpoint_host": "x.example.net", "kind": "no_key", "reason": "r"}]

    out = agents_api.list_models(ApiRequest(path_params={}, query={}, body=None,
                                            services=SimpleNamespace(agents=Agents())))
    assert out["detected"] == [] and out["unavailable"] == [] and out["default_id"] == "m-1"


# --------------------------------------------------------------------------- wizard save keeps the test


def test_a_slurm_host_saved_right_after_its_test_shows_the_work_directory_failure(tmp_path):
    from cloud_compute_core.prober import Prober, TestResult, TestStep
    from cloud_compute_core.store import Store
    from cloud_compute_core.testing import sample_host_state
    store = Store(tmp_path / "s.db")

    class FakeProber:
        remember_workdir = Prober.remember_workdir

        def __init__(self):
            self.services = None

        def test_connection(self, spec):
            return TestResult(ok=True, status="online", steps=[
                TestStep("scheduler", True, "Slurm 24"),
                TestStep("workdir", False, "~/cloud-compute is not visible from the compute nodes",
                         "Choose a Work directory on storage shared with the compute nodes")])

        def probe(self, host):                       # a periodic probe never checks the folder
            return store.set_host_state(sample_host_state(host_id=host.id, status="online")) or \
                store.get_host_state(host.id)

    prober = FakeProber()
    services = make_services(store=store, prober=prober)
    prober.services = services
    body = {"name": "Cluster", "kind": "slurm", "address": "login.example.net",
            "slurm": {"workdir": "~/cloud-compute"}}
    req = lambda b: ApiRequest(path_params={}, query={}, body=dict(b), services=services)  # noqa: E731
    tested = hosts_api.test_host(req(body))
    assert [s["name"] for s in tested["steps"] if s["ok"] is False] == ["workdir"]
    saved = hosts_api.create_host(req(body))
    caps = saved["state"]["capabilities"]
    assert caps["workdir_shared"] is False
    assert "not visible from the compute nodes" in caps["workdir_detail"]
    assert "Fix: Choose a Work directory" in caps["workdir_detail"]
    # A host saved without a test has nothing to show, and the test is used once.
    store.delete_host("cluster")
    again = hosts_api.create_host(req(body))
    assert again["state"]["capabilities"]["workdir_shared"] is None
    store.close()


# --------------------------------------------------------------------------- wording (backend)


def test_clear_finished_note_has_no_internal_field_name():
    from cloud_compute_core.slurm import _removed_note
    jobs = [SimpleNamespace(id=f"j-{i}", job_dir=f"/data/j-{i}") for i in range(3)]
    note = _removed_note(jobs, "slurm-r7")
    assert "job_dirs" not in note and note.endswith("were not deleted.")
    assert note.startswith("Removed 3 finished jobs from this computer's job list only.")


def test_array_job_output_headers_are_labelled_by_task():
    from cloud_compute_core.slurm import label_tasks
    raw = ("==> /data/cloud-compute/j-ea348351/slurm-157_1.out <==\ntask 1\n\n"
           "==> /data/cloud-compute/j-ea348351/slurm-157_2.out <==\ntask 2\n")
    out = label_tasks(raw)
    assert out == "Task 1\ntask 1\n\nTask 2\ntask 2\n" and "==>" not in out
    assert label_tasks("plain output\n") == "plain output\n"


def test_two_sbatch_errors_are_shown_one_per_line():
    from cloud_compute_core.slurm import _lines
    err = ("sbatch: error: CPU count per node can not be satisfied\n"
           "sbatch: error: Batch job submission failed: Requested node configuration is not available\n")
    assert _lines(err) == ("sbatch: error: CPU count per node can not be satisfied\n"
                           "sbatch: error: Batch job submission failed: Requested node configuration is not available")


def test_instances_carry_the_folder_a_delete_saves():
    from cloud_compute_core import instances_api

    class Rec:
        def __init__(self, i):
            self.id = i

        def to_dict(self):
            return {"id": self.id, "template_id": "python-dev"}

    class Sandboxes:
        def list(self, host, refresh, include_destroyed=False):
            return [Rec("i-1"), Rec("i-2")]

        def get(self, iid):
            return Rec(iid)

        def work_folder(self, rec):
            return "/data/work" if rec.id == "i-1" else None

    services = SimpleNamespace(sandboxes=Sandboxes())
    got = instances_api.list_instances(ApiRequest(path_params={}, query={}, body=None, services=services))
    assert [(g["id"], g["workdir"]) for g in got] == [("i-1", "/data/work"), ("i-2", None)]
    one = instances_api.get_instance(ApiRequest(path_params={"instance_id": "i-1"}, query={},
                                                body=None, services=services))
    assert one["workdir"] == "/data/work"


# --------------------------------------------------------------------------- help wording


def test_remote_screen_help_says_open_in_browser_once_and_agents_help_names_profile_models():
    topics = {t["topic"]: docs_content.get_topic(t["topic"])["body_md"] for t in docs_content.list_topics()}
    screen = topics["display"] if "display" in topics else next(b for b in topics.values() if "Browser fallback" in b)
    fallback = screen.split("### Browser fallback", 1)[1]
    assert fallback.count("Open in browser") == 1
    agents = topics["agents"]
    assert "A model this Hermes profile already offers uses its own key secret instead." in agents
    assert "CC_AGENT_API_KEY" in agents


def test_stop_help_says_it_fetches_first():
    agents = docs_content.get_topic("agents")["body_md"]
    assert "**Stop** first fetches commits you do not have yet, then removes its sandbox." in agents
