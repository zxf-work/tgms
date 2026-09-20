"""Regression test for the missed-restart defect diagnosed against the
2026-09-19 72h soak (`longevity/2026-09-19-soak3`): `child_writer`'s
control-file block (`scripts/longevity_run.py`) armed a designed restart by
setting `TGMS_CRASH_POINT` and then always called `store.adapter.compact()`
for a MAINT_BOUNDARIES boundary — regardless of which one the orchestrator
picked. `compact_before_install`'s crash point lives inside `compact()`
(`crates/tgms-engine-core/src/compact.rs`), so that boundary fired correctly.
`gc_mid_delete`'s crash point lives inside `gc()`
(`crates/tgms-engine-core/src/gc.rs`) instead — a call `compact()` never
makes — so an armed `gc_mid_delete` restart never died there. Nothing else
in the writer calls `gc()` with the crash point still armed either: the only
other `gc()` call is the periodic real-maintenance one
(`batches % compact_every_batches == 0`), and that block unconditionally
pops `TGMS_CRASH_POINT` before calling `compact()`/`gc()` again (so a real
maintenance cycle never trips a stale armed crash point). Net effect: once
the orchestrator's `rng.choice(ALL_BOUNDARIES)` drew `"gc_mid_delete"`
(1 in 10 per restart), that life ran unrestarted until the *next*
`--restart-every` boundary happened to draw something else — confirmed live
on soak3, where `writer_control.json` sat at
`{"seq": 7, "boundary": "gc_mid_delete"}` for 6+ hours with no restart, and
life 4 ran a full 12h (double its 6h period) for the same reason.

This test drives `child_writer` directly (the same `_child writer` subprocess
protocol `_spawn` uses), pre-arming the control file with `gc_mid_delete`
before the writer's first batch, so the writer must honor it within one
batch — deterministically, on a tiny store, with no dependency on real
compaction timing or store size.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LR_PATH = ROOT / "scripts" / "longevity_run.py"
sys.path.insert(0, str(ROOT / "scripts"))


def _build_tiny_store(path: Path) -> None:
    import tgms

    store = tgms.open(path, backend="native")
    n = 3000
    events = [{"src": f"n{i}", "dst": f"n{(i + 1) % n}", "rel_type": "R",
              "vt_s": i} for i in range(n)]
    store.ingest_events(events)
    store.close()


def _run_armed_writer(tmp_path: Path, boundary: str,
                      compact_every_batches: int = 100_000) -> subprocess.CompletedProcess:
    """Spawn a writer child (the exact `_child writer` protocol `_spawn`
    uses) against a fresh tiny store, with `boundary` already armed in
    `writer_control.json` before the writer ever starts — so its very first
    control-file check (top of the loop, before batch 1) must act on it.

    `compact_every_batches` defaults to something the writer will never
    reach in this test's short `end_at` window, so the periodic
    real-maintenance compact()/gc() cycle (which pops any armed
    TGMS_CRASH_POINT — see the module docstring) cannot mask a writer that
    fails to honor the control file on its own.
    """
    store_dir = tmp_path / "tiny-store"
    _build_tiny_store(store_dir)

    out_dir = tmp_path / "run-out"
    out_dir.mkdir()
    control_path = out_dir / "writer_control.json"
    control_path.write_text(json.dumps({"seq": 1, "boundary": boundary}))

    cfg = {
        "store": str(store_dir), "mix": "balanced", "seed": 0, "artifacts": 2,
        "compact_every_batches": compact_every_batches,
        "compact_min_interval_s": 0.0,
        "start_n": 3000,
        # A generous deadline: what's under test is that the writer dies
        # almost immediately (within one batch), not that it survives to
        # end_at. A buggy writer that never honors the control file runs
        # all the way to end_at and exits 0 instead.
        "end_at": time.time() + 20.0,
        "control_path": str(control_path),
        "progress_path": str(out_dir / "writer_progress-0.json"),
        "compactions_path": str(out_dir / "compactions.jsonl"),
        "metrics_path": str(out_dir / "metrics.jsonl"),
        "report_every_s": 60, "writer_sleep_s": 0.0, "life_index": 0,
    }

    env = __import__("os").environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(LR_PATH), "_child", "writer"],
        input=json.dumps(cfg).encode("utf-8"),
        capture_output=True, env=env, timeout=25)
    proc.elapsed_s = time.perf_counter() - t0  # type: ignore[attr-defined]
    return proc


def test_armed_gc_mid_delete_kills_writer_within_one_batch(tmp_path: Path) -> None:
    proc = _run_armed_writer(tmp_path, "gc_mid_delete")
    stderr = proc.stderr.decode("utf-8", "replace")

    # "gc_mid_delete" is an ENGINE-side crash point (gc.rs::crash_point ->
    # std::process::abort()), so Python sees SIGABRT (-6), same as the
    # other ENGINE_BOUNDARIES/MAINT_BOUNDARIES — only the PY_BOUNDARIES
    # (tgms/storage/crashpoint.py) exit via os._exit(137). See the
    # orchestrator's own comment on this distinction (cmd_run, around the
    # `designed = armed and wrc in (137, -6)` check).
    assert proc.returncode == -6, (
        f"an armed 'gc_mid_delete' restart must kill the writer via "
        f"the engine's std::process::abort() (rc=-6) — got "
        f"returncode={proc.returncode}, stderr:\n{stderr}")
    assert "TGMS_CRASH_POINT hit: gc_mid_delete" in stderr, (
        f"expected the crash_point() hit line for 'gc_mid_delete' in "
        f"stderr, got:\n{stderr}")
    # "within one batch": the writer must not wait anywhere near its 20s
    # end_at deadline, and must not need a real periodic compaction (which
    # this test's compact_every_batches=100_000 makes unreachable) either.
    assert proc.elapsed_s < 10.0, (
        f"writer took {proc.elapsed_s:.1f}s to honor an armed 'gc_mid_delete' "
        f"restart — should die within its first batch, not wait for a "
        f"periodic maintenance cycle or the end_at deadline")


def test_armed_compact_before_install_still_kills_writer(tmp_path: Path) -> None:
    """Sibling boundary, unchanged behavior — guards the fix against
    swapping the two MAINT_BOUNDARIES branches instead of completing them."""
    proc = _run_armed_writer(tmp_path, "compact_before_install")
    stderr = proc.stderr.decode("utf-8", "replace")

    assert proc.returncode == -6, (
        f"an armed 'compact_before_install' restart must kill the writer "
        f"via the engine's std::process::abort() (rc=-6) — got "
        f"returncode={proc.returncode}, stderr:\n{stderr}")
    assert "TGMS_CRASH_POINT hit: compact_before_install" in stderr, (
        f"expected the crash_point() hit line for 'compact_before_install' "
        f"in stderr, got:\n{stderr}")
    assert proc.elapsed_s < 10.0
