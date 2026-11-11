"""EC2 hosts from chat: what the ``compute_host`` tool takes, the checks made BEFORE any approval
card, every EC2 approval card, and the one place that words the cost.

* Chat can add, change and remove an EC2 host and start, stop, extend and terminate its machine.
  Each of these asks the user first; the card shows every value and says what it costs.
* Chat never takes an AWS key. The key lives in the Hermes secrets; the tool takes only the
  NAMES of the two secrets, and a parameter or value that looks like a key is refused.
* The image (also a custom AMI), subnet, security group, SSH user and the name of the session
  token secret stay on the Compute page: they change what runs and who can reach it beyond what
  a card can show.
* A request that cannot work is refused before a card (no approval is asked for it). Nothing
  here calls AWS: a card is built from the store only.

Every cost sentence is built here (:func:`start_cost`, :data:`ADD_COST`, :data:`STOP_COST`,
:data:`TERMINATE_COST`, :func:`running_cost`), so the cards of the page route, the chat tool and
``env_spawn`` cannot drift apart. No price is named and nothing is called free: Own Compute
cannot see AWS prices or the account's allowance.

Standard library only.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .interfaces import ApiError
from .models import Ec2Settings, HostSpec, parse_iso

__all__ = [
    "EC2_PARAMS", "EC2_PAGE_ONLY", "ADD_COST", "STOP_COST", "update_cost", "TERMINATE_COST", "KEY_MESSAGE",
    "start_cost", "start_marker", "calm_label", "running_cost", "facts", "duration", "ssh_access_text", "cost_state",
    "reject_key_values", "is_page_only", "build_new", "build_update", "describe_add",
    "describe_update", "describe_remove", "describe_test", "describe_machine",
    "spawn_text", "MARK",
]

#: Tool parameters an EC2 host takes from chat (they are the :class:`Ec2Settings` fields).
EC2_PARAMS = ("region", "instance_type", "disk_gb", "max_lifetime_minutes", "idle_stop_minutes",
              "auto_start", "ssh_cidr", "access_key_secret", "secret_key_secret")
#: Settings only the Compute page changes, and why: a custom image runs code the card cannot
#: show; a subnet or security group of the user's own decides who can reach the machine (and
#: replaces the SSH rule the card describes); the SSH user only matters for a custom image;
#: the session token secret is a third key name.
EC2_PAGE_ONLY = ("image", "ami", "subnet_id", "security_group_id", "ssh_user",
                 "session_token_secret")
#: Every approval card of an EC2 host carries this word; the approval keys hash the whole card
#: for such cards, so one answer never covers a different value.
MARK = "EC2"

ADD_COST = ("Adding this host starts nothing and costs nothing yet. Starting the machine later "
            "is billed by AWS to the account these keys belong to.")

def update_cost(s: Ec2Settings) -> str:
    """What changing a host's settings costs (it starts nothing)."""
    if s.instance_id:
        return ("Changing these settings starts nothing. The machine that exists keeps being "
                "billed by AWS to the account these keys belong to for as long as it exists.")
    return ("Changing this host starts nothing and costs nothing yet. Starting the machine "
            "later is billed by AWS to the account these keys belong to.")


#: How a machine that is not running is started: with this plugin, never in the AWS console.
START_WITH_PLUGIN = ('To start it again use compute_host(action="start", host_id=...) (the user '
                     "approves a card that states the cost) or the Start machine button on the "
                     "Hosts tab of the Compute page; do not start it in the AWS console or with "
                     "the AWS command line.")
STOP_COST = ("AWS charges no running time once the machine has stopped, but the disk is still "
             "billed until the machine is terminated.")
TERMINATE_COST = ("AWS deletes the machine and its disk, and nothing more is billed for them. "
                  "Environments on it must be saved first; their files are deleted with the disk.")

KEY_MESSAGE = ("AWS key values are never accepted in chat. Store the keys in Hermes secrets "
               "(AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or other names given with "
               "access_key_secret and secret_key_secret), then call again without any key.")
#: Parameter names that would carry an AWS key value.
_KEY_PARAMS = frozenset({
    "aws_access_key_id", "aws_secret_access_key", "aws_session_token", "access_key_id",
    "secret_access_key", "secret_key", "access_key", "session_token", "aws_key", "aws_secret",
    "aws_credentials"})
_KEY_ID = re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|APKA|AROA|ASCA)"
                     r"[A-Z0-9]{16}\b")
_KEY_ASSIGN = re.compile(r"aws_(?:secret_access_key|access_key_id|session_token)\s*[=:]", re.I)

_MAX_NAME = 100


def _bad(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


# --------------------------------------------------------------------------- wording


def duration(minutes: Any) -> str:
    """``90`` -> ``90 minutes``; ``120`` -> ``2 hours``."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "its time limit"
    if m > 0 and m % 60 == 0:
        h = m // 60
        return f"{h} hour" + ("" if h == 1 else "s")
    return f"{m} minute" + ("" if m == 1 else "s")


def _idle_text(minutes: Any) -> str:
    m = int(minutes or 0)
    return (f"after {duration(m)} with no running environment" if m > 0
            else "off (the machine is not stopped for being idle)")


def ssh_access_text(s: Ec2Settings) -> str:
    """Who may use SSH. A range wider than one address is called out in plain words."""
    if s.security_group_id:
        return (f"decided by the security group {s.security_group_id} that was set on the "
                "Compute page (the allowed address below is not applied)")
    cidr = (s.ssh_cidr or "").strip()
    if not cidr:
        return "only this computer's public address (looked up when the machine is launched)"
    base, _, bits = cidr.partition("/")
    if bits in ("", "32"):
        return f"only {base}"
    try:
        n = 2 ** (32 - int(bits))
    except ValueError:
        return cidr
    if bits == "0":
        return f"SSH open to the whole internet ({cidr})"
    return (f"SSH OPEN TO THIS RANGE: {cidr} ({n:,} addresses), not only this computer")


def facts(s: Ec2Settings, starts_billing: bool) -> str:
    """Region, type, disk, lifetime, idle rule, who may use SSH and whether billing starts now.
    The type and disk are those of the machine that exists, else of the next launch."""
    from .ec2 import launched_values
    kind, disk = launched_values(s)
    return (f"Region: {s.region}; Instance type: {kind}; Disk: {disk} GB; Max lifetime: "
            f"{duration(s.max_lifetime_minutes)}; Idle stop: {_idle_text(s.idle_stop_minutes)}; "
            f"SSH allowed from: {ssh_access_text(s)}; Starts billing now: "
            f"{'yes' if starts_billing else 'no'}.")


def _timer_gap(s: Ec2Settings) -> str:
    """The sentence about the machine's boot timer, when it still has another lifetime."""
    from .ec2 import boot_timer_note
    note = boot_timer_note(s)
    return f" {note}" if note else ""


def start_marker(s: Ec2Settings) -> str:
    """Which start this is. It changes with every start of the host's machine, so the text of
    a Start card (and with it its approval key) is never the same twice: one answer of
    "session" or "always" covers that start only."""
    from .approval import _when
    last = parse_iso(s.stopped_at) if s.stopped_at else None
    when = _when(last) if last is not None else "never"
    return f"Start number {int(s.start_count or 0) + 1} of this host's machine; last stopped: {when}."


def start_cost(s: Ec2Settings) -> str:
    """What starting (or auto-starting) the machine costs. One sentence for every card."""
    from .ec2 import launched_values
    kind, disk = launched_values(s)
    stop = (f"It stops by itself after {duration(s.max_lifetime_minutes)}, or after "
            f"{duration(s.idle_stop_minutes)} with no running environment."
            if int(s.idle_stop_minutes or 0) > 0 else
            f"It stops by itself after {duration(s.max_lifetime_minutes)}; it is not stopped "
            "for being idle.")
    return (f"This starts an AWS machine ({kind} in {s.region}, {disk} GB disk). AWS bills the "
            "account while it runs, bills for its disk for as long as the machine exists (also "
            "while stopped), and may bill for its public address. " + stop + " Own Compute "
            "cannot see AWS prices or whether the account's free allowance covers this: check "
            "the AWS billing page.")


def running_cost(s: Ec2Settings) -> str:
    """The machine is already running: AWS bills the account until it stops."""
    stop = f" It stops by itself at {s.auto_stop_at}." if s.auto_stop_at else ""
    return "AWS bills the account while this machine runs, and for its disk until it is " \
           "terminated." + stop


#: An EC2 host with no machine, a stopped one or one on its way up or down is in a normal state,
#: not a failure: summaries and the chat line name the state and never say "offline". The
#: Hosts tab of the page uses the same words.
CALM_LABELS = {"none": "No machine yet", "stopped": "Machine stopped",
               "launching": "Machine starting", "starting": "Machine starting",
               "stopping": "Machine stopping"}


def calm_label(spec: Any, status: Any) -> str:
    """The neutral words for an EC2 host that is not online because it has no running
    machine; ``""`` for every other host (and for a machine in error or ready)."""
    if not isinstance(spec, dict) or spec.get("kind") != "ec2" or status == "online":
        return ""
    e = spec.get("ec2") if isinstance(spec.get("ec2"), dict) else {}
    return CALM_LABELS.get(e.get("machine_state") or "none", "")


def cost_state(ec2: Any) -> str:
    """One short sentence for a tool result: whether a machine runs and when it stops."""
    d = ec2.to_dict() if hasattr(ec2, "to_dict") else dict(ec2 or {})
    state = d.get("machine_state") or "none"
    stop = f" It stops by itself at {d['auto_stop_at']} UTC." if d.get("auto_stop_at") else ""
    if not d.get("instance_id") and state in ("none", ""):
        return "No machine is running."
    if state in ("launching", "starting"):
        return ("The machine is starting. AWS bills the account while it runs." + stop)
    if state == "ready":
        since = f" since {d['started_at']} UTC" if d.get("started_at") else ""
        return (f"A machine is running{since}. AWS bills the account while it runs." + stop)
    if state in ("stopping", "stopped"):
        return ("The machine is stopped. Its disk is still billed until the machine is terminated. "
                + START_WITH_PLUGIN)
    if state == "terminating":
        return "The machine is being terminated."
    if state == "error":
        return ("The machine is not ready and may still be running and billed. "
                + str(d.get("machine_detail") or "")[:160]).strip()
    return f"Machine state: {state}."


# --------------------------------------------------------------------------- parameters


def is_page_only(args: dict, services: Any) -> bool:
    """True when ``args`` set an EC2 setting that only the Compute page changes."""
    if not any(args.get(k) is not None for k in EC2_PAGE_ONLY):
        return False
    if str(args.get("kind") or "").strip().lower() == "ec2":
        return True
    try:
        host = services.store.get_host(args.get("host_id"))
    except Exception:  # noqa: BLE001
        host = None
    return host is not None and host.kind == "ec2"


def looks_like_key_text(value: Any) -> bool:
    """True for text that looks like an AWS access key id, secret key or session token."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if _KEY_ID.search(text) or _KEY_ASSIGN.search(text):
        return True
    if len(text) >= 32 and "_" not in text and re.search(r"\d", text) \
            and re.search(r"[a-z]", text) and re.search(r"[A-Z]", text):
        return True
    return len(text) >= 20 and bool(re.fullmatch(r"[A-Za-z0-9/+=]+", text)) \
        and bool(re.search(r"[/+=]", text)) and bool(re.search(r"[A-Za-z]", text))


def reject_key_values(args: dict) -> None:
    """Refuse a parameter, or a value, that carries an AWS key (raised before any card; the
    refused text is never repeated)."""
    for key, value in args.items():
        if str(key).lower() in _KEY_PARAMS and value is not None:
            raise _bad(KEY_MESSAGE)
        if isinstance(value, str) and looks_like_key_text(value) and (
                key in ("access_key_secret", "secret_key_secret") or _KEY_ID.search(value)
                or _KEY_ASSIGN.search(value)):
            raise _bad(KEY_MESSAGE)


def _plain(exc: ApiError) -> ApiError:
    """The validation message with tool parameter names instead of ``ec2.field``."""
    return ApiError(exc.status, exc.code, re.sub(r"\bec2\.", "", exc.message))


#: The secret names chat may point the AWS key at. The value of a named secret is sent to AWS in a
#: signed request, so any other Hermes secret (a model key, a token) is named on the page only.
DEFAULT_KEY_NAMES = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def check_secret_names(args: dict) -> None:
    """Refuse a secret name from chat that is neither a default name nor starts with ``AWS_``."""
    for param in ("access_key_secret", "secret_key_secret"):
        value = args.get(param)
        if value is None:
            continue
        name = str(value).strip()
        if name in DEFAULT_KEY_NAMES or name.startswith("AWS_"):
            continue
        raise _bad(f"From chat, {param} can only be AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY or a "
                   "name that starts with AWS_, because the value of that secret is sent to AWS. "
                   "Any other secret name can be set on the Compute page only.")


def _given(args: dict) -> dict:
    return {k: args[k] for k in EC2_PARAMS if args.get(k) is not None}


def _check_secrets(services: Any, s: Ec2Settings) -> None:
    missing = [n for n in (s.access_key_secret, s.secret_key_secret)
               if not _secret_set(services, n)]
    if not missing:
        return
    names = " and ".join(missing)
    plural = "secrets" if len(missing) > 1 else "secret"
    raise _bad(f"The Hermes {plural} {names} {'are' if len(missing) > 1 else 'is'} not set. "
               f"Add {'them' if len(missing) > 1 else 'it'} in Hermes secrets (the key is never "
               "typed into chat), then ask again.")


def _secret_set(services: Any, name: str) -> bool:
    try:
        return bool(services.secrets.is_set(name))
    except Exception:  # noqa: BLE001 - unknown means not set
        return False


def _validated(raw: dict, saved: dict | None) -> Ec2Settings:
    from .ec2_api import validate_ec2
    try:
        data = validate_ec2(raw, saved)
    except ApiError as exc:
        raise _plain(exc) from None
    return Ec2Settings.from_dict(data)


def build_new(args: dict, services: Any) -> Ec2Settings:
    """The settings of a new EC2 host from chat parameters (defaults filled in). Raises the
    plain-sentence error of the first thing that cannot work. No AWS call."""
    reject_key_values(args)
    check_secret_names(args)
    s = _validated(_given(args), None)
    _check_secrets(services, s)
    return s


def build_update(args: dict, host: HostSpec, services: Any) -> tuple[Ec2Settings, Ec2Settings]:
    """``(old, new)`` settings of an update. Raises when the change cannot work (including a
    launch-only setting while a machine exists)."""
    reject_key_values(args)
    check_secret_names(args)
    old = host.ec2 if host.ec2 is not None else Ec2Settings()
    saved = old.to_dict()
    values = _given(args)
    new = _validated({**saved, **values}, saved)
    if "access_key_secret" in values or "secret_key_secret" in values:
        _check_secrets(services, new)
    return old, new


# --------------------------------------------------------------------------- field rows

_LABELS = {
    "region": "Region", "instance_type": "Instance type", "disk_gb": "Disk",
    "max_lifetime_minutes": "Max lifetime", "idle_stop_minutes": "Idle stop",
    "auto_start": "Start the machine when an environment needs it",
    "ssh_cidr": "SSH allowed from", "access_key_secret": "Access key secret (name)",
    "secret_key_secret": "Secret key secret (name)",
}


def _show(param: str, s: Ec2Settings) -> str:
    if param == "disk_gb":
        return f"{s.disk_gb} GB"
    if param == "max_lifetime_minutes":
        return duration(s.max_lifetime_minutes)
    if param == "idle_stop_minutes":
        return _idle_text(s.idle_stop_minutes)
    if param == "auto_start":
        return "yes" if s.auto_start else "no"
    if param == "ssh_cidr":
        return ssh_access_text(s)
    return str(getattr(s, param))


def _values_text(s: Ec2Settings, given: dict) -> str:
    return "; ".join(f"{_LABELS[p]}: {_show(p, s)}" + ("" if p in given else " (default)")
                     for p in EC2_PARAMS) + "."


def _changes_text(old: Ec2Settings, new: Ec2Settings, given: dict) -> str:
    parts = []
    for p in EC2_PARAMS:
        if p not in given:
            continue
        before, after = _show(p, old), _show(p, new)
        parts.append(f"{_LABELS[p]}: {before} -> {after}" if before != after
                     else f"{_LABELS[p]}: {after} (unchanged)")
    return "; ".join(parts) + "." if parts else "no fields."


def _costlier(old: Ec2Settings, new: Ec2Settings) -> list[str]:
    """What an update makes more expensive or longer-lived (prices are not compared)."""
    out = []
    if new.instance_type != old.instance_type:
        out.append(f"instance type {old.instance_type} -> {new.instance_type} (billed at that "
                   "type's AWS rate; Own Compute cannot compare prices)")
    if new.disk_gb > old.disk_gb:
        out.append(f"disk {old.disk_gb} GB -> {new.disk_gb} GB")
    if new.max_lifetime_minutes > old.max_lifetime_minutes:
        out.append(f"max lifetime {duration(old.max_lifetime_minutes)} -> "
                   f"{duration(new.max_lifetime_minutes)}")
    if new.idle_stop_minutes == 0 and old.idle_stop_minutes > 0:
        out.append("idle stop turned off (the machine then stops only at its time limit)")
    elif new.idle_stop_minutes > old.idle_stop_minutes > 0:
        out.append(f"idle stop {old.idle_stop_minutes} -> {new.idle_stop_minutes} minutes")
    if new.auto_start and not old.auto_start:
        out.append("auto-start turned on (spawning an environment can start the machine)")
    return out


def _update_effect(old: Ec2Settings, new: Ec2Settings) -> str:
    lines = []
    costlier = _costlier(old, new)
    if costlier:
        lines.append("MORE COSTLY OR LONGER-LIVED: " + "; ".join(costlier) + ".")
    if old.instance_id:
        lines.append(f"A machine exists ({old.instance_id}, {old.machine_state}). The idle rule "
                     "applies at once; a new max lifetime applies from the next start and "
                     "raises or lowers the Extend limit at once; the machine's own boot timer "
                     "gets it as soon as Hermes reaches the machine, and keeps the old one until "
                     "then; the machine keeps the type and disk it was launched with.")
    else:
        lines.append("No machine exists, so the new values apply when a machine is started.")
    if new.ssh_cidr != old.ssh_cidr:
        lines.append("The SSH rule in AWS follows the next time the machine is started.")
    if new.auto_start:
        lines.append("Auto-start is on. " + start_cost(new))
    return " ".join(lines)


# --------------------------------------------------------------------------- cards


def _label(host: HostSpec) -> str:
    return f"{host.name} ({host.id})"


def _lookup(services: Any, host_id: Any) -> HostSpec | None:
    from .models import is_valid_host_id
    if not isinstance(host_id, str) or not is_valid_host_id(host_id):
        return None
    return services.store.get_host(host_id)


def _new_name(args: dict, s: Ec2Settings) -> str:
    name = args.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else "AWS machine"


def describe_add(args: dict, services: Any) -> str:
    """Card for adding an EC2 host. Raises :class:`ApiError` when it cannot work."""
    from .approval import bounded_text
    from .models import slugify_host_id
    s = build_new(args, services)
    name = _new_name(args, s)
    if len(name) > _MAX_NAME or "\n" in name:
        raise _bad("name must be a single line of at most 100 characters")
    if services.store.get_host(slugify_host_id(name)) is not None:
        raise ApiError(409, "conflict", f"a host named {name!r} already exists; add it with a "
                       "different name or change the existing one")
    lookup = ("" if s.ssh_cidr else " It also looks up this computer's public address at "
              "checkip.amazonaws.com.")
    auto = (" Auto-start is on: spawning an environment on this host can start the machine "
            "later, and its card will state the cost. " + start_cost(s)) if s.auto_start else ""
    return (f"Adds {MARK} host {bounded_text(name, _MAX_NAME)} to Own Compute. After you "
            "approve, Own Compute checks the AWS account with the stored keys (read-only "
            "calls and a DryRun launch that AWS checks without starting anything) and saves "
            f"the host only if the checks pass.{lookup} {_values_text(s, _given(args))} "
            f"Starts billing now: no. {ADD_COST}{auto}")


def describe_update(args: dict, host: HostSpec, services: Any) -> str:
    old, new = build_update(args, host, services)
    given = _given(args)
    name = args.get("name")
    renamed = (f" Name: {host.name} -> {name.strip()}." if isinstance(name, str) and name.strip()
               and name.strip() != host.name else "")
    return (f"Changes {MARK} host {_label(host)} in Own Compute. {_changes_text(old, new, given)}"
            f"{renamed} Starts billing now: no. {update_cost(old)} {_update_effect(old, new)}")


def describe_remove(host: HostSpec, services: Any) -> str:
    s = host.ec2 if host.ec2 is not None else Ec2Settings()
    if s.instance_id:
        raise ApiError(409, "conflict", f"{host.name} still has an {MARK} machine "
                       f"({s.instance_id}). Terminate the machine first; AWS bills for it and "
                       "its disk until then.")
    pending = ""
    if s.launch_token:
        if _launch_in_flight(services, host):
            raise ApiError(409, "conflict", f"A start of {host.name} is in progress right now "
                           "(in this or another Hermes process). Try again in a minute, when "
                           "it has finished or its claim has expired.")
        pending = (" A launch of this host that never reached a machine is recorded: Cloud "
                   "Compute asks AWS first, discards the record if AWS has no such machine, and "
                   "refuses to remove the host if the launch did create one.")
    live = [r for r in services.store.list_instances(host.id) if r.status != "destroyed"]
    if live:
        raise ApiError(409, "conflict", f"{host.name} still has {len(live)} environment(s); "
                       "destroy them first (their files are saved).")
    return (f"Removes {MARK} host {_label(host)} from Own Compute. Own Compute then calls AWS "
            "with the stored keys to delete the key pair and the security group it created for "
            "this host, and deletes this host's private SSH key file on this computer. No "
            "machine exists for this host, so removing it starts nothing and ends no running "
            f"charge.{pending} Starts billing now: no.")


def _launch_in_flight(services: Any, host: HostSpec) -> bool:
    try:
        return bool(services.ec2.launch_in_flight(host.id))
    except Exception:  # noqa: BLE001 - unknown: do not claim a launch is running
        return False


def describe_test(args: dict, host: HostSpec | None, services: Any) -> str:
    """Card for a test that is not of a saved host exactly as saved."""
    if host is None:
        s = build_new(args, services)
        values = f" {_values_text(s, _given(args))}"
        subject = "an EC2 host that is not saved."
    else:
        old, s = build_update(args, host, services)
        values = f" {_changes_text(old, s, _given(args))}"
        subject = f"{MARK} host {_label(host)} with these values instead of the saved ones:"
    return (f"Tests {subject}{values} Nothing is saved and no machine is started. Own Compute "
            "makes read-only calls to AWS with the stored keys (the account, the region, the "
            "machine image) and a DryRun launch that AWS checks without starting anything, and "
            "looks up this computer's public address at checkip.amazonaws.com. Starts billing "
            "now: no.")


def _block(message: str) -> Any:
    from .approval import BlockedMessage
    return BlockedMessage(message)


def describe_machine(action: str, host: HostSpec, args: dict, now: datetime | None = None,
                     envs: list[str] | None = None) -> Any:
    """Card for start, stop, extend or terminate of the machine. A request that cannot work
    comes back as a blocked message (no card)."""
    from .approval import _when
    from .ec2 import EXTEND_LIMIT, extend_window, _minutes_text
    s = host.ec2 if host.ec2 is not None else Ec2Settings()
    state = s.machine_state or "none"
    label = _label(host)
    if action == "start":
        if state in ("launching", "starting", "ready"):
            return _block(f"The machine of {host.name} is already {state}; there is nothing to "
                          "start. compute_status shows when it stops by itself.")
        if state in ("stopping", "terminating"):
            return _block(f"The machine of {host.name} is {state}. Try again in a minute.")
        if s.instance_id:
            first = (f"Starts the stopped {MARK} machine {s.instance_id} for {label}, as it was "
                     "launched. If AWS no longer has it, a new machine with the same values is "
                     "created instead.")
        else:
            first = f"Launches a new {MARK} machine for {label}."
        return (f"{first} {start_marker(s)} {facts(s, True)} {start_cost(s)}"
                + _timer_gap(s))
    if action == "stop":
        if not s.instance_id or state in ("none", "stopped", "stopping"):
            return _block(f"The machine of {host.name} is {state}; there is nothing to stop.")
        return (f"Stops the {MARK} machine of {label}. Its environments stop; their containers "
                f"and files stay on the disk. {facts(s, False)} {STOP_COST}")
    if action == "extend":
        minutes = args.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
            return _block("minutes must be a whole number from 1 to 1440.")
        if state != "ready":
            return _block(f"The machine of {host.name} is {state}; only a running machine can "
                          "be extended.")
        current, new, latest = extend_window(s, minutes, now or datetime.now(timezone.utc))
        if new > latest:
            return _block(EXTEND_LIMIT.format(
                latest=latest.strftime("%Y-%m-%d %H:%M UTC"),
                lifetime=_minutes_text(int(s.max_lifetime_minutes))))
        return (f"Extends the {MARK} machine of {label} by {duration(minutes)}. It now stops "
                f"itself at {_when(current)}; after this it stops itself at {_when(new)} (at "
                f"most {_when(latest)} is allowed: twice the max lifetime from the last start). "
                f"{facts(s, False)} The machine keeps being billed until then: AWS bills the "
                "account while it runs.")
    if action == "terminate":
        if not s.instance_id and s.launch_token:
            return (f"Looks up the unfinished launch of the {MARK} machine of {label} in AWS. If "
                    "AWS created a machine, it is terminated (the instance and its disk are "
                    "deleted; this cannot be undone); if not, the record of the launch is "
                    f"discarded. {facts(s, False)} {TERMINATE_COST}")
        if not s.instance_id:
            return _block(f"{host.name} has no machine to terminate.")
        delete = args.get("delete_environments") is True
        save = args.get("save", True) is not False
        tail = ""
        names = ", ".join(envs[:8]) if envs else ""
        listed = f" ({names})" if names else ""
        if delete and save and envs and state != "ready":
            return _block(f"The machine of {host.name} is {state}, so the files of its "
                          f"environments{listed} cannot be saved. Saving needs the machine "
                          "running. Ask the user which they want: start the machine (AWS bills "
                          "while it runs) and then terminate with the files saved, or terminate "
                          "without saving (save=false), which deletes those files.")
        if delete and save:
            tail = " Every environment on it is deleted first, with its files saved to this computer."
        elif delete:
            tail = (f" Every environment on it{listed} is deleted too, WITHOUT saving its files: "
                    "the files are NOT saved and are lost for good. The machine is not "
                    "contacted, so this works even while it is stopped.")
        return (f"Terminates the {MARK} machine of {label}. AWS deletes the instance and its "
                f"disk; this cannot be undone.{tail} {facts(s, False)} {TERMINATE_COST}")
    return f"{action.capitalize()} the {MARK} machine of {label}?"


def spawn_text(host: HostSpec, s: Ec2Settings) -> str:
    """The EC2 part of an ``env_spawn`` card: where it runs, and the cost when the spawn starts
    a machine that is not running."""
    if s.machine_state == "ready":
        return (f" It runs on the {MARK} machine of {host.name}, which is already running. "
                f"{facts(s, False)} {running_cost(s)}")
    return (f" It runs on the {MARK} machine of {host.name}. The machine is not running and "
            f"will be started first. {start_marker(s)} {facts(s, True)} {start_cost(s)}"
            + _timer_gap(s))
