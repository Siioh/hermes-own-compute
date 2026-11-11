"""Display state table and run-override / URL helper tests."""

from __future__ import annotations

import itertools

import pytest

from cloud_compute_core import display as d
from cloud_compute_core import runtimes
from cloud_compute_core.store import Store
from cloud_compute_core.testing import (
    sample_capabilities, sample_instance, sample_slurm_capabilities, sample_template,
)


def _caps(kind: str, disp: str):
    if kind == "slurm":
        return sample_slurm_capabilities(display=disp, display_reason="r")
    if kind == "ec2":   # no runtime reachable until launched
        return sample_capabilities(runtime=None, containers=False, display=disp, display_reason="r")
    return sample_capabilities(display=disp, display_reason="r")   # ssh / local


def _expected(kind, disp, tmpl_on, inst):
    if inst == "without":
        return "unsupported"
    if inst == "running":
        return "supported"
    if not tmpl_on:
        return "unsupported"
    if kind == "slurm":
        return "unsupported"
    if kind == "ec2":
        return "unknown" if disp == "unknown" else "unsupported"
    return disp


@pytest.mark.parametrize("kind,disp,tmpl_on,inst", list(itertools.product(
    ["ssh", "local", "slurm", "ec2"], ["supported", "buildable", "unsupported", "unknown"],
    [True, False], ["none", "running", "without"])))
def test_display_state_table(kind, disp, tmpl_on, inst):
    record = {"none": None,
              "running": sample_instance(display=True, status="running"),
              "without": sample_instance(display=False, status="running")}[inst]
    state, reason = d.display_state(_caps(kind, disp), sample_template(display=tmpl_on), record)
    assert state == _expected(kind, disp, tmpl_on, inst)
    assert reason and len(reason) < 100


def test_reasons():
    assert "login node" in d.display_state(sample_slurm_capabilities(), None, None)[1]
    s, r = d.display_state(sample_capabilities(display="buildable"), None, None)
    assert s == "buildable" and "build" in r
    assert d.display_state(sample_capabilities(runtime=None, containers=False, display="unsupported"),
                           None, None) == ("unsupported", "No container runtime on the host")
    assert d.display_state(sample_capabilities(display="unknown", runtime=None, containers=False),
                           None, None)[0] == "unknown"
    assert "without a screen" in d.display_state(sample_capabilities(), None,
                                                 sample_instance(display=False))[1]


def test_overrides_shape():
    env, publish, labels = d.display_run_overrides("i-ab12cd34")
    assert list(env) == ["VNC_PASSWORD"] and len(env["VNC_PASSWORD"]) == 32
    int(env["VNC_PASSWORD"], 16)
    assert publish == [("127.0.0.1", None, runtimes.DISPLAY_PORT)]
    assert labels == {runtimes.LABEL_DISPLAY: runtimes.DISPLAY_LABEL_VALUE}
    tokens = {d.display_run_overrides("i-ab12cd34")[0]["VNC_PASSWORD"] for _ in range(20)}
    assert len(tokens) == 20


def test_overrides_persist(tmp_path):
    store = Store(tmp_path / "s.db")
    env, _, _ = d.display_run_overrides("i-ab12cd34", store=store)
    assert store.kv_get("display-token:i-ab12cd34") == env["VNC_PASSWORD"]
    assert d.get_display_token(store, "i-ab12cd34") == env["VNC_PASSWORD"]
    assert d.get_display_token(store, "i-other") is None


def test_clear_display_token(tmp_path):
    store = Store(tmp_path / "s.db")
    d.display_run_overrides("i-ab12cd34", store=store)
    assert d.clear_display_token(store, "i-ab12cd34") is True
    assert store.kv_get("display-token:i-ab12cd34") is None
    assert d.clear_display_token(store, "i-ab12cd34") is False
    with pytest.raises(ValueError):
        d.clear_display_token(store, "-x")


def test_overrides_no_store_persists_nothing(tmp_path):
    store = Store(tmp_path / "s.db")
    d.display_run_overrides("i-ab12cd34")
    assert store.kv_get("display-token:i-ab12cd34") is None


@pytest.mark.parametrize("bad", ["", "-x", "a b", "i;rm", "a\nb", None, 5])
def test_invalid_instance_id(bad, tmp_path):
    store = Store(tmp_path / "s.db")
    with pytest.raises(ValueError):
        d.display_run_overrides(bad, store=store)


def test_url_and_redaction():
    tok = "0123456789abcdef0123456789abcdef"
    url = d.display_url(41234, tok)
    assert url == f"http://127.0.0.1:41234/{tok}/vnc_lite.html?path={tok}/websockify&scale=true"
    assert "password=" not in url
    path, query = url.split("?")
    assert path.count(tok) == 1 and query.count(tok) == 1
    red = d.redact_display_url(url)
    assert tok not in red
    assert red == "http://127.0.0.1:41234/<token>/vnc_lite.html?path=<token>/websockify&scale=true"
    assert d.redact_display_url("http://x/y") == "http://x/y"
    with pytest.raises(ValueError):
        d.display_url(0, tok)
    with pytest.raises(ValueError):
        d.display_url(80, "short")


def test_parse_ports_entry():
    assert d.parse_ports_entry("127.0.0.1:41234") == 41234
    assert d.parse_ports_entry("garbage") is None
    assert d.parse_ports_entry("127.0.0.1:") is None
    assert d.parse_ports_entry("127.0.0.1:99999") is None


def test_embeddable():
    assert d.EMBEDDABLE is True
