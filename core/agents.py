"""Cloud agents: run a coding agent in a sandbox on a branch shipped in as a git bundle.

Flow (see :class:`AgentManager`):

1. ``spawn`` bundles a branch of a local repository (new branches start from ``base``),
   starts a sandbox from an agent template, streams the bundle into the container,
   clones it to ``/work/repo`` (no remotes, local identity only) and launches a
   detached runner that writes ``{log,exit_code,pid}`` in the run state folder
   (``/home/agent/.cc-agent``: outside ``/work``, so a saved copy of the work folder never
   holds the run state or the agent's Hermes home, which can hold the model key).
2. ``poll`` / ``status`` read the runner state with one ``exec`` per active run.
3. ``collect`` bundles the new commits inside the sandbox, streams them back and
   imports them as ``refs/heads/cloud/<run>/<branch>`` (optionally checked out in a
   sibling worktree). It never merges and never moves the user's branches.
4. ``stop`` stops the runner and destroys the sandbox.

5. ``message`` sends a follow-up to a run whose turn ended: same sandbox, same branch and,
   when Hermes reported a session id, the same Hermes session (``hermes chat -Q --resume``).
6. ``remove`` deletes the record of a run whose sandbox is gone.

Modes: ``hermes`` runs ``hermes chat -Q`` against an OpenAI-compatible endpoint; its stdout
(the final answer) becomes ``AgentRun.reply``. ``script`` is the handoff test: it commits a
marker file without any model (no secret at all).

The model comes from (in order) the run's ``model_id`` (a saved choice, records kind
``agent_model``), the template's ``agent.base_url``/``agent.model``, the default saved choice,
then the ``agent_base_url``/``agent_model`` settings. The only credential that ever enters a
sandbox is the model key, declared on the template as the runtime variable
``CC_AGENT_API_KEY`` and injected by the sandbox manager at container start; a saved choice
names the secret it is read from. It never appears in argv, logs, records, events or
bundles. Standard library only.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
import re
import shlex
import threading
import time
import typing
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable, Iterable, Literal

from . import agent_models, gitrelay
from . import secrets as secrets_mod
from .interfaces import ApiError
from .redact import redact_tokens
from .models import (
    Model, TemplateSpec, VariableSpec, fitting_host_kinds, new_id, parse_iso, utc_now_iso,
)
from .runtimes import container_name, runtime_for

__all__ = [
    "AgentStatus", "AGENT_STATUSES", "ACTIVE_STATUSES", "AgentRun", "CollectResult",
    "AgentManager", "AGENT_TEMPLATES", "AGENT_IMAGE", "AGENT_IMAGE_LITE", "KEY_ENV",
    "DEFAULT_KEY_SECRET", "KEY_DESCRIPTION", "KEPT_SANDBOX_MESSAGE", "NOT_FETCHED_PREFIX",
    "DEFAULT_MODEL_MARKER", "RECORD_KIND",
    "CONTAINERFILE", "NO_ENDPOINT_MESSAGE", "provide", "MODEL_KIND", "SCRIPT_REPLY",
    "SETUP_SENTENCE", "REPLY_MAX_CHARS", "WAIT_HARD_CAP_S", "AgentModel",
]

log = logging.getLogger(__name__)

AgentStatus = Literal["starting", "running", "done", "failed", "stopped", "collected"]
AGENT_STATUSES: tuple[str, ...] = typing.get_args(AgentStatus)
#: Statuses that ``poll`` refreshes.
ACTIVE_STATUSES: tuple[str, ...] = ("starting", "running")
AGENT_MODES: tuple[str, ...] = ("hermes", "script")

RECORD_KIND = "agent_runs"
#: Name of the environment variable that carries the model key inside the sandbox.
KEY_ENV = "CC_AGENT_API_KEY"
#: Default Hermes secret name the key is read from on this computer.
DEFAULT_KEY_SECRET = "CC_AGENT_API_KEY"
#: Plain-language help shown next to the key variable (template editor, spawn confirmation).
KEY_DESCRIPTION = ("API key for the cloud agent's model endpoint. Sent only into cloud-agent "
                   "sandboxes in Hermes mode, after you approve the run. Use a scoped, "
                   "spend-capped key.")
#: Run error when Stop could not save the sandbox's files, so the sandbox was kept.
#: ``model_id`` of a rerun that means "the current default model" (not the old run's model).
DEFAULT_MODEL_MARKER = "default"
#: The first words of the error Stop records when the agent's newest commits could not be
#: fetched and the sandbox was therefore kept.
NOT_FETCHED_PREFIX = "The agent's newest commits could not be fetched to this computer"
KEPT_SANDBOX_MESSAGE = ("The sandbox was kept because the files in /work could not be saved "
                        "to this computer. "
                        "Delete it from Overview when ready.")

AGENT_IMAGE = "localhost/cloud-compute-agent:0.1"
AGENT_IMAGE_LITE = "localhost/cloud-compute-agent:0.1-lite"
AGENT_IMAGE_PREFIX = "localhost/cloud-compute-agent:"
#: Lighter base for script mode, pinned by the multi-arch index digest of python:3.12-slim.
LITE_BASE = ("docker.io/library/python:3.12-slim"
             "@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b")
CONTAINERFILE = Path(__file__).resolve().parents[1] / "images" / "agent" / "Containerfile"

# Paths inside the sandbox.
WORK = "/work"
REPO_DIR = "/work/repo"
#: Where runs started by earlier versions kept the run state and the Hermes home: inside the
#: work folder, which Stop/Delete saves to this computer. New runs never use these.
LEGACY_STATE_DIR = "/work/.cc-agent"
LEGACY_HERMES_HOME = "/work/.hermes"
#: Home folder of the sandbox user. The run state and the agent's Hermes home (which can hold
#: the model key) live below it, on the container's own filesystem: outside the work folder
#: that is saved, and outside any volume the image declares (the Hermes image declares
#: /opt/data), so nothing survives the container.
AGENT_HOME = "/home/agent"
STATE_NAME = ".cc-agent"
#: Folders of an agent sandbox that a saved copy never contains (relative to its work folder,
#: plus the new location). They can hold the model key.
AGENT_INTERNAL = (LEGACY_HERMES_HOME, LEGACY_STATE_DIR, f"{AGENT_HOME}/{STATE_NAME}")
_STATE_RE = re.compile(r"^/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")


def state_dir_of(run: Any) -> str:
    """The run state folder inside the sandbox (``/work/.cc-agent`` for an older run)."""
    value = getattr(run, "state_dir", None)
    return value if isinstance(value, str) and value else LEGACY_STATE_DIR


def hermes_home_of(run: Any) -> str:
    """The agent's Hermes home inside the sandbox (``/work/.hermes`` for an older run)."""
    value = getattr(run, "state_dir", None)
    return f"{value}/hermes" if isinstance(value, str) and value else LEGACY_HERMES_HOME

GIT_NAME = "Own Compute agent"
GIT_EMAIL = "agent@cloud-compute.invalid"
RESULT_FILE = "CLOUD_AGENT_RESULT.md"

MAX_GOAL_CHARS = 8000
MAX_TURNS_LIMIT = 200
LOG_LINES = 20
STATUS_REFRESH_S = 10.0          # status() refreshes an active run at most this often
POLL_BUDGET_S = 8.0              # one poll/status tick spends at most this long on sandbox execs
POLL_MIN_EXEC_S = 1.0            # do not start another exec with less than this left in the tick
BACKOFF_BASE_S = 30.0            # a host that failed a poll is skipped 30 s, then 60 s, ...
BACKOFF_MAX_S = 300.0            # ... capped at 5 min; one good poll resets it
RESTART_WAIT_S = 60.0            # collect waits up to this long for a restarted sandbox
RESTART_POLL_S = 1.0
STARTING_STALE_S = 15 * 60       # a run stuck in "starting" this long without a sandbox failed

#: The agent's final answer is kept up to this many characters (a note says when it was cut).
REPLY_MAX_CHARS = 4000
#: What a script-mode (handoff test) run reports as its reply.
SCRIPT_REPLY = "Handoff test: no model was used. A marker file was committed."
#: Saved cloud-agent model choices (generic records).
MODEL_KIND = "agent_model"
#: Shown instead of a run's reply or log when its sandbox still exists but the model key it
#: was started with cannot be read back from it to hide it: the text is never shown unhidden.
TEXT_WITHHELD_MESSAGE = (
    "{what} withheld: cloud agent run {run} used a model key that is hidden in replies and "
    "logs, and this computer cannot read it back from the sandbox right now to hide it. Check "
    "that the host {host} is reachable and that the sandbox still exists, then try again.")
#: Shown for a run recorded before the key was hidden when its sandbox is gone and the secret
#: holding the key cannot be found, so the stored text cannot be checked for it.
OLD_TEXT_WITHHELD_MESSAGE = (
    "{what} withheld: cloud agent run {run} was recorded before the model key was hidden in "
    "replies and logs, its sandbox is gone, and the secret {secret} is not set on this "
    "computer, so the text cannot be checked for the key. Set the secret {secret} to the key "
    "the run used and open the run again, or start a new run.")
#: A follow-up cannot change the turn limit: Hermes keeps the one a session started with.
TURN_LIMIT_FIXED = ("The turn limit is set when the run starts. Start it again with rerun to "
                    "change it.")
#: Where the user picks or adds a model, said the same way everywhere.
SETUP_SENTENCE = "Add one in Compute, Agents, New agent run, Model."
#: A blocking wait never runs longer than this: Hermes cuts a tool call off at 420 s
#: (agent/tool_executor.py _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S) and a plugin cannot raise it.
WAIT_HARD_CAP_S = 360
DEFAULT_WAIT_MAX_S = 300
WAIT_POLL_S = 6.0                # a blocking wait checks the sandbox this often

RUN_ID_RE = re.compile(r"^a-[0-9a-f]{8}$")
MODEL_ID_RE = re.compile(r"^m-[0-9a-f]{8}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.@:/=+-]+$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,128}$")
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(/[^\s@]*)?$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


# --------------------------------------------------------------------------- records


@dataclass
class AgentRun(Model):
    id: str                                    # "a-" + 8 hex
    instance_id: str
    host_id: str
    goal: str
    repo_path: str
    branch: str
    base_sha: str
    head_sha: str | None
    commits_ahead: int
    status: AgentStatus
    started_at: str
    ended_at: str | None
    session_id: str | None
    last_log: list[str] = field(default_factory=list)
    mode: str = "hermes"
    template_id: str = "hermes-agent"
    collected_ref: str | None = None
    worktree_path: str | None = None
    exit_code: int | None = None
    error: str | None = None
    reply: str | None = None                   # the agent's final answer text
    turns: int = 1                             # prompts sent to the agent: 1 plus follow-ups
    model: str | None = None
    endpoint_host: str | None = None
    max_turns: int = 30                        # turn limit of the latest prompt
    model_id: str | None = None                # saved model choice the run used, if any
    criteria: str | None = None                # "Done when: ..." appended to the first prompt
    key_secret: str | None = None              # NAME of the secret the model key came from
    state_dir: str | None = None               # run state folder in the sandbox; None = older layout
    rerun_of: str | None = None                # id of the run this one was started again from
    error_kind: str | None = None              # why a failed run failed, when known (FAILURE_KINDS)
    new_branch: bool | None = None             # the run created its branch (None: not recorded)
    collected_head_sha: str | None = None      # sandbox head the collected branch holds (None: unknown)
    stop_note: str | None = None               # what Stop fetched, or where unfetched commits are
    text_masked: bool | None = None            # reply, log and error were stored with the model
    #                                            key hidden (None: a record of an earlier version)


@dataclass
class AgentModel(Model):
    """A saved model choice for cloud agents (records kind ``agent_model``). Holds the NAME
    of the secret with the key, never the key."""
    id: str                                    # "m-" + 8 hex
    name: str
    base_url: str
    model: str
    key_secret: str = "CC_AGENT_API_KEY"
    is_default: bool = False


@dataclass
class CollectResult(Model):
    run_id: str
    ref: str                                   # refs/heads/cloud/<run>/<branch>
    commits_ahead: int
    worktree_path: str | None
    diverged: bool
    conflicts_dry_run: list[str] = field(default_factory=list)
    message: str = ""                          # why diverged is true when it is the sandbox result

    def to_dict(self) -> dict:
        data = super().to_dict()
        if not data.get("message"):            # additive: only present when there is something to say
            data.pop("message", None)
        return data


# --------------------------------------------------------------------------- templates


HERMES_DESCRIPTION = ("Headless Hermes agent that works on a git branch in a sandbox and "
                      "commits its changes. Uses the model chosen for the run, else the default "
                      "cloud-agent model (Compute, Agents, New agent run, Model); agent.base_url "
                      "and agent.model here override both.")


def _agent_templates() -> list[TemplateSpec]:
    hermes = TemplateSpec(
        id="hermes-agent",
        name="Hermes cloud agent",
        description=HERMES_DESCRIPTION,
        image=AGENT_IMAGE,
        command=["sleep", "infinity"],
        workdir=WORK,
        cpus=2, memory_gb=4, ttl_minutes=None, idle_minutes=None,
        network="default",
        variables=[VariableSpec(name=KEY_ENV, source=f"secret:{DEFAULT_KEY_SECRET}",
                                scope="runtime", description=KEY_DESCRIPTION)],
        host_kinds=["ssh", "local"],
        agent={"enabled": True, "mode": "hermes", "model": None, "base_url": None,
               "key_secret": DEFAULT_KEY_SECRET, "max_turns": 30, "image_base": None},
    )
    script = TemplateSpec(
        id="agent-script",
        name="Handoff test (no model)",
        description=("Commits a marker file to prove the branch round trip. Does not use a "
                     "model and ignores the task text."),
        image=AGENT_IMAGE_LITE,
        command=["sleep", "infinity"],
        workdir=WORK,
        cpus=1, memory_gb=1, ttl_minutes=None, idle_minutes=None,
        network="default",
        variables=[],
        host_kinds=["ssh", "local"],
        agent={"enabled": True, "mode": "script", "max_turns": 30, "image_base": LITE_BASE},
    )
    return [hermes, script]


#: Built-in agent templates, saved to the template store on first use. Like the container
#: built-ins they leave ``ttl_minutes`` / ``idle_minutes`` unset, so the ``default_ttl_minutes``
#: / ``default_idle_minutes`` settings apply at spawn (request > template > Settings).
AGENT_TEMPLATES: list[TemplateSpec] = _agent_templates()
_TEMPLATE_IDS = {t.id: t for t in AGENT_TEMPLATES}

#: ``(ttl, idle)`` that earlier versions seeded into the stored copies. An otherwise unmodified
#: copy that still has exactly these is switched to unset by :meth:`AgentManager.ensure_templates`.
_LEGACY_LIFETIMES = {"hermes-agent": (240, 120), "agent-script": (120, 60)}
#: ``(name, description)`` of earlier seeds. An unedited stored copy with one of these is
#: replaced by the current built-in.
_LEGACY_TEXT = {
    "hermes-agent": [("Hermes cloud agent",
                      "Headless Hermes agent working on a git branch in a sandbox. Needs "
                      "a model endpoint and model (agent.base_url / agent.model, or the "
                      "Own Compute settings) and the model key secret named in agent.key_secret.")],
    "agent-script": [("Scripted cloud agent",
                      "Commits a result file describing the goal, without a model. Proves the "
                      "branch handoff; sends no secrets.")],
}


# --------------------------------------------------------------------------- sandbox scripts
# Fixed scripts run with ``sh -c <script> sh <args...>``: user values only ever arrive as
# positional parameters or files, never inside the script text.

_PUT_FILE = 'set -e; umask 077; mkdir -p "$(dirname "$1")"; cat > "$1"'

#: Picks and creates the run state folder: below the sandbox user's home, else below $HOME,
#: else /tmp. All of them are on the container's own filesystem, never in the work folder.
_PICK_STATE = f"""for d in {AGENT_HOME} "${{HOME:-}}" /tmp; do
  case "$d" in /|"") continue ;; esac
  if mkdir -p "$d/{STATE_NAME}" 2>/dev/null && chmod 700 "$d/{STATE_NAME}" 2>/dev/null \\
     && [ -w "$d/{STATE_NAME}" ]; then echo "$d/{STATE_NAME}"; exit 0; fi
done
exit 1
"""


def _setup_script(state: str) -> str:
    return f"""set -eu
S={state}; R={REPO_DIR}
test -s "$S/in.bundle"
git init -q "$R"
cd "$R"
git fetch -q --update-head-ok "$S/in.bundle" "+refs/heads/$1:refs/heads/$1"
git symbolic-ref HEAD "refs/heads/$1"
git reset -q --hard
git config user.name {shlex.quote(GIT_NAME)}
git config user.email {shlex.quote(GIT_EMAIL)}
git config commit.gpgsign false
for r in $(git remote); do git remote remove "$r"; done
rm -f "$S/in.bundle"
git rev-parse HEAD
"""


def _launch_script(state: str) -> str:
    return f"""set -eu
S={state}
rm -f "$S/exit_code" "$S/exit_code.tmp"
if command -v setsid >/dev/null 2>&1; then
  setsid sh "$S/run.sh" </dev/null >"$S/log" 2>&1 &
else
  nohup sh "$S/run.sh" </dev/null >"$S/log" 2>&1 &
fi
echo $! > "$S/pid"
echo launched
"""


def _poll_script(state: str) -> str:
    return f"""S={state}
cd "$S" 2>/dev/null || exit 3
if [ -f exit_code ]; then
  echo "exit=$(cat exit_code)"
else
  p=$(cat pid 2>/dev/null || true)
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    echo "exit=running"
  elif [ -f exit_code ]; then
    echo "exit=$(cat exit_code)"
  else
    echo "exit=lost"
  fi
fi
if cd {REPO_DIR} 2>/dev/null; then
  h=$(git rev-parse --verify -q "refs/heads/$2^{{commit}}" 2>/dev/null || true)
  echo "head=$h"
  if [ -n "$h" ]; then echo "ahead=$(git rev-list --count "$1..$h" 2>/dev/null || echo 0)"; fi
fi
echo "==log"
tail -n {LOG_LINES} "$S/log" 2>/dev/null || true
if [ -f "$S/exit_code" ]; then
  echo "==reply"
  head -c 16000 "$S/reply.txt" 2>/dev/null || true
fi
"""

_COLLECT = f"""set -eu
cd {REPO_DIR}
h=$(git rev-parse --verify -q "refs/heads/$2^{{commit}}") || {{ echo "branch $2 not found in the sandbox" >&2; exit 4; }}
n=$(git rev-list --count "$1..$h")
echo "head=$h"
echo "ahead=$n"
if [ "$n" -gt 0 ]; then
  echo "==bundle"
  if command -v base64 >/dev/null 2>&1; then
    git bundle create - "$1..refs/heads/$2" 2>/dev/null | base64
  else
    git bundle create - "$1..refs/heads/$2" 2>/dev/null | python3 -c 'import base64,sys; sys.stdout.write(base64.encodebytes(sys.stdin.buffer.read()).decode())'
  fi
fi
"""

def _kill_script(state: str) -> str:
    return f"""p=$(cat {state}/pid 2>/dev/null || true)
if [ -n "$p" ]; then kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null || true; fi
echo stopped
"""


def _runner_script(run_id: str, mode: str, max_turns: int,
                   state_dir: str = LEGACY_STATE_DIR) -> str:
    """The detached runner. Reads the prompt from a file; writes reply.txt, then exit_code last.

    Hermes mode: ``hermes chat -Q`` prints only the final answer on stdout (saved as
    ``reply.txt``) and ``session_id: <id>`` on stderr (Hermes cli_single_query.py), which goes
    to the log and is kept in ``session_id`` so a follow-up can ``--resume`` the same session.
    Without a recorded session a follow-up starts a new one with ``prompt_fresh.txt``, which
    says what the earlier turns did.
    """
    hermes_home = f"{state_dir}/hermes" if state_dir != LEGACY_STATE_DIR else LEGACY_HERMES_HOME
    lines = [
        "#!/bin/sh",
        f"S={state_dir}",
        "rc=0",
        'rm -f "$S/reply.txt"',
        f'cd {REPO_DIR} || {{ echo 1 > "$S/exit_code.tmp"; mv "$S/exit_code.tmp" "$S/exit_code"; exit 1; }}',
    ]
    if mode == "hermes":
        lines += [
            f"export HERMES_HOME={hermes_home}",
            # Hermes keeps its approval system on (catalog rule 12): no bypass flag on the
            # command line, and none inherited from the image environment.
            "unset HERMES_YOLO_MODE",
            'q="$S/goal.txt"',
            # A new session gets the turn limit; a resumed one keeps the limit it was started
            # with (Hermes refuses --max-turns on --resume, gateway_chat.check_resume_policy).
            f'mt="--max-turns {int(max_turns)}"',
            "set --",
            'if [ -f "$S/followup" ]; then',
            '  sid=$(cat "$S/session_id" 2>/dev/null || true)',
            '  case "$sid" in ""|*[!A-Za-z0-9_.:-]*) q="$S/prompt_fresh.txt" ;; *) set -- --resume "$sid"; mt="" ;; esac',
            "fi",
            # --query-file keeps the prompt out of argv parsing (a prompt may start with "-").
            'hermes chat -Q --query-file "$q" $mt "$@" >"$S/reply.txt" || rc=$?',
            'sid=$(sed -n "s/^session_id: *//p" "$S/log" 2>/dev/null | tail -n 1)',
            'case "$sid" in ""|*[!A-Za-z0-9_.:-]*) ;; *) printf "%s\n" "$sid" > "$S/session_id" ;; esac',
            'git add -A || true',
            'if ! git diff --cached --quiet; then',
            f'  git commit -q -m "Cloud agent {run_id}: uncommitted changes" || {{ [ "$rc" -ne 0 ] || rc=1; }}',
            'fi',
        ]
    else:
        lines += [
            'n=$(cat "$S/turn" 2>/dev/null || echo 1)',
            "{",
            '  echo "# Cloud agent handoff test"',
            "  echo",
            f'  echo "Run: {run_id}, turn $n"',
            "  echo",
            '  echo "This marker file proves the branch round trip. No model was used and the task text was not acted on."',
            "  echo",
            '  echo "Task text:"',
            "  echo",
            '  cat "$S/goal.txt"',
            "  echo",
            f"}} > {RESULT_FILE} || rc=1",
            f"git add {RESULT_FILE} || rc=1",
            'if ! git diff --cached --quiet; then',
            f'  git commit -q -m "Cloud agent {run_id}: handoff test marker" || rc=1',
            'fi',
            f'echo {shlex.quote(SCRIPT_REPLY)} > "$S/reply.txt"',
            f'echo {shlex.quote(SCRIPT_REPLY)}',
        ]
    lines += ['echo "$rc" > "$S/exit_code.tmp" && mv "$S/exit_code.tmp" "$S/exit_code"', ""]
    return "\n".join(lines)


#: Shown when a hermes-mode run has no model endpoint or model anywhere.
NO_ENDPOINT_MESSAGE = ("No model is set up for cloud agents. Add one in Compute, Agents, New "
                       "agent run, Model. A chat agent should tell the user that sentence.")


def _hermes_config(base_url: str, model: str) -> str:
    """Non-secret Hermes config: the key is read from the env var named by key_env.

    The ``approvals`` block keeps Hermes's approval system on for the unattended run.
    ``hermes chat -Q`` is a single-query session with nobody to answer a prompt, so a command
    Hermes flags as dangerous (recursive delete, pipe-to-shell, a hard reset, ...) is denied at
    once (``single_query_mode: deny``, Hermes tools/approval.py) and the agent has to find
    another way. Ordinary work (file edits, ``git add``/``commit``, running tests) is not
    flagged and proceeds. ``mode: manual`` plus a short ``timeout`` makes any other path that
    would prompt fail closed within seconds instead of waiting for an answer that never comes.
    """
    return ("model:\n"
            "  provider: custom\n"
            f"  base_url: {json.dumps(base_url)}\n"
            f"  default: {json.dumps(model)}\n"
            f"  key_env: {KEY_ENV}\n"
            "approvals:\n"
            "  mode: manual\n"
            "  timeout: 30\n"
            "  single_query_mode: deny\n"
            "  cron_mode: deny\n"
            "  unattended_mode: deny\n")


# --------------------------------------------------------------------------- helpers


def _slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = slug[:limit].strip("-")
    return slug or "task"


def _endpoint_host(base_url: Any) -> str:
    """``host[:port]`` of a model endpoint URL (never credentials or the path)."""
    if not isinstance(base_url, str) or not base_url:
        return ""
    try:
        parts = urlsplit(base_url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if host and port else host


def _short(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _parse_kv(text: str) -> tuple[dict[str, str], list[str]]:
    """Split ``key=value`` lines before a ``==<section>`` line from the lines after it."""
    info: dict[str, str] = {}
    rest: list[str] = []
    in_section = False
    for line in (text or "").splitlines():
        if not in_section and line.startswith("=="):
            in_section = True
            continue
        if in_section:
            rest.append(line)
        elif "=" in line:
            k, _, v = line.partition("=")
            info[k.strip()] = v.strip()
    return info, rest


def _err_text(exc: BaseException) -> str:
    if isinstance(exc, ApiError):
        return exc.message
    if isinstance(exc, gitrelay.GitError):
        msg = str(exc.args[0]) if exc.args else "git failed"
        stderr = getattr(exc, "stderr", "") or ""
        return f"{msg}: {stderr}" if stderr and stderr not in msg else msg
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- failure causes

_STATUS_RES = (re.compile(r"\bHTTP\s*(?:error\s*)?(\d{3})\b", re.I),
               re.compile(r"\bError code:\s*(\d{3})\b", re.I),
               re.compile(r"\bstatus[_ ]?code[=: ]+(\d{3})\b", re.I))
_CONNECT_RE = re.compile(
    r"connection (?:refused|error|reset|aborted)|connect(?:ion)? timed? ?out|connecttimeout|"
    r"readtimeout|timed out|name or service not known|temporary failure in name resolution|"
    r"getaddrinfo|no route to host|network is unreachable|apiconnectionerror|failed to connect|"
    r"max retries exceeded", re.I)
_CONTEXT_RE = re.compile(
    r"context[_ ](?:length|window)|maximum context|too many tokens|prompt is too long|"
    r"exceeds? the (?:model'?s )?(?:max|context)|input is too long", re.I)
_QUOTA_RE = re.compile(r"insufficient[_ ]quota|quota|billing|credit|exceeded your current|"
                       r"out of (?:credits|budget)|spend limit|rate[_ ]?limit", re.I)
#: Hermes advice for an interactive session that does not apply to a cloud agent run.
_HERMES_ADVICE_RE = re.compile(r"/retry|/model\b|hermes fallback|hermes model", re.I)
#: Failure kinds that a follow-up in the same sandbox would only repeat: the model, the key or
#: the endpoint has to change first (the run is started again instead).
MODEL_FAILURE_KINDS = ("model_not_found", "bad_key", "unreachable", "rate_limit", "server",
                       "context", "endpoint")
#: ``turn_limit``: the agent used all its turns; its work is on the branch.
FAILURE_KINDS = MODEL_FAILURE_KINDS + ("turn_limit",)
_BUDGET_RE = re.compile(r"iteration budget exhausted\s*\((\d+)\s*/\s*(\d+)|"
                        r"reached the iteration limit|maximum number of tool-calling iterations|"
                        r"stopped at the iteration limit|iteration limit mid-task|"
                        r"max(?:imum)? iterations (?:was |were )?(?:reached|hit)|"
                        r"(?:turn|iteration)s? (?:limit|budget) (?:was )?(?:reached|exhausted|hit)",
                        re.I)


def _provider_message(text: str, start: int) -> str:
    """The provider's own words after a status code: a JSON ``message``/``error`` value or the
    rest of the line, cut short."""
    tail = text[start:start + 400]
    m = re.search(r'"(?:message|error)"\s*:\s*"([^"]{1,200})"', tail)
    msg = m.group(1) if m else tail.split("\n", 1)[0].strip(" :-{}\"'")
    return _short(msg.replace("\\n", " "), 140)


def diagnose_failure(text: str, *, model: str | None, endpoint: str | None,
                     key_secret: str | None, host_id: str | None = None,
                     max_turns: int | None = None) -> tuple[str, str] | None:
    """``(kind, sentence)`` for a failed hermes run whose cause is in the model call, else None.

    ``text`` is what the agent printed (its reply and the end of its log). The sentence names
    the cause and the next step in plain words; it never contains the key. It does not repeat
    Hermes's own advice (``/retry``, ``/model``), which does not apply to a cloud agent run.
    """
    text = text or ""
    where = endpoint or "the model endpoint"
    what = f"model {model}" if model else "the model"
    status = None
    pos = 0
    for rx in _STATUS_RES:
        for m in rx.finditer(text):
            if m.start() >= pos:          # the last status wins: Hermes prints it at the end
                status, pos = int(m.group(1)), m.end()
    if status is not None and status >= 400:
        said = _provider_message(text, pos)
        said_part = f' It said: "{said}".' if said else ""
        if status in (401, 403):
            key = f" Check the key secret {key_secret}" if key_secret else " Check the model key"
            return ("bad_key",
                    f"The model endpoint {where} refused the key (HTTP {status}): it is wrong, "
                    f"expired, or not allowed to use {what}.{said_part}{key}, then start the "
                    "run again.")
        if status == 404:
            return ("model_not_found",
                    f"The model endpoint {where} answered 404: {what} was not found there (or "
                    f"the endpoint address is wrong).{said_part} Choose another model and start "
                    "the run again.")
        if status in (402, 429):
            return ("rate_limit",
                    f"The model endpoint {where} answered {status}: the rate limit or the quota "
                    f"of the key is used up.{said_part} Start the run again later, or choose "
                    "another model or key.")
        if status == 400 and _CONTEXT_RE.search(text):
            return ("context",
                    f"The task needs more room than {what} accepts at {where} (HTTP 400).{said_part}"
                    " Shorten the task, or choose a model with a larger context window, and "
                    "start the run again.")
        if status >= 500:
            return ("server",
                    f"The model endpoint {where} answered {status}: it has an error or is "
                    f"overloaded.{said_part} Start the run again later, or choose another model.")
        return ("endpoint",
                f"The model endpoint {where} answered {status}.{said_part} Check the model and "
                "the endpoint, then start the run again.")
    budget = _BUDGET_RE.search(text)
    if budget:
        limit = (budget.group(2) if budget.lastindex and budget.group(2) else None) or max_turns
        used = f"all {limit} of its turns" if limit else "all of its turns"
        return ("turn_limit",
                f"The agent used {used} before it finished. Its work so far is on the branch. "
                "Start the run again with a higher turn limit (rerun with max_turns), or send a "
                "follow-up to let it continue.")
    if _CONTEXT_RE.search(text):
        return ("context",
                f"The task needs more room than {what} accepts at {where}. Shorten the task, or "
                "choose a model with a larger context window, and start the run again.")
    if _CONNECT_RE.search(text):
        host = f" (host {host_id})" if host_id else ""
        return ("unreachable",
                f"The sandbox could not reach the model endpoint {where}: the connection was "
                f"refused or timed out. Check the endpoint address and that the model server is "
                f"running and reachable from the host{host}, then start the run again.")
    if _QUOTA_RE.search(text):
        return ("rate_limit",
                f"The model endpoint {where} reports that the rate limit or the quota of the key "
                "is used up. Start the run again later, or choose another model or key.")
    return None


_PREVIEW_LINE = re.compile("^\\s*\\u2502")
_HUNK_LINE = re.compile("^\\s*\\u2502\\s*@@ .*@@")


def strip_tool_previews(text: str) -> str:
    """The agent's answer without the diff previews Hermes prints before it.

    Hermes shows an edited file as a box of lines that start with U+2502 (the file name, a
    ``@@ ... @@`` hunk header, then the changed lines). Only such a block that holds a hunk
    header is dropped; a U+2502 list in the answer itself (a table, a folder tree) stays.
    When nothing but previews would be left, the text is returned as it was.
    """
    lines = (text or "").splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        if not _PREVIEW_LINE.match(lines[i]):
            kept.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and _PREVIEW_LINE.match(lines[j]):
            j += 1
        block = lines[i:j]
        if not any(_HUNK_LINE.match(line) for line in block):
            kept.extend(block)
        i = j
    cleaned = chr(10).join(kept).strip()
    return cleaned or (text or "").strip()


def _drop_hermes_advice(text: str) -> str:
    """The reply of a failed run without the lines that give Hermes chat advice."""
    kept = [ln for ln in (text or "").splitlines() if not _HERMES_ADVICE_RE.search(ln)]
    return "\n".join(kept).strip()


def _gone(stderr: str) -> bool:
    s = (stderr or "").lower()
    return "no such container" in s or "no container with name or id" in s


# --------------------------------------------------------------------------- manager


NOT_FETCHED = "not_fetched"


class _NotFetched(Exception):
    """The commits in a sandbox could not be brought to this computer (``detail`` says why)."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class _CannotMask(Exception):
    """The model key of a run cannot be determined: its text must not be shown or stored."""


@dataclass
class _Mask:
    fn: Callable[[str], str]
    read_back: bool            # the values came from the run's sandbox
    own: bool                  # the run's own key secret resolved by name


def _replace_values(text: str, values: Iterable[str]) -> str:
    out = text or ""
    for value in sorted({v for v in values if v}, key=len, reverse=True):
        out = out.replace(value, "[redacted]")
    return out


class AgentManager:
    """Start, watch, collect and stop cloud agent runs (records kind ``agent_runs``)."""

    def __init__(self, services: Any):
        self.services = services
        self._lock = threading.RLock()
        self._run_locks: dict[str, threading.RLock] = {}
        self._spawning: set[str] = set()
        self._refreshed: dict[str, float] = {}
        self._host_fail: dict[str, tuple[int, float]] = {}   # host id -> (failures, retry at)
        self._templates_ready = False          # built-ins seeded (see ensure_templates)
        self._clock = time.monotonic           # injectable for tests
        self._sleep = time.sleep

    # ------------------------------------------------------------------ records

    def _run_lock(self, run_id: str) -> threading.RLock:
        with self._lock:
            lock = self._run_locks.get(run_id)
            if lock is None:
                lock = self._run_locks[run_id] = threading.RLock()
            return lock

    def _save(self, run: AgentRun) -> None:
        self.services.store.put_record(RECORD_KIND, run.id, run.to_dict(),
                                       host_id=run.host_id, status=run.status)

    def _load(self, run_id: str) -> AgentRun:
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
            raise ApiError(404, "not_found", f"agent run {run_id!r} not found")
        data = self.services.store.get_record(RECORD_KIND, run_id)
        if data is None:
            raise ApiError(404, "not_found", f"agent run {run_id} not found")
        return AgentRun.from_dict(data)

    def _all(self, statuses: Iterable[str] | None = None) -> list[AgentRun]:
        rows = self.services.store.list_records(
            RECORD_KIND, statuses=set(statuses) if statuses is not None else None)
        runs: list[AgentRun] = []
        for row in rows:
            try:
                runs.append(AgentRun.from_dict(row))
            except (TypeError, ValueError) as exc:
                log.warning("cloud-compute: skipping unreadable agent run: %s", exc)
        runs.sort(key=lambda r: (r.started_at or "", r.id), reverse=True)
        return runs

    def _emit(self, ids: list[str], reason: str) -> None:
        if not ids:
            return
        try:
            self.services.events.emit("agents.changed", {"ids": list(ids), "reason": reason})
        except Exception:  # noqa: BLE001 - events never break the flow
            log.debug("cloud-compute: agents.changed emit failed", exc_info=True)

    # ------------------------------------------------------------------ secrets / redaction

    def _secret_values(self, key_secret: str | None = None) -> list[str]:
        names = {DEFAULT_KEY_SECRET}
        if key_secret:
            names.add(key_secret)
        values: list[str] = []
        for name in names:
            try:
                value = self.services.secrets.get(name)
            except Exception:  # noqa: BLE001
                value = None
            if value:
                values.append(value)
        return values

    def _mask_fn(self, run: AgentRun) -> _Mask:
        """How to hide the model key in the text of ``run``.

        The mask hides the key value(s) the run's sandbox was started with (known in this
        process, else read back once from the container's own configuration, the same
        mechanism environment output uses), the current value of the run's key secret and of
        the default one, and secret-shaped tokens. Ordinary words are left alone. Raises
        :class:`_CannotMask` when the sandbox still exists but its values cannot be
        determined: text must not be shown or stored then.
        """
        key_secret = self._key_secret_of(run)
        named = sorted(set(self._secret_values(key_secret)), key=len, reverse=True)
        own = bool(self._secret_value_of(key_secret or DEFAULT_KEY_SECRET))
        base: Callable[[str], str] | None = None
        if run.mode == "hermes" and run.instance_id:
            assume = [{"name": KEY_ENV, "secret": key_secret or DEFAULT_KEY_SECRET}]
            try:
                base = self.services.sandboxes.output_mask(run.instance_id, assume=assume)
            except ApiError as exc:
                if exc.status != 404:
                    raise _CannotMask() from None      # it exists but cannot be read
            except Exception:  # noqa: BLE001 - fail closed
                raise _CannotMask() from None

        def fn(text: str) -> str:
            out = text or ""
            for value in named:
                out = out.replace(value, "[redacted]")
            return base(out) if base is not None else redact_tokens(out)

        return _Mask(fn=fn, read_back=base is not None, own=own)

    def _secret_value_of(self, name: str) -> str | None:
        try:
            return self.services.secrets.get(name) or None
        except Exception:  # noqa: BLE001
            return None

    def _withheld(self, run: AgentRun, what: str) -> str:
        return TEXT_WITHHELD_MESSAGE.format(what=what, run=run.id,
                                            host=self._host_name(run.host_id))

    def _hide(self, run: AgentRun, text: str, what: str = "Error", *, head: int | None = None,
              tail: int | None = None) -> str:
        """``text`` with the model key hidden, cut to its first ``head`` or last ``tail``
        characters AFTER hiding (a cut never leaves part of a key). When the key cannot be
        determined the plain sentence replaces the text."""
        try:
            mask = self._mask_fn(run).fn
        except _CannotMask:
            return self._withheld(run, what)
        out = mask(text or "")
        if head is not None:
            out = out[:head]
        if tail is not None:
            out = out[-tail:]
        return out

    def _present(self, run: AgentRun) -> AgentRun:
        """``run`` as it is shown (status, wait, tool and REST results).

        Text stored by this version was hidden before it was stored and is shown as is.
        A record of an earlier version (``text_masked`` unset) is hidden now: with the values
        read back from its sandbox, else with the key secret resolved by name. When a key was
        sent and neither works (sandbox gone and secret not set) its reply, log, error and note are
        replaced by a plain sentence; when the sandbox exists but cannot be read, likewise.
        """
        if run.text_masked or not (run.reply or run.last_log or run.error or run.stop_note):
            return run
        withheld = None            # (what) -> sentence
        try:
            mask = self._mask_fn(run)
            fn = mask.fn
            if run.mode == "hermes" and not mask.read_back and not mask.own:
                secret = self._key_secret_of(run) or DEFAULT_KEY_SECRET
                withheld = lambda what: OLD_TEXT_WITHHELD_MESSAGE.format(   # noqa: E731
                    what=what, run=run.id, secret=secret)
        except _CannotMask:
            named = self._secret_values(self._key_secret_of(run))
            fn = lambda text: redact_tokens(_replace_values(text, named))   # noqa: E731
            withheld = lambda what: self._withheld(run, what)               # noqa: E731
        shown = replace(run)
        if withheld is not None:
            shown.reply = withheld("Reply") if run.reply else run.reply
            shown.last_log = [withheld("Log")] if run.last_log else []
        else:
            shown.reply = fn(run.reply) if run.reply else run.reply
            shown.last_log = fn("\n".join(run.last_log)).split("\n") if run.last_log else []
        if withheld is not None:
            shown.error = withheld("Error") if run.error else run.error
            shown.stop_note = withheld("Note") if run.stop_note else run.stop_note
        else:
            shown.error = fn(run.error) if run.error else run.error
            shown.stop_note = fn(run.stop_note) if run.stop_note else run.stop_note
        return shown

    def _host_name(self, host_id: str) -> str:
        try:
            return getattr(self.services.store.get_host(host_id), "name", None) or host_id
        except Exception:  # noqa: BLE001
            return host_id

    def _key_secret_of(self, run: AgentRun) -> str | None:
        if getattr(run, "key_secret", None):
            return run.key_secret
        tpl = _TEMPLATE_IDS.get(run.template_id)
        try:
            tpl = self.services.templates.get(run.template_id)
        except Exception:  # noqa: BLE001 - only used for redaction
            pass
        agent = (tpl.agent or {}) if tpl is not None else {}
        name = agent.get("key_secret")
        return name if isinstance(name, str) else None

    # ------------------------------------------------------------------ hosts / templates

    def _host(self, host_id: str):
        if not isinstance(host_id, str) or not host_id:
            raise ApiError(400, "invalid", "host_id is required")
        host = self.services.store.get_host(host_id)
        if host is None:
            raise ApiError(404, "not_found", f"host {host_id} not found")
        if host.kind == "slurm":
            raise ApiError(400, "invalid",
                           f"cloud agents do not run on Slurm hosts ({host_id}); pick an SSH or local host")
        if not getattr(host.limits, "allow_agents", False):
            raise ApiError(403, "limit_reached",
                           f"cloud agents are disabled on host {host_id}; enable 'allow agents' in its limits")
        return host

    def ensure_templates(self, ids: Iterable[str] | None = None) -> list[str]:
        """Save missing built-in agent templates to the template store; returns saved ids.

        Local store only, never the network. A template that already exists (including a
        user's edited copy) is left alone, with one exception: a copy seeded by an earlier
        version, still byte-for-byte what that version wrote (the old hard-coded lifetime
        and nothing else changed), has its ttl/idle reset to unset so the Settings defaults
        apply. A copy with any other difference is the user's and stays as it is. The full
        seed (``ids=None``) runs once per manager.
        """
        if ids is None and self._templates_ready:
            return []
        wanted = set(ids) if ids is not None else set(_TEMPLATE_IDS)
        saved: list[str] = []
        store = self.services.templates
        for spec in AGENT_TEMPLATES:
            if spec.id not in wanted:
                continue
            try:
                existing = store.get(spec.id)
            except ApiError as exc:
                if exc.code != "not_found":
                    raise
            except KeyError:
                pass
            else:
                self._upgrade_legacy(store, spec, existing)
                continue
            store.save(copy.deepcopy(spec))
            saved.append(spec.id)
        if ids is None:
            self._templates_ready = True
        return saved

    @staticmethod
    def _upgrade_legacy(store: Any, spec: TemplateSpec, existing: TemplateSpec) -> None:
        """Replace a stored copy that is still exactly what an earlier version seeded.

        Earlier seeds differ from the current one only in the lifetime (a hard-coded ttl/idle)
        and in having no variable descriptions. A copy equal to one of those seeds was never
        edited, so it is replaced; a copy with any other difference is the user's.
        """
        lifetimes = [(spec.ttl_minutes, spec.idle_minutes)]
        if spec.id in _LEGACY_LIFETIMES:
            lifetimes.append(_LEGACY_LIFETIMES[spec.id])
        current = existing.to_dict()
        current.pop("builtin", None)
        texts = [(spec.name, spec.description)] + list(_LEGACY_TEXT.get(spec.id, []))
        for (name, description), (ttl, idle), described in (
                (t, life, d) for t in texts for life in lifetimes for d in (True, False)):
            if True:
                old = replace(copy.deepcopy(spec), ttl_minutes=ttl, idle_minutes=idle,
                              name=name, description=description)
                if not described:
                    for var in old.variables:
                        var.description = ""
                old_dict = old.to_dict()
                old_dict.pop("builtin", None)
                if current == old_dict:
                    if described and (ttl, idle) == lifetimes[0] and name == spec.name \
                            and description == spec.description:
                        return                      # already the current default
                    try:
                        store.save(copy.deepcopy(spec))
                    except Exception:  # noqa: BLE001 - a stale copy is harmless; never block a spawn
                        log.debug("cloud-compute: could not update the %s template", spec.id,
                                  exc_info=True)
                    return

    def _seed_templates(self) -> None:
        try:
            self.ensure_templates()
        except Exception:  # noqa: BLE001 - spawn seeds the one it needs; never block service setup
            log.debug("cloud-compute: could not seed the agent templates", exc_info=True)

    def _template(self, template_id: str) -> TemplateSpec:
        if not isinstance(template_id, str) or not template_id:
            raise ApiError(400, "invalid", "template is required")
        if template_id in _TEMPLATE_IDS:
            self.ensure_templates([template_id])
        try:
            return self.services.templates.get(template_id)
        except KeyError:
            raise ApiError(404, "not_found", f"template {template_id} not found") from None

    @staticmethod
    def _agent_cfg(tpl: TemplateSpec) -> dict:
        agent = tpl.agent if isinstance(tpl.agent, dict) else None
        if not agent or not agent.get("enabled", True) or agent.get("mode") not in AGENT_MODES:
            raise ApiError(400, "invalid",
                           f"template {tpl.id} is not an agent template (agent.mode must be hermes or script)")
        mode = agent["mode"]
        cfg = {
            "mode": mode,
            "model": agent.get("model"),
            "base_url": agent.get("base_url"),
            "key_secret": agent.get("key_secret") or DEFAULT_KEY_SECRET,
            "image_base": agent.get("image_base"),
        }
        return cfg

    def _resolve_endpoint(self, agent: dict | None) -> tuple[str | None, str | None]:
        """Model endpoint and id: the template's ``agent.base_url`` / ``agent.model`` first, then
        the ``agent_base_url`` / ``agent_model`` settings. ``None`` when neither sets one."""
        agent = agent if isinstance(agent, dict) else {}
        config = getattr(self.services, "config", None)
        reload = getattr(config, "reload", None)
        if callable(reload):                    # settings saved a moment ago apply to this run
            try:
                reload()
            except Exception:  # noqa: BLE001 - keep the values read last
                pass

        def pick(key: str, setting: str):
            value = agent.get(key)
            if isinstance(value, str):
                value = value.strip()
            if value:
                return value
            fallback = getattr(config, setting, "")
            return fallback.strip() if isinstance(fallback, str) and fallback.strip() else None

        return pick("base_url", "agent_base_url"), pick("model", "agent_model")

    # ------------------------------------------------------------------ saved model choices

    def models(self) -> list[AgentModel]:
        """Saved model choices, default first, then by name."""
        out: list[AgentModel] = []
        for row in self.services.store.list_records(MODEL_KIND):
            try:
                out.append(AgentModel.from_dict(row))
            except (TypeError, ValueError) as exc:
                log.warning("cloud-compute: skipping unreadable agent model: %s", exc)
        out.sort(key=lambda m: (not m.is_default, m.name.lower(), m.id))
        return out

    def model(self, model_id: str) -> AgentModel:
        if isinstance(model_id, str):
            model_id = model_id.strip()
        if not isinstance(model_id, str) or not MODEL_ID_RE.match(model_id):
            raise ApiError(404, "not_found", self._unknown_model_text(model_id))
        row = self.services.store.get_record(MODEL_KIND, model_id)
        if row is None:
            raise ApiError(404, "not_found", self._unknown_model_text(model_id))
        return AgentModel.from_dict(row)

    def _unknown_model_text(self, model_id: Any) -> str:
        """The relevant sentence and the valid ids, nothing else."""
        text = f"No saved cloud-agent model {model_id!r}."
        try:
            choices = self.models()
        except Exception:  # noqa: BLE001 - only used for a message
            choices = []
        if not choices:
            return f"{text} No models are saved yet. {SETUP_SENTENCE}"
        listed = "; ".join(f"{m.id} ({m.name}: {m.model} at {_endpoint_host(m.base_url)}"
                           f"{', default' if m.is_default else ''})" for m in choices)
        return f"{text} Valid ids: {listed}."

    def _choices_text(self) -> str:
        try:
            choices = self.models()
        except Exception:  # noqa: BLE001 - only used for a message
            choices = []
        if not choices:
            return NO_ENDPOINT_MESSAGE
        listed = "; ".join(f"{m.id} ({m.name}: {m.model} at {_endpoint_host(m.base_url)}"
                           f"{', default' if m.is_default else ''})" for m in choices)
        return f"Saved models: {listed}. Pass model_id, or {SETUP_SENTENCE[0].lower()}{SETUP_SENTENCE[1:]}"

    def model_view(self, m: AgentModel) -> dict:
        """What leaves the backend about a saved choice: never the key, only whether it is set."""
        data = m.to_dict()
        data["endpoint_host"] = _endpoint_host(m.base_url)
        data["key_set"] = self._key_set(m.key_secret)
        return data

    def _key_set(self, name: str | None) -> bool:
        try:
            return bool(name) and bool(self.services.secrets.is_set(name))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _valid_model_fields(name: Any, base_url: Any, model: Any, key_secret: Any
                            ) -> tuple[str, str, str, str]:
        name = name.strip() if isinstance(name, str) else ""
        base_url = base_url.strip() if isinstance(base_url, str) else ""
        model = model.strip() if isinstance(model, str) else ""
        key_secret = key_secret.strip() if isinstance(key_secret, str) and key_secret.strip() \
            else DEFAULT_KEY_SECRET
        if not base_url or not _URL_RE.match(base_url):
            raise ApiError(400, "invalid", "base_url must be an http(s) URL without credentials, "
                                           "for example https://llm.example.net/v1")
        if not model or not _MODEL_RE.match(model):
            raise ApiError(400, "invalid", "model must be a model id such as qwen3-coder "
                                           "(letters, digits and . _ : / @ + -)")
        if not _SECRET_NAME_RE.match(key_secret):
            raise ApiError(400, "invalid", "key_secret must be a secret NAME such as "
                                           "CC_AGENT_API_KEY (letters, digits and _), never the key")
        if not name:
            name = f"{model} at {_endpoint_host(base_url)}"
        return name[:80], base_url, model, key_secret

    def save_model(self, data: dict) -> AgentModel:
        """Create (no ``id``) or update a saved model choice. The first one becomes the default."""
        data = data if isinstance(data, dict) else {}
        name, base_url, model, key_secret = self._valid_model_fields(
            data.get("name"), data.get("base_url"), data.get("model"), data.get("key_secret"))
        with self._lock:
            model_id = data.get("id")
            existing = self.model(model_id) if model_id not in (None, "") else None
            others = [m for m in self.models() if existing is None or m.id != existing.id]
            want_default = data.get("is_default")
            if want_default is not None and not isinstance(want_default, bool):
                raise ApiError(400, "invalid", "is_default must be true or false")
            is_default = (bool(want_default) if want_default is not None
                          else (existing.is_default if existing else not others))
            if not others:
                is_default = True
            rec = AgentModel(id=existing.id if existing else new_id("m"), name=name,
                             base_url=base_url, model=model, key_secret=key_secret,
                             is_default=is_default)
            if is_default:
                for m in others:
                    if m.is_default:
                        m.is_default = False
                        self.services.store.put_record(MODEL_KIND, m.id, m.to_dict())
            self.services.store.put_record(MODEL_KIND, rec.id, rec.to_dict())
        self._emit_models()
        return rec

    def delete_model(self, model_id: str) -> dict:
        with self._lock:
            m = self.model(model_id)
            self.services.store.delete_record(MODEL_KIND, m.id)
            rest = self.models()
            if m.is_default and rest and not any(r.is_default for r in rest):
                rest[0].is_default = True
                self.services.store.put_record(MODEL_KIND, rest[0].id, rest[0].to_dict())
        self._emit_models()
        return {"deleted": m.id}

    def set_default_model(self, model_id: str) -> AgentModel:
        with self._lock:
            chosen = self.model(model_id)
            for m in self.models():
                want = m.id == chosen.id
                if m.is_default != want:
                    m.is_default = want
                    self.services.store.put_record(MODEL_KIND, m.id, m.to_dict())
            chosen.is_default = True
        self._emit_models()
        return chosen

    def default_model(self) -> AgentModel | None:
        return next((m for m in self.models() if m.is_default), None)

    def _emit_models(self) -> None:
        try:
            self.services.events.emit("agents.changed", {"ids": [], "reason": "models"})
        except Exception:  # noqa: BLE001
            log.debug("cloud-compute: agents.changed emit failed", exc_info=True)

    #: Replaced in tests: returns the :class:`agent_models.ProfileApi` to read the profile with.
    _profile_api: Any = staticmethod(agent_models.hermes_profile_api)

    def _profile_models(self) -> dict:
        """Endpoints of this Hermes profile (see :mod:`agent_models`); never raises and never
        holds a key value."""
        try:
            api = self._profile_api()
            return agent_models.detect(api, self._key_set)
        except Exception:  # noqa: BLE001 - no Hermes (or no readable profile): offers nothing
            log.debug("cloud-compute: could not read the Hermes profile's models", exc_info=True)
            return {"detected": [], "unavailable": []}

    def detected_models(self) -> list[dict]:
        """Model endpoints of this Hermes profile that use a key stored under a secret NAME:
        ``{id, name, source, base_url, endpoint_host, models, model, key_secret, key_set}``.
        To use one, save it as a model choice (name, base_url, model, key_secret); the run's
        approval then names the endpoint host and the key name."""
        return self._profile_models()["detected"]

    def unavailable_models(self) -> list[dict]:
        """Endpoints of this Hermes profile that cloud agents cannot use, each with a plain
        reason: ``{name, source, endpoint_host, kind, reason}``."""
        return self._profile_models()["unavailable"]

    # ------------------------------------------------------------------ endpoint resolution

    def _resolve_model(self, cfg: dict, model_id: str | None,
                       endpoint: dict | None = None) -> dict:
        """Endpoint, model and key secret for a run, in the documented order:
        ``model_id`` -> the template's agent.base_url/model -> the default saved choice ->
        the settings -> ``needs_setup``.

        ``endpoint`` (a run started again, see :meth:`rerun`) overrides the result: ``model``
        replaces only the model id; ``base_url`` must be the endpoint of a saved choice, whose
        key secret is then used (a key is never sent to an address nobody saved)."""
        try:
            chosen = self._resolve_model_base(cfg, model_id)
        except ApiError as exc:
            if exc.code != "needs_setup" or not endpoint:
                raise
            # nothing is set up, but the override may name the endpoint (and the model)
            chosen = {"base_url": None, "model": None, "key_secret": cfg["key_secret"],
                      "model_id": None, "source": "override"}
        if not endpoint:
            return chosen
        base_url = (endpoint.get("base_url") or "").strip()
        model = (endpoint.get("model") or "").strip()
        if base_url:
            saved = next((m for m in self.models()
                          if m.base_url.rstrip("/") == base_url.rstrip("/")), None)
            if saved is not None:
                chosen = {"base_url": saved.base_url, "model": saved.model,
                          "key_secret": saved.key_secret, "model_id": saved.id,
                          "source": "model_id"}
            else:
                # An endpoint the user's Hermes profile defines, with a key stored under a
                # secret name, is as good as a saved one. Nothing else ever gets a key.
                found = next((d for d in self.detected_models()
                              if str(d.get("base_url", "")).rstrip("/") == base_url.rstrip("/")),
                             None)
                if found is None:
                    raise ApiError(
                        400, "invalid",
                        f"No saved cloud-agent model and no endpoint of this Hermes profile "
                        f"uses {base_url}. A model key is only sent to an endpoint that was "
                        f"saved or is defined in the profile. {SETUP_SENTENCE} Then pass its "
                        "model_id.")
                chosen = {"base_url": found["base_url"], "model": found.get("model"),
                          "key_secret": found["key_secret"], "model_id": None,
                          "source": "override"}
        if model:
            chosen = dict(chosen, model=model, model_id=None, source="override")
        return chosen

    def _resolve_model_base(self, cfg: dict, model_id: str | None) -> dict:
        if model_id:
            m = self.model(model_id)
            return {"base_url": m.base_url, "model": m.model, "key_secret": m.key_secret,
                    "model_id": m.id, "source": "model_id"}
        tpl_url, tpl_model = cfg.get("base_url"), cfg.get("model")
        if (isinstance(tpl_url, str) and tpl_url.strip()) or (isinstance(tpl_model, str) and tpl_model.strip()):
            base_url, model = self._resolve_endpoint(cfg)
            return {"base_url": base_url, "model": model, "key_secret": cfg["key_secret"],
                    "model_id": None, "source": "template"}
        default = None
        try:
            default = self.default_model()
        except Exception:  # noqa: BLE001 - a broken store row never blocks the settings fallback
            log.debug("cloud-compute: could not read the saved agent models", exc_info=True)
        if default is not None:
            return {"base_url": default.base_url, "model": default.model,
                    "key_secret": default.key_secret, "model_id": default.id, "source": "default"}
        base_url, model = self._resolve_endpoint({})
        if base_url or model:
            return {"base_url": base_url, "model": model, "key_secret": cfg["key_secret"],
                    "model_id": None, "source": "settings"}
        raise ApiError(409, "needs_setup", self._choices_text())

    def _check_template(self, tpl: TemplateSpec, cfg: dict, host: Any,
                        model_id: str | None = None, endpoint: dict | None = None) -> None:
        if tpl.kind != "container":
            raise ApiError(400, "invalid", f"template {tpl.id} is not a container template")
        if tpl.host_kinds and host.kind not in fitting_host_kinds(tpl.host_kinds):
            raise ApiError(400, "invalid", f"template {tpl.id} does not run on {host.kind} hosts")
        base = cfg.get("image_base")
        if base is not None and (not isinstance(base, str) or not _SAFE_TOKEN.match(base)):
            raise ApiError(400, "invalid", f"template {tpl.id}: agent.image_base is not a valid image name")
        if cfg["mode"] == "script":
            if tpl.variables:
                raise ApiError(400, "invalid",
                               f"template {tpl.id}: script mode sends no variables; remove them")
            if model_id:
                self.model(model_id)          # a wrong id is still an error
            return
        key_secret = cfg["key_secret"]
        if not isinstance(key_secret, str) or not _SECRET_NAME_RE.match(key_secret):
            raise ApiError(400, "invalid", f"template {tpl.id}: agent.key_secret is not a valid secret name")
        ok = (len(tpl.variables) == 1 and tpl.variables[0].name == KEY_ENV
              and isinstance(tpl.variables[0].source, str)
              and tpl.variables[0].source.startswith("secret:")
              and tpl.variables[0].scope == "runtime")
        if not ok:
            raise ApiError(400, "invalid",
                           f"template {tpl.id}: hermes mode sends exactly one variable, "
                           f"{KEY_ENV} = secret:{key_secret} (scope runtime)")
        var_hosts = tpl.variables[0].hosts
        if var_hosts is not None and host.id not in var_hosts:
            raise ApiError(400, "invalid", f"the model key is not allowed on host {host.id}")
        chosen = self._resolve_model(cfg, model_id, endpoint)
        base_url, model = chosen["base_url"], chosen["model"]
        where = {"model_id": "the saved model", "default": "the default saved model",
                 "template": f"template {tpl.id}", "settings": "Settings",
                 "override": "the chosen model"}[chosen["source"]]
        if not base_url or not model:
            raise ApiError(409, "needs_setup",
                           f"{where} sets {'no endpoint' if not base_url else 'no model id'}. "
                           + self._choices_text())
        if not isinstance(base_url, str) or not _URL_RE.match(base_url):
            raise ApiError(400, "invalid",
                           f"the model endpoint in {where} must be an http(s) URL without credentials")
        if not isinstance(model, str) or not _MODEL_RE.match(model):
            raise ApiError(400, "invalid", f"the model id in {where} is not a valid model id")
        key_secret = chosen["key_secret"]
        if not isinstance(key_secret, str) or not _SECRET_NAME_RE.match(key_secret):
            raise ApiError(400, "invalid", f"the key secret name in {where} is not valid")
        cfg["base_url"], cfg["model"], cfg["key_secret"] = base_url, model, key_secret
        cfg["model_id"] = chosen["model_id"]
        if not self.services.secrets.is_set(key_secret):
            hint = (" Enter it in Settings, Own Compute, Cloud agent model key."
                    if key_secret == DEFAULT_KEY_SECRET else
                    f" Add {key_secret} to this Hermes profile's secrets.")
            raise ApiError(409, "needs_setup",
                           f"The model key secret {key_secret} is not set.{hint}")

    # ------------------------------------------------------------------ host / container access

    def _runtime_for_host(self, host: Any) -> str:
        if host.runtime_pref in ("podman", "docker"):
            return host.runtime_pref
        try:
            state = self.services.store.get_host_state(host.id)
        except Exception:  # noqa: BLE001
            state = None
        rt = getattr(getattr(state, "capabilities", None), "runtime", None)
        return rt if rt in ("podman", "docker") else "podman"

    def _transport(self, host_id: str):
        host = self.services.store.get_host(host_id)
        if host is None:
            raise ApiError(404, "not_found", f"host {host_id} not found")
        return host, self.services.transports.for_host(host)

    def _instance_runtime(self, run: AgentRun, host: Any) -> str:
        rt = None
        try:
            rt = getattr(self.services.sandboxes.get(run.instance_id), "runtime", None)
        except Exception:  # noqa: BLE001 - fall back to the host preference
            rt = None
        return rt if rt in ("podman", "docker") else self._runtime_for_host(host)

    def _cexec(self, run: AgentRun, script: str, args: Iterable[str] = (), *,
               stdin: bytes | None = None, timeout: float = 120, workdir: str = WORK):
        if not run.instance_id or not _SAFE_TOKEN.match(run.instance_id):
            raise ApiError(409, "conflict", f"agent run {run.id} has no sandbox")
        host, transport = self._transport(run.host_id)
        rt = self._instance_runtime(run, host)
        argv = runtime_for(rt).exec_argv(container_name(run.instance_id),
                                         ["sh", "-c", script, "sh", *[str(a) for a in args]],
                                         workdir=workdir)
        argv.insert(2, "-i")                   # keep stdin open for file uploads
        return transport.run(argv, stdin=stdin, timeout=timeout)

    def _tail_of(self, run: AgentRun, res: Any) -> str:
        """The last 300 characters a failed sandbox command printed, key hidden first."""
        return self._hide(run, (res.stderr or res.stdout or "").strip(), "Error", tail=300)

    def _put_file(self, run: AgentRun, path: str, data: bytes, *, timeout: float = 120) -> None:
        res = self._cexec(run, _PUT_FILE, [path], stdin=data, timeout=timeout)
        if res.rc != 0:
            raise ApiError(502, "internal", f"could not write {path} in the sandbox: "
                                            f"{self._tail_of(run, res)}")

    def ensure_image(self, host: Any, tpl: TemplateSpec, cfg: dict | None = None) -> bool:
        """Build the agent image on the host when it is missing. True if a build ran."""
        tag = tpl.image
        if not tag.startswith(AGENT_IMAGE_PREFIX) or not _SAFE_TOKEN.match(tag):
            return False
        cfg = cfg or self._agent_cfg(tpl)
        runtime = runtime_for(self._runtime_for_host(host))
        transport = self.services.transports.for_host(host)
        if transport.run(runtime.image_exists_argv(tag), timeout=60).rc == 0:
            return False
        try:
            containerfile = CONTAINERFILE.read_bytes()
        except OSError as exc:
            raise ApiError(503, "unavailable", f"agent image Containerfile missing: {exc}") from exc
        # <rt> build -t TAG [--build-arg BASE=...] -   : the Containerfile on stdin, no build
        # context (it copies no files). A plain argv: no shell is needed on the host, so a
        # local Windows host without a POSIX sh builds it too.
        build = runtime.build_argv(tag)
        if cfg.get("image_base"):
            build[4:4] = ["--build-arg", f"BASE={cfg['image_base']}"]
        res = transport.run(build, stdin=containerfile, timeout=1800)
        if res.rc != 0:
            tail = "\n".join((res.stderr or res.stdout or "").strip().splitlines()[-8:])
            raise ApiError(502, "internal", f"building {tag} on {host.id} failed: {tail}")
        return True

    # ------------------------------------------------------------------ repo helpers

    @staticmethod
    def _repo(repo_path: str) -> Path:
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise ApiError(400, "invalid", "repo_path is required")
        path = Path(repo_path).expanduser()
        if not path.is_dir() or not gitrelay.is_repo(path):
            raise ApiError(400, "invalid", f"{repo_path} is not a git repository")
        try:
            top = gitrelay.git(path, "rev-parse", "--show-toplevel").decode("utf-8", "replace").strip()
        except gitrelay.GitError as exc:
            raise ApiError(400, "invalid", f"{repo_path}: {_err_text(exc)}") from exc
        if not top:
            raise ApiError(400, "invalid", f"{repo_path} has no working tree")
        return Path(top).resolve()

    @staticmethod
    def _branch_name(name: str) -> str:
        if not isinstance(name, str) or not name or name.startswith("-"):
            raise ApiError(400, "invalid", f"invalid branch name {name!r}")
        try:
            return gitrelay.safe_branch_name(name)
        except gitrelay.GitError:
            raise ApiError(400, "invalid", f"invalid branch name {name!r}") from None

    @staticmethod
    def _current_branch(repo: Path) -> str | None:
        try:
            out = gitrelay.git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        except gitrelay.GitError:
            return None
        name = out.decode("utf-8", "replace").strip()
        return name or None

    # ------------------------------------------------------------------ spawn

    @staticmethod
    def _max_turns(value: Any) -> int:
        if isinstance(value, bool):
            raise ApiError(400, "invalid", "max_turns must be an integer")
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ApiError(400, "invalid", "max_turns must be an integer") from None
        if not 1 <= value <= MAX_TURNS_LIMIT:
            raise ApiError(400, "invalid", f"max_turns must be between 1 and {MAX_TURNS_LIMIT}")
        return value

    def spawn(self, goal: str, host_id: str, *, repo_path: str, branch: str | None = None,
              new_branch: bool = True, base: str = "HEAD", template: str = "hermes-agent",
              max_turns: int = 30, session_id: str | None = None, model_id: str | None = None,
              criteria: str | None = None, endpoint: dict | None = None,
              rerun_of: str | None = None) -> AgentRun:
        """Start an agent run. Validation errors raise ``ApiError``; once the run is
        recorded, later failures return the run with status ``failed`` and ``error`` (the
        record keeps goal, host, repo, branch, template, max turns and model; :meth:`rerun`
        starts a new run from it). ``endpoint`` and ``rerun_of`` are for :meth:`rerun`."""
        goal = (goal or "").strip() if isinstance(goal, str) else ""
        if not goal:
            raise ApiError(400, "invalid", "task is required: say what the agent should do")
        criteria = criteria.strip() if isinstance(criteria, str) and criteria.strip() else None
        if len(goal) + len(criteria or "") > MAX_GOAL_CHARS:
            raise ApiError(400, "invalid", f"task and criteria are longer than {MAX_GOAL_CHARS} characters")
        max_turns = self._max_turns(max_turns)
        model_id = model_id.strip() if isinstance(model_id, str) and model_id.strip() else None
        host = self._host(host_id)
        repo = self._repo(repo_path)
        tpl = self._template(template or "hermes-agent")
        cfg = self._agent_cfg(tpl)
        self._check_template(tpl, cfg, host, model_id, endpoint)

        run_id = new_id("a")
        if new_branch:
            if branch:
                branch = self._branch_name(branch)
                if gitrelay.ref_exists(repo, f"refs/heads/{branch}"):
                    raise ApiError(409, "conflict",
                                   f"branch {branch} already exists in {repo.name}; pass new_branch=false "
                                   "to continue it or choose another name")
            else:
                branch = self._branch_name(_slug(goal))
                if gitrelay.ref_exists(repo, f"refs/heads/{branch}"):
                    branch = self._branch_name(f"{branch}-{run_id[2:6]}")
            if not isinstance(base, str) or not base or base.startswith("-"):
                raise ApiError(400, "invalid", f"invalid base {base!r}")
            try:
                start_sha = gitrelay.rev_parse(repo, base)
            except gitrelay.GitError:
                raise ApiError(400, "invalid", f"base {base} is not a commit in {repo.name}") from None
        else:
            branch = branch or self._current_branch(repo)
            if not branch:
                raise ApiError(400, "invalid", "no branch given and the repository HEAD is detached")
            branch = self._branch_name(branch)
            if not gitrelay.ref_exists(repo, f"refs/heads/{branch}"):
                raise ApiError(400, "invalid",
                               f"branch {branch} does not exist in {repo.name}; use new_branch=true")
            start_sha = gitrelay.rev_parse(repo, f"refs/heads/{branch}")

        run = AgentRun(
            id=run_id, instance_id="", host_id=host.id, goal=goal, repo_path=str(repo),
            branch=branch, base_sha=start_sha, head_sha=start_sha, commits_ahead=0,
            status="starting", started_at=utc_now_iso(), ended_at=None, session_id=session_id,
            last_log=[], mode=cfg["mode"], template_id=tpl.id, max_turns=max_turns,
            criteria=criteria, new_branch=bool(new_branch), rerun_of=rerun_of, text_masked=True,
        )
        if cfg["mode"] == "hermes":
            run.model = cfg["model"]
            run.endpoint_host = _endpoint_host(cfg["base_url"]) or None
            run.model_id = cfg.get("model_id")
            run.key_secret = cfg["key_secret"]
        else:
            run.model_id = model_id
        with self._lock:
            self._spawning.add(run_id)
        try:
            with self._run_lock(run_id):
                self._save(run)
                self._emit([run_id], "spawned")
                try:
                    self._start(run, host, tpl, cfg, self._first_prompt(goal, criteria), max_turns)
                except Exception as exc:  # noqa: BLE001 - recorded on the run
                    self._fail(run, self._start_error(exc, host), cfg.get("key_secret"), destroy=True)
                    return run
                run.status = "running"
                self._save(run)
                self._emit([run_id], "running")
                return run
        finally:
            with self._lock:
                self._spawning.discard(run_id)

    def _start(self, run: AgentRun, host: Any, tpl: TemplateSpec, cfg: dict, goal: str,
               max_turns: int) -> None:
        repo = Path(run.repo_path)
        base = run.base_sha if not gitrelay.ref_exists(repo, f"refs/heads/{run.branch}") else run.branch
        data, base_sha = gitrelay.bundle_bytes(repo, run.branch, base)
        run.base_sha = run.head_sha = base_sha
        self.ensure_image(host, tpl, cfg)
        # The key variable is read from the secret the chosen model names (the sandbox
        # manager resolves template variables at container start, inside this context).
        overrides = {KEY_ENV: cfg["key_secret"]} if cfg["mode"] == "hermes" else {}
        with secrets_mod.variable_sources(overrides):
            inst = self.services.sandboxes.spawn(tpl.id, host.id, name=f"agent-{run.id}",
                                                 session_id=run.session_id)
        run.instance_id = inst.id
        self._save(run)
        run.state_dir = self._pick_state_dir(run)
        state = state_dir_of(run)
        self._save(run)
        self._put_file(run, f"{state}/in.bundle", data, timeout=900)
        self._put_file(run, f"{state}/goal.txt", goal.encode("utf-8"))
        self._put_file(run, f"{state}/turn", b"1\n")
        self._put_file(run, f"{state}/base", (base_sha + "\n").encode("ascii"))
        res = self._cexec(run, _setup_script(state), [run.branch], timeout=300)
        if res.rc != 0:
            raise ApiError(502, "internal", "could not unpack the branch in the sandbox: "
                                            f"{self._tail_of(run, res)}")
        if cfg["mode"] == "hermes":
            self._put_file(run, f"{hermes_home_of(run)}/config.yaml",
                           _hermes_config(cfg["base_url"], cfg["model"]).encode("utf-8"))
        self._put_file(run, f"{state}/run.sh",
                       _runner_script(run.id, cfg["mode"], max_turns, state).encode("utf-8"))
        res = self._cexec(run, _launch_script(state), timeout=60)
        if res.rc != 0:
            raise ApiError(502, "internal", "could not start the agent runner: "
                                            f"{self._tail_of(run, res)}")

    def _pick_state_dir(self, run: AgentRun) -> str:
        """Create the run state folder outside the work folder; returns its path."""
        res = self._cexec(run, _PICK_STATE, timeout=60)
        path = (res.stdout or "").strip().splitlines()[-1].strip() if (res.stdout or "").strip() else ""
        if res.rc != 0 or not _STATE_RE.match(path) or path == WORK or path.startswith(WORK + "/"):
            raise ApiError(502, "internal", "could not create a private folder for the agent in "
                                            f"the sandbox: {self._tail_of(run, res)}")
        return path

    @staticmethod
    def _first_prompt(goal: str, criteria: str | None) -> str:
        return f"{goal}\n\nDone when: {criteria}" if criteria else goal

    _LIMIT_RE = re.compile(r"already runs (\d+) of (\d+) instances")

    @classmethod
    def _start_error(cls, exc: BaseException, host: Any) -> str:
        """Run error text; the host environment limit is said in plain words."""
        if isinstance(exc, ApiError) and exc.code == "limit_reached":
            m = cls._LIMIT_RE.search(exc.message or "")
            if m:
                name = getattr(host, "name", None) or getattr(host, "id", "the host")
                return (f"{name} already runs {m.group(1)} of {m.group(2)} environments. Stop or "
                        "delete one, or raise Max environments for this host.")
        return _err_text(exc)

    def _fail(self, run: AgentRun, message: str, key_secret: str | None = None, *,
              destroy: bool = False, emit: bool = True, fixed: bool = False) -> None:
        """Record a failure. ``fixed``: the message is a sentence of this plugin with no
        output of the sandbox in it, so it needs no hiding. Otherwise the model key is hidden
        in it (or the text is replaced by the plain withheld sentence) before it is stored.
        ``key_secret`` is kept for callers; the run's own secret name is used."""
        run.status = "failed"
        run.error = message[:1000] if fixed else self._hide(run, message, "Error", head=1000)
        run.ended_at = run.ended_at or utc_now_iso()
        if destroy and run.instance_id:
            try:
                self.services.sandboxes.destroy(run.instance_id)
            except Exception as exc:  # noqa: BLE001 - best effort; the reaper also cleans up
                log.warning("cloud-compute: could not destroy sandbox %s: %s",
                            run.instance_id, type(exc).__name__)
        try:
            self._save(run)
        except Exception:  # noqa: BLE001
            log.warning("cloud-compute: could not save failed agent run %s", run.id)
        if emit:
            self._emit([run.id], "failed")

    # ------------------------------------------------------------------ status / poll

    def _host_blocked(self, host_id: str) -> bool:
        with self._lock:
            entry = self._host_fail.get(host_id)
        return entry is not None and self._clock() < entry[1]

    def _note_host(self, host_id: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self._host_fail.pop(host_id, None)
                return
            fails = self._host_fail.get(host_id, (0, 0.0))[0] + 1
            delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** (fails - 1)))
            self._host_fail[host_id] = (fails, self._clock() + delay)

    def _refresh(self, run: AgentRun, exec_timeout: float = 60.0) -> str | None:
        """Refresh one active run in place (one exec). Returns the change reason or None."""
        if run.status not in ACTIVE_STATUSES:
            return None
        if not run.instance_id:
            with self._lock:
                spawning = run.id in self._spawning
            started = parse_iso(run.started_at)
            age = (time.time() - started.timestamp()) if started else STARTING_STALE_S + 1
            if not spawning and age > STARTING_STALE_S:
                self._fail(run, "the run did not finish starting", emit=False, fixed=True)
                return "failed"
            return None
        key_secret = self._key_secret_of(run)
        try:        # keep the sandbox "active" so the idle reaper leaves a running agent alone
            self.services.sandboxes.touch(run.instance_id)
        except Exception:  # noqa: BLE001 - activity tracking is optional
            pass
        try:
            res = self._cexec(run, _poll_script(state_dir_of(run)), [run.base_sha, run.branch],
                              timeout=exec_timeout)
        except Exception:
            self._note_host(run.host_id, False)
            raise
        self._refreshed[run.id] = time.monotonic()
        self._note_host(run.host_id, not (res.timed_out or res.rc in (124, 255)))
        if res.rc != 0:
            if res.rc == 3 or _gone(res.stderr):
                self._fail(run, "the sandbox is gone or was reset", key_secret, emit=False, fixed=True)
                self._finished(run)
                return "failed"
            log.debug("cloud-compute: agent poll for %s returned rc %s", run.id, res.rc)
            return None
        info, rest = _parse_kv(res.stdout)
        log_lines, reply_lines = rest, None
        if "==reply" in rest:
            cut = rest.index("==reply")
            log_lines, reply_lines = rest[:cut], rest[cut + 1:]
        before = (run.status, run.head_sha, run.commits_ahead, list(run.last_log), run.exit_code)
        head = info.get("head") or None
        if head:
            run.head_sha = head
        try:
            run.commits_ahead = int(info.get("ahead", run.commits_ahead))
        except ValueError:
            pass
        try:
            mask = self._mask_fn(run).fn
        except _CannotMask:
            # The key this run's sandbox holds cannot be determined, so nothing it printed
            # is stored or shown. The state (and the reply) is read again next time.
            run.last_log = [self._withheld(run, "Log")]
            if (run.status, run.head_sha, run.commits_ahead, list(run.last_log),
                    run.exit_code) != before:
                self._save(run)
            return None
        # Whole blocks at once: a private key block spans lines.
        log_lines = mask("\n".join(log_lines[-LOG_LINES:])).split("\n") if log_lines else []
        reply_lines = mask("\n".join(reply_lines)).split("\n") if reply_lines else reply_lines
        run.last_log = list(log_lines)
        if not run.text_masked:        # an older record: its other stored text, hidden now
            run.error = mask(run.error) if run.error else run.error
            run.stop_note = mask(run.stop_note) if run.stop_note else run.stop_note
            if run.reply:
                run.reply = mask(run.reply)
        run.text_masked = True
        state = info.get("exit", "")
        reason = None
        if state == "running":
            run.status = "running"
        elif state == "lost":
            run.status = "failed"
            run.error = "the agent runner stopped without an exit code"
            run.ended_at = run.ended_at or utc_now_iso()
        elif state:
            try:
                code = int(state)
            except ValueError:
                code = -1
            run.exit_code = code
            run.status = "done" if code == 0 else "failed"
            reply_text = "\n".join(reply_lines or [])
            if code != 0:
                run.error = f"the agent exited with code {code}"
                run.error_kind = None
                found = None
                if run.mode == "hermes":
                    found = diagnose_failure(
                        reply_text + "\n" + "\n".join(log_lines[-LOG_LINES:]),
                        model=run.model, endpoint=run.endpoint_host, key_secret=key_secret,
                        host_id=run.host_id, max_turns=run.max_turns)
                if found is not None:
                    run.error_kind = found[0]
                    run.error = mask(found[1])[:1000]
                    reply_text = _drop_hermes_advice(reply_text)   # /retry, /model: not here
            run.ended_at = run.ended_at or utc_now_iso()
            run.reply = self._clean_reply(reply_text, mask, state_dir_of(run))
            if run.mode == "script" and code == 0:
                run.reply = SCRIPT_REPLY
        after = (run.status, run.head_sha, run.commits_ahead, list(run.last_log), run.exit_code)
        if after != before:
            self._save(run)
            reason = run.status if run.status in ("done", "failed") and before[0] != run.status \
                else "running"
            if before[0] in ACTIVE_STATUSES and run.status not in ACTIVE_STATUSES:
                self._finished(run)
        return reason

    def _clean_reply(self, text: str, mask: Callable[[str], str],
                     state_dir: str = LEGACY_STATE_DIR) -> str | None:
        """The agent's answer: the key hidden (``mask``), then cut at
        :data:`REPLY_MAX_CHARS` with a note."""
        text = strip_tool_previews((text or "").strip())
        if not text:
            return None
        text = mask(text)
        if len(text) > REPLY_MAX_CHARS:
            text = (text[:REPLY_MAX_CHARS].rstrip()
                    + f"\n\n[Reply cut at {REPLY_MAX_CHARS} characters; the full text is in "
                      f"{state_dir}/reply.txt in the sandbox.]")
        return text

    def _finished(self, run: AgentRun) -> None:
        """``agent.finished`` plus a user notification, once per turn that ends."""
        # A failed run is announced with its cause sentence, not the provider's raw reply.
        preview = _short((run.error if run.status == "failed" and run.error else run.reply or run.error) or "", 200)
        try:
            self.services.events.emit("agent.finished", {
                "id": run.id, "status": run.status, "host_id": run.host_id,
                "reply_preview": preview})
            ok = run.status == "done"
            host = None
            try:
                host = self.services.store.get_host(run.host_id)
            except Exception:  # noqa: BLE001
                host = None
            where = getattr(host, "name", None) or run.host_id
            self.services.events.emit("notify", {
                "level": "info" if ok else "error",
                "title": f"Cloud agent {run.id} {'finished' if ok else 'failed'}",
                "body": (f"Run on {where} {'finished' if ok else 'failed'}"
                         f"{f' with {run.commits_ahead} new commit(s)' if ok else ''}."
                         + (f" {preview}" if preview else "")),
                "ref": {"kind": "agent", "id": run.id},
            })
        except Exception:  # noqa: BLE001 - events never break the flow
            log.debug("cloud-compute: agent.finished emit failed", exc_info=True)

    def status(self, run_id: str | None = None) -> list[AgentRun]:
        """Runs (newest first), or one run. Active runs are refreshed at most every 10 s."""
        if not self._templates_ready:             # retry a seed that failed at provide time
            self._seed_templates()
        runs = [self._load(run_id)] if run_id is not None else self._all()
        now = time.monotonic()
        due = [r for r in runs if r.status in ACTIVE_STATUSES
               and now - self._refreshed.get(r.id, 0.0) >= STATUS_REFRESH_S]
        self._refresh_batch(due)
        return [self._present(r) for r in runs]

    def _refresh_batch(self, runs: list[AgentRun]) -> None:
        """Refresh ``runs`` within one time budget; hosts that failed recently are skipped
        (with backoff). Runs left over are picked up by the next tick. Never raises."""
        changed: dict[str, list[str]] = {}
        started = self._clock()
        for run in runs:
            remaining = POLL_BUDGET_S - (self._clock() - started)
            if remaining < POLL_MIN_EXEC_S:
                break
            if run.instance_id and self._host_blocked(run.host_id):
                continue
            try:
                with self._run_lock(run.id):
                    self._sync(run)          # another thread may have refreshed it meanwhile
                    reason = self._refresh(run, exec_timeout=min(60.0, remaining))
            except Exception as exc:  # noqa: BLE001 - one bad run never stops the others
                log.debug("cloud-compute: agent refresh of %s failed: %s", run.id,
                          type(exc).__name__)
                continue
            if reason:
                changed.setdefault(reason, []).append(run.id)
        for reason, ids in changed.items():
            self._emit(ids, reason)

    def poll(self) -> list[AgentRun]:
        """Refresh active runs (one exec each, about 8 s in total, skipping hosts that failed
        recently); emits ``agents.changed``. Never raises."""
        try:
            runs = self._all(ACTIVE_STATUSES)
        except Exception:  # noqa: BLE001
            log.debug("cloud-compute: agent poll could not list runs", exc_info=True)
            return []
        self._refresh_batch(runs)
        return [self._present(r) for r in runs]

    def has_active(self) -> bool:
        try:
            return bool(self.services.store.list_records(RECORD_KIND, statuses=set(ACTIVE_STATUSES)))
        except Exception:  # noqa: BLE001
            return False

    def _sync(self, run: AgentRun) -> None:
        """Bring ``run`` (an object loaded earlier) up to the stored record."""
        try:
            data = self.services.store.get_record(RECORD_KIND, run.id)
            fresh = AgentRun.from_dict(data) if data is not None else None
        except Exception:  # noqa: BLE001 - keep the object as it is
            return
        if fresh is not None and fresh != run:
            run.__dict__.update(fresh.__dict__)

    # ------------------------------------------------------------------ wait

    def wait(self, run_id: str, wait_s: float) -> tuple[AgentRun, bool, float]:
        """Block until the run leaves ``starting``/``running`` or ``wait_s`` passes:
        ``(run, finished, waited_s)``. Checks the sandbox every :data:`WAIT_POLL_S` (each check
        also marks the sandbox active). The caller caps ``wait_s``."""
        start = self._clock()
        deadline = start + max(0.0, float(wait_s))
        run = self._load(run_id)
        while run.status in ACTIVE_STATUSES:
            if self._clock() >= deadline:
                break
            try:
                with self._run_lock(run.id):
                    run = self._load(run_id)
                    if run.status in ACTIVE_STATUSES:
                        left = max(5.0, deadline - self._clock())
                        reason = self._refresh(run, exec_timeout=min(60.0, left))
                        if reason:
                            self._emit([run.id], reason)
            except ApiError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed check is retried next time
                log.debug("cloud-compute: wait check of %s failed: %s", run_id, type(exc).__name__)
            if run.status not in ACTIVE_STATUSES:
                break
            left = deadline - self._clock()
            if left <= 0:
                break
            self._sleep(min(WAIT_POLL_S, left))
        return (self._present(run), run.status not in ACTIVE_STATUSES,
                round(self._clock() - start, 1))

    # ------------------------------------------------------------------ follow-up

    def _gone_error(self, run: AgentRun) -> ApiError:
        if run.collected_ref:
            short = run.collected_ref[len("refs/heads/"):] if run.collected_ref.startswith(
                "refs/heads/") else run.collected_ref
            how = (f"Start a new run from the collected branch: cloud_agent_spawn with "
                   f"repo_path={run.repo_path!r} and base={short!r}.")
        else:
            how = ("Nothing was collected from it. Start a new run with cloud_agent_spawn on "
                   f"repo {run.repo_path}.")
        return ApiError(409, "conflict",
                        f"The sandbox of cloud agent run {run.id} no longer exists, so the agent "
                        f"cannot continue. {how}")

    def _sandbox_exists(self, run: AgentRun) -> bool:
        if not run.instance_id:
            return False
        try:
            inst = self.services.sandboxes.get(run.instance_id)
        except ApiError as exc:
            if exc.code == "not_found":
                return False
            raise
        return getattr(inst, "status", None) not in ("destroyed",)

    def message(self, run_id: str, message: str, max_turns: int | None = None) -> AgentRun:
        """Send a follow-up to a run whose turn ended. The agent continues in the same sandbox
        and branch; in the same Hermes session when the first turn reported one, otherwise in a
        new session told what was done before. Starts a stopped sandbox first. The turn limit
        stays the one the run started with: ``max_turns`` may only repeat it, any other value
        is refused (Hermes does not allow a resumed session to change it)."""
        text = message.strip() if isinstance(message, str) else ""
        if not text:
            raise ApiError(400, "invalid", "message is required: say what the agent should do next")
        if len(text) > MAX_GOAL_CHARS:
            raise ApiError(400, "invalid", f"message is longer than {MAX_GOAL_CHARS} characters")
        run = self._load(run_id)
        with self._run_lock(run.id):
            run = self._load(run_id)
            if run.status in ACTIVE_STATUSES:
                try:
                    reason = self._refresh(run)
                except Exception:  # noqa: BLE001
                    reason = None
                if reason:
                    self._emit([run.id], reason)
            if run.status in ACTIVE_STATUSES:
                raise ApiError(409, "conflict",
                               f"cloud agent run {run.id} is still working on its turn. Wait for it "
                               f"(cloud_agent_status with run_id and wait_s) or stop it first.")
            if not self._sandbox_exists(run):
                raise self._gone_error(run)
            if max_turns is not None and self._max_turns(max_turns) != (run.max_turns or 30):
                raise ApiError(400, "invalid", TURN_LIMIT_FIXED)
            turns = run.max_turns or 30            # the limit the run started with, never changed
            self._ensure_running(run)
            n = (run.turns or 1) + 1
            fresh = (f"This is a follow-up in the same sandbox and git branch ({run.branch}). "
                     f"The earlier task was:\n{run.goal}\n\n"
                     f"The earlier answer was:\n{run.reply or '(no answer was recorded)'}\n\n"
                     f"The branch has {run.commits_ahead} commit(s) from the earlier turns.\n\n"
                     f"New instruction:\n{text}")
            state = state_dir_of(run)
            self._put_file(run, f"{state}/goal.txt", text.encode("utf-8"))
            self._put_file(run, f"{state}/prompt_fresh.txt", fresh.encode("utf-8"))
            self._put_file(run, f"{state}/turn", f"{n}\n".encode("ascii"))
            self._put_file(run, f"{state}/followup", b"1\n")
            self._put_file(run, f"{state}/run.sh",
                           _runner_script(run.id, run.mode, turns, state).encode("utf-8"))
            res = self._cexec(run, _launch_script(state), timeout=60)
            if res.rc != 0:
                raise ApiError(502, "internal", "could not start the follow-up: "
                                                f"{self._tail_of(run, res)}")
            run.turns = n
            run.status = "running"
            run.exit_code = None
            run.error = None
            run.error_kind = None
            run.ended_at = None
            run.last_log = []
            self._refreshed.pop(run.id, None)
            self._save(run)
            self._emit([run.id], "running")
            return self._present(run)

    # ------------------------------------------------------------------ rerun

    #: Statuses a run can be started again from.
    RERUN_STATUSES = ("failed", "done", "stopped", "collected")

    def _rerun_plan(self, run_id: str, *, task: str | None = None, model_id: str | None = None,
                    model: str | None = None, base_url: str | None = None,
                    host_id: str | None = None, max_turns: int | None = None,
                    check_active: bool = True) -> dict:
        """Spawn arguments for a new run built from the record of ``run_id`` and the given
        overrides. Raises ``ApiError`` for a run that is still active (``check_active``)."""
        old = self._load(run_id)
        if check_active and old.status in ACTIVE_STATUSES:
            try:
                self._refresh(old)                 # it may have ended since the last look
            except Exception:  # noqa: BLE001
                pass
        if check_active and old.status not in self.RERUN_STATUSES:
            raise ApiError(409, "conflict",
                           f"Cloud agent run {old.id} is still {old.status}. Wait for it to end "
                           "(or press Stop), then start it again.")
        goal = (task.strip() if isinstance(task, str) and task.strip() else old.goal)
        endpoint = None
        if model or base_url:                     # applies on top of the chosen saved model
            endpoint = {"model": model.strip() if isinstance(model, str) else "",
                        "base_url": base_url.strip() if isinstance(base_url, str) else ""}
        # No override: the model is chosen the way the first run chose it (the saved choice it
        # used, else the template, the default saved choice or Settings as they are now, so a
        # fix made there since applies).
        wanted = model_id.strip() if isinstance(model_id, str) else ""
        if wanted.lower() == DEFAULT_MODEL_MARKER:
            chosen_id = None                      # the default model as it is now, not the old one
        else:
            chosen_id = wanted or old.model_id
        new_branch = True if old.new_branch is None else bool(old.new_branch)
        branch = old.branch
        if new_branch:
            repo = Path(old.repo_path)
            if gitrelay.is_repo(repo):
                n = 1
                while gitrelay.ref_exists(repo, f"refs/heads/{branch}"):
                    n += 1
                    branch = f"{old.branch}-{n}"       # never overwrite an existing branch
        return {
            "goal": goal, "host_id": host_id.strip() if isinstance(host_id, str) and host_id.strip()
            else old.host_id, "repo_path": old.repo_path, "branch": branch,
            "new_branch": new_branch, "base": old.base_sha if new_branch else "HEAD",
            "template": old.template_id,
            "max_turns": max_turns if max_turns is not None else (old.max_turns or 30),
            "model_id": chosen_id, "criteria": old.criteria, "endpoint": endpoint,
            "rerun_of": old.id, "session_id": old.session_id,
        }

    def rerun(self, run_id: str, *, task: str | None = None, model_id: str | None = None,
              model: str | None = None, base_url: str | None = None,
              host_id: str | None = None, max_turns: int | None = None,
              session_id: str | None = None) -> AgentRun:
        """Start a NEW run from the record of a finished one (failed, done, stopped or
        collected): same task, success criteria, repository, branch settings, base, template,
        turn limit and model choice, unless overridden. The old record stays; the new one has
        ``rerun_of``. A branch name that already exists in the repository gets a numeric suffix
        (``-2``, ``-3``), so no branch is ever overwritten. Goes through the same checks (and
        the same approval) as :meth:`spawn`."""
        plan = self._rerun_plan(run_id, task=task, model_id=model_id, model=model,
                                base_url=base_url, host_id=host_id, max_turns=max_turns)
        if session_id:
            plan["session_id"] = session_id
        goal = plan.pop("goal")
        host = plan.pop("host_id")
        return self.spawn(goal, host, **plan)

    # ------------------------------------------------------------------ remove

    def remove(self, run_id: str) -> dict:
        """Delete the record of a run that is not running and whose sandbox is gone. Never
        touches collected branches or worktrees on this computer."""
        run = self._load(run_id)
        with self._run_lock(run.id):
            run = self._load(run_id)
            if run.status in ACTIVE_STATUSES:
                raise ApiError(409, "conflict",
                               f"cloud agent run {run.id} is still running. Press Stop first.")
            if self._sandbox_exists(run):
                raise ApiError(409, "conflict",
                               f"The sandbox of cloud agent run {run.id} still exists. Press Stop "
                               "first: Stop saves the files in /work to this computer, then "
                               "deletes the sandbox.")
            self.services.store.delete_record(RECORD_KIND, run.id)
            self._refreshed.pop(run.id, None)
            self._emit([run.id], "removed")
            return {"removed": run.id,
                    "note": "Only the run record was removed. Collected branches and worktrees "
                            "on this computer were not changed."}

    # ------------------------------------------------------------------ collect

    def _ensure_running(self, run: AgentRun) -> None:
        """Start the run's sandbox when the idle reaper stopped it, and wait until it runs.

        Raises ``ApiError`` if it cannot be started or is not running within ``RESTART_WAIT_S``.
        """
        if not run.instance_id:
            return
        sandboxes = self.services.sandboxes
        try:
            status = getattr(sandboxes.get(run.instance_id), "status", None)
        except ApiError:
            return                         # gone: the collect exec reports it
        if status not in ("stopped", "exited"):
            return                         # running, or destroyed/creating/error: nothing to restart
        try:
            sandboxes.start(run.instance_id)
        except ApiError as exc:
            raise ApiError(409, "conflict", "the sandbox was stopped when idle and could not be "
                                            f"restarted: {exc.message}") from exc
        deadline = self._clock() + RESTART_WAIT_S
        while True:
            try:
                status = getattr(sandboxes.get(run.instance_id), "status", None)
            except ApiError as exc:
                raise ApiError(409, "conflict", "the sandbox disappeared while restarting: "
                                                f"{exc.message}") from exc
            if status == "running":
                return
            if status in ("destroyed", "error"):
                raise ApiError(409, "conflict", f"the sandbox could not be restarted (now {status})")
            if self._clock() >= deadline:
                raise ApiError(409, "conflict", "the sandbox did not come back within "
                                                f"{RESTART_WAIT_S:g}s of being restarted; try again")
            self._sleep(RESTART_POLL_S)

    def collect(self, run_id: str, into: str = "worktree") -> CollectResult:
        """Bring the run's branch back as ``refs/heads/cloud/<run>/<branch>``.

        Allowed when the run is done, failed (partial work) or already collected while
        its sandbox still exists. Never merges and never moves the user's branches.
        ``into="worktree"`` also checks the ref out at ``<repo parent>/<repo name>-<run id>``.
        """
        if into not in ("worktree", "branch"):
            raise ApiError(400, "invalid", "into must be 'worktree' or 'branch'")
        run = self._load(run_id)
        with self._run_lock(run.id):
            run = self._load(run_id)
            if run.status in ACTIVE_STATUSES:
                try:
                    reason = self._refresh(run)
                except ApiError:
                    reason = None
                if reason:
                    self._emit([run.id], reason)
            if run.status in ACTIVE_STATUSES:
                raise ApiError(409, "conflict",
                               f"agent run {run.id} is still running; wait for it or stop it first")
            if run.status == "stopped" and not run.instance_id:
                raise ApiError(409, "conflict", f"agent run {run.id} was stopped and its sandbox removed")
            repo = Path(run.repo_path)
            if not gitrelay.is_repo(repo):
                raise ApiError(409, "conflict", f"repository {run.repo_path} is no longer available")
            prefix = f"refs/heads/cloud/{run.id}/"
            ref = prefix + run.branch

            import_problem = ""
            start_error: ApiError | None = None
            try:
                self._ensure_running(run)
            except ApiError as exc:
                start_error = exc
            if start_error is None and not run.instance_id:
                start_error = ApiError(409, "conflict", "the sandbox was removed")
            if start_error is None:
                res = self._cexec(run, _COLLECT, [run.base_sha, run.branch], timeout=900)
                failed = res.rc != 0
                detail = self._tail_of(run, res)
            else:
                res = None
                failed = True
                detail = self._hide(run, start_error.message, "Error")
            if failed:
                if not (run.collected_ref and gitrelay.ref_exists(repo, run.collected_ref)):
                    raise ApiError(409, "conflict",
                                   f"could not read the result from the sandbox: {detail}")
                ref = run.collected_ref
            else:
                info, body = _parse_kv(res.stdout)
                try:
                    sandbox_ahead = int(info.get("ahead", "0"))
                except ValueError:
                    sandbox_ahead = 0
                sandbox_head = (info.get("head") or "").strip()
                # Collecting again with nothing new in the sandbox fetches nothing.
                unchanged = bool(sandbox_head and gitrelay.ref_exists(repo, ref)
                                 and gitrelay.rev_parse(repo, ref) == sandbox_head)
                if sandbox_ahead > 0 and not unchanged:
                    try:
                        data = base64.b64decode("".join(body).strip())
                    except (binascii.Error, ValueError) as exc:
                        raise ApiError(502, "internal", "the result bundle was damaged in transit") from exc
                    try:
                        imported = gitrelay.import_bundle_bytes_detailed(repo, data, prefix)
                    except gitrelay.GitError as exc:
                        raise ApiError(409, "conflict",
                                       f"could not import the result: {_err_text(exc)}") from exc
                    mine = next((i for i in imported if i.ref == ref), None)
                    if mine is None and not gitrelay.ref_exists(repo, ref):
                        raise ApiError(502, "internal", f"the result bundle did not contain {run.branch}")
                    if mine is not None and not mine.ok:
                        import_problem = mine.message
                elif not gitrelay.ref_exists(repo, ref):
                    gitrelay.git(repo, "update-ref", ref, run.base_sha)
                run.commits_ahead = sandbox_ahead
            if run.error == KEPT_SANDBOX_MESSAGE:
                run.error = None                    # the files were collected after all
            run.head_sha = gitrelay.rev_parse(repo, ref)

            user_ref = f"refs/heads/{run.branch}"
            target = user_ref if gitrelay.ref_exists(repo, user_ref) else run.base_sha
            ahead, behind = gitrelay.ahead_behind(repo, ref, target)
            diverged = ahead > 0 and behind > 0
            conflicts = gitrelay.merge_conflicts_dry_run(repo, target, ref) if diverged else []
            if import_problem:
                diverged = True

            worktree_path = run.worktree_path
            if into == "worktree":
                path = Path(worktree_path) if worktree_path else repo.parent / f"{repo.name}-{run.id}"
                if not (path / ".git").exists():
                    try:
                        gitrelay.create_worktree(repo, ref, path)
                    except gitrelay.GitError as exc:
                        raise ApiError(409, "conflict",
                                       f"could not create the worktree at {path}: {_err_text(exc)}") from exc
                worktree_path = str(path)

            run.collected_ref = ref
            run.collected_head_sha = run.head_sha
            run.worktree_path = worktree_path
            run.status = "collected"
            run.ended_at = run.ended_at or utc_now_iso()
            self._save(run)
            self._emit([run.id], "collected")
            return CollectResult(run_id=run.id, ref=ref, commits_ahead=ahead,
                                 worktree_path=worktree_path, diverged=diverged,
                                 conflicts_dry_run=list(conflicts), message=import_problem)

    # ------------------------------------------------------------------ stop

    def _fetch_for_stop(self, run: AgentRun) -> str | None:
        """Fetch the commits the sandbox holds that this computer does not have yet (the same
        thing Get results does, without a worktree). Returns the sentence for the Stop result,
        or ``None`` when there was nothing new. Raises :class:`_NotFetched` when the sandbox
        could not be read or the commits could not be imported."""
        repo = Path(run.repo_path)
        prefix = f"refs/heads/cloud/{run.id}/"
        ref = prefix + run.branch
        try:
            self._ensure_running(run)
            res = self._cexec(run, _COLLECT, [run.base_sha, run.branch], timeout=900)
        except ApiError as exc:
            if exc.code == "not_found":
                return None                # the sandbox is already gone: nothing to read
            raise _NotFetched(self._hide(run, exc.message, "Error")) from None
        except Exception as exc:  # noqa: BLE001 - host offline, timeout, ...
            raise _NotFetched(self._hide(run, _err_text(exc), "Error")) from None
        if res.rc != 0:
            text = (res.stderr or res.stdout or "").strip()
            low = text.lower()
            if _gone(res.stderr) or "not found in the sandbox" in low \
                    or "no such file or directory" in low:
                return None                # no container, no repository or no branch: no commits
            raise _NotFetched(self._hide(run, text, "Error", tail=300) or f"exit code {res.rc}")
        info, body = _parse_kv(res.stdout)
        try:
            ahead = int(info.get("ahead", "0"))
        except ValueError:
            ahead = 0
        head = (info.get("head") or "").strip()
        if ahead <= 0:
            return None
        if not gitrelay.is_repo(repo):
            raise _NotFetched(f"the repository {run.repo_path} is no longer available")
        if head and gitrelay.ref_exists(repo, ref) and gitrelay.rev_parse(repo, ref) == head:
            run.collected_ref, run.head_sha = ref, head
            run.collected_head_sha, run.commits_ahead = head, ahead
            return None                    # everything was fetched before
        try:
            data = base64.b64decode("".join(body).strip())
        except (binascii.Error, ValueError):
            raise _NotFetched("the result bundle was damaged in transit") from None
        before = gitrelay.rev_parse(repo, ref) if gitrelay.ref_exists(repo, ref) else run.base_sha
        try:
            imported = gitrelay.import_bundle_bytes_detailed(repo, data, prefix)
        except gitrelay.GitError as exc:
            raise _NotFetched(f"could not import the result: {_err_text(exc)}") from None
        mine = next((i for i in imported if i.ref == ref), None)
        if mine is None and not gitrelay.ref_exists(repo, ref):
            raise _NotFetched(f"the result bundle did not contain {run.branch}")
        kept_as = None
        if mine is not None and not mine.ok:
            kept_as = mine.incoming_ref
            if not kept_as:
                raise _NotFetched(mine.message or "the branch could not be updated")
        where = kept_as or ref
        try:
            new = gitrelay.ahead_behind(repo, where, before)[0]
        except Exception:  # noqa: BLE001
            new = ahead
        new = max(1, new)
        run.collected_ref = ref if gitrelay.ref_exists(repo, ref) else where
        run.head_sha = head or gitrelay.rev_parse(repo, where)
        run.collected_head_sha, run.commits_ahead = run.head_sha, ahead
        commits = f"{new} new {'commit' if new == 1 else 'commits'}"
        if kept_as:
            return (f"Fetched {commits} before stopping. Your branch has moved on, so they are "
                    f"kept as {kept_as}.")
        short = ref[len("refs/heads/"):]
        return f"Fetched {commits} to branch {short} before stopping."

    @staticmethod
    def _not_fetched_text(detail: str) -> str:
        why = f" ({detail.strip().rstrip('.')})" if detail and detail.strip() else ""
        return (f"{NOT_FETCHED_PREFIX}{why}, so the sandbox was kept and nothing was deleted. "
                "Press Get results to try again. Deleting it anyway saves the files in /work to "
                "this computer first, and the commits are then only inside that saved copy, not "
                "on a branch.")

    def unfetched_commits(self, instance_id: str) -> bool:
        """Does a run on this sandbox hold commits that were never fetched to this computer?
        (The idle reaper leaves such a sandbox alone.)"""
        try:
            runs = self._all()
        except Exception:  # noqa: BLE001
            return False
        for run in runs:
            if run.instance_id != instance_id:
                continue
            if (run.error or "").startswith(NOT_FETCHED_PREFIX):
                return True                   # Stop could not read them: they are only there
            if not (run.commits_ahead or 0) > 0:
                continue
            if run.status == "collected" or (run.head_sha and run.head_sha == run.collected_head_sha):
                continue
            return True
        return False

    def stop(self, run_id: str, keep_branch: bool = True, delete_anyway: bool = False) -> AgentRun:
        """Stop the runner and destroy the sandbox. ``keep_branch=False`` also deletes the
        collected refs under ``refs/heads/cloud/<run>/`` (refs checked out in a worktree stay).

        Commits the sandbox holds that were not fetched yet are fetched first (like Get
        results). When that fails nothing is deleted: the run is marked stopped, keeps its
        ``instance_id``, carries the reason and ``ApiError(409, 'not_fetched')`` is raised,
        unless ``delete_anyway`` is true; the commits are then only inside the saved copy and
        ``stop_note`` says where. With ``keep_branch=False`` nothing is fetched (the branches
        are deleted anyway; the saved copy of the files still has the commits).

        Destroying a sandbox saves the files in its workdir first. When that fails (``not_saved``) nothing is
        destroyed: the run is marked stopped, keeps its ``instance_id`` and carries
        :data:`KEPT_SANDBOX_MESSAGE`; the sandbox is deleted from Overview, or by stopping again.
        """
        run = self._load(run_id)
        with self._run_lock(run.id):
            run = self._load(run_id)
            kept = False
            fetched_note: str | None = None
            unfetched: str | None = None          # why the commits were not fetched (deleted anyway)
            report: dict = {}
            if run.instance_id:
                if run.status in ACTIVE_STATUSES:
                    try:
                        self._cexec(run, _kill_script(state_dir_of(run)), timeout=30)
                    except Exception:  # noqa: BLE001 - destroy below ends it anyway
                        pass
                if keep_branch:
                    try:
                        fetched_note = self._fetch_for_stop(run)
                    except _NotFetched as exc:
                        if not delete_anyway:
                            if run.status != "collected":
                                run.status = "stopped"
                            run.error = self._not_fetched_text(exc.detail)
                            run.ended_at = run.ended_at or utc_now_iso()
                            self._save(run)
                            self._emit([run.id], "stopped")
                            raise ApiError(409, NOT_FETCHED, run.error) from None
                        unfetched = exc.detail or "unknown error"
                try:
                    self.services.sandboxes.destroy(run.instance_id, report=report)
                except ApiError as exc:
                    if exc.code == "not_saved":
                        kept = True
                    elif exc.code != "not_found":
                        raise
            if not keep_branch:
                self._delete_refs(run)
            if run.status != "collected":
                run.status = "stopped"
            notes: list[str] = []
            saved = report.get("saved") if isinstance(report, dict) else None
            where = saved.get("path") if isinstance(saved, dict) else None
            if kept:
                run.error = KEPT_SANDBOX_MESSAGE    # instance_id stays: the sandbox still exists
            else:
                run.instance_id = ""            # the sandbox is gone; a collected run keeps its
                #                                 status, collected_ref and worktree_path
                if run.error == KEPT_SANDBOX_MESSAGE or (run.error or "").startswith(NOT_FETCHED_PREFIX):
                    run.error = None
                if unfetched is not None:
                    notes.append(
                        "The agent's newest commits could not be fetched ("
                        f"{unfetched.strip().rstrip('.')}) and the sandbox was deleted as asked. "
                        + (f"They are only in the saved copy of its files: {where}."
                           if where else "No saved copy was made, so they are gone."))
            if fetched_note:
                notes.insert(0, fetched_note)
            run.stop_note = " ".join(notes) or None
            run.ended_at = run.ended_at or utc_now_iso()
            self._save(run)
            self._emit([run.id], "stopped")
            return self._present(run)

    @staticmethod
    def _delete_refs(run: AgentRun) -> list[str]:
        repo = Path(run.repo_path)
        if not gitrelay.is_repo(repo):
            return []
        prefix = f"refs/heads/cloud/{run.id}/"
        out = gitrelay.git(repo, "for-each-ref", "--format=%(refname)", prefix)
        refs = [r for r in out.decode("utf-8", "replace").splitlines() if r.startswith(prefix)]
        busy: set[str] = set()
        try:
            wt = gitrelay.git(repo, "worktree", "list", "--porcelain").decode("utf-8", "replace")
            busy = {line[len("branch "):].strip() for line in wt.splitlines()
                    if line.startswith("branch ")}
        except gitrelay.GitError:
            pass
        deleted = []
        for ref in refs:
            if ref in busy:
                continue
            gitrelay.git(repo, "update-ref", "-d", ref)
            deleted.append(ref)
        # tips a Collect kept because the branch could not be fast-forwarded
        kept = f"refs/cloud-compute/incoming/heads/cloud/{run.id}/"
        out = gitrelay.git(repo, "for-each-ref", "--format=%(refname)", kept)
        for ref in out.decode("utf-8", "replace").splitlines():
            if ref.startswith(kept):
                gitrelay.git(repo, "update-ref", "-d", ref)
        if run.collected_ref in deleted:
            run.collected_ref = None
        return deleted

    # ------------------------------------------------------------------ approval text

    def describe_rerun(self, args: dict) -> str:
        """Approval message for ``cloud_agent_rerun``: the text of a new run with the same
        task, host, repository, branch and model the rerun would use (store lookups only)."""
        args = args if isinstance(args, dict) else {}
        run_id = str(args.get("run_id") or "")

        def text(name: str) -> str | None:
            value = args.get(name)
            return value if isinstance(value, str) and value.strip() else None

        turns = args.get("max_turns")
        try:
            plan = self._rerun_plan(
                run_id, task=text("task"), model_id=text("model_id"), model=text("model"),
                base_url=text("base_url"), host_id=text("host_id"),
                max_turns=turns if isinstance(turns, int) and not isinstance(turns, bool) else None,
                check_active=False)
        except Exception:  # noqa: BLE001 - the rerun itself reports the problem
            return (f"Start cloud agent run {run_id or '?'} again (same task, repository and "
                    "model unless you change them). Sends the model key into the new sandbox. "
                    "No other secrets.")
        spawn_args = {"task": plan["goal"], "host_id": plan["host_id"],
                      "repo_path": plan["repo_path"], "branch": plan["branch"],
                      "new_branch": plan["new_branch"], "template": plan["template"],
                      "max_turns": plan["max_turns"], "model_id": plan["model_id"],
                      "criteria": plan.get("criteria")}
        return self.describe_spawn(spawn_args, endpoint=plan["endpoint"])

    def describe_spawn(self, args: dict, endpoint: dict | None = None) -> str:
        """Approval message for ``cloud_agent_spawn``. No network or SSH."""
        args = args if isinstance(args, dict) else {}
        full_task = str(args.get("task") or args.get("goal") or "")
        goal = _short(full_task)
        host_id = str(args.get("host_id") or "?")
        template_id = str(args.get("template") or "hermes-agent")
        try:
            max_turns = int(args.get("max_turns") or 30)
        except (TypeError, ValueError):
            max_turns = 30
        repo_path = str(args.get("repo_path") or "") or "?"
        criteria = str(args.get("criteria") or "").strip()
        new_branch = args.get("new_branch", True)
        branch = args.get("branch")
        if not branch:
            branch = _slug(goal) if new_branch is not False else "(current branch)"
        tpl = _TEMPLATE_IDS.get(template_id)
        try:
            tpl = self.services.templates.get(template_id)
        except Exception:  # noqa: BLE001 - fall back to the built-in definition
            pass
        agent = (tpl.agent or {}) if tpl is not None else {}
        mode = agent.get("mode") if agent.get("mode") in AGENT_MODES else "unknown"
        # What runs: the whole task and success criteria (bounded), the full repository path.
        from .approval import bounded_text
        task_text = f" Task: '{bounded_text(full_task)}'." if full_task.strip() else ""
        if criteria:
            task_text += f" Success criteria: '{bounded_text(criteria)}'."
        if mode == "script":
            return (f"Start a cloud agent handoff test on {host_id} (template {template_id}, "
                    f"no model): commits a marker file on {repo_path} branch {branch}."
                    f"{task_text} Sends no secrets.")
        cfg = {"base_url": agent.get("base_url"), "model": agent.get("model"),
               "key_secret": agent.get("key_secret") or DEFAULT_KEY_SECRET}
        model_id = args.get("model_id") if isinstance(args.get("model_id"), str) else None
        try:
            chosen = self._resolve_model(cfg, model_id.strip() if model_id else None, endpoint)
            base_url, model, key_secret = chosen["base_url"], chosen["model"], chosen["key_secret"]
        except Exception:  # noqa: BLE001 - the spawn itself reports the problem
            base_url, model, key_secret = None, None, cfg["key_secret"]
        source = f" (read from the secret {key_secret})"
        endpoint = _endpoint_host(base_url)
        if model and endpoint:
            where = f"model {model} at {endpoint}"
        elif model:
            where = f"model {model}, no endpoint set"
        elif endpoint:
            where = f"no model set, endpoint {endpoint}"
        else:
            where = "no model endpoint set"
        return (f"Start a cloud agent on {host_id} (template {template_id}, {mode} mode, {where}, "
                f"max {max_turns} turns) on {repo_path} branch {branch}.{task_text} "
                f"Sends the model key {KEY_ENV}{source} into the sandbox. No other secrets.")

    def describe_message(self, args: dict) -> str:
        """Approval message for ``cloud_agent_message`` (store lookup only)."""
        args = args if isinstance(args, dict) else {}
        run_id = str(args.get("run_id") or "?")
        from .approval import bounded_text
        text = bounded_text(str(args.get("message") or "").strip())
        turns = args.get("max_turns")
        turns_text = (f" (up to {turns} turns)"
                      if isinstance(turns, int) and not isinstance(turns, bool) else "")
        host = "?"
        try:
            data = self.services.store.get_record(RECORD_KIND, run_id)
            if data:
                host = str(data.get("host_id") or "?")
        except Exception:  # noqa: BLE001
            pass
        return (f'Sends a follow-up to cloud agent run {run_id} on {host}{turns_text}: "{text}". '
                "The agent continues in the same sandbox and branch.")


def provide(services: Any) -> AgentManager:
    """Build the manager and make the built-in agent templates exist right away, so they are
    listed (and editable) before the first spawn."""
    mgr = AgentManager(services)
    mgr._seed_templates()
    return mgr
