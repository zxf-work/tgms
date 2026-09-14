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
