#!/usr/bin/env python3
"""Generates dashboards/self-healing.json. Every query targets a real metric exported by the app
(app_*), Prometheus (up) or the orchestrator (shl_*). Run: python3 monitoring/grafana/generate_dashboard.py"""
import json
import os

DS = {"type": "prometheus", "uid": "prometheus"}
_id = [0]


def _base(title, typ, x, y, w, h, targets, **extra):
    _id[0] += 1
    return {"id": _id[0], "title": title, "type": typ, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets, **extra}


def tgt(expr, legend="", instant=False, fmt="time_series", ref="A"):
    t = {"refId": ref, "datasource": DS, "expr": expr, "legendFormat": legend, "instant": instant,
         "range": not instant, "format": fmt}
    return t


def stat(title, expr, x, y, unit="none", steps=None, mappings=None, w=3):
    steps = steps or [{"color": "green", "value": None}]
    return _base(title, "stat", x, y, w, 4, [tgt(expr, instant=True)],
                 options={"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none"},
                 fieldConfig={"defaults": {"unit": unit, "mappings": mappings or [],
                                           "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []})


def ts(title, targets, x, y, w=12, h=8, unit="none", mn=None, mx=None, draw="line"):
    d = {"unit": unit, "custom": {"drawStyle": draw, "lineWidth": 2, "fillOpacity": 10}}
    if mn is not None:
        d["min"] = mn
    if mx is not None:
        d["max"] = mx
    return _base(title, "timeseries", x, y, w, h, targets, fieldConfig={"defaults": d, "overrides": []},
                 options={"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}})


RED_GREEN = [{"color": "red", "value": None}, {"color": "green", "value": 1}]
GREEN_RED = [{"color": "green", "value": None}, {"color": "red", "value": 1}]
UPDOWN = [{"type": "value", "options": {"0": {"text": "DOWN"}, "1": {"text": "UP"}}}]

panels = [
    stat("Workloads healthy", "min(shl_workload_healthy)", 0, 0, steps=RED_GREEN,
         mappings=[{"type": "value", "options": {"0": {"text": "DEGRADED"}, "1": {"text": "HEALTHY"}}}]),
    stat("App availability (Prometheus up)", 'min(up{job="app"})', 3, 0, steps=RED_GREEN, mappings=UPDOWN),
    stat("Active incidents", "sum(shl_incidents_active) or vector(0)", 6, 0, steps=GREEN_RED),
    stat("Incidents detected (1h)", "sum(increase(shl_incidents_detected_total[1h])) or vector(0)", 9, 0,
         steps=[{"color": "blue", "value": None}]),
    stat("Remediations OK (1h)", 'sum(increase(shl_remediation_attempts_total{result="success"}[1h])) or vector(0)',
         12, 0, steps=[{"color": "green", "value": None}]),
    stat("Remediations failed (1h)", 'sum(increase(shl_remediation_attempts_total{result="failure"}[1h])) or vector(0)',
         15, 0, steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]),
    stat("Escalations (1h)", "sum(increase(shl_incidents_escalated_total[1h])) or vector(0)", 18, 0,
         steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    stat("Mean recovery time (1h)",
         "sum(increase(shl_recovery_seconds_sum[1h])) / sum(increase(shl_recovery_seconds_count[1h]))",
         21, 0, unit="s"),

    ts("CPU utilisation (ratio of limit)", [tgt("shl_instance_cpu_ratio", "{{workload}} / {{instance}}")],
       0, 4, unit="percentunit", mn=0),
    ts("Memory utilisation (ratio of limit)", [tgt("shl_instance_memory_ratio", "{{workload}} / {{instance}}")],
       12, 4, unit="percentunit", mn=0),
    ts("Instance status (1 = running, ready, healthy)", [tgt("shl_instance_up", "{{instance}}")],
       0, 12, mn=0, mx=1, draw="points"),
    ts("Replicas: ready vs desired",
       [tgt("shl_workload_ready_replicas", "ready {{workload}}", ref="A"),
        tgt("shl_workload_desired_replicas", "desired {{workload}}", ref="B")], 12, 12, mn=0),
    ts("Pod / container restarts", [tgt("shl_instance_restarts", "{{instance}}")], 0, 20, mn=0),
    ts("Application availability (app_health_status)", [tgt("app_health_status", "{{instance}}")],
       12, 20, mn=0, mx=1, draw="points"),

    ts("Incidents detected by type (5m window)",
       [tgt("sum by (failure_type) (increase(shl_incidents_detected_total[5m]))", "{{failure_type}}")],
       0, 28, draw="bars"),
    ts("Remediation attempts by action / result (5m window)",
       [tgt('sum by (action, result) (increase(shl_remediation_attempts_total[5m])) > 0', "{{action}} - {{result}}")],
       12, 28, draw="bars"),
    ts("Recovery time (most recent, by failure type)", [tgt("shl_last_recovery_seconds", "{{failure_type}}")],
       0, 36, unit="s", mn=0),
    ts("Orchestrator control loop (cycles / min)", [tgt("rate(shl_cycles_total[1m]) * 60", "cycles/min")],
       12, 36, mn=0),

    _base("Incident history (most recent 15)", "table", 0, 44, 24, 9,
          [tgt("shl_incident_info * 1000", instant=True, fmt="table")],
          options={"showHeader": True, "sortBy": [{"displayName": "Detected", "desc": True}]},
          fieldConfig={"defaults": {}, "overrides": [
              {"matcher": {"id": "byName", "options": "Value"},
               "properties": [{"id": "unit", "value": "dateTimeAsLocal"}, {"id": "displayName", "value": "Detected"}]}]},
          transformations=[{"id": "organize", "options": {
              "excludeByName": {"Time": True, "__name__": True, "job": True, "instance": True},
              "indexByName": {"id": 0, "workload": 1, "failure_type": 2, "status": 3, "action": 4,
                              "attempts": 5, "resolution": 6, "Value": 7}}}]),
]

dashboard = {"uid": "self-healing", "title": "Autonomous Self-Healing Infrastructure", "tags": ["self-healing"],
             "schemaVersion": 39, "version": 1, "refresh": "5s", "time": {"from": "now-30m", "to": "now"},
             "timezone": "browser", "editable": True, "panels": panels}

if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "dashboards", "self-healing.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(dashboard, f, indent=2)
    print(f"wrote {out} ({len(panels)} panels)")
