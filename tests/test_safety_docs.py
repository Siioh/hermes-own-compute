"""Every sentence in the guide and the Help topics matches what the code does, and the
sentences that were wrong before are gone."""

from __future__ import annotations

from pathlib import Path

from cloud_compute_core import approval, docs_content, hosts_api

GUIDE = (Path(__file__).resolve().parents[1] / "skills" / "guide" / "SKILL.md").read_text(
    encoding="utf-8")
HELP = "\n".join(t["body_md"] for t in docs_content.TOPICS)


def test_the_false_shell_claims_are_gone():
    for gone in ("Tools never run commands in a host's shell",
                 "Your host's shell is never driven by the model",
                 "never placed in a command line, a log or a chat message",
                 "Nothing is deleted automatically unless"):
        assert gone not in HELP


def test_help_says_the_prefix_is_page_only_and_that_a_new_address_asks():
    assert "command prefix can only be set on" in HELP
    assert "testing an address that is not saved" in HELP
    assert "cloud agent's sandbox" in HELP
    assert "failed start" in HELP                       # the reaper also removes these, after saving


def test_help_describes_the_delete_folder_and_the_redaction_limit():
    assert "the one the environment was created with" in HELP
    assert "not when a command changes it first" in HELP


def test_the_guide_states_what_is_page_only_and_when_a_test_asks():
    assert hosts_api.PAGE_ONLY_MESSAGE in GUIDE
    assert "exec_prefix" in GUIDE and "interactive_prefix" in GUIDE
    assert "| `exec_prefix` |" not in GUIDE             # not a tool parameter any more
    assert "test` of an address that is not saved" in GUIDE


def test_the_guide_carries_the_agent_sandbox_refusal_verbatim():
    assert approval.AGENT_SANDBOX_MESSAGE in GUIDE


def test_the_guide_env_open_port_row_is_true_for_a_host():
    assert "the template must publish it" in GUIDE and "only with `host_id`" in GUIDE
    assert "any loopback port of that ssh host" in GUIDE


def test_the_guide_upload_row_names_the_whole_hermes_folder():
    assert "the whole Hermes folder" in GUIDE and "two backslashes" in GUIDE


def test_the_guide_delete_row_names_the_recorded_folder():
    assert "the work folder the environment was created with" in GUIDE


def test_the_approval_docstring_no_longer_claims_the_same_rule_as_the_files_describer():
    source = Path(approval.__file__).read_text(encoding="utf-8")
    assert "the same rule as files_api._fetch_approval" not in source
