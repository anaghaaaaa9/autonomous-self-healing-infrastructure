# Demo walkthrough and first-run checklist

## First-run checklist (do this once on your Mac, ~10 min)
Everything below the "verified" line in BUILD_STATUS.md has never run against a real Docker daemon or cluster.
1. `make test` -> expect `30 passed`.
2. `make run-local` then `python3 scripts/chaos/chaos.py cpu --backend local` -> proves the engine on your machine.
3. `make up` -> `docker ps` shows `shl-app`, `shl-prometheus`, `shl-grafana`. Prometheus http://localhost:9090/targets: `app` and
   `orchestrator` UP (orchestrator is UP only while `make run` is running; `host.docker.internal` works on Docker Desktop).
4. `make run`, then `make demo-crash`. Open Grafana http://localhost:3000 (admin/admin) -> Self-Healing dashboard.
5. Kind (optional): `make kind-up mon-k8s run-k8s`, then `make demo-bad-image`.

Likely first-run snags: port 8081/9090/3000/9100 already in use; Docker Desktop not running (orchestrator logs
`cycle_error ... docker unavailable` and keeps retrying, it does not crash); `kind`/`kubectl` not installed.
If `memory` chaos shows the container `OOMKilled` instead of `high_memory`, lower `--mb` (default 150 of a 192 MB limit);
the orchestrator will still heal it, as a `container_crash`.

## Demo script (5 minutes, Docker mode)
Terminals: **T1** `make run` (JSON logs) - **T2** commands below - **browser** Grafana dashboard.

| # | Command (T2) | Injection | What you should see (T2 timeline) |
|---|---|---|---|
| 1 | `make demo-crash` | `docker kill shl-app` | DETECTED container_crash (~3 s) -> REMEDIATE `start_instance` verified -> RECOVERED (~4 s) |
| 2 | `make demo-health` | `/health` returns 503 | DETECTED health_check_failure (~3 cycles = 10-15 s) -> `restart_instance` -> RECOVERED |
| 3 | `make demo-memory` | allocates 150 MB (limit 192 MB) | DETECTED high_memory -> `restart_instance` -> RECOVERED |
| 4 | `make demo-cpu` | 1 busy thread, 90 s, capped at 0.5 CPU | DETECTED high_cpu -> `restart_instance` -> RECOVERED |
| 5 | `make incidents` | - | table of all four incidents: status `resolved`, resolution `remediated`, 1 attempt |

Reference output from a **real run of the local backend** (same engine, same chaos hooks):
```
[t+0s] failure injected (cpu); waiting for the orchestrator on :9100 ...
[t+9s] DETECTED  incident INC-20261116-f35a11 type=high_cpu workload=shl-app
[t+13s] REMEDIATE attempt 1 action=restart_instance verified=yes
[t+13s] RECOVERED incident INC-20261116-f35a11 resolution=remediated recovery_time=4.0s attempts=1
```
```
INCIDENT             WORKLOAD     TYPE                   STATUS     ACTION             TRY DETECTED  RECOVERY  RESOLUTION
INC-20261116-f35a11  shl-app      high_cpu               resolved   restart_instance   1   09:29:29  4.0s      remediated
INC-20261116-e8d939  shl-app      high_memory            resolved   restart_instance   1   09:29:16  4.0s      remediated
INC-20261116-88f827  shl-app      health_check_failure   resolved   restart_instance   1   09:29:00  4.0s      remediated
INC-20261116-9ebd1e  shl-app      container_crash        resolved   start_instance     1   09:28:47  4.0s      remediated
```
Docker/Kubernetes timings will differ slightly (container restart takes a few seconds longer).

## Kubernetes scenarios (what is *expected*, not yet run)
| Command | Expected |
|---|---|
| `make demo-pod-kill` | Kubernetes recreates the pod itself. Orchestrator may open `pod_failure`, waits, then resolves `self_recovered`, `attempts=0` (no credit claimed). |
| `make demo-bad-image` | New pod `ImagePullBackOff` (old pods keep serving) -> `pod_failure` -> action `rollout_undo` -> verified -> `remediated`. |
| `make demo-k8s-cpu` | `high_cpu` -> `scale_out` (2 -> 3 replicas). If the hot pod is still hot after verification fails, attempt 2 `delete_pod` (after 10 s backoff). After 120 s calm: `scale_in`. |
| `make demo-k8s-memory` / `demo-k8s-health` | `delete_pod` of the affected pod -> new pod Ready -> verified. |

## Showing the reliability policies (T2)
Kill the remediation path so it cannot succeed, e.g. `docker rm -f shl-app` (container missing, `start` cannot work):
attempts are spaced ~10 s then ~20 s apart (exponential backoff), then the incident becomes `escalated`
(`resolution=escalated:max_attempts_exhausted`, CRITICAL log, `shl_incidents_escalated_total` increments) and the orchestrator
stops acting. Recreate it with `make up` and the incident auto-closes as `recovered_after_escalation`.
(This exact behaviour is covered deterministically in `tests/test_engine.py`.)

## Likely interview questions
- *Why consecutive cycles?* Avoid reacting to spikes; thresholds are per failure type (crash 1, CPU/memory/health 3).
- *How do you avoid remediation loops?* Retry cap, exponential backoff, per-hour circuit breaker, duplicate suppression, escalation
  that hands the incident to humans permanently.
- *How do you know recovery worked?* Condition must be absent for 2 consecutive fresh observations within a timeout; otherwise the
  attempt is recorded as failed and the next ladder step runs.
- *What if Kubernetes already fixed it?* Grace period, then observe -> `self_recovered`; never claims credit.
- *What would you change for production?* Async verification, in-cluster deployment using the provided RBAC, leader election for
  multiple orchestrators, alert routing (PagerDuty/Slack) on escalation, Prometheus-based rather than probe-based detection.
