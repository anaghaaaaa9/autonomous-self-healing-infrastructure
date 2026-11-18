"""Shared data model: failure types and backend-neutral observations."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

CONTAINER_CRASH = "container_crash"
POD_FAILURE = "pod_failure"
HIGH_CPU = "high_cpu"
HIGH_MEMORY = "high_memory"
HEALTH_CHECK_FAILURE = "health_check_failure"
ALL_TYPES = (CONTAINER_CRASH, POD_FAILURE, HIGH_CPU, HIGH_MEMORY, HEALTH_CHECK_FAILURE)


@dataclass
class Instance:
    """One container (docker/local) or pod (kubernetes)."""
    name: str
    running: bool = True            # main process/container is running
    ready: bool = True              # container: == running; pod: Ready condition
    restarts: int = 0
    reason: str = ""                # e.g. exited, OOMKilled, CrashLoopBackOff, ImagePullBackOff
    healthy: Optional[bool] = None  # result of the /health probe (None = not probed)
    cpu: Optional[float] = None     # ratio of limit
    memory: Optional[float] = None  # ratio of limit


@dataclass
class Observation:
    workload: str
    backend: str
    instances: List[Instance] = field(default_factory=list)
    desired: int = 1
