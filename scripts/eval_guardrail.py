"""The cost guardrail scored as a classifier (D-086; plan docs/eval_guardrail.md).

Every call in a grid runs twice, conceptually: once through the operator's
`cost_fn` to record the estimate the guardrail would act on, and once to
ground truth with the check skipped, timed. Cells whose estimate exceeds the
default ceilings by a wide margin — the calls the guardrail exists to refuse
— run in a child process under a hard wall cap, since the whole premise is
that we do not trust them to come back. The frontier is computed afterward
by sweeping a scalar multiplier over the default ceiling vector: at each
(budget, multiplier), a cell is a false admission if admitted but slower
than the budget, a false rejection if refused but fast enough.

    python scripts/eval_guardrail.py --stores 200k-d05,200k-d20 --json out.json
    python scripts/eval_guardrail.py --stores 200k-d05,1m-d05,collegemsg --json out.json
    python scripts/eval_guardrail.py --child <store> <op> <argsjson>   # internal
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval_harness as H  # noqa: E402
from eval_bitemporal import build_log  # noqa: E402

import tgms  # noqa: E402
from tgms.temporal.algebra import (  # noqa: E402
    REGISTRY, _fill_defaults, call_operator, ensure_all_registered,
)
from tgms.temporal.guardrails import add_time_estimate  # noqa: E402

WINDOW_FRACS = [0.01, 0.1, 0.5, 1.0]
#: operator shapes under test: scan, interval-join, motif (expansion),
#: reachability, point read — the five distinct cost models in the registry
OPS = ["series.count", "coactive.narrow", "motif.filtered", "reach.window",
       "hist.single"]
CHILD_CAP_S = 60.0
#: estimates this many times over the default ceiling run in a capped child
CHILD_MARGIN = 2.0
WARMUPS, REPS = 2, 3

STORES = {
    "200k-d05": ("synth", 200_000, 0.5),
    "200k-d20": ("synth", 200_000, 20.0),
    "1m-d05": ("synth", 1_000_000, 0.5),
    "1m-d20": ("synth", 1_000_000, 20.0),
    "collegemsg": ("frozen", 0, 0.0),
}
FROZEN = Path(__file__).resolve().parents[1] / \
    "benchmarks/frozen-v1/collegemsg.eventlog.jsonl"


def build_store(key: str) -> tuple[Path, Any]:
    kind, scale, dens = STORES[key]
    if kind == "synth":
        data, _ = build_log(scale, dens)
        log = Path(data.log)
    else:
        log = FROZEN
    path = Path(tempfile.mkdtemp(prefix=f"tgms-guard-{key}-")) / "store"
    H.load_store(path, "native", log)
    return path, tgms.open(path, backend="native")


def grid_cells(store, key: str) -> list[dict[str, Any]]:
    """The query grid for one store, windows scaled to its real extent."""
    adapter = store.adapter
    stats = adapter.stats()
    t0, t1 = stats["vt_min"], stats["vt_max"]
    span = max(1, t1 - t0)
    # filter uids must exist in THIS store — CollegeMsg's identities are
    # not the synthetic n0..n39 (the first record run died on exactly that)
    uids = sorted({v.uid for v in adapter.all_node_versions()})[:40]
    qs = {q.id: q for q in H.registry(t0, t1, tt_epoch1=t0 + 1,
                                       filter_uids=tuple(uids))}
    cells = []
    # the would-be-refused class: motifs with no node filter, the E_COST
    # demo shape — without it the frontier has no positive class at the
    # default ceilings
    unfiltered = json.loads(json.dumps(qs["motif.filtered"].args))
    unfiltered.pop("node_filter", None)
    for frac in WINDOW_FRACS:
        a = json.loads(json.dumps(unfiltered))
        if "window" in a:
            a["window"] = {"t_a": t0, "t_b": t0 + max(1, int(span * frac))}
        cells.append({"store": key, "qid": "motif.unfiltered",
                      "op": qs["motif.filtered"].op, "frac": frac, "args": a})
    for op_qid in OPS:
        q = qs[op_qid]
        for frac in WINDOW_FRACS:
            args = json.loads(json.dumps(q.args))  # deep copy
            if "window" in args:
                args["window"] = {"t_a": t0, "t_b": t0 + max(1, int(span * frac))}
            elif frac != 1.0:
                continue  # no window axis (point reads, interval joins)
            cells.append({"store": key, "qid": op_qid, "op": q.op,
                          "frac": frac, "args": args})
    return cells


def measure_cell(store_path: Path, store, cell: dict[str, Any]) -> dict[str, Any]:
    from tgms.temporal.guardrails import DEFAULT_CEILINGS

    adapter = store.adapter
    spec = REGISTRY[cell["op"]]
    filled = _fill_defaults(spec.args_schema, cell["args"])
    # exactly what the production guardrail sees: the cost_fn output with
    # the time estimate attached (call_operator's pipeline; the first d087
    # receipt missed this and carried time only for the motif op, which
    # embeds its own — every reach/scan cell read time_est_ms = 0)
    est = spec.cost_fn(filled, adapter.stats()) if spec.cost_fn else {}
    est = add_time_estimate(cell["op"], est)
    cell = {**cell, "estimate": est}

    dangerous = any(est.get(k, 0) > CHILD_MARGIN * v
                    for k, v in DEFAULT_CEILINGS.items())
    if dangerous:
        proc = subprocess.run(
            [sys.executable, __file__, "--child", str(store_path),
             cell["op"], json.dumps(cell["args"])],
            capture_output=True, text=True, timeout=CHILD_CAP_S + 30)
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
            cell["actual_ms"] = out["wall_ms"]
            cell["timed_out"] = out.get("timed_out", False)
        except Exception:  # noqa: BLE001 — child died at the cap
            cell["actual_ms"] = CHILD_CAP_S * 1000
            cell["timed_out"] = True
        cell["mode"] = "child"
        return cell

    for _ in range(WARMUPS):
        call_operator(adapter, cell["op"], dict(cell["args"]),
                      skip_cost_check=True)
    times = []
    for _ in range(REPS):
        t = time.perf_counter()
        call_operator(adapter, cell["op"], dict(cell["args"]),
                      skip_cost_check=True)
        times.append((time.perf_counter() - t) * 1000)
    cell["actual_ms"] = round(statistics.median(times), 3)
    cell["timed_out"] = False
    cell["mode"] = "inproc"
    return cell


def child(store_path: str, op: str, args_json: str) -> int:
    """One capped ground-truth execution of a would-be-refused call."""
    import signal

    def bail(*_):
        print(json.dumps({"wall_ms": CHILD_CAP_S * 1000, "timed_out": True}))
        import os
        os._exit(0)

    signal.signal(signal.SIGALRM, bail)
    signal.alarm(int(CHILD_CAP_S))
    store = tgms.open(store_path, backend="native")
    t = time.perf_counter()
    call_operator(store.adapter, op, json.loads(args_json),
                  skip_cost_check=True)
    print(json.dumps({"wall_ms": round((time.perf_counter() - t) * 1000, 1),
                      "timed_out": False}))
    return 0


def frontier(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Sweep a scalar over the default ceilings; classify per budget."""
    from tgms.temporal.guardrails import DEFAULT_CEILINGS

    budgets_ms = [500, 2_000, 10_000]
    multipliers = [2**k for k in range(-8, 9)]
    out: dict[str, Any] = {}
    for b in budgets_ms:
        rows = []
        for m in multipliers:
            fa = fr = 0
            for c in cells:
                refused = any(c["estimate"].get(k, 0) > m * v
                              for k, v in DEFAULT_CEILINGS.items())
                slow = c["actual_ms"] > b
                if refused and not slow:
                    fr += 1
                if not refused and slow:
                    fa += 1
            rows.append({"multiplier": m, "false_admissions": fa,
                         "false_rejections": fr})
        default = next(r for r in rows if r["multiplier"] == 1)
        best = min(rows, key=lambda r: r["false_admissions"] * 10
                   + r["false_rejections"])  # admissions cost more
        out[f"budget_{b}ms"] = {"sweep": rows, "at_default": default,
                                "best": best, "n_cells": len(cells)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", nargs=3, metavar=("STORE", "OP", "ARGS"))
    ap.add_argument("--stores", default="200k-d05,200k-d20")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    ensure_all_registered()
    if args.child:
        return child(*args.child)

    all_cells: list[dict[str, Any]] = []
    for key in args.stores.split(","):
        print(f"store {key}: building …", flush=True)
        store_path, store = build_store(key)
        cells = grid_cells(store, key)
        for c in cells:
            r = measure_cell(store_path, store, c)
            all_cells.append(r)
            if args.json:  # incremental: a late crash keeps earlier stores
                args.json.write_text(json.dumps({"cells": all_cells,
                                                 "partial": True}, indent=1))
            e = r["estimate"]
            print(f"  {key} {r['qid']:>15} frac={r['frac']:<5} "
                  f"est_rows={e.get('rows_scanned_est', 0):>12,} "
                  f"est_exp={e.get('expansions_est', 0):>12,} "
                  f"actual={r['actual_ms']:>10.1f} ms "
                  f"{'TIMEOUT' if r['timed_out'] else ''}", flush=True)
        store.close()

    fr = frontier(all_cells)
    for b, row in fr.items():
        print(f"{b}: default FA={row['at_default']['false_admissions']} "
              f"FR={row['at_default']['false_rejections']} of {row['n_cells']} | "
              f"best m={row['best']['multiplier']} "
              f"FA={row['best']['false_admissions']} "
              f"FR={row['best']['false_rejections']}")
    if args.json:
        # compute-node images may lack git (it cost one probe run its
        # receipt); prefer the COMMIT env a job passes via sbatch --export
        commit = os.environ.get("COMMIT", "")
        if not commit:
            try:
                commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"], capture_output=True,
                    text=True).stdout.strip()
            except OSError:
                commit = "unknown"
        args.json.write_text(json.dumps(
            {"cells": all_cells, "frontier": fr,
             "manifest": {"commit": commit,
                          "host": platform.node(),
                          "child_cap_s": CHILD_CAP_S}}, indent=1) + "\n")
        print(f"record → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
