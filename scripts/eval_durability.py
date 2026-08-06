"""Durability under injected crashes (D-086; plan docs/eval_durability.md).

A parent process runs one trial per (boundary × repetition): a child applies
acknowledged writes, records each acknowledgment to a sidecar file the
moment the write call returns, then attempts one more write that dies at the
named boundary — engine boundaries via `TGMS_CRASH_POINT` (a real `abort()`
mid-commit), Python boundaries via harness-local wrappers in the child. The
parent then reopens the store, letting ordinary recovery run, and answers
the four questions by machine:

  Q1 every acknowledged write is present (returned-success implies present;
     the in-flight batch is all-or-nothing);
  Q2 recovery is deterministic: recovered digest == clean replay of the
     recovered store's own event log into a fresh store;
  Q3 single-generation visibility: verify() clean, generation is previous
     or next, never a blend;
  Q4 orphan reclamation: compact()+gc() leave verify() clean and no *.tmp.

    python scripts/eval_durability.py --json out.json
    python scripts/eval_durability.py --child <store> <boundary> <acked>
"""

from __future__ import annotations

import argparse
import json
import os
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
PY_BOUNDARIES = ["torn_wal_append", "after_wal_fsync", "before_engine_commit"]
ALL = PY_BOUNDARIES + ENGINE_BOUNDARIES + MAINT_BOUNDARIES

SEED = 6          # acknowledged plain writes
ACKED_CORR = 4    # acknowledged corrections (so close runs exist)


# --- child ----------------------------------------------------------------- #


def child(store_path: str, boundary: str, acked_path: str) -> int:
    import tgms
    from tgms.storage.eventlog import EventLog

    acked = open(acked_path, "a", buffering=1)
    store = tgms.open(store_path, backend="native")

    def write(uid: str, i: int) -> None:
        store.assert_node(uid, "N", {"i": i}, vt_s=0, vt_e=1_000_000)
        acked.write(f"{uid} {i}\n")

    for i in range(SEED):
        write(f"a{i}", i)
    for i in range(ACKED_CORR):
        write(f"a{i}", 100 + i)   # corrections: close runs exist from here on

    # the crash write is a correction too, so most boundaries carry a
    # pending close when they fire
    if boundary in ENGINE_BOUNDARIES:
        os.environ["TGMS_CRASH_POINT"] = boundary
        write("a0", 999)          # aborts inside engine commit
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

    # python-side boundaries
    if boundary == "torn_wal_append":
        def torn(self, tt, ops):
            # replicate the exact record bytes without appending, then tear
            from tgms.core.model import canonical_json
            import hashlib
            record = canonical_json(
                {"batch_id": hashlib.sha256(
                    canonical_json({"tt": tt, "ops": ops}).encode()).hexdigest()[:16],
                 "tt": tt, "ops": ops})
            data = (record + "\n").encode()
            with open(self.path, "ab") as f:
                f.write(data[: max(1, len(data) // 2)])
                f.flush()
            os._exit(137)

        EventLog.append = torn  # type: ignore[method-assign]
        write("a0", 999)
    elif boundary == "after_wal_fsync":
        orig = EventLog.append

        def then_die(self, tt, ops):
            out = orig(self, tt, ops)
            os._exit(137)
            return out

        EventLog.append = then_die  # type: ignore[method-assign]
        write("a0", 999)
    elif boundary == "before_engine_commit":
        adapter = store.adapter

        def die(*a, **kw):
            os._exit(137)

        adapter.commit = die  # type: ignore[method-assign]
        write("a0", 999)
    return 4  # a python boundary returning at all is a harness bug


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


def run_trial(boundary: str) -> dict[str, Any]:
    import tgms

    work = Path(tempfile.mkdtemp(prefix=f"tgms-dur-{boundary}-"))
    store_path, acked_path = work / "store", work / "acked.txt"
    acked_path.touch()

    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, __file__, "--child", str(store_path), boundary,
         str(acked_path)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "TGMS_CRASH_POINT": ""},
    )
    rec: dict[str, Any] = {
        "boundary": boundary,
        "child_exit": proc.returncode,
        "child_died": proc.returncode not in (0, 3),
    }
    if boundary != "after_current" and boundary not in MAINT_BOUNDARIES:
        rec["crash_confirmed"] = proc.returncode not in (0, 3, 4)
    acked = [line.split() for line in acked_path.read_text().splitlines()]

    problems: list[str] = []
    try:
        store = tgms.open(store_path, backend="native")
    except Exception as e:  # noqa: BLE001 — an unopenable store is the finding
        rec["q3_single_generation"] = False
        rec["problems"] = [f"reopen failed: {type(e).__name__}: {e}"]
        rec["wall_s"] = round(time.perf_counter() - t0, 2)
        return rec

    # Q3 — verify clean
    v = store.adapter.verify()
    if v.get("problems"):
        problems.append(f"verify: {v['problems'][:3]}")
    rec["q3_single_generation"] = not v.get("problems")

    # Q1 — every acknowledged write present with its latest acknowledged value
    latest: dict[str, int] = {}
    for uid, i in acked:
        latest[uid] = int(i)
    # Write-ahead means the crash batch's log record may be durable even
    # though the caller never saw success; suffix replay then correctly
    # resurrects it. So a0 may read 999 at every boundary whose crash lies
    # past the WAL append — that is Q1's "returned implies present", not a
    # violation of it. Only the torn-append boundary must NOT show 999.
    crash_may_survive = boundary != "torn_wal_append"
    q1 = True
    for uid, want in latest.items():
        got = store.adapter.believed_node_versions(uid)
        vals_here = [g.props.get("i") for g in got]
        if uid == "a0" and crash_may_survive and vals_here == [999]:
            continue
        if len(got) != 1 or got[0].props.get("i") != want:
            q1 = False
            problems.append(
                f"acked {uid}={want} but believed={vals_here}")
    # the in-flight write is all-or-nothing
    a0 = store.adapter.believed_node_versions("a0")
    vals = {g.props.get("i") for g in a0}
    if not vals <= {latest.get("a0"), 999}:
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
    rec["wall_s"] = round(time.perf_counter() - t0, 2)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", nargs=3, metavar=("STORE", "BOUNDARY", "ACKED"))
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--boundaries", default=",".join(ALL))
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    if args.child:
        return child(*args.child)

    results = []
    for b in args.boundaries.split(","):
        for t in range(args.trials):
            r = run_trial(b)
            r["trial"] = t
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
             "manifest": {"commit": subprocess.run(
                 ["git", "rev-parse", "HEAD"], capture_output=True,
                 text=True).stdout.strip()}}, indent=1) + "\n")
        print(f"record → {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
