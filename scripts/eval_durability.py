"""Durability under injected crashes (D-086; plan docs/eval_durability.md).

A parent process runs one trial per (boundary × repetition): a child applies
acknowledged writes, records each acknowledgment to a sidecar file the
moment the write call returns, then attempts one more write that dies at the
named boundary — every boundary, engine and Python alike, fires through the
same `TGMS_CRASH_POINT` env var (a real `abort()`/`os._exit(137)` mid-commit,
no unwinding): engine boundaries at `crash_point(name)` call sites in the
native store, Python boundaries at `crash_point(name)` call sites in
`tgms/storage/eventlog.py` and `tgms/store.py` (D-086). The parent then
reopens the store, letting ordinary recovery run, and answers the four
questions by machine:

  Q1 every acknowledged write is present (returned-success implies present;
     the in-flight batch is all-or-nothing);
  Q2 recovery is deterministic: recovered digest == clean replay of the
     recovered store's own event log into a fresh store;
  Q3 single-generation visibility: verify() clean, generation is previous
     or next, never a blend;
  Q4 orphan reclamation: compact()+gc() leave verify() clean and no *.tmp.

    python scripts/eval_durability.py --json out.json
    python scripts/eval_durability.py --seed 7 --trials 20 --json out.json
    python scripts/eval_durability.py --child <store> <boundary> <acked>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENGINE_BOUNDARIES = [
    "after_seal", "after_close_runs", "after_dict",
    "after_manifest", "after_current",
]
MAINT_BOUNDARIES = ["compact_before_install", "gc_mid_delete"]
#: Product-side hooks (`crash_point` calls in tgms/storage/eventlog.py and
#: tgms/store.py) — same env-var protocol as the engine's, no monkeypatching.
PY_BOUNDARIES = ["py_torn_wal_append", "py_after_wal_fsync",
                 "py_before_engine_commit"]
ALL = PY_BOUNDARIES + ENGINE_BOUNDARIES + MAINT_BOUNDARIES
#: Boundaries selected purely by setting TGMS_CRASH_POINT before one more
#: write; MAINT_BOUNDARIES additionally need a compact()/gc() call instead.
CRASH_WRITE_BOUNDARIES = PY_BOUNDARIES + ENGINE_BOUNDARIES

SEED = 6          # acknowledged plain writes
ACKED_CORR = 4    # acknowledged corrections (so close runs exist)

#: --seed workload knobs (D-086 P0.7): per-trial randomization once a seed is
#: given: number of write "batches", each batch's op count, and the fraction
#: of ops that mutate an existing entity (correct/retract) rather than
#: asserting a new one.
RANDOM_BATCHES_RANGE = (1, 8)
RANDOM_BATCH_SIZE_RANGE = (1, 50)
CORRECTION_DENSITIES = (0.0, 0.05, 0.2, 0.5)


def derive_trial_seed(seed: int, boundary: str, trial: int) -> int:
    """A deterministic per-(seed, boundary, trial) seed, stable across runs
    and interpreters — `hash()` on strings is salted per-process, so it
    cannot be what "same --seed reproduces the same workload" means."""
    digest = hashlib.sha256(f"{seed}:{boundary}:{trial}".encode()).hexdigest()
    return int(digest[:16], 16)


def _random_workload(rng: "random.Random") -> list[dict[str, Any]]:
    """A random sequence of acknowledged write ops for the seed/correction
    phase: a mix of `assert_node`/`assert_edge`/`correct`/`retract` over a
    growing pool of entities. `a0` is never touched here — every boundary
    reserves it for the crash write, so Q1's in-flight-batch check keeps its
    simple shape regardless of how the surrounding workload is randomized.
    """
    n_batches = rng.randint(*RANDOM_BATCHES_RANGE)
    density = rng.choice(CORRECTION_DENSITIES)
    live_nodes: list[str] = []
    live_edges: list[tuple[str, str, str, str]] = []
    ops: list[dict[str, Any]] = []
    n_entities = 0
    for _ in range(n_batches):
        batch_size = rng.randint(*RANDOM_BATCH_SIZE_RANGE)
        for _ in range(batch_size):
            mutate = (live_nodes or live_edges) and rng.random() < density
            if mutate:
                target_node = live_nodes and (not live_edges or rng.random() < 0.5)
                if target_node:
                    uid = rng.choice(live_nodes)
                    call = rng.choice(["correct_node", "retract_node"])
                    ops.append({"call": call, "uid": uid})
                    if call == "retract_node":
                        live_nodes.remove(uid)
                else:
                    edge = rng.choice(live_edges)
                    call = rng.choice(["correct_edge", "retract_edge"])
                    ops.append({"call": call, "edge": edge})
                    if call == "retract_edge":
                        live_edges.remove(edge)
            else:
                n_entities += 1
                if rng.random() < 0.5:
                    uid = f"r{n_entities}"
                    ops.append({"call": "assert_node", "uid": uid})
                    live_nodes.append(uid)
                else:
                    edge = (f"r{n_entities}s", f"r{n_entities}d", "R", "")
                    ops.append({"call": "assert_edge", "edge": edge})
                    live_edges.append(edge)
    return ops


def _apply_workload_op(store: Any, acked: Any, op: dict[str, Any],
                       rng: "random.Random") -> None:
    """Execute one `_random_workload` op and record what was acknowledged.

    The sidecar line is the ground truth the parent checks against later:
    `{"kind": "node"|"edge", "key": ..., "i": <int, or null for a retract>}`
    — recorded from the call the child just made, not re-derived from the
    store's temporal semantics, so the parent needs no copy of them either.
    """
    from tgms.core.model import EntityRef

    call = op["call"]
    if call == "assert_node":
        i = rng.randint(0, 1_000_000)
        store.assert_node(op["uid"], "N", {"i": i}, vt_s=0, vt_e=1_000_000)
        acked.write(json.dumps({"kind": "node", "key": op["uid"], "i": i}) + "\n")
    elif call == "correct_node":
        i = rng.randint(0, 1_000_000)
        store.correct(EntityRef(kind="node", uid=op["uid"]), {"i": i},
                      vt_s=0, vt_e=1_000_000)
        acked.write(json.dumps({"kind": "node", "key": op["uid"], "i": i}) + "\n")
    elif call == "retract_node":
        store.retract(EntityRef(kind="node", uid=op["uid"]), t=0)
        acked.write(json.dumps({"kind": "node", "key": op["uid"], "i": None}) + "\n")
    elif call == "assert_edge":
        src, dst, rel, disc = op["edge"]
        i = rng.randint(0, 1_000_000)
        store.assert_edge(src, dst, rel, {"i": i}, vt_s=0, vt_e=1_000_000, disc=disc)
        acked.write(json.dumps({"kind": "edge", "key": list(op["edge"]), "i": i}) + "\n")
    elif call == "correct_edge":
        src, dst, rel, disc = op["edge"]
        i = rng.randint(0, 1_000_000)
        store.correct(EntityRef(kind="edge", src=src, dst=dst, rel_type=rel, disc=disc),
                      {"i": i}, vt_s=0, vt_e=1_000_000)
        acked.write(json.dumps({"kind": "edge", "key": list(op["edge"]), "i": i}) + "\n")
    elif call == "retract_edge":
        src, dst, rel, disc = op["edge"]
        store.retract(EntityRef(kind="edge", src=src, dst=dst, rel_type=rel, disc=disc), t=0)
        acked.write(json.dumps({"kind": "edge", "key": list(op["edge"]), "i": None}) + "\n")
    else:  # pragma: no cover - _random_workload only emits the calls above
        raise ValueError(f"unknown workload op: {call}")


# --- child ----------------------------------------------------------------- #


def child(store_path: str, boundary: str, acked_path: str,
         trial_seed: int | None = None) -> int:
    import tgms

    acked = open(acked_path, "a", buffering=1)
    store = tgms.open(store_path, backend="native")

    def write(uid: str, i: int) -> None:
        store.assert_node(uid, "N", {"i": i}, vt_s=0, vt_e=1_000_000)
        acked.write(json.dumps({"kind": "node", "key": uid, "i": i}) + "\n")

    if trial_seed is None:
        for i in range(SEED):
            write(f"a{i}", i)
        for i in range(ACKED_CORR):
            write(f"a{i}", 100 + i)   # corrections: close runs exist from here
    else:
        # a0 is reserved for the crash write in every mode: seed it and give
        # it one correction so most boundaries still fire with a close run
        # pending, then randomize everything else.
        rng = random.Random(trial_seed)
        write("a0", 0)
        write("a0", 100)
        for op in _random_workload(rng):
            _apply_workload_op(store, acked, op, rng)

    # the crash write is a correction too, so most boundaries carry a
    # pending close when they fire. Every boundary — engine or Python-side —
    # now fires the same way: set TGMS_CRASH_POINT and make one more write;
    # the crash_point() call sites in the engine and in tgms/storage/
    # eventlog.py + tgms/store.py do the rest (D-086).
    if boundary in CRASH_WRITE_BOUNDARIES:
        os.environ["TGMS_CRASH_POINT"] = boundary
        write("a0", 999)
        return 3                  # only after_current should reach here alive
    if boundary in MAINT_BOUNDARIES:
        if boundary == "gc_mid_delete":
            store.adapter.compact()   # clean compact so gc has victims
        os.environ["TGMS_CRASH_POINT"] = boundary
        if boundary == "compact_before_install":
            store.adapter.compact()
        else:
            store.adapter.gc(keep_last=0)
        return 3
    return 4  # an unknown boundary reaching here is a harness bug


# --- parent ---------------------------------------------------------------- #


def clean_replay_digest(log_path: Path) -> str:
    import tgms
    from tgms.storage.eventlog import replay

    with tempfile.TemporaryDirectory(prefix="tgms-dur-replay-") as tmp:
        s = tgms.open(Path(tmp) / "store", backend="native")
        replay(log_path, s.adapter)
        d = s.digest()
        s.close()
        return d


def run_trial(boundary: str, trial: int, seed: int | None = None) -> dict[str, Any]:
    import tgms
    from tgms.core.model import edge_eid

    trial_seed = derive_trial_seed(seed, boundary, trial) if seed is not None else None

    work = Path(tempfile.mkdtemp(prefix=f"tgms-dur-{boundary}-"))
    store_path, acked_path = work / "store", work / "acked.txt"
    acked_path.touch()

    t0 = time.perf_counter()
    cmd = [sys.executable, __file__, "--child", str(store_path), boundary,
           str(acked_path)]
    if trial_seed is not None:
        cmd += ["--trial-seed", str(trial_seed)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120,
        env={**os.environ, "TGMS_CRASH_POINT": ""},
    )
    rec: dict[str, Any] = {
        "boundary": boundary,
        "trial": trial,
        "seed": seed,
        "trial_seed": trial_seed,
        "workload_digest": hashlib.sha256(acked_path.read_bytes()).hexdigest()[:16],
        "child_exit": proc.returncode,
        "child_died": proc.returncode not in (0, 3),
    }
    if boundary != "after_current" and boundary not in MAINT_BOUNDARIES:
        rec["crash_confirmed"] = proc.returncode not in (0, 3, 4)
    acked: list[dict[str, Any]] = [
        json.loads(line) for line in acked_path.read_text().splitlines() if line.strip()
    ]

    problems: list[str] = []
    try:
        store = tgms.open(store_path, backend="native")
    except Exception as e:  # noqa: BLE001 — an unopenable store is the finding
        rec["q3_single_generation"] = False
        rec["q1_acked_survive"] = False
        rec["q2_deterministic"] = False
        rec["q4_orphans_reclaimed"] = False
        rec["problems"] = [f"reopen failed: {type(e).__name__}: {e}"]
        rec["notes"] = rec["problems"][0]
        rec["wall_s"] = round(time.perf_counter() - t0, 2)
        return rec

    # Q3 — verify clean
    v = store.adapter.verify()
    if v.get("problems"):
        problems.append(f"verify: {v['problems'][:3]}")
    rec["q3_single_generation"] = not v.get("problems")

    # Q1 — every acknowledged write present with its latest acknowledged value
    # (or, for a retract, absent). `latest` keeps the LAST sidecar line per
    # entity — a correction or retraction supersedes an earlier assert of the
    # same key, exactly like the store itself.
    latest: dict[tuple[str, Any], int | None] = {}
    for rec_ack in acked:
        key = (rec_ack["kind"],
              tuple(rec_ack["key"]) if isinstance(rec_ack["key"], list) else rec_ack["key"])
        latest[key] = rec_ack["i"]
    # Write-ahead means the crash batch's log record may be durable even
    # though the caller never saw success; suffix replay then correctly
    # resurrects it. So a0 may read 999 at every boundary whose crash lies
    # past the WAL append — that is Q1's "returned implies present", not a
    # violation of it. Only the torn-append boundary must NOT show 999.
    crash_may_survive = boundary != "py_torn_wal_append"
    q1 = True
    for (kind, key), want in latest.items():
        if kind == "node":
            got = store.adapter.believed_node_versions(key)
        else:
            src, dst, rel, disc = key
            got = store.adapter.believed_edge_versions(
                edge_eid(src, dst, rel, disc), src=src, dst=dst)
        vals_here = [g.props.get("i") for g in got]
        if kind == "node" and key == "a0" and crash_may_survive and vals_here == [999]:
            continue
        if want is None:  # a retracted entity: no believed version should remain
            if got:
                q1 = False
                problems.append(f"acked {kind} {key} retracted but believed={vals_here}")
            continue
        if len(got) != 1 or got[0].props.get("i") != want:
            q1 = False
            problems.append(
                f"acked {kind} {key}={want} but believed={vals_here}")
    # the in-flight write is all-or-nothing
    a0 = store.adapter.believed_node_versions("a0")
    vals = {g.props.get("i") for g in a0}
    if not vals <= {latest.get(("node", "a0")), 999}:
        q1 = False
        problems.append(f"in-flight batch left a blend: a0 -> {vals}")
    rec["q1_acked_survive"] = q1
    rec["crash_batch_visible"] = 999 in vals

    # Q2 — deterministic recovery
    d1 = store.digest()
    store.close()
    d2 = clean_replay_digest(store_path / "eventlog.jsonl")
    rec["q2_deterministic"] = d1 == d2
    if d1 != d2:
        problems.append("recovered digest != clean replay digest")

    # Q4 — orphan reclamation
    store = tgms.open(store_path, backend="native")
    try:
        store.adapter.compact()
        store.adapter.gc(keep_last=0)
        v = store.adapter.verify()
        tmps = list((store_path / "native").rglob("*.tmp"))
        rec["q4_orphans_reclaimed"] = not v.get("problems") and not tmps
        if v.get("problems"):
            problems.append(f"post-gc verify: {v['problems'][:2]}")
        if tmps:
            problems.append(f"tmp files survive gc: {[t.name for t in tmps]}")
    finally:
        store.close()

    rec["problems"] = problems
    rec["notes"] = ("; ".join(problems) if problems else
                    ("crash batch visible via write-ahead replay" if rec["crash_batch_visible"]
                     else "clean"))
    rec["wall_s"] = round(time.perf_counter() - t0, 2)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", nargs=3, metavar=("STORE", "BOUNDARY", "ACKED"))
    ap.add_argument("--trial-seed", type=int, default=None,
                    help="(--child only) per-trial seed for a randomized workload")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--boundaries", default=",".join(ALL))
    ap.add_argument("--seed", type=int, default=None,
                    help="randomize the per-trial workload (default: the fixed "
                         "legacy workload); each (boundary, trial) derives its "
                         "own seed from this one, recorded in every trial record")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    if args.child:
        return child(*args.child, trial_seed=args.trial_seed)

    results = []
    for b in args.boundaries.split(","):
        for t in range(args.trials):
            r = run_trial(b, t, args.seed)
            results.append(r)
            ok = all(r.get(k, False) for k in
                     ("q1_acked_survive", "q2_deterministic",
                      "q3_single_generation", "q4_orphans_reclaimed"))
            print(f"  {b:>24} trial {t}: "
                  f"{'OK' if ok else 'PROBLEM ' + '; '.join(r['problems'])[:120]}",
                  flush=True)

    bad = [r for r in results
           if not all(r.get(k, False) for k in
                      ("q1_acked_survive", "q2_deterministic",
                       "q3_single_generation", "q4_orphans_reclaimed"))]
    print(f"\n{len(results)} trials, {len(bad)} with problems")
    if args.json:
        args.json.write_text(json.dumps(
            {"results": results,
             "manifest": {
                 "commit": subprocess.run(
                     ["git", "rev-parse", "HEAD"], capture_output=True,
                     text=True).stdout.strip(),
                 "seed": args.seed,
             }}, indent=1) + "\n")
        print(f"record → {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
