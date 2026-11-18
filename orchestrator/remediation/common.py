"""Shared helpers: subprocess runner, HTTP probe, Prometheus text parser."""
from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from typing import Optional, Tuple


def run(cmd, timeout: int = 30) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def http_get(url: str, timeout: float = 2.0) -> Tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def parse_prom(text: str) -> dict:
    """Parse unlabelled samples from Prometheus text format into {name: value}."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and "{" not in parts[0]:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return out


def ratios(metrics_text: str) -> Tuple[Optional[float], Optional[float]]:
    m = parse_prom(metrics_text)
    return m.get("app_cpu_utilization_ratio"), m.get("app_memory_utilization_ratio")


def probe_app(base_url: str):
    """Returns (healthy, cpu_ratio, memory_ratio) by calling the app over HTTP."""
    code, _ = http_get(base_url + "/health")
    cpu = mem = None
    code2, body = http_get(base_url + "/metrics")
    if code2 == 200:
        cpu, mem = ratios(body)
    return code == 200, cpu, mem
