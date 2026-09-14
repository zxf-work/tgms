"""A tiny, dependency-free JSONL metrics sink.

`Metrics(path)` accumulates counters, gauges, and latency histograms in
memory and writes them as newline-delimited JSON on `flush()` — one line
per tracked series, `{"ts", "kind", "name", "labels", "value"}` for a
counter/gauge or `{"ts", "kind", "name", "labels", "hist"}` for a
histogram. It takes no dependency beyond the standard library and is safe
to call from multiple threads.

This module is a standalone skeleton: nothing in `tgms.store` or the
server calls it yet (that wiring belongs to the lanes that own those call
sites). `path=None` — including the default when `TGMS_METRICS_PATH` is
also unset — makes every method a no-op, so a caller can hold a `Metrics`
instance unconditionally without an `if telemetry_enabled` branch.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

#: Upper bounds (milliseconds) of the fixed histogram buckets, plus an
#: overflow bucket. A value falls in the first bucket whose bound it does
#: not exceed. Quantiles are then linearly interpolated within whichever
#: bucket they fall in (Prometheus's `histogram_quantile` approach) — an
#: estimate, not the exact order statistic, and only as fine-grained as
#: these boundaries.
DEFAULT_BUCKETS_MS: tuple[float, ...] = (
    1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, math.inf,
)

_Key = tuple[str, tuple[tuple[str, object], ...]]


def _key(name: str, labels: dict[str, object]) -> _Key:
    return (name, tuple(sorted(labels.items())))


class _Histogram:
    """A fixed-bucket latency histogram (values in milliseconds)."""

    __slots__ = ("bounds", "counts", "count", "total")

    def __init__(self, bounds: tuple[float, ...] = DEFAULT_BUCKETS_MS) -> None:
        self.bounds = bounds
        self.counts = [0] * len(bounds)
        self.count = 0
        self.total = 0.0

    def add(self, value_ms: float) -> None:
        self.count += 1
        self.total += value_ms
        for i, bound in enumerate(self.bounds):
            if value_ms <= bound:
                self.counts[i] += 1
                return
        self.counts[-1] += 1  # unreachable while the last bound is +inf

    def quantile(self, q: float) -> float:
        if self.count == 0:
            return 0.0
        target = q * self.count
        cumulative = 0.0
        prev_bound = 0.0
        for bound, count in zip(self.bounds, self.counts):
            if cumulative + count >= target and count > 0:
                fraction = (target - cumulative) / count
                upper = bound if math.isfinite(bound) else prev_bound
                return prev_bound + fraction * (upper - prev_bound)
            cumulative += count
            if math.isfinite(bound):
                prev_bound = bound
        return prev_bound

    def summary(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "sum_ms": self.total,
            "p50": self.quantile(0.50),
            "p95": self.quantile(0.95),
            "p99": self.quantile(0.99),
        }


class Metrics:
    """Accumulates counters/gauges/histograms; `flush()` writes JSONL.

    `path` wins if given; otherwise the `TGMS_METRICS_PATH` environment
    variable is used; if neither is set, every method is a no-op.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        if path is None:
            path = os.environ.get("TGMS_METRICS_PATH") or None
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._counters: dict[_Key, float] = {}
        self._gauges: dict[_Key, float] = {}
        self._histograms: dict[_Key, _Histogram] = {}

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def counter(self, name: str, delta: float = 1, **labels: object) -> None:
        if self._path is None:
            return
        key = _key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + delta

    def gauge(self, name: str, value: float, **labels: object) -> None:
        if self._path is None:
            return
        key = _key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, value_ms: float, **labels: object) -> None:
        if self._path is None:
            return
        key = _key(name, labels)
        with self._lock:
            hist = self._histograms.get(key)
            if hist is None:
                hist = self._histograms[key] = _Histogram()
            hist.add(value_ms)

    def flush(self, fsync: bool = False) -> None:
        """Append one JSONL line per tracked series to `path`.

        A no-op (including the file open) when there is nothing tracked
        yet, so an idle sink never creates an empty file.
        """
        if self._path is None:
            return
        ts = time.time()
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in self._counters.items():
                lines.append(_line(ts, "counter", name, labels, value=value))
            for (name, labels), value in self._gauges.items():
                lines.append(_line(ts, "gauge", name, labels, value=value))
            for (name, labels), hist in self._histograms.items():
                lines.append(_line(ts, "histogram", name, labels, hist=hist.summary()))
        if not lines:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())


def _line(
    ts: float,
    kind: str,
    name: str,
    labels: tuple[tuple[str, object], ...],
    **payload: object,
) -> str:
    record = {"ts": ts, "kind": kind, "name": name, "labels": dict(labels), **payload}
    return json.dumps(record, sort_keys=True)
