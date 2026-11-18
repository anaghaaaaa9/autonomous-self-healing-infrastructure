"""Structured (JSON-lines) logging."""
from __future__ import annotations

import json
import logging
import sys
import time

_logger = logging.getLogger("shl")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {"ts": round(record.created, 3), "level": record.levelname.lower(),
               "event": record.getMessage()}
        doc.update(getattr(record, "fields", {}))
        return json.dumps(doc, default=str)


def setup(level: int = logging.INFO) -> None:
    if _logger.handlers:
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    _logger.addHandler(h)
    _logger.setLevel(level)
    _logger.propagate = False


def ev(level: str, event: str, **fields) -> None:
    """Emit one structured event, e.g. ev('info', 'incident_opened', id=...)."""
    getattr(_logger, level)(event, extra={"fields": fields})
