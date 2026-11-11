"""Own Compute: run sandboxed environments and Slurm jobs on compute you own or have an account on.

``register(ctx)`` wires the plugin into Hermes: the chat tools collected from the
feature modules, the ``pre_llm_call`` (environment context) and ``pre_tool_call``
(approval) hooks, a system-prompt section, the bundled skill and ``/compute``.

Registration is fast and offline: nothing here touches the network or opens the
database. Services are built lazily on first use by a tool, hook or command.

The first tool call or ``pre_llm_call`` also starts the background loop (idle and TTL
reaping, Slurm polling, agent polling), so a user who only chats, and never opens
the Desktop page, still gets spend control. The core package is loaded once as
``cloud_compute_core``, the same copy the dashboard API uses, so the tools and the
REST routes share one service container and one loop.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

PLUGIN_ID = "cloud-compute"
TOOLSET = "cloud_compute"
PLUGIN_DIR = Path(__file__).resolve().parent
CORE_PACKAGE = "cloud_compute_core"
SKILL_NAME = "guide"                         # shown as cloud-compute:guide
SKILL_PATH = PLUGIN_DIR / "skills" / SKILL_NAME / "SKILL.md"

#: Must equal ``provides_tools`` in plugin.yaml. A tool not listed here is never registered.
PROVIDES_TOOLS: tuple[str, ...] = (
    "compute_status", "env_spawn", "env_exec", "env_control", "env_open", "slurm_submit", "slurm_jobs",
    "cloud_agent_spawn", "cloud_agent_status", "cloud_agent_collect", "cloud_agent_stop", "env_files",
    "compute_host", "cloud_agent_message", "cloud_agent_rerun",
)
#: Must equal ``provides_hooks`` in plugin.yaml. Both are always registered.
PROVIDES_HOOKS: tuple[str, ...] = ("pre_llm_call", "pre_tool_call")

#: Context keys passed from Hermes' tool dispatch to our handlers (``session_id`` always present).
_CONTEXT_KEYS: tuple[str, ...] = ("session_id", "task_id", "tool_call_id", "turn_id", "user_task")

_MAX_ERROR_CHARS = 300

# Lazily built service container shared by tools, hooks and the command.
_SERVICES: Any = None
_services_lock = threading.Lock()

# name -> ToolSpec of the tools registered by the last register() call.
_TOOLS: dict[str, Any] = {}


_core_lock = threading.Lock()


def _load_core() -> Any:
    """Load ``core/`` as the top-level package ``cloud_compute_core`` (shared with the dashboard API)."""
    with _core_lock:
        existing = sys.modules.get(CORE_PACKAGE)
        if existing is not None:
            return existing
        core_dir = PLUGIN_DIR / "core"
        spec = importlib.util.spec_from_file_location(
            CORE_PACKAGE, core_dir / "__init__.py", submodule_search_locations=[str(core_dir)])
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {core_dir}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[CORE_PACKAGE] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(CORE_PACKAGE, None)
            raise
        return module


def _core_module(name: str) -> Any:
    """``cloud_compute_core.<name>`` (imports it)."""
    _load_core()
    return importlib.import_module(f"{CORE_PACKAGE}.{name}")


def _services() -> Any:
    """The current shared service container; resolved on every call, never cached.

    ``_SERVICES`` is only an injected override (tests). A container whose store was
    closed (the plugin was unloaded and loaded again) is replaced by a fresh one.
    """
    registry = _core_module("registry")
    services = _SERVICES
    if services is not None and not registry.is_closed(services):
        return services
    return registry.shared_services()


def _ensure_background() -> None:
    """Start the background loop if it is not running; cheap, idempotent, never raises."""
    try:
        _core_module("background").ensure_started(_services())
    except Exception as exc:  # noqa: BLE001 - a tool call or hook must not fail because of this
        logger.debug("cloud-compute: could not ensure the background loop: %s", exc)


def _tools() -> dict[str, Any]:
    return dict(_TOOLS)


def _bounded(text: str) -> str:
    return text if len(text) <= _MAX_ERROR_CHARS else text[: _MAX_ERROR_CHARS - 3] + "..."


def _error_json(code: str, message: str) -> str:
    return json.dumps({"error": {"code": code, "message": _bounded(str(message))}})


def _to_json(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - a tool result must always be a string
        logger.warning("cloud-compute: tool result is not JSON-serializable: %s", exc)
        return _error_json("internal", "tool result could not be encoded as JSON")


def _api_error_type() -> type | None:
    try:
        return _core_module("interfaces").ApiError
    except Exception:  # noqa: BLE001
        return None


def _has_card(value: Any, depth: int = 0) -> bool:
    if depth > 3:
        return False
    if isinstance(value, dict):
        if isinstance(value.get("card"), str) and value["card"].lstrip().startswith("::cc-"):
            return True
        return any(_has_card(v, depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_has_card(v, depth + 1) for v in value[:50])
    return False


def _with_card_hint(result: Any) -> Any:
    """A tool result that carries a card also says how to write it: alone on its line."""
    try:
        if isinstance(result, dict) and "card_hint" not in result and _has_card(result):
            hint = getattr(_core_module("chat_context"), "CARD_HINT", None)
            if hint:
                return {**result, "card_hint": hint}
    except Exception:  # noqa: BLE001 - the hint is optional
        pass
    return result


_CARD_KEYS = ("card", "copy_card")


def _pad_cards(value: Any, depth: int = 0) -> Any:
    """Put a blank line before and after every chat card directive in a tool result.

    Desktop draws a ``::cc-...{id="..."}`` directive only when it is a block of its own, and
    the plugin cannot change how Desktop reads the message text. A model that pastes the
    ``card`` field as it is then gets a block, whatever words it writes around it.
    """
    if depth > 4:
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _CARD_KEYS and isinstance(item, str) and item.strip().startswith("::cc-"):
                out[key] = "\n\n" + item.strip() + "\n\n"
            else:
                out[key] = _pad_cards(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_pad_cards(v, depth + 1) for v in value]
    return value


def _make_handler(spec: Any) -> Callable[..., str]:
    """Hermes tool handler for ``spec``: returns a JSON string and never raises."""
    api_error = _api_error_type()
    name = spec.name

    def handler(args: Any = None, **kwargs: Any) -> str:
        try:
            if args is None:
                args = {}
            if not isinstance(args, dict):
                return _error_json("invalid", "tool arguments must be a JSON object")
            ctx_kwargs = {key: kwargs.get(key) for key in _CONTEXT_KEYS}
            services = _services()
            _ensure_background()
            result = spec.handler(dict(args), services, ctx_kwargs)
        except Exception as exc:  # noqa: BLE001 - handlers never raise into Hermes
            if api_error is not None and isinstance(exc, api_error):
                return _error_json(getattr(exc, "code", "internal"), getattr(exc, "message", str(exc)))
            logger.warning("cloud-compute: tool %s failed: %s", name, exc, exc_info=True)
            return _error_json("internal", f"{type(exc).__name__}: {exc}")
        return _to_json(_pad_cards(_with_card_hint(result)))

    handler.__name__ = f"cloud_compute_{name}"
    handler.__qualname__ = handler.__name__
    return handler


# --- fail-closed approval used when the approval module is unavailable or errors ---

def _fallback_needs_approval(tool_name: Any, args: Any) -> bool:
    """Every state-changing tool asks; only known read-only actions pass (fail closed)."""
    if tool_name in ("env_spawn", "slurm_submit", "cloud_agent_spawn", "cloud_agent_stop",
                     "cloud_agent_message", "cloud_agent_rerun"):
        return True
    if tool_name == "env_open":
        # kind=port with a host_id forwards any loopback port of that machine to this computer
        return (isinstance(args, dict) and str(args.get("kind") or "").strip().lower() == "port"
                and bool(str(args.get("host_id") or "").strip()))
    if tool_name in ("env_control", "env_files", "slurm_jobs", "compute_host"):
        if not isinstance(args, dict):
            return True
        action = str(args.get("action") or "").strip().lower()
        if tool_name == "env_control":
            return action != "logs"
        if tool_name == "env_files":
            if action == "fetch":              # a save to a chosen folder, or one that unpacks
                return bool(str(args.get("dest_dir") or "").strip()) or _truthy(args.get("extract"))
            return action not in ("list", "exports")
        if tool_name == "compute_host":
            if action == "test":
                # Only a saved host given by host_id, with nothing else that decides where or
                # how to connect, runs at once (the same rule as hosts_api.needs_test_approval).
                host_id = args.get("host_id")
                if not isinstance(host_id, str) or not host_id.strip():
                    return True
                return any(v is not None for k, v in args.items()
                           if k not in ("action", "host_id", "kind", "name"))
            if args.get("exec_prefix") is not None or args.get("interactive_prefix") is not None:
                return True
            return action in ("add", "update", "remove", "start", "stop", "terminate", "extend")
        return action == "cancel"
    return False


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str)
                             and value.strip().lower() in ("true", "1", "yes", "on"))


def _fallback_rule_key(tool_name: Any, args: Any) -> str:
    """Unique per call: tool + hash of the sorted args, so a cached approval never
    covers a different action."""
    try:
        text = json.dumps(args, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        text = repr(args)
    if _starts_machine(tool_name, args):
        # The card that would tell the starts apart could not be built: no remembered answer
        # ("session" or "always") may cover a later start, so the key is new every time.
        text += "|" + uuid.uuid4().hex
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return f"cloud-compute:{tool_name}:{digest}"


def _starts_machine(tool_name: Any, args: Any) -> bool:
    """A call that can start (or keep) a machine that AWS bills: a Start, an Extend, or a
    spawn that may start the machine first."""
    if tool_name == "env_spawn":
        return True
    return (tool_name == "compute_host" and isinstance(args, dict)
            and str(args.get("action") or "").strip().lower() in ("start", "extend"))


def _fallback_approval(tool_name: Any, args: Any) -> dict | None:
    if not _fallback_needs_approval(tool_name, args):
        return None
    host_id = args.get("host_id") if isinstance(args, dict) else None
    where = f" on {host_id}" if isinstance(host_id, str) and host_id else ""
    cost = ""
    action = str(args.get("action") or "").strip().lower() if isinstance(args, dict) else ""
    if tool_name == "env_spawn" or (tool_name == "compute_host" and action in (
            "add", "update", "start", "extend", "stop", "terminate", "test")):
        # The full card could not be built: still say that an EC2 host costs money.
        cost = (" If the host is an EC2 host, this can start or keep an AWS machine that AWS "
                "bills to the account while it runs, and for its disk while the machine exists.")
    return {
        "action": "approve",
        "message": f"Allow the Own Compute tool {tool_name}{where}?{cost}",
        "rule_key": _fallback_rule_key(tool_name, args),
    }


def _fallback_pre_llm_call(**kwargs: Any) -> None:
    return None


def _fallback_pre_tool_call(**kwargs: Any) -> dict | None:
    return _fallback_approval(kwargs.get("tool_name"), kwargs.get("args"))


def _safe_pre_llm_call(inner: Callable[..., Any]) -> Callable[..., Any]:
    def pre_llm_call(**kwargs: Any) -> Any:
        try:
            return inner(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a hook must never break the turn
            logger.debug("cloud-compute: pre_llm_call failed: %s", exc)
            return None
        finally:
            _ensure_background()

    return pre_llm_call


def _safe_pre_tool_call(inner: Callable[..., Any]) -> Callable[..., Any]:
    def pre_tool_call(**kwargs: Any) -> Any:
        try:
            return inner(**kwargs)
        except Exception as exc:  # noqa: BLE001 - fail closed for our mutating tools
            logger.debug("cloud-compute: pre_tool_call failed: %s", exc)
            return _fallback_pre_tool_call(**kwargs)

    return pre_tool_call


# --- registration helpers ---

def _optional(name: str) -> Any:
    """Import ``.core.<name>``; ``None`` (logged) if it is absent or fails to import."""
    full = f"{CORE_PACKAGE}.{name}"
    try:
        return _core_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == full:
            logger.info("cloud-compute: %s is not installed; skipping", name)
        else:
            logger.warning("cloud-compute: %s failed to import: %s", name, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a broken module must not break registration
        logger.warning("cloud-compute: %s failed to import: %s", name, exc, exc_info=True)
        return None


def _skill_description(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return ""


def _register_tools(ctx: Any) -> list[str]:
    _TOOLS.clear()
    try:
        specs = _core_module("registry").collect_tools()
    except Exception as exc:  # noqa: BLE001
        logger.warning("cloud-compute: could not collect tools: %s", exc, exc_info=True)
        return []
    allowed = set(PROVIDES_TOOLS)
    for spec in specs:
        name = getattr(spec, "name", None)
        if name not in allowed:
            logger.warning("cloud-compute: tool %r is not declared in plugin.yaml; not registered", name)
            continue
        if name in _TOOLS:
            continue
        schema = spec.schema if isinstance(spec.schema, dict) else {}
        try:
            ctx.register_tool(
                name=name,
                toolset=getattr(spec, "toolset", None) or TOOLSET,
                schema=schema,
                handler=_make_handler(spec),
                description=str(schema.get("description") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cloud-compute: could not register tool %s: %s", name, exc)
            continue
        _TOOLS[name] = spec
    return list(_TOOLS)


def _register_hooks(ctx: Any) -> None:
    chat_context = _optional("chat_context")
    pre_llm: Callable[..., Any] = _fallback_pre_llm_call
    if chat_context is not None and callable(getattr(chat_context, "make_pre_llm_call", None)):
        try:
            pre_llm = _safe_pre_llm_call(chat_context.make_pre_llm_call(_services))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cloud-compute: chat context hook unavailable: %s", exc)
    approval = _optional("approval")
    pre_tool: Callable[..., Any] = _fallback_pre_tool_call
    if approval is not None and callable(getattr(approval, "make_pre_tool_call", None)):
        try:
            pre_tool = _safe_pre_tool_call(approval.make_pre_tool_call(_tools, _services))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cloud-compute: approval hook unavailable; using the fail-closed default: %s", exc)
    for hook, callback in (("pre_llm_call", pre_llm), ("pre_tool_call", pre_tool)):
        try:
            ctx.register_hook(hook, callback)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cloud-compute: could not register hook %s: %s", hook, exc)


def _register_prompt_section(ctx: Any) -> None:
    if not hasattr(ctx, "register_system_prompt_section"):
        logger.debug("cloud-compute: this host has no system-prompt sections; skipping")
        return
    chat_context = _optional("chat_context")
    content = getattr(chat_context, "SYSTEM_PROMPT_SECTION", None) if chat_context else None
    if not isinstance(content, str) or not content.strip():
        return
    try:
        ctx.register_system_prompt_section(PLUGIN_ID, content, max_chars=1500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cloud-compute: could not register the system-prompt section: %s", exc)


def _register_skill(ctx: Any) -> None:
    if not hasattr(ctx, "register_skill"):
        logger.debug("cloud-compute: this host has no plugin skills; skipping")
        return
    if not SKILL_PATH.is_file():
        logger.info("cloud-compute: bundled skill not found; skipping")
        return
    try:
        ctx.register_skill(SKILL_NAME, SKILL_PATH, description=_skill_description(SKILL_PATH))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cloud-compute: could not register the skill: %s", exc)


def _register_command(ctx: Any) -> None:
    if not hasattr(ctx, "register_command"):
        logger.debug("cloud-compute: this host has no plugin commands; skipping /compute")
        return
    commands = _optional("commands")
    if commands is None or not callable(getattr(commands, "make_compute_command", None)):
        return
    try:
        handler = commands.make_compute_command(_services)
        name = getattr(commands, "COMMAND_NAME", "compute")
        description = "Summary of compute hosts, environments and Slurm jobs"
        ctx.register_command(name, handler, description=description,
                             args_hint=getattr(commands, "ARGS_HINT", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cloud-compute: could not register /compute: %s", exc)


def _register_redaction(ctx: Any) -> None:
    """Hand secret-shaped token patterns to hosts that support it; never raises."""
    if not hasattr(ctx, "register_redaction_patterns"):
        return
    try:
        ctx.register_redaction_patterns(list(_core_module("redact").HOST_PATTERN_SOURCES))
    except Exception:
        logger.debug("cloud-compute: redaction pattern registration failed", exc_info=True)


def _cleanup() -> None:
    """Unload callback: stop the background loop and close the store. Idempotent; never raises."""
    global _SERVICES
    if CORE_PACKAGE not in sys.modules:
        return                               # core was never loaded: nothing to stop or close
    try:
        _core_module("background").shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.debug("cloud-compute: could not stop the background loop: %s", exc)
    with _services_lock:
        injected, _SERVICES = _SERVICES, None
    try:
        registry = _core_module("registry")
    except Exception as exc:  # noqa: BLE001
        logger.debug("cloud-compute: could not close the store: %s", exc)
        return
    shared = getattr(registry, "_shared", None)
    closing = [injected] if injected is not None else []
    if shared is not None and shared is not injected:
        closing.append(shared)
    for services in closing:
        try:
            if "store" in services.built() and not registry.is_closed(services):
                services.store.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("cloud-compute: could not close the store: %s", exc)
    try:
        if shared is not None:
            registry.reset_shared_services()     # the next call builds a fresh container
    except Exception as exc:  # noqa: BLE001
        logger.debug("cloud-compute: could not reset the shared services: %s", exc)


def _register_unload(ctx: Any) -> None:
    if not hasattr(ctx, "on_unload"):
        logger.debug("cloud-compute: this host has no unload callbacks; skipping")
        return
    try:
        ctx.on_unload(_cleanup)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cloud-compute: could not register the unload callback: %s", exc)


def register(ctx: Any) -> None:
    """Hermes plugin entry point. Fast and offline; services are built on first use."""
    started = time.monotonic()
    tools = _register_tools(ctx)
    _register_hooks(ctx)
    _register_prompt_section(ctx)
    _register_skill(ctx)
    _register_command(ctx)
    _register_redaction(ctx)
    _register_unload(ctx)
    logger.debug("cloud-compute: registered %d tool(s) in %.0f ms: %s",
                 len(tools), (time.monotonic() - started) * 1000, ", ".join(tools) or "none")
