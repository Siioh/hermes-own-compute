"""A chat card directive in a tool result always has a blank line before and after it.

Desktop draws ``::cc-...{id="..."}`` only when it is a block of its own, and the plugin cannot
change how Desktop reads message text. So the tool result's ``card`` field carries the blank
lines already: a model that pastes the field as it is gets a block.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _sibling(name: str, module_name: str):
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name(name))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[module_name]


_reg = _sibling("test_integration_register.py", "cloud_compute_tests_integration_register")
plugin, bg_calls = _reg.plugin, _reg.bg_calls                 # noqa: F811


def test_card_fields_are_padded_at_every_depth(plugin):
    result = {"card": '::cc-env{id="i-ab12cd34"}', "copy_card": '::cc-copy{id="x-0000abcd"}',
              "instances": [{"id": "i-1", "card": '::cc-env{id="i-1"}'}],
              "note": '::cc-env{id="i-9"} stays in a note', "card_text": "not a card"}
    out = plugin._pad_cards(result)
    assert out["card"] == '\n\n::cc-env{id="i-ab12cd34"}\n\n'
    assert out["copy_card"] == '\n\n::cc-copy{id="x-0000abcd"}\n\n'
    assert out["instances"][0]["card"] == '\n\n::cc-env{id="i-1"}\n\n'
    assert out["note"] == result["note"] and out["card_text"] == "not a card"
    assert result["card"] == '::cc-env{id="i-ab12cd34"}'                      # the input is not changed
    assert plugin._pad_cards({"card": "not a directive"}) == {"card": "not a directive"}
    assert plugin._pad_cards("text") == "text"


def test_the_tool_handler_returns_the_padded_card(plugin):
    spec = SimpleNamespace(name="probe", handler=lambda args, services, ctx: {
        "ok": True, "card": '::cc-host{id="x1carbon"}'})
    plugin._services = lambda: object()
    plugin._ensure_background = lambda: None
    raw = plugin._make_handler(spec)({})
    data = json.loads(raw)
    assert data["card"] == '\n\n::cc-host{id="x1carbon"}\n\n'
    assert "card_hint" in data                                  # still carries the hint
