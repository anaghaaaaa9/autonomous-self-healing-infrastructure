"""HTTP endpoint for Prometheus (/metrics) plus /incidents JSON and /healthz."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def start_server(port: int, registry, store) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            raw = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/metrics"):
                return self._send(200, registry.render(), "text/plain; version=0.0.4")
            if self.path.startswith("/incidents"):
                rows = store.history(50)
                for r in rows:
                    r["attempt_log"] = store.attempts_for(r["id"])
                return self._send(200, json.dumps(rows, default=str), "application/json")
            if self.path.startswith("/healthz"):
                return self._send(200, "ok", "text/plain")
            return self._send(404, "not found", "text/plain")

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
