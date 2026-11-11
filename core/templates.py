"""Environment templates: four built-ins plus user templates kept in the store.

Built-ins are copy-on-write: saving a template with a built-in id stores a user
override that shadows the built-in, and deleting the override restores it. A built-in is
never removed: deleting one that has no override is refused. The two agent templates
(``hermes-agent``, ``agent-script``) are owned by :mod:`agents`, which keeps a stored copy of
each; they follow the same rules (see :meth:`TemplateStore.default`).

``GET /templates/{id}?default=1`` (route in ``instances_api``) is meant to return
:meth:`TemplateStore.get_default`: the shipped version of a template. A template is shipped
when that lookup succeeds, and modified when it differs from the stored one (ignoring
``builtin``); see :meth:`TemplateStore.is_modified`.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import uuid
from typing import Any

from .interfaces import ApiError
from .models import HOST_KINDS, TemplateSpec, VariableSpec
from .runtimes import DISPLAY_IMAGE_TAG, DISPLAY_PORT

log = logging.getLogger(__name__)

__all__ = ["BUILTIN_TEMPLATES", "TemplateStore", "builtin_templates", "provide",
           "image_provides_display", "DISPLAY_IMAGE_MESSAGE", "MAX_PUBLISHED_PORTS"]

BUILTIN_TEMPLATES = ["ubuntu-dev", "python-dev", "desktop-novnc", "slurm-python"]

TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_NAME_RE = ENV_NAME_RE
# Same shape agents.py enforces when a run starts (http(s), no userinfo, no whitespace).
BASE_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(/[^\s@]*)?$")
KINDS = ("container", "slurm_job")
NETWORKS = ("default", "none")
SCOPES = ("runtime", "redacted", "agent-visible")
MIN_MINUTES, MAX_MINUTES = 5, 1440
MAX_NAME_CHARS = 60
MAX_DESCRIPTION_CHARS = 500
MAX_VARIABLE_DESCRIPTION_CHARS = 500
MAX_PUBLISHED_PORTS = 10
DISPLAY_IMAGE_MESSAGE = ("Show a screen needs the Desktop (noVNC) image. Start from the Desktop "
                         "(noVNC) template, or turn this switch off.")
BUILTIN_DELETE_MESSAGE = ("Built-in templates cannot be deleted. Edit it to make your own "
                          "version; Reset to default restores it.")


def builtin_templates() -> list[TemplateSpec]:
    """Fresh copies of the built-in templates, in :data:`BUILTIN_TEMPLATES` order.

    The container built-ins leave ``ttl_minutes`` and ``idle_minutes`` unset (``None``), so
    a spawn takes them from the Settings ``default_ttl_minutes`` / ``default_idle_minutes``
    (240 / 60 unless changed). An explicit value in a template always wins. ``slurm-python``
    is a batch job and never spawned as an environment, so the rule does not apply to it.
    """
    return [
        TemplateSpec(
            id="ubuntu-dev", name="Ubuntu dev",
            description="Plain Ubuntu 24.04 shell environment for builds and experiments.",
            image="docker.io/library/ubuntu:24.04", command=["sleep", "infinity"],
            workdir="/work", cpus=2, memory_gb=4, gpus=0,
            ttl_minutes=None, idle_minutes=None, host_kinds=["ssh", "local"], builtin=True),
        TemplateSpec(
            id="python-dev", name="Python dev",
            description="Python 3.12 (slim) environment for scripts and notebooks.",
            image="docker.io/library/python:3.12-slim", command=["sleep", "infinity"],
            workdir="/work", cpus=2, memory_gb=4, gpus=0,
            ttl_minutes=None, idle_minutes=None,
            host_kinds=["ssh", "local"], builtin=True),
        TemplateSpec(
            id="desktop-novnc", name="Desktop (noVNC)",
            description="Linux desktop you can view in the browser through a noVNC screen.",
            image=DISPLAY_IMAGE_TAG, command=[], workdir="/work",
            cpus=2, memory_gb=4, gpus=0, ttl_minutes=None, idle_minutes=None,
            display=True, host_kinds=["ssh", "local"], builtin=True),
        TemplateSpec(
            id="slurm-python", name="Slurm Python job",
            description="Python 3.12 batch job run through Apptainer on a Slurm cluster.",
            kind="slurm_job", image="docker://python:3.12-slim", command=["python", "-V"],
            workdir="/work", cpus=2, memory_gb=4, gpus=0,
            ttl_minutes=None, idle_minutes=None, host_kinds=["slurm"], builtin=True),
    ]


def _image_repo(image: str) -> str:
    """``cloud-compute-novnc`` from ``localhost/cloud-compute-novnc:0.1`` (registry, tag and
    digest dropped)."""
    name = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return name.split(":", 1)[0].lower()


def image_provides_display(image: Any) -> bool:
    """True when ``image`` is the Desktop (noVNC) image (any tag) that can serve a screen.

    A plain OS or language image has no VNC server, so a screen can never start in it.
    """
    if not isinstance(image, str) or not image:
        return False
    return _image_repo(image) == _image_repo(DISPLAY_IMAGE_TAG)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _json_safe(v: Any) -> bool:
    try:
        json.dumps(v, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


#: What an earlier python-dev built-in carried: a silent HF_TOKEN secret. A stored copy that is
#: exactly this (or already exactly the current default) was never edited by the user.
_LEGACY_PYTHON_DEV_VARIABLES = [
    VariableSpec(name="HF_TOKEN", source="secret:HF_TOKEN", scope="runtime")]


def _agent_defaults() -> dict[str, TemplateSpec]:
    """The shipped agent templates, owned by :mod:`agents` (imported lazily, no cycle)."""
    try:
        from .agents import AGENT_TEMPLATES
    except Exception:  # noqa: BLE001 - templates must work without the agents module
        return {}
    return {t.id: t for t in AGENT_TEMPLATES}


def _same(a: TemplateSpec, b: TemplateSpec) -> bool:
    """Equal apart from the ``builtin`` flag (an override is always stored with it off)."""
    da, db = a.to_dict(), b.to_dict()
    da.pop("builtin", None)
    db.pop("builtin", None)
    return da == db


def _name_problem(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return "name is required"
    name = name.strip()
    if len(name) > MAX_NAME_CHARS:
        return f"name must be {MAX_NAME_CHARS} characters or fewer"
    if any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in name):
        return "name must not contain control characters or line breaks"
    return None


class TemplateStore:
    """Built-in and user environment templates (user entries shadow built-ins)."""

    def __init__(self, store: Any):
        self.store = store
        self._lock = threading.RLock()
        self._builtins = {t.id: t for t in builtin_templates()}
        self._migrated = False

    # ------------------------------------------------------------------ shipped defaults

    def default(self, template_id: str) -> TemplateSpec | None:
        """A fresh copy of the shipped template with this id, or ``None`` for a user template."""
        if not isinstance(template_id, str):
            return None
        found = self._builtins.get(template_id) or _agent_defaults().get(template_id)
        return copy.deepcopy(found) if found is not None else None

    def get_default(self, template_id: str) -> TemplateSpec:
        """The shipped version of ``template_id`` (404 for a template that is not shipped)."""
        found = self.default(template_id)
        if found is None:
            raise ApiError(404, "not_found", f"template {template_id} has no default version")
        return found

    def is_modified(self, template_id: str) -> bool:
        """True when a shipped template has been edited (its stored copy differs from the default)."""
        default = self.default(template_id)
        if default is None:
            return False
        with self._lock:
            self._migrate()
            stored = self.store.get_template(template_id)
        return stored is not None and not _same(stored, default)

    def _migrate(self) -> None:
        """Drop a stored python-dev copy that is the old default (with HF_TOKEN) or equal to
        the current one. A copy the user changed in any other way stays as it is."""
        if self._migrated:
            return
        self._migrated = True
        try:
            stored = self.store.get_template("python-dev")
            if stored is None:
                return
            current = self._builtins["python-dev"]
            old = copy.deepcopy(current)
            old.variables = copy.deepcopy(_LEGACY_PYTHON_DEV_VARIABLES)
            if _same(stored, old) or _same(stored, current):
                self.store.delete_template("python-dev")
        except Exception:  # noqa: BLE001 - a stale copy is harmless; never block a read
            log.debug("cloud-compute: could not upgrade the stored python-dev template", exc_info=True)

    def generate_id(self, name: str) -> str:
        """A free template id made from ``name`` (``my-lab-env``, ``my-lab-env-2``, ...)."""
        slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].strip("-") or "template"
        with self._lock:
            taken = {t.id for t in self.store.list_templates()} | set(self._builtins) \
                | set(_agent_defaults())
        if slug not in taken:
            return slug
        for n in range(2, 100):
            if f"{slug}-{n}" not in taken:
                return f"{slug}-{n}"
        return f"{slug}-{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------ reads

    def list(self) -> list[TemplateSpec]:
        """Built-ins first (fixed order, user overrides in place), then user templates by id."""
        with self._lock:
            self._migrate()
            users = {t.id: t for t in self.store.list_templates()}
            out: list[TemplateSpec] = []
            for tid in BUILTIN_TEMPLATES:
                out.append(copy.deepcopy(users.pop(tid, None) or self._builtins[tid]))
            out.extend(copy.deepcopy(users[k]) for k in sorted(users))
            return out

    def get(self, template_id: str) -> TemplateSpec:
        with self._lock:
            self._migrate()
            found = self.store.get_template(template_id) if isinstance(template_id, str) else None
            if found is None and template_id in self._builtins:
                found = self._builtins[template_id]
            if found is None:
                raise ApiError(404, "not_found", f"template {template_id} not found")
            return copy.deepcopy(found)

    # ------------------------------------------------------------------ writes

    def save(self, spec: TemplateSpec) -> TemplateSpec:
        errors = self.validate(spec)
        if errors:
            raise ApiError(400, "invalid", "; ".join(errors))
        stored = copy.deepcopy(spec)
        stored.builtin = False
        stored.name = stored.name.strip()
        with self._lock:
            self._migrate()
            return self.store.upsert_template(stored) or stored

    def create(self, data: dict) -> TemplateSpec:
        """Make a new template from a name and an image (every other field has a default).

        ``id`` is generated from the name when missing, ``description`` may be empty, and
        ``ttl_minutes`` / ``idle_minutes`` stay unset so the Settings defaults apply.
        """
        if not isinstance(data, dict):
            raise ApiError(400, "invalid", "The template details must be a JSON object.")
        data = dict(data)
        problem = _name_problem(data.get("name"))
        if problem:
            raise ApiError(400, "invalid", problem)
        image = data.get("image")
        if not isinstance(image, str) or not image.strip():
            raise ApiError(400, "invalid",
                           "image is required, for example docker.io/library/python:3.12-slim")
        data["image"] = image.strip()
        if data.get("description") is None:
            data["description"] = ""
        for key in ("ttl_minutes", "idle_minutes"):
            data.setdefault(key, None)
        if not data.get("id"):
            data["id"] = self.generate_id(data["name"])
        try:
            spec = TemplateSpec.from_dict(data)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ApiError(400, "invalid", f"The template details are not valid: {exc}") from None
        try:
            existing = self.get(spec.id)
        except ApiError as exc:
            if exc.code != "not_found":
                raise
            existing = None
        if existing is not None and not existing.builtin:
            raise ApiError(409, "conflict", f"template {spec.id} already exists; "
                                            "pick another id or leave it empty to generate one")
        return self.save(spec)

    def delete(self, template_id: str) -> bool:
        """Delete a user template, or reset an edited built-in to its default.

        A built-in is never removed: with no edits to reset the call is refused (400).
        """
        with self._lock:
            self._migrate()
            default = self.default(template_id)
            if default is None:
                return bool(self.store.delete_template(template_id)) \
                    if isinstance(template_id, str) else False
            stored = self.store.get_template(template_id)
            if stored is None:
                raise ApiError(400, "invalid", BUILTIN_DELETE_MESSAGE)
            if template_id in self._builtins:         # the override shadows the built-in
                self.store.delete_template(template_id)
                return True
            # Agent templates keep a stored copy that the agents module seeds: put the default back.
            if _same(stored, default):
                raise ApiError(400, "invalid", BUILTIN_DELETE_MESSAGE)
            default.builtin = False
            self.store.upsert_template(default)
            return True

    # ------------------------------------------------------------------ validation

    def validate(self, spec: TemplateSpec) -> list[str]:
        """Return a list of human-readable problems (empty when the spec is valid)."""
        errs: list[str] = []
        tid = getattr(spec, "id", None)
        if not isinstance(tid, str) or not TEMPLATE_ID_RE.match(tid):
            errs.append("id must be lowercase letters, digits, '-' or '_' "
                        "(1-63 chars, starting with a letter or digit)")
        problem = _name_problem(getattr(spec, "name", None))
        if problem:
            errs.append(problem)
        desc = getattr(spec, "description", "")
        if not isinstance(desc, str):
            errs.append("description must be a string")
        elif len(desc) > MAX_DESCRIPTION_CHARS:
            errs.append(f"description must be {MAX_DESCRIPTION_CHARS} characters or fewer")
        if spec.kind not in KINDS:
            errs.append("kind must be one of: container, slurm_job")
        image = spec.image
        if not isinstance(image, str) or not image or re.search(r"\s", image):
            errs.append("image is required and must not contain whitespace")
        cmd = spec.command
        if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
            errs.append("command must be a list of strings")
        if not isinstance(spec.workdir, str) or not spec.workdir.startswith("/"):
            errs.append("workdir must be an absolute path")
        if not _is_number(spec.cpus) or not spec.cpus > 0:
            errs.append("cpus must be greater than 0")
        if not _is_number(spec.memory_gb) or not spec.memory_gb > 0:
            errs.append("memory_gb must be greater than 0")
        if not isinstance(spec.gpus, int) or isinstance(spec.gpus, bool) or spec.gpus < 0:
            errs.append("gpus must be an integer of 0 or more")
        for label, val in (("ttl_minutes", spec.ttl_minutes), ("idle_minutes", spec.idle_minutes)):
            if val is not None and (not isinstance(val, int) or isinstance(val, bool)
                                    or not MIN_MINUTES <= val <= MAX_MINUTES):
                errs.append(f"{label} must be empty or between {MIN_MINUTES} and {MAX_MINUTES}")
        if spec.network not in NETWORKS:
            errs.append("network must be one of: default, none")
        if not isinstance(spec.persistent, bool):
            errs.append("persistent must be true or false")
        if not isinstance(spec.display, bool):
            errs.append("display must be true or false")
        elif (spec.display and spec.kind == "container" and isinstance(image, str) and image
              and not image_provides_display(image)):
            errs.append(DISPLAY_IMAGE_MESSAGE)
        self._check_ports(spec, errs)
        self._check_variables(spec, errs)
        kinds = spec.host_kinds
        if not isinstance(kinds, list) or not kinds or not all(k in HOST_KINDS for k in kinds):
            errs.append(f"host_kinds must be a non-empty list drawn from: {', '.join(HOST_KINDS)}")
        elif spec.kind == "slurm_job" and any(k != "slurm" for k in kinds):
            errs.append("slurm_job templates can only target slurm hosts")
        elif spec.kind == "container" and "slurm" in kinds:
            errs.append("container templates cannot target slurm hosts")
        if spec.agent is not None and (not isinstance(spec.agent, dict) or not _json_safe(spec.agent)):
            errs.append("agent must be an object with JSON-safe values")
        elif isinstance(spec.agent, dict):
            base_url = spec.agent.get("base_url")
            if base_url is not None and base_url != "":
                if not isinstance(base_url, str) or not BASE_URL_RE.match(base_url):
                    errs.append("agent.base_url must be empty or an http(s):// URL "
                                "without credentials or spaces")
        return errs

    @staticmethod
    def _check_ports(spec: TemplateSpec, errs: list[str]) -> None:
        ports = getattr(spec, "ports", [])
        if not isinstance(ports, list):
            errs.append("ports must be a list of container port numbers, for example [8000]")
            return
        if len(ports) > MAX_PUBLISHED_PORTS:
            errs.append(f"ports can list at most {MAX_PUBLISHED_PORTS} container ports")
        seen: set[int] = set()
        for port in ports:
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                errs.append(f"port {port!r} is not valid: use a container port number from 1 to 65535")
            elif port in seen:
                errs.append(f"port {port} is listed twice")
            elif port == DISPLAY_PORT and spec.display is True:
                errs.append(f"port {DISPLAY_PORT} is used by the screen; remove it from ports or "
                            "turn Show a screen off")
            else:
                seen.add(port)

    @staticmethod
    def _check_variables(spec: TemplateSpec, errs: list[str]) -> None:
        variables = spec.variables
        if not isinstance(variables, list):
            errs.append("variables must be a list")
            return
        seen: set[str] = set()
        for v in variables:
            name = getattr(v, "name", None)
            label = name if isinstance(name, str) and name else "?"
            if not isinstance(name, str) or not ENV_NAME_RE.match(name):
                errs.append(f"variable {label}: name must match [A-Za-z_][A-Za-z0-9_]*")
            elif name in seen:
                errs.append(f"variable {name}: duplicate name")
            else:
                seen.add(name)
            src = getattr(v, "source", None)
            if not isinstance(src, str):
                errs.append(f"variable {label}: source must be literal:<value> or secret:<NAME>")
            elif src.startswith("literal:"):
                if "\n" in src or "\r" in src or "\0" in src:
                    errs.append(f"variable {label}: literal value must be a single line")
            elif src.startswith("secret:"):
                if not SECRET_NAME_RE.match(src[len("secret:"):]):
                    errs.append(f"variable {label}: secret name must match [A-Za-z_][A-Za-z0-9_]*")
            else:
                errs.append(f"variable {label}: source must be literal:<value> or secret:<NAME>")
            if getattr(v, "scope", None) not in SCOPES:
                errs.append(f"variable {label}: scope must be one of: runtime, redacted, agent-visible")
            vdesc = getattr(v, "description", "")
            if not isinstance(vdesc, str) or len(vdesc) > MAX_VARIABLE_DESCRIPTION_CHARS:
                errs.append(f"variable {label}: description must be text of at most "
                            f"{MAX_VARIABLE_DESCRIPTION_CHARS} characters")
            hosts = getattr(v, "hosts", None)
            if hosts is not None and (not isinstance(hosts, list)
                                      or not all(isinstance(h, str) for h in hosts)):
                errs.append(f"variable {label}: hosts must be empty or a list of host ids")


def provide(services: Any) -> TemplateStore:
    """Service factory used by the registry."""
    return TemplateStore(services.store)
