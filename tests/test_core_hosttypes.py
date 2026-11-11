"""Tests for the host-type catalog (``cloud_compute_core.hosttypes``)."""

from __future__ import annotations

import json

import pytest

from cloud_compute_core import hosttypes
from cloud_compute_core.models import HOST_KINDS, Capabilities

ENTRY_KEYS = {
    "kind", "title", "one_liner", "when_to_use", "capabilities", "capability_notes", "needs",
    "learn_more_topic", "mvp",
}

EXPECTED_KINDS = ["ssh", "slurm", "local", "ec2"]

CARDS = {
    "ssh": {
        "title": "SSH host",
        "one_liner": "Run sandboxed containers on a Linux machine you can SSH into.",
        "when_to_use": "Your own laptop, workstation, homelab or VM.",
        "learn_more_topic": "ssh-host",
    },
    "slurm": {
        "title": "Slurm login node",
        "one_liner": "Submit batch jobs to an HPC cluster through its login node.",
        "when_to_use": "HPC or lab clusters, sweeps, scheduled work.",
        "learn_more_topic": "slurm",
    },
    "local": {
        "title": "Local Docker/Podman",
        "one_liner": "Run containers on this computer.",
        "when_to_use": "Quick tests without another machine.",
        "learn_more_topic": "local",
    },
}


# --------------------------------------------------------------------------- flags


def test_capability_flags_tuple():
    assert hosttypes.CAPABILITY_FLAGS == (
        "containers", "batch_jobs", "interactive_terminal", "display",
        "port_forward", "gpu", "agents", "costs_money",
    )


def test_flag_order_matches_catalog_keys():
    for entry in hosttypes.HOST_TYPES:
        assert tuple(entry["capabilities"]) == hosttypes.CAPABILITY_FLAGS


# --------------------------------------------------------------------------- shape


def test_four_entries_in_order():
    assert [e["kind"] for e in hosttypes.HOST_TYPES] == EXPECTED_KINDS
    assert [e["kind"] for e in hosttypes.host_types()] == EXPECTED_KINDS


def test_kinds_are_known_host_kinds():
    for entry in hosttypes.host_types():
        assert entry["kind"] in HOST_KINDS


@pytest.mark.parametrize("entry", hosttypes.HOST_TYPES, ids=[e["kind"] for e in hosttypes.HOST_TYPES])
def test_entry_shape(entry):
    assert set(entry) == ENTRY_KEYS
    assert isinstance(entry["kind"], str) and entry["kind"]
    assert isinstance(entry["title"], str) and entry["title"]
    assert isinstance(entry["one_liner"], str) and entry["one_liner"]
    assert isinstance(entry["when_to_use"], str) and entry["when_to_use"]
    assert isinstance(entry["learn_more_topic"], str) and entry["learn_more_topic"]
    assert isinstance(entry["mvp"], bool)

    caps = entry["capabilities"]
    assert isinstance(caps, dict)
    assert set(caps) == set(hosttypes.CAPABILITY_FLAGS)
    for flag, value in caps.items():
        assert value in ("yes", "no", "partial"), f"{entry['kind']}.{flag} = {value!r}"

    needs = entry["needs"]
    assert isinstance(needs, list) and needs
    for need in needs:
        assert isinstance(need, str) and need.strip()
        assert "http://" not in need and "https://" not in need
        assert "](" not in need and "**" not in need


def test_no_duplicate_kinds():
    kinds = [e["kind"] for e in hosttypes.HOST_TYPES]
    assert len(kinds) == len(set(kinds))


# --------------------------------------------------------------------------- copy


def test_card_strings_exact():
    by_kind = {e["kind"]: e for e in hosttypes.host_types()}
    for kind, card in CARDS.items():
        for key, text in card.items():
            assert by_kind[kind][key] == text, f"{kind}.{key}"


def test_ec2_card_and_mvp_flags():
    ec2 = hosttypes.get_host_type("ec2")
    assert ec2["title"] == "EC2"
    assert "on demand" in ec2["one_liner"]
    assert ec2["learn_more_topic"] == "ec2"
    assert ec2["mvp"] is True

    assert hosttypes.get_host_type("ssh")["mvp"] is True
    assert hosttypes.get_host_type("slurm")["mvp"] is True
    assert hosttypes.get_host_type("local")["mvp"] is True


def test_learn_more_topics():
    topics = {e["kind"]: e["learn_more_topic"] for e in hosttypes.host_types()}
    assert topics == {"ssh": "ssh-host", "slurm": "slurm", "local": "local", "ec2": "ec2"}


# --------------------------------------------------------------------------- capabilities content


def test_ssh_capabilities():
    caps = hosttypes.get_host_type("ssh")["capabilities"]
    assert caps["containers"] == "yes"
    assert caps["interactive_terminal"] == "yes"
    assert caps["port_forward"] == "yes"
    assert caps["batch_jobs"] == "no"
    assert caps["costs_money"] == "no"
    assert caps["gpu"] == "partial"
    assert caps["agents"] == "partial"
    assert caps["display"] == "partial"


def test_slurm_capabilities():
    caps = hosttypes.get_host_type("slurm")["capabilities"]
    assert caps["batch_jobs"] == "yes"
    assert caps["containers"] == "no"
    assert caps["interactive_terminal"] == "partial"
    assert caps["display"] == "no"
    assert caps["port_forward"] == "no"
    assert caps["gpu"] == "partial"
    assert caps["agents"] == "no"
    assert caps["costs_money"] == "partial"


def test_local_capabilities():
    caps = hosttypes.get_host_type("local")["capabilities"]
    assert caps["containers"] == "yes"
    assert caps["interactive_terminal"] == "yes"
    assert caps["port_forward"] == "yes"
    assert caps["batch_jobs"] == "no"
    assert caps["costs_money"] == "no"
    assert caps["agents"] == "partial"


def test_ec2_capabilities():
    caps = hosttypes.get_host_type("ec2")["capabilities"]
    assert caps["containers"] == "yes"
    assert caps["costs_money"] == "yes"
    assert caps["agents"] == "partial"
    assert caps["batch_jobs"] == "no"


# --------------------------------------------------------------------------- copies


def test_host_types_returns_a_copy():
    first = hosttypes.host_types()
    first[0]["title"] = "changed"
    first[0]["capabilities"]["containers"] = "no"
    first.pop()
    first[2]["needs"].append("extra")

    second = hosttypes.host_types()
    assert len(second) == 4
    assert second[0]["title"] == "SSH host"
    assert second[0]["capabilities"]["containers"] == "yes"
    assert "extra" not in second[2]["needs"]
    assert hosttypes.HOST_TYPES[0]["title"] == "SSH host"


def test_get_host_type_returns_a_copy():
    entry = hosttypes.get_host_type("slurm")
    entry["when_to_use"] = "changed"
    entry["needs"].clear()
    again = hosttypes.get_host_type("slurm")
    assert again["when_to_use"] == "HPC or lab clusters, sweeps, scheduled work."
    assert again["needs"]


def test_get_host_type_unknown():
    assert hosttypes.get_host_type("kubernetes") is None
    assert hosttypes.get_host_type("") is None
    assert hosttypes.get_host_type(None) is None  # type: ignore[arg-type]


def test_is_host_kind():
    assert hosttypes.is_host_kind("ssh")
    assert not hosttypes.is_host_kind("kubernetes")
    assert not hosttypes.is_host_kind(7)


def test_json_serializable():
    payload = hosttypes.host_types()
    text = json.dumps(payload)
    assert json.loads(text) == payload


# --------------------------------------------------------------------------- default capabilities


def test_default_capabilities_ssh():
    caps = hosttypes.default_capabilities("ssh")
    assert isinstance(caps, Capabilities)
    assert caps.containers is True
    assert caps.interactive_terminal is True
    assert caps.port_forward is True
    assert caps.display == "unknown"
    assert caps.batch_jobs is False
    assert caps.scheduler is None


def test_default_capabilities_local_and_ec2_like_ssh():
    ssh = hosttypes.default_capabilities("ssh")
    for kind in ("local", "ec2"):
        caps = hosttypes.default_capabilities(kind)
        assert isinstance(caps, Capabilities)
        assert caps.containers is True
        assert caps.interactive_terminal is True
        assert caps.port_forward is True
        assert caps.display == "unknown"
        assert caps.to_dict() == ssh.to_dict()


def test_default_capabilities_slurm():
    caps = hosttypes.default_capabilities("slurm")
    assert isinstance(caps, Capabilities)
    assert caps.batch_jobs is True
    assert caps.interactive_terminal is True
    assert caps.containers is False
    assert caps.port_forward is False
    assert caps.display == "unsupported"
    assert caps.display_reason == "compute nodes sit behind the login node"
    assert caps.scheduler == "slurm"


def test_default_capabilities_are_fresh_objects():
    first = hosttypes.default_capabilities("ssh")
    first.containers = False
    first.gpus.append("not-a-gpu")  # type: ignore[arg-type]
    assert hosttypes.default_capabilities("ssh").containers is True
    assert hosttypes.default_capabilities("ssh").gpus == []


@pytest.mark.parametrize("kind", ["kubernetes", "", "SSH", None])
def test_default_capabilities_unknown_kind(kind):
    with pytest.raises(ValueError):
        hosttypes.default_capabilities(kind)  # type: ignore[arg-type]


def test_default_capabilities_json_safe():
    for kind in EXPECTED_KINDS:
        caps = hosttypes.default_capabilities(kind)
        json.dumps(caps.to_dict())


# --------------------------------------------------------------------------- capability notes


def test_every_conditional_capability_has_a_plain_note():
    """'partial' alone says nothing; each one needs a sentence that names the condition."""
    for entry in hosttypes.host_types():
        notes = entry["capability_notes"]
        assert set(notes) <= set(hosttypes.CAPABILITY_FLAGS), entry["kind"]
        for flag, value in entry["capabilities"].items():
            if value == "partial":
                assert notes.get(flag, "").strip(), f"{entry['kind']}.{flag} is partial without a note"
        for flag, text in notes.items():
            assert entry["capabilities"][flag] != "no", f"{entry['kind']}.{flag} has a note but is off"
            assert text.endswith("."), (entry["kind"], flag)
            assert "partial" not in text.lower(), (entry["kind"], flag)


def test_capability_notes_examples():
    notes = {e["kind"]: e["capability_notes"] for e in hosttypes.host_types()}
    assert notes["slurm"]["gpu"] == "If your cluster has GPUs."
    assert "display image on the host" in notes["ssh"]["display"]
    assert "cluster time" in notes["slurm"]["costs_money"]
    assert "AWS" in notes["ec2"]["costs_money"]


def test_notes_are_not_shared_between_copies():
    first = hosttypes.host_types()
    first[0]["capability_notes"]["gpu"] = "changed"
    assert hosttypes.host_types()[0]["capability_notes"]["gpu"] != "changed"
    assert hosttypes.get_host_type("slurm")["capability_notes"]["gpu"] == "If your cluster has GPUs."


# --------------------------------------------------------------------------- wording

import re  # noqa: E402

_PERSONAL = re.compile(r"\b(we|i|our|us|let's|we're|we'll|i'm|i've)\b", re.IGNORECASE)
_WINDOWS = re.compile(r"windows|\bPC\b", re.IGNORECASE)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def test_host_type_text_has_no_we_or_i_and_no_single_os_wording():
    for text in _strings(hosttypes.host_types()):
        assert _PERSONAL.search(text) is None, text
        assert _WINDOWS.search(text) is None, text


def test_slurm_card_does_not_name_universities():
    slurm = hosttypes.get_host_type("slurm")
    assert "universit" not in json.dumps(slurm).lower()
