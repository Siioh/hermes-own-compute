"""Template store: built-ins, copy-on-write overrides, validation (no network)."""

from __future__ import annotations

import copy

import pytest

from cloud_compute_core import runtimes
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.models import TemplateSpec, VariableSpec
from cloud_compute_core.templates import BUILTIN_TEMPLATES, TemplateStore, provide


@pytest.fixture
def tpls(store):
    return TemplateStore(store)


def _user(**kw) -> TemplateSpec:
    base = dict(id="my-env", name="My env", description="d")
    base.update(kw)
    return TemplateSpec(**base)


def _errors(tpls, **kw) -> str:
    return "; ".join(tpls.validate(_user(**kw)))


def test_builtins_listed_valid_and_ordered(tpls):
    listed = tpls.list()
    assert [t.id for t in listed] == BUILTIN_TEMPLATES
    assert BUILTIN_TEMPLATES == ["ubuntu-dev", "python-dev", "desktop-novnc", "slurm-python"]
    for t in listed:
        assert t.builtin is True
        assert tpls.validate(t) == []
    by_id = {t.id: t for t in listed}
    assert by_id["ubuntu-dev"].image == "docker.io/library/ubuntu:24.04"
    assert by_id["ubuntu-dev"].command == ["sleep", "infinity"]
    assert by_id["python-dev"].image == "docker.io/library/python:3.12-slim"
    assert by_id["desktop-novnc"].display is True
    assert by_id["desktop-novnc"].image == runtimes.DISPLAY_IMAGE_TAG
    slurm = by_id["slurm-python"]
    assert slurm.kind == "slurm_job" and slurm.host_kinds == ["slurm"]
    assert slurm.image.startswith("docker://")


def test_builtin_variables_carry_no_values(tpls):
    for t in tpls.list():
        for v in t.variables:
            assert v.source.startswith("secret:") or v.source == "literal:"


def test_list_returns_copies(tpls):
    tpls.list()[0].name = "mutated"
    assert tpls.get("ubuntu-dev").name != "mutated"


def test_get_unknown_is_404(tpls):
    with pytest.raises(ApiError) as exc:
        tpls.get("nope")
    assert exc.value.status == 404 and exc.value.code == "not_found"


def test_save_user_template_round_trip(tpls, store, tmp_path):
    spec = _user(cpus=1.5, memory_gb=2, ttl_minutes=30, idle_minutes=None,
                 variables=[VariableSpec("A", "literal:1", "agent-visible"),
                            VariableSpec("TOK", "secret:MY_TOKEN", "runtime", hosts=["x1carbon"])])
    saved = tpls.save(spec)
    assert saved.builtin is False
    assert tpls.get("my-env") == spec
    assert [t.id for t in tpls.list()] == BUILTIN_TEMPLATES + ["my-env"]
    assert TemplateStore(store).get("my-env").variables[1].hosts == ["x1carbon"]


def test_user_templates_sorted_by_id(tpls):
    for tid in ("zeta", "alpha", "mid"):
        tpls.save(_user(id=tid))
    assert [t.id for t in tpls.list()][4:] == ["alpha", "mid", "zeta"]


def test_save_builtin_is_copy_on_write_and_delete_reverts(tpls):
    original = tpls.get("python-dev")
    edited = copy.deepcopy(original)
    edited.cpus = 8
    edited.builtin = True  # callers may round-trip the flag; the override is a user template
    saved = tpls.save(edited)
    assert saved.builtin is False
    assert tpls.get("python-dev").cpus == 8 and tpls.get("python-dev").builtin is False
    listed = tpls.list()
    assert [t.id for t in listed] == BUILTIN_TEMPLATES  # shadowed in place, not duplicated
    assert listed[1].cpus == 8
    assert tpls.delete("python-dev") is True
    reverted = tpls.get("python-dev")
    assert reverted == original and reverted.builtin is True


def test_delete_pure_builtin_is_refused(tpls):
    with pytest.raises(ApiError) as exc:
        tpls.delete("ubuntu-dev")
    assert exc.value.status == 400 and exc.value.code == "invalid"
    assert tpls.get("ubuntu-dev").builtin is True


def test_delete_user_and_unknown(tpls):
    tpls.save(_user())
    assert tpls.delete("my-env") is True
    assert tpls.delete("my-env") is False
    assert tpls.delete("never-existed") is False
    with pytest.raises(ApiError):
        tpls.get("my-env")


def test_save_invalid_raises_400_with_joined_errors(tpls, store):
    with pytest.raises(ApiError) as exc:
        tpls.save(_user(name="", cpus=0))
    assert exc.value.status == 400 and exc.value.code == "invalid"
    assert "name is required" in exc.value.message and "; " in exc.value.message
    assert "cpus" in exc.value.message
    assert store.get_template("my-env") is None


@pytest.mark.parametrize("kw,needle", [
    (dict(id=""), "id must"),
    (dict(id="Bad Id"), "id must"),
    (dict(id="-lead"), "id must"),
    (dict(id="a/b"), "id must"),
    (dict(id="x" * 64), "id must"),
    (dict(name="   "), "name is required"),
    (dict(kind="vm"), "kind must"),
    (dict(image=""), "image is required"),
    (dict(image="python 3.12"), "image is required"),
    (dict(command="sleep infinity"), "command must"),
    (dict(command=["sleep", 5]), "command must"),
    (dict(cpus=0), "cpus"),
    (dict(cpus=-1), "cpus"),
    (dict(cpus="2"), "cpus"),
    (dict(memory_gb=0), "memory_gb"),
    (dict(gpus=-1), "gpus"),
    (dict(gpus=1.5), "gpus"),
    (dict(ttl_minutes=4), "ttl_minutes"),
    (dict(ttl_minutes=1441), "ttl_minutes"),
    (dict(idle_minutes=0), "idle_minutes"),
    (dict(network="host"), "network must"),
    (dict(host_kinds=[]), "host_kinds"),
    (dict(host_kinds=["ssh", "mars"]), "host_kinds"),
    (dict(kind="slurm_job", host_kinds=["ssh"]), "slurm"),
    (dict(kind="slurm_job", host_kinds=["slurm", "ssh"]), "slurm"),
    (dict(host_kinds=["slurm"]), "container templates"),
    (dict(agent="yes"), "agent must"),
    (dict(agent={"x": object()}), "agent must"),
    (dict(variables=[VariableSpec("1BAD", "literal:x", "runtime")]), "name must match"),
    (dict(variables=[VariableSpec("A-B", "literal:x", "runtime")]), "name must match"),
    (dict(variables=[VariableSpec("A", "env:X", "runtime")]), "source must be"),
    (dict(variables=[VariableSpec("A", "secret:bad name", "runtime")]), "secret name"),
    (dict(variables=[VariableSpec("A", "secret:", "runtime")]), "secret name"),
    (dict(variables=[VariableSpec("A", "literal:a\nb", "runtime")]), "single line"),
    (dict(variables=[VariableSpec("A", "literal:x", "public")]), "scope must"),
    (dict(variables=[VariableSpec("A", "literal:x", "runtime"),
                     VariableSpec("A", "literal:y", "redacted")]), "duplicate"),
])
def test_validation_errors(tpls, kw, needle):
    assert needle in _errors(tpls, **kw)


def test_ttl_idle_boundaries_and_none_accepted(tpls):
    for ttl, idle in ((5, 5), (1440, 1440), (None, None)):
        assert tpls.validate(_user(ttl_minutes=ttl, idle_minutes=idle)) == []


def test_valid_slurm_template(tpls):
    spec = _user(kind="slurm_job", image="docker://python:3.12-slim", host_kinds=["slurm"],
                 ttl_minutes=None, idle_minutes=None)
    assert tpls.validate(spec) == []


def test_agent_template_accepted(tpls):
    spec = _user(
        id="hermes-agent", name="Hermes cloud agent",
        image="localhost/cloud-compute-agent:0.1", ttl_minutes=240, idle_minutes=120,
        variables=[VariableSpec("CC_AGENT_API_KEY", "secret:CC_AGENT_API_KEY", "runtime")],
        agent={"enabled": True, "mode": "hermes", "model": None, "base_url": None,
               "key_secret": "CC_AGENT_API_KEY", "max_turns": 30, "image_base": None,
               "toolsets": ["terminal"], "budget_usd": 1.5},
    )
    assert tpls.validate(spec) == []
    saved = tpls.save(spec)
    assert tpls.get("hermes-agent").agent == saved.agent
    script = _user(id="agent-script", name="Scripted", image="localhost/cloud-compute-agent:0.1-lite",
                   cpus=1, memory_gb=1, agent={"enabled": True, "mode": "script"})
    assert tpls.save(script).agent["mode"] == "script"


def test_to_dict_from_dict_round_trip(tpls):
    for t in tpls.list():
        assert TemplateSpec.from_dict(t.to_dict()) == t
    spec = _user(variables=[VariableSpec("A", "secret:S", "runtime", hosts=["h"])],
                 agent={"mode": "script"})
    assert TemplateSpec.from_dict(spec.to_dict()) == spec


def test_provide_uses_services_store(services):
    ts = provide(services)
    assert isinstance(ts, TemplateStore)
    ts.save(_user())
    assert services.store.get_template("my-env") is not None


def test_agent_base_url_and_model_persist_on_builtin_agent_override(tpls):
    from cloud_compute_core.agents import AGENT_TEMPLATES
    base = copy.deepcopy(next(t for t in AGENT_TEMPLATES if t.id == "hermes-agent"))
    base.agent = dict(base.agent, base_url="https://api.example.com/v1", model="my-model")
    saved = tpls.save(base)
    assert saved.builtin is False
    got = tpls.get("hermes-agent")
    assert got.agent["base_url"] == "https://api.example.com/v1"
    assert got.agent["model"] == "my-model"


@pytest.mark.parametrize("url", [None, "", "http://localhost:8000/v1", "https://h.example/v1",
                                 "http://10.0.0.5:11434"])
def test_agent_base_url_accepted(tpls, url):
    assert _errors(tpls, agent={"enabled": True, "base_url": url}) == ""


@pytest.mark.parametrize("url", ["ftp://h/v1", "api.example.com/v1", "javascript:alert(1)",
                                 "https://user:pw@h/v1", "https://h/v 1", "http://", 5, ["x"]])
def test_agent_base_url_rejected(tpls, url):
    assert "agent.base_url" in _errors(tpls, agent={"enabled": True, "base_url": url})


def test_container_builtins_leave_ttl_and_idle_to_the_settings_defaults(tpls):
    """Built-ins must not hard-code limits, or the default_ttl/idle settings would do nothing."""
    by_id = {t.id: t for t in tpls.list()}
    for tid in ("ubuntu-dev", "python-dev", "desktop-novnc"):
        assert by_id[tid].kind == "container"
        assert by_id[tid].ttl_minutes is None and by_id[tid].idle_minutes is None, tid
        assert tpls.validate(by_id[tid]) == []


# --------------------------------------------------------------------------- safe, editable templates

def _all_shipped(tpls):
    from cloud_compute_core.agents import AGENT_TEMPLATES
    return list(tpls.list()) + [copy.deepcopy(t) for t in AGENT_TEMPLATES]


def test_builtins_ship_no_secret_variables(tpls):
    for t in tpls.list():
        assert t.builtin is True
        assert not [v for v in t.variables if v.source.startswith("secret:")], t.id
    assert tpls.get("python-dev").variables == []


def test_every_shipped_variable_is_explained(tpls):
    seen = 0
    for t in _all_shipped(tpls):
        for v in t.variables:
            seen += 1
            assert v.description.strip(), f"{t.id}.{v.name} has no description"
    assert seen >= 1                                  # the agent key at least


def test_agent_key_description_text_and_script_agent_carries_no_key():
    from cloud_compute_core.agents import AGENT_TEMPLATES, KEY_DESCRIPTION, KEY_ENV
    by_id = {t.id: t for t in AGENT_TEMPLATES}
    key = by_id["hermes-agent"].variables[0]
    assert key.name == KEY_ENV
    assert key.description == ("API key for the cloud agent's model endpoint. Sent only into "
                               "cloud-agent sandboxes in Hermes mode, after you approve the run. "
                               "Use a scoped, spend-capped key.") == KEY_DESCRIPTION
    assert by_id["agent-script"].variables == []
    assert by_id["agent-script"].agent["mode"] == "script"


def test_user_variable_keeps_the_description_typed_or_empty(tpls):
    spec = _user(variables=[VariableSpec("A", "literal:1", "runtime", description="Mine"),
                            VariableSpec("B", "literal:2", "runtime")])
    tpls.save(spec)
    got = tpls.get("my-env")
    assert [v.description for v in got.variables] == ["Mine", ""]


def test_variable_description_must_be_short_text(tpls):
    assert "description" in _errors(tpls, variables=[VariableSpec("A", "literal:1", "runtime",
                                                                  description="x" * 501)])
    assert "description" in _errors(tpls, variables=[VariableSpec("A", "literal:1", "runtime",
                                                                  description=5)])


def _old_python_dev(tpls, **changes):
    """The stored copy an earlier version made: the current default plus the HF_TOKEN variable."""
    spec = tpls.get_default("python-dev")
    spec.variables = [VariableSpec(name="HF_TOKEN", source="secret:HF_TOKEN", scope="runtime")]
    for k, v in changes.items():
        setattr(spec, k, v)
    return spec


def test_unedited_old_python_dev_copy_is_treated_as_unmodified(store):
    store.upsert_template(_old_python_dev(TemplateStore(store)))
    fresh = TemplateStore(store)
    assert fresh.get("python-dev").variables == []
    assert [t.variables for t in fresh.list() if t.id == "python-dev"] == [[]]
    assert store.get_template("python-dev") is None          # the old copy is dropped
    assert fresh.is_modified("python-dev") is False


def test_edited_old_python_dev_copy_stays_as_it_is(store):
    store.upsert_template(_old_python_dev(TemplateStore(store), cpus=8))
    fresh = TemplateStore(store)
    got = fresh.get("python-dev")
    assert got.cpus == 8 and [v.name for v in got.variables] == ["HF_TOKEN"]
    assert fresh.is_modified("python-dev") is True


def test_user_can_add_their_own_hf_token_variable(tpls):
    edited = tpls.get("python-dev")
    edited.variables = [VariableSpec("HF_TOKEN", "secret:HF_TOKEN", "runtime",
                                     description="Hugging Face token for model downloads.")]
    tpls.save(edited)
    assert [v.name for v in tpls.get("python-dev").variables] == ["HF_TOKEN"]


# ---- built-ins are never deleted; edits are recoverable

def test_delete_pristine_builtin_is_refused_with_guidance(tpls):
    for tid in BUILTIN_TEMPLATES:
        with pytest.raises(ApiError) as exc:
            tpls.delete(tid)
        assert exc.value.status == 400 and exc.value.code == "invalid"
        assert exc.value.message == ("Built-in templates cannot be deleted. Edit it to make your "
                                     "own version; Reset to default restores it.")
        assert tpls.get(tid).builtin is True


def test_delete_override_resets_builtin_to_default(tpls):
    original = tpls.get("desktop-novnc")
    edited = copy.deepcopy(original)
    edited.cpus = 6
    edited.name = "My desktop"
    tpls.save(edited)
    assert tpls.is_modified("desktop-novnc") is True
    assert tpls.delete("desktop-novnc") is True
    assert tpls.get("desktop-novnc") == original
    assert tpls.is_modified("desktop-novnc") is False
    with pytest.raises(ApiError) as exc:                       # nothing left to reset
        tpls.delete("desktop-novnc")
    assert exc.value.status == 400


def test_get_default_returns_the_shipped_version_and_404_for_user_templates(tpls):
    edited = tpls.get("ubuntu-dev")
    edited.cpus = 16
    tpls.save(edited)
    assert tpls.get_default("ubuntu-dev").cpus == 2 and tpls.get_default("ubuntu-dev").builtin is True
    assert tpls.get_default("hermes-agent").agent["mode"] == "hermes"
    tpls.save(_user())
    with pytest.raises(ApiError) as exc:
        tpls.get_default("my-env")
    assert exc.value.status == 404


def test_is_modified_false_for_user_templates_and_untouched_builtins(tpls):
    tpls.save(_user())
    assert tpls.is_modified("my-env") is False
    assert tpls.is_modified("ubuntu-dev") is False
    assert tpls.is_modified("nope") is False


def test_editing_a_builtin_does_not_lose_the_default(tpls):
    original = tpls.get("slurm-python")
    edited = copy.deepcopy(original)
    edited.command = ["python", "-c", "print(1)"]
    tpls.save(edited)
    assert tpls.get_default("slurm-python") == original
    assert tpls.get("slurm-python").command == ["python", "-c", "print(1)"]


def test_agent_template_delete_resets_an_edit_and_refuses_when_pristine(tpls):
    from cloud_compute_core.agents import AGENT_TEMPLATES
    tpls.save(copy.deepcopy(next(t for t in AGENT_TEMPLATES if t.id == "agent-script")))
    with pytest.raises(ApiError) as exc:                       # stored copy equals the default
        tpls.delete("agent-script")
    assert exc.value.status == 400 and "cannot be deleted" in exc.value.message
    edited = tpls.get("agent-script")
    edited.cpus = 3
    tpls.save(edited)
    assert tpls.is_modified("agent-script") is True
    assert tpls.delete("agent-script") is True
    assert tpls.get("agent-script").cpus == 1                 # back to the default, still present
    assert tpls.is_modified("agent-script") is False


def test_agent_template_missing_from_the_store_cannot_be_deleted(tpls):
    with pytest.raises(ApiError) as exc:
        tpls.delete("hermes-agent")
    assert exc.value.status == 400


# ---- rename

def test_rename_a_user_template_trims_and_keeps_the_rest(tpls):
    tpls.save(_user(name="Ubuntu dev (copy)", cpus=3))
    spec = tpls.get("my-env")
    spec.name = "Analysis\tbox"
    with pytest.raises(ApiError):                              # a tab inside the name is refused
        tpls.save(spec)
    assert tpls.get("my-env").name == "Ubuntu dev (copy)"      # nothing was stored
    spec.name = "  Analysis box \t"                            # surrounding whitespace is trimmed
    tpls.save(spec)
    got = tpls.get("my-env")
    assert got.name == "Analysis box" and got.cpus == 3


def test_duplicate_then_rename_flow(tpls):
    original = tpls.get("ubuntu-dev")
    dup = copy.deepcopy(original)
    dup.id, dup.name, dup.builtin = "ubuntu-dev-copy", original.name + " (copy)", False
    tpls.save(dup)
    renamed = tpls.get("ubuntu-dev-copy")
    renamed.name = "Build box"
    tpls.save(renamed)
    assert tpls.get("ubuntu-dev-copy").name == "Build box"
    assert tpls.get("ubuntu-dev").name == "Ubuntu dev"


def test_rename_a_builtin_override_and_reset_restores_the_name(tpls):
    edited = tpls.get("python-dev")
    edited.name = "My Python"
    tpls.save(edited)
    assert tpls.get("python-dev").name == "My Python"
    assert [t.name for t in tpls.list() if t.id == "python-dev"] == ["My Python"]
    tpls.delete("python-dev")
    assert tpls.get("python-dev").name == "Python dev"


def test_duplicate_names_are_allowed(tpls):
    tpls.save(_user(id="one", name="Same"))
    tpls.save(_user(id="two", name="Same"))
    assert [t.name for t in tpls.list() if t.id in ("one", "two")] == ["Same", "Same"]


@pytest.mark.parametrize("name,needle", [
    ("", "name is required"), ("   ", "name is required"), (None, "name is required"),
    ("x" * 61, "60 characters or fewer"),
    ("two\nlines", "control characters"), ("tab\there", "control characters"),
    ("nul\x00byte", "control characters"), ("bell\x07", "control characters"),
    ("del\x7f", "control characters"),
])
def test_rename_validation(tpls, name, needle):
    assert needle in _errors(tpls, name=name)


def test_name_of_60_chars_is_accepted(tpls):
    assert _errors(tpls, name="x" * 60) == ""
    assert _errors(tpls, name=" " + "x" * 60 + " ") == ""      # trimmed first


# ---- create from scratch

def test_create_from_a_name_and_an_image_generates_an_id_and_works(tpls):
    spec = tpls.create({"name": "  My lab env ", "image": "docker.io/library/debian:12"})
    assert spec.id == "my-lab-env" and spec.name == "My lab env" and spec.builtin is False
    assert spec.description == "" and spec.command == ["sleep", "infinity"]
    assert spec.kind == "container" and spec.host_kinds == ["ssh", "local"]
    assert spec.ttl_minutes is None and spec.idle_minutes is None   # Settings defaults apply
    assert tpls.validate(spec) == []
    assert tpls.get("my-lab-env") == spec


def test_create_generates_unique_ids(tpls):
    a = tpls.create({"name": "Lab", "image": "i"})
    b = tpls.create({"name": "Lab", "image": "i"})
    c = tpls.create({"name": "Ubuntu dev", "image": "i"})       # not the built-in's id
    d = tpls.create({"name": "!!!", "image": "i"})
    assert (a.id, b.id, c.id) == ("lab", "lab-2", "ubuntu-dev-2")
    assert d.id == "template"
    assert len({a.id, b.id, c.id, d.id}) == 4


def test_create_keeps_an_explicit_id_and_refuses_a_taken_one(tpls):
    spec = tpls.create({"id": "mine", "name": "Mine", "image": "i", "ttl_minutes": 60})
    assert spec.id == "mine" and spec.ttl_minutes == 60
    with pytest.raises(ApiError) as exc:
        tpls.create({"id": "mine", "name": "Again", "image": "i"})
    assert exc.value.status == 409 and exc.value.code == "conflict"


@pytest.mark.parametrize("data,needle", [
    ({"image": "i"}, "name is required"),
    ({"name": "N"}, "image is required"),
    ({"name": "N", "image": "  "}, "image is required"),
    ({"name": "N", "image": "a b"}, "image is required"),
    ({"name": "N" * 61, "image": "i"}, "60 characters or fewer"),
    ({"name": "N", "image": "i", "cpus": 0}, "cpus must be greater than 0"),
    ({"name": "N", "image": "i", "id": "Bad Id"}, "id must be lowercase"),
    ({"name": "N", "image": "i", "cpus": "many"}, "cpus must be greater than 0"),
])
def test_create_validation_messages_are_clear(tpls, data, needle):
    with pytest.raises(ApiError) as exc:
        tpls.create(data)
    assert exc.value.status == 400 and exc.value.code == "invalid"
    assert needle in exc.value.message


def test_create_rejects_non_objects(tpls):
    with pytest.raises(ApiError) as exc:
        tpls.create(["x"])
    assert exc.value.status == 400


def test_create_cannot_overwrite_a_pristine_builtin_flag(tpls):
    spec = tpls.create({"id": "my-box", "name": "N", "image": "i", "builtin": True})
    assert spec.builtin is False


# ---- agent templates are marked by the agent object

def test_agent_templates_are_told_apart_by_the_agent_object(tpls):
    from cloud_compute_core.agents import AGENT_TEMPLATES
    for t in AGENT_TEMPLATES:
        assert isinstance(t.agent, dict) and t.agent["enabled"] is True
        assert t.agent["mode"] in ("hermes", "script")
    for t in tpls.list():
        assert t.agent is None                               # container built-ins are not agents


# ---- wording

def test_user_facing_strings_do_not_say_we_or_i():
    import re
    from pathlib import Path
    import cloud_compute_core.templates as mod
    import cloud_compute_core.agents as agents
    for path in (Path(mod.__file__), Path(agents.__file__)):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for lit in re.findall(r'"([^"\n]{12,})"', line):
                if line.lstrip().startswith("#") or lit.startswith("cloud-compute:"):
                    continue
                assert not re.search(r"\b(we|We|I|I'm|I'll|our|Our)\b", lit), (path.name, no, lit)


# ---- through the real routes (the handlers call TemplateStore unchanged)

def _call(services, handler, template_id=None, body=None, query=None):
    from cloud_compute_core import instances_api
    from cloud_compute_core.interfaces import ApiRequest
    params = {"template_id": template_id} if template_id else {}
    return getattr(instances_api, handler)(
        ApiRequest(path_params=params, query=query or {}, body=body, services=services))


def test_routes_rename_reset_and_refuse_delete(services):
    renamed = _call(services, "patch_template", "python-dev", {"name": "  My Python  "})
    assert renamed["name"] == "My Python" and renamed["id"] == "python-dev"
    assert [t["name"] for t in _call(services, "list_templates") if t["id"] == "python-dev"] == ["My Python"]
    assert _call(services, "delete_template", "python-dev") == {"deleted": True}      # Reset to default
    assert _call(services, "get_template", "python-dev")["name"] == "Python dev"
    with pytest.raises(ApiError) as exc:
        _call(services, "delete_template", "python-dev")
    assert exc.value.status == 400 and exc.value.code == "invalid"
    assert "Reset to default restores it." in exc.value.message


def test_route_duplicate_then_rename(services):
    original = _call(services, "get_template", "ubuntu-dev")
    dup = dict(original, id="ubuntu-dev-copy", name="Ubuntu dev (copy)", builtin=False)
    created = _call(services, "create_template", body=dup)
    assert created["builtin"] is False
    renamed = _call(services, "patch_template", "ubuntu-dev-copy", {"name": "Build box"})
    assert renamed["name"] == "Build box"
    assert _call(services, "get_template", "ubuntu-dev")["name"] == "Ubuntu dev"


def test_route_patch_rejects_a_bad_name_with_a_clear_message(services):
    with pytest.raises(ApiError) as exc:
        _call(services, "patch_template", "ubuntu-dev", {"name": "x" * 61})
    assert exc.value.status == 400 and "60 characters or fewer" in exc.value.message
    assert _call(services, "get_template", "ubuntu-dev")["name"] == "Ubuntu dev"


def test_route_listing_shape_is_unchanged(services):
    keys = set(TemplateSpec(id="a", name="b", description="c").to_dict())
    for t in _call(services, "list_templates"):
        assert set(t) == keys
        for v in t["variables"]:
            assert set(v) == {"name", "source", "scope", "hosts", "description"}
