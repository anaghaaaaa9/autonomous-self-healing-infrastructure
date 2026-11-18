"""Backends: observe a workload and execute remediation actions.

observe(w)  -> Observation     (raises RuntimeError if the platform itself is unreachable)
execute(a, w, target) -> (ok, message)
"""
from __future__ import annotations

import atexit
import json
import os
import shlex
import subprocess
import sys
import time
from typing import Optional, Tuple

from ..models import Instance, Observation
from .ansible_exec import PLAYBOOKS, run_playbook
from .common import probe_app, ratios, run

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class _Base:
    name = "base"

    def __init__(self, cfg: dict, runner=run) -> None:
        self.cfg = cfg
        self.run = runner
        self.use_ansible = cfg.get("executor") == "ansible"

    def _ansible(self, action, extra) -> Optional[Tuple[bool, str]]:
        if self.use_ansible and action in PLAYBOOKS:
            return run_playbook(action, extra, self.run)
        return None


# --------------------------------------------------------------------------- docker
class DockerBackend(_Base):
    name = "docker"

    def observe(self, w: dict) -> Observation:
        c = w["container"]
        rc, out = self.run(["docker", "inspect", c])
        if rc != 0:
            if "no such" in out.lower():
                return Observation(w["name"], "docker", [Instance(c, running=False, ready=False, reason="NotFound")])
            raise RuntimeError(f"docker unavailable: {out[:150]}")
        info = json.loads(out)[0]
        st = info["State"]
        running = bool(st.get("Running"))
        reason = "OOMKilled" if st.get("OOMKilled") else st.get("Status", "")
        inst = Instance(c, running=running, ready=running, restarts=info.get("RestartCount", 0), reason=reason)
        if running:
            inst.healthy, inst.cpu, inst.memory = probe_app(w["url"])
        return Observation(w["name"], "docker", [inst])

    def execute(self, action: str, w: dict, target: Optional[str] = None) -> Tuple[bool, str]:
        c = w["container"]
        a = self._ansible(action, {"container": c})
        if a:
            return a
        if action == "start_instance":
            cmd = ["docker", "start", c]
        elif action == "restart_instance":
            cmd = ["docker", "restart", "-t", "10", c]
        else:
            return False, f"unsupported action {action} for docker"
        rc, out = self.run(cmd, 60)
        return rc == 0, f"{' '.join(cmd)} -> rc={rc} {out[:120]}"


# --------------------------------------------------------------------------- local processes
class LocalBackend(_Base):
    """Runs the app as a child process. For development/CI without Docker."""
    name = "local"

    def __init__(self, cfg: dict, runner=run) -> None:
        super().__init__(cfg, runner)
        self.procs: dict = {}
        atexit.register(self.cleanup)

    def _spawn(self, w: dict) -> None:
        env = os.environ.copy()
        env.update({k: str(v) for k, v in (w.get("env") or {}).items()})
        cmd = shlex.split(w.get("command", "{python} application/app.py").replace("{python}", sys.executable))
        self.procs[w["name"]] = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def bootstrap(self, cfg: dict) -> None:
        for w in cfg["workloads"]:
            self._spawn(w)
            for _ in range(50):  # wait for readiness
                if probe_app(w["url"])[0]:
                    break
                time.sleep(0.1)

    def observe(self, w: dict) -> Observation:
        p = self.procs.get(w["name"])
        running = p is not None and p.poll() is None
        inst = Instance(w["name"], running=running, ready=running,
                        reason="" if running else f"exit code {p.returncode if p else 'n/a'}")
        if running:
            inst.healthy, inst.cpu, inst.memory = probe_app(w["url"])
        return Observation(w["name"], "local", [inst])

    def execute(self, action: str, w: dict, target: Optional[str] = None) -> Tuple[bool, str]:
        p = self.procs.get(w["name"])
        alive = p is not None and p.poll() is None
        if action == "start_instance" and alive:
            return True, "already running"
        if action in ("start_instance", "restart_instance"):
            if alive:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
            self._spawn(w)
            return True, f"{action}: spawned new process pid={self.procs[w['name']].pid}"
        return False, f"unsupported action {action} for local"

    def cleanup(self) -> None:
        for p in self.procs.values():
            if p.poll() is None:
                p.terminate()


# --------------------------------------------------------------------------- kubernetes
class KubeBackend(_Base):
    name = "kubernetes"

    def _k(self, *args, timeout: int = 30):
        cmd = ["kubectl"]
        if self.cfg.get("kube_context"):
            cmd += ["--context", self.cfg["kube_context"]]
        return self.run(cmd + ["--request-timeout=5s"] + list(args), timeout)

    @staticmethod
    def _ns(w):  # namespace
        return w.get("namespace", "shl")

    def _probe(self, ns: str, pod: str, port: int):
        base = f"/api/v1/namespaces/{ns}/pods/{pod}:{port}/proxy"
        rc, _ = self._k("get", "--raw", base + "/health", timeout=10)
        cpu = mem = None
        rc2, body = self._k("get", "--raw", base + "/metrics", timeout=10)
        if rc2 == 0:
            cpu, mem = ratios(body)
        return rc == 0, cpu, mem

    def observe(self, w: dict) -> Observation:
        ns, dep = self._ns(w), w["deployment"]
        sel = w.get("selector", f"app={dep}")
        rc, out = self._k("-n", ns, "get", "deployment", dep, "-o", "json")
        if rc != 0:
            raise RuntimeError(f"kubectl get deployment failed: {out[:150]}")
        desired = json.loads(out)["spec"].get("replicas", 1)
        rc, out = self._k("-n", ns, "get", "pods", "-l", sel, "-o", "json")
        if rc != 0:
            raise RuntimeError(f"kubectl get pods failed: {out[:150]}")
        insts = []
        for item in json.loads(out)["items"]:
            if item["metadata"].get("deletionTimestamp"):
                continue  # terminating pods are ignored
            st = item.get("status", {})
            phase = st.get("phase", "")
            cs = (st.get("containerStatuses") or [None])[0]
            running, reason, restarts = False, "", 0
            if cs:
                restarts = cs.get("restartCount", 0)
                s = cs.get("state", {})
                if "running" in s:
                    running = True
                elif "waiting" in s:
                    reason = s["waiting"].get("reason", "Waiting")
                elif "terminated" in s:
                    reason = s["terminated"].get("reason", "Terminated")
            else:
                reason = phase or "Pending"
            if phase in ("Failed", "Unknown"):
                running, reason = False, reason or phase
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions", []))
            inst = Instance(item["metadata"]["name"], running=running, ready=ready, restarts=restarts, reason=reason)
            if running:
                inst.healthy, inst.cpu, inst.memory = self._probe(ns, inst.name, w.get("port", 8080))
            insts.append(inst)
        return Observation(w["name"], "kubernetes", insts, desired=desired)

    def _replicas(self, w) -> int:
        rc, out = self._k("-n", self._ns(w), "get", "deployment", w["deployment"], "-o", "json")
        if rc != 0:
            raise RuntimeError(out[:150])
        return json.loads(out)["spec"].get("replicas", 1)

    def execute(self, action: str, w: dict, target: Optional[str] = None) -> Tuple[bool, str]:
        ns, dep = self._ns(w), w["deployment"]
        try:
            if action in ("scale_out", "scale_in"):
                cur = self._replicas(w)
                if action == "scale_out":
                    new = min(cur + 1, self.cfg["scale"]["max_replicas"])
                    if new == cur:
                        return False, f"already at max replicas ({cur})"
                else:
                    new = w.get("replicas", 1)
                    if new == cur:
                        return True, "already at base replicas"
                a = self._ansible(action, {"namespace": ns, "deployment": dep, "replicas": new})
                if a:
                    return a
                rc, out = self._k("-n", ns, "scale", f"deployment/{dep}", f"--replicas={new}")
                return rc == 0, f"scale {cur}->{new}: {out[:120]}"
            if action == "delete_pod":
                if not target:
                    obs = self.observe(w)
                    bad = [i for i in obs.instances if not (i.running and i.ready)]
                    target = (bad or obs.instances or [None])[0]
                    target = target.name if target else None
                if not target:
                    return False, "no pod to delete"
                a = self._ansible(action, {"namespace": ns, "pod": target})
                if a:
                    return a
                rc, out = self._k("-n", ns, "delete", "pod", target, "--wait=false")
                return rc == 0, out[:150]
            if action == "restart_deployment":
                a = self._ansible(action, {"namespace": ns, "deployment": dep})
                if a:
                    return a
                rc, out = self._k("-n", ns, "rollout", "restart", f"deployment/{dep}")
                return rc == 0, out[:150]
            if action == "rollout_undo":
                a = self._ansible(action, {"namespace": ns, "deployment": dep})
                if a:
                    return a
                rc, out = self._k("-n", ns, "rollout", "undo", f"deployment/{dep}")
                return rc == 0, out[:150]
        except RuntimeError as e:
            return False, str(e)
        return False, f"unsupported action {action} for kubernetes"


def make_backend(cfg: dict, runner=run):
    kind = cfg["backend"]
    if kind == "docker":
        return DockerBackend(cfg, runner)
    if kind == "kubernetes":
        return KubeBackend(cfg, runner)
    if kind == "local":
        return LocalBackend(cfg, runner)
    raise ValueError(f"unknown backend {kind}")
