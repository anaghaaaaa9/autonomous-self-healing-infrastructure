#!/usr/bin/env python3
"""Failure injection + live demo tracker.

  python3 scripts/chaos/chaos.py <scenario> [--backend docker|kubernetes|local] [--no-wait]

Scenarios: container-crash | pod-kill | bad-image | cpu | memory | health-fail | reset
After injecting, it polls the orchestrator (http://localhost:9100/incidents) and prints the
Detection -> Incident -> Remediation -> Recovery timeline until the incident is resolved/escalated.
Resource use is bounded by the app (cpu <= 2 threads / 120 s, memory <= MAX_CHAOS_MB).
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request

APP_URL = {"docker": "http://localhost:8081", "local": "http://localhost:8081"}
EXPECT = {"container-crash": {"container_crash"}, "pod-kill": {"pod_failure", "container_crash"},
          "bad-image": {"pod_failure"}, "cpu": {"high_cpu"}, "memory": {"high_memory"},
          "health-fail": {"health_check_failure"}}


def sh(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{p.stdout}{p.stderr}")
    return p.stdout.strip()


def http(url):
    try:
        return urllib.request.urlopen(url, timeout=5).read().decode()
    except Exception as e:  # crash drops the connection
        return f"({type(e).__name__})"


def kube_pods(ns, sel):
    out = sh(["kubectl", "-n", ns, "get", "pods", "-l", sel, "-o", "jsonpath={.items[*].metadata.name}"])
    pods = out.split()
    if not pods:
        sys.exit("no pods found - is the cluster up? (make kind-up)")
    return pods


def kube_call(ns, pod, path):
    return sh(["kubectl", "-n", ns, "get", "--raw", f"/api/v1/namespaces/{ns}/pods/{pod}:8080/proxy{path}"])


def inject(a):
    s, be = a.scenario, a.backend
    if s == "reset":
        if be == "kubernetes":
            for p in kube_pods(a.namespace, "app=shl-app"):
                print(f"reset {p}: {kube_call(a.namespace, p, '/chaos/reset')}")
        else:
            print(http(APP_URL[be] + "/chaos/reset"))
        return
    qs = {"cpu": f"/chaos/cpu?seconds={a.duration}&threads=1",
          "memory": f"/chaos/memory?mb={a.mb}&seconds={a.duration}",
          "health-fail": "/chaos/health-fail", "container-crash": "/chaos/crash"}
    if be == "kubernetes":
        pods = kube_pods(a.namespace, "app=shl-app")
        if s == "pod-kill":
            print(f"deleting pod {pods[0]}")
            sh(["kubectl", "-n", a.namespace, "delete", "pod", pods[0], "--wait=false"])
        elif s == "bad-image":
            print("rolling out a nonexistent image tag (old pods keep serving: maxUnavailable=0)")
            sh(["kubectl", "-n", a.namespace, "set", "image", "deployment/shl-app", "app=shl-app:does-not-exist"])
        else:
            print(f"{s} on pod {pods[0]} -> {kube_call(a.namespace, pods[0], qs[s])}")
        return
    if s in ("pod-kill", "bad-image"):
        sys.exit(f"{s} is Kubernetes-only (use --backend kubernetes)")
    if s == "container-crash" and be == "docker":
        print("docker kill shl-app")
        sh(["docker", "kill", "shl-app"])
    else:
        print(f"{s}: {http(APP_URL[be] + qs[s])}")


def track(a, t0):
    seen, start = {}, time.time()
    print(f"[t+0s] failure injected ({a.scenario}); waiting for the orchestrator on :{a.orch_port} ...")
    while time.time() - start < a.timeout:
        try:
            rows = json.loads(http(f"http://localhost:{a.orch_port}/incidents"))
        except ValueError:
            print("cannot reach orchestrator - is `make run` running?")
            return 2
        for r in sorted((r for r in rows if r["detected_at"] >= t0 - 1), key=lambda r: r["detected_at"]):
            sig = (r["status"], r["attempts"], r["resolution"])
            if seen.get(r["id"]) == sig:
                continue
            first = r["id"] not in seen
            seen[r["id"]] = sig
            t = f"[t+{time.time() - start:.0f}s]"
            if first:
                print(f"{t} DETECTED  incident {r['id']} type={r['failure_type']} workload={r['workload']}")
            for at in r.get("attempt_log", []):
                if at["attempt"] == r["attempts"] and r["status"] != "open" or at["attempt"] <= r["attempts"]:
                    key = ("att", r["id"], at["attempt"])
                    if key not in seen:
                        seen[key] = 1
                        print(f"{t} REMEDIATE attempt {at['attempt']} action={at['action']} "
                              f"verified={'yes' if at['success'] else 'NO'}")
            if r["status"] == "resolved":
                print(f"{t} RECOVERED incident {r['id']} resolution={r['resolution']} "
                      f"recovery_time={r['recovery_seconds']:.1f}s attempts={r['attempts']}")
            elif r["status"] == "escalated":
                print(f"{t} ESCALATED incident {r['id']} reason={r['resolution']}")
        done = [r for r in rows if r["detected_at"] >= t0 - 1 and r["status"] in ("resolved", "escalated")]
        if done and not [r for r in rows if r["detected_at"] >= t0 - 1 and r["status"] in ("open", "remediating")]:
            print("done - see history with: make incidents")
            return 0
        time.sleep(1)
    print(f"timeout after {a.timeout}s - check orchestrator logs")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=[*EXPECT, "reset"])
    ap.add_argument("--backend", default="docker", choices=["docker", "kubernetes", "local"])
    ap.add_argument("--namespace", default="shl")
    ap.add_argument("--duration", type=int, default=90, help="cpu/memory chaos seconds (app caps at 120)")
    ap.add_argument("--mb", type=int, default=150, help="memory to allocate (app caps at MAX_CHAOS_MB)")
    ap.add_argument("--orch-port", type=int, default=9100)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--no-wait", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    inject(a)
    if a.scenario == "reset" or a.no_wait:
        return 0
    return track(a, t0)


if __name__ == "__main__":
    sys.exit(main())
