"""Strict-parse checks for plugin.yaml and dashboard/manifest.json.

A duplicate key is silently last-wins in most parsers (`yaml.safe_load`, `json.loads`), so a stray
second ``version:`` would change the manifest without any error. These tests fail on it instead.
When PyYAML is not importable, a line-based check of top-level and second-level keys is used.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_YAML = PLUGIN_DIR / "plugin.yaml"
DASHBOARD_MANIFEST = PLUGIN_DIR / "dashboard" / "manifest.json"

try:
    import yaml
except ImportError:                                  # the gate's environment may not have PyYAML
    yaml = None


# --- the checkers (tested on their own below) ---

def _json_duplicates(text: str) -> list[str]:
    dups: list[str] = []

    def hook(pairs):
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                dups.append(key)
            seen.add(key)
        return dict(pairs)

    json.loads(text, object_pairs_hook=hook)
    return dups


def _line_duplicates(text: str) -> list[str]:
    """Duplicate mapping keys at indent 0 and 2 (fallback when PyYAML is missing)."""
    dups: list[str] = []
    seen: dict[tuple, set[str]] = {}
    parent = ""
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        match = re.match(r"^(?:- )?([A-Za-z_][\w.-]*):(?:\s|$)", raw.lstrip(" "))
        if indent == 0 and match:
            key = match.group(1)
            parent = key
            bucket = seen.setdefault((), set())
        elif indent == 2 and match and not raw.lstrip().startswith("- "):
            key = match.group(1)
            bucket = seen.setdefault((parent,), set())
        else:
            continue
        if key in bucket:
            dups.append(key)
        bucket.add(key)
    return dups


def _yaml_duplicates(text: str) -> list[str]:
    if yaml is None:
        return _line_duplicates(text)
    dups: list[str] = []

    class StrictLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        seen: set = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                dups.append(str(key))
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    yaml.load(text, Loader=StrictLoader)             # noqa: S506 - SafeLoader subclass
    return dups


# --- the real files ---

def test_plugin_yaml_has_no_duplicate_keys():
    assert _yaml_duplicates(PLUGIN_YAML.read_text(encoding="utf-8")) == []


def test_plugin_yaml_has_no_duplicate_keys_by_line_check_too():
    assert _line_duplicates(PLUGIN_YAML.read_text(encoding="utf-8")) == []


def test_dashboard_manifest_has_no_duplicate_keys():
    assert _json_duplicates(DASHBOARD_MANIFEST.read_text(encoding="utf-8")) == []


def test_version_is_quoted():
    """An unquoted `version: 0.1` parses as a float and `0.1.0` as a string only by luck."""
    lines = [ln for ln in PLUGIN_YAML.read_text(encoding="utf-8").splitlines() if ln.startswith("version:")]
    assert len(lines) == 1
    assert re.fullmatch(r'version:\s*(".+"|\'.+\')\s*(#.*)?', lines[0]), lines[0]
    if yaml is not None:
        assert isinstance(yaml.safe_load(PLUGIN_YAML.read_text(encoding="utf-8"))["version"], str)


def test_any_40_hex_sha_in_plugin_yaml_is_quoted():
    """An unquoted all-digit sha such as 1234567890123456789012345678901234567890 parses as an int."""
    for number, line in enumerate(PLUGIN_YAML.read_text(encoding="utf-8").splitlines(), 1):
        for match in re.finditer(r"(?<![0-9a-fA-F\"'])[0-9a-f]{40}(?![0-9a-fA-F\"'])", line.split("#", 1)[0]):
            pytest.fail(f"plugin.yaml:{number}: unquoted 40-hex sha {match.group(0)[:8]}...")


# --- the checkers catch what they should ---

def test_yaml_checker_catches_duplicates():
    assert _yaml_duplicates("a: 1\nb: 2\na: 3\n") == ["a"]
    assert _yaml_duplicates("a:\n  x: 1\n  x: 2\n") == ["x"]
    assert _yaml_duplicates("a: 1\nb:\n  a: 2\n") == []


def test_line_checker_catches_duplicates():
    assert _line_duplicates("a: 1\nb: 2\na: 3\n") == ["a"]
    assert _line_duplicates("a:\n  x: 1\n  x: 2\n") == ["x"]
    assert _line_duplicates("a: 1\nb:\n  a: 2\n") == []
    assert _line_duplicates("l:\n  - name: x\n  - name: x\n") == []     # list items are separate maps


def test_json_checker_catches_duplicates():
    assert _json_duplicates('{"a": 1, "a": 2}') == ["a"]
    assert _json_duplicates('{"a": {"b": 1}, "b": {"b": 2}}') == []


def test_sha_check_pattern_flags_an_unquoted_sha():
    sha = "0dd6e058f" + "a" * 31
    assert re.search(r"(?<![0-9a-fA-F\"'])[0-9a-f]{40}(?![0-9a-fA-F\"'])", f"pin: {sha}")
    assert not re.search(r"(?<![0-9a-fA-F\"'])[0-9a-f]{40}(?![0-9a-fA-F\"'])", f'pin: "{sha}"')
