"""The guide skill matches the registered tools.

``skills/guide/SKILL.md`` is what a chat model reads to drive every tool. A name in it that the
tools do not accept sends the model into a loop of ``unknown_parameter`` errors, and an action
the guide does not mention is an action the model will not find. So the test reads the guide
against the real registered schemas:

- every registered tool has a ``### <tool>`` section, and no section names a tool that is not registered;
- each section's parameter table lists exactly the schema's properties, with the right
  required flag, and every enum value appears in the type cell;
- the action table lists exactly the actions the schema allows, and the parameters named
  in its "Needs" column exist;
- every ``tool(name=value, ...)`` example in a code block calls a registered tool with
  parameter names (and ``action=`` values) that exist;
- every tool-looking name in the prose is a registered tool.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "skills" / "guide" / "SKILL.md"
TOOL_NAME = re.compile(r"\b(?:compute|env|slurm|cloud_agent)_[a-z_]+\b")


def registered_tools() -> dict[str, dict]:
    """``{tool name: {"properties", "required"}}`` read from the real registrations."""
    from cloud_compute_core import registry

    found = registry.collect_tools()
    specs = dict(found) if isinstance(found, dict) else {t.name: t for t in found}
    out = {}
    for name, spec in specs.items():
        params = spec.schema.get("parameters", {})
        out[name] = {"properties": dict(params.get("properties", {})), "required": list(params.get("required", []))}
    return out


# --------------------------------------------------------------------------- parsing


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _tables(lines: list[str]) -> list[tuple[list[str], list[list[str]]]]:
    tables, i = [], 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header, rows, i = _cells(lines[i]), [], i + 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            tables.append((header, rows))
        else:
            i += 1
    return tables


def _names(cell: str) -> list[str]:
    return re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", cell)


def sections(text: str) -> dict[str, list[str]]:
    """``{tool name: lines}`` of the ``### <tool>`` sections under ``## Tools``."""
    out: dict[str, list[str]] = {}
    in_tools, current = False, None
    for line in text.splitlines():
        if line.startswith("## "):
            in_tools, current = line.strip() == "## Tools", None
        elif in_tools and line.startswith("### "):
            current = line[4:].strip()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return out


def _split_args(body: str) -> list[str]:
    """Top-level keyword names in the text between a call's parentheses."""
    names, depth, quote, token = [], 0, "", ""
    for ch in body + ",":
        if quote:
            token += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote, token = ch, token + ch
        elif ch in "([{":
            depth, token = depth + 1, token + ch
        elif ch in ")]}":
            depth, token = depth - 1, token + ch
        elif ch == "," and depth == 0:
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", token)
            if m:
                names.append(token.strip())
            token = ""
        else:
            token += ch
    return names


def calls(text: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Every ``tool(...)`` call in a fenced code block: ``(tool name, [(param, value text)])``."""
    found = []
    for block in re.findall(r"```\n(.*?)```", text, re.S):
        for line in block.splitlines():
            line = line.split("   #")[0].rstrip()
            m = re.match(r"^\s*([a-z_]+)\((.*)\)\s*$", line)
            if not m:
                continue
            pairs = []
            for arg in _split_args(m.group(2)):
                key, _, value = arg.partition("=")
                pairs.append((key.strip(), value.strip().strip("\"'")))
            found.append((m.group(1), pairs))
    return found


# --------------------------------------------------------------------------- the check


def check_guide(text: str, tools: dict[str, dict]) -> list[str]:
    problems: list[str] = []
    secs = sections(text)
    for name in tools:
        if name not in secs:
            problems.append(f"tool {name} has no section in the guide")
    for name in secs:
        if name not in tools:
            problems.append(f"the guide has a section for {name}, which is not a registered tool")

    for name, lines in secs.items():
        if name not in tools:
            continue
        props, required = tools[name]["properties"], set(tools[name]["required"])
        tables = _tables(lines)
        param_tables = [t for t in tables if t[0] and t[0][0] == "Parameter"]
        action_tables = [t for t in tables if t[0] and t[0][0].startswith("Action")]
        if len(param_tables) != 1:
            problems.append(f"{name}: expected one parameter table, found {len(param_tables)}")
            continue
        header, rows = param_tables[0]
        listed = {}
        for row in rows:
            names = _names(row[0])
            if len(names) != 1:
                problems.append(f"{name}: parameter row without one `name`: {row[0]!r}")
                continue
            listed[names[0]] = row
        for p in sorted(set(props) - set(listed)):
            problems.append(f"{name}: parameter {p} is registered but missing from the guide")
        for p in sorted(set(listed) - set(props)):
            problems.append(f"{name}: the guide lists parameter {p}, which the tool does not have")
        for p, row in listed.items():
            if p not in props:
                continue
            is_required = row[2].strip().lower() == "yes"
            if is_required != (p in required):
                problems.append(f"{name}: {p} required flag is {row[2]!r} but the schema says "
                                f"{'required' if p in required else 'optional'}")
            for value in props[p].get("enum", []):
                if f"`{value}`" not in row[1]:
                    problems.append(f"{name}: {p} value {value} is missing from its type cell")

        enum_action = [p for p in ("action",) if "enum" in props.get(p, {})]
        if enum_action and not action_tables:
            problems.append(f"{name}: no action table")
        for header, rows in action_tables:
            m = re.search(r"`([A-Za-z_]+)`", header[0])
            param = m.group(1) if m else ""
            if param not in props or "enum" not in props[param]:
                problems.append(f"{name}: the action table is about {param!r}, which is not an enum parameter")
                continue
            documented = {n for row in rows for n in _names(row[0])[:1]}
            for a in sorted(set(props[param]["enum"]) - documented):
                problems.append(f"{name}: action {a} is registered but missing from the guide")
            for a in sorted(documented - set(props[param]["enum"])):
                problems.append(f"{name}: the guide documents action {a}, which does not exist")
            for row in rows:
                for n in _names(row[1]):
                    if n not in props:
                        problems.append(f"{name}: action {row[0]} needs {n}, which is not a parameter")

    for tool, pairs in calls(text):
        if tool not in tools:
            problems.append(f"example calls {tool}(...), which is not a registered tool")
            continue
        props = tools[tool]["properties"]
        for key, value in pairs:
            if key not in props:
                problems.append(f"example {tool}(...) uses parameter {key}, which does not exist")
            elif "enum" in props[key] and value not in props[key]["enum"]:
                problems.append(f"example {tool}({key}={value}) is not one of {props[key]['enum']}")

    for word in sorted(set(TOOL_NAME.findall(text))):
        if word not in tools:
            problems.append(f"the text names {word}, which is not a registered tool")
    return problems


# --------------------------------------------------------------------------- tests


@pytest.fixture(scope="module")
def tools() -> dict[str, dict]:
    return registered_tools()


@pytest.fixture(scope="module")
def guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def test_every_tool_action_and_parameter_in_the_guide_exists_and_every_one_is_documented(tools, guide):
    assert check_guide(guide, tools) == []


def test_the_guide_covers_all_the_tools_the_plugin_registers(tools, guide):
    assert {"compute_status", "compute_host", "env_spawn", "env_exec", "env_control", "env_files", "env_open",
            "slurm_submit", "slurm_jobs", "cloud_agent_spawn", "cloud_agent_status", "cloud_agent_collect",
            "cloud_agent_stop", "cloud_agent_message"} <= set(tools)
    assert set(sections(guide)) == set(tools)


def test_the_guide_has_the_frontmatter_hermes_needs(guide):
    assert guide.startswith("---\nname: guide\ndescription: ")


def test_the_guide_names_the_usual_order_of_calls_and_the_common_errors(guide):
    for text in ("The usual order of calls", "Set up a machine", "Work in a sandbox", "Run a Slurm job",
                 "Run a cloud agent", "`unknown_parameter`", "`not_found`", "`nothing_to_save`",
                 "`missing`"):
        assert text in guide, text


# A wrong name must fail the check. Each mutation below breaks the real guide in one way.


def _mutated(guide: str, old: str, new: str, count: int = 1) -> str:
    assert old in guide, f"the mutation target {old!r} is not in the guide"
    return guide.replace(old, new, count)


def test_a_misspelled_parameter_fails(tools, guide):
    bad = _mutated(guide, "| `wait_s` |", "| `wait_secs` |")
    problems = check_guide(bad, tools)
    assert any("wait_secs" in p for p in problems), problems
    assert any("wait_s is registered but missing" in p for p in problems), problems


def test_a_wrong_tool_name_in_the_text_fails(tools, guide):
    bad = _mutated(guide, "`compute_status`", "`compute_state`")
    assert any("compute_state" in p for p in check_guide(bad, tools))


def test_a_wrong_parameter_in_an_example_fails(tools, guide):
    bad = _mutated(guide, 'env_exec(instance_id="i-1a2b3c4d", command="pytest -q")', 'env_exec(instance="i-1a2b3c4d", command="pytest -q")')
    assert any("env_exec(...) uses parameter instance" in p for p in check_guide(bad, tools))


def test_a_wrong_action_value_in_an_example_fails(tools, guide):
    bad = _mutated(guide, 'slurm_jobs(action="cancel", job_id="j-1a2b3c4d")', 'slurm_jobs(action="kill", job_id="j-1a2b3c4d")')
    assert any("action=kill" in p for p in check_guide(bad, tools))


def test_a_registered_action_missing_from_the_guide_fails(tools, guide):
    lines = [l for l in guide.splitlines() if not l.startswith("| `delete` | `job_id` |")]
    assert len(lines) == len(guide.splitlines()) - 1
    problems = check_guide("\n".join(lines), tools)
    assert any("action delete is registered but missing" in p for p in problems), problems


def test_an_action_the_tool_does_not_have_fails(tools, guide):
    bad = _mutated(guide, "| `delete_export` | `export_id` |", "| `remove_export` | `export_id` |")
    problems = check_guide(bad, tools)
    assert any("documents action remove_export" in p for p in problems), problems
    assert any("action delete_export is registered but missing" in p for p in problems), problems


def test_a_tool_without_a_section_fails(tools, guide):
    bad = guide.replace("### cloud_agent_stop\n", "### cloud_agent_halt\n")
    problems = check_guide(bad, tools)
    assert any("cloud_agent_stop has no section" in p for p in problems), problems
    assert any("cloud_agent_halt" in p for p in problems), problems


def test_a_wrong_required_flag_fails(tools, guide):
    bad = _mutated(guide, "| `host_id` | string | yes | Slurm host id from compute_status. |", "| `host_id` | string | no | Slurm host id from compute_status. |")
    assert any("slurm_submit: host_id required flag" in p for p in check_guide(bad, tools))
