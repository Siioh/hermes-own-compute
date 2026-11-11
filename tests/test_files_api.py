"""files_api: routes, the env_files tool (never raises) and its approval text."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cloud_compute_core import files_api as api
from cloud_compute_core import registry
from cloud_compute_core.interfaces import ApiError, ApiRequest, Services
from cloud_compute_core.testing import sample_host, sample_instance


class FakeFiles:
    def __init__(self):
        self.calls = []
        self.fetched = {"id": "x-1", "source_path": "/work", "instance_name": "python-dev-1",
                        "local_path": "C:/data/exports/a.tar.gz", "bytes": 2048, "files": 3,
                        "extracted_to": None}
        self.sized = {"path": "/work", "known": True, "bytes": 5 * 1024 * 1024, "files": 1204,
                      "reason": None}

    def _rec(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        return {"called": name}

    def list_dir(self, instance_id, path=None):
        return self._rec("list_dir", instance_id, path)

    def fetch(self, instance_id, path=None, *, dest_dir=None, extract=False):
        self._rec("fetch", instance_id, path, dest_dir=dest_dir, extract=extract)
        return dict(self.fetched)

    def size(self, instance_id, path=None):
        self._rec("size", instance_id, path)
        return dict(self.sized)

    def get_export(self, export_id):
        self._rec("get_export", export_id)
        return {"id": export_id}

    def put(self, instance_id, local_path, dest, *, extract=False):
        return self._rec("put", instance_id, local_path, dest, extract=extract)

    def restore(self, export_id, instance_id, dest="/work"):
        return self._rec("restore", export_id, instance_id, dest)

    def delete_export(self, export_id):
        self._rec("delete_export", export_id)
        return {"deleted": True}

    def list_exports(self, instance_id=None):
        self._rec("list_exports", instance_id)
        return [{"id": "x-1"}]

    def exports_dir(self):
        return "C:/data/exports"


@pytest.fixture
def svc(store):
    store.upsert_host(sample_host())
    store.upsert_instance(sample_instance())
    files = FakeFiles()
    services = Services(store=store, files=files, config=SimpleNamespace(approval="always"))
    return SimpleNamespace(services=services, files=files)


def _req(svc, path=None, query=None, body=None):
    return ApiRequest(path_params=path or {}, query=query or {}, body=body,
                      services=svc.services)


def _route(method, path):
    return next(r for r in api.routes() if (r.method, r.path) == (method, path))


def test_routes_are_declared():
    got = {(r.method, r.path) for r in api.routes()}
    assert got == {
        ("GET", "/instances/{instance_id}/files"),
        ("GET", "/instances/{instance_id}/files/size"),
        ("GET", "/exports/{export_id}"),
        ("POST", "/instances/{instance_id}/files/fetch"),
        ("POST", "/instances/{instance_id}/files/put"),
        ("GET", "/exports"),
        ("POST", "/exports/{export_id}/restore"),
        ("DELETE", "/exports/{export_id}"),
    }
    assert all(r.summary for r in api.routes())
    collected = {(r.method, r.path) for r in registry.collect_routes()}
    assert got <= collected


def test_route_handlers_call_the_service(svc):
    iid = {"instance_id": "i-ab12cd34"}
    _route("GET", "/instances/{instance_id}/files").handler(_req(svc, iid, {"path": "/work"}))
    _route("POST", "/instances/{instance_id}/files/fetch").handler(
        _req(svc, iid, body={"path": "/work/results"}))
    _route("POST", "/instances/{instance_id}/files/put").handler(
        _req(svc, iid, body={"local_path": "C:/x.txt", "dest": "/work", "extract": True}))
    _route("POST", "/exports/{export_id}/restore").handler(
        _req(svc, {"export_id": "x-1"}, body={"instance_id": "i-ab12cd34"}))
    assert _route("DELETE", "/exports/{export_id}").handler(
        _req(svc, {"export_id": "x-1"})) == {"deleted": True}
    out = _route("GET", "/exports").handler(_req(svc, query={"instance_id": "i-ab12cd34"}))
    assert out == {"exports": [{"id": "x-1"}], "folder": "C:/data/exports"}
    assert [c[0] for c in svc.files.calls] == [
        "list_dir", "fetch", "put", "restore", "delete_export", "list_exports"]
    assert svc.files.calls[1] == ("fetch", ("i-ab12cd34", "/work/results"),
                                  {"dest_dir": None, "extract": False})
    assert svc.files.calls[0][1] == ("i-ab12cd34", "/work")
    assert svc.files.calls[2] == ("put", ("i-ab12cd34", "C:/x.txt", "/work"), {"extract": True})
    assert svc.files.calls[3][1] == ("x-1", "i-ab12cd34", "/work")
    assert svc.files.calls[5][1] == ("i-ab12cd34",)


def test_route_validation(svc):
    with pytest.raises(ApiError) as info:
        _route("POST", "/instances/{instance_id}/files/put").handler(
            _req(svc, {"instance_id": "i"}, body={"dest": "/work"}))
    assert info.value.code == "invalid"
    with pytest.raises(ApiError):
        _route("POST", "/exports/{export_id}/restore").handler(
            _req(svc, {"export_id": "x-1"}, body={}))
    with pytest.raises(ApiError):
        _route("POST", "/instances/{instance_id}/files/fetch").handler(
            _req(svc, {"instance_id": "i"}, body={"path": 5}))


def test_errors_use_the_standard_shape(store, tmp_path):
    services = registry.build_services(store=store)
    out = api.env_files({"action": "list", "instance_id": "i-missing"}, services, {})
    assert out == {"error": {"code": "not_found",
                             "message": "Environment i-missing was not found."}}


# --------------------------------------------------------------------------- tool


def test_tool_spec_is_concise_and_registered():
    (spec,) = api.tools()
    assert spec.name == "env_files" and spec.toolset == "cloud_compute"
    assert spec.approval is api.approval_env_files
    params = spec.schema["parameters"]
    assert params["properties"]["action"]["enum"] == list(api.TOOL_ACTIONS)
    assert params["required"] == ["action"] and params["additionalProperties"] is False
    assert len(spec.schema["description"]) < 450
    assert "env_files" in [t.name for t in registry.collect_tools()]


@pytest.mark.parametrize("args,called", [
    ({"action": "list", "instance_id": "i-1"}, "list_dir"),
    ({"action": "fetch", "instance_id": "i-1", "path": "/work"}, "fetch"),
    ({"action": "put", "instance_id": "i-1", "local_path": "C:/a"}, "put"),
    ({"action": "exports"}, "list_exports"),
    ({"action": "restore", "export_id": "x-1", "instance_id": "i-1"}, "restore"),
    ({"action": "delete_export", "export_id": "x-1"}, "delete_export"),
])
def test_tool_actions(svc, args, called):
    out = api.env_files(args, svc.services, {"session_id": "s"})
    assert "error" not in out
    assert svc.files.calls[-1][0] == called
    if called == "fetch":
        assert out["export"]["id"] == "x-1" and out["card"] == '::cc-copy{id="x-1"}'
        assert out["message"] == ("Saved /work from python-dev-1 to C:/data/exports/a.tar.gz "
                                  "(3 files, 2.0 KB compressed).")
    if called == "list_exports":
        assert out["folder"] == "C:/data/exports"


@pytest.mark.parametrize("args", [
    None, "nope", {}, {"action": "explode"}, {"action": "list"},
    {"action": "put", "instance_id": "i-1"}, {"action": "restore", "export_id": "x"},
    {"action": "delete_export"}, {"action": "put", "instance_id": "i", "local_path": "a",
                                  "extract": "maybe?"},
    {"action": "put", "instance_id": "i", "local_path": "a", "extract": 3},
])
def test_tool_never_raises_on_bad_args(svc, args):
    out = api.env_files(args, svc.services, {})
    assert "error" in out and out["error"]["code"] in ("invalid", "internal",
                                                       "unknown_parameter")


def test_tool_never_raises_on_service_failure():
    class Boom:
        @property
        def files(self):
            raise RuntimeError("kaput")

    out = api.env_files({"action": "exports"}, Boom(), {})
    assert out["error"]["code"] == "internal"
    out = api.env_files({"action": "exports"}, Services(), {})
    assert out["error"]["code"] == "unavailable"


# --------------------------------------------------------------------------- approval


def test_approval_only_for_changing_actions(svc):
    s = svc.services
    for action in ("list", "fetch", "exports", "size"):
        assert api.approval_env_files({"action": action, "instance_id": "i-ab12cd34"}, s) is None
    put = api.approval_env_files({"action": "put", "instance_id": "i-ab12cd34",
                                  "local_path": "C:/data/in.csv", "dest": "/work/in"}, s)
    assert put == ("Uploads C:/data/in.csv from this computer to python-dev-1:/work/in. "
                   "Files with the same names are replaced.")
    restore = api.approval_env_files({"action": "restore", "export_id": "x-1", "dest": "/work",
                                      "instance_id": "i-ab12cd34"}, s)
    assert restore == ("Restores the saved copy x-1 into python-dev-1:/work. "
                       "Files with the same names are replaced.")
    delete = api.approval_env_files({"action": "delete_export", "export_id": "x-1"}, s)
    assert delete == "Deletes the saved copy x-1 from this computer. It cannot be recovered."
    for text in (put, restore, delete):
        words = text.lower().replace("?", " ").replace(".", " ").split()
        assert "we" not in words and "i" not in words and "pc" not in words


def test_approval_fails_closed(svc):
    assert api.approval_env_files({"action": "weird"}, svc.services) == api.GENERIC_APPROVAL
    assert api.approval_env_files("junk", None) == api.GENERIC_APPROVAL


def test_pre_tool_call_gates_put_but_not_list(svc):
    from cloud_compute_core.approval import approval_for

    (spec,) = api.tools()
    s = svc.services
    assert approval_for("env_files", {"action": "list", "instance_id": "i-ab12cd34"}, spec, s) \
        is None
    out = approval_for("env_files", {"action": "put", "instance_id": "i-ab12cd34",
                                     "local_path": "C:/a"}, spec, s)
    assert out["action"] == "approve" and out["rule_key"].startswith(
        "cloud-compute:env_files:put:i-ab12cd34:")


# --------------------------------------------------------------------------- saves and unpacking


def test_new_routes_call_the_service(svc):
    iid = {"instance_id": "i-ab12cd34"}
    out = _route("GET", "/instances/{instance_id}/files/size").handler(
        _req(svc, iid, {"path": "/work"}))
    assert out == svc.files.sized
    assert _route("GET", "/exports/{export_id}").handler(
        _req(svc, {"export_id": "x-9"})) == {"id": "x-9"}
    _route("POST", "/instances/{instance_id}/files/fetch").handler(
        _req(svc, iid, body={"path": "/work", "dest_dir": "C:/proj", "extract": True}))
    assert svc.files.calls[-1] == ("fetch", ("i-ab12cd34", "/work"),
                                   {"dest_dir": "C:/proj", "extract": True})


def test_unknown_parameter_is_named_with_the_valid_ones(svc):
    out = api.env_files({"action": "fetch", "instance_id": "i-1", "local_path": "C:/a"},
                        svc.services, {})
    assert out["error"]["code"] == "unknown_parameter"
    assert out["error"]["message"] == ("Unknown parameter 'local_path' for env_files fetch. "
                                       "Valid parameters: action, instance_id, path, dest_dir, "
                                       "extract.")
    out = api.env_files({"action": "list", "instance_id": "i-1", "bogus": 1}, svc.services, {})
    assert out["error"]["code"] == "unknown_parameter" and "'bogus'" in out["error"]["message"]
    assert svc.files.calls == []
    # empty optional values count as absent
    out = api.env_files({"action": "list", "instance_id": "i-1", "dest": ""}, svc.services, {})
    assert "error" not in out


def test_every_schema_property_is_valid_for_some_action():
    (spec,) = api.tools()
    props = set(spec.schema["parameters"]["properties"]) - {"action"}
    assert props == {p for ps in api.ACTION_PARAMS.values() for p in ps}
    assert set(api.ACTION_PARAMS) == set(api.TOOL_ACTIONS)


def test_description_is_short_and_states_the_upload_rule():
    (spec,) = api.tools()
    text = spec.schema["description"]
    assert len(text.split()) <= 65
    assert "Hermes home" in text and "dest_dir" in text and "size(" in text
    assert "nothing_to_save" in text


def test_size_action_adds_a_plain_message(svc):
    out = api.env_files({"action": "size", "instance_id": "i-ab12cd34"}, svc.services, {})
    assert out["known"] is True and out["message"] == \
        "/work in python-dev-1 holds 1,204 files (5.0 MB)."
    svc.files.sized = {"path": "/work", "known": False, "bytes": None, "files": None,
                       "reason": "python-dev-1 is not running."}
    out = api.env_files({"action": "size", "instance_id": "i-ab12cd34"}, svc.services, {})
    assert out["message"] == "python-dev-1 is not running."


def test_restore_result_carries_the_copy_card(svc):
    out = api.env_files({"action": "restore", "export_id": "x-7", "instance_id": "i-1"},
                        svc.services, {})
    assert out["card"] == '::cc-copy{id="x-7"}'


def test_fetch_asks_only_when_large_or_directed(svc):
    s = svc.services
    args = {"action": "fetch", "instance_id": "i-ab12cd34", "path": "/work"}
    assert api.approval_env_files(args, s) is None                 # 5 MB, default folder
    svc.files.sized = {"path": "/work", "known": True, "bytes": int(3.2 * 1024 ** 3),
                       "files": 1204, "reason": None}
    assert api.approval_env_files(args, s) == (
        "Saves /work (3.2 GB, 1,204 files) from python-dev-1 to the exports folder on this computer.")
    svc.files.sized = {"path": "/work", "known": True, "bytes": 10, "files": 1, "reason": None}
    text = api.approval_env_files({**args, "dest_dir": '"C:/proj"', "extract": True}, s)
    assert text == ("Saves /work (10 B, 1 file) from python-dev-1 to C:/proj on this computer, "
                    "and unpacks it into a new folder there.")
    svc.files.sized = {"path": "/work", "known": False, "bytes": None, "files": None,
                       "reason": "python-dev-1 is not running."}
    text = api.approval_env_files({**args, "dest_dir": "C:/proj"}, s)
    assert text == ("Saves /work (size not known) from python-dev-1 to C:/proj on this "
                    "computer. python-dev-1 is not running.")


def test_fetch_rule_key_is_per_copy(svc):
    from cloud_compute_core.approval import rule_key
    a = {"action": "fetch", "instance_id": "i-1", "path": "/work", "dest_dir": "C:/a"}
    keys = {rule_key("env_files", a, "h"),
            rule_key("env_files", {**a, "dest_dir": "C:/b"}, "h"),
            rule_key("env_files", {**a, "extract": True}, "h"),
            rule_key("env_files", {**a, "path": "/data"}, "h")}
    assert len(keys) == 4
    assert all(k.startswith("cloud-compute:env_files:fetch:i-1:") for k in keys)
    assert rule_key("env_files", {**a, "dest_dir": ' "C:/a" '}, "h") == \
        rule_key("env_files", a, "h")


def test_delete_export_approval_names_a_kept_unpacked_folder(svc, tmp_path):
    folder = tmp_path / "unpacked"
    folder.mkdir()
    svc.services.store.put_record("export", "x-5", {"id": "x-5", "local_path": "C:/a.tar.gz",
                                                    "extracted_to": str(folder)})
    text = api.approval_env_files({"action": "delete_export", "export_id": "x-5"},
                                  svc.services)
    assert text.endswith(f" The unpacked folder {folder} is left in place.")
