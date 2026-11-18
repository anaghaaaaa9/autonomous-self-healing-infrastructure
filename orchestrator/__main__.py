"""CLI: python -m orchestrator run|incidents [--config FILE]"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from . import logging_setup
from .config import load_config
from .engine import Engine
from .incidents.store import IncidentStore
from .metrics import Registry
from .remediation import make_backend
from .server import start_server


def _fmt_ts(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "-"


def _on_sigterm(*_):
    raise KeyboardInterrupt  # route SIGTERM (docker stop, pkill) through the same clean-shutdown path


def cmd_run(args) -> int:
    signal.signal(signal.SIGTERM, _on_sigterm)
    logging_setup.setup()
    cfg = load_config(args.config)
    store = IncidentStore(cfg["db_path"])
    reg = Registry()
    backend = make_backend(cfg)
    if hasattr(backend, "bootstrap"):
        backend.bootstrap(cfg)
    start_server(cfg["metrics_port"], reg, store)
    engine = Engine(cfg, backend, store, reg)
    try:
        if args.once:
            engine.run_cycle()
        else:
            engine.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if hasattr(backend, "cleanup"):
            backend.cleanup()
    return 0


def cmd_incidents(args) -> int:
    cfg = load_config(args.config)
    store = IncidentStore(cfg["db_path"])
    rows = store.history(args.limit)
    if args.json:
        for r in rows:
            r["attempt_log"] = store.attempts_for(r["id"])
        print(json.dumps(rows, indent=2, default=str))
        return 0
    print(f"{'INCIDENT':<20} {'WORKLOAD':<12} {'TYPE':<22} {'STATUS':<10} {'ACTION':<18} {'TRY':<3} {'DETECTED':<9} {'RECOVERY':<9} RESOLUTION")
    for r in rows:
        rec = f"{r['recovery_seconds']:.1f}s" if r["recovery_seconds"] is not None else "-"
        print(f"{r['id']:<20} {r['workload']:<12} {r['failure_type']:<22} {r['status']:<10} "
              f"{(r['action'] or '-'):<18} {r['attempts']:<3} {_fmt_ts(r['detected_at']):<9} {rec:<9} {r['resolution'] or '-'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start the self-healing control loop")
    r.add_argument("--config")
    r.add_argument("--once", action="store_true", help="run a single cycle and exit")
    i = sub.add_parser("incidents", help="show incident history")
    i.add_argument("--config")
    i.add_argument("--limit", type=int, default=20)
    i.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    return cmd_run(args) if args.cmd == "run" else cmd_incidents(args)


if __name__ == "__main__":
    sys.exit(main())
