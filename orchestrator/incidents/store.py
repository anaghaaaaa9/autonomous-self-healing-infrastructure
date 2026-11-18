"""SQLite-backed incident + remediation-attempt store."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import List, Optional

ACTIVE = ("open", "remediating", "escalated")

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, workload TEXT NOT NULL, failure_type TEXT NOT NULL,
  status TEXT NOT NULL, detected_at REAL NOT NULL, resolved_at REAL,
  detail TEXT, action TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  recovery_status TEXT NOT NULL DEFAULT 'pending', resolution TEXT,
  recovery_seconds REAL
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  action TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, success INTEGER, message TEXT,
  FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE INDEX IF NOT EXISTS idx_inc_active ON incidents(workload, failure_type, status);
CREATE INDEX IF NOT EXISTS idx_att_incident ON attempts(incident_id);
"""


class IncidentStore:
    def __init__(self, path: str = "data/incidents.db") -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # -- helpers
    def _x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _q(self, sql, args=()) -> List[dict]:
        with self.lock:
            rows = [dict(r) for r in self.conn.execute(sql, args).fetchall()]
        for r in rows:
            if r.get("detail"):
                r["detail"] = json.loads(r["detail"])
        return rows

    # -- incidents
    def open_incident(self, workload: str, ftype: str, detail: dict, now: Optional[float] = None) -> dict:
        now = now if now is not None else time.time()
        iid = f"INC-{time.strftime('%Y%m%d', time.gmtime(now))}-{uuid.uuid4().hex[:6]}"
        self._x("INSERT INTO incidents(id,workload,failure_type,status,detected_at,detail) VALUES(?,?,?,?,?,?)",
                (iid, workload, ftype, "open", now, json.dumps(detail, default=str)))
        return self.get(iid)

    def get(self, iid: str) -> Optional[dict]:
        r = self._q("SELECT * FROM incidents WHERE id=?", (iid,))
        return r[0] if r else None

    def find_active(self, workload: str, ftype: str) -> Optional[dict]:
        r = self._q("SELECT * FROM incidents WHERE workload=? AND failure_type=? AND status IN (?,?,?)",
                    (workload, ftype) + ACTIVE)
        return r[0] if r else None

    def active(self, workload: Optional[str] = None) -> List[dict]:
        q = "SELECT * FROM incidents WHERE status IN (?,?,?)"
        args = list(ACTIVE)
        if workload:
            q += " AND workload=?"
            args.append(workload)
        return self._q(q + " ORDER BY detected_at", tuple(args))

    def update(self, iid: str, **fields) -> None:
        if "detail" in fields:
            fields["detail"] = json.dumps(fields["detail"], default=str)
        sets = ",".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE incidents SET {sets} WHERE id=?", tuple(fields.values()) + (iid,))

    def resolve(self, iid: str, now: float, resolution: str, action: Optional[str] = None) -> dict:
        inc = self.get(iid)
        fields = dict(status="resolved", resolved_at=now, resolution=resolution,
                      recovery_status="verified", recovery_seconds=max(0.0, now - inc["detected_at"]))
        if action:
            fields["action"] = action
        self.update(iid, **fields)
        return self.get(iid)

    def escalate(self, iid: str, now: float, reason: str) -> dict:
        self.update(iid, status="escalated", recovery_status="failed", resolution=f"escalated:{reason}")
        return self.get(iid)

    # -- attempts
    def record_attempt(self, iid: str, attempt: int, action: str, started: float, ended: float,
                       success: bool, message: str) -> None:
        self._x("INSERT INTO attempts(incident_id,attempt,action,started_at,ended_at,success,message) "
                "VALUES(?,?,?,?,?,?,?)", (iid, attempt, action, started, ended, int(success), message))

    def attempts_for(self, iid: str) -> List[dict]:
        return self._q("SELECT * FROM attempts WHERE incident_id=? ORDER BY attempt", (iid,))

    def last_attempt_end(self, iid: str) -> Optional[float]:
        r = self._q("SELECT MAX(ended_at) AS e FROM attempts WHERE incident_id=?", (iid,))
        return r[0]["e"] if r and r[0]["e"] is not None else None

    def count_attempts_since(self, workload: str, since: float) -> int:
        r = self._q("SELECT COUNT(*) AS n FROM attempts a JOIN incidents i ON i.id=a.incident_id "
                    "WHERE i.workload=? AND a.started_at>=?", (workload, since))
        return r[0]["n"]

    def history(self, limit: int = 50) -> List[dict]:
        return self._q("SELECT * FROM incidents ORDER BY detected_at DESC LIMIT ?", (limit,))
