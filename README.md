# Autonomous Self-Healing Infrastructure

A control loop that **monitors** a containerised service, **detects** five classes of failure, **opens incidents**,
**remediates** with an escalating ladder of actions, **verifies** the recovery, and **records** everything in SQLite and
Prometheus - with guard-rails so it can never get stuck in a remediation loop.

Runs on a MacBook Air M1 / 8 GB: the app, orchestrator and metrics are pure Python stdlib; Prometheus + Grafana are
capped at 256 MB each; Kind is a single node.

```
                         +-------------------- orchestrator (Python) ---------------------+
                         |  observe -> detect -> incident -> remediate -> verify -> record |
  failure injection      |   backends/        detectors     SQLite      ladders    policies|
  scripts/chaos/chaos.py |  docker|kubectl|local  + tracker   store     (+Ansible)  backoff |
        |                +-----+---------------------------------------------------+-------+
        v                      | docker CLI / kubectl (API-server pod proxy)       | /metrics :9100
  +-------------+   health,    v                                                   v
  | sample app  |<--cpu,mem----+                                            +------------+      +---------+
  | /health     |   probes                                                  | Prometheus |----->| Grafana |
  | /metrics    |------------------------ /metrics ----------------------->  +------------+      +---------+
  | /chaos/*    |     Docker container  or  Kind pods (Deployment, 2 replicas, RBAC)
  +-------------+
```

## What it detects and how it heals

| Failure | Detection (after N consecutive 5 s cycles) | Docker / local remediation | Kubernetes remediation ladder |
|---|---|---|---|
| Container crash | container not running (N=1) | `start` -> `restart` | restart-count jump; wait for kubelet, then `delete pod` -> `rollout restart` |
| Pod failure | Failed/Unknown/CrashLoop/ImagePull, missing pod (N=2) | - | wait for ReplicaSet; bad image -> `rollout undo`; else `delete pod` -> `rollout restart` |
| High CPU | CPU > 80 % of limit (N=3) | controlled `restart` | `scale out` (max 3) -> replace hot pod -> `rollout restart`; auto scale-in after 120 s calm |
| High memory | RSS > 85 % of limit (N=3) | controlled `restart` (`-t 10`) | `delete pod` -> `rollout restart` |
| Health-check failure | `/health` != 200 or pod not Ready (N=3) | `restart` | `delete pod` -> `rollout restart` |

**Reliability policies** (`orchestrator/policies`, `config/*.yaml`): retry limit (3), exponential backoff (10 s x 2^n, cap 120 s),
per-workload circuit breaker (6 remediations/hour), duplicate-incident suppression, escalation (`status=escalated`,
CRITICAL log, `shl_incidents_escalated_total`) - an escalated incident is never touched again by automation - and
**recovery verification** (condition must be absent for 2 consecutive probes within a timeout).

**Honest about Kubernetes self-healing:** on Kind, `pod-kill` and a container crash are normally fixed by Kubernetes within
seconds. The orchestrator waits a grace period, *observes* the recovery and records `resolution=self_recovered, attempts=0` -
it does not claim credit. A bad image (`bad-image`) is not self-healing, so the orchestrator acts (`rollout undo`).

## Quick start

Requirements: Python 3.10+, `pip install -r requirements-dev.txt` (PyYAML, pytest). For Docker mode: Docker Desktop.
For Kubernetes mode: `kind`, `kubectl`. Optional: `terraform`, `ansible`.

```bash
make test                      # 30 tests, ~20 s, no Docker needed

# A) Docker-free (fastest sanity check)
make run-local                 # terminal 1: orchestrator runs the app as a child process
python3 scripts/chaos/chaos.py cpu --backend local        # terminal 2

# B) Docker Compose (recommended demo)
make up                        # app :8081, Prometheus :9090, Grafana :3000 (admin/admin)
make run                       # terminal 1: orchestrator (metrics on :9100)
make demo-crash                # terminal 2: also demo-cpu | demo-memory | demo-health
make incidents                 # history table

# C) Kubernetes (Kind)
make kind-up mon-k8s           # cluster + app + Prometheus/Grafana
make run-k8s                   # terminal 1
make demo-bad-image            # terminal 2: also demo-pod-kill | demo-k8s-crash|cpu|memory|health
make kind-down
```
Grafana dashboard: **Self-Healing -> Autonomous Self-Healing Infrastructure** (auto-provisioned, 5 s refresh).

Terraform alternative to `make kind-up`: `cd terraform && terraform init && terraform apply`.
Ansible executor: set `executor: ansible` in the config; remediation then runs `ansible/playbooks/*.yml`.

## Layout

```
application/app.py        sample service: /health /ready /metrics /work /chaos/* (stdlib, bounded chaos)
orchestrator/             engine.py (loop) | detection/ | incidents/ (SQLite) | policies/ | state/ | remediation/ (backends, ansible)
                          metrics.py (Prometheus text) | server.py (:9100) | logging_setup.py (JSON logs) | __main__.py (CLI)
config/                   docker.yaml | kubernetes.yaml | local.yaml  (thresholds, cycles, policy, workloads)
kubernetes/               kind-config, namespace, deployment (limits, probes), service (NodePort), rbac (least privilege)
terraform/                kind cluster + image load + manifests
ansible/playbooks/        container_recover, kube_{delete_pod,restart_deployment,rollout_undo,scale_deployment}
monitoring/               prometheus (compose + k8s), grafana provisioning, generate_dashboard.py -> dashboards/self-healing.json
scripts/chaos/chaos.py    inject failure + live timeline tracker
tests/                    unit, backend (fake runner), real-app, real end-to-end, dashboard-metrics contract
```

## Design notes (good interview material)

- **Backend abstraction.** `observe()` returns backend-neutral `Instance`s; detectors are pure functions; remediation is a
  ladder of named actions executed by `docker`, `kubectl` or Ansible. Adding a backend = two methods.
- **No metric is faked.** The app computes CPU (`process_time` delta / limit) and RSS from the live process; the orchestrator
  exports `shl_*` series from its real state. `tests/test_dashboard.py` fails if the dashboard queries a metric nobody exports.
- **Time is injectable** (`Engine(clock, sleep)`), so backoff, escalation and circuit-breaker behaviour is tested
  deterministically without waiting.
- **Per-pod observation on Kubernetes** goes through the API-server pod proxy (`kubectl get --raw .../pods/<p>:8080/proxy/...`),
  so no port-forwards and RBAC needs only `pods/proxy get`.
- **Trade-offs:** recovery is verified synchronously (the loop blocks while verifying one workload - simple and correct for a
  demo; production would verify asynchronously); the orchestrator runs on the host rather than in-cluster; CPU "scale-out" in
  Compose mode is a restart because fixed host port mappings prevent replicas.

## Metrics (orchestrator `:9100/metrics`)

`shl_incidents_detected_total{workload,failure_type}`, `shl_incidents_active`, `shl_incidents_resolved_total`,
`shl_incidents_escalated_total`, `shl_remediation_attempts_total{action,result}`, `shl_recovery_seconds` (histogram),
`shl_last_recovery_seconds`, `shl_instance_{up,cpu_ratio,memory_ratio,restarts}`, `shl_workload_{healthy,desired_replicas,ready_replicas}`,
`shl_incident_info` (history table), `shl_cycles_total`. App: `app_health_status`, `app_cpu_utilization_ratio`,
`app_memory_utilization_ratio`, `process_resident_memory_bytes`, `app_chaos_active`, `app_requests_total`.
`recovery_seconds` is measured from *incident opened* (after the consecutive-cycle confirmation window) to *verified recovery*.

## Status and limitations

See [BUILD_STATUS.md](BUILD_STATUS.md) for the verified-vs-unverified ledger. In short: engine, policies, store, metrics,
app and the full detect->heal->verify loop are tested (including real end-to-end runs). The Docker, Kubernetes, Terraform,
Ansible, Prometheus and Grafana layers were written carefully but **not executed** in the build sandbox (no Docker daemon /
network); run the first-run checklist in [docs/DEMO.md](docs/DEMO.md) and fix anything environment-specific.
Chaos endpoints are for demos: set `ENABLE_CHAOS=false` anywhere real.
