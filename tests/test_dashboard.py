"""Every metric the Grafana dashboard queries must really be exported (no invented series)."""
import json
import os
import re

from conftest import FakeBackend

from orchestrator.engine import Engine

DASH = os.path.join(os.path.dirname(__file__), "..", "monitoring", "grafana", "dashboards", "self-healing.json")
APP_METRICS = {"app_health_status", "up"}          # exported by the app / by Prometheus itself


def test_dashboard_queries_only_use_exported_metrics(cfg, store, metrics, clock):
    # produce one resolved incident and one escalated incident so every shl_* series exists
    ok = FakeBackend("docker", fixes={"start_instance": ["down"]})
    ok.flags["down"] = True
    e = Engine(cfg, ok, store, metrics, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        e.run_cycle(); clock.t += 5
    bad = FakeBackend("docker", fixes={})
    bad.flags["cpu"] = 0.99
    bad.flags["mem"] = 0.10
    e2 = Engine(cfg, bad, store, metrics, clock=clock, sleep=clock.sleep)
    for _ in range(80):
        e2.run_cycle(); clock.t += 5
    exported = set(re.findall(r"^(\w+?)(?:_bucket|_sum|_count)?[{ ]", metrics.render(), re.M)) | APP_METRICS
    exported |= set(re.findall(r"^# TYPE (\w+)", metrics.render(), re.M))
    dash = json.load(open(DASH))
    used = set()
    for p in dash["panels"]:
        for t in p["targets"]:
            used |= set(re.findall(r"\b((?:shl|app)_\w+|up)\b", t["expr"]))
    used = {re.sub(r"_(sum|count|bucket)$", "", u) if u.startswith("shl_recovery_seconds") else u for u in used}
    missing = {u for u in used if u not in exported}
    assert not missing, f"dashboard references metrics nobody exports: {missing}"
