"""Routes and the ``env_files`` chat tool for file transfer (see :mod:`.files`).

Routes:

- ``GET /instances/{instance_id}/files?path=``: list a folder (running only).
- ``GET /instances/{instance_id}/files/size?path=``: ``{path, known, bytes, files, reason}``.
- ``POST /instances/{instance_id}/files/fetch`` ``{path, dest_dir, extract}``: save a folder
  to this computer; returns the ExportRecord. No files: 409 ``nothing_to_save``.
- ``POST /instances/{instance_id}/files/put`` ``{local_path, dest, extract}``: upload.
- ``GET /exports?instance_id=``: ``{exports: [...], folder: "<abs exports dir>"}``.
- ``GET /exports/{export_id}``: one ExportRecord (the saved-copy card reads it).
- ``POST /exports/{export_id}/restore`` ``{instance_id, dest}``: unpack a saved export.
- ``DELETE /exports/{export_id}``: ``{deleted: true}``, plus ``message`` when an unpacked
  folder was left in place.

Errors use the standard ``{"error": {"code", "message"}}`` shape. Tool handlers never raise.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from .files import LARGE_SAVE_BYTES, clean_path_text, file_count, human_size
from .interfaces import ApiError, ApiRequest, BackgroundTask, Route, Services, ToolSpec

__all__ = ["routes", "tools", "background_tasks", "env_files", "approval_env_files",
           "TOOL_ACTIONS", "ACTION_PARAMS", "copy_card", "fetch_rule_part"]

log = logging.getLogger(__name__)

TOOL_ACTIONS = ("list", "size", "fetch", "put", "exports", "restore", "delete_export")
GENERIC_APPROVAL = "Change files in an Own Compute environment or on this computer?"
#: Parameters each action takes (besides ``action``); anything else is ``unknown_parameter``.
ACTION_PARAMS: dict[str, tuple[str, ...]] = {
    "list": ("instance_id", "path"),
    "size": ("instance_id", "path"),
    "fetch": ("instance_id", "path", "dest_dir", "extract"),
    "put": ("instance_id", "local_path", "dest", "extract"),
    "exports": ("instance_id",),
    "restore": ("export_id", "instance_id", "dest"),
    "delete_export": ("export_id",),
}


# --------------------------------------------------------------------------- helpers


def _bad(message: str) -> ApiError:
    return ApiError(400, "invalid", message)


def _body(req: ApiRequest) -> dict:
    if req.body is None:
        return {}
    if not isinstance(req.body, dict):
        raise _bad("request body must be a JSON object")
    return req.body


def _param(req: ApiRequest, key: str) -> str:
    return str((req.path_params or {}).get(key, ""))


def _opt_str(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _bad(f"{key} must be a string")
    return value


def _req_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{key} is required")
    return value.strip()


def _flag(data: dict, key: str) -> bool:
    value = data.get(key, False)
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes"):
        return True
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "0", "false",
                                                                             "no")):
        return False
    if not isinstance(value, bool):
        raise _bad(f"{key} must be true or false")
    return value


def copy_card(export_id: Any) -> str:
    """The chat card directive of one saved copy."""
    return f'::cc-copy{{id="{export_id}"}}'


def _exports(services: Any, instance_id: str | None) -> dict:
    files = services.files
    return {"exports": files.list_exports(instance_id), "folder": str(files.exports_dir())}


# --------------------------------------------------------------------------- routes


def list_files(req: ApiRequest) -> dict:
    return req.services.files.list_dir(_param(req, "instance_id"),
                                       _opt_str(req.query or {}, "path"))


def size_files(req: ApiRequest) -> dict:
    return req.services.files.size(_param(req, "instance_id"), _opt_str(req.query or {}, "path"))


def fetch_files(req: ApiRequest) -> dict:
    body = _body(req)
    return req.services.files.fetch(_param(req, "instance_id"), _opt_str(body, "path"),
                                    dest_dir=_opt_str(body, "dest_dir"),
                                    extract=_flag(body, "extract"))


def put_files(req: ApiRequest) -> dict:
    body = _body(req)
    return req.services.files.put(_param(req, "instance_id"), _req_str(body, "local_path"),
                                  _opt_str(body, "dest") or "", extract=_flag(body, "extract"))


def list_exports(req: ApiRequest) -> dict:
    instance_id = str((req.query or {}).get("instance_id", "")).strip() or None
    return _exports(req.services, instance_id)


def get_export(req: ApiRequest) -> dict:
    return req.services.files.get_export(_param(req, "export_id"))


def restore_export(req: ApiRequest) -> dict:
    body = _body(req)
    return req.services.files.restore(_param(req, "export_id"), _req_str(body, "instance_id"),
                                      _opt_str(body, "dest") or "/work")


def delete_export(req: ApiRequest) -> dict:
    return req.services.files.delete_export(_param(req, "export_id"))


def routes() -> list[Route]:
    return [
        Route("GET", "/instances/{instance_id}/files", list_files,
              "List a folder in a running environment"),
        Route("GET", "/instances/{instance_id}/files/size", size_files,
              "Size and file count of a folder in a running environment"),
        Route("POST", "/instances/{instance_id}/files/fetch", fetch_files,
              "Save a folder of an environment to this computer"),
        Route("POST", "/instances/{instance_id}/files/put", put_files,
              "Upload a file or folder from this computer into an environment"),
        Route("GET", "/exports", list_exports, "Saved exports and their folder on this computer"),
        Route("GET", "/exports/{export_id}", get_export, "One saved export"),
        Route("POST", "/exports/{export_id}/restore", restore_export,
              "Unpack a saved export into an environment"),
        Route("DELETE", "/exports/{export_id}", delete_export, "Delete a saved export"),
    ]


# --------------------------------------------------------------------------- tool


def _error(exc: BaseException) -> dict:
    if isinstance(exc, ApiError):
        return exc.to_dict()
    log.warning("cloud-compute: env_files failed: %s", type(exc).__name__)
    return {"error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}}


def _check_params(action: str, args: dict) -> None:
    valid = ACTION_PARAMS[action]
    for key in args:
        if key == "action" or key in valid or args[key] is None or args[key] == "":
            continue
        raise ApiError(400, "unknown_parameter",
                       f"Unknown parameter '{key}' for env_files {action}. Valid parameters: "
                       f"{', '.join(('action',) + valid)}.")


def _size_message(out: dict, name: str) -> str:
    if out.get("known"):
        return (f"{out['path']} in {name} holds {file_count(out['files'])} "
                f"({human_size(out['bytes'])}).")
    return str(out.get("reason") or "The size is not known.")


def _name(services: Any, instance_id: Any) -> str:
    try:
        rec = services.store.get_instance(str(instance_id))
        return rec.name if rec is not None else str(instance_id)
    except Exception:  # noqa: BLE001 - the id names it
        return str(instance_id)


def env_files(args: dict, services: Services, ctx: dict | None = None) -> dict:
    try:
        if not isinstance(args, dict):
            raise _bad("arguments must be an object")
        action = args.get("action")
        if action not in TOOL_ACTIONS:
            raise _bad(f"action must be one of: {', '.join(TOOL_ACTIONS)}")
        _check_params(action, args)
        files = services.files
        if action == "exports":
            return _exports(services, _opt_str(args, "instance_id"))
        if action == "delete_export":
            return files.delete_export(_req_str(args, "export_id"))
        if action == "restore":
            export_id = _req_str(args, "export_id")
            out = files.restore(export_id, _req_str(args, "instance_id"),
                                _opt_str(args, "dest") or "")
            return {**out, "card": copy_card(out.get("export_id", export_id))}
        instance_id = _req_str(args, "instance_id")
        if action in ("list", "fetch"):
            guard = getattr(files, "refuse_outside_work", None)
            if callable(guard):           # a cloud agent's sandbox: its work folder only
                guard(instance_id, _opt_str(args, "path"),
                      "listed" if action == "list" else "saved")
        if action == "list":
            return files.list_dir(instance_id, _opt_str(args, "path"))
        if action == "size":
            out = files.size(instance_id, _opt_str(args, "path"))
            return {**out, "message": _size_message(out, _name(services, instance_id))}
        if action == "fetch":
            rec = files.fetch(instance_id, _opt_str(args, "path"),
                              dest_dir=_opt_str(args, "dest_dir"),
                              extract=_flag(args, "extract"))
            message = (f"Saved {rec.get('source_path')} from {rec.get('instance_name')} to "
                       f"{rec.get('local_path')} ({file_count(rec.get('files'))}, "
                       f"{human_size(rec.get('bytes'))} compressed).")
            if rec.get("extracted_to"):
                message += f" Unpacked into {rec['extracted_to']}."
            if rec.get("note"):
                message += " " + str(rec["note"])
            return {"export": rec, "card": copy_card(rec.get("id")), "message": message}
        return files.put(instance_id, _req_str(args, "local_path"), _opt_str(args, "dest") or "",
                         extract=_flag(args, "extract"))
    except Exception as exc:  # noqa: BLE001 - tools report, never raise
        return _error(exc)


def _approved_flag(value: Any) -> bool:
    """The yes/no meaning approvals use (the same in the normal and the fallback hook)."""
    from .approval import _flag as approval_flag
    return approval_flag(value)


def fetch_rule_part(args: dict) -> str:
    """Approval-key part of one fetch: its path, destination and unpack choice (hashed), so
    approving one save never approves a save to another folder."""
    args = args if isinstance(args, dict) else {}
    text = "|".join([str(clean_path_text(args.get("path")) or ""),
                     str(clean_path_text(args.get("dest_dir"), local=True) or ""),
                     "x" if _approved_flag(args.get("extract")) else ""])
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _fetch_approval(args: dict, services: Services) -> str | None:
    """Ask before a chat save to a chosen folder, or of a known size above 200 MB."""
    instance_id = args.get("instance_id")
    dest_dir = clean_path_text(args.get("dest_dir"), local=True) or None
    extract = _approved_flag(args.get("extract"))
    try:
        info = services.files.size(str(instance_id), clean_path_text(args.get("path")) or None)
    except Exception:  # noqa: BLE001 - the save itself reports a bad path or environment
        info = None
    known = bool(info and info.get("known"))
    if not dest_dir and not extract and not (known and int(info["bytes"]) > LARGE_SAVE_BYTES):
        return None
    name = _name(services, instance_id)
    path = (info or {}).get("path") or clean_path_text(args.get("path")) or "the work folder"
    size = f" ({human_size(info['bytes'])}, {file_count(info['files'])})" if known else \
        " (size not known)"
    where = f"{dest_dir} on this computer" if dest_dir else "the exports folder on this computer"
    text = f"Saves {path}{size} from {name} to {where}"
    text += ", and unpacks it into a new folder there." if extract else "."
    if not known and info and info.get("reason"):
        text += f" {info['reason']}"
    return text


def approval_env_files(args: dict, services: Services) -> str | None:
    """Approval text for put, restore, delete_export and large or directed fetches; None for
    read-only actions.

    The wording of put, restore and delete_export lives in :func:`approval.describe_env_files`.
    An unknown action or any failure fails closed to a generic message.
    """
    try:
        args = args if isinstance(args, dict) else {}
        action = args.get("action")
        if action in ("list", "size", "exports"):
            return None
        if action == "fetch":
            return _fetch_approval(args, services)
        if action not in ("put", "restore", "delete_export"):
            return GENERIC_APPROVAL
        from .approval import describe_env_files
        text = describe_env_files(args, services) or GENERIC_APPROVAL
        if action == "delete_export":
            text += _unpacked_note(args.get("export_id"), services)
        return text
    except Exception:  # noqa: BLE001 - fail closed
        return GENERIC_APPROVAL


def _unpacked_note(export_id: Any, services: Services) -> str:
    try:
        from pathlib import Path
        data = services.store.get_record("export", str(export_id)) or {}
        folder = data.get("extracted_to")
        if folder and Path(folder).is_dir():
            return f" The unpacked folder {folder} is left in place."
    except Exception:  # noqa: BLE001 - the base text stands
        pass
    return ""


_SCHEMA = {
    "name": "env_files",
    "description": (
        "Files between an environment and this computer. list(instance_id, path): entries. "
        "size(instance_id, path): bytes and file count. fetch(instance_id, path, dest_dir, "
        "extract): saves a .tar.gz copy; no files gives nothing_to_save. put(instance_id, "
        "local_path, dest, extract): upload; files in the Hermes home or SSH folder are refused. exports: "
        "saved copies. restore(export_id, instance_id, dest). delete_export(export_id)."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(TOOL_ACTIONS)},
            "instance_id": {"type": "string"},
            "path": {"type": "string", "description": "Folder in the environment; default workdir."},
            "local_path": {"type": "string", "description": "Absolute path on this computer (put)."},
            "dest": {"type": "string", "description": "Folder in the environment (put, restore)."},
            "dest_dir": {"type": "string",
                         "description": "Existing folder on this computer for the copy (fetch)."},
            "extract": {"type": "boolean",
                        "description": "put: unpack a .tar/.tar.gz. fetch: also unpack the copy "
                                       "into a new folder."},
            "export_id": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def tools() -> list[ToolSpec]:
    return [ToolSpec(name="env_files", schema=_SCHEMA, handler=env_files,
                     approval=approval_env_files)]


def background_tasks() -> list[BackgroundTask]:
    return []
