#!/bin/sh
# Start a virtual screen and serve it through noVNC.
#
# VNC_PASSWORD holds the per-instance access token (32 lowercase hex characters).
# The screen is only reachable under /<token>/:
#   http://<host>:6080/<token>/vnc_lite.html?path=<token>/websockify&scale=true
# Every other path answers 404. Xvfb and x11vnc listen on loopback inside the
# container only; the web server listens on port 6080, which the container
# runtime publishes on the host's loopback interface. The token is never logged.
#
# Arguments, if any, are started as an extra program on the screen.
set -eu

if [ -z "${VNC_PASSWORD:-}" ]; then
    echo "start-display: VNC_PASSWORD is not set" >&2
    exit 1
fi
case "$VNC_PASSWORD" in
    *[!0-9a-f]*)
        echo "start-display: VNC_PASSWORD must be 32 lowercase hex characters" >&2
        exit 1
        ;;
esac
if [ "${#VNC_PASSWORD}" -ne 32 ]; then
    echo "start-display: VNC_PASSWORD must be 32 lowercase hex characters" >&2
    exit 1
fi

export DISPLAY=:1
SCREEN_GEOMETRY=1600x900x24

env -u VNC_PASSWORD Xvfb :1 -screen 0 "$SCREEN_GEOMETRY" -nolisten tcp &

i=0
while [ ! -S /tmp/.X11-unix/X1 ]; do
    i=$((i + 1))
    if [ "$i" -gt 100 ]; then
        echo "start-display: the virtual screen did not start" >&2
        exit 1
    fi
    sleep 0.1
done

env -u VNC_PASSWORD openbox &
env -u VNC_PASSWORD xterm -fa "DejaVu Sans Mono" -fs 11 &
if [ "$#" -gt 0 ]; then
    env -u VNC_PASSWORD "$@" &
fi
# -rfbportv6 -1 turns off the separate IPv6 listener, so only 127.0.0.1:5900 is open.
env -u VNC_PASSWORD x11vnc -display :1 -localhost -noipv6 -rfbport 5900 -rfbportv6 -1 \
    -forever -shared -nopw -quiet &

exec python3 - <<'PY'
"""Token-gated noVNC web server and websocket proxy (port 6080)."""
import hmac
import logging
import os
import re
import sys
from urllib.parse import quote, unquote, urlsplit

from websockify.websocketproxy import ProxyRequestHandler, WebSocketProxy

WEB_ROOT = "/usr/share/novnc"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 6080
VNC_HOST = "127.0.0.1"
VNC_PORT = 5900
ALLOWED_FILES = ("vnc_lite.html",)
ALLOWED_DIRS = ("core/", "vendor/")
# Plain HTTP only (the port is reached through a loopback tunnel); a missing
# certificate makes websockify refuse TLS handshakes.
NO_TLS_CERT = "/nonexistent/display-cert.pem"

TOKEN = os.environ.pop("VNC_PASSWORD", "")
if not re.fullmatch(r"[0-9a-f]{32}", TOKEN):
    sys.exit("start-display: VNC_PASSWORD must be 32 lowercase hex characters")
PREFIX = "/" + TOKEN + "/"


def redact(text):
    return str(text).replace(TOKEN, "<token>")


class TokenHandler(ProxyRequestHandler):
    """Serves the noVNC files and the websocket only under /<token>/."""

    def end_headers(self):
        # The page may be shown in a sandboxed frame (opaque origin), which
        # loads noVNC's module scripts through CORS requests.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, format, *args):
        super().log_message("%s", redact(format % args))

    def relative_path(self):
        path = urlsplit(self.path).path
        head = path[:len(PREFIX)]
        if len(head) != len(PREFIX) or not hmac.compare_digest(head.encode(), PREFIX.encode()):
            return None
        return unquote(path[len(PREFIX):])

    def handle_upgrade(self):
        if self.relative_path() != "websockify":
            self.send_error(404, "Not found")
            return
        super().handle_upgrade()

    def static_target(self):
        rel = self.relative_path()
        if not rel or "\\" in rel or "\x00" in rel:
            return None
        if rel not in ALLOWED_FILES and not rel.startswith(ALLOWED_DIRS):
            return None
        full = os.path.realpath(os.path.join(WEB_ROOT, rel))
        if not full.startswith(os.path.join(WEB_ROOT, "")) or not os.path.isfile(full):
            return None
        return "/" + quote(rel)

    def do_GET(self):
        target = self.static_target()
        if target is None:
            self.send_error(404, "Not found")
            return
        self.path = target
        super().do_GET()

    def do_HEAD(self):
        target = self.static_target()
        if target is None:
            self.send_error(404, "Not found")
            return
        self.path = target
        super().do_HEAD()


logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
server = WebSocketProxy(
    RequestHandlerClass=TokenHandler,
    listen_host=LISTEN_HOST,
    listen_port=LISTEN_PORT,
    target_host=VNC_HOST,
    target_port=VNC_PORT,
    web=WEB_ROOT,
    file_only=True,
    cert=NO_TLS_CERT,
)
server.start_server()
PY
