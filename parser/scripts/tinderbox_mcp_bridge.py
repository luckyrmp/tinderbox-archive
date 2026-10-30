#!/usr/bin/env python3
"""Tailscale-only streamable-HTTP bridge for the read-only Tinderbox MCP server.

POST /mcp with a JSON-RPC message (or batch) -> forwarded to a long-lived stdio
child (scripts/tinderbox_mcp_remote.sh); responses returned as application/json.
Requires `Authorization: Bearer $TINDERBOX_MCP_TOKEN` (from
~/.secrets/tinderbox-reader.env). Never logs the token or request bodies.
Env: TINDERBOX_BRIDGE_HOST (required, e.g. tailscale IP), TINDERBOX_BRIDGE_PORT (8765).
"""
import hmac, json, logging, os, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s bridge %(message)s")
log = logging.getLogger()
HERE = Path(__file__).resolve().parent
ENV_FILE = Path.home() / ".secrets" / "tinderbox-reader.env"


def _load_token() -> str:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("TINDERBOX_MCP_TOKEN="):
            tok = line.split("=", 1)[1].strip()
            if len(tok) >= 32:
                return tok
    sys.exit("TINDERBOX_MCP_TOKEN missing/too short in tinderbox-reader.env")


TOKEN = _load_token().encode()
HOST = os.environ["TINDERBOX_BRIDGE_HOST"]
PORT = int(os.environ.get("TINDERBOX_BRIDGE_PORT", "8765"))
if HOST in ("0.0.0.0", "::", ""):
    sys.exit("refusing to bind to all interfaces")


class Child:
    def __init__(self):
        self.lock = threading.Lock()
        self.p = None

    def _ensure(self):
        if self.p is None or self.p.poll() is not None:
            env = {k: v for k, v in os.environ.items() if k not in ("SUPABASE_SERVICE_KEY", "SUPABASE_URL")}
            self.p = subprocess.Popen([str(HERE / "tinderbox_mcp_remote.sh")], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=sys.stderr, env=env, text=True, bufsize=1)
            log.info("started stdio server pid %s", self.p.pid)

    def call(self, msg: dict):
        with self.lock:
            self._ensure()
            self.p.stdin.write(json.dumps(msg) + "\n"); self.p.stdin.flush()
            if "id" not in msg or "method" not in msg:
                return None  # notification / response: no reply expected
            while True:
                line = self.p.stdout.readline()
                if not line:
                    self.p = None
                    raise RuntimeError("stdio server exited")
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id") == msg["id"]:
                    return r


CHILD = Child()


class H(BaseHTTPRequestHandler):
    server_version = "tinderbox-bridge"

    def log_message(self, fmt, *a):
        log.info("%s %s", self.client_address[0], fmt % a)

    def _send(self, code, body=None, extra=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        h = self.headers.get("Authorization", "")
        ok = h.startswith("Bearer ") and hmac.compare_digest(h[7:].strip().encode(), TOKEN)
        if not ok:
            self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Bearer realm="tinderbox"'})
        return ok

    def do_GET(self):
        if self._authed():
            self._send(405, {"error": "SSE stream not supported; use POST"}, {"Allow": "POST"})

    def do_DELETE(self):
        if self._authed():
            self._send(405, {"error": "no sessions"}, {"Allow": "POST"})

    def do_POST(self):
        if not self._authed():
            return
        if self.path.split("?")[0] not in ("/mcp", "/mcp/"):
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 1_000_000:
                return self._send(413, {"error": "too large"})
            payload = json.loads(self.rfile.read(n))
        except Exception:
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        try:
            msgs = payload if isinstance(payload, list) else [payload]
            out = [r for r in (CHILD.call(m) for m in msgs) if r is not None]
        except Exception as e:
            log.warning("bridge error: %s", type(e).__name__)
            return self._send(502, {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "upstream error"}})
        if not out:
            return self._send(202)
        self._send(200, out if isinstance(payload, list) else out[0])


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), H)
    log.info("listening on http://%s:%s/mcp", HOST, PORT)
    srv.serve_forever()
