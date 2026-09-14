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
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgms.tools.retry_io import mkdir_with_retry, write_bytes_with_retry  # noqa: E402

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

#: Crash points *inside* `Store._recover` (Lane A EXP-A2, `tgms/store.py`),
#: armed the same TGMS_CRASH_POINT way. `py_tcsr_mid_rebuild`
#: (`tgms/storage/tcsr.py`) is deliberately excluded: it only fires from
#: inside `adapter.tcsr()`, which a bare `tgms.open()` never calls, so it
#: cannot interrupt recovery itself — it is exercised by its own dedicated
#: test instead (`tests/test_crash_during_recovery.py`).
RECOVERY_CRASH_POINTS = [
    "py_recover_after_trim",
    "py_recover_before_cursor_publish",
    "py_recover_after_cursor_publish",
    "py_recover_mid_replay",
]
#: Write boundaries used to *set up* a recovery-crash trial's starting
#: checkpoint: every CRASH_WRITE_BOUNDARIES entry except "after_current",
#: which publishes the crash batch's generation (CURRENT already flipped)
#: before the process dies — the cursor is already caught up, so a bare
#: reopen has nothing left in the suffix for the new recovery crash points
#: to interrupt.
RECOVERY_TRIAL_WRITE_BOUNDARIES = [b for b in CRASH_WRITE_BOUNDARIES if b != "after_current"]

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


def recover_child(store_path: str, boundary: str) -> int:
    """Open `store_path`, optionally armed to crash inside recovery itself.

    `boundary` is one of RECOVERY_CRASH_POINTS, or "" for an uninterrupted
    open. `Store.__init__` runs `_recover()` before this function does
    anything else, so if the named point actually fires the process is
    killed (exit 137) from inside `open()` — never returning here at all.
    If it does not fire (nothing left in the suffix for that point to reach,
    e.g. a prior attempt already advanced the cursor to the log's end),
    `open()` returns normally and this exits 0 — a legitimate, expected
    outcome, not a harness bug.
    """
    import tgms

    if boundary:
        os.environ["TGMS_CRASH_POINT"] = boundary
    store = tgms.open(store_path, backend="native")
    os.environ["TGMS_CRASH_POINT"] = ""
    store.close()
    return 0


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


def assess_q1_q3_q4(store_path: Path, acked_path: Path,
                    crash_may_survive: bool) -> tuple[dict[str, Any], list[str]]:
    """Q1 (acked survival) + Q3 (single-generation) + Q4 (orphan reclamation)
    against an already-recovered store at `store_path`. Shared between the
    single-boundary trials (`run_trial`) and the recovery-crash trials
    (`run_recovery_crash_trial`) so both interrogate a finished recovery the
    same way. Returns `(fields, problems)`; `fields` still needs `q2_*`
    (and, for recovery-crash trials, `q5_*`) added by the caller — those
    compare against digests only the caller has in scope.
    """
    import tgms
    from tgms.core.model import edge_eid

    problems: list[str] = []
    fields: dict[str, Any] = {}
    acked: list[dict[str, Any]] = [
        json.loads(line) for line in acked_path.read_text().splitlines() if line.strip()
    ]

    store = tgms.open(store_path, backend="native")

    # Q3 — verify clean
    v = store.adapter.verify()
    if v.get("problems"):
        problems.append(f"verify: {v['problems'][:3]}")
    fields["q3_single_generation"] = not v.get("problems")

    # Q1 — every acknowledged write present with its latest acknowledged value
    # (or, for a retract, absent). `latest` keeps the LAST sidecar line per
    # entity — a correction or retraction supersedes an earlier assert of the
    # same key, exactly like the store itself.
    latest: dict[tuple[str, Any], int | None] = {}
    for rec_ack in acked:
        key = (rec_ack["kind"],
              tuple(rec_ack["key"]) if isinstance(rec_ack["key"], list) else rec_ack["key"])
        latest[key] = rec_ack["i"]
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
    fields["q1_acked_survive"] = q1
    fields["crash_batch_visible"] = 999 in vals
    fields["digest"] = store.digest()
    store.close()

    # Q4 — orphan reclamation
    store = tgms.open(store_path, backend="native")
    try:
        store.adapter.compact()
        store.adapter.gc(keep_last=0)
        v = store.adapter.verify()
        tmps = list((store_path / "native").rglob("*.tmp"))
        fields["q4_orphans_reclaimed"] = not v.get("problems") and not tmps
        if v.get("problems"):
            problems.append(f"post-gc verify: {v['problems'][:2]}")
        if tmps:
            problems.append(f"tmp files survive gc: {[t.name for t in tmps]}")
    finally:
        store.close()

    return fields, problems


def run_trial(boundary: str, trial: int, seed: int | None = None) -> dict[str, Any]:
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

    # Write-ahead means the crash batch's log record may be durable even
    # though the caller never saw success; suffix replay then correctly
    # resurrects it. So a0 may read 999 at every boundary whose crash lies
    # past the WAL append — that is Q1's "returned implies present", not a
    # violation of it. Only the torn-append boundary must NOT show 999.
    crash_may_survive = boundary != "py_torn_wal_append"

    try:
        fields, problems = assess_q1_q3_q4(store_path, acked_path, crash_may_survive)
    except Exception as e:  # noqa: BLE001 — an unopenable store is the finding
        rec["q3_single_generation"] = False
        rec["q1_acked_survive"] = False
        rec["q2_deterministic"] = False
        rec["q4_orphans_reclaimed"] = False
        rec["problems"] = [f"reopen failed: {type(e).__name__}: {e}"]
        rec["notes"] = rec["problems"][0]
        rec["wall_s"] = round(time.perf_counter() - t0, 2)
        return rec
    rec.update({k: v for k, v in fields.items() if k != "digest"})

    # Q2 — deterministic recovery
    d1 = fields["digest"]
    d2 = clean_replay_digest(store_path / "eventlog.jsonl")
    rec["q2_deterministic"] = d1 == d2
    if d1 != d2:
        problems.append("recovered digest != clean replay digest")

    rec["problems"] = problems
    rec["notes"] = ("; ".join(problems) if problems else
                    ("crash batch visible via write-ahead replay" if rec["crash_batch_visible"]
                     else "clean"))
    rec["wall_s"] = round(time.perf_counter() - t0, 2)
    return rec


def run_recovery_crash_trial(trial: int, seed: int) -> dict[str, Any]:
    """A2's harness mode: build a checkpoint by crashing one write, then
    crash *recovery itself*, repeatedly, before finally letting it finish
    uninterrupted — and check the result converges no matter how it got
    there (Q1-Q4 plus Q5 convergence).

    `tt` is a hybrid-logical clock seeded from wall-clock microseconds
    (`tgms/core/clock.py`), not from `seed` — so literally re-executing the
    write phase twice would produce two logs with different transaction
    times and, correctly, two different digests; that would not be a
    recovery bug, just two different histories. Q5's "same seed reproduces
    the same digest" is therefore checked the way that is actually
    meaningful here: the write phase runs *once*, producing one on-disk
    checkpoint (log + whatever the crash left of the backend); that
    checkpoint is copied twice, and the *same* (seed-derived, deterministic)
    recovery-crash-point sequence is replayed against each copy
    independently. Convergence means both copies reach the same digest as
    each other and as a clean replay of the (shared) log — i.e. recovery's
    outcome depends only on the durable log and the crash sequence, not on
    which of two identical attempts you happened to run.
    """
    trial_seed = derive_trial_seed(seed, "recovery-crash", trial)
    rng = random.Random(trial_seed)
    write_boundary = rng.choice(RECOVERY_TRIAL_WRITE_BOUNDARIES)
    n_rounds = rng.randint(1, 4)
    crash_sequence = [rng.choice(RECOVERY_CRASH_POINTS) for _ in range(n_rounds)]

    t0 = time.perf_counter()
    work = Path(tempfile.mkdtemp(prefix="tgms-dur-recovery-crash-"))
    checkpoint, acked_path = work / "checkpoint", work / "acked.txt"
    acked_path.touch()

    # Phase 1: seed K acked batches, then crash mid-write at a random write
    # boundary — exactly `child()`'s existing shape, reused so this trial's
    # checkpoint is built the same way every other boundary trial's is.
    write_proc = subprocess.run(
        [sys.executable, __file__, "--child", str(checkpoint), write_boundary,
         str(acked_path), "--trial-seed", str(trial_seed)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "TGMS_CRASH_POINT": ""},
    )
    rec: dict[str, Any] = {
        "mode": "recovery-crash",
        "trial": trial,
        "seed": seed,
        "trial_seed": trial_seed,
        "write_boundary": write_boundary,
        "planned_rounds": n_rounds,
        "recovery_crash_sequence": crash_sequence,
        "write_child_exit": write_proc.returncode,
        "write_child_died": write_proc.returncode not in (0, 3),
    }

    crash_may_survive = write_boundary != "py_torn_wal_append"
    problems: list[str] = []
    digests: dict[str, str] = {}
    for copy_name in ("A", "B"):
        copy_path = work / f"copy_{copy_name}"
        shutil.copytree(checkpoint, copy_path)
        copy_acked = work / f"acked_{copy_name}.txt"
        copy_acked.write_bytes(acked_path.read_bytes())

        # Phase 2: crash recovery itself, up to n_rounds times, each under a
        # fresh (but pre-planned, seed-derived) recovery crash point.
        attempts = []
        for rp in crash_sequence:
            rproc = subprocess.run(
                [sys.executable, __file__, "--recover-child", str(copy_path), rp],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "TGMS_CRASH_POINT": ""},
            )
            attempts.append({"point": rp, "exit": rproc.returncode})
            if rproc.returncode not in (0, 137):
                problems.append(
                    f"copy {copy_name}: recovery child under {rp} exited "
                    f"{rproc.returncode} (stderr={rproc.stderr[-200:]!r})")
        rec[f"recovery_attempts_{copy_name}"] = attempts

        # Phase 3: finally, an uninterrupted recovery.
        final_proc = subprocess.run(
            [sys.executable, __file__, "--recover-child", str(copy_path), ""],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "TGMS_CRASH_POINT": ""},
        )
        rec[f"final_recover_exit_{copy_name}"] = final_proc.returncode
        if final_proc.returncode != 0:
            problems.append(
                f"copy {copy_name}: final uninterrupted recovery exited "
                f"{final_proc.returncode} (stderr={final_proc.stderr[-200:]!r})")
            continue

        try:
            fields, copy_problems = assess_q1_q3_q4(copy_path, copy_acked, crash_may_survive)
        except Exception as e:  # noqa: BLE001 — an unopenable store is the finding
            problems.append(f"copy {copy_name}: reopen failed: {type(e).__name__}: {e}")
            continue
        digests[copy_name] = fields["digest"]
        gate_keys = ("q1_acked_survive", "q3_single_generation", "q4_orphans_reclaimed")
        for k, v in fields.items():
            if k == "digest":
                continue
            rec[f"{k}_{copy_name}"] = v
            if k in gate_keys and not v:
                problems.append(f"copy {copy_name}: {k} failed")
        problems.extend(f"copy {copy_name}: {p}" for p in copy_problems)

    # Aggregate gate verdicts across both copies (both must pass) so the
    # summary line and --json bad-trial filter can check the same key names
    # `run_trial` uses, uniformly.
    for k in ("q1_acked_survive", "q3_single_generation", "q4_orphans_reclaimed"):
        rec[k] = bool(rec.get(f"{k}_A")) and bool(rec.get(f"{k}_B"))

    if "A" in digests:
        # Compared against copy A's *own* log, post-recovery: a torn-tail
        # checkpoint (write_boundary == "py_torn_wal_append") is only valid
        # `replay()` input once recovery has trimmed it in place, exactly
        # like `run_trial`'s Q2 compares against `store_path`'s log after
        # its own reopen, never the untouched pre-recovery checkpoint.
        d_clean = clean_replay_digest(work / "copy_A" / "eventlog.jsonl")
        rec["q2_deterministic"] = digests.get("A") == d_clean
        if digests.get("A") != d_clean:
            problems.append("copy A digest != clean replay of its own log")
    else:
        rec["q2_deterministic"] = False

    # Q5 — convergence: both independently-crashed-and-recovered copies of
    # the identical checkpoint, driven by the identical crash sequence,
    # must land on the identical digest.
    rec["q5_convergence"] = (
        "A" in digests and "B" in digests and digests["A"] == digests["B"]
    )
    if not rec["q5_convergence"]:
        problems.append(f"digests did not converge: {digests}")

    rec["problems"] = problems
    rec["notes"] = "; ".join(problems) if problems else "converged"
    rec["wall_s"] = round(time.perf_counter() - t0, 2)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", nargs=3, metavar=("STORE", "BOUNDARY", "ACKED"))
    ap.add_argument("--recover-child", nargs=2, metavar=("STORE", "BOUNDARY"),
                    help="(internal, Lane A EXP-A2) open STORE once, armed at "
                         "BOUNDARY ('' for an uninterrupted open) — the "
                         "subprocess `--recovery-crash` spawns per attempt")
    ap.add_argument("--trial-seed", type=int, default=None,
                    help="(--child only) per-trial seed for a randomized workload")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--boundaries", default=",".join(ALL))
    ap.add_argument("--recovery-crash", action="store_true",
                    help="Lane A EXP-A2: instead of the boundary matrix, run "
                         "--trials trials that crash recovery itself "
                         "(RECOVERY_CRASH_POINTS), repeatedly, before a final "
                         "uninterrupted recovery, and check Q1-Q4 plus Q5 "
                         "convergence. Requires --seed (recovery-crash-point "
                         "and round-count selection is seed-derived, not "
                         "fixed-workload).")
    ap.add_argument("--seed", type=int, default=None,
                    help="randomize the per-trial workload (default: the fixed "
                         "legacy workload); each (boundary, trial) derives its "
                         "own seed from this one, recorded in every trial record. "
                         "Required by --recovery-crash.")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    if args.child:
        return child(*args.child, trial_seed=args.trial_seed)
    if args.recover_child:
        return recover_child(*args.recover_child)

    if args.recovery_crash:
        if args.seed is None:
            ap.error("--recovery-crash requires --seed")
        results = []
        for t in range(args.trials):
            r = run_recovery_crash_trial(t, args.seed)
            results.append(r)
            ok = all(r.get(k, False) for k in
                     ("q1_acked_survive", "q2_deterministic",
                      "q3_single_generation", "q4_orphans_reclaimed",
                      "q5_convergence"))
            print(f"  recovery-crash trial {t} "
                  f"(write={r['write_boundary']}, rounds={r['planned_rounds']}, "
                  f"seq={r['recovery_crash_sequence']}): "
                  f"{'OK' if ok else 'PROBLEM ' + '; '.join(r['problems'])[:160]} "
                  f"[{r['wall_s']}s]",
                  flush=True)
        bad = [r for r in results
               if not all(r.get(k, False) for k in
                          ("q1_acked_survive", "q2_deterministic",
                           "q3_single_generation", "q4_orphans_reclaimed",
                           "q5_convergence"))]
        print(f"\n{len(results)} recovery-crash trials, {len(bad)} with problems")
        if args.json:
            mkdir_with_retry(args.json.parent)
            write_bytes_with_retry(args.json, (json.dumps(
                {"results": results,
                 "manifest": {
                     "commit": subprocess.run(
                         ["git", "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip(),
                     "seed": args.seed,
                     "mode": "recovery-crash",
                 }}, indent=1) + "\n").encode("utf-8"))
            print(f"record → {args.json}")
        return 1 if bad else 0

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
        mkdir_with_retry(args.json.parent)
        write_bytes_with_retry(args.json, (json.dumps(
            {"results": results,
             "manifest": {
                 "commit": subprocess.run(
                     ["git", "rev-parse", "HEAD"], capture_output=True,
                     text=True).stdout.strip(),
                 "seed": args.seed,
             }}, indent=1) + "\n").encode("utf-8"))
        print(f"record → {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
