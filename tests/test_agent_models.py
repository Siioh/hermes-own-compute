"""Model endpoints detected from the Hermes profile (core/agent_models.py).

Fake config objects only: no real Hermes config, no network."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from cloud_compute_core import agent_models, agents_api
from cloud_compute_core.agent_models import REASONS, ProfileApi, detect
from cloud_compute_core.interfaces import ApiRequest

INLINE_KEY = "inline-key-value-must-never-appear-1234567890"
SECRET = "env-key-value-must-never-appear-0987654321"


def api(config, customs=None, profiles=None):
    profiles = profiles or {}
    return ProfileApi(
        load_config=lambda: config,
        custom_providers=lambda cfg: list(customs or []),
        provider_profile=lambda name: profiles.get(name))


def profile(auth_type="api_key", env_vars=(), base_url="", display_name=""):
    return SimpleNamespace(auth_type=auth_type, env_vars=env_vars, base_url=base_url,
                           display_name=display_name)


def is_set(names=()):
    return lambda name: name in names


def test_a_custom_endpoint_with_a_key_name_is_listed_with_its_models():
    customs = [{"name": "baseten", "base_url": "https://inference.baseten.co/v1",
                "key_env": "BASETEN_API_KEY", "model": "deepseek-ai/DeepSeek-V4.1-Flash-Fast",
                "models": {"deepseek-ai/DeepSeek-V4.1-Flash-Fast": {}, "other/model": {}}}]
    out = detect(api({}, customs), is_set({"BASETEN_API_KEY"}))
    assert out["unavailable"] == []
    [item] = out["detected"]
    assert item["id"].startswith("d-") and len(item["id"]) == 10
    assert (item["name"], item["source"], item["base_url"], item["endpoint_host"]) == (
        "baseten", "custom_provider", "https://inference.baseten.co/v1", "inference.baseten.co")
    assert item["models"] == ["deepseek-ai/DeepSeek-V4.1-Flash-Fast", "other/model"]
    assert item["model"] == "deepseek-ai/DeepSeek-V4.1-Flash-Fast"
    assert (item["key_secret"], item["key_set"]) == ("BASETEN_API_KEY", True)
    assert "note" not in item
    again = detect(api({}, customs), is_set())
    assert again["detected"][0]["key_set"] is False and again["detected"][0]["id"] == item["id"]


def test_inline_keys_key_commands_and_missing_keys_are_unavailable_with_plain_reasons():
    customs = [
        {"name": "inline", "base_url": "https://a.example.net/v1", "api_key": INLINE_KEY},
        {"name": "cmd", "base_url": "https://b.example.net/v1", "key_cmd": "pass show llm"},
        {"name": "nokey", "base_url": "https://c.example.net/v1"},
        {"name": "badurl", "base_url": "https://user:pw@d.example.net/v1", "key_env": "K"},
    ]
    out = detect(api({}, customs), is_set({"K"}))
    assert out["detected"] == []
    kinds = {u["name"]: (u["kind"], u["reason"], u["endpoint_host"]) for u in out["unavailable"]}
    assert kinds["inline"] == ("inline_key", REASONS["inline_key"], "a.example.net")
    assert kinds["cmd"] == ("key_command", REASONS["key_command"], "b.example.net")
    assert kinds["nokey"] == ("no_key", REASONS["no_key"], "c.example.net")
    assert kinds["badurl"][0] == "bad_address" and kinds["badurl"][2] == ""
    assert INLINE_KEY not in repr(out) and "pass show" not in repr(out)


def test_a_sign_in_provider_is_unavailable_with_the_sentence_users_read():
    config = {"model": {"provider": "nous", "default": "hermes-4"}}
    profiles = {"nous": profile("oauth_device_code", base_url="https://inference.example.net/v1",
                                display_name="Nous Portal")}
    out = detect(api(config, profiles=profiles), is_set())
    assert out["detected"] == []
    [item] = out["unavailable"]
    assert item["kind"] == "sign_in" and item["name"] == "Nous Portal"
    assert item["reason"] == ("Uses a sign-in, which cannot be sent to a remote sandbox. Cloud "
                              "agents need an API-key provider or a model server you run.")


def test_the_main_model_of_an_api_key_provider_names_the_key_variable_that_is_set():
    config = {"model": {"provider": "openrouter", "default": "anthropic/claude-x"}}
    profiles = {"openrouter": profile(env_vars=("OPENROUTER_API_KEY", "OR_ALT_KEY"),
                                      base_url="https://openrouter.ai/api/v1",
                                      display_name="OpenRouter")}
    [item] = detect(api(config, profiles=profiles), is_set({"OR_ALT_KEY"}))["detected"]
    assert (item["name"], item["source"], item["key_secret"], item["key_set"]) == (
        "OpenRouter", "profile_model", "OR_ALT_KEY", True)
    assert item["models"] == ["anthropic/claude-x"] and item["endpoint_host"] == "openrouter.ai"
    [item] = detect(api(config, profiles=profiles), is_set())["detected"]
    assert (item["key_secret"], item["key_set"]) == ("OPENROUTER_API_KEY", False)


def test_the_main_model_on_a_custom_endpoint_uses_its_key_env_and_a_local_address_is_noted():
    config = {"model": {"provider": "custom", "default": "qwen3", "base_url": "http://127.0.0.1:8080/v1",
                        "key_env": "LOCAL_LLM_KEY"}}
    [item] = detect(api(config), is_set({"LOCAL_LLM_KEY"}))["detected"]
    assert item["name"] == "Main model" and item["models"] == ["qwen3"]
    assert item["key_secret"] == "LOCAL_LLM_KEY" and item["note"] == agent_models.LOCAL_NOTE


def test_the_same_endpoint_and_key_is_listed_once_and_bad_names_are_dropped():
    config = {"model": {"provider": "custom", "default": "m1", "base_url": "https://x.example.net/v1",
                        "key_env": "K"}}
    customs = [{"name": "dup", "base_url": "https://x.example.net/v1/", "key_env": "K",
                "model": "m2"},
               {"name": "weird", "base_url": "https://y.example.net/v1", "key_env": "not a name"}]
    out = detect(api(config, customs), is_set({"K"}))
    assert [d["name"] for d in out["detected"]] == ["Main model"]
    assert [u["kind"] for u in out["unavailable"]] == ["no_key"]


def test_an_unreadable_profile_offers_nothing_and_never_raises():
    def boom():
        raise RuntimeError("no config")
    assert detect(ProfileApi(boom, lambda c: [], lambda n: None), is_set()) == {
        "detected": [], "unavailable": []}
    assert detect(api("not a dict"), is_set()) == {"detected": [], "unavailable": []}


def test_no_key_value_is_ever_read_into_the_result():
    customs = [{"name": "e", "base_url": "https://e.example.net/v1", "key_env": "E_KEY"},
               {"name": "i", "base_url": "https://i.example.net/v1", "api_key": INLINE_KEY}]
    calls = []

    def secret_is_set(name):
        calls.append(name)
        return True

    out = detect(api({}, customs), secret_is_set)
    assert calls == ["E_KEY"] and SECRET not in repr(out) and INLINE_KEY not in repr(out)


# --------------------------------------------------------------------------- through the manager


def _load(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


_am = _load("cloud_compute_tests_agents_manager", "test_agents_manager.py")
env = _am.env  # noqa: F811 - the shared agent fixture


def test_the_route_lists_detected_and_unavailable_and_a_detected_choice_goes_through_approval(env):
    e = env
    customs = [{"name": "baseten", "base_url": "https://inference.baseten.co/v1",
                "key_env": _am.KEY_NAME, "model": "deepseek-ai/V4"},
               {"name": "signin", "base_url": "https://s.example.net/v1", "api_key": INLINE_KEY}]
    e.mgr._profile_api = lambda: api({}, customs)
    e.secrets.set(_am.KEY_NAME, SECRET)
    e.services.set("agents", e.mgr)
    handler = next(r.handler for r in agents_api.routes() if (r.method, r.path) == ("GET", "/agent-models"))
    out = handler(ApiRequest(path_params={}, query={}, body=None, services=e.services))
    assert [d["name"] for d in out["detected"]] == ["baseten"]
    assert [u["kind"] for u in out["unavailable"]] == ["inline_key"]
    assert out["models"] == [] and out["default_id"] is None
    assert SECRET not in repr(out) and INLINE_KEY not in repr(out)
    # using it = saving it as a model choice; the run's approval names the host and the key NAME
    d = out["detected"][0]
    saved = e.mgr.save_model({"name": d["name"], "base_url": d["base_url"], "model": d["model"],
                              "key_secret": d["key_secret"]})
    text = e.mgr.describe_spawn({"task": "t", "host_id": "x1carbon", "repo_path": str(e.repo),
                                 "model_id": saved.id})
    assert "model deepseek-ai/V4 at inference.baseten.co" in text
    assert f"read from the secret {_am.KEY_NAME}" in text and SECRET not in text


def test_without_hermes_the_manager_reports_nothing(env):
    e = env

    def no_hermes():
        raise ImportError("no hermes_cli")
    e.mgr._profile_api = no_hermes
    assert e.mgr.detected_models() == [] and e.mgr.unavailable_models() == []


def test_rerun_may_pick_an_endpoint_the_profile_defines_but_no_other(env):
    e = env
    e.mgr._profile_api = lambda: api({}, [
        {"name": "baseten", "base_url": "https://inference.baseten.co/v1",
         "key_env": _am.KEY_NAME, "model": "deepseek-ai/V4"}])
    e.secrets.set(_am.KEY_NAME, SECRET)
    # the same resolution a rerun uses
    cfg = {"base_url": None, "model": None, "key_secret": "CC_AGENT_API_KEY"}
    chosen = e.mgr._resolve_model(cfg, None, {"base_url": "https://inference.baseten.co/v1/",
                                              "model": ""})
    assert (chosen["base_url"], chosen["model"], chosen["key_secret"], chosen["model_id"]) == (
        "https://inference.baseten.co/v1", "deepseek-ai/V4", _am.KEY_NAME, None)
    chosen = e.mgr._resolve_model(cfg, None, {"base_url": "https://inference.baseten.co/v1",
                                              "model": "other/model"})
    assert chosen["model"] == "other/model" and chosen["key_secret"] == _am.KEY_NAME
    from cloud_compute_core.interfaces import ApiError
    import pytest as _pytest
    with _pytest.raises(ApiError) as info:
        e.mgr._resolve_model(cfg, None, {"base_url": "https://evil.example.net/v1", "model": "m"})
    assert info.value.code == "invalid" and "evil.example.net" in info.value.message
    assert "saved or is defined in the profile" in info.value.message
    # the approval card names the endpoint host and the key NAME
    text = e.mgr.describe_spawn({"task": "t", "host_id": "x1carbon", "repo_path": str(e.repo)},
                                endpoint={"base_url": "https://inference.baseten.co/v1",
                                          "model": ""})
    assert "model deepseek-ai/V4 at inference.baseten.co" in text
    assert f"read from the secret {_am.KEY_NAME}" in text and SECRET not in text
