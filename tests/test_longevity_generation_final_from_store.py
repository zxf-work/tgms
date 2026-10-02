"""Regression test for the stale-`generation_final` defect found by the
soak 4 record step (`benchmarks/longevity-v1/README.md`'s "Full-mode `tgms
check` of the final store" and "Honest limits" sections,
`benchmarks/longevity-v1/verify-full-4.txt`): `child_writer`'s periodic
`metrics.gauge("generation", ...)` sample (emitted only on the
`report_every_s`-cadenced flush inside the commit loop) can freeze a few
commits short of the store's true final generation, because the shutdown
code that runs after the loop exits never re-samples it. Soak 4's manifest
reported `generation_final=1896750.0` while `tgms check --mode full`
(reading the store's own `native/manifests/` listing) found the true final
generation was `1897091` — 341 ahead.

The fix (`scripts/longevity_run.py::summarize`) takes `generation_final`
from a `final_generation` argument — the just-reopened final store's own
`adapter.generation`, supplied by the orchestrator — and keeps the old
gauge-derived value under the new `generation_final_gauge` field so old
records stay interpretable.

This test drives `child_writer` directly (in-process, no subprocess, no
readers) against a tiny native store for about a second, with a very
short `report_every_s` so several periodic gauge flushes happen, then
reopens the store exactly the way the orchestrator's final-verification
step does and checks `summarize()` reports that store's own generation,
not whatever the last gauge sample happened to be.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import longevity_run as LR  # noqa: E402


def _build_tiny_store(path: Path) -> int:
    import tgms

    store = tgms.open(path, backend="native")
    n = 200
    events = [{"src": f"n{i}", "dst": f"n{(i + 1) % n}", "rel_type": "R",
              "vt_s": i} for i in range(n)]
    store.ingest_events(events)
    store.close()
    return n


def test_generation_final_comes_from_final_store_not_last_gauge_sample(
        tmp_path: Path) -> None:
    store_dir = tmp_path / "tiny-store"
    start_n = _build_tiny_store(store_dir)

    out_dir = tmp_path / "run-out"
    out_dir.mkdir()
    metrics_path = out_dir / "metrics.jsonl"
    progress_path = out_dir / "writer_progress-0.json"
    control_path = out_dir / "writer_control.json"
    compactions_path = out_dir / "compactions.jsonl"

    cfg = {
        "store": str(store_dir), "mix": "balanced", "seed": 1,
        "artifacts": 0, "compact_every_batches": 10_000,
        "compact_min_interval_s": 2.0, "start_n": start_n,
        "end_at": time.time() + 1.0, "control_path": str(control_path),
        "progress_path": str(progress_path),
        "compactions_path": str(compactions_path),
        "metrics_path": str(metrics_path),
        # Short enough that the ~1s run crosses it several times, so the
        # metrics.jsonl "generation" gauge gets multiple periodic samples
        # — exactly the series `summarize()`'s old, buggy code read
        # `generation_final` from.
        "report_every_s": 0.05,
        "writer_sleep_s": 0.0, "life_index": 0,
    }

    LR.child_writer(cfg)  # in-process, no subprocess — runs for ~1s

    assert metrics_path.exists(), "no metrics.jsonl written"
    gauge_lines = [json.loads(ln) for ln in metrics_path.read_text().splitlines()
                   if ln.strip() and json.loads(ln).get("name") == "generation"]
    assert gauge_lines, "expected at least one periodic 'generation' gauge sample"

    # Reopen the store exactly the way the orchestrator's final-verification
    # step does (scripts/longevity_run.py: `final = _reopen_rw_with_retry
    # (live_store)` then `final.adapter.generation`) to get the true final
    # generation independently of anything summarize() computes.
    import tgms
    final = tgms.open(store_dir, backend="native")
    true_final_generation = final.adapter.generation
    final.close()

    recoveries_path = out_dir / "recoveries.jsonl"
    reader_restarts_path = out_dir / "reader_restarts.jsonl"

    summary = LR.summarize(out_dir, metrics_path, t_start=time.time() - 2.0,
                           end_at=time.time(), recoveries_path=recoveries_path,
                           reader_restarts_path=reader_restarts_path,
                           compactions_path=compactions_path,
                           final_generation=true_final_generation)

    assert "generation_final_gauge" in summary, (
        "old gauge-derived value must stay available under its own name "
        "so pre-fix records stay interpretable")
    assert summary["generation_final_gauge"] == gauge_lines[-1]["value"]

    assert summary["generation_final"] == float(true_final_generation), (
        f"generation_final must come from the final store's own "
        f"adapter.generation ({true_final_generation}), not the last "
        f"gauge sample ({summary['generation_final_gauge']}) — the soak 4 "
        f"defect was exactly this gap (341 generations, "
        f"benchmarks/longevity-v1/README.md)")

    # Without a final_generation argument (the shape every pre-existing
    # direct call to summarize() uses, e.g. test_longevity_smoke.py's
    # test_summarize_sums_writer_counters_across_lives), the field must
    # still fall back to the gauge-derived value rather than erroring or
    # going missing.
    summary_no_arg = LR.summarize(out_dir, metrics_path, t_start=time.time() - 2.0,
                                  end_at=time.time(), recoveries_path=recoveries_path,
                                  reader_restarts_path=reader_restarts_path,
                                  compactions_path=compactions_path)
    assert summary_no_arg["generation_final"] == summary_no_arg["generation_final_gauge"]
