"""Every chat card line the backends emit has a card in the desktop half.

The tools put ``::cc-<kind>{id="..."}`` in their results; Desktop only draws a directive that
``desktop/plugin.js`` registered. This test reads the code (no node needed): any ``::cc-`` kind
named in ``core/`` or passed to a ``_card("<kind>", ...)`` helper must be registered there.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
PLUGIN_JS = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")


def _emitted_kinds() -> set[str]:
    kinds: set[str] = set()
    for path in sorted(CORE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name == "docs_content.py":
            continue
        for match in re.finditer(r"::cc-([a-z]+)\{", text):
            kinds.add(match.group(1))
        # hosts_api builds the line from a kind: _card("host", id), _card("env", id) ...
        for match in re.finditer(r"""_card\(\s*["']([a-z]+)["']\s*,""", text):
            kinds.add(match.group(1))
    return kinds


def _registered_kinds() -> set[str]:
    block = re.search(r"const CARD_KINDS = \{(.*?)\n\}\n", PLUGIN_JS, re.S)
    assert block, "CARD_KINDS is missing from desktop/plugin.js"
    kinds = {m.group(1) for m in re.finditer(r"directive: 'cc-([a-z]+)'", block.group(1))}
    # the environment card keeps its own registration
    if re.search(r"name: 'cc-env'", PLUGIN_JS):
        kinds.add("env")
    return kinds


def test_core_emits_at_least_the_known_card_kinds():
    assert {"env", "job", "agent", "copy", "host"} <= _emitted_kinds()


def test_every_card_line_the_backends_emit_has_a_registered_card():
    missing = _emitted_kinds() - _registered_kinds()
    assert not missing, f"no card is registered for: {sorted(missing)}"


def test_each_registered_card_is_wired_to_a_component_and_a_rest_route():
    for kind, route in (("job", "/jobs/{id}"), ("agent", "/agents/{id}"), ("copy", "/exports/{id}"),
                        ("host", "/hosts/{id}")):
        assert f"directive: 'cc-{kind}'" in PLUGIN_JS, kind
        assert f"{{ method: 'GET', path: '{route}' }}" in PLUGIN_JS, route
