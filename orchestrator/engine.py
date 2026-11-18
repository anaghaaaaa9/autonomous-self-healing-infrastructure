"""The control loop: observe -> detect -> incident -> remediate -> verify -> record."""
from __future__ import annotations

import time
from typing import Callable

from .detection.detectors import detect, is_recovered
from .logging_setup import ev
from .models import ALL_TYPES
from .policies.ladder import ALL_ACTIONS, plan
from .policies.reliability import ReliabilityPolicy
from .state.tracker import Tracker


class Engine:
    def __init__(self, cfg: dict, backend, store, metrics,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg, self.backend, self.store, self.m = cfg, backend, store, metrics
        self.clock, self.sleep = clock, sleep
        self.policy = ReliabilityPolicy(cfg["policy"])
        self.tracker = Tracker()
        self.interval = cfg["interval_seconds"]
        self._snap: dict = {}
        self._init_metrics()

    # ------------------------------------------------------------------ metrics
    def _init_metrics(self) -> None:
        m = self.m
        m.set("shl_orchestrator_up", 1, help="Orchestrator process is running")
        for w in self.cfg["workloads"]:
            for t in ALL_TYPES:  # pre-create counters at 0 so increase()/rate() work from the first event
                m.inc("shl_incidents_detected_total", {"workload": w["name"], "failure_type": t}, 0,
                      "Incidents opened by the detection engine")
        for a in ALL_ACTIONS:
            for r in ("success", "failure"):
                m.inc("shl_remediation_attempts_total", {"action": a, "result": r}, 0,
                      "Remediation attempts by action and verified result")
        m.inc("shl_cycles_total", None, 0, "Control loop cycles")

    def _g(self, name, labels, value) -> None:
        self._snap.setdefault(name, []).append((labels, value))

    def _export_obs(self, w, obs, raw, active) -> None:
        n = w["name"]
        ready = 0
        for i in obs.instances:
            lab = {"workload": n, "instance": i.name}
            up = 1 if (i.running and i.ready and i.healthy is not False) else 0
            ready += up
            self._g("shl_instance_up", lab, up)
            self._g("shl_instance_restarts", lab, i.restarts)
            if i.cpu is not None:
                self._g("shl_instance_cpu_ratio", lab, i.cpu)
            if i.memory is not None:
                self._g("shl_instance_memory_ratio", lab, i.memory)
        self._g("shl_workload_desired_replicas", {"workload": n}, obs.desired)
        self._g("shl_workload_ready_replicas", {"workload": n}, ready)
        self._g("shl_workload_healthy", {"workload": n}, 0 if (raw or active) else 1)

    def _export_incidents(self) -> None:
        counts: dict = {}
        for inc in self.store.active():
            k = (inc["workload"], inc["failure_type"], inc["status"])
            counts[k] = counts.get(k, 0) + 1
        self.m.replace("shl_incidents_active",
                       [({"workload": a, "failure_type": b, "status": c}, v) for (a, b, c), v in counts.items()],
                       "Currently active incidents")
        hist = self.store.history(15)
        self.m.replace("shl_incident_info",
                       [({"id": h["id"], "workload": h["workload"], "failure_type": h["failure_type"],
                          "status": h["status"], "action": h["action"] or "-",
                          "attempts": h["attempts"], "resolution": h["resolution"] or "-"}, h["detected_at"])
                        for h in hist],
                       "Recent incidents (value = detection time, unix seconds)")

    # ------------------------------------------------------------------ loop
    def run_forever(self, stop=lambda: False) -> None:
        ev("info", "engine_started", backend=self.cfg["backend"], interval=self.interval,
           workloads=[w["name"] for w in self.cfg["workloads"]])
        while not stop():
            t0 = self.clock()
            self.run_cycle()
            self.sleep(max(0.0, self.interval - (self.clock() - t0)))

    def run_cycle(self) -> None:
        t0 = self.clock()
        self._snap = {}
        for w in self.cfg["workloads"]:
            try:
                self._process(w)
            except Exception as e:  # noqa: BLE001 - one bad workload must not stop the loop
                ev("error", "cycle_error", workload=w["name"], error=str(e))
                self.m.inc("shl_cycle_errors_total", {"workload": w["name"]}, 1, "Observation/processing errors")
        for name, items in self._snap.items():
            self.m.replace(name, items)
        self._export_incidents()
        self.m.inc("shl_cycles_total")
        self.m.set("shl_cycle_duration_seconds", self.clock() - t0, help="Duration of the last cycle")

    def _process(self, w: dict) -> None:
        name = w["name"]
        obs = self.backend.observe(w)
        raw = detect(obs, self.cfg, self.tracker.restarts_snapshot(name))
        self.tracker.save_restarts(name, obs)
        confirmed = self.tracker.update(name, set(raw), self.cfg["consecutive_cycles"])

        for t in sorted(confirmed):
            if self.store.find_active(name, t):          # duplicate suppression
                continue
            inc = self.store.open_incident(name, t, raw[t], self.clock())
            self.m.inc("shl_incidents_detected_total", {"workload": name, "failure_type": t})
            ev("warning", "incident_opened", incident=inc["id"], workload=name, failure_type=t, detail=raw[t])

        active = self.store.active(name)
        self._export_obs(w, obs, raw, active)
        for inc in active:
            self._handle(w, inc, obs, raw)
        self._maybe_scale_in(w, bool(self.store.active(name)))

    # ------------------------------------------------------------------ per-incident logic
    def _handle(self, w, inc, obs, raw) -> None:
        t, now = inc["failure_type"], self.clock()
        if is_recovered(obs, t, self.cfg):
            res = ("recovered_after_escalation" if inc["status"] == "escalated"
                   else "self_recovered" if inc["attempts"] == 0 else "recovered_between_attempts")
            self._resolve(inc, res)
            return
        if inc["status"] == "escalated":
            return                                         # humans own it now; never loop
        grace = self.cfg["grace_cycles"].get(t, 0) * self.interval
        if inc["attempts"] == 0 and now - inc["detected_at"] < grace:
            return                                         # give the platform time to self-heal
        if self.policy.circuit_open(self.store.count_attempts_since(w["name"], now - 3600)):
            self._escalate(inc, "circuit_breaker")
            return
        if self.policy.exhausted(inc["attempts"]):
            self._escalate(inc, "max_attempts_exhausted")
            return
        last = self.store.last_attempt_end(inc["id"])
        if last is not None and now < self.policy.next_allowed(last, inc["attempts"]):
            return                                         # exponential backoff / cooldown
        self._remediate(w, inc, raw.get(t) or inc["detail"])

    def _remediate(self, w, inc, detail) -> None:
        t, n = inc["failure_type"], inc["attempts"] + 1
        steps = plan(self.cfg["backend"], t, detail)
        action = steps[min(n - 1, len(steps) - 1)]
        target = (detail.get("instances") or [None])[0]
        self.store.update(inc["id"], status="remediating", action=action)
        started = self.clock()
        ev("info", "remediation_started", incident=inc["id"], workload=w["name"], action=action,
           attempt=n, target=target)
        ok, msg = self.backend.execute(action, w, target)
        verified = False
        if ok:
            if action == "scale_out":
                self.tracker.scaled_out_at[w["name"]] = self.clock()
            verified = self._verify(w, t)
        ended = self.clock()
        self.store.record_attempt(inc["id"], n, action, started, ended, verified,
                                  msg if (ok and verified) else (msg if not ok else f"{msg} | verification failed"))
        self.m.inc("shl_remediation_attempts_total", {"action": action, "result": "success" if verified else "failure"})
        ev("info" if verified else "error", "remediation_finished", incident=inc["id"], action=action,
           attempt=n, executed=ok, verified=verified, message=msg)
        if verified:
            self.store.update(inc["id"], attempts=n)
            self._resolve(self.store.get(inc["id"]), "remediated", action)
        else:
            self.store.update(inc["id"], status="open", attempts=n, recovery_status="failed", action=action)

    def _verify(self, w, ftype) -> bool:
        p = self.cfg["policy"]
        deadline = self.clock() + p["verify_timeout_seconds"]
        good = 0
        while self.clock() < deadline:
            self.sleep(p["verify_interval_seconds"])
            try:
                ok = is_recovered(self.backend.observe(w), ftype, self.cfg)
            except Exception:  # noqa: BLE001
                ok = False
            good = good + 1 if ok else 0
            if good >= p["verify_consecutive"]:
                return True
        return False

    def _resolve(self, inc, resolution, action=None) -> None:
        done = self.store.resolve(inc["id"], self.clock(), resolution, action)
        rs, t = done["recovery_seconds"], done["failure_type"]
        self.m.inc("shl_incidents_resolved_total",
                   {"workload": done["workload"], "failure_type": t, "resolution": resolution},
                   1, "Resolved incidents")
        self.m.observe("shl_recovery_seconds", rs, {"failure_type": t}, "Detection-to-verified-recovery time")
        self.m.set("shl_last_recovery_seconds", rs, {"failure_type": t}, "Most recent recovery time")
        ev("info", "incident_resolved", incident=done["id"], workload=done["workload"], failure_type=t,
           resolution=resolution, action=done["action"], recovery_seconds=round(rs, 2))

    def _escalate(self, inc, reason) -> None:
        self.store.escalate(inc["id"], self.clock(), reason)
        self.m.inc("shl_incidents_escalated_total",
                   {"workload": inc["workload"], "failure_type": inc["failure_type"], "reason": reason},
                   1, "Incidents escalated to humans")
        ev("critical", "incident_escalated", incident=inc["id"], workload=inc["workload"],
           failure_type=inc["failure_type"], reason=reason, attempts=inc["attempts"])

    def _maybe_scale_in(self, w, has_active: bool) -> None:
        at = self.tracker.scaled_out_at.get(w["name"])
        if at is None or has_active:
            return
        if self.clock() - at >= self.cfg["scale"]["scale_in_after_seconds"]:
            ok, msg = self.backend.execute("scale_in", w, None)
            ev("info", "scale_in", workload=w["name"], ok=ok, message=msg)
            self.m.inc("shl_remediation_attempts_total", {"action": "scale_in", "result": "success" if ok else "failure"})
            if ok:
                del self.tracker.scaled_out_at[w["name"]]
