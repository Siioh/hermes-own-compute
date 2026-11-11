"""The Slurm submit card and the add-host card show everything that will be used: the command,
and every host field, not only name and user@address."""

from __future__ import annotations

import pytest

from cloud_compute_core import approval, hosts_api, slurm_api
from cloud_compute_core.store import Store
from cloud_compute_core.testing import FakeEvents, FakeSecrets, make_services, sample_slurm_host
from cloud_compute_core.config import Config


@pytest.fixture
def svc(tmp_path):
    store = Store(tmp_path / "c.db")
    store.upsert_host(sample_slurm_host())
    services = make_services(store=store, events=FakeEvents(), config=Config(), secrets=FakeSecrets({}))
    yield services
    store.close()


def test_slurm_submit_card_shows_the_command_and_the_resources(svc):
    tool = next(t for t in slurm_api.tools() if t.name == "slurm_submit")
    out = approval.approval_for(
        "slurm_submit", {"host_id": "slurm-x1", "name": "train", "command": "python train.py --epochs 3",
                         "partition": "gpu", "cpus": 8, "gpus": 2, "time_limit": "02:00:00"}, tool, svc)
    msg = out["message"]
    assert "python train.py --epochs 3" in msg
    for text in ("partition gpu", "8", "2", "02:00:00"):
        assert text in msg
    assert out["rule_key"].startswith("cloud-compute:slurm_submit:slurm-x1:")


def test_add_host_card_shows_every_value(svc):
    tool = hosts_api.tools()[1]
    out = approval.approval_for(
        "compute_host", {"action": "add", "kind": "ssh", "name": "Lab", "address": "labbox",
                         "user": "bob", "port": 2222, "jump_host": "bastion",
                         "identity_file": "C:/keys/lab", "allow_agents": True, "max_instances": 3},
        tool, svc)
    msg = out["message"]
    for text in ("Lab", "Address: labbox", "User: bob", "Port: 2222", "Jump host: bastion",
                 "Key file: C:/keys/lab", "Allow cloud agents: yes", "Most environments at once: 3"):
        assert text in msg, text
