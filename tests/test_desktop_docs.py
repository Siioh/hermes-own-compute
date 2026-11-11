"""Help topics, UI copy and the /docs routes (Desktop area)."""

import importlib
import importlib.machinery
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = PLUGIN_DIR / "core"
PKG_NAME = "cloud_compute_core"


def _core_package():
    """Return the ``cloud_compute_core`` package (provided by conftest, or built here)."""
    pkg = sys.modules.get(PKG_NAME)
    if pkg is not None:
        return pkg
    pkg = types.ModuleType(PKG_NAME)
    spec = importlib.machinery.ModuleSpec(PKG_NAME, loader=None, is_package=True)
    spec.submodule_search_locations = [str(CORE_DIR)]
    pkg.__spec__ = spec
    pkg.__path__ = [str(CORE_DIR)]
    pkg.__package__ = PKG_NAME
    sys.modules[PKG_NAME] = pkg
    return pkg


_core_package()
docs_content = importlib.import_module(PKG_NAME + ".docs_content")
docs_api = importlib.import_module(PKG_NAME + ".docs_api")


def _interfaces():
    return importlib.import_module(PKG_NAME + ".interfaces")


def _req(**path_params):
    return SimpleNamespace(path_params=path_params, query={}, body=None, services=None)


EXPECTED_TOPICS = [
    ("getting-started", "Getting started"),
    ("ssh-host", "SSH hosts"),
    ("slurm", "Slurm login nodes"),
    ("local", "Local Docker/Podman"),
    ("ec2", "EC2"),
    ("templates", "Environment templates"),
    ("keeping-data", "Keeping your data"),
    ("display", "Remote screen"),
    ("terminal", "Terminals and ports"),
    ("chat", "Using Own Compute from chat"),
    ("agents", "Cloud agents"),
    ("security", "Security and privacy"),
    ("troubleshooting", "Troubleshooting"),
]

EXPECTED_COPY = {
    "type_cards": {
        "ssh": {
            "kind": "ssh",
            "title": "SSH host",
            "one_liner": "Run sandboxed containers on a Linux machine you can SSH into.",
            "when_to_use": "Your own laptop, workstation, homelab or VM.",
            "learn_more_topic": "ssh-host",
            "mvp": True,
            "capabilities": {
                "containers": "yes",
                "batch_jobs": "no",
                "interactive_terminal": "yes",
                "display": "partial",
                "port_forward": "yes",
                "gpu": "partial",
                "agents": "partial",
                "costs_money": "no",
            },
            "capability_notes": {
                "display": "Needs the display image on the host; the Screen button offers to build it.",
                "gpu": "If the host has a GPU that Docker or Podman can use.",
                "agents": "Off until allowed in the host's sandbox defaults.",
            },
        },
        "slurm": {
            "kind": "slurm",
            "title": "Slurm login node",
            "one_liner": "Submit batch jobs to an HPC cluster through its login node.",
            "when_to_use": "HPC or lab clusters, sweeps, scheduled work.",
            "learn_more_topic": "slurm",
            "mvp": True,
            "capabilities": {
                "containers": "no",
                "batch_jobs": "yes",
                "interactive_terminal": "partial",
                "display": "no",
                "port_forward": "no",
                "gpu": "partial",
                "agents": "no",
                "costs_money": "partial",
            },
            "capability_notes": {
                "interactive_terminal": "A shell on the login node, or inside a running job.",
                "gpu": "If your cluster has GPUs.",
                "costs_money": "Only if your site bills for cluster time.",
            },
        },
        "local": {
            "kind": "local",
            "title": "Local Docker/Podman",
            "one_liner": "Run containers on this computer.",
            "when_to_use": "Quick tests without another machine.",
            "learn_more_topic": "local",
            "mvp": True,
            "capabilities": {
                "containers": "yes",
                "batch_jobs": "no",
                "interactive_terminal": "yes",
                "display": "partial",
                "port_forward": "yes",
                "gpu": "partial",
                "agents": "partial",
                "costs_money": "no",
            },
            "capability_notes": {
                "display": "Needs the display image on this computer; the Screen button offers to build it.",
                "port_forward": "Forwards the screen and any ports a template publishes.",
                "gpu": "If Docker or Podman GPU access works on this computer.",
                "agents": "Off until allowed in the host's sandbox defaults.",
            },
        },
        "ec2": {
            "kind": "ec2",
            "title": "EC2",
            "one_liner": "Start an AWS machine on demand and run containers on it like an SSH host.",
            "when_to_use": "You need more CPU than you own, for a few hours, with a time limit.",
            "learn_more_topic": "ec2",
            "mvp": True,
            "capabilities": {
                "containers": "yes",
                "batch_jobs": "no",
                "interactive_terminal": "yes",
                "display": "partial",
                "port_forward": "yes",
                "gpu": "partial",
                "agents": "partial",
                "costs_money": "yes",
            },
            "capability_notes": {
                "display": "Needs about 2 GB of memory; a 1 GB instance type is too small for it.",
                "gpu": "Only with a GPU instance type that the AWS account's quota allows.",
                "agents": "Off until allowed in the host's sandbox defaults.",
                "costs_money": "Billed by AWS while the machine runs, and a little for its disk while it is stopped.",
            },
        },
    },
    "wizard_hints": {
        "endpoint": "Use an alias from your SSH config if you have one. Example: gpu-box.",
        "login": "Uses your SSH agent or the key file you choose. The key itself is never stored.",
        "test_connection": "Checks DNS, port 22, login, the container runtime or scheduler, display support and GPUs.",
        "sandbox_defaults": "Limits apply to environments the plugin starts on this host.",
        "jump_host": "Optional. A gateway server to connect through (ssh -J), as [user@]host[:port]. Leave empty for a direct connection.",
        "partition": "Optional. The Slurm partition for jobs that do not name one (sbatch -p). Leave empty to use the cluster default.",
        "account": "Optional. The Slurm account added to every job (sbatch -A). Leave empty if the cluster does not ask for one.",
        "qos": "Optional. The quality-of-service level added to every job (sbatch --qos). Leave empty unless the site asks for one.",
        "workdir": (
            "Where each job gets its own folder, holding its script and output. Jobs start there, "
            "but it is not a sandbox: they run as you and can reach anything your account can. "
            "The folder must be on storage the compute nodes also see (shared storage). Home folders are not shared on every cluster, and job output cannot be read from a folder that is not. Test connection checks this."
        ),
        "exec_prefix": (
            "Advanced. Added before every command run on this host. "
            "Only for a scheduler inside a container, for example podman exec -i slurmctld. "
            "It can only be set here: the chat tool refuses it."
        ),
        "interactive_prefix": (
            "Advanced. Used instead of the command prefix for terminal sessions that need a TTY, "
            "for example podman exec -it -w /data slurmctld. "
            "It can only be set here: the chat tool refuses it."
        ),
        "slurm_agents": (
            "Cloud agents are off for Slurm hosts: an agent needs a long-running container, "
            "and most clusters do not allow long-running processes on login nodes."
        ),
    },
    "tooltips": {
        "screen": "Opens the sandbox desktop in a tab. Only for templates with a display.",
        "screen_disabled": "This host type can't show a screen.",
        "terminal": (
            "Opens a terminal window on this computer, inside this environment. "
            "Windows Terminal or Command Prompt on Windows, Terminal on macOS, your usual terminal on Linux."
        ),
        "terminal_fallback": "Couldn't open a terminal. The command is on your clipboard.",
        "forward_port": "Makes a port inside the environment available at 127.0.0.1 on this computer.",
        "send_preview": (
            "Exactly what starts on the host: image, limits and the names of variables. "
            "Secret values are never shown."
        ),
        "idle_timeout": "Stopped automatically after this many minutes without use.",
        "command_prefix": "Advanced. Only for schedulers inside a container.",
        "refresh_status": (
            "Re-reads this host's status, resources and capabilities now. "
            "Changes nothing on the host."
        ),
        "test_connection": (
            "Runs the step-by-step connection checklist: DNS, network port, login, "
            "container runtime or scheduler, screen support and GPUs. Nothing is saved."
        ),
        "screen_not_enabled": "Screen: not enabled in this template",
    },
    "status_lights": {
        "online": {
            "tone": "green",
            "label": "Online",
            "meaning": "Connected; runtime or scheduler working",
            "action": "",
        },
        "degraded": {
            "tone": "amber",
            "label": "Degraded",
            "meaning": "Connected, but the runtime/scheduler is missing or failing",
            "action": "open the host to see which check failed",
        },
        "auth_required": {
            "tone": "amber",
            "label": "Needs login",
            "meaning": "The machine answers but SSH login needs a prompt",
            "action": "run ssh <alias> once in a terminal, then Retry",
        },
        "offline": {
            "tone": "red",
            "label": "Offline",
            "meaning": "No answer on port 22",
            "action": "check the machine is on and on your network/VPN",
        },
        "unknown": {
            "tone": "grey",
            "label": "Unknown",
            "meaning": "Not checked yet",
            "action": "Refresh status",
        },
    },
}

FORBIDDEN = re.compile(
    r"AGENTS\.md|CLAUDE\.md|\bws\.py\b|subagent|claude|anthropic"
    r"|sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|xox[bp]-[A-Za-z0-9-]{10,}"
    r"|BEGIN [A-Z ]*PRIVATE KEY|password\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def _all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _all_strings(k)
            yield from _all_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _all_strings(v)


# --------------------------------------------------------------------------- topics


def test_exactly_the_eleven_topics_in_order():
    assert [(t["topic"], t["title"]) for t in docs_content.TOPICS] == EXPECTED_TOPICS


def test_topic_entries_are_well_formed():
    for entry in docs_content.TOPICS:
        assert set(entry) == {"topic", "title", "body_md", "links"}
        assert re.match(r"^[a-z][a-z0-9-]{0,40}$", entry["topic"])
        assert isinstance(entry["body_md"], str)
        words = len(entry["body_md"].split())
        assert 100 <= words <= 520, (entry["topic"], words)
        for link in entry["links"]:
            assert set(link) == {"label", "url"}
            assert link["label"]
            assert link["url"].startswith("https://"), link


def test_list_topics_shape():
    assert docs_content.list_topics() == [{"topic": t, "title": n} for t, n in EXPECTED_TOPICS]


def test_get_topic_returns_copies_and_none_for_unknown():
    first = docs_content.get_topic("slurm")
    first["body_md"] = "changed"
    first["links"].append({"label": "x", "url": "https://example.com"})
    again = docs_content.get_topic("slurm")
    assert again["body_md"] != "changed"
    assert {"label": "x", "url": "https://example.com"} not in again["links"]
    assert docs_content.get_topic("nope") is None


def test_ec2_topic_names_billing_lookup_and_tags():
    body = docs_content.get_topic("ec2")["body_md"]
    assert "AWS bills your account" in body and "checkip.amazonaws.com" in body
    assert "cloud-compute:install" in body and "never the root user" in body
    assert " is free" not in body.lower()


def test_topics_cover_key_points():
    body = {t["topic"]: t["body_md"] for t in docs_content.TOPICS}
    assert "Rescan" in body["getting-started"] and "new chat" in body["getting-started"].lower()
    assert "Apptainer" in body["slurm"] and "array" in body["slurm"] and "Needs login" in body["slurm"]
    slurm = body["slurm"]
    assert "about every 10 seconds" in slurm and "when you open the job list" in slurm
    assert "allocated" in slurm and "RAM n/a" in slurm
    assert "sacct" in slurm and "state unconfirmed" in slurm
    assert "every minute" not in slurm.lower() and "per minute" not in slurm.lower()
    assert "127.0.0.1" in body["ssh-host"]
    assert "Terminal on macOS" in body["terminal"] and "clipboard" in body["terminal"]
    assert "ssh -L" in body["display"] and "browser" in body["display"].lower()
    for tool in ("compute_status", "env_", "slurm_", "cloud_agent_"):
        assert tool in body["chat"]
    assert "push credentials" in body["chat"]
    assert "approval" in body["chat"].lower()


def test_getting_started_matches_the_real_flow():
    body = docs_content.get_topic("getting-started")["body_md"]
    assert "Capabilities" in body and "**Installed**" in body
    assert "per browser" in body
    assert "before the gateway starts" in body and "restart the gateway" in body


def test_cloud_agents_topic():
    topic = docs_content.get_topic("agents")
    assert topic["title"] == "Cloud agents"
    body = topic["body_md"]
    for needle in ("CC_AGENT_API_KEY", ".env", "Cloud agent model endpoint", "agent.base_url",
                   "refs/heads/cloud/<run>/<branch>", "worktree", "approval", "warm it first",
                   "No push credentials enter the sandbox"):
        assert needle in body, needle
    assert "Settings" in body


def test_cloud_agents_topic_leads_with_the_agents_tab():
    body = docs_content.get_topic("agents")["body_md"]
    assert body.index("Agents tab") < body.index("Before you start one")
    assert "REST" not in body
    for needle in ("**Agents**", "**New agent run**", "**Get results**", "**Stop**", "**Settings**",
                   "no restart", "in chat", "cloud_agent_spawn"):
        assert needle in body, needle
    # the tab comes first, chat is the alternative
    assert body.index("**New agent run**") < body.index("cloud_agent_spawn")
    assert 100 <= len(body.split()) <= 520


def test_cloud_agents_topic_says_the_sandbox_agent_runs_with_approvals_on():
    body = docs_content.get_topic("agents")["body_md"]
    sentence = ("Inside the sandbox the agent runs with Hermes approvals on: commands Hermes flags "
                "as dangerous are refused, and the run continues.")
    assert sentence in body
    assert body.count("approvals on") == 1
    assert body.index("### The approval") < body.index(sentence) < body.index("### Get results", body.index(sentence))
    assert "--yolo" not in body
    assert len(body.split()) <= 520


# --------------------------------------------------------------------------- api


def test_list_docs_handler_shape():
    result = docs_api.list_docs(_req())
    assert result == [{"topic": t, "title": n} for t, n in EXPECTED_TOPICS]
    result.append({"topic": "x", "title": "y"})
    assert len(docs_api.list_docs(_req())) == len(EXPECTED_TOPICS)


@pytest.mark.parametrize("topic,title", EXPECTED_TOPICS)
def test_get_doc_every_topic(topic, title):
    doc = docs_api.get_doc(_req(topic=topic))
    assert set(doc) == {"topic", "title", "body_md", "links"}
    assert doc["topic"] == topic and doc["title"] == title
    assert doc["body_md"].strip()
    assert isinstance(doc["links"], list)
    for link in doc["links"]:
        assert link["label"] and link["url"].startswith("https://")


def test_get_doc_returns_fresh_copies():
    doc = docs_api.get_doc(_req(topic="chat"))
    doc["links"].clear()
    doc["body_md"] = ""
    again = docs_api.get_doc(_req(topic="chat"))
    assert again["body_md"] and again["links"]


@pytest.mark.parametrize(
    "topic",
    ["nope", "", "Getting-Started", "../etc/passwd", "1abc", "a" * 42, "ssh_host", "a b", "-x", None],
)
def test_unknown_or_malformed_topic_is_404(topic):
    interfaces = _interfaces()
    with pytest.raises(interfaces.ApiError) as info:
        docs_api.get_doc(_req(topic=topic))
    err = info.value
    assert err.status == 404
    assert err.code == "not_found"


def test_missing_topic_param_is_404():
    interfaces = _interfaces()
    with pytest.raises(interfaces.ApiError) as info:
        docs_api.get_doc(_req())
    assert info.value.status == 404


def test_routes_and_handlers():
    interfaces = _interfaces()
    routes = docs_api.routes()
    assert [(r.method, r.path) for r in routes] == [("GET", "/docs"), ("GET", "/docs/{topic}")]
    assert all(isinstance(r, interfaces.Route) and r.summary for r in routes)
    listing = routes[0].handler(_req())
    assert len(listing) == len(EXPECTED_TOPICS)
    doc = routes[1].handler(_req(topic="ssh-host"))
    assert doc["title"] == "SSH hosts"


def test_no_tools_or_background_tasks():
    assert docs_api.tools() == []
    assert docs_api.background_tasks() == []


# --------------------------------------------------------------------------- copy


def test_copy_matches_expected_strings():
    assert docs_content.COPY == EXPECTED_COPY


def test_copy_has_exact_structure():
    copy = docs_content.COPY
    assert list(copy) == ["type_cards", "wizard_hints", "tooltips", "status_lights"]
    assert set(copy["type_cards"]) == {"ssh", "slurm", "local", "ec2"}
    assert set(copy["wizard_hints"]) == {
        "endpoint", "login", "test_connection", "sandbox_defaults", "jump_host", "partition", "account",
        "qos", "workdir", "exec_prefix", "interactive_prefix", "slurm_agents",
    }
    assert set(copy["tooltips"]) == {
        "screen", "screen_disabled", "terminal", "terminal_fallback",
        "forward_port", "send_preview", "idle_timeout", "command_prefix",
        "refresh_status", "test_connection", "screen_not_enabled",
    }
    assert set(copy["status_lights"]) == {"online", "degraded", "auth_required", "offline", "unknown"}
    for card in copy["type_cards"].values():
        assert set(card) == {
            "kind", "title", "one_liner", "when_to_use", "learn_more_topic", "mvp", "capabilities",
            "capability_notes",
        }
        assert set(card["capabilities"]) == {
            "containers", "batch_jobs", "interactive_terminal", "display", "port_forward", "gpu",
            "agents", "costs_money",
        }
        assert docs_content.get_topic(card["learn_more_topic"]) is not None
    for light in copy["status_lights"].values():
        assert set(light) == {"tone", "label", "meaning", "action"}


def test_no_internal_or_secret_like_text():
    for text in _all_strings([docs_content.TOPICS, docs_content.COPY]):
        match = FORBIDDEN.search(text)
        assert match is None, "forbidden text %r in %r" % (match.group(0), text[:80])


def test_type_cards_follow_core_host_types():
    hosttypes = importlib.import_module(PKG_NAME + ".hosttypes")
    cards = docs_content.COPY["type_cards"]
    assert list(cards) == [entry["kind"] for entry in hosttypes.HOST_TYPES]
    for entry in hosttypes.HOST_TYPES:
        card = cards[entry["kind"]]
        for field in ("kind", "title", "one_liner", "when_to_use", "learn_more_topic", "mvp", "capabilities",
                      "capability_notes"):
            assert card[field] == entry[field], (entry["kind"], field)
        assert list(card["capabilities"]) == list(hosttypes.CAPABILITY_FLAGS)


def test_limits_text_matches_host_limits():
    body = docs_content.get_topic("ssh-host")["body_md"]
    assert "default template" not in body
    assert "whether cloud agents are allowed" in body


def test_help_says_lifetimes_follow_settings_for_environments_and_agents():
    ssh = docs_content.get_topic("ssh-host")["body_md"]
    assert "come from the template" not in ssh and "else the defaults in **Settings**" in ssh
    assert "cloud agents" in ssh.split("else the defaults in **Settings**")[1][:40]
    templates = docs_content.get_topic("templates")["body_md"]
    assert "**Default ttl minutes**" in templates and "a value in the template wins" in templates
    agents = docs_content.get_topic("agents")["body_md"]
    assert "**Default ttl minutes**" in agents and "**Default idle minutes**" in agents
    assert "unless the template sets its own" in agents
    for topic in docs_content.TOPICS:               # the old hard-coded agent lifetimes are gone
        assert "240 min" not in topic["body_md"] and "120 min" not in topic["body_md"], topic["topic"]

# --------------------------------------------------------------------------- plugin.js equality


def test_copy_equals_plugin_js():
    node = shutil.which("node")
    dump = PLUGIN_DIR / "tests" / "js" / "dump.mjs"
    plugin_js = PLUGIN_DIR / "desktop" / "plugin.js"
    if node is None:
        pytest.skip("node is not on PATH")
    assert dump.exists(), "tests/js/dump.mjs is missing"
    assert plugin_js.exists(), "desktop/plugin.js is missing"
    proc = subprocess.run(
        [node, "--import", "./tests/js/register-stubs.mjs", "./tests/js/dump.mjs"],
        cwd=str(PLUGIN_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        shell=False,
    )
    assert proc.returncode == 0, "dump.mjs failed: %s" % proc.stderr[-2000:]
    dumped = json.loads(proc.stdout)
    assert "copy" in dumped
    assert dumped["copy"] == docs_content.COPY


def test_no_topic_describes_per_minute_polling():
    for topic in docs_content.TOPICS:
        text = topic["body_md"].lower()
        assert "polled every minute" not in text and "every minute" not in text, topic["topic"]


def test_docs_use_https_and_neutral_host_examples():
    body = docs_content.get_topic("agents")["body_md"]
    assert "https://llm.example.net/v1" in body and "http://" not in body
    assert "private network" in body
    everything = json.dumps(docs_content.TOPICS) + json.dumps(docs_content.COPY)
    assert "x1carbon" not in everything and "gpu-box" in everything
    skill = (Path(__file__).resolve().parents[1] / "skills" / "guide" / "SKILL.md").read_text(encoding="utf-8")
    assert "x1carbon" not in skill and "gpu-box" in skill


# --------------------------------------------------------------------------- wording and layout


_PERSONAL = re.compile(r"\b(we|i|our|us|let's|we're|we'll|i'm|i've)\b", re.IGNORECASE)
_OS_WORDS = re.compile(r"windows|\bPC\b|powershell", re.IGNORECASE)


_CLI_FLAG = re.compile(r"(?<![\w-])-{1,2}[A-Za-z][\w-]*")  # allowlist: command-line flags such as ``-i``


def _prose(text):
    """User-facing prose of a markdown body: code spans, fenced code and CLI flags removed."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    return _CLI_FLAG.sub(" ", re.sub(r"`[^`]*`", " ", text))


def test_no_we_or_i_in_help_and_copy():
    for topic in docs_content.TOPICS:
        for text in (topic["title"], _prose(topic["body_md"]), *(link["label"] for link in topic["links"])):
            hit = _PERSONAL.search(text)
            assert hit is None, (topic["topic"], hit and text[max(0, hit.start() - 40):hit.end() + 40])
    for text in _all_strings(docs_content.COPY):
        hit = _PERSONAL.search(_prose(text))
        assert hit is None, text


def test_no_single_os_wording_outside_the_terminal_lists():
    allowed = ("Windows Terminal or Command Prompt on Windows",)
    for topic in docs_content.TOPICS:
        text = _prose(topic["body_md"])
        for phrase in allowed:
            text = text.replace(phrase, "")
        hit = _OS_WORDS.search(text)
        assert hit is None, (topic["topic"], text[max(0, hit.start() - 40):hit.end() + 40] if hit else "")
    for text in _all_strings(docs_content.COPY):
        for phrase in allowed:
            text = text.replace(phrase, "")
        assert _OS_WORDS.search(text) is None, text


def test_terminal_help_is_platform_neutral_and_describes_auto():
    for text in (
        docs_content.get_topic("terminal")["body_md"],
        docs_content.COPY["tooltips"]["terminal"],
    ):
        assert "Windows Terminal or Command Prompt on Windows, Terminal on macOS, your usual terminal on Linux" in text
    body = docs_content.get_topic("terminal")["body_md"]
    assert "tries Windows Terminal first" not in body
    assert "**auto**" in body


def test_external_links_are_documentation_labels():
    labels = {link["label"] for t in docs_content.TOPICS for link in t["links"]}
    assert "Install Podman" not in labels and "Windows Terminal" not in labels
    assert "OpenSSH on Windows" not in labels
    assert "Podman install guide" in labels
    for label in labels:
        assert not re.match(r"(install|download|get|run|open|start)\b", label, re.IGNORECASE), label


def test_chat_topic_title_names_the_plugin_and_no_title_says_it():
    titles = {t["topic"]: t["title"] for t in docs_content.TOPICS}
    assert titles["chat"] == "Using Own Compute from chat"
    for title in titles.values():
        assert not re.search(r"\bit\b", title, re.IGNORECASE), title


def test_headings_are_markdown_headings_not_bold_lines():
    """A lone bold line reads like a button; real headings are styled by the page."""
    for topic in docs_content.TOPICS:
        body = topic["body_md"]
        assert not re.search(r"(?m)^\*\*[^*\n]+\*\*$", body), topic["topic"]
    assert "### Step 1: Install and turn on" in docs_content.get_topic("getting-started")["body_md"]


def test_getting_started_names_both_switches_plainly():
    body = docs_content.get_topic("getting-started")["body_md"]
    assert "**Agent**" in body and "**Desktop**" in body
    assert "turns on the **Desktop** switch too" in body
    assert "hermes plugins enable cloud-compute" in body
    assert "both halves" not in body
    steps = re.findall(r"(?m)^\d+\. ", body.split("### Step 2")[0])
    assert 4 <= len(steps) <= 7


def test_slurm_wording():
    cards = docs_content.COPY["type_cards"]
    assert "universit" not in json.dumps(cards["slurm"]).lower()
    assert "universit" not in docs_content.get_topic("slurm")["body_md"].lower()
    slurm = docs_content.get_topic("slurm")["body_md"]
    assert "site policy" not in slurm.lower() and "cluster policy" not in slurm.lower()
    assert "long-running processes on login nodes" in slurm


def test_probe_and_test_are_renamed():
    everything = json.dumps(docs_content.TOPICS) + json.dumps(docs_content.COPY)
    assert "Probe now" not in everything
    assert "Refresh status" in docs_content.get_topic("troubleshooting")["body_md"]
    tips = docs_content.COPY["tooltips"]
    assert "Re-reads" in tips["refresh_status"] and "checklist" in tips["test_connection"]


def test_slurm_field_help_is_accurate():
    hints = docs_content.COPY["wizard_hints"]
    assert "not a sandbox" in hints["workdir"]
    assert "sbatch -p" in hints["partition"] and "sbatch -A" in hints["account"]
    assert "--qos" in hints["qos"] and "ssh -J" in hints["jump_host"]
    for key in ("jump_host", "partition", "account", "qos", "workdir", "exec_prefix", "interactive_prefix"):
        assert "\n" not in hints[key] and hints[key].strip(), key


def test_every_topic_still_fits_the_length_limits():
    for entry in docs_content.TOPICS:
        words = len(entry["body_md"].split())
        assert 100 <= words <= 520, (entry["topic"], words)


def test_skill_prose_has_no_we_or_i_or_single_os_wording():
    skill = (PLUGIN_DIR / "skills" / "guide" / "SKILL.md").read_text(encoding="utf-8")
    prose = _prose(skill.split("---", 2)[2])
    hit = _PERSONAL.search(prose)
    assert hit is None, prose[max(0, hit.start() - 40):hit.end() + 40] if hit else ""
    assert _OS_WORDS.search(prose) is None


def test_keeping_your_data_topic_covers_the_data_safety_rules():
    body = docs_content.get_topic("keeping-data")["body_md"]
    words = len(body.split())
    assert 100 <= words <= 520, words
    assert re.search(r"(?m)^### ", body)
    for phrase in (
        "stopped", "not deleted", "Extend", "idle", "Auto delete days", "saved to this computer first",
        "nothing is deleted", "Delete without saving", "without a record", "Files", "Restore", "Export max mb",
    ):
        assert phrase in body, phrase
    assert "`/work`) only" in body and "Keep important work in `/work`" in body
    assert "its files were saved" not in body.lower()


def test_templates_and_slurm_topics_describe_round_five_changes():
    templates = docs_content.get_topic("templates")["body_md"]
    for phrase in ("+ New template", "Reset to default", "**Name** field", "Send these secrets into the environment",
                   "not set, will not be sent", "own section"):
        assert phrase in templates, phrase
    slurm = docs_content.get_topic("slurm")["body_md"]
    assert "inside Apptainer only when an image is given; otherwise it runs directly as your user on the compute nodes" in slurm
    assert "TTL" not in templates


def test_docs_list_fixture_equals_the_topic_list():
    import json

    fixture = json.loads((PLUGIN_DIR / "tests" / "fixtures" / "desktop" / "docs_list.json").read_text(encoding="utf-8"))
    assert fixture == docs_content.list_topics()
