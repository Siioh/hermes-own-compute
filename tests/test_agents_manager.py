"""AgentManager: spawn, poll, collect, stop and the security boundary of cloud agents.

The remote side is emulated: a fake sandbox manager hands out instances whose
``<runtime> exec`` calls run the real sandbox scripts with a local ``sh`` and real
``git`` inside a temporary directory standing in for the container filesystem.
That proves the whole bundle -> commit -> bundle back -> ref -> worktree path on
temp repositories without any network.
"""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_compute_core import agents as agents_mod
from cloud_compute_core import gitrelay
from cloud_compute_core.agents import (
    AGENT_IMAGE_LITE, AGENT_TEMPLATES, KEY_ENV, LITE_BASE, AgentManager, AgentRun, CollectResult,
)
from cloud_compute_core.interfaces import ApiError
from cloud_compute_core.redact import redact_tokens
from cloud_compute_core.models import HostLimits, TemplateSpec, VariableSpec, new_id
from cloud_compute_core.sandbox import SandboxManager
from cloud_compute_core.testing import (
    exec_result, sample_host, sample_instance, sample_local_host, sample_slurm_host,
)

SECRET_VALUE = "fake-model-key-0123456789-do-not-leak"
KEY_NAME = "LLAMA_SWAP_API_KEY"


# --------------------------------------------------------------------------- local shell


def _find_sh() -> str | None:
    sh = shutil.which("sh")
    if sh:
        return sh
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parents[1]
        for cand in (root / "usr" / "bin" / "sh.exe", root / "bin" / "sh.exe",
                     root.parent / "usr" / "bin" / "sh.exe", root.parent / "bin" / "sh.exe"):
            if cand.exists():
                return str(cand)
    return None


SH = _find_sh()
needs_shell = pytest.mark.skipif(SH is None or shutil.which("git") is None,
                                 reason="needs a POSIX sh and git")


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return out.stdout.decode("utf-8", "replace").strip()


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "config", "core.autocrlf", "false")
    _git(path, "checkout", "-q", "-b", "main")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "initial")
    return path


def _user_heads(repo: Path) -> dict[str, str]:
    out = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/")
    pairs = (line.split(" ", 1) for line in out.splitlines() if line)
    return {ref: sha for ref, sha in pairs if not ref.startswith("refs/heads/cloud/")}


# --------------------------------------------------------------------------- fakes


class FakeTemplateStore:
    def __init__(self):
        self.items: dict = {}
        self.saved: list[str] = []

    def get(self, template_id):
        if template_id not in self.items:
            raise ApiError(404, "not_found", f"template {template_id} not found")
        return copy.deepcopy(self.items[template_id])

    def save(self, spec):
        self.items[spec.id] = copy.deepcopy(spec)
        self.saved.append(spec.id)
        return spec

    def list(self):
        return list(self.items.values())


class FakeSandboxManager:
    """Instances map to ``<root>/cc-<id>/work``; the env each container would get is
    resolved from the template variables exactly as the real spawn would."""

    def __init__(self, root: Path, services):
        self.root = root
        self.services = services
        self.instances: dict = {}
        self.envs: dict[str, dict[str, str]] = {}
        self.destroyed: list[str] = []
        self.spawn_error: Exception | None = None
        self.touched: list[str] = []
        self.start_calls: list[str] = []
        self.start_error: Exception | None = None
        self.destroy_error: Exception | None = None     # e.g. ApiError(409, "not_saved", ...)
        self.start_lag = 0              # `get` calls that still see "stopped" after a start
        self.limits: dict[str, tuple[int, int]] = {}      # instance id -> (ttl, idle) minutes
        self.activity: dict[str, datetime] = {}           # instance id -> last touch
        self.clock = lambda: datetime.now(timezone.utc)

    # The real limit rule (request > template > Settings), so the tests below exercise it.
    _effective_limits = SandboxManager._effective_limits
    _config_minutes = SandboxManager._config_minutes
    _minutes = staticmethod(SandboxManager._minutes)

    def spawn(self, template_id, host_id, *, name=None, ttl_minutes=None, idle_minutes=None,
              session_id=None):
        if self.spawn_error is not None:
            raise self.spawn_error
        tpl = self.services.templates.get(template_id)
        env, _previews = self.services.secrets.resolve(tpl.variables, host_id)
        inst = sample_instance(id=new_id("i"), name=name or template_id, host_id=host_id,
                               template_id=template_id, runtime="podman", session_id=session_id)
        self.instances[inst.id] = inst
        self.envs[inst.id] = dict(env)
        self.limits[inst.id] = self._effective_limits(tpl, ttl_minutes, idle_minutes)
        self.activity[inst.id] = self.clock()
        (self.root / f"cc-{inst.id}" / "work").mkdir(parents=True)
        return inst

    def get(self, instance_id):
        if instance_id not in self.instances:
            raise ApiError(404, "not_found", f"instance {instance_id} not found")
        inst = self.instances[instance_id]
        if inst.status == "starting-up":
            if self.start_lag <= 0:
                inst.status = "running"
            self.start_lag -= 1
        return inst

    def start(self, instance_id):
        self.start_calls.append(instance_id)
        if self.start_error is not None:
            raise self.start_error
        inst = self.get(instance_id)
        if inst.status == "stopped":
            inst.status = "starting-up" if self.start_lag > 0 else "running"
        return inst

    def destroy(self, instance_id, report=None, **kwargs):
        inst = self.get(instance_id)
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed.append(instance_id)
        inst.status = "destroyed"
        if report is not None:        # what the real manager reports about the saved copy
            report.update(workdir="/work", saved={"export_id": "x-fake0001", "files": 3,
                                                  "path": str(self.root / "saved" / instance_id)})
        return inst

    def output_mask(self, instance_id, *, assume=()):
        """Like the real manager: the values this fake sent, else 404 for a gone sandbox."""
        inst = self.instances.get(instance_id)
        if inst is None or inst.status == "destroyed":
            raise ApiError(404, "not_found", f"instance {instance_id} no longer exists")
        values = sorted({v for v in self.envs.get(instance_id, {}).values() if v}, key=len,
                        reverse=True)

        def mask(text):
            for v in values:
                text = text.replace(v, "[redacted]")
            return redact_tokens(text)
        return mask

    def touch(self, instance_id):
        self.touched.append(instance_id)
        self.activity[instance_id] = self.clock()

    def work_dir(self, instance_id) -> Path:
        return self.root / f"cc-{instance_id}" / "work"

    def home_dir(self, instance_id) -> Path:
        """Stands in for /home/agent (the container's own filesystem, never saved)."""
        return self.root / f"cc-{instance_id}" / "home"

    def state_dir(self, instance_id) -> Path:
        return self.home_dir(instance_id) / ".cc-agent"

    def hermes_home(self, instance_id) -> Path:
        return self.state_dir(instance_id) / "hermes"


#: What a script run on this computer by the emulator gets for ``hermes``: never the real one.
_HERMES_STUB = (
    "#!/bin/sh\n"
    "echo 'test isolation: a unit test script tried to start Hermes; use a fake hermes in "
    "the emulator extra_path' >&2\n"
    "exit 97\n")
#: ... and for ``python`` / ``python3``: the test interpreter in isolated mode without
#: site-packages (``-I -S``), so it can never import Hermes, and never with a Hermes argument.
_PYTHON_STUB = (
    "#!/bin/sh\n"
    'case "$*" in *hermes*) echo "test isolation: refused python with a Hermes argument" >&2; '
    "exit 97 ;; esac\n"
    'exec "{python}" -I -S "$@"\n')
#: Variables that could point a child Python at another environment.
_PY_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV")


def host_stub_dir(root: Path) -> Path:
    """A folder of harmless ``hermes``, ``python`` and ``python3`` stand-ins (see above)."""
    stubs = root / "host-stubs"
    stubs.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable).as_posix()
    for name, text in (("hermes", _HERMES_STUB), ("python", _PYTHON_STUB.format(python=python)),
                       ("python3", _PYTHON_STUB.format(python=python))):
        f = stubs / name
        f.write_bytes(text.encode("utf-8"))
        f.chmod(0o755)
    return stubs


class ContainerEmulator:
    """Handles ``podman exec -i -w <dir> cc-<id> sh -c <script> sh <args...>`` locally.

    The script runs on this computer with a real shell, so its PATH starts with the test's
    ``extra_path`` (fakes such as a stand-in ``hermes``), then :func:`host_stub_dir`: a
    bootstrap that calls ``hermes`` or ``python3`` never reaches a Hermes installed here.
    """

    def __init__(self, sandboxes: FakeSandboxManager, extra_path: Path | None = None):
        self.sandboxes = sandboxes
        self.extra_path = extra_path
        self.fail_matching: str | None = None
        self.stub_dir = host_stub_dir(sandboxes.root.parent)

    def matches(self, argv, stdin) -> bool:
        return len(argv) > 6 and argv[1] == "exec" and argv[5].startswith("cc-")

    def __call__(self, argv, stdin):
        instance_id = argv[5][len("cc-"):]
        inst = self.sandboxes.instances.get(instance_id)
        if inst is None or instance_id in self.sandboxes.destroyed:
            return exec_result(rc=125, stderr=f"Error: no container with name or id {argv[5]} found")
        if inst.status != "running":      # what podman/docker say for a stopped container
            return exec_result(rc=125, stderr="Error: can only create exec sessions on running "
                                              "containers: container state improper")
        if self.fail_matching and self.fail_matching in " ".join(argv):
            return exec_result(rc=1, stderr="simulated failure")
        assert argv[:5] == ["podman", "exec", "-i", "-w", argv[4]]
        assert argv[6:8] == ["sh", "-c"]
        work = self.sandboxes.work_dir(instance_id).as_posix()

        home = self.sandboxes.home_dir(instance_id).as_posix()
        home.replace("\\", "/")

        def rewrite(s: str) -> str:
            return s.replace("/work", work).replace("/home/agent", home)

        cmd = [SH] + [rewrite(a) for a in argv[7:]]
        data = stdin
        if stdin is not None and argv[-1].endswith((".sh", ".yaml")):
            data = rewrite(stdin.decode("utf-8")).encode("utf-8")
        env = {k: v for k, v in os.environ.items() if k.upper() not in _PY_ENV}
        env.update(self.sandboxes.envs.get(instance_id, {}))
        front = [str(self.stub_dir)]
        if self.extra_path is not None:
            front.insert(0, str(self.extra_path))
        env["PATH"] = os.pathsep.join(front + [env.get("PATH", "")])
        proc = subprocess.run(cmd, input=data, capture_output=True, cwd=rewrite(argv[4]),
                              env=env, timeout=120)
        out = proc.stdout.decode("utf-8", "replace").replace(home, "/home/agent")
        return exec_result(rc=proc.returncode, stdout=out,
                           stderr=proc.stderr.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def env(tmp_path, services, fake_transports, fake_events, fake_secrets):
    templates = FakeTemplateStore()
    services.set("templates", templates)
    sandboxes = FakeSandboxManager(tmp_path / "containers", services)
    services.set("sandboxes", sandboxes)
    services.store.upsert_host(sample_host(runtime_pref="podman",
                                           limits=HostLimits(allow_agents=True)))
    emulator = ContainerEmulator(sandboxes)
    transport = fake_transports.get("x1carbon")
    transport.script(emulator.matches, emulator)
    transport.script(("podman", "image", "exists"), exec_result(rc=0))
    mgr = AgentManager(services)
    repo = _make_repo(tmp_path / "user" / "proj")

    return SimpleNamespace(services=services, mgr=mgr, repo=repo, tmp=tmp_path,
                           templates=templates, sandboxes=sandboxes, emulator=emulator,
                           transport=transport, events=fake_events, secrets=fake_secrets)


def _wait_done(mgr: AgentManager, run_id: str, timeout: float = 60.0) -> AgentRun:
    deadline = time.monotonic() + timeout
    while True:
        mgr.poll()
        run = mgr._load(run_id)
        if run.status not in ("starting", "running"):
            return run
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run_id} still {run.status}: {run.last_log}")
        time.sleep(0.2)


def _configure_hermes(e, *, key_secret: str = KEY_NAME):
    tpl = copy.deepcopy(next(t for t in AGENT_TEMPLATES if t.id == "hermes-agent"))
    tpl.variables = [VariableSpec(name=KEY_ENV, source=f"secret:{key_secret}", scope="runtime")]
    tpl.agent = dict(tpl.agent, base_url="http://192.0.2.10:8080/v1",
                     model="hyperqwen-swift", key_secret=key_secret)
    e.templates.save(tpl)
    return tpl


# --------------------------------------------------------------------------- host isolation


@needs_shell
def test_emulated_scripts_never_reach_a_hermes_installed_on_this_computer(env, tmp_path,
                                                                           monkeypatch):
    """On macOS the hermes-agent bootstrap ran on the host and started Hermes."""
    e = env
    marker = tmp_path / "real-hermes-ran"
    real_bin = tmp_path / "host-bin"                  # stands in for a Hermes install on PATH
    real_bin.mkdir()
    fake_real = real_bin / "hermes"
    fake_real.write_bytes(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n".encode("utf-8"))
    fake_real.chmod(0o755)
    pkg = tmp_path / "site" / "hermes_cli"                # and an importable hermes_cli
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        f"open({marker.as_posix()!r}, 'w').write('imported')\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(real_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "site"))
    inst = sample_instance(id="i-stub0001", status="running", host_id="x1carbon")
    e.sandboxes.instances[inst.id] = inst
    e.sandboxes.work_dir(inst.id).mkdir(parents=True)
    # Spelled in pieces: the suite's own guard refuses a command line naming Hermes outright.
    script = ('h=her; h="${h}mes"; "$h" chat -Q --query-file x; echo hermes_rc=$?; '
              "python3 -m \"her\"\"mes_cli\" 2>/dev/null; echo py_rc=$?; "
              "python3 -c 'import importlib, sys; importlib.import_module(sys.argv[1] + \"mes_cli\")' "
              "her 2>/dev/null; echo import_rc=$?; "
              "python3 -c 'import base64; print(base64.b64encode(b\"ok\").decode())'")
    res = e.emulator(["podman", "exec", "-i", "-w", "/work", f"cc-{inst.id}", "sh", "-c", script],
                     None)
    assert not marker.exists(), marker.read_text()
    assert "hermes_rc=97" in res.stdout and "py_rc=97" in res.stdout
    assert "import_rc=1" in res.stdout                # not importable even by another name
    assert "b2s=" in res.stdout                       # ordinary python3 use still works
    assert "test isolation" in res.stderr


# --------------------------------------------------------------------------- models / templates


def test_templates_and_records():
    ids = {t.id: t for t in AGENT_TEMPLATES}
    assert set(ids) == {"hermes-agent", "agent-script"}
    hermes, script = ids["hermes-agent"], ids["agent-script"]
    assert hermes.agent["mode"] == "hermes" and script.agent["mode"] == "script"
    assert [(v.name, v.source, v.scope) for v in hermes.variables] == [
        (KEY_ENV, "secret:CC_AGENT_API_KEY", "runtime")]
    assert hermes.agent["base_url"] is None and hermes.agent["key_secret"] == "CC_AGENT_API_KEY"
    assert script.variables == []
    assert script.image == AGENT_IMAGE_LITE and script.agent["image_base"] == LITE_BASE
    for t in (hermes, script):
        assert t.command == ["sleep", "infinity"] and t.workdir == "/work"
        assert t.host_kinds == ["ssh", "local"] and t.network == "default"

    run = AgentRun(id="a-0123abcd", instance_id="i-1", host_id="h", goal="g", repo_path="/r",
                   branch="b", base_sha="0" * 40, head_sha=None, commits_ahead=0,
                   status="running", started_at="2026-11-08T12:00:00Z", ended_at=None,
                   session_id=None, last_log=["x"])
    data = run.to_dict()
    assert list(data)[:14] == ["id", "instance_id", "host_id", "goal", "repo_path", "branch",
                               "base_sha", "head_sha", "commits_ahead", "status", "started_at",
                               "ended_at", "session_id", "last_log"]
    assert AgentRun.from_dict(data) == run
    assert (run.reply, run.turns, run.model, run.endpoint_host) == (None, 1, None, None)
    more = AgentRun.from_dict({**data, "reply": "done", "turns": 3, "model": "m", "endpoint_host": "llm.example.net"})
    assert (more.reply, more.turns, more.model, more.endpoint_host) == ("done", 3, "m", "llm.example.net")
    assert AgentRun.from_dict(more.to_dict()) == more
    old = {k: v for k, v in data.items() if k not in ("reply", "turns", "model", "endpoint_host")}
    assert AgentRun.from_dict(old) == run                  # old stored rows still load
    cr = CollectResult(run_id="a-0123abcd", ref="refs/heads/cloud/a-0123abcd/b", commits_ahead=1,
                       worktree_path=None, diverged=False, conflicts_dry_run=[])
    assert CollectResult.from_dict(cr.to_dict()) == cr


def test_provide_and_registry(services):
    assert isinstance(agents_mod.provide(services), AgentManager)
    assert isinstance(services.agents, AgentManager)


def test_containerfile_pins_base_and_holds_no_secrets():
    text = agents_mod.CONTAINERFILE.read_text(encoding="utf-8")
    assert "ARG BASE=docker.io/nousresearch/hermes-agent:v0.23.0@sha256:" in text
    assert "USER agent" in text and "ENTRYPOINT []" in text and "/work" in text
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for word in ("API_KEY", "TOKEN", "PASSWORD", "auth.json", "credential", "SECRET"):
        assert word not in body


# --------------------------------------------------------------------------- end to end (script mode)


@needs_shell
def test_script_mode_bundle_roundtrip_to_worktree(env):
    e = env
    before = _user_heads(e.repo)
    run = e.mgr.spawn("Write a short result note", "x1carbon", repo_path=str(e.repo),
                      template="agent-script", session_id="sess-1")
    assert run.status == "running", run.error
    assert run.branch == "write-a-short-result-note"      # no cloud/ prefix: the ref gets one
    assert run.base_sha == _git(e.repo, "rev-parse", "main")
    assert run.session_id == "sess-1" and run.mode == "script"
    assert e.templates.saved == ["agent-script"]
    assert e.sandboxes.envs[run.instance_id] == {}          # script mode: no secret at all

    # The cloned repo in the sandbox has no remotes and a local identity only.
    crepo = e.sandboxes.work_dir(run.instance_id) / "repo"
    assert _git(crepo, "remote") == ""
    cfg = (crepo / ".git" / "config").read_text(encoding="utf-8")
    assert "url =" not in cfg and "[remote" not in cfg and "credential" not in cfg
    assert _git(crepo, "config", "user.email") == "agent@cloud-compute.invalid"
    assert not (e.sandboxes.state_dir(run.instance_id) / "in.bundle").exists()

    done = _wait_done(e.mgr, run.id)
    assert done.status == "done" and done.exit_code == 0
    assert done.commits_ahead == 1 and done.head_sha != done.base_sha
    assert e.sandboxes.touched

    result = e.mgr.collect(run.id)
    ref = f"refs/heads/cloud/{run.id}/{run.branch}"
    assert ref == f"refs/heads/cloud/{run.id}/write-a-short-result-note"   # cloud/ appears once
    assert result.ref == ref and result.commits_ahead == 1
    assert result.diverged is False and result.conflicts_dry_run == []
    wt = Path(result.worktree_path)
    assert wt == e.repo.parent / f"proj-{run.id}"
    note = (wt / "CLOUD_AGENT_RESULT.md").read_text(encoding="utf-8")
    assert "Write a short result note" in note and run.id in note
    assert _git(e.repo, "rev-parse", ref) == done.head_sha
    assert _user_heads(e.repo) == before                   # never merges or moves user refs
    assert _git(e.repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    final = e.mgr.status(run.id)[0]
    assert final.status == "collected" and final.collected_ref == ref
    assert final.worktree_path == str(wt)
    reasons = [p["reason"] for p in e.events.payloads("agents.changed")]
    assert reasons[:2] == ["spawned", "running"] and "done" in reasons and reasons[-1] == "collected"
    for p in e.events.payloads("agents.changed"):
        assert set(p) == {"ids", "reason"} and p["ids"] == [run.id]

    # Stop frees the sandbox; the collected result stays.
    stopped = e.mgr.stop(run.id)
    assert stopped.status == "collected" and run.instance_id in e.sandboxes.destroyed
    assert e.mgr.has_active() is False


@needs_shell
def test_existing_branch_diverged_with_conflicts(env):
    e = env
    _git(e.repo, "checkout", "-q", "-b", "feature")
    (e.repo / "CLOUD_AGENT_RESULT.md").write_text("user version\n", encoding="utf-8")
    _git(e.repo, "add", "CLOUD_AGENT_RESULT.md")
    _git(e.repo, "commit", "-q", "-m", "user note")
    start = _git(e.repo, "rev-parse", "feature")

    with pytest.raises(ApiError) as exc:
        e.mgr.spawn("x", "x1carbon", repo_path=str(e.repo), branch="feature",
                    template="agent-script")
    assert exc.value.code == "conflict"

    run = e.mgr.spawn("Agent edits the note", "x1carbon", repo_path=str(e.repo),
                      new_branch=False, template="agent-script")
    assert run.branch == "feature" and run.base_sha == start
    _wait_done(e.mgr, run.id)

    # Not diverged yet: the agent is strictly ahead of the user's branch.
    r1 = e.mgr.collect(run.id, into="branch")
    assert (r1.commits_ahead, r1.diverged, r1.worktree_path) == (1, False, None)

    # The user commits on the same branch meanwhile -> diverged with a conflict.
    (e.repo / "CLOUD_AGENT_RESULT.md").write_text("user changed it again\n", encoding="utf-8")
    _git(e.repo, "commit", "-q", "-am", "user edit")
    user_sha = _git(e.repo, "rev-parse", "feature")
    r2 = e.mgr.collect(run.id, into="branch")
    assert r2.diverged is True and r2.commits_ahead == 1
    assert r2.conflicts_dry_run == ["CLOUD_AGENT_RESULT.md"]
    assert _git(e.repo, "rev-parse", "feature") == user_sha     # untouched
    assert _git(e.repo, "status", "--porcelain") == ""
    assert gitrelay.ahead_behind(e.repo, r2.ref, "feature") == (1, 1)


@needs_shell
def test_reassign_from_collected_ref(env):
    e = env
    first = e.mgr.spawn("First pass", "x1carbon", repo_path=str(e.repo), template="agent-script")
    _wait_done(e.mgr, first.id)
    r1 = e.mgr.collect(first.id, into="branch")
    first_head = _git(e.repo, "rev-parse", r1.ref)

    second = e.mgr.spawn("Second pass", "x1carbon", repo_path=str(e.repo), base=r1.ref,
                         branch=first.branch, template="agent-script")
    assert second.status == "running", second.error
    assert second.base_sha == first_head and second.branch == first.branch
    _wait_done(e.mgr, second.id)
    r2 = e.mgr.collect(second.id, into="worktree")
    assert r2.ref == f"refs/heads/cloud/{second.id}/{first.branch}"
    # The branch carried over: the new ref contains both runs' commits.
    assert _git(e.repo, "rev-list", "--count", f"main..{r2.ref}") == "2"
    assert _git(e.repo, "merge-base", "--is-ancestor", first_head, r2.ref) == ""
    note = (Path(r2.worktree_path) / "CLOUD_AGENT_RESULT.md").read_text(encoding="utf-8")
    assert "Second pass" in note


@needs_shell
def test_stop_running_and_delete_branch(env):
    e = env
    run = e.mgr.spawn("Short job", "x1carbon", repo_path=str(e.repo), template="agent-script")
    _wait_done(e.mgr, run.id)
    res = e.mgr.collect(run.id, into="branch")
    assert gitrelay.ref_exists(e.repo, res.ref)
    stopped = e.mgr.stop(run.id, keep_branch=False)
    assert not gitrelay.ref_exists(e.repo, res.ref)
    assert stopped.collected_ref is None and run.instance_id in e.sandboxes.destroyed
    assert "stopped" in [p["reason"] for p in e.events.payloads("agents.changed")]
    with pytest.raises(ApiError) as exc:
        e.mgr.collect(run.id)
    assert exc.value.code == "conflict"


# --------------------------------------------------------------------------- hermes mode security


@needs_shell
def test_hermes_mode_only_model_key_enters_sandbox(env, tmp_path):
    e = env
    e.secrets.set(KEY_NAME, SECRET_VALUE)
    _configure_hermes(e)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    hermes = bin_dir / "hermes"
    hermes.write_bytes(
        b"#!/bin/sh\n"
        b"echo \"hermes args: $*\"\n"
        b"test -f \"$HERMES_HOME/config.yaml\" && echo config-present\n"
        b"printf 'echoing key %s\\n' \"$CC_AGENT_API_KEY\"\n"
        b"echo 'made by the model' > MODEL_CHANGE.txt\n"
        b"exit 0\n")
    hermes.chmod(0o755)
    e.emulator.extra_path = bin_dir

    msg = e.mgr.describe_spawn({"goal": "Fix the README", "host_id": "x1carbon",
                                "repo_path": str(e.repo)})
    assert "hermes mode" in msg and "CC_AGENT_API_KEY" in msg and "[redacted]" not in msg
    assert "model hyperqwen-swift at 192.0.2.10:8080" in msg
    assert SECRET_VALUE not in msg

    run = e.mgr.spawn("Fix the README", "x1carbon", repo_path=str(e.repo), max_turns=5)
    assert run.status == "running", run.error
    # The sandbox env holds only the model key, under its sandbox name.
    assert e.sandboxes.envs[run.instance_id] == {KEY_ENV: SECRET_VALUE}
    config = (e.sandboxes.hermes_home(run.instance_id) / "config.yaml").read_text(encoding="utf-8")
    assert "provider: custom" in config and "key_env: CC_AGENT_API_KEY" in config
    assert "hyperqwen-swift" in config and SECRET_VALUE not in config

    done = _wait_done(e.mgr, run.id)
    assert done.status == "done", done.last_log
    assert "--max-turns 5" in done.reply                  # stdout is the reply now
    assert "--yolo" not in done.reply
    assert "approvals:" in config and "single_query_mode: deny" in config
    assert "config-present" in done.reply
    assert "[redacted]" in done.reply and SECRET_VALUE not in done.reply
    result = e.mgr.collect(run.id, into="branch")
    assert result.commits_ahead == 1
    assert _git(e.repo, "show", f"{result.ref}:MODEL_CHANGE.txt") == "made by the model"

    # The key value never left this computer through argv/stdin, events, records or the bundle.
    for call in e.transport.calls:
        assert SECRET_VALUE not in " ".join(call.argv or [])
        assert SECRET_VALUE.encode() not in (call.stdin or b"")
        assert KEY_NAME not in " ".join(call.argv or [])
    assert SECRET_VALUE not in repr(e.events.events)
    for row in e.services.store.list_records("agent_runs"):
        assert SECRET_VALUE not in repr(row)
    db = (e.tmp / "cloud-compute.db").read_bytes()
    assert SECRET_VALUE.encode() not in db
    log_text = _git(e.repo, "log", "-p", result.ref)
    assert SECRET_VALUE not in log_text
    work = e.sandboxes.work_dir(run.instance_id)
    crepo_cfg = (work / "repo" / ".git" / "config").read_text(encoding="utf-8")
    assert "[remote" not in crepo_cfg and "credential" not in crepo_cfg


def test_hermes_mode_requires_endpoint_and_key(env):
    e = env
    with pytest.raises(ApiError) as exc:      # built-in default: base_url must be configured
        e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    assert exc.value.code == "needs_setup" and exc.value.message == agents_mod.NO_ENDPOINT_MESSAGE

    _configure_hermes(e)
    with pytest.raises(ApiError) as exc:      # key secret not set
        e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    assert exc.value.code == "needs_setup" and KEY_NAME in exc.value.message

    e.secrets.set(KEY_NAME, SECRET_VALUE)
    tpl = e.templates.get("hermes-agent")
    tpl.variables.append(VariableSpec(name="GH_TOKEN", source="secret:GH_TOKEN", scope="runtime"))
    e.templates.save(tpl)
    with pytest.raises(ApiError) as exc:      # nothing but the model key may be sent
        e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    assert exc.value.code == "invalid"

    tpl = _configure_hermes(e)
    tpl.agent["base_url"] = "http://user:pw@example.invalid/v1"
    e.templates.save(tpl)
    with pytest.raises(ApiError) as exc:      # no credentials inside the endpoint URL
        e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    assert exc.value.code == "invalid"

    script = e.mgr._template("agent-script")
    script.variables = [VariableSpec(name="X", source="literal:1")]
    e.templates.save(script)
    with pytest.raises(ApiError):             # script mode sends nothing at all
        e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo), template="agent-script")
    assert e.sandboxes.instances == {}
    assert e.services.store.list_records("agent_runs") == []


# --------------------------------------------------------------------------- validation


def test_spawn_validation_errors(env, tmp_path):
    e = env
    store = e.services.store
    store.upsert_host(sample_slurm_host(limits=HostLimits(allow_agents=True)))
    store.upsert_host(sample_local_host(id="noagents"))

    def err(**kw):
        args = dict(goal="g", host_id="x1carbon", repo_path=str(e.repo), template="agent-script")
        args.update(kw)
        goal, host_id = args.pop("goal"), args.pop("host_id")
        with pytest.raises(ApiError) as exc:
            e.mgr.spawn(goal, host_id, **args)
        return exc.value

    assert err(host_id="nope").code == "not_found"
    assert err(host_id="slurm-x1").code == "invalid"
    assert err(host_id="noagents").code == "limit_reached"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert err(repo_path=str(plain)).code == "invalid"
    assert err(branch="bad..name").code == "invalid"
    assert err(branch="-x").code == "invalid"
    assert err(branch="main").code == "conflict"
    assert err(new_branch=False, branch="missing").code == "invalid"
    assert err(base="no-such-rev").code == "invalid"
    assert err(goal="  ").code == "invalid"
    assert err(max_turns=0).code == "invalid"
    assert err(template="nope").code == "not_found"
    assert e.sandboxes.instances == {}
    with pytest.raises(ApiError) as exc:
        e.mgr.status("a-deadbeef")
    assert exc.value.code == "not_found"
    for bad in ("a-deadbeef", "../x"):
        with pytest.raises(ApiError):
            e.mgr.collect(bad)
    with pytest.raises(ApiError):
        e.mgr.stop("a-deadbeef")


# --------------------------------------------------------------------------- failure paths


def test_failure_after_sandbox_start_destroys_instance(env):
    e = env
    e.emulator.fail_matching = "cat > "       # streaming the bundle in fails
    run = e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo), template="agent-script")
    assert run.status == "failed" and "simulated failure" in run.error
    assert run.instance_id and e.sandboxes.destroyed == [run.instance_id]
    assert e.mgr.status(run.id)[0].status == "failed"
    assert [p["reason"] for p in e.events.payloads("agents.changed")] == ["spawned", "failed"]
    assert e.mgr.has_active() is False


def test_sandbox_spawn_error_is_recorded(env):
    e = env
    e.sandboxes.spawn_error = ApiError(409, "limit_reached", "host x1carbon allows 4 instances")
    run = e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo), template="agent-script")
    assert run.status == "failed" and "allows 4 instances" in run.error
    assert run.instance_id == "" and e.sandboxes.destroyed == []


def test_failure_message_is_redacted(env):
    e = env
    e.secrets.set(KEY_NAME, SECRET_VALUE)
    _configure_hermes(e)
    e.sandboxes.spawn_error = RuntimeError(f"boom {SECRET_VALUE}")
    run = e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    assert run.status == "failed" and SECRET_VALUE not in run.error and "[redacted]" in run.error


def _stored_run(e, **kw) -> AgentRun:
    e.mgr.ensure_templates()
    inst = e.sandboxes.spawn("agent-script", "x1carbon") if "instance_id" not in kw else None
    data = dict(id=new_id("a"), instance_id=inst.id if inst else kw.pop("instance_id"),
                host_id="x1carbon", goal="g", repo_path=str(e.repo), branch="cloud/g",
                base_sha=_git(e.repo, "rev-parse", "main"), head_sha=None, commits_ahead=0,
                status="running", started_at="2026-11-08T12:00:00Z", ended_at=None,
                session_id=None, last_log=[], mode="script", template_id="agent-script")
    data.update(kw)
    run = AgentRun(**data)
    e.mgr._save(run)
    return run


def test_poll_parses_state_and_never_raises(env, fake_transports):
    e = env
    run = _stored_run(e)
    t = fake_transports.get("x1carbon")
    t._run_rules.clear()
    t.script(("podman", "exec"), exec_result(stdout="exit=running\nhead=abc\nahead=2\n==log\nline 1\nline 2\n"))
    assert e.mgr.has_active()
    e.mgr.poll()
    got = e.mgr._load(run.id)
    assert (got.status, got.head_sha, got.commits_ahead, got.last_log) == (
        "running", "abc", 2, ["line 1", "line 2"])
    with pytest.raises(ApiError) as exc:      # still running -> cannot collect yet
        e.mgr.collect(run.id)
    assert exc.value.code == "conflict"

    t._run_rules.clear()
    t.script(("podman", "exec"), exec_result(stdout="exit=3\nhead=abc\nahead=2\n==log\n"))
    e.mgr.poll()
    got = e.mgr._load(run.id)
    assert got.status == "failed" and got.exit_code == 3 and got.ended_at

    run2 = _stored_run(e)
    t._run_rules.clear()

    def boom(argv, stdin):
        raise OSError("ssh exploded")

    t.script(("podman", "exec"), boom)
    assert isinstance(e.mgr.poll(), list)            # never raises
    assert e.mgr._load(run2.id).status == "running"
    e.mgr._host_fail.clear()                     # the exploded exec put the host in backoff
    t._run_rules.clear()
    t.script(("podman", "exec"), exec_result(rc=125, stderr="Error: no such container cc-x"))
    e.mgr.poll()
    assert e.mgr._load(run2.id).status == "failed"
    reasons = [p["reason"] for p in e.events.payloads("agents.changed")]
    assert reasons.count("failed") == 2


def test_stale_starting_run_fails(env):
    e = env
    run = _stored_run(e, instance_id="", status="starting", started_at="2020-01-01T00:00:00Z")
    e.mgr.poll()
    assert e.mgr._load(run.id).status == "failed"


def test_stop_running_kills_and_destroys(env, fake_transports):
    e = env
    run = _stored_run(e)
    t = fake_transports.get("x1carbon")
    t._run_rules.clear()
    t.script(("podman", "exec"), exec_result(stdout="stopped\n"))
    stopped = e.mgr.stop(run.id)
    assert stopped.status == "stopped" and stopped.ended_at
    assert run.instance_id in e.sandboxes.destroyed
    assert any("kill -TERM" in " ".join(a) for a in t.argvs())
    assert e.mgr.has_active() is False


# --------------------------------------------------------------------------- image + approval


def test_ensure_image_builds_with_containerfile_on_stdin(env, fake_transports):
    e = env
    t = fake_transports.get("x1carbon")
    t._run_rules.clear()
    t.script(("podman", "image", "exists"), exec_result(rc=1))
    t.script(("podman", "build"), exec_result(stdout="built"))
    tpl = e.mgr._template("agent-script")
    host = e.services.store.get_host("x1carbon")
    assert e.mgr.ensure_image(host, tpl) is True
    build = [c for c in t.calls if c.argv and c.argv[:2] == ["podman", "build"]][0]
    # A plain argv (no shell on the host), the Containerfile on stdin and no build context.
    assert build.argv == ["podman", "build", "-t", AGENT_IMAGE_LITE,
                          "--build-arg", f"BASE={LITE_BASE}", "-"]
    assert build.stdin == agents_mod.CONTAINERFILE.read_bytes()

    t._run_rules.clear()
    t.script(("podman", "image", "exists"), exec_result(rc=0))
    t.reset_calls()
    assert e.mgr.ensure_image(host, tpl) is False and len(t.calls) == 1

    t._run_rules.clear()
    t.script(("podman", "image", "exists"), exec_result(rc=1))
    t.script(("podman", "build"), exec_result(rc=1, stderr="pull failed"))
    with pytest.raises(ApiError) as exc:
        e.mgr.ensure_image(host, tpl)
    assert "pull failed" in exc.value.message


def test_describe_spawn_text(env):
    e = env
    msg = e.mgr.describe_spawn({"goal": "x" * 200, "host_id": "x1carbon", "repo_path": str(e.repo),
                                "template": "agent-script", "branch": "feat/a"})
    assert msg == (f"Start a cloud agent handoff test on x1carbon (template agent-script, no model): "
                   f"commits a marker file on {e.repo} branch feat/a. Task: '{'x' * 200}'. "
                   "Sends no secrets.")
    # Hermes mode with no endpoint anywhere: still a plain statement, no stray "?" or "[redacted]".
    msg = e.mgr.describe_spawn({"goal": "Fix it", "host_id": "x1carbon", "repo_path": str(e.repo)})
    assert msg == (f"Start a cloud agent on x1carbon (template hermes-agent, hermes mode, no model endpoint set, max 30 turns) on {e.repo} branch fix-it. Task: 'Fix it'. "
                   "Sends the model key CC_AGENT_API_KEY (read from the secret CC_AGENT_API_KEY) into the sandbox. No other secrets.")
    # With the endpoint and model from the template.
    _configure_hermes(e, key_secret="CC_AGENT_API_KEY")
    msg = e.mgr.describe_spawn({"goal": "Fix it", "host_id": "x1carbon", "repo_path": str(e.repo)})
    assert msg == (f"Start a cloud agent on x1carbon (template hermes-agent, hermes mode, model hyperqwen-swift at "
                   f"192.0.2.10:8080, max 30 turns) on {e.repo} branch fix-it. Task: 'Fix it'. "
                   "Sends the model key CC_AGENT_API_KEY (read from the secret CC_AGENT_API_KEY) into the sandbox. No other secrets.")
    # A key read from a differently named secret is named.
    _configure_hermes(e, key_secret=KEY_NAME)
    msg = e.mgr.describe_spawn({"goal": "Fix it", "host_id": "x1carbon", "repo_path": str(e.repo)})
    assert f"CC_AGENT_API_KEY (read from the secret {KEY_NAME}) into the sandbox" in msg
    assert e.sandboxes.instances == {} and e.transport.calls == []


# --------------------------------------------------------------------------- endpoint resolution


def _plain_hermes(e):
    """The built-in hermes-agent template, saved with no endpoint or model of its own."""
    tpl = copy.deepcopy(next(t for t in AGENT_TEMPLATES if t.id == "hermes-agent"))
    assert tpl.agent["base_url"] is None and tpl.agent["model"] is None
    e.templates.save(tpl)
    return tpl


def _check(e, tpl):
    host = e.services.store.get_host("x1carbon")
    cfg = e.mgr._agent_cfg(tpl)
    e.mgr._check_template(tpl, cfg, host)
    return cfg


def test_endpoint_from_settings_when_template_has_none(env):
    e = env
    e.secrets.set("CC_AGENT_API_KEY", SECRET_VALUE)
    e.services.config.agent_base_url = " http://10.0.0.5:8080/v1 "
    e.services.config.agent_model = "qwen-test"
    cfg = _check(e, _plain_hermes(e))
    assert (cfg["base_url"], cfg["model"]) == ("http://10.0.0.5:8080/v1", "qwen-test")


def test_template_endpoint_wins_over_settings(env):
    e = env
    e.secrets.set("CC_AGENT_API_KEY", SECRET_VALUE)
    e.services.config.agent_base_url = "http://settings.invalid/v1"
    e.services.config.agent_model = "settings-model"
    tpl = _plain_hermes(e)
    tpl.agent = dict(tpl.agent, base_url="http://tpl.invalid:1/v1")      # model still from settings
    cfg = _check(e, tpl)
    assert (cfg["base_url"], cfg["model"]) == ("http://tpl.invalid:1/v1", "settings-model")
    tpl.agent = dict(tpl.agent, model="tpl-model")
    assert _check(e, tpl)["model"] == "tpl-model"


@pytest.mark.parametrize("base_url,model", [("", ""), ("http://h/v1", ""), ("", "m")])
def test_missing_endpoint_or_model_is_a_clear_400(env, base_url, model):
    e = env
    e.secrets.set("CC_AGENT_API_KEY", SECRET_VALUE)
    e.services.config.agent_base_url = base_url
    e.services.config.agent_model = model
    _plain_hermes(e)
    with pytest.raises(ApiError) as exc:
        e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    assert exc.value.status == 409 and exc.value.code == "needs_setup"
    assert "Compute, Agents, New agent run, Model" in exc.value.message
    assert e.sandboxes.instances == {}


@pytest.mark.parametrize("url", ["ftp://host/v1", "host:8080/v1", "http://user:pw@host/v1", "javascript:1"])
def test_settings_endpoint_must_be_http_without_credentials(env, url):
    e = env
    e.secrets.set("CC_AGENT_API_KEY", SECRET_VALUE)
    e.services.config.agent_base_url = url
    e.services.config.agent_model = "m"
    _plain_hermes(e)
    with pytest.raises(ApiError) as exc:
        e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    assert exc.value.code == "invalid" and "http(s)" in exc.value.message
    assert "pw" not in exc.value.message


def test_script_mode_needs_no_model(env):
    e = env
    assert e.services.config.agent_base_url == "" and e.services.config.agent_model == ""
    e.mgr.ensure_templates()
    cfg = _check(e, e.templates.get("agent-script"))
    assert cfg["mode"] == "script"


@needs_shell
def test_settings_endpoint_reaches_the_sandbox_config(env, tmp_path):
    e = env
    bin_dir = tmp_path / "fakebin"                    # never run a real `hermes chat` here
    bin_dir.mkdir()
    shim = bin_dir / "hermes"
    shim.write_bytes(b"#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)
    e.emulator.extra_path = bin_dir
    e.secrets.set("CC_AGENT_API_KEY", SECRET_VALUE)
    e.services.config.agent_base_url = "http://10.0.0.5:8080/v1"
    e.services.config.agent_model = "qwen-test"
    _plain_hermes(e)
    run = e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    assert run.status == "running", run.error
    config = (e.sandboxes.hermes_home(run.instance_id) / "config.yaml").read_text(encoding="utf-8")
    assert "http://10.0.0.5:8080/v1" in config and "qwen-test" in config
    assert SECRET_VALUE not in config and SECRET_VALUE not in repr(run.to_dict())
    msg = e.mgr.describe_spawn({"goal": "g", "host_id": "x1carbon", "repo_path": str(e.repo)})
    assert "model qwen-test at 10.0.0.5:8080" in msg


# --------------------------------------------------------------------------- idle-stopped sandbox


def _finished_run(e):
    run = e.mgr.spawn("Write a short result note", "x1carbon", repo_path=str(e.repo),
                      template="agent-script")
    assert run.status == "running", run.error
    done = _wait_done(e.mgr, run.id)
    assert done.status == "done"
    return done


def _fake_clock(e):
    clk = [1000.0]
    e.mgr._clock = lambda: clk[0]

    def sleep(seconds):
        clk[0] += seconds

    e.mgr._sleep = sleep
    return clk


@needs_shell
def test_collect_restarts_a_sandbox_the_reaper_idle_stopped(env):
    e = env
    done = _finished_run(e)
    e.sandboxes.instances[done.instance_id].status = "stopped"      # what the reaper does
    result = e.mgr.collect(done.id)
    assert e.sandboxes.start_calls == [done.instance_id]
    assert e.sandboxes.instances[done.instance_id].status == "running"
    assert result.commits_ahead == 1
    assert (Path(result.worktree_path) / "CLOUD_AGENT_RESULT.md").is_file()
    assert e.mgr.status(done.id)[0].status == "collected"


@needs_shell
def test_collect_does_not_start_a_running_sandbox(env):
    e = env
    done = _finished_run(e)
    e.mgr.collect(done.id)
    assert e.sandboxes.start_calls == []


@needs_shell
def test_collect_waits_for_the_restarted_sandbox_to_run(env):
    e = env
    clk = _fake_clock(e)
    done = _finished_run(e)
    e.sandboxes.instances[done.instance_id].status = "stopped"
    e.sandboxes.start_lag = 3                                       # three polls until running
    t0 = clk[0]
    e.mgr.collect(done.id)
    assert e.sandboxes.start_calls == [done.instance_id]
    assert 0 < clk[0] - t0 < agents_mod.RESTART_WAIT_S              # waited, not forever


def test_collect_times_out_clearly_when_the_sandbox_never_runs(env):
    e = env
    clk = _fake_clock(e)
    run = _stored_run(e, status="done", exit_code=0)
    e.sandboxes.instances[run.instance_id].status = "stopped"
    e.sandboxes.start_lag = 10 ** 9                                 # never comes back
    with pytest.raises(ApiError) as exc:
        e.mgr.collect(run.id)
    assert exc.value.code == "conflict"
    assert "did not come back" in exc.value.message and "60s" in exc.value.message
    assert clk[0] - 1000.0 >= agents_mod.RESTART_WAIT_S
    assert not any(a[1] == "exec" for a in e.transport.argvs())     # never ran against it
    assert e.mgr._load(run.id).status == "done"                     # still collectable later


def test_collect_reports_a_failed_restart(env):
    e = env
    run = _stored_run(e, status="done", exit_code=0)
    e.sandboxes.instances[run.instance_id].status = "stopped"
    e.sandboxes.start_error = ApiError(502, "internal", "podman start failed (rc=125)")
    with pytest.raises(ApiError) as exc:
        e.mgr.collect(run.id)
    assert exc.value.code == "conflict"
    assert "could not be restarted" in exc.value.message and "rc=125" in exc.value.message


@needs_shell
def test_recollect_uses_the_saved_ref_when_the_sandbox_cannot_restart(env):
    e = env
    done = _finished_run(e)
    first = e.mgr.collect(done.id)
    e.sandboxes.instances[done.instance_id].status = "stopped"
    e.sandboxes.start_error = ApiError(502, "internal", "podman start failed")
    again = e.mgr.collect(done.id)
    assert again.ref == first.ref


def test_poll_touches_the_sandbox_on_every_tick(env):
    e = env
    run = _stored_run(e)
    t = e.transport
    t._run_rules.clear()
    t.script(("podman", "exec"), exec_result(stdout="exit=running\nhead=abc\nahead=0\n==log\n"))
    e.mgr.poll()
    e.mgr.poll()
    assert e.sandboxes.touched == [run.instance_id, run.instance_id]
    t._run_rules.clear()                    # a failing poll still counts as activity
    t.script(("podman", "exec"), exec_result(rc=1, stderr="boom"))
    e.mgr.poll()
    assert e.sandboxes.touched.count(run.instance_id) == 3


# --------------------------------------------------------------------------- lifetimes (Settings)

def _legacy_copy(template_id: str, **changes) -> TemplateSpec:
    """A stored copy exactly as an earlier version seeded it."""
    from dataclasses import replace
    ttl, idle = agents_mod._LEGACY_LIFETIMES[template_id]
    spec = next(t for t in AGENT_TEMPLATES if t.id == template_id)
    return replace(copy.deepcopy(spec), ttl_minutes=ttl, idle_minutes=idle, **changes)


def test_legacy_seeded_agent_templates_switch_to_unset(env):
    e = env
    for tid in ("hermes-agent", "agent-script"):
        e.templates.save(_legacy_copy(tid))
    assert (e.templates.get("hermes-agent").ttl_minutes, e.templates.get("hermes-agent").idle_minutes) == (240, 120)
    assert (e.templates.get("agent-script").ttl_minutes, e.templates.get("agent-script").idle_minutes) == (120, 60)
    assert e.mgr.ensure_templates() == []                 # nothing newly saved...
    for tid in ("hermes-agent", "agent-script"):          # ...but the lifetimes are now unset
        got = e.templates.get(tid)
        assert got.ttl_minutes is None and got.idle_minutes is None, tid


def test_edited_agent_templates_keep_their_values(env):
    e = env
    e.templates.save(_legacy_copy("hermes-agent", cpus=4))                 # other field edited
    e.templates.save(_legacy_copy("agent-script"))
    tpl = e.templates.get("agent-script")
    tpl.ttl_minutes = 180                                                  # lifetime edited
    e.templates.save(tpl)
    e.mgr.ensure_templates()
    hermes = e.templates.get("hermes-agent")
    assert (hermes.ttl_minutes, hermes.idle_minutes, hermes.cpus) == (240, 120, 4)
    script = e.templates.get("agent-script")
    assert (script.ttl_minutes, script.idle_minutes) == (180, 60)


def test_ensure_templates_leaves_explicit_values_that_are_not_the_old_seed(env):
    e = env
    tpl = _legacy_copy("hermes-agent")
    tpl.ttl_minutes, tpl.idle_minutes = 240, 90        # idle differs from the old seed
    e.templates.save(tpl)
    e.mgr.ensure_templates()
    got = e.templates.get("hermes-agent")
    assert (got.ttl_minutes, got.idle_minutes) == (240, 90)



def _live(**values):
    """A LiveConfig over a mutable dict; only reload() re-reads (huge min_interval)."""
    from cloud_compute_core.config import Config, LiveConfig
    state = dict(values)
    return LiveConfig(loader=lambda: Config(**state), min_interval=3600), state


def test_builtin_agent_templates_leave_ttl_and_idle_to_the_settings_defaults():
    for t in AGENT_TEMPLATES:
        assert t.ttl_minutes is None and t.idle_minutes is None, t.id
        assert t.persistent is False, t.id          # a TTL is always in force for agent runs


@needs_shell
@pytest.mark.parametrize("template", ["agent-script", "hermes-agent"])
def test_agent_run_gets_ttl_and_idle_from_settings(env, template):
    e = env
    e.secrets.set(KEY_NAME, SECRET_VALUE)
    _configure_hermes(e)
    run = e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo), template=template)
    assert e.sandboxes.limits[run.instance_id] == (240, 60)        # the Settings defaults


@needs_shell
def test_settings_change_applies_to_the_next_agent_run_without_a_restart(env):
    e = env
    cfg, state = _live(default_ttl_minutes=90, default_idle_minutes=15)
    e.services.set("config", cfg)
    first = e.mgr.spawn("one", "x1carbon", repo_path=str(e.repo), template="agent-script")
    assert e.sandboxes.limits[first.instance_id] == (90, 15)
    state.update(default_ttl_minutes=600, default_idle_minutes=120)        # saved in Settings
    second = e.mgr.spawn("two", "x1carbon", repo_path=str(e.repo), template="agent-script")
    assert e.sandboxes.limits[second.instance_id] == (600, 120)
    assert e.sandboxes.limits[first.instance_id] == (90, 15)               # the running one is kept


@needs_shell
def test_an_explicit_template_lifetime_beats_settings(env):
    e = env
    cfg, _state = _live(default_ttl_minutes=90, default_idle_minutes=15)
    e.services.set("config", cfg)
    e.mgr.ensure_templates()
    tpl = e.templates.get("agent-script")
    tpl.ttl_minutes, tpl.idle_minutes = 30, None       # mixed: TTL explicit, idle from Settings
    e.templates.save(tpl)
    run = e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo), template="agent-script")
    assert e.sandboxes.limits[run.instance_id] == (30, 15)


def test_a_working_agent_is_not_idle_stopped_with_the_default_idle_timeout(env):
    """Poll touches the sandbox; the reaper's rule then keeps a run alive past the 60 min idle default."""
    from cloud_compute_core.reaper import ReapReport, Reaper
    from cloud_compute_core.testing import sample_instance as rec_of

    e = env
    t0 = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    now = {"t": t0}
    e.sandboxes.clock = lambda: now["t"]
    e.mgr.ensure_templates()
    inst = e.sandboxes.spawn("agent-script", "x1carbon")
    ttl, idle = e.sandboxes.limits[inst.id]
    assert (ttl, idle) == (240, 60)
    run = _stored_run(e, instance_id=inst.id)
    t = e.transport
    t._run_rules.clear()
    t.script(("podman", "exec"), exec_result(stdout=_RUNNING))

    def reap(at):
        stopped: list[str] = []
        sandboxes = SimpleNamespace(stop=lambda i, reason="": stopped.append(i), destroy=lambda *a, **k: None)
        rec = rec_of(id=inst.id, idle_minutes=idle, status="running",
                     expires_at=(t0 + timedelta(minutes=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     last_activity_at=e.sandboxes.activity[inst.id].strftime("%Y-%m-%dT%H:%M:%SZ"))
        Reaper(e.services)._reap_record(sandboxes, rec, at, ReapReport())
        return stopped

    for minute in range(5, 125, 5):             # a poll every 5 minutes for 2 hours
        now["t"] = t0 + timedelta(minutes=minute)
        e.mgr.poll()
        assert e.sandboxes.touched[-1] == run.instance_id
        assert reap(now["t"]) == [], minute
    assert reap(now["t"] + timedelta(minutes=61)) == [inst.id]    # control: no polls -> idle stop


# --------------------------------------------------------------------------- poll budget + backoff

_RUNNING = "exit=running\nhead=abc\nahead=0\n==log\n"


def _clocked_exec(e, clk, *, cost, result):
    """Every `podman exec` takes `cost` seconds of fake time (never more than its timeout)."""
    t = e.transport
    t._run_rules.clear()

    def run(argv, stdin):
        spent = min(cost, t.calls[-1].timeout)
        clk[0] += spent
        return result() if callable(result) else result

    t.script(("podman", "exec"), run)
    return t


def _execs(t):
    return [c for c in t.calls if c.kind == "run" and c.argv and c.argv[1] == "exec"]


def test_poll_tick_is_bounded_to_the_time_budget(env):
    e = env
    clk = [1000.0]
    e.mgr._clock = lambda: clk[0]
    runs = [_stored_run(e) for _ in range(5)]
    t = _clocked_exec(e, clk, cost=3.0, result=exec_result(stdout=_RUNNING))
    e.mgr.poll()
    calls = _execs(t)
    assert len(calls) == 3                                   # 4th and 5th wait for the next tick
    assert [c.timeout for c in calls] == [8.0, 5.0, 2.0]     # each exec capped by what is left
    assert clk[0] - 1000.0 <= agents_mod.POLL_BUDGET_S + 1.0
    assert all(e.mgr._load(r.id).status == "running" for r in runs)


def test_offline_host_is_skipped_with_backoff_and_resets_on_success(env):
    e = env
    clk = [1000.0]
    e.mgr._clock = lambda: clk[0]
    offline = exec_result(rc=124, stderr="timed out", timed_out=True)
    runs = [_stored_run(e), _stored_run(e)]
    t = _clocked_exec(e, clk, cost=15.0, result=offline)

    e.mgr.poll()                                             # first tick: one timeout, host marked
    assert len(_execs(t)) == 1 and _execs(t)[0].timeout == 8.0
    assert clk[0] - 1000.0 <= agents_mod.POLL_BUDGET_S + 0.01   # exec was capped at the budget

    def tick(advance):
        before = len(_execs(t))
        clk[0] += advance
        e.mgr.poll()
        return len(_execs(t)) - before

    assert tick(10) == 0                                     # inside the 30 s backoff
    assert tick(21) == 1                                     # backoff over -> one more attempt
    assert tick(40) == 0                                     # second failure -> 60 s backoff
    assert tick(30) == 1
    assert e.mgr._host_fail["x1carbon"][0] == 3

    # Backoff doubles and is capped at 5 minutes.
    delays = []
    for _ in range(8):
        e.mgr._note_host("x1carbon", False)
        delays.append(e.mgr._host_fail["x1carbon"][1] - clk[0])
    assert max(delays) == agents_mod.BACKOFF_MAX_S == 300.0 and delays[-1] == 300.0

    # The host recovers: the next attempt succeeds and clears the backoff immediately.
    _clocked_exec(e, clk, cost=0.1, result=exec_result(stdout=_RUNNING))
    clk[0] += 301
    e.mgr.poll()
    assert "x1carbon" not in e.mgr._host_fail
    t2 = e.transport
    before = len(_execs(t2))
    e.mgr.poll()
    assert len(_execs(t2)) - before == len(runs)             # polled again right away


def test_one_offline_host_does_not_block_other_hosts(env, fake_transports):
    e = env
    clk = [1000.0]
    e.mgr._clock = lambda: clk[0]
    e.services.store.upsert_host(sample_host(id="h2", address="h2.invalid", runtime_pref="podman",
                                             limits=HostLimits(allow_agents=True)))
    bad = _stored_run(e)
    inst2 = e.sandboxes.spawn("agent-script", "h2")
    good = _stored_run(e, instance_id=inst2.id, host_id="h2")
    _clocked_exec(e, clk, cost=1.0, result=exec_result(rc=124, timed_out=True))
    t2 = fake_transports.get("h2")
    t2.script(("podman", "exec"), exec_result(stdout=_RUNNING))

    e.mgr.poll()
    assert e.mgr._host_blocked("x1carbon") and not e.mgr._host_blocked("h2")
    n2 = len(_execs(t2))
    clk[0] += 5
    e.mgr.poll()
    assert len(_execs(e.transport)) == 1                     # x1carbon skipped
    assert len(_execs(t2)) == n2 + 1                         # h2 still polled
    assert e.mgr._load(bad.id).status == "running" and e.mgr._load(good.id).status == "running"


# --------------------------------------------------------------------------- templates exist before a spawn


def _real_templates(services):
    from cloud_compute_core.templates import TemplateStore
    store = TemplateStore(services.store)
    services.set("templates", store)
    return store


def test_agent_templates_are_listed_before_any_spawn(services):
    store = _real_templates(services)
    assert {"hermes-agent", "agent-script"}.isdisjoint(t.id for t in store.list())
    mgr = agents_mod.provide(services)                 # what services.agents does on first use
    listed = {t.id: t for t in store.list()}
    assert {"hermes-agent", "agent-script"} <= set(listed)
    assert listed["hermes-agent"].agent["mode"] == "hermes" and listed["agent-script"].agent["mode"] == "script"
    assert mgr.status() == []                          # still no run


def test_template_seed_runs_once_and_keeps_user_edits(services):
    store = _real_templates(services)
    saves = []
    real_save = store.save
    store.save = lambda spec: (saves.append(spec.id), real_save(spec))[1]
    mgr = agents_mod.provide(services)
    assert sorted(saves) == ["agent-script", "hermes-agent"]
    mine = store.get("hermes-agent")
    mine.agent = dict(mine.agent, model="my-model", base_url="http://10.0.0.5:8080/v1")
    real_save(mine)
    saves.clear()
    assert mgr.ensure_templates() == [] and mgr.status() == []      # the seed is not repeated
    assert saves == []
    again = agents_mod.provide(services)                            # a new manager must not overwrite
    assert saves == [] and again is not mgr
    assert store.get("hermes-agent").agent["model"] == "my-model"


def test_failed_seed_does_not_break_the_service_and_is_retried(services):
    store = _real_templates(services)
    real_list_ok = store.get

    def boom(template_id):
        raise OSError("store locked")

    store.get = boom
    mgr = agents_mod.provide(services)                 # must not raise
    assert mgr._templates_ready is False
    store.get = real_list_ok
    mgr.status()                                       # a later call seeds them
    assert {"hermes-agent", "agent-script"} <= {t.id for t in store.list()}
    assert mgr._templates_ready is True


# --------------------------------------------------------------------------- collect again


def _sandbox_repo(e, run) -> Path:
    crepo = e.sandboxes.work_dir(run.instance_id) / "repo"
    assert _git(crepo, "rev-parse", "--abbrev-ref", "HEAD") == run.branch
    return crepo


def _agent_commit(crepo: Path, name: str, text: str) -> str:
    (crepo / name).write_text(text, encoding="utf-8")
    _git(crepo, "add", name)
    _git(crepo, "commit", "-q", "-m", f"agent adds {name}")
    return _git(crepo, "rev-parse", "HEAD")


@needs_shell
def test_second_collect_with_nothing_new_is_a_noop(env, monkeypatch):
    e = env
    done = _finished_run(e)
    first = e.mgr.collect(done.id)
    assert Path(first.worktree_path).is_dir()
    tip = _git(e.repo, "rev-parse", first.ref)

    def no_fetch(*a, **k):
        raise AssertionError("nothing changed in the sandbox: no bundle should be fetched")

    monkeypatch.setattr(gitrelay, "import_bundle_bytes_detailed", no_fetch)
    monkeypatch.setattr(gitrelay, "import_bundle_bytes", no_fetch)
    second = e.mgr.collect(done.id)
    third = e.mgr.collect(done.id)
    assert second == first == third                       # the same CollectResult, no error
    assert second.message == "" and second.diverged is False
    assert _git(e.repo, "rev-parse", first.ref) == tip
    assert e.mgr.status(done.id)[0].status == "collected"


@needs_shell
def test_second_collect_fast_forwards_the_worktree_with_new_commits(env):
    e = env
    done = _finished_run(e)
    first = e.mgr.collect(done.id)
    wt = Path(first.worktree_path)
    new = _agent_commit(_sandbox_repo(e, done), "extra.txt", "more work\n")

    second = e.mgr.collect(done.id)
    assert second.ref == first.ref and second.commits_ahead == 2
    assert second.worktree_path == first.worktree_path and second.message == ""
    assert _git(e.repo, "rev-parse", first.ref) == new
    assert (wt / "extra.txt").read_text(encoding="utf-8") == "more work\n"   # worktree followed
    assert _git(wt, "rev-parse", "HEAD") == new and _git(wt, "status", "--porcelain") == ""
    assert e.mgr.status(done.id)[0].status == "collected"
    assert e.mgr.collect(done.id) == second                                   # and again: a no-op


@needs_shell
def test_second_collect_diverged_reports_it_and_never_forces(env):
    e = env
    done = _finished_run(e)
    first = e.mgr.collect(done.id)
    wt = Path(first.worktree_path)
    _git(wt, "config", "user.name", "Test User")
    _git(wt, "config", "user.email", "test@example.invalid")
    (wt / "mine.txt").write_text("my edit\n", encoding="utf-8")
    _git(wt, "add", "mine.txt")
    _git(wt, "commit", "-q", "-m", "my own commit in the worktree")
    mine = _git(wt, "rev-parse", "HEAD")
    theirs = _agent_commit(_sandbox_repo(e, done), "extra.txt", "more work\n")
    heads = _user_heads(e.repo)

    result = e.mgr.collect(done.id)
    assert result.diverged is True and result.ref == first.ref
    assert "nothing was changed" in result.message
    assert "refs/cloud-compute/incoming/" in result.message
    assert _git(e.repo, "rev-parse", first.ref) == mine               # not moved
    assert _git(wt, "rev-parse", "HEAD") == mine and not (wt / "extra.txt").exists()
    incoming = f"refs/cloud-compute/incoming/heads/cloud/{done.id}/{done.branch}"
    assert _git(e.repo, "rev-parse", incoming) == theirs               # the commits are kept
    assert _user_heads(e.repo) == heads
    assert e.mgr.status(done.id)[0].status == "collected"

    # Deleting the run's branches also removes the kept tip.
    e.mgr.stop(done.id, keep_branch=False)
    assert subprocess.run(["git", "-C", str(e.repo), "rev-parse", "--verify", "-q", incoming],
                          capture_output=True).returncode != 0


@needs_shell
def test_first_collect_after_a_poll_still_fetches(env):
    """The unchanged shortcut only applies once the local ref already is the sandbox head."""
    e = env
    done = _finished_run(e)
    assert done.head_sha and not gitrelay.ref_exists(e.repo, f"refs/heads/cloud/{done.id}/{done.branch}")
    result = e.mgr.collect(done.id)
    assert result.commits_ahead == 1 and Path(result.worktree_path).is_dir()
    assert _git(e.repo, "rev-parse", result.ref) == done.head_sha


@needs_shell
def test_collect_after_the_user_advanced_the_worktree_is_still_unchanged(env):
    e = env
    done = _finished_run(e)
    first = e.mgr.collect(done.id)
    wt = Path(first.worktree_path)
    _git(wt, "config", "user.name", "Test User")
    _git(wt, "config", "user.email", "test@example.invalid")
    (wt / "mine.txt").write_text("mine\n", encoding="utf-8")
    _git(wt, "add", "mine.txt")
    _git(wt, "commit", "-q", "-m", "mine")
    mine = _git(wt, "rev-parse", "HEAD")
    again = e.mgr.collect(done.id)               # sandbox has nothing new; the user is ahead
    assert again.ref == first.ref and again.diverged is False and again.message == ""
    assert _git(e.repo, "rev-parse", first.ref) == mine and _git(wt, "rev-parse", "HEAD") == mine


def test_spawn_uses_settings_saved_after_startup_without_a_restart(env, monkeypatch):
    """The manager reads the endpoint when it needs it, not when the services were built."""
    from cloud_compute_core.config import Config, LiveConfig

    e = env
    saved = {"agent_base_url": "", "agent_model": ""}
    clock = [0.0]
    live = LiveConfig(loader=lambda: Config.from_mapping(saved), min_interval=3600.0,
                      clock=lambda: clock[0])
    e.services.set("config", live)                       # built at startup, never rebuilt
    e.secrets.set("CC_AGENT_API_KEY", SECRET_VALUE)
    _plain_hermes(e)
    with pytest.raises(ApiError) as exc:
        e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    assert exc.value.message == agents_mod.NO_ENDPOINT_MESSAGE
    assert e.sandboxes.instances == {}

    saved.update(agent_base_url="http://10.0.0.5:8080/v1", agent_model="qwen-test")   # Settings -> Save
    assert e.services.config is live and live.agent_model == ""    # a read inside the window is stale
    cfg = _check(e, _plain_hermes(e))                              # but acting on a click re-reads
    assert (cfg["base_url"], cfg["model"]) == ("http://10.0.0.5:8080/v1", "qwen-test")
    msg = e.mgr.describe_spawn({"goal": "g", "host_id": "x1carbon", "repo_path": str(e.repo)})
    assert "model qwen-test at 10.0.0.5:8080" in msg

    saved.update(agent_model="other-model")
    assert _check(e, _plain_hermes(e))["model"] == "other-model"   # every later save too
    saved.update(agent_base_url="", agent_model="")
    with pytest.raises(ApiError):
        _check(e, _plain_hermes(e))


@needs_shell
def test_spawn_after_saving_settings_reaches_the_sandbox(env, tmp_path):
    from cloud_compute_core.config import Config, LiveConfig

    e = env
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    shim = bin_dir / "hermes"
    shim.write_bytes(b"#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)
    e.emulator.extra_path = bin_dir
    saved: dict = {}
    e.services.set("config", LiveConfig(loader=lambda: Config.from_mapping(saved),
                                        min_interval=3600.0, clock=lambda: 0.0))
    e.secrets.set("CC_AGENT_API_KEY", SECRET_VALUE)
    _plain_hermes(e)
    with pytest.raises(ApiError):
        e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    saved.update(agent_base_url="http://10.0.0.5:8080/v1", agent_model="qwen-test")
    run = e.mgr.spawn("g", "x1carbon", repo_path=str(e.repo))
    assert run.status == "running", run.error
    config = (e.sandboxes.hermes_home(run.instance_id) / "config.yaml").read_text(encoding="utf-8")
    assert "http://10.0.0.5:8080/v1" in config and "qwen-test" in config


@needs_shell
def test_stop_clears_the_instance_and_keeps_the_collected_branch(env):
    e = env
    done = _finished_run(e)
    assert done.instance_id
    collected = e.mgr.collect(done.id)

    stopped = e.mgr.stop(done.id)
    assert stopped.status == "collected"                          # status unchanged
    assert stopped.instance_id == ""                              # the sandbox is gone: say so
    assert stopped.collected_ref == collected.ref and stopped.worktree_path == collected.worktree_path
    assert done.instance_id in e.sandboxes.destroyed
    stored = e.mgr.status(done.id)[0].to_dict()
    assert stored["instance_id"] == "" and stored["collected_ref"] == collected.ref
    assert stored["worktree_path"] == collected.worktree_path
    assert gitrelay.ref_exists(e.repo, collected.ref)

    e.sandboxes.destroyed.clear()
    again = e.mgr.stop(done.id)                                   # a second Stop is harmless
    assert again.instance_id == "" and e.sandboxes.destroyed == []
    # the saved result is still answered without a sandbox
    assert e.mgr.collect(done.id).ref == collected.ref


def test_stop_of_a_run_that_was_never_collected_sets_stopped_and_clears_the_instance(env):
    e = env
    run = _stored_run(e, status="running")
    stopped = e.mgr.stop(run.id)
    assert (stopped.status, stopped.instance_id) == ("stopped", "")


def test_collect_result_message_is_additive():
    plain = CollectResult(run_id="a-0123abcd", ref="r", commits_ahead=1, worktree_path=None,
                          diverged=False)
    assert "message" not in plain.to_dict()
    said = CollectResult(run_id="a-0123abcd", ref="r", commits_ahead=1, worktree_path=None,
                         diverged=True, message="nothing was changed")
    assert said.to_dict()["message"] == "nothing was changed"
    assert CollectResult.from_dict(said.to_dict()) == said
    assert CollectResult.from_dict(plain.to_dict()) == plain


# --------------------------------------------------------------------------- approval system stays on


def test_hermes_runner_script_never_bypasses_hermes_approvals():
    """Catalog rule 12: no YOLO flag, and none inherited from the image environment."""
    script = agents_mod._runner_script("a-0000abcd", "hermes", 7)
    assert "hermes chat -Q --query-file" in script and "--max-turns 7" in script
    assert "--yolo" not in script.lower() and "--accept-hooks" not in script
    assert "unset HERMES_YOLO_MODE" in script
    assert script.index("unset HERMES_YOLO_MODE") < script.index("hermes chat")
    assert "--yolo" not in agents_mod._runner_script("a-0000abcd", "script", 7)


def test_hermes_config_keeps_approvals_on_and_denies_unanswerable_prompts():
    text = agents_mod._hermes_config("https://llm.example.net/v1", "some-model")
    cfg = {}
    section = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        key, _, value = raw.strip().partition(":")
        if not raw.startswith(" "):
            section = cfg.setdefault(key, {})
        else:
            section[key] = value.strip()
    approvals = cfg["approvals"]
    assert approvals["mode"] == "manual"                  # never "off"
    assert approvals["single_query_mode"] == "deny"        # hermes chat -Q: flagged commands are denied
    assert approvals["cron_mode"] == "deny" and approvals["unattended_mode"] == "deny"
    assert 0 < int(approvals["timeout"]) <= 60              # an unanswered prompt fails closed fast
    assert "off" not in approvals.values() and "approve" not in approvals.values()
    assert cfg["model"]["key_env"] == KEY_ENV and "api_key:" not in text.lower()


# --------------------------------------------------------------------------- templates and Stop


def _not_saved():
    return ApiError(409, "not_saved", "The files could not be saved to this computer, so nothing was deleted.")


def test_stop_keeps_the_sandbox_when_its_files_cannot_be_saved(env):
    e = env
    run = _stored_run(e, status="running")
    e.sandboxes.destroy_error = _not_saved()
    stopped = e.mgr.stop(run.id)
    assert stopped.status == "stopped"
    assert stopped.instance_id == run.instance_id                  # still points at the sandbox
    assert stopped.error == ("The sandbox was kept because the files in /work could not be saved "
                            "to this computer. "
                             "Delete it from Overview when ready.")
    assert stopped.error == agents_mod.KEPT_SANDBOX_MESSAGE and stopped.ended_at
    assert e.sandboxes.destroyed == [] and e.sandboxes.instances[run.instance_id].status != "destroyed"
    stored = e.mgr._load(run.id)
    assert (stored.status, stored.instance_id, stored.error) == (
        "stopped", run.instance_id, agents_mod.KEPT_SANDBOX_MESSAGE)
    assert e.mgr.has_active() is False
    assert [p["reason"] for p in e.events.payloads("agents.changed")][-1] == "stopped"


def test_stop_again_after_a_kept_sandbox_destroys_it_once_saving_works(env):
    e = env
    run = _stored_run(e, status="running")
    e.sandboxes.destroy_error = _not_saved()
    assert e.mgr.stop(run.id).instance_id == run.instance_id
    e.sandboxes.destroy_error = _not_saved()
    again = e.mgr.stop(run.id)                                     # still not saved: still kept
    assert again.instance_id == run.instance_id and again.error == agents_mod.KEPT_SANDBOX_MESSAGE
    e.sandboxes.destroy_error = None
    done = e.mgr.stop(run.id)
    assert (done.status, done.instance_id, done.error) == ("stopped", "", None)
    assert e.sandboxes.destroyed == [run.instance_id]


def test_stop_keeps_the_collected_status_and_ref_when_the_sandbox_is_kept(env):
    e = env
    run = _stored_run(e, status="collected", collected_ref="refs/heads/cloud/x/y")
    e.sandboxes.destroy_error = _not_saved()
    stopped = e.mgr.stop(run.id)
    assert stopped.status == "collected" and stopped.collected_ref == "refs/heads/cloud/x/y"
    assert stopped.instance_id == run.instance_id and stopped.error == agents_mod.KEPT_SANDBOX_MESSAGE


@pytest.mark.parametrize("error,expected", [
    (ApiError(409, "conflict", "busy"), "conflict"),
    (ApiError(500, "internal", "boom"), "internal"),
])
def test_stop_other_destroy_errors_behave_as_before(env, error, expected):
    e = env
    run = _stored_run(e, status="running")
    e.sandboxes.destroy_error = error
    with pytest.raises(ApiError) as exc:
        e.mgr.stop(run.id)
    assert exc.value.code == expected
    assert e.mgr._load(run.id).status == "running"                # nothing was recorded


def test_stop_when_the_sandbox_is_already_gone_still_clears_it(env):
    e = env
    run = _stored_run(e, status="running")
    e.sandboxes.destroy_error = ApiError(404, "not_found", "instance gone")
    stopped = e.mgr.stop(run.id)
    assert (stopped.status, stopped.instance_id, stopped.error) == ("stopped", "", None)


def test_collect_of_a_stopped_run_whose_sandbox_was_kept_is_not_refused(env):
    """A kept sandbox still holds the work, so Collect must not claim it was removed."""
    e = env
    gone = _stored_run(e, status="stopped", instance_id="")
    with pytest.raises(ApiError) as exc:
        e.mgr.collect(gone.id)
    assert "sandbox removed" in exc.value.message
    kept = _stored_run(e, status="stopped", error=agents_mod.KEPT_SANDBOX_MESSAGE)
    with pytest.raises(ApiError) as exc:
        e.mgr.collect(kept.id)
    assert "sandbox removed" not in exc.value.message              # it went on to read the sandbox


def test_new_agent_texts_have_no_first_person():
    import re
    assert not re.search(r"\b(we|I)\b", agents_mod.KEPT_SANDBOX_MESSAGE + agents_mod.KEY_DESCRIPTION, re.I)


def test_hermes_agent_template_explains_the_key_and_script_agent_sends_nothing(env):
    e = env
    e.mgr.ensure_templates()
    hermes = e.templates.get("hermes-agent")
    assert [v.name for v in hermes.variables] == [KEY_ENV]
    assert hermes.variables[0].description == agents_mod.KEY_DESCRIPTION
    assert "cloud-agent sandboxes in Hermes mode" in hermes.variables[0].description
    script = e.templates.get("agent-script")
    assert script.variables == [] and script.agent["mode"] == "script"
    # script mode refuses any variable at spawn, so the key can never ride along
    script.variables = [VariableSpec(name=KEY_ENV, source="secret:CC_AGENT_API_KEY", scope="runtime")]
    host = e.services.store.get_host("x1carbon")
    with pytest.raises(ApiError) as exc:
        e.mgr._check_template(script, e.mgr._agent_cfg(script), host)
    assert "script mode sends no variables" in exc.value.message


def test_describe_spawn_for_script_mode_still_says_no_secrets(env):
    e = env
    e.mgr.ensure_templates()
    msg = e.mgr.describe_spawn({"goal": "g", "host_id": "x1carbon", "template": "agent-script",
                                "repo_path": str(e.repo)})
    assert "Sends no secrets." in msg


def test_seeded_copy_without_a_key_description_is_upgraded(env):
    """A copy an earlier version stored (no variable description) was never edited: it gets the text."""
    e = env
    old = copy.deepcopy(next(t for t in AGENT_TEMPLATES if t.id == "hermes-agent"))
    old.variables[0].description = ""
    e.templates.save(old)
    assert e.templates.get("hermes-agent").variables[0].description == ""
    assert e.mgr.ensure_templates() == ["agent-script"]          # only the missing one is newly saved
    assert e.templates.get("hermes-agent").variables[0].description == agents_mod.KEY_DESCRIPTION


def test_seeded_copy_with_old_lifetimes_and_no_description_is_fully_upgraded(env):
    e = env
    legacy = _legacy_copy("hermes-agent")
    legacy.variables[0].description = ""
    e.templates.save(legacy)
    e.mgr.ensure_templates()
    got = e.templates.get("hermes-agent")
    assert (got.ttl_minutes, got.idle_minutes) == (None, None)
    assert got.variables[0].description == agents_mod.KEY_DESCRIPTION


def test_edited_copy_without_a_description_is_left_alone(env):
    e = env
    mine = copy.deepcopy(next(t for t in AGENT_TEMPLATES if t.id == "hermes-agent"))
    mine.variables[0].description = ""
    mine.cpus = 4                                                  # the user changed something
    e.templates.save(mine)
    e.mgr.ensure_templates()
    got = e.templates.get("hermes-agent")
    assert got.cpus == 4 and got.variables[0].description == ""


def test_a_user_description_on_the_key_variable_is_kept(env):
    e = env
    mine = copy.deepcopy(next(t for t in AGENT_TEMPLATES if t.id == "hermes-agent"))
    mine.variables[0].description = "My own words"
    e.templates.save(mine)
    e.mgr.ensure_templates()
    assert e.templates.get("hermes-agent").variables[0].description == "My own words"
