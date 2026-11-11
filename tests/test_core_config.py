"""Tests for cloud_compute_core.config."""

from __future__ import annotations

import copy
import logging
import re
import sys
import types

import pytest

from cloud_compute_core.config import CONFIG_FIELDS, Config, provide


def _fake_hermes_config(monkeypatch, data):
    mod = types.ModuleType("hermes_cli.config")
    mod.load_config_readonly = lambda: data
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", mod)


def test_defaults_match_spec():
    c = Config()
    assert c.to_dict() == {
        "liveness_seconds": 15, "poll_seconds": 60, "tunnel_ttl_minutes": 60,
        "default_terminal": "auto", "default_idle_minutes": 60, "default_ttl_minutes": 240,
        "chat_context": True, "chat_context_max_chars": 600, "approval": "always",
        "notify_job_complete": True, "ssh_path": "", "use_asyncssh": False,
        "expiry_warning_minutes": 15, "auto_delete_days": 0, "export_max_mb": 2048,
        "exports_dir": "", "agent_base_url": "", "agent_model": "", "wait_max_seconds": 300,
    }


def test_config_fields_mirror_dataclass():
    values = [n for n, m in CONFIG_FIELDS.items() if m["type"] != "secret"]
    assert values == list(Config().to_dict())
    for name, meta in CONFIG_FIELDS.items():
        if meta["type"] == "secret":            # kept by Hermes in the profile's secrets
            assert meta["env"] == "CC_AGENT_API_KEY" and meta["default"] == ""
            continue
        assert meta["default"] == getattr(Config(), name)
        assert meta["type"] in {"int", "bool", "str"}
        assert meta["description"]
        if meta["type"] == "int":
            assert meta["min"] <= meta["default"] <= meta["max"]
        if "choices" in meta:
            assert meta["default"] in meta["choices"]


@pytest.mark.parametrize("m", [None, "x", 5, [], {}])
def test_from_mapping_defaults(m):
    assert Config.from_mapping(m) == Config()


@pytest.mark.parametrize("key,low,high,lo_exp,hi_exp", [
    ("liveness_seconds", 1, 99999, 5, 3600),
    ("poll_seconds", 1, 99999, 30, 3600),
    ("tunnel_ttl_minutes", 0, 99999, 5, 1440),
    ("default_idle_minutes", -3, 99999, 5, 1440),
    ("default_ttl_minutes", 0, 99999, 5, 10080),
    ("chat_context_max_chars", 10, 99999, 100, 4000),
])
def test_from_mapping_clamps(key, low, high, lo_exp, hi_exp):
    assert getattr(Config.from_mapping({key: low}), key) == lo_exp
    assert getattr(Config.from_mapping({key: high}), key) == hi_exp
    mid = (lo_exp + hi_exp) // 2
    assert getattr(Config.from_mapping({key: mid}), key) == mid


def test_from_mapping_ignores_unknown_keys():
    c = Config.from_mapping({"nope": 1, "poll_seconds": 90})
    assert c.poll_seconds == 90
    assert not hasattr(c, "nope")


def test_from_mapping_coerces_strings():
    c = Config.from_mapping({
        "chat_context": "false", "notify_job_complete": "0", "use_asyncssh": "true",
        "poll_seconds": "120", "liveness_seconds": " 20 ",
    })
    assert c.chat_context is False
    assert c.notify_job_complete is False
    assert c.use_asyncssh is True
    assert c.poll_seconds == 120
    assert c.liveness_seconds == 20
    assert Config.from_mapping({"use_asyncssh": "1"}).use_asyncssh is True


def test_from_mapping_wrong_types_use_default():
    c = Config.from_mapping({
        "poll_seconds": "soon", "liveness_seconds": True, "tunnel_ttl_minutes": [1],
        "chat_context": "maybe", "use_asyncssh": 7, "ssh_path": 5, "default_terminal": None,
        "default_idle_minutes": 1.5, "default_ttl_minutes": None,
    })
    assert c == Config()


def test_from_mapping_integral_float_accepted():
    assert Config.from_mapping({"poll_seconds": 90.0}).poll_seconds == 90


def test_from_mapping_rejects_bad_choices():
    c = Config.from_mapping({"default_terminal": "powershell", "approval": "never"})
    assert c.default_terminal == "auto" and c.approval == "always"
    c = Config.from_mapping({"default_terminal": "cmd", "approval": "remote-only"})
    assert c.default_terminal == "cmd" and c.approval == "remote-only"


def test_ssh_path_is_kept():
    assert Config.from_mapping({"ssh_path": "C:/Windows/System32/OpenSSH/ssh.exe"}).ssh_path \
        == "C:/Windows/System32/OpenSSH/ssh.exe"


def test_load_reads_settings_without_mutating(monkeypatch):
    data = {"plugins": {"entries": {"cloud-compute": {"settings": {"poll_seconds": 120, "approval": "remote-only"}}}}}
    before = copy.deepcopy(data)
    _fake_hermes_config(monkeypatch, data)
    c = Config.load()
    assert c.poll_seconds == 120 and c.approval == "remote-only"
    assert data == before


def test_load_legacy_config_subtree(monkeypatch):
    data = {"plugins": {"entries": {"cloud-compute": {"config": {"liveness_seconds": 30}}}}}
    before = copy.deepcopy(data)
    _fake_hermes_config(monkeypatch, data)
    assert Config.load().liveness_seconds == 30
    assert data == before


def test_load_settings_win_over_legacy(monkeypatch):
    data = {"plugins": {"entries": {"cloud-compute": {
        "settings": {"liveness_seconds": 40}, "config": {"liveness_seconds": 30}}}}}
    _fake_hermes_config(monkeypatch, data)
    assert Config.load().liveness_seconds == 40


@pytest.mark.parametrize("data", [
    {}, None, "text", {"plugins": None}, {"plugins": {"entries": []}},
    {"plugins": {"entries": {"cloud-compute": "x"}}},
    {"plugins": {"entries": {"other": {"settings": {"poll_seconds": 99}}}}},
])
def test_load_missing_or_malformed_gives_defaults(monkeypatch, data):
    _fake_hermes_config(monkeypatch, data)
    assert Config.load() == Config()


def test_load_when_hermes_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    assert Config.load() == Config()


def test_load_when_hermes_raises(monkeypatch):
    mod = types.ModuleType("hermes_cli.config")

    def boom():
        raise RuntimeError("broken")

    mod.load_config_readonly = boom
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", mod)
    assert Config.load() == Config()


def test_provide_returns_loaded_config(monkeypatch):
    data = {"plugins": {"entries": {"cloud-compute": {"settings": {"poll_seconds": 77}}}}}
    _fake_hermes_config(monkeypatch, data)
    assert provide(None).poll_seconds == 77


def test_agent_endpoint_settings():
    meta = CONFIG_FIELDS["agent_base_url"], CONFIG_FIELDS["agent_model"]
    assert meta[0]["label"] == "Cloud agent model endpoint" and meta[1]["label"] == "Cloud agent model"
    assert all(m["type"] == "str" and m["default"] == "" for m in meta)
    c = Config.from_mapping({"agent_base_url": "  http://h:8080/v1 ", "agent_model": " m "})
    assert (c.agent_base_url, c.agent_model) == ("http://h:8080/v1", "m")
    c = Config.from_mapping({"agent_base_url": 5, "agent_model": None})
    assert (c.agent_base_url, c.agent_model) == ("", "")


# --------------------------------------------------------------------------- LiveConfig (no restart)


def _settings(data_values):
    return {"plugins": {"entries": {"cloud-compute": {"settings": dict(data_values)}}}}


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_live_config_follows_saved_settings(monkeypatch):
    from cloud_compute_core.config import LiveConfig

    saved = _settings({"agent_base_url": "", "agent_model": "", "poll_seconds": 60})
    _fake_hermes_config(monkeypatch, saved)             # load_config_readonly returns `saved`
    clock = _Clock()
    cfg = LiveConfig(min_interval=2.0, clock=clock)
    assert isinstance(cfg, Config)
    assert (cfg.agent_base_url, cfg.agent_model, cfg.poll_seconds) == ("", "", 60)

    # Settings -> Save writes the config file; the loader now returns new values.
    saved["plugins"]["entries"]["cloud-compute"]["settings"].update(
        agent_base_url="http://10.0.0.5:8080/v1", agent_model="qwen-test", poll_seconds=120)
    assert cfg.agent_model == ""                        # inside the 2 s window: not re-read yet
    clock.now += 2.0
    assert (cfg.agent_base_url, cfg.agent_model, cfg.poll_seconds) == (
        "http://10.0.0.5:8080/v1", "qwen-test", 120)    # same object, new values, no restart
    assert cfg.to_dict()["agent_model"] == "qwen-test"
    assert cfg == LiveConfig(min_interval=2.0, clock=clock)


def test_live_config_reload_is_immediate(monkeypatch):
    from cloud_compute_core.config import LiveConfig

    saved = _settings({"agent_model": "a"})
    _fake_hermes_config(monkeypatch, saved)
    cfg = LiveConfig(min_interval=3600.0, clock=_Clock())
    saved["plugins"]["entries"]["cloud-compute"]["settings"]["agent_model"] = "b"
    assert cfg.agent_model == "a"
    assert cfg.reload() is cfg
    assert cfg.agent_model == "b"


def test_live_config_reads_are_throttled(monkeypatch):
    from cloud_compute_core.config import LiveConfig

    calls = []
    clock = _Clock()

    def loader():
        calls.append(clock.now)
        return Config(poll_seconds=45)

    cfg = LiveConfig(loader=loader, min_interval=2.0, clock=clock)
    assert len(calls) == 1
    for _ in range(50):
        assert cfg.poll_seconds == 45 and cfg.approval == "always"
    assert len(calls) == 1
    clock.now += 1.9
    cfg.poll_seconds
    assert len(calls) == 1
    clock.now += 0.2
    cfg.poll_seconds
    assert len(calls) == 2


def test_live_config_keeps_last_good_values_when_the_read_fails():
    from cloud_compute_core.config import LiveConfig

    state = {"fail": False}
    clock = _Clock()

    def loader():
        if state["fail"]:
            raise RuntimeError("config file is being written")
        return Config(agent_model="good")

    cfg = LiveConfig(loader=loader, min_interval=0.0, clock=clock)
    state["fail"] = True
    assert cfg.agent_model == "good"
    state["fail"] = False
    assert cfg.agent_model == "good"


def test_live_config_without_hermes_gives_defaults(monkeypatch):
    from cloud_compute_core.config import LiveConfig

    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    assert LiveConfig().to_dict() == Config().to_dict()


def test_live_config_is_read_only():
    from cloud_compute_core.config import LiveConfig

    cfg = LiveConfig(loader=Config, min_interval=0.0)
    with pytest.raises(AttributeError):
        cfg.agent_model = "x"


def test_provide_returns_live_config(monkeypatch):
    from cloud_compute_core.config import LiveConfig

    saved = _settings({"approval": "remote-only"})
    _fake_hermes_config(monkeypatch, saved)
    cfg = provide(None)
    assert isinstance(cfg, LiveConfig) and cfg.approval == "remote-only"


# --------------------------------------------------------------------------- clamping is visible


@pytest.fixture
def fresh_warnings(monkeypatch):
    from cloud_compute_core import config as config_mod
    monkeypatch.setattr(config_mod, "_warned", set())
    return config_mod


def test_clamp_logs_a_warning_naming_field_given_and_applied(fresh_warnings, caplog):
    with caplog.at_level(logging.WARNING, logger=fresh_warnings.log.name):
        c = Config.from_mapping({"default_ttl_minutes": 3})
    assert c.default_ttl_minutes == 5
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(msgs) == 1
    assert "default_ttl_minutes" in msgs[0] and "3" in msgs[0] and "5" in msgs[0]


def test_clamp_warning_is_once_per_field_and_value(fresh_warnings, caplog):
    with caplog.at_level(logging.WARNING, logger=fresh_warnings.log.name):
        for _ in range(5):                       # LiveConfig re-reads every second
            Config.from_mapping({"default_ttl_minutes": 3})
        assert len(caplog.records) == 1
        Config.from_mapping({"default_ttl_minutes": 4})              # a new value warns again
        Config.from_mapping({"default_idle_minutes": 3})             # so does another field
        Config.from_mapping({"default_ttl_minutes": 99999999})       # and the upper bound
    names = [r.getMessage() for r in caplog.records]
    assert len(names) == 4
    assert "10080" in names[3]


def test_in_range_and_invalid_values_do_not_warn(fresh_warnings, caplog):
    with caplog.at_level(logging.WARNING, logger=fresh_warnings.log.name):
        Config.from_mapping({"default_ttl_minutes": 5, "default_idle_minutes": 1440,
                             "poll_seconds": "abc", "chat_context": "x"})
    assert not caplog.records


def test_clamped_map_reports_applied_values(fresh_warnings):
    c = Config.from_mapping({"default_ttl_minutes": 3, "poll_seconds": 90, "default_idle_minutes": 5000})
    assert dict(c.clamped) == {
        "default_ttl_minutes": {"given": 3, "applied": 5, "min": 5, "max": 10080},
        "default_idle_minutes": {"given": 5000, "applied": 1440, "min": 5, "max": 1440},
    }
    eff = c.effective()
    assert eff["settings"]["default_ttl_minutes"] == 5 and eff["clamped"]["default_ttl_minutes"]["given"] == 3
    assert Config().clamped == {} and Config().effective()["clamped"] == {}
    assert "clamped" not in Config().to_dict()          # a report, not a setting
    assert Config.from_mapping({"default_ttl_minutes": 3}) == Config.from_mapping({"default_ttl_minutes": 5})


def test_live_config_exposes_clamped_and_follows_changes(fresh_warnings):
    from cloud_compute_core.config import LiveConfig
    state = {"default_ttl_minutes": 3}
    live = LiveConfig(loader=lambda: Config.from_mapping(state), min_interval=3600)
    assert live.default_ttl_minutes == 5
    assert live.clamped["default_ttl_minutes"] == {"given": 3, "applied": 5, "min": 5, "max": 10080}
    assert live.effective()["clamped"]["default_ttl_minutes"]["applied"] == 5
    state["default_ttl_minutes"] = 30
    live.reload()
    assert live.default_ttl_minutes == 30 and dict(live.clamped) == {}


def test_int_descriptions_state_the_allowed_range():
    for name, meta in CONFIG_FIELDS.items():
        if meta["type"] != "int":
            continue
        assert f"({meta['min']}-{meta['max']})" in meta["description"], name
    assert "raised to 5" in CONFIG_FIELDS["default_ttl_minutes"]["description"]


def test_agent_base_url_example_is_https():
    text = CONFIG_FIELDS["agent_base_url"]["description"]
    assert "https://llm.example.net/v1" in text and "http://" not in text
    assert "private network" in text


def test_new_settings_ranges_and_terminal_choices():
    meta = CONFIG_FIELDS
    assert (meta["expiry_warning_minutes"]["default"], meta["expiry_warning_minutes"]["min"],
            meta["expiry_warning_minutes"]["max"]) == (15, 0, 1440)
    assert (meta["auto_delete_days"]["default"], meta["auto_delete_days"]["min"],
            meta["auto_delete_days"]["max"]) == (0, 0, 365)
    assert (meta["export_max_mb"]["default"], meta["export_max_mb"]["min"],
            meta["export_max_mb"]["max"]) == (2048, 10, 102400)
    assert meta["default_terminal"]["choices"] == ["auto", "wt", "cmd", "macos", "linux", "copy"]
    c = Config.from_mapping({"expiry_warning_minutes": 5000, "auto_delete_days": -1, "export_max_mb": 1,
                             "default_terminal": "macos"})
    assert (c.expiry_warning_minutes, c.auto_delete_days, c.export_max_mb) == (1440, 0, 10)
    assert c.default_terminal == "macos"
    for choice in meta["default_terminal"]["choices"]:
        assert Config.from_mapping({"default_terminal": choice}).default_terminal == choice


def test_default_terminal_description_labels_the_values_by_operating_system():
    from cloud_compute_core import config
    text = config.CONFIG_FIELDS["default_terminal"]["description"]
    for phrase in ("wt and cmd are for Windows", "macos is for macOS", "linux is for Linux",
                   "behaves like auto", "copy only puts the command on the clipboard"):
        assert phrase in text, phrase
