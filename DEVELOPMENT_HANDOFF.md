# DEVELOPMENT HANDOFF (for another Claude session)

Read `BUILD_STATUS.md` first, then this file. Everything needed is in the repo; no chat context required.

## What this is
Python control loop (`orchestrator/`) that observes a workload through a **backend** (`docker` | `kubernetes` | `local`),
runs pure **detectors**, opens **incidents** in SQLite, picks an action from a **ladder** (`policies/ladder.py`), executes it
(CLI or Ansible), **verifies** recovery and exports Prometheus metrics. Entry point: `python -m orchestrator run --config config/<x>.yaml`.

## Key invariants (do not break)
- `detect()` and `is_recovered()` in `detection/detectors.py` are pure; consecutive-cycle logic lives only in `state/tracker.py`.
- `Engine._handle` order: recovered? -> escalated(skip) -> grace -> circuit breaker -> exhausted -> backoff -> remediate.
- `escalated` incidents are active (suppress duplicates) but are never remediated; they auto-resolve if the condition clears.
- Actions are named strings: `start_instance restart_instance delete_pod restart_deployment scale_out scale_in rollout_undo`;
  add a new one in `policies/ladder.py` (`ALL_ACTIONS`, `plan`), the backend `execute`, and `ansible_exec.PLAYBOOKS`.
- Dashboard JSON is generated: edit `monitoring/grafana/generate_dashboard.py`, run `make dashboard`;
  `tests/test_dashboard.py` enforces that queried metrics exist.
- Engine takes injectable `clock`/`sleep`; use `tests/conftest.py` `FakeBackend`/`FakeClock` for deterministic tests.

## Tests
`python3 -m pytest tests -q` -> 30 passed (~20 s). `tests/test_e2e_local.py` starts a real app on :18081 and runs the real
engine against real chaos; `tests/test_components.py` starts the app on :18099.

## NOT yet verified (first thing to do when an environment with Docker/Kind is available)
1. `make up` / `make run` / `make demo-*` against real Docker (`DockerBackend` is only unit-tested with a fake runner).
2. `make kind-up mon-k8s run-k8s` + `demo-bad-image`, `demo-pod-kill`, `demo-k8s-cpu` (`KubeBackend` only unit-tested).
3. Grafana dashboard rendering (table transformation `organize`, `dateTimeAsLocal` unit on the history table).
4. `terraform init/apply` (tehcyx/kind provider version `~> 0.5` unverified) and `ansible-playbook` runs / `executor: ansible`.
5. Memory chaos headroom: 150 MB chaos vs 192 MB limit (baseline RSS ~25-30 MB in `python:3.12-slim`). OOMKill is also handled.

## Known limitations / ideas (priority order)
1. Verification blocks the loop per workload (synchronous) - make async if multiple workloads are monitored.
2. Orchestrator is not containerised/in-cluster; `kubernetes/rbac` is ready for it (needs a Dockerfile with kubectl).
3. Compose mode CPU remediation is a restart (no replicas because of fixed port mapping); Kubernetes mode scales out.
4. `shl_incident_info` is capped at the 15 most recent incidents.
5. Prometheus alert rules and Alertmanager not included.
