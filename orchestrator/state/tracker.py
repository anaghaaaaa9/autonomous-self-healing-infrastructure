"""Consecutive-cycle tracking, restart-count snapshots and scale-out bookkeeping."""
from __future__ import annotations

from typing import Dict, Optional, Set, Tuple

from ..models import ALL_TYPES, Observation


class Tracker:
    def __init__(self) -> None:
        self.counts: Dict[Tuple[str, str], int] = {}
        self.restarts: Dict[str, Dict[str, int]] = {}
        self.scaled_out_at: Dict[str, float] = {}

    def update(self, workload: str, present: Set[str], needed: Dict[str, int]) -> Set[str]:
        """Update consecutive counters; return the types confirmed this cycle."""
        confirmed = set()
        for t in ALL_TYPES:
            key = (workload, t)
            if t in present:
                self.counts[key] = self.counts.get(key, 0) + 1
                if self.counts[key] >= needed.get(t, 1):
                    confirmed.add(t)
            else:
                self.counts[key] = 0
        return confirmed

    def restarts_snapshot(self, workload: str) -> Optional[Dict[str, int]]:
        return self.restarts.get(workload)

    def save_restarts(self, workload: str, obs: Observation) -> None:
        self.restarts[workload] = {i.name: i.restarts for i in obs.instances}
