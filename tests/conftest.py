import copy
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from orchestrator.config import DEFAULTS  # noqa: E402
from orchestrator.incidents.store import IncidentStore  # noqa: E402
from orchestrator.metrics import Registry  # noqa: E402
from orchestrator.models import Instance, Observation  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeBackend:
    """Condition flags drive observations; `fixes` maps action -> flags it clears."""

    def __init__(self, kind="docker", desired=1, fixes=None):
        self.name = kind
        self.kind = kind
        self.desired = desired
        self.flags = {"down": False, "health_bad": False, "cpu": None, "mem": None,
                      "pod_bad": None, "restarts": 0}
        self.fixes = fixes if fixes is not None else {}
        self.actions = []

    def observe(self, w):
        f = self.flags
        if self.kind in ("docker", "local"):
            i = Instance("c1", running=not f["down"], ready=not f["down"], reason="exited" if f["down"] else "")
            if not f["down"]:
                i.healthy = not f["health_bad"]
                i.cpu, i.memory = f["cpu"], f["mem"]
            return Observation(w["name"], self.kind, [i], 1)
        insts = []
        for n in range(self.desired):
            bad = f["pod_bad"] and n == 0
            i = Instance(f"p{n}", running=not bad, ready=not bad and not f["health_bad"],
                         restarts=f["restarts"], reason=f["pod_bad"] or "")
            if not bad:
                i.healthy = not f["health_bad"]
                i.cpu, i.memory = f["cpu"], f["mem"]
            insts.append(i)
        return Observation(w["name"], self.kind, insts, self.desired)

    def execute(self, action, w, target=None):
        self.actions.append(action)
        for k in self.fixes.get(action, []):
            self.flags[k] = {"down": False, "health_bad": False, "cpu": None, "mem": None, "pod_bad": None}.get(k)
        return True, "ok"


@pytest.fixture
def cfg():
    c = copy.deepcopy(DEFAULTS)
    c["workloads"] = [{"name": "svc"}]
    c["policy"].update(verify_timeout_seconds=10, verify_interval_seconds=2)
    return c


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store():
    return IncidentStore(":memory:")


@pytest.fixture
def metrics():
    return Registry()
