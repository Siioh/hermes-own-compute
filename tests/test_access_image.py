"""Static checks for the noVNC display image (images/novnc)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from cloud_compute_core import display, runtimes

IMAGE_DIR = Path(__file__).resolve().parents[1] / "images" / "novnc"
CONTAINERFILE = IMAGE_DIR / "Containerfile"
START_SCRIPT = IMAGE_DIR / "start-display.sh"
EMBED_RE = re.compile(
    rb"^RUN cat > /usr/local/bin/start-display\.sh <<'(?P<tag>[A-Z_]+)'\n(?P<body>.*?\n)(?P=tag)\n",
    re.S | re.M)
TOKEN = "0123456789abcdef0123456789abcdef"


def _cf() -> str:
    return CONTAINERFILE.read_text(encoding="utf-8")


def _script() -> str:
    return START_SCRIPT.read_text(encoding="utf-8")


def _instructions() -> list[str]:
    """Containerfile instruction keywords (heredoc bodies and continuations skipped)."""
    out, in_heredoc, cont = [], None, False
    for line in _cf().splitlines():
        if in_heredoc is not None:
            if line == in_heredoc:
                in_heredoc = None
            continue
        if cont:
            cont = line.endswith("\\")
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
        m = re.search(r"<<'?([A-Z_]+)'?", stripped)
        if m:
            in_heredoc = m.group(1)
        cont = stripped.endswith("\\")
    return out


def test_embedded_start_script_matches_file_byte_for_byte():
    m = EMBED_RE.search(CONTAINERFILE.read_bytes())
    assert m, "start-display.sh heredoc not found in the Containerfile"
    assert m.group("body") == START_SCRIPT.read_bytes()
    assert m.group("tag") not in START_SCRIPT.read_bytes()


def test_files_use_lf_line_endings():
    for path in (CONTAINERFILE, START_SCRIPT):
        data = path.read_bytes()
        assert b"\r" not in data, f"{path.name} has CR characters"
        assert data.endswith(b"\n")


def test_no_copy_or_add_and_built_without_context():
    keywords = [i.split()[0].upper() for i in _instructions()]
    assert "COPY" not in keywords and "ADD" not in keywords
    assert runtimes.runtime_for("podman").build_argv(runtimes.DISPLAY_IMAGE_TAG) == [
        "podman", "build", "-t", "localhost/cloud-compute-novnc:0.1", "-"]


def test_base_image_pinned():
    froms = [i for i in _instructions() if i.upper().startswith("FROM ")]
    assert len(froms) == 1
    ref = froms[0].split()[1]
    assert ref.startswith("docker.io/library/debian:bookworm-")
    assert ":bookworm-slim" not in ref and ":latest" not in ref
    assert re.search(r"@sha256:[0-9a-f]{64}$", ref)


def test_label_expose_user_and_entrypoint():
    instr = _instructions()
    labels = [i for i in instr if i.startswith("LABEL ")]
    assert labels == [f'LABEL {runtimes.LABEL_DISPLAY}="{runtimes.DISPLAY_LABEL_VALUE}"']
    assert runtimes.DISPLAY_LABEL_VALUE == f"novnc:{runtimes.DISPLAY_PORT}"
    assert f"EXPOSE {runtimes.DISPLAY_PORT}" in instr
    users = [i for i in instr if i.startswith("USER ")]
    assert users and users[-1] != "USER root"
    assert 'ENTRYPOINT ["/usr/local/bin/start-display.sh"]' in instr


def test_packages_installed_without_recommends():
    cf = _cf()
    assert "--no-install-recommends" in cf
    for pkg in ("xvfb", "openbox", "x11vnc", "xterm", "novnc", "python3-websockify"):
        assert re.search(rf"\b{re.escape(pkg)}\b", cf), pkg
    assert "rm -rf /var/lib/apt/lists/*" in cf


def test_start_script_requires_token():
    s = _script()
    assert s.startswith("#!/bin/sh\n")
    assert "set -eu" in s
    assert display.TOKEN_ENV == "VNC_PASSWORD"
    assert 'if [ -z "${VNC_PASSWORD:-}" ]; then' in s
    assert '"${#VNC_PASSWORD}" -ne 32' in s
    assert "*[!0-9a-f]*)" in s


def test_start_script_binds_loopback_and_web_port():
    s = _script()
    assert re.search(r"\bXvfb :1 -screen 0 \S+ -nolisten tcp", s)
    x11vnc = s[s.index("x11vnc -display"):].split("&", 1)[0]
    for flag in ("-localhost", "-nopw", "-rfbport 5900", "-rfbportv6 -1", "-forever", "-shared"):
        assert flag in x11vnc
    assert 'VNC_HOST = "127.0.0.1"' in s and "VNC_PORT = 5900" in s
    assert f"LISTEN_PORT = {runtimes.DISPLAY_PORT}" in s
    assert 'WEB_ROOT = "/usr/share/novnc"' in s
    assert '"vnc_lite.html"' in s
    assert '"Access-Control-Allow-Origin", "*"' in s
    assert "ExpectOrigin" not in s and "auth_plugin" not in s


def test_start_script_never_prints_token():
    s = _script()
    for line in s.splitlines():
        if re.search(r"\b(echo|printf)\b", line):
            assert "$VNC_PASSWORD" not in line and "${VNC_PASSWORD" not in line
    # the X programs do not inherit the token
    for prog in ("Xvfb", "openbox", "xterm", "x11vnc"):
        assert f"env -u VNC_PASSWORD {prog}" in s


def test_start_script_posix_syntax():
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no POSIX sh available")
    res = subprocess.run([sh, "-n", str(START_SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


# ---- embedded web server, run against a stand-in websockify module ----------

def _server_source() -> str:
    s = _script()
    m = re.search(r"^exec python3 - <<'PY'\n(.*?)^PY\n", s, re.S | re.M)
    assert m, "embedded server not found"
    return m.group(1)


@pytest.fixture
def server_ns(monkeypatch):
    import http.server

    created = {}

    class ProxyRequestHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):  # never constructed by the tests
            raise AssertionError

        def handle_upgrade(self):
            created["upgraded"] = self.path

        def log_message(self, format, *args):
            created.setdefault("logs", []).append(format % args)

        def do_GET(self):
            created["served"] = self.path

        def do_HEAD(self):
            created["head"] = self.path

        def send_error(self, code, message=None, explain=None):
            created["error"] = code

    class WebSocketProxy:
        def __init__(self, **kwargs):
            created["server_kwargs"] = kwargs

        def start_server(self):
            created["started"] = True

    pkg = types.ModuleType("websockify")
    mod = types.ModuleType("websockify.websocketproxy")
    mod.ProxyRequestHandler = ProxyRequestHandler
    mod.WebSocketProxy = WebSocketProxy
    pkg.websocketproxy = mod
    monkeypatch.setitem(sys.modules, "websockify", pkg)
    monkeypatch.setitem(sys.modules, "websockify.websocketproxy", mod)
    monkeypatch.setenv("VNC_PASSWORD", TOKEN)
    ns: dict = {"__name__": "display_server"}
    exec(compile(_server_source(), "start-display-server", "exec"), ns)
    assert "VNC_PASSWORD" not in os.environ
    return ns, created


def _handler(ns, path, web_root):
    h = object.__new__(ns["TokenHandler"])
    h.path = path
    ns["WEB_ROOT"] = os.path.realpath(web_root)
    return h


def test_server_settings(server_ns):
    ns, created = server_ns
    kw = created["server_kwargs"]
    assert created["started"] is True
    assert kw["listen_host"] == "0.0.0.0" and kw["listen_port"] == 6080
    assert kw["target_host"] == "127.0.0.1" and kw["target_port"] == 5900
    assert kw["web"] == "/usr/share/novnc" and kw["file_only"] is True
    assert kw["cert"].startswith("/nonexistent/")
    assert kw["RequestHandlerClass"] is ns["TokenHandler"]


def test_server_rejects_bad_token(monkeypatch, server_ns):
    monkeypatch.setenv("VNC_PASSWORD", "not-a-token")
    with pytest.raises(SystemExit):
        exec(compile(_server_source(), "start-display-server", "exec"), {"__name__": "x"})


@pytest.fixture
def web_root(tmp_path):
    root = tmp_path / "novnc"
    (root / "core").mkdir(parents=True)
    (root / "vendor" / "pako").mkdir(parents=True)
    (root / "vnc_lite.html").write_text("x")
    (root / "vnc.html").write_text("x")
    (root / "core" / "rfb.js").write_text("x")
    (root / "vendor" / "pako" / "index.js").write_text("x")
    (tmp_path / "secret.txt").write_text("x")
    return root


@pytest.mark.parametrize("path,target", [
    (f"/{TOKEN}/vnc_lite.html", "/vnc_lite.html"),
    (f"/{TOKEN}/vnc_lite.html?path={TOKEN}/websockify&scale=true", "/vnc_lite.html"),
    (f"/{TOKEN}/core/rfb.js", "/core/rfb.js"),
    (f"/{TOKEN}/vendor/pako/index.js", "/vendor/pako/index.js"),
    ("/vnc_lite.html", None),
    ("/core/rfb.js", None),
    (f"/{TOKEN[:-1]}0/vnc_lite.html", None),
    (f"/{TOKEN}", None),
    (f"/{TOKEN}/", None),
    (f"/{TOKEN}/core/", None),
    (f"/{TOKEN}/vnc.html", None),
    (f"/{TOKEN}/core/../../secret.txt", None),
    (f"/{TOKEN}/core/%2e%2e/%2e%2e/secret.txt", None),
    (f"/{TOKEN}/core/missing.js", None),
    (f"/{TOKEN}/websockify", None),
])
def test_static_paths(server_ns, web_root, path, target):
    ns, created = server_ns
    h = _handler(ns, path, web_root)
    assert h.static_target() == target
    h.do_GET()
    if target is None:
        assert created["error"] == 404 and "served" not in created
    else:
        assert created["served"] == target


def test_head_uses_same_gate(server_ns, web_root):
    ns, created = server_ns
    _handler(ns, "/vnc_lite.html", web_root).do_HEAD()
    assert created["error"] == 404 and "head" not in created
    _handler(ns, f"/{TOKEN}/vnc_lite.html", web_root).do_HEAD()
    assert created["head"] == "/vnc_lite.html"


@pytest.mark.parametrize("path,ok", [
    (f"/{TOKEN}/websockify", True),
    (f"/{TOKEN}/websockify?x=1", True),
    ("/websockify", False),
    (f"/{TOKEN}/vnc_lite.html", False),
    (f"/{'f' * 32}/websockify", False),
])
def test_websocket_path(server_ns, web_root, path, ok):
    ns, created = server_ns
    _handler(ns, path, web_root).handle_upgrade()
    if ok:
        assert created["upgraded"] == path
    else:
        assert created["error"] == 404 and "upgraded" not in created


def test_cors_header_on_every_response(server_ns):
    ns, _ = server_ns
    import http.server

    sent = []
    h = object.__new__(ns["TokenHandler"])
    h.send_header = lambda k, v: sent.append((k, v))
    orig = http.server.SimpleHTTPRequestHandler.end_headers
    http.server.SimpleHTTPRequestHandler.end_headers = lambda self: sent.append(("END", ""))
    try:
        h.end_headers()
    finally:
        http.server.SimpleHTTPRequestHandler.end_headers = orig
    assert ("Access-Control-Allow-Origin", "*") in sent
    assert sent[-1] == ("END", "")


def test_logs_redact_token(server_ns):
    ns, created = server_ns
    h = object.__new__(ns["TokenHandler"])
    h.log_message("%s: Path: '%s'", "127.0.0.1", f"/{TOKEN}/websockify")
    assert created["logs"] == ["127.0.0.1: Path: '/<token>/websockify'"]
