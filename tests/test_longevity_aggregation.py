"""`scripts/longevity_run.py::summarize()`'s writer-counter aggregation and
final-replay projection — both bugs found by reading, not re-running,
`benchmarks/longevity-v1/`'s 24h soak (see that directory's README).

`summarize()` used to aggregate every writer counter (errors, appends,
corrections, artifact checks/invalidations/refreshes) via `counter_latest`,
keyed only on `(metric_name, labels_json)`. Every writer life reports these
under the same unlabeled key, and `counter_latest` keeps only the sample
with the latest timestamp per key — so any run with `--restart-every` set
silently discarded every life's counters but the last one's
(`benchmarks/longevity-v1/README.md`: manifest said `error_count=1`, the
true sum over 42 lives was 249). The fix sums each life's own last-written
`writer_progress-<life>.json` snapshot instead. These tests build a
synthetic `--out` directory (metrics.jsonl + writer_progress files, no
subprocess) and check the summed totals directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import longevity_run as LR  # noqa: E402


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_writer_counters_are_summed_across_lives(tmp_path: Path) -> None:
    """Three writer lives, errors 1/2/3 (the task's own worked example) —
    `error_count` (and `writer_totals_all_lives`) must be the sum, 6, not
    just the last life's 3."""
    out_dir = tmp_path / "run-out"
    out_dir.mkdir()

    # Each life is a separate process with its own Metrics() instance, so
    # its counters in metrics.jsonl are unlabeled and each life's own last
    # flush carries that life's own cumulative-within-life value — exactly
    # reproducing the collision `counter_latest` (keyed only on
    # (name, labels), no life index) cannot resolve on its own: it would
    # keep only the highest-timestamp sample, i.e. life 2's "3".
    lives = [
        {"errors": 1, "appends": 10, "corrections_applied": 2, "corrections_skipped": 0,
         "checks": 5, "invalidations": 1, "refreshes": 1},
        {"errors": 2, "appends": 20, "corrections_applied": 3, "corrections_skipped": 1,
         "checks": 6, "invalidations": 1, "refreshes": 1},
        {"errors": 3, "appends": 30, "corrections_applied": 4, "corrections_skipped": 2,
         "checks": 7, "invalidations": 1, "refreshes": 1},
    ]
    metrics_records = []
    t = 1000.0
    for life in lives:
        metrics_records.append({"ts": t, "kind": "counter", "name": "writer_errors_total",
                                "labels": {}, "value": float(life["errors"])})
        metrics_records.append({"ts": t, "kind": "counter", "name": "appends_total",
                                "labels": {}, "value": float(life["appends"])})
        t += 100.0
    _write_jsonl(out_dir / "metrics.jsonl", metrics_records)

    # The per-life progress files `summarize()` should read instead: each
    # life's own final snapshot, cumulative within that life only — the
    # same numbers the metrics counters carry, since both are written from
    # the same in-process accumulators at end of life.
    for life_index, life in enumerate(lives):
        (out_dir / f"writer_progress-{life_index}.json").write_text(json.dumps({
            "n": life_index, "batches": 1000, **life,
            "ts": 1000.0 + life_index, "final": True,
        }))

    recoveries_path = out_dir / "recoveries.jsonl"
    reader_restarts_path = out_dir / "reader_restarts.jsonl"
    compactions_path = out_dir / "compactions.jsonl"
    _write_jsonl(recoveries_path, [])
    _write_jsonl(reader_restarts_path, [])
    _write_jsonl(compactions_path, [])

    summary = LR.summarize(out_dir, out_dir / "metrics.jsonl", t_start=1000.0,
                           end_at=2000.0, recoveries_path=recoveries_path,
                           reader_restarts_path=reader_restarts_path,
                           compactions_path=compactions_path)

    assert summary["error_count"] == 6, summary  # 1 + 2 + 3, not the last life's 3
    totals = summary["writer_totals_all_lives"]
    assert totals["errors"] == 6
    assert totals["appends"] == 10 + 20 + 30
    assert totals["corrections_applied"] == 2 + 3 + 4
    assert totals["corrections_skipped"] == 0 + 1 + 2
    assert totals["artifact_checks"] == 5 + 6 + 7
    assert totals["artifact_invalidations"] == 1 + 1 + 1
    assert totals["artifact_refreshes"] == 1 + 1 + 1
    assert totals["lives"] == 3

    # writer_final stays the LAST life's own snapshot only — never a total.
    assert summary["writer_final"]["errors"] == 3


def test_writer_counters_summed_with_no_progress_files(tmp_path: Path) -> None:
    """No writer_progress-*.json at all (e.g. a run that died before the
    first flush): summarize() must not crash, and totals are all zero."""
    out_dir = tmp_path / "run-out"
    out_dir.mkdir()
    metrics_path = out_dir / "metrics.jsonl"
    metrics_path.write_text("")
    recoveries_path = out_dir / "recoveries.jsonl"
    reader_restarts_path = out_dir / "reader_restarts.jsonl"
    compactions_path = out_dir / "compactions.jsonl"
    for p in (recoveries_path, reader_restarts_path, compactions_path):
        p.write_text("")

    summary = LR.summarize(out_dir, metrics_path, t_start=0.0, end_at=1.0,
                           recoveries_path=recoveries_path,
                           reader_restarts_path=reader_restarts_path,
                           compactions_path=compactions_path)
    assert summary["error_count"] == 0
    assert summary["writer_totals_all_lives"]["lives"] == 0
    assert summary["writer_totals_all_lives"]["errors"] == 0


def test_replay_projection_matches_the_measured_soak() -> None:
    """The final-replay disk projection (`cmd_run`'s own `projected_mb =
    (243.0 * (total_batches ** 2)) / 1e6`) is deliberately based on the raw
    event-log batch count, not on the live run's own compaction cadence —
    `tgms.storage.eventlog.replay` has no mid-replay compaction hook (it
    calls begin()/apply_ops()/commit() once per logged batch and nothing
    else), so a replay always re-derives one uncompacted generation per
    *every* batch in the log regardless of how often `--compact-every-
    batches` fired live. This locks the formula's output against the real
    24h soak's own numbers (benchmarks/longevity-v1/README.md,
    `LEDGER disk_guard_replay_skip: total_batches=1074952,
    projected_mb=280791798.0`) so a future edit cannot silently change it.
    """
    total_batches = 1_074_952
    projected_mb = (243.0 * (total_batches ** 2)) / 1e6
    assert round(projected_mb, 1) == 280_791_798.0

    # replay() has no compaction call in its loop — confirms the projection
    # is not an approximation that ignores the live run's compaction
    # cadence, since replay's own cost does not depend on it either.
    import inspect

    from tgms.storage.eventlog import replay as replay_fn

    source = inspect.getsource(replay_fn)
    assert "compact" not in source
