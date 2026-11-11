"""EC2 hosts: start, stop and terminate one AWS instance per host on demand.

An ``ec2`` host is an AWS account (its key is read from the Hermes secret store by NAME), a
region and launch defaults (:class:`~.models.Ec2Settings`). The host's machine is one EC2
instance; once it is ready it behaves like an ordinary SSH host with Docker (or Podman) on it,
and environments run on it as containers through the normal SSH transport. This module does
what is not a shell command: launch, stop, start, terminate, the on-instance timer, the
connection test, the key pair and security group, and reconciliation with AWS.

Spending and safety rules kept here:

* every instance, volume, key pair and security group the plugin creates carries the tag
  ``cloud-compute:install=<this install's id>``; every call that changes an existing resource
  first reads it back and refuses when that tag is missing or different;
* a launch carries a ClientToken stored on the host before the call, and one start runs per
  host at a time, so a retry, a lost answer or two callers never launch a second instance;
* each machine has its own stop timer, a transient systemd timer (``cloud-compute-stop``) that
  powers the machine off: the first-boot script arms it and installs a unit that arms it again
  on every later boot, and the exact time is set over SSH as soon as SSH works after each
  start. (A scheduled ``shutdown -h`` is not used: it refuses SSH logins for its last five
  minutes.) ``InstanceInitiatedShutdownBehavior=stop`` turns the power-off into a stop. While
  Hermes runs, :meth:`Ec2Manager.tick` also enforces the time
  limit and the idle rule in every state that has an instance, resumes a start whose worker
  died, and stops a machine that is in error;
* a terminate deletes the disk, so it saves each environment's files first (the sandbox delete
  flow) unless the user chose otherwise;
* AWS key values never reach a command line, the database, an event, a log line, an error
  text or the instance.

Standard library only.
"""

from __future__ import annotations

import base64
import contextvars
import copy
import dataclasses
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .background import track_worker
from .aws import AwsClient, AwsError, Credentials, REGION_RE, lookup_public_ip
from .interfaces import NO_MACHINE as _NO_MACHINE, ApiError
from .models import (
    Capabilities, Ec2Settings, ExecResult, HostSpec, HostState, InstanceRecord, parse_iso,
    utc_now_iso,
)

__all__ = [
    "TAG_INSTALL", "TAG_INSTANCE", "TAG_HOST", "IMAGES", "BILLING_SENTENCE", "STOPPED_NOTE",
    "Ec2Manager", "provide", "settings_of", "is_arm_type", "user_data", "parse_host_keys",
    "INSTANCE_TYPE_RE", "DEFAULT_SESSION_TOKEN", "NO_MACHINE", "check_cidr", "launched_values",
    "extend_window", "extend_offer", "EXTEND_STEPS", "EXTEND_LIMIT", "FOREIGN_ACCOUNT", "UNKNOWN_ACCOUNT", "boot_timer_note",
]

log = logging.getLogger(__name__)

TAG_INSTALL = "cloud-compute:install"
TAG_INSTANCE = "cloud-compute:instance"
TAG_HOST = "cloud-compute:host"
DEFAULT_SESSION_TOKEN = "AWS_SESSION_TOKEN"
INSTANCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z0-9]+$")
_AMI_RE = re.compile(r"^ami-[0-9a-f]{8,17}$")
_SUBNET_RE = re.compile(r"^subnet-[0-9a-f]{8,17}$")
_SG_RE = re.compile(r"^sg-[0-9a-f]{8,17}$")
_AWS_ID_RE = re.compile(r"^i-[0-9a-f]{8,17}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

#: Automatic images: SSM public parameter per architecture, and the image's login user.
IMAGES: dict[str, dict[str, Any]] = {
    "auto-ubuntu": {
        "title": "Ubuntu 24.04 LTS (latest)",
        "user": "ubuntu",
        "x86_64": "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
        "arm64": "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id",
    },
    "auto-al2023": {
        "title": "Amazon Linux 2023 (latest)",
        "user": "ec2-user",
        "x86_64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
        "arm64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
    },
}

BILLING_SENTENCE = ("AWS bills your account while the instance runs; a stopped instance still "
                    "bills a small amount for its disk until it is deleted.")
EXTEND_LIMIT = ("A machine runs at most twice its max lifetime ({lifetime}) after its last start, "
                "so it cannot be extended beyond {latest}. A longer max lifetime can be set "
                "on the Compute page.")
STOPPED_NOTE = ("Stopped: the disk is kept (AWS bills a small amount for it); starting it again "
                "gives it a new public address.")
FOREIGN_ACCOUNT = ("The AWS keys now belong to a different account than the one this machine was "
                   "started in. The machine may still be running and billed in the first "
                   "account. Restore the keys, or stop it in the AWS console.")
UNKNOWN_ACCOUNT = ("Own Compute could not check which AWS account the keys belong to, so it "
                   "did not forget the machine. Press Check AWS again in a minute.")
_RUNNING_STATES = ("pending", "running")
_LIVE_STATES = ("pending", "running", "stopping", "stopped", "shutting-down")
_RECONCILE_EVERY_S = 300.0
_STALE_STOPPED_S = 60.0          # how long a "stopped" view after StartInstances may be stale
_UNTRACKED_KEY = "ec2_untracked:"
_IDLE_KEY = "ec2_idle_since:"
#: The address of an EC2 host that has no running machine (never resolvable; never dialled,
#: because the prober and the sandbox check the machine state first).
NO_MACHINE = _NO_MACHINE


# --------------------------------------------------------------------------- pure helpers


class _Superseded(Exception):
    """A start worker found the machine stopped, terminated or restarted by someone else."""


def settings_of(host: HostSpec) -> Ec2Settings:
    if host.kind != "ec2":
        raise ApiError(400, "invalid", f"host {host.id} is not an EC2 host")
    return host.ec2 if host.ec2 is not None else Ec2Settings()


def extend_window(s: Ec2Settings, minutes: int, now: datetime) -> tuple[datetime, datetime, datetime]:
    """``(stops now at, would stop at, latest allowed)`` for an Extend by ``minutes``.

    The most a machine may run is twice its max lifetime counted from its last start: the time
    limit it started with plus at most one more. More needs a longer max lifetime, which is set
    on the Compute page."""
    current = max(parse_iso(s.auto_stop_at) or now, now)
    started = parse_iso(s.started_at) or parse_iso(s.launched_at) or now
    return (current, current + timedelta(minutes=minutes),
            started + timedelta(minutes=2 * int(s.max_lifetime_minutes)))


#: The Extend steps the page may offer, largest first. The smallest is the least that is worth
#: offering: with less room than that, Extend is not offered at all.
EXTEND_STEPS = (60, 30, 15)


def extend_offer(s: Ec2Settings, now: datetime) -> tuple[int, int]:
    """``(room, step)`` in minutes for the page: how much longer the machine may still run
    under the ceiling of :func:`extend_window`, and the largest of :data:`EXTEND_STEPS` that
    fits in it (0 when none fits or the machine is not running). One rule for the page, the
    card and the backend."""
    if s.machine_state != "ready":
        return 0, 0
    current, _new, latest = extend_window(s, 0, now)
    room = max(0, int((latest - current).total_seconds() // 60))
    return room, next((m for m in EXTEND_STEPS if m <= room), 0)


def launched_values(s: Ec2Settings) -> tuple[str, int]:
    """``(instance type, disk GB)`` of the machine a Start acts on: the values the existing
    instance was launched with, else the settings a new launch would use."""
    if s.instance_id and s.launched_type:
        return s.launched_type, int(s.launched_disk_gb or s.disk_gb)
    return s.instance_type, s.disk_gb


def boot_timer_note(s: Ec2Settings) -> str:
    """The sentence for the window before Hermes reaches a (re)started machine, when the
    lifetime last written to the machine's boot timer differs from the setting; else ``""``."""
    old = s.unit_lifetime_minutes
    if not s.instance_id or not old or int(old) == int(s.max_lifetime_minutes):
        return ""
    return ("If Hermes closes before it reaches the machine, the machine stops after "
            f"{_duration_text(int(old))} instead.")


def _duration_text(m: int) -> str:
    if m > 0 and m % 60 == 0:
        return f"{m // 60} hour" + ("" if m == 60 else "s")
    return f"{m} minute" + ("" if m == 1 else "s")


def is_arm_type(instance_type: str) -> bool:
    """True for Graviton (arm64) types such as t4g.small, m7g.large, c6gn.xlarge."""
    family = str(instance_type or "").split(".", 1)[0]
    m = re.fullmatch(r"([a-z]+)(\d+)([a-z-]*)", family)
    return bool(m and "g" in m.group(3))


#: The machine's own stop: a transient systemd timer, ``cloud-compute-stop.timer``, that powers
#: the machine off after ``{minutes}`` minutes (``InstanceInitiatedShutdownBehavior=stop`` turns
#: that into a stop). Unlike a scheduled ``shutdown -h +N`` it does not create /run/nologin five
#: minutes before, so SSH keeps working until the machine stops. A timer of the same name is
#: replaced. Only a whole number of minutes is interpolated.
_ARM = ("systemctl stop cloud-compute-stop.timer >/dev/null 2>&1; "
        "systemd-run --unit=cloud-compute-stop --on-active={minutes}m "
        "--timer-property=AccuracySec=1s systemctl poweroff")


def user_data(lifetime_minutes: int, user: str = "ec2-user") -> str:
    """The first-boot script (run once as root by EC2): arm the stop at the time limit,
    so the machine stops even if the computer that started it is off, then install a container
    runtime for the login user (Docker on Amazon Linux 2023, Podman on Ubuntu)."""
    minutes = max(1, int(lifetime_minutes))
    if not _USER_RE.fullmatch(user or ""):
        raise ApiError(400, "invalid", f"invalid SSH user {user!r}")
    arm = _ARM.format(minutes=minutes)
    return ("#!/bin/sh\n"
            "# cloud-compute: time limit set when the machine was launched (a systemd timer,\n"
            "# not a scheduled shutdown: that would refuse SSH logins for the last five minutes)\n"
            f"({arm}) >/dev/null 2>&1 || "
            f"(sleep {minutes * 60} && poweroff) &\n"
            "# cloud-compute: the same limit on every later boot, so a machine that is started\n"
            "# again has one even before this computer can reach it (reset once SSH works)\n"
            "tee /etc/systemd/system/cloud-compute-limit.service >/dev/null <<'UNIT'\n"
            "[Unit]\nDescription=cloud-compute time limit\n"
            "[Service]\nType=oneshot\n"
            f"ExecStart=/bin/sh -c \"{arm}\"\n"
            "[Install]\nWantedBy=multi-user.target\nUNIT\n"
            "systemctl daemon-reload >/dev/null 2>&1 && "
            "systemctl enable cloud-compute-limit.service >/dev/null 2>&1 || :\n"
            "# cloud-compute: container runtime for the environments\n"
            "if command -v dnf >/dev/null 2>&1; then\n"
            "  dnf install -y docker && systemctl enable --now docker && "
            f"usermod -aG docker {user}\n"
            "elif command -v apt-get >/dev/null 2>&1; then\n"
            "  apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y podman\n"
            "fi\n")


def parse_host_keys(console: str) -> list[tuple[str, str]]:
    """``(type, base64)`` host keys from the block cloud-init prints to the console."""
    m = re.search(r"-----BEGIN SSH HOST KEY KEYS-----(.*?)-----END SSH HOST KEY KEYS-----",
                  console or "", re.S)
    if not m:
        return []
    keys = []
    for line in m.group(1).splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and re.fullmatch(r"(ssh-ed25519|ecdsa-sha2-nistp\d+|ssh-rsa)",
                                             parts[0]) and re.fullmatch(r"[A-Za-z0-9+/=]+",
                                                                        parts[1]):
            keys.append((parts[0], parts[1]))
    return keys


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _tags(item: dict) -> dict[str, str]:
    out = {}
    for t in _list(item.get("tagSet")):
        if isinstance(t, dict) and t.get("key"):
            out[str(t["key"])] = str(t.get("value") or "")
    return out


def _instances(resp: dict) -> list[dict]:
    out = []
    for res in _list(resp.get("reservationSet")):
        out.extend(i for i in _list(res.get("instancesSet")) if isinstance(i, dict))
    return out


def _tag_spec(index: int, resource: str, tags: dict[str, str]) -> dict:
    return {f"TagSpecification.{index}.ResourceType": resource,
            **{f"TagSpecification.{index}.Tag.{n}.{part}": value
               for n, (k, v) in enumerate(tags.items(), 1)
               for part, value in (("Key", k), ("Value", v))}}


def _minutes_text(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def _short(text: str, limit: int = 300) -> str:
    return (text or "").strip()[-limit:]


# --------------------------------------------------------------------------- manager


class Ec2Manager:
    """Everything the plugin does with AWS for ``ec2`` hosts (see the module docstring).

    ``http`` replaces the network for every AWS call and the address lookup (tests);
    ``keygen(path, comment)`` replaces ``ssh-keygen``; ``sleep`` and ``clock`` drive the waits.
    ``background=False`` runs a launch's wait in the calling thread (tests).
    """

    def __init__(self, services: Any, *, http: Callable[..., Any] | None = None,
                 keygen: Callable[[Path, str], None] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], datetime] | None = None, background: bool = True,
                 wait_running_s: float = 300.0, wait_status_s: float = 420.0,
                 wait_ssh_s: float = 240.0, poll_s: float = 5.0):
        self.services = services
        self._http = http
        self._keygen = keygen or _ssh_keygen
        self._sleep = sleep or time.sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.background = background
        self.wait_running_s, self.wait_status_s, self.wait_ssh_s = (
            wait_running_s, wait_status_s, wait_ssh_s)
        self.poll_s = poll_s
        self._lock = threading.RLock()
        self._last_reconcile: dict[str, float] = {}
        # hosts whose start has a live worker in THIS process (host id -> monotonic start)
        self._workers: dict[str, float] = {}
        # hosts whose stop is being waited for by a worker of THIS process
        self._stoppers: dict[str, float] = {}
        self._unit_tried: dict[str, float] = {}
        self._owner = f"ec2-{os.getpid()}-{uuid.uuid4().hex[:8]}"     # this process's lease owner

    # ------------------------------------------------------------------ basics

    @property
    def store(self) -> Any:
        return self.services.store

    def install_id(self) -> str:
        from .sandbox import get_install_id
        return get_install_id(self.store)

    def _now(self) -> datetime:
        now = self._clock()
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    def credentials(self, s: Ec2Settings) -> Credentials | None:
        """The AWS key from the Hermes secrets named on the host (read now, never kept)."""
        secrets = self.services.secrets
        ak = secrets.get(s.access_key_secret) if _SECRET_NAME_RE.fullmatch(
            s.access_key_secret or "") else None
        sk = secrets.get(s.secret_key_secret) if _SECRET_NAME_RE.fullmatch(
            s.secret_key_secret or "") else None
        token = None
        if s.session_token_secret and _SECRET_NAME_RE.fullmatch(s.session_token_secret):
            token = secrets.get(s.session_token_secret)
        if not ak or not sk:
            return None
        return Credentials(ak, sk, token)

    def client(self, s: Ec2Settings) -> AwsClient:
        return AwsClient(s.region, lambda: self.credentials(s), http=self._http,
                         clock=self._clock, sleep=self._sleep)

    def _api(self, exc: AwsError, status: int | None = None) -> ApiError:
        """An AwsError as the ApiError the routes and tools report (text is already plain)."""
        auth = ("no_credentials", "AuthFailure", "InvalidClientTokenId", "SignatureDoesNotMatch",
                "ExpiredToken", "ExpiredTokenException", "UnauthorizedOperation", "AccessDenied",
                "AccessDeniedException", "RequestExpired")
        limits = ("InstanceLimitExceeded", "VcpuLimitExceeded", "InsufficientInstanceCapacity",
                  "InsufficientCapacity", "ResourceLimitExceeded")
        if exc.code in auth:
            code, http_status = "auth_required", 401 if exc.code == "no_credentials" else 403
        elif exc.code in limits:
            code, http_status = "limit_reached", 409
        elif exc.code == "host_offline":
            code, http_status = "host_offline", 502
        elif exc.code.startswith("Invalid") or exc.code in ("invalid_region", "Unsupported",
                                                            "OptInRequired"):
            code, http_status = "invalid", 400
        else:
            code, http_status = "unavailable", 502
        return ApiError(status or http_status, code, exc.plain)

    def _call(self, s: Ec2Settings, action: str, params: dict | None = None) -> dict:
        try:
            return self.client(s).ec2(action, params)
        except AwsError as exc:
            raise self._api(exc) from None

    def _data_dir(self, host_id: str) -> Path:
        path = getattr(self.store, "path", None)
        if path:
            base = Path(path).parent
        else:
            try:
                from plugins.plugin_storage import plugin_data_dir  # type: ignore[import-not-found]
                base = Path(plugin_data_dir("cloud-compute"))
            except Exception:  # noqa: BLE001
                raise ApiError(503, "unavailable", "plugin storage not available") from None
        d = base / "ec2" / host_id
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d

    # ------------------------------------------------------------------ images

    def _image(self, client: AwsClient, s: Ec2Settings, instance_type: str) -> dict:
        """``{image_id, user, root_device, arch}`` for the host's image choice."""
        arch = "arm64" if is_arm_type(instance_type) else "x86_64"
        auto = IMAGES.get(s.image)
        if auto is not None:
            image_id = client.ssm_parameter(auto[arch])
            user = s.ssh_user or auto["user"]
        elif _AMI_RE.fullmatch(s.image or ""):
            image_id, user = s.image, s.ssh_user or "ec2-user"
        else:
            raise ApiError(400, "invalid", "image must be auto-ubuntu, auto-al2023 or an AMI id "
                           "such as ami-0123456789abcdef0")
        found = _list(client.ec2("DescribeImages", {"ImageId": [image_id]}).get("imagesSet"))
        if not found:
            raise AwsError("InvalidAMIID.NotFound", "The machine image (AMI) was not found in "
                           f"{s.region}. Pick the automatic image or an AMI id from this region.")
        img = found[0]
        img_arch = str(img.get("architecture") or arch)
        if img_arch != arch:
            raise AwsError("Unsupported", f"The image is built for {img_arch}, but {instance_type} "
                           f"needs an {arch} image. Pick another image or instance type.")
        return {"image_id": image_id, "user": user, "arch": arch,
                "root_device": str(img.get("rootDeviceName") or "/dev/sda1")}

    # ------------------------------------------------------------------ connection test

    def test(self, spec: HostSpec) -> dict:
        """The connection test of an EC2 host: read-only calls plus a DryRun launch.

        Returns ``{ok, status, steps: [{name, label, ok, detail, hint}]}``. Launches nothing
        and saves nothing. Each row says what failed and how to fix it.
        """
        s = settings_of(spec)
        steps: list[dict] = []

        def row(name: str, label: str, ok: bool | None, detail: str, hint: str = "") -> None:
            steps.append({"name": name, "label": label, "ok": ok, "detail": detail,
                          "hint": hint or None})

        def done() -> dict:
            ok = all(st["ok"] is not False for st in steps)
            status = "online" if ok else ("auth_required" if any(
                st["name"] in ("credentials", "identity") and st["ok"] is False for st in steps)
                else "degraded")
            return {"ok": ok, "status": status, "steps": steps}

        if not REGION_RE.fullmatch(s.region or ""):
            row("region", "Region", False, f"{s.region!r} is not an AWS region name.",
                "Use a name such as us-east-1 or eu-central-1.")
            return done()
        creds = self.credentials(s)
        if creds is None:
            row("credentials", "AWS key stored", False,
                f"The Hermes secrets {s.access_key_secret} and {s.secret_key_secret} are not "
                "both set.",
                "Add them to this Hermes profile's secrets (never in chat), then test again.")
            return done()
        row("credentials", "AWS key stored", True,
            f"{s.access_key_secret} and {s.secret_key_secret} are set"
            + (f"; {s.session_token_secret} is used too" if creds.token else "") + ".")
        client = self.client(s)
        try:
            who = client.sts("GetCallerIdentity")
            account = str(who.get("Account") or "")
            row("identity", "Key accepted", True,
                f"AWS account {account[:4]}…{account[-2:]}" if account else "Key accepted.")
        except AwsError as exc:
            row("identity", "Key accepted", False, exc.plain,
                "Check the two key secrets, or store a fresh session token.")
            return done()
        try:
            regions = _list(client.ec2("DescribeRegions", {"RegionName": [s.region]})
                            .get("regionInfo"))
            if not regions:
                raise AwsError("OptInRequired", f"Region {s.region} is not enabled for this "
                               "account.")
            row("region", "Region", True, f"{s.region} is available.")
        except AwsError as exc:
            row("region", "Region", False, exc.plain, "Choose a region enabled for the account.")
            return done()
        image: dict | None = None
        try:
            image = self._image(client, s, s.instance_type)
            row("image", "Machine image", True,
                f"{image['image_id']} ({IMAGES.get(s.image, {}).get('title', 'custom AMI')}, "
                f"{image['arch']}), login user {image['user']}.")
        except (AwsError, ApiError) as exc:
            row("image", "Machine image", False, exc.plain if isinstance(exc, AwsError) else exc.message,
                "Pick the automatic image, or an AMI id that exists in this region.")
        if image is not None:
            params = self._run_params(s, image, 5, spec.id, "dry-run", include_access=False)
            params["DryRun"] = True
            try:
                res = client.ec2("RunInstances", params)
                if res.get("DryRunOperation"):
                    row("launch", "Allowed to launch", True,
                        f"A {s.instance_type} launch would be accepted (checked with DryRun; "
                        "nothing was started).")
                else:      # pragma: no cover - AWS never launches on DryRun
                    row("launch", "Allowed to launch", None, "Unexpected answer to DryRun.")
            except AwsError as exc:
                row("launch", "Allowed to launch", False, exc.plain,
                    "Give the key permission to launch with the cloud-compute tags (see Help, "
                    "EC2), or pick another instance type.")
        if s.ssh_cidr:
            row("address", "Who may use SSH", True, f"{s.ssh_cidr} (typed on the host).")
        elif s.security_group_id:
            row("address", "Who may use SSH", True,
                f"Your own security group {s.security_group_id} decides.")
        else:
            try:
                ip = lookup_public_ip(self._http)
                row("address", "Who may use SSH", True,
                    f"Only this computer's public address {ip} (looked up at "
                    "checkip.amazonaws.com).")
            except AwsError as exc:
                row("address", "Who may use SSH", False, exc.plain,
                    "Type the address or range that may use SSH on the host.")
        return done()

    # ------------------------------------------------------------------ host resources

    def _cidr(self, s: Ec2Settings) -> str:
        if s.ssh_cidr:
            return check_cidr(s.ssh_cidr)
        try:
            return f"{lookup_public_ip(self._http)}/32"
        except AwsError as exc:
            raise ApiError(502, "host_offline", exc.plain) from None

    def _owned_tags(self) -> dict[str, str]:
        return {TAG_INSTALL: self.install_id()}

    def prepare_host(self, host: HostSpec) -> HostSpec:
        """Create what launches need and save it on the host: a key pair (generated here,
        private half never leaves this computer) and, unless the user gave one, a security
        group that allows SSH only from this computer's address. Idempotent."""
        s = copy.deepcopy(settings_of(host))
        d = self._data_dir(host.id)
        key_file = d / "id_ed25519"
        if not key_file.is_file() or not (d / "id_ed25519.pub").is_file():
            for stale in (key_file, d / "id_ed25519.pub"):
                try:
                    stale.unlink()
                except OSError:
                    pass
            self._keygen(key_file, f"cloud-compute-{host.id}")
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        known = d / "known_hosts"
        if not known.exists():
            known.touch()
            try:
                os.chmod(known, 0o600)
            except OSError:
                pass
        s.key_file, s.known_hosts_file = str(key_file), str(known)
        tags = {**self._owned_tags(), TAG_HOST: host.id}
        if not s.key_name:
            name = f"cloud-compute-{self.install_id()}-{host.id}"[:255]
            public = (d / "id_ed25519.pub").read_text(encoding="ascii").strip()
            try:
                self.client(s).ec2("ImportKeyPair", {
                    "KeyName": name,
                    "PublicKeyMaterial": base64.b64encode(public.encode("ascii")).decode(),
                    **_tag_spec(1, "key-pair", {**tags, "Name": name})})
            except AwsError as exc:
                if exc.code != "InvalidKeyPair.Duplicate":
                    raise self._api(exc) from None
                self._check_key_pair(s, name)       # ours from an earlier try: reuse it
            s.key_name = name
        if not s.security_group_id and not s.managed_group_id:
            s.managed_group_id = self._create_group(s, host.id, tags)
        if s.managed_group_id and not s.security_group_id:
            self._ensure_ssh_rule(s)
        if not s.ssh_user:
            s.ssh_user = IMAGES.get(s.image, {}).get("user") or "ec2-user"
        saved = dataclasses.replace(host, ec2=s)
        return self.store.upsert_host(saved) or saved

    def _check_key_pair(self, s: Ec2Settings, name: str) -> None:
        found = _list(self._call(s, "DescribeKeyPairs", {"KeyName": [name]}).get("keySet"))
        if not found or _tags(found[0]).get(TAG_INSTALL) != self.install_id():
            raise ApiError(409, "conflict", f"A key pair named {name} exists in AWS but was not "
                           "created by this installation; it was not used. Remove it in the "
                           "AWS console or rename the host.")

    def _create_group(self, s: Ec2Settings, host_id: str, tags: dict[str, str]) -> str:
        if s.subnet_id:
            subnets = _list(self._call(s, "DescribeSubnets", {"SubnetId": [s.subnet_id]})
                            .get("subnetSet"))
            vpc = str(subnets[0].get("vpcId") or "") if subnets else ""
        else:
            vpcs = _list(self._call(s, "DescribeVpcs", {
                "Filter": [{"Name": "isDefault", "Value": ["true"]}]}).get("vpcSet"))
            vpc = str(vpcs[0].get("vpcId") or "") if vpcs else ""
        if not vpc:
            raise ApiError(400, "invalid", "This region has no default network (VPC). Give a "
                           "subnet id and a security group id on the host, or create a default "
                           "VPC in the AWS console.")
        name = f"cloud-compute-{self.install_id()}-{host_id}"
        resp = self._call(s, "CreateSecurityGroup", {
            "GroupName": name, "VpcId": vpc,
            "GroupDescription": "Own Compute: SSH from the computer that runs Hermes",
            **_tag_spec(1, "security-group", {**tags, "Name": name})})
        group = str(resp.get("groupId") or "")
        if not _SG_RE.fullmatch(group):
            raise ApiError(502, "unavailable", "AWS did not return a security group id.")
        return group

    def _group(self, s: Ec2Settings) -> dict:
        found = _list(self._call(s, "DescribeSecurityGroups", {
            "GroupId": [s.managed_group_id]}).get("securityGroupInfo"))
        if not found or _tags(found[0]).get(TAG_INSTALL) != self.install_id():
            raise ApiError(409, "conflict", f"Security group {s.managed_group_id} does not carry "
                           "this installation's tag, so Own Compute did not change it.")
        return found[0]

    def _ensure_ssh_rule(self, s: Ec2Settings) -> bool:
        """Allow TCP 22 from the current address on the managed group; drop the old one.
        Returns True when the rule changed."""
        cidr = self._cidr(s)
        if s.ssh_cidr_used == cidr:
            return False
        group = self._group(s)
        present = set()
        for perm in _list(group.get("ipPermissions")):
            if str(perm.get("fromPort")) == "22":
                present.update(str(r.get("cidrIp")) for r in _list(perm.get("ipRanges")))
        if cidr not in present:
            self._call(s, "AuthorizeSecurityGroupIngress", {
                "GroupId": s.managed_group_id,
                "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                                   "IpRanges": [{"CidrIp": cidr,
                                                 "Description": "cloud-compute ssh"}]}]})
        stale = [c for c in present if c != cidr]
        if s.ssh_cidr_used in stale:
            try:
                self._call(s, "RevokeSecurityGroupIngress", {
                    "GroupId": s.managed_group_id,
                    "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                                       "IpRanges": [{"CidrIp": s.ssh_cidr_used}]}]})
            except ApiError:
                log.info("cloud-compute: could not remove the old SSH rule of %s",
                         s.managed_group_id)
        s.ssh_cidr_used = cidr
        return True

    def refresh_ssh_rule(self, host: HostSpec) -> HostSpec:
        """Re-check this computer's address (it may have changed) and update the SSH rule."""
        s = copy.deepcopy(settings_of(host))
        if not s.managed_group_id or s.security_group_id:
            return host
        if self._ensure_ssh_rule(s):
            host = dataclasses.replace(host, ec2=s)
            self.store.upsert_host(host)
        return host

    def remove_host(self, host: HostSpec) -> list[str]:
        """Delete the key pair and security group this install created for ``host`` and its
        private files. Refuses while any environment of the host still exists. Returns notes."""
        live = [r for r in self.store.list_instances(host.id) if r.status != "destroyed"]
        if live:
            raise ApiError(409, "conflict", f"{host.name} still has {len(live)} environment(s); "
                           "delete them first (their files are saved).")
        s = settings_of(host)
        notes: list[str] = []
        if self.credentials(s) is None:
            if s.key_name or s.managed_group_id:
                notes.append("No AWS key is stored, so the key pair and security group were left "
                             "in AWS; remove them in the AWS console.")
        else:
            if s.key_name:
                try:
                    self._check_key_pair(s, s.key_name)
                    self._call(s, "DeleteKeyPair", {"KeyName": s.key_name})
                    notes.append(f"Removed the key pair {s.key_name}.")
                except ApiError as exc:
                    notes.append(f"The key pair {s.key_name} was not removed: {exc.message}")
            if s.managed_group_id and not s.security_group_id:
                try:
                    self._group(s)
                    self._call(s, "DeleteSecurityGroup", {"GroupId": s.managed_group_id})
                    notes.append(f"Removed the security group {s.managed_group_id}.")
                except ApiError as exc:
                    notes.append(f"The security group {s.managed_group_id} was not removed "
                                 f"(an instance may still use it): {exc.message}")
        try:
            d = self._data_dir(host.id)
            shutil.rmtree(d, ignore_errors=True)
        except ApiError:
            pass
        return notes

    # ------------------------------------------------------------------ instance lookups

    def describe(self, host: HostSpec, aws_id: str) -> dict | None:
        s = settings_of(host)
        found = _instances(self._call(s, "DescribeInstances", {"InstanceId": [aws_id]}))
        return found[0] if found else None

    def _owned(self, host: HostSpec, aws_id: str | None) -> dict:
        """The instance, read back; ``{}`` when it is gone. Refuses (changes nothing) unless
        it carries this install's tag."""
        if not _AWS_ID_RE.fullmatch(str(aws_id or "")):
            raise ApiError(400, "invalid", f"{aws_id!r} is not an EC2 instance id")
        try:
            item = self.describe(host, str(aws_id))
        except ApiError as exc:
            if "no longer exists" in exc.message:
                return {}
            raise
        if item is None or str((item.get("instanceState") or {}).get("name")) == "terminated":
            return {}
        if _tags(item).get(TAG_INSTALL) != self.install_id():
            raise ApiError(409, "conflict", f"Instance {aws_id} does not carry this "
                           "installation's tag, so Own Compute did not change it.")
        return item

    def pin_host_key(self, host: HostSpec, aws_id: str) -> bool:
        """Store the instance's SSH host keys from its console output under its id; True if
        any. Without console output the first SSH login records the key, for this instance
        only, in the plugin's own known-hosts file."""
        s = settings_of(host)
        if not s.known_hosts_file:
            return False
        try:
            raw = str(self._call(s, "GetConsoleOutput", {"InstanceId": aws_id,
                                                         "Latest": True}).get("output") or "")
            console = base64.b64decode(raw).decode("utf-8", "replace") if raw else ""
        except (ApiError, ValueError):
            return False
        keys = parse_host_keys(console)
        if not keys:
            return False
        path = Path(s.known_hosts_file)
        with self._lock:
            try:
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                         if ln.split(" ", 1)[0] != aws_id]
            except OSError:
                lines = []
            lines += [f"{aws_id} {kind} {blob}" for kind, blob in keys]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    def forget_host_key(self, host: HostSpec, aws_id: str) -> None:
        s = settings_of(host)
        if not s.known_hosts_file:
            return
        path = Path(s.known_hosts_file)
        with self._lock:
            try:
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                         if ln.split(" ", 1)[0] != aws_id]
                path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            except OSError:
                pass

    # ------------------------------------------------------------------ launch params

    def _run_params(self, s: Ec2Settings, image: dict, lifetime: int, host_id: str,
                    name: str, *, include_access: bool = True) -> dict:
        tags = {**self._owned_tags(), TAG_HOST: host_id, "Name": f"cloud-compute {name}"[:255]}
        params: dict[str, Any] = {
            "ImageId": image["image_id"], "InstanceType": s.instance_type,
            "MinCount": 1, "MaxCount": 1,
            "InstanceInitiatedShutdownBehavior": "stop",
            "UserData": base64.b64encode(user_data(lifetime, image["user"]).encode("utf-8")
                                         ).decode("ascii"),
            "BlockDeviceMapping": [{"DeviceName": image["root_device"],
                                    "Ebs": {"VolumeSize": int(s.disk_gb), "VolumeType": "gp3",
                                            "DeleteOnTermination": True}}],
            "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"},
            **_tag_spec(1, "instance", tags), **_tag_spec(2, "volume", tags),
        }
        groups = [g for g in (s.security_group_id or s.managed_group_id,) if g]
        if include_access and s.key_name:
            params["KeyName"] = s.key_name
        if s.subnet_id:
            params["NetworkInterface"] = [{"DeviceIndex": 0, "SubnetId": s.subnet_id,
                                           "AssociatePublicIpAddress": True,
                                           **({"SecurityGroupId": groups}
                                              if include_access and groups else {})}]
        elif include_access and groups:
            params["SecurityGroupId"] = groups
        return params

    def free_tier_types(self, s: Ec2Settings) -> list[dict]:
        """Instance types AWS reports as free-tier eligible for this account and region
        (``DescribeInstanceTypes`` with the ``free-tier-eligible`` filter). Never a claim of
        the plugin's own."""
        items: list = []
        token = None
        for _page in range(60):      # AWS filters per page: most pages are empty + nextToken
            params: dict[str, Any] = {"Filter": [{"Name": "free-tier-eligible",
                                                  "Value": ["true"]}], "MaxResults": 100}
            if token:
                params["NextToken"] = token
            resp = self._call(s, "DescribeInstanceTypes", params)
            items += _list(resp.get("instanceTypeSet"))
            token = resp.get("nextToken")
            if not token:
                break
        out = []
        for item in items:
            if not isinstance(item, dict) or not item.get("instanceType"):
                continue
            mem = (item.get("memoryInfo") or {}).get("sizeInMiB")
            cpu = (item.get("vCpuInfo") or {}).get("defaultVCpus")
            out.append({"instance_type": str(item["instanceType"]),
                        "memory_mib": int(mem) if str(mem or "").isdigit() else None,
                        "vcpus": int(cpu) if str(cpu or "").isdigit() else None})
        return sorted(out, key=lambda t: ((t["memory_mib"] or 0), t["instance_type"]))

    # ------------------------------------------------------------------ machine state

    def _fresh(self, host: HostSpec | str) -> HostSpec:
        host_id = host if isinstance(host, str) else host.id
        found = self.store.get_host(host_id)
        if found is None:
            raise ApiError(404, "not_found", f"host {host_id} not found")
        settings_of(found)
        return found

    def _set(self, host_id: str, *, address: str | None = None, **changes: Any) -> HostSpec:
        """Re-read the host, apply ``changes`` to its EC2 settings and save it."""
        with self._lock:
            host = self._fresh(host_id)
            s = copy.deepcopy(settings_of(host))
            for key, value in changes.items():
                setattr(s, key, value)
            host = dataclasses.replace(host, ec2=s,
                                       address=address if address is not None else host.address)
            host = self.store.upsert_host(host) or host
        try:
            self.services.events.emit("hosts.changed", {"ids": [host_id], "reason": "machine"})
        except Exception:  # noqa: BLE001
            pass
        return host

    def machine(self, host: HostSpec) -> dict:
        """What the host card shows about the machine (no secrets, no key material)."""
        s = settings_of(host)
        running = s.machine_state in ("launching", "starting", "ready")
        kind, disk = launched_values(s)
        return {"state": s.machine_state, "detail": s.machine_detail, "instance_id": s.instance_id,
                "instance_type": kind, "region": s.region, "disk_gb": disk,
                "public_ip": host.address if s.machine_state == "ready" else None,
                "launched_at": s.launched_at, "started_at": s.started_at,
                "auto_stop_at": s.auto_stop_at, "max_lifetime_minutes": s.max_lifetime_minutes,
                "idle_stop_minutes": s.idle_stop_minutes, "auto_start": s.auto_start,
                "unit_lifetime_minutes": s.unit_lifetime_minutes,
                "boot_timer_note": boot_timer_note(s),
                "billing": BILLING_SENTENCE if running else (STOPPED_NOTE if s.machine_state ==
                                                             "stopped" else "")}

    def is_ready(self, host: HostSpec) -> bool:
        return host.kind == "ec2" and settings_of(host).machine_state == "ready"

    # ------------------------------------------------------------------ start

    def _claim(self, host_id: str) -> bool:
        """Mark ``host_id`` as having a live start worker; False when one already has."""
        with self._lock:
            if self._worker_alive(host_id):
                return False
            self._workers[host_id] = time.monotonic()
            return True

    def _release(self, host_id: str) -> None:
        with self._lock:
            self._workers.pop(host_id, None)

    def _worker_alive(self, host_id: str) -> bool:
        """True while a start worker of this process works on ``host_id``. A claim older than
        every wait together is treated as dead (a thread that could not even start)."""
        since = self._workers.get(host_id)
        if since is None:
            return False
        limit = self.wait_running_s + self.wait_status_s + self.wait_ssh_s + 120.0
        return time.monotonic() - since <= limit

    def start_machine(self, host: HostSpec, *, wait: bool = False) -> HostSpec:
        """Launch the host's instance (first time) or start it again (after a stop).

        Returns at once with state ``launching`` / ``starting``; the rest (running, AWS
        status checks, SSH, the container runtime, the stop timer) happens in the background
        and ends in ``ready`` or ``error``. ``wait`` finishes in this thread instead.

        One start per host at a time: a second caller (a parallel ``env_spawn``, a double
        click, chat and the page together) does not launch again. It gets the first start's
        host (after waiting for it when ``wait``).
        """
        host = self._fresh(host)
        if not self._claim(host.id):
            return self._after_other_start(host, wait)
        try:
            host = self._fresh(host.id)           # the state may have changed before the claim
            s = settings_of(host)
            if s.machine_state in ("launching", "starting", "ready"):
                self._release(host.id)
                return host
            if s.machine_state in ("stopping", "terminating"):
                raise ApiError(409, "conflict", f"The machine of {host.name} is "
                               f"{s.machine_state}. Try again in a minute.")
            if not self._take_lease(host.id):        # another Hermes process is starting it
                raise ApiError(409, "conflict", f"The machine of {host.name} is already being "
                               "started by another Hermes process. Check again in a minute.")
            now = self._now()
            stop_at = (now + timedelta(minutes=s.max_lifetime_minutes)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            return self._start_claimed(host, s, now, stop_at, wait)
        except BaseException:
            self._release(host.id)
            self._drop_lease(host.id)
            raise

    #: How long a "start in progress" claim in the shared database lasts if its process dies.
    LEASE_S = 180.0

    def _lease_name(self, host_id: str) -> str:
        return f"ec2-start:{host_id}"

    def _take_lease(self, host_id: str) -> bool:
        """Claim "a start of this host is in progress" in the plugin's database, which every
        Hermes process of this user shares. False when another process holds it. The claim
        expires on its own (:data:`LEASE_S`), so a process that dies never blocks the host."""
        acquire = getattr(self.store, "acquire_lease", None)
        if acquire is None:
            return True
        try:
            return bool(acquire(self._lease_name(host_id), self._owner, self.LEASE_S))
        except Exception:  # noqa: BLE001 - an unreadable lease must not block every start
            return True

    def _drop_lease(self, host_id: str) -> None:
        release = getattr(self.store, "release_lease", None)
        if release is not None:
            try:
                release(self._lease_name(host_id), self._owner)
            except Exception:  # noqa: BLE001
                pass

    def launch_in_flight(self, host_id: str) -> bool:
        """True while a start of ``host_id`` is being made by this or another Hermes process
        (it has not recorded its instance yet)."""
        if self._worker_alive(host_id):
            return True
        acquire = getattr(self.store, "acquire_lease", None)
        if acquire is None:
            return False
        try:
            probe = f"probe-{uuid.uuid4().hex[:8]}"
            if acquire(self._lease_name(host_id), probe, 1.0):
                self.store.release_lease(self._lease_name(host_id), probe)
                return False
            return True
        except Exception:  # noqa: BLE001
            return False

    def _after_other_start(self, host: HostSpec, wait: bool) -> HostSpec:
        """Another start of this host is running: report it, or wait for it to finish."""
        if wait:
            limit = time.monotonic() + self.wait_running_s + self.wait_status_s + self.wait_ssh_s
            while self._worker_alive(host.id) and time.monotonic() < limit:
                time.sleep(0.05)
        return self._fresh(host.id)

    def _start_claimed(self, host: HostSpec, s: Ec2Settings, now: datetime, stop_at: str,
                       wait: bool) -> HostSpec:
        if s.instance_id and self._owned(host, s.instance_id):
            host = self.refresh_ssh_rule(host)
            item = self._owned(host, s.instance_id)
            state = str((item.get("instanceState") or {}).get("name") or "")
            if state not in ("running", "pending"):
                deadline = time.monotonic() + self.wait_running_s
                while state == "stopping":     # StartInstances refuses until fully stopped
                    if time.monotonic() > deadline:
                        raise ApiError(504, "host_offline", "The machine is still stopping. Try again "
                                       "in a minute.")
                    self._sleep(self.poll_s)
                    state = str(((self.describe(host, s.instance_id) or {}).get("instanceState")
                                 or {}).get("name") or "")
                self._call(settings_of(host), "StartInstances", {"InstanceId": [s.instance_id]})
            host = self._set(host.id, machine_state="starting", auto_stop_at=stop_at,
                             started_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             start_count=int(s.start_count or 0) + 1,
                             machine_detail="Starting the machine (AWS gives it a new address).")
        else:
            if not s.instance_id and not s.launch_token:
                # The ClientToken is stored before ANY AWS call of this launch attempt, so a
                # second process, a retry or a restart finds this attempt and never makes another.
                host = self._set(host.id, launch_token=uuid.uuid4().hex,
                                 start_count=int(s.start_count or 0) + 1)
                s = settings_of(host)
            if s.instance_id:                    # gone from AWS: start over with a new one,
                old_id = s.instance_id
                self._machine_gone(host, "The instance no longer exists in AWS.", refuse=True)
                self.forget_host_key(host, old_id)   # (refused when the keys are of another account)
                host = self._set(host.id, launch_token=uuid.uuid4().hex,
                                 start_count=int(s.start_count or 0) + 1)
                s = settings_of(host)
            if not s.key_name or not s.key_file or (not s.security_group_id
                                                    and not s.managed_group_id):
                host = self.prepare_host(host)
            elif s.managed_group_id and not s.security_group_id:
                host = self.refresh_ssh_rule(host)
            s = settings_of(host)
            adopted = self._adopt_launch(host) if s.launch_token else None
            if adopted is not None:           # an earlier attempt launched it; its answer was lost
                host = adopted
            else:
                host = self._launch(host, s, now, stop_at)
        self._drop_lease(host.id)             # the instance is recorded: others see its state
        if wait or not self.background:
            self._finish_start(host.id, claimed=True)
            return self._fresh(host.id)
        self.run_in_background(lambda: self._finish_start(host.id, claimed=True),
                               f"cc-ec2-{host.id}")
        return host

    def _launch(self, host: HostSpec, s: Ec2Settings, now: datetime, stop_at: str) -> HostSpec:
        """RunInstances with a ClientToken that is stored on the host BEFORE the call, so a
        lost answer, a retry or a restart finds the same instance instead of launching
        another. The token is cleared only once the launch is recorded or was refused."""
        client = self.client(s)
        try:
            image = self._image(client, s, s.instance_type)
        except AwsError as exc:
            raise self._api(exc) from None
        token = s.launch_token
        if not token:
            token = uuid.uuid4().hex
            host = self._set(host.id, launch_token=token,
                             start_count=int(s.start_count or 0) + 1)
        if not s.account_id:                    # the account the machine is started in
            account = self.current_account(s)
            host = self._set(host.id, account_id=account)
        params = self._run_params(s, image, s.max_lifetime_minutes, host.id, host.name)
        params["ClientToken"] = token
        try:
            resp = client.ec2("RunInstances", params)
        except AwsError as exc:
            if exc.code == "IdempotentParameterMismatch":
                self._set(host.id, launch_token=None)      # settings changed since; an instance
                                                           # with this token exists: it is listed as untracked
            elif not (exc.code in ("network", "bad_response") or (exc.status or 0) >= 500):
                self._set(host.id, launch_token=None)      # refused: nothing was launched
            raise self._api(exc) from None                 # otherwise: in doubt, keep the token
        found = [i for i in _list(resp.get("instancesSet")) if isinstance(i, dict)]
        aws_id = str(found[0].get("instanceId") or "") if found else ""
        if not _AWS_ID_RE.fullmatch(aws_id):
            raise ApiError(502, "unavailable", "AWS did not return an instance id; an unfinished "
                           "launch is looked up again on the next Start.")
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._set(host.id, instance_id=aws_id, machine_state="launching",
                         launched_at=stamp, started_at=stamp, auto_stop_at=stop_at,
                         ssh_user=image["user"], address=NO_MACHINE, launch_token=None,
                         launched_type=s.instance_type, launched_disk_gb=s.disk_gb,
                         unit_lifetime_minutes=s.max_lifetime_minutes,
                         machine_detail="Launching the instance.")

    def _adopt_launch(self, host: HostSpec) -> HostSpec | None:
        """The instance an earlier launch attempt created (found by its ClientToken and this
        install's tag), recorded on the host; ``None`` when AWS has none. An AWS error here
        propagates: nothing is launched again while this cannot be checked."""
        s = settings_of(host)
        if not s.launch_token or s.instance_id:
            return None
        resp = self._call(s, "DescribeInstances", {"Filter": [
            {"Name": "client-token", "Value": [s.launch_token]},
            {"Name": f"tag:{TAG_INSTALL}", "Value": [self.install_id()]},
            {"Name": "instance-state-name", "Value": list(_LIVE_STATES)}]})
        found = [i for i in _instances(resp) if _tags(i).get(TAG_INSTALL) == self.install_id()
                 and _AWS_ID_RE.fullmatch(str(i.get("instanceId") or ""))]
        if not found:
            return None
        item = found[0]
        launched = parse_iso(str(item.get("launchTime") or "")) or self._now()
        stamp = launched.strftime("%Y-%m-%dT%H:%M:%SZ")
        stop_at = (launched + timedelta(minutes=s.max_lifetime_minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return self._set(host.id, instance_id=str(item["instanceId"]), machine_state="launching",
                         launched_at=stamp, started_at=stamp, auto_stop_at=stop_at,
                         address=NO_MACHINE, launch_token=None,
                         launched_type=str(item.get("instanceType") or s.instance_type),
                         launched_disk_gb=s.disk_gb,
                         unit_lifetime_minutes=s.max_lifetime_minutes,
                         machine_detail="Found the instance of an earlier launch.")

    def resolve_pending(self, host: HostSpec) -> HostSpec:
        """Record the instance of an unfinished launch (if AWS has one); used before a
        terminate or a removal so such an instance is never left behind."""
        host = self._fresh(host)
        if settings_of(host).launch_token and not settings_of(host).instance_id:
            adopted = self._adopt_launch(host)
            if adopted is not None:
                return adopted
            host = self._set(host.id, launch_token=None)      # AWS has none: abandoned
        return host

    def _progress(self, host_id: str, text: str) -> None:
        self._set(host_id, machine_detail=text)

    def _finish_start(self, host_id: str, claimed: bool = False) -> None:
        """Wait for a started machine to become ready (see :meth:`_finish_start_inner`). Only
        one worker per host: the caller either holds the claim or this call takes it."""
        if not claimed and not self._claim(host_id):
            return
        try:
            self._finish_start_inner(host_id)
        finally:
            self._release(host_id)

    def _still_starting(self, host_id: str, aws_id: str) -> None:
        """Raise :class:`_Superseded` once the host was stopped, terminated or restarted by
        someone else, so a late worker never overwrites what happened since."""
        s = settings_of(self._fresh(host_id))
        if s.instance_id != aws_id or s.machine_state not in ("launching", "starting"):
            raise _Superseded()

    def _is_pinned(self, host: HostSpec, aws_id: str) -> bool:
        path = settings_of(host).known_hosts_file
        try:
            return bool(path) and any(ln.split(" ", 1)[0] == aws_id for ln in
                                      Path(path).read_text(encoding="utf-8").splitlines())
        except OSError:
            return False

    def _try_arm(self, host_id: str, aws_id: str, poll: int) -> bool:
        """Set the on-instance stop timer at the earliest moment SSH works, with the host key
        pinned from the console output first. True once it is set; never raises."""
        if poll % 3:
            return False
        try:
            host = self._fresh(host_id)
            if not self._is_pinned(host, aws_id) and not self.pin_host_key(host, aws_id):
                return False
            self._rearm(host)
            return True
        except Exception:  # noqa: BLE001 - the normal path below sets it again
            return False

    def _finish_start_inner(self, host_id: str) -> None:
        aws_id = ""
        try:
            host = self._fresh(host_id)
            s = settings_of(host)
            aws_id = s.instance_id or ""
            deadline = time.monotonic() + self.wait_running_s
            # Right after StartInstances, DescribeInstances can still show the old "stopped"
            # (with the reason of the earlier stop). A stop only counts as a failure of THIS
            # start once the machine was seen leaving stopped, or after a bounded grace.
            stale_until = time.monotonic() + _STALE_STOPPED_S
            left_stopped = False
            while True:
                self._still_starting(host_id, aws_id)
                try:
                    item = self.describe(host, aws_id) or {}
                except ApiError as exc:
                    # A new id may be unknown to DescribeInstances for a few seconds (AWS is
                    # eventually consistent): keep waiting, within the same deadline.
                    if "no longer exists" not in exc.message or time.monotonic() > deadline:
                        raise
                    item = {}
                state = str((item.get("instanceState") or {}).get("name") or "")
                if state == "running" and item.get("ipAddress"):
                    break
                left_stopped = left_stopped or state in _RUNNING_STATES
                stale = (state in ("stopped", "stopping") and not left_stopped
                         and time.monotonic() <= stale_until)
                if state in ("terminated", "shutting-down", "stopped", "stopping") and not stale:
                    reason = str((item.get("stateReason") or {}).get("message") or state)
                    raise ApiError(502, "unavailable", f"The machine stopped while starting "
                                   f"({reason}).")
                if time.monotonic() > deadline:
                    raise ApiError(504, "host_offline", "The machine did not reach running with a "
                                   "public address in time. If the subnet gives no public "
                                   "addresses, pick one that does.")
                self._sleep(self.poll_s)
            host = self._set(host_id, address=str(item["ipAddress"]),
                             machine_detail="Running; waiting for the AWS status checks.")
            deadline = time.monotonic() + self.wait_status_s
            polls, armed = 0, False
            while True:
                self._still_starting(host_id, aws_id)
                st = _list(self._call(s, "DescribeInstanceStatus", {
                    "InstanceId": [aws_id]}).get("instanceStatusSet"))
                if st and all(str((st[0].get(k) or {}).get("status")) == "ok"
                              for k in ("systemStatus", "instanceStatus")):
                    break
                if time.monotonic() > deadline:
                    raise ApiError(504, "host_offline", "The machine's AWS status checks did not pass "
                                   "in time. Stop and start it, or terminate it.")
                if not armed:
                    armed = self._try_arm(host_id, aws_id, polls)
                polls += 1
                self._sleep(self.poll_s)
            self._still_starting(host_id, aws_id)
            self.pin_host_key(host, aws_id)
            self._progress(host_id, "Checks passed; waiting for SSH and the container runtime.")
            self._wait_runtime(host_id)
            host = self._fresh(host_id)
            self._rearm(host)
            self._set(host_id, machine_state="ready", machine_detail="")
            try:
                self.services.prober.probe(self._fresh(host_id))
            except Exception:  # noqa: BLE001 - the card refreshes on the next probe
                pass
            self._notify(host_id, "info", f"{host.name} is ready",
                         f"The EC2 machine of {host.name} is running and ready for "
                         f"environments. {BILLING_SENTENCE}")
        except _Superseded:
            return                      # stopped, terminated or restarted meanwhile: leave it
        except Exception as exc:  # noqa: BLE001 - every failure ends in a clear state
            text = exc.message if isinstance(exc, ApiError) else (
                exc.plain if isinstance(exc, AwsError) else f"{type(exc).__name__}")
            try:
                self._still_starting(host_id, aws_id)
            except _Superseded:
                return
            stopped = self._stop_after_failure(host_id)
            self._set(host_id, machine_state="error",
                      machine_detail=f"{text} " + (
                          "The machine was stopped so it does not keep running; Start machine "
                          "tries again." if stopped else
                          "The machine may still be running and billed: stop or terminate it."))
            self._notify(host_id, "warn", "EC2 machine not ready", text)

    def _stop_after_failure(self, host_id: str) -> bool:
        try:
            host = self._fresh(host_id)
            s = settings_of(host)
            if s.instance_id and self._owned(host, s.instance_id):
                self._call(s, "StopInstances", {"InstanceId": [s.instance_id]})
                return True
        except Exception:  # noqa: BLE001
            log.info("cloud-compute: could not stop the machine of %s after a failure", host_id)
        return False

    def _wait_runtime(self, host_id: str) -> None:
        """Wait until the plugin's normal connection test passes (SSH login and Docker or
        Podman, which the first-boot script installs)."""
        deadline = time.monotonic() + self.wait_ssh_s
        rechecked = False
        last = ""
        while True:
            host = self._fresh(host_id)
            try:
                result = self.services.prober.test_connection(host, _machine=True)
                data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
                failing = [st for st in data.get("steps") or [] if st.get("ok") is False]
                if data.get("ok") or not failing:
                    return
                last = f"{failing[0].get('name')}: {failing[0].get('detail')}"
                if failing[0].get("name") in ("dns", "tcp") and not rechecked:
                    rechecked = True            # this computer's address may have changed
                    self.refresh_ssh_rule(host)
            except ApiError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = type(exc).__name__
            if time.monotonic() > deadline:
                raise ApiError(504, "host_offline", "The machine runs, but SSH or the container "
                               f"runtime was not ready in time ({_short(last, 160)}). Stop and "
                               "start it to try again.")
            self._sleep(self.poll_s)

    def _rearm(self, host: HostSpec) -> None:
        """Set the machine's own stop timer to ``auto_stop_at`` (start and Extend)."""
        s = settings_of(host)
        expires = parse_iso(s.auto_stop_at)
        if expires is None:
            return
        minutes = max(1, int((expires - self._now()).total_seconds() // 60))
        lifetime = max(1, int(s.max_lifetime_minutes))
        res = self.services.transports.for_host(host).run(["sh", "-c", _REARM, "sh",
                                                           str(minutes), str(lifetime)],
                                                          timeout=60)
        if res.rc == 0:                    # the next boot starts with the lifetime written now
            if s.unit_lifetime_minutes != lifetime:
                self._set(host.id, unit_lifetime_minutes=lifetime)
        elif res.rc != 3:
            raise ApiError(502, "internal", "The machine's own stop timer could not be set: "
                           + (_short(res.stderr, 200) or f"rc={res.rc}"))
        # rc 3: the stop is scheduled; the boot unit keeps the lifetime recorded in
        # unit_lifetime_minutes (the cards say so) and is tried again by the next check

    def _sync_unit(self, host: HostSpec) -> None:
        """Write the current max lifetime to the boot unit of a reachable machine when it differs
        from what was written last (the setting changed while the machine existed). Best effort,
        tried again at most once a minute."""
        s = settings_of(host)
        if s.machine_state != "ready" or s.unit_lifetime_minutes == s.max_lifetime_minutes:
            return
        last = self._unit_tried.get(host.id)
        if last is not None and time.monotonic() - last < 60.0:
            return
        self._unit_tried[host.id] = time.monotonic()
        try:
            self._rearm(self._fresh(host))
        except Exception:  # noqa: BLE001 - the card says what the machine still has
            log.info("cloud-compute: could not update the boot timer of %s", host.id)

    def extend_machine(self, host: HostSpec, minutes: int) -> HostSpec:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
            raise ApiError(400, "invalid", "minutes must be an integer from 1 to 1440")
        host = self._fresh(host)
        s = settings_of(host)
        if s.machine_state != "ready":
            raise ApiError(409, "conflict", f"The machine of {host.name} is {s.machine_state}; "
                           "only a running machine can be extended.")
        _current, new, latest = extend_window(s, minutes, self._now())
        if new > latest:
            raise ApiError(409, "limit_reached", EXTEND_LIMIT.format(
                latest=latest.strftime("%Y-%m-%d %H:%M UTC"),
                lifetime=_minutes_text(int(s.max_lifetime_minutes))))
        host = self._set(host.id, auto_stop_at=new.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._rearm(host)
        return host

    # ------------------------------------------------------------------ stop / terminate

    def _environments(self, host_id: str, statuses: tuple[str, ...] | None = None) -> list:
        return [r for r in self.store.list_instances(host_id) if r.status != "destroyed"
                and (statuses is None or r.status in statuses)]

    def stop_machine(self, host: HostSpec, *, reason: str = "user") -> HostSpec:
        """Stop the containers, then the instance. The disk (and every container with its
        files) is kept; AWS still bills a small amount for it."""
        host = self._fresh(host)
        s = settings_of(host)
        if s.machine_state in ("none", "stopped", "stopping") or not s.instance_id:
            return host
        if not self._owned(host, s.instance_id):
            self._machine_gone(host, "The instance no longer exists in AWS.", refuse=True)
            return self._fresh(host.id)
        sandboxes = self.services.sandboxes
        for rec in self._environments(host.id, ("running", "creating")):
            try:
                sandboxes.stop(rec.id, reason="host-gone" if reason != "user" else "stopped")
            except Exception:  # noqa: BLE001 - the machine stop stops it anyway
                sandboxes.mark_stopped(rec.id, "host-gone")
        self._call(s, "StopInstances", {"InstanceId": [s.instance_id]})
        host = self._set(host.id, machine_state="stopping", address=NO_MACHINE,
                         machine_detail=STOPPED_NOTE,
                         stopped_at=self._now().strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._clear_idle(host.id)
        with self._lock:
            self._stoppers[host.id] = time.monotonic()
        try:
            self.run_in_background(lambda: self._finish_stop(host.id), f"cc-ec2-stop-{host.id}")
        except BaseException:
            self._stoppers.pop(host.id, None)
            raise
        return self._fresh(host.id)

    def _stop_worker_alive(self, host_id: str) -> bool:
        """True while a stop worker of this process waits for ``host_id``. A claim older than
        the stop wait is treated as dead (a thread that could not start)."""
        since = self._stoppers.get(host_id)
        return since is not None and time.monotonic() - since <= self.wait_running_s + 120.0

    def _finish_stop(self, host_id: str) -> None:
        """Wait for AWS to report the instance stopped. A failed read, or a wait that ends
        before AWS says so, leaves the host in ``stopping``: the next check (tick, the
        watcher, or Check AWS on the page) settles it, so the host never stays there for good."""
        deadline = time.monotonic() + self.wait_running_s
        try:
            while time.monotonic() < deadline:
                if self._settle_stopping(self._fresh(host_id)):
                    return
                self._sleep(self.poll_s)
        except Exception:  # noqa: BLE001 - the next check settles it
            log.debug("cloud-compute: waiting for the stop of %s failed", host_id, exc_info=True)
        finally:
            self._stoppers.pop(host_id, None)

    def _settle_stopping(self, host: HostSpec) -> bool:
        """One look at AWS for a host in ``stopping``. True once the host left that state.

        * AWS says stopped (or terminated, or the instance is gone): the host is stopped
          (or has no machine) and Start is offered again;
        * AWS says stopping: wait, try again at the next check;
        * AWS says running or pending although the stop was requested (somebody started it
          again, or the stop never took): the stop is requested again, because a stop that
          the user or a rule asked for still stands; if AWS refuses, the card says the machine
          may still be running and billed;
        * a failed read (network, throttling) is never an answer: try again next time."""
        s = settings_of(host)
        if s.machine_state != "stopping" or not s.instance_id:
            return True
        try:
            item = self._owned(host, s.instance_id)
        except ApiError as exc:
            log.info("cloud-compute: could not read the stopping machine of %s: %s", host.id,
                     exc.message)
            return False
        if not item:
            return self._machine_gone(host, "The instance no longer exists in AWS.")
        state = str((item.get("instanceState") or {}).get("name") or "")
        if state == "stopped":
            self._mark_stopped(host.id)
            return True
        if state in _RUNNING_STATES:
            try:
                self._call(s, "StopInstances", {"InstanceId": [s.instance_id]})
            except ApiError as exc:
                self._warn_running(host, f"The machine should have stopped but AWS shows it "
                                   f"running and refused to stop it: {_short(exc.message, 160)}")
        return False

    def _mark_stopped(self, host_id: str) -> None:
        """The machine is stopped. ``stopped_at`` keeps the time of the stop request; a stop
        nobody asked for here (its own timer) is stamped now."""
        s = settings_of(self._fresh(host_id))
        stamp = (s.stopped_at if s.machine_state == "stopping" and s.stopped_at
                 else self._now().strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._set(host_id, machine_state="stopped", address=NO_MACHINE,
                  machine_detail=STOPPED_NOTE, stopped_at=stamp)
        self._offline(self._fresh(host_id))

    def current_account(self, s: Ec2Settings) -> str:
        """The 12-digit AWS account the stored keys belong to (STS GetCallerIdentity)."""
        try:
            who = self.client(s).sts("GetCallerIdentity")
        except AwsError as exc:
            raise self._api(exc) from None
        account = str(who.get("Account") or "")
        if not re.fullmatch(r"\d{12}", account):
            raise ApiError(502, "unavailable", "AWS did not say which account the keys belong to.")
        return account

    def _account_problem(self, host: HostSpec) -> str:
        """``""`` when the keys now belong to the account the machine was started in (or no
        account was recorded: a machine from before the account was kept); otherwise the
        sentence that says why the machine must not be forgotten."""
        s = settings_of(host)
        try:
            now = self.current_account(s)
        except ApiError:
            return UNKNOWN_ACCOUNT
        return FOREIGN_ACCOUNT if s.account_id and now != s.account_id else ""

    def _note_account(self, host: HostSpec) -> None:
        """The machine is visible with the current keys: record the account of a machine that
        has none recorded yet, and take the account warning off the card when the keys are
        those of the account the machine was started in."""
        s = settings_of(self._fresh(host))
        try:
            now = self.current_account(s)
        except ApiError:
            return
        changes: dict[str, Any] = {}
        if not s.account_id:
            changes["account_id"] = now
        if s.machine_detail in (FOREIGN_ACCOUNT, UNKNOWN_ACCOUNT) and (
                not s.account_id or s.account_id == now):
            changes["machine_detail"] = (STOPPED_NOTE if s.machine_state == "stopped" else "")
        if changes:
            self._set(host.id, **changes)

    def _warn_account(self, host: HostSpec, text: str) -> None:
        """Put the account warning on the host card (once)."""
        if settings_of(self._fresh(host)).machine_detail != text:
            self._set(host.id, machine_detail=text)
            self._notify(host.id, "warn", f"{host.name}: AWS account changed", text)

    def _machine_gone(self, host: HostSpec, detail: str, *, refuse: bool = False) -> bool:
        """AWS has no such instance. Forget it, but only when the keys belong to the account
        the machine was started in: with the keys of another account AWS cannot show it, and
        the machine may still be running and billed there. Otherwise the record is kept with a
        warning on the host card (``refuse``: and the caller gets the sentence as an error).
        True when the machine was forgotten."""
        host = self._fresh(host)
        problem = self._account_problem(host)
        if problem:
            self._warn_account(host, problem)
            if refuse:
                raise ApiError(409, "conflict", problem)
            return False
        self._set(host.id, machine_state="none", instance_id=None, address=NO_MACHINE,
                  account_id=None, unit_lifetime_minutes=None, machine_detail=detail)
        for rec in self._environments(host.id):
            self.services.sandboxes.mark_stopped(rec.id, "host-gone")
        self._offline(self._fresh(host.id))
        return True

    def terminate_machine(self, host: HostSpec, *, delete_environments: bool = False,
                          save: bool = True) -> dict:
        """Terminate the instance (its disk is deleted with it).

        Refused while the host has environments, unless ``delete_environments``: then each one
        is deleted through the normal delete flow first, with its files saved to this computer
        (``save=False`` only on the user's explicit choice). Saving needs a running machine.
        """
        host = self.resolve_pending(host)
        s = settings_of(host)
        envs = self._environments(host.id)
        if envs and not delete_environments:
            names = ", ".join(r.name for r in envs[:8])
            raise ApiError(409, "conflict", f"{host.name} still has {len(envs)} "
                           f"environment(s) ({names}). Terminating deletes the machine's disk with "
                           "them. Save and delete them first: choose Save all and terminate, or "
                           "delete each one (its files are saved).")
        if envs and save and s.machine_state != "ready":
            names = ", ".join(r.name for r in envs[:8])
            state = "has no machine" if s.machine_state == "none" else f"is {s.machine_state}"
            raise ApiError(409, "not_saved", f"The machine of {host.name} {state}, so the files "
                           f"of its environments ({names}) cannot be saved. Saving needs the "
                           "machine running. Choose one: start the machine (AWS bills while it "
                           "runs) and then Save all and terminate; or Terminate without saving, "
                           "which deletes those files.")
        report: list[dict] = []
        sandboxes = self.services.sandboxes
        if save:
            for rec in envs:
                out: dict = {}
                sandboxes.destroy(rec.id, save=True, report=out, notify=False)
                report.append({"id": rec.id, "name": rec.name, "message": out.get("message")})
        if s.instance_id:
            if self._owned(host, s.instance_id):
                self._call(s, "TerminateInstances", {"InstanceId": [s.instance_id]})
                self.forget_host_key(host, s.instance_id)
            else:                        # AWS shows none: only the account it was started in may say so
                problem = self._account_problem(host)
                if problem:
                    self._warn_account(host, problem)
                    raise ApiError(409, "conflict", problem)
        self._set(host.id, machine_state="none", instance_id=None, address=NO_MACHINE,
                  account_id=None, unit_lifetime_minutes=None, launched_at=None,
                  started_at=None, auto_stop_at=None,
                  machine_detail="Terminated: the instance and its disk were deleted.")
        if not save:        # the disk and every container went with the instance: no command
            for rec in envs:
                out = {}
                sandboxes.destroy(rec.id, save=False, with_machine=True, report=out,
                                  notify=False)
                report.append({"id": rec.id, "name": rec.name, "message": out.get("message")})
        sandboxes.clear_leftovers(host.id)
        self._offline(self._fresh(host.id))
        return {"terminated": True, "environments": report}

    # ------------------------------------------------------------------ timers + reconcile

    def _clear_idle(self, host_id: str) -> None:
        try:
            self.store.kv_delete(_IDLE_KEY + host_id)
        except Exception:  # noqa: BLE001
            pass

    def tick(self, host: HostSpec) -> None:
        """Lifetime, idle rule and AWS check for one host (called by each probe and by the
        watcher). They apply in EVERY state in which the host has an instance id: a machine
        that is starting without a worker, or in error, is not left running unobserved."""
        host = self._fresh(host)
        s = settings_of(host)
        if s.instance_id:
            if s.machine_state in ("launching", "starting"):
                if not self._worker_alive(host.id):
                    self._resume_or_expire(host)
                return
            if s.machine_state == "stopping":
                if not self._stop_worker_alive(host.id):     # its worker died or gave up
                    self._settle_stopping(host)
            elif s.machine_state == "error":
                self._stop_if_running(host)
            elif s.machine_state == "ready":
                if self._enforce_ready(host):
                    return
                self._sync_unit(host)
        self.reconcile(host, force=False)

    def watch(self, host: HostSpec) -> None:
        """Pick up a start or a stop whose worker died (Hermes quit or restarted while it
        ran): resume the wait, or settle the state from what AWS reports."""
        s = settings_of(host)
        if not s.instance_id:
            return
        if s.machine_state in ("launching", "starting") and not self._worker_alive(host.id):
            self._resume_or_expire(self._fresh(host))
        elif s.machine_state == "stopping" and not self._stop_worker_alive(host.id):
            self._settle_stopping(self._fresh(host))

    def _settle_transitional(self, host: HostSpec) -> None:
        """Check AWS for a host in a transitional state that no worker of this process is
        working on, so no state is a dead end after a restart or one failed read. Used by
        Check AWS on the page (``reconcile``)."""
        s = settings_of(host)
        if not s.instance_id:
            return
        if s.machine_state in ("launching", "starting"):
            if not self._worker_alive(host.id):
                self._resume_or_expire(host)
        elif s.machine_state == "stopping":
            if not self._stop_worker_alive(host.id):
                self._settle_stopping(host)
        elif s.machine_state == "terminating":     # never saved by this plugin; be safe
            try:
                if not self._owned(host, s.instance_id):
                    self._machine_gone(host, "The instance was terminated.")
            except ApiError:
                pass

    def _enforce_ready(self, host: HostSpec) -> bool:
        """The time limit and the idle rule of a ready machine; True when it was stopped."""
        s = settings_of(host)
        now = self._now()
        stop_at = parse_iso(s.auto_stop_at)
        if stop_at is not None and now >= stop_at:
            return self._stop_for(host, "expired", f"The machine of {host.name} reached its "
                                  f"time limit and was stopped. {STOPPED_NOTE}")
        if s.idle_stop_minutes > 0:
            busy = self._environments(host.id, ("running", "creating"))
            if busy:
                self._clear_idle(host.id)
                return False
            since = parse_iso(self.store.kv_get(_IDLE_KEY + host.id) or "")
            if since is None:
                self.store.kv_set(_IDLE_KEY + host.id, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            elif now - since >= timedelta(minutes=s.idle_stop_minutes):
                return self._stop_for(host, "idle", f"The machine of {host.name} ran no "
                                      f"environment for {s.idle_stop_minutes} min and was "
                                      f"stopped. {STOPPED_NOTE}")
        return False

    def _stop_for(self, host: HostSpec, reason: str, done_text: str) -> bool:
        """Stop ``host``'s machine because a rule says so. When the stop itself fails the card
        says the machine may still be running and billed (and the next tick tries again)."""
        try:
            self.stop_machine(host, reason=reason)
        except (ApiError, AwsError) as exc:
            text = exc.message if isinstance(exc, ApiError) else exc.plain
            self._warn_running(host, f"The machine should have stopped ({reason}) but AWS "
                               f"refused: {_short(text, 160)}")
            return True
        self._notify(host.id, "info", f"{host.name} was stopped", done_text)
        return True

    def _warn_running(self, host: HostSpec, why: str) -> None:
        """Say on the card that the machine may still be running and billed, once."""
        text = (f"{why} The machine may still be running and billed: press Stop machine, or "
                "stop it in the AWS console.")
        if settings_of(self._fresh(host)).machine_detail != text:
            self._set(host.id, machine_detail=text)
            self._notify(host.id, "warn", f"{host.name} may still be running", text)

    def _resume_or_expire(self, host: HostSpec) -> None:
        """A start without a live worker: past its time limit, stop the instance and say so;
        otherwise wait for it again (reach ready, arm the on-instance timer)."""
        s = settings_of(host)
        stop_at = parse_iso(s.auto_stop_at)
        if stop_at is not None and self._now() >= stop_at:
            stopped = self._stop_after_failure(host.id)
            self._set(host.id, machine_state="error", machine_detail=(
                "The machine was still starting when Hermes stopped watching it, and its time "
                "limit has passed. " + ("It was stopped; Start machine tries again." if stopped
                                        else "It could not be stopped and may still be running "
                                             "and billed: press Stop machine.")))
            self._notify(host.id, "warn", f"{host.name} was stopped" if stopped else
                         f"{host.name} may still be running",
                         "The machine did not finish starting before its time limit.")
            return
        if not self._claim(host.id):
            return
        self._set(host.id, machine_detail="Waiting for the machine again after Hermes restarted.")
        try:
            self.run_in_background(lambda: self._finish_start(host.id, claimed=True),
                                   f"cc-ec2-{host.id}")
        except BaseException:
            self._release(host.id)
            raise

    def _stop_if_running(self, host: HostSpec) -> None:
        """A host in error whose instance is still running: stop it again (every tick, until
        AWS says it is stopping), with a visible warning when the stop fails."""
        s = settings_of(host)
        item = self._owned(host, s.instance_id)
        if not item:
            return
        if str((item.get("instanceState") or {}).get("name")) not in _RUNNING_STATES:
            return
        try:
            self._call(s, "StopInstances", {"InstanceId": [s.instance_id]})
        except ApiError as exc:
            self._warn_running(host, f"The machine could not be stopped: {_short(exc.message, 160)}")
            return
        self._set(host.id, machine_detail=(s.machine_detail.split(" The machine may still")[0]
                                           + " The machine was stopped again so it does not keep "
                                             "running; Start machine tries again."))

    def list_tagged(self, host: HostSpec) -> list[dict]:
        """Every live instance in the host's region that carries this install's tag."""
        s = settings_of(host)
        resp = self._call(s, "DescribeInstances", {"Filter": [
            {"Name": f"tag:{TAG_INSTALL}", "Value": [self.install_id()]},
            {"Name": "instance-state-name", "Value": list(_LIVE_STATES)}]})
        out = []
        for item in _instances(resp):
            tags = _tags(item)
            if tags.get(TAG_INSTALL) != self.install_id():
                continue                         # never trust the filter alone
            out.append({"instance_id": str(item.get("instanceId") or ""),
                        "state": str((item.get("instanceState") or {}).get("name") or ""),
                        "instance_type": str(item.get("instanceType") or ""),
                        "public_ip": item.get("ipAddress") or None,
                        "launched_at": str(item.get("launchTime") or ""),
                        "host": tags.get(TAG_HOST) or None, "name": tags.get("Name") or ""})
        return out

    def reconcile(self, host: HostSpec, *, force: bool = True) -> dict:
        """Match AWS with the host's machine and list tagged instances nobody tracks.

        The host's own machine state follows AWS (stopped by its own timer, or gone).
        Instances with this install's tag that belong to no host are only listed, never
        touched automatically.
        """
        if not force:
            last = self._last_reconcile.get(host.id)
            if last is not None and time.monotonic() - last < _RECONCILE_EVERY_S:
                return self.untracked(host.id)
        self._last_reconcile[host.id] = time.monotonic()
        host = self._fresh(host)
        if settings_of(host).launch_token and not settings_of(host).instance_id:
            try:
                host = self._adopt_launch(host) or host
            except ApiError:
                pass
        if force:
            self._settle_transitional(host)
            host = self._fresh(host)
        s = settings_of(host)
        seen = {i["instance_id"]: i for i in self.list_tagged(host)}
        known = {settings_of(h).instance_id for h in self.store.list_hosts("ec2")
                 if settings_of(h).region == s.region and settings_of(h).instance_id}
        if s.instance_id and s.machine_state not in ("launching", "starting", "stopping",
                                                     "terminating"):
            mine = seen.get(s.instance_id)
            if mine is None:
                self._machine_gone(host, "The instance no longer exists in AWS.")
            else:
                if not s.account_id or s.machine_detail in (FOREIGN_ACCOUNT, UNKNOWN_ACCOUNT):
                    self._note_account(host)
                    s = settings_of(self._fresh(host))
                if mine["state"] in ("stopped", "stopping") and s.machine_state in (
                        "ready", "error"):
                    for rec in self._environments(host.id, ("running", "creating")):
                        self.services.sandboxes.mark_stopped(rec.id, "expired")
                    self._mark_stopped(host.id)
                elif (mine["state"] == "running" and s.machine_state == "ready"
                      and mine["public_ip"] and mine["public_ip"] != host.address):
                    self._set(host.id, address=str(mine["public_ip"]))
        untracked = [i for aws_id, i in seen.items() if aws_id not in known]
        fresh = {i["instance_id"] for i in untracked} - {
            i.get("instance_id") for i in self.untracked(host.id)["untracked"]}
        if fresh:
            self._notify(host.id, "warn", "Instances found on AWS that no host tracks",
                         f"{', '.join(sorted(fresh))} carry this install's tag but belong to no "
                         "host. AWS bills the account for them until they are stopped or "
                         "terminated: open the host card to stop or terminate them.")
        data = {"host_id": host.id, "checked_at": utc_now_iso(), "untracked": untracked}
        try:
            self.store.kv_set(_UNTRACKED_KEY + host.id, json.dumps(data, sort_keys=True))
        except Exception:  # noqa: BLE001
            pass
        return data

    def untracked(self, host_id: str) -> dict:
        try:
            raw = self.store.kv_get(_UNTRACKED_KEY + host_id)
            data = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            data = None
        return data if isinstance(data, dict) else {"host_id": host_id, "checked_at": None,
                                                    "untracked": []}

    def act_untracked(self, host: HostSpec, aws_id: str, action: str) -> dict:
        """Stop or terminate an instance found on AWS with this install's tag but tracked by no
        host. Only on the user's request from the page; the tag is checked again first."""
        if action not in ("stop", "terminate"):
            raise ApiError(400, "invalid", "action must be stop or terminate")
        listed = {i["instance_id"] for i in self.untracked(host.id)["untracked"]}
        if aws_id not in listed:
            raise ApiError(404, "not_found", f"{aws_id} is not in the list of untracked "
                           "instances of this host; refresh the list first.")
        if any(settings_of(h).instance_id == aws_id for h in self.store.list_hosts("ec2")):
            raise ApiError(409, "conflict", f"{aws_id} is the machine of a host; use its card.")
        item = self._owned(host, aws_id)
        if not item:
            raise ApiError(404, "not_found", f"{aws_id} no longer exists in AWS.")
        verb = "StopInstances" if action == "stop" else "TerminateInstances"
        self._call(settings_of(host), verb, {"InstanceId": [aws_id]})
        self.reconcile(host)
        return {"instance_id": aws_id, "action": action, "done": True}

    # ------------------------------------------------------------------ prober hooks

    def _offline(self, host: HostSpec) -> HostState:
        """Store an offline state without any network call: the machine is not running."""
        s = settings_of(host)
        prev = self.store.get_host_state(host.id)
        text = {"none": "No machine: press Start machine to launch one.",
                "stopped": "The machine is stopped. " + STOPPED_NOTE,
                "stopping": "The machine is stopping.",
                "launching": s.machine_detail or "The machine is starting.",
                "starting": s.machine_detail or "The machine is starting.",
                "error": s.machine_detail or "The machine is not ready."}.get(
            s.machine_state, s.machine_detail or "The machine is not ready.")
        state = HostState(host_id=host.id, status="offline", last_seen=prev.last_seen if prev
                          else None, last_error=text, latency_ms=None,
                          capabilities=prev.capabilities if prev else Capabilities(),
                          resources=prev.resources if prev else None, probed_at=utc_now_iso())
        before = self.store.set_host_state(state)
        if before != "offline":
            try:
                # ``expected``: no machine, a stopped or a starting one is a normal state, so the
                # page raises no "went offline" notice for it
                self.services.events.emit("host.status", {
                    "id": host.id, "status": "offline", "prev": before or "unknown",
                    "last_error": text,
                    "expected": s.machine_state in ("none", "stopped", "stopping", "launching",
                                                    "starting")})
            except Exception:  # noqa: BLE001
                pass
        return state

    def offline_state(self, host: HostSpec) -> HostState:
        """The prober's answer for a host whose machine is not ready (no SSH attempt)."""
        try:
            self.tick(host)
        except ApiError as exc:
            log.info("cloud-compute: EC2 check of %s failed: %s", host.id, exc.message)
        return self._offline(self._fresh(host.id))

    def _notify(self, host_id: str, level: str, title: str, body: str) -> None:
        try:
            self.services.events.emit("notify", {"level": level, "title": title, "body": body,
                                                 "ref": {"kind": "host", "id": host_id}})
        except Exception:  # noqa: BLE001
            pass

    def run_in_background(self, fn: Callable[[], None], name: str) -> None:
        """Run ``fn`` on a daemon thread that keeps the secret scope of this request."""
        if not self.background:
            fn()
            return
        ctx = contextvars.copy_context()
        track_worker(threading.Thread(target=ctx.run, args=(fn,), name=name,
                                      daemon=True)).start()   # shutdown waits for it


#: ``$1``: minutes until the machine stops now; ``$2``: the lifetime every later boot uses (the
#: boot unit written by :func:`user_data` is rewritten with it). Exit 1: the stop could not be
#: scheduled; exit 3: it was, but the boot unit could not be rewritten. The old timer is stopped
#: and the new one started (see :data:`_ARM`); a scheduled ``shutdown`` left by an older
#: first-boot script is cancelled too, because it would lock SSH out before its time.
_REARM = ("sudo -n shutdown -c >/dev/null 2>&1 || :; "
          "sudo -n systemctl stop cloud-compute-stop.timer >/dev/null 2>&1 || :; "
          "sudo -n systemctl reset-failed cloud-compute-stop.timer cloud-compute-stop.service "
          ">/dev/null 2>&1 || :; "
          "sudo -n systemd-run --unit=cloud-compute-stop --on-active=\"$1m\" "
          "--timer-property=AccuracySec=1s systemctl poweroff >/dev/null 2>&1 || exit 1; "
          "U=/etc/systemd/system/cloud-compute-limit.service; "
          "[ -f \"$U\" ] && sudo -n sed -i \"s/--on-active=[0-9][0-9]*m/--on-active=$2m/\" \"$U\" "
          "&& sudo -n systemctl daemon-reload >/dev/null 2>&1 || exit 3")


def check_cidr(value: str) -> str:
    """A CIDR (or one address) that may use SSH; refuses ranges that open SSH to everyone."""
    text = str(value or "").strip()
    try:
        net = ipaddress.ip_network(text if "/" in text else text + ("/32" if ":" not in text
                                                                    else "/128"), strict=False)
    except ValueError:
        raise ApiError(400, "invalid", f"{value!r} is not an address or range (for example "
                       "203.0.113.7 or 203.0.113.0/24).") from None
    if net.version != 4:
        raise ApiError(400, "invalid", "Use an IPv4 address or range for the SSH rule.")
    if net.prefixlen < 16:
        raise ApiError(400, "invalid", "That range would open SSH to too many addresses. Use your "
                       "own address or a range of at most /16.")
    return str(net)


def _ssh_keygen(path: Path, comment: str) -> None:
    """Generate an ed25519 key pair with the system ``ssh-keygen`` (no passphrase)."""
    exe = shutil.which("ssh-keygen")
    if exe is None:
        try:
            from .transport import resolve_ssh_binary
            ssh = resolve_ssh_binary()
            cand = Path(ssh).with_name("ssh-keygen" + (".exe" if ssh.lower().endswith(".exe")
                                                       else ""))
            exe = str(cand) if cand.is_file() else None
        except Exception:  # noqa: BLE001
            exe = None
    if exe is None:
        raise ApiError(503, "unavailable", "ssh-keygen was not found. Install the OpenSSH client, "
                       "then test the host again.")
    res = subprocess.run([exe, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if res.returncode != 0 or not path.is_file():
        raise ApiError(502, "internal", "ssh-keygen failed: " + _short(res.stderr, 200))


def provide(services: Any) -> Ec2Manager:
    """Service provider used by the module registry."""
    return Ec2Manager(services)
