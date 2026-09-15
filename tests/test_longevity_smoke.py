"""Smoke test for `scripts/longevity_run.py` (P4.5) — a 60-90s run on a tiny
store this test builds itself, standing in for the 24h/72h soaks this
harness exists to run. Exercises the whole arc end to end: writer + readers
+ artifact checker/refresher, at least one restart cycle and one
compaction, final `verify()` + replay/digest equivalence, and a manifest
that validates against `benchmarks/schema/result_manifest.schema.json`.

Not a substitute for the real soak — a few restarts and one compaction in
90 seconds says the plumbing works, not that metadata stays bounded over
24 hours. That is exactly why this is a smoke test and the dev-host run in
the commit report is separately labeled "not a reported number".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_result_manifest as CRM  # noqa: E402
import longevity_run as LR  # noqa: E402 — reuse its own disk-size walk
from tgms.telemetry.metrics import Metrics  # noqa: E402

#: Generous headroom over what a few-thousand-event store should ever need:
#: this is the PI ruling 2026-09-14 ceiling for anything this test writes
#: under tmp_path, not a claim about what a real run needs.
MAX_DISK_MB = 50


def _build_tiny_store(path: Path) -> None:
    import tgms

    store = tgms.open(path, backend="native")
    n = 3000
    events = [{"src": f"n{i}", "dst": f"n{(i + 1) % n}", "rel_type": "R",
              "vt_s": i} for i in range(n)]
    store.ingest_events(events)
    store.close()


def test_writer_counters_emit_deltas_not_cumulative_snapshots(tmp_path: Path) -> None:
    """Guards against `child_writer` re-feeding its cumulative accumulators
    into `Metrics.counter` (which *adds*) on every periodic flush — that
    bug made the sink's running total the sum of k cumulative snapshots
    (10, 20, 30 -> 60) instead of the count itself. `_emit_cumulative_counters`
    must convert each life's cumulative (appends, applied, skipped) into the
    delta since the previous call before handing it to `counter()`.
    """
    m = Metrics(tmp_path / "metrics.jsonl")
    last: dict[str, int] = {}

    for appends, applied, skipped in [(10, 3, 1), (20, 5, 2), (30, 7, 3)]:
        LR._emit_cumulative_counters(m, last,
                                     appends_total=appends,
                                     corrections_applied_total=applied,
                                     corrections_skipped_total=skipped)
        m.flush()

    lines = [json.loads(line)
             for line in (tmp_path / "metrics.jsonl").read_text().splitlines()]
    by_name: dict[str, list[dict]] = {}
    for rec in lines:
        by_name.setdefault(rec["name"], []).append(rec)

    assert [rec["value"] for rec in by_name["appends_total"]] == [10, 20, 30]
    assert [rec["value"] for rec in by_name["corrections_applied_total"]] == [3, 5, 7]
    assert [rec["value"] for rec in by_name["corrections_skipped_total"]] == [1, 2, 3]

    assert by_name["appends_total"][-1]["value"] == 30
    assert by_name["corrections_applied_total"][-1]["value"] == 7
    assert by_name["corrections_skipped_total"][-1]["value"] == 3

    assert all(rec["kind"] == "counter" and rec["labels"] == {} for rec in lines)


def test_smoke_run(tmp_path: Path) -> None:
    store_dir = tmp_path / "tiny-store"
    _build_tiny_store(store_dir)

    out_dir = tmp_path / "run-out"
    cmd = [sys.executable, str(ROOT / "scripts" / "longevity_run.py"),
          "--store", str(store_dir), "--duration", "40s", "--mix", "balanced",
          "--readers", "1", "--compact-every-batches", "5",
          "--compact-min-interval-s", "0.5", "--reader-reopen-every-s", "10",
          # Throttled: the final replay step re-applies every logged batch as
          # its own uncompacted generation (module docstring, D-149's own
          # O(batches^2) manifest-growth pathology) — measured directly
          # (scratch probe against this same tiny store shape): ~1,200
          # unthrottled batches already replays to ~350 MB of manifests, over
          # this test's own 50 MB ceiling. `--writer-sleep-s` keeps the batch
          # count (and therefore the replay's own footprint) low without
          # touching how many restarts or compactions happen.
          "--writer-sleep-s", "0.2",
          "--restart-every", "10s", "--artifacts", "5", "--seed", "1",
          "--out", str(out_dir), "--max-disk-mb", str(MAX_DISK_MB)]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
    print(proc.stdout)
    print(proc.stderr, file=sys.stderr)

    assert proc.stdout.splitlines()[0].startswith("RUN_STARTED commit=")
    assert proc.returncode == 0, f"longevity_run.py failed:\n{proc.stdout}\n{proc.stderr}"

    manifests = sorted(out_dir.glob("longevity-*.json"))
    assert manifests, "no manifest written"
    manifest = json.loads(manifests[-1].read_text())

    schema = CRM.load_schema()
    ok, message = CRM.validate_one(manifests[-1], schema)
    assert ok, f"manifest does not conform to result_manifest.schema.json: {message}"

    summary = manifest["summary"]
    assert summary["digest_equal"] is True, "final replay digest did not match"
    assert summary["verify_healthy"] is True
    assert summary["recoveries"] >= 1, "expected at least one restart cycle"
    assert summary["compactions"] >= 1, "expected at least one compaction"
    assert summary["error_count"] == 0, f"unexpected errors: {summary}"

    metrics_path = out_dir / "metrics.jsonl"
    assert metrics_path.exists(), "no metrics.jsonl written"
    lines = [ln for ln in metrics_path.read_text().splitlines() if ln.strip()]
    assert lines, "metrics.jsonl is empty"
    for ln in lines:
        rec = json.loads(ln)                 # every line must parse
        assert rec["kind"] in ("counter", "gauge", "histogram")
        assert "ts" in rec and "name" in rec

    # The whole point of this test (PI ruling 2026-09-14, the 38 GB local
    # incident this harness's own fixes are named after in the module
    # docstring): the working directory must stay small, not merely finish.
    used_mb = LR._dir_size_bytes(out_dir) / 1e6
    assert used_mb < MAX_DISK_MB, (
        f"--out grew to {used_mb:.1f} MB, over the {MAX_DISK_MB} MB smoke "
        f"ceiling — see scripts/longevity_run.py's module docstring for the "
        f"38 GB incident this guards against")

    # Multi-life structural checks (cb2e055, hardened further by the
    # pre-crash flush in child_writer's control-file block): --restart-every
    # 10s over a 40s run must produce at least one restart, and every life —
    # including ones killed by a designed crash point — now flushes its own
    # writer_progress-<life>.json right before the crash point is armed, so
    # writer_totals_all_lives must be the sum over every life's own
    # progress snapshot, not just the last one's (see
    # test_summarize_sums_writer_counters_across_lives for the synthetic,
    # deterministic version of this same check).
    progress_files = sorted(out_dir.glob("writer_progress-*.json"))
    assert len(progress_files) >= 2, (
        f"expected at least 2 writer lives (one restart) from "
        f"--restart-every 10s over 40s, got {len(progress_files)}")

    totals = summary["writer_totals_all_lives"]
    assert totals["lives"] == len(progress_files) == summary["recoveries"] + 1, (
        f"writer_totals_all_lives['lives']={totals['lives']} should equal "
        f"the number of writer_progress-*.json files "
        f"({len(progress_files)}) and recoveries+1 ({summary['recoveries'] + 1})")

    per_life = [json.loads(p.read_text()) for p in progress_files]
    assert totals["errors"] == sum(p["errors"] for p in per_life), (
        "writer_totals_all_lives['errors'] must be the sum of every life's "
        "own 'errors', not just the last life's (the 1-vs-249 defect)")
    assert totals["appends"] == sum(p["appends"] for p in per_life), (
        "writer_totals_all_lives['appends'] must be the sum of every "
        "life's own 'appends', not just the last life's")

    # Strict: the earlier lives appended too (balanced mix is 25 append /
    # 10 correction weight, 0.2s writer sleep, so even a 10s life makes
    # dozens of batches), so the all-lives total must exceed the last
    # life's own final count, not merely be >= it.
    assert totals["appends"] > summary["writer_final"]["appends"], (
        "writer_totals_all_lives['appends'] should exceed writer_final's "
        "own appends — the earlier lives' appends must be counted too")
    assert totals["appends"] > 0, "expected the writer to make progress"


def test_summarize_sums_writer_counters_across_lives(tmp_path: Path) -> None:
    """Regression test for the defect fixed by cb2e055 and documented in
    benchmarks/longevity-v1/README.md's "errors observed" section: writer
    counters (errors, appends, ...) are unlabeled and reset to 0 in every
    fresh writer "life", so the old `counter_latest` aggregation — keyed
    only on (metric name, labels) and keeping whichever sample had the
    latest timestamp — silently collapsed every life onto the last one's
    count. A real 24h/--restart-every soak with 42 lives reported
    error_count=1 (the last life's own count) when the true sum over all
    lives was 249.

    This test calls `LR.summarize()` directly on a synthetic three-life run
    directory (no subprocess, no engine, no store) so the fix is pinned
    down deterministically instead of relying on however many restarts a
    real timed soak happens to hit.
    """
    out_dir = tmp_path / "run-out"
    out_dir.mkdir()

    # Three lives (two restarts). Every counter uses a distinct value per
    # life so an accidental "last life only" or "first life only" bug would
    # produce a visibly wrong sum instead of silently matching by luck.
    # errors: 3, 5, 0 — life 2's zero-error life must still count as a life.
    life_progress = [
        {"n": 100, "batches": 13, "appends": 10, "corrections_applied": 2,
         "corrections_skipped": 1, "compactions": 1, "compactions_throttled": 0,
         "errors": 3, "checks": 4, "invalidations": 1, "refreshes": 1,
         "ts": 100.0, "final": True},
        {"n": 90, "batches": 8, "appends": 7, "corrections_applied": 1,
         "corrections_skipped": 0, "compactions": 2, "compactions_throttled": 1,
         "errors": 5, "checks": 3, "invalidations": 0, "refreshes": 0,
         "ts": 200.0, "final": True},
        {"n": 70, "batches": 9, "appends": 4, "corrections_applied": 3,
         "corrections_skipped": 2, "compactions": 0, "compactions_throttled": 1,
         "errors": 0, "checks": 2, "invalidations": 1, "refreshes": 1,
         "ts": 300.0, "final": True},
    ]
    for i, prog in enumerate(life_progress):
        (out_dir / f"writer_progress-{i}.json").write_text(json.dumps(prog))

    # metrics.jsonl: the pre-fix-shaped unlabeled writer_errors_total samples
    # that `counter_latest` used to collapse (life 1 resets to 0, so its own
    # cumulative sample by the time it flushes is 5, not 3+5=8), a labeled
    # writer_errors_total pair (post-fix, still per-flush-cumulative-within-a-
    # life so still not a run total on its own), the reader-side counters
    # `child_reader` already labels correctly, and a couple of gauge lines so
    # that code path runs too.
    metrics_path = out_dir / "metrics.jsonl"
    metrics_lines = [
        {"kind": "counter", "name": "writer_errors_total", "labels": {},
         "value": 3, "ts": 100.0},
        {"kind": "counter", "name": "writer_errors_total", "labels": {},
         "value": 5, "ts": 200.0},
        {"kind": "counter", "name": "writer_errors_total",
         "labels": {"error": "RuntimeError"}, "value": 3, "ts": 100.0},
        {"kind": "counter", "name": "writer_errors_total",
         "labels": {"error": "RuntimeError"}, "value": 5, "ts": 200.0},
        {"kind": "counter", "name": "reader_errors_total",
         "labels": {"reader": 0, "query": "q1", "error": "ValueError"},
         "value": 2, "ts": 150.0},
        {"kind": "counter", "name": "queries_total",
         "labels": {"reader": 0, "query": "q1"}, "value": 40, "ts": 150.0},
        {"kind": "gauge", "name": "rss_kb", "labels": {}, "value": 12000.0,
         "ts": 100.0},
        {"kind": "gauge", "name": "rss_kb", "labels": {}, "value": 12500.0,
         "ts": 200.0},
    ]
    metrics_path.write_text(
        "\n".join(json.dumps(rec) for rec in metrics_lines) + "\n")

    recoveries_path = out_dir / "recoveries.jsonl"
    recoveries_path.write_text(
        json.dumps({"kind": "designed", "ts": 100.0}) + "\n" +
        json.dumps({"kind": "unexpected", "ts": 200.0}) + "\n")

    # Absent, the way a run with no reader restarts and no compactions.jsonl
    # entries would leave them — `_read_jsonl` must tolerate missing files.
    reader_restarts_path = out_dir / "reader_restarts.jsonl"
    compactions_path = out_dir / "compactions.jsonl"

    summary = LR.summarize(out_dir, metrics_path, t_start=0.0, end_at=300.0,
                           recoveries_path=recoveries_path,
                           reader_restarts_path=reader_restarts_path,
                           compactions_path=compactions_path)

    totals = summary["writer_totals_all_lives"]
    assert totals["lives"] == 3, (
        f"expected 3 lives (3 writer_progress-*.json files), got "
        f"{totals['lives']}")
    assert totals["errors"] == 8, (
        f"writer_totals_all_lives['errors'] should sum every life's own "
        f"'errors' (3+5+0=8), got {totals['errors']} — this is exactly the "
        f"1-vs-249 defect if it regresses to a single life's count")
    assert totals["appends"] == 21, f"expected 10+7+4=21, got {totals['appends']}"
    assert totals["corrections_applied"] == 6, (
        f"expected 2+1+3=6, got {totals['corrections_applied']}")
    assert totals["corrections_skipped"] == 3, (
        f"expected 1+0+2=3, got {totals['corrections_skipped']}")
    assert totals["compactions"] == 3, f"expected 1+2+0=3, got {totals['compactions']}"
    assert totals["compactions_throttled"] == 2, (
        f"expected 0+1+1=2, got {totals['compactions_throttled']}")
    assert totals["artifact_checks"] == 9, f"expected 4+3+2=9, got {totals['artifact_checks']}"
    assert totals["artifact_invalidations"] == 2, (
        f"expected 1+0+1=2, got {totals['artifact_invalidations']}")
    assert totals["artifact_refreshes"] == 2, (
        f"expected 1+0+1=2, got {totals['artifact_refreshes']}")

    assert summary["reader_errors_total"] == 2, (
        f"expected the single labeled reader_errors_total sample (value=2), "
        f"got {summary['reader_errors_total']}")

    # error_count = writer life-sum (8) + reader_errors_total (2) +
    # unexpected recoveries (1, the "unexpected" line — "designed" doesn't
    # count).
    assert summary["error_count"] == 8 + 2 + 1, (
        f"expected error_count = writer_totals_all_lives['errors'] (8) + "
        f"reader_errors_total (2) + unexpected recoveries (1) = 11, got "
        f"{summary['error_count']}")

    # writer_final stays the LAST life's own snapshot only — never a total —
    # by design (see the comment above writer_totals_all_lives in
    # scripts/longevity_run.py's summarize()).
    assert summary["writer_final"]["errors"] == 0, (
        "writer_final must be life 2's own snapshot (errors=0), not a sum "
        "or any earlier life's snapshot")
    assert summary["writer_final"] == life_progress[-1], (
        "writer_final must equal exactly the dict written to "
        "writer_progress-2.json (last-life-only semantics)")

    # Anti-regression: the pre-fix `counter_latest` aggregation (verified
    # directly against this same synthetic directory at cb2e055^) reports
    # error_count = 13, not the true 11. It keeps one last-sample per
    # (name, labels) key: the unlabeled writer_errors_total series' last
    # sample is 5 and the labeled {"error": "RuntimeError"} series' last
    # sample is also 5 (both are life 1's own cumulative-within-a-life
    # value, life 2 never emitting a writer_errors_total sample at all
    # since it has 0 errors) — summed together that's 10, plus
    # reader_errors_total (2) plus the one unexpected recovery (1) = 13.
    # It never reaches the true per-life sum of 8 because both keys
    # independently collapse onto life 1's last sample instead of summing
    # every life's own writer_progress-<life>.json (the 1-vs-249 defect in
    # benchmarks/longevity-v1/README.md's "errors observed" section).
    assert summary["error_count"] != 13, (
        "error_count must not equal 13 — that is exactly what the pre-fix "
        "counter_latest last-sample-wins aggregation reports on this same "
        "synthetic directory (unlabeled last-sample 5 + labeled "
        "last-sample 5 + reader 2 + unexpected 1), instead of the true "
        "per-life sum of 11")
