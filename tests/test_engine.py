from conftest import FakeBackend

from orchestrator.engine import Engine


def make(cfg, backend, store, metrics, clock):
    return Engine(cfg, backend, store, metrics, clock=clock, sleep=clock.sleep)


def run(engine, clock, cycles, step=5):
    for _ in range(cycles):
        engine.run_cycle()
        clock.t += step


def test_crash_detected_remediated_and_verified(cfg, store, metrics, clock):
    be = FakeBackend("docker", fixes={"start_instance": ["down"]})
    be.flags["down"] = True
    e = make(cfg, be, store, metrics, clock)
    run(e, clock, 3)
    inc = store.history()[0]
    assert inc["failure_type"] == "container_crash"
    assert inc["status"] == "resolved" and inc["resolution"] == "remediated"
    assert inc["action"] == "start_instance" and inc["attempts"] == 1
    assert inc["recovery_status"] == "verified" and inc["recovery_seconds"] is not None
    assert store.attempts_for(inc["id"])[0]["success"] == 1
    assert metrics.get("shl_remediation_attempts_total", {"action": "start_instance", "result": "success"}) == 1


def test_transient_cpu_spike_is_ignored(cfg, store, metrics, clock):
    be = FakeBackend("docker")
    e = make(cfg, be, store, metrics, clock)
    be.flags["cpu"] = 0.95
    run(e, clock, 2)                       # threshold is 3 consecutive cycles
    be.flags["cpu"] = 0.1
    run(e, clock, 3)
    assert store.history() == [] and be.actions == []


def test_sustained_cpu_triggers_incident_and_recovery(cfg, store, metrics, clock):
    be = FakeBackend("docker", fixes={"restart_instance": ["cpu"]})
    be.flags["cpu"] = 0.95
    e = make(cfg, be, store, metrics, clock)
    run(e, clock, 5)
    inc = store.history()[0]
    assert inc["failure_type"] == "high_cpu" and inc["status"] == "resolved"
    assert be.actions == ["restart_instance"]


def test_memory_and_health_failures(cfg, store, metrics, clock):
    for flag, val, ftype in (("mem", 0.95, "high_memory"), ("health_bad", True, "health_check_failure")):
        store2 = type(store)(":memory:")
        be = FakeBackend("docker", fixes={"restart_instance": ["mem", "health_bad"]})
        be.flags[flag] = val
        run(make(cfg, be, store2, type(metrics)(), clock), clock, 5)
        inc = store2.history()[0]
        assert inc["failure_type"] == ftype and inc["status"] == "resolved"


def test_duplicate_incidents_suppressed_and_loop_bounded(cfg, store, metrics, clock):
    be = FakeBackend("docker", fixes={})          # remediation never works
    be.flags["down"] = True
    e = make(cfg, be, store, metrics, clock)
    run(e, clock, 200)                            # ~1000 simulated seconds
    hist = store.history()
    assert len(hist) == 1, "same failure must not open duplicate incidents"
    assert hist[0]["status"] == "escalated"
    assert hist[0]["resolution"] == "escalated:max_attempts_exhausted"
    assert len(be.actions) == cfg["policy"]["max_attempts"], "no infinite remediation loop"
    assert be.actions == ["start_instance", "restart_instance", "restart_instance"]  # ladder


def test_exponential_backoff_between_attempts(cfg, store, metrics, clock):
    be = FakeBackend("docker", fixes={})
    be.flags["down"] = True
    e = make(cfg, be, store, metrics, clock)
    run(e, clock, 100)
    att = store.attempts_for(store.history()[0]["id"])
    gaps = [att[i + 1]["started_at"] - att[i]["ended_at"] for i in range(len(att) - 1)]
    assert gaps[0] >= 10 and gaps[1] >= 20       # base 10s, factor 2


def test_escalated_incident_resolves_when_fixed_manually(cfg, store, metrics, clock):
    be = FakeBackend("docker", fixes={})
    be.flags["down"] = True
    e = make(cfg, be, store, metrics, clock)
    run(e, clock, 100)
    assert store.history()[0]["status"] == "escalated"
    be.flags["down"] = False
    run(e, clock, 2)
    assert store.history()[0]["resolution"] == "recovered_after_escalation"


def test_circuit_breaker_stops_flapping(cfg, store, metrics, clock):
    cfg["policy"]["max_remediations_per_hour"] = 2
    be = FakeBackend("docker", fixes={"start_instance": ["down"]})
    e = make(cfg, be, store, metrics, clock)
    for _ in range(4):                             # crash repeatedly
        be.flags["down"] = True
        run(e, clock, 3)
    assert len(be.actions) == 2
    assert any(i["resolution"] == "escalated:circuit_breaker" for i in store.history())


def test_kubernetes_self_recovery_is_observed_not_claimed(cfg, store, metrics, clock):
    cfg["backend"] = "kubernetes"
    cfg["grace_cycles"] = {"pod_failure": 2}
    be = FakeBackend("kubernetes", desired=2)
    be.flags["pod_bad"] = "ContainerCreating"
    e = make(cfg, be, store, metrics, clock)
    run(e, clock, 2)                               # confirmed after 2 cycles
    assert store.history()[0]["status"] == "open"
    be.flags["pod_bad"] = None                     # kubernetes healed it by itself
    run(e, clock, 2)
    inc = store.history()[0]
    assert inc["resolution"] == "self_recovered" and inc["attempts"] == 0
    assert be.actions == []                        # orchestrator took no action and claims none


def test_kubernetes_bad_image_uses_rollout_undo(cfg, store, metrics, clock):
    cfg["backend"] = "kubernetes"
    be = FakeBackend("kubernetes", desired=2, fixes={"rollout_undo": ["pod_bad"]})
    be.flags["pod_bad"] = "ImagePullBackOff"
    run(make(cfg, be, store, metrics, clock), clock, 5)
    inc = store.history()[0]
    assert inc["failure_type"] == "pod_failure" and inc["action"] == "rollout_undo"
    assert inc["status"] == "resolved"


def test_kubernetes_cpu_ladder_scale_then_replace(cfg, store, metrics, clock):
    cfg["backend"] = "kubernetes"
    be = FakeBackend("kubernetes", desired=2, fixes={"delete_pod": ["cpu"]})
    be.flags["cpu"] = 0.95
    run(make(cfg, be, store, metrics, clock), clock, 40)
    assert be.actions[:2] == ["scale_out", "delete_pod"]
    assert store.history()[0]["status"] == "resolved"


def test_metrics_exposed(cfg, store, metrics, clock):
    be = FakeBackend("docker", fixes={"start_instance": ["down"]})
    be.flags["down"] = True
    run(make(cfg, be, store, metrics, clock), clock, 3)
    text = metrics.render()
    for needle in ("shl_incidents_detected_total", "shl_remediation_attempts_total",
                   "shl_recovery_seconds_bucket", "shl_instance_up", "shl_workload_healthy",
                   "shl_incident_info", "shl_cycles_total"):
        assert needle in text
