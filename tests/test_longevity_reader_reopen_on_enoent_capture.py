"""`scripts/longevity_run.py::child_reader` must capture the native
adapter's D-088 reopen-on-ENOENT net counter
(`tgms.storage.native.adapter.NativeAdapter.reopen_on_enoent_total()`,
P-SOAK4 prediction b) per reader, so a later soak record can show whether
the net is firing.

The counter lives on one `NativeAdapter` *instance*
(`tgms/storage/native/adapter.py`'s own comment: "Per-adapter, not per
native handle"), so it survives the net's own internal `_reopen()` calls —
but `child_reader`'s own `--reader-reopen-every-s` handle refresh
(`store.close(); store = tgms.open(...)`) builds a brand-new adapter, which
starts back at 0. `child_reader` must therefore keep its own running total
across the reader's whole life, folding the retiring handle's count in
right before every scheduled reopen and before the final flush.

This test exercises `child_reader` for real (in-process, no subprocess)
against a tiny native store with `reopen_every_s` set low enough to force
several handle refreshes in under a second, and checks the new counter
shows up, at value 0 (a healthy run has no ENOENT reopens to report), in
all three places it now needs to: `metrics.jsonl` as
`reader_reopen_on_enoent_total`, the reader's own `reader-<i>-progress.json`
as `reopen_on_enoent_total`, and `summarize()`'s manifest `summary` as
`reader_reopen_on_enoent_total`.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import longevity_run as LR  # noqa: E402


def _build_tiny_store(path: Path) -> None:
    import tgms

    store = tgms.open(path, backend="native")
    n = 200
    events = [{"src": f"n{i}", "dst": f"n{(i + 1) % n}", "rel_type": "R",
              "vt_s": i} for i in range(n)]
    store.ingest_events(events)
    store.close()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_reader_reopen_on_enoent_total_captured_end_to_end(tmp_path: Path) -> None:
    store_dir = tmp_path / "tiny-store"
    _build_tiny_store(store_dir)

    out_dir = tmp_path / "run-out"
    out_dir.mkdir()
    metrics_path = out_dir / "metrics.jsonl"
    progress_path = out_dir / "reader-0-progress.json"

    mix = [{"id": "hist", "op": "entity_history", "args": {"uid": "n0"}}]
    cfg = {
        "idx": 0,
        "store": str(store_dir),
        "mix": mix,
        "metrics_path": str(metrics_path),
        "progress_path": str(progress_path),
        "report_every_s": 0.2,
        # Deliberately tiny relative to the run's own 3s window (below), so
        # that even under heavy machine load (other test files' subprocess
        # spawns, concurrent sessions on this host) the outer loop still
        # gets many chances to cross the reopen threshold — this test's own
        # point is the cross-reopen accumulation, which needs at least one
        # real reopen to exercise at all.
        "reopen_every_s": 0.02,
        "end_at": time.time() + 3.0,
    }

    LR.child_reader(cfg)

    # --- metrics.jsonl ----------------------------------------------------- #
    lines = [json.loads(ln) for ln in metrics_path.read_text().splitlines() if ln.strip()]
    reopen_on_enoent_recs = [
        rec for rec in lines
        if rec.get("kind") == "counter" and rec.get("name") == "reader_reopen_on_enoent_total"
    ]
    assert reopen_on_enoent_recs, "reader_reopen_on_enoent_total never emitted to metrics.jsonl"
    assert all(rec["labels"] == {"reader": 0} for rec in reopen_on_enoent_recs), (
        reopen_on_enoent_recs)
    assert reopen_on_enoent_recs[-1]["value"] == 0

    # A real handle-refresh schedule must actually have fired, or this test
    # is not exercising the cross-reopen accumulation it claims to.
    reopens_recs = [rec for rec in lines
                    if rec.get("kind") == "counter" and rec.get("name") == "reader_reopens_total"]
    assert reopens_recs and reopens_recs[-1]["value"] >= 1, (
        "expected at least one scheduled reopen in this run — the test's "
        "reopen_every_s is not actually forcing handle refreshes")

    # --- reader-0-progress.json --------------------------------------------- #
    final = json.loads(progress_path.read_text())
    assert final["final"] is True
    assert final["reopen_on_enoent_total"] == 0
    assert final["reopens"] >= 1

    # --- summarize()'s manifest summary -------------------------------------- #
    recoveries_path = out_dir / "recoveries.jsonl"
    reader_restarts_path = out_dir / "reader_restarts.jsonl"
    compactions_path = out_dir / "compactions.jsonl"
    _write_jsonl(recoveries_path, [])
    _write_jsonl(reader_restarts_path, [])
    _write_jsonl(compactions_path, [])

    summary = LR.summarize(out_dir, metrics_path, t_start=time.time() - 2.0,
                           end_at=time.time(), recoveries_path=recoveries_path,
                           reader_restarts_path=reader_restarts_path,
                           compactions_path=compactions_path)

    assert "reader_reopen_on_enoent_total" in summary
    assert summary["reader_reopen_on_enoent_total"] == 0
