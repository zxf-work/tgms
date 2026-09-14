#!/usr/bin/env python3
"""CLI for the Correction Storm benchmark (Lane C, C1+C3, plus C2's rate/
age/degree/range-width axes and end-to-end TTF, and C5's DAG/cascade;
`docs/design/CORRECTION_STORM_DESIGN_2026-09-13.md`).

    uv run python scripts/bench_correction_storm.py \\
        --store stores/collegemsg --n-artifacts 50 --batches 5 --seed 0 \\
        --mix c2 --age deep --measure-ttf end-to-end \\
        --dag-shape tree --dag-depth 4 --dag-fanout 3 \\
        --out benchmarks/storm-v1

Writes `<out>/storm-<store>-<seed>.json` (the manifest, arm summary table,
and check-cost curve — `benchmarks/schema/result_manifest.schema.json`'s
required fields, checked in this repo by `scripts/check_result_manifest.py`)
and `<out>/storm-<store>-<seed>-rows.jsonl` (one `BatchResult.to_json()` line
per batch, `manifest["record"]` names it).

**Isolation.** The named store is never written in place: it is copied into
a scratch directory first (`tgms.eval.corrections`/`bench_freshness.py`'s own
"copy the store, inject" discipline, §4.1) — a storm run registers an
artifact population and republishes every one of them every batch, which
would otherwise permanently grow a store this repo tracks.

**C5's DAG/cascade phase is optional and separate from the batch loop.**
When `--dag-shape` is given, after the correction-storm batches finish this
script also registers a real dependency DAG (`tgms.eval.storm_dag.
build_dag`) over the *same* store/registry, applies one correction to the
DAG root's own uid, refreshes the root, and runs the k-hop cascade
(`--dag-cascade-k`) — reported under the manifest's own `"dag"` key,
independent of `summary`/the per-batch rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tgms.artifact.refresh import refresh  # noqa: E402
from tgms.eval.storm import (  # noqa: E402
    ARMS, AGE_BANDS, DEGREE_BUCKETS, MIXES, RANGE_WIDTHS, TTF_MODES, Storm, build_mix, summarize,
)
from tgms.eval.corrections import _believed_nodes  # noqa: E402
from tgms.eval.storm_dag import SHAPES as DAG_SHAPES, _handle_for, build_dag, cascade  # noqa: E402
from tgms.storage.base import make_op  # noqa: E402
from tgms.storage.eventlog import extend_chain  # noqa: E402

SCHEMA_VERSION = "1.0.0"

#: §3's R-18 trip point: the median `intersects_calls`/batch crosses
#: 50,000 (design memo §3's table) somewhere between 10^4 and 10^5
#: artifacts, and a C4 burst lowers that further. `--n-artifacts` above this
#: line requires an explicit `--allow-r18-trip` — the same "named, not
#: silently absorbed" discipline the design memo itself uses for the trip.
R18_TRIP_N_ARTIFACTS = 10_000


def _git_commit() -> str:
    env = os.environ.get("TGMS_COMMIT")
    if env:
        return env
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:  # pragma: no cover - environment without git
        return "0" * 40


def _ram_gb() -> float:
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3), 2)
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        return 1.0


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_json(obj: Any) -> str:
    from tgms.core.model import canonical_json
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _print_table(store_label: str, summary: dict[str, Any]) -> None:
    print(f"\nCorrection Storm — {store_label}")
    header = (f"{'arm':<16}{'false_fresh':>12}{'false_stale':>12}{'invalidated':>12}"
             f"{'avoid_dec':>10}{'avoid_wall':>11}{'ttf_p50_ms':>12}{'ttf_p95_ms':>12}")
    print(header)
    print("-" * len(header))
    for arm in ARMS:
        row = summary["arms"].get(arm)
        if row is None:
            continue

        def fmt(x: float | None) -> str:
            return "-" if x is None else f"{x:.3f}"

        print(f"{arm:<16}{row['false_fresh']:>12}{row['false_stale']:>12}"
             f"{row['invalidated']:>12}{fmt(row['avoided_recompute_decision']):>10}"
             f"{fmt(row['avoided_recompute_wall']):>11}{fmt(row['ttf_p50_ms']):>12}"
             f"{fmt(row['ttf_p95_ms']):>12}")
    print()
    for arm in ARMS:
        row = summary["arms"].get(arm)
        if row is None:
            continue
        if arm.startswith("tgms-") and row["false_fresh"] != 0:
            print(f"** G-S1 VIOLATION: {arm} false_fresh={row['false_fresh']} (must be 0) **")


def _correct_uid(store: Any, uid: str, *, seq: int) -> None:
    """One deterministic, clock-free `correct` op against `uid` — the
    `demo_propagation.py`/`tests/test_artifact_refresh.py` `_apply` idiom,
    restated here (this script's own copy) so C5's DAG phase does not need
    a live `Storm` to inject a correction. `seq` only varies `props` so
    repeated calls (one per `--dag-shape` invocation of this script) do not
    collide on content.

    The DAG phase runs *after* the correction-storm batch loop, against the
    same, already-mutated store — so `uid` (drawn by `build_dag`'s own
    `probe_substrate` call) may be one the batch loop itself injected (a
    `new-identity` placement's `__inj...` uid), whose believed interval is
    not `[0, 50)`. The overlapping interval is read off `uid`'s own believed
    versions (`corrections.py::_believed_nodes`, the same primitive `storm.py`'s
    age axis uses) rather than assumed, so this never guesses wrong."""
    believed = _believed_nodes(store, uid)
    if not believed:
        raise RuntimeError(f"no believed node version for DAG root uid {uid!r} to correct")
    v = believed[0]
    vt_s = v.vt_s
    vt_e = vt_s + 1 if v.vt_e <= vt_s else min(v.vt_e, vt_s + 50)
    log = store.eventlog
    tt = log.last_tt() + 1
    ops = [make_op("correct", ref={"kind": "node", "uid": uid},
                   props={"injected": "dag-root-correction", "seq": seq},
                   vt_s=vt_s, vt_e=vt_e, source="inject", provenance_ref=None)]
    _batch_id, end_offset, record = log.append(tt, ops)
    note_cursor = getattr(store.adapter, "note_event_cursor", None)
    if note_cursor is not None:
        if store._chain is None:
            store._chain = log.chain_of_prefix(end_offset - len(record))
        store._chain = extend_chain(store._chain, record)
    store.adapter.begin()
    try:
        store.adapter.apply_ops(ops, tt)
    except Exception:
        store.adapter.rollback()
        raise
    if note_cursor is not None:
        note_cursor(end_offset, store._chain)
    store.adapter.commit()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", required=True, help="store directory, or a name under stores/")
    ap.add_argument("--n-artifacts", type=int, default=100)
    ap.add_argument("--batches", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--arms", nargs="*", default=list(ARMS), choices=list(ARMS))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--backend", default="native")
    ap.add_argument("--max-attempts-factor", type=int, default=4)
    ap.add_argument("--keep-work-dir", action="store_true",
                    help="do not delete the isolated store copy afterward (debugging)")
    # C2 — rate/age/degree/range-width axes (design memo §2)
    ap.add_argument("--mix", choices=list(MIXES), default=None,
                    help="correction-rate mix (c1 99/1 .. c4 burst); default: no bias, "
                         "corrections.generate()'s own unweighted output")
    ap.add_argument("--age", choices=list(AGE_BANDS), default=None,
                    help="correction-age band; implies a mix (default c1 if --mix omitted)")
    ap.add_argument("--degree", choices=list(DEGREE_BUCKETS), default=None,
                    help="affected-entity degree quantile bucket selector")
    ap.add_argument("--range-width", choices=list(RANGE_WIDTHS), default=None,
                    help="temporal range-width bucket selector")
    ap.add_argument("--burst-size", type=int, default=10_000,
                    help="C4: historical corrections bundled into one burst batch")
    ap.add_argument("--burst-after", type=int, default=10,
                    help="C4: the burst fires on the (burst-after + 1)-th mix call")
    ap.add_argument("--measure-ttf", choices=list(TTF_MODES), default="sum",
                    help="sum (default, Σ per-artifact costs) or end-to-end (actually "
                         "re-run check->refresh for the tgms arms, timed as one interval)")
    ap.add_argument("--allow-r18-trip", action="store_true",
                    help=f"permit --n-artifacts above {R18_TRIP_N_ARTIFACTS} (design memo "
                         "§3's R-18 trip point) — the campaign's 10^5 cells need this")
    # C5 — DAG generator + k-hop cascade (design memo §4), a separate phase
    ap.add_argument("--dag-shape", choices=list(DAG_SHAPES), default=None,
                    help="also build and cascade-test a dependency DAG of this shape "
                         "over the same store/registry, after the batch loop")
    ap.add_argument("--dag-depth", type=int, default=4)
    ap.add_argument("--dag-fanout", type=int, default=3)
    ap.add_argument("--dag-cascade-k", type=int, default=4)
    args = ap.parse_args(argv)

    if args.n_artifacts > R18_TRIP_N_ARTIFACTS and not args.allow_r18_trip:
        ap.error(f"--n-artifacts {args.n_artifacts} is above the R-18 trip point "
                f"({R18_TRIP_N_ARTIFACTS}); pass --allow-r18-trip to proceed anyway "
                f"(design memo §3)")

    store_path = Path(args.store)
    if not store_path.exists():
        store_path = ROOT / "stores" / args.store
    if not store_path.exists():
        ap.error(f"no such store: {args.store}")
    store_label = store_path.name

    work_root = Path(tempfile.mkdtemp(prefix="storm-"))
    work_store = work_root / "store"
    shutil.copytree(store_path, work_store)

    mix = None
    if any((args.mix, args.age, args.degree, args.range_width)):
        mix = build_mix(args.mix or "c1", burst_size=args.burst_size,
                        burst_after=args.burst_after, age=args.age, degree=args.degree,
                        range_width=args.range_width)

    t_start = time.time()
    storm = Storm(work_store, n_artifacts=args.n_artifacts, seed=args.seed,
                 backend=args.backend, arms=tuple(args.arms), mix=mix,
                 measure_ttf=args.measure_ttf)
    n_registered = len(storm.artifacts)
    n_skipped = storm.n_registration_skipped
    results = storm.run(args.batches, max_attempts_factor=args.max_attempts_factor)
    wall_s = time.time() - t_start

    dag_payload: dict[str, Any] | None = None
    if args.dag_shape is not None:
        t_dag = time.time()
        dag_info = build_dag(storm.registry, storm.store, args.dag_shape, args.dag_depth,
                             args.dag_fanout, args.seed, name_prefix=f"storm-dag-{store_label}")
        root = dag_info.nodes[0]
        _correct_uid(storm.store, root.uid, seq=args.seed)
        root_record = storm.registry.current(root.name)
        root1 = refresh(root_record, _handle_for(root_record), storm.store, storm.registry)
        cascade_result = cascade(storm.registry, storm.store, root1.id, args.dag_cascade_k)
        dag_payload = {
            "shape": args.dag_shape, "depth": args.dag_depth, "fanout": args.dag_fanout,
            "cascade_k": args.dag_cascade_k, "root": root.name, "root_uid": root.uid,
            "info": dag_info.to_json(), "cascade": cascade_result.to_json(),
            "wall_s": time.time() - t_dag,
        }
        if cascade_result.false_safe:
            print(f"** G-S2 VIOLATION: cascade false_safe={list(cascade_result.false_safe)} "
                 f"(must be empty) **")

    eventlog_digest = _sha256_file(storm.store.eventlog.path)
    interval_vt = storm.interval_vt
    storm.close()

    if not args.keep_work_dir:
        shutil.rmtree(work_root, ignore_errors=True)

    args.out.mkdir(parents=True, exist_ok=True)
    base = f"storm-{store_label}-{args.seed}"
    rows_path = args.out / f"{base}-rows.jsonl"
    with open(rows_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.to_json(), sort_keys=True) + "\n")

    summary = summarize(results)
    rows_json = [r.to_json() for r in results]

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "git_commit": _git_commit(),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": {"host": socket.gethostname(), "platform": platform.platform(),
                    "cpus": os.cpu_count() or 1, "ram_gb": _ram_gb()},
        "config": {"store": str(args.store), "n_artifacts": args.n_artifacts,
                  "batches": args.batches, "seed": args.seed, "arms": list(args.arms),
                  "backend": args.backend, "n_registered": n_registered,
                  "n_registration_skipped": n_skipped, "wall_s": wall_s,
                  "mix": args.mix, "age": args.age, "degree": args.degree,
                  "range_width": args.range_width, "burst_size": args.burst_size,
                  "burst_after": args.burst_after, "measure_ttf": args.measure_ttf,
                  "allow_r18_trip": args.allow_r18_trip, "interval_vt": interval_vt,
                  "dag_shape": args.dag_shape, "dag_depth": args.dag_depth,
                  "dag_fanout": args.dag_fanout, "dag_cascade_k": args.dag_cascade_k},
        "seed": {"value": args.seed},
        "dataset": {"name": store_label, "digest": eventlog_digest,
                   "digest_kind": "eventlog_sha"},
        "result_digest": _sha256_json(rows_json),
        "protocol": {"warmups": 0, "reps": 1, "ceilings": {
            "r18_trip_n_artifacts": R18_TRIP_N_ARTIFACTS}},
        "record": str(rows_path.relative_to(ROOT)) if rows_path.is_relative_to(ROOT)
        else str(rows_path),
        "summary": summary,
        "dag": dag_payload,
    }
    manifest_path = args.out / f"{base}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    print(f"wrote {manifest_path}")
    print(f"wrote {rows_path}")
    _print_table(store_label, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
