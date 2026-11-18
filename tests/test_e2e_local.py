"""End-to-end: real app process + real engine + real chaos, no mocks (local backend, no Docker needed).

For each scenario: inject -> detect -> incident -> remediate -> verify recovery -> history recorded.
"""
import copy
import time
import urllib.request

import pytest

from orchestrator.config import DEFAULTS
from orchestrator.engine import Engine
from orchestrator.incidents.store import IncidentStore
from orchestrator.metrics import Registry
from orchestrator.remediation.backends import LocalBackend

PORT = 18081
URL = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def system():
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(backend="local", interval_seconds=1)
    cfg["thresholds"] = {"cpu_ratio": 0.8, "memory_ratio": 0.8}
    cfg["consecutive_cycles"] = {"container_crash": 1, "high_cpu": 2, "high_memory": 2, "health_check_failure": 2}
    cfg["policy"].update(base_backoff_seconds=3, verify_timeout_seconds=25, verify_interval_seconds=1)
    cfg["workloads"] = [{"name": "shl-app", "url": URL, "command": "{python} application/app.py",
                         "env": {"PORT": str(PORT), "APP_MEMORY_LIMIT_MB": "128",
                                 "APP_CPU_LIMIT": "0.5", "MAX_CHAOS_MB": "100"}}]
    backend = LocalBackend(cfg)
    backend.bootstrap(cfg)
    store, reg = IncidentStore(":memory:"), Registry()
    engine = Engine(cfg, backend, store, reg)
    yield engine, store, reg
    backend.cleanup()


def hit(path):
    try:
        urllib.request.urlopen(URL + path, timeout=3).read()
    except Exception:
        pass  # /chaos/crash drops the connection


def run_until_resolved(engine, store, ftype, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        engine.run_cycle()
        inc = next((i for i in store.history() if i["failure_type"] == ftype), None)
        if inc and inc["status"] in ("resolved", "escalated"):
            return inc
        time.sleep(1)
    raise AssertionError(f"{ftype} not resolved in {timeout}s: {store.history()}")


@pytest.mark.parametrize("scenario,path,ftype", [
    ("container failure", "/chaos/crash", "container_crash"),
    ("health-check failure", "/chaos/health-fail", "health_check_failure"),
    ("high memory", "/chaos/memory?mb=100&seconds=120", "high_memory"),
    ("high cpu", "/chaos/cpu?seconds=100", "high_cpu"),
])
def test_scenario(system, scenario, path, ftype):
    engine, store, reg = system
    engine.run_cycle()
    assert store.active() == [], "system must be healthy before injection"
    hit(path)                                                    # 1. failure injection
    inc = run_until_resolved(engine, store, ftype)               # 2-5. detect, incident, remediate, verify
    assert inc["status"] == "resolved", inc
    assert inc["resolution"] == "remediated" and inc["recovery_status"] == "verified"
    assert inc["attempts"] >= 1 and inc["recovery_seconds"] > 0
    attempts = store.attempts_for(inc["id"])
    assert attempts and attempts[-1]["success"] == 1
    assert reg.get("shl_incidents_resolved_total",
                   {"workload": "shl-app", "failure_type": ftype, "resolution": "remediated"}) == 1
    # service is genuinely healthy again
    assert urllib.request.urlopen(URL + "/health", timeout=3).status == 200
