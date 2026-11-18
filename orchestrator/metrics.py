"""Tiny dependency-free Prometheus registry (counter, gauge, histogram)."""
from __future__ import annotations

import threading

BUCKETS = (1, 2, 5, 10, 20, 30, 60, 120, 300, float("inf"))


def _key(labels):
    return tuple(sorted((labels or {}).items()))


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _fmt(key, extra=()) -> str:
    items = list(key) + list(extra)
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in items) + "}" if items else ""


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._meta: dict = {}
        self._vals: dict = {}

    def _reg(self, name, typ, help_):
        self._meta.setdefault(name, (typ, help_))
        self._vals.setdefault(name, {})

    def inc(self, name, labels=None, value=1.0, help=""):
        with self._lock:
            self._reg(name, "counter", help)
            d = self._vals[name]
            k = _key(labels)
            d[k] = d.get(k, 0.0) + value

    def set(self, name, value, labels=None, help=""):
        with self._lock:
            self._reg(name, "gauge", help)
            self._vals[name][_key(labels)] = float(value)

    def replace(self, name, items, help=""):
        """Atomically replace every series of a gauge (drops stale label sets)."""
        with self._lock:
            self._reg(name, "gauge", help)
            self._vals[name] = {_key(lb): float(v) for lb, v in items}

    def observe(self, name, value, labels=None, help=""):
        with self._lock:
            self._reg(name, "histogram", help)
            d = self._vals[name]
            k = _key(labels)
            h = d.setdefault(k, {"b": [0] * len(BUCKETS), "sum": 0.0, "count": 0})
            for i, ub in enumerate(BUCKETS):
                if value <= ub:
                    h["b"][i] += 1
            h["sum"] += value
            h["count"] += 1

    def get(self, name, labels=None, default=0.0):
        with self._lock:
            return self._vals.get(name, {}).get(_key(labels), default)

    def render(self) -> str:
        with self._lock:
            lines = []
            for name in sorted(self._meta):
                typ, help_ = self._meta[name]
                lines.append(f"# HELP {name} {help_ or name}")
                lines.append(f"# TYPE {name} {typ}")
                for k, v in sorted(self._vals[name].items()):
                    if typ == "histogram":
                        for ub, c in zip(BUCKETS, v["b"]):
                            le = "+Inf" if ub == float("inf") else f"{ub:g}"
                            lines.append(f"{name}_bucket{_fmt(k, [('le', le)])} {c}")
                        lines.append(f"{name}_sum{_fmt(k)} {v['sum']}")
                        lines.append(f"{name}_count{_fmt(k)} {v['count']}")
                    else:
                        lines.append(f"{name}{_fmt(k)} {v}")
            return "\n".join(lines) + "\n"
