import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dashboard" / "dist" / "index.js"


def test_dist_exists_and_is_import_free():
    text = DIST.read_text(encoding="utf-8")
    assert text.strip()
    assert "import " not in text and "require(" not in text


def test_manifest_default_entry_resolves():
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert (ROOT / "dashboard" / manifest.get("entry", "dist/index.js")).is_file()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dist_parses_with_node():
    result = subprocess.run(["node", "--check", str(DIST)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
