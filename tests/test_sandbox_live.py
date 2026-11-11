"""Live sandbox lifecycle on a real SSH host (runs only with ``CC_LIVE=1``).

Spawns the built-in ``python-dev`` template with the real store, transports and
runtime argv builders, runs ``python -V`` inside it, stops, starts and destroys
it, and checks that no container of this run is left on the host.

Environment:
  CC_LIVE=1                         enable this test
  CC_LIVE_SANDBOX_HOST=<ssh alias>  host to use (default ``x1carbon``; rootless podman)
"""

from __future__ import annotations

import os

import pytest

from cloud_compute_core.config import Config
from cloud_compute_core.events import EventBus
from cloud_compute_core.interfaces import Services
from cloud_compute_core.models import HostLimits, HostSpec
from cloud_compute_core.runtimes import LABEL_INSTALL, MANAGED_LABEL
from cloud_compute_core.sandbox import SandboxManager, get_install_id
from cloud_compute_core.secrets import SecretResolver
from cloud_compute_core.store import Store
from cloud_compute_core.transport import TransportFactory

pytestmark = pytest.mark.live

HOST_ALIAS = os.environ.get("CC_LIVE_SANDBOX_HOST", "x1carbon")
FAKE_TOKEN = "live-test-token-7f3a9c"  # dummy value for the template's HF_TOKEN variable


def _ps(t, label: str) -> list[str]:
    res = t.run(["podman", "ps", "-a", "--filter", f"label={label}", "--format", "{{.Names}}"],
                timeout=60)
    assert res.rc == 0, res.stderr
    return [n.strip() for n in res.stdout.splitlines() if n.strip()]


@pytest.fixture
def live(tmp_path):
    from cloud_compute_core.templates import TemplateStore

    store = Store(tmp_path / "cloud-compute.db")
    config = Config()
    secrets = SecretResolver(getter=lambda name: FAKE_TOKEN if name == "HF_TOKEN" else None)
    services = Services(store=store, config=config, secrets=secrets, events=EventBus(store),
                        transports=TransportFactory(config))
    host = HostSpec(id="x1carbon", name="x1carbon", kind="ssh", address=HOST_ALIAS,
                    runtime_pref="podman", limits=HostLimits(max_instances=2))
    store.upsert_host(host)
    services.set("templates", TemplateStore(store))
    mgr = SandboxManager(services)
    services.set("sandboxes", mgr)
    from cloud_compute_core.files import FileManager  # destroy(save=True) needs a files service
    services.set("files", FileManager(services, exports_dir=tmp_path / "exports"))
    t = services.transports.for_host(host)
    label = f"{LABEL_INSTALL}={get_install_id(store)}"
    try:
        yield services, mgr, host, t, label
    finally:
        for name in _ps(t, label):
            t.run(["podman", "rm", "-f", name], timeout=120)
        left = _ps(t, label)
        store.close()
        assert left == [], f"containers left behind: {left}"


def test_python_dev_lifecycle(live):
    services, mgr, host, t, label = live
    preview = mgr.preview("python-dev", host.id)
    assert FAKE_TOKEN not in str(preview.to_dict())
    assert [v.will_send for v in preview.variables] == []  # python-dev ships no variables

    rec = mgr.spawn("python-dev", host.id, name="live-python")
    print(f"\n[live] spawned {rec.id} container={rec.container_id} status={rec.status}")
    assert rec.status == "running" and rec.runtime == "podman"
    assert _ps(t, label) == [f"cc-{rec.id}"]
    assert f"cc-{rec.id}" in _ps(t, f"{MANAGED_LABEL}=1")
    # The env file was deleted right after run.
    res = t.run(["sh", "-c", f'ls "$HOME/.cache/cloud-compute/{rec.id}.env" 2>/dev/null | wc -l'])
    assert res.stdout.strip() == "0"
    # The leftover sweep runs on the real host (nothing old is expected there).
    swept = mgr.sweep_env_files(host.id, older_than_minutes=15)
    print(f"[live] swept {swept} stale env file(s)")
    assert isinstance(swept, int) and swept >= 0

    out = mgr.exec(rec.id, 'python -V; printf "%s\\n" "$HF_TOKEN"')
    print(f"[live] exec rc={out.rc} stdout={out.stdout.strip()!r}")
    assert out.rc == 0 and out.stdout.startswith("Python 3.12")
    assert FAKE_TOKEN not in out.stdout

    assert mgr.stop(rec.id).status == "stopped"
    assert mgr.start(rec.id).status == "running"
    assert mgr.exec(rec.id, ["python", "-V"]).rc == 0
    assert mgr.destroy(rec.id).status == "destroyed"
    assert _ps(t, label) == []
    assert mgr.get(rec.id).status == "destroyed"
    events = services.store.events_since(0, limit=100)
    reasons = [e["payload"].get("reason") for e in events if e["name"] == "instances.changed"]
    assert reasons == ["spawned", "stopped", "started", "destroyed"]
    assert FAKE_TOKEN not in str(events)
