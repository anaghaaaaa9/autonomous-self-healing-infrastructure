# BUILD STATUS

_Last updated: 2026-11-16. All five phases written. See "Verified vs not verified"._

## Phases
| Phase | Scope | State |
|---|---|---|
| 1 Core | app, detectors, SQLite incidents, remediation ladders, policies, verification, Dockerfile | COMPLETE, tested |
| 2 Monitoring | Prometheus configs, Grafana provisioning, generated 19-panel dashboard, orchestrator metrics | COMPLETE (config validated, not run) |
| 3 Infra automation | Kind config, manifests, RBAC, Terraform, 5 Ansible playbooks | COMPLETE (parsed, not run) |
| 4 Simulation + tests | `scripts/chaos/chaos.py`, 30 pytest tests incl. real end-to-end | COMPLETE, tested (local backend) |
| 5 Docs | README, docs/DEMO.md, HANDOFF, this file | COMPLETE |

## Verified vs not verified (honest ledger)
| Item | Evidence |
|---|---|
| Engine, policies (retry/backoff/circuit breaker/escalation/dedup), store, detectors, metrics format | 30 passing pytest tests |
| Real app + real engine, 4 scenarios (crash, health, memory, CPU): inject -> detect -> incident -> remediate -> verify | `tests/test_e2e_local.py` + manual run of `chaos.py` against `make run-local` (all resolved `remediated`, ~4 s recovery) |
| Dashboard only queries exported metrics | `tests/test_dashboard.py` |
| SIGTERM/Ctrl-C leaves no orphan app process (local backend) | manual check |
| Docker backend / Kubernetes backend command construction and output parsing | unit tests with a fake command runner only |
| docker-compose stack, Prometheus scraping, Grafana rendering | NOT RUN (no Docker daemon in build sandbox); YAML/JSON validated to parse |
| Kind cluster, manifests, RBAC, k8s scenarios | NOT RUN |
| Terraform (`tehcyx/kind ~> 0.5`), Ansible playbooks / `executor: ansible` | NOT RUN (terraform/ansible not installed) |

Bugs found and fixed during the build: attempt count not recorded on first-try success; app process orphaned on SIGTERM.

## How to continue
See `DEVELOPMENT_HANDOFF.md` ("NOT yet verified" list = next steps, in order).
