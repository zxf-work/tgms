"""Service-surface limits (Lane B5/F2): result size and call concurrency.

This is the *outer* backpressure surface, deliberately independent of TGIR's
plan-cost admission (`tgms.tgir.admission`, frozen — this module never
imports it and never changes its policy). Admission prices a plan **before**
it runs, against static or per-node estimates; the caps here act on a call
**after** it has already run (rows/bytes actually produced) and **before**
it runs (a concurrency slot), for the two things admission cannot see at
all: how many callers are in flight right now, and — for a leaf whose
estimate under-priced it, or for an admission-bypass path — how big the
realized result actually turned out to be.

**The D-155 rule applies here too: refuse, never truncate.** A result over
`max_rows`/`max_bytes` is refused with a structured error carrying
`stage: "limit"` in its details, exactly like a `RefusalCertificate`
(`tgms.tgir.admission.RefusalCertificate`) carries `stage: "plan"/"node"/
"runtime"` — a caller can tell the two refusal mechanisms apart by that one
field, and neither ever silently drops rows to fit a cap. `LimitError`
(`tgms.core.errors`, code `E_LIMIT`, "hard cap violated") is the existing
taxonomy member this reuses rather than inventing a new error type.

**Wall clock is deliberately NOT enforced here, or anywhere in-process.**
`tgms.tgir.admission.Budget`'s docstring is explicit about why: a charge is
only levied between candidates, so a single adapter call that runs long
inside the Rust extension is never interrupted — a signal handler needs the
interpreter, which such a call is not running. `Limits.max_wall_s` is
carried as *configuration* (so one object holds every service-surface
ceiling an operator would read from `TGMS_MAX_*`), but nothing in this
module starts a timer against it. Bounding wall-clock time is
`scripts/tgms_supervise.py`'s job: an out-of-process watcher that kills and
restarts a child stuck past its ceiling.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from tgms.core.errors import LimitError


@dataclass(frozen=True, slots=True)
class LimitRefusal:
    """A structured refusal for the service-surface limits — shaped like
    `tgms.tgir.admission.RefusalCertificate` (a `stage`, what was observed,
    what ceiling it hit) but independent of it: this is the outer serving
    surface's own backpressure, not a TGIR admission decision. `stage` is
    always `"limit"`, so a caller can distinguish "TGIR admission refused
    this plan" from "the service surface refused this call/result" by one
    field alone.
    """

    limit_kind: str          # "rows" | "bytes" | "concurrency"
    observed: int
    ceiling: int
    tool: str | None = None
    stage: str = "limit"

    def to_json(self) -> dict[str, Any]:
        return {"stage": self.stage, "limit_kind": self.limit_kind,
                "observed": self.observed, "ceiling": self.ceiling,
                "tool": self.tool}

    def raise_(self, what: str) -> None:
        """Raise `LimitError` (E_LIMIT) carrying this refusal in `details`.

        Never truncates and never returns a partial result — the caller of
        `raise_` must not have handed any rows to anyone yet when it does.
        """
        raise LimitError(f"{what} exceeds the configured limit",
                         **self.to_json())


@dataclass
class Limits:
    """The four service-surface ceilings, from the constructor or the
    environment. Every field is `None` by default (unenforced) — a bare
    `Limits()` reproduces today's unlimited behaviour exactly, so opting in
    is additive.

    `max_wall_s` is carried but never enforced in-process — see the module
    docstring. It exists so `scripts/tgms_supervise.py` and `ToolRouter` can
    be configured from the same object/environment.
    """

    max_rows: int | None = None
    max_bytes: int | None = None
    max_concurrent: int | None = None
    max_wall_s: float | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Limits":
        """`TGMS_MAX_ROWS`, `TGMS_MAX_BYTES`, `TGMS_MAX_CONCURRENT`,
        `TGMS_MAX_WALL_S`. Unset or empty means unenforced, same as the
        constructor default."""
        env = env if env is not None else os.environ

        def _int(name: str) -> int | None:
            v = env.get(name)
            return int(v) if v not in (None, "") else None

        def _float(name: str) -> float | None:
            v = env.get(name)
            return float(v) if v not in (None, "") else None

        return cls(max_rows=_int("TGMS_MAX_ROWS"),
                   max_bytes=_int("TGMS_MAX_BYTES"),
                   max_concurrent=_int("TGMS_MAX_CONCURRENT"),
                   max_wall_s=_float("TGMS_MAX_WALL_S"))


class ConcurrencyGate:
    """A non-blocking counting gate: `try_acquire()` refuses immediately
    (never queues, never waits) once `max_concurrent` calls are already in
    flight. This is deliberately not a `threading.Semaphore` — a semaphore's
    `acquire()` blocks by default, which would turn an overload into a pile
    of waiting threads instead of an immediate, observable refusal; the
    whole point of a *tool-call* concurrency cap is to shed load, not queue
    it (queueing belongs to `tgms.write.GroupCommitWriter`'s bounded queue,
    the write-side analogue of this)."""

    def __init__(self, max_concurrent: int | None) -> None:
        self.max_concurrent = max_concurrent
        self._lock = threading.Lock()
        self._in_flight = 0

    def try_acquire(self) -> bool:
        if self.max_concurrent is None:
            return True
        with self._lock:
            if self._in_flight >= self.max_concurrent:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        if self.max_concurrent is None:
            return
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight


def check_result_limits(envelope: dict[str, Any], limits: Limits, tool: str) -> None:
    """Count `envelope`'s rows/bytes **after** execution and refuse (never
    truncate) when either exceeds `limits`. A no-op when both ceilings are
    unset (the default).

    Row counting is best-effort and documented as such: TGIR-plan results
    (`op == "tgir_plan"`, or any envelope carrying `tgir.rows_total`) use
    that authoritative count; a single-leaf operator envelope has no one
    canonical "row" field across all fifteen operators (§ the pagination
    convention `rows`/`rows_total` covers most but not all — `diff_snapshots`
    reports `nodes_added`/`nodes_removed`/... instead, `count_temporal_motifs`
    reports a scalar `count`), so this falls back to the size of the `rows`
    list when present, else the sum of every top-level list-valued field —
    which is exact for every operator whose output is one or more row lists,
    and reports 0 (never enforced against) for one whose output is purely
    scalar.
    """
    if limits.max_rows is not None:
        n = _row_count(envelope)
        if n > limits.max_rows:
            LimitRefusal("rows", n, limits.max_rows, tool).raise_(
                f"{tool} result row count ({n})")
    if limits.max_bytes is not None:
        nbytes = _byte_size(envelope)
        if nbytes > limits.max_bytes:
            LimitRefusal("bytes", nbytes, limits.max_bytes, tool).raise_(
                f"{tool} result size ({nbytes} bytes)")


def _row_count(envelope: dict[str, Any]) -> int:
    tgir = envelope.get("tgir")
    if isinstance(tgir, dict) and isinstance(tgir.get("rows_total"), int):
        return tgir["rows_total"]
    rows = envelope.get("rows")
    if isinstance(rows, list):
        return len(rows)
    total = 0
    found = False
    for v in envelope.values():
        if isinstance(v, list):
            total += len(v)
            found = True
    return total if found else 0


def _byte_size(envelope: dict[str, Any]) -> int:
    return len(json.dumps(envelope, default=str).encode("utf-8"))


__all__ = ["ConcurrencyGate", "Limits", "LimitRefusal", "check_result_limits"]
