"""P0.6: tgms.telemetry.metrics — the dependency-free JSONL metrics sink.

Standalone module tests only: nothing here exercises tgms.store or the
server, since this lane does not wire the sink into either.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tgms.telemetry.metrics import Metrics


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_counters_accumulate(tmp_path: Path):
    m = Metrics(tmp_path / "m.jsonl")
    m.counter("requests")
    m.counter("requests", 2)
    m.counter("requests", 3, route="/ask")  # distinct series (different labels)
    m.flush()

    lines = _read_lines(tmp_path / "m.jsonl")
    by_labels = {tuple(sorted(rec["labels"].items())): rec for rec in lines}
    assert by_labels[()]["value"] == 3
    assert by_labels[(("route", "/ask"),)]["value"] == 3
    assert all(rec["kind"] == "counter" and rec["name"] == "requests" for rec in lines)


def test_counter_accumulates_across_flushes(tmp_path: Path):
    m = Metrics(tmp_path / "m.jsonl")
    m.counter("hits", 1)
    m.flush()
    m.counter("hits", 1)
    m.flush()

    lines = _read_lines(tmp_path / "m.jsonl")
    assert [rec["value"] for rec in lines] == [1, 2]


def test_gauge_holds_last_value(tmp_path: Path):
    m = Metrics(tmp_path / "m.jsonl")
    m.gauge("queue_depth", 5)
    m.gauge("queue_depth", 8)
    m.flush()

    [line] = _read_lines(tmp_path / "m.jsonl")
    assert line["kind"] == "gauge"
    assert line["value"] == 8


def test_histogram_quantiles_on_known_sample(tmp_path: Path):
    m = Metrics(tmp_path / "m.jsonl")
    # 50 observations at the 10ms bucket bound, 30 at 50ms, 20 at 100ms —
    # each value lands exactly on a bucket boundary, so the fixed-bucket
    # linear-interpolation estimate is reproducible by hand:
    #   p50: target=50th of 100 -> falls at the top of the [5,10] bucket
    #        (cumulative 0->50 exactly fills it) => 10.0
    #   p95: target=95 -> 80 counted through the 50ms bucket, 15/20 of the
    #        way through (80,100] => 50 + 0.75*(100-50) = 87.5
    #   p99: target=99 -> 19/20 of the way through the same bucket
    #        => 50 + 0.95*(100-50) = 97.5
    for _ in range(50):
        m.observe("latency", 10)
    for _ in range(30):
        m.observe("latency", 50)
    for _ in range(20):
        m.observe("latency", 100)
    m.flush()

    [line] = _read_lines(tmp_path / "m.jsonl")
    assert line["kind"] == "histogram"
    hist = line["hist"]
    assert hist["count"] == 100
    assert hist["sum_ms"] == 50 * 10 + 30 * 50 + 20 * 100
    assert hist["p50"] == pytest.approx(10.0)
    assert hist["p95"] == pytest.approx(87.5)
    assert hist["p99"] == pytest.approx(97.5)


def test_jsonl_lines_parse_and_one_series_per_line(tmp_path: Path):
    m = Metrics(tmp_path / "m.jsonl")
    m.counter("c")
    m.gauge("g", 1)
    m.observe("h", 3)
    m.flush()

    lines = _read_lines(tmp_path / "m.jsonl")
    assert {rec["kind"] for rec in lines} == {"counter", "gauge", "histogram"}
    for rec in lines:
        assert set(rec) >= {"ts", "kind", "name", "labels"}


def test_none_path_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TGMS_METRICS_PATH", raising=False)
    m = Metrics(None)
    assert not m.enabled
    m.counter("c")
    m.gauge("g", 1)
    m.observe("h", 3)
    m.flush()  # must not raise, and must not create anything

    assert list(tmp_path.iterdir()) == []


def test_env_override_when_path_omitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "env.jsonl"
    monkeypatch.setenv("TGMS_METRICS_PATH", str(target))
    m = Metrics()
    assert m.enabled
    m.counter("c")
    m.flush()

    assert target.exists()
    assert _read_lines(target)[0]["name"] == "c"


def test_explicit_path_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TGMS_METRICS_PATH", str(tmp_path / "env.jsonl"))
    explicit = tmp_path / "explicit.jsonl"
    m = Metrics(explicit)
    m.counter("c")
    m.flush()

    assert explicit.exists()
    assert not (tmp_path / "env.jsonl").exists()


def test_thread_safety_eight_threads(tmp_path: Path):
    m = Metrics(tmp_path / "m.jsonl")
    n_threads = 8
    increments_per_thread = 500

    def worker():
        for _ in range(increments_per_thread):
            m.counter("hits")
            m.observe("latency", 1)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    m.flush()

    lines = _read_lines(tmp_path / "m.jsonl")
    counter_line = next(rec for rec in lines if rec["kind"] == "counter")
    hist_line = next(rec for rec in lines if rec["kind"] == "histogram")
    expected = n_threads * increments_per_thread
    assert counter_line["value"] == expected
    assert hist_line["hist"]["count"] == expected
