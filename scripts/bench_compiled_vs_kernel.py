"""E14 P2 — the compositional route against the kernel (§C2).

**The claim under test.** On the operators that have both a hand-written kernel
and a compiled core expansion, the compiled plan is within **3x** of the kernel
at 1M and 10M.

**The population is two, and the paper says two.** `COMPILED` holds exactly
`entity_history` and `version_history` (`tgms/tgir/compiled/__init__.py`); M3.3
was cut to those by coordinator ruling and `snapshot_subgraph`,
`diff_snapshots` and the `aggregate_events` fragment stay opaque leaves
permanently, each for a recorded reason (`tgms/tgir/rollout.py`). Both compiled
entries sit at `COMPILE_MODE = "leaf"`: the compiled path exists to prove the
core is expressive enough, not as a shipping route. Reporting this as though it
covered fifteen operators would be the overclaim the whole evidence campaign is
built to avoid.

**What it buys the argument.** It is the empirical half of the answer to "why
not annotate an existing graph algebra with temporal scope?" — the compositional
route computes the same answers at a comparable price where both routes exist.
The theoretical half (the pinned-stability invariant, the completeness lattice
and its propagation rules — properties that must hold *per operator* and so
cannot be a wrapper) stays the primary answer.

3x is a bound the claim can survive, not a number we expect to hit: the
compiled form materialises a relation the kernel streams. A measured 1.2x is a
better sentence than a bound tuned after the fact, and a ratio worse than 3x is
reported as-is with what it costs.

    uv run python scripts/bench_compiled_vs_kernel.py --store stores/synth-1m-native \
        --out benchmarks/results-v1/e14-p2-compiled-1m.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WARMUPS = 5
REPS_FAST = 30
REPS_SLOW = 10
FAST_THRESHOLD_MS = 1000.0
CALL_CEILING_S = 600

#: The claim's bound (§C2).
BAND = 3.0


def cases(stats: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Arguments for the two operators that have both routes."""
    lo, hi = int(stats["vt_min"]), int(stats["vt_max"])
    return {
        "entity_history": {"uid": "n1", "limit": 50},
        "version_history": {"kind": "edge", "window": {"t_a": lo, "t_b": hi},
                            "limit": 50},
    }


def measure(store_path: str, op: str, arm: str) -> dict[str, Any]:
    """Child: time one arm of one operator in this process.

    The two arms never share an interpreter, so the kernel cannot warm the
    compiled path's segment cache or the reverse (`engine_lessons.md` §9g).
    """
    import tgms
    from tgms.core.errors import TgmsError
    from tgms.temporal.algebra import (
        call_operator, ensure_all_registered, validate_args,
    )
    from tgms.tgir.compiled import COMPILED

    ensure_all_registered()
    store = tgms.open(store_path, read_only=True)
    try:
        args = cases(store.stats())[op]

        if arm == "compiled":
            # The compiled form takes **filled** arguments — R5 resolves every
            # default at bind time, so `as_of_tt` and friends must already be
            # present. The kernel fills them itself. `tgir_equiv.py` draws the
            # same distinction; timing the two arms has to respect it or the
            # compiled arm measures an exception.
            filled = validate_args(op, dict(args))

            def go() -> Any:
                return COMPILED[op](store.adapter, dict(filled))
        else:
            def go() -> Any:
                return call_operator(store.adapter, op, dict(args))

        try:
            t = time.time()
            first = go()
            first_ms = (time.time() - t) * 1000.0
        except TgmsError as e:
            return {"op": op, "arm": arm, "outcome": "REFUSED_OR_ERRORED",
                    "code": e.to_payload().get("code")}

        rows = first.get("rows") if isinstance(first, dict) else None
        reps = REPS_FAST if first_ms < FAST_THRESHOLD_MS else REPS_SLOW
        for _ in range(WARMUPS - 1):
            go()
        times = []
        for _ in range(reps):
            t = time.time()
            go()
            times.append((time.time() - t) * 1000.0)
    finally:
        store.close()

    times.sort()
    return {"op": op, "arm": arm, "outcome": "OK", "reps": len(times),
            "rows": len(rows) if rows is not None else None,
            "p50_ms": times[len(times) // 2],
            "p95_ms": times[min(len(times) - 1, int(len(times) * 0.95))],
            "min_ms": times[0], "max_ms": times[-1]}


def run_child(store_path: str, op: str, arm: str) -> dict[str, Any]:
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()),
           "--single", op, "--arm", arm, "--store", store_path,
           "--out", os.devnull]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=CALL_CEILING_S, cwd=ROOT,
                              env={**os.environ, "PYTHONPATH": str(ROOT)})
    except subprocess.TimeoutExpired:
        return {"op": op, "arm": arm, "outcome": "TIMEOUT"}
    for line in reversed(done.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {"op": op, "arm": arm, "outcome": "ERRORED",
            "error": (done.stderr or done.stdout)[-300:]}


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--single", default="")
    ap.add_argument("--arm", default="kernel", choices=("kernel", "compiled"))
    args = ap.parse_args()

    if args.single:
        print(json.dumps(measure(args.store, args.single, args.arm), default=str))
        return 0

    import tgms

    sha = _sha()
    store = tgms.open(args.store, read_only=True)
    stats = store.stats()
    ops = list(cases(stats))
    store.close()

    print(f"RUN_STARTED commit={sha} store={args.store} ops={len(ops)} "
          f"host={platform.node()}", flush=True)
    t0 = time.time()
    rows = []
    for op in ops:
        k = run_child(args.store, op, "kernel")
        c = run_child(args.store, op, "compiled")
        ratio = None
        if k.get("outcome") == "OK" and c.get("outcome") == "OK" and k["p50_ms"]:
            ratio = c["p50_ms"] / k["p50_ms"]
        agree = (k.get("rows") == c.get("rows")
                 if k.get("rows") is not None and c.get("rows") is not None
                 else None)
        rows.append({"op": op, "kernel": k, "compiled": c,
                     "compiled_over_kernel": ratio, "row_counts_agree": agree})
        flag = "" if ratio is None else (
            "  within-band" if ratio <= BAND else f"  OVER {BAND:g}x")
        print(f"  {op:20s} kernel {k.get('p50_ms', k.get('outcome')):>10} "
              f"compiled {c.get('p50_ms', c.get('outcome')):>10}  "
              f"{'—' if ratio is None else f'{ratio:.3f}x'}{flag}"
              f"  rows {k.get('rows')}/{c.get('rows')}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "manifest": {
            "commit": sha, "host": platform.node(),
            "platform": platform.platform(), "store": args.store,
            "store_stats": stats, "backend": "native",
            "population": ("the two operators in COMPILED; the other thirteen "
                           "have no compiled form and are excluded, not failed"),
            "band": f"compiled/kernel <= {BAND:g}",
            "protocol": (f"warmups {WARMUPS}, reps {REPS_FAST} under "
                         f"{FAST_THRESHOLD_MS:.0f} ms else {REPS_SLOW}; "
                         f"median and p95; one arm per process"),
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "wall_s": round(time.time() - t0, 1),
        },
        "rows": rows,
    }, indent=1, sort_keys=True, default=str))

    over = [r for r in rows if r["compiled_over_kernel"] and
            r["compiled_over_kernel"] > BAND]
    print(f"\nover the {BAND:g}x band: {len(over)}")
    print(f"record: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
