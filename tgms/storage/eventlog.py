"""Append-only JSONL write-ahead provenance log (WP1.1).

Every write batch is appended *before* it is applied to the backend.
Record: {"batch_id", "tt", "ops": [...]}. First line is a header record.
Purposes: provenance, crash recovery (replay), backend migration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from tgms.core.errors import StateError
from tgms.core.model import canonical_json, sha256_hex

HEADER = {"format": "tgms-eventlog", "version": 1}


class EventLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(canonical_json(HEADER) + "\n")
        else:
            with open(self.path, "r", encoding="utf-8") as f:
                head = json.loads(f.readline())
            if head.get("format") != HEADER["format"]:
                raise StateError(f"not a tgms event log: {self.path}")

    def append(self, tt: int, ops: list[dict[str, Any]]) -> str:
        """Append one batch; fsync before returning (write-ahead guarantee)."""
        batch_id = sha256_hex(canonical_json({"tt": tt, "ops": ops}))[:16]
        record = canonical_json({"batch_id": batch_id, "tt": tt, "ops": ops})
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(record + "\n")
            f.flush()
            os.fsync(f.fileno())
        return batch_id

    def batches(self) -> Iterator[dict[str, Any]]:
        with open(self.path, "r", encoding="utf-8") as f:
            f.readline()  # header
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def last_tt(self) -> int:
        """Transaction time of the last batch (0 if empty).

        Linear scan; fine at research scale. TODO(phase3): tail-seek.
        """
        last = 0
        for batch in self.batches():
            last = batch["tt"]
        return last


def replay(eventlog_path: str | Path, adapter: Any) -> int:
    """Replay a log into a fresh adapter; returns number of batches applied.

    Applies each batch at its recorded tt, so the resulting store content
    (and store_digest) is identical to the original, on any backend.
    """
    from tgms.core.errors import TgmsError

    log = EventLog(eventlog_path)
    n = 0
    prev_tt = 0
    for batch in log.batches():
        tt = batch["tt"]
        if tt <= prev_tt:
            raise StateError(f"non-monotonic tt in event log: {tt} after {prev_tt}")
        adapter.begin()
        try:
            adapter.apply_ops(batch["ops"], tt)
        except TgmsError:
            # a batch that failed on the live path fails identically here
            # (apply is deterministic); skip it, exactly as the writer did
            adapter.rollback()
            prev_tt = tt
            continue
        adapter.commit()
        prev_tt = tt
        n += 1
    return n
