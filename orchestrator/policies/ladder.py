"""Escalating remediation ladders: attempt N uses step N (last step repeats)."""
from __future__ import annotations

from typing import List

from ..models import (CONTAINER_CRASH, HEALTH_CHECK_FAILURE, HIGH_CPU, HIGH_MEMORY, POD_FAILURE)
from ..detection.detectors import IMAGE_REASONS

ALL_ACTIONS = ("start_instance", "restart_instance", "delete_pod", "restart_deployment",
               "scale_out", "scale_in", "rollout_undo")


def plan(backend: str, ftype: str, detail: dict) -> List[str]:
    if backend in ("docker", "local"):
        if ftype == CONTAINER_CRASH:
            return ["start_instance", "restart_instance"]
        return ["restart_instance"]          # health / cpu / memory: controlled restart
    # kubernetes
    if ftype == POD_FAILURE and IMAGE_REASONS & set(detail.get("reasons", [])):
        return ["rollout_undo", "restart_deployment"]
    if ftype == HIGH_CPU:
        return ["scale_out", "delete_pod", "restart_deployment"]
    return ["delete_pod", "restart_deployment"]
