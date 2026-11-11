"""Short, cached context line for chat sessions (``pre_llm_call``).

The hook reads ``store.snapshot()`` only (no SSH, no transports), injects a compact
summary on the first turn of a session and whenever the set of hosts, environments
and jobs changes, and otherwise stays silent. The last injected digest per session is
kept in the store key/value table under ``ctx:<session_id>``.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

__all__ = ["SYSTEM_PROMPT_SECTION", "CARD_HINT", "render_context", "context_for",
           "make_pre_llm_call"]

#: Added to every tool result that carries a ``card``: a directive only draws a card when it
#: stands alone on its line.
CARD_HINT = ("Put the card line on a line of its own, with an empty line before it and after "
             "it. Never put it in the middle of a sentence.")

log = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
                             "NODE_FAIL"})

SYSTEM_PROMPT_SECTION = (
    "## Own Compute\n"
    "You can run work on the user's remote machines through the cloud_compute tools.\n"
    "- Start with compute_status: it lists hosts, sandboxes (envs) and Slurm jobs, and gives "
    "the host ids the other tools need.\n"
    "- Run workloads only inside sandboxes (env_spawn, then env_exec) or as Slurm jobs "
    "(slurm_submit). Never run heavy work on a Slurm login node or directly on a host.\n"
    "- Spawning, submitting, destroying an env and cancelling a job need the user's approval; "
    "describe what you intend before calling.\n"
    "- Poll Slurm sparingly: check slurm_jobs once, then wait or do other work instead of "
    "looping.\n"
    "- A host that is offline or needs auth cannot be used; tell the user rather than retrying.\n"
    "- Give sandboxes a sensible TTL and idle timeout, and destroy them when done.\n"
    "- A card shows live details. When the user asks about one, copy the card field of the tool "
    "result onto a line of its own, with an empty line before and after (never inside a "
    "sentence, or it shows as plain text): ::cc-env{id=\"i-...\"} environment, "
    "::cc-job{id=\"j-...\"} Slurm job, ::cc-agent{id=\"a-...\"} cloud agent run, "
    "::cc-copy{id=\"x-...\"} saved copy of files, ::cc-host{id=\"<host id>\"} host.\n"
    "- EC2 hosts bill the user's AWS account while their machine runs: say the cost_state "
    "and the cost lines of the card. Never take an AWS key in chat.\n"
    "- Never print secret values; variables are passed to sandboxes by name."
)


def _get(d: Any, key: str, default: Any = None) -> Any:
    return d.get(key, default) if isinstance(d, dict) else default


def _num(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "?"
    return str(int(f)) if f == int(f) else f"{f:.1f}"


def _host_name(h: dict) -> str:
    spec = _get(h, "spec", {})
    return str(_get(spec, "id") or _get(spec, "name") or "?")


def _host_line(h: dict, instances: list[dict]) -> str:
    spec = _get(h, "spec", {})
    state = _get(h, "state") or {}
    kind = str(_get(spec, "kind", "?"))
    caps = _get(state, "capabilities") or {}
    res = _get(state, "resources") or {}
    hid = _host_name(h)
    runtime = _get(caps, "runtime")
    label = f"{kind}/{runtime}" if runtime and kind != "slurm" else kind
    parts = [label]
    if kind == "slurm":
        total, used = _get(res, "cpu_total"), _get(res, "cpu_used")
        if total is not None:
            try:
                parts.append(f"cpu: {_num(float(total) - float(used or 0))}/{_num(total)} CPUs idle")
            except (TypeError, ValueError):
                pass
        parts.append(f"gpus: {len(_get(res, 'gpus') or [])}")
    else:
        if _get(res, "cpu_total") is not None:
            parts.append(f"CPU {_num(_get(res, 'cpu_used'))}/{_num(_get(res, 'cpu_total'))}")
        mt, mu = _get(res, "mem_total_mb"), _get(res, "mem_used_mb")
        if mt:
            try:
                parts.append(f"RAM {float(mu or 0) / 1024:.1f}/{float(mt) / 1024:.1f} GB")
            except (TypeError, ValueError):
                pass
        envs = [i for i in instances if _get(i, "host_id") == hid]
        if envs:
            listed = ", ".join(f"{_get(i, 'id')} {_get(i, 'template_id') or _get(i, 'name') or ''}"
                               .strip() for i in envs)
            parts.append(f"{len(envs)} env{'s' if len(envs) != 1 else ''}: {listed}")
    return f"{hid} ({', '.join(parts)})"


def _job_text(j: dict) -> str:
    return f"{_get(j, 'id', '?')} {_get(j, 'state', 'UNKNOWN')}"


def _finished_text(j: dict) -> str:
    code = _get(j, "exit_code")
    return _job_text(j) + (f" ({code})" if code not in (None, "") else "")


def render_context(snapshot: dict, *, max_chars: int = 600,
                   finished: list[dict] | None = None) -> str:
    """Render ``snapshot`` as a compact text block of at most ``max_chars`` characters."""
    from .ec2_chat import calm_label
    snap = snapshot if isinstance(snapshot, dict) else {}
    hosts = [h for h in (snap.get("hosts") or []) if isinstance(h, dict)]
    instances = [i for i in (snap.get("instances") or []) if isinstance(i, dict)]
    jobs = [j for j in (snap.get("jobs") or []) if isinstance(j, dict)]
    if not hosts:
        body = "[cloud-compute] No hosts configured."
    else:
        online, offline, no_machine = [], [], []
        for h in hosts:
            status = _get(_get(h, "state") or {}, "status", "unknown")
            calm = calm_label(_get(h, "spec", {}), status)
            if calm:
                no_machine.append(f"{_host_name(h)} ({calm})")
            elif status in ("online", "degraded"):
                online.append(_host_line(h, instances))
            else:
                offline.append(f"{_host_name(h)} ({status})")
        active = [j for j in jobs if _get(j, "state") not in TERMINAL_STATES]
        body = f"[cloud-compute] Online: {'; '.join(online) or 'none'}. "
        body += f"Offline: {', '.join(offline) or 'none'}. "
        if no_machine:
            body += f"No machine running: {', '.join(no_machine)}. "
        body += f"Jobs: {', '.join(_job_text(j) for j in active) or 'none'}."
    if finished:
        body += f"\nFinished since last turn: {', '.join(_finished_text(j) for j in finished)}."
    body += "\nUse compute_status / env_* / slurm_* tools."
    limit = max(int(max_chars), 20)
    if len(body) > limit:
        body = body[: limit - 1].rstrip() + "…"
    return body


def _digest_parts(snapshot: dict) -> tuple[str, list[str]]:
    """(digest, ids of finished jobs) from the stable facts only (no resource numbers)."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    hosts = sorted(f"{_host_name(h)}:{_get(_get(h, 'state') or {}, 'status', 'unknown')}"
                   for h in snap.get("hosts") or [] if isinstance(h, dict))
    insts = sorted(f"{_get(i, 'id')}:{_get(i, 'status')}"
                   for i in snap.get("instances") or [] if isinstance(i, dict))
    jobs = sorted(f"{_get(j, 'id')}:{_get(j, 'state')}"
                  for j in snap.get("jobs") or [] if isinstance(j, dict))
    fin = [f"{_get(j, 'id')}:{_get(j, 'state')}" for j in snap.get("jobs") or []
           if isinstance(j, dict) and _get(j, "state") in TERMINAL_STATES]
    raw = json.dumps([hosts, insts, jobs], separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16], sorted(fin)


def _load_last(raw: str | None) -> tuple[str | None, set[str] | None]:
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
        return str(data.get("digest")), set(data.get("finished") or [])
    except Exception:  # noqa: BLE001 - old or foreign value: treat as unknown
        return raw, None


def context_for(services: Any, session_id: str | None, is_first_turn: bool) -> str | None:
    """Context text for this turn, or None when nothing changed (or the feature is off)."""
    config = services.config
    if not getattr(config, "chat_context", True):
        return None
    max_chars = int(getattr(config, "chat_context_max_chars", 600) or 600)
    store = services.store
    snapshot = store.snapshot()
    digest, fin_keys = _digest_parts(snapshot)
    key = f"ctx:{session_id}" if session_id else None
    last_digest, last_fin = _load_last(store.kv_get(key)) if key else (None, None)
    if not is_first_turn and last_digest == digest:
        return None
    seen = last_fin if (last_fin is not None and not is_first_turn) else None
    finished: list[dict] = []
    if seen is not None:
        for j in snapshot.get("jobs") or []:
            if isinstance(j, dict) and f"{_get(j, 'id')}:{_get(j, 'state')}" in fin_keys \
                    and f"{_get(j, 'id')}:{_get(j, 'state')}" not in seen:
                finished.append(j)
    text = render_context(snapshot, max_chars=max_chars, finished=finished)
    if key:
        store.kv_set(key, json.dumps({"digest": digest, "finished": fin_keys}))
    return text


def make_pre_llm_call(get_services: Callable[[], Any]) -> Callable[..., dict | None]:
    """Hermes ``pre_llm_call`` callback: ``{"context": text}`` or None; never raises."""

    def pre_llm_call(**kwargs: Any) -> dict | None:
        try:
            text = context_for(get_services(), kwargs.get("session_id"),
                               bool(kwargs.get("is_first_turn")))
            return {"context": text} if text else None
        except Exception as exc:  # noqa: BLE001 - a hook must never break the turn
            log.debug("cloud-compute chat context skipped: %s", exc)
            return None

    return pre_llm_call
