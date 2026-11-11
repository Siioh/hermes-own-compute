"""EC2 host routes: the machine's Start / Stop / Terminate / Extend, its status, the instances
found on AWS that nobody tracks, and the free-tier type list for the Add host wizard.

Routes (all under the plugin's API prefix):

* ``GET  /hosts/{host_id}/machine`` -- machine state, time left, the billing sentence and the
  untracked instances of the last AWS check;
* ``POST /hosts/{host_id}/machine/{action}`` -- ``start``, ``stop``, ``terminate`` (body
  ``delete_environments``, ``save``), ``extend`` (body ``minutes``), ``reconcile``;
* ``GET  /hosts/{host_id}/machine/{action}/confirm`` -- for ``start``, ``stop`` and ``extend``
  (query ``minutes``): what the action does and costs, in the words of the chat card;
* ``POST /hosts/{host_id}/untracked/{instance_id}/{action}`` -- ``stop`` or ``terminate`` an
  instance that carries this install's tag but belongs to no host (the user's choice only);
* ``POST /ec2/free-tier-types`` -- body: an unsaved EC2 host; the types AWS reports as
  free-tier eligible for the account.

Also :func:`validate_ec2` (used by the host routes) and the chat-tool texts for the machine
actions. Standard library only.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .interfaces import ApiError, ApiRequest, BackgroundTask, Route

log = logging.getLogger(__name__)

__all__ = ["routes", "tools", "background_tasks", "validate_ec2", "MACHINE_ACTIONS",
           "machine_action", "describe_machine_action", "CREATED_FIELDS"]

MACHINE_ACTIONS = ("start", "stop", "terminate", "extend")
#: Fields only the plugin writes: never taken from a request.
CREATED_FIELDS = ("key_name", "key_file", "known_hosts_file", "managed_group_id",
                  "ssh_cidr_used", "instance_id", "machine_state", "machine_detail",
                  "launched_at", "started_at", "auto_stop_at", "launched_type",
                  "launched_disk_gb", "launch_token", "account_id", "start_count",
                  "stopped_at", "unit_lifetime_minutes",
                  # derived for the page on every read, never stored
                  "extend_room_minutes", "extend_step_minutes")
#: Settings that only take effect when a machine is CREATED. While an instance exists for the
#: host they cannot change, so a card never names a machine AWS will not start.
LAUNCH_ONLY = ("instance_type", "disk_gb", "image", "subnet_id", "security_group_id")
LAUNCH_ONLY_NAMES = {"instance_type": "instance type", "disk_gb": "disk size", "image": "image",
                     "subnet_id": "subnet", "security_group_id": "security group"}
#: The NAMES of the secrets that hold the AWS key. The machine is looked up with that key, so the
#: names cannot change while the host has a machine or an unfinished launch.
KEY_NAMES = ("access_key_secret", "secret_key_secret", "session_token_secret")
KEY_NAMES_TEXT = {"access_key_secret": "access key secret name",
                  "secret_key_secret": "secret key secret name",
                  "session_token_secret": "session token secret name"}
_TYPE_RE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z0-9]+$")
_SECRET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _bad(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _int(data: dict, key: str, default: int, lo: int, hi: int, what: str) -> None:
    value = data.get(key, default)
    if value is None:
        value = default
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise _bad(f"{what} must be a whole number from {lo} to {hi}.")
    data[key] = value


def validate_ec2(raw: Any, saved: dict | None = None) -> dict:
    """Check the ``ec2`` object of a host body; keep the plugin-written fields of ``saved``."""
    from .aws import REGION_RE
    from .ec2 import IMAGES, check_cidr
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _bad("ec2 must be an object")
    data = {k: v for k, v in raw.items() if k not in CREATED_FIELDS}
    has_machine = bool(saved and saved.get("instance_id"))
    # An unfinished launch (token stored, no instance id yet) may become a machine at any moment:
    # the key names stay put then too.
    locked_keys = has_machine or bool(saved and saved.get("launch_token"))
    if has_machine:                              # absent launch-only keys keep the saved value
        for key in LAUNCH_ONLY:
            if key not in data and key in saved:
                data[key] = saved[key]
    if locked_keys:
        for key in KEY_NAMES:
            if key not in data and key in saved:
                data[key] = saved[key]
    for key, value in list(data.items()):
        if isinstance(value, str):
            data[key] = value.strip()
        if data[key] == "":
            data[key] = None
    region = data.get("region") or "us-east-1"
    if not REGION_RE.fullmatch(str(region)):
        raise _bad(f"{region!r} is not an AWS region name (for example us-east-1)")
    data["region"] = region
    for key, default in (("access_key_secret", "AWS_ACCESS_KEY_ID"),
                         ("secret_key_secret", "AWS_SECRET_ACCESS_KEY")):
        name = data.get(key) or default
        if not _SECRET_RE.fullmatch(str(name)):
            raise _bad(f"ec2.{key} must be the NAME of a Hermes secret (letters, digits, _), "
                       "never the key itself")
        data[key] = name
    token = data.get("session_token_secret", "AWS_SESSION_TOKEN")
    if token is not None and not _SECRET_RE.fullmatch(str(token)):
        raise _bad("ec2.session_token_secret must be the NAME of a Hermes secret")
    kind = data.get("instance_type") or "t3.micro"
    if not _TYPE_RE.fullmatch(str(kind)):
        raise _bad(f"{kind!r} is not an instance type name (for example t3.micro)")
    data["instance_type"] = kind
    image = data.get("image") or "auto-al2023"
    if image not in IMAGES and not re.fullmatch(r"ami-[0-9a-f]{8,17}", str(image)):
        raise _bad("ec2.image must be auto-al2023, auto-ubuntu or an AMI id (ami-...)")
    data["image"] = image
    if data.get("ssh_user") is not None and not _USER_RE.fullmatch(str(data["ssh_user"])):
        raise _bad("ec2.ssh_user must be a Linux user name such as ec2-user")
    # the labels of the wizard fields (the page shows them next to the allowed range)
    _int(data, "disk_gb", 10, 8, 500, "Root disk (GB)")
    _int(data, "max_lifetime_minutes", 120, 15, 1440, "Max lifetime (minutes)")
    _int(data, "idle_stop_minutes", 30, 0, 1440, "Stop when idle (minutes)")
    if not isinstance(data.get("auto_start", False), bool):
        raise _bad("ec2.auto_start must be true or false")
    for key, pattern in (("subnet_id", r"subnet-[0-9a-f]{8,17}"),
                         ("security_group_id", r"sg-[0-9a-f]{8,17}")):
        if data.get(key) is not None and not re.fullmatch(pattern, str(data[key])):
            raise _bad(f"ec2.{key} {data[key]!r} is not a valid id")
    if data.get("ssh_cidr") is not None:
        data["ssh_cidr"] = check_cidr(str(data["ssh_cidr"]))
    if saved:
        for key in CREATED_FIELDS:
            if key in saved:
                data[key] = saved[key]
        if has_machine and saved.get("region") and data["region"] != saved["region"]:
            raise _bad("The region cannot change while the host has a machine. Terminate the "
                       "machine first.")
        if locked_keys:
            names = [KEY_NAMES_TEXT[k] for k in KEY_NAMES
                     if (data.get(k) or None) != (saved.get(k) or None)]
            if names:
                raise _bad(f"The {', '.join(names)} cannot change while the host has a machine: "
                           "the machine is looked up with those keys, and with other keys it "
                           "could be lost from view while AWS keeps billing for it. Terminate "
                           "the machine first, then change them.")
        if has_machine:
            changed = [LAUNCH_ONLY_NAMES[k] for k in LAUNCH_ONLY
                       if (data.get(k) or None) != (saved.get(k) or None)]
            if changed:
                raise _bad(f"The {', '.join(changed)} cannot change while the host has a "
                           "machine. These settings apply when a machine is created. Terminate "
                           "the machine first, then change them.")
    return data


def _host(req: ApiRequest) -> Any:
    host_id = str((req.path_params or {}).get("host_id", ""))
    host = req.services.store.get_host(host_id)
    if host is None:
        raise ApiError(404, "not_found", f"host {host_id} not found")
    if host.kind != "ec2":
        raise _bad(f"{host.name} is not an EC2 host")
    return host


def _view(services: Any, host: Any) -> dict:
    ec2 = services.ec2
    host = services.store.get_host(host.id) or host
    return {"machine": ec2.machine(host), "untracked": ec2.untracked(host.id)}


def machine_action(services: Any, host: Any, action: str, body: dict | None = None) -> dict:
    """Run one machine action; shared by the route and the chat tool."""
    body = body or {}
    ec2 = services.ec2
    if action == "start":
        ec2.start_machine(host)
    elif action == "stop":
        ec2.stop_machine(host)
    elif action == "extend":
        minutes = body.get("minutes")
        if isinstance(minutes, str) and minutes.strip().isdigit():
            minutes = int(minutes)
        ec2.extend_machine(host, minutes)
    elif action == "terminate":
        delete = body.get("delete_environments", False)
        save = body.get("save", True)
        if not isinstance(delete, bool) or not isinstance(save, bool):
            raise _bad("delete_environments and save must be true or false")
        out = ec2.terminate_machine(host, delete_environments=delete, save=save)
        return {**_view(services, host), **out}
    elif action == "reconcile":
        ec2.reconcile(host)
    else:
        raise _bad(f"action must be one of: {', '.join(MACHINE_ACTIONS)}, reconcile")
    return _view(services, host)


def get_machine(req: ApiRequest) -> dict:
    """Machine state plus the instances found on AWS that no host tracks. The check against
    AWS runs here too (at most every few minutes), so the host card shows them on its own."""
    host = _host(req)
    try:
        req.services.ec2.reconcile(host, force=False)
    except Exception as exc:  # noqa: BLE001 - the stored answer is still shown
        log.info("cloud-compute: EC2 check of %s failed: %s", host.id,
                 getattr(exc, "message", type(exc).__name__))
    return _view(req.services, host)


def post_machine(req: ApiRequest) -> dict:
    action = str((req.path_params or {}).get("action", ""))
    body = req.body if isinstance(req.body, dict) else {}
    return machine_action(req.services, _host(req), action, body)


def machine_confirm(req: ApiRequest) -> dict:
    """What a machine action does and costs, in the words of the chat card (one text for the
    page and the chat). ``blocked`` is true when the action cannot work now; the text says why."""
    host = _host(req)
    action = str((req.path_params or {}).get("action", ""))
    if action not in ("start", "stop", "extend"):
        raise _bad("action must be one of: start, stop, extend")
    args: dict = {}
    if action == "extend":
        raw = (req.query or {}).get("minutes", "60")
        if not str(raw).strip().isdigit():
            raise _bad("minutes must be a whole number from 1 to 1440")
        args["minutes"] = int(str(raw).strip())
    envs = [r.name for r in req.services.store.list_instances(host.id) if r.status != "destroyed"]
    text = describe_machine_action(action, host, args, req.services.ec2._now(), envs)
    return {"action": action, "message": str(text),
            "blocked": type(text).__name__ == "BlockedMessage"}


def post_untracked(req: ApiRequest) -> dict:
    host = _host(req)
    params = req.path_params or {}
    return req.services.ec2.act_untracked(host, str(params.get("instance_id", "")),
                                          str(params.get("action", "")))


def free_tier_types(req: ApiRequest) -> dict:
    from .models import Ec2Settings
    body = req.body if isinstance(req.body, dict) else {}
    data = validate_ec2(body.get("ec2") if isinstance(body.get("ec2"), dict) else body)
    settings = Ec2Settings.from_dict(data)
    types = req.services.ec2.free_tier_types(settings)
    return {"region": settings.region, "types": types,
            "note": ("AWS reports these types as free-tier eligible for this account; check "
                     "your AWS billing page.")}


def routes() -> list[Route]:
    return [
        Route("GET", "/hosts/{host_id}/machine", get_machine, "EC2 machine state"),
        Route("POST", "/hosts/{host_id}/machine/{action}", post_machine,
              "Start, stop, terminate or extend an EC2 host's machine"),
        Route("GET", "/hosts/{host_id}/machine/{action}/confirm", machine_confirm,
              "What starting, stopping or extending an EC2 machine does and costs"),
        Route("POST", "/hosts/{host_id}/untracked/{instance_id}/{action}", post_untracked,
              "Stop or terminate an instance found on AWS that no host tracks"),
        Route("POST", "/ec2/free-tier-types", free_tier_types,
              "Instance types AWS reports as free-tier eligible"),
    ]


def tools() -> list:
    return []


def run_watch(services: Any) -> None:
    """Pick up the start of every EC2 host whose worker is gone (Hermes quit or restarted
    while a machine was starting): resume the wait, or stop an instance past its time limit.
    Runs once when the background loop starts, then every :data:`WATCH_EVERY_S`."""
    hosts = [h for h in services.store.list_hosts("ec2")
             if h.ec2 is not None and h.ec2.instance_id
             and h.ec2.machine_state in ("launching", "starting", "stopping")]
    for host in hosts:
        try:
            services.ec2.watch(host)
        except Exception as exc:  # noqa: BLE001 - one host never blocks the others
            log.info("cloud-compute: EC2 watch of %s failed: %s", host.id,
                     getattr(exc, "message", type(exc).__name__))


WATCH_EVERY_S = 20.0


def background_tasks() -> list:
    return [BackgroundTask("ec2.watch", lambda _services: WATCH_EVERY_S, run_watch)]


def describe_machine_action(action: str, host: Any, args: dict, now: Any = None,
                            envs: list | None = None) -> Any:
    """Approval text for a machine action from chat (see :mod:`.ec2_chat`)."""
    from .ec2_chat import describe_machine
    return describe_machine(action, host, args, now, envs)
