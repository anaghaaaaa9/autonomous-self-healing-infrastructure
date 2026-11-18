import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from orchestrator.detection.detectors import detect, is_recovered
from orchestrator.metrics import Registry
from orchestrator.models import Instance, Observation
from orchestrator.policies.ladder import plan
from orchestrator.policies.reliability import ReliabilityPolicy
from orchestrator.remediation.backends import DockerBackend, KubeBackend
from orchestrator.remediation.common import parse_prom
from orchestrator.state.tracker import Tracker


def test_store_lifecycle_and_unique_ids(store):
    a = store.open_incident("w", "high_cpu", {"x": 1}, 100.0)
    b = store.open_incident("w", "high_memory", {}, 101.0)
    assert a["id"] != b["id"] and a["id"].startswith("INC-")
    assert store.find_active("w", "high_cpu")["id"] == a["id"]
    store.record_attempt(a["id"], 1, "restart_instance", 100, 105, True, "ok")
    done = store.resolve(a["id"], 110.0, "remediated", "restart_instance")
    assert done["recovery_seconds"] == 10.0 and done["status"] == "resolved"
    assert store.find_active("w", "high_cpu") is None
    assert len(store.history()) == 2


def test_store_persists_to_disk(tmp_path):
    from orchestrator.incidents.store import IncidentStore
    p = str(tmp_path / "i.db")
    IncidentStore(p).open_incident("w", "pod_failure", {}, 1.0)
    assert IncidentStore(p).history()[0]["failure_type"] == "pod_failure"


def test_detectors_docker(cfg):
    ok = Observation("w", "docker", [Instance("c", healthy=True, cpu=0.1, memory=0.1)])
    assert detect(ok, cfg) == {}
    down = Observation("w", "docker", [Instance("c", running=False, ready=False, reason="exited")])
    assert list(detect(down, cfg)) == ["container_crash"]
    mixed = Observation("w", "docker", [Instance("c", healthy=False, cpu=0.99, memory=0.99)])
    assert set(detect(mixed, cfg)) == {"health_check_failure", "high_cpu", "high_memory"}


def test_detectors_kubernetes(cfg):
    pods = [Instance("a", running=False, ready=False, reason="CrashLoopBackOff"), Instance("b", healthy=True)]
    d = detect(Observation("w", "kubernetes", pods, 2), cfg)
    assert list(d) == ["pod_failure"] and d["pod_failure"]["reasons"] == ["CrashLoopBackOff"]
    missing = detect(Observation("w", "kubernetes", [Instance("b", healthy=True)], 2), cfg)
    assert missing["pod_failure"]["missing_pods"] == 1
    bumped = detect(Observation("w", "kubernetes", [Instance("a", restarts=3, healthy=True)], 1), cfg, {"a": 2})
    assert list(bumped) == ["container_crash"]
    assert not is_recovered(Observation("w", "kubernetes", [Instance("b")], 2), "pod_failure", cfg)


def test_tracker_requires_consecutive_cycles():
    t = Tracker()
    need = {"high_cpu": 3}
    assert t.update("w", {"high_cpu"}, need) == set()
    assert t.update("w", {"high_cpu"}, need) == set()
    assert t.update("w", set(), need) == set()          # spike ended -> counter reset
    assert t.update("w", {"high_cpu"}, need) == set()
    t.update("w", {"high_cpu"}, need)
    assert t.update("w", {"high_cpu"}, need) == {"high_cpu"}


def test_policy_backoff_and_limits(cfg):
    p = ReliabilityPolicy(cfg["policy"])
    assert [p.backoff(n) for n in (0, 1, 2, 3, 10)] == [0, 10, 20, 40, 120]
    assert p.exhausted(3) and not p.exhausted(2)
    assert p.circuit_open(6) and not p.circuit_open(5)


def test_ladders():
    assert plan("docker", "container_crash", {}) == ["start_instance", "restart_instance"]
    assert plan("kubernetes", "pod_failure", {"reasons": ["ImagePullBackOff"]})[0] == "rollout_undo"
    assert plan("kubernetes", "high_cpu", {})[0] == "scale_out"


def test_registry_render_histogram_and_replace():
    r = Registry()
    r.inc("c_total", {"a": "x"}, 2)
    r.observe("h", 3, {"t": "q"})
    r.replace("g", [({"i": "1"}, 1), ({"i": "2"}, 0)])
    r.replace("g", [({"i": "2"}, 1)])
    out = r.render()
    assert 'c_total{a="x"} 2.0' in out and 'h_bucket{t="q",le="5"} 1' in out and 'h_bucket{t="q",le="2"} 0' in out
    assert 'h_count{t="q"} 1' in out and 'g{i="1"}' not in out


def test_parse_prom():
    assert parse_prom("# HELP x\nx 1.5\ny{a=\"b\"} 2\nz 3\n") == {"x": 1.5, "z": 3.0}


def test_docker_backend_commands_and_observation():
    calls = []
    inspect = json.dumps([{"State": {"Running": False, "Status": "exited", "OOMKilled": True}, "RestartCount": 2}])

    def runner(cmd, timeout=30):
        calls.append(cmd)
        return (0, inspect) if cmd[1] == "inspect" else (0, "ok")

    be = DockerBackend({"executor": "native"}, runner)
    obs = be.observe({"name": "w", "container": "c", "url": "http://x"})
    assert obs.instances[0].running is False and obs.instances[0].reason == "OOMKilled"
    assert be.execute("restart_instance", {"container": "c"})[0]
    assert calls[-1] == ["docker", "restart", "-t", "10", "c"]
    missing = DockerBackend({}, lambda c, t=30: (1, "Error: No such object: c"))
    assert missing.observe({"name": "w", "container": "c", "url": "u"}).instances[0].reason == "NotFound"
    with pytest.raises(RuntimeError):
        DockerBackend({}, lambda c, t=30: (1, "Cannot connect to the Docker daemon")).observe(
            {"name": "w", "container": "c", "url": "u"})


def test_kube_backend_parses_pods_and_scales():
    pods = {"items": [
        {"metadata": {"name": "good"}, "status": {"phase": "Running",
         "containerStatuses": [{"restartCount": 1, "state": {"running": {}}}],
         "conditions": [{"type": "Ready", "status": "True"}]}},
        {"metadata": {"name": "bad"}, "status": {"phase": "Pending",
         "containerStatuses": [{"restartCount": 0, "state": {"waiting": {"reason": "ImagePullBackOff"}}}],
         "conditions": []}},
        {"metadata": {"name": "gone", "deletionTimestamp": "x"}, "status": {}},
    ]}
    calls = []

    def runner(cmd, timeout=30):
        calls.append(cmd)
        if "deployment" in cmd and "get" in cmd:
            return 0, json.dumps({"spec": {"replicas": 2}})
        if "pods" in cmd and "-l" in cmd:
            return 0, json.dumps(pods)
        if "--raw" in cmd:
            return 0, "app_cpu_utilization_ratio 0.5\napp_memory_utilization_ratio 0.4\n"
        return 0, "done"

    be = KubeBackend({"scale": {"max_replicas": 3}}, runner)
    w = {"name": "w", "namespace": "shl", "deployment": "d"}
    obs = be.observe(w)
    assert obs.desired == 2 and [i.name for i in obs.instances] == ["good", "bad"]
    assert obs.instances[0].ready and obs.instances[0].cpu == 0.5
    assert obs.instances[1].reason == "ImagePullBackOff" and not obs.instances[1].running
    assert be.execute("scale_out", w)[0]
    assert any("--replicas=3" in c for c in calls[-1])
    assert be.execute("rollout_undo", w)[0] and calls[-1][-3:] == ["rollout", "undo", "deployment/d"]


def test_ansible_executor_builds_command():
    seen = []
    be = DockerBackend({"executor": "ansible"}, lambda c, t=30: (seen.append(c), (0, "ok"))[1])
    ok, msg = be.execute("restart_instance", {"container": "c"})
    assert ok and seen[0][0] == "ansible-playbook" and seen[0][5].endswith("container_recover.yml")
    assert json.loads(seen[0][-1]) == {"container_action": "restart", "container": "c"}


# --------------------------------------------------------------- the real app
@pytest.fixture
def app_url():
    port = 18099
    env = dict(os.environ, PORT=str(port), APP_MEMORY_LIMIT_MB="128", MAX_CHAOS_MB="60")
    root = os.path.join(os.path.dirname(__file__), "..")
    p = subprocess.Popen([sys.executable, "application/app.py"], cwd=root, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(url + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield url
    p.terminate()
    p.wait(5)


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_app_health_metrics_and_chaos(app_url):
    assert _get(app_url + "/health")[0] == 200
    m = parse_prom(_get(app_url + "/metrics")[1])
    assert m["app_up"] == 1 and m["app_health_status"] == 1 and m["process_resident_memory_bytes"] > 0
    assert _get(app_url + "/chaos/health-fail")[0] == 200
    assert _get(app_url + "/health")[0] == 503
    assert parse_prom(_get(app_url + "/metrics")[1])["app_health_status"] == 0
    _get(app_url + "/chaos/reset")
    assert _get(app_url + "/health")[0] == 200
    before = parse_prom(_get(app_url + "/metrics")[1])["process_resident_memory_bytes"]
    _get(app_url + "/chaos/memory?mb=50&seconds=30")
    after = parse_prom(_get(app_url + "/metrics")[1])
    assert after["process_resident_memory_bytes"] - before > 40 * 1024 * 1024
    assert after["app_memory_utilization_ratio"] > before / (128 * 1024 * 1024)
    _get(app_url + "/chaos/reset")
    _get(app_url + "/chaos/cpu?seconds=6")
    time.sleep(5)
    assert parse_prom(_get(app_url + "/metrics")[1])["app_cpu_utilization_ratio"] > 0.5
    _get(app_url + "/chaos/reset")
    _get(app_url + "/chaos/crash")
    time.sleep(1)
    assert _get.__call__ and pytest.raises(Exception, urllib.request.urlopen, app_url + "/health", timeout=1)
