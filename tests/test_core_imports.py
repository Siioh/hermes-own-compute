"""Every core/*.py module imports in a bare interpreter using only the stdlib."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CORE_DIR = Path(__file__).resolve().parents[1] / "core"

CHILD = r"""
import importlib, importlib.util, json, sys
core_dir = sys.argv[1]
names = json.loads(sys.argv[2])
before = set(sys.modules)
spec = importlib.util.spec_from_file_location(
    "cloud_compute_core", core_dir + "/__init__.py", submodule_search_locations=[core_dir])
pkg = importlib.util.module_from_spec(spec)
sys.modules["cloud_compute_core"] = pkg
spec.loader.exec_module(pkg)
failed = {}
for name in names:
    try:
        importlib.import_module("cloud_compute_core." + name)
    except BaseException as exc:
        failed[name] = type(exc).__name__ + ": " + str(exc)
new = sorted({m.split(".")[0] for m in set(sys.modules) - before})
std = set(sys.stdlib_module_names)
foreign = [m for m in new if m != "cloud_compute_core" and m not in std]
print(json.dumps({"failed": failed, "foreign": foreign}))
"""


def _core_modules() -> list[str]:
    return sorted(p.stem for p in CORE_DIR.glob("*.py") if p.stem != "__init__")


def test_core_modules_exist():
    assert {"models", "interfaces", "registry", "testing"} <= set(_core_modules())


@pytest.mark.skipif(sys.version_info < (3, 10), reason="sys.stdlib_module_names needs 3.10+")
def test_core_imports_stdlib_only():
    names = _core_modules()
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", CHILD, CORE_DIR.as_posix(), json.dumps(names)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["failed"] == {}, result["failed"]
    assert result["foreign"] == [], f"non-stdlib imports in core/: {result['foreign']}"


def test_package_import_is_cheap():
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         "import importlib.util, sys; d = sys.argv[1]; "
         "s = importlib.util.spec_from_file_location('cloud_compute_core', d + '/__init__.py', "
         "submodule_search_locations=[d]); m = importlib.util.module_from_spec(s); "
         "sys.modules['cloud_compute_core'] = m; s.loader.exec_module(m); "
         "print(sorted(k for k in sys.modules if k.startswith('cloud_compute_core.')))",
         CORE_DIR.as_posix()],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"
