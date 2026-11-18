"""Configuration loading: YAML file deep-merged over safe defaults."""
from __future__ import annotations

import copy
import os

import yaml

DEFAULTS = {
    "backend": "docker",                    # docker | kubernetes | local
    "executor": "native",                   # native (docker/kubectl CLI) | ansible (playbooks)
    "interval_seconds": 5,
    "db_path": "data/incidents.db",
    "metrics_port": 9100,
    "thresholds": {"cpu_ratio": 0.80, "memory_ratio": 0.85},
    # consecutive detection cycles required before an incident is opened
    "consecutive_cycles": {
        "container_crash": 1,
        "pod_failure": 2,
        "high_cpu": 3,
        "high_memory": 3,
        "health_check_failure": 3,
    },
    # cycles to wait for the platform (kubelet/ReplicaSet) to self-heal before acting
    "grace_cycles": {},
    "policy": {
        "max_attempts": 3,
        "base_backoff_seconds": 10,
        "backoff_factor": 2,
        "max_backoff_seconds": 120,
        "max_remediations_per_hour": 6,     # circuit breaker per workload
        "verify_timeout_seconds": 45,
        "verify_interval_seconds": 2,
        "verify_consecutive": 2,
    },
    "scale": {"max_replicas": 3, "scale_in_after_seconds": 120},
    "workloads": [],
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: str | None = None) -> dict:
    path = path or os.getenv("SHL_CONFIG", "config/docker.yaml")
    cfg = copy.deepcopy(DEFAULTS)
    with open(path) as f:
        _merge(cfg, yaml.safe_load(f) or {})
    if not cfg["workloads"]:
        raise ValueError(f"{path}: no workloads configured")
    return cfg
