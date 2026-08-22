"""Calibrate the unbounded `Expand`'s time coefficient — M3.1's phase gate.

`TGIR_SPEC.md` §2.3 names this number as the least-calibrated one in the
guardrail and makes its calibration "an M3 obligation, not an assumption", and
the M3 plan's §7.4 says why the assumption is dangerous in both directions: too
high refuses rows the forecast predicts unlocked, too low admits a fixpoint that
runs until the backstop fires or the process dies. Neither failure is visible
without a measurement.

**The two shapes §7.3 fixes**, measured on a real store:

1. **from a small bound anchor set** — ten seeds, the shape a cohort-anchored
   query has;
2. **hoisted over a whole population** — every node as a seed, the shape the
   forecast's IS2 discussion warns about.

The output is a receipt under `docs/tgir/calib/`, carrying the git SHA, the
store digest, the machine, the config and the case count (process rule 1.6), and
one coefficient to write into `TIME_COEFF_MS_PER_M`. Calibration is **measured
and recorded, never asserted**.

    uv run python scripts/calibrate_expand.py [--store PATH] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

from tgms.storage.eventlog import replay
from tgms.tgir.eval import Execution
from tgms.tgir.eval.adjacency import AdjacencyCache
from tgms.tgir.node import Expand, NodeScan, Unbounded
from tgms.tgir.prune import live_columns

ROOT = Path(__file__).resolve().parents[1]
FROZEN_LOG = ROOT / "benchmarks/frozen-v1/collegemsg.eventlog.jsonl"
REPEATS = 3


class Counter:
    """A `Budget`-shaped probe that counts instead of refusing — the same
    `charge()` the evaluator calls, so the measured unit is exactly the unit
    the cost model estimates."""

    def __init__(self) -> None:
        self.spent = 0

    def charge(self, expansions: int) -> None:
        self.spent += int(expansions)


def build(log: Path) -> Any:
    from tgms.storage.native import NativeAdapter

    adapter = NativeAdapter(Path(tempfile.mkdtemp()) / "store")
    replay(log, adapter)
    return adapter


def measure(adapter: Any, seeds: tuple[str, ...], label: str) -> dict[str, Any]:
    node = Expand(NodeScan("p", uids=seeds), "p", "q", Unbounded(1), dir="both")
    live = live_columns(node)
    times, expansions, rows = [], 0, 0
    for _ in range(REPEATS):
        counter = Counter()
        run = Execution(adapter, live, budget=counter)
        # a fresh adjacency per repeat, so the build is inside the measurement:
        # the estimate prices the whole node, not only its traversal
        run.adjacency = AdjacencyCache(adapter)
        t0 = time.perf_counter()
        out = run.run(node)
        times.append((time.perf_counter() - t0) * 1000.0)
        expansions, rows = counter.spent, out.n
    median = statistics.median(times)
    return {"shape": label, "seeds": len(seeds), "rows": rows,
            "expansions": expansions, "median_ms": round(median, 3),
            "ms_per_million": round(median / max(expansions, 1) * 1_000_000, 1),
            "samples": [round(t, 3) for t in times]}


def git_sha() -> str:
    """Read the checked-out SHA from `.git` directly — a receipt records what
    it was measured against."""
    head = ROOT / ".git" / "HEAD"
    if not head.exists():
        return "unknown"
    ref = head.read_text().strip()
    if ref.startswith("ref: "):
        target = ROOT / ".git" / ref[5:]
        return target.read_text().strip()[:12] if target.exists() else "unknown"
    return ref[:12]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=str(FROZEN_LOG))
    ap.add_argument("--out", default=str(ROOT / "docs/tgir/calib"))
    args = ap.parse_args()

    log = Path(args.store)
    if not log.exists():
        print(f"skip: {log} is not present (frozen corpus not checked out)")
        return 0

    adapter = build(log)
    stats = adapter.stats()
    all_uids = adapter.uids_for(list(range(adapter.num_entities())))

    results = [
        measure(adapter, tuple(all_uids[:10]), "ten bound anchors"),
        measure(adapter, tuple(all_uids), "hoisted over the whole population"),
    ]
    coefficient = max(r["ms_per_million"] for r in results)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"expand-unbounded-{date.today().isoformat()}.md"
    path.write_text(_receipt(results, coefficient, stats, adapter, log))
    print(json.dumps(results, indent=1))
    print(f"\ncoefficient (ms per million expansions): {coefficient}")
    print(f"receipt: {path.relative_to(ROOT)}")
    adapter.close()
    return 0


def _receipt(results: list[dict[str, Any]], coefficient: float,
             stats: dict[str, Any], adapter: Any, log: Path) -> str:
    rows = "\n".join(
        f"| {r['shape']} | {r['seeds']} | {r['rows']} | {r['expansions']:,} | "
        f"{r['median_ms']} | {r['ms_per_million']} |" for r in results)
    return f"""# Calibration — unbounded `Expand`

**Measured {date.today().isoformat()}.** `TGIR_SPEC.md` §2.3 makes this an M3
obligation rather than an assumption: the unbounded form's `time_est_ms` is the
least-calibrated number in the guardrail, and a wrong value is invisible in both
directions — too high refuses rows the forecast predicts unlocked, too low
admits a fixpoint that runs until the runtime backstop fires.

## Provenance

| | |
|---|---|
| git SHA | `{git_sha()}` |
| store | `{log.name}`, rebuilt by **replay** (never ingest) |
| store digest | `{adapter.store_digest()[:16]}` |
| backend | native |
| entities / edge versions | {stats.get('n_entities'):,} / {stats.get('n_edge_versions'):,} |
| machine | {platform.platform()} |
| repeats per shape | {REPEATS} (median reported) |
| unit | one *expansion* = one neighbour visited, the same unit `cost.py` estimates |

## The two shapes §7.3 fixes

| shape | seeds | rows out | expansions | median ms | ms/M |
|---|---|---|---|---|---|
{rows}

## Coefficient

```
TIME_COEFF_MS_PER_M["tgir_expand_unbounded"] = {coefficient}
```

The **larger** of the two shapes is taken, so the estimate over-refuses rather
than over-admits on the shape it was not measured against — which is §2.13's own
instruction for an uncalibrated operator, applied to a calibrated one at its
worst measured shape.

**What this does not establish.** One store, one machine, one backend. The
reachability coefficient is a super-linear fixpoint and
`EVIDENCE_MODEL` §9 records that it does not transfer across scale; this receipt
fixes the admission arithmetic at *this* scale and is the number the freeze's
secondary admission axis is reported against. A deployment on slower hardware
scales it with `TGMS_TIME_COEFF_SCALE`, exactly as the operator coefficients
scale.
"""


if __name__ == "__main__":
    sys.exit(main())
