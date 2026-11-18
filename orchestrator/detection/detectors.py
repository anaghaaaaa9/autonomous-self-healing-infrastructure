"""Pure detection logic: Observation -> {failure_type: detail}.

Consecutive-cycle filtering lives in state.tracker; this module only reports
what is wrong *right now*.
"""
from __future__ import annotations

from typing import Dict, Optional

from ..models import (CONTAINER_CRASH, HEALTH_CHECK_FAILURE, HIGH_CPU, HIGH_MEMORY,
                      POD_FAILURE, Observation)

IMAGE_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull",
                 "CreateContainerConfigError"}


def detect(obs: Observation, cfg: dict, prev_restarts: Optional[Dict[str, int]] = None) -> Dict[str, dict]:
    th = cfg["thresholds"]
    insts = obs.instances
    found: Dict[str, dict] = {}

    if obs.backend in ("docker", "local"):
        down = [i for i in insts if not i.running]
        if down or not insts:
            found[CONTAINER_CRASH] = {"instances": [i.name for i in down] or [obs.workload],
                                      "reasons": [i.reason for i in down]}
            return found
    else:  # kubernetes
        failing = [i for i in insts if not i.running]
        missing = max(0, obs.desired - len(insts))
        if failing or missing:
            found[POD_FAILURE] = {"instances": [i.name for i in failing],
                                  "reasons": sorted({i.reason for i in failing if i.reason}),
                                  "missing_pods": missing}
            return found
        if prev_restarts is not None:
            bumped = [i.name for i in insts if i.name in prev_restarts and i.restarts > prev_restarts[i.name]]
            if bumped:
                found[CONTAINER_CRASH] = {"instances": bumped, "reasons": ["container restarted"]}
                return found

    bad = [i.name for i in insts if i.running and (i.healthy is False or not i.ready)]
    if bad:
        found[HEALTH_CHECK_FAILURE] = {"instances": bad}
    hot = [i for i in insts if i.cpu is not None and i.cpu > th["cpu_ratio"]]
    if hot:
        hot.sort(key=lambda i: i.cpu, reverse=True)
        found[HIGH_CPU] = {"instances": [i.name for i in hot], "value": round(hot[0].cpu, 3),
                           "threshold": th["cpu_ratio"]}
    mem = [i for i in insts if i.memory is not None and i.memory > th["memory_ratio"]]
    if mem:
        mem.sort(key=lambda i: i.memory, reverse=True)
        found[HIGH_MEMORY] = {"instances": [i.name for i in mem], "value": round(mem[0].memory, 3),
                              "threshold": th["memory_ratio"]}
    return found


def is_recovered(obs: Observation, ftype: str, cfg: dict) -> bool:
    """True when the given failure type is no longer present in this observation."""
    if ftype in (CONTAINER_CRASH, POD_FAILURE):
        if not obs.instances or len(obs.instances) < obs.desired:
            return False
        if any((not i.running) or (not i.ready) for i in obs.instances):
            return False
    return ftype not in detect(obs, cfg, None)
