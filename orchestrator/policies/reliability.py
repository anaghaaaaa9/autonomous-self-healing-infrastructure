"""Retry limits, exponential backoff and the per-workload circuit breaker."""
from __future__ import annotations


class ReliabilityPolicy:
    def __init__(self, p: dict) -> None:
        self.max_attempts = p["max_attempts"]
        self.base = p["base_backoff_seconds"]
        self.factor = p["backoff_factor"]
        self.max_backoff = p["max_backoff_seconds"]
        self.max_per_hour = p["max_remediations_per_hour"]

    def backoff(self, attempts_done: int) -> float:
        """Wait before attempt N+1 after N failed attempts: base * factor^(N-1), capped."""
        if attempts_done <= 0:
            return 0.0
        return min(self.base * (self.factor ** (attempts_done - 1)), self.max_backoff)

    def next_allowed(self, last_attempt_end: float, attempts_done: int) -> float:
        return last_attempt_end + self.backoff(attempts_done)

    def exhausted(self, attempts_done: int) -> bool:
        return attempts_done >= self.max_attempts

    def circuit_open(self, attempts_last_hour: int) -> bool:
        """Too many remediations on one workload within an hour => stop acting, escalate."""
        return attempts_last_hour >= self.max_per_hour
