"""Structured JSON logging for tool-serving surfaces (Lane B5/F2).

Named `jsonlog` rather than `logging` so it never shadows the stdlib module
it is built on — every symbol here is a thin wrapper over `logging.Logger`
plus one `logging.Formatter` that renders a record as one JSON line.

**Off by default in library use, on in the server** (per the design brief).
Importing this module, or constructing a `CallLogger`, configures nothing
and emits nothing — `CallLogger(enabled=False)` (the default) is a no-op on
every call. `tgms serve` / `tgms webapp` call `configure_logging()` once at
startup and pass `CallLogger(enabled=True)` to `ToolRouter`; an in-process
caller that builds a bare `ToolRouter()` for an experiment gets exactly
today's silence.

Two lines per call, `event: "started"` and `event: "finished"`, each
carrying `request_id` — this is deliberate, not an accident of the shape:
`scripts/tgms_supervise.py` watches for a `"started"` line with no matching
`"finished"` line inside its wall-clock ceiling, and needs both events on
the same field to correlate them. A single "call completed" line would give
a supervisor nothing to watch *while* a call is still running.

Every line: `{ts, level, event, request_id, tool, store_identity,
generation, wall_ms, outcome, refusal_stage}` — fields that don't apply to
`"started"` (`wall_ms`, `outcome`, `refusal_stage`) are simply omitted
rather than emitted as null, so a line's own keys already say which event
it is without reading `event` first.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from typing import Any

#: The record attributes `JsonFormatter` looks for and renders when present.
#: Anything not set via `extra=` on a given call is simply absent from that
#: line, not emitted as `null` — see the module docstring.
_STRUCTURED_FIELDS = (
    "event", "request_id", "tool", "store_identity", "generation",
    "wall_ms", "outcome", "refusal_stage",
)

#: Marks a handler this module attached, so `configure_logging` can tell
#: "already wired up" from "some other handler is on this logger" and stay
#: idempotent either way.
_TGMS_HANDLER_ATTR = "_tgms_jsonlog_handler"

DEFAULT_LOGGER_NAME = "tgms.tools"


class JsonFormatter(logging.Formatter):
    """Renders one `logging.LogRecord` as one JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {"ts": record.created, "level": record.levelname}
        for key in _STRUCTURED_FIELDS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        message = record.getMessage()
        if message:
            payload["message"] = message
        return json.dumps(payload, default=str, sort_keys=True)


def new_request_id() -> str:
    """A fresh per-call id (uuid4, hex — no dashes, so it is a safe bare
    token in the envelope's annotations channel and in a log line alike)."""
    return uuid.uuid4().hex


def configure_logging(path: str | os.PathLike[str] | None = None,
                      logger_name: str = DEFAULT_LOGGER_NAME,
                      level: int = logging.INFO) -> logging.Logger:
    """Attach a JSON-formatting handler to `logger_name`.

    `path` wins if given; otherwise `TGMS_LOG_PATH`; otherwise stderr — the
    same "explicit > env > default" order `tgms.telemetry.metrics.Metrics`
    uses. Idempotent: calling this twice (e.g. once from `tgms serve` and
    once from a test fixture) does not double-attach a handler this module
    already installed on the same logger.
    """
    logger = logging.getLogger(logger_name)
    if any(getattr(h, _TGMS_HANDLER_ATTR, False) for h in logger.handlers):
        return logger
    resolved = str(path) if path is not None else os.environ.get("TGMS_LOG_PATH")
    handler: logging.Handler
    if resolved:
        from pathlib import Path
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(resolved)
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    setattr(handler, _TGMS_HANDLER_ATTR, True)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


class CallLogger:
    """Logs the `"started"`/`"finished"` pair for one tool call.

    `enabled=False` (the default) makes every method a no-op — a caller can
    hold one unconditionally, exactly like `tgms.telemetry.metrics.Metrics`
    with no path configured.
    """

    def __init__(self, logger: logging.Logger | None = None,
                enabled: bool = False) -> None:
        self.logger = logger if logger is not None else logging.getLogger(
            DEFAULT_LOGGER_NAME)
        self.enabled = enabled

    def log_start(self, *, request_id: str, tool: str) -> None:
        if not self.enabled:
            return
        self.logger.info("", extra={"event": "started", "request_id": request_id,
                                    "tool": tool})

    def log_finish(self, *, request_id: str, tool: str,
                   store_identity: str | None, generation: int | None,
                   wall_ms: float, outcome: str,
                   refusal_stage: str | None = None) -> None:
        if not self.enabled:
            return
        level = logging.INFO if outcome == "ok" else logging.WARNING
        self.logger.log(level, "", extra={
            "event": "finished", "request_id": request_id, "tool": tool,
            "store_identity": store_identity, "generation": generation,
            "wall_ms": round(wall_ms, 3), "outcome": outcome,
            "refusal_stage": refusal_stage,
        })


__all__ = ["CallLogger", "JsonFormatter", "configure_logging", "new_request_id"]
