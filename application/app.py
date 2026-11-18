
#!/usr/bin/env python3
"""Sample application for the self-healing platform.

Stdlib only. Exposes /health, /ready, /metrics, /work and bounded chaos hooks
(/chaos/*) that the failure-simulation scripts use. Metrics are computed from
the live process (CPU via process_time, memory via RSS) - nothing is hardcoded.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

START = time.time()
PORT = int(os.getenv("PORT", "8080"))
CHAOS_ENABLED = os.getenv("ENABLE_CHAOS", "true").lower() == "true"
MAX_CHAOS_MB = int(os.getenv("MAX_CHAOS_MB", "160"))
MAX_CHAOS_SECONDS = int(os.getenv("MAX_CHAOS_SECONDS", "120"))
CPU_LIMIT = float(os.getenv("APP_CPU_LIMIT", "1.0"))
VERSION = os.getenv("APP_VERSION", "1.0.0")


def memory_limit_bytes() -> int:
    env = os.getenv("APP_MEMORY_LIMIT_MB")
    if env:
        return int(float(env) * 1024 * 1024)
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            v = f.read().strip()
        if v.isdigit():
            return int(v)
    except OSError:
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 1 << 30


def rss_bytes() -> int:
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        pass
    try:  # macOS fallback (local backend outside Docker)
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        return int(out) * 1024
    except Exception:
        return 0


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.health_fail_until = 0.0          # inf => until reset
        self.cpu_until = 0.0
        self.cpu_gen = 0
        self.mem_blocks: list = []
        self.cpu_ratio = 0.0
        self.requests: dict = {}

    def health_ok(self) -> bool:
        return time.time() >= self.health_fail_until


STATE = State()
MEM_LIMIT = memory_limit_bytes()


def log(event: str, **fields) -> None:
    print(json.dumps({"ts": round(time.time(), 3), "app": "shl-app", "event": event, **fields}), flush=True)


# ---- chaos -----------------------------------------------------------------
def _burn(until: float, gen: int) -> None:
    x = 0
    while time.time() < until and STATE.cpu_gen == gen:
        for _ in range(20000):
            x = (x * 31 + 7) % 1000003


def chaos_cpu(seconds: float, threads: int) -> dict:
    seconds = min(max(seconds, 1), MAX_CHAOS_SECONDS)
    threads = min(max(threads, 1), 2)
    until = time.time() + seconds
    STATE.cpu_until = max(STATE.cpu_until, until)
    for _ in range(threads):
        threading.Thread(target=_burn, args=(until, STATE.cpu_gen), daemon=True).start()
    return {"chaos": "cpu", "seconds": seconds, "threads": threads}


def chaos_memory(mb: int, seconds: float) -> dict:
    mb = min(max(mb, 1), MAX_CHAOS_MB)
    block = bytearray(b"\x01") * (mb * 1024 * 1024)   # touched pages -> real RSS
    with STATE.lock:
        STATE.mem_blocks.append(block)
    if seconds > 0:
        seconds = min(seconds, MAX_CHAOS_SECONDS)
        threading.Timer(seconds, release_memory).start()
    return {"chaos": "memory", "mb": mb, "seconds": seconds}


def release_memory() -> None:
    with STATE.lock:
        STATE.mem_blocks.clear()


def chaos_health(seconds: float) -> dict:
    STATE.health_fail_until = float("inf") if seconds <= 0 else time.time() + min(seconds, MAX_CHAOS_SECONDS)
    return {"chaos": "health-fail", "seconds": seconds or "until-reset"}


def chaos_reset() -> dict:
    STATE.health_fail_until = 0.0
    STATE.cpu_gen += 1
    STATE.cpu_until = 0.0
    release_memory()
    return {"chaos": "reset"}


def chaos_crash() -> dict:
    threading.Timer(0.3, lambda: os._exit(1)).start()
    return {"chaos": "crash", "exit_in_seconds": 0.3}


# ---- metrics ---------------------------------------------------------------
def sampler() -> None:
    last_c, last_t = time.process_time(), time.monotonic()
    while True:
        time.sleep(2)
        c, t = time.process_time(), time.monotonic()
        STATE.cpu_ratio = max(0.0, (c - last_c) / max(t - last_t, 1e-6) / CPU_LIMIT)
        last_c, last_t = c, t


def render_metrics() -> str:
    rss = rss_bytes()
    now = time.time()
    out = []

    def m(name, typ, help_, value, labels=""):
        out.append(f"# HELP {name} {help_}\n# TYPE {name} {typ}\n{name}{labels} {value}")

    m("app_up", "gauge", "Application process is up", 1)
    m("app_health_status", "gauge", "1 if /health returns 200", 1 if STATE.health_ok() else 0)
    m("app_info", "gauge", "Build info", 1, f'{{version="{VERSION}"}}')
    m("app_start_time_seconds", "gauge", "Start time (unix)", round(START, 3))
    m("app_uptime_seconds", "gauge", "Uptime", round(now - START, 3))
    m("app_cpu_utilization_ratio", "gauge", "Process CPU as a ratio of the CPU limit", round(STATE.cpu_ratio, 4))
    m("app_cpu_limit_cores", "gauge", "Configured CPU limit (cores)", CPU_LIMIT)
    m("process_resident_memory_bytes", "gauge", "Resident memory", rss)
    m("app_memory_limit_bytes", "gauge", "Configured memory limit", MEM_LIMIT)
    m("app_memory_utilization_ratio", "gauge", "RSS as a ratio of the memory limit", round(rss / MEM_LIMIT, 4))
    out.append("# HELP app_chaos_active Chaos injection currently active\n# TYPE app_chaos_active gauge")
    out.append(f'app_chaos_active{{type="cpu"}} {1 if now < STATE.cpu_until else 0}')
    out.append(f'app_chaos_active{{type="memory"}} {1 if STATE.mem_blocks else 0}')
    out.append(f'app_chaos_active{{type="health"}} {0 if STATE.health_ok() else 1}')
    out.append("# HELP app_requests_total HTTP requests\n# TYPE app_requests_total counter")
    for (path, code), n in sorted(STATE.requests.items()):
        out.append(f'app_requests_total{{path="{path}",code="{code}"}} {n}')
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "shl-app"

    def log_message(self, *a):  # silence default access log
        pass

    def _send(self, code: int, body, ctype="application/json") -> None:
        data = body if isinstance(body, str) else json.dumps(body)
        raw = data.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        key = (urlparse(self.path).path, code)
        STATE.requests[key] = STATE.requests.get(key, 0) + 1

    def do_POST(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            if p in ("/health", "/ready"):
                ok = STATE.health_ok()
                return self._send(200 if ok else 503, {"status": "ok" if ok else "unhealthy"})
            if p == "/metrics":
                return self._send(200, render_metrics(), "text/plain; version=0.0.4")
            if p == "/work":
                t_end = time.time() + min(float(q.get("ms", "20")), 500) / 1000
                while time.time() < t_end:
                    pass
                return self._send(200, {"result": "done"})
            if p == "/":
                with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as f:
                    html = f.read()
                return self._send(200, html, "text/html; charset=utf-8")
            if p.startswith("/chaos/"):
                if not CHAOS_ENABLED:
                    return self._send(403, {"error": "chaos disabled"})
                what = p[len("/chaos/"):]
                log("chaos_requested", kind=what, params=q)
                if what == "cpu":
                    return self._send(200, chaos_cpu(float(q.get("seconds", "45")), int(q.get("threads", "1"))))
                if what == "memory":
                    return self._send(200, chaos_memory(int(q.get("mb", "150")), float(q.get("seconds", "60"))))
                if what == "health-fail":
                    return self._send(200, chaos_health(float(q.get("seconds", "0"))))
                if what == "crash":
                    return self._send(200, chaos_crash())
                if what == "reset":
                    return self._send(200, chaos_reset())
            return self._send(404, {"error": "not found"})
        except Exception as e:  # never let a bad request kill the server
            return self._send(500, {"error": str(e)})


def _term(*_):
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _term)
    threading.Thread(target=sampler, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    log("started", port=PORT, memory_limit_bytes=MEM_LIMIT, cpu_limit=CPU_LIMIT, chaos=CHAOS_ENABLED)
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    sys.exit(main())
